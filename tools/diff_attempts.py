#!/usr/bin/env python3
"""What changes between two attempts: per input file, compare the outputs of attempt A and B.

  python tools/diff_attempts.py --a-work WORK_A --a-corpus A/corpus --b-work WORK_B --b-corpus B/corpus \
      --out REPORT_DIR [--jobs 4]

Pairs outputs by INPUT path (from each work dir's results.sqlite), so renamed outputs are
matched. Categories per file: same (path and bytes), bytes_changed, path_changed,
path_and_bytes_changed, status changes. Writes REPORT_DIR/diff_summary.txt (counts by
extension and category: public-safe) and REPORT_DIR/diff_files.tsv (private: paths).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from deid.pipeline.pool import Pool  # noqa: E402


def _init(_):
    def h(p):
        a, b = p
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        ha, hb = hashlib.blake2b(digest_size=16), hashlib.blake2b(digest_size=16)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(1 << 22), fb.read(1 << 22)
                if x != y:
                    return False
                if not x:
                    return True
    return h


UNCHANGED = {"copied_unsupported", "verified_unchanged", "source_exception", "fallback_copy"}


def load(work):
    con = sqlite3.connect(f"file:{work}/results.sqlite?mode=ro", uri=True)
    return {p: (o, s, a) for p, o, s, a in con.execute("SELECT path, out_path, status, adapter FROM results")}


def ext_of(p):
    base = p.rsplit("/", 1)[-1]
    parts = base.split(".")
    if len(parts) >= 3 and parts[-1].isdigit():
        return "." + parts[-2].lower() + ".N"
    return "." + parts[-1].lower() if len(parts) > 1 else "(none)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-work", required=True)
    ap.add_argument("--a-corpus", required=True)
    ap.add_argument("--b-work", required=True)
    ap.add_argument("--b-corpus", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    A, B = load(a.a_work), load(a.b_work)
    os.makedirs(a.out, exist_ok=True)
    cats = collections.defaultdict(collections.Counter)
    status_moves = collections.Counter()
    rows = []
    tasks = []
    pending_unchanged = []
    for p in sorted(set(A) | set(B)):
        if p not in A or p not in B:
            cats[ext_of(p)]["only_in_" + ("a" if p in A else "b")] += 1
            continue
        (oa, sa, _), (ob, sb, adb) = A[p], B[p]
        if sa != sb:
            status_moves[f"{sa} -> {sb}"] += 1
        rows.append((p, oa, ob, sa, sb))
        if sa in UNCHANGED and sb in UNCHANGED:
            # both outputs are the input's own bytes: identical by construction, no need to read
            cats[ext_of(p)]["same" if oa == ob else "path_changed"] += 1
            if oa != ob:
                pending_unchanged.append((p, oa, ob, sa, sb))
            continue
        fa, fb = os.path.join(a.a_corpus, oa), os.path.join(a.b_corpus, ob)
        tasks.append((p, (fa, fb), 600 + os.path.getsize(fa) / 50e6))
    meta = {r[0]: r for r in rows}
    out = open(os.path.join(a.out, "diff_files.tsv"), "w", encoding="utf-8")
    out.write("input\tout_a\tout_b\tstatus_a\tstatus_b\tcategory\n")
    for p, oa, ob, sa, sb in pending_unchanged:
        out.write(f"{p}\t{oa}\t{ob}\t{sa}\t{sb}\tpath_changed\n")
    pool = Pool(a.jobs, _init, (None,), 2 << 30)
    try:
        for p, _, same in pool.run(tasks):
            _, oa, ob, sa, sb = meta[p]
            same = same is True
            cat = ("same" if same else "bytes_changed") if oa == ob else \
                  ("path_changed" if same else "path_and_bytes_changed")
            cats[ext_of(p)][cat] += 1
            if cat != "same":
                out.write(f"{p}\t{oa}\t{ob}\t{sa}\t{sb}\t{cat}\n")
    finally:
        pool.close()
        out.close()
    tot = collections.Counter()
    for c in cats.values():
        tot.update(c)
    L = ["Attempt diff (outputs paired by input file)", "  total: " + ", ".join(f"{k}={v}" for k, v in sorted(tot.items())),
         "  status changes: " + (", ".join(f"{k}: {v}" for k, v in status_moves.most_common()) or "none"),
         "  by extension (only extensions with changes):"]
    for e, c in sorted(cats.items(), key=lambda kv: -sum(v for k, v in kv[1].items() if k != "same")):
        if sum(v for k, v in c.items() if k != "same"):
            L.append(f"    {e}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    open(os.path.join(a.out, "diff_summary.txt"), "w").write("\n".join(L) + "\n")
    print("\n".join(L[:3]))


if __name__ == "__main__":
    main()
