"""Modern (2015+) SOLIDWORKS documents: .sldprt .sldasm .slddrw (+ templates
.prtdot .asmdot .drwdot, .sldlfp, .slddrt) in the non-CFB chunk container.

Container (after the Apache-2.0 swformat project's documented findings, own
implementation): a flat sequence of chunks, each ``val_a(4) | marker 14 00 06
00 08 00 | type .. | f1 @0x0E | csz @0x12 | usz @0x16 | nsz @0x1A | name
(ROL-obfuscated with key data[7]) @0x1E | raw DEFLATE payload (csz)``,
followed by a trailing directory (TOC) whose records repeat each stream's
csz/usz and offset.  SOLIDWORKS locates streams through the TOC, so offsets
must never move: every edited stream is re-deflated and zero-padded back to its
original compressed span (span preservation, SW-verified by swformat); only
``usz`` changes, in the chunk header and in the TOC record.

Edits
-----
* XML streams (docProps/custom.xml, core.xml, app.xml, Config-N-Properties.xml,
  ISolidWorksInformation.xml, swXmlContents/*, [Content_Types] ...): every text
  node / attribute value through the plan; any-length change allowed (the
  stream is re-deflated and must still fit its span).
* Binary streams (CArchive): MFC unicode CStrings only, same character count
  (plan alias of equal length, else ``plan.replace_bytes(length_preserving)``).
  Component / drawing references are rewritten with ``plan.map_reference``
  when the result keeps the same length, otherwise a constraint "keep the
  referenced file's basename" is recorded.
* Preview images are left as is (part/assembly previews show geometry only).

verify(): output re-parses to the same chunk list (names, offsets, csz), each
edited chunk inflates to its declared usz, TOC usz matches, file size equal,
XML streams still parse.
"""
from __future__ import annotations

import os
import re
import struct
import zlib
from typing import Optional

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult, Reference,
                      REWRITTEN, VERIFIED_UNCHANGED, COPIED_UNSUPPORTED, SOURCE_EXCEPTION)
from deid.formats import _nativeutil as nu

MARKER = b"\x14\x00\x06\x00\x08\x00"
_OFF_F1, _OFF_CSZ, _OFF_USZ, _OFF_NSZ, _OFF_NAME = 0x0E, 0x12, 0x16, 0x1A, 0x1E
_TOC_NAME = 0x2C
SW_EXTS = (".sldprt", ".sldasm", ".slddrw", ".prtdot", ".asmdot", ".drwdot", ".sldlfp", ".slddrt", ".sldblk")
SW_REF_RE = re.compile(r"\.(sldprt|sldasm|slddrw|sldlfp|slddrt|prtdot|asmdot|drwdot|sldblk)$", re.I)


def _rol(b: int, k: int) -> int:
    k &= 7
    return ((b << k) | (b >> (8 - k))) & 0xFF if k else b


def _rol_name(raw: bytes, key: int) -> str:
    return bytes(_rol(b, key) for b in raw).decode("utf-8", "replace")


def _printable(s: str) -> bool:
    return bool(s) and all(" " <= c <= "~" for c in s)


class Chunk:
    __slots__ = ("si", "data_off", "csz", "usz", "name", "f1")

    def __init__(self, si, data_off, csz, usz, name, f1):
        self.si, self.data_off, self.csz, self.usz, self.name, self.f1 = si, data_off, csz, usz, name, f1


def parse_chunks(data: bytes):
    """Return (chunks, toc_records) ; toc_records: list of (si, name)."""
    n = len(data)
    if n < 64:
        return [], []
    key = data[7]
    chunks = []
    pos = 0
    while True:
        m = data.find(MARKER, pos)
        if m < 0:
            break
        if m < 4:
            pos = m + 1
            continue
        si = m - 4
        if si + _OFF_NAME > n:
            break
        f1, csz, usz, nsz = struct.unpack_from("<IIII", data, si + _OFF_F1)
        if nsz == 0 or nsz > 512 or csz > (1 << 31):
            pos = m + 1
            continue
        ne = si + _OFF_NAME + nsz
        if ne > n:
            pos = m + 1
            continue
        name = _rol_name(data[si + _OFF_NAME:ne], key)
        if not _printable(name) or not (f1 >= 65536 and csz > 0):
            pos = m + len(MARKER)
            continue
        if ne + csz > n:
            pos = m + 1
            continue
        chunks.append(Chunk(si, ne, csz, usz, name, f1))
        pos = ne + csz
    # TOC records: markers outside chunk payloads with name at +0x2C
    toc = []
    covered = [(c.si, c.data_off + c.csz) for c in chunks]
    ci = 0
    pos = 0
    while True:
        m = data.find(MARKER, pos)
        if m < 0:
            break
        pos = m + 1
        si = m - 4
        if si < 0 or si + _TOC_NAME > n:
            continue
        while ci < len(covered) and covered[ci][1] <= si:
            ci += 1
        if ci < len(covered) and covered[ci][0] <= si < covered[ci][1]:
            pos = covered[ci][1]
            continue
        nsz = struct.unpack_from("<I", data, si + _OFF_NSZ)[0]
        if not (0 < nsz < 512) or si + _TOC_NAME + nsz > n:
            continue
        name = _rol_name(data[si + _TOC_NAME:si + _TOC_NAME + nsz], key)
        if _printable(name):
            toc.append((si, name))
    return chunks, toc


def _inflate(data: bytes, c: Chunk) -> Optional[bytes]:
    try:
        d = zlib.decompressobj(-15)
        out = d.decompress(data[c.data_off:c.data_off + c.csz], max(c.usz, 1) + 1024)
        return out
    except zlib.error:
        return None


def _deflate_fit(payload: bytes, limit: int) -> Optional[bytes]:
    """Raw-deflate ``payload`` into at most ``limit`` bytes (zero padded to
    exactly ``limit``), trying several encoder settings; None if impossible."""
    best = None
    for level, mem, strat in ((6, 8, zlib.Z_DEFAULT_STRATEGY), (9, 9, zlib.Z_DEFAULT_STRATEGY),
                              (9, 9, zlib.Z_FIXED), (9, 9, zlib.Z_FILTERED), (9, 8, zlib.Z_FIXED),
                              (1, 9, zlib.Z_DEFAULT_STRATEGY), (9, 9, zlib.Z_HUFFMAN_ONLY)):
        co = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strat)
        comp = co.compress(payload) + co.flush()
        if best is None or len(comp) < len(best):
            best = comp
        if len(best) <= limit:
            break
    if best is None or len(best) > limit:
        return None
    return best + b"\x00" * (limit - len(best))


def _xml_kind(u: bytes) -> Optional[str]:
    h = u[:64].lstrip(b"\xef\xbb\xbf \r\n\t")
    if h.startswith(b"<?xml") or h.startswith(b"<"):
        try:
            u.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            return None
    if u[:2] in (b"\xff\xfe",) or u[:4] == b"<\x00?\x00":
        return "utf-16-le"
    return None


def _decode_xml(u: bytes, enc: str) -> str:
    if enc == "utf-16-le":
        s = u.decode("utf-16-le", "strict")
        return s
    return u.decode("utf-8")


def _encode_xml(s: str, enc: str, orig: bytes) -> bytes:
    if enc == "utf-16-le":
        return s.encode("utf-16-le")
    return s.encode("utf-8")


def _xml_parses(b: bytes) -> bool:
    try:
        from lxml import etree
        etree.fromstring(b, etree.XMLParser(resolve_entities=False, huge_tree=True, recover=False))
        return True
    except Exception:
        return False


_PROP_RE = re.compile(r'<property name="([^"]*)"[^>]*>\s*<vt:(\w+)>([^<]*)</vt:\2>')
_CORE_RE = re.compile(r"<(dc:creator|cp:lastModifiedBy|dc:title|dc:subject|dc:description|cp:keywords|"
                      r"cp:category|Company|Manager|Author|LastAuthor)>([^<]+)</\1>")


def _stream_base(name: str) -> str:
    return re.sub(r"\d+", "N", name)


class SolidWorksAdapter(Adapter):
    name = "solidworks"
    version = "1"
    priority = 20
    extensions = SW_EXTS
    path_independent = False   # references depend on the path plan
    timeout_s = 900.0

    def matches(self, ctx, head):
        if ctx.ext not in SW_EXTS:
            return False
        if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or head[:4] == b"PK\x03\x04":
            return False
        return MARKER in head[:4096]

    # -- scan ------------------------------------------------------------
    def scan(self, ctx, src_path):
        with open(src_path, "rb") as f:
            data = f.read()
        chunks, toc = parse_chunks(data)
        if not chunks:
            return ScanResult(status="unsupported", notes=["no chunk structure"])
        obs, refs, notes = [], [], []
        bad = 0
        names = []
        for c in chunks:
            names.append(c.name)
            u = _inflate(data, c)
            if u is None:
                bad += 1
                continue
            base = _stream_base(c.name)
            enc = _xml_kind(u) if (c.name.endswith(".xml") or c.name.startswith(("docProps", "swXml"))) else None
            if enc:
                try:
                    t = _decode_xml(u, enc)
                except UnicodeDecodeError:
                    continue
                for m in _PROP_RE.finditer(t):
                    pname, val = nu.xml_unescape(m.group(1)), nu.xml_unescape(m.group(3))
                    if not val.strip() or m.group(2) in ("i2", "i4", "r8", "bool", "filetime", "date"):
                        continue
                    k = nu.prop_kind(pname)
                    if k is None:
                        continue
                    if nu.looks_like_ref(val) or re.match(r"^[A-Za-z]:\\|^\\\\", val):
                        nu.add_obs(obs, "path", f"sw.prop.{pname[:40]}", val, base, 0.6)
                        nu.contact_observations(val, f"sw.prop.{pname[:40]}", obs)
                        continue
                    nu.add_obs(obs, k, f"sw.prop.{pname[:40]}", val, base, 0.7)
                    nu.contact_observations(val, f"sw.prop.{pname[:40]}", obs)
                for m in _CORE_RE.finditer(t):
                    tag, val = m.group(1), nu.xml_unescape(m.group(2))
                    kind = {"dc:creator": "person", "cp:lastModifiedBy": "person", "Author": "person",
                            "LastAuthor": "person", "Manager": "person", "Company": "company"}.get(tag, "free_text")
                    conf = 0.2 if (tag == "Company" and "SolidWorks" in val) else 0.8
                    nu.add_obs(obs, kind, f"sw.{c.name.split('/')[-1]}.{tag}", val, base, conf)
                for m in re.finditer(r"[A-Za-z]:\\[^<>\"|?*\x00-\x1f]{2,260}", t):
                    s = m.group(0)
                    nu.contact_observations(s, f"sw.{base}", obs)
                    if SW_REF_RE.search(s):
                        refs.append(Reference(s, "component", f"sw.{base}"))
                continue
            if c.name.endswith(("Preview", "PreviewPNG")) or "Preview" in c.name.split("/")[-1]:
                continue
            first_in_header = base in ("HeaderN", "Contents/Config-N-ModelHeader")
            for i, (q, ln, s) in enumerate(nu.iter_cstrings_u16(u)):
                if first_in_header and i == 0:
                    nu.add_obs(obs, "account", f"sw.{base}.author", s, "moHeader_c first string", 0.6)
                if SW_REF_RE.search(s):
                    kind = "xref" if ctx.ext in (".slddrw", ".drwdot") else "component"
                    if base.startswith("_MO_VERSION_N/Biography"):
                        kind = "other"
                    refs.append(Reference(s, kind, f"sw.{base}#fixedlen"))
                    nu.contact_observations(s, f"sw.{base}", obs)
                elif "\\" in s or "/" in s:
                    nu.contact_observations(s, f"sw.{base}", obs)
                elif "@" in s or "www." in s or "http" in s:
                    nu.contact_observations(s, f"sw.{base}", obs)
        fp = {"chunks": len(chunks), "toc": len(toc), "bad_chunks": bad, "size": len(data),
              "streams": sorted(set(_stream_base(n) for n in names))[:400]}
        if bad:
            notes.append(f"{bad} chunks do not inflate")
        return ScanResult(observations=nu.dedupe_obs(obs), references=nu.dedupe_refs(refs),
                          fingerprint=fp, notes=notes)

    # -- rewrite ----------------------------------------------------------
    def rewrite(self, ctx, src_path, dst_path, plan):
        with open(src_path, "rb") as f:
            data = f.read()
        chunks, toc = parse_chunks(data)
        if not chunks:
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=["no chunk structure"])
        out = bytearray(data)
        toc_by_name = {}
        for si, nm in toc:
            toc_by_name.setdefault(nm, []).append(si)
        counts = {"xml_values": 0, "cstrings": 0, "refs_rewritten": 0, "refs_kept": 0,
                  "strings_unfit": 0, "streams_rewritten": 0, "streams_overflow": 0}
        notes = []
        for c in chunks:
            u = _inflate(data, c)
            if u is None or len(u) != c.usz:
                continue
            if "Preview" in c.name.split("/")[-1]:
                continue
            enc = _xml_kind(u) if (c.name.endswith(".xml") or c.name.startswith(("docProps", "swXml"))
                                   or u[:5] == b"<?xml") else None
            new = None
            if enc:
                new = self._edit_xml(ctx, u, enc, plan, counts)
            else:
                new = self._edit_binary(ctx, u, plan, counts)
            if new is None or new == u:
                continue
            comp = _deflate_fit(new, c.csz)
            if comp is None and enc:
                # retry with value edits that keep each value's length
                alt = self._edit_xml(ctx, u, enc, plan, {"xml_values": 0}, same_len=True)
                if alt is not None and alt != u:
                    comp = _deflate_fit(alt, c.csz)
                    if comp is not None:
                        new = alt
            if comp is None:
                counts["streams_overflow"] += 1
                continue
            out[c.data_off:c.data_off + c.csz] = comp
            struct.pack_into("<I", out, c.si + _OFF_USZ, len(new))
            for si in toc_by_name.get(c.name, []):
                # only patch records that describe this stream (csz matches)
                if struct.unpack_from("<I", data, si + _OFF_CSZ)[0] == c.csz:
                    struct.pack_into("<I", out, si + _OFF_USZ, len(new))
            counts["streams_rewritten"] += 1
        if counts["streams_overflow"]:
            notes.append(f"{counts['streams_overflow']} streams could not be re-fitted to their span")
        if not counts["streams_rewritten"]:
            return RewriteResult(status=VERIFIED_UNCHANGED, counts=counts, notes=notes)
        with open(dst_path, "wb") as f:
            f.write(out)
        return RewriteResult(status=REWRITTEN, counts=counts, notes=notes)

    def _edit_xml(self, ctx, u, enc, plan, counts, same_len=False):
        try:
            t = _decode_xml(u, enc)
        except UnicodeDecodeError:
            return None

        def vf(v, where):
            if nu.looks_like_ref(v):
                try:
                    nv = plan.map_reference(ctx, v)
                except Exception:
                    nv = v
                if nv == v:
                    nv = plan.replace_text(v)
                if same_len and len(nv) != len(v):
                    nv2 = nu.map_ref_same_len(plan, ctx, v)
                    return nv2 if nv2 is not None else v
                return nv
            nv = nu.sub_value(plan, v)
            if same_len and len(nv) != len(v):
                nv = nu.same_len_text(plan, v)
                if nv is None:
                    try:
                        nv = plan.replace_bytes(v.encode("utf-16-le"), ("utf-16-le",), True).decode("utf-16-le")
                    except Exception:
                        nv = v
                    if len(nv) != len(v):
                        nv = v
                if nv == v and nu.sub_value(plan, v) != v:
                    nv = nu.fit_len(nu.sub_value(plan, v), len(v))
            return nv

        nt, n = nu.rewrite_xml_values(t, vf)
        if not n:
            return None
        nb = _encode_xml(nt, enc, u)
        if _xml_parses(u) and not _xml_parses(nb):
            return None
        counts["xml_values"] += n
        return nb

    def _edit_binary(self, ctx, u, plan, counts):
        buf = None
        for q, ln, s in nu.iter_cstrings_u16(u):
            if SW_REF_RE.search(s) or nu.looks_like_ref(s):
                ns = nu.map_ref_same_len(plan, ctx, s)
                if ns is None:
                    counts["refs_kept"] += 1
                    nu.record_constraint(ctx, s, "binary CString reference; new name has a different length",
                                         "solidworks")
                    continue
                if ns == s:
                    continue
                if buf is None:
                    buf = bytearray(u)
                if nu.patch_u16_same_len(buf, q, ln, ns):
                    counts["refs_rewritten"] += 1
                continue
            ns = nu.sub_value(plan, s)
            if ns == s:
                continue
            if len(ns) != len(s):
                try:
                    nb = plan.replace_bytes(s.encode("utf-16-le"), ("utf-16-le",), True)
                    ns2 = nb.decode("utf-16-le")
                except Exception:
                    ns2 = s
                if len(ns2) != len(s) or ns2 == s:
                    ns2 = nu.fit_len(ns, len(s))
                    counts["strings_truncated"] = counts.get("strings_truncated", 0) + 1
                ns = ns2
            if buf is None:
                buf = bytearray(u)
            if nu.patch_u16_same_len(buf, q, ln, ns):
                counts["cstrings"] += 1
        return bytes(buf) if buf is not None else None

    # -- verify -----------------------------------------------------------
    def verify(self, ctx, src_path, dst_path, plan):
        with open(src_path, "rb") as f:
            a = f.read()
        with open(dst_path, "rb") as f:
            b = f.read()
        if len(a) != len(b):
            return VerifyResult(ok=False, notes=["size changed"])
        ca, ta = parse_chunks(a)
        cb, tb = parse_chunks(b)
        if [(c.si, c.csz, c.name) for c in ca] != [(c.si, c.csz, c.name) for c in cb]:
            return VerifyResult(ok=False, notes=["chunk layout changed"])
        if [x for x in ta] != [x for x in tb]:
            return VerifyResult(ok=False, notes=["toc layout changed"])
        # gaps (bytes outside payloads) may differ only in usz fields
        tocpos = {si for si, _ in tb}
        changed_xml = 0
        for x, y in zip(ca, cb):
            pa = a[x.data_off:x.data_off + x.csz]
            pb = b[y.data_off:y.data_off + y.csz]
            if pa == pb and x.usz == y.usz:
                continue
            ub = _inflate(b, y)
            if ub is None or len(ub) != y.usz:
                return VerifyResult(ok=False, notes=["edited chunk does not inflate to usz"])
            ua = _inflate(a, x)
            if ua is not None and _xml_kind(ua) and _xml_parses(ua):
                if not _xml_parses(ub):
                    return VerifyResult(ok=False, notes=["xml stream no longer parses"])
                changed_xml += 1
            elif ua is not None and len(ua) != len(ub):
                return VerifyResult(ok=False, notes=["binary stream length changed"])
            for si, nm in tb:
                if nm == y.name and struct.unpack_from("<I", b, si + _OFF_CSZ)[0] == y.csz:
                    if struct.unpack_from("<I", b, si + _OFF_USZ)[0] != y.usz:
                        return VerifyResult(ok=False, notes=["toc usz mismatch"])
        # every non-payload byte identical except usz fields of headers/toc
        allowed = set()
        for y in cb:
            allowed.update(range(y.si + _OFF_USZ, y.si + _OFF_USZ + 4))
        for si in tocpos:
            allowed.update(range(si + _OFF_USZ, si + _OFF_USZ + 4))
        import numpy as np
        na = np.frombuffer(a, dtype=np.uint8)
        nb_ = np.frombuffer(b, dtype=np.uint8)
        mask = na != nb_
        for y in cb:
            mask[y.data_off:y.data_off + y.csz] = False
        idx = np.nonzero(mask)[0]
        if len(idx) and not set(int(i) for i in idx[:100000]) <= allowed:
            return VerifyResult(ok=False, notes=["bytes outside payloads changed"])
        return VerifyResult(ok=True, fingerprint={"chunks": len(cb), "xml_changed": changed_xml})


register(SolidWorksAdapter())


class SolidWorksLockAdapter(Adapter):
    """``~$name.SLDPRT`` owner/lock files: the payload is the Windows user
    name of whoever had the document open (NUL terminated)."""
    name = "solidworks_lock"
    version = "1"
    priority = 25
    extensions = SW_EXTS
    path_independent = True

    def matches(self, ctx, head):
        base = ctx.member_name.rsplit("/", 1)[-1]
        return (ctx.ext in SW_EXTS and base.startswith("~$") and 0 < len(head) <= 512
                and MARKER not in head and not head.startswith(b"\xd0\xcf\x11\xe0"))

    @staticmethod
    def _decode(b):
        t = b.split(b"\x00", 1)[0]
        for enc in ("utf-8", "cp1252"):
            try:
                return t.decode(enc), enc
            except UnicodeDecodeError:
                continue
        return None, None

    def scan(self, ctx, src_path):
        b = open(src_path, "rb").read()
        t, enc = self._decode(b)
        obs = []
        if t:
            nu.add_obs(obs, "account", "sw.lockfile.user", t, "SOLIDWORKS lock file owner", 0.9)
        return ScanResult(observations=obs, fingerprint={"size": len(b)})

    def rewrite(self, ctx, src_path, dst_path, plan):
        b = open(src_path, "rb").read()
        t, enc = self._decode(b)
        if not t:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        n = nu.sub_value(plan, t, "account")
        if n == t:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        head = b.split(b"\x00", 1)
        tail = b[len(head[0]):]
        with open(dst_path, "wb") as f:
            f.write(n.encode(enc, "replace") + tail)
        return RewriteResult(status=REWRITTEN, counts={"lock_owner": 1})


register(SolidWorksLockAdapter())
