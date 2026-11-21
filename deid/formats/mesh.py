"""Mesh formats: STL (binary and ASCII), OBJ, MTL, VRML 1/2 (.wrl), OFF.

Only names, comments, string literals and file references change; vertex,
facet and index data are copied byte for byte.  Binary STL: the 80-byte header
is patched in place (fixed size), everything after it is streamed unchanged.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION)
from deid.formats._neutral import PlanCache, ObsCollector, looks_like_reference, ref_kind

CHUNK = 16 << 20


def _dec(b: bytes):
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


def _copy_range(fin, out, n: int):
    while n > 0:
        b = fin.read(min(CHUNK, n))
        if not b:
            break
        out.write(b)
        n -= len(b)


def _files_equal_from(a: str, b: str, off: int) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        fa.seek(off); fb.seek(off)
        while True:
            x = fa.read(CHUNK)
            y = fb.read(CHUNK)
            if x != y:
                return False
            if not x:
                return True


# ---------------------------------------------------------------------------
# Line-oriented streaming engine (ASCII STL, OBJ, MTL, OFF)
# ---------------------------------------------------------------------------

_NLRX_CACHE: dict = {}


def _iter_lines_matching(f, rx):
    """Yield ('pass', bytes, b'') and ('line', bytes-without-eol, eol) events.
    rx selects lines by matching at their start; the search is driven by a
    literal-newline prefix so non-matching lines are skipped at C speed."""
    nlrx = _NLRX_CACHE.get(rx.pattern)
    if nlrx is None:
        nlrx = _NLRX_CACHE[rx.pattern] = re.compile(b"\n(?=" + rx.pattern + b")", rx.flags & re.I)
    carry = b""
    while True:
        data = f.read(CHUNK)
        buf = carry + data
        if not buf:
            return
        if data:
            cut = buf.rfind(b"\n") + 1
            if cut == 0:
                carry = buf
                if len(carry) > 256 << 20:
                    yield "pass", carry, b""
                    carry = b""
                continue
            part, carry = buf[:cut], buf[cut:]
        else:
            part, carry = buf, b""
        pos = 0
        starts = [0] if rx.match(part, 0) else []
        for s in _chain(starts, (m.start() + 1 for m in nlrx.finditer(part))):
            e = part.find(b"\n", s)
            if e < 0:
                e = len(part)
                eol = b""
                nxt = e
            else:
                eol = b"\n"
                nxt = e + 1
            line = part[s:e]
            if line.endswith(b"\r"):
                line = line[:-1]
                eol = b"\r" + eol
            if s > pos:
                yield "pass", part[pos:s], b""
            yield "line", line, eol
            pos = nxt
        if pos < len(part):
            yield "pass", part[pos:], b""
        if not data:
            return


def _chain(a, b):
    yield from a
    yield from b


def _masked_hash(path: str, rx) -> tuple[str, int]:
    """Hash of the file with every rx-selected line replaced by its first token."""
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for kind, b, eol in _iter_lines_matching(f, rx):
            if kind == "pass":
                h.update(b)
            else:
                n += 1
                tok = b.lstrip().split(None, 1)[:1]
                t = tok[0] if tok else b""
                if t.startswith(b"#"):
                    t = b"#"
                h.update(b"\x00" + t + eol)
    return h.hexdigest(), n


# ---------------------------------------------------------------------------
# STL
# ---------------------------------------------------------------------------

def _stl_kind(path: str, head: bytes, size: int) -> str:
    if size >= 84:
        n = struct.unpack_from("<I", head, 80)[0]
        if 84 + 50 * n == size:
            return "binary"
    h = head.lstrip()
    if h[:5].lower() == b"solid" and (b"facet" in head or b"endsolid" in head or size < 200):
        if b"\x00" not in head[:min(len(head), 4096)]:
            return "ascii"
    if size >= 84:
        return "binary_badcount"
    return "invalid"


_STL_RX = re.compile(rb"[ \t]*(?:end)?solid\b", re.I)


class StlAdapter(Adapter):
    name = "stl"
    version = "1"
    priority = 10
    magics = ("stl_ascii", "stl_binary")
    extensions = (".stl",)
    path_independent = True
    timeout_s = 1800.0

    def matches(self, ctx, head):
        if ctx.magic in self.magics:
            return True
        if ctx.ext == ".stl" and ctx.magic in ("text", "binary", "unknown"):
            return True
        return False

    def scan(self, ctx, src_path):
        size = os.path.getsize(src_path)
        with open(src_path, "rb") as f:
            head = f.read(8192)
        kind = _stl_kind(src_path, head, size)
        col = ObsCollector(cap=500)
        fp = {"stl": kind, "size": size}
        if kind in ("binary", "binary_badcount"):
            txt = head[:80].split(b"\x00", 1)[0]
            val, _ = _dec(txt)
            val = val.strip()
            if val and sum(c.isalpha() for c in val) >= 2:
                col.add("free_text", "stl.header", val, "binary header", 0.6)
            fp["facets"] = struct.unpack_from("<I", head, 80)[0]
            st = "ok" if kind == "binary" else "source_exception"
            notes = [] if kind == "binary" else ["binary STL facet count does not match file size"]
            return ScanResult(observations=col.obs, fingerprint=fp, status=st, notes=notes)
        if kind == "invalid":
            return ScanResult(fingerprint=fp, status="source_exception", notes=["not a valid STL"])
        with open(src_path, "rb") as f:
            for k, line, eol in _iter_lines_matching(f, _STL_RX):
                if k != "line":
                    continue
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    val, _ = _dec(parts[1])
                    col.add("part_name", "stl.solid", val.strip(), parts[0].decode("latin-1").lower(), 0.7)
        return ScanResult(observations=col.obs, fingerprint=fp)

    def rewrite(self, ctx, src_path, dst_path, plan):
        pc = PlanCache(plan, ctx)
        size = os.path.getsize(src_path)
        with open(src_path, "rb") as f:
            head = f.read(8192)
        kind = _stl_kind(src_path, head, size)
        if kind == "invalid":
            return RewriteResult(status=SOURCE_EXCEPTION, notes=["not a valid STL"])
        if kind.startswith("binary"):
            hdr = head[:80]
            new = _patch_fixed(hdr, pc)
            if new == hdr:
                return RewriteResult(status=VERIFIED_UNCHANGED)
            with open(src_path, "rb") as fin, open(dst_path, "wb") as out:
                out.write(new)
                fin.seek(80)
                _copy_range(fin, out, size - 80)
            return RewriteResult(status=REWRITTEN, counts={"header_patched": 1})
        changed = 0
        with open(src_path, "rb") as fin, open(dst_path + ".part", "wb") as out:
            for k, line, eol in _iter_lines_matching(fin, _STL_RX):
                if k == "pass":
                    out.write(line)
                    continue
                m = re.match(rb"([ \t]*(?:end)?solid)([ \t]+)(.*)$", line, re.I | re.S)
                if m and m.group(3).strip():
                    val, encd = _dec(m.group(3))
                    nv = pc.sub(val, "part_name")
                    if nv != val:
                        line = m.group(1) + m.group(2) + _enc(nv, encd)
                        changed += 1
                out.write(line + eol)
        if not changed:
            os.unlink(dst_path + ".part")
            return RewriteResult(status=VERIFIED_UNCHANGED)
        os.replace(dst_path + ".part", dst_path)
        return RewriteResult(status=REWRITTEN, counts={"names_replaced": changed})

    def verify(self, ctx, src_path, dst_path, plan):
        size = os.path.getsize(src_path)
        with open(src_path, "rb") as f:
            head = f.read(8192)
        kind = _stl_kind(src_path, head, size)
        if kind.startswith("binary"):
            ok = os.path.getsize(dst_path) == size and _files_equal_from(src_path, dst_path, 80)
            return VerifyResult(ok=ok, notes=[] if ok else ["binary STL body differs"])
        a = _masked_hash(src_path, _STL_RX)
        b = _masked_hash(dst_path, _STL_RX)
        return VerifyResult(ok=a == b, notes=[] if a == b else ["ASCII STL facet data differs"])


def _patch_fixed(hdr: bytes, pc: PlanCache) -> bytes:
    """Same-length rewrite of a fixed-size text header (binary STL, etc.).
    Text up to the first NUL is decoded; replacement shorter -> padded with spaces,
    longer -> truncated at the field size (header is free text, never parsed)."""
    end = hdr.find(b"\x00")
    text = hdr if end < 0 else hdr[:end]
    if not text.strip():
        return hdr
    # Only the leading printable run is treated as text (vendor headers may carry binary after it).
    m = re.match(rb"[\x20-\x7e]*", text)
    t = m.group(0)
    if not t.strip():
        return hdr
    val = t.decode("ascii")
    nv = pc.text(val)
    if nv == val:
        nv2 = pc.sub(val.strip(), "part_name")
        if nv2 != val.strip():
            i = val.index(val.strip())
            nv = val[:i] + nv2 + val[i + len(val.strip()):]
    if nv == val:
        return hdr
    nb = nv.encode("ascii", "replace")
    rest = hdr[len(t):]
    room = len(hdr) if not rest.strip(b"\x00 ") else len(t)   # trailing padding may be used
    nb = nb[:room]
    if len(nb) < len(t):
        return nb + b" " * (len(t) - len(nb)) + rest
    return nb + hdr[len(nb):]


# ---------------------------------------------------------------------------
# OBJ / MTL / OFF
# ---------------------------------------------------------------------------

_OBJ_RX = re.compile(rb"[ \t]*(?:#|o[ \t]|g[ \t]|mtllib[ \t]|usemtl[ \t]|call[ \t]|maplib[ \t]|usemap[ \t]|"
                     rb"shadow_obj[ \t]|trace_obj[ \t]|newmtl[ \t]|map_\w+[ \t]|bump[ \t]|disp[ \t]|decal[ \t]|refl[ \t]|norm[ \t])")
_OFF_RX = re.compile(rb"[ \t]*#")


def _split_opts_path(rest: bytes):
    """MTL map statements: options (-o 1 1 1, -clamp on, ...) then a filename.
    Returns (prefix_bytes, filename_bytes)."""
    toks = list(re.finditer(rb"\S+", rest))
    if not toks:
        return rest, b""
    # filename = everything from the first token that is not an option/option value
    i = 0
    nargs = {b"-blendu": 1, b"-blendv": 1, b"-bm": 1, b"-boost": 1, b"-cc": 1, b"-clamp": 1, b"-imfchan": 1,
             b"-mm": 2, b"-o": 3, b"-s": 3, b"-t": 3, b"-texres": 1, b"-type": 1}
    while i < len(toks):
        t = toks[i].group(0)
        if t in nargs:
            j = i + 1
            k = 0
            while j < len(toks) and k < nargs[t] and re.match(rb"^-?[\d.]+$|^on$|^off$|^[a-z]$", toks[j].group(0)):
                j += 1; k += 1
            if t in (b"-clamp", b"-blendu", b"-blendv", b"-imfchan", b"-type") and k == 0 and j < len(toks):
                j += 1
            i = j
            continue
        break
    if i >= len(toks):
        return rest, b""
    s = toks[i].start()
    return rest[:s], rest[s:]


def _obj_line(line: bytes, pc: PlanCache, col: ObsCollector = None, fmt="obj") -> bytes:
    m = re.match(rb"([ \t]*)(#|\S+)([ \t]*)(.*)$", line, re.S)
    if not m:
        return line
    lead, kw, sp, rest = m.groups()
    if not rest.strip() and kw != b"#":
        return line
    kwl = kw.lower()
    trail = rest[len(rest.rstrip()):]
    body = rest.rstrip()
    if kw == b"#" or kw.startswith(b"#"):
        # comment: kw may be '#foo'
        text = line[len(lead) + 1:]
        val, encd = _dec(text)
        if col is not None:
            if any(c.isalpha() for c in val):
                col.add("free_text", f"{fmt}.comment", val.strip(), "comment", 0.5)
            return line
        nv = pc.text(val)
        return line if nv == val else lead + b"#" + _enc(nv, encd)
    val, encd = _dec(body)
    if kwl in (b"mtllib", b"maplib", b"call", b"shadow_obj", b"trace_obj"):
        # one or more file names separated by spaces (names may contain spaces in practice:
        # treat the whole remainder as one reference when it ends with a known extension)
        if col is not None:
            col.ref(val, ref_kind(val), f"{fmt}.{kwl.decode()}")
            return line
        nv = pc.ref(val)
    elif kwl.startswith(b"map_") or kwl in (b"bump", b"disp", b"decal", b"refl", b"norm"):
        pre, fn = _split_opts_path(body)
        if not fn:
            return line
        fval, encd = _dec(fn)
        if col is not None:
            col.ref(fval, ref_kind(fval), f"{fmt}.{kwl.decode()}")
            return line
        nf = pc.ref(fval)
        if nf == fval:
            return line
        return lead + kw + sp + pre + _enc(nf, encd) + trail
    elif kwl in (b"usemtl", b"newmtl", b"usemap"):
        if col is not None:
            col.add("free_text", f"{fmt}.material", val, kwl.decode(), 0.4)
            return line
        nv = pc.text(val)    # same function on both sides keeps usemtl/newmtl consistent
    else:  # o / g names
        if col is not None:
            for name in val.split():
                col.add("part_name", f"{fmt}.{kwl.decode()}", name, "group/object name", 0.6)
            if len(val.split()) > 1:
                col.add("part_name", f"{fmt}.{kwl.decode()}", val, "group/object name", 0.4)
            return line
        if kwl == b"g":
            nv = " ".join(pc.sub(t, "part_name") for t in val.split(" "))
            nv = pc.text(nv) if nv == val else nv
        else:
            nv = pc.sub(val, "part_name")
        nv = nv.replace("\n", " ").replace("\r", " ")
    if nv == val:
        return line
    return lead + kw + sp + _enc(nv, encd) + trail


class _LineTextAdapter(Adapter):
    rx = _OBJ_RX
    fmt = "obj"
    path_independent = False

    def scan(self, ctx, src_path):
        col = ObsCollector(cap=3000)
        with open(src_path, "rb") as f:
            for k, line, eol in _iter_lines_matching(f, self.rx):
                if k == "line":
                    _obj_line(line, None, col, self.fmt)
        return ScanResult(observations=col.obs, references=col.refs)

    def rewrite(self, ctx, src_path, dst_path, plan):
        pc = PlanCache(plan, ctx)
        changed = 0
        tmp = dst_path + ".part"
        with open(src_path, "rb") as fin, open(tmp, "wb") as out:
            for k, line, eol in _iter_lines_matching(fin, self.rx):
                if k == "pass":
                    out.write(line)
                    continue
                nl = _obj_line(line, pc, None, self.fmt)
                if nl != line:
                    changed += 1
                out.write(nl + eol)
        if not changed:
            os.unlink(tmp)
            return RewriteResult(status=VERIFIED_UNCHANGED)
        os.replace(tmp, dst_path)
        return RewriteResult(status=REWRITTEN, counts={"lines_replaced": changed})

    def verify(self, ctx, src_path, dst_path, plan):
        a = _masked_hash(src_path, self.rx)
        b = _masked_hash(dst_path, self.rx)
        return VerifyResult(ok=a == b, notes=[] if a == b else ["geometry lines differ"])


def _looks_obj(head: bytes) -> bool:
    return bool(re.search(rb"(?m)^[ \t]*(?:v|vn|vt|f|o|g|mtllib|usemtl)[ \t]+\S", head))


class ObjAdapter(_LineTextAdapter):
    name = "obj"
    version = "1"
    priority = 8
    extensions = (".obj",)
    fmt = "obj"
    rx = _OBJ_RX

    def matches(self, ctx, head):
        return ctx.ext == ".obj" and ctx.magic in ("text", "unknown") and _looks_obj(head)


class MtlAdapter(_LineTextAdapter):
    name = "mtl"
    version = "1"
    priority = 8
    extensions = (".mtl",)
    fmt = "mtl"
    rx = _OBJ_RX

    def matches(self, ctx, head):
        return ctx.ext == ".mtl" and ctx.magic in ("text", "unknown")


class OffAdapter(_LineTextAdapter):
    name = "off"
    version = "1"
    priority = 8
    extensions = (".off",)
    fmt = "off"
    rx = _OFF_RX

    def matches(self, ctx, head):
        return ctx.magic in ("text", "unknown") and bool(re.match(rb"\s*(?:#[^\n]*\n\s*)*(?:ST)?C?N?4?n?OFF", head))


# ---------------------------------------------------------------------------
# VRML 1.0 / 2.0 (.wrl)
# ---------------------------------------------------------------------------
# Interesting tokens: strings "...", comments #..., and DEF/USE names.
_VRML_TOK = re.compile(
    rb'(?:[^"#DU]++|D(?!EF[ \t\r\n,])|U(?!SE[ \t\r\n,]))*+'
    rb'("(?:[^"\\]++|\\.)*+"|#[^\r\n]*|(?:DEF|USE)[ \t\r\n,]+[^ \t\r\n,"\'#.\[\]\\{}\x00-\x1f]+)', re.S)
_VRML_SKIP = re.compile(rb'(?:[^"#DU]++|D(?!EF[ \t\r\n,])|U(?!SE[ \t\r\n,]))*+')
_VRML_NAME_BAD = re.compile(r'[\s"\'#,.\[\]\\{}+\x00-\x1f\x7f]')


def _vrml_id(s: str) -> str:
    s = _VRML_NAME_BAD.sub("_", s)
    if not s or s[0].isdigit() or s[0] in "+-":
        s = "_" + s
    return s


def _vrml_tokens(f):
    """Yield ('pass', bytes) / ('tok', bytes, prev_context_bytes)."""
    carry = b""
    eof = False
    while True:
        data = f.read(CHUNK)
        if not data:
            eof = True
        buf = carry + data
        if not buf:
            return
        n = len(buf)
        pos = 0
        while True:
            m = _VRML_TOK.match(buf, pos)
            if m is None:
                break
            s, e = m.span(1)
            if not eof and e >= n - 1:
                break
            if s > pos:
                yield "pass", buf[pos:s], b""
            yield "tok", buf[s:e], buf[max(0, s - 200):s]
            pos = e
        if eof:
            if pos < n:
                yield "pass", buf[pos:], b""
            return
        k = _VRML_SKIP.match(buf, pos).end()
        lim = min(k, n - 8)
        cut = buf.rfind(b"\n", pos, lim)
        cut = cut + 1 if cut >= 0 else pos
        if cut > pos:
            yield "pass", buf[pos:cut], b""
        carry = buf[cut:]
        if len(carry) > 256 << 20:
            raise ValueError("unterminated VRML token")


def _vrml_unescape(b: bytes) -> bytes:
    return re.sub(rb"\\(.)", rb"\1", b, flags=re.S)


def _vrml_escape(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b'"', b'\\"')


_VRML_URL_CTX = re.compile(rb"(?:url|Inline|ImageTexture|MovieTexture|AudioClip|Anchor|filename|"
                           rb"parameter|EXTERNPROTO)\s*\[?\s*(?:\"[^\"]*\"\s*,?\s*)*$")


class VrmlAdapter(Adapter):
    name = "vrml"
    version = "1"
    priority = 10
    magics = ("vrml",)
    extensions = (".wrl", ".vrml", ".wrz")
    path_independent = False
    timeout_s = 1800.0

    def matches(self, ctx, head):
        if ctx.magic == "vrml":
            return not head.lstrip().startswith(b"#X3D")
        return False

    def _iter(self, src_path, pc, col):
        with open(src_path, "rb") as f:
            for k, tok, prev in _vrml_tokens(f):
                if k == "pass":
                    yield tok
                    continue
                c = tok[:1]
                if c == b"#":
                    if tok.startswith((b"#VRML", b"#X3D", b"#Inventor")) and prev[-1:] in (b"", b"\n") and not prev.strip():
                        yield tok
                        continue
                    val, encd = _dec(tok[1:])
                    if col is not None:
                        if sum(ch.isalpha() for ch in val) >= 2:
                            col.add("free_text", "vrml.comment", val.strip(), "comment", 0.5)
                        yield tok
                        continue
                    nv = pc.text(val)
                    yield tok if nv == val else b"#" + _enc(nv, encd).replace(b"\n", b" ").replace(b"\r", b" ")
                elif c == b'"':
                    inner = _vrml_unescape(tok[1:-1])
                    val, encd = _dec(inner)
                    is_ref = bool(_VRML_URL_CTX.search(prev[-200:])) or looks_like_reference(val)
                    if col is not None:
                        if is_ref:
                            col.ref(val, ref_kind(val), "vrml.url")
                        elif sum(ch.isalpha() for ch in val) >= 2:
                            col.add("free_text", "vrml.string", val, "string", 0.5)
                        yield tok
                        continue
                    nv = pc.ref(val) if is_ref else pc.text(val)
                    yield tok if nv == val else b'"' + _vrml_escape(_enc(nv, encd)) + b'"'
                else:  # DEF/USE name
                    m = re.match(rb"(DEF|USE)([ \t\r\n,]+)(.*)$", tok, re.S)
                    kw, sp, name = m.groups()
                    val, encd = _dec(name)
                    if col is not None:
                        if kw == b"DEF":
                            col.add("part_name", "vrml.DEF", val, "DEF name", 0.4)
                        yield tok
                        continue
                    nv = pc.text(val)
                    if nv != val:
                        nv = _vrml_id(nv)
                    yield tok if nv == val else kw + sp + _enc(nv, encd)

    def scan(self, ctx, src_path):
        col = ObsCollector(cap=3000)
        for _ in self._iter(src_path, None, col):
            pass
        return ScanResult(observations=col.obs, references=col.refs)

    def rewrite(self, ctx, src_path, dst_path, plan):
        pc = PlanCache(plan, ctx)
        tmp = dst_path + ".part"
        changed = 0
        with open(tmp, "wb", buffering=1 << 20) as out:
            # compare token identity cheaply: _iter yields the original object when unchanged
            for piece in self._iter(src_path, pc, None):
                out.write(piece)
        # determine change by size/hash compare only when needed
        if os.path.getsize(tmp) == os.path.getsize(src_path) and _same_file(tmp, src_path):
            os.unlink(tmp)
            return RewriteResult(status=VERIFIED_UNCHANGED)
        os.replace(tmp, dst_path)
        return RewriteResult(status=REWRITTEN)

    def verify(self, ctx, src_path, dst_path, plan):
        a = _vrml_fp(src_path)
        b = _vrml_fp(dst_path)
        ok = a == b
        return VerifyResult(ok=ok, notes=[] if ok else ["VRML structure differs"])


def _same_file(a, b):
    return _files_equal_from(a, b, 0)


def _vrml_fp(path):
    h = hashlib.sha256()
    n = 0
    names: dict = {}
    defs = []
    with open(path, "rb") as f:
        for k, tok, prev in _vrml_tokens(f):
            if k == "pass":
                h.update(tok)
                continue
            n += 1
            c = tok[:1]
            if c == b"#" or c == b'"':
                h.update(c)
            else:
                kw, _, name = tok.partition(b" ")
                kw = tok[:3]
                # DEF/USE structure: map names to first-seen index so renames are allowed
                # but the DEF/USE pairing must be preserved
                nm = re.split(rb"[ \t\r\n,]+", tok, maxsplit=1)[1]
                idx = names.setdefault(nm, len(names))
                h.update(kw + b"%d" % idx)
    return h.hexdigest(), n


register(StlAdapter())
register(ObjAdapter())
register(MtlAdapter())
register(OffAdapter())
register(VrmlAdapter())
