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
  per the MS-OLEPS layout. VT_LPSTR, VT_LPWSTR and VT_BSTR values are rewritten in place
  (same byte count; the dictionary of property names is left alone).
* Other uncompressed streams: MFC unicode CStrings and UTF-16LE / ASCII
  text runs; only runs that contain a plan identity are rewritten, at the same
  length.  File references (component / drawing paths) follow
  ``plan.map_reference`` when the result keeps the length.
* zlib-compressed streams (SOLIDWORKS ``*__ZLB``), previews and sheet images
  are scanned. Supported SolidWorks XML streams can be edited at the
  original compressed size.

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
from deid.formats import cfb_accounts as accounts, cfb_zlb

try:
    import olefile
except Exception:  # pragma: no cover
    olefile = None

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
VT_LPSTR, VT_LPWSTR, VT_BSTR, VT_I2 = 0x1E, 0x1F, 0x08, 0x02

FMTID_SUMMARY = "F29F85E0-4FF9-1068-AB91-08002B27B3D9"
FMTID_DOCSUMMARY = "D5CDD502-2E9C-101B-9397-08002B2CF9AE"
FMTID_USER = "D5CDD505-2E9C-101B-9397-08002B2CF9AE"
FMTID_INV_DESIGN = "32853F0F-3444-11D1-9E93-0060B03C1CA6"
FMTID_INV_SUMMARY = "3D38DE39-0588-4C14-BB37-18F4D5DD31C7"
FMTID_INV_DOCSUMMARY = "8CF58000-DA66-4AE6-8FF0-7B58406FB049"
FMTID_INV_TRACKING_CONTROL = "D861FB30-3136-11D1-9E92-0060B03C1CA6"
FMTID_EDGE_EXTSUMMARY = "CC024FA2-6EB5-11CE-8AA2-08003601E988"

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
_ACCOUNT_PREVIEW = re.compile(r"Preview|Thumbnail|^Images/|Bitmap|PNG$|\x02OlePres", re.I)
_SW_CFB_EXTS = {".sldprt", ".sldasm", ".slddrw", ".prtdot", ".asmdot", ".drwdot", ".slddrt"}


def _is_propset(path):
    # SolidWorks uses a real property-set stream without the standard name prefix.
    return path[-1].startswith("\x05") or path[-1].casefold() == "isolidworksinformation"


def _is_sw_header(ctx, path):
    return ctx.ext in _SW_CFB_EXTS and len(path) == 1 and path[0].casefold() == "header2"


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
    if not 1 <= nsec <= 16 or 28 + 20 * nsec > len(data):
        raise ValueError("too many sections")
    out, names, secs = [], {}, []
    for i in range(nsec):
        fmtid = _guid(data[28 + 20 * i:44 + 20 * i])
        so = struct.unpack_from("<I", data, 44 + 20 * i)[0]
        if so + 8 > len(data):
            raise ValueError("bad section offset")
        ssize, nprop = struct.unpack_from("<II", data, so)
        section_end = min(so + ssize, len(data))
        table_end = 8 + 8 * nprop
        if nprop > 10000 or so + table_end > section_end:
            raise ValueError("bad property count")
        props = [struct.unpack_from("<II", data, so + 8 + 8 * k) for k in range(nprop)]
        offsets = sorted({po for _, po in props if table_end <= po < section_end - so}
                         | {section_end - so})
        ends = dict(zip(offsets, offsets[1:]))
        codepage = 1252
        for pid, po in props:
            if pid == 1 and po in ends and po + 6 <= ends[po]:
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
            if po not in ends:
                continue
            a, prop_end = so + po, so + ends[po]
            if pid == 0 and a + 4 <= prop_end:
                # dictionary: count, then (pid, len, name)
                try:
                    cnt = struct.unpack_from("<I", data, a)[0]
                    q = a + 4
                    for _ in range(min(cnt, 10000)):
                        if q + 8 > prop_end:
                            break
                        dpid, ln = struct.unpack_from("<II", data, q)
                        q += 8
                        width = 2 if enc == "utf-16-le" else 1
                        if q + width * ln > prop_end:
                            break
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
            if a + 8 > prop_end:
                continue
            vt = struct.unpack_from("<I", data, a)[0] & 0xFFFF
            if vt not in (VT_LPSTR, VT_LPWSTR, VT_BSTR):
                continue
            cnt = struct.unpack_from("<I", data, a + 4)[0]
            ps = PropString()
            ps.pid, ps.vt, ps.fmtid = pid, vt, fmtid
            ps.off = a + 8
            # UnicodeString counts UTF-16 code units. CodePageString counts bytes,
            # including for code page 1200 and VT_BSTR (MS-OLEPS sections 2.5/2.7).
            nb = 2 * cnt if vt == VT_LPWSTR else cnt
            ps.enc = "utf-16-le" if vt == VT_LPWSTR else enc
            if cnt > 1_000_000 or ps.off + nb > prop_end or (ps.enc == "utf-16-le" and nb % 2):
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


def _property_string_layout(strings):
    return [(p.fmtid, p.pid, p.vt, p.off, p.nbytes, p.enc) for p in strings]


def _outside_strings_equal(before, after, strings):
    # Comparing the bytes outside values also covers VT_CF thumbnails and
    # uninterpreted vendor properties in these sets.
    end = 0
    for p in sorted(strings, key=lambda p: p.off):
        if before[end:p.off] != after[end:p.off]:
            return False
        end = max(end, p.off + p.nbytes)
    return before[end:] == after[end:]


def _prop_kind(ps, names):
    if ps.fmtid == FMTID_SUMMARY:
        return SUMMARY_KIND.get(ps.pid, "free_text"), f"summary.{ps.pid}"
    if ps.fmtid == FMTID_INV_SUMMARY and ps.pid == 4:
        return "person", "inventor.summary.author"
    if ps.fmtid in (FMTID_DOCSUMMARY, FMTID_INV_DOCSUMMARY):
        return DOCSUMMARY_KIND.get(ps.pid, "free_text"), f"docsummary.{ps.pid}"
    if ps.fmtid == FMTID_INV_TRACKING_CONTROL and ps.pid == 16:
        return "saved_account", "inventor.tracking_control.last_saved_by"
    if ps.fmtid == FMTID_EDGE_EXTSUMMARY and ps.pid == 8:
        return "saved_account", "solidedge.extended_summary.username"
    if ps.fmtid == FMTID_INV_DESIGN:
        return INV_DESIGN_KIND.get(ps.pid, "free_text"), f"inventor.design.{ps.pid}"
    nm = names.get((ps.fmtid, ps.pid))
    if nm:
        if accounts.is_save_label(nm):
            return "saved_account", "user.last_saved_by"
        return nu.prop_kind(nm), f"user.{nm[:40]}"
    return "free_text", f"{ps.fmtid[:8]}.{ps.pid}"


def _fit_encoded(plan, value: str, enc: str, nbytes: int, kind=None, replacement=None) -> Optional[bytes]:
    """New encoded bytes of exactly nbytes for value, or None if unchanged."""
    new = nu.sub_value(plan, value, kind) if replacement is None else replacement
    if new == value:
        return None
    try:
        b = new.encode(enc)
    except UnicodeEncodeError:
        if replacement is not None:
            return None
        b = new.encode(enc, "replace")
    if len(b) == nbytes:
        return b
    if replacement is not None:
        return None
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


def _edit_text_runs(ctx, data: bytes, plan, counts, scan_only=False, obs=None, refs=None, field="",
                    saving_accounts=None, exact_cstrings=False):
    """Same-length edits of CStrings / UTF-16 / ASCII runs in a binary stream."""
    buf = None
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
                ns = s
            if ns != s:
                counts["refs_rewritten"] = counts.get("refs_rewritten", 0) + 1
            if saving_accounts:
                ns = saving_accounts.profiles(ns)
        else:
            ns = plan.replace_text(s)
            if saving_accounts:
                ns = saving_accounts.text(ns)
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
        if saving_accounts:
            ns = saving_accounts.profiles(ns) if CAD_REF.search(s) else saving_accounts.text(ns)
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
    if saving_accounts:
        # Validated CStrings cover non-Latin and short text that printable-run
        # heuristics miss. Compose with prior text edits at the original offsets.
        for start, nchars, before in nu.iter_cstrings_u16(data):
            end = start + 2 * nchars
            current = bytes(buf[start:end]).decode("utf-16-le", "replace") if buf is not None else before
            if CAD_REF.search(before):
                after = saving_accounts.profiles(current)
            else:
                original_alias = saving_accounts.exact(before) if exact_cstrings else before
                after = (original_alias if original_alias != before
                         else saving_accounts.text(current, exact=exact_cstrings))
            if after == current:
                continue
            encoded = after.encode("utf-16-le")
            if len(encoded) != end - start:
                continue
            if buf is None:
                buf = bytearray(data)
            buf[start:end] = encoded
            counts["account_cstrings"] = counts.get("account_cstrings", 0) + 1
    return bytes(buf) if buf is not None else None


def _zlb_payload(data: bytes) -> Optional[bytes]:
    offsets = (24, 0, 4, 8) if data.startswith(cfb_zlb.MAGIC) else (0, 4, 8)
    for off, window in [(off, zlib.MAX_WBITS) for off in offsets] + [(0, -zlib.MAX_WBITS)]:
        try:
            decoder = zlib.decompressobj(window)
            output = decoder.decompress(memoryview(data)[off:], 64 << 20)
            if decoder.eof:
                return output
        except zlib.error:
            continue
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


def dead_has_identity(path, plan, saving_accounts=None):
    regions = dead_regions(path)
    blob = b"\x01".join(_read_regions(path, regions))
    if not blob.strip(b"\x00\x01"):
        return False, regions
    if saving_accounts and saving_accounts.appears_in_slack(blob):
        return True, regions
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
    version = "4"
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
                    size = ole.get_size(path)
                    data = ole.openstream(path).read()
                except Exception:
                    notes.append("stream unreadable")
                    continue
                entries.append([name.replace("\x05", "#"), size])
                field = re.sub(r"\d+", "N", name.replace("\x05", "#"))[:60]
                if _is_propset(path):
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
                if _is_sw_header(ctx, path):
                    account = accounts.header_account(data)
                    if account and accounts.is_account(account[2]):
                        nu.add_obs(obs, "saved_account", "cfb.sw.header2.last_saved_by",
                                   account[2], "Header2 account CString", 0.85)
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
            paths = ole.listdir(streams=True, storages=False)
            prop_sets = {}
            header = None
            originals = []
            for path in paths:
                if not (_is_propset(path) or _is_sw_header(ctx, path)):
                    continue
                try:
                    data = ole.openstream(path).read()
                    if _is_propset(path):
                        _, props, names = parse_propset(data)
                        prop_sets[tuple(path)] = props, names
                        originals.extend(p.value for p in props if _prop_kind(p, names)[0] == "saved_account")
                    else:
                        header = accounts.header_account(data)
                        if header:
                            originals.append(header[2])
                except (ValueError, OSError, IndexError, struct.error):
                    continue
            saving_accounts = accounts.AccountAliases(plan, originals)
            for path in paths:
                name = "/".join(path)
                try:
                    data = ole.openstream(path).read()
                except Exception:
                    continue
                new = None
                if _is_propset(path):
                    parsed = prop_sets.get(tuple(path))
                    if parsed is None:
                        continue
                    strs, names = parsed
                    buf = None
                    for ps in strs:
                        k, _ = _prop_kind(ps, names)
                        nb = None
                        if saving_accounts and k not in (None, "software", "part_name", "part_number",
                                                         "document_number"):
                            alias = saving_accounts.exact(ps.value)
                            if alias != ps.value:
                                nb = _fit_encoded(plan, ps.value, ps.enc, ps.nbytes, replacement=alias)
                        if nb is not None:
                            if k == "saved_account":
                                counts["saving_accounts"] = counts.get("saving_accounts", 0) + 1
                        elif CAD_REF.search(ps.value) and ("\\" in ps.value or "/" in ps.value):
                            ns = nu.map_ref_same_len(plan, ctx, ps.value)
                            nb = ns.encode(ps.enc, "replace") if ns not in (None, ps.value) else None
                            if nb is not None and len(nb) != ps.nbytes:
                                nb = None
                            if nb is None:
                                nb = _fit_encoded(plan, ps.value, ps.enc, ps.nbytes, None)
                        else:
                            nb = _fit_encoded(plan, ps.value, ps.enc, ps.nbytes, k)
                        if saving_accounts and ("\\" in ps.value or "/" in ps.value):
                            current = nb.decode(ps.enc) if nb is not None else ps.value
                            profile = saving_accounts.profiles(current)
                            if profile != current:
                                fit = _fit_encoded(plan, ps.value, ps.enc, ps.nbytes, replacement=profile)
                                if fit is not None:
                                    nb = fit
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
                elif cfb_zlb.is_xml_stream(path) and not _ACCOUNT_PREVIEW.search(name):
                    if saving_accounts:
                        new = cfb_zlb.rewrite(data, saving_accounts, CAD_REF)
                        if new is not None:
                            counts["account_zlb_streams"] = counts.get("account_zlb_streams", 0) + 1
                elif name.endswith("__ZLB") or _SKIP_STREAM.search(name) or len(data) > 64 << 20:
                    continue
                else:
                    editable_accounts = saving_accounts if not _ACCOUNT_PREVIEW.search(name) else None
                    exact_cstrings = ctx.ext in _SW_CFB_EXTS and path[-1].casefold() in {"header2", "biography"}
                    new = _edit_text_runs(ctx, data, plan, counts, saving_accounts=editable_accounts,
                                          exact_cstrings=exact_cstrings)
                    if header and _is_sw_header(ctx, path):
                        start, nchars, val = header
                        alias = saving_accounts.exact(val)
                        encoded = alias.encode("utf-16-le")
                        if alias != val and len(encoded) == 2 * nchars:
                            work = new if new is not None else data
                            new = work[:start] + encoded + work[start + len(encoded):]
                            counts["saving_accounts"] = counts.get("saving_accounts", 0) + 1
                if new is not None and new != data:
                    if len(new) != len(data):
                        continue
                    changes[tuple(path)] = new
        try:
            dead_hit, _ = dead_has_identity(src_path, plan, saving_accounts)
        except Exception:
            dead_hit = False
        if not changes and not dead_hit:
            return RewriteResult(status=VERIFIED_UNCHANGED, counts=counts)
        shutil.copyfile(src_path, dst_path)
        try:
            with self._open(dst_path, write=True) as w:
                for path, new in changes.items():
                    w.write_stream(list(path), new)
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
                    if a.get_size(path) != b.get_size(path):
                        return VerifyResult(ok=False, notes=["stream size changed"])
                    da, db = a.openstream(path).read(), b.openstream(path).read()
                    if da == db:
                        continue
                    changed += 1
                    if _is_propset(path):
                        try:
                            sa, pa, na = parse_propset(da)
                            sb, pb, nb = parse_propset(db)
                        except Exception:
                            return VerifyResult(ok=False, notes=["property set no longer parses"])
                        if (sa != sb or na != nb
                                or _property_string_layout(pa) != _property_string_layout(pb)):
                            return VerifyResult(ok=False, notes=["property set layout changed"])
                        if not _outside_strings_equal(da, db, pa):
                            return VerifyResult(ok=False, notes=["non-string property bytes changed"])
                    elif n.endswith("__ZLB"):
                        pa, pb = cfb_zlb.payload(da), cfb_zlb.payload(db)
                        if (not cfb_zlb.is_xml_stream(path) or _ACCOUNT_PREVIEW.search(n)
                                or da[:24] != db[:24] or da[-8:] != db[-8:]
                                or pa is None or pb is None or len(pa) != len(pb)):
                            return VerifyResult(ok=False, notes=["compressed stream layout changed"])
        except Exception as e:
            return VerifyResult(ok=False, notes=[f"reopen failed: {type(e).__name__}"])
        return VerifyResult(ok=True, fingerprint={"streams_changed": changed})


register(CfbAdapter())
