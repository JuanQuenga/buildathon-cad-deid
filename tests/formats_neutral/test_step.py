from conftest import run_adapter
from deid.formats import step

STEP = (b"ISO-10303-21;\r\nHEADER;\r\n/* exported by Jane Roe */\r\n"
        b"FILE_DESCRIPTION(('Bracket_Main assembly'),'2;1');\r\n"
        b"FILE_NAME('C:\\\\Users\\\\jroe\\\\Bracket_Main.stp','2020-01-01T00:00:00',('Jane Roe'),"
        b"('Acme Widgets'),'Some Exporter 1.0','Some CAD','');\r\n"
        b"FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\r\nENDSEC;\r\nDATA;\r\n"
        b"#1=PRODUCT('ACME-4711','Bracket_Main','',(#2));\r\n"
        b"#2=PRODUCT_CONTEXT('',#3,'mechanical');\r\n"
        b"#3=APPLICATION_CONTEXT('core data for automotive mechanical design processes');\r\n"
        b"#4=CARTESIAN_POINT('',(0.,1.5E-3,-2.));\r\n"
        b"#5=PERSON('jroe','Roe','Jane',$,$,$);\r\n"
        b"#6=ORGANIZATION('','Acme Widgets','it''s Acme Widgets\\X2\\00E9\\X0\\');\r\n"
        b"#7=(GEOMETRIC_REPRESENTATION_CONTEXT(3)REPRESENTATION_CONTEXT('Context #1','3D'));\r\n"
        b"#8=SHAPE_REPRESENTATION('Bracket_Main',(#4),#7);\r\n"
        b"#9=ADVANCED_FACE('NONE',(#4),#4,.T.);\r\n"
        b"ENDSEC;\r\nEND-ISO-10303-21;\r\n")


def test_step_rewrite(tmp_path, plan):
    sr, rr, vr, out = run_adapter(tmp_path, "step", "a.stp", STEP, plan)
    fields = {(o.field, o.value) for o in sr.observations}
    assert ("step.FILE_NAME.author", "Jane Roe") in fields
    assert ("step.ORGANIZATION.name", "Acme Widgets") in fields
    assert ("step.PRODUCT.id", "ACME-4711") in fields
    assert any(r.raw.endswith("Bracket_Main.stp") for r in sr.references)
    assert rr.status == "rewritten"
    for bad in (b"Jane", b"Roe", b"Acme", b"ACME-4711", b"jroe", b"Bracket_Main"):
        assert bad not in out, bad
    assert b"#4=CARTESIAN_POINT('',(0.,1.5E-3,-2.));\r\n" in out
    assert b"'Company Beta'" in out
    assert b"it''s Company Beta\\X2\\00E9\\X0\\'" in out
    assert b"C:\\\\Users\\\\user01\\\\Part_Gamma.stp" in out
    assert b"/* exported by Person Alpha */" in out
    assert out.isascii()


def test_codec_roundtrip():
    for raw in [b"abc", b"it''s", b"\\X2\\00E900FC\\X0\\x", b"\\S\\Dx", b"\\X\\E9", b"a\\\\b",
                b"C:\\Users\\x", "caf\u00e9".encode()]:
        txt, style = step.decode_p21(raw)
        assert step.encode_p21(txt, style).replace(b"\\X\\E9", b"") is not None
    assert step.decode_p21(b"\\X2\\00E9\\X0\\")[0] == "\u00e9"
    assert step.decode_p21(b"it''s")[0] == "it's"
    assert step.encode_p21("it's \u00e9", step.decode_p21(b"x")[1]) == b"it''s \\X2\\00E9\\X0\\"


def test_step_streaming_chunks(tmp_path, plan, monkeypatch):
    # tiny chunks exercise every carry/cut path
    big = STEP.replace(b"ENDSEC;\r\nEND", b"".join(
        b"#%d=CARTESIAN_POINT('',(%d.,0.,0.));\r\n#%d=SHAPE_REPRESENTATION('Bracket_Main x''',(#4),#7);\r\n"
        % (100 + 2 * i, i, 101 + 2 * i) for i in range(300)) + b"ENDSEC;\r\nEND")
    monkeypatch.setattr(step, "CHUNK", 97)
    import deid.formats.step as s2
    orig = s2._Stream.__init__
    monkeypatch.setattr(s2._Stream, "__init__", lambda self, f, chunk=97: orig(self, f, 97))
    sr, rr, vr, out = run_adapter(tmp_path, "step", "b.step", big, plan)
    assert b"Bracket_Main" not in out and out.count(b"'Part_Gamma x'''") == 300
    assert out.replace(b"Part_Gamma", b"Bracket_Main").replace(b"Person Alpha", b"Jane Roe") != big  # other names changed
    assert len(out.splitlines()) == len(big.splitlines())


def test_step_unchanged(tmp_path):
    from conftest import StubPlan
    sr, rr, vr, out = run_adapter(tmp_path, "step", "c.step", STEP, StubPlan({"Zzz": "Yyy"}))
    assert rr.status == "verified_unchanged"
