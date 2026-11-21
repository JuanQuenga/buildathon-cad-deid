"""PostScript / EPS (.ps .eps .epsf .epsi), including DOS binary EPS with
TIFF/WMF previews.  Nothing is executed.

Edited: comment lines (DSC %%Title/%%Creator/%%For/%%Author/... and ordinary
% comments) and literal strings ``( ... )`` outside data regions.  Data regions
are copied verbatim: ``%%BeginData``/``%%BeginBinary`` blocks with byte counts,
inline ASCII85/hex image data after ``currentfile``, eexec-encrypted font
sections, and ``%%BeginPreview`` bitmaps.  Binary EPS: the PostScript section is
rewritten, preview sections are copied unchanged and header offsets updated.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION, COPIED_UNSUPPORTED)
from deid.formats._neutral import PlanCache, ObsCollector, looks_like_reference, ref_kind

DOS_EPS = b"\xc5\xd0\xd3\xc6"

# Tokens: comment to EOL, literal string (balanced parens, escapes), or a data-region opener.
_TOK = re.compile(
    rb"%[^\r\n]*|\((?:[^()\\]|\\.|\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\))*\)|"
    rb"currentfile\b|eexec\b|<~", re.S)
_COMMENT_KEYS_PERSON = (b"%%For", b"%%Author", b"%%Routing")
_SKIP_STARTS = (b"%%BeginData", b"%%BeginBinary", b"%%BeginPreview", b"%%BeginPhotoshop",
                b"%BeginPhotoshop", b"%%BeginICCProfile", b"%ImageData", b"%AI9_DataStream",
                b"%AI12_CompressedData", b"%AI24_ZStandard_Data", b"%%BeginDocument")
_END_FOR = {b"%%BeginData": b"%%EndData", b"%%BeginBinary": b"%%EndBinary", b"%%BeginPreview": b"%%EndPreview",
            b"%%BeginPhotoshop": b"%%EndPhotoshop", b"%BeginPhotoshop": b"%EndPhotoshop",
            b"%%BeginICCProfile": b"%%EndICCProfile", b"%ImageData": b"%%EndData",
            b"%AI9_DataStream": b"%AI9_PrivateDataEnd", b"%AI12_CompressedData": b"%AI9_PrivateDataEnd",
            b"%AI24_ZStandard_Data": b"%AI9_PrivateDataEnd"}
_MARKER = re.compile(rb"%+[!A-Za-z0-9_\-+.]*\s*$")
_NEXT_DSC = re.compile(rb"[\r\n]%[%A]")
_ESC = re.compile(rb"\\([nrtbf()\\]|[0-7]{1,3}|\r\n|\n|\r)")


def _ps_unescape(b: bytes) -> bytes:
    def r(m):
        g = m.group(1)
        if g[:1].isdigit():
            return bytes([int(g, 8) & 0xFF])
        return {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")",
                b"\\": b"\\"}.get(g, b"")
    return _ESC.sub(r, b)


def _ps_escape(b: bytes) -> bytes:
    out = bytearray()
    for c in b:
        if c in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([c])
        elif 32 <= c < 127:
            out.append(c)
        else:
            out += b"\\%03o" % c
    return bytes(out)


def _dec(b: bytes):
    try:
        return b.decode("ascii"), "ascii"
    except UnicodeDecodeError:
        try:
            return b.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            return b.decode("latin-1"), "latin-1"


def _enc(s: str, e: str) -> bytes:
    if e == "ascii":
        try:
            return s.encode("ascii")
        except UnicodeEncodeError:
            return s.encode("latin-1", "replace")
    return s.encode(e, "replace")


def _walk(data: bytes, fn):
    """Yield output pieces; fn(kind, raw_bytes, ctx) -> replacement bytes or None.
    kind: 'comment' (whole line incl. %), 'string' (content without parens)."""
    pos = 0
    n = len(data)
    i = 0
    depth = 0   # procedure brace depth: currentfile/eexec inside {...} is a definition, not data
    while True:
        m = _TOK.search(data, i)
        if m is None:
            break
        s, e = m.span()
        if s > i:
            gap = data[i:s]
            depth = max(0, depth + gap.count(b"{") - gap.count(b"}"))
        tok = m.group(0)
        c = tok[:1]
        if depth > 0 and tok in (b"currentfile", b"eexec"):
            i = e
            continue
        if c == b"%":
            # data regions announced by DSC comments
            for st in _SKIP_STARTS:
                if tok.startswith(st):
                    end_kw = _END_FOR.get(st)
                    j = -1
                    if st in (b"%%BeginData", b"%%BeginBinary"):
                        mm = re.match(rb"%%Begin(?:Data|Binary):\s*(\d+)(?:\s+\w+)?(?:\s+(Bytes|Lines))?", tok)
                        if mm and (mm.group(2) or b"Bytes") == b"Bytes":
                            j = e + int(mm.group(1))
                            # skip line terminator after the comment
                            k = e
                            if data[k:k + 2] == b"\r\n":
                                k += 2
                            elif data[k:k + 1] in (b"\r", b"\n"):
                                k += 1
                            j = k + int(mm.group(1))
                    if j < 0 and end_kw and st != b"%%BeginDocument":
                        j = data.find(end_kw, e)
                    if st == b"%%BeginDocument":
                        j = -2   # embedded document: process its contents normally
                    if j == -1:
                        j = n
                    if j >= 0:
                        i = max(j, e)
                        break
            else:
                new = fn("comment", tok, None)
                if new is not None and new != tok:
                    yield data[pos:s]
                    yield new
                    pos = e
                i = e
                continue
            if i != e and i > s:
                continue
            i = e
            continue
        if tok == b"currentfile":
            # inline image / binary data follows the operator that reads it: skip to the end of the
            # data (ASCII85 '~>', or the next %%EndData/%%EndBinary/ 'cleartomark' for eexec) when we
            # can recognise it; otherwise skip conservatively to the next line that starts with '%%'
            k = data.find(b"\n", e)
            seg = data[e:e + 200]
            if b"eexec" in seg:
                j = data.find(b"cleartomark", e)
                i = n if j < 0 else j
                continue
            if b"ASCII85Decode" in seg or b"A85" in data[max(0, s - 200):s + 200]:
                j = data.find(b"~>", e)
                i = n if j < 0 else j + 2
                continue
            if any(x in data[e:e + 300] for x in (b"image", b"readstring", b"readhexstring", b"filter",
                                                     b"colorimage", b"imagemask", b"readline")):
                mm = _NEXT_DSC.search(data, e)
                i = n if mm is None else mm.start() + 1
                continue
            i = e
            continue
        if tok == b"eexec":
            j = data.find(b"cleartomark", e)
            i = n if j < 0 else j
            continue
        if tok == b"<~":
            j = data.find(b"~>", e)
            i = n if j < 0 else j + 2
            continue
        # literal string
        new = fn("string", tok[1:-1], data[max(0, s - 64):s])
        if new is not None and new != tok[1:-1]:
            yield data[pos:s]
            yield b"(" + new + b")"
            pos = e
        i = e
    yield data[pos:]


def _comment_value(tok: bytes):
    """Split a comment line into (prefix, value).  DSC '%%Key: value'; plain '% text'."""
    m = re.match(rb"(%%?[A-Za-z][\w+\-]*:\s*|%+\s*)(.*)$", tok, re.S)
    if not m:
        return tok[:1], tok[1:]
    return m.group(1), m.group(2)


def _strip_parens(v: bytes):
    if v.startswith(b"(") and v.endswith(b")"):
        return b"(", _ps_unescape(v[1:-1]), b")"
    return b"", v, b""


class PsAdapter(Adapter):
    name = "ps"
    version = "5"
    priority = 10
    magics = ("ps", "eps", "eps_binary")
    extensions = (".ps", ".eps", ".epsf", ".epsi", ".ai")
    path_independent = True
    timeout_s = 900.0

    def matches(self, ctx, head):
        if ctx.magic in self.magics:
            return True
        return head.lstrip().startswith(b"%!")

    @staticmethod
    def _split(data: bytes):
        if data[:4] == DOS_EPS and len(data) >= 30:
            ps_off, ps_len, wmf_off, wmf_len, tif_off, tif_len, cks = struct.unpack_from("<IIIIIIH", data, 4)
            if ps_off + ps_len > len(data):
                raise ValueError("binary EPS PostScript section out of range")
            return (ps_off, ps_len, wmf_off, wmf_len, tif_off, tif_len, cks)
        return None

    def _process(self, data: bytes, fn) -> bytes:
        return b"".join(_walk(data, fn))

    def scan(self, ctx, src_path):
        data = open(src_path, "rb").read()
        if not data.strip():
            return ScanResult(status="source_exception", notes=["empty file"])
        col = ObsCollector(cap=3000)
        try:
            hdr = self._split(data)
        except ValueError as e:
            return ScanResult(status="source_exception", notes=[str(e)])
        ps = data[hdr[0]:hdr[0] + hdr[1]] if hdr else data
        nstr = 0

        def fn(kind, raw, prev):
            nonlocal nstr
            if kind == "comment":
                if _MARKER.match(raw):
                    return None
                pre, val = _comment_value(raw)
                _, v, _ = _strip_parens(val)
                t, _ = _dec(v)
                t = t.strip()
                if t and sum(ch.isalpha() for ch in t) >= 2:
                    key = pre.strip().rstrip(b":").decode("latin-1")
                    kind_ = "person" if pre.startswith(_COMMENT_KEYS_PERSON) else "free_text"
                    col.add(kind_, f"ps.comment.{key}" if key.startswith("%%") else "ps.comment", t, key, 0.6)
                return None
            nstr += 1
            t, _ = _dec(_ps_unescape(raw))
            if sum(ch.isalpha() for ch in t) >= 2 and len(t) < 2000 and not t.startswith(("%", "/")):
                if looks_like_reference(t):
                    col.ref(t, ref_kind(t), "ps.string")
                col.add("free_text", "ps.string", t, "string", 0.4)
            return None

        for _ in _walk(ps, fn):
            pass
        notes = ["binary EPS with preview"] if hdr else []
        return ScanResult(observations=col.obs, references=col.refs, fingerprint={"strings": nstr}, notes=notes)

    def rewrite(self, ctx, src_path, dst_path, plan):
        data = open(src_path, "rb").read()
        if not data.strip():
            return RewriteResult(status=SOURCE_EXCEPTION, notes=["empty file"])
        try:
            hdr = self._split(data)
        except ValueError as e:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[str(e)])
        pc = PlanCache(plan, ctx)
        nchg = 0

        def fn(kind, raw, prev):
            nonlocal nchg
            if kind == "comment":
                if _MARKER.match(raw):
                    return None   # structural marker such as %AI9_PrivateDataEnd / %%EndComments
                pre, val = _comment_value(raw)
                if not val.strip():
                    return None
                lp, v, rp = _strip_parens(val)
                t, e = _dec(v)
                k = "person" if pre.startswith(_COMMENT_KEYS_PERSON) else None
                nt = pc.sub(t, k) if k else pc.text(t)
                if nt == t:
                    return None
                nb = _enc(nt, e).replace(b"\r", b" ").replace(b"\n", b" ")
                if lp:
                    nb = _ps_escape(nb)
                nchg += 1
                return pre + lp + nb + rp
            v = _ps_unescape(raw)
            t, e = _dec(v)
            if not any(ch.isalpha() for ch in t) or t.startswith(("%", "/")):
                return None   # DSC keywords / names compared by procedures: code, not text
            nt = pc.ref(t) if looks_like_reference(t) else pc.text(t)
            if nt == t:
                return None
            nchg += 1
            return _ps_escape(_enc(nt, e))

        if hdr is None:
            out = self._process(data, fn)
        else:
            ps_off, ps_len, wmf_off, wmf_len, tif_off, tif_len, cks = hdr
            ps = self._process(data[ps_off:ps_off + ps_len], fn)
            out = self._rebuild_dos(data, hdr, ps)
        if out == data:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        with open(dst_path, "wb") as f:
            f.write(out)
        return RewriteResult(status=REWRITTEN, counts={"replaced": nchg})

    @staticmethod
    def _rebuild_dos(data, hdr, ps):
        ps_off, ps_len, wmf_off, wmf_len, tif_off, tif_len, cks = hdr
        secs = [("ps", ps_off, ps_len, ps)]
        if wmf_len:
            secs.append(("wmf", wmf_off, wmf_len, data[wmf_off:wmf_off + wmf_len]))
        if tif_len:
            secs.append(("tif", tif_off, tif_len, data[tif_off:tif_off + tif_len]))
        secs.sort(key=lambda x: x[1])
        pos = 30
        newoff = {}
        body = bytearray()
        # keep any gap between the header and the first section
        first = secs[0][1]
        gap = data[30:first] if first >= 30 else b""
        body += gap
        pos += len(gap)
        for name, off, ln, payload in secs:
            newoff[name] = (pos, len(payload))
            body += payload
            pos += len(payload)
        h = bytearray(data[:30])
        struct.pack_into("<II", h, 4, *newoff["ps"])
        if "wmf" in newoff:
            struct.pack_into("<II", h, 12, *newoff["wmf"])
        if "tif" in newoff:
            struct.pack_into("<II", h, 20, *newoff["tif"])
        struct.pack_into("<H", h, 28, 0xFFFF)
        return bytes(h) + bytes(body)

    def verify(self, ctx, src_path, dst_path, plan):
        try:
            a = ps_fingerprint(open(src_path, "rb").read())
            b = ps_fingerprint(open(dst_path, "rb").read())
        except Exception as e:
            return VerifyResult(ok=False, notes=[f"parse error {type(e).__name__}"])
        notes = [f"{k} differs" for k in a if a[k] != b[k]]
        return VerifyResult(ok=not notes, notes=notes, fingerprint=b)


def ps_fingerprint(data: bytes) -> dict:
    """Hash of all bytes outside edited comments/strings (comment keys kept), plus counts.
    Binary EPS: preview payloads hashed separately."""
    extra = {}
    if data[:4] == DOS_EPS:
        ps_off, ps_len, wmf_off, wmf_len, tif_off, tif_len, cks = struct.unpack_from("<IIIIIIH", data, 4)
        extra["wmf"] = hashlib.sha256(data[wmf_off:wmf_off + wmf_len]).hexdigest() if wmf_len else ""
        extra["tif"] = hashlib.sha256(data[tif_off:tif_off + tif_len]).hexdigest() if tif_len else ""
        data = data[ps_off:ps_off + ps_len]
    h = hashlib.sha256()
    counts = {"comment": 0, "string": 0}

    def fn(kind, raw, prev):
        counts[kind] += 1
        if kind == "comment":
            pre, _ = _comment_value(raw)
            return b"\0" + pre.split(b":")[0].rstrip() if pre.startswith(b"%%") else b"%\0"
        return b"\0"

    for piece in _walk(data, fn):
        h.update(piece)
    return {"masked_sha256": h.hexdigest(), "strings": counts["string"], "comments": counts["comment"], **extra}


register(PsAdapter())
