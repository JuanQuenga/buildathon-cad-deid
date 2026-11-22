"""IGES 5.x fixed-format (.igs .iges).  Compressed IGES ('C' sections) and
files without 80-column records are scanned but not rewritten.

Edited: Start section text, Global section strings (sender/receiver product
ids, file name, native system, preprocessor, author, organization), and
Hollerith strings in the parameter data of text-bearing entities (212 general
note, 213, 308/320 subfigure names, 314 colour names, 406 properties incl.
names/levels, 408/410/416 names and external file references).

Layout policy: a changed parameter record is re-flowed into the same number of
lines when it fits (no pointer changes); otherwise the P section is
re-sequenced and the Directory Entry pointers/line counts and the Terminate
section are updated.  Unchanged records are copied byte for byte.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION, COPIED_UNSUPPORTED)
from deid.formats import cadnames
from deid.formats._neutral import PlanCache, ObsCollector, looks_like_reference, ref_kind

STRING_TYPES = {212, 213, 308, 310, 312, 314, 320, 402, 406, 408, 410, 416, 420, 422}
G_FIELDS = {3: "product_id_sender", 4: "file_name", 5: "native_system", 6: "preprocessor_version",
            12: "product_id_receiver", 15: "units_name", 18: "date", 21: "author", 22: "organization",
            25: "date_modified", 26: "application_protocol"}
G_KIND = {3: "part_name", 12: "part_name", 4: "path", 21: "person", 22: "company", 5: "software",
          6: "software"}
G_SKIP = {1, 2, 15, 18, 23, 24, 25, 26}


class IgesError(ValueError):
    pass


def _split_lines(data: bytes):
    lines = data.splitlines(keepends=True)
    return lines


def _body(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def _eol(line: bytes) -> bytes:
    b = line.rstrip(b"\r\n")
    return line[len(b):]


class Iges:
    def __init__(self, data: bytes):
        self.data = data
        lines = _split_lines(data)
        self.lines = lines
        secs = {"S": [], "G": [], "D": [], "P": [], "T": []}
        self.fixed = True
        for i, ln in enumerate(lines):
            b = _body(ln)
            if not b.strip() and i == len(lines) - 1:
                continue
            if len(b) < 73:
                self.fixed = False
                b = b.ljust(80)
            c = chr(b[72])
            if c not in secs:
                if c == "C":
                    raise IgesError("compressed IGES")
                raise IgesError("bad section letter")
            if len(b) != 80:
                self.fixed = False
            secs[c].append(i)
        if not secs["D"] and not secs["G"]:
            raise IgesError("no global/directory section")
        self.secs = secs
        self.eol = _eol(lines[0]) or b"\n"
        g = b"".join(_body(lines[i]).ljust(80)[:72] for i in secs["G"])
        self.gtext = g
        self.pdelim, self.rdelim = b",", b";"
        if g:
            m = re.match(rb"\s*1H(.)", g)
            if m:
                self.pdelim = m.group(1)
                rest = g[m.end():]
                rest = rest[1:] if rest[:1] == self.pdelim else rest
                m2 = re.match(rb"\s*1H(.)", rest)
                if m2:
                    self.rdelim = m2.group(1)
        # directory entries
        self.dir = []   # (type, pstart, pcount, d_line_index1, d_line_index2)
        dl = secs["D"]
        for k in range(0, len(dl) - 1, 2):
            a = _body(lines[dl[k]]).ljust(80)
            b = _body(lines[dl[k + 1]]).ljust(80)
            try:
                t = int(a[0:8])
                ps = int(a[8:16] or b"0")
                pc = int(b[24:32] or b"0")
            except ValueError:
                raise IgesError("bad directory entry")
            self.dir.append((t, ps, pc, dl[k], dl[k + 1]))

    def precord(self, ps: int, pc: int) -> bytes:
        pl = self.secs["P"]
        return b"".join(_body(self.lines[pl[i]]).ljust(80)[:64] for i in range(ps - 1, ps - 1 + pc))


_HOLL = re.compile(rb"\s*(\d+)H")


def parse_params(data: bytes, pdelim: bytes, rdelim: bytes):
    """Tokenise a parameter record.  Returns list of (start, end, is_string) spans
    (spans exclude delimiters) and the position after the record delimiter."""
    toks = []
    pos = 0
    n = len(data)
    while pos < n:
        m = _HOLL.match(data, pos)
        if m:
            k = int(m.group(1))
            s = m.end()
            e = s + k
            if e > n:
                raise IgesError("hollerith past end")
            toks.append((pos, e, True))
            pos = e
        else:
            j = pos
            while j < n and data[j:j + 1] not in (pdelim, rdelim):
                j += 1
            toks.append((pos, j, False))
            pos = j
        # skip spaces up to the delimiter
        while pos < n and data[pos:pos + 1] == b" ":
            pos += 1
        if pos >= n:
            break
        d = data[pos:pos + 1]
        pos += 1
        if d == rdelim:
            return toks, pos
        if d != pdelim:
            raise IgesError("unexpected character in parameters")
    return toks, pos


def _holl_value(tok: bytes) -> bytes:
    m = _HOLL.match(tok)
    return tok[m.end():]


def _mk_holl(val: bytes) -> bytes:
    return b"%dH" % len(val) + val


def _dec(b: bytes) -> str:
    try:
        return b.decode("ascii")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def _enc(s: str) -> bytes:
    return s.encode("latin-1", errors="replace")


def _layout(pieces: list, width: int) -> list:
    """Lay out token pieces (each ends with its delimiter) into lines of <= width chars,
    breaking between pieces; a piece longer than a line is split (strings may span lines)."""
    lines = []
    cur = b""
    for p in pieces:
        if len(cur) + len(p) <= width:
            cur += p
            continue
        if cur and len(p) <= width:
            lines.append(cur)
            cur = p
            continue
        # split long piece
        room = width - len(cur)
        cur += p[:room]
        lines.append(cur)
        p = p[room:]
        while len(p) > width:
            lines.append(p[:width])
            p = p[width:]
        cur = p
    if cur or not lines:
        lines.append(cur)
    return lines


BIG = 150 << 20


def _head_split(path: str):
    """Big-file mode: read the S and G sections, and locate D start and the T line.
    Returns (head_bytes, d_offset, t_offset, t_line)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = []
        off = 0
        while True:
            ln = f.readline()
            if not ln:
                raise IgesError("no directory section")
            b = ln.rstrip(b"\r\n")
            if len(b) >= 73 and b[72:73] == b"D":
                break
            if len(b) < 73 or b[72:73] not in (b"S", b"G"):
                raise IgesError("bad section letter")
            head.append(ln)
            off += len(ln)
            if off > (16 << 20):
                raise IgesError("start/global section too large")
        f.seek(max(0, size - 4096))
        tail = f.read()
    lines = tail.splitlines(keepends=True)
    if not lines:
        raise IgesError("no terminate section")
    t = lines[-1]
    if not t.strip() and len(lines) > 1:
        t = lines[-2]
    b = t.rstrip(b"\r\n")
    if len(b) < 73 or b[72:73] != b"T":
        raise IgesError("no terminate section")
    t_off = size - len(t) - (len(lines[-1]) if t is not lines[-1] else 0)
    return b"".join(head), off, t_off, t


class IgesAdapter(Adapter):
    name = "iges"
    version = "1n"
    priority = 9
    magics = ()
    extensions = (".igs", ".iges")
    path_independent = False
    timeout_s = 1800.0

    def matches(self, ctx, head):
        if ctx.magic not in ("text", "unknown"):
            return False
        first = head.split(b"\n", 1)[0].rstrip(b"\r")
        if len(first) >= 73 and first[72:73] in (b"S", b"G", b"C") and first[73:80].strip().isdigit():
            return True
        return ctx.ext in self.extensions and len(first) >= 73 and first[72:73] in (b"S", b"G")

    # ------------------------------------------------------------------
    def _edits(self, ig: Iges, fn_g, fn_s, fn_p):
        """Compute new S lines, new G lines, and changed P records {dir_index: new_data_bytes}."""
        lines = ig.lines
        # Start section: line by line
        new_s = []
        s_changed = False
        for i in ig.secs["S"]:
            b = _body(lines[i]).ljust(80)
            t = b[:72]
            nt = fn_s(t.rstrip())
            if isinstance(nt, str):
                nt = _enc(nt)
            if nt is not None and nt != t.rstrip():
                s_changed = True
                parts = [nt[k:k + 72] for k in range(0, max(len(nt), 1), 72)]
                new_s.extend(p.ljust(72) for p in parts)
            else:
                new_s.append(t)
        # Global
        new_g = None
        g = ig.gtext
        if g:
            toks, end = parse_params(g, ig.pdelim, ig.rdelim)
            pieces = []
            changed = False
            for idx, (s, e, is_str) in enumerate(toks, start=1):
                tok = g[s:e]
                if is_str and idx not in G_SKIP:
                    val = _holl_value(tok.strip())
                    nv = fn_g(idx, _dec(val))
                    if nv is not None and _enc(nv) != val:
                        tok = _mk_holl(_enc(nv))
                        changed = True
                pieces.append(tok)
            if changed:
                ends = [ig.pdelim] * (len(pieces) - 1) + [ig.rdelim]
                pcs = [p + d for p, d in zip(pieces, ends)]
                new_g = [ln.ljust(72) for ln in _layout(pcs, 72)]
        # Parameter data
        new_p = {}
        for di, (t, ps, pc, d1, d2) in enumerate(ig.dir):
            if t not in STRING_TYPES or pc <= 0:
                continue
            rec = ig.precord(ps, pc)
            try:
                toks, end = parse_params(rec, ig.pdelim, ig.rdelim)
            except IgesError:
                continue
            if not any(is_str for _, _, is_str in toks):
                continue
            pieces = [rec[s:e] for s, e, _ in toks]
            changed = False
            for k, (s, e, is_str) in enumerate(toks):
                if not is_str:
                    continue
                val = _holl_value(pieces[k].strip())
                nv = fn_p(t, k, _dec(val))
                if nv is None or _enc(nv) == val:
                    continue
                nb = _enc(nv)
                pieces[k] = _mk_holl(nb)
                changed = True
                if t == 212 and k >= 12:
                    # NC (character count) is 11 parameters before each TEXT string
                    nc_i = k - 11
                    try:
                        if int(pieces[nc_i].strip()) == len(val):
                            pieces[nc_i] = b"%d" % len(nb)
                    except ValueError:
                        pass
            if changed:
                ends = [ig.pdelim] * (len(pieces) - 1) + [ig.rdelim]
                new_p[di] = [p + d for p, d in zip(pieces, ends)] + ([rec[end:].rstrip()] if rec[end:].strip() else [])
        return (new_s if s_changed else None), new_g, new_p

    # ------------------------------------------------------------------
    def scan(self, ctx, src_path):
        big = os.path.getsize(src_path) > BIG
        try:
            if big:
                data = _head_split(src_path)[0]
            else:
                data = open(src_path, "rb").read()
            if not data.strip():
                return ScanResult(status="source_exception", notes=["empty file"])
            ig = Iges(data)
        except IgesError as e:
            return ScanResult(status="unsupported", notes=[f"iges: {e}"])
        col = ObsCollector(cap=3000)

        def fn_s(t):
            v = _dec(t).strip()
            if v and sum(c.isalpha() for c in v) >= 2:
                col.add("free_text", "iges.start", v, "start section", 0.5)
            return None

        def fn_g(idx, v):
            v = v.strip()
            if not v:
                return None
            field = "iges.global." + G_FIELDS.get(idx, f"f{idx}")
            if idx == 4 or looks_like_reference(v):
                col.ref(v, ref_kind(v), field)
            col.add(G_KIND.get(idx, "free_text"), field, v, "global", 0.8 if idx in G_KIND else 0.5)
            return None

        def fn_p(t, k, v):
            v = v.strip()
            if not v or not any(c.isalpha() for c in v):
                return None
            field = f"iges.e{t}"
            if t == 416 or looks_like_reference(v):
                col.ref(v, ref_kind(v), field)
                col.add("path", field, v, f"entity {t}", 0.7)
            else:
                col.add("part_name" if t in (308, 320, 406, 408) else "free_text", field, v, f"entity {t}", 0.5)
            return None

        try:
            self._edits(ig, fn_g, fn_s, fn_p)
        except IgesError as e:
            return ScanResult(observations=col.obs, status="source_exception", notes=[f"iges: {e}"])
        return ScanResult(observations=col.obs, references=col.refs,
                          fingerprint={"entities": len(ig.dir), "fixed": ig.fixed})

    def rewrite(self, ctx, src_path, dst_path, plan):
        big = os.path.getsize(src_path) > BIG
        try:
            if big:
                head, d_off, t_off, t_line = _head_split(src_path)
                data = head
            else:
                data = open(src_path, "rb").read()
            if not data.strip():
                return RewriteResult(status=SOURCE_EXCEPTION, notes=["empty file"])
            ig = Iges(data)
        except IgesError as e:
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=[f"iges: {e}"])
        if not ig.fixed:
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=["iges: records are not 80 columns"])
        pc = PlanCache(cadnames.wrap(plan), ctx)

        def fn_s(t):
            v = _dec(t)
            return pc.text(v)

        def fn_g(idx, v):
            if not v.strip():
                return None
            if idx == 4 or looks_like_reference(v):
                return pc.ref(v)
            k = G_KIND.get(idx)
            return pc.sub(v, k) if k and k != "software" else pc.text(v)

        def fn_p(t, k, v):
            if not v.strip():
                return None
            if t == 416 or looks_like_reference(v):
                return pc.ref(v)
            return pc.text(v)

        new_s, new_g, new_p = self._edits(ig, fn_g, fn_s, fn_p)
        if new_s is None and new_g is None and not new_p:
            return RewriteResult(status=VERIFIED_UNCHANGED, notes=["big file: S/G only"] if big else [])
        if big:
            return self._write_big(ig, new_s, new_g, src_path, dst_path, d_off, t_off, t_line)
        out = self._write(ig, new_s, new_g, new_p)
        with open(dst_path, "wb") as f:
            f.write(out)
        return RewriteResult(status=REWRITTEN, counts={"records_changed": len(new_p),
                                                         "global_changed": int(new_g is not None),
                                                         "start_changed": int(new_s is not None)})

    @staticmethod
    def _write_big(ig, new_s, new_g, src_path, dst_path, d_off, t_off, t_line):
        eol = ig.eol
        s_lines = new_s if new_s is not None else [_body(ig.lines[i]).ljust(80)[:72] for i in ig.secs["S"]]
        g_lines = new_g if new_g is not None else [_body(ig.lines[i]).ljust(80)[:72] for i in ig.secs["G"]]
        tb = t_line.rstrip(b"\r\n")
        try:
            nd, npl = int(tb[17:24]), int(tb[25:32])
        except ValueError:
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=["iges: bad terminate line"])
        with open(src_path, "rb") as fin, open(dst_path, "wb") as out:
            if new_s is None:
                out.write(b"".join(ig.lines[i] for i in ig.secs["S"]))
            else:
                for n, t in enumerate(s_lines, start=1):
                    out.write(t + b"S" + b"%7d" % n + eol)
            if new_g is None:
                out.write(b"".join(ig.lines[i] for i in ig.secs["G"]))
            else:
                for n, t in enumerate(g_lines, start=1):
                    out.write(t + b"G" + b"%7d" % n + eol)
            fin.seek(d_off)
            left = t_off - d_off
            while left > 0:
                b = fin.read(min(16 << 20, left))
                if not b:
                    break
                out.write(b)
                left -= len(b)
            out.write(b"S%7dG%7dD%7dP%7d" % (len(s_lines), len(g_lines), nd, npl) + tb[32:] +
                      t_line[len(tb):])
        return RewriteResult(status=REWRITTEN, notes=["big file: S/G only"])

    @staticmethod
    def _write(ig: Iges, new_s, new_g, new_p) -> bytes:
        lines = ig.lines
        eol = ig.eol
        out = []

        def sec(letter, texts):
            for n, t in enumerate(texts, start=1):
                out.append(t + letter + b"%7d" % n + eol)

        # S
        if new_s is None:
            out.extend(lines[i] for i in ig.secs["S"])
            ns = len(ig.secs["S"])
        else:
            sec(b"S", new_s)
            ns = len(new_s)
        # G
        if new_g is None:
            out.extend(lines[i] for i in ig.secs["G"])
            ng = len(ig.secs["G"])
        else:
            sec(b"G", new_g)
            ng = len(new_g)
        # P layout first (to know counts)
        p_lines_new = {}   # dir index -> list of 64-col data
        for di, pcs in new_p.items():
            t, ps, pc, d1, d2 = ig.dir[di]
            lay = _layout(pcs, 64)
            if len(lay) < pc:
                lay = lay + [b""] * (pc - len(lay))
            p_lines_new[di] = lay
        resequence = any(len(p_lines_new[di]) != ig.dir[di][2] for di in p_lines_new)
        # D
        if not resequence:
            out.extend(lines[i] for i in ig.secs["D"])
        else:
            newstart = {}
            cur = 1
            for di, (t, ps, pc, d1, d2) in enumerate(ig.dir):
                cnt = len(p_lines_new[di]) if di in p_lines_new else pc
                newstart[di] = (cur, cnt)
                cur += cnt
            for di, (t, ps, pc, d1, d2) in enumerate(ig.dir):
                a = _body(lines[d1])
                b = _body(lines[d2])
                st, cnt = newstart[di]
                if pc <= 0:
                    st, cnt = ps, pc
                a = a[:8] + b"%8d" % st + a[16:]
                b = b[:24] + b"%8d" % cnt + b[32:]
                out.append(a + _eol(lines[d1]))
                out.append(b + _eol(lines[d2]))
        # P
        pl = ig.secs["P"]
        if not new_p:
            out.extend(lines[i] for i in pl)
            npl = len(pl)
        else:
            seq = 0
            # map P line index -> owning directory index for changed records
            owner = {}
            for di in p_lines_new:
                t, ps, pc, d1, d2 = ig.dir[di]
                owner[ps - 1] = di
            i = 0
            while i < len(pl):
                di = owner.get(i)
                if di is not None:
                    t, ps, pc, d1, d2 = ig.dir[di]
                    deptr = _body(lines[pl[i]]).ljust(80)[64:72]
                    for txt in p_lines_new[di]:
                        seq += 1
                        out.append(txt.ljust(64) + deptr + b"P" + b"%7d" % seq + eol)
                    i += pc
                    continue
                seq += 1
                if not resequence:
                    out.append(lines[pl[i]])
                else:
                    b = _body(lines[pl[i]])
                    out.append(b[:73] + b"%7d" % seq + _eol(lines[pl[i]]))
                i += 1
            npl = seq
        # T
        nd = len(ig.secs["D"])
        tt = b"S%7dG%7dD%7dP%7d" % (ns, ng, nd, npl)
        if ig.secs["T"]:
            tl = _body(lines[ig.secs["T"][0]])
            out.append(tt + tl[32:72].ljust(40) + b"T" + b"%7d" % 1 + _eol(lines[ig.secs["T"][0]]))
            out.extend(lines[i] for i in ig.secs["T"][1:])
        else:
            out.append(tt.ljust(72) + b"T      1" + eol)
        # anything after T (rare trailing junk) is dropped only if blank
        return b"".join(out)

    def verify(self, ctx, src_path, dst_path, plan):
        if os.path.getsize(src_path) > BIG:
            return _verify_big(src_path, dst_path)
        try:
            a = iges_fingerprint(open(src_path, "rb").read())
            b = iges_fingerprint(open(dst_path, "rb").read())
        except IgesError as e:
            return VerifyResult(ok=False, notes=[f"parse error: {e}"])
        notes = [f"{k} differs" for k in a if k != "lines" and a[k] != b[k]]
        if not b["t_ok"]:
            notes.append("terminate counts wrong")
        if not b["fixed"]:
            notes.append("not 80 columns")
        return VerifyResult(ok=not notes, notes=notes, fingerprint=b)


def _verify_big(src, dst) -> VerifyResult:
    try:
        ha, da, ta, tla = _head_split(src)
        hb, db, tb_, tlb = _head_split(dst)
        ga, gb = Iges(ha), Iges(hb)
    except IgesError as e:
        return VerifyResult(ok=False, notes=[f"parse error: {e}"])
    notes = []
    if ta - da != tb_ - db:
        notes.append("D/P size differs")
    else:
        with open(src, "rb") as fa, open(dst, "rb") as fb:
            fa.seek(da); fb.seek(db)
            left = ta - da
            while left > 0:
                x = fa.read(min(16 << 20, left)); y = fb.read(min(16 << 20, left))
                if x != y:
                    notes.append("D/P bytes differ")
                    break
                left -= len(x)
                if not x:
                    break
    ka = [ga.gtext[s:e].strip() if not st else b"" for s, e, st in parse_params(ga.gtext, ga.pdelim, ga.rdelim)[0]]
    kb = [gb.gtext[s:e].strip() if not st else b"" for s, e, st in parse_params(gb.gtext, gb.pdelim, gb.rdelim)[0]]
    if ka != kb:
        notes.append("global numeric fields differ")
    t = tlb.rstrip(b"\r\n")
    try:
        if [int(t[1:8]), int(t[9:16])] != [len(gb.secs["S"]), len(gb.secs["G"])]:
            notes.append("terminate counts wrong")
    except ValueError:
        notes.append("terminate line unreadable")
    return VerifyResult(ok=not notes, notes=notes)


def iges_fingerprint(data: bytes) -> dict:
    ig = Iges(data)
    h = hashlib.sha256()
    # directory entries without P pointer/count and sequence numbers
    for t, ps, pc, d1, d2 in ig.dir:
        a = _body(ig.lines[d1]).ljust(80)
        b = _body(ig.lines[d2]).ljust(80)
        h.update(a[:8] + a[16:72] + b[:24] + b[32:72])
    # parameter records: non-string tokens (212 NC values masked)
    for t, ps, pc, d1, d2 in ig.dir:
        if pc <= 0:
            continue
        rec = ig.precord(ps, pc)
        if t not in STRING_TYPES:
            h.update(rec)
            h.update(b"|")
            continue
        try:
            toks, end = parse_params(rec, ig.pdelim, ig.rdelim)
        except IgesError:
            h.update(rec)
            continue
        strs = [k for k, (s, e, is_str) in enumerate(toks) if is_str]
        nc_mask = {k - 11 for k in strs} if t == 212 else set()
        for k, (s, e, is_str) in enumerate(toks):
            h.update(b"\0" if (is_str or k in nc_mask) else rec[s:e].strip())
            h.update(b"|")
    # global non-string fields
    if ig.gtext:
        toks, _ = parse_params(ig.gtext, ig.pdelim, ig.rdelim)
        for s, e, is_str in toks:
            h.update(b"\0" if is_str else ig.gtext[s:e].strip())
    t_ok = True
    if ig.secs["T"]:
        tl = _body(ig.lines[ig.secs["T"][0]])
        try:
            cnt = [int(tl[i + 1:i + 8]) for i in (0, 8, 16, 24)]
            t_ok = cnt == [len(ig.secs[c]) for c in "SGDP"]
        except ValueError:
            t_ok = False
    # P pointer consistency
    for t, ps, pc, d1, d2 in ig.dir:
        if pc > 0:
            pl = ig.secs["P"]
            if ps - 1 + pc > len(pl):
                t_ok = False
                break
    return {"masked_sha256": h.hexdigest(), "entities": len(ig.dir), "t_ok": t_ok, "fixed": ig.fixed,
            "lines": len(ig.lines)}


register(IgesAdapter())
