import zipfile

import pytest

from _plan import DictPlan
from deid.api import FileContext, REWRITTEN
from deid.formats.zippkg import ZipPackageAdapter

DOC = ('<?xml version="1.0" encoding="utf-8"?><Document SchemaVersion="4"><Properties Count="3">'
       '<Property name="CreatedBy" type="App::PropertyString"><String value="Alice Example"/></Property>'
       '<Property name="Company" type="App::PropertyString"><String value="Example Corp"/></Property>'
       '<Property name="Label" type="App::PropertyString"><String value="Bracket"/></Property>'
       '</Properties><Objects Count="1"><Object type="Part::Box" name="Box"/></Objects>'
       '<ObjectData><Object name="Box"><Properties><Property name="Shape" type="Part::PropertyPartShape">'
       '<Part file="PartShape.brp"/></Property><Property name="Length"><Float value="25.4"/></Property>'
       '</Properties></Object></ObjectData></Document>')

MODEL = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
         '<metadata name="Designer">Alice Example</metadata><metadata name="Title">Example Corp widget</metadata>'
         '<resources><object id="1" name="Example Corp widget" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/>'
         '</vertices><triangles/></mesh></object></resources><build><item objectid="1"/></build></model>')

PLAN = DictPlan({"Alice Example": "Person Alpha", "Example Corp": "Company Gamma"})


@pytest.mark.parametrize("zip64", [False, True])
def test_fcstd(tmp_path, zip64):
    src, dst = tmp_path / "a.FCStd", tmp_path / "b.FCStd"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Document.xml", DOC)
        with z.open("PartShape.brp", "w", force_zip64=zip64) as f:
            f.write(b"DBRep_DrawableShape\n" * 50)
        z.writestr("GuiDocument.xml", '<?xml version="1.0"?><Document/>')
    ad = ZipPackageAdapter()
    ctx = FileContext(logical_path="x/a.FCStd", magic="zip", tmp_dir=str(tmp_path))
    assert ad.matches(ctx, src.read_bytes()[:8192])
    s = ad.scan(ctx, str(src))
    assert ("person", "Alice Example") in {(o.kind, o.value) for o in s.observations}
    r = ad.rewrite(ctx, str(src), str(dst), PLAN)
    assert r.status == REWRITTEN
    assert ad.verify(ctx, str(src), str(dst), PLAN).ok
    with zipfile.ZipFile(dst) as z:
        assert z.testzip() is None
        assert z.namelist() == ["Document.xml", "PartShape.brp", "GuiDocument.xml"]
        d = z.read("Document.xml").decode()
        assert "Person Alpha" in d and "Company Gamma" in d and "Alice" not in d
        assert 'name="Box"' in d and 'file="PartShape.brp"' in d and "25.4" in d
        assert z.read("PartShape.brp") == b"DBRep_DrawableShape\n" * 50


def test_3mf(tmp_path):
    src, dst = tmp_path / "a.3mf", tmp_path / "b.3mf"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("3D/3dmodel.model", MODEL)
        z.writestr("Metadata/model_settings.config", "; user = Alice Example\n")
    ad = ZipPackageAdapter()
    ctx = FileContext(logical_path="x/a.3mf", magic="zip", tmp_dir=str(tmp_path))
    r = ad.rewrite(ctx, str(src), str(dst), PLAN)
    assert r.status == REWRITTEN
    assert ad.verify(ctx, str(src), str(dst), PLAN).ok
    with zipfile.ZipFile(dst) as z:
        m = z.read("3D/3dmodel.model").decode()
        assert 'name="Company Gamma widget"' in m and "Person Alpha" in m and 'name="Designer"' in m
        assert "Alice" not in z.read("Metadata/model_settings.config").decode()
