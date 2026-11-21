"""ACIS SAT text (.sat; .sab binary is detected and left to copy).

Layout: line 1 ``<version> <nrecords> <nbodies> <history>``; line 2 three counted
strings ``<len> <text>`` (producer, ACIS version, date); line 3 units/tolerances;
then records terminated by ``#``.  From ACIS 7.0 (version >= 700) strings in
records are ``@<len> <text>``; before 7.0 they are ``<len> <text>`` and only
recognised in a whitelist of attribute records where the layout is known.

Only string payloads (and their length prefixes) change.  Every other byte is
copied verbatim; verify compares the string-masked token streams.
"""
from __future__ import annotations

import hashlib
import mmap
import os
import re
from typing import Optional

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED)
from deid.formats._neutral import PlanCache, ObsCollector, looks_like_reference, ref_kind

_AT = re.compile(rb"@(\d{1,9}) ")
_HDR1 = re.compile(rb"\s*(\d+) +(-?\d+) +(-?\d+) +(-?\d+)")
_INT = re.compile(rb"(\d{1,6}) ")
_OLDTOK = re.compile(rb"(\$?-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|#|\S+)")
_BORING = {b"unknown", b"plain", b"color", b"phong", b"none", b"ambient factor", b"diffuse factor",
           b"specular factor", b"exponent", b"specular color", b"transparency", b"reflection",
           b"ASM_EDGE_FROM_STEP", b"ASM_FACE_FROM_STEP", b"ASM_BODY_FROM_STEP", b"ASM",
           b"ATTRIB_XACIS_NAME", b"tangent_not_g2", b"tangent", b"ATTRIB_XACIS_COLOR",
           b"ATTRIB_XACIS_LAYER", b"ATTRIB_XACIS_ID", b"forward", b"reversed", b"in", b"out",
           b"single", b"double", b"unknown_type"}

# Pre-7.0 record types whose trailing data is a sequence of counted strings / ints.
_OLD_STRING_RECORDS = re.compile(
    rb"^(?:-\d+ )?((?:attrib_gssl_step_\w+-attrib_gssl_step-attrib)|(?:[\w]*name_attrib[\w-]*)|"
    rb"(?:string_attrib[\w-]*)|(?:[\w]*(?:string|text|name|label)[\w]*-[\w-]*attrib))\b")

KIND_BY_RECORD = {
    "attrib_gssl_step_person_details": "person",
    "attrib_gssl_step_org_details": "company",
    "attrib_gssl_step_product_details": "part_name",
}


def _rtype(line: bytes) -> str:
    t = line.lstrip().split(b" ", 2)
    if not t or not t[0]:
        return "?"
    w = t[0]
    if w.startswith(b"-") and w[1:].isdigit() and len(t) > 1:
        w = t[1]
    return w.decode("latin-1")[:80]


def _dec(b: bytes) -> tuple[str, str]:
    if b.isascii():
        return b.decode("ascii"), "ascii"
    try:
        return b.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return b.decode("latin-1"), "latin-1"


def _enc(s: str, encd: str) -> bytes:
    if encd == "ascii":
        try:
            return s.encode("ascii")
        except UnicodeEncodeError:
            return s.encode("utf-8")
    return s.encode(encd, errors="replace")


class _Sat:
    """Locate every string payload in a SAT buffer.  Yields (start, end, len_start, kind, rtype, field)
    where buf[len_start:start] is the length prefix (``@12 `` or ``12 ``)."""

    def __init__(self, buf):
        self.buf = buf
        m = _HDR1.match(buf)
        if not m:
            raise ValueError("not a SAT header")
        self.version = int(m.group(1))
        self.l1_end = buf.find(b"\n", m.end())
        if self.l1_end < 0:
            raise ValueError("truncated header")
        self.l1_end += 1
        if _INT.match(buf, self.l1_end):
            self.l2_end = buf.find(b"\n", self.l1_end)
            if self.l2_end < 0:
                self.l2_end = len(buf)
        else:  # very old files (ACIS 1.x): no producer line
            self.l2_end = self.l1_end

    def header_strings(self):
        buf = self.buf
        pos = self.l1_end
        names = ("producer", "acis_version", "date")
        i = 0
        while i < 3 and pos < self.l2_end:
            m = _INT.match(buf, pos)
            if not m:
                break
            n = int(m.group(1))
            s = m.end()
            e = s + n
            if e > self.l2_end:
                break
            yield s, e, pos, "header", names[i]
            pos = e
            while pos < self.l2_end and buf[pos:pos + 1] == b" ":
                pos += 1
            i += 1

    def record_strings(self):
        buf = self.buf
        start = self.l2_end
        if self.version >= 700:
            pos = start
            rec = start
            search = _AT.search
            while True:
                m = search(buf, pos)
                if m is None:
                    return
                if m.start() > 0 and buf[m.start() - 1] not in (32, 9, 10, 13):
                    pos = m.start() + 1   # '@' inside a token: not a string prefix
                    continue
                j = buf.rfind(b"#", pos, m.start())
                if j >= 0:
                    rec = j + 1
                n = int(m.group(1))
                s = m.end()
                e = s + n
                if e > len(buf):
                    raise ValueError("string runs past end of file")
                yield s, e, m.start(), rec
                pos = e

    def old_records(self):
        """Pre-7.0 whitelisted records: yields (s, e, len_start, rec_start)."""
        buf = self.buf
        pos = self.l2_end
        n = len(buf)
        while pos < n:
            nl = buf.find(b"\n", pos)
            if nl < 0:
                nl = n
            line = buf[pos:nl]
            m = _OLD_STRING_RECORDS.match(line)
            if m:
                yield from self._old_counted(line, pos)
            pos = nl + 1

    @staticmethod
    def _old_counted(line: bytes, base: int):
        # skip record type and leading pointers/ints until the first plausible counted string
        toks = re.finditer(rb"\S+", line)
        first = next(toks, None)
        if first is None:
            return
        p = first.end() + 1
        L = len(line)
        while p < L:
            if line[p:p + 1] == b" ":
                p += 1
                continue
            m = _OLDTOK.match(line, p)
            if not m:
                return
            tok = m.group(1)
            if tok.isdigit():
                k = int(tok)
                s = m.end() + 1
                e = s + k
                # counted string: exactly k bytes, followed by a space (or end of record)
                if (k > 0 and m.end() < L and line[m.end():m.end() + 1] == b" " and e <= L
                        and (e == L or line[e:e + 1] in (b" ", b"\r")) and re.search(rb"[A-Za-z]", line[s:e])
                        and not line[s:e].startswith((b"$", b"#"))):
                    yield base + s, base + e, base + p, base
                    p = e
                    continue
                if k == 0 and line[m.end():m.end() + 2] == b"  ":
                    p = m.end() + 1
                    continue
            p = m.end()


def _iter_all(sat: _Sat):
    for s, e, ls, kind, name in sat.header_strings():
        yield s, e, ls, "header", name
    if sat.version >= 700:
        for s, e, ls, rec in sat.record_strings():
            yield s, e, ls, "rec", rec
    else:
        for s, e, ls, rec in sat.old_records():
            yield s, e, ls, "rec", rec


def _open_map(path):
    f = open(path, "rb")
    size = os.fstat(f.fileno()).st_size
    if size == 0:
        f.close()
        return None, None
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    return f, mm


def _is_sat(head: bytes) -> bool:
    m = _HDR1.match(head)
    if not m:
        return False
    v = int(m.group(1))
    if not (100 <= v <= 100000):
        return False
    nl = head.find(b"\n")
    if nl < 0:
        return False
    if _INT.match(head, nl + 1):
        return b"ACIS" in head[:600] or b"#" in head
    return bool(re.match(rb"(?:-\d+ )?body \$", head[nl + 1:nl + 40]))


class SatAdapter(Adapter):
    name = "sat"
    version = "2"
    priority = 9
    magics = ()
    extensions = (".sat", ".sab", ".asat")
    path_independent = True
    timeout_s = 1800.0

    def matches(self, ctx, head):
        if ctx.magic not in ("text", "unknown"):
            return False
        return _is_sat(head)

    def scan(self, ctx, src_path):
        col = ObsCollector(cap=3000)
        f, mm = _open_map(src_path)
        if mm is None:
            return ScanResult(status="source_exception", notes=["empty file"])
        nstr = 0
        try:
            sat = _Sat(mm)
            for s, e, ls, kind, info in _iter_all(sat):
                nstr += 1
                raw = mm[s:e]
                if raw in _BORING:
                    continue
                val, _ = _dec(raw)
                if not val.strip():
                    continue
                if kind == "header":
                    field = f"sat.header.{info}"
                    if info == "date":
                        continue
                    col.add("software" if info == "acis_version" else "free_text", field, val, "header", 0.5)
                    for p in re.findall(r"(?:[A-Za-z]:)?[\\/][^\s]+[\\/][^\s]+", val):
                        col.add("path", field, p, "header path", 0.7)
                    continue
                rt = _rtype(mm[info:min(info + 200, s)])
                base = rt.split("-", 1)[0]
                field = f"sat.{base}"
                if looks_like_reference(val):
                    col.ref(val, ref_kind(val), field)
                    col.add("path", field, val, base, 0.7)
                    continue
                k = KIND_BY_RECORD.get(base)
                if k is None and "name" in base:
                    k = "part_name"
                col.add(k or "free_text", field, val, base, 0.8 if k else 0.5)
        finally:
            mm.close(); f.close()
        notes = [f"acis version {sat.version}"]
        if col.dropped:
            notes.append(f"observations capped ({col.dropped} more)")
        return ScanResult(observations=col.obs, references=col.refs,
                          fingerprint={"version": sat.version, "strings": nstr}, notes=notes)

    def rewrite(self, ctx, src_path, dst_path, plan):
        pc = PlanCache(plan, ctx)
        f, mm = _open_map(src_path)
        if mm is None:
            return RewriteResult(status="source_exception", notes=["empty file"])
        changed = 0
        seen = 0
        cache: dict = {}
        tmp = dst_path + ".part"
        try:
            sat = _Sat(mm)
            with open(tmp, "wb", buffering=1 << 20) as out:
                w = out.write
                pos = 0
                for s, e, ls, kind, info in _iter_all(sat):
                    seen += 1
                    raw = mm[s:e]
                    if raw in _BORING:
                        continue
                    if kind == "header":
                        if info == "date":
                            continue
                        k = "software" if info == "acis_version" else None
                        key = (raw, k)
                    else:
                        base = _rtype(mm[info:min(info + 200, s)]).split("-", 1)[0]
                        k = KIND_BY_RECORD.get(base) or ("part_name" if "name" in base else None)
                        key = (raw, k)
                    new = cache.get(key)
                    if new is None:
                        val, encd = _dec(raw)
                        if not val.strip():
                            nv = val
                        elif looks_like_reference(val):
                            nv = pc.ref(val)
                        elif k:
                            nv = pc.sub(val, k)
                        else:
                            nv = pc.text(val)
                        new = raw if nv == val else _enc(nv, encd)
                        cache[key] = new
                    if new == raw:
                        continue
                    # length prefix: '@N ' or 'N '
                    prefix = mm[ls:s]
                    at = prefix.startswith(b"@")
                    w(mm[pos:ls])
                    w((b"@%d " if at else b"%d ") % len(new))
                    w(new)
                    pos = e
                    changed += 1
                w(mm[pos:len(mm)])
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        finally:
            mm.close(); f.close()
        if changed == 0:
            os.unlink(tmp)
            return RewriteResult(status=VERIFIED_UNCHANGED, counts={"strings": seen})
        os.replace(tmp, dst_path)
        return RewriteResult(status=REWRITTEN, counts={"strings": seen, "strings_replaced": changed})

    def verify(self, ctx, src_path, dst_path, plan):
        try:
            a = sat_fingerprint(src_path)
            b = sat_fingerprint(dst_path)
        except Exception as ex:  # parse failure of the output
            return VerifyResult(ok=False, notes=[f"parse error {type(ex).__name__}"])
        notes = [k for k in a if a[k] != b[k]]
        return VerifyResult(ok=not notes, notes=[f"{k} differs" for k in notes], fingerprint=b)


def sat_fingerprint(path: str) -> dict:
    """Hash of all bytes outside string payloads and length prefixes, plus counts."""
    f, mm = _open_map(path)
    if mm is None:
        return {"masked_sha256": "", "strings": 0, "records": 0, "end_marker": False}
    try:
        sat = _Sat(mm)
        h = hashlib.sha256()
        pos = 0
        n = 0
        for s, e, ls, kind, info in _iter_all(sat):
            h.update(mm[pos:ls])
            h.update(b"\x00")
            pos = e
            n += 1
        h.update(mm[pos:len(mm)])
        tail = mm[max(0, len(mm) - 200):len(mm)]
        # records: count '#' terminators outside strings
        return {"masked_sha256": h.hexdigest(), "strings": n, "version": sat.version,
                "end_marker": b"End-of-ACIS-data" in tail or b"End-of-ASM-data" in tail}
    finally:
        mm.close(); f.close()


register(SatAdapter())
