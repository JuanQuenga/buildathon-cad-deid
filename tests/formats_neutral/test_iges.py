import pytest

from conftest import run_adapter, StubPlan
from deid.formats import iges


def _line(text, sec, n):
    return text.ljust(72)[:72].encode() + sec.encode() + b"%7d" % n + b"\r\n"


def _holl(s):
    return f"{len(s)}H{s}"


def make_iges():
    S = [_line("Exported for Acme Widgets by Jane Roe", "S", 1)]
    g = ",".join(["1H,", "1H;", _holl("Bracket_Main"), _holl("C:\\Users\\jroe\\Bracket_Main.igs"),
                  _holl("SomeCAD 1.0"), _holl("SomeCAD IGES"), "32", "38", "6", "308", "15",
                  _holl("Bracket_Main"), "1.", "2", _holl("MM"), "1", "1.", _holl("20200101.000000"),
                  "0.001", "100.", _holl("Jane Roe"), _holl("Acme Widgets"), "11", "0"]) + ";"
    G = [_line(g[i:i + 72], "G", k + 1) for k, i in enumerate(range(0, len(g), 72))]
    # entities: 110 line, 212 note, 406 name property
    precs = [
        (110, "110,0.,0.,0.,10.5,2.25,0.;"),
        (212, "212,1,8,4.,1.,1,0.,0.,0,0,1.,2.,0.," + _holl("Jane Roe") + ";"),
        (406, "406,1," + _holl("Bracket_Main") + ";"),
    ]
    D, P = [], []
    pseq = 1
    for k, (t, rec) in enumerate(precs):
        dseq = 2 * k + 1
        chunks = [rec[i:i + 64] for i in range(0, len(rec), 64)]
        D.append(_line(f"{t:8d}{pseq:8d}{0:8d}{0:8d}{0:8d}{0:8d}{0:8d}{0:8d}{0:8d}", "D", dseq))
        D.append(_line(f"{t:8d}{0:8d}{0:8d}{len(chunks):8d}{0:8d}{'':8s}{'':8s}{'LABEL':>8s}{0:8d}", "D", dseq + 1))
        for c in chunks:
            P.append((c.ljust(64) + " " + f"{dseq:7d}").encode() + b"P" + b"%7d" % pseq + b"\r\n")
            pseq += 1
    T = _line(f"S{len(S):7d}G{len(G):7d}D{len(D):7d}P{len(P):7d}", "T", 1)
    return b"".join(S + G + D + P) + T


def check_structure(out):
    lines = out.split(b"\r\n")[:-1]
    assert all(len(l) == 80 for l in lines)
    fp = iges.iges_fingerprint(out)
    assert fp["t_ok"]
    return fp


def test_iges(tmp_path, plan):
    data = make_iges()
    assert iges.iges_fingerprint(data)["t_ok"]
    sr, rr, vr, out = run_adapter(tmp_path, "iges", "a.igs", data, plan)
    assert rr.status == "rewritten"
    for bad in (b"Jane", b"Acme", b"jroe", b"Bracket_Main"):
        assert bad not in out, bad
    check_structure(out)
    assert b"110,0.,0.,0.,10.5,2.25,0.;" in out
    assert b"12,4.,1.,1,0.,0.,0,0,1.,2.,0.,12HPerson Alpha;" in out   # NC updated 8 -> 12
    f = {(o.field, o.value) for o in sr.observations}
    assert ("iges.global.author", "Jane Roe") in f and ("iges.global.organization", "Acme Widgets") in f


def test_iges_resequence(tmp_path):
    long = StubPlan({"Jane Roe": "A Very Long Pseudonymous Person Name That Needs More Lines " * 2})
    data = make_iges()
    sr, rr, vr, out = run_adapter(tmp_path, "iges", "b.igs", data, long)
    fp = check_structure(out)
    assert fp["entities"] == 3


def test_iges_big_mode(tmp_path, plan, monkeypatch):
    monkeypatch.setattr(iges, "BIG", 0)
    data = make_iges()
    sr, rr, vr, out = run_adapter(tmp_path, "iges", "c.igs", data, plan)
    assert rr.status == "rewritten"
    assert b"Jane Roe" in out          # parameter data untouched in big mode
    assert b"jroe" not in out[:400]
    check_structure(out)
