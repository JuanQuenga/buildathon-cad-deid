#!/usr/bin/env python3
"""Write <attempt>.md5cache.json (the cache tools/gcs_copy_unchanged.py reads) cheaply.

Outputs that are the input's own bytes (copied_unsupported, verified_unchanged, source_exception,
fallback_copy) take the manifest MD5 when the manifest has one; everything else is hashed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from deid.pipeline.pool import Pool  # noqa: E402

UNCHANGED = {"copied_unsupported", "verified_unchanged", "source_exception", "fallback_copy"}


def _init(_):
    def h(path):
        m = hashlib.md5()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 22), b""):
                m.update(b)
        return m.hexdigest()
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attempt", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    att = os.path.abspath(a.attempt)
    man = {}
    with open(a.manifest, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            c = line.rstrip("\n").split("\t")
            if len(c) >= 3 and c[2]:
                man[c[0]] = (int(c[1]), c[2])
    con = sqlite3.connect(f"file:{a.work}/results.sqlite?mode=ro", uri=True)
    cache, tasks = {}, []
    for p, o, st in con.execute("SELECT path, out_path, status FROM results"):
        full = os.path.join(att, "corpus", o)
        if not os.path.isfile(full):
            continue
        s = os.stat(full)
        key = "corpus/" + o
        if st in UNCHANGED and p in man and man[p][0] == s.st_size:
            cache[key] = [s.st_size, int(s.st_mtime), man[p][1]]
        else:
            tasks.append((key, full, 300 + s.st_size / 50e6))
    for sub in ("code",):
        for root, _, files in os.walk(os.path.join(att, sub)):
            for fn in files:
                full = os.path.join(root, fn)
                tasks.append((f"{sub}/{os.path.relpath(full, os.path.join(att, sub))}", full, 60))
    tasks.append(("README.txt", os.path.join(att, "README.txt"), 60))
    reused = len(cache)
    pool = Pool(a.jobs, _init, (None,), 2 << 30)
    try:
        for key, full, md5 in pool.run(tasks):
            if isinstance(md5, str):
                s = os.stat(full)
                cache[key] = [s.st_size, int(s.st_mtime), md5]
    finally:
        pool.close()
    with open(att + ".md5cache.json", "w") as f:
        json.dump(cache, f)
    print(f"md5 cache: {len(cache)} objects ({reused} from the manifest, {len(cache) - reused} hashed)")


if __name__ == "__main__":
    main()
