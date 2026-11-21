"""OLE / Compound File Binary documents: Inventor (.ipt .iam .idw .ipn and
templates), Solid Edge (.par .psm .asm .dft .pwd), legacy SOLIDWORKS
(pre-2015 .sldprt .sldasm .slddrw, .slddrt, .prtdot ...), NX .prt in CFB, and
any other CFB file routed here.

Every edit keeps every stream's size (the identity plan's aliases are
length-preserving), so the container is patched in place with
``olefile.write_stream`` — the CFB directory, FAT and sector chains never
change.

* Property-set streams (``\\x05...``: SummaryInformation,
  DocumentSummaryInformation incl. user-defined section, Inventor iProperties
  such as Design Tracking / User Defined, Solid Edge property sets): parsed
  per the MS-OLEPS layout; VT_LPSTR / VT_LPWSTR values rewritten in place
  (same byte count; the dictionary of property names is left alone).
* Other uncompressed streams: MFC unicode CStrings and UTF-16LE / ASCII
  text runs; only runs that contain a plan identity are rewritten, at the same
  length.  File references (component / drawing paths) follow
  ``plan.map_reference`` when the result keeps the length.
* zlib-compressed streams (SOLIDWORKS ``*__ZLB``), previews and sheet images
  are scanned but not modified.

verify(): output opens with olefile, identical directory (names, sizes,
types), every unchanged stream byte-identical, edited property sets re-parse
to the same property ids/types.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import zlib
from typing import Optional

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult, Reference,
                      REWRITTEN, VERIFIED_UNCHANGED, COPIED_UNSUPPORTED, SOURCE_EXCEPTION)
from deid.formats import _nativeutil as nu

try:
    import olefile
except Exception:  # pragma: no cover
    olefile = None

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
VT_LPSTR, VT_LPWSTR, VT_I2 = 0x1E, 0x1F, 0x02

FMTID_SUMMARY = "F29F85E0-4FF9-1068-AB91-08002B27B3D9"
FMTID_DOCSUMMARY = "D5CDD502-2E9C-101B-9397-08002B2CF9AE"
FMTID_USER = "D5CDD505-2E9C-101B-9397-08002B2CF9AE"
FMTID_INV_DESIGN = "32853F0F-3444-11D1-9E93-0060B03C1CA6"
FMTID_INV_SUMMARY = "F29F85E0-4FF9-1068-AB91-08002B27B3D9"

SUMMARY_KIND = {2: "part_name", 3: "free_text", 4: "saved_account", 5: "free_text", 6: "free_text",
                7: None, 8: "saved_account", 9: None, 18: "software"}
DOCSUMMARY_KIND = {2: "free_text", 14: "person", 15: "company", 26: None, 27: None}
# Inventor "Design Tracking Properties" (documented iProperty ids)
INV_DESIGN_KIND = {5: "part_name", 7: "free_text", 9: "person", 10: "free_text", 11: "free_text",
                   12: "free_text", 13: "free_text", 17: "person", 18: "person", 19: "person",
                   20: "free_text", 21: "free_text", 22: "free_text", 23: "free_text", 24: "free_text",
                   28: "url", 29: "free_text", 36: "part_number", 37: "project", 38: "person",
                   39: "person", 40: "part_name", 41: "person", 42: "person", 43: "free_text",
                   44: "free_text", 46: "free_text", 47: "free_text", 48: "free_text", 49: "person",
                   56: "free_text", 58: "free_text", 59: "document_number", 61: "free_text"}

CAD_REF = re.compile(r"\.(ipt|iam|idw|ipn|ipj|dwg|par|psm|asm|dft|pwd|sldprt|sldasm|slddrw|slddrt|prt|drw|"
                     r"catpart|catproduct|step|stp|igs|iges|x_t|x_b|sat|jt|xls|xlsx|pdf)(?::\d+)?\b", re.I)
_U16RUN = re.compile(rb"(?:[\x20-\x7e\xa0-\xff]\x00){4,}")
_ASCRUN = re.compile(rb"[\x20-\x7e]{5,}")
_SKIP_STREAM = re.compile(r"(__ZLB$|Preview|Thumbnail|^Images/|Bitmap|PNG$|\x01CompObj$|"
                          r"DisplayLists|Display/Cache|ParasolidPartition|Partition$|Protein$|"
                          r"PSMspacemap)", re.I)


def _guid(b: bytes) -> str:
    d1, d2, d3 = struct.unpack_from("<IHH", b, 0)
    return f"{d1:08X}-{d2:04X}-{d3:04X}-{b[8:10].hex().upper()}-{b[10:16].hex().upper()}"


class PropString:
    __slots__ = ("pid", "vt", "off", "nbytes", "enc", "value", "fmtid")


def parse_propset(data: bytes):
    """Return (sections, strings, names) for an OLE property-set stream.
    strings: list[PropString] (byte range of the string payload w/o NUL)."""
    if len(data) < 48 or data[:2] != b"\xfe\xff":
        raise ValueError("not a property set")
    nsec = struct.unpack_from("<I", data, 24)[0]
    if nsec > 16:
        raise ValueError("too many sections")
    out, names, secs = [], {}, []
    for i in range(nsec):
        fmtid = _guid(data[28 + 20 * i:44 + 20 * i])
        so = struct.unpack_from("<I", data, 44 + 20 * i)[0]
        if so + 8 > len(data):
            raise ValueError("bad section offset")
        ssize, nprop = struct.unpack_from("<II", data, so)
        if nprop > 10000 or so + 8 + 8 * nprop > len(data):
            raise ValueError("bad property count")
        props = [struct.unpack_from("<II", data, so + 8 + 8 * k) for k in range(nprop)]
        codepage = 1252
        for pid, po in props:
            if pid == 1 and so + po + 6 <= len(data):
                vt = struct.unpack_from("<I", data, so + po)[0] & 0xFFFF
                if vt == VT_I2:
                    codepage = struct.unpack_from("<h", data, so + po + 4)[0] & 0xFFFF
        enc = {1200: "utf-16-le", 65001: "utf-8", 1252: "cp1252", 0: "cp1252"}.get(codepage, f"cp{codepage}")
        try:
            "a".encode(enc)
        except LookupError:
            enc = "cp1252"
        secs.append((fmtid, so, [p for p, _ in props]))
        for pid, po in props:
            a = so + po
            if a + 8 > len(data):
                continue
            if pid == 0:
                # dictionary: count, then (pid, len, name)
                try:
                    cnt = struct.unpack_from("<I", data, a)[0]
                    q = a + 4
                    for _ in range(min(cnt, 10000)):
                        dpid, ln = struct.unpack_from("<II", data, q)
                        q += 8
                        if enc == "utf-16-le":
                            nm = data[q:q + 2 * ln].decode("utf-16-le", "replace").rstrip("\x00")
                            q += 2 * ln
                            q += (-q) % 4
                        else:
                            nm = data[q:q + ln].decode(enc, "replace").rstrip("\x00")
                            q += ln
                        names[(fmtid, dpid)] = nm
                except struct.error:
                    pass
                continue
            vt = struct.unpack_from("<I", data, a)[0] & 0xFFFF
            if vt not in (VT_LPSTR, VT_LPWSTR):
                continue
            cnt = struct.unpack_from("<I", data, a + 4)[0]
            ps = PropString()
            ps.pid, ps.vt, ps.fmtid = pid, vt, fmtid
            ps.off = a + 8
            if vt == VT_LPWSTR or enc == "utf-16-le":
                nb = 2 * cnt
                ps.enc = "utf-16-le"
            else:
                nb = cnt
                ps.enc = enc
            if cnt > 1_000_000 or ps.off + nb > len(data):
                continue
            raw = data[ps.off:ps.off + nb]
            # strip terminating NULs
            step = 2 if ps.enc == "utf-16-le" else 1
            end = nb
            while end >= step and raw[end - step:end] == b"\x00" * step:
                end -= step
            ps.nbytes = end
            try:
                ps.value = raw[:end].decode(ps.enc)
            except UnicodeDecodeError:
                continue
            out.append(ps)
    return secs, out, names


def _prop_kind(ps, names):
    if ps.fmtid == FMTID_SUMMARY:
        return SUMMARY_KIND.get(ps.pid, "free_text"), f"summary.{ps.pid}"
    if ps.fmtid == FMTID_DOCSUMMARY:
        return DOCSUMMARY_KIND.get(ps.pid, "free_text"), f"docsummary.{ps.pid}"
    if ps.fmtid == FMTID_INV_DESIGN:
        return INV_DESIGN_KIND.get(ps.pid, "free_text"), f"inventor.design.{ps.pid}"
    nm = names.get((ps.fmtid, ps.pid))
    if nm:
        return nu.prop_kind(nm), f"user.{nm[:40]}"
    return "free_text", f"{ps.fmtid[:8]}.{ps.pid}"


def _fit_encoded(plan, value: str, enc: str, nbytes: int, kind=None) -> Optional[bytes]:
    """New encoded bytes of exactly nbytes for value, or None if unchanged."""
    new = nu.sub_value(plan, value, kind)
    if new == value:
        return None
    try:
        b = new.encode(enc)
    except UnicodeEncodeError:
        b = new.encode(enc, "replace")
    if len(b) == nbytes:
        return b
    try:
        rb = plan.replace_bytes(value.encode(enc), (enc,), True)
        if len(rb) == nbytes and rb != value.encode(enc):
            return rb
    except Exception:
        pass
    # last resort: pad/truncate the pseudonym (never leave the original)
    step = 2 if enc == "utf-16-le" else 1
    if len(b) > nbytes:
        b = b[:nbytes - (nbytes % step)]
        try:
            b.decode(enc)
        except UnicodeDecodeError:
            b = new.encode("ascii", "replace")[: nbytes // step].decode().encode(enc)
    pad = " ".encode(enc)
    while len(b) + len(pad) <= nbytes:
        b += pad
    if len(b) != nbytes:
        return None
    return b


def _edit_text_runs(ctx, data: bytes, plan, counts, scan_only=False, obs=None, refs=None, field=""):
    """Same-length edits of CStrings / UTF-16 / ASCII runs in a binary stream."""
    buf = None
    done = set()
    # UTF-16 runs (covers MFC CStrings)
    for m in _U16RUN.finditer(data):
        s = m.group(0).decode("utf-16-le", "replace")
        start = m.start()
        if scan_only:
            if CAD_REF.search(s):
                for part in re.split(r"[\x00|]", s):
                    if CAD_REF.search(part):
                        refs.append(Reference(part.strip(), "component", f"cfb.{field}#fixedlen"))
                nu.contact_observations(s, f"cfb.{field}", obs)
            elif "@" in s or "\\" in s or "http" in s:
                nu.contact_observations(s, f"cfb.{field}", obs)
            continue
        if CAD_REF.search(s) and ("\\" in s or "/" in s or s.strip().count(" ") < 6):
            ns = nu.map_ref_same_len(plan, ctx, s)
            if ns is None:
                counts["refs_kept"] = counts.get("refs_kept", 0) + 1
                nu.record_constraint(ctx, s, "CFB UTF-16 reference; different length", "cfb")
                continue
            if ns != s:
                counts["refs_rewritten"] = counts.get("refs_rewritten", 0) + 1
        else:
            ns = plan.replace_text(s)
            if ns == s:
                continue
            if len(ns) != len(s):
                try:
                    nb = plan.replace_bytes(m.group(0), ("utf-16-le",), True)
                except Exception:
                    nb = b""
                ns = nb.decode("utf-16-le", "replace") if len(nb) == len(m.group(0)) else nu.fit_len(ns, len(s))
            counts["u16_runs"] = counts.get("u16_runs", 0) + 1
        enc = ns.encode("utf-16-le", "replace")
        if len(enc) != len(m.group(0)) or ns == s:
            continue
        if buf is None:
            buf = bytearray(data)
        buf[start:start + len(enc)] = enc
        done.add(start)
    # ASCII runs (8-bit strings)
    for m in _ASCRUN.finditer(data):
        s = m.group(0).decode("latin-1")
        if scan_only:
            if "@" in s or "http" in s or (CAD_REF.search(s) and ("\\" in s or "/" in s)):
                nu.contact_observations(s, f"cfb.{field}", obs)
                if CAD_REF.search(s) and ("\\" in s or "/" in s):
                    refs.append(Reference(s.strip(), "component", f"cfb.{field}#fixedlen"))
            continue
        ns = plan.replace_text(s)
        if ns == s:
            continue
        if len(ns) != len(s):
            try:
                nb = plan.replace_bytes(m.group(0), ("latin-1",), True)
            except Exception:
                nb = b""
            ns = nb.decode("latin-1") if len(nb) == len(m.group(0)) else nu.fit_len(ns, len(s))
        enc = ns.encode("latin-1", "replace")
        if len(enc) != len(m.group(0)):
            continue
        if buf is None:
            buf = bytearray(data)
        buf[m.start():m.end()] = enc
        counts["ascii_runs"] = counts.get("ascii_runs", 0) + 1
    return bytes(buf) if buf is not None else None


def _zlb_payload(data: bytes) -> Optional[bytes]:
    for off in (0, 4, 8):
        try:
            return zlib.decompressobj().decompress(data[off:], 64 << 20)
        except zlib.error:
            continue
    try:
        return zlib.decompressobj(-15).decompress(data, 64 << 20)
    except zlib.error:
        return None


# ---------------------------------------------------------------------------
# Dead space: bytes no CFB reader ever returns (free sectors, slack after a stream's
# last byte, free mini sectors, unused directory entries). Old content of deleted or
# shrunk streams survives there; zeroing it cannot change any stream.
_EMPTY_DIRENT = b"\x00" * 66 + b"\x00\x00" + b"\xff\xff\xff\xff" * 3 + b"\x00" * 50


def dead_regions(path):
    """[(file_offset, length)] of dead bytes in a CFB file (requires olefile)."""
    regions = []
    ole = olefile.OleFileIO(path)
    try:
        ss = ole.sectorsize
        fsize = os.path.getsize(path)

        def soff(i):
            return (i + 1) * ss
        fat = ole.fat
        for i, v in enumerate(fat):
            if v == olefile.FREESECT and soff(i) + ss <= fsize:
                regions.append((soff(i), ss))

        def chain(start, limit=1 << 22):
            out, s, n = [], start, 0
            while 0 <= s < len(fat) and n < limit:
                out.append(s)
                s = fat[s]
                n += 1
            return out
        cutoff = ole.minisectorcutoff
        mss = ole.minisectorsize
        root = ole.root
        mini_chain = chain(root.isectStart) if root.size else []
        for de in ole.direntries:
            if de is None:
                continue
            if de.entry_type == olefile.STGTY_STREAM and de.size >= cutoff:
                ch = chain(de.isectStart)
                used = de.size % ss
                if ch and used and len(ch) * ss >= de.size:
                    last = ch[(de.size - 1) // ss]
                    regions.append((soff(last) + used, ss - used))
        # root (mini stream container) slack
        if mini_chain and root.size % ss and len(mini_chain) * ss >= root.size:
            last = mini_chain[(root.size - 1) // ss]
            regions.append((soff(last) + root.size % ss, ss - root.size % ss))

        def moff(j):
            k = (j * mss) // ss
            if k >= len(mini_chain):
                return None
            return soff(mini_chain[k]) + (j * mss) % ss
        try:
            ole.loadminifat()
            minifat = ole.minifat
        except Exception:
            minifat = []
        for j, v in enumerate(minifat):
            if v == olefile.FREESECT and (j + 1) * mss <= root.size:
                o = moff(j)
                if o is not None:
                    regions.append((o, mss))
        for de in ole.direntries:
            if de is None or de.entry_type != olefile.STGTY_STREAM or de.size >= cutoff or not de.size:
                continue
            s, n, ch = de.isectStart, 0, []
            while 0 <= s < len(minifat) and n < 1 << 20:
                ch.append(s)
                s = minifat[s]
                n += 1
            used = de.size % mss
            if ch and used and len(ch) * mss >= de.size:
                o = moff(ch[(de.size - 1) // mss])
                if o is not None:
                    regions.append((o + used, mss - used))
    finally:
        ole.close()
    return regions


def _read_regions(path, regions):
    out = []
    with open(path, "rb") as f:
        for off, ln in regions:
            f.seek(off)
            out.append(f.read(abs(ln)))
    return out


def dead_has_identity(path, plan):
    regions = dead_regions(path)
    blob = b"\x01".join(_read_regions(path, regions))
    if not blob.strip(b"\x00\x01"):
        return False, regions
    for enc in ("utf-16-le", "latin-1"):
        try:
            if plan.replace_bytes(blob, (enc,), True) != blob:
                return True, regions
        except Exception:
            continue
    return False, regions


def _streams(path):
    ole = olefile.OleFileIO(path)
    try:
        return {"/".join(p): ole.openstream(p).read() for p in ole.listdir(streams=True, storages=True)
                if ole.get_type(p) == olefile.STGTY_STREAM} | {"#dirs": ole.listdir(streams=True, storages=True)}
    finally:
        ole.close()


def scrub_dead(path, regions):
    with open(path, "r+b") as f:
        for off, ln in regions:
            f.seek(off)
            f.write(_EMPTY_DIRENT if ln < 0 else b"\x00" * ln)
    return len(regions)


class CfbAdapter(Adapter):
    name = "cfb"
    version = "3"
    priority = 10
    magics = ("cfb",)
    path_independent = False
    timeout_s = 900.0

    def matches(self, ctx, head):
        return head[:8] == CFB_MAGIC

    def _open(self, path, write=False):
        return olefile.OleFileIO(path, write_mode=write)

    def scan(self, ctx, src_path):
        if olefile is None:
            return ScanResult(status="unsupported", notes=["olefile missing"])
        try:
            ole = self._open(src_path)
        except Exception as e:
            return ScanResult(status="source_exception", notes=[f"unreadable CFB: {type(e).__name__}"])
        obs, refs, notes = [], [], []
        entries = []
        with ole:
            for path in ole.listdir(streams=True, storages=False):
                name = "/".join(path)
                try:
                    size = ole.get_size(name)
                    data = ole.openstream(path).read()
                except Exception:
                    notes.append("stream unreadable")
                    continue
                entries.append([name.replace("\x05", "#"), size])
                field = re.sub(r"\d+", "N", name.replace("\x05", "#"))[:60]
                if path[-1].startswith("\x05"):
                    try:
                        secs, strs, names = parse_propset(data)
                    except Exception:
                        continue
                    for ps in strs:
                        k, f = _prop_kind(ps, names)
                        if k is None or not ps.value.strip():
                            continue
                        if CAD_REF.search(ps.value) and ("\\" in ps.value or "/" in ps.value):
                            refs.append(Reference(ps.value, "other", f"cfb.prop.{f}#fixedlen"))
                            nu.add_obs(obs, "path", f"cfb.prop.{f}", ps.value, field, 0.5)
                        else:
                            nu.add_obs(obs, k, f"cfb.prop.{f}", ps.value, field, 0.8 if k in ("person", "company") else 0.5)
                        nu.contact_observations(ps.value, f"cfb.prop.{f}", obs)
                    continue
                if name.endswith("__ZLB"):
                    u = _zlb_payload(data)
                    if u:
                        _edit_text_runs(ctx, u, None, {}, True, obs, refs, field + "(zlb)")
                    continue
                if _SKIP_STREAM.search(name):
                    continue
                if len(data) > 64 << 20:
                    continue
                _edit_text_runs(ctx, data, None, {}, True, obs, refs, field)
                if field in ("HeaderN",) or name.endswith("Header2"):
                    for i, (q, ln, s) in enumerate(nu.iter_cstrings_u16(data)):
                        nu.add_obs(obs, "account", "cfb.sw.HeaderN.author", s, "moHeader_c first string", 0.6)
                        break
        fp = {"streams": len(entries), "entries": entries[:2000]}
        return ScanResult(observations=nu.dedupe_obs(obs), references=nu.dedupe_refs(refs),
                          fingerprint=fp, notes=notes)

    def rewrite(self, ctx, src_path, dst_path, plan):
        if olefile is None:
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=["olefile missing"])
        try:
            ole = self._open(src_path)
        except Exception as e:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[f"unreadable CFB: {type(e).__name__}"])
        counts = {"prop_strings": 0}
        changes = {}
        with ole:
            for path in ole.listdir(streams=True, storages=False):
                name = "/".join(path)
                try:
                    data = ole.openstream(path).read()
                except Exception:
                    continue
                new = None
                if path[-1].startswith("\x05"):
                    try:
                        secs, strs, names = parse_propset(data)
                    except Exception:
                        continue
                    buf = None
                    for ps in strs:
                        k, f = _prop_kind(ps, names)
                        if CAD_REF.search(ps.value) and ("\\" in ps.value or "/" in ps.value):
                            ns = nu.map_ref_same_len(plan, ctx, ps.value)
                            nb = ns.encode(ps.enc, "replace") if ns not in (None, ps.value) else None
                            if nb is not None and len(nb) != ps.nbytes:
                                nb = None
                            if nb is None and ns is not None and ns != ps.value:
                                pass
                            if nb is None:
                                nb = _fit_encoded(plan, ps.value, ps.enc, ps.nbytes, None)
                        else:
                            nb = _fit_encoded(plan, ps.value, ps.enc, ps.nbytes, k)
                        if nb is None:
                            continue
                        if buf is None:
                            buf = bytearray(data)
                        buf[ps.off:ps.off + ps.nbytes] = nb
                        counts["prop_strings"] += 1
                    new = bytes(buf) if buf is not None else None
                elif (ctx.ext in (".slddrw", ".drwdot") and re.fullmatch(r"PreviewPNG|Preview|Images/Sheet_\d+", name)
                      and os.environ.get("DEID_IMAGE_OCR", "1") == "1"):
                    from deid.formats.solidworks import _redact_preview
                    kind = "Preview" if name == "Preview" else "PreviewPNG"
                    new = _redact_preview(ctx, kind, data, plan, counts)
                    if new is not None and len(new) < len(data) and kind == "PreviewPNG":
                        new = new + b"\x00" * (len(data) - len(new))   # ignored after IEND
                    if new is not None and len(new) != len(data):
                        counts["previews_unfit"] = counts.get("previews_unfit", 0) + 1
                        new = None
                elif name.endswith("__ZLB") or _SKIP_STREAM.search(name) or len(data) > 64 << 20:
                    continue
                else:
                    new = _edit_text_runs(ctx, data, plan, counts)
                if new is not None and new != data:
                    if len(new) != len(data):
                        continue
                    changes[tuple(path)] = new
        try:
            dead_hit, _ = dead_has_identity(src_path, plan)
        except Exception:
            dead_hit = False
        if not changes and not dead_hit:
            return RewriteResult(status=VERIFIED_UNCHANGED, counts=counts)
        shutil.copyfile(src_path, dst_path)
        try:
            with self._open(dst_path, write=True) as w:
                for path, new in changes.items():
                    w.write_stream("/".join(path), new)
        except Exception as e:
            try:
                os.remove(dst_path)
            except OSError:
                pass
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=[f"in-place stream write failed: {type(e).__name__}"])
        # files whose last sector is truncated grow when olefile writes a full
        # sector: cut back to the original size and prove the streams read back
        osz = os.path.getsize(src_path)
        if os.path.getsize(dst_path) != osz:
            with open(dst_path, "r+b") as fh:
                fh.truncate(osz)
            try:
                with self._open(dst_path) as chk:
                    for path, new in changes.items():
                        if chk.openstream(list(path)).read() != new:
                            raise ValueError("stream mismatch after truncation")
            except Exception as e:
                os.remove(dst_path)
                return RewriteResult(status=COPIED_UNSUPPORTED, notes=[f"in-place write not reproducible: {type(e).__name__}"])
        counts["streams_rewritten"] = len(changes)
        if dead_hit:
            pre = os.path.join(ctx.tmp_dir, "prescrub.cfb")
            shutil.copyfile(dst_path, pre)
            try:
                n = scrub_dead(dst_path, dead_regions(dst_path))
                if _streams(pre) != _streams(dst_path):
                    raise ValueError("a live stream changed")
                counts["dead_regions_zeroed"] = n
            except Exception:
                shutil.copyfile(pre, dst_path)      # keep the stream edits, skip the scrub
                counts["dead_scrub_skipped"] = 1
        return RewriteResult(status=REWRITTEN, counts=counts)

    def verify(self, ctx, src_path, dst_path, plan):
        try:
            if os.path.getsize(src_path) != os.path.getsize(dst_path):
                return VerifyResult(ok=False, notes=["size changed"])
            with self._open(src_path) as a, self._open(dst_path) as b:
                la = a.listdir(streams=True, storages=True)
                lb = b.listdir(streams=True, storages=True)
                if la != lb:
                    return VerifyResult(ok=False, notes=["directory changed"])
                changed = 0
                for path in a.listdir(streams=True, storages=False):
                    n = "/".join(path)
                    if a.get_size(n) != b.get_size(n):
                        return VerifyResult(ok=False, notes=["stream size changed"])
                    da, db = a.openstream(path).read(), b.openstream(path).read()
                    if da == db:
                        continue
                    changed += 1
                    if path[-1].startswith("\x05"):
                        try:
                            sa, _, _ = parse_propset(da)
                            sb, _, _ = parse_propset(db)
                        except Exception:
                            return VerifyResult(ok=False, notes=["property set no longer parses"])
                        if sa != sb:
                            return VerifyResult(ok=False, notes=["property set layout changed"])
                    elif n.endswith("__ZLB"):
                        return VerifyResult(ok=False, notes=["compressed stream changed"])
        except Exception as e:
            return VerifyResult(ok=False, notes=[f"reopen failed: {type(e).__name__}"])
        return VerifyResult(ok=True, fingerprint={"streams_changed": changed})


register(CfbAdapter())
