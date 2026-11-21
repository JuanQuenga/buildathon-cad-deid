#!/usr/bin/env python3
"""Corpus spread of PDF text tokens (private work artefact, never shipped).

    tools/pdf_spread.py --input INPUT_DIR --manifest manifest.tsv --out WORK/pdf_spread.json.gz [--jobs 3]

For every folded token (case and accents removed) of the text layer of the input PDFs, counts the source
namespaces (repository owner / catalog entry / web site / curated folder) whose PDFs contain it. Only
tokens seen in >= 3 namespaces are written: they are the generic vocabulary the PDF sweep never touches
(DEID_PDF_SPREAD=<out> during rewrite). Deterministic for a given input.
"""
import argparse, gzip, json, os, re, subprocess, unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

TOK = re.compile(r"[^\W\d_][^\W_]*", re.U)


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def namespace(p):
    q = p.split("/")
    if len(q) > 2 and q[1] in ("github", "gitlab"):
        return "/".join(q[:3]).split("__")[0]
    if len(q) > 2 and q[1] in ("catalog", "web"):
        return "/".join(q[:3])
    return "/".join(q[:5])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args()
    paths = [l.split("\t", 1)[0] for l in open(a.manifest, encoding="utf-8") if l.strip()]
    paths = sorted(p for p in paths if p.lower().endswith(".pdf"))

    def toks(p):
        try:
            r = subprocess.run(["pdftotext", "-q", "-l", "8", os.path.join(a.input, p), "-"],
                               capture_output=True, timeout=120)
            t = r.stdout.decode("utf-8", "replace")
        except Exception:
            t = ""
        return p, {fold(x) for x in TOK.findall(t) if len(x) >= 3}

    df = defaultdict(set)
    with ThreadPoolExecutor(a.jobs) as ex:
        for p, ts in ex.map(toks, paths):
            n = namespace(p)
            for t in ts:
                df[t].add(n)
    out = {t: len(v) for t, v in sorted(df.items()) if len(v) >= 3}
    with gzip.open(a.out, "wt", encoding="utf-8") as f:
        json.dump(out, f, sort_keys=True)
    print(f"{len(paths)} PDFs, {len(df)} tokens, {len(out)} written to {a.out}")


if __name__ == "__main__":
    main()
