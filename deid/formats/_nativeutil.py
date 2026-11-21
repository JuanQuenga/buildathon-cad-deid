"""Shared helpers for the docs-native adapters (pdf, images, cfb, solidworks,
zippkg, dwg, creo, catia, opaque binaries).

Nothing here registers an adapter.  Notes/counts never carry raw values.
"""
from __future__ import annotations

import os
import re
import json
import struct
from typing import Iterable, Optional

from deid.api import Observation, Reference

# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------


def sub_value(plan, value: str, kind: Optional[str] = None) -> str:
    """Pseudonymise a structured field value: exact alias first, then free-text
    replacement.  Returns the input unchanged when nothing applies."""
    if not value:
        return value
    try:
        a = plan.alias(value, kind)
    except Exception:
        a = None
    if a is None and value.strip() != value:
        try:
            a2 = plan.alias(value.strip(), kind)
        except Exception:
            a2 = None
        if a2 is not None:
            lead = value[: len(value) - len(value.lstrip())]
            trail = value[len(value.rstrip()):]
            a = lead + a2 + trail
    if a is not None:
        return a
    return plan.replace_text(value)


def sub_text(plan, text: str) -> str:
    if not text:
        return text
    return plan.replace_text(text)


def fit_len(s: str, n: int, pad: str = " ") -> str:
    """Truncate or pad to exactly n characters (last-resort for fixed slots)."""
    return s[:n] if len(s) >= n else s + pad * (n - len(s))


def same_len_text(plan, value: str, kind: Optional[str] = None, *, pad: str = " ") -> Optional[str]:
    """Return a replacement with exactly len(value) characters, or None if the
    plan's replacement cannot be made to fit without losing information
    (longer replacements are never truncated silently: None means "skip")."""
    new = sub_value(plan, value, kind)
    if new == value:
        return value
    if len(new) == len(value):
        return new
    if len(new) < len(value):
        return new + pad * (len(value) - len(new))
    return None


# ---------------------------------------------------------------------------
# Observation helpers
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(?:https?|ftp)://[^\s<>\"')\]]+|www\.[A-Za-z0-9\-]+\.[A-Za-z0-9.\-/_%?=&#]+", re.I)
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{2,4}[\s.\-]\d{3,4}[\s.\-]\d{3,4}")
SCHEMA_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:schemas\.openxmlformats\.org|schemas\.microsoft\.com|"
                           r"www\.w3\.org|w3\.org|purl\.org|ns\.adobe\.com|schemas\.slic3r\.org|"
                           r"schemas\.bambulab\.com|www\.solidworks\.com/(?:sw|config)|openoffice\.org/|"
                           r"urn:oasis|docs\.oasis-open\.org|xml\.org|iptc\.org/std|"
                           r"schemas\.autodesk\.com|www\.freecad(?:web)?\.org/?$)", re.I)
WINPATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\x00-\x1f\"*?<>|]{2,260}")
USERDIR_RE = re.compile(r"(?:\\|/)(?:Users|Documents and Settings|home)(?:\\|/)([^\\/\x00-\x1f]{1,64})", re.I)

# Title-block labels (multilingual-ish).  value of the label -> observation kind
TB_LABELS = [
    (re.compile(r"\b(drawn|drn|drawn by|draftsman|drafter|designer|designed|design(?:ed)? by|dessin[ée]|gezeichnet|gez\.?|dibujado|dibuj[oó]|disegnato|author|autor|auteur|created by|creator|engineer|eng(?:ineering)?|checked|chk|chkd|checked by|gepr[üu]ft|gepr\.?|revisado|verificado|controllato|v[ée]rifi[ée]|approved|appd|appr|approved by|freigegeben|aprobado|approvato|qa|mfg|manager|responsible|name|nombre|bearb\.?|bearbeitet|norm|owner|student|alumno)\b", re.I), "person"),
    (re.compile(r"\b(company|firma|empresa|soci[ée]t[ée]|azienda|organi[sz]ation|customer|client|kunde|cliente|vendor|supplier|manufacturer|university|universidad|school|lab)\b", re.I), "company"),
    (re.compile(r"\b(project|projekt|proyecto|projet|progetto|job|program)\b", re.I), "project"),
    (re.compile(r"\b(dwg\.? ?no|drawing (?:no|number|nr)|zeichnungs?-?nr|zeichnungsnummer|plano n|n[º°o]\.? de plano|doc(?:ument)?\.? ?(?:no|number|nr)|sheet no)\b", re.I), "document_number"),
    (re.compile(r"\b(part (?:no|number|nr)|p/?n|item no|art(?:ikel)?\.?-?nr|sachnummer|teilenummer|n[º°o]\.? de pieza|ref(?:erence)?\.? no)\b", re.I), "part_number"),
    (re.compile(r"\b(title|titel|t[íi]tulo|titre|benennung|description|denominaci[oó]n|designation|bezeichnung)\b", re.I), "part_name"),
    (re.compile(r"\b(e-?mail|mail|tel|phone|fax|telefon|tel[ée]fono)\b", re.I), "contact"),
    (re.compile(r"\b(address|adresse|direcci[oó]n|anschrift)\b", re.I), "address"),
]


def label_kind(text: str) -> Optional[str]:
    t = text.strip()
    if not t or len(t) > 40:
        return None
    for rx, kind in TB_LABELS:
        if rx.search(t):
            return kind
    return None


def contact_observations(text: str, field: str, out: list, limit: int = 200) -> None:
    """Typed observations for emails, URLs and phone numbers inside free text."""
    if not text:
        return
    n = 0
    for m in EMAIL_RE.finditer(text):
        out.append(Observation("email", field, m.group(0), "regex", 0.95)); n += 1
        if n >= limit:
            return
    for m in URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;")
        if SCHEMA_URL_RE.search(u):
            continue
        out.append(Observation("url", field, u, "regex", 0.7)); n += 1
        if n >= limit:
            return
    for m in WINPATH_RE.finditer(text):
        out.append(Observation("path", field, m.group(0).rstrip(), "windows path", 0.6)); n += 1
        if n >= limit:
            return
    for m in USERDIR_RE.finditer(text):
        out.append(Observation("account", field, m.group(1), "user profile dir", 0.8)); n += 1
        if n >= limit:
            return


def add_obs(out: list, kind: str, field: str, value, evidence: str = "", confidence: float = 1.0) -> None:
    if value is None:
        return
    v = str(value).strip("\x00").strip()
    if not v or len(v) > 4000:
        return
    out.append(Observation(kind, field, v, evidence, confidence))


def dedupe_obs(obs: list, cap: int = 5000) -> list:
    seen = set()
    res = []
    for o in obs:
        k = (o.kind, o.field, o.value)
        if k in seen:
            continue
        seen.add(k)
        res.append(o)
        if len(res) >= cap:
            break
    return res


def dedupe_refs(refs: list, cap: int = 5000) -> list:
    seen = set()
    res = []
    for r in refs:
        k = (r.raw, r.kind, r.field)
        if k in seen:
            continue
        seen.add(k)
        res.append(r)
        if len(res) >= cap:
            break
    return res


# ---------------------------------------------------------------------------
# MFC CArchive strings (used by SolidWorks, Inventor, Solid Edge streams)
# ---------------------------------------------------------------------------
# Unicode CString: FF FE FF <len8> | FF FE FF FF <len16> | FF FE FF FF FF FF <len32>, then UTF-16LE.
# ANSI CString:    <len8> | FF <len16> | FF FF FF <len32>, then bytes (we only use the unicode form
# because ANSI length bytes are too ambiguous to find by scanning).
_CSTR_U = re.compile(rb"\xff\xfe\xff")


def iter_cstrings_u16(buf: bytes, min_chars: int = 1, max_chars: int = 4096):
    """Yield (char_start_offset, nchars, text) for MFC unicode CStrings.

    Only accepts strings whose characters are all valid, non-control UTF-16
    code units (tab/newline/CR allowed) so random binary is not mistaken for a
    string."""
    n = len(buf)
    for m in _CSTR_U.finditer(buf):
        p = m.end()
        if p >= n:
            continue
        ln = buf[p]
        q = p + 1
        if ln == 0xFF:
            if q + 2 > n:
                continue
            ln = struct.unpack_from("<H", buf, q)[0]
            q += 2
            if ln == 0xFFFF:
                if q + 4 > n:
                    continue
                ln = struct.unpack_from("<I", buf, q)[0]
                q += 4
        if ln < min_chars or ln > max_chars or q + 2 * ln > n:
            continue
        raw = buf[q:q + 2 * ln]
        try:
            s = raw.decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if any((ord(c) < 32 and c not in "\t\r\n") or 0xD800 <= ord(c) <= 0xDFFF or c in "￾￿" for c in s):
            continue
        yield q, ln, s


def patch_u16_same_len(buf: bytearray, start: int, nchars: int, new: str) -> bool:
    if len(new) != nchars:
        return False
    enc = new.encode("utf-16-le")
    if len(enc) != 2 * nchars:  # surrogate pairs change unit count
        return False
    buf[start:start + 2 * nchars] = enc
    return True


# ---------------------------------------------------------------------------
# Constraints (references we could not rewrite: keep the target's name)
# ---------------------------------------------------------------------------
CONSTRAINTS_ENV = "DEID_CONSTRAINTS_DIR"


def record_constraint(ctx, raw_ref: str, reason: str, fmt: str) -> None:
    """Record "the file referenced by raw_ref from ctx must keep its basename"
    in the private constraints dir (JSON lines, one file per process).
    Format documented in docs/native-formats.md."""
    d = os.environ.get(CONSTRAINTS_ENV)
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        rec = {"v": 1, "source": ctx.logical_path, "raw": raw_ref, "keep": "basename",
               "reason": reason, "format": fmt}
        with open(os.path.join(d, f"native-{os.getpid()}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def path_basename(raw: str) -> str:
    s = raw.replace("\\", "/")
    return s.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Property-name -> observation kind
# ---------------------------------------------------------------------------
_PROP_KINDS = [
    (re.compile(r"e-?mail", re.I), "email"),
    (re.compile(r"phone|fax|tel[eé]?fon", re.I), "phone"),
    (re.compile(r"address|adresse|street|city|zip|postal", re.I), "address"),
    (re.compile(r"company|organi[sz]ation|customer|client|vendor|supplier|manufacturer|firma|empresa|"
                r"soci[eé]t[eé]|kunde|cliente|owner ?org|university|school", re.I), "company"),
    (re.compile(r"author|drawn|draftsman|designer|designed|engineer|checked|chk|approv|created ?by|"
                r"creator|last ?saved|lastmodifiedby|modified ?by|saved ?by|owner|manager|user|"
                r"responsible|zeichner|bearbeit|gepr|dibuj|revis(?:ed|ado) ?by|qa|mfg", re.I), "person"),
    (re.compile(r"project|projekt|proyecto|projet|program|job", re.I), "project"),
    (re.compile(r"drawing ?n|dwg ?n|doc(?:ument)? ?n|zeichnungs", re.I), "document_number"),
    (re.compile(r"part ?n|partno|p/?n\b|number|nummer|n[uú]mero|item ?n|sku|article|artikel|code|"
                r"stock ?code|bom", re.I), "part_number"),
    (re.compile(r"description|title|name|bezeichnung|benennung|denominaci|titel|t[ií]tulo|"
                r"subject|comment|keyword|note", re.I), "part_name"),
    (re.compile(r"url|web|homepage|link|domain", re.I), "url"),
]
_PROP_SKIP = re.compile(r"^(?:\$?prp|sw-?(?:date|fecha|short ?date|long ?date)|date|datum|fecha|"
                        r"revision|rev|material|mass|weight|finish|units?|scale|sheet|qty|quantity|"
                        r"volume|density|tolerance|cost|version|configuration ?name)$", re.I)


def prop_kind(name: str) -> Optional[str]:
    """Observation kind for a named property; None for clearly non-identifying keys."""
    n = (name or "").strip()
    if not n:
        return "free_text"
    if _PROP_SKIP.match(n):
        return None
    for rx, k in _PROP_KINDS:
        if rx.search(n):
            return k
    return "free_text"


# ---------------------------------------------------------------------------
# Generic XML value rewriting (keeps markup bytes intact except changed values)
# ---------------------------------------------------------------------------
_XML_TEXT = re.compile(r">([^<>]+)<")
_XML_ATTR = re.compile(r"(\s)([A-Za-z_][\w.:\-]*)=(\"[^\"]*\"|'[^']*')")
_XML_ATTR_KEEP = {"name", "pid", "fmtid", "id", "type", "typeid", "xsi:type", "version", "encoding",
                  "standalone", "schemaversion", "programversion", "fileversion", "Target", "TargetMode",
                  "Type", "ContentType", "Extension", "PartName", "count", "value_type", "Id"}


def xml_unescape(s: str) -> str:
    def ent(m):
        e = m.group(1)
        if e.startswith("#x") or e.startswith("#X"):
            try:
                return chr(int(e[2:], 16))
            except ValueError:
                return m.group(0)
        if e.startswith("#"):
            try:
                return chr(int(e[1:]))
            except ValueError:
                return m.group(0)
        return {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}.get(e, m.group(0))
    return re.sub(r"&(#?[A-Za-z0-9]+);", ent, s)


def xml_escape(s: str, quote: Optional[str] = None) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote == '"':
        s = s.replace('"', "&quot;")
    elif quote == "'":
        s = s.replace("'", "&apos;")
    return s


def rewrite_xml_values(text: str, value_fn, attr_keep=None) -> tuple:
    """Apply value_fn(value, where) -> new_value to every text node and attribute
    value (unescaped), re-escaping results.  `where` is "text" or the attribute
    name.  Comments/CDATA/processing instructions are treated as text nodes only
    when they appear between tags (rare in CAD XML).  Returns (new_text, n_changed)."""
    keep = _XML_ATTR_KEEP if attr_keep is None else attr_keep
    n = [0]

    def tnode(m):
        raw = m.group(1)
        if not raw.strip() or raw.lstrip().startswith(("<!--", "<![CDATA[", "<?")):
            return m.group(0)
        v = xml_unescape(raw)
        nv = value_fn(v, "text")
        if nv == v:
            return m.group(0)
        n[0] += 1
        return ">" + xml_escape(nv) + "<"

    def anode(m):
        sp, name, q = m.group(1), m.group(2), m.group(3)
        if name in keep or name.startswith("xmlns"):
            return m.group(0)
        quote = q[0]
        v = xml_unescape(q[1:-1])
        nv = value_fn(v, name)
        if nv == v:
            return m.group(0)
        n[0] += 1
        return f"{sp}{name}={quote}{xml_escape(nv, quote)}{quote}"

    # attributes live inside tags; text between tags
    out = []
    pos = 0
    for tm in re.finditer(r"<[^<>]*>", text):
        seg = text[pos:tm.start()]
        if seg:
            out.append(_XML_TEXT.sub(tnode, ">" + seg + "<")[1:-1])
        tag = tm.group(0)
        if not tag.startswith(("<?", "<!")):
            tag = _XML_ATTR.sub(anode, tag)
        out.append(tag)
        pos = tm.end()
    seg = text[pos:]
    if seg:
        out.append(seg)
    return "".join(out), n[0]


CAD_REF_RE = re.compile(r"\.(sldprt|sldasm|slddrw|slddrt|sldlfp|prtdot|asmdot|drwdot|ipt|iam|idw|ipn|dwg|dxf|"
                        r"par|asm|psm|pwd|dft|prt|drw|catpart|catproduct|catdrawing|step|stp|igs|iges|x_t|x_b|"
                        r"sat|stl|jt|fcstd|f3d|3mf|pdf|png|jpg|bmp|xls|xlsx|csv|txt|ipj)(?:\.\d+)?$", re.I)


def looks_like_ref(s: str) -> bool:
    s = s.strip()
    if not s or len(s) > 400 or "\n" in s:
        return False
    return bool(CAD_REF_RE.search(s))


def map_ref_same_len(plan, ctx, raw: str) -> Optional[str]:
    """Reference rewrite that keeps the exact character count (for binary
    containers where a string's length cannot change).  The basename follows
    plan.map_reference and must keep its length; the directory part is
    de-identified with a same-length replacement.  Returns the new string, the
    unchanged string if nothing to do, or None if impossible."""
    try:
        new = plan.map_reference(ctx, raw)
    except Exception:
        new = raw
    sep = max(raw.rfind("\\"), raw.rfind("/"))
    nsep = max(new.rfind("\\"), new.rfind("/"))
    ob, nb = raw[sep + 1:], new[nsep + 1:]
    if len(ob) != len(nb):
        return None
    od = raw[:sep + 1]
    nd = plan.replace_text(od) if od else od
    if len(nd) != len(od):
        try:
            nd = plan.replace_bytes(od.encode("utf-16-le"), ("utf-16-le",), True).decode("utf-16-le")
        except Exception:
            nd = od
        if len(nd) != len(od):
            nd = od
    return nd + nb
