#!/usr/bin/env python3
"""Pass 1 of the PDF rewrite: identities that the PDF rules discover locally (people / companies next to
person or company labels, owners after 'property of' / (c), bylines, unlabelled names, organisations and
institutes, component vendors) collected over all input PDFs. The rewrite (DEID_PDF_GLOBAL_IDS=<out>) then
replaces their distinctive tokens in every PDF with the plan's deterministic token aliases, so a name found in
one drawing's title block is also replaced where a sibling drawing prints it without a label.

    tools/pdf_global_ids.py --input INPUT_DIR --manifest manifest.tsv --work WORK --out WORK/pdf_global_ids.json.gz

Private work artefact (real values), never shipped. Needs DEID_PDF_SPREAD (tools/pdf_spread.py) first.
"""
import argparse, gzip, json, os, re, sys, unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

NAME = re.compile(r"^(?:[A-ZÀ-Ý]\.\s?){0,2}[A-ZÀ-Ý][a-zà-ÿ'’\-]+(?:\s(?:[A-ZÀ-Ý]\.\s?)?[A-ZÀ-Ý][a-zà-ÿ'’\-]+){1,3}$"
                  r"|^(?:[A-ZÀ-Ý]\.\s?){1,2}[A-ZÀ-Ý][A-Za-zà-ÿ'’\-]+$")
INPUT = None
# generic words that title-block / organisation rules can pick up in other languages or as typos (not identities)
GENERIC = set("""definiert chasis chassis cote coté côté insterting bluetooth humanoid unidades grado""".split())


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def discover(rel):
    from deid.formats import pdf as P
    out = []
    try:
        pages = [(W, H, P._merge_spaced_cjk(l)) for W, H, l in P._bbox_lines(os.path.join(INPUT, rel), max_pages=8)]
    except Exception:
        return out
    for W, H, lines in pages:
        for i, (x0, y0, x1, y1, t) in enumerate(lines):
            t = " ".join(t.split())
            if not t:
                continue
            for rx in P._E5_BOILER[:2]:
                out += [("owner", m.group(1).strip()) for m in rx.finditer(t)]
            if P._E25_OWNER_END.search(t):
                lh0 = max(y1 - y0, 1.0)
                nxt = sorted((b0 - y1, tt) for (a0, b0, a1, b1, tt) in lines
                             if 0 <= b0 - y1 < 2.5 * lh0 and a0 < x1 + 5 * lh0 and a1 > x0 - 5 * lh0 and tt.strip())
                if nxt:
                    out.append(("owner", " ".join(nxt[0][1].split()).rstrip(".,;")))
            out += [("vendor", v) for v in P._e5_vendor_hits(t)]
            out += [("e14", v) for v in P.e14_candidates(t)]
            out += [("byline", v) for v in P._e26_bylines(t)]
            m = re.match(r"^(.{2,30}?)\s*[:：]\s*(.{2,60})$", t)
            if m and P._e5_label(m.group(1)) in ("person", "company"):
                out.append((P._e5_label(m.group(1)), m.group(2)))
            k = P._e5_label(t) or P._e5_label(t.rstrip(":："))
            if k not in ("person", "company"):
                continue
            lh = max(y1 - y0, 1.0)
            right, below = [], []
            for j, (a0, b0, a1, b1, tt) in enumerate(lines):
                if j == i or not tt.strip():
                    continue
                if abs(b0 - y0) < 0.6 * lh and a0 >= x1 - 1 and a0 - x1 < 30 * lh:
                    right.append((a0 - x1, tt))
                elif 0 <= b0 - y1 < 2.5 * lh and a0 < x1 and a1 > x0 - 2 * lh:
                    below.append((b0 - y1, tt))
            for _, tt in sorted(right)[:2] + sorted(below)[:1]:
                v = " ".join(tt.split()).strip(":：;,")
                if P._e5_label(v) is None:
                    out.append((k, v))
    return out


def init(inp):
    global INPUT
    INPUT = inp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args()
    from deid.formats import pdf as P
    from deid.identity.vocab import is_word
    sp = P._spread()
    if not sp:
        sys.exit("DEID_PDF_SPREAD is required (run tools/pdf_spread.py first)")
    paths = sorted(l.split("\t", 1)[0] for l in open(a.manifest, encoding="utf-8")
                   if l.strip() and l.split("\t", 1)[0].lower().endswith(".pdf"))
    tok_docs = defaultdict(set)
    tok_kind = defaultdict(Counter)
    with ProcessPoolExecutor(a.jobs, initializer=init, initargs=(a.input,)) as ex:
        for rel, found in zip(paths, ex.map(discover, paths, chunksize=8)):
            for k, v in found:
                v = v.strip()
                if k in ("person", "company") and not NAME.match(v):
                    continue
                if not P._e5_value_ok(v):
                    continue
                for t in re.findall(r"[^\W\d_]+", v):
                    if not t[0].isupper():
                        continue
                    f = fold(t)
                    if len(f) < 4 or sp.get(f, 0) > 6 or f in P._E5_ENG_WORDS or f in P._TB_GENERIC:
                        continue
                    if is_word(f) and k != "byline":
                        continue
                    tok_docs[f].add(rel)
                    tok_kind[f][k] += 1
    # a token seen in a single document only counts when it came from a name-shaped source
    keep = {f: f for f, d in tok_docs.items()
            if (len(d) >= 2 or tok_kind[f]["byline"] or tok_kind[f]["person"]) and f not in GENERIC}
    with gzip.open(a.out, "wt", encoding="utf-8") as fh:
        json.dump(keep, fh, sort_keys=True)
    print(f"{len(paths)} PDFs, {len(tok_docs)} candidate tokens, {len(keep)} written to {a.out}")


if __name__ == "__main__":
    main()
