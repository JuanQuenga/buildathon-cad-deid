"""Identities from drawing title blocks (PDF scan observations) -> plan table.

The PDF adapter reports labelled title-block values (``pdf.text.label.<label>``), the
text lines of the title-block region (``pdf.text.titleblock``) and the Info dictionary.
This module turns that evidence into plan entries so that the people, companies,
projects, contact numbers and the drawing's own part/document number that a title block
carries are replaced everywhere, consistently (same entry -> same pseudonym in every file,
file name and reference).

  python -m deid.identity.pdfids --scan-db WORK/scan.sqlite --out WORK/identity/pdf-identities.tsv

Output rows: value \\t kind \\t mode  (read by build_plan as an extra table).
"""
from __future__ import annotations

import argparse
import collections
import re
import sqlite3

from .aliases import norm_key
from .vocab import PLACEHOLDERS, SOFTWARE, is_word

_WORDCHARS = r"A-Za-zÀ-ÖØ-öø-ÿĀ-žḀ-ỿ"
NAME_TOK = re.compile(rf"[{_WORDCHARS}][{_WORDCHARS}'’\-]*\.?")
INIT_SURNAME = re.compile(rf"^([A-ZÀ-ÖØ-Þ])\.\s?([A-ZÀ-ÖØ-Þ][{_WORDCHARS}'’\-]{{2,}})$")
HONORIFIC = re.compile(rf"^(?:Hr|Herr|Fr|Frau|Mr|Mrs|Ms|Dr|Prof|Ing|Sr|Sra|Dipl\.?-Ing)\.?\s+([A-ZÀ-ÖØ-Þ][{_WORDCHARS}'’\-]{{2,}}(?:\s+[A-ZÀ-ÖØ-Þ][{_WORDCHARS}'’\-]{{2,}})?)$")
DATE_TAIL = re.compile(r"[\s,;:/-]*(?:\d{1,4}[./-]\d{1,2}[./-]\d{1,4}|\d{1,2}\s?[A-Za-z]{3}\s?\d{2,4}|\d+)\s*$")
CODE = re.compile(r"^(?=[^ ]*\d)[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*$")
DATE = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}$")
DIM = re.compile(r"^\d+([.,]\d+)?\s*[x×:]\s*\d+|^[mM]\d|^(?:rev|v)\d", re.I)
PHONE = re.compile(r"^\+?[\d ()./-]{7,24}$")
STOP_UPPER = {"N/A", "TBD", "SEE", "ANY", "ALL", "THE", "AND", "FOR", "REV", "DWG", "QTY", "MAT", "NTS", "MFG",
              "ENG", "CHK", "APP", "DRN", "SIZE", "SCALE", "DATE", "NAME", "NONE", "NOTE", "ISO", "DIN"}
LABEL_WORDS = re.compile(r"(?i)\b(drawn|checked|approved|date|datum|fecha|data|scale|sheet|title|name|signature|"
                         r"firma|material|weight|finish|rev|revision|tolerance|size|dwg|description|qty|part|"
                         r"project|company|designed|created|by|of|no|nr|number|format)\b")


ENG_NOUNS = set("""
zahnrad zahnkranz stirnrad abtrieb antrieb welle platte halter deckel gehause gehäuse schraube mutter buchse
hulse hülse flansch lager rahmen teil rad hebel griff bolzen stift feder ring scheibe profil winkel trager
träger adapter kupplung motor servo sensor lens retainer hub gear coupling base cable tie connector bracket
plate arm shaft housing cover mount holder spacer frame feeder quick fit gearbox pulley bearing nut bolt screw
washer pin spring clamp block tube pipe rod wheel assembly part pieza placa soporte eje tapa tornillo tuerca
carcasa piastra supporto albero coperchio vite dado piece pièce plaque axe capot vis ecrou écrou peca peça
suport upper lower left right top bottom front rear inner outer main side center centre generator pulse
board pcb module unit kit set box case panel door handle knob lever cap lid guide rail slide carriage motor
nozzle hotend extruder bed fan duct sheet drawing detail section view list parts bom revision table
stempelplatte drehteil werkstuck werkstück frasteil frästeil kleinteile minifigure legs servo arm futaba
""".split())
STOP_UPPER |= {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "ODD",
               "NEW", "OLD", "TOP", "END", "MIN", "MAX", "STD", "REF", "ASSY", "INCH", "MM", "PCS"}


def _ok_token(t):
    k = norm_key(t)
    return bool(k) and k not in PLACEHOLDERS and k not in SOFTWARE and k not in ENG_NOUNS


def person_value(v: str):
    """Normalised person spelling or None."""
    v = v.strip().strip(":;,|")
    v = DATE_TAIL.sub("", v).strip().strip(":;,|")
    if not v or len(v) > 48 or any(ch.isdigit() for ch in v) or LABEL_WORDS.search(v):
        return None
    m = HONORIFIC.match(v)
    if m:
        v = m.group(1)
    if INIT_SURNAME.match(v):
        sur = INIT_SURNAME.match(v).group(2)
        return v if _ok_token(sur) and not is_word(sur.lower()) else None
    toks = NAME_TOK.findall(v)
    if not toks or len(toks) > 4 or " ".join(toks) != " ".join(v.split()):
        return None
    if not all(t[0].isupper() for t in toks):
        return None
    if not all(_ok_token(t) for t in toks) or norm_key(v) in PLACEHOLDERS:
        return None
    if len(toks) == 1:
        t = toks[0]
        if t.isupper() and 3 <= len(t) <= 4 and t not in STOP_UPPER and not is_word(t.lower()):
            return t                      # initials such as 'BJD' next to a person label
        return t if len(t) >= 4 and not t.isupper() and not is_word(t.lower()) else None
    words = sum(1 for t in toks if len(t) > 2 and is_word(t.lower().rstrip(".")))
    if words * 2 > len(toks):
        return None                       # 'Upper Cover', 'Base Plate': mostly dictionary words
    return v


ORG_TAIL = re.compile(r"(?i)\b(inc|corp|corporation|ltd|llc|gmbh|ag|kg|ohg|sa|sas|sarl|srl|spa|s\.?l\.?|bv|nv|ab|oy|"
                      r"pty|co|company|group|lab|labs|laboratory|university|universit[yéaà]|universidad|universitat|"
                      r"institute|institut|instituto|school|college|engineering|systems|technologies|technology|"
                      r"industries|works|studio|team)\.?$")


def company_value(v: str):
    v = v.strip().strip(":;,|")
    if not v or len(v) > 60 or LABEL_WORDS.search(v) or any(ch.isdigit() for ch in v) or "," in v:
        return None
    toks = NAME_TOK.findall(v)
    if not toks or len(toks) > 5 or not all(t[0].isupper() or len(t) <= 3 for t in toks):
        return None
    if norm_key(v) in PLACEHOLDERS or norm_key(v) in SOFTWARE or any(norm_key(t) in ENG_NOUNS for t in toks):
        return None
    if ORG_TAIL.search(v):
        return v
    words = sum(1 for t in toks if len(t) > 2 and is_word(t.lower()))
    if len(toks) == 1:
        return v if len(toks[0]) >= 4 and not is_word(toks[0].lower()) else None
    return v if words * 2 <= len(toks) else None


def code_value(v: str):
    v = v.strip().strip(":;,|")
    first = v.split()[0] if v.split() else ""
    for c in (v, first):
        if CODE.match(c) and 4 <= len(c) <= 32 and not DATE.match(c) and not DIM.match(c):
            digits = sum(ch.isdigit() for ch in c)
            letters = sum(ch.isalpha() for ch in c)
            if (letters and digits >= 2) or digits >= 5:
                return c
    return None


def phone_value(v: str):
    v = v.strip()
    m = re.search(r"\+?\(?\d[\d ()./-]{6,22}\d", v)
    if not m:
        return None
    p = m.group(0).strip()
    digits = sum(ch.isdigit() for ch in p)
    if digits < 7 or DATE.match(p) or re.fullmatch(r"\d{4}[-./]\d{2}[-./]\d{2}", p):
        return None
    return p


def extract(scan_db: str):
    con = sqlite3.connect(f"file:{scan_db}?mode=ro", uri=True)
    found = collections.defaultdict(lambda: [0, set()])   # (value, kind) -> [count, files]

    def add(value, kind, path):
        rec = found[(value, kind)]
        rec[0] += 1
        rec[1].add(path)
    q = ("SELECT path, kind, field, value FROM observations WHERE field LIKE 'pdf.text.label.%' "
         "OR field = 'pdf.text.titleblock' OR field IN ('pdf.info.Author', 'pdf.info.Company', 'pdf.info.Manager')")
    tb_text = collections.defaultdict(list)
    for path, kind, field, value in con.execute(q):
        if not value:
            continue
        top = path.split("!/", 1)[0]
        if field == "pdf.text.titleblock":
            tb_text[path].append(value)
            v = value.strip()
            if INIT_SURNAME.match(v) or HONORIFIC.match(v):
                pv = person_value(v)
                if pv:
                    add(pv, "person", top)
            continue
        if field in ("pdf.info.Author", "pdf.info.Manager"):
            pv = person_value(value)
            if pv and len(pv.split()) >= 2:
                add(pv, "person", top)
            continue
        if field == "pdf.info.Company":
            cv = company_value(value)
            if cv:
                add(cv, "company", top)
            continue
        if kind == "person":
            pv = person_value(value)
            if pv:
                add(pv, "person", top)
        elif kind == "company":
            cv = company_value(value)
            if cv and ORG_TAIL.search(cv):
                add(cv, "company", top)
        elif kind in ("part_number", "document_number"):
            cv = code_value(value)
            if cv:
                add(cv, "part_number", top)
        elif kind == "contact":
            ph = phone_value(value)
            if ph:
                add(ph, "phone", top)
    # the drawing's own number: a code in its file name that its title block repeats
    for path, lines in tb_text.items():
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        text = " ".join(lines)
        for tok in re.findall(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", stem):
            cv = code_value(tok)
            if cv and cv == tok and re.search(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", text):
                add(cv, "part_number", path.split("!/", 1)[0])
    return found


ACCOUNT_FIELDS = ("cfb.prop.summary.4", "cfb.prop.summary.8", "sw.HeaderN.author", "sw.Contents/Config-N-ModelHeader.author",
                  "sw.prop.SW-Last Saved By", "sw.prop.Author", "sw.prop.SW-Author", "sw.lockfile.user",
                  "dwg.summary.lastsavedby", "dwg.summary.author", "cfb.prop.user.Username", "cfb.prop.user.Last Author",
                  "cfb.prop.docsummary.14", "cfb.prop.inventor.design.9")


def saved_accounts(scan_db: str):
    """Accounts that saved native files (OLE/SolidWorks/DWG): {value: n_files}."""
    con = sqlite3.connect(f"file:{scan_db}?mode=ro", uri=True)
    q = f"SELECT value, path FROM observations WHERE field IN ({','.join('?' * len(ACCOUNT_FIELDS))})"
    found = collections.defaultdict(set)
    for value, path in con.execute(q, ACCOUNT_FIELDS):
        v = (value or "").strip().strip("\x00")
        k = norm_key(v)
        if (not v or len(v) < 3 or len(v) > 40 or not k or k in PLACEHOLDERS or k in SOFTWARE or v.isdigit()
                or any(c in v for c in "\\/:@") or not any(ch.isalpha() for ch in v)
                or (" " not in v and len(v) > 3 and is_word(v.lower()) and not v[:1].isupper())
                or k in ("operator", "space", "traceparts", "test", "temp", "student", "cad", "design")):
            continue
        found[v].add(path.split("!/", 1)[0])
    return {v: len(p) for v, p in found.items()}


def entries(scan_db: str):
    """[(value, kind, mode, n_files)] for build_plan."""
    out = []
    for (value, kind), (n, files) in sorted(extract(scan_db).items()):
        exact = kind in ("part_number", "phone") or (kind == "person" and value.isupper() and len(value) <= 4)
        out.append((value, kind, "exact" if exact else "flex", len(files)))
    for value, n in sorted(saved_accounts(scan_db).items()):
        out.append((value, "account", "flex" if " " in value.strip() else "exact", n))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-db", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    found = extract(a.scan_db)
    rows = collections.Counter()
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("# value\tkind\tmode   (title-block identities; generated by deid.identity.pdfids)\n")
        for (value, kind), (n, files) in sorted(found.items()):
            if kind == "part_number" or (kind == "person" and value.isupper() and len(value) <= 4):
                mode = "exact"
            elif kind == "phone":
                mode = "exact"
            else:
                mode = "flex"
            f.write(f"{value}\t{kind}\t{mode}\n")
            rows[kind] += 1
    print(f"wrote {a.out}: {dict(rows)}")


if __name__ == "__main__":
    main()
