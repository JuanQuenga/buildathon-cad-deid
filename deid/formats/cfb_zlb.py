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
