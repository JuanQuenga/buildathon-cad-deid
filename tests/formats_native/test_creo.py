from _plan import DictPlan
from deid.api import FileContext, REWRITTEN
from deid.formats.creo import CreoAdapter

HDR = (b"#UGC:2 PART 1632 1060 805 1 1 15 3300 2015280 000002d0 \\\n"
       b"#- VERS 0 0                                                          \\\n"
       b"#- HOST alicepc                                                      \\\n"
       b"#- CMNM 014bracket_alice.prt                                         \\\n"
       b"#-END_OF_UGC_HEADER\n#UGC_TOC 2 32 81 17####\n\x00\x01binary-section bracket_alice\x00")


def test_creo_header(tmp_path):
    src, dst = tmp_path / "bracket_alice.prt.3", tmp_path / "o"
    src.write_bytes(HDR)
    ad = CreoAdapter()
    ctx = FileContext(logical_path="x/bracket_alice.prt.3", magic="creo")
    assert ad.matches(ctx, HDR)
    s = ad.scan(ctx, str(src))
    assert ("account", "alicepc") in {(o.kind, o.value) for o in s.observations}
    plan = DictPlan({"alicepc": "host001", "alice": "pp001"}, paths={"bracket_alice": "bracket_pp001"})
    r = ad.rewrite(ctx, str(src), str(dst), plan)
    assert r.status == REWRITTEN
    out = dst.read_bytes()
    assert b"#- HOST host001 " in out and b"014bracket_pp001.prt" in out
    assert out.endswith(b"binary-section bracket_alice\x00")   # sections untouched
    assert ad.verify(ctx, str(src), str(dst), plan).ok


def test_creo_kept_stem(tmp_path):
    src, dst = tmp_path / "bracket_alice.prt.3", tmp_path / "o"
    src.write_bytes(HDR)
    ctx = FileContext(logical_path="x/bracket_alice.prt.3", magic="creo")
    plan = DictPlan({"alicepc": "host001", "alice": "pp001"})   # map_path keeps the stem
    r = CreoAdapter().rewrite(ctx, str(src), str(dst), plan)
    out = dst.read_bytes()
    assert b"014bracket_alice.prt" in out and b"#- HOST host001 " in out
