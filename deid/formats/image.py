"""Raster images: PNG, JPEG, TIFF (owner: docs-native).

Metadata edits are lossless (no pixel re-encoding):
* PNG: tEXt / zTXt / iTXt chunks (Author, Comment, Description, Software,
  Copyright, Title, XMP ...) rewritten through the plan with fresh CRCs; eXIf
  handled like a TIFF/Exif block (same-length string edits).
* JPEG: COM segments, APP1 Exif (TIFF IFD ASCII and XP* UTF-16 tags, same
  length in place, including the IFD1 thumbnail's tags), APP1 XMP (any
  length, segment length updated), APP13 Photoshop/IRB-IPTC (same length).
* TIFF: ASCII tags in every IFD (same length, in place).

Pixel text (title blocks, logos with text): optional OCR via
``deid.formats._ocr`` (``DEID_IMAGE_OCR``; default on).  Only when the plan
matches OCR'd words are the pixels changed; the image is then re-encoded with
the same dimensions, mode, DPI and ICC profile (PNG lossless; JPEG at
quality 95 with the original subsampling).
"""
from __future__ import annotations

import io
import os
import re
import struct
import zlib
from typing import Optional

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION)
from deid.formats import _nativeutil as nu

PNG_SIG = b"\x89PNG\r\n\x1a\n"
TEXT_KIND = {"author": "person", "artist": "person", "copyright": "company", "software": "software",
             "comment": "free_text", "description": "free_text", "title": "free_text", "source": "free_text",
             "disclaimer": "free_text", "warning": "free_text", "creation time": None}
EXIF_ASCII_KIND = {0x010E: "free_text", 0x010F: "software", 0x0110: "software", 0x0131: "software",
                   0x013B: "person", 0x8298: "company", 0x9286: "free_text", 0xA430: "person",
                   0xA431: None, 0xA420: None, 0x0132: None, 0x9003: None, 0x9004: None}
XP_TAGS = {0x9C9B: "free_text", 0x9C9C: "free_text", 0x9C9D: "person", 0x9C9E: "free_text", 0x9C9F: "free_text"}


def _ocr_enabled():
    return os.environ.get("DEID_IMAGE_OCR", "1") == "1"


# ---------------------------------------------------------------------------
# TIFF / Exif IFD walking (in place, same length)
# ---------------------------------------------------------------------------
def tiff_strings(buf: bytes, base: int = 0, limit: int = 0):
    """Yield (tag, type, value_offset, nbytes) for ASCII (2) and UNDEFINED/BYTE
    XP tags in all IFDs (incl. Exif, GPS, IFD1) of a TIFF structure that starts
    at ``base``.  Offsets are absolute in ``buf``."""
    end = limit or len(buf)
    if base + 8 > end:
        return
    bo = buf[base:base + 2]
    if bo == b"II":
        e = "<"
    elif bo == b"MM":
        e = ">"
    else:
        return
    if struct.unpack_from(e + "H", buf, base + 2)[0] != 42:
        return
    first = struct.unpack_from(e + "I", buf, base + 4)[0]
    todo = [first]
    seen = set()
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
    while todo:
        off = todo.pop()
        if off == 0 or off in seen or base + off + 2 > end or len(seen) > 64:
            continue
        seen.add(off)
        n = struct.unpack_from(e + "H", buf, base + off)[0]
        if n > 2000 or base + off + 2 + 12 * n > end:
            continue
        for i in range(n):
            p = base + off + 2 + 12 * i
            tag, typ, cnt = struct.unpack_from(e + "HHI", buf, p)
            sz = sizes.get(typ, 0) * cnt
            if tag in (0x8769, 0x8825, 0xA005) and typ in (4, 13):
                todo.append(struct.unpack_from(e + "I", buf, p + 8)[0])
                continue
            if typ == 2 or (tag in XP_TAGS and typ in (1, 7)):
                if sz <= 4:
                    voff = p + 8
                else:
                    voff = base + struct.unpack_from(e + "I", buf, p + 8)[0]
                if voff + sz <= end and sz > 0:
                    yield tag, typ, voff, sz
        nxt_p = base + off + 2 + 12 * n
        if nxt_p + 4 <= end:
            todo.append(struct.unpack_from(e + "I", buf, nxt_p)[0])


def _decode_tiff_str(raw: bytes, tag: int, typ: int):
    if tag in XP_TAGS and typ != 2:
        return raw.decode("utf-16-le", "replace").rstrip("\x00"), "utf-16-le"
    t = raw.split(b"\x00", 1)[0]
    try:
        return t.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return t.decode("latin-1"), "latin-1"


def edit_tiff_strings(buf: bytearray, base: int, plan, counts, limit: int = 0, obs=None, field="tiff"):
    for tag, typ, voff, sz in list(tiff_strings(bytes(buf), base, limit)):
        raw = bytes(buf[voff:voff + sz])
        val, enc = _decode_tiff_str(raw, tag, typ)
        if not val.strip():
            continue
        kind = XP_TAGS.get(tag) or EXIF_ASCII_KIND.get(tag, "free_text")
        if obs is not None:
            if kind is not None:
                nu.add_obs(obs, kind, f"{field}.0x{tag:04X}", val, "Exif/TIFF tag", 0.7 if kind == "person" else 0.4)
                nu.contact_observations(val, f"{field}.0x{tag:04X}", obs)
            continue
        if kind is None or kind == "software":
            # software strings are kept unless they carry an identity
            nv = plan.replace_text(val)
        else:
            nv = nu.sub_value(plan, val, kind)
        if nv == val:
            continue
        nb = nv.encode(enc, "replace")
        vb = len(val.encode(enc, "replace"))
        if len(nb) != vb:
            nb = nu.fit_len(nv, len(val)).encode(enc, "replace")
            if len(nb) != vb:
                continue
        buf[voff:voff + vb] = nb
        counts["exif_strings"] = counts.get("exif_strings", 0) + 1


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------
def png_chunks(data: bytes):
    if not data.startswith(PNG_SIG):
        raise ValueError("not png")
    p = 8
    while p + 12 <= len(data):
        ln = struct.unpack_from(">I", data, p)[0]
        typ = data[p + 4:p + 8]
        if p + 12 + ln > len(data):
            raise ValueError("truncated chunk")
        yield typ, data[p + 8:p + 8 + ln], p
        p += 12 + ln
        if typ == b"IEND":
            break


def _chunk(typ: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + typ + body + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF)


def _png_text(typ, body):
    """(keyword, text, rebuild(newtext)->body) or None."""
    try:
        if typ == b"tEXt":
            k, t = body.split(b"\x00", 1)
            kw = k.decode("latin-1")
            return kw, t.decode("latin-1"), lambda nt: k + b"\x00" + nt.encode("latin-1", "replace")
        if typ == b"zTXt":
            k, rest = body.split(b"\x00", 1)
            t = zlib.decompress(rest[1:])
            return (k.decode("latin-1"), t.decode("latin-1"),
                    lambda nt: k + b"\x00\x00" + zlib.compress(nt.encode("latin-1", "replace")))
        if typ == b"iTXt":
            k, rest = body.split(b"\x00", 1)
            cflag, cmeth = rest[0], rest[1]
            lang, rest2 = rest[2:].split(b"\x00", 1)
            tkw, txt = rest2.split(b"\x00", 1)
            t = zlib.decompress(txt) if cflag else txt
            head = k + b"\x00" + bytes([cflag, cmeth]) + lang + b"\x00" + tkw + b"\x00"
            return (k.decode("latin-1"), t.decode("utf-8", "replace"),
                    lambda nt: head + (zlib.compress(nt.encode("utf-8")) if cflag else nt.encode("utf-8")))
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------
def jpeg_segments(data: bytes):
    """Yield (marker, payload_start, payload_end, seg_start) up to SOS."""
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("not jpeg")
    p = 2
    n = len(data)
    while p + 4 <= n:
        if data[p] != 0xFF:
            raise ValueError("bad marker")
        m = data[p + 1]
        if m == 0xFF:
            p += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            p += 2
            continue
        ln = struct.unpack_from(">H", data, p + 2)[0]
        yield m, p + 4, p + 2 + ln, p
        if m == 0xDA:
            return
        p += 2 + ln


def _xmp_edit(text: str, plan, counts) -> str:
    from deid.formats.pdf import rewrite_xmp_text
    nt = rewrite_xmp_text(text, plan)
    if nt != text:
        counts["xmp"] = counts.get("xmp", 0) + 1
    return nt


def _iptc_edit(buf: bytearray, start: int, end: int, plan, counts, obs=None):
    """Photoshop IRB with IPTC-IIM datasets (1C rr dd len value): same length."""
    p = start
    while p + 5 <= end:
        if buf[p] == 0x1C:
            rec, ds = buf[p + 1], buf[p + 2]
            ln = struct.unpack_from(">H", buf, p + 3)[0]
            if ln & 0x8000:
                break
            vs = p + 5
            if vs + ln > end:
                break
            if rec == 2 and ds in (5, 25, 40, 55, 80, 85, 90, 92, 95, 100, 101, 105, 110, 115, 116, 118, 120, 122):
                raw = bytes(buf[vs:vs + ln])
                try:
                    val = raw.decode("utf-8")
                    enc = "utf-8"
                except UnicodeDecodeError:
                    val, enc = raw.decode("latin-1"), "latin-1"
                kind = {80: "person", 85: "free_text", 110: "company", 116: "company", 118: "free_text",
                        122: "person", 90: "address", 92: "address", 95: "address", 101: "address"}.get(ds, "free_text")
                if obs is not None:
                    nu.add_obs(obs, kind, f"iptc.2.{ds}", val, "IPTC", 0.6)
                else:
                    nv = nu.sub_value(plan, val, kind)
                    if nv != val:
                        nb = nu.fit_len(nv, len(val)).encode(enc, "replace")
                        if len(nv.encode(enc, "replace")) == ln:
                            nb = nv.encode(enc, "replace")
                        if len(nb) == ln:
                            buf[vs:vs + ln] = nb
                            counts["iptc"] = counts.get("iptc", 0) + 1
            p = vs + ln
        else:
            p += 1


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
class ImageAdapter(Adapter):
    name = "image"
    version = "1"
    priority = 10
    magics = ("png", "jpeg", "tiff")
    extensions = (".png", ".jpg", ".jpeg", ".tif", ".tiff")
    path_independent = True
    timeout_s = 600.0

    def scan(self, ctx, src_path):
        with open(src_path, "rb") as f:
            data = f.read()
        obs, notes = [], []
        fp = {"size": len(data)}
        counts = {}
        try:
            if data.startswith(PNG_SIG):
                fp["format"] = "png"
                for typ, body, _ in png_chunks(data):
                    if typ in (b"tEXt", b"zTXt", b"iTXt"):
                        t = _png_text(typ, body)
                        if t:
                            kw, val, _ = t
                            if "xmp" in kw.lower():
                                from deid.formats.pdf import xmp_observations
                                xmp_observations(val, obs, "png.xmp")
                            else:
                                k = TEXT_KIND.get(kw.lower(), "free_text")
                                if k:
                                    nu.add_obs(obs, k, f"png.text.{kw[:30]}", val[:2000], "PNG text chunk", 0.6)
                                nu.contact_observations(val, f"png.text.{kw[:30]}", obs)
                    elif typ == b"eXIf":
                        edit_tiff_strings(bytearray(body), 0, None, counts, obs=obs, field="png.exif")
            elif data.startswith(b"\xff\xd8"):
                fp["format"] = "jpeg"
                for m, s, e, _ in jpeg_segments(data):
                    seg = data[s:e]
                    if m == 0xE1 and seg.startswith(b"Exif\x00\x00"):
                        edit_tiff_strings(bytearray(data), s + 6, None, counts, limit=e, obs=obs, field="jpeg.exif")
                    elif m == 0xE1 and seg.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                        from deid.formats.pdf import xmp_observations
                        xmp_observations(seg[29:].decode("utf-8", "replace"), obs, "jpeg.xmp")
                    elif m == 0xFE:
                        v = seg.decode("utf-8", "replace")
                        nu.add_obs(obs, "free_text", "jpeg.comment", v[:2000], "COM segment", 0.5)
                        nu.contact_observations(v, "jpeg.comment", obs)
                    elif m == 0xED:
                        _iptc_edit(bytearray(data), s, e, None, counts, obs=obs)
            elif data[:4] in (b"II*\x00", b"MM\x00*"):
                fp["format"] = "tiff"
                edit_tiff_strings(bytearray(data), 0, None, counts, obs=obs, field="tiff")
        except Exception as e:
            notes.append(f"metadata parse incomplete: {type(e).__name__}")
        try:
            from PIL import Image
            with Image.open(src_path) as im:
                fp.update(width=im.size[0], height=im.size[1], mode=im.mode)
                if _ocr_enabled() and os.environ.get("DEID_IMAGE_OCR_SCAN", "0") == "1":
                    from deid.formats import _ocr
                    if _ocr.available() and im.size[0] * im.size[1] <= _ocr.MAX_PIXELS:
                        im.load()
                        for line in _ocr.ocr_text(im, ctx.tmp_dir):
                            if len(line.strip()) >= 4:
                                nu.add_obs(obs, "free_text", "image.ocr", line[:300], "OCR line", 0.2)
                                nu.contact_observations(line, "image.ocr", obs)
        except Exception as e:
            notes.append(f"image not decodable: {type(e).__name__}")
            return ScanResult(observations=nu.dedupe_obs(obs), fingerprint=fp, notes=notes,
                              status="source_exception" if "width" not in fp else "ok")
        return ScanResult(observations=nu.dedupe_obs(obs), fingerprint=fp, notes=notes)

    def rewrite(self, ctx, src_path, dst_path, plan):
        with open(src_path, "rb") as f:
            data = f.read()
        counts = {}
        try:
            if data.startswith(PNG_SIG):
                new = self._png(data, plan, counts)
            elif data.startswith(b"\xff\xd8"):
                new = self._jpeg(data, plan, counts)
            elif data[:4] in (b"II*\x00", b"MM\x00*"):
                buf = bytearray(data)
                edit_tiff_strings(buf, 0, plan, counts)
                new = bytes(buf)
            else:
                return RewriteResult(status=VERIFIED_UNCHANGED)
        except ValueError as e:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[f"malformed image: {e}"])
        # pixel text
        if _ocr_enabled():
            try:
                new = self._ocr(ctx, new, plan, counts)
            except Exception as e:
                counts["ocr_errors"] = 1
        if new == data:
            return RewriteResult(status=VERIFIED_UNCHANGED, counts=counts)
        with open(dst_path, "wb") as f:
            f.write(new)
        return RewriteResult(status=REWRITTEN, counts=counts)

    def _png(self, data, plan, counts):
        out = [PNG_SIG]
        changed = False
        for typ, body, _ in png_chunks(data):
            if typ in (b"tEXt", b"zTXt", b"iTXt"):
                t = _png_text(typ, body)
                if t:
                    kw, val, rebuild = t
                    if "xmp" in kw.lower():
                        nv = _xmp_edit(val, plan, counts)
                    else:
                        k = TEXT_KIND.get(kw.lower(), "free_text")
                        nv = plan.replace_text(val) if k in (None, "software") else nu.sub_value(plan, val, k)
                    if nv != val:
                        out.append(_chunk(typ, rebuild(nv)))
                        counts["png_text"] = counts.get("png_text", 0) + 1
                        changed = True
                        continue
            elif typ == b"eXIf":
                b = bytearray(body)
                edit_tiff_strings(b, 0, plan, counts)
                if bytes(b) != body:
                    out.append(_chunk(typ, bytes(b)))
                    changed = True
                    continue
            out.append(_chunk(typ, body))
        if not changed:
            return data
        # keep anything after IEND verbatim
        end = data.rfind(b"IEND")
        tail = data[end + 8:] if end > 0 else b""
        return b"".join(out) + tail

    def _jpeg(self, data, plan, counts):
        buf = bytearray(data)
        repl = []   # (start, end, new_segment_bytes) for length-changing XMP/COM
        for m, s, e, seg_start in jpeg_segments(data):
            seg = data[s:e]
            if m == 0xE1 and seg.startswith(b"Exif\x00\x00"):
                edit_tiff_strings(buf, s + 6, plan, counts, limit=e)
            elif m == 0xE1 and seg.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                t = seg[29:].decode("utf-8", "replace")
                nt = _xmp_edit(t, plan, counts)
                if nt != t:
                    body = seg[:29] + nt.encode("utf-8")
                    if len(body) + 2 <= 0xFFFF:
                        repl.append((seg_start, e, b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body))
            elif m == 0xFE:
                try:
                    t = seg.decode("utf-8")
                    enc = "utf-8"
                except UnicodeDecodeError:
                    t, enc = seg.decode("latin-1"), "latin-1"
                nt = plan.replace_text(t)
                if nt != t:
                    body = nt.encode(enc, "replace")
                    repl.append((seg_start, e, b"\xff\xfe" + struct.pack(">H", len(body) + 2) + body))
                    counts["jpeg_comment"] = counts.get("jpeg_comment", 0) + 1
            elif m == 0xED:
                _iptc_edit(buf, s, e, plan, counts)
        if repl:
            out = bytearray()
            p = 0
            for a, b, nb in sorted(repl):
                out += buf[p:a] + nb
                p = b
            out += buf[p:]
            return bytes(out)
        return bytes(buf)

    def _ocr(self, ctx, data, plan, counts):
        from deid.formats import _ocr
        if not _ocr.available():
            return data
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if getattr(im, "n_frames", 1) > 1 or im.size[0] * im.size[1] > _ocr.MAX_PIXELS:
            return data
        im.load()
        new, n = _ocr.redact_image(im, plan, ctx.tmp_dir)
        if not n or new is None:
            return data
        counts["ocr_regions"] = n
        out = io.BytesIO()
        info = im.info
        kw = {}
        if info.get("icc_profile"):
            kw["icc_profile"] = info["icc_profile"]
        if info.get("dpi"):
            kw["dpi"] = info["dpi"]
        fmt = im.format
        if fmt == "JPEG":
            exif = im.info.get("exif")
            if exif:
                kw["exif"] = exif
            try:
                from PIL import JpegImagePlugin
                kw["subsampling"] = JpegImagePlugin.get_sampling(im)
            except Exception:
                pass
            new.save(out, "JPEG", quality=95, **kw)
        elif fmt == "TIFF":
            new.save(out, "TIFF", compression=im.info.get("compression", "tiff_lzw") or "tiff_lzw", **kw)
        else:
            if "transparency" in info:
                kw["transparency"] = info["transparency"]
            new.save(out, "PNG", optimize=False, **kw)
            # carry the (already rewritten) text chunks over
            res = out.getvalue()
            texts = [c for c in (_chunk(t, b) for t, b, _ in png_chunks(data) if t in (b"tEXt", b"zTXt", b"iTXt"))]
            if texts:
                i = res.find(b"IDAT") - 4
                res = res[:i] + b"".join(texts) + res[i:]
            return res
        return out.getvalue()

    def verify(self, ctx, src_path, dst_path, plan):
        try:
            from PIL import Image
            with Image.open(src_path) as a, Image.open(dst_path) as b:
                if a.size != b.size or a.mode != b.mode or a.format != b.format:
                    return VerifyResult(ok=False, notes=["image geometry/mode changed"])
                b.load()
                if getattr(a, "n_frames", 1) != getattr(b, "n_frames", 1):
                    return VerifyResult(ok=False, notes=["frame count changed"])
                if (a.info.get("icc_profile") or b"") != (b.info.get("icc_profile") or b""):
                    return VerifyResult(ok=False, notes=["icc profile changed"])
        except Exception as e:
            return VerifyResult(ok=False, notes=[f"decode failed: {type(e).__name__}"])
        return VerifyResult(ok=True)


register(ImageAdapter())
