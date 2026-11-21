import struct

from conftest import run_adapter


def test_stl_binary(tmp_path, plan):
    hdr = b"solid Bracket_Main by Jane Roe".ljust(80, b"\x00")
    tri = struct.pack("<12fH", *([0.0] * 3 + [1.0, 2.0, 3.0] * 3), 7)
    data = hdr + struct.pack("<I", 2) + tri + tri
    sr, rr, vr, out = run_adapter(tmp_path, "stl", "m.stl", data, plan)
    assert rr.status == "rewritten" and len(out) == len(data)
    assert out[80:] == data[80:]
    assert b"Bracket_Main" not in out and b"Jane" not in out
    assert out[:80].startswith(b"solid Part_Gamma by Person Alpha")


def test_stl_ascii(tmp_path, plan):
    data = (b"solid Bracket_Main\r\n  facet normal 0 0 1\r\n    outer loop\r\n      vertex 0 0 0\r\n"
            b"      vertex 1 0 0\r\n      vertex 0 1 0\r\n    endloop\r\n  endfacet\r\nendsolid Bracket_Main\r\n")
    sr, rr, vr, out = run_adapter(tmp_path, "stl", "a.stl", data, plan)
    assert out == data.replace(b"Bracket_Main", b"Part_Gamma")


def test_obj_mtl(tmp_path, plan):
    obj = (b"# made by Jane Roe at Acme Widgets\nmtllib Bracket_Main.mtl\no Bracket_Main\nv 1 2 3\nv 4 5 6\n"
           b"v 7 8 9\nusemtl Acme Widgets steel\nf 1 2 3\ng Bracket_Main other\n")
    sr, rr, vr, out = run_adapter(tmp_path, "obj", "a.obj", obj, plan)
    assert out == (b"# made by Person Alpha at Company Beta\nmtllib Part_Gamma.mtl\no Part_Gamma\nv 1 2 3\n"
                   b"v 4 5 6\nv 7 8 9\nusemtl Company Beta steel\nf 1 2 3\ng Part_Gamma other\n")
    assert any(r.raw == "Bracket_Main.mtl" for r in sr.references)
    mtl = b"newmtl Acme Widgets steel\nKd 1 1 1\nmap_Kd -s 1 1 1 C:\\Users\\jroe\\tex.png\n"
    sr, rr, vr, out = run_adapter(tmp_path, "mtl", "Bracket_Main.mtl", mtl, plan)
    assert out == b"newmtl Company Beta steel\nKd 1 1 1\nmap_Kd -s 1 1 1 C:\\Users\\user01\\tex.png\n"


def test_vrml(tmp_path, plan):
    wrl = (b"#VRML V2.0 utf8\n# exported for Acme Widgets\nWorldInfo { title \"Bracket_Main\" info [\"by Jane Roe\"] }\n"
           b"DEF Bracket_Main Transform { children [ Inline { url \"parts/Bracket_Main.wrl\" } ] }\n"
           b"USE Bracket_Main\nDEF Other Shape { geometry IndexedFaceSet { coord Coordinate { point [ 0 0 0, 1 1 1 ] } } }\n")
    sr, rr, vr, out = run_adapter(tmp_path, "vrml", "a.wrl", wrl, plan)
    assert out.startswith(b"#VRML V2.0 utf8\n# exported for Company Beta\n")
    assert b"DEF Part_Gamma Transform" in out and b"USE Part_Gamma\n" in out
    assert b'url "parts/Part_Gamma.wrl"' in out and b'"by Person Alpha"' in out
    assert b"point [ 0 0 0, 1 1 1 ]" in out


def test_off(tmp_path, plan):
    off = b"OFF\n# Jane Roe\n3 1 0\n0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n"
    sr, rr, vr, out = run_adapter(tmp_path, "off", "a.off", off, plan)
    assert out == off.replace(b"Jane Roe", b"Person Alpha")
