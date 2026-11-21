"""Dry-run report of the path plan.

    python -m deid.identity.report --plan-dir WORK/plan [--private-examples WORK/identity/path-examples.tsv]

Prints aggregate stats only (safe to paste in docs).  Raw examples, if
requested, are written to a PRIVATE file under work/.
"""
from __future__ import annotations

import argparse
import collections
import sys

from .plan import load_plan, split_ext


def path_report(plan, paths, examples_out=None):
    st = collections.Counter()
    seen_ci = {}
    collisions = []
    comp_changed = collections.Counter()
    by_bucket = collections.Counter()
    ex = []
    for p in paths:
        m = plan.map_path(p)
        st["paths"] += 1
        po, mo = p.split("/"), m.split("/")
        if len(po) != len(mo):
            st["depth_changed"] += 1
        if split_ext(po[-1])[1] != split_ext(mo[-1])[1]:
            st["ext_changed"] += 1
        if len(p) != len(m):
            st["length_changed"] += 1
        if m != p:
            st["changed"] += 1
            by_bucket["/".join(po[:2])] += 1
            for i, (a, b) in enumerate(zip(po, mo)):
                if a != b:
                    lvl = "file" if i == len(po) - 1 else ("slot" if i == (2 if po[0] == "sources" else 1) else "dir")
                    comp_changed[lvl] += 1
            if examples_out is not None and len(ex) < 200000:
                ex.append((p, m))
        k = m.lower()
        if k in seen_ci and seen_ci[k] != p:
            collisions.append((seen_ci[k], p))
        seen_ci[k] = p
    st["collisions_ci"] = len(collisions)
    if examples_out:
        with open(examples_out, "w", encoding="utf-8") as f:
            for a, b in ex:
                f.write(f"{a}\t{b}\n")
            for a, b in collisions:
                f.write(f"COLLISION\t{a}\t{b}\n")
    return st, comp_changed, by_bucket


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-dir", required=True)
    ap.add_argument("--private-examples")
    args = ap.parse_args(argv)
    plan = load_plan(args.plan_dir)
    paths = [p for p in plan.inventory]
    st, comp, bucket = path_report(plan, paths, args.private_examples)
    for k, v in sorted(st.items()):
        print(f"{k:16s} {v}")
    print("changed components by level:", dict(comp))
    print("changed paths by bucket:", dict(bucket))


if __name__ == "__main__":
    main()
