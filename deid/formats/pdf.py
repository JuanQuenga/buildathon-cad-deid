"""PDF adapter (owner: docs-native).

What it covers
--------------
* Document Info dictionary and every XMP packet (document, page, image level).
* Every text string anywhere in the object graph (outlines, annotations, form
  fields, link URIs, file specifications, optional-content names, structure
  tree /Alt /ActualText, JavaScript, page labels ...) through ``plan``.
* Page content streams, Form XObjects and annotation appearance streams:
  a small text interpreter decodes text-showing operators with the font's
  ToUnicode / Encoding, positions each glyph, groups glyphs into lines and asks
  ``plan.replace_text`` per line.  Matched glyphs are removed (replaced by an
  equivalent TJ kerning displacement, so the rest of the line does not move)
  and the pseudonym is drawn at the first removed glyph's position with a
  standard-14 Helvetica (WinAnsi) overlay font: subset fonts usually lack
  the glyphs a pseudonym needs, Helvetica never does and stays extractable.
* Embedded files (attachments): payloads are recursed through the registry
  with ``ctx.dispatch_rewrite``; their names follow ``plan.map_path``.
* Application private data (/PieceInfo) and page thumbnails (/Thumb) are
  dropped (they can hold a full copy of the authoring document / an image of
  the original page and are not needed to view or print).
* Raster images: optional OCR pass (``DEID_PDF_OCR=1``) via deid.formats._ocr.
* Saved with a full rewrite (no incremental updates, unreferenced objects
  dropped), deterministic /ID.

verify(): reopen with qpdf, same page count and boxes, every page renders
(poppler, low resolution) and text still extracts.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import difflib
from typing import Optional

import pikepdf
from pikepdf import Name, String, Array, Dictionary, Stream, Operator, ContentStreamInstruction

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult, Observation,
                      Reference, REWRITTEN, VERIFIED_UNCHANGED, COPIED_UNSUPPORTED, SOURCE_EXCEPTION)
from deid.formats import _nativeutil as nu

try:  # pdfminer.six (MIT): encodings, glyph names, standard-14 metrics
    from pdfminer.latin_enc import ENCODING as _LATIN_ENC
    from pdfminer.glyphlist import glyphname2unicode as _G2U
    from pdfminer.fontmetrics import FONT_METRICS as _FM
except Exception:  # pragma: no cover
    _LATIN_ENC, _G2U, _FM = [], {}, {}

OVERLAY_FONT = Name("/DeidHelv")
_HELV_W = _FM.get("Helvetica", ({}, {}))[1]


def _enc_table(col: int) -> dict:
    t = {}
    for row in _LATIN_ENC:
        name, code = row[0], row[col]
        if code is not None:
            t[code] = name
    return t


_STD = _enc_table(1)
_MAC = _enc_table(2)
_WIN = _enc_table(3)
_PDFDOC = _enc_table(4)
_BASE_ENC = {"/StandardEncoding": _STD, "/MacRomanEncoding": _MAC, "/WinAnsiEncoding": _WIN,
             "/PDFDocEncoding": _PDFDOC}


def _glyph_uni(name: str) -> str:
    if name in _G2U:
        return _G2U[name]
    m = re.match(r"^uni([0-9A-Fa-f]{4,})", name)
    if m:
        try:
            h = m.group(1)
            return "".join(chr(int(h[i:i + 4], 16)) for i in range(0, len(h) - len(h) % 4, 4))
        except ValueError:
            pass
    m = re.match(r"^u([0-9A-Fa-f]{4,6})$", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            pass
    base = name.split(".")[0].split("_")[0]
    if base != name and base in _G2U:
        return _G2U[base]
    return ""


# ---------------------------------------------------------------------------
# CMaps
# ---------------------------------------------------------------------------
_HEX = re.compile(rb"<([0-9A-Fa-f\s]*)>")


def _hexb(tok: bytes) -> bytes:
    h = re.sub(rb"\s", b"", tok)
    if len(h) % 2:
        h += b"0"
    return bytes.fromhex(h.decode())


def _utf16be(b: bytes) -> str:
    try:
        return b.decode("utf-16-be", errors="replace")
    except Exception:
        return ""


def parse_tounicode(data: bytes):
    """Return (mapping code_int->str, codespace list[(nbytes, lo, hi)])."""
    m: dict = {}
    spaces = []
    for blk in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        toks = _HEX.findall(blk.group(1))
        for i in range(0, len(toks) - 1, 2):
            lo, hi = _hexb(toks[i]), _hexb(toks[i + 1])
            spaces.append((len(lo), int.from_bytes(lo, "big"), int.from_bytes(hi, "big")))
    for blk in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        toks = _HEX.findall(blk.group(1))
        for i in range(0, len(toks) - 1, 2):
            src = _hexb(toks[i])
            m[int.from_bytes(src, "big")] = _utf16be(_hexb(toks[i + 1]))
    for blk in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        body = blk.group(1)
        for ent in re.finditer(rb"<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>\s*(\[[^\]]*\]|<[0-9A-Fa-f\s]*>)", body):
            lo = int.from_bytes(_hexb(ent.group(1)), "big")
            hi = int.from_bytes(_hexb(ent.group(2)), "big")
            if hi < lo or hi - lo > 65536:
                continue
            dst = ent.group(3)
            if dst.startswith(b"["):
                arr = _HEX.findall(dst)
                for k, t in enumerate(arr):
                    if lo + k > hi:
                        break
                    m[lo + k] = _utf16be(_hexb(t))
            else:
                d = _hexb(dst[1:-1])
                if not d:
                    continue
                base = int.from_bytes(d, "big")
                ln = len(d)
                for k in range(hi - lo + 1):
                    v = base + k
                    try:
                        m[lo + k] = _utf16be(v.to_bytes(ln, "big"))
                    except OverflowError:
                        break
    return m, spaces


def parse_cid_cmap(data: bytes):
    """Embedded (non-predefined) Type0 encoding CMap: codespace only is needed
    to split byte strings into codes; the code->CID map is used for widths."""
    spaces = []
    for blk in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        toks = _HEX.findall(blk.group(1))
        for i in range(0, len(toks) - 1, 2):
            lo, hi = _hexb(toks[i]), _hexb(toks[i + 1])
            spaces.append((len(lo), int.from_bytes(lo, "big"), int.from_bytes(hi, "big")))
    cid: dict = {}
    for blk in re.finditer(rb"begincidchar(.*?)endcidchar", data, re.S):
        for ent in re.finditer(rb"<([0-9A-Fa-f\s]*)>\s*(\d+)", blk.group(1)):
            cid[int.from_bytes(_hexb(ent.group(1)), "big")] = int(ent.group(2))
    for blk in re.finditer(rb"begincidrange(.*?)endcidrange", data, re.S):
        for ent in re.finditer(rb"<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>\s*(\d+)", blk.group(1)):
            lo = int.from_bytes(_hexb(ent.group(1)), "big")
            hi = int.from_bytes(_hexb(ent.group(2)), "big")
            if 0 <= hi - lo <= 65536:
                for k in range(hi - lo + 1):
                    cid[lo + k] = int(ent.group(3)) + k
    return spaces, cid


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
class FontInfo:
    """Decoding + metrics for one font dictionary."""

    def __init__(self, fd):
        self.ok = True
        self.vertical = False
        self.two_byte = False
        self.spaces = None          # codespace ranges for Type0
        self.cidmap = None          # code -> CID (embedded CMap)
        self.tou = {}
        self.enc = {}
        self.widths = {}
        self.default_w = 0.0
        self.scale = 0.001          # glyph space -> text space
        self.simple = True
        try:
            self._load(fd)
        except Exception:
            self.ok = False

    def _load(self, fd):
        sub = str(fd.get("/Subtype", ""))
        tu = fd.get("/ToUnicode")
        if isinstance(tu, Stream):
            try:
                self.tou, sp = parse_tounicode(tu.read_bytes())
            except Exception:
                self.tou, sp = {}, []
        if sub == "/Type0":
            self.simple = False
            enc = fd.get("/Encoding")
            if isinstance(enc, Name):
                en = str(enc)
                if en in ("/Identity-H", "/Identity-V"):
                    self.two_byte = True
                    self.vertical = en.endswith("V")
                elif en.endswith("-V"):
                    self.vertical = True
                    self.two_byte = True
                elif "UCS2" in en or "UTF16" in en or en.startswith("/Uni"):
                    self.two_byte = True
                else:
                    # predefined multibyte CMaps (RKSJ, GBK, ...): codespace unknown here
                    self.ok = False
            elif isinstance(enc, Stream):
                sp2, cmap = parse_cid_cmap(enc.read_bytes())
                if sp2:
                    self.spaces = sorted(sp2)
                    self.cidmap = cmap
                else:
                    self.two_byte = True
                wm = enc.get("/WMode")
                if wm is not None and int(wm) == 1:
                    self.vertical = True
            else:
                self.two_byte = True
            desc = fd.get("/DescendantFonts")
            dfont = desc[0] if isinstance(desc, Array) and len(desc) else None
            self.default_w = 1000.0
            if isinstance(dfont, Dictionary):
                if dfont.get("/DW") is not None:
                    self.default_w = float(dfont.get("/DW"))
                w = dfont.get("/W")
                if isinstance(w, Array):
                    i = 0
                    items = list(w)
                    while i < len(items):
                        try:
                            c0 = int(items[i])
                            nxt = items[i + 1]
                            if isinstance(nxt, Array):
                                for k, v in enumerate(nxt):
                                    self.widths[c0 + k] = float(v)
                                i += 2
                            else:
                                c1 = int(nxt)
                                v = float(items[i + 2])
                                if c1 - c0 <= 65536:
                                    for c in range(c0, c1 + 1):
                                        self.widths[c] = v
                                i += 3
                        except (IndexError, TypeError, ValueError):
                            break
            return
        # simple fonts: Type1, MMType1, TrueType, Type3
        base = _STD
        enc = fd.get("/Encoding")
        if sub == "/TrueType" and enc is None:
            base = _WIN
        diffs = None
        if isinstance(enc, Name):
            base = _BASE_ENC.get(str(enc), base)
        elif isinstance(enc, Dictionary):
            be = enc.get("/BaseEncoding")
            if isinstance(be, Name):
                base = _BASE_ENC.get(str(be), base)
            diffs = enc.get("/Differences")
        self.enc = {c: _glyph_uni(n) for c, n in base.items()}
        if isinstance(diffs, Array):
            code = 0
            for it in diffs:
                if isinstance(it, Name):
                    self.enc[code] = _glyph_uni(str(it)[1:])
                    code += 1
                else:
                    try:
                        code = int(it)
                    except (TypeError, ValueError):
                        pass
        fc = fd.get("/FirstChar")
        ws = fd.get("/Widths")
        if fc is not None and isinstance(ws, Array):
            fc = int(fc)
            for k, v in enumerate(ws):
                try:
                    self.widths[fc + k] = float(v)
                except (TypeError, ValueError):
                    pass
        fdesc = fd.get("/FontDescriptor")
        if isinstance(fdesc, Dictionary) and fdesc.get("/MissingWidth") is not None:
            self.default_w = float(fdesc.get("/MissingWidth"))
        if sub == "/Type3":
            fm = fd.get("/FontMatrix")
            try:
                self.scale = float(fm[0])
            except Exception:
                self.scale = 0.001
        elif not self.widths:
            bf = str(fd.get("/BaseFont", ""))[1:]
            bf = bf.split("+", 1)[-1]
            met = _FM.get(bf) or _FM.get(bf.replace(",", "-")) or _FM.get("Helvetica")
            if met:
                wmap = met[1]
                for c, u in self.enc.items():
                    if u and u in wmap:
                        self.widths[c] = float(wmap[u])
                self.default_w = 500.0

    # -- decoding --------------------------------------------------------
    def split(self, data: bytes):
        """Yield (code_int, nbytes)."""
        if self.simple:
            for b in data:
                yield b, 1
            return
        if self.spaces:
            i, n = 0, len(data)
            while i < n:
                for nb, lo, hi in self.spaces:
                    if i + nb <= n:
                        v = int.from_bytes(data[i:i + nb], "big")
                        if lo <= v <= hi and nb == len(lo.to_bytes(nb, "big")):
                            yield v, nb
                            i += nb
                            break
                else:
                    yield data[i], 1
                    i += 1
            return
        for i in range(0, len(data) - 1, 2):
            yield (data[i] << 8) | data[i + 1], 2

    def uni(self, code: int) -> str:
        u = self.tou.get(code)
        if u is not None:
            return u
        if self.simple:
            u = self.enc.get(code)
            if u:
                return u
            return chr(code) if 32 <= code < 127 else "�"
        return "�"

    def width(self, code: int) -> float:
        if not self.simple and self.cidmap is not None:
            code = self.cidmap.get(code, code)
        w = self.widths.get(code)
        if w is None:
            w = self.default_w
        return w * self.scale


# ---------------------------------------------------------------------------
# Content-stream text interpreter
# ---------------------------------------------------------------------------
def _mul(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


_ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
SHOW_OPS = {"Tj", "TJ", "'", '"'}


class Glyph:
    __slots__ = ("ins", "elem", "code", "nb", "ch", "x0", "y0", "x1", "y1", "size", "font", "fsize",
                 "adv_k", "vertical")

    def __init__(self, ins, elem, code, nb, ch, x0, y0, x1, y1, size, font, fsize, adv_k, vertical):
        self.ins, self.elem, self.code, self.nb, self.ch = ins, elem, code, nb, ch
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.size, self.font, self.fsize, self.adv_k, self.vertical = size, font, fsize, adv_k, vertical


class _GS:
    __slots__ = ("ctm", "font", "fsize", "tc", "tw", "th", "tl", "rise")

    def __init__(self):
        self.ctm = _ID
        self.font = None
        self.fsize = 0.0
        self.tc = 0.0
        self.tw = 0.0
        self.th = 1.0
        self.tl = 0.0
        self.rise = 0.0

    def copy(self):
        g = _GS()
        g.ctm, g.font, g.fsize, g.tc, g.tw, g.th, g.tl, g.rise = (
            self.ctm, self.font, self.fsize, self.tc, self.tw, self.th, self.tl, self.rise)
        return g


def _num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


class TextWalker:
    """Run text operators of an instruction list; collect glyphs.

    ``fonts``: callable(name)->FontInfo|None.  ``on_form``: callable(name, ctm)
    invoked for ``Do`` so forms can be handled separately.
    """

    def __init__(self, instrs, fonts, on_form=None, ctm=_ID):
        self.instrs = instrs
        self.fonts = fonts
        self.on_form = on_form
        self.glyphs: list = []
        self.start_ctm = ctm
        # per instruction: (font_name, fsize, tc, tw, th) at show time, for rewriting
        self.show_state: dict = {}

    def run(self):
        gs = _GS()
        gs.ctm = self.start_ctm
        stack = []
        tm = tlm = _ID
        for idx, ins in enumerate(self.instrs):
            if not isinstance(ins, ContentStreamInstruction):
                continue
            op = str(ins.operator)
            ops = ins.operands
            if op == "q":
                stack.append(gs.copy())
            elif op == "Q":
                if stack:
                    gs = stack.pop()
            elif op == "cm" and len(ops) == 6:
                gs.ctm = _mul(tuple(_num(v) for v in ops), gs.ctm)
            elif op == "BT":
                tm = tlm = _ID
            elif op == "Tf" and len(ops) == 2:
                gs.font = ops[0]
                gs.fsize = _num(ops[1])
            elif op == "Tc" and ops:
                gs.tc = _num(ops[0])
            elif op == "Tw" and ops:
                gs.tw = _num(ops[0])
            elif op == "Tz" and ops:
                gs.th = _num(ops[0], 100.0) / 100.0
            elif op == "TL" and ops:
                gs.tl = _num(ops[0])
            elif op == "Ts" and ops:
                gs.rise = _num(ops[0])
            elif op == "Td" and len(ops) == 2:
                tlm = _mul((1, 0, 0, 1, _num(ops[0]), _num(ops[1])), tlm)
                tm = tlm
            elif op == "TD" and len(ops) == 2:
                gs.tl = -_num(ops[1])
                tlm = _mul((1, 0, 0, 1, _num(ops[0]), _num(ops[1])), tlm)
                tm = tlm
            elif op == "Tm" and len(ops) == 6:
                tlm = tm = tuple(_num(v) for v in ops)
            elif op == "T*":
                tlm = _mul((1, 0, 0, 1, 0, -gs.tl), tlm)
                tm = tlm
            elif op in SHOW_OPS:
                if op == "'" or op == '"':
                    if op == '"' and len(ops) == 3:
                        gs.tw = _num(ops[0])
                        gs.tc = _num(ops[1])
                    tlm = _mul((1, 0, 0, 1, 0, -gs.tl), tlm)
                    tm = tlm
                tm = self._show(idx, ins, op, gs, tm)
            elif op == "Do" and ops and self.on_form is not None:
                try:
                    self.on_form(ops[0], gs.ctm)
                except Exception:
                    pass
        return self

    def _show(self, idx, ins, op, gs, tm):
        font = self.fonts(gs.font) if gs.font is not None else None
        self.show_state[idx] = (gs.font, gs.fsize, gs.tc, gs.tw, gs.th)
        ops = ins.operands
        if op == "TJ":
            elems = list(ops[0]) if ops and isinstance(ops[0], Array) else []
        else:
            elems = [ops[-1]] if ops else []
        fs, th, tc, tw = gs.fsize, gs.th, gs.tc, gs.tw
        for ei, el in enumerate(elems):
            if isinstance(el, String):
                data = bytes(el)
                if font is None or not font.ok:
                    # unknown font: still advance roughly so later positions are sane
                    tm = _mul((1, 0, 0, 1, len(data) * 0.5 * fs * th, 0), tm)
                    continue
                for code, nb in font.split(data):
                    w0 = font.width(code)
                    space = tw if (nb == 1 and code == 32) else 0.0
                    if font.vertical:
                        tx = 0.0
                        ty = -(w0 * fs) + tc + space  # crude: vertical fonts are not rewritten
                    else:
                        tx = (w0 * fs + tc + space) * th
                        ty = 0.0
                    trm = _mul(tm, gs.ctm)
                    x0 = trm[4] + gs.rise * trm[2]
                    y0 = trm[5] + gs.rise * trm[3]
                    end = _mul((1, 0, 0, 1, tx, ty), tm)
                    trm2 = _mul(end, gs.ctm)
                    size = abs(fs) * math.sqrt(abs(trm[0] * trm[3] - trm[1] * trm[2])) or 1.0
                    # adv_k: TJ number equivalent to this glyph's advance
                    adv_k = -(tx / (fs * th) * 1000.0) if fs and th and not font.vertical else None
                    self.glyphs.append(Glyph(idx, ei, code, nb, font.uni(code), x0, y0,
                                             trm2[4] + gs.rise * trm2[2], trm2[5] + gs.rise * trm2[3],
                                             size, gs.font, fs, adv_k, font.vertical))
                    tm = end
            else:
                k = _num(el)
                if font is not None and font.vertical:
                    tm = _mul((1, 0, 0, 1, 0, -k / 1000.0 * fs), tm)
                else:
                    tm = _mul((1, 0, 0, 1, -k / 1000.0 * fs * th, 0), tm)
        return tm


def group_lines(glyphs):
    """Group glyphs (content order) into lines: returns list of lists of
    (char, glyph_index or None).  Separators (space/newline) carry None."""
    lines = []
    cur = []
    prev = None
    for gi, g in enumerate(glyphs):
        if prev is not None:
            dx, dy = g.x0 - prev.x1, g.y0 - prev.y1
            ax, ay = prev.x1 - prev.x0, prev.y1 - prev.y0
            al = math.hypot(ax, ay)
            if al < 1e-9:
                ax, ay, al = 1.0, 0.0, 1.0
            ux, uy = ax / al, ay / al
            along = dx * ux + dy * uy
            perp = abs(-dx * uy + dy * ux)
            sz = max(prev.size, g.size, 1e-6)
            if perp > 0.45 * sz or along < -1.0 * sz or along > 6.0 * sz:
                if cur:
                    lines.append(cur)
                cur = []
            elif along > 0.22 * sz and g.ch != " " and prev.ch != " ":
                cur.append((" ", None))
        for c in (g.ch or "�"):
            cur.append((c, gi))
        prev = g
    if cur:
        lines.append(cur)
    return lines


_TOK = re.compile(r"\w+|\s+|[^\w\s]", re.U)


def _tokens(s):
    return _TOK.findall(s)


def line_edits(line, new_text):
    """Diff a line (list of (char, gi)) against its replacement text.  Returns a
    list of (glyph_indices_removed, insert_text, anchor) where anchor is a glyph
    index (draw before it) or ("after"|"before", gi) for pure insertions.
    Neighbouring changed words separated only by whitespace are merged so a
    multi-word pseudonym is drawn as one run."""
    old_text = "".join(c for c, _ in line)
    ot, nt = _tokens(old_text), _tokens(new_text)
    offs = []
    p = 0
    for t in ot:
        offs.append(p)
        p += len(t)
    offs.append(p)
    sm = difflib.SequenceMatcher(None, ot, nt, autojunk=False)
    ops = [list(o) for o in sm.get_opcodes()]
    merged = []
    for o in ops:
        if o[0] == "equal":
            merged.append(o)
            continue
        # merge with previous change if only whitespace 'equal' tokens separate them
        if (len(merged) >= 2 and merged[-1][0] == "equal" and merged[-2][0] != "equal"
                and all(not t.strip() for t in ot[merged[-1][1]:merged[-1][2]])):
            eq = merged.pop()
            prev = merged[-1]
            prev[0] = "replace"
            prev[2] = o[2]
            prev[4] = o[4]
            continue
        if merged and merged[-1][0] != "equal":
            prev = merged[-1]
            prev[0] = "replace"
            prev[2] = o[2]
            prev[4] = o[4]
            continue
        merged.append(o)
    edits = []
    for tag, i1, i2, j1, j2 in merged:
        if tag == "equal":
            continue
        a, b = offs[i1], offs[i2]
        ins = "".join(nt[j1:j2])
        gis = []
        for c, gi in line[a:b]:
            if gi is not None and (not gis or gis[-1] != gi):
                gis.append(gi)
        if gis:
            # keep surrounding whitespace glyphs out of the drawn text
            edits.append((gis, ins.strip() if ins.strip() else ins, gis[0]))
        elif ins.strip():
            anchor = None
            for c, gi in reversed(line[:a]):
                if gi is not None:
                    anchor = ("after", gi)
                    break
            if anchor is None:
                for c, gi in line[b:]:
                    if gi is not None:
                        anchor = ("before", gi)
                        break
            if anchor is not None:
                edits.append(([], ins, anchor))
    return edits


def _encode_winansi(text: str) -> bytes:
    out = bytearray()
    for ch in text:
        try:
            b = ch.encode("cp1252")
        except UnicodeEncodeError:
            b = b"?"
        out += b
    return bytes(out)


def _helv_width(text: str) -> float:
    return sum(float(_HELV_W.get(c, 556)) for c in text)


class StreamRewriter:
    """Rewrite one content stream (page, form or appearance)."""

    def __init__(self, pdf, instrs, resources, plan, on_form=None, ctm=_ID, font_cache=None):
        self.pdf = pdf
        self.instrs = instrs
        self.resources = resources
        self.plan = plan
        self.font_cache = font_cache if font_cache is not None else {}
        self.on_form = on_form
        self.ctm = ctm
        self.replaced = 0
        self.unrewritable = 0
        self.used_overlay = False

    def font(self, name):
        try:
            fd = self.resources.get("/Font", {}).get(name) if self.resources is not None else None
        except Exception:
            fd = None
        if fd is None:
            return None
        key = fd.objgen if fd.is_indirect else id(fd)
        fi = self.font_cache.get(key)
        if fi is None:
            fi = FontInfo(fd)
            self.font_cache[key] = fi
        return fi

    def walk(self):
        w = TextWalker(self.instrs, self.font, self.on_form, self.ctm).run()
        self.walker = w
        return w

    def compute_edits(self):
        w = self.walker
        lines = group_lines(w.glyphs)
        all_edits = []
        for line in lines:
            old = "".join(c for c, _ in line)
            if not old.strip():
                continue
            new = self.plan.replace_text(old)
            if new == old:
                continue
            all_edits.extend(line_edits(line, new))
        return all_edits

    def apply(self, edits):
        """Apply edits; returns new instruction list (or None if nothing)."""
        if not edits:
            return None
        g = self.walker.glyphs
        removed = set()
        inserts = {}   # gi -> list of (position 'before'|'after', text)
        for gis, text, anchor in edits:
            if any(g[i].vertical or g[i].adv_k is None for i in gis):
                self.unrewritable += 1
                continue
            for i in gis:
                removed.add(i)
            if isinstance(anchor, tuple):
                pos, gi = anchor
            else:
                pos, gi = "before", anchor
            if g[gi].vertical or g[gi].adv_k is None:
                self.unrewritable += 1
                continue
            if text:
                inserts.setdefault(gi, []).append((pos, text, self._fit_units(gis)))
            self.replaced += 1
        if not removed and not inserts:
            return None
        # glyphs per (instruction, element) in order
        by_ins: dict = {}
        for gi, gl in enumerate(g):
            if gi in removed or gi in inserts:
                by_ins.setdefault(gl.ins, None)
        touched = set(by_ins)
        glyphs_of: dict = {}
        for gi, gl in enumerate(g):
            if gl.ins in touched:
                glyphs_of.setdefault(gl.ins, []).append(gi)
        out = []
        for idx, ins in enumerate(self.instrs):
            if idx not in touched:
                out.append(ins)
                continue
            out.extend(self._rewrite_show(idx, ins, glyphs_of.get(idx, []), removed, inserts))
        return out

    def _fit_units(self, gis):
        """Width available for the pseudonym, in TJ units of the anchor glyph."""
        if not gis:
            return None
        g = self.walker.glyphs
        first, last = g[gis[0]], g[gis[-1]]
        tot = sum(-g[i].adv_k for i in gis if g[i].adv_k is not None)
        dev_adv = math.hypot(first.x1 - first.x0, first.y1 - first.y0)
        if first.adv_k and dev_adv > 1e-9 and -first.adv_k > 1e-9:
            unit = dev_adv / (-first.adv_k)
            span = math.hypot(last.x1 - first.x0, last.y1 - first.y0)
            if span > 0:
                tot = max(tot, span / unit)
        return tot if tot > 0 else None

    def _rewrite_show(self, idx, ins, gis, removed, inserts):
        g = self.walker.glyphs
        op = str(ins.operator)
        ops = list(ins.operands)
        pre = []
        if op == "'":
            pre.append(ContentStreamInstruction([], Operator("T*")))
            elems = [ops[-1]]
        elif op == '"':
            pre.append(ContentStreamInstruction([ops[0]], Operator("Tw")))
            pre.append(ContentStreamInstruction([ops[1]], Operator("Tc")))
            pre.append(ContentStreamInstruction([], Operator("T*")))
            elems = [ops[-1]]
        elif op == "TJ":
            elems = list(ops[0])
        else:
            elems = [ops[-1]]
        fname, fsize, tc, tw, th = self.walker.show_state[idx]
        # Build a flat list of pieces: ('raw', elem) | ('g', gi, bytes) | ('k', num) | ('ins', text)
        gmap: dict = {}
        for gi in gis:
            gmap.setdefault(g[gi].elem, []).append(gi)
        pieces = []
        for ei, el in enumerate(elems):
            if isinstance(el, String):
                data = bytes(el)
                glist = gmap.get(ei, [])
                if not glist:
                    pieces.append(("raw", el))
                    continue
                p = 0
                for gi in glist:
                    nb = g[gi].nb
                    chunk = data[p:p + nb]
                    p += nb
                    for pos, text, fit in inserts.get(gi, []):
                        if pos == "before":
                            pieces.append(("ins", (text, fit)))
                    if gi in removed:
                        pieces.append(("k", g[gi].adv_k))
                    else:
                        pieces.append(("g", chunk))
                    for pos, text, fit in inserts.get(gi, []):
                        if pos == "after":
                            pieces.append(("ins", (text, fit)))
                if p < len(data):
                    pieces.append(("g", data[p:]))
            else:
                pieces.append(("raw", el))
        # Emit: runs of glyph/kern pieces as TJ arrays; insertions switch fonts.
        out = list(pre)
        arr: list = []

        def flush():
            if arr:
                out.append(ContentStreamInstruction([Array(list(arr))], Operator("TJ")))
                arr.clear()

        for kind, val in pieces:
            if kind == "raw":
                arr.append(val)
            elif kind == "g":
                if arr and isinstance(arr[-1], String):
                    arr[-1] = String(bytes(arr[-1]) + val)
                else:
                    arr.append(String(val))
            elif kind == "k":
                if arr and not isinstance(arr[-1], String):
                    try:
                        arr[-1] = float(arr[-1]) + val
                        continue
                    except (TypeError, ValueError):
                        pass
                arr.append(val)
            elif kind == "ins":
                flush()
                if not fsize:
                    continue
                text, fit = val
                enc = _encode_winansi(text)
                txt = enc.decode("cp1252", errors="replace")
                nspace = txt.count(" ")
                comp = _helv_width(txt) + 1000.0 * (tc * len(txt) + tw * nspace) / fsize
                scale = 1.0
                if fit and comp > fit * 1.02:
                    scale = max(0.4, fit / comp)
                out.append(ContentStreamInstruction([OVERLAY_FONT, fsize], Operator("Tf")))
                if scale < 1.0:
                    out.append(ContentStreamInstruction([round(th * 100.0 * scale, 3)], Operator("Tz")))
                out.append(ContentStreamInstruction([Array([String(enc), comp])], Operator("TJ")))
                if scale < 1.0:
                    out.append(ContentStreamInstruction([round(th * 100.0, 3)], Operator("Tz")))
                if fname is not None:
                    out.append(ContentStreamInstruction([fname, fsize], Operator("Tf")))
                self.used_overlay = True
        flush()
        return out


def _ensure_overlay_font(pdf, resources):
    fonts = resources.get("/Font")
    if fonts is None:
        resources[Name.Font] = Dictionary()
        fonts = resources.get("/Font")
    if OVERLAY_FONT not in fonts:
        fonts[OVERLAY_FONT] = pdf.make_indirect(Dictionary(
            Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding))


# ---------------------------------------------------------------------------
# Generic object-graph string rewriting
# ---------------------------------------------------------------------------
_SKIP_KEYS = {"/ID", "/O", "/U", "/OE", "/UE", "/Perms", "/Filter", "/Length", "/DecodeParms",
              "/ByteRange", "/Cert", "/Signature", "/CheckSum"}


def _looks_text(s: str) -> bool:
    if not s:
        return False
    bad = sum(1 for c in s if (ord(c) < 32 and c not in "\t\r\n") or c == "�")
    return bad <= len(s) * 0.05


def _string_text(o: String) -> Optional[str]:
    try:
        b = bytes(o)
    except Exception:
        return None
    if b.startswith(b"\xfe\xff") or b.startswith(b"\xff\xfe"):
        try:
            return b.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b.startswith(b"\xef\xbb\xbf"):
        try:
            return b[3:].decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        return str(o)
    except Exception:
        return None


class _GraphWalk:
    def __init__(self, plan, ctx, observe=None):
        self.plan = plan
        self.ctx = ctx
        self.changed = 0
        self.observe = observe   # callable(key_path, text) for scan
        self.seen = set()

    def fix(self, key, s: String):
        t = _string_text(s)
        if t is None or not _looks_text(t):
            return None
        if self.observe is not None:
            self.observe(key, t)
            return None
        if key in ("/URI",):
            new = nu.sub_value(self.plan, t, "url")
            if new == t:
                try:
                    new = self.plan.map_reference(self.ctx, t)
                except Exception:
                    new = t
        elif key in ("/F", "/UF", "/DOS", "/Mac", "/Unix"):
            new = nu.sub_value(self.plan, t, "path")
        elif key == "/T":
            new = nu.sub_value(self.plan, t, "person")
        else:
            new = nu.sub_value(self.plan, t)
        if new != t:
            self.changed += 1
            return String(new)
        return None

    def visit(self, o, depth=0, parent_key=""):
        if depth > 60:
            return
        if isinstance(o, (Dictionary, Stream)):
            if o.is_indirect:
                if o.objgen in self.seen:
                    return
                self.seen.add(o.objgen)
            d = o.stream_dict if isinstance(o, Stream) else o
            if d.get("/Type") == Name("/Sig") or d.get("/Type") == Name("/Encrypt"):
                return
            if d.get("/Type") == Name("/Font") or d.get("/Type") == Name("/FontDescriptor"):
                return
            for k in list(d.keys()):
                if k in _SKIP_KEYS or k in ("/Parent", "/P"):
                    continue
                v = d.get(k)
                if isinstance(v, String):
                    nv = self.fix(k, v)
                    if nv is not None:
                        d[k] = nv
                elif isinstance(v, (Dictionary, Array, Stream)):
                    self.visit(v, depth + 1, k)
        elif isinstance(o, Array):
            if o.is_indirect:
                if o.objgen in self.seen:
                    return
                self.seen.add(o.objgen)
            for i in range(len(o)):
                v = o[i]
                if isinstance(v, String):
                    nv = self.fix(parent_key, v)
                    if nv is not None:
                        o[i] = nv
                elif isinstance(v, (Dictionary, Array, Stream)):
                    self.visit(v, depth + 1, parent_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pdftotext_pages(path: str, timeout: float = 120.0) -> Optional[list]:
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-q", "-enc", "UTF-8", path, "-"], capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0 and not r.stdout:
        return None
    return r.stdout.decode("utf-8", "replace").split("\f")


_WORD_RE = re.compile(r'<word xMin="([\d.\-]+)" yMin="([\d.\-]+)" xMax="([\d.\-]+)" yMax="([\d.\-]+)">(.*?)</word>', re.S)
_PAGE_RE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)
_LINE_RE = re.compile(r"<line[^>]*>(.*?)</line>", re.S)


def _unxml(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&apos;", "'").replace("&#39;", "'").replace("&amp;", "&"))


def _bbox_lines(path: str, max_pages: int = 50, timeout: float = 120.0):
    """[(page_w, page_h, [(x0, y0, x1, y1, text), ...]), ...] using pdftotext -bbox-layout."""
    exe = shutil.which("pdftotext")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "-q", "-bbox-layout", "-l", str(max_pages), path, "-"],
                           capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return []
    html = r.stdout.decode("utf-8", "replace")
    pages = []
    for pm in _PAGE_RE.finditer(html):
        W, H = float(pm.group(1)), float(pm.group(2))
        lines = []
        for lm in _LINE_RE.finditer(pm.group(3)):
            words = _WORD_RE.findall(lm.group(1))
            if not words:
                continue
            x0 = min(float(w[0]) for w in words)
            y0 = min(float(w[1]) for w in words)
            x1 = max(float(w[2]) for w in words)
            y1 = max(float(w[3]) for w in words)
            lines.append((x0, y0, x1, y1, " ".join(_unxml(w[4]) for w in words)))
        pages.append((W, H, lines))
    return pages


def titleblock_observations(pages, out: list, prefix: str = "pdf.text"):
    """Observations from positioned text lines (top-left origin coordinates)."""
    n = 0
    for W, H, lines in pages:
        if not W or not H:
            continue
        for i, (x0, y0, x1, y1, t) in enumerate(lines):
            t = t.strip()
            if not t:
                continue
            in_tb = x0 > 0.45 * W and y0 > 0.55 * H
            k = nu.label_kind(t)
            # "LABEL: value" on one line
            m = re.match(r"^\s*([^:]{2,30}):\s*(.+)$", t)
            if m and nu.label_kind(m.group(1)):
                nu.add_obs(out, nu.label_kind(m.group(1)), f"{prefix}.label.{_norm_label(m.group(1))}",
                           m.group(2), "label: value", 0.6)
            elif k is not None:
                # nearest line to the right or just below the label
                best = None
                lh = max(y1 - y0, 1.0)
                for j, (a0, b0, a1, b1, tt) in enumerate(lines):
                    if j == i or not tt.strip() or nu.label_kind(tt):
                        continue
                    right = (abs(b0 - y0) < 0.6 * lh and 0 <= a0 - x1 < 40 * lh)
                    below = (0 <= b0 - y1 < 2.2 * lh and abs(a0 - x0) < 6 * lh)
                    if right or below:
                        # table-style title blocks put the value in the cell to the right: prefer it
                        d = (a0 - x1) / 20.0 if right else (b0 - y1) * 2 + lh
                        if best is None or d < best[0]:
                            best = (d, tt.strip())
                if best is not None:
                    nu.add_obs(out, k, f"{prefix}.label.{_norm_label(t)}", best[1], "label nearby", 0.5)
            if in_tb and n < 300:
                nu.add_obs(out, "free_text", f"{prefix}.titleblock", t, "title-block region", 0.3)
                n += 1


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:30] or "label"


def _xmp_streams(pdf):
    """Yield every XML metadata stream object (document, page, image, ...)."""
    for obj in pdf.objects:
        if isinstance(obj, Stream):
            try:
                if obj.get("/Type") == Name("/Metadata") or obj.get("/Subtype") == Name("/XML"):
                    yield obj
            except Exception:
                continue


_XMP_LEAF = re.compile(r"<([A-Za-z][\w.\-]*:[\w.\-]+)(?:\s[^>]*)?>([^<]{1,2000})</\1>")
_XMP_ATTR = re.compile(r"\s([A-Za-z][\w.\-]*:[\w.\-]+)=\"([^\"]{1,2000})\"")
_XMP_PERSON = {"dc:creator", "pdf:Author", "xmp:Author", "photoshop:AuthorsPosition", "photoshop:CaptionWriter",
               "xmpRights:Owner", "dc:contributor", "pdfx:Author", "cp:lastModifiedBy"}
_XMP_SOFTWARE = {"xmp:CreatorTool", "pdf:Producer", "stEvt:softwareAgent"}
_XMP_SKIP_PREFIX = ("xmp:CreateDate", "xmp:ModifyDate", "xmp:MetadataDate", "xmpMM:DocumentID",
                    "xmpMM:InstanceID", "xmpMM:OriginalDocumentID", "stEvt:when", "stEvt:instanceID",
                    "stRef:instanceID", "stRef:documentID", "dc:format", "pdf:PDFVersion",
                    "pdfaid:", "xmpTPg:", "stDim:", "xmpG:", "exif:", "tiff:")


def xmp_observations(text: str, out: list, field_prefix: str = "pdf.xmp"):
    for rx in (_XMP_LEAF, _XMP_ATTR):
        for m in rx.finditer(text):
            q, v = m.group(1), _unxml(m.group(2)).strip()
            if not v or q.startswith(_XMP_SKIP_PREFIX) or q.startswith("xmlns") or q.startswith("rdf:"):
                continue
            if q.startswith("x:"):
                continue
            if q in _XMP_PERSON:
                kind = "person"
            elif q in _XMP_SOFTWARE:
                kind = "software"
            elif q.endswith("filePath") or q.endswith("FilePath"):
                kind = "path"
            else:
                kind = "free_text"
            nu.add_obs(out, kind, f"{field_prefix}.{q}", v, "xmp", 0.8 if kind == "person" else 0.5)


def _xml_escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rewrite_xmp_text(text: str, plan) -> str:
    """Replace identifying values inside an XMP packet without breaking XML:
    leaf element texts and attribute values are unescaped, replaced, re-escaped."""
    def leaf(m):
        q, raw = m.group(1), m.group(2)
        v = _unxml(raw)
        kind = "person" if q in _XMP_PERSON else ("path" if q.lower().endswith("filepath") else None)
        nv = nu.sub_value(plan, v, kind)
        if nv == v:
            return m.group(0)
        return m.group(0).replace(">" + raw + "<", ">" + _xml_escape_text(nv) + "<", 1)

    def attr(m):
        q, raw = m.group(1), m.group(2)
        if q.startswith("xmlns"):
            return m.group(0)
        v = _unxml(raw)
        nv = nu.sub_value(plan, v)
        if nv == v:
            return m.group(0)
        return f' {q}="{_xml_escape_text(nv).replace(chr(34), "&quot;")}"'

    text = _XMP_LEAF.sub(leaf, text)
    text = _XMP_ATTR.sub(attr, text)
    return text


def _iter_filespecs(pdf):
    """Yield (filespec_dict, where) for embedded files: name tree + file attachment annots."""
    seen = set()
    try:
        for name, fs in pdf.attachments.items():
            obj = fs.obj
            key = obj.objgen if obj.is_indirect else id(obj)
            if key not in seen:
                seen.add(key)
                yield obj, "names"
    except Exception:
        pass
    for page in pdf.pages:
        for a in page.obj.get("/Annots", []) or []:
            try:
                if a.get("/Subtype") == Name("/FileAttachment") and isinstance(a.get("/FS"), Dictionary):
                    obj = a.FS
                    key = obj.objgen if obj.is_indirect else id(obj)
                    if key not in seen:
                        seen.add(key)
                        yield obj, "annot"
            except Exception:
                continue


def _fs_name(fs) -> str:
    for k in ("/UF", "/F", "/DOS", "/Unix", "/Mac"):
        v = fs.get(k)
        if isinstance(v, String):
            t = _string_text(v)
            if t:
                return t
    return "attachment.bin"


def _fs_stream(fs):
    ef = fs.get("/EF")
    if isinstance(ef, Dictionary):
        for k in ("/UF", "/F", "/DOS", "/Unix", "/Mac"):
            s = ef.get(k)
            if isinstance(s, Stream):
                return s
    return None


def _ocr_overlay_pages(pdf, src_path, pages, plan, ctx, counts) -> int:
    """For pages without a text layer (scans, vector-outline text): render with
    poppler, OCR, and cover each identity match with a background-coloured box
    carrying the pseudonym in Helvetica (rotation aware).  Returns overlays."""
    from deid.formats import _ocr
    if not _ocr.available() or not shutil.which("pdftoppm") or not pages:
        return 0
    import time
    from PIL import Image
    dpi = int(os.environ.get("DEID_PDF_OCR_DPI", "150"))
    budget = float(os.environ.get("DEID_PDF_OCR_BUDGET", "30"))
    max_pages = int(os.environ.get("DEID_PDF_OCR_PAGES", "3"))
    t0 = time.monotonic()
    total = 0
    for pi in sorted(pages)[:max_pages]:
        if time.monotonic() - t0 > budget:
            break
        d = tempfile.mkdtemp(dir=ctx.tmp_dir or None)
        try:
            r = subprocess.run(["pdftoppm", "-q", "-r", str(dpi), "-f", str(pi + 1), "-l", str(pi + 1), "-png",
                                "-singlefile", src_path, os.path.join(d, "p")], capture_output=True, timeout=120)
            png = os.path.join(d, "p.png")
            if r.returncode != 0 or not os.path.exists(png):
                continue
            img = Image.open(png).convert("RGB")
            edits = _ocr.find_edits(img, plan, d)
            if not edits:
                continue
            page = pdf.pages[pi]
            cb = [float(v) for v in page.cropbox]
            x0, y0, x1, y1 = min(cb[0], cb[2]), min(cb[1], cb[3]), max(cb[0], cb[2]), max(cb[1], cb[3])
            rot = int(page.obj.get("/Rotate", 0) or 0) % 360
            sc = dpi / 72.0

            def to_user(u, v):
                if rot == 90:
                    return x0 + v / sc, y0 + u / sc
                if rot == 180:
                    return x1 - u / sc, y0 + v / sc
                if rot == 270:
                    return x1 - v / sc, y1 - u / sc
                return x0 + u / sc, y1 - v / sc

            tm = {0: (1, 0, 0, 1), 90: (0, 1, -1, 0), 180: (-1, 0, 0, -1), 270: (0, -1, 1, 0)}.get(rot, (1, 0, 0, 1))
            ops = []
            for (l, t, rr, b), text in edits:
                pad = 2
                l, t, rr, b = l - pad, t - pad, rr + pad, b + pad
                bg = _ocr._bg_color(img, (max(l, 0), max(t, 0), min(rr, img.size[0] - 1), min(b, img.size[1] - 1)))
                ink = _ocr._ink_color(img, (max(l + pad, 0), max(t + pad, 0), min(rr - pad, img.size[0] - 1),
                                            min(b - pad, img.size[1] - 1)), bg)
                ax, ay = to_user(l, t)
                bx, by = to_user(rr, b)
                rx, ry, rw, rh = min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)
                ops.append(f"{bg[0]/255:.3f} {bg[1]/255:.3f} {bg[2]/255:.3f} rg {rx:.2f} {ry:.2f} {rw:.2f} {rh:.2f} re f")
                hpt = (b - t - 2 * pad) / sc
                wpt = (rr - l - 2 * pad) / sc
                enc = _encode_winansi(text)
                txt = enc.decode("cp1252", "replace")
                wu = _helv_width(txt) / 1000.0
                fs = max(2.0, min(hpt * 1.3, (wpt * 1.05) / wu if wu else hpt))
                # baseline: bottom-left of the word in screen terms, lifted by ~20% of height
                bu, bv = l + pad, b - pad - 0.2 * (b - t - 2 * pad)
                ox, oy = to_user(bu, bv)
                lit = enc.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
                ops.append(f"{ink[0]/255:.3f} {ink[1]/255:.3f} {ink[2]/255:.3f} rg BT /DeidHelv {fs:.2f} Tf "
                           f"{tm[0]} {tm[1]} {tm[2]} {tm[3]} {ox:.2f} {oy:.2f} Tm (" + lit.decode("latin-1") + ") Tj ET")
            res = page.obj.get("/Resources")
            if not isinstance(res, Dictionary):
                page.obj.Resources = Dictionary()
                res = page.obj.Resources
            _ensure_overlay_font(pdf, res)
            page.contents_add(pdf.make_stream(b"q\n"), prepend=True)
            body = ("\nQ\nq\n" + "\n".join(ops) + "\nQ\n").encode("latin-1")
            page.contents_add(pdf.make_stream(body), prepend=False)
            total += len(edits)
        except Exception:
            continue
        finally:
            shutil.rmtree(d, ignore_errors=True)
    counts["ocr_overlays"] = total
    return total


def _renders(path: str, page: int, tmp_dir: str = "") -> Optional[bool]:
    """True if poppler renders the page to a non-empty PNG, False if it fails,
    None on timeout."""
    d = tempfile.mkdtemp(dir=tmp_dir or None)
    try:
        r = subprocess.run(["pdftoppm", "-q", "-r", "10", "-f", str(page), "-l", str(page), "-png",
                            "-singlefile", path, os.path.join(d, "p")], capture_output=True, timeout=120)
        out = os.path.join(d, "p.png")
        return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
    except subprocess.TimeoutExpired:
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------
INFO_KIND = {"/Author": "person", "/Creator": "software", "/Producer": "software", "/Company": "company",
             "/Manager": "person", "/LastModifiedBy": "person", "/Title": "free_text", "/Subject": "free_text",
             "/Keywords": "free_text", "/Comments": "free_text", "/SourceModified": None,
             "/CreationDate": None, "/ModDate": None, "/Trapped": None}


class _LocalPlan:
    """Plan + identities read from this PDF's own title block (people, companies, phones next to
    recognised labels, initial+surname / honorific lines). Each extra value gets the pseudonym the plan
    itself would give it (plan.alias with an identity kind: deterministic token aliases), so the same
    person gets the same pseudonym in every PDF without changing the plan or any other file.
    Part/document numbers are deliberately not added here (they also name model files)."""

    def __init__(self, plan, extras):
        self._p = plan
        self.extras = []
        for value, alias in sorted(extras.items(), key=lambda kv: -len(kv[0])):
            rx = re.compile(r"(?<![\w])" + r"\s+".join(re.escape(t) for t in value.split()) + r"(?![\w])", re.I)
            self.extras.append((rx, alias))

    def __getattr__(self, name):
        return getattr(self._p, name)

    def replace_text(self, text):
        r = self._p.replace_text(text)
        for rx, alias in self.extras:
            r = rx.sub(alias, r)
        return r

    def alias(self, value, kind=None):
        a = self._p.alias(value, kind)
        if a is None and value:
            for rx, al in self.extras:
                if rx.fullmatch(value.strip()):
                    return al
        return a


_LOCAL_STOP = set("""
the this that such for check drill building with from into each also note notes see per all any and or not
duplicated used permission prohibited reproduction dimensions millimetres millimeters inches unless otherwise
specified property confidential proprietary drawing drawings copyright rights reserved without written
consent information contained about us tolerance tolerances scale sheet title material finish weight date
""".split())
_GERMAN_NOUN = re.compile(r"(?i)(ung|teil|bau|satz|deckel|breite|glas|lager|ring|platte|halter|schraube|welle|"
                          r"geh[äa]use|keit|heit|st[üu]ck|scheibe|bolzen|mutter|buchse|h[üu]lse|rad|lasche|winkel)$")


def _local_ok(v):
    toks = re.findall(r"[^\W\d_][^\W\d_'’\-]*", v)
    if not 2 <= len(toks) <= 3 or any(ch in v for ch in "/Ø()") or v.count(".") > 2:
        return False
    for t in toks:
        tl = t.lower()
        if tl in _LOCAL_STOP or (len(t) > 3 and _GERMAN_NOUN.search(t)):
            return False
    return True


def local_titleblock_identities(src_path, plan):
    """{value: pseudonym} for identities of this PDF that the plan does not replace: names next to
    person labels (every nearby cell is tried, table layouts included), initial+surname and
    honorific names anywhere on the page, companies and phones next to their labels."""
    if os.environ.get("DEID_PDF_LOCAL_IDS", "1") != "1":
        return {}
    try:
        from deid.identity.pdfids import person_value, company_value, phone_value, _ok_token, ORG_TAIL
        from deid.identity.vocab import is_word
    except Exception:
        return {}
    try:
        pages = _bbox_lines(src_path, max_pages=8)
    except Exception:
        return {}
    cands = []                                   # (value, kind)
    name_in_line = re.compile(r"(?<![\w.])(?:[A-ZÀ-ÖØ-Þ]\.\s?[A-ZÀ-ÖØ-Þ][a-zà-ÿ'’\-]{2,}|"
                              r"(?:Hr|Herr|Fr|Frau|Mr|Mrs|Ms|Dr|Prof)\.?\s+[A-ZÀ-ÖØ-Þ][a-zà-ÿ'’\-]{2,})(?![\w])")
    for W, H, lines in pages:
        for i, (x0, y0, x1, y1, t) in enumerate(lines):
            t = t.strip()
            if not t:
                continue
            for m in name_in_line.finditer(t):
                cands.append((m.group(0), "name_pattern"))
            k = nu.label_kind(t)
            if k not in ("person", "company", "contact"):
                continue
            lh = max(y1 - y0, 1.0)
            near = []
            for j, (a0, b0, a1, b1, tt) in enumerate(lines):
                if j == i or not tt.strip():
                    continue
                if abs(b0 - y0) < 0.6 * lh and a0 >= x1 - 1:
                    near.append((0, a0 - x1, tt))
                elif 0 <= b0 - y1 < 2.5 * lh and a0 < x1 and a1 > x0 - 2 * lh:
                    near.append((1, b0 - y1, tt))
            for _, _, tt in sorted(near)[:4]:
                if nu.label_kind(tt):
                    continue
                cands.append((tt.strip(), k))
    out = {}
    for raw, kind in cands:
        if kind == "name_pattern":
            sur = re.split(r"[.\s]+", raw.strip())[-1]
            ok = _ok_token(sur) and sur.lower() not in _LOCAL_STOP and not (len(sur) <= 5 and is_word(sur.lower()))
            v, kind = (raw.strip() if ok else None), "person"
        elif kind == "person":
            v = person_value(raw)
            if v and not _local_ok(v):
                v = None
        elif kind == "company":
            v = company_value(raw)
            if v and not ORG_TAIL.search(v):
                v = None
        else:
            v, kind = phone_value(raw), "phone"
        if not v or len(v) < 3 or v in out:
            continue
        if plan.replace_text(v) != v:
            continue                     # already an identity of the plan
        try:
            a = plan.alias(v, "account" if (kind == "person" and v.isupper() and len(v) <= 4) else kind)
        except Exception:
            a = None
        if a and a != v and len(a) == len(v):
            out[v] = a
    return out


class PdfAdapter(Adapter):
    name = "pdf"
    version = "4"
    priority = 20
    magics = ("pdf",)
    extensions = (".pdf",)
    path_independent = False   # attachments' names depend on the path plan
    timeout_s = 900.0

    # -- scan ----------------------------------------------------------
    def scan(self, ctx, src_path):
        obs, refs, notes = [], [], []
        try:
            pdf = pikepdf.open(src_path)
        except pikepdf.PasswordError:
            return ScanResult(status="source_exception", notes=["encrypted with a user password"])
        except Exception as e:
            return ScanResult(status="source_exception", notes=[f"unreadable PDF: {type(e).__name__}"])
        fp = {"pages": len(pdf.pages), "encrypted": bool(pdf.is_encrypted)}
        with pdf:
            try:
                boxes = []
                nimg = nann = 0
                for p in pdf.pages:
                    boxes.append([round(float(v), 1) for v in p.mediabox])
                    nann += len(p.obj.get("/Annots", []) or [])
                    try:
                        for _, x in (p.resources.get("/XObject") or {}).items():
                            if x.get("/Subtype") == Name("/Image"):
                                nimg += 1
                    except Exception:
                        pass
                fp.update(mediaboxes=boxes[:200], images=nimg, annots=nann)
            except Exception:
                pass
            # Info dictionary
            try:
                for k, v in pdf.docinfo.items():
                    k = str(k)
                    kind = INFO_KIND.get(k, "free_text")
                    if kind is None:
                        continue
                    try:
                        t = str(v)
                    except Exception:
                        continue
                    nu.add_obs(obs, kind, f"pdf.info.{k[1:]}", t, "Info dictionary", 0.9 if kind == "person" else 0.5)
                    nu.contact_observations(t, f"pdf.info.{k[1:]}", obs)
            except Exception:
                notes.append("info dictionary unreadable")
            # XMP packets
            for st in _xmp_streams(pdf):
                try:
                    xmp_observations(st.read_bytes().decode("utf-8", "replace"), obs)
                except Exception:
                    pass
            # every other string in the graph
            def observe(key, text):
                if key in ("/URI",):
                    nu.add_obs(obs, "url", "pdf.link.URI", text, "link annotation", 0.7)
                    refs.append(Reference(text, "uri", "pdf.link.URI"))
                elif key in ("/F", "/UF", "/DOS", "/Unix", "/Mac"):
                    nu.add_obs(obs, "path", f"pdf.filespec{key}", text, "file specification", 0.6)
                    refs.append(Reference(text, "path", f"pdf.filespec{key}"))
                elif key == "/T":
                    nu.add_obs(obs, "free_text", "pdf.annot.T", text, "annotation author / field name", 0.4)
                elif key in ("/Title", "/Contents", "/TU", "/V", "/Subj", "/Alt", "/ActualText", "/Name",
                             "/Desc", "/RC", "/JS", "/CA"):
                    nu.add_obs(obs, "free_text", f"pdf.obj{key}", text[:2000], "object string", 0.3)
                    nu.contact_observations(text, f"pdf.obj{key}", obs)
            gw = _GraphWalk(None, ctx, observe)
            try:
                gw.visit(pdf.Root)
                for p in pdf.pages:
                    gw.visit(p.obj)
            except Exception:
                notes.append("object walk incomplete")
            # attachments -> children
            children = []
            for fs, where in _iter_filespecs(pdf):
                nm = _fs_name(fs)
                nu.add_obs(obs, "path", "pdf.attachment.name", nm, where, 0.6)
                st = _fs_stream(fs)
                if st is None or ctx.dispatch_scan is None:
                    continue
                try:
                    data = st.read_bytes()
                    tmpd = tempfile.mkdtemp(dir=ctx.tmp_dir or None)
                    p = os.path.join(tmpd, "member" + os.path.splitext(nm)[1][:16])
                    with open(p, "wb") as f:
                        f.write(data)
                    cctx = ctx.child(nm.replace("\\", "/").split("/")[-1], size=len(data))
                    children.append((cctx.logical_path, ctx.dispatch_scan(cctx, p)))
                    shutil.rmtree(tmpd, ignore_errors=True)
                except Exception as e:
                    notes.append(f"attachment scan failed: {type(e).__name__}")
            fp["attachments"] = len(children)
        # positioned text (poppler) -> title-block observations; full text -> contacts
        pages = _bbox_lines(src_path)
        titleblock_observations(pages, obs)
        texts = _pdftotext_pages(src_path) or []
        full = "\n".join(texts)
        fp["text_chars"] = len(full.strip())
        nu.contact_observations(full, "pdf.text", obs, limit=300)
        res = ScanResult(observations=nu.dedupe_obs(obs), references=nu.dedupe_refs(refs),
                         fingerprint=fp, notes=notes)
        res.children = children
        return res

    # -- rewrite -------------------------------------------------------
    def rewrite(self, ctx, src_path, dst_path, plan):
        counts = {}
        notes = []
        try:
            pdf = pikepdf.open(src_path)
        except pikepdf.PasswordError:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=["encrypted with a user password"])
        except Exception as e:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[f"unreadable PDF: {type(e).__name__}"])
        children = []
        extras = local_titleblock_identities(src_path, plan)
        if extras:
            plan = _LocalPlan(plan, extras)
            counts["local_titleblock_ids"] = len(extras)
        with pdf:
            changed = 0
            # 1. content streams (only pages whose extracted text has an identity)
            texts = _pdftotext_pages(src_path)
            cand = set()
            if texts is None:
                cand = set(range(len(pdf.pages)))
            else:
                for i, t in enumerate(texts[:len(pdf.pages)]):
                    if t.strip() and plan.replace_text(t) != t:
                        cand.add(i)
            if cand:
                c = self._rewrite_pages(pdf, cand, plan, counts)
                changed += c
            # 2. Info + all strings in the object graph
            gw = _GraphWalk(plan, ctx)
            try:
                if pdf.trailer.get("/Info") is not None:
                    info = pdf.trailer.Info
                    for k in list(info.keys()):
                        v = info.get(k)
                        if isinstance(v, String):
                            t = _string_text(v)
                            if t is None:
                                continue
                            nv = nu.sub_value(plan, t, INFO_KIND.get(str(k)) or None)
                            if nv != t:
                                info[k] = String(nv)
                                gw.changed += 1
                gw.visit(pdf.Root)
                for p in pdf.pages:
                    gw.visit(p.obj)
            except Exception as e:
                notes.append(f"object walk incomplete: {type(e).__name__}")
            counts["strings_replaced"] = gw.changed
            changed += gw.changed
            # 3. XMP packets
            nx = 0
            for st in list(_xmp_streams(pdf)):
                try:
                    raw = st.read_bytes()
                    t = raw.decode("utf-8")
                except Exception:
                    continue
                nt = rewrite_xmp_text(t, plan)
                if nt != t:
                    try:
                        from lxml import etree
                        body = nt[nt.find("<x:xmpmeta"):] if "<x:xmpmeta" in nt else nt
                        end = body.rfind("</x:xmpmeta>")
                        if end > 0:
                            etree.fromstring((body[:end + len("</x:xmpmeta>")]).encode("utf-8"))
                    except Exception:
                        notes.append("xmp rewrite skipped: would not parse")
                        continue
                    st.write(nt.encode("utf-8"))
                    nx += 1
            counts["xmp_packets"] = nx
            changed += nx
            # 4. private application data and thumbnails
            nd = 0
            for holder in [pdf.Root] + [p.obj for p in pdf.pages]:
                for key in ("/PieceInfo",):
                    if key in holder:
                        del holder[key]
                        nd += 1
                if "/Thumb" in holder:
                    del holder["/Thumb"]
                    nd += 1
            counts["private_data_dropped"] = nd
            changed += nd
            # 5. attachments
            na = 0
            for fs, where in list(_iter_filespecs(pdf)):
                try:
                    r = self._rewrite_attachment(ctx, pdf, fs, plan)
                    if r is not None:
                        children.append(r)
                        if r[1].status == REWRITTEN:
                            na += 1
                except Exception as e:
                    notes.append(f"attachment rewrite failed: {type(e).__name__}")
            counts["attachments_rewritten"] = na
            changed += na
            # 6. OCR of raster images (DEID_PDF_OCR: auto (default) | all | 0)
            ocr_mode = os.environ.get("DEID_PDF_OCR", "auto")
            if ocr_mode == "1":
                ocr_mode = "all"
            if ocr_mode in ("auto", "all"):
                try:
                    from deid.formats import _ocr
                    textless = {i for i, t in enumerate(texts or []) if len(t.strip()) < 20}
                    # pages with a text layer but a title block drawn as vector outlines or an image:
                    # no extractable line in the bottom-right title-block region
                    if os.environ.get("DEID_PDF_OCR_TITLEBLOCK", "1") == "1":
                        try:
                            npg = int(os.environ.get("DEID_PDF_OCR_PAGES", "3"))
                            for i, (W, H, lines) in enumerate(_bbox_lines(src_path, max_pages=npg)):
                                if W and H and not any(t.strip() and x0 > 0.5 * W and y0 > 0.6 * H
                                                       for (x0, y0, x1, y1, t) in lines):
                                    textless.add(i)
                        except Exception:
                            pass
                    nocr = _ocr.pdf_images(pdf, plan, ctx, ocr_mode, textless)
                    counts["ocr_regions"] = nocr
                    changed += nocr
                    # vector-outline text / scans: overlay pseudonyms on text-less pages
                    if textless and os.environ.get("DEID_PDF_OCR_OVERLAY", "1") == "1":
                        changed += _ocr_overlay_pages(pdf, src_path, textless, plan, ctx, counts)
                except Exception as e:
                    notes.append(f"ocr failed: {type(e).__name__}")
            if not changed:
                return RewriteResult(status=VERIFIED_UNCHANGED, counts=counts, notes=notes, children=children)
            pdf.save(dst_path, compress_streams=True, deterministic_id=True,
                     object_stream_mode=pikepdf.ObjectStreamMode.preserve,
                     stream_decode_level=pikepdf.StreamDecodeLevel.generalized)
        return RewriteResult(status=REWRITTEN, counts=counts, notes=notes, children=children)

    def _rewrite_pages(self, pdf, cand, plan, counts):
        font_cache = {}
        done_forms = set()
        total = 0
        repl = unrw = 0

        def process_stream(holder, resources, ctm, is_page):
            nonlocal total, repl, unrw
            try:
                instrs = pikepdf.parse_content_stream(holder)
            except Exception:
                return

            forms = []

            def on_form(name, fctm):
                forms.append((name, fctm))

            sr = StreamRewriter(pdf, instrs, resources, plan, on_form=on_form, ctm=ctm, font_cache=font_cache)
            sr.walk()
            edits = sr.compute_edits()
            new = sr.apply(edits)
            repl += sr.replaced
            unrw += sr.unrewritable
            if new is not None:
                if sr.used_overlay:
                    _ensure_overlay_font(pdf, resources)
                data = pikepdf.unparse_content_stream(new)
                if is_page:
                    holder.obj.Contents = pdf.make_stream(data)
                else:
                    holder.write(data)
                total += 1
            # nested forms
            xo = resources.get("/XObject") if resources is not None else None
            for name, fctm in forms:
                try:
                    x = xo.get(name) if xo is not None else None
                except Exception:
                    x = None
                if not isinstance(x, Stream) or x.get("/Subtype") != Name("/Form"):
                    continue
                if x.objgen in done_forms:
                    continue
                done_forms.add(x.objgen)
                fres = x.get("/Resources")
                if not isinstance(fres, Dictionary):
                    fres = resources
                m = x.get("/Matrix")
                mm = tuple(_num(v) for v in m) if isinstance(m, Array) and len(m) == 6 else _ID
                process_stream(x, fres, _mul(mm, fctm), False)

        for i, page in enumerate(pdf.pages):
            if i not in cand:
                continue
            try:
                res = page.obj.get("/Resources")
                if not isinstance(res, Dictionary):
                    # inherited resources: copy onto the page so we can add the overlay font
                    inh = page.resources
                    res = inh
                process_stream(page, res, _ID, True)
            except Exception:
                continue
            # annotation appearance streams
            for a in page.obj.get("/Annots", []) or []:
                try:
                    ap = a.get("/AP")
                    if not isinstance(ap, Dictionary):
                        continue
                    for key in ("/N", "/R", "/D"):
                        s = ap.get(key)
                        streams = [s] if isinstance(s, Stream) else (list(s.values()) if isinstance(s, Dictionary) else [])
                        for st in streams:
                            if isinstance(st, Stream) and st.objgen not in done_forms:
                                done_forms.add(st.objgen)
                                r = st.get("/Resources")
                                process_stream(st, r if isinstance(r, Dictionary) else Dictionary(), _ID, False)
                except Exception:
                    continue
        counts["text_spans_replaced"] = repl
        counts["text_spans_unrewritable"] = unrw
        counts["streams_rewritten"] = total
        return total

    def _rewrite_attachment(self, ctx, pdf, fs, plan):
        nm = _fs_name(fs)
        base = nm.replace("\\", "/").split("/")[-1]
        cctx = ctx.child(base)
        # name follows the path plan
        try:
            newbase = plan.map_path(cctx.logical_path).rsplit("!/", 1)[-1].rsplit("/", 1)[-1]
        except Exception:
            newbase = base
        if newbase != base:
            newname = nm[: len(nm) - len(base)] + newbase
            for k in ("/UF", "/F", "/DOS", "/Unix", "/Mac"):
                if isinstance(fs.get(k), String):
                    fs[k] = String(newname)
        st = _fs_stream(fs)
        if st is None or ctx.dispatch_rewrite is None:
            return None
        data = st.read_bytes()
        tmpd = tempfile.mkdtemp(dir=ctx.tmp_dir or None)
        try:
            ext = os.path.splitext(base)[1][:16]
            sp = os.path.join(tmpd, "src" + ext)
            dp = os.path.join(tmpd, "dst" + ext)
            with open(sp, "wb") as f:
                f.write(data)
            cctx.size = len(data)
            rr = ctx.dispatch_rewrite(cctx, sp, dp, plan)
            if rr is not None and rr.status == REWRITTEN and os.path.exists(dp):
                with open(dp, "rb") as f:
                    nd = f.read()
                st.write(nd)
                params = st.get("/Params")
                if isinstance(params, Dictionary):
                    params[Name.Size] = len(nd)
                    params[Name.CheckSum] = String(hashlib.md5(nd).digest())
            return (cctx.logical_path, rr)
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)

    # -- verify --------------------------------------------------------
    def verify(self, ctx, src_path, dst_path, plan):
        notes = []
        if not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
            return VerifyResult(ok=False, notes=["missing output"])
        try:
            with pikepdf.open(src_path) as a, pikepdf.open(dst_path) as b:
                if len(a.pages) != len(b.pages):
                    return VerifyResult(ok=False, notes=["page count changed"])
                for pa, pb in zip(a.pages, b.pages):
                    for box in ("mediabox", "cropbox"):
                        va = [round(float(v), 2) for v in getattr(pa, box)]
                        vb = [round(float(v), 2) for v in getattr(pb, box)]
                        if va != vb:
                            return VerifyResult(ok=False, notes=[f"{box} changed"])
                fp = {"pages": len(b.pages)}
                try:
                    b.check_pdf_syntax() if hasattr(b, "check_pdf_syntax") else None
                except Exception:
                    pass
        except Exception as e:
            return VerifyResult(ok=False, notes=[f"reopen failed: {type(e).__name__}"])
        # render (low res) the first and last pages; poppler must not error out
        if shutil.which("pdftoppm"):
            n = fp["pages"]
            for pg in sorted({1, n}):
                ok_dst = _renders(dst_path, pg, ctx.tmp_dir)
                if ok_dst is False and _renders(src_path, pg, ctx.tmp_dir):
                    return VerifyResult(ok=False, notes=[f"page {pg} no longer renders"])
                if ok_dst is None:
                    notes.append("render timeout")
        ta = _pdftotext_pages(src_path) or []
        tb = _pdftotext_pages(dst_path) or []
        if sum(len(t.strip()) for t in ta) > 50 and sum(len(t.strip()) for t in tb) == 0:
            return VerifyResult(ok=False, notes=["text no longer extracts"])
        # residual identities (informational)
        resid = sum(1 for t in tb if t.strip() and plan.replace_text(t) != t)
        if resid:
            notes.append(f"pages with residual identifiers: {resid}")
        fp["residual_pages"] = resid
        return VerifyResult(ok=True, notes=notes, fingerprint=fp)


register(PdfAdapter())
