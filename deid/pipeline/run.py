"""Orchestration: scan, plan, rewrite over the manifest with a process pool."""
from __future__ import annotations

import collections
import json
import os
import sys
import time

from deid import api
from deid.pipeline import core, db
from deid.pipeline.pool import Pool

GB = 1 << 30


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def load_manifest(manifest: str, input_root: str, only_prefix: str = "", limit: int = 0,
                  skip_missing: bool = False):
    """Rows: dicts path,size,md5. Paths are taken verbatim (case-sensitive, no decoding)."""
    rows, missing = [], 0
    with open(manifest, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            path, size, md5 = parts[0], int(parts[1]), parts[2] if len(parts) > 2 else ""
            if only_prefix and not path.startswith(only_prefix):
                continue
            if skip_missing:
                fp = os.path.join(input_root, path)
                # absent, or still downloading (size differs from the manifest)
                if not os.path.isfile(fp) or os.path.getsize(fp) != size:
                    missing += 1
                    continue
            rows.append({"path": path, "size": size, "md5": md5})
            if limit and len(rows) >= limit:
                break
    return rows, missing


def safe_join(root: str, rel: str) -> str:
    p = os.path.normpath(os.path.join(root, rel))
    if not (p == root or p.startswith(root.rstrip("/") + "/")):
        raise ValueError(f"path escapes root: {rel!r}")
    return p


def load_plan(work: str, plan_dir: str = None):
    plan_dir = plan_dir or os.path.join(work, "plan")
    if os.path.isdir(plan_dir):
        try:
            from deid.identity import load_plan as lp  # owned by identity
            return lp(plan_dir)
        except ImportError:
            pass
    return api.NullPlan()


class Progress:
    def __init__(self, total, label, total_bytes=0):
        self.total, self.label, self.total_bytes = total, label, total_bytes
        self.n = self.bytes = 0
        self.t0 = self.last = time.monotonic()
        self.status = collections.Counter()

    def tick(self, status, size=0):
        self.n += 1
        self.bytes += size
        self.status[status] += 1
        now = time.monotonic()
        if now - self.last > 30 or self.n == self.total:
            self.last = now
            el = now - self.t0
            print(f"[{self.label}] {self.n}/{self.total} files {self.bytes / 1e9:.2f} GB "
                  f"{self.n / el:.1f} f/s {self.bytes / 1e6 / el:.1f} MB/s "
                  f"{dict(self.status.most_common(8))}", file=sys.stderr, flush=True)


def _route_head(proc: core.Processor, path, src):
    ctx, head = proc.make_ctx(path, src)
    a = api.route(ctx, head) if ctx.magic != "empty" else None
    return ctx, head, a


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def _scan_init(tmp_root):
    api.load_all()
    proc = core.Processor(tmp_root=tmp_root)

    def handle(p):
        src = p["src"]
        sha = core.file_hash(src)
        ctx, head = proc.make_ctx(p["path"], src, sha=sha)
        name, ver, res, err, secs = proc.scan(ctx, src, head)
        rows = core.scan_to_rows(p["path"], res)
        return {"sha256": sha, "magic": ctx.magic, "size": ctx.size, "adapter": name,
                "adapter_version": ver, "status": res.status, "error": err, "seconds": secs,
                "fingerprint": json.dumps(res.fingerprint, default=str, sort_keys=True),
                "notes": json.dumps(res.notes, default=str), **rows}
    return handle


def _store_scan(con, path, r, parent=None):
    db.delete_scan_rows(con, path)
    con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, r["size"], r["sha256"], r["magic"], r["adapter"], r["adapter_version"],
                 r["status"], r["fingerprint"], r["notes"], r["error"], r["seconds"], parent))
    con.executemany("INSERT INTO observations VALUES(?,?,?,?,?,?)", r["obs"])
    con.executemany("INSERT INTO refs VALUES(?,?,?,?)", r["refs"])
    for cp, st, fp, notes in r["children"]:
        con.execute("INSERT OR REPLACE INTO files(path,status,fingerprint,notes,parent) VALUES(?,?,?,?,?)",
                    (cp, st, fp, notes, path))


def _retarget(r, old, new):
    """Copy of a scan result with member paths re-prefixed from old to new."""
    def fix(p):
        return new + p[len(old):] if p.startswith(old) else p
    r2 = dict(r)
    r2["obs"] = [(fix(o[0]),) + tuple(o[1:]) for o in r["obs"]]
    r2["refs"] = [(fix(o[0]),) + tuple(o[1:]) for o in r["refs"]]
    r2["children"] = [(fix(c[0]),) + tuple(c[1:]) for c in r["children"]]
    return r2


def cmd_scan(args):
    rows, missing = load_manifest(args.manifest, args.input, args.only_prefix, args.limit,
                                  args.skip_missing)
    os.makedirs(args.work, exist_ok=True)
    tmp_root = os.path.join(args.work, "tmp")
    os.makedirs(tmp_root, exist_ok=True)
    errs = api.load_all()
    for m, e in errs:
        print(f"adapter import error {m}: {e}", file=sys.stderr)
    con = db.scan_db(args.work)
    have = {p: (av, a, st, sz) for p, a, av, st, sz in
            con.execute("SELECT path, adapter, adapter_version, status, size FROM files WHERE parent IS NULL")}
    proc = core.Processor()
    todo, dups, skipped = [], collections.defaultdict(list), 0
    rep_by_md5 = {}
    for r in sorted(rows, key=lambda r: -r["size"]):
        src = safe_join(args.input, r["path"])
        if not os.path.isfile(src):
            continue
        _, _, a = _route_head(proc, r["path"], src)
        cur = (a.version if a else "", a.name if a else "")
        h = have.get(r["path"])
        if h and not args.force and h[2] != "error" and (h[0], h[1]) == cur and h[3] == r["size"]:
            skipped += 1
            continue
        key = (r["md5"], r["size"]) if r["md5"] and r["size"] > 0 else None
        if key and key in rep_by_md5:
            dups[rep_by_md5[key]].append(r)
            continue
        if key:
            rep_by_md5[key] = r["path"]
        todo.append((r["path"], {"path": r["path"], "src": src},
                     core.timeout_for(a, r["size"]) * 1.5 + 30))
    ndup = sum(len(v) for v in dups.values())
    print(f"scan: {len(rows)} selected, {missing} missing, {skipped} up to date, "
          f"{len(todo)} to scan, {ndup} duplicates reuse a scan", file=sys.stderr, flush=True)
    prog = Progress(len(todo) + ndup, "scan")
    sizes = {r["path"]: r["size"] for r in rows}
    pool = Pool(args.jobs, _scan_init, (tmp_root,), args.mem_gb * GB)
    n = 0
    try:
        for path, payload, res in pool.run(todo):
            if "_pool_error" in res:
                res = {"sha256": "", "magic": "", "size": sizes[path], "adapter": "", "adapter_version": "",
                       "status": "error", "error": res["_pool_error"], "seconds": 0,
                       "fingerprint": "{}", "notes": "[]", "obs": [], "refs": [], "children": []}
            _store_scan(con, path, res)
            prog.tick(res["status"] + (":" + res["adapter"] if res["adapter"] else ""), sizes[path])
            for d in dups.get(path, []):
                _store_scan(con, d["path"], _retarget(res, path, d["path"]))
                prog.tick("dup", d["size"])
            n += 1
            if n % 200 == 0:
                con.commit()
    finally:
        con.commit()
        pool.close()
    return 0


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def cmd_plan(args):
    try:
        from deid.identity import build_plan
    except ImportError as e:
        print(f"deid.identity.build_plan not available ({e}); rewrite will use NullPlan",
              file=sys.stderr)
        return 1
    build_plan(os.path.join(args.work, "scan.sqlite"), args.manifest, os.path.join(args.work, "plan"))
    return 0


# ---------------------------------------------------------------------------
# Rewrite
# ---------------------------------------------------------------------------
def _rewrite_init(tmp_root, work, plan_dir):
    api.load_all()
    plan = load_plan(work, plan_dir)
    proc = core.Processor(plan=plan, tmp_root=tmp_root)

    def handle(p):
        ctx, head = proc.make_ctx(p["path"], p["src"], sha=p.get("sha256", ""))
        r = proc.rewrite(ctx, p["src"], p["dst"], head)
        r["magic"] = ctx.magic
        r["out_size"] = os.path.getsize(p["dst"]) if os.path.exists(p["dst"]) else -1
        return r
    return handle


def compute_out_paths(rows, plan):
    out, rev, collisions = {}, {}, []
    for r in rows:
        o = plan.map_path(r["path"])
        if o in rev:
            collisions.append((rev[o], r["path"]))
        out[r["path"]] = o
        rev[o] = r["path"]
    return out, collisions


def cmd_rewrite(args):
    rows, missing = load_manifest(args.manifest, args.input, args.only_prefix, args.limit,
                                  args.skip_missing)
    corpus = os.path.join(args.out, "corpus")
    os.makedirs(corpus, exist_ok=True)
    tmp_root = os.path.join(args.work, "tmp")
    os.makedirs(tmp_root, exist_ok=True)
    api.load_all()
    plan = load_plan(args.work, args.plan)
    out_paths, collisions = compute_out_paths(rows, plan)
    if collisions:
        print(f"FATAL: {len(collisions)} output path collisions (see work/collisions.tsv)", file=sys.stderr)
        with open(os.path.join(args.work, "collisions.tsv"), "w") as f:
            for a, b in collisions:
                f.write(f"{a}\t{b}\n")
        return 2
    scon = db.scan_db(args.work)
    shas = dict(scon.execute("SELECT path, sha256 FROM files WHERE parent IS NULL"))
    rcon = db.results_db(args.work)
    prev = {p: (k, o, s) for p, k, o, s in rcon.execute("SELECT path, key, out_path, out_size FROM results")}
    proc = core.Processor(plan=plan)
    todo, deferred, skipped = [], collections.defaultdict(list), 0
    reps = {}
    meta = {}
    for r in sorted(rows, key=lambda r: -r["size"]):
        src = safe_join(args.input, r["path"])
        if not os.path.isfile(src):
            continue
        dst = safe_join(corpus, out_paths[r["path"]])
        ctx, head, a = _route_head(proc, r["path"], src)
        sha = shas.get(r["path"]) or ""
        if not sha:
            sha = core.file_hash(src)
        pi = bool(a and a.path_independent)
        key = "|".join([sha, a.name if a else "-", a.version if a else "-", plan.version,
                        out_paths[r["path"]], "" if pi else r["path"]])
        meta[r["path"]] = (key, out_paths[r["path"]], r["size"], src, dst)
        p = prev.get(r["path"])
        if (p and not args.force and p[0] == key and p[1] == out_paths[r["path"]]
                and os.path.exists(dst) and os.path.getsize(dst) == p[2]):
            skipped += 1
            continue
        dkey = (sha, a.name, a.version) if (pi and a and r["size"] > 0) else None
        if dkey and dkey in reps:
            deferred[reps[dkey]].append(r["path"])
            continue
        if dkey:
            reps[dkey] = r["path"]
        todo.append((r["path"], {"path": r["path"], "src": src, "dst": dst, "sha256": sha},
                     core.timeout_for(a, r["size"]) * 1.5 + 60))
    ndef = sum(len(v) for v in deferred.values())
    print(f"rewrite: plan {plan.version}; {len(rows)} selected, {missing} missing, {skipped} up to date, "
          f"{len(todo)} to process, {ndef} identical copies", file=sys.stderr, flush=True)
    prog = Progress(len(todo) + ndef, "rewrite")
    pool = Pool(args.jobs, _rewrite_init, (tmp_root, args.work, args.plan), args.mem_gb * GB)
    n = 0

    def store(path, res):
        key, o, size, src, dst = meta[path]
        rcon.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (path, o, key, res["status"], res.get("adapter", ""), res.get("adapter_version", ""),
                      json.dumps(res.get("counts", {}), default=str),
                      json.dumps(res.get("notes", []), default=str), res.get("error"),
                      res.get("out_size", -1), res.get("seconds", 0),
                      json.dumps(res.get("children", []), default=str), res.get("magic", ""), size))
        prog.tick(res["status"], size)
    try:
        for path, payload, res in pool.run(todo):
            if "_pool_error" in res:
                core.fastcopy(payload["src"], payload["dst"])
                res = {"status": api.FALLBACK_COPY, "error": "pool:" + res["_pool_error"],
                       "out_size": os.path.getsize(payload["dst"])}
            store(path, res)
            for d in deferred.get(path, []):
                core.fastcopy(payload["dst"], meta[d][4])
                r2 = dict(res)
                r2["notes"] = list(res.get("notes", [])) + ["identical content reused"]
                r2["out_size"] = os.path.getsize(meta[d][4])
                store(d, r2)
            n += 1
            if n % 200 == 0:
                rcon.commit()
    finally:
        rcon.commit()
        pool.close()
    rcon.execute("INSERT OR REPLACE INTO meta VALUES('plan_version', ?)", (plan.version,))
    try:
        import subprocess
        sha = subprocess.run(["git", "-C", os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                              "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        rcon.execute("INSERT OR REPLACE INTO meta VALUES('git_sha', ?)", (sha,))
    except OSError:
        pass
    rcon.commit()
    if not args.only_prefix and not args.limit:
        removed = remove_orphans(corpus, {m[1] for m in meta.values()})
        print(f"rewrite: removed {removed} stale outputs", file=sys.stderr)
    return 0


def remove_orphans(corpus, expected):
    removed = 0
    for root, dirs, files in os.walk(corpus):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, corpus)
            if rel not in expected:
                os.unlink(full)
                removed += 1
    for root, dirs, files in os.walk(corpus, topdown=False):
        if root != corpus and not os.listdir(root):
            os.rmdir(root)
    return removed


# ---------------------------------------------------------------------------
# One file (debug)
# ---------------------------------------------------------------------------
def cmd_one(args):
    import tempfile
    errs = api.load_all()
    for m, e in errs:
        print(f"adapter import error {m}: {e}")
    plan = load_plan(args.work or "", args.plan) if (args.work or args.plan) else api.NullPlan()
    tmp = tempfile.mkdtemp(prefix="deid-one-")
    proc = core.Processor(plan=plan, tmp_root=tmp)
    for f in args.files:
        logical = args.logical or os.path.basename(f)
        ctx, head = proc.make_ctx(logical, f)
        a = api.route(ctx, head)
        if args.adapter and (a is None or a.name != args.adapter):
            forced = [x for x in api.adapters() if x.name == args.adapter]
            print(f"note: router chose {a.name if a else None}; forcing {args.adapter}")
            a = forced[0] if forced else None
            if a:
                api._REGISTRY.insert(0, a)  # debug only: make it win routing
        print(f"== {f}\n magic={ctx.magic} adapter={a.name if a else None}")
        name, ver, res, err, secs = proc.scan(ctx, f, head)
        print(f" scan: status={res.status} err={err} {secs:.2f}s obs={len(res.observations)} "
              f"refs={len(res.references)} children={len(res.children)}")
        for o in res.observations[: args.show]:
            print(f"   obs {o.kind:12} {o.field:30} {o.value[:60]!r}")
        for x in res.references[: args.show]:
            print(f"   ref {x.kind:8} {x.raw[:80]!r}")
        print(f"   fingerprint {json.dumps(res.fingerprint, default=str)[:300]}")
        dst = os.path.join(tmp, "out", os.path.basename(f))
        r = proc.rewrite(ctx, f, dst, head)
        print(f" rewrite: status={r['status']} err={r['error']} {r['seconds']:.2f}s counts={r['counts']} "
              f"notes={r['notes'][:5]}")
        print(f" out: {dst} ({os.path.getsize(dst)} bytes)")
    return 0
