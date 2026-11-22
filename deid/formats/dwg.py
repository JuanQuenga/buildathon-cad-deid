"""AutoCAD DWG (owner: docs-native).

Implemented from the Open Design Alliance's public "Open Design Specification
for .dwg files" (R2004 container, also used by R2010/R2013/R2018:
AC1018, AC1024, AC1027, AC1032).

* The encrypted file header (0x80) gives the page map and section map; the
  section map lists data sections (AcDb:SummaryInfo, AcDb:Header,
  AcDb:AcDbObjects, AcDb:Preview ...) and their pages.  Data pages have an
  encrypted 32-byte header and an LZ77-compressed payload (R2004 scheme).
* scan(): SummaryInfo fields (title, subject, author, keywords, comments,
  last saved by, revision, hyperlink base, custom properties) and UTF-16 /
  ASCII strings of the decompressed object section (text, MTEXT, attribute
  values, xref and image paths) -> observations and references.
* rewrite(): SummaryInfo strings are replaced at the same length (identity
  aliases are length-preserving), the page is re-compressed and must fit the
  original compressed size (padding after the end-of-stream opcode keeps
  every size field, the page map and the section map unchanged); the data
  and header checksums of that page are recomputed.  Everything else is
  byte-identical.  If it does not fit, the file is copied unchanged.
* Other versions (R13-R2000 AC1012-AC1015, R2007 AC1021) are scanned for
  plain strings only.

verify(): output re-parses, same page/section maps, every page of every
section still decompresses to its declared size, SummaryInfo re-reads.
"""
from __future__ import annotations

import re
import os
import struct
from typing import Optional

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult, Reference,
                      REWRITTEN, VERIFIED_UNCHANGED, COPIED_UNSUPPORTED, SOURCE_EXCEPTION)
from deid.formats import _nativeutil as nu

R2004_FAMILY = (b"AC1018", b"AC1024", b"AC1027", b"AC1032")


class DwgError(Exception):
    pass


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------
def _decrypt_header(raw: bytes) -> bytes:
    out = bytearray(len(raw))
    seed = 1
    for i, b in enumerate(raw):
        seed = (seed * 0x343FD + 0x269EC3) & 0xFFFFFFFF
        out[i] = b ^ ((seed >> 16) & 0xFF)
    return bytes(out)


def page_checksum(seed: int, data: bytes) -> int:
    s1 = seed & 0xFFFF
    s2 = (seed >> 16) & 0xFFFF
    i = 0
    n = len(data)
    while i < n:
        chunk = data[i:i + 0x15B0]
        for b in chunk:
            s1 += b
            s2 += s1
        s1 %= 0xFFF1
        s2 %= 0xFFF1
        i += 0x15B0
    return ((s2 << 16) | (s1 & 0xFFFF)) & 0xFFFFFFFF


def decompress(src: bytes, out_size: int) -> bytes:
    """R2004 LZ77 decompression."""
    dst = bytearray()
    p = 0
    n = len(src)

    def rd():
        nonlocal p
        if p >= n:
            raise DwgError("compressed stream overrun")
        b = src[p]
        p += 1
        return b

    def lit_len():
        b = rd()
        if 0x01 <= b <= 0x0F:
            return b + 3, 0
        if b == 0:
            total = 0x0F
            b = rd()
            while b == 0:
                total += 0xFF
                b = rd()
            return total + b + 3, 0
        return 0, b   # it's an opcode

    def long_off():
        b = rd()
        total = 0
        if b == 0:
            total = 0xFF
            b = rd()
            while b == 0:
                total += 0xFF
                b = rd()
        return total + b

    def two_byte():
        b1 = rd()
        b2 = rd()
        return (b1 >> 2) | (b2 << 6), b1 & 3

    ll, op = lit_len()
    dst += src[p:p + ll]
    p += ll
    while p < n:
        if op == 0:
            op = rd()
        if op >= 0x40:
            cb = ((op & 0xF0) >> 4) - 1
            op2 = rd()
            off = (op2 << 2) | ((op & 0x0C) >> 2)
            if op & 3:
                ll = op & 3
                op = 0
            else:
                ll, op = lit_len()
        elif 0x21 <= op <= 0x3F:
            cb = op - 0x1E
            off, ll = two_byte()
            if ll:
                op = 0
            else:
                ll, op = lit_len()
        elif op == 0x20:
            cb = long_off() + 0x21
            off, ll = two_byte()
            if ll:
                op = 0
            else:
                ll, op = lit_len()
        elif 0x12 <= op <= 0x1F:
            cb = (op & 0x0F) + 2
            off, ll = two_byte()
            off += 0x3FFF
            if ll:
                op = 0
            else:
                ll, op = lit_len()
        elif op == 0x10:
            cb = long_off() + 9
            off, ll = two_byte()
            off += 0x3FFF
            if ll:
                op = 0
            else:
                ll, op = lit_len()
        elif op == 0x11:
            break
        else:
            raise DwgError(f"bad opcode {op:#x}")
        s = len(dst) - off - 1
        if s < 0:
            raise DwgError("back reference before start")
        for k in range(cb):
            dst.append(dst[s + k])
        dst += src[p:p + ll]
        p += ll
        if len(dst) > max(out_size, 0x7400) + 64:
            raise DwgError("decompressed overrun")
    return bytes(dst[:out_size])


def _enc_lit_len_first(n: int) -> bytes:
    """Literal length (>= 4) as read by the decoder's literal-length routine."""
    if n < 4:
        raise ValueError("literal run < 4")
    if n - 3 <= 0x0F:
        return bytes([n - 3])
    rest = n - 3 - 0x0F
    k = (rest - 1) // 0xFF
    return bytes([0] + [0] * k + [rest - 0xFF * k])


def _enc_long(v: int) -> bytes:
    if v <= 0:
        raise ValueError("long value must be >= 1")
    if v <= 0xFF:
        return bytes([v])
    k = (v - 1) // 0xFF
    return bytes([0] * k + [v - 0xFF * k])


def compress(data: bytes) -> bytes:
    """Greedy R2004 LZ77 compressor (hash-chain match finder, offsets < 0x3FFF+0xFFFF)."""
    n = len(data)
    out = bytearray()
    MINM = 3
    head = {}
    i = 0
    lit_start = 0
    first = True
    pending_match = None  # (opcode bytes without literal count) built lazily

    def emit_literals(lits: bytes, after_match_buf: Optional[bytearray]):
        """Literals following a match: up to 3 go into the match's low bits."""
        pass

    tokens = []   # list of ("lit", bytes) / ("match", length, offset)
    while i < n:
        best_len = 0
        best_off = 0
        if i + MINM <= n:
            key = data[i:i + 3]
            cands = head.get(key)
            if cands:
                for j in reversed(cands[-24:]):
                    off = i - j - 1
                    if off > 0x3FFE + 0xFFFF or off < 0:
                        continue
                    L = 0
                    mx = min(n - i, 0x10000)
                    while L < mx and data[j + L] == data[i + L]:
                        L += 1
                    if L > best_len:
                        best_len, best_off = L, off
                        if L >= 64:
                            break
            head.setdefault(key, []).append(i)
        if best_off > 0x3FFE and best_len < 4:
            best_len = 0
        if best_len >= MINM:
            if i > lit_start:
                tokens.append(("lit", data[lit_start:i]))
            tokens.append(("match", best_len, best_off))
            for k in range(i + 1, min(i + best_len, n - 2)):
                head.setdefault(data[k:k + 3], []).append(k)
            i += best_len
            lit_start = i
        else:
            i += 1
    if lit_start < n:
        tokens.append(("lit", data[lit_start:n]))
    # The stream must start with literals of length >= 4 (or a match). Merge.
    if tokens and tokens[0][0] == "lit" and len(tokens[0][1]) < 4:
        acc = bytearray(tokens[0][1])
        rest = tokens[1:]
        while len(acc) < 4 and rest:
            t0 = rest.pop(0)
            if t0[0] == "lit":
                acc += t0[1]
                continue
            L, off = t0[1], t0[2]
            take = min(L, 4 - len(acc))
            s0 = len(acc) - off - 1
            for q in range(take):
                acc.append(acc[s0 + q])
            left = L - take
            if left >= (4 if off > 0x3FFE else 3):
                rest.insert(0, ("match", left, off))
            elif left:
                s0 = len(acc) - off - 1
                for q in range(left):
                    acc.append(acc[s0 + q])
        tokens = [("lit", bytes(acc))] + rest
    # coalesce adjacent literal runs
    merged = []
    for tok in tokens:
        if tok[0] == "lit" and merged and merged[-1][0] == "lit":
            merged[-1] = ("lit", merged[-1][1] + tok[1])
        else:
            merged.append(tok)
    tokens = merged
    # encode
    t = 0
    if tokens and tokens[0][0] == "lit":
        out += _enc_lit_len_first(len(tokens[0][1])) + tokens[0][1]
        t = 1
    while t < len(tokens):
        tok = tokens[t]
        if tok[0] == "lit":
            # literal run not preceded by a match (only possible at start): encode as zero-length
            raise DwgError("internal: unexpected literal")
        L, off = tok[1], tok[2]
        lits = tokens[t + 1][1] if t + 1 < len(tokens) and tokens[t + 1][0] == "lit" else b""
        inline = len(lits) if len(lits) <= 3 else 0
        if off <= 0x3FF and 3 <= L <= 14:
            op = ((L + 1) << 4) | ((off & 3) << 2) | inline
            out += bytes([op, (off >> 2) & 0xFF])
        elif off <= 0x3FFE:
            if 3 <= L <= 0x21:
                out.append(0x1E + L)
            elif L < 3:
                raise DwgError("internal: short match")
            else:
                out.append(0x20)
                out += _enc_long(L - 0x21)
            out += bytes([((off & 0x3F) << 2) | inline, (off >> 6) & 0xFF])
        else:
            o2 = off - 0x3FFF
            if o2 > 0xFFFF:
                raise DwgError("internal: offset")
            if 4 <= L <= 17:
                out.append(0x10 | (L - 2))
            elif L >= 10:
                out.append(0x10)
                out += _enc_long(L - 9)
            else:
                raise DwgError("internal: far match too short")
            out += bytes([((o2 & 0x3F) << 2) | inline, (o2 >> 6) & 0xFF])
        if lits:
            if inline:
                out += lits
            else:
                out += _enc_lit_len_first(len(lits)) + lits
            t += 2
        else:
            t += 1
    out.append(0x11)
    return bytes(out)


# ---------------------------------------------------------------------------
# container
# ---------------------------------------------------------------------------
class Page:
    __slots__ = ("number", "addr", "size")


class SectionPage:
    __slots__ = ("number", "data_size", "start", "addr", "page_size", "hdr", "comp_off")


class Section:
    __slots__ = ("name", "size", "page_count", "max_size", "compressed", "sid", "encrypted", "pages")


class Dwg:
    def __init__(self, data: bytes):
        self.data = data
        self.version = data[:6]
        if self.version not in R2004_FAMILY:
            raise DwgError("unsupported version")
        hdr = _decrypt_header(data[0x80:0x80 + 0x6C])
        if hdr[:11] != b"AcFssFcAJMB":
            raise DwgError("bad header id")
        self.hdr = hdr
        self.page_map_addr = struct.unpack_from("<Q", hdr, 0x54)[0] + 0x100
        self.section_map_id = struct.unpack_from("<I", hdr, 0x5C)[0]
        pm = self._system_page(self.page_map_addr, 0x41630E3B)
        self.pages = {}
        addr = 0x100
        p = 0
        while p + 8 <= len(pm):
            num, size = struct.unpack_from("<ii", pm, p)
            p += 8
            if num < 0:
                p += 16
            else:
                pg = Page()
                pg.number, pg.addr, pg.size = num, addr, size
                self.pages[num] = pg
            addr += size
        if self.section_map_id not in self.pages:
            raise DwgError("section map page missing")
        sm = self._system_page(self.pages[self.section_map_id].addr, 0x4163003B)
        nsec = struct.unpack_from("<i", sm, 0)[0]
        p = 20
        self.sections = {}
        for _ in range(max(0, nsec)):
            if p + 96 > len(sm):
                break
            s = Section()
            s.size, s.page_count, s.max_size, _unk, s.compressed, s.sid, s.encrypted = struct.unpack_from("<QiIiiii", sm, p)
            s.name = sm[p + 32:p + 96].split(b"\x00", 1)[0].decode("latin-1")
            p += 96
            s.pages = []
            for _ in range(max(0, s.page_count)):
                sp = SectionPage()
                sp.number, sp.data_size, sp.start = struct.unpack_from("<iiQ", sm, p)
                p += 16
                pg = self.pages.get(sp.number)
                sp.addr = pg.addr if pg else None
                s.pages.append(sp)
            if s.name:
                self.sections[s.name] = s

    def _system_page(self, addr, expect):
        d = self.data
        typ, dsize, csize, ctype, chk = struct.unpack_from("<IIIII", d, addr)
        if typ != expect:
            raise DwgError("bad system page type")
        comp = d[addr + 20:addr + 20 + csize]
        return decompress(comp, dsize) if ctype == 2 else comp

    @staticmethod
    def page_header(data, addr):
        mask = 0x4164536B ^ (addr & 0xFFFFFFFF)
        raw = struct.unpack_from("<8I", data, addr)
        return [v ^ mask for v in raw]

    def read_section(self, name) -> Optional[bytes]:
        s = self.sections.get(name)
        if s is None:
            return None
        if s.encrypted == 1:
            raise DwgError("encrypted section")
        buf = bytearray(s.size)
        for sp in s.pages:
            if sp.addr is None:
                raise DwgError("missing page")
            h = self.page_header(self.data, sp.addr)
            if h[0] != 0x4163043B:
                raise DwgError("bad data page")
            csize = h[2]
            usize = max(0, min(s.max_size, s.size - sp.start))
            comp = self.data[sp.addr + 32:sp.addr + 32 + csize]
            raw = decompress(comp, usize) if s.compressed == 2 else comp[:usize]
            end = min(sp.start + len(raw), len(buf))
            buf[sp.start:end] = raw[:end - sp.start]
        return bytes(buf)


# ---------------------------------------------------------------------------
# SummaryInfo
# ---------------------------------------------------------------------------
SUMMARY_FIELDS = ["title", "subject", "author", "keywords", "comments", "lastsavedby", "revision", "hyperlinkbase"]
SUMMARY_KIND = {"title": "part_name", "subject": "free_text", "author": "person", "keywords": "free_text",
                "comments": "free_text", "lastsavedby": "person", "revision": None, "hyperlinkbase": "url"}


def parse_summary(buf: bytes, wide: bool):
    """[(field, offset_of_chars, nbytes, text)] ; custom properties as ("custom:<name>", ...)."""
    out = []
    p = 0

    def rd_str():
        nonlocal p
        ln = struct.unpack_from("<H", buf, p)[0]
        p += 2
        nb = ln * 2 if wide else ln
        off = p
        raw = buf[p:p + nb]
        p += nb
        if wide:
            t = raw.decode("utf-16-le", "replace")
            # strip terminating NUL (counted in ln)
            ch = 2
        else:
            t = raw.decode("cp1252", "replace")
            ch = 1
        tl = len(raw)
        while tl >= ch and raw[tl - ch:tl] == b"\x00" * ch:
            tl -= ch
        return off, tl, t.rstrip("\x00")

    for f in SUMMARY_FIELDS:
        off, nb, t = rd_str()
        out.append((f, off, nb, t))
    p += 8 + 8 + 8  # total editing time, create date, modified date (julian)
    if p + 2 <= len(buf):
        cnt = struct.unpack_from("<H", buf, p)[0]
        p += 2
        for _ in range(min(cnt, 1000)):
            ko, kn, k = rd_str()
            vo, vn, v = rd_str()
            out.append((f"custom:{k}", vo, vn, v))
    return out


_U16RUN = re.compile(rb"(?:[\x20-\x7e\xa0-\xff]\x00){4,}")
_ASCRUN = re.compile(rb"[\x20-\x7e]{6,}")
_DWG_REF = re.compile(r"[^\x00|<>\"*?]{1,240}\.(dwg|dxf|pdf|png|jpg|jpeg|tif|bmp|ole|xls|xlsx|dwf|ctb|stb|shx|ttf)\b", re.I)


class DwgAdapter(Adapter):
    name = "dwg"
    version = "1a"
    priority = 20
    magics = ("dwg",)
    extensions = (".dwg",)
    path_independent = True
    timeout_s = 600.0

    def matches(self, ctx, head):
        return head[:4] == b"AC10" and head[4:6].isdigit()

    def scan(self, ctx, src_path):
        data = open(src_path, "rb").read()
        obs, refs, notes = [], [], []
        fp = {"version": data[:6].decode("latin-1", "replace"), "size": len(data)}
        texts = []
        try:
            d = Dwg(data)
            fp["sections"] = sorted(d.sections)
            si = d.read_section("AcDb:SummaryInfo")
            if si:
                wide = d.version >= b"AC1021"
                for f, off, nb, t in parse_summary(si, wide):
                    if not t.strip():
                        continue
                    if f.startswith("custom:"):
                        k = nu.prop_kind(f[7:])
                        if k:
                            nu.add_obs(obs, k, f"dwg.summary.{f[:48]}", t, "custom property", 0.6)
                    else:
                        k = SUMMARY_KIND.get(f)
                        if k:
                            nu.add_obs(obs, k, f"dwg.summary.{f}", t, "SummaryInfo", 0.8 if k == "person" else 0.5)
                    nu.contact_observations(t, f"dwg.summary.{f[:40]}", obs)
            objs = d.read_section("AcDb:AcDbObjects")
            if objs:
                texts.append(objs)
        except DwgError as e:
            notes.append(f"container: {e}")
            texts.append(data)
        except Exception as e:
            notes.append(f"container: {type(e).__name__}")
            texts.append(data)
        seen = set()
        for blob in texts:
            for rx, enc in ((_U16RUN, "utf-16-le"), (_ASCRUN, "latin-1")):
                for m in rx.finditer(blob):
                    s = m.group(0).decode(enc, "replace").strip()
                    if s in seen or len(seen) > 20000:
                        continue
                    seen.add(s)
                    nu.contact_observations(s, "dwg.text", obs, limit=50)
                    for r in _DWG_REF.finditer(s):
                        refs.append(Reference(r.group(0).strip(), "xref", "dwg.objects#fixedlen"))
                    if enc == "utf-16-le" and 4 <= len(s) <= 200:
                        nu.add_obs(obs, "free_text", "dwg.text", s, "drawing text", 0.2)
        return ScanResult(observations=nu.dedupe_obs(obs, 3000), references=nu.dedupe_refs(refs, 2000),
                          fingerprint=fp, notes=notes)

    def rewrite(self, ctx, src_path, dst_path, plan):
        data = open(src_path, "rb").read()
        try:
            d = Dwg(data)
        except DwgError as e:
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=[f"dwg container: {e}"])
        out = bytearray(data)
        counts = {}
        notes = []
        wide = d.version >= b"AC1021"
        s = d.sections.get("AcDb:SummaryInfo")
        if s is not None and len(s.pages) == 1 and s.encrypted != 1 and s.pages[0].addr is not None:
            r = self._rewrite_summary(d, data, out, s, plan, wide)
            if isinstance(r, str):
                return RewriteResult(status=COPIED_UNSUPPORTED, notes=[r])
            if r:
                counts["summary_fields"] = r
        elif os.environ.get("DEID_DWG_APPINFO", "1") != "1":
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=["summary info layout not supported"])
        # application history / info: saving accounts and user folders (uncompressed single-page sections)
        if os.environ.get("DEID_DWG_APPINFO", "1") == "1":
            for name in ("AcDb:AppInfoHistory", "AcDb:AppInfo"):
                s2 = d.sections.get(name)
                if s2 is None or len(s2.pages) != 1 or s2.encrypted == 1 or s2.compressed == 2 or s2.pages[0].addr is None:
                    continue
                sp2 = s2.pages[0]
                h2 = d.page_header(data, sp2.addr)
                csize2 = h2[2]
                usize2 = min(s2.max_size, s2.size, csize2)
                comp2 = data[sp2.addr + 32:sp2.addr + 32 + csize2]
                raw2 = bytearray(comp2[:usize2])
                n2 = _rewrite_app_strings(raw2, plan, wide)
                if not n2:
                    continue
                saved = bytes(out[sp2.addr:sp2.addr + 32 + csize2])
                out[sp2.addr + 32:sp2.addr + 32 + csize2] = bytes(raw2) + comp2[usize2:]
                if self._fix_checksums(data, out, sp2.addr, csize2) is None:
                    out[sp2.addr:sp2.addr + 32 + csize2] = saved
                    notes.append(f"{name}: page checksum scheme not recognised")
                    continue
                counts["appinfo_strings"] = counts.get("appinfo_strings", 0) + n2
        if not counts:
            if s is None or len(s.pages) != 1:
                return RewriteResult(status=COPIED_UNSUPPORTED, notes=notes or ["summary info layout not supported"])
            return RewriteResult(status=VERIFIED_UNCHANGED, notes=notes)
        open(dst_path, "wb").write(out)
        return RewriteResult(status=REWRITTEN, counts=counts, notes=notes)

    def _rewrite_summary(self, d, data, out, s, plan, wide):
        sp = s.pages[0]
        h = d.page_header(data, sp.addr)
        csize = h[2]
        usize = min(s.max_size, s.size)
        comp = data[sp.addr + 32:sp.addr + 32 + csize]
        raw = bytearray(decompress(comp, usize) if s.compressed == 2 else comp[:usize])
        n = 0
        enc = "utf-16-le" if wide else "cp1252"
        for f, off, nb, t in parse_summary(bytes(raw), wide):
            if not t.strip():
                continue
            kind = SUMMARY_KIND.get(f, "free_text") if not f.startswith("custom:") else nu.prop_kind(f[7:])
            if kind is None:
                continue
            nt = nu.sub_value(plan, t, kind)
            if nt == t:
                continue
            b = nt.encode(enc, "replace")
            if len(b) != nb:
                b = nu.fit_len(nt, len(t)).encode(enc, "replace")
                if len(b) != nb:
                    continue
            raw[off:off + nb] = b
            n += 1
        if not n:
            return 0
        if s.compressed == 2:
            newc = compress(bytes(raw))
            if decompress(newc, usize) != bytes(raw):
                return "recompression self-check failed"
            if len(newc) > csize:
                return "summary info does not fit its page"
            newc = newc + b"\x00" * (csize - len(newc))
        else:
            newc = bytes(raw) + comp[usize:]
        out[sp.addr + 32:sp.addr + 32 + csize] = newc
        if self._fix_checksums(data, out, sp.addr, csize) is None:
            return "page checksum scheme not recognised"
        return n

    @staticmethod
    def _hdr_bytes(vals, addr):
        mask = 0x4164536B ^ (addr & 0xFFFFFFFF)
        return struct.pack("<8I", *[v ^ mask for v in vals])

    def _fix_checksums(self, old, out, addr, csize):
        h = Dwg.page_header(old, addr)
        comp_old = old[addr + 32:addr + 32 + csize]
        # data checksum candidates
        dchk = page_checksum(0, comp_old)
        if dchk != h[7]:
            return None
        hz = list(h)
        hz[6] = 0
        hz[7] = 0
        # header checksum: seed = data checksum, over the plain header with checksums zeroed
        plain = struct.pack("<8I", *hz)
        variants = {"plainzero": page_checksum(dchk, plain)}
        hz2 = list(h)
        hz2[6] = 0
        variants["plain_dchk"] = page_checksum(dchk, struct.pack("<8I", *hz2))
        scheme = next((k for k, v in variants.items() if v == h[6]), None)
        if scheme is None:
            return None
        newcomp = bytes(out[addr + 32:addr + 32 + csize])
        nd = page_checksum(0, newcomp)
        nh = list(h)
        nh[7] = nd
        if scheme == "plainzero":
            z = list(h)
            z[6] = 0
            z[7] = 0
            nh[6] = page_checksum(nd, struct.pack("<8I", *z))
        else:
            z = list(h)
            z[6] = 0
            z[7] = nd
            nh[6] = page_checksum(nd, struct.pack("<8I", *z))
        out[addr:addr + 32] = self._hdr_bytes(nh, addr)
        return nh

    def verify(self, ctx, src_path, dst_path, plan):
        a = open(src_path, "rb").read()
        b = open(dst_path, "rb").read()
        if len(a) != len(b):
            return VerifyResult(ok=False, notes=["size changed"])
        try:
            da, db = Dwg(a), Dwg(b)
        except DwgError as e:
            return VerifyResult(ok=False, notes=[f"reparse failed: {e}"])
        if sorted(da.sections) != sorted(db.sections) or set(da.pages) != set(db.pages):
            return VerifyResult(ok=False, notes=["maps changed"])
        for name, s in db.sections.items():
            for sp in s.pages:
                if sp.addr is None:
                    continue
                ha, hb = Dwg.page_header(a, sp.addr), Dwg.page_header(b, sp.addr)
                if ha[:6] != hb[:6]:
                    return VerifyResult(ok=False, notes=["page header fields changed"])
                if a[sp.addr:sp.addr + 32 + ha[2]] != b[sp.addr:sp.addr + 32 + hb[2]]:
                    comp = b[sp.addr + 32:sp.addr + 32 + hb[2]]
                    if page_checksum(0, comp) != hb[7]:
                        return VerifyResult(ok=False, notes=["data checksum mismatch"])
                    try:
                        us = max(0, min(s.max_size, s.size - sp.start))
                        if s.compressed == 2 and len(decompress(comp, us)) != us:
                            return VerifyResult(ok=False, notes=["page decompresses short"])
                    except DwgError:
                        return VerifyResult(ok=False, notes=["page no longer decompresses"])
        if "AcDb:SummaryInfo" in db.sections:
            try:
                si = db.read_section("AcDb:SummaryInfo")
                parse_summary(si, db.version >= b"AC1021")
            except Exception:
                return VerifyResult(ok=False, notes=["summary info unreadable"])
        # bytes outside the edited pages (SummaryInfo; uncompressed single-page AppInfo / AppInfoHistory)
        # must be identical
        spans = []
        for name in ("AcDb:SummaryInfo", "AcDb:AppInfoHistory", "AcDb:AppInfo"):
            s = db.sections.get(name)
            if s is None or len(s.pages) != 1 or s.pages[0].addr is None:
                continue
            if name != "AcDb:SummaryInfo" and s.compressed == 2:
                continue
            sp = s.pages[0]
            h = Dwg.page_header(b, sp.addr)
            spans.append((sp.addr, sp.addr + 32 + h[2]))
        spans.sort()
        pos = 0
        for lo, hi in spans:
            if a[pos:lo] != b[pos:lo]:
                return VerifyResult(ok=False, notes=["bytes outside the edited pages changed"])
            pos = max(pos, hi)
        if a[pos:] != b[pos:]:
            return VerifyResult(ok=False, notes=["bytes outside the edited pages changed"])
        return VerifyResult(ok=True)


register(DwgAdapter())

_APP_RUN_W = re.compile(rb"(?:[\x20-\x7e\xa0-\xff]\x00){3,}")
_APP_RUN_A = re.compile(rb"[\x20-\x7e]{3,}")
_APP_STRING = re.compile(r"(<string>)([^<]{2,80})(</string>)")


def _rewrite_app_strings(raw: bytearray, plan, wide: bool) -> int:
    """Same-length edits of the text runs of AcDb:AppInfo(History): a <string> value that is a known
    plan identity (field-only entries included: saving accounts) gets its alias; the rest of the run gets
    the plan's free-text pass (user folders, names)."""
    from deid.formats import cadnames
    wp = cadnames.wrap(plan)
    enc = "utf-16-le" if wide else "cp1252"
    n = 0
    for m in list((_APP_RUN_W if wide else _APP_RUN_A).finditer(bytes(raw))):
        try:
            t = m.group(0).decode(enc)
        except UnicodeDecodeError:
            continue

        def ent(mm):
            v = mm.group(2)
            key = "".join(v.lower().split())
            e = getattr(plan, "entries", {}).get(key) if hasattr(plan, "entries") else None
            if e is None:
                return mm.group(0)
            a = plan.alias(v, e.kind) or v
            return mm.group(1) + (a if len(a) == len(v) else v) + mm.group(3)
        nt = _APP_STRING.sub(ent, t)
        try:
            nt2 = wp.replace_text(nt)
        except Exception:
            nt2 = nt
        if len(nt2) == len(nt):
            nt = nt2
        if nt == t:
            continue
        b = nt.encode(enc, "replace")
        if len(b) != len(m.group(0)):
            continue
        raw[m.start():m.end()] = b
        n += 1
    return n
