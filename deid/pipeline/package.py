"""Assemble OUT/attempt-N/{corpus,code,README.txt} from a stage produced by `rewrite`.

corpus/  reflink copy of exactly the expected outputs (no stale or temp files)
code/    `git archive` of the repository at HEAD (includes uv.lock)
README.txt  run instructions, limitations, validation summary, source exceptions by final path
"""
from __future__ import annotations

import collections
import datetime
import io
import json
import os
import subprocess
import sys
import tarfile

from deid.pipeline import core, db, run

FORBIDDEN_IN_CODE = (".sqlite", "secret.key", "plan.json", "inventory.txt", ".deid-tmp", ".env")


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout.strip()


def cmd_package(args):
    attempt = args.out
    stage_corpus = os.path.join(os.path.abspath(args.stage), "corpus")
    if os.path.exists(os.path.join(attempt, "corpus")):
        print(f"refusing: {attempt}/corpus exists (attempt folders are immutable)", file=sys.stderr)
        return 2
    rows, missing = run.load_manifest(args.manifest, args.input, args.only_prefix, args.limit,
                                      args.skip_missing)
    plan = run.load_plan(args.work, args.plan)
    out_paths, collisions = run.compute_out_paths(rows, plan)
    if collisions:
        print("refusing: output path collisions", file=sys.stderr)
        return 2
    rcon = db.results_db(args.work)
    res = {p: (o, st, notes, a) for p, o, st, notes, a in
           rcon.execute("SELECT path, out_path, status, notes, adapter FROM results")}
    # 1. corpus
    n = 0
    for r in rows:
        o = out_paths[r["path"]]
        src = os.path.join(stage_corpus, o)
        if not os.path.isfile(src):
            print(f"refusing: stage is missing an output ({n} copied so far); run rewrite first",
                  file=sys.stderr)
            return 2
        if r["path"] not in res or res[r["path"]][0] != o:
            print("refusing: stage was produced with a different plan; rerun rewrite", file=sys.stderr)
            return 2
        core.fastcopy(src, os.path.join(attempt, "corpus", o))
        n += 1
    # 2. code
    repo = os.path.abspath(args.repo)
    sha = git(repo, "rev-parse", "HEAD")
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no")
    code = os.path.join(attempt, "code")
    os.makedirs(code)
    data = subprocess.run(["git", "-C", repo, "archive", "--format=tar", "HEAD"], capture_output=True,
                          check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        for m in t.getmembers():
            if m.name.startswith("/") or ".." in m.name.split("/") or m.issym() or m.islnk():
                continue
            if m.name.endswith(FORBIDDEN_IN_CODE):
                print(f"refusing: forbidden file in repo: {m.name}", file=sys.stderr)
                return 2
            t.extract(m, code, filter="data")
    with open(os.path.join(code, "SOURCE_COMMIT.txt"), "w") as f:
        f.write(f"{sha}\n")
    # 3. README.txt
    rewrite_sha = dict(rcon.execute("SELECT k, v FROM meta")).get("git_sha", "")
    summary_path = os.path.join(args.work, "validate", "summary.txt")
    summary = open(summary_path).read() if os.path.exists(summary_path) else "(validation not run)\n"
    readme = render_readme(sha, dirty, rewrite_sha, plan.version, rows, out_paths, res, summary)
    with open(os.path.join(attempt, "README.txt"), "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"packaged {n} corpus files, code at {sha[:12]}{' (dirty tree: uncommitted changes NOT included)' if dirty else ''}"
          f" -> {attempt}", file=sys.stderr)
    return 0


def render_readme(sha, dirty, rewrite_sha, plan_version, rows, out_paths, res, summary):
    st = collections.Counter()
    exc, fb = [], collections.Counter()
    for r in rows:
        p = r["path"]
        if p not in res:
            continue
        o, s, notes, a = res[p]
        st[s] += 1
        if s == "source_exception":
            try:
                n = "; ".join(json.loads(notes or "[]"))[:160]
            except ValueError:
                n = ""
            exc.append((out_paths[p], n or "source defect"))
        elif s == "fallback_copy":
            fb[os.path.splitext(out_paths[p])[1].lower()] += 1
    exc.sort()
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = []
    L.append("CAD De-ID submission")
    L.append("=" * 60)
    L.append(f"Generated {today}. Pipeline source: code/ (commit {sha}).")
    if rewrite_sha and rewrite_sha != sha:
        L.append(f"Note: the corpus was produced at commit {rewrite_sha}; code/ is {sha}.")
    L.append("")
    L.append("WHAT THIS IS")
    L.append("corpus/ is the full input corpus with identifying information replaced by consistent,")
    L.append("typed pseudonyms. Every input file has exactly one output file (same extension, same")
    L.append("directory depth); identifying file and folder names are replaced by their pseudonyms and")
    L.append("references between files are rewritten to the renamed targets. Files are kept in their")
    L.append("native formats; archives are rewritten in their own format with processed members.")
    L.append("")
    L.append("HOW TO RUN (exact)")
    L.append("  Requirements: Linux, Python 3.13, uv 0.13, git, libarchive (bsdtar) 3.7+, ~2.5x corpus disk.")
    L.append("  cd code && uv sync --frozen            # installs the locked dependencies (uv.lock)")
    L.append("  python3 tools/acquire.py --manifest manifest.tsv --dest INPUT_DIR   # verified download of the input")
    L.append("  export DEID_MANIFEST=manifest.tsv      # the organisers' manifest (path, bytes, md5, url)")
    L.append("  .venv/bin/python -m deid.pipeline scan     --input INPUT_DIR --work WORK --jobs 6")
    L.append("  .venv/bin/python -m deid.pipeline plan     --input INPUT_DIR --work WORK")
    L.append("  .venv/bin/python -m deid.pipeline rewrite  --input INPUT_DIR --work WORK --out STAGE --jobs 6")
    L.append("  .venv/bin/python -m deid.pipeline validate --input INPUT_DIR --work WORK --out STAGE --leak-scan full")
    L.append("  .venv/bin/python -m deid.pipeline package  --input INPUT_DIR --work WORK --stage STAGE --out ATTEMPT_DIR")
    L.append("  The pseudonym key (WORK/plan/secret.key) and the private alias table stay in WORK and are")
    L.append("  not part of this submission; with a different key the pseudonyms differ but stay consistent.")
    L.append("  Re-runs are incremental: only files whose content, adapter version or plan changed are redone.")
    L.append("")
    L.append("PROCESSING STATUS (files)")
    for k, v in sorted(st.items()):
        L.append(f"  {k}: {v}")
    L.append("  rewritten          = edited by a format adapter and re-verified by reparsing")
    L.append("  verified_unchanged = parsed, nothing identifying found to change")
    L.append("  copied_unsupported = no safe writer for this format/variant yet; bytes unchanged (path still mapped)")
    L.append("  source_exception   = input defect (empty, truncated, encrypted); bytes unchanged, listed below")
    L.append("  fallback_copy      = an edit failed verification or timed out; original bytes kept so the file stays usable")
    L.append("")
    L.append("LIMITATIONS")
    L.append("  * Proprietary native CAD formats without a verified open writer are passed through unchanged")
    L.append("    except where a format adapter performs verified in-place edits; see status counts.")
    L.append("  * RAR archives are rewritten as RAR 4.x archives with stored (uncompressed) entries,")
    L.append("    because no free RAR compressor exists; contents and member names are processed.")
    L.append("  * gzip comment fields are dropped (the writer has no comment field); file names are mapped.")
    L.append("  * Archives with encrypted headers or members, and truncated downloads (.resume-partial),")
    L.append("    cannot be opened and are copied unchanged (listed below).")
    if fb:
        L.append("  * Files kept unchanged after a failed edit (by extension): " +
                 ", ".join(f"{k or '(none)'}={v}" for k, v in fb.most_common()))
    L.append("")
    L.append("VALIDATION")
    L.extend("  " + x if not x.startswith("  ") else x for x in summary.rstrip("\n").split("\n"))
    L.append("")
    L.append(f"SOURCE EXCEPTIONS ({len(exc)} files, final paths)")
    for o, n in exc:
        L.append(f"  corpus/{o}\t{n}")
    L.append("")
    return "\n".join(L) + "\n"
