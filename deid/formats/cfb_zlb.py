"""Same-size edits for the single-record SolidWorks ZLB XML wrapper."""
import re
import struct
import xml.etree.ElementTree as ET
import zlib

from deid.formats._nativeutil import rewrite_xml_values
from deid.formats.cfb_accounts import is_save_label

MAGIC = bytes.fromhex("231dd571da8148a2a85898b21b89ef99")
MAX_XML_SIZE = 4 << 20
_XML_START = re.compile(r"^\ufeff?\s*<[\w?]")


def is_xml_stream(path):
    return (len(path) >= 2 and path[-2].casefold() == "swxmlcontents"
            and path[-1].casefold().endswith("__zlb"))


def payload(data):
    """Return the complete first record only for the supported single-record layout."""
    if len(data) < 32 or not data.startswith(MAGIC) or data[-8:] != bytes(8):
        return None
    size, compressed = struct.unpack_from("<II", data, 16)
    if not (0 < size <= MAX_XML_SIZE and compressed == len(data) - 32):
        return None
    try:
        dec = zlib.decompressobj()
        result = dec.decompress(data[24:-8], size + 1)
    except zlib.error:
        return None
    if not dec.eof or dec.unused_data or len(result) != size:
        return None
    return result


def _deflate_exact(data, size):
    """Add valid empty DEFLATE blocks when a smaller stream can fill the span."""
    for level in (6, 9, 1):
        result = zlib.compress(data, level)
        if len(result) == size:
            return result
        if len(result) >= size:
            continue
        enc = zlib.compressobj(level, wbits=-zlib.MAX_WBITS)
        # SYNC_FLUSH leaves the next block on a byte boundary.
        body = enc.compress(data) + enc.flush(zlib.Z_SYNC_FLUSH)
        gap = size - 6 - len(body)  # zlib header and Adler-32 trailer
        if gap < 2:
            continue
        # Tail lengths 2, 3 and 4 use one to three empty fixed-Huffman
        # blocks. Length 6 uses a fixed block followed by a stored block.
        tail = {
            0: b"\x01\x00\x00\xff\xff",
            1: b"\x02\x04\x00\x00\xff\xff",
            2: b"\x03\x00",
            3: b"\x02\x0c\x00",
            4: b"\x02\x08\x30\x00",
        }[gap % 5]
        if gap < len(tail):
            continue
        empty = b"\x00\x00\x00\xff\xff" * ((gap - len(tail)) // 5)
        return b"\x78\x9c" + body + empty + tail + struct.pack(">I", zlib.adler32(data))
    return None


def _xml_view(raw):
    for offset in (0, 1):
        for encoding in ("utf-8", "utf-16-le"):
            try:
                xml = raw[offset:].decode(encoding).rstrip("\x00")
            except UnicodeDecodeError:
                continue
            if not _XML_START.match(xml) or "<!DOCTYPE" in xml:
                continue
            try:
                ET.fromstring(xml)
            except (ET.ParseError, ValueError):
                continue
            return offset, encoding, xml
    return None


def rewrite(data, accounts, cad_reference):
    raw = payload(data)
    view = _xml_view(raw) if raw is not None else None
    if view is None:
        return None
    offset, enc, xml = view

    def edit(value, where):
        if cad_reference.search(value):
            return accounts.profiles(value)
        return accounts.text(value, exact=is_save_label(where))

    changed, count = rewrite_xml_values(xml, edit)
    if not count:
        return None
    new_text = changed.encode(enc)
    old_text = xml.encode(enc)
    if len(new_text) != len(old_text):
        return None
    new_raw = raw[:offset] + new_text + raw[offset + len(old_text):]
    result = _deflate_exact(new_raw, len(data) - 32)
    if result is None:
        return None
    updated = data[:24] + result + data[-8:]
    if payload(updated) == new_raw and _xml_view(new_raw) is not None:
        return updated
    return None


# ---------------------------------------------------------------------------
# OLE round 2: saving accounts inside binary single-record ZLB streams (display lists, visual states)
# ---------------------------------------------------------------------------
MAX_BIN_SIZE = 64 << 20
# accounts (>= 5 chars, not dictionary words) are also replaced inside longer tokens ("ENGR<account>"),
# as a substring search would still find them there
import os as _os
SUBSTR = _os.environ.get("DEID_CFB_ACCOUNT_SUBSTR", "1") == "1"


def payload_any(data, max_size=MAX_BIN_SIZE):
    """payload() for binary records up to max_size (same single-record layout checks)."""
    if len(data) < 32 or not data.startswith(MAGIC) or data[-8:] != bytes(8):
        return None
    size, compressed = struct.unpack_from("<II", data, 16)
    if not (0 < size <= max_size and compressed == len(data) - 32):
        return None
    try:
        dec = zlib.decompressobj()
        result = dec.decompress(data[24:-8], size + 1)
    except zlib.error:
        return None
    if not dec.eof or dec.unused_data or len(result) != size:
        return None
    return result


def _case_like(src: str, alias: str) -> str:
    if src.isupper():
        return alias.upper()
    if src.islower():
        return alias.lower()
    if src[:1].isupper() and src[1:].islower():
        return alias[:1].upper() + alias[1:].lower()
    return alias


def rewrite_binary(data, pairs):
    """Replace whole-word occurrences of the file's saving accounts (UTF-16LE and ANSI) with their
    same-length aliases inside the inflated record, re-deflate to exactly the original compressed size.
    Returns (new_data or None, reason)."""
    raw = payload_any(data)
    if raw is None:
        return None, "layout"
    buf = bytearray(raw)
    spans = []
    for orig, alias in pairs:
        if len(orig) != len(alias):
            continue
        for enc in ("utf-16-le", "cp1252"):
            try:
                o = orig.encode(enc)
                alias.encode(enc)
            except UnicodeEncodeError:
                continue
            if enc == "utf-16-le":
                pat = re.compile((rb"(?<![A-Za-z0-9]\x00)" if not SUBSTR else b"") + re.escape(o) +
                                 (rb"(?![A-Za-z0-9]\x00)" if not SUBSTR else b""), re.I)
            else:
                pat = re.compile((rb"(?<![A-Za-z0-9])" if not SUBSTR else b"") + re.escape(o) +
                                 (rb"(?![A-Za-z0-9])" if not SUBSTR else b""), re.I)
            for m in pat.finditer(raw):
                src = raw[m.start():m.end()].decode(enc, "replace")
                rep = _case_like(src, alias).encode(enc)
                if len(rep) != m.end() - m.start():
                    continue
                buf[m.start():m.end()] = rep
                spans.append((m.start(), m.end()))
    if not spans:
        return None, "none"
    new_raw = bytes(buf)
    result = _deflate_exact(new_raw, len(data) - 32)
    if result is None:
        return None, "nofit"
    updated = data[:24] + result + data[-8:]
    if payload_any(updated) != new_raw:
        return None, "verify"
    # only the replaced byte ranges differ
    diff = [i for i in range(len(raw)) if raw[i] != new_raw[i]] if len(raw) < 4_000_000 else None
    if diff is not None and any(not any(a <= i < b for a, b in spans) for i in diff):
        return None, "verify"
    return updated, "ok"


# ---------------------------------------------------------------------------
# OLE round 3: multi-record ZLB layout  MAGIC | (size u32, comp u32, zlib[comp])+ | 8 zero bytes
# ---------------------------------------------------------------------------
def records(data, max_size=MAX_BIN_SIZE):
    """[(offset_of_zlib, size, comp, payload)] or None when the layout is not exactly this."""
    if len(data) < 40 or not data.startswith(MAGIC) or data[-8:] != bytes(8):
        return None
    off, out = 16, []
    while off < len(data) - 8:
        if off + 8 > len(data) - 8:
            return None
        size, comp = struct.unpack_from("<II", data, off)
        if not (0 < size <= max_size) or comp == 0 or off + 8 + comp > len(data) - 8:
            return None
        try:
            dec = zlib.decompressobj()
            raw = dec.decompress(data[off + 8:off + 8 + comp], size + 1)
        except zlib.error:
            return None
        if not dec.eof or dec.unused_data or len(raw) != size:
            return None
        out.append((off + 8, size, comp, raw))
        off += 8 + comp
    return out if off == len(data) - 8 else None


def _replace_accounts(raw, pairs):
    buf = bytearray(raw)
    spans = []
    for orig, alias in pairs:
        if len(orig) != len(alias):
            continue
        for enc in ("utf-16-le", "cp1252"):
            try:
                o = orig.encode(enc)
                alias.encode(enc)
            except UnicodeEncodeError:
                continue
            if enc == "utf-16-le":
                pat = re.compile((rb"(?<![A-Za-z0-9]\x00)" if not SUBSTR else b"") + re.escape(o) +
                                 (rb"(?![A-Za-z0-9]\x00)" if not SUBSTR else b""), re.I)
            else:
                pat = re.compile((rb"(?<![A-Za-z0-9])" if not SUBSTR else b"") + re.escape(o) +
                                 (rb"(?![A-Za-z0-9])" if not SUBSTR else b""), re.I)
            for m in pat.finditer(raw):
                src = raw[m.start():m.end()].decode(enc, "replace")
                rep = _case_like(src, alias).encode(enc)
                if len(rep) == m.end() - m.start():
                    buf[m.start():m.end()] = rep
                    spans.append((m.start(), m.end()))
    return bytes(buf), spans


def rewrite_multi(data, pairs):
    """Round 3: every record of a multi-record ZLB stream; each re-deflated to its exact original size."""
    recs = records(data)
    if recs is None or len(recs) < 2:
        return None, "layout"
    out = bytearray(data)
    changed = 0
    for zoff, size, comp, raw in recs:
        new_raw, spans = _replace_accounts(raw, pairs)
        if not spans:
            continue
        z = _deflate_exact(new_raw, comp)
        if z is None:
            return None, "nofit"
        out[zoff:zoff + comp] = z
        changed += 1
    if not changed:
        return None, "none"
    new = bytes(out)
    chk = records(new)
    if chk is None or [(s, c) for _, s, c, _ in chk] != [(s, c) for _, s, c, _ in recs]:
        return None, "verify"
    return new, "ok"


def rewrite_plain(data, pairs):
    """Round 3: uncompressed streams (GeometricVersionHistory, Parasolid text): same-length account edits."""
    new, spans = _replace_accounts(data, pairs)
    return (new, "ok") if spans else (None, "none")
