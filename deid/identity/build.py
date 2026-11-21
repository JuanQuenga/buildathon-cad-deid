"""Build the identity plan from the manifest, the scan DB and private
curated/discovered tables.

    python -m deid.identity.build --manifest M --plan-dir WORK/plan [--scan-db WORK/scan.sqlite]
        [--extra WORK/identity/curated.tsv ...] [--report WORK/identity/path-report.txt]

Inputs other than the manifest are optional.  Raw values only ever live in
the plan directory and work/ tables (private).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys

from .aliases import TOKEN_RE, norm_key, norm_token
from .plan import EXACT, FIELD, FLEX, Entry, IdentityPlan, save_plan, SHA_PIECE_RE, split_ext
from .vocab import HOST_GENERIC, PLACEHOLDERS, SOFTWARE, is_generic_token, is_plain_token

# Observation kinds that make an identity on their own (by field position).
STRONG_KINDS = {"person", "company", "organization", "organisation", "account", "username", "email",
                "domain", "project", "address", "phone"}
WEAK_KINDS = {"part_number", "part_name", "document_number", "url", "path", "free_text"}

PUBLIC_SUFFIX_2 = {"co.uk", "ac.uk", "org.uk", "gov.uk", "com.au", "edu.au", "org.au", "co.jp", "ac.jp",
                   "com.cn", "edu.cn", "com.br", "co.nz", "ac.nz", "co.kr", "ac.kr", "com.tw", "edu.tw",
                   "co.in", "ac.in", "com.mx", "co.za", "ac.at", "co.at", "gv.at", "ac.be", "ac.il",
                   "co.il", "com.sg", "edu.sg", "com.hk", "edu.hk", "com.tr", "edu.tr"}


def _compact(v):
    return "".join(norm_token(t) for t in TOKEN_RE.findall(v))


def _all_generic(v):
    toks = TOKEN_RE.findall(v)
    return bool(toks) and all(is_plain_token(t) for t in toks)


NAME_KINDS = {"person", "company", "organization", "organisation"}


def _mode_for(value, *, strong=True, kind=None):
    key = norm_key(value)
    c = key.replace(" ", "")
    if not c or key in PLACEHOLDERS:
        return None
    if key in SOFTWARE:
        return FIELD
    if len(c) < 4 or c.isdigit():
        return FIELD
    toks = key.split()
    if kind in NAME_KINDS and len(toks) >= 2 and sum(len(t) >= 2 for t in toks) >= 2 and len(c) >= 6:
        return FLEX  # 'Rose Hunter': dictionary words, but a full name is an identity
    if _all_generic(value):
        return EXACT if len(c) >= 12 and len(key.split()) >= 2 else FIELD
    return FLEX if strong else EXACT


def registrable(host: str) -> str:
    labels = host.lower().strip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in PUBLIC_SUFFIX_2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


class Builder:
    MAX_TOKEN_SPREAD = 4   # a sub-token seen under more sources than this is vocabulary
    MAX_OWNER_TOKEN_SPREAD = 10

    def __init__(self):
        self.entries = []
        self.stats = collections.Counter()
        self.spread = {}

    def add(self, value, kind, mode=None, keep=(), origin="", count=1, sources=1):
        if not value or not value.strip():
            return
        if mode is None:
            mode = _mode_for(value, kind=kind)
            if mode is None:
                return
        self.entries.append(Entry(value=value.strip(), kind=kind, mode=mode, keep=tuple(keep),
                                  count=count, sources=sources, origin=origin))
        self.stats[f"{kind}:{mode}"] += 1

    # ---------------------------------------------------------------- paths
    def add_domain(self, host, origin, label_flex=True):
        host = host.strip().strip(".")
        if not host or "." not in host:
            return
        labels = host.split(".")
        keep = {l.lower() for l in labels if l.lower() in HOST_GENERIC}
        keep.add(labels[-1].lower())
        reg = registrable(host)
        for l in reg.split(".")[1:]:
            keep.add(l)
        keep = tuple(sorted(norm_token(k) for k in keep))
        self.add(host, "domain", EXACT, keep, origin)
        if reg != host.lower():
            self.add(reg, "domain", EXACT, keep, origin)
        for lab in (labels[:-1] if label_flex else []):
            if lab.lower() in HOST_GENERIC:
                continue
            m = _mode_for(lab)
            if m:
                self.add(lab, "domain_label", m, (), origin)

    def add_repo_piece(self, piece, kind, origin):
        if not piece or SHA_PIECE_RE.match(piece) or norm_key(piece) in PLACEHOLDERS:
            return
        mode = _mode_for(piece) or FIELD
        toks = TOKEN_RE.findall(piece)
        if (mode == FLEX and len(toks) == 1 and is_generic_token(toks[0])
                and self.spread.get(norm_token(toks[0]), 0) > self.MAX_TOKEN_SPREAD):
            mode = FIELD  # e.g. a repo called '3DModels': compound vocabulary used everywhere
        self.add(piece, kind, mode, (), origin)
        # distinctive sub-tokens are identities on their own (e.g. a product name inside a repo name)
        toks = TOKEN_RE.findall(piece)
        if len(toks) < 2:
            return
        for t in toks:
            k = norm_token(t)
            if (len(k) >= 4 and not k.isdigit() and not is_generic_token(t) and k not in SOFTWARE
                    and self.spread.get(k, 0) <= (self.MAX_OWNER_TOKEN_SPREAD if kind == "account"
                                                  else self.MAX_TOKEN_SPREAD)):
                self.add(t, kind + "_token", FLEX, (), origin)
            elif len(k) >= 4 and not is_generic_token(t):
                self.stats["subtoken_rejected_spread"] += 1

    def compute_spread(self, paths):
        """token -> number of distinct owners/sources whose paths contain it."""
        sets = collections.defaultdict(set)
        for p in paths:
            parts = p.split("/")
            if parts[0] == "sources" and len(parts) > 3:
                slot, rest = parts[2], parts[3:]
            elif parts[0] == "archives":
                slot, rest = parts[1], []
            else:
                continue
            pieces = [x for x in slot.split("__") if x]
            if parts[0] == "sources" and parts[1] == "gitlab" and len(pieces) > 1:
                pieces = pieces[1:]
            owner = (pieces[0] if parts[0] == "archives" or parts[1] in ("github", "gitlab") else slot).lower()
            for comp in [slot] + rest:
                for t in TOKEN_RE.findall(comp):
                    if len(t) >= 4:
                        sets[norm_token(t)].add(owner)
        self.spread = {k: len(v) for k, v in sets.items()}

    def from_manifest(self, paths):
        if not self.spread:
            self.compute_spread(paths)
        seen = set()
        for p in paths:
            parts = p.split("/")
            slot = None
            if parts[0] == "sources" and len(parts) > 3:
                slot = (parts[1], parts[2])
            elif parts[0] == "archives" and len(parts) == 2:
                slot = ("archives", split_ext(parts[1])[0])
            if not slot or slot in seen:
                continue
            seen.add(slot)
            bucket, comp = slot
            if bucket in ("github", "archives"):
                pieces = [x for x in comp.split("__") if x]
                if pieces and SHA_PIECE_RE.match(pieces[-1]) and len(pieces) > 1:
                    pieces = pieces[:-1]
                if pieces:
                    self.add_repo_piece(pieces[0], "account", f"path.{bucket}.owner")
                for x in pieces[1:]:
                    self.add_repo_piece(x, "project", f"path.{bucket}.repo")
                if len(pieces) >= 2:  # 'owner/repo' spelled with a slash is matched component-wise
                    pass
            elif bucket == "gitlab":
                pieces = [x for x in comp.split("__") if x]
                if pieces and "." in pieces[0]:
                    pieces = pieces[1:]
                for i, x in enumerate(pieces):
                    self.add_repo_piece(x, "account" if i == 0 else "project", "path.gitlab")
            elif bucket == "web":
                self.add_domain(comp, "path.web.domain")
            # catalog / existing: curated tables
        self.stats["slots"] = len(seen)

    # ---------------------------------------------------------------- scan DB
    def from_scan_db(self, db):
        """Adapter observations, filtered by the same evidence rules as discovery."""
        from .classify import classify, read_scan_db
        for r in classify(read_scan_db(db), trust_fields=True):
            self._observation(r["kind"], r["value"], r["files"], r["files"], set(r["fields"]), "scan")

    def _observation(self, kind, value, n, f, fields, origin="scan"):
        v = value.strip()
        if not v or len(v) > 200:
            return
        if kind in ("email",):
            local, _, dom = v.partition("@")
            if dom:
                self.add_domain(dom, origin + ".email", label_flex=False)
            if local and norm_key(local) not in PLACEHOLDERS and len(_compact(local)) >= 3:
                self.add(local, "email_local", FLEX if len(_compact(local)) >= 4 else FIELD, (), origin)
            return
        if kind == "domain":
            self.add_domain(v, origin + ".domain", label_flex=False)
            return
        if kind in STRONG_KINDS:
            m = _mode_for(v, kind=kind)
            if m:
                self.add(v, kind, m, (), origin + ":" + ",".join(sorted(fields))[:80], count=n, sources=f)
            return
        # weak kinds need curation or discovery evidence; ignored here

    def from_table(self, path):
        """TSV: value \t kind \t mode(optional) \t keep(optional, comma list)."""
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                value, kind = cols[0], (cols[1] if len(cols) > 1 else "curated")
                mode = cols[2] if len(cols) > 2 and cols[2] else None
                keep = tuple(x for x in cols[3].split(",") if x) if len(cols) > 3 else ()
                if kind == "domain":
                    self.add_domain(value, "table", label_flex=False)
                elif kind == "email":
                    self._observation("email", value, 1, 1, {"table"}, "table")
                else:
                    self.add(value, kind, mode, keep, "table:" + os.path.basename(path))

    def from_jsonl(self, path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                d = json.loads(line)
                self._observation(d["kind"], d["value"], d.get("count", 1), d.get("files", 1),
                                  set(d.get("fields", ["discovered"])), "discovered")


def read_manifest(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            out.append(line.rstrip("\n").split("\t")[0])
    return out


def build_plan(scan_db, manifest, plan_dir, extra_tables=(), secret=None):
    """Pipeline entry point (see docs/INTERFACE.md)."""
    b = Builder()
    paths = read_manifest(manifest)
    b.from_manifest(paths)
    inventory = list(paths)
    if scan_db and os.path.exists(scan_db):
        b.from_scan_db(scan_db)
        try:
            con = sqlite3.connect(f"file:{scan_db}?mode=ro", uri=True)
            members = [r[0] for r in con.execute("select path from files where path like '%!/%'")]
            con.close()
            inventory.extend(members)
        except sqlite3.Error:
            pass
    tables = list(extra_tables)
    for name in ("curated.tsv", "discovered.tsv"):
        p = os.path.join(os.path.dirname(os.path.abspath(plan_dir)), "identity", name)
        if os.path.exists(p) and p not in tables:
            tables.append(p)
    for t in tables:
        if t.endswith(".jsonl"):
            b.from_jsonl(t)
        else:
            b.from_table(t)
    if secret is None:
        sp = os.environ.get("DEID_SECRET_FILE") or os.path.join(plan_dir, "secret.key")
        if os.path.exists(sp):
            secret = open(sp, "rb").read()
        else:
            secret = os.urandom(32)
    plan = IdentityPlan(b.entries, secret, inventory, meta={"stats": dict(b.stats)})
    save_plan(plan, plan_dir)
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--plan-dir", required=True)
    ap.add_argument("--scan-db")
    ap.add_argument("--extra", action="append", default=[])
    args = ap.parse_args(argv)
    plan = build_plan(args.scan_db, args.manifest, args.plan_dir, args.extra)
    kinds = collections.Counter((e.kind, e.mode) for e in plan.entries.values())
    for (k, m), n in sorted(kinds.items()):
        print(f"{k:16s} {m:6s} {n}")
    print("entries", len(plan.entries), "version", plan.version)


if __name__ == "__main__":
    main()
