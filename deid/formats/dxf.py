"""ASCII DXF (binary DXF is not claimed: the pipeline copies it).

Streams group-code/value line pairs and edits only string values with
identity-bearing group codes (text, attribute values, layer/block/style names,
XDATA strings, comments, xref/image paths, selected header variables).  Every
other line, including handles, numbers, code lines and line endings, is
copied byte for byte.  Names (layers, blocks, styles) are rewritten by the same
deterministic function everywhere they occur, so references stay consistent.
"""
from __future__ import annotations

import hashlib
import os
import re

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION)
from deid.formats._neutral import PlanCache, ObsCollector, looks_like_reference, ref_kind

CHUNK = 8 << 20

TEXT_CODES = {1, 3, 4, 300, 301, 302, 303, 304, 305, 306, 307, 308, 309, 999, 1000,
              470, 471, 472, 473, 474, 475, 476, 477, 478, 479}
NAME_CODES = {2, 7, 8, 1003, 410, 411, 412, 413, 414, 415, 416, 417, 418, 419, 430, 431, 432, 433,
              434, 435, 436, 437, 438, 439}
EDIT_CODES = TEXT_CODES | NAME_CODES

HEADER_EDIT_VARS = {"$LASTSAVEDBY", "$PROJECTNAME", "$HYPERLINKBASE", "$STYLESHEET", "$AUTHOR", "$TITLE",
                    "$SUBJECT", "$KEYWORDS", "$COMMENTS", "$DWGNAME", "$LOGINNAME"}
HEADER_KIND = {"$LASTSAVEDBY": "person", "$AUTHOR": "person", "$LOGINNAME": "account",
               "$PROJECTNAME": "project"}
REF_ENTITIES = {"IMAGEDEF", "PDFDEFINITION", "DWFDEFINITION", "DGNDEFINITION", "UNDERLAYDEFINITION",
                "RASTERVARIABLES", "ACAD_PROXY_OBJECT"}
SKIP_SECTIONS = {"CLASSES", "THUMBNAILIMAGE", "ACDSDATA"}
SKIP_ENTITIES = {"TABLE", "CLASS", "APPID", "SECTION", "ENDTAB", "ACDBPLACEHOLDER", "MATERIAL", "VISUALSTYLE",
                 "MLEADERSTYLE", "TABLESTYLE", "CELLSTYLEMAP", "ACDBDETAILVIEWSTYLE", "ACDBSECTIONVIEWSTYLE",
                 "SCALE", "DICTIONARYVAR", "DICTIONARY", "ACDBDICTIONARYWDFLT", "LTYPE", "DIMSTYLE",
                 "MLINESTYLE", "SUN", "VBA_PROJECT", "LAYER_FILTER", "LAYER_INDEX", "SPATIAL_FILTER",
                 "SPATIAL_INDEX", "DIMASSOC", "WIPEOUTVARIABLES", "RASTERVARIABLES", "GEODATA",
                 "SORTENTSTABLE", "IDBUFFER", "ACAD_PROXY_OBJECT", "ACAD_PROXY_ENTITY", "DATATABLE",
                 "PLOTSETTINGS", "ACDBASSOCNETWORK", "ACDBASSOCACTION", "ACDBASSOCDEPENDENCY",
                 "ACDBASSOCGEOMDEPENDENCY", "ACDBPERSSUBENTMANAGER", "ACDB_DYNAMICBLOCKPURGEPREVENTER_VERSION",
                 "ACDB_BLOCKREPRESENTATION_DATA", "ACAD_EVALUATION_GRAPH", "BLOCKVISIBILITYPARAMETER",
                 "VLO-VL", "ACDBNAVISWORKSMODELDEF", "ACDBPOINTCLOUDDEFEX", "ACDBPOINTCLOUDEX"}
SKIP_CODES_BY_ENTITY = {"STYLE": {3, 4, 1000, 1001}, "DIMENSION": {3}, "LEADER": {3}, "TOLERANCE": {3},
                        "ARC_DIMENSION": {3}, "LARGE_RADIAL_DIMENSION": {3}, "MULTILEADER": {3}}
HEADER_NAME_VARS = {"$CLAYER", "$TEXTSTYLE", "$UCSNAME", "$PUCSNAME", "$DIMTXSTY"}
_RESERVED = re.compile(r"(?i)^(?:\*|\$|acad|acdb|acad_|aecc|aec_|ac_|standard$|continuous$|bylayer$|byblock$|"
                       r"defpoints$|model$|layout\d*$|0$|annotative$|iso|ansi|legacy$)")
_STRUCT = re.compile(r"^(?:[A-Za-z_]*\{|\})$")   # structural markers / class names
_BAD_NAME = re.compile(r'[<>/\\":;?*|=`\x00-\x1f]')
_UESC = re.compile(r"\\U\+([0-9A-Fa-f]{4})")

CODEPAGES = {
    "ansi_874": "cp874", "ansi_932": "cp932", "ansi_936": "gbk", "ansi_949": "cp949", "ansi_950": "cp950",
    "ansi_1250": "cp1250", "ansi_1251": "cp1251", "ansi_1252": "cp1252", "ansi_1253": "cp1253",
    "ansi_1254": "cp1254", "ansi_1255": "cp1255", "ansi_1256": "cp1256", "ansi_1257": "cp1257",
    "ansi_1258": "cp1258", "mac-roman": "mac_roman", "dos437": "cp437", "dos850": "cp850",
    "dos852": "cp852", "dos855": "cp855", "dos857": "cp857", "dos860": "cp860", "dos861": "cp861",
    "dos863": "cp863", "dos864": "cp864", "dos865": "cp865", "dos869": "cp869", "iso8859-1": "latin-1",
}


def _sniff_encoding(head: bytes) -> str:
    m = re.search(rb"\$ACADVER\s*\r?\n\s*1\s*\r?\n\s*AC(\d{4})", head)
    if m and int(m.group(1)) >= 1021:
        return "utf-8"
    m = re.search(rb"\$DWGCODEPAGE\s*\r?\n\s*3\s*\r?\n\s*([^\r\n]+)", head)
    if m:
        return CODEPAGES.get(m.group(1).strip().decode("latin-1").lower(), "cp1252")
    return "cp1252"


def _dec(b: bytes, encd: str) -> str:
    try:
        s = b.decode(encd)
    except UnicodeDecodeError:
        s = b.decode("latin-1")
    if "\\U+" in s:
        s = _UESC.sub(lambda m: chr(int(m.group(1), 16)), s)
    return s


def _enc(s: str, encd: str) -> bytes:
    out = []
    for ch in s:
        try:
            out.append(ch.encode(encd))
        except UnicodeEncodeError:
            out.append(("\\U+%04X" % ord(ch)).encode("ascii") if ord(ch) <= 0xFFFF else b"?")
    return b"".join(out)


def _sanitize_name(s: str) -> str:
    return _BAD_NAME.sub("_", s)


def _pairs(f):
    """Yield lists of lines (each with its line terminator) in even-length batches,
    so lines[i] is a group code and lines[i+1] its value."""
    carry = b""
    while True:
        data = f.read(CHUNK)
        buf = carry + data
        if not buf:
            return
        if data:
            cut = buf.rfind(b"\n") + 1
            if cut == 0:
                carry = buf
                continue
            part, carry = buf[:cut], buf[cut:]
        else:
            part, carry = buf, b""
        lines = part.splitlines(keepends=True)
        if len(lines) % 2 and data:
            carry = lines.pop() + carry
        yield lines
        if not data:
            return


def _code(line: bytes):
    try:
        return int(line)
    except ValueError:
        return None


class _State:
    __slots__ = ("section", "entity", "var", "next_is_section", "attr_tag", "in_props")

    def __init__(self):
        self.section = ""
        self.entity = ""
        self.var = ""
        self.next_is_section = False
        self.attr_tag = ""
        self.in_props = False


def _walk(f, encd, visit):
    """visit(code, value_str, state) -> new value str or None.  Yields output line batches."""
    st = _State()
    for lines in _pairs(f):
        n = len(lines)
        i = 0
        changed = False
        while i + 1 < n:
            cl = lines[i]
            code = _code(cl)
            if code is None:
                raise ValueError("malformed group code line")
            if code == 0:
                v = lines[i + 1].strip()
                st.entity = v.decode("latin-1")
                st.next_is_section = st.entity == "SECTION"
                if st.entity == "ENDSEC":
                    st.section = ""
                i += 2
                continue
            if code == 2 and st.next_is_section:
                st.section = lines[i + 1].strip().decode("latin-1")
                st.next_is_section = False
                i += 2
                continue
            if code == 9 and st.section == "HEADER":
                st.var = lines[i + 1].strip().decode("latin-1")
                i += 2
                continue
            if code in EDIT_CODES and st.section not in SKIP_SECTIONS and (
                    st.section == "HEADER" or st.entity not in SKIP_ENTITIES):
                if st.section == "HEADER":
                    if st.var not in HEADER_EDIT_VARS and st.var not in HEADER_NAME_VARS:
                        i += 2
                        continue
                elif code in SKIP_CODES_BY_ENTITY.get(st.entity, ()):
                    i += 2
                    continue
                vl = lines[i + 1]
                body = vl.rstrip(b"\r\n")
                eol = vl[len(body):]
                if body.strip():
                    val = _dec(body, encd)
                    if (((code in NAME_CODES or st.entity == "LAYOUT") and _RESERVED.match(val.strip()))
                            or _STRUCT.match(val.strip())):
                        i += 2
                        continue
                    new = visit(code, val, st)
                    if new is not None and new != val:
                        lines[i + 1] = _enc(new, encd) + eol
                        changed = True
            i += 2
        yield lines, changed


class DxfAdapter(Adapter):
    name = "dxf"
    version = "1"
    priority = 9
    magics = ()
    extensions = (".dxf",)
    path_independent = False
    timeout_s = 1800.0

    def matches(self, ctx, head):
        if ctx.magic not in ("text", "unknown"):
            return False
        return bool(re.match(rb"\s*(?:999\s*\r?\n[^\n]*\n\s*)*0\s*\r?\n\s*SECTION\s*\r?\n", head))

    @staticmethod
    def _kind(code, st):
        if st.section == "HEADER":
            return HEADER_KIND.get(st.var)
        return None

    def scan(self, ctx, src_path):
        with open(src_path, "rb") as f:
            head = f.read(1 << 16)
        encd = _sniff_encoding(head)
        col = ObsCollector(cap=4000)
        counts = {}

        def visit(code, val, st):
            v = val.strip()
            if not any(ch.isalpha() for ch in v):
                return None
            ent = st.entity
            if st.section == "HEADER":
                if st.var in HEADER_NAME_VARS:
                    return None
                col.add(HEADER_KIND.get(st.var, "free_text"), f"dxf.header.{st.var}", v, st.var, 0.8)
                return None
            if code == 1 and (ent in REF_ENTITIES or (ent == "BLOCK" and looks_like_reference(v))):
                col.ref(v, ref_kind(v), f"dxf.{ent}.path")
                col.add("path", f"dxf.{ent}.path", v, ent, 0.7)
                return None
            if looks_like_reference(v) and code in TEXT_CODES:
                col.ref(v, ref_kind(v), f"dxf.{ent}.{code}")
            if code in NAME_CODES:
                if code == 8 or v.startswith("*") or v.upper() in ("0", "STANDARD", "CONTINUOUS", "BYLAYER",
                                                                    "BYBLOCK", "DEFPOINTS", "ANNOTATIVE"):
                    return None
                col.add("free_text", f"dxf.{ent}.name{code}", v, ent, 0.4)
                return None
            col.add("free_text", f"dxf.{ent}.{code}", v, ent, 0.5)
            return None

        try:
            with open(src_path, "rb") as f:
                npairs = 0
                for lines, _ in _walk(f, encd, visit):
                    npairs += len(lines) // 2
        except ValueError as e:
            return ScanResult(observations=col.obs, status="source_exception", notes=[f"malformed DXF: {e}"])
        return ScanResult(observations=col.obs, references=col.refs,
                          fingerprint={"pairs": npairs, "encoding": encd})

    def rewrite(self, ctx, src_path, dst_path, plan):
        with open(src_path, "rb") as f:
            head = f.read(1 << 16)
        encd = _sniff_encoding(head)
        pc = PlanCache(plan, ctx)
        nchg = 0

        def visit(code, val, st):
            nonlocal nchg
            ent = st.entity
            if code == 1 and (ent in REF_ENTITIES or (ent == "BLOCK" and looks_like_reference(val))):
                new = pc.ref(val)
            elif code in NAME_CODES or (st.section == "HEADER" and st.var in HEADER_NAME_VARS):
                new = pc.text(val)
                if new != val:
                    new = _sanitize_name(new)
            elif st.section == "HEADER" and st.var in HEADER_KIND:
                new = pc.sub(val, HEADER_KIND[st.var])
            elif looks_like_reference(val.strip()) and code in (1, 300, 1000):
                new = pc.ref(val)
            else:
                new = pc.text(val)
            if new != val:
                nchg += 1
            return new

        tmp = dst_path + ".part"
        changed_any = False
        try:
            with open(src_path, "rb") as f, open(tmp, "wb", buffering=1 << 20) as out:
                for lines, changed in _walk(f, encd, visit):
                    changed_any |= changed
                    out.write(b"".join(lines))
        except ValueError as e:
            if os.path.exists(tmp):
                os.unlink(tmp)
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[f"malformed DXF: {e}"])
        if not changed_any:
            os.unlink(tmp)
            return RewriteResult(status=VERIFIED_UNCHANGED)
        os.replace(tmp, dst_path)
        return RewriteResult(status=REWRITTEN, counts={"values_replaced": nchg})

    def verify(self, ctx, src_path, dst_path, plan):
        a = dxf_fingerprint(src_path)
        b = dxf_fingerprint(dst_path)
        notes = [f"{k} differs" for k in ("masked_sha256", "pairs", "entities") if a[k] != b[k]]
        if not notes and os.path.getsize(src_path) <= (8 << 20):
            ea, eb = _ezdxf_summary(src_path), _ezdxf_summary(dst_path)
            if ea is not None and ea != eb:
                notes.append("ezdxf summary differs")
        return VerifyResult(ok=not notes, notes=notes, fingerprint=b)


def dxf_fingerprint(path: str) -> dict:
    """Masked hash: every code line and every value of a non-editable code, plus
    value line terminators; editable string values are replaced by a marker."""
    h = hashlib.sha256()
    pairs = 0
    ents = 0
    with open(path, "rb") as f:
        for lines in _pairs(f):
            for i in range(0, len(lines) - 1, 2):
                cl, vl = lines[i], lines[i + 1]
                h.update(cl)
                c = _code(cl)
                if c in EDIT_CODES:
                    body = vl.rstrip(b"\r\n")
                    h.update(b"\0" + vl[len(body):])
                else:
                    h.update(vl)
                if c == 0:
                    ents += 1
                pairs += 1
    return {"masked_sha256": h.hexdigest(), "pairs": pairs, "entities": ents}


def _ezdxf_summary(path):
    try:
        import ezdxf
        doc = ezdxf.readfile(path)
    except Exception:
        return None
    try:
        msp = doc.modelspace()
        return (len(msp), len(doc.blocks), len(doc.layers), len(doc.entitydb),
                sorted({e.dxftype() for e in msp}))
    except Exception:
        return ("err",)


register(DxfAdapter())
