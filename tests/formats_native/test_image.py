import io

import pytest
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

from _plan import DictPlan
from deid.api import FileContext, REWRITTEN
from deid.formats import _ocr
from deid.formats.image import ImageAdapter, png_chunks, jpeg_segments

PLAN = DictPlan({"Alice Example": "Person Alpha", "EXAMPLE CORP": "COMPANY GAMMA"})


def test_png_text_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_IMAGE_OCR", "0")
    src, dst = tmp_path / "a.png", tmp_path / "b.png"
    im = Image.new("RGB", (64, 32), "white")
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Author", "Alice Example")
    meta.add_text("Comment", "made at EXAMPLE CORP", zip=True)
    meta.add_itxt("Description", "for Alice Example")
    im.save(src, pnginfo=meta, dpi=(300, 300))
    ad = ImageAdapter()
    ctx = FileContext(logical_path="a.png", magic="png")
    s = ad.scan(ctx, str(src))
    assert ("person", "Alice Example") in {(o.kind, o.value) for o in s.observations}
    r = ad.rewrite(ctx, str(src), str(dst), PLAN)
    assert r.status == REWRITTEN and ad.verify(ctx, str(src), str(dst), PLAN).ok
    out = Image.open(dst)
    assert out.text["Author"] == "Person Alpha" and "COMPANY GAMMA" in out.text["Comment"]
    assert out.text["Description"] == "for Person Alpha"
    assert round(out.info["dpi"][0]) == 300
    # pixel data untouched
    idat = lambda p: b"".join(b for t, b, _ in png_chunks(p.read_bytes()) if t == b"IDAT")
    assert idat(src) == idat(dst)


def test_jpeg_exif_and_comment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_IMAGE_OCR", "0")
    src, dst = tmp_path / "a.jpg", tmp_path / "b.jpg"
    im = Image.new("RGB", (64, 32), "white")
    ex = Image.Exif()
    ex[0x013B] = "Alice Example"
    ex[0x0131] = "Camera FW 1.0"
    im.save(src, exif=ex.tobytes(), comment=b"scanned by Alice Example", quality=90)
    ad = ImageAdapter()
    ctx = FileContext(logical_path="a.jpg", magic="jpeg")
    r = ad.rewrite(ctx, str(src), str(dst), PLAN)
    assert r.status == REWRITTEN and ad.verify(ctx, str(src), str(dst), PLAN).ok
    b = dst.read_bytes()
    assert b"Alice" not in b and b"Person Alpha" in b and b"Camera FW 1.0" in b
    # entropy-coded data identical (no re-encode)
    a = src.read_bytes()
    assert a[a.find(b"\xff\xda"):] == b[b.find(b"\xff\xda"):]


@pytest.mark.skipif(not _ocr.available(), reason="tesseract not installed")
def test_ocr_pixels(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_IMAGE_OCR", "1")
    src, dst = tmp_path / "tb.png", tmp_path / "o.png"
    im = Image.new("RGB", (900, 200), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.load_default(size=40)
    d.text((20, 20), "DRAWN Alice Example", fill="black", font=f)
    d.text((20, 110), "SCALE 1:2", fill="black", font=f)
    im.save(src)
    ad = ImageAdapter()
    ctx = FileContext(logical_path="tb.png", magic="png", tmp_dir=str(tmp_path))
    r = ad.rewrite(ctx, str(src), str(dst), PLAN)
    assert r.status == REWRITTEN and r.counts.get("ocr_regions", 0) >= 1
    assert ad.verify(ctx, str(src), str(dst), PLAN).ok
    text = " ".join(_ocr.ocr_text(Image.open(dst)))
    assert "Alice" not in text and "SCALE" in text
