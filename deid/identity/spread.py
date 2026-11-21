"""Corpus-wide spread of plan entries (general vocabulary rule).

For every free-text plan entry, count in how many distinct owner namespaces
(``sources/<site>/<owner>``, archive roots, ...) of the INPUT corpus it occurs,
looking at raw bytes and decoded streams (zip/gzip/.Z/tar members, PDF Flate
streams) with token boundaries. Identifying values stay inside their owner's
tree; boilerplate (licence/copyright phrases, font foundries, title-block
labels, library names, certificate authorities) occurs across many unrelated
owners. build_plan demotes widely spread entries to FIELD mode (replaced only in
identity fields, never in free text) unless they come from a structural path slot.

  python -m deid.identity.spread --plan-dir WORK/plan --input IN --manifest M \
      --out WORK/identity/entry-spread.tsv [--jobs 4]
"""
from __future__ import annotations

import argparse
import collections
import os
import sys

from deid import api
from deid.pipeline.pool import Pool

SPREAD_MAX = 15     # an entry seen in more owner namespaces than this is vocabulary


def namespace(path: str) -> str:
    parts = path.split("/")
    if parts[0] == "sources" and len(parts) > 3:
        return "/".join(parts[:3])
    return "/".join(parts[:2])


def _init(pats):
    from deid.validate.gate import StrLeakScanner, _views
    sc = StrLeakScanner(pats)

    def handle(p):
        hits = set()
        try:
            hits |= sc.scan_file(p["src"], cap=256 << 20)
            with open(p["src"], "rb") as f:
                head = f.read(api.HEAD_BYTES)
            for _, b in _views(p["src"], head):
                hits |= sc.scan_bytes(b)
        except Exception:
            pass
        return sorted(hits)
    return handle


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-dir", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args(argv)
    from deid.identity import load_plan
    from deid.validate.gate import build_patterns
    from deid.pipeline.run import load_manifest
    plan = load_plan(a.plan_dir)
    pats, values, _ = build_patterns(plan)
    rows, _ = load_manifest(a.manifest, a.input)
    # each distinct content counts once (copies of the same files across forks are one source)
    seen, tasks = set(), []
    for r in sorted(rows, key=lambda r: -r["size"]):
        if r["size"] <= 0 or (r["md5"] and r["md5"] in seen):
            continue
        if r["md5"]:
            seen.add(r["md5"])
        tasks.append((r["path"], {"src": os.path.join(a.input, r["path"])}, 900 + r["size"] / 20e6))
    ns_sets = collections.defaultdict(set)
    files = collections.Counter()
    pool = Pool(a.jobs, _init, (pats,), 4 << 30)
    n = 0
    try:
        for path, _, res in pool.run(tasks):
            n += 1
            if isinstance(res, dict):
                continue
            ns = namespace(path)
            for h in res:
                ns_sets[h].add(ns)
                files[h] += 1
            if n % 20000 == 0:
                print(f"{n}/{len(tasks)}", file=sys.stderr, flush=True)
    finally:
        pool.close()
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("# value\tkind\tnamespaces\tfiles\n")
        for h, (v, kind, mode) in enumerate(values):
            if files[h]:
                f.write(f"{v}\t{kind}\t{len(ns_sets[h])}\t{files[h]}\n")
    print(f"wrote {a.out}: {sum(1 for h in files)} entries seen", file=sys.stderr)


if __name__ == "__main__":
    main()
