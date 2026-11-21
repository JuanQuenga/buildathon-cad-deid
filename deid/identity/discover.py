"""Independent identity discovery scanner (all formats, streaming, stdlib).

Extracts *candidate* identity values with their field context, so the plan
builder can decide by evidence (frequency, field position, spread):

* STEP header FILE_NAME author/organization/authorisation, PERSON/ORGANIZATION
  entities, addresses (STEP string escapes decoded)
* IGES global section sender/author/organization
* Parasolid text header USER= / SITE=, SAT header
* PDF Info dictionary and XMP (also inside Flate streams), PostScript DSC %%For/%%Creator
* OLE/CFB SummaryInformation / DocumentSummaryInformation (author, last author,
  company, manager) via a minimal stdlib property-set reader
* SVG/XML/ODF/3MF/FCStd metadata (dc:creator, meta:initial-creator, Author, ...)
* emails, URLs (hosts), user directories in ASCII and UTF-16LE strings of any file,
  zip members and Flate streams

    python -m deid.identity.discover --root INPUT --manifest M --out WORK/identity/discovery.jsonl
           [--jobs 12] [--prefix P] [--max-bytes 67108864]

Output is PRIVATE (raw values).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib
from concurrent.futures import ProcessPoolExecutor, as_completed

ASCII_RUN = re.compile(rb"[\x20-\x7e\t]{6,}")
U16_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")
EMAIL = re.compile(rb"[A-Za-z0-9][A-Za-z0-9._%+-]{1,63}@(?:[A-Za-z0-9-]{2,63}\.)+[A-Za-z]{2,12}")
URL = re.compile(rb"(?i)\b(?:https?|ftp)://([A-Za-z0-9.-]{3,253})")
WWW = re.compile(rb"(?i)\bwww\.([A-Za-z0-9-]{2,63}(?:\.[A-Za-z0-9-]{2,63})+)")
USERDIR = re.compile(rb"(?i)(?:Users|Documents and Settings|home|Utilisateurs|Benutzer|Usuarios|Utenti|Gebruikers)[\\/]{1,2}([^\\/\x00-\x1f\"'<>|:*?]{1,40})[\\/]")
STEP_FILE_NAME = re.compile(rb"FILE_NAME\s*\(", re.S)
STEP_ENT = re.compile(rb"=\s*(PERSON|ORGANIZATION|PERSON_AND_ORGANIZATION_ROLE|ADDRESS|PERSONAL_ADDRESS|ORGANIZATIONAL_ADDRESS|APPROVAL_PERSON_ORGANIZATION|DOCUMENT_FILE|PRODUCT)\s*\(", re.S)
PDF_INFO = re.compile(rb"/(Author|Creator|Producer|Title|Subject|Keywords|Company|Manager|SourceModified|Owner)\s*(\((?:\\.|[^\\)]){0,400}\)|<[0-9A-Fa-f\s]{0,800}>)")
XMP_TAG = re.compile(rb"<(dc:creator|dc:rights|dc:publisher|dc:contributor|xmp:CreatorTool|pdf:Producer|pdf:Author|photoshop:AuthorsPosition|xmpRights:Owner|pdfx:[A-Za-z]+|cc:attributionName|dc:title|dc:description)[^>]*>(.*?)</\1>", re.S)
XML_META = re.compile(rb"<(meta:initial-creator|dc:creator|cp:lastModifiedBy|Author|Designer|Company|Manager|metadata\s+name=\"(?:Designer|Copyright|Title|Description|LicenseTerms|Rating|CreationDate)\")[^>]*>(.*?)</(?:meta:initial-creator|dc:creator|cp:lastModifiedBy|Author|Designer|Company|Manager|metadata)>", re.S)
FC_PROP = re.compile(rb"<Property name=\"(CreatedBy|LastModifiedBy|Company|License|LicenseURL|Comment|Label|FileName|Meta)\"[^>]*>\s*<(?:String|Map|Path|PropertyString)[^>]*value=\"([^\"]{1,300})\"", re.S)
PS_DSC = re.compile(rb"^%%(For|Creator|Title|Author|Copyright|Company):\s*(.{1,200})$", re.M)
PARASOLID_HDR = re.compile(rb"(USER|SITE|MC_MODEL|MC_ID|APPL|FILE|KEY)=([^;\r\n]{1,120})")
TITLE_LABEL = re.compile(r"(?i)\b(drawn|drawn by|designed|designed by|designer|author|checked|checked by|approved|approved by|engineer|draftsman|creator|owner|company|customer|client|gezeichnet|bearbeitet|gepr[uü]ft|dessin[eé]|auteur|v[eé]rifi[eé]|nome|nombre|dibuj[oó]|disegnato)\s*[:=]?\s+([A-Z][\w.\-']+(?:\s+[A-Z][\w.\-']+){0,3})")

MAX_MEMBER = 64 << 20


def _obs(out, kind, field, value, ev=""):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace") if not _is_latin1(value) else value.decode("latin-1")
    value = value.strip().strip("\x00")
    if not value or len(value) > 300:
        return
    out.append((kind, field, value, ev))


def _is_latin1(b):
    try:
        b.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


# ------------------------------------------------------------------ STEP
def step_decode(s: str) -> str:
    """Decode STEP string escapes: '' -> ', \\X2\\hhhh..\\X0\\, \\X\\hh, \\S\\c."""
    s = s.replace("''", "'")

    def x2(m):
        h = m.group(1)
        try:
            return "".join(chr(int(h[i:i + 4], 16)) for i in range(0, len(h), 4))
        except ValueError:
            return m.group(0)
    s = re.sub(r"\\X2\\([0-9A-Fa-f]+)\\X0\\", x2, s)
    s = re.sub(r"\\X4\\([0-9A-Fa-f]+)\\X0\\", lambda m: "".join(chr(int(m.group(1)[i:i + 8], 16)) for i in range(0, len(m.group(1)), 8)), s)
    s = re.sub(r"\\X\\([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\S\\(.)", lambda m: chr(ord(m.group(1)) + 128), s)
    return s


def step_args(buf: bytes, start: int, limit: int = 20000):
    """Parse a STEP parameter list starting right after '(' -> nested lists of strings/tokens."""
    i = start
    n = min(len(buf), start + limit)
    stack = [[]]
    while i < n:
        c = buf[i:i + 1]
        if c == b"'":
            j = i + 1
            sb = bytearray()
            while j < n:
                if buf[j:j + 1] == b"'":
                    if buf[j + 1:j + 2] == b"'":
                        sb += b"''"
                        j += 2
                        continue
                    break
                sb += buf[j:j + 1]
                j += 1
            stack[-1].append(("s", step_decode(bytes(sb).decode("latin-1").replace("\r", "").replace("\n", ""))))
            i = j + 1
        elif c == b"(":
            stack.append([])
            i += 1
        elif c == b")":
            top = stack.pop()
            if not stack:
                return top
            stack[-1].append(top)
            i += 1
        elif c in (b"/",) and buf[i:i + 2] == b"/*":
            j = buf.find(b"*/", i + 2)
            i = j + 2 if j >= 0 else n
        else:
            i += 1
    return stack[0] if stack else []


def _strings(x):
    if isinstance(x, tuple):
        return [x[1]]
    out = []
    for y in x:
        out.extend(_strings(y))
    return out


def scan_step(data: bytes, out):
    m = STEP_FILE_NAME.search(data, 0, 200000)
    if m:
        args = step_args(data, m.end())
        names = ["name", "time_stamp", "author", "organization", "preprocessor_version", "originating_system",
                 "authorisation"]
        for i, a in enumerate(args[:7]):
            fld = names[i]
            for s in _strings(a) if not isinstance(a, tuple) else [a[1]]:
                kind = {"author": "person", "organization": "company", "authorisation": "person",
                        "name": "path", "preprocessor_version": "software", "originating_system": "software"}.get(fld)
                if kind:
                    _obs(out, kind, "step.FILE_NAME." + fld, s)
    m2 = re.search(rb"FILE_DESCRIPTION\s*\(", data[:200000])
    if m2:
        for s in _strings(step_args(data, m2.end())):
            _obs(out, "free_text", "step.FILE_DESCRIPTION", s)
    for em in STEP_ENT.finditer(data):
        ent = em.group(1).decode()
        args = step_args(data, em.end(), 4000)
        strs = _strings(args)
        if ent == "PERSON":
            for s in strs:
                _obs(out, "person", "step.PERSON", s)
            if len(strs) >= 3:
                _obs(out, "person", "step.PERSON.full", " ".join(x for x in strs[1:3][::-1] if x))
        elif ent == "ORGANIZATION":
            for s in strs[:2]:
                _obs(out, "company", "step.ORGANIZATION", s)
        elif ent.endswith("ADDRESS"):
            for s in strs:
                _obs(out, "address", "step." + ent, s)
        elif ent == "DOCUMENT_FILE":
            for s in strs[:2]:
                _obs(out, "path", "step.DOCUMENT_FILE", s)
        elif ent == "PRODUCT":
            for s in strs[:2]:
                _obs(out, "part_name", "step.PRODUCT", s)


# ------------------------------------------------------------------ PDF
def _pdf_str(raw: bytes) -> str:
    if raw.startswith(b"<"):
        h = re.sub(rb"\s", b"", raw[1:-1])
        try:
            b = bytes.fromhex(h.decode() + ("0" if len(h) % 2 else ""))
        except ValueError:
            return ""
    else:
        b = raw[1:-1]
        b = re.sub(rb"\\([nrtbf()\\])", lambda m: {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                                                   b"f": b"\f"}.get(m.group(1), m.group(1)), b)
        b = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) & 255]), b)
    if b.startswith(b"\xfe\xff"):
        return b[2:].decode("utf-16-be", "replace")
    if b.startswith(b"\xff\xfe"):
        return b[2:].decode("utf-16-le", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


INFO_KIND = {"Author": "person", "Creator": "software", "Producer": "software", "Company": "company",
             "Manager": "person", "Owner": "person", "Title": "free_text", "Subject": "free_text",
             "Keywords": "free_text", "SourceModified": "free_text"}


def scan_pdf(data: bytes, out, deep=True):
    for m in PDF_INFO.finditer(data):
        _obs(out, INFO_KIND[m.group(1).decode()], "pdf.info." + m.group(1).decode(), _pdf_str(m.group(2)))
    scan_xmp(data, out, "pdf.xmp")
    if not deep:
        return
    # Flate streams (object streams can hold the Info dict; content streams hold text)
    n = 0
    for m in re.finditer(rb"stream\r?\n", data):
        n += 1
        if n > 4000:
            break
        try:
            d = zlib.decompressobj().decompress(data[m.end():m.end() + (8 << 20)], 32 << 20)
        except zlib.error:
            continue
        if not d:
            continue
        if b"/Author" in d or b"/Producer" in d:
            for mm in PDF_INFO.finditer(d):
                _obs(out, INFO_KIND[mm.group(1).decode()], "pdf.objstm." + mm.group(1).decode(), _pdf_str(mm.group(2)))
        if b"<x:xmpmeta" in d or b"<rdf:RDF" in d:
            scan_xmp(d, out, "pdf.xmp")
        scan_generic(d, out, "pdf.stream")


def scan_xmp(data: bytes, out, field):
    if b"xmpmeta" not in data and b"rdf:RDF" not in data:
        return
    for m in XMP_TAG.finditer(data):
        tag = m.group(1).decode()
        inner = re.sub(rb"<[^>]+>", b"\n", m.group(2))
        for v in inner.split(b"\n"):
            v = v.strip()
            if v:
                kind = {"dc:creator": "person", "pdf:Author": "person", "dc:rights": "free_text",
                        "dc:publisher": "company", "xmpRights:Owner": "person", "cc:attributionName": "person",
                        "dc:contributor": "person"}.get(tag, "software" if "Tool" in tag or "Producer" in tag
                                                         else "free_text")
                _obs(out, kind, f"{field}.{tag}", _xml_unescape(v))


def _xml_unescape(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
            .replace("&#39;", "'").replace("&amp;", "&"))


# ------------------------------------------------------------------ OLE / CFB
def _cfb_streams(data: bytes, wanted=("\x05SummaryInformation", "\x05DocumentSummaryInformation")):
    """Minimal CFB reader: returns {name: bytes} for wanted stream names."""
    if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return {}
    ss = 1 << struct.unpack_from("<H", data, 30)[0]
    mss = 1 << struct.unpack_from("<H", data, 32)[0]
    nfat, dir_start, _, cutoff, minifat_start, nminifat, difat_start, ndifat = struct.unpack_from("<IIIIIIII", data, 44)
    nsec = (len(data) - ss) // ss

    def sec(i):
        o = ss + i * ss
        return data[o:o + ss]
    difat = list(struct.unpack_from("<109I", data, 76))
    d = difat_start
    guard = 0
    while d not in (0xFFFFFFFE, 0xFFFFFFFF) and d < nsec and guard < 10000:
        s = sec(d)
        vals = struct.unpack_from(f"<{ss // 4}I", s)
        difat.extend(vals[:-1])
        d = vals[-1]
        guard += 1
    fat = []
    for fs in difat[:nfat]:
        if fs >= nsec:
            break
        fat.extend(struct.unpack_from(f"<{ss // 4}I", sec(fs)))

    def chain(start, limit=1 << 26):
        out = bytearray()
        i = start
        seen = 0
        while i < len(fat) and i not in (0xFFFFFFFE, 0xFFFFFFFF) and seen < 200000 and len(out) < limit:
            out += sec(i)
            i = fat[i]
            seen += 1
        return bytes(out)
    dirs = chain(dir_start, 4 << 20)
    entries = []
    for o in range(0, len(dirs) - 127, 128):
        nl = struct.unpack_from("<H", dirs, o + 64)[0]
        name = dirs[o:o + max(0, nl - 2)].decode("utf-16-le", "replace")
        typ = dirs[o + 66]
        start, size = struct.unpack_from("<IQ", dirs, o + 116)
        entries.append((name, typ, start, size))
    if not entries:
        return {}
    root = entries[0]
    ministream = chain(root[2]) if root[1] == 5 else b""
    minifat = []
    if nminifat:
        mf = chain(minifat_start)
        minifat = list(struct.unpack_from(f"<{len(mf) // 4}I", mf))
    res = {}
    for name, typ, start, size in entries:
        if typ != 2 or name not in wanted:
            continue
        size = size & 0xFFFFFFFF
        if size < cutoff:
            out = bytearray()
            i = start
            g = 0
            while i < len(minifat) and i not in (0xFFFFFFFE, 0xFFFFFFFF) and g < 100000 and len(out) < size:
                out += ministream[i * mss:(i + 1) * mss]
                i = minifat[i]
                g += 1
            res[name] = bytes(out[:size])
        else:
            res[name] = chain(start)[:size]
    return res


SUMMARY_PIDS = {2: ("free_text", "Title"), 3: ("free_text", "Subject"), 4: ("person", "Author"),
                5: ("free_text", "Keywords"), 6: ("free_text", "Comments"), 8: ("person", "LastAuthor"),
                18: ("software", "AppName")}
DOCSUM_PIDS = {14: ("person", "Manager"), 15: ("company", "Company"), 2: ("free_text", "Category")}


def _propset(b: bytes, pids):
    out = []
    try:
        nsets = struct.unpack_from("<I", b, 24)[0]
        for k in range(min(nsets, 2)):
            off = struct.unpack_from("<I", b, 28 + k * 20 + 16)[0]
            size, nprops = struct.unpack_from("<II", b, off)
            codepage = 1252
            props = []
            for j in range(min(nprops, 400)):
                pid, poff = struct.unpack_from("<II", b, off + 8 + j * 8)
                props.append((pid, poff))
            for pid, poff in props:
                if pid == 1:
                    codepage = struct.unpack_from("<h", b, off + poff + 4)[0] & 0xFFFF
            for pid, poff in props:
                typ = struct.unpack_from("<I", b, off + poff)[0] & 0xFFFF
                if typ == 30:  # VT_LPSTR
                    ln = struct.unpack_from("<I", b, off + poff + 4)[0]
                    raw = b[off + poff + 8: off + poff + 8 + min(ln, 4096)].split(b"\x00")[0]
                    enc = "utf-8" if codepage == 65001 else ("utf-16-le" if codepage == 1200 else "cp1252")
                    v = raw.decode(enc, "replace")
                elif typ == 31:  # VT_LPWSTR
                    ln = struct.unpack_from("<I", b, off + poff + 4)[0]
                    v = b[off + poff + 8: off + poff + 8 + 2 * min(ln, 4096)].decode("utf-16-le", "replace").split("\x00")[0]
                else:
                    continue
                if k == 0 and pid in pids:
                    out.append((pids[pid][0], pids[pid][1], v))
                elif k == 1:  # user-defined properties section
                    out.append(("free_text", f"user.{pid}", v))
    except struct.error:
        pass
    return out


def scan_cfb(data: bytes, out):
    try:
        st = _cfb_streams(data)
    except Exception:
        return
    for name, b in st.items():
        pids = SUMMARY_PIDS if "Document" not in name else DOCSUM_PIDS
        for kind, fld, v in _propset(b, pids):
            _obs(out, kind, "cfb." + fld, v)


# ------------------------------------------------------------------ misc formats
def scan_iges(data: bytes, out):
    lines = data[:400000].split(b"\n")
    g = b"".join(l[:72] for l in lines if len(l) >= 73 and l[72:73] == b"G")
    if not g:
        return
    pd, i = b",", 0
    if g[:2] == b"1H":
        pd = g[2:3]
        i = 4
    params = []
    while i < len(g) and len(params) < 26:
        m = re.match(rb"\s*(\d+)H", g[i:])
        if m:
            n = int(m.group(1))
            s = g[i + m.end(): i + m.end() + n]
            params.append(s)
            i = i + m.end() + n
        else:
            j = g.find(pd, i)
            j2 = g.find(b";", i)
            if j < 0 or (0 <= j2 < j):
                params.append(g[i:j2 if j2 >= 0 else len(g)].strip())
                break
            params.append(g[i:j].strip())
            i = j
        if i < len(g) and g[i:i + 1] in (pd, b";"):
            i += 1
    # 0-based: 2 sender product id, 3 file name, 4 native system, 5 preprocessor, 11 receiver id,
    # 20 author, 21 organization
    names = {2: ("part_name", "sender_product"), 3: ("path", "file_name"), 4: ("software", "system"),
             5: ("software", "preprocessor"), 11: ("part_name", "receiver_product"),
             20: ("person", "author"), 21: ("company", "organization")}
    for idx, (kind, fld) in names.items():
        if idx < len(params) and params[idx]:
            _obs(out, kind, "iges.G." + fld, params[idx].decode("latin-1"))
    for l in lines[:200]:
        if len(l) >= 73 and l[72:73] == b"S":
            scan_text_line(l[:72].decode("latin-1"), out, "iges.S")


def scan_parasolid(data: bytes, out):
    head = data[:8192]
    for m in PARASOLID_HDR.finditer(head):
        k = m.group(1).decode()
        kind = {"USER": "account", "SITE": "company"}.get(k, "free_text")
        _obs(out, kind, "parasolid." + k, m.group(2))


def scan_ps(data: bytes, out):
    for m in PS_DSC.finditer(data[:65536]):
        k = m.group(1).decode()
        kind = {"For": "person", "Author": "person", "Company": "company", "Creator": "software"}.get(k, "free_text")
        v = m.group(2).strip()
        if v.startswith(b"(") and v.endswith(b")"):
            v = v[1:-1]
        _obs(out, kind, "ps.DSC." + k, v)


def scan_xmlish(data: bytes, out, field):
    for m in XML_META.finditer(data[:4 << 20]):
        tag = m.group(1).decode(errors="replace")
        kind = "person" if ("creator" in tag or "Author" in tag or "Designer" in tag or "lastModifiedBy" in tag
                            or "Manager" in tag) else ("company" if "Company" in tag else "free_text")
        _obs(out, kind, f"{field}.{tag[:40]}", _xml_unescape(re.sub(rb"<[^>]+>", b" ", m.group(2))))
    for m in FC_PROP.finditer(data[:16 << 20]):
        k = m.group(1).decode()
        kind = {"CreatedBy": "person", "LastModifiedBy": "person", "Company": "company"}.get(k, "free_text")
        _obs(out, kind, f"{field}.fcprop.{k}", _xml_unescape(m.group(2)))
    scan_xmp(data, out, field + ".xmp")


def scan_text_line(s: str, out, field):
    for m in TITLE_LABEL.finditer(s):
        _obs(out, "person", field + ".label." + m.group(1).lower(), m.group(2))


def scan_generic(data: bytes, out, field):
    """Emails, URL hosts and user directories in ASCII and UTF-16LE strings."""
    for chunk, enc in ((data, "a"),):
        for m in EMAIL.finditer(chunk):
            _obs(out, "email", field + ".email", m.group(0))
        for m in URL.finditer(chunk):
            _obs(out, "domain", field + ".url", m.group(1).rstrip(b".").lower())
        for m in WWW.finditer(chunk):
            _obs(out, "domain", field + ".www", b"www." + m.group(1).rstrip(b".").lower())
        for m in USERDIR.finditer(chunk):
            _obs(out, "account", field + ".userdir", m.group(1))
    if b"\x00" in data[:1 << 20] or len(data) < (1 << 20):
        for m in U16_RUN.finditer(data):
            s = m.group(0).decode("utf-16-le", "replace").encode("latin-1", "replace")
            for mm in EMAIL.finditer(s):
                _obs(out, "email", field + ".u16.email", mm.group(0))
            for mm in URL.finditer(s):
                _obs(out, "domain", field + ".u16.url", mm.group(1).rstrip(b".").lower())
            for mm in USERDIR.finditer(s):
                _obs(out, "account", field + ".u16.userdir", mm.group(1))


def scan_bytes(data: bytes, name: str, out, depth=0):
    from deid.api import sniff
    magic = sniff(data[:8192], name)
    low = name.lower()
    if magic == "step":
        scan_step(data, out)
    elif magic == "pdf":
        scan_pdf(data, out)
    elif magic == "cfb":
        scan_cfb(data, out)
    elif magic in ("ps", "eps", "eps_binary"):
        scan_ps(data, out)
    elif magic == "parasolid_text" or low.endswith((".x_t", ".xmt_txt")):
        scan_parasolid(data, out)
    elif low.endswith((".igs", ".iges")):
        scan_iges(data, out)
    elif magic in ("svg", "xml") or low.endswith((".svg", ".xml", ".ipj")):
        scan_xmlish(data, out, "xml")
    if magic == "zip" and depth < 3:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            for zi in zf.infolist()[:5000]:
                _obs(out, "path", "zip.member", zi.filename)
                if zi.comment:
                    _obs(out, "free_text", "zip.member_comment", zi.comment)
                if zi.file_size > MAX_MEMBER or zi.is_dir():
                    continue
                try:
                    b = zf.read(zi)
                except Exception:
                    continue
                n0 = len(out)
                scan_bytes(b, zi.filename, out, depth + 1)
                for i in range(n0, len(out)):
                    k, f, v, ev = out[i]
                    out[i] = (k, "zip>" + f, v, ev)
            if zf.comment:
                _obs(out, "free_text", "zip.comment", zf.comment)
        except Exception:
            pass
        return
    if magic == "gzip" and depth < 3:
        try:
            import gzip
            b = gzip.decompress(data[:MAX_MEMBER])
            scan_bytes(b, low[:-3] if low.endswith(".gz") else low, out, depth + 1)
        except Exception:
            pass
        return
    if magic != "pdf":
        scan_generic(data, out, magic)


def scan_path(fs_path: str, logical: str, max_bytes: int):
    out = []
    try:
        size = os.path.getsize(fs_path)
        with open(fs_path, "rb") as f:
            if size <= max_bytes:
                data = f.read()
            else:
                head = f.read(max_bytes // 2)
                f.seek(size - max_bytes // 4)
                data = head + b"\n" + f.read()
        scan_bytes(data, logical, out)
    except Exception as e:  # never die on one file
        out.append(("error", "scan", type(e).__name__, ""))
    return logical, out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=12)
    ap.add_argument("--prefix", default="")
    ap.add_argument("--max-bytes", type=int, default=64 << 20)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    done = set()
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["path"])
                except Exception:
                    pass
    todo = []
    with open(args.manifest, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            p, size = line.split("\t")[:2]
            if not p.startswith(args.prefix) or p in done:
                continue
            fp = os.path.join(args.root, p)
            if os.path.exists(fp) and os.path.getsize(fp) == int(size):
                todo.append((fp, p))
    if args.limit:
        todo = todo[: args.limit]
    print(f"scanning {len(todo)} files", file=sys.stderr)
    n = 0
    with open(args.out, "a", encoding="utf-8") as fo, ProcessPoolExecutor(args.jobs) as ex:
        futs = [ex.submit(scan_path, fp, p, args.max_bytes) for fp, p in todo]
        for fu in as_completed(futs):
            p, obs = fu.result()
            fo.write(json.dumps({"path": p, "obs": obs}, ensure_ascii=False) + "\n")
            n += 1
            if n % 2000 == 0:
                print(n, file=sys.stderr, flush=True)
    print("done", n, file=sys.stderr)


if __name__ == "__main__":
    main()
