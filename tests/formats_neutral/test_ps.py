import struct

from conftest import run_adapter

PS = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%Title: (Bracket_Main drawing)\n%%Creator: SomeCAD\n%%For: Jane Roe\n"
      b"%%BoundingBox: 0 0 100 100\n%%EndComments\n% note: Acme Widgets internal\n"
      b"/Helvetica findfont 12 scalefont setfont\n10 10 moveto (Drawn by Jane Roe \\(Acme Widgets\\)) show\n"
      b"%%BeginData: 12 Binary Bytes\n(Jane Roe)%x\n%%EndData\n"
      b"currentfile /ASCII85Decode filter /DCTDecode filter image\n(Jane Roe)%%~>\n"
      b"showpage\n%%EOF\n")


def test_ps(tmp_path, plan):
    sr, rr, vr, out = run_adapter(tmp_path, "ps", "a.eps", PS, plan)
    assert b"%%Title: (Part_Gamma drawing)" in out
    assert b"%%For: Person Alpha" in out
    assert b"% note: Company Beta internal" in out
    assert b"(Drawn by Person Alpha \\(Company Beta\\)) show" in out
    # data regions untouched
    assert b"%%BeginData: 12 Binary Bytes\n(Jane Roe)%x\n" in out
    assert b"image\n(Jane Roe)%%~>" in out


def test_dos_eps(tmp_path, plan):
    tif = b"II*\x00" + b"\x01" * 40
    ps = PS
    hdr = struct.pack("<4sIIIIIIH", b"\xc5\xd0\xd3\xc6", 30, len(ps), 0, 0, 30 + len(ps), len(tif), 0xFFFF)
    data = hdr + ps + tif
    sr, rr, vr, out = run_adapter(tmp_path, "ps", "b.eps", data, plan)
    ps_off, ps_len, _, _, t_off, t_len = struct.unpack_from("<IIIIII", out, 4)
    assert out[t_off:t_off + t_len] == tif
    assert out[ps_off:ps_off + ps_len].startswith(b"%!PS") and b"%%For: Person Alpha" in out[ps_off:ps_off + ps_len]
