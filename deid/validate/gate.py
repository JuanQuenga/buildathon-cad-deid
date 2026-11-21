"""Local acceptance gate for an attempt (run before any upload).

Checks
  1. coverage: every input has exactly one output at plan.map_path(input), no collisions,
     same segment count, extension(s) preserved, no stray/temp files, no symlinks
  2. limits: no object >= 2 GB, <= 400,000 objects
  3. statuses per adapter / extension (from results.sqlite)
  4. reference graph: references that resolved before still resolve to the mapped target
  5. leak scan: every plan identifier (several encodings, case-insensitive) in output
     paths, bytes and decompressed streams (zip/gzip/.Z members, PDF Flate streams)

Writes private details to WORK/validate/ and a public-safe summary (counts only) to
WORK/validate/summary.txt for README.txt. Exit code 1 on hard failures.
"""
from __future__ import annotations

import collections
import gzip
import io
import json
import os
import re
import sys
import time
import zipfile
import zlib

from deid import api
from deid.pipeline import db, run
from deid.pipeline.pool import Pool

HARD_SIZE = 2_000_000_000         # "2 GB": use the decimal reading to be safe
MAX_OBJECTS = 400_000
MIN_PATTERN = 4
BOUNDARY_BELOW = 8                # identifiers shorter than this must stand alone (not inside a word)
_ALNUM = set("abcdefghijklmnopqrstuvwxyz0123456789")
SCAN_CAP = 512 << 20              # bytes per view in leak scan
VIEW_DEPTH = 3

VERSION_RE = re.compile(r"\.\d+$")


def suffix_of(name: str) -> str:
    """Protected suffix: last extension plus Creo version number and .tar.gz."""
    base = name.rsplit("/", 1)[-1]
    suf = ""
    m = VERSION_RE.search(base)
    if m and "." in base[: m.start()]:
        suf = m.group(0)
        base = base[: m.start()]
    i = base.rfind(".")
    if i > 0:
        suf = base[i:] + suf
        if base[:i].lower().endswith(".tar"):
            suf = base[i - 4:i] + suf
    return suf


# ---------------------------------------------------------------------------
# Leak scan
# ---------------------------------------------------------------------------
def build_patterns(plan):
    """[(needle_bytes_lower, entry_index, encoding)] from the plan's identifier list."""
    values = []
    ents = getattr(plan, "entries", None)
    if isinstance(ents, dict):
        ents = list(ents.values())
    values = [(getattr(e, "value", e), getattr(e, "kind", ""), getattr(e, "mode", "")) for e in (ents or [])]
    pats, skipped = {}, 0
    for idx, (v, kind, mode) in enumerate(values):
        # "field" entries are generic words the plan only replaces in identity slots
        if not v or len(v) < MIN_PATTERN or mode == "field":
            skipped += 1
            continue
        variants = {v}
        if " " in v:
            variants |= {v.replace(" ", "_"), v.replace(" ", "-"), v.replace(" ", "%20"), v.replace(" ", "")}
        for var in variants:
            for enc in ("utf-8", "utf-16-le", "utf-16-be"):
                try:
                    b = var.lower().encode(enc)
                except UnicodeEncodeError:
                    continue
                pats.setdefault(b, (idx, enc, kind))
    return pats, values, skipped


class LeakScanner:
    def __init__(self, pats):
        import ahocorasick
        self.A = ahocorasick.Automaton(ahocorasick.STORE_ANY, ahocorasick.KEY_SEQUENCE)
        self.maxlen = 1
        for b, meta in pats.items():
            self.A.add_word(tuple(b), (len(b), meta))
            self.maxlen = max(self.maxlen, len(b))
        self.empty = not pats
        if not self.empty:
            self.A.make_automaton()

    def scan_bytes(self, data: bytes):
        if self.empty or not data:
            return set()
        hits = set()
        # pyahocorasick KEY_SEQUENCE works on sequences of ints; use latin-1 str for speed
        return self._scan_str(data.lower().decode("latin-1"))

    def _scan_str(self, s):
        hits = set()
        n = len(s)
        for end, (ln, meta) in self.A.iter(s):
            enc = meta[1]
            step = 1 if enc == "utf-8" else 2
            if ln // step < BOUNDARY_BELOW:
                start = end - ln + 1
                if enc == "utf-16-be":
                    before, after = start - 1, end + 2
                else:
                    before, after = start - step, end + 1
                if (before >= 0 and s[before] in _ALNUM) or (after < n and s[after] in _ALNUM):
                    continue
            hits.add(meta[0])
        return hits


class StrLeakScanner(LeakScanner):
    """Same automaton built on latin-1 strings (much faster than int tuples)."""

    def __init__(self, pats):
        import ahocorasick
        self.A = ahocorasick.Automaton()
        self.maxlen = 1
        for b, meta in pats.items():
            self.A.add_word(b.decode("latin-1"), (len(b), meta))
            self.maxlen = max(self.maxlen, len(b))
        self.empty = not pats
        if not self.empty:
            self.A.make_automaton()

    def scan_file(self, path, cap=None):
        hits = set()
        tail = b""
        n = 0
        with open(path, "rb") as f:
            while True:
                b = f.read(16 << 20)
                if not b:
                    break
                n += len(b)
                hits |= self.scan_bytes(tail + b)
                tail = b[-self.maxlen:]
                if cap and n >= cap:
                    break
        return hits


def _views(path, data_head, depth=0):
    """Yield (label, bytes) decompressed views of a file (bounded)."""
    if depth > VIEW_DEPTH:
        return
    magic = api.sniff(data_head, os.path.basename(path) if isinstance(path, str) else "")
    try:
        if magic == "zip":
            with zipfile.ZipFile(path) as z:
                for i in z.infolist():
                    if i.is_dir() or i.file_size > SCAN_CAP or i.flag_bits & 1:
                        continue
                    try:
                        b = z.read(i)
                    except Exception:
                        continue
                    yield f"zip:{i.filename}", b
                    yield from _views(io.BytesIO(b), b[:api.HEAD_BYTES], depth + 1)
        elif magic == "gzip":
            f = gzip.open(path) if isinstance(path, str) else gzip.GzipFile(fileobj=path)
            b = f.read(SCAN_CAP)
            yield "gzip", b
            if b[257:262] == b"ustar" or (len(b) > 512 and _maybe_tar(b)):
                yield from _tar_views(b, depth)
        elif magic == "compress":
            from deid.formats.archives import lzw_decompress
            raw = open(path, "rb").read() if isinstance(path, str) else path.getvalue()
            b = lzw_decompress(raw)
            yield "Z", b
        elif magic == "tar":
            raw = open(path, "rb").read(SCAN_CAP) if isinstance(path, str) else path.getvalue()
            yield from _tar_views(raw, depth)
        elif magic == "pdf":
            raw = open(path, "rb").read(SCAN_CAP) if isinstance(path, str) else path.getvalue()
            for m in re.finditer(rb"stream\r?\n", raw):
                d = zlib.decompressobj()
                try:
                    b = d.decompress(raw[m.end():m.end() + (64 << 20)], 64 << 20)
                except zlib.error:
                    continue
                if b:
                    yield "pdfstream", b
    except Exception:
        return


def _maybe_tar(b):
    from deid.formats.archives import _is_tar_header
    return _is_tar_header(b[:512])


def _tar_views(raw, depth):
    import tarfile
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as t:
            for ti in t:
                if ti.isreg() and ti.size <= SCAN_CAP:
                    b = t.extractfile(ti).read()
                    yield f"tar:{ti.name}", b
                    yield from _views(io.BytesIO(b), b[:api.HEAD_BYTES], depth + 1)
    except Exception:
        return


def _leak_init(pats, mode):
    sc = StrLeakScanner(pats)

    def handle(p):
        path = p["path"]
        out = {"raw": [], "views": [], "path_hits": []}
        out["path_hits"] = sorted(sc.scan_bytes(p["rel"].encode("utf-8")))
        try:
            out["raw"] = sorted(sc.scan_file(path))
            if mode == "full":
                with open(path, "rb") as f:
                    head = f.read(api.HEAD_BYTES)
                vh = collections.defaultdict(set)
                for label, b in _views(path, head):
                    for h in sc.scan_bytes(b):
                        vh[h].add(label.split(":", 1)[0])
                out["views"] = sorted((h, sorted(v)) for h, v in vh.items())
        except Exception as e:
            out["error"] = type(e).__name__
        return out
    return handle


# ---------------------------------------------------------------------------
# Reference graph
# ---------------------------------------------------------------------------
class _OutView:
    """Minimal plan-like object over output paths for refs.resolve()."""

    def __init__(self, inventory):
        self.inventory = list(inventory) + [p.replace("!/./", "!/") for p in inventory if "!/./" in p]
        self._inv_index = None

    def _index(self):
        if self._inv_index is None:
            full, base = {}, {}
            for p in self.inventory:
                full.setdefault(p.lower(), p)
                base.setdefault(p.rsplit("/", 1)[-1].lower(), []).append(p)
            self._inv_index = (full, base)
        return self._inv_index


def check_refs(plan, scon, out_paths, rep):
    try:
        from deid.identity import refs as R
    except ImportError:
        rep["refs"] = {"skipped": "identity.refs unavailable"}
        return
    members = [p for (p,) in scon.execute("SELECT path FROM files WHERE parent IS NOT NULL")]
    inv = getattr(plan, "inventory", None) or list(out_paths)   # the resolver's universe (full manifest)
    view = _OutView([plan.map_path(p) for p in inv] + [plan.map_path(m) for m in members])
    c = collections.Counter()
    bad = []
    for path, raw, kind in scon.execute("SELECT path, raw, kind FROM refs"):
        top = path.split("!/", 1)[0]
        if top not in out_paths:
            continue
        try:
            tgt = plan.resolve_reference(path, raw)
        except Exception:
            c["resolver_error"] += 1
            continue
        if not tgt:
            c["unresolved_before"] += 1
            continue
        c["resolved_before"] += 1
        new_raw = plan.map_reference(api.FileContext(logical_path=path), raw)
        out_ctx = plan.map_path(path)
        try:
            got = R.resolve(view, out_ctx, new_raw)
        except Exception:
            got = None
        want = plan.map_path(tgt)
        if got and got.replace("!/./", "!/") == want.replace("!/./", "!/"):
            got = want.replace("!/./", "!/")
        if got == want:
            c["resolved_after_ok"] += 1
        else:
            c["broken_after"] += 1
            bad.append((path, raw, new_raw, tgt, want, got))
    rep["refs"] = dict(c)
    return bad


# ---------------------------------------------------------------------------
def cmd_validate(args):
    t0 = time.time()
    vdir = os.path.join(args.work, "validate")
    os.makedirs(vdir, exist_ok=True)
    rows, missing = run.load_manifest(args.manifest, args.input, args.only_prefix, args.limit,
                                      args.skip_missing)
    plan = run.load_plan(args.work, args.plan)
    corpus = os.path.join(args.out, "corpus")
    rep = {"plan_version": plan.version, "inputs": len(rows), "inputs_missing_on_disk": missing}
    hard = []

    # 1. coverage --------------------------------------------------------
    out_paths, collisions = run.compute_out_paths(rows, plan)
    rcon = db.results_db(args.work)
    res = {p: (o, st, a, sz) for p, o, st, a, sz in
           rcon.execute("SELECT path, out_path, status, adapter, out_size FROM results")}
    cov = collections.Counter()
    problems = []
    lower_seen = {}
    for r in rows:
        p, o = r["path"], out_paths[r["path"]]
        full = os.path.join(corpus, o)
        if not os.path.isfile(full) or os.path.islink(full):
            cov["missing_output"] += 1
            problems.append(("missing_output", p, o))
            continue
        sz = os.path.getsize(full)
        if r["size"] > 0 and sz == 0:
            cov["empty_output"] += 1
            problems.append(("empty_output", p, o))
        if p.count("/") != o.count("/"):
            cov["segment_count_changed"] += 1
            problems.append(("segments", p, o))
        if suffix_of(p) != suffix_of(o):
            cov["suffix_changed"] += 1
            problems.append(("suffix", p, o))
        if p not in res or res[p][0] != o:
            cov["no_result_record"] += 1
        if o != p:
            cov["renamed"] += 1
        lo = o.lower()
        if lo in lower_seen and lower_seen[lo] != o:
            cov["case_insensitive_collision"] += 1
        lower_seen[lo] = o
        cov["ok"] += 1
    cov["collisions"] = len(collisions)
    rep["coverage"] = dict(cov)
    for k in ("missing_output", "empty_output", "segment_count_changed", "suffix_changed", "collisions"):
        if cov.get(k):
            hard.append(f"{k}={cov[k]}")

    # 2. limits / stray -----------------------------------------------------
    expected = set(out_paths.values())
    lim = collections.Counter()
    for root, dirs, files in os.walk(corpus):
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                lim["symlinks"] += 1
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, corpus)
            lim["objects"] += 1
            if os.path.islink(full):
                lim["symlinks"] += 1
                continue
            sz = os.path.getsize(full)
            lim["bytes"] += sz
            if sz >= HARD_SIZE:
                lim["over_2GB"] += 1
                problems.append(("over_2GB", "", rel))
            if rel.endswith((".deid-tmp", ".deid-new")):
                lim["temp_files"] += 1
            elif rel not in expected:
                lim["unexpected_files"] += 1
                problems.append(("unexpected", "", rel))
    rep["limits"] = dict(lim)
    if lim["objects"] > MAX_OBJECTS:
        hard.append(f"objects={lim['objects']}")
    for k in ("symlinks", "over_2GB", "temp_files"):
        if lim.get(k):
            hard.append(f"{k}={lim[k]}")
    if lim.get("unexpected_files") and not (args.only_prefix or args.limit or args.skip_missing):
        hard.append(f"unexpected_files={lim['unexpected_files']}")
    for d in ("code", "README.txt"):
        rep.setdefault("layout", {})[d] = os.path.exists(os.path.join(args.out, d))

    # 3. statuses -----------------------------------------------------------
    st = collections.Counter()
    by_ext = collections.defaultdict(collections.Counter)
    for p, (o, s, a, sz) in res.items():
        if p in out_paths:
            st[f"{a or '-'}:{s}"] += 1
            by_ext[suffix_of(p).lower().split(".")[-1] if suffix_of(p) else "(none)"][s] += 1
    rep["status_by_adapter"] = dict(sorted(st.items()))
    rep["status_by_ext"] = {k: dict(v) for k, v in sorted(by_ext.items())}
    exc = []
    for p, o, s, notes in rcon.execute(
            "SELECT path, out_path, status, notes FROM results WHERE status IN ('source_exception','fallback_copy')"):
        if p in out_paths:
            exc.append((o, s, notes))
    rep["exceptions"] = len(exc)

    # 3b. alias consistency -----------------------------------------------------
    ents = getattr(plan, "entries", None)
    if isinstance(ents, dict):
        seen, ac = {}, collections.Counter()
        for k, e in ents.items():
            try:
                a1 = plan.alias(e.value, e.kind)
                a2 = plan.alias(e.value, e.kind)
            except Exception:
                ac["alias_error"] += 1
                continue
            if a1 is None:
                ac["no_alias"] += 1
                continue
            if a1 != a2:
                ac["nondeterministic"] += 1
            if a1.lower() == e.value.lower():
                ac["alias_equals_original"] += 1
            if a1.lower() in seen and seen[a1.lower()] != k:
                ac["shared_alias"] += 1
            seen[a1.lower()] = k
            ac["aliased"] += 1
        rep["aliases"] = dict(ac)
        for k in ("nondeterministic", "shared_alias"):
            if ac.get(k):
                hard.append(f"alias_{k}={ac[k]}")

    # 4. references -----------------------------------------------------------
    scon = db.scan_db(args.work)
    bad_refs = check_refs(plan, scon, out_paths, rep) or []

    # 5. leak scan --------------------------------------------------------------
    leaks = []
    if args.leak_scan != "off":
        pats, values, skipped = build_patterns(plan)
        rep["leak_patterns"] = {"identifiers": len(values), "too_short_skipped": skipped,
                                "needles": len(pats)}
        tasks = []
        for r in rows:
            o = out_paths[r["path"]]
            full = os.path.join(corpus, o)
            if os.path.isfile(full):
                tasks.append((o, {"path": full, "rel": o}, 600 + os.path.getsize(full) / 20e6))
        tasks.sort(key=lambda t: -os.path.getsize(t[1]["path"]))
        pool = Pool(args.jobs, _leak_init, (pats, args.leak_scan), args.mem_gb << 30)
        lc = collections.Counter()
        files_with = collections.Counter()
        ext_with = collections.Counter()
        try:
            for o, payload, r in pool.run(tasks):
                if "_pool_error" in r:
                    lc["scan_errors"] += 1
                    continue
                anyhit = False
                for where, hs in (("path", r["path_hits"]), ("bytes", r["raw"])):
                    for h in hs:
                        leaks.append((o, where, values[h][1], values[h][0]))
                        lc[f"{where}_hits"] += 1
                        anyhit = True
                for h, labels in r.get("views", []):
                    if h in r["raw"]:
                        continue
                    leaks.append((o, "decoded:" + ",".join(labels), values[h][1], values[h][0]))
                    lc["decoded_hits"] += 1
                    anyhit = True
                if anyhit:
                    files_with["files"] += 1
                    ext_with[suffix_of(o).lower().split(".")[-1] or "(none)"] += 1
        finally:
            pool.close()
        rep["leaks"] = dict(lc)
        rep["leaks"]["files_with_leaks"] = files_with["files"]
        rep["leaks_by_ext"] = dict(ext_with.most_common())

    rep["hard_failures"] = hard
    rep["seconds"] = round(time.time() - t0, 1)

    # reports ----------------------------------------------------------------------
    with open(os.path.join(vdir, "report.json"), "w") as f:
        json.dump(rep, f, indent=1, sort_keys=True)
    with open(os.path.join(vdir, "problems.tsv"), "w") as f:
        for x in problems:
            f.write("\t".join(map(str, x)) + "\n")
    with open(os.path.join(vdir, "leaks.tsv"), "w") as f:
        for x in leaks:
            f.write("\t".join(map(str, x)) + "\n")
    with open(os.path.join(vdir, "broken_refs.tsv"), "w") as f:
        for x in bad_refs:
            f.write("\t".join(map(str, x)) + "\n")
    with open(os.path.join(vdir, "exceptions.tsv"), "w") as f:
        for x in exc:
            f.write("\t".join(map(str, x)) + "\n")
    summary = public_summary(rep)
    with open(os.path.join(vdir, "summary.txt"), "w") as f:
        f.write(summary)
    print(summary)
    print(f"private details: {vdir}", file=sys.stderr)
    return 1 if hard else 0


def public_summary(rep) -> str:
    """Counts only: safe to paste into README.txt."""
    L = ["Validation summary (local acceptance gate)",
         f"  inputs: {rep['inputs']}  outputs ok: {rep['coverage'].get('ok', 0)}  "
         f"renamed: {rep['coverage'].get('renamed', 0)}",
         f"  objects: {rep['limits'].get('objects', 0)}  bytes: {rep['limits'].get('bytes', 0)}  "
         f"over 2 GB: {rep['limits'].get('over_2GB', 0)}",
         "  coverage problems: " + (", ".join(f"{k}={v}" for k, v in rep["coverage"].items()
                                              if k not in ("ok", "renamed") and v) or "none"),
         "  status by extension:"]
    for ext, c in rep["status_by_ext"].items():
        L.append(f"    .{ext}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    if "aliases" in rep:
        L.append("  aliases: " + ", ".join(f"{k}={v}" for k, v in sorted(rep["aliases"].items())))
    if "refs" in rep:
        L.append("  references: " + ", ".join(f"{k}={v}" for k, v in sorted(rep["refs"].items())))
    if "leaks" in rep:
        L.append("  residual identifier hits: " + ", ".join(f"{k}={v}" for k, v in sorted(rep["leaks"].items())))
        L.append("  files with residual hits by extension: " +
                 ", ".join(f".{k}={v}" for k, v in rep.get("leaks_by_ext", {}).items()))
    L.append("  hard failures: " + (", ".join(rep["hard_failures"]) or "none"))
    return "\n".join(L) + "\n"
