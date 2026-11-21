import io

import ezdxf

from conftest import run_adapter


def _make():
    doc = ezdxf.new("R2010")
    doc.header["$LASTSAVEDBY"] = "jroe"
    doc.layers.add("Acme Widgets")
    blk = doc.blocks.new("Bracket_Main")
    blk.add_line((0, 0), (1, 1))
    blk.add_attdef("DRAWN", (0, 0), text="Jane Roe")
    msp = doc.modelspace()
    msp.add_text("Drawn by Jane Roe", dxfattribs={"layer": "Acme Widgets"}).set_placement((1, 2))
    msp.add_mtext("ACME-4711\\PAcme Widgets", dxfattribs={"layer": "Acme Widgets"})
    ins = msp.add_blockref("Bracket_Main", (5, 5))
    ins.add_attrib("DRAWN", "Jane Roe", (5, 5))
    msp.add_line((0, 0), (10.125, 3.5), dxfattribs={"layer": "0"})
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().replace("\n", "\r\n").encode("cp1252")


def test_dxf(tmp_path, plan):
    data = _make()
    sr, rr, vr, out = run_adapter(tmp_path, "dxf", "a.dxf", data, plan)
    assert rr.status == "rewritten"
    for bad in (b"Jane", b"Acme", b"ACME-4711", b"jroe", b"Bracket_Main"):
        assert bad not in out, bad
    assert out.count(b"\r\n") == data.count(b"\r\n")
    (tmp_path / "check.dxf").write_bytes(out)
    doc = ezdxf.readfile(str(tmp_path / "check.dxf"))
    assert "Company Beta" in doc.layers and "Part_Gamma" in doc.blocks
    msp = doc.modelspace()
    ins = msp.query("INSERT")[0]
    assert ins.dxf.name == "Part_Gamma" and ins.get_attrib_text("DRAWN") == "Person Alpha"
    assert msp.query("TEXT")[0].dxf.text == "Drawn by Person Alpha"
    assert msp.query("TEXT")[0].dxf.layer == "Company Beta"
    line = msp.query("LINE")[0]
    assert tuple(line.dxf.end)[:2] == (10.125, 3.5)
    assert doc.header["$LASTSAVEDBY"] == "user01"
    kinds = {(o.kind, o.field) for o in sr.observations}
    assert ("person", "dxf.header.$LASTSAVEDBY") in kinds
