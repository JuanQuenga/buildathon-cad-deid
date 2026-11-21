#!/usr/bin/env python3
"""Preflight + upload wrapper around the organisers' upload.py.

Default is a DRY RUN: checks the attempt folder and prints what would be uploaded.
Only with --go does it call upload.py (16 parallel PUTs, resumable, token renewal
via --token-file). The token is read from the token file and passed through the
environment; it is never printed or logged.

  python3 tools/upload_attempt.py --attempt out/attempt-1 \
      --dest gs://builders-machina-submissions-1120/teams/<team-id>/attempt-1/ \
      --token-file ~/machina-token.txt [--plan-dir work/plan] [--report work/validate/report.json] [--go]

Preflight (refuses --go on any failure unless --force):
  * layout: corpus/ (non-empty), code/, README.txt; nothing else at the top
  * no symlinks; no object >= 2 GB; <= 400,000 corpus objects
  * code/ has no private artefacts (sqlite, secret.key, plan.json, inventory, logs, raw data)
  * README.txt and code/ contain no plan identifier (if --plan-dir is given)
  * validation report (if given) has no hard failures
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HARD_SIZE = 2_000_000_000
MAX_OBJECTS = 400_000
PRIVATE_SUFFIXES = (".sqlite", ".sqlite-wal", ".sqlite-shm", "secret.key", "plan.json", "inventory.txt",
                    ".log", ".tsv", ".deid-tmp", ".deid-new", ".env", ".pem", "token.txt")
DEFAULT_UPLOADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
                                "cad-deid", "starter", "upload.py")


def walk(d):
    for root, dirs, files in os.walk(d):
        for x in dirs:
            p = os.path.join(root, x)
            if os.path.islink(p):
                yield p, True, 0
        for f in files:
            p = os.path.join(root, f)
            yield p, os.path.islink(p), (0 if os.path.islink(p) else os.path.getsize(p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attempt", required=True)
    ap.add_argument("--dest", required=True, help="gs://.../attempt-N/ (from the track page)")
    ap.add_argument("--token-file", help="file holding the current upload token (renewals are written here)")
    ap.add_argument("--uploader", default=DEFAULT_UPLOADER)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--plan-dir", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--mbps", type=float, default=89.0, help="measured upload Mb/s for the ETA")
    ap.add_argument("--log", default=None)
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    att = os.path.abspath(a.attempt)
    fails, warns = [], []

    top = sorted(os.listdir(att)) if os.path.isdir(att) else []
    if top != ["README.txt", "code", "corpus"]:
        fails.append(f"top level must be exactly corpus/, code/, README.txt (found {top})")
    n_obj = n_bytes = big = links = 0
    for p, islink, sz in walk(os.path.join(att, "corpus")):
        n_obj += 1
        n_bytes += sz
        links += islink
        big += sz >= HARD_SIZE
    if n_obj == 0:
        fails.append("corpus/ is empty")
    if n_obj > MAX_OBJECTS:
        fails.append(f"{n_obj} corpus objects > {MAX_OBJECTS}")
    if big:
        fails.append(f"{big} corpus objects >= 2 GB")
    code_files = 0
    code_bytes = 0
    for p, islink, sz in walk(os.path.join(att, "code")):
        code_files += 1
        code_bytes += sz
        links += islink
        if p.endswith(PRIVATE_SUFFIXES) or "/.venv/" in p or "/work/" in p:
            fails.append(f"private artefact in code/: {os.path.relpath(p, att)}")
        if sz > 20_000_000:
            warns.append(f"large file in code/: {os.path.relpath(p, att)} ({sz} bytes)")
    if links:
        fails.append(f"{links} symlinks (upload.py skips them; the grader would miss files)")

    if a.plan_dir:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        from deid.identity import load_plan
        from deid.validate.gate import build_patterns, StrLeakScanner
        plan = load_plan(a.plan_dir)
        pats, values, _ = build_patterns(plan)
        sc = StrLeakScanner(pats)
        hits = sc.scan_file(os.path.join(att, "README.txt")) if os.path.exists(os.path.join(att, "README.txt")) else set()
        # names of the pipeline's own dependencies/tools in the credits are not leaks
        deps = {"ezdxf", "pikepdf", "pdfminer", "pdfminer.six", "olefile", "pillow", "lxml", "numpy", "libarchive",
                "libarchive-c", "pyahocorasick", "qpdf", "tesseract", "poppler", "gzip", "pytest", "uv", "python"}
        real = [h for h in hits if values[h][0].lower() not in deps]
        if len(real) < len(hits):
            warns.append(f"README.txt: {len(hits) - len(real)} plan values equal dependency names (ignored)")
        if real:
            fails.append(f"README.txt contains {len(real)} plan identifiers")
        chits = 0
        for p, islink, sz in walk(os.path.join(att, "code")):
            if not islink and sz < 50_000_000 and sc.scan_file(p):
                chits += 1
        if chits:
            warns.append(f"{chits} code/ files contain plan identifiers (review: matching logic is allowed, "
                         "raw lookup tables are not)")
    if a.report:
        rep = json.load(open(a.report))
        if rep.get("hard_failures"):
            fails.append(f"validation hard failures: {rep['hard_failures']}")

    eta_h = (n_bytes + code_bytes) * 8 / (a.mbps * 1e6) / 3600
    print(f"attempt: {att}")
    print(f"corpus: {n_obj} objects, {n_bytes / 1e9:.2f} GB; code: {code_files} files; README.txt")
    print(f"projected upload at {a.mbps:.0f} Mb/s: {eta_h:.1f} h (+ per-object overhead); "
          f"tokens last 1 h: keep --token-file fresh")
    for w in warns:
        print("WARN", w)
    for f in fails:
        print("FAIL", f)
    if not a.go:
        print("dry run only (pass --go to upload)")
        return 1 if fails else 0
    if fails and not a.force:
        print("refusing to upload: fix the failures or pass --force")
        return 2
    if not a.token_file or not os.path.exists(a.token_file):
        print("need --token-file with the current upload token")
        return 2
    if not os.path.exists(a.uploader):
        print(f"uploader not found: {a.uploader}")
        return 2
    env = dict(os.environ)
    env["MACHINA_UPLOAD_TOKEN"] = open(a.token_file).read().strip()
    cmd = [sys.executable, a.uploader, "--dest", a.dest, "--corpus", os.path.join(att, "corpus"),
           "--code", os.path.join(att, "code"), "--readme", os.path.join(att, "README.txt"),
           "--token-file", a.token_file, "--workers", str(a.workers)]
    log = a.log or os.path.join(att + f".upload-{time.strftime('%Y%m%d-%H%M%S')}.log")
    print(f"uploading; log: {log}")
    with open(log, "a") as lf:
        p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            sys.stdout.write(line)
            lf.write(line)
            lf.flush()
        return p.wait()


if __name__ == "__main__":
    sys.exit(main())
