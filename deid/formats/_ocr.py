"""Shared OCR helper (docs-native): find identity text in raster pixels and
paint the pseudonym over it.

Uses the external ``tesseract`` executable (Apache-2.0) through its TSV
output; nothing is linked.  If tesseract is missing every function is a no-op.

``redact_image(img, plan)`` -> (new PIL image or None, n_regions):
  1. OCR (psm 11 sparse text; additionally psm 6 for small crops),
  2. group words into lines, ask ``plan.replace_text`` per line,
  3. for every changed word span: fill its box with the local background colour
     and draw the pseudonym (Pillow's bundled scalable font) fitted to the box.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import difflib
import re
from typing import Optional

TESSERACT = shutil.which("tesseract")
MAX_PIXELS = int(os.environ.get("DEID_OCR_MAX_PIXELS", str(40_000_000)))
OCR_TIMEOUT = float(os.environ.get("DEID_OCR_TIMEOUT", "90"))


def available() -> bool:
    return TESSERACT is not None


OCR_MAX_SIDE = int(os.environ.get("DEID_OCR_MAX_SIDE", "3500"))


def _cache_path(img, psm):
    d = os.environ.get("DEID_OCR_CACHE")
    if not d:
        return None
    import hashlib
    h = hashlib.sha256(img.tobytes())
    h.update(f"{img.size}{img.mode}{psm}{OCR_MAX_SIDE}".encode())
    return os.path.join(d, h.hexdigest()[:2], h.hexdigest() + ".json")


def ocr_words(img, psm: int = 11, tmp_dir: str = ""):
    """[(left, top, width, height, conf, text, block, par, line)] for a PIL image
    (coordinates in the original image).  Results are cached on disk when
    DEID_OCR_CACHE is set (scan and rewrite then OCR each image once)."""
    if TESSERACT is None:
        return []
    import json
    cp = _cache_path(img, psm)
    if cp and os.path.exists(cp):
        try:
            with open(cp) as f:
                return [tuple(w) for w in json.load(f)]
        except (OSError, ValueError):
            pass
    d = tempfile.mkdtemp(dir=tmp_dir or None)
    try:
        p = os.path.join(d, "i.png")
        g = img.convert("L")
        scale = 1.0
        if max(g.size) < 1200:
            scale = 2.0
        elif max(g.size) > OCR_MAX_SIDE:
            scale = OCR_MAX_SIDE / max(g.size)
        if scale != 1.0:
            g = g.resize((max(1, int(g.size[0] * scale)), max(1, int(g.size[1] * scale))))
        g.save(p)
        env = dict(os.environ, OMP_THREAD_LIMIT="1")
        r = subprocess.run([TESSERACT, p, "stdout", "--psm", str(psm), "-l", "eng", "tsv"],
                           capture_output=True, timeout=OCR_TIMEOUT, env=env)
        out = []
        for line in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
            f = line.split("\t")
            if len(f) < 12 or not f[11].strip():
                continue
            try:
                conf = float(f[10])
            except ValueError:
                continue
            l, t, w, h = (int(int(f[6]) / scale), int(int(f[7]) / scale), int(int(f[8]) / scale) + 1,
                          int(int(f[9]) / scale) + 1)
            out.append((l, t, w, h, conf, f[11], int(f[2]), int(f[3]), int(f[4])))
        if cp:
            try:
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                with open(cp + ".tmp", "w") as fh:
                    json.dump(out, fh)
                os.replace(cp + ".tmp", cp)
            except OSError:
                pass
        return out
    except (subprocess.TimeoutExpired, OSError):
        return []
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _lines(words):
    groups = {}
    for w in words:
        groups.setdefault((w[6], w[7], w[8]), []).append(w)
    return [sorted(v, key=lambda w: w[0]) for k, v in sorted(groups.items())]


def ocr_text(img, tmp_dir: str = "") -> list:
    """Lines of OCR text (for scan observations)."""
    return [" ".join(w[5] for w in ln) for ln in _lines(ocr_words(img, tmp_dir=tmp_dir))]


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=max(6, size))
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _bg_color(img, box):
    """Most common colour on the border of the box (background)."""
    l, t, r, b = box
    px = []
    W, H = img.size
    for x in range(max(l - 2, 0), min(r + 2, W)):
        for y in (max(t - 2, 0), min(b + 1, H - 1)):
            px.append(img.getpixel((x, y)))
    for y in range(max(t - 2, 0), min(b + 2, H)):
        for x in (max(l - 2, 0), min(r + 1, W - 1)):
            px.append(img.getpixel((x, y)))
    if not px:
        return (255, 255, 255)
    from collections import Counter
    return Counter(px).most_common(1)[0][0]


def _ink_color(img, box, bg):
    """Most common colour inside the box that is clearly not the background."""
    from collections import Counter
    l, t, r, b = box
    c = Counter()
    step = max(1, (r - l) * (b - t) // 4000)
    i = 0
    for y in range(t, b + 1):
        for x in range(l, r + 1):
            i += 1
            if i % step:
                continue
            px = img.getpixel((x, y))
            a = px if isinstance(px, tuple) else (px, px, px)
            g = bg if isinstance(bg, tuple) else (bg, bg, bg)
            if sum(abs(int(a[k]) - int(g[k])) for k in range(3)) > 150:
                c[px] += 1
    if not c:
        return _fg_color(bg)
    # prefer the most saturated/dark among the top candidates (anti-aliasing produces mid tones)
    cand = [p for p, _ in c.most_common(5)]
    g = bg if isinstance(bg, tuple) else (bg, bg, bg)
    return max(cand, key=lambda p: sum(abs(int((p if isinstance(p, tuple) else (p, p, p))[k]) - int(g[k])) for k in range(3)))


def _fg_color(bg):
    lum = sum(bg[:3]) / 3 if isinstance(bg, tuple) else bg
    return (0, 0, 0) if lum > 127 else (255, 255, 255)


_CONF = str.maketrans({"0": "o", "1": "i", "l": "i", "|": "i", "!": "i", "j": "i", "5": "s", "8": "b",
                       "2": "z", "6": "g", "9": "g", "$": "s", "€": "e", "@": "a"})
_FUZZY_CACHE = {}


def _fnorm(s: str) -> str:
    s = s.lower().replace("rn", "m").replace("vv", "w").replace("cl", "d")
    s = re.sub(r"[^0-9a-z|!$@\u00c0-\uffff]", "", s)
    return s.translate(_CONF)


def _fuzzy_index(plan):
    """normalised compact value -> original value, from the plan's entries
    (OCR-tolerant lookup).  Empty if the plan exposes no entries."""
    key = id(plan)
    if key in _FUZZY_CACHE:
        return _FUZZY_CACHE[key]
    idx = {}
    ents = getattr(plan, "entries", None)
    if isinstance(ents, dict):
        for e in ents.values():
            v = getattr(e, "value", None)
            if not v or getattr(e, "kind", "") in ("software",):
                continue
            k = _fnorm(v)
            if len(k) >= 5:
                idx.setdefault(k, v)
                if len(k) >= 8:
                    idx.setdefault(("pfx", k[:3]), []).append((k, v))
    _FUZZY_CACHE.clear()
    _FUZZY_CACHE[key] = idx
    return idx


def _fuzzy_lookup(acc, idx):
    """Exact normalised hit, else a near match (one OCR slip per ~8 chars)."""
    v = idx.get(acc)
    if v is not None:
        return v
    if len(acc) < 8:
        return None
    best, bv = 0.0, None
    for k, val in idx.get(("pfx", acc[:3]), ()):
        if abs(len(k) - len(acc)) > max(1, len(k) // 8):
            continue
        r = difflib.SequenceMatcher(None, k, acc, autojunk=False).ratio()
        if r > best:
            best, bv = r, val
    return bv if best >= 0.9 else None


def _fuzzy_edits(ln, plan, idx):
    """Runs of OCR words whose normalised concatenation matches a plan entry."""
    out = []
    toks = [w[5] for w in ln]
    norm = [_fnorm(t) for t in toks]
    i = 0
    while i < len(toks):
        hit = None
        acc = ""
        for j in range(i, min(i + 6, len(toks))):
            acc += norm[j]
            if len(acc) >= 5:
                v = _fuzzy_lookup(acc, idx)
                if v is not None:
                    hit = (j, v)
        if hit is None:
            i += 1
            continue
        j, value = hit
        new = plan.alias(value) or plan.replace_text(value)
        if new and new != value:
            ws = ln[i:j + 1]
            l = min(w[0] for w in ws)
            t = min(w[1] for w in ws)
            r = max(w[0] + w[2] for w in ws)
            b = max(w[1] + w[3] for w in ws)
            out.append(((l, t, r, b), new))
        i = j + 1
    return out


def redact_image(img, plan, tmp_dir: str = "", min_conf: float = 30.0):
    """Return (new_image or None, regions_replaced)."""
    if TESSERACT is None or img.size[0] * img.size[1] > MAX_PIXELS:
        return None, 0
    words = [w for w in ocr_words(img, tmp_dir=tmp_dir) if w[4] >= min_conf or w[4] < 0]
    if not words:
        return None, 0
    edits = []
    fidx = _fuzzy_index(plan)
    for ln in _lines(words):
        toks = [w[5] for w in ln]
        old = " ".join(toks)
        new = plan.replace_text(old)
        if new == old:
            if fidx:
                edits.extend(_fuzzy_edits(ln, plan, fidx))
            continue
        nt = new.split(" ")
        sm = difflib.SequenceMatcher(None, toks, nt, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal" or i1 == i2:
                continue
            ws = ln[i1:i2]
            l = min(w[0] for w in ws)
            t = min(w[1] for w in ws)
            r = max(w[0] + w[2] for w in ws)
            b = max(w[1] + w[3] for w in ws)
            edits.append(((l, t, r, b), " ".join(nt[j1:j2])))
    if not edits:
        return None, 0
    from PIL import ImageDraw
    mode = img.mode
    work = img.convert("RGB") if mode not in ("RGB", "L") else img.copy()
    draw = ImageDraw.Draw(work)
    for (l, t, r, b), text in edits:
        pad = 1
        box = (max(l - pad, 0), max(t - pad, 0), min(r + pad, work.size[0] - 1), min(b + pad, work.size[1] - 1))
        bg = _bg_color(work, box)
        fg = _ink_color(work, box, bg)
        draw.rectangle(box, fill=bg)
        if not text.strip():
            continue
        h = max(b - t, 6)
        bw = max(r - l, 6)
        size = max(6, int(h * 1.35))
        font = _font(size)
        while size > 6:
            tb = draw.textbbox((0, 0), text, font=font)
            if tb[2] - tb[0] <= bw * 1.05 + 2 and tb[3] - tb[1] <= h * 1.1 + 1:
                break
            size -= 1
            font = _font(size)
        tb = draw.textbbox((0, 0), text, font=font)
        draw.text((l - tb[0], t - tb[1] + max(0, (h - (tb[3] - tb[1])) // 2)), text, fill=fg, font=font)
    if work.mode != mode:
        try:
            work = work.convert(mode)
        except ValueError:
            pass
    return work, len(edits)


# ---------------------------------------------------------------------------
# PDF raster images (optional stage, DEID_PDF_OCR=1)
# ---------------------------------------------------------------------------
def pdf_images(pdf, plan, ctx) -> int:
    """OCR every reasonably sized image XObject; replace pixels where the plan
    matches.  Returns the number of regions replaced."""
    if TESSERACT is None:
        return 0
    import pikepdf
    from pikepdf import PdfImage, Name
    from PIL import Image
    seen = set()
    total = 0
    for page in pdf.pages:
        try:
            xo = page.resources.get("/XObject") or {}
        except Exception:
            continue
        for name, x in list(xo.items()):
            try:
                if x.get("/Subtype") != Name("/Image") or x.objgen in seen:
                    continue
                seen.add(x.objgen)
                w, h = int(x.get("/Width", 0)), int(x.get("/Height", 0))
                if w * h < 40_000 or w * h > MAX_PIXELS or "/SMask" in x and False:
                    continue
                pim = PdfImage(x)
                img = pim.as_pil_image()
                new, n = redact_image(img, plan, ctx.tmp_dir if ctx is not None else "")
                if not n or new is None:
                    continue
                if new.mode not in ("RGB", "L"):
                    new = new.convert("RGB")
                import zlib
                raw = new.tobytes()
                x.write(zlib.compress(raw), filter=Name("/FlateDecode"))
                x.ColorSpace = Name("/DeviceRGB") if new.mode == "RGB" else Name("/DeviceGray")
                x.BitsPerComponent = 8
                for k in ("/DecodeParms", "/Decode"):
                    if k in x:
                        del x[k]
                total += n
            except Exception:
                continue
    return total
