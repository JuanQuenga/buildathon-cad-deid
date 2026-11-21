"""ZIP-based engineering packages: FreeCAD .fcstd, 3MF .3mf, OpenDocument
Drawing .odg (and other ODF), Fusion 360 .f3d / .f3z.

The container is rewritten member by member in original order: unchanged
members are copied as their exact original bytes (local header + compressed
data + data descriptor), so unknown compression methods (Fusion uses zstd),
the ODF ``mimetype``-first/stored rule and member order all survive.  Changed
members are re-encoded with their original method when it is stored or
deflate (otherwise left unchanged).  Member names are never renamed: package
files reference each other by member name.

Per member:
* XML (Document.xml, GuiDocument.xml, *.model, *.rels, meta.xml, content.xml,
  styles.xml, settings.xml, *.config XML): text nodes and attribute values go
  through the plan; FreeCAD internal object ``name`` attributes are kept;
  references to other files (FreeCAD links, PropertyFileIncluded excluded)
  use ``plan.map_reference``.
* JSON / INI / plain text members: ``plan.replace_text`` on the whole text.
* Fusion ``*.dat`` members: length-prefixed strings only, same length.
* Images (thumbnails, pictures), nested packages (.f3d inside .f3z) and
  other embedded files: through the registry via ``ctx.dispatch_rewrite``.

verify(): zip test (CRC of every member), member list/order/methods equal,
XML members still parse, unchanged members byte-identical.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import tempfile
import zipfile
import zlib
from typing import Optional

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult, Reference,
                      REWRITTEN, VERIFIED_UNCHANGED, COPIED_UNSUPPORTED, SOURCE_EXCEPTION)
from deid.formats import _nativeutil as nu

PKG_EXTS = (".fcstd", ".fcstd1", ".3mf", ".odg", ".otg", ".odt", ".ods", ".odp", ".f3d", ".f3z", ".fcmacro")
IMG_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif")
TEXT_EXTS = (".json", ".txt", ".ini", ".cfg", ".config", ".csv", ".gcode", ".py", ".md", ".svg", ".xml",
             ".model", ".rels", ".mtl")
MAX_MEMBER = 512 * 1024 * 1024


class _Entry:
    __slots__ = ("info", "loc_off", "loc_end", "raw")


def _local_span(f, info) -> tuple:
    """Return (start, end) of the full local record (header+data+descriptor)."""
    f.seek(info.header_offset)
    h = f.read(30)
    if len(h) < 30 or h[:4] != b"PK\x03\x04":
        raise ValueError("bad local header")
    nlen, xlen = struct.unpack_from("<HH", h, 26)
    data_start = info.header_offset + 30 + nlen + xlen
    end = data_start + info.compress_size
    if info.flag_bits & 0x08:
        f.seek(end)
        dd = f.read(24)
        if dd[:4] == b"PK\x07\x08":
            end += 16 if not (info.compress_size >= 0xFFFFFFFF or info.file_size >= 0xFFFFFFFF) else 24
        else:
            end += 12
    return info.header_offset, data_start, end


def _dos(info):
    y, mo, d, h, mi, s = info.date_time
    return ((max(y, 1980) - 1980) << 9 | mo << 5 | d), (h << 11 | mi << 5 | s // 2)


def rewrite_zip(src: str, dst: str, new_data: dict) -> None:
    """Copy ``src`` to ``dst`` replacing the uncompressed contents of members
    listed in ``new_data`` (name -> bytes).  Other members are byte-copied."""
    with zipfile.ZipFile(src) as z, open(src, "rb") as f, open(dst, "wb") as out:
        infos = z.infolist()
        order = sorted(range(len(infos)), key=lambda i: infos[i].header_offset)
        new_offsets = {}
        new_cd_fields = {}
        for i in order:
            info = infos[i]
            start, data_start, end = _local_span(f, info)
            new_offsets[i] = out.tell()
            if info.filename in new_data:
                payload = new_data[info.filename]
                f.seek(start)
                h = bytearray(f.read(data_start - start))
                method = info.compress_type
                if method == zipfile.ZIP_STORED:
                    comp = payload
                elif method == zipfile.ZIP_DEFLATED:
                    co = zlib.compressobj(6, zlib.DEFLATED, -15)
                    comp = co.compress(payload) + co.flush()
                else:
                    raise ValueError("cannot re-encode member method")
                crc = zlib.crc32(payload) & 0xFFFFFFFF
                if len(comp) >= 0xFFFFFFFF or len(payload) >= 0xFFFFFFFF:
                    raise ValueError("zip64 member")
                flags = struct.unpack_from("<H", h, 6)[0] & ~0x08
                struct.pack_into("<H", h, 6, flags)
                o_csz, o_usz = struct.unpack_from("<II", h, 18)
                struct.pack_into("<I", h, 14, crc)
                struct.pack_into("<I", h, 18, 0xFFFFFFFF if o_csz == 0xFFFFFFFF else len(comp))
                struct.pack_into("<I", h, 22, 0xFFFFFFFF if o_usz == 0xFFFFFFFF else len(payload))
                nlen_, xlen = struct.unpack_from("<HH", h, 26)
                xp, xe = 30 + nlen_, 30 + nlen_ + xlen
                while xp + 4 <= xe:
                    hid, hsz = struct.unpack_from("<HH", h, xp)
                    if hid == 0x0001 and hsz >= 16:
                        struct.pack_into("<QQ", h, xp + 4, len(payload), len(comp))
                    xp += 4 + hsz
                out.write(h)
                out.write(comp)
                new_cd_fields[i] = (flags, crc, len(comp), len(payload))
            else:
                f.seek(start)
                remaining = end - start
                while remaining:
                    b = f.read(min(remaining, 1 << 20))
                    if not b:
                        raise ValueError("truncated member")
                    out.write(b)
                    remaining -= len(b)
        # central directory: copy original records, patch fields
        f.seek(0, 2)
        size = f.tell()
        tail = min(size, 65536 + 22)
        f.seek(size - tail)
        buf = f.read(tail)
        e = buf.rfind(b"PK\x05\x06")
        if e < 0:
            raise ValueError("no end of central directory")
        eocd = bytearray(buf[e:])
        cd_size, cd_off = struct.unpack_from("<II", eocd, 12)
        z64 = None
        loc = buf.rfind(b"PK\x06\x07", 0, e)
        if loc >= 0 and e - loc == 20:
            z64_off = struct.unpack_from("<Q", buf, loc + 8)[0]
            f.seek(z64_off)
            z64 = bytearray(f.read(56))
            if z64[:4] != b"PK\x06\x06":
                raise ValueError("bad zip64 end record")
            zsz = struct.unpack_from("<Q", z64, 4)[0]
            f.seek(z64_off)
            z64 = bytearray(f.read(12 + zsz))
            cd_size, cd_off = struct.unpack_from("<QQ", z64, 40)
            z64loc = bytearray(buf[loc:loc + 20])
        elif cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
            raise ValueError("zip64 archive without locator")
        f.seek(cd_off)
        cd = f.read(cd_size)
        cd_start = out.tell()
        p = 0
        idx = 0
        while p < len(cd) and cd[p:p + 4] == b"PK\x01\x02":
            nlen, xlen, clen = struct.unpack_from("<HHH", cd, p + 28)
            rec = bytearray(cd[p:p + 46 + nlen + xlen + clen])
            o_csz, o_usz = struct.unpack_from("<II", rec, 20)
            lo = struct.unpack_from("<I", rec, 42)[0]
            if idx in new_cd_fields:
                flags, crc, csz, usz = new_cd_fields[idx]
                struct.pack_into("<H", rec, 8, flags)
                struct.pack_into("<I", rec, 16, crc)
                if o_csz != 0xFFFFFFFF:
                    struct.pack_into("<I", rec, 20, csz)
                if o_usz != 0xFFFFFFFF:
                    struct.pack_into("<I", rec, 24, usz)
            else:
                csz = usz = None
            # zip64 extra: fields present only for 32-bit values set to 0xFFFFFFFF
            xp, xe = 46 + nlen, 46 + nlen + xlen
            while xp + 4 <= xe:
                hid, hsz = struct.unpack_from("<HH", rec, xp)
                if hid == 0x0001:
                    q = xp + 4
                    if o_usz == 0xFFFFFFFF:
                        if usz is not None:
                            struct.pack_into("<Q", rec, q, usz)
                        q += 8
                    if o_csz == 0xFFFFFFFF:
                        if csz is not None:
                            struct.pack_into("<Q", rec, q, csz)
                        q += 8
                    if lo == 0xFFFFFFFF:
                        struct.pack_into("<Q", rec, q, new_offsets[idx])
                xp += 4 + hsz
            if lo != 0xFFFFFFFF:
                struct.pack_into("<I", rec, 42, new_offsets[idx])
            out.write(rec)
            p += len(rec)
            idx += 1
        if idx != len(infos):
            raise ValueError("central directory mismatch")
        cd_new_size = out.tell() - cd_start
        if z64 is not None:
            struct.pack_into("<QQ", z64, 40, cd_new_size, cd_start)
            z64_new = out.tell()
            out.write(z64)
            struct.pack_into("<Q", z64loc, 8, z64_new)
            out.write(z64loc)
            o_size, o_off = struct.unpack_from("<II", eocd, 12)
            if o_size != 0xFFFFFFFF:
                struct.pack_into("<I", eocd, 12, min(cd_new_size, 0xFFFFFFFF))
            if o_off != 0xFFFFFFFF:
                struct.pack_into("<I", eocd, 16, min(cd_start, 0xFFFFFFFF))
        else:
            struct.pack_into("<II", eocd, 12, cd_new_size, cd_start)
        out.write(eocd)


# ---------------------------------------------------------------------------
# member editors
# ---------------------------------------------------------------------------
def _is_xml(name: str, data: bytes) -> bool:
    h = data[:200].lstrip(b"\xef\xbb\xbf \r\n\t")
    return h.startswith(b"<?xml") or (h.startswith(b"<") and name.lower().endswith(
        (".xml", ".model", ".rels", ".config", ".svg")))


def _xml_parses(b: bytes) -> bool:
    try:
        from lxml import etree
        etree.fromstring(b, etree.XMLParser(resolve_entities=False, huge_tree=True))
        return True
    except Exception:
        return False


def _decode_text(data: bytes):
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data[3:].decode("utf-8"), "utf-8-sig"
        except UnicodeDecodeError:
            return None, None
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            return None, None
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    if b"\x00" in data[:4096]:
        return None, None
    return data.decode("cp1252", "replace"), "cp1252"


def _encode_text(t: str, enc: str) -> bytes:
    if enc == "utf-8-sig":
        return b"\xef\xbb\xbf" + t.encode("utf-8")
    return t.encode(enc, "replace")


_FC_FILE_ATTR = re.compile(r'(<(?:XLink|Link|LinkSub)\b[^>]*\bfile=")([^"]*)(")')


def _iter_lp_strings(buf: bytes, min_len: int = 4, max_len: int = 4096):
    """uint32-LE length-prefixed printable UTF-8 strings (Fusion .dat)."""
    n = len(buf)
    i = 0
    while i + 4 < n:
        ln = buf[i] | buf[i + 1] << 8 | buf[i + 2] << 16 | buf[i + 3] << 24
        if min_len <= ln <= max_len and i + 4 + ln <= n:
            s = buf[i + 4:i + 4 + ln]
            if all(32 <= c < 127 or c >= 0x80 for c in s):
                try:
                    t = s.decode("utf-8")
                    yield i + 4, ln, t
                    i += 4 + ln
                    continue
                except UnicodeDecodeError:
                    pass
        i += 1


_HEAVY = re.compile(r"<((?:\w+:)?(?:mesh|vertices|triangles|beamlattice|slices?|slicestack))\b[^>]*>.*?</\1>", re.S)
HEAVY_MIN = 1 << 20


def _split_heavy(t: str):
    """Split large XML into (heavy, text) segments; heavy = mesh/slice payload
    blocks that hold only numbers and are never edited."""
    if len(t) < HEAVY_MIN:
        return [(False, t)]
    out = []
    pos = 0
    for m in _HEAVY.finditer(t):
        if m.start() > pos:
            out.append((False, t[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(t):
        out.append((False, t[pos:]))
    return out


class ZipPackageAdapter(Adapter):
    name = "zippkg"
    version = "1"
    priority = 15
    magics = ("zip",)
    extensions = PKG_EXTS
    path_independent = False
    timeout_s = 900.0

    def matches(self, ctx, head):
        return ctx.ext in PKG_EXTS and head[:4] == b"PK\x03\x04"

    def _kind(self, ctx):
        e = ctx.ext
        if e.startswith(".fcstd"):
            return "fcstd"
        if e == ".3mf":
            return "3mf"
        if e in (".f3d", ".f3z"):
            return "fusion"
        return "odf"

    # -- scan ---------------------------------------------------------------
    def scan(self, ctx, src_path):
        try:
            z = zipfile.ZipFile(src_path)
        except Exception as e:
            return ScanResult(status="source_exception", notes=[f"not a readable zip: {type(e).__name__}"])
        obs, refs, notes, children = [], [], [], []
        kind = self._kind(ctx)
        with z:
            infos = z.infolist()
            fp = {"members": len(infos), "names": [i.filename for i in infos][:2000],
                  "methods": sorted({i.compress_type for i in infos})}
            for info in infos:
                nm = info.filename
                low = nm.lower()
                if nm.endswith("/") or info.file_size > MAX_MEMBER:
                    continue
                try:
                    data = z.read(info)
                except Exception:
                    notes.append("member not decodable")
                    continue
                if low.endswith(IMG_EXTS) or low.endswith((".f3d", ".fcstd", ".3mf", ".pdf", ".step", ".stp",
                                                           ".stl", ".dxf", ".svg")) and kind != "fcstd":
                    if ctx.dispatch_scan is not None and not low.endswith(".svg"):
                        children.append(self._child_scan(ctx, nm, data))
                    if not low.endswith(".svg"):
                        continue
                if low.endswith(".dat") and kind == "fusion":
                    for off, ln, t in _iter_lp_strings(data):
                        if not re.fullmatch(r"[0-9A-Fa-f\-{}$%#]+", t):
                            nu.add_obs(obs, "free_text", f"zippkg.fusion.dat", t[:500], nm.rsplit("/", 1)[-1], 0.2)
                            nu.contact_observations(t, "zippkg.fusion.dat", obs)
                    continue
                if _is_xml(nm, data):
                    self._scan_xml(kind, nm, data, obs, refs)
                    continue
                if low.endswith(TEXT_EXTS) or kind == "3mf" and low.startswith("metadata/"):
                    t, enc = _decode_text(data)
                    if t:
                        nu.contact_observations(t, f"zippkg.{kind}.text", obs)
                        for m in re.finditer(r'"(?:author|creator|designer|user(?:name)?|owner|company|'
                                             r'email|name|title|description)"\s*:\s*"([^"]{2,200})"', t, re.I):
                            nu.add_obs(obs, "free_text", f"zippkg.{kind}.json", m.group(1), nm, 0.4)
        res = ScanResult(observations=nu.dedupe_obs(obs), references=nu.dedupe_refs(refs), fingerprint=fp, notes=notes)
        res.children = [c for c in children if c]
        return res

    def _child_scan(self, ctx, nm, data):
        tmpd = tempfile.mkdtemp(dir=ctx.tmp_dir or None)
        try:
            p = os.path.join(tmpd, "m" + os.path.splitext(nm)[1][:12])
            with open(p, "wb") as f:
                f.write(data)
            cctx = ctx.child(nm, size=len(data))
            return (cctx.logical_path, ctx.dispatch_scan(cctx, p))
        except Exception:
            return None
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)

    def _scan_xml(self, kind, nm, data, obs, refs):
        t, enc = _decode_text(data)
        if not t:
            return
        if len(t) >= HEAVY_MIN:
            t = "".join(seg for heavy, seg in _split_heavy(t) if not heavy)
        base = nm.rsplit("/", 1)[-1]
        if kind == "fcstd":
            for m in re.finditer(r'<Property name="(CreatedBy|LastModifiedBy|Company|Comment|License|LicenseURL|'
                                 r'Label|Label2|FileName|Id|Uid|Meta|Material|Description|Author|Designer)"'
                                 r'[^>]*>\s*<(?:String|Uuid|Map|PropertyFile)\s+value="([^"]*)"', t):
                pn, v = m.group(1), nu.xml_unescape(m.group(2))
                if not v.strip() or pn in ("Uid",):
                    continue
                k = {"CreatedBy": "person", "LastModifiedBy": "person", "Company": "company",
                     "FileName": "path", "LicenseURL": "url"}.get(pn, "free_text")
                nu.add_obs(obs, k, f"fcstd.{base}.{pn}", v, "FreeCAD property", 0.8 if k != "free_text" else 0.4)
                nu.contact_observations(v, f"fcstd.{base}.{pn}", obs)
            for m in _FC_FILE_ATTR.finditer(t):
                v = nu.xml_unescape(m.group(2))
                if v:
                    refs.append(Reference(v, "xref", f"fcstd.{base}.link"))
            for m in re.finditer(r'<Cell address="[^"]+" content="([^"]{2,200})"', t):
                v = nu.xml_unescape(m.group(1))
                if not v.startswith("=") and not re.fullmatch(r"[\d.,+\-eE ]+(?:mm|cm|m|in|deg|°)?", v):
                    nu.add_obs(obs, "free_text", "fcstd.spreadsheet.cell", v, "spreadsheet cell", 0.3)
        elif kind == "3mf":
            for m in re.finditer(r'<metadata\s+name="([^"]+)"[^>]*>([^<]*)</metadata>', t):
                pn, v = m.group(1), nu.xml_unescape(m.group(2)).strip()
                if not v:
                    continue
                k = nu.prop_kind(pn.split(":")[-1])
                if pn.lower() in ("application", "creationdate", "modificationdate", "bambustudio:3mfversion"):
                    k = "software" if pn.lower() == "application" else None
                if k:
                    nu.add_obs(obs, k, f"3mf.metadata.{pn[:40]}", v, base, 0.7)
                    nu.contact_observations(v, f"3mf.metadata.{pn[:40]}", obs)
            for m in re.finditer(r'<object\b[^>]*\bname="([^"]+)"', t):
                nu.add_obs(obs, "part_name", "3mf.object.name", nu.xml_unescape(m.group(1)), base, 0.3)
            for m in re.finditer(r'(?:key="(?:name|source_file)"\s+value|<plate>.*?name)="([^"]+)"', t):
                nu.add_obs(obs, "free_text", "3mf.config.name", nu.xml_unescape(m.group(1)), base, 0.3)
        else:
            for m in re.finditer(r"<(meta:initial-creator|dc:creator|meta:printed-by|dc:title|dc:subject|"
                                 r"dc:description|meta:keyword|meta:user-defined[^>]*)>([^<]+)<", t):
                tag, v = m.group(1).split()[0], nu.xml_unescape(m.group(2))
                k = "person" if tag in ("meta:initial-creator", "dc:creator", "meta:printed-by") else "free_text"
                nu.add_obs(obs, k, f"odf.{tag}", v, base, 0.8 if k == "person" else 0.4)
            if base == "content.xml" or base == "styles.xml":
                for m in re.finditer(r"<text:(?:p|span|h)\b[^>]*>([^<]{2,300})<", t):
                    nu.add_obs(obs, "free_text", f"odf.{base}.text", nu.xml_unescape(m.group(1)), base, 0.3)
                for m in re.finditer(r'xlink:href="([^"#][^"]*)"', t):
                    v = nu.xml_unescape(m.group(1))
                    if not v.startswith(("Pictures/", "./Pictures", "Object")):
                        refs.append(Reference(v, "uri" if "://" in v else "path", f"odf.{base}.href"))
        nu.contact_observations(t, f"zippkg.{kind}.{base}", obs)

    # -- rewrite --------------------------------------------------------------
    def rewrite(self, ctx, src_path, dst_path, plan):
        try:
            z = zipfile.ZipFile(src_path)
        except Exception as e:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[f"not a readable zip: {type(e).__name__}"])
        kind = self._kind(ctx)
        new_data = {}
        counts = {"members_changed": 0, "xml_values": 0, "text_members": 0, "dat_strings": 0,
                  "embedded_rewritten": 0, "members_skipped": 0}
        notes, children = [], []
        with z:
            for info in z.infolist():
                nm = info.filename
                low = nm.lower()
                if nm.endswith("/") or info.file_size > MAX_MEMBER:
                    continue
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    counts["members_skipped"] += 1
                    continue
                try:
                    data = z.read(info)
                except Exception:
                    counts["members_skipped"] += 1
                    continue
                new = None
                if (low.endswith(IMG_EXTS) or (kind != "fcstd" and low.endswith(
                        (".f3d", ".fcstd", ".3mf", ".pdf", ".step", ".stp", ".stl", ".dxf")))):
                    new = self._child_rewrite(ctx, nm, data, plan, children)
                    if new is not None:
                        counts["embedded_rewritten"] += 1
                elif low.endswith(".dat") and kind == "fusion":
                    new = self._edit_dat(data, plan, counts)
                elif _is_xml(nm, data):
                    new = self._edit_xml(ctx, kind, nm, data, plan, counts)
                elif low.endswith(TEXT_EXTS) or (kind == "3mf" and low.startswith("metadata/")):
                    t, enc = _decode_text(data)
                    if t is not None and enc != "cp1252":
                        nt = plan.replace_text(t)
                        if nt != t:
                            new = _encode_text(nt, enc)
                            counts["text_members"] += 1
                if new is not None and new != data:
                    new_data[nm] = new
        if not new_data:
            return RewriteResult(status=VERIFIED_UNCHANGED, counts=counts, notes=notes, children=children)
        try:
            rewrite_zip(src_path, dst_path, new_data)
        except ValueError as e:
            return RewriteResult(status=COPIED_UNSUPPORTED, counts=counts,
                                 notes=[f"container not rewritable: {e}"], children=children)
        counts["members_changed"] = len(new_data)
        return RewriteResult(status=REWRITTEN, counts=counts, notes=notes, children=children)

    def _child_rewrite(self, ctx, nm, data, plan, children):
        if ctx.dispatch_rewrite is None:
            return None
        tmpd = tempfile.mkdtemp(dir=ctx.tmp_dir or None)
        try:
            ext = os.path.splitext(nm)[1][:12]
            sp, dp = os.path.join(tmpd, "s" + ext), os.path.join(tmpd, "d" + ext)
            with open(sp, "wb") as f:
                f.write(data)
            cctx = ctx.child(nm, size=len(data))
            rr = ctx.dispatch_rewrite(cctx, sp, dp, plan)
            children.append((cctx.logical_path, rr))
            if rr is not None and rr.status == REWRITTEN and os.path.exists(dp):
                with open(dp, "rb") as f:
                    return f.read()
            return None
        except Exception:
            return None
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)

    def _edit_dat(self, data, plan, counts):
        buf = None
        for off, ln, t in _iter_lp_strings(data):
            if re.fullmatch(r"[0-9A-Fa-f\-{}$%#]+", t):
                continue
            nt = nu.sub_value(plan, t)
            if nt == t:
                continue
            nb = nt.encode("utf-8")
            if len(nb) != ln:
                continue
            if buf is None:
                buf = bytearray(data)
            buf[off:off + ln] = nb
            counts["dat_strings"] += 1
        return bytes(buf) if buf is not None else None

    def _edit_xml(self, ctx, kind, nm, data, plan, counts):
        t, enc = _decode_text(data)
        if t is None:
            return None
        segs = _split_heavy(t)
        light = "".join(seg for heavy, seg in segs if not heavy)
        # fast path: nothing in this member is known to the plan
        if plan.replace_text(nu.xml_unescape(light)) == nu.xml_unescape(light):
            return None
        keep = set(nu._XML_ATTR_KEEP)
        if kind == "3mf":
            keep.discard("name")
        if kind == "fcstd":
            keep.update({"file", "Name", "object", "sub", "link", "obj", "ObjName", "address", "alias"})

        def vf(v, where):
            if where in ("file",):
                return v
            if nu.looks_like_ref(v) and ("/" in v or "\\" in v or kind == "fcstd"):
                try:
                    nv = plan.map_reference(ctx, v)
                except Exception:
                    nv = v
                if nv != v:
                    return nv
            return nu.sub_value(plan, v)

        if len(segs) == 1:
            nt, n = nu.rewrite_xml_values(t, vf, keep)
        else:
            parts, n = [], 0
            for heavy, seg in segs:
                if heavy:
                    parts.append(seg)
                    continue
                # segments are cut at tag boundaries, so they are valid fragments
                ns, k = nu.rewrite_xml_values(seg, vf, keep)
                parts.append(ns)
                n += k
            nt = "".join(parts)
        # FreeCAD external links: file="..." on XLink elements -> map_reference
        if kind == "fcstd":
            def fl(m):
                v = nu.xml_unescape(m.group(2))
                try:
                    nv = plan.map_reference(ctx, v)
                except Exception:
                    nv = v
                if nv == v:
                    return m.group(0)
                return m.group(1) + nu.xml_escape(nv, '"') + m.group(3)
            nt2 = _FC_FILE_ATTR.sub(fl, nt)
            if nt2 != nt:
                n += 1
                nt = nt2
        if not n:
            return None
        nb = _encode_text(nt, enc)
        if _xml_parses(data) and not _xml_parses(nb):
            return None
        counts["xml_values"] += n
        return nb

    # -- verify ----------------------------------------------------------------
    def verify(self, ctx, src_path, dst_path, plan):
        try:
            with zipfile.ZipFile(src_path) as a, zipfile.ZipFile(dst_path) as b:
                ia, ib = a.infolist(), b.infolist()
                if [(i.filename, i.compress_type) for i in ia] != [(i.filename, i.compress_type) for i in ib]:
                    return VerifyResult(ok=False, notes=["member list changed"])
                for x, y in zip(ia, ib):
                    if y.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        # cannot decode here: must be byte-identical to the source member
                        if (x.CRC, x.compress_size) != (y.CRC, y.compress_size):
                            return VerifyResult(ok=False, notes=["opaque member changed"])
                        continue
                    if y.filename.endswith("/"):
                        continue
                    try:
                        db = b.read(y)   # CRC checked by zipfile
                    except Exception:
                        return VerifyResult(ok=False, notes=["member fails CRC/decode"])
                    if x.CRC != y.CRC and _is_xml(y.filename, db):
                        da = a.read(x)
                        if _xml_parses(da) and not _xml_parses(db):
                            return VerifyResult(ok=False, notes=["xml member no longer parses"])
                if ia and ia[0].filename == "mimetype" and ib[0].compress_type != zipfile.ZIP_STORED:
                    return VerifyResult(ok=False, notes=["mimetype not stored"])
        except Exception as e:
            return VerifyResult(ok=False, notes=[f"zip reopen failed: {type(e).__name__}"])
        return VerifyResult(ok=True, fingerprint={"members": len(ib)})


register(ZipPackageAdapter())
