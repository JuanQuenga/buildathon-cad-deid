import uuid

import olefile

from _plan import DictPlan
from cfbgen import write_cfb, propset
from deid.api import FileContext, REWRITTEN
from deid.formats.cfb import CfbAdapter, parse_propset

SUMMARY = uuid.UUID("F29F85E0-4FF9-1068-AB91-08002B27B3D9").bytes_le


def make(path):
    ps = propset(SUMMARY, [(2, "Bracket"), (4, "Alice Example"), (8, "aexample")])
    body = (b"\x00\x01" + b"\xff\xfe\xff\x26" + "C:\\Users\\aexample\\work\\Bracket.ipt".encode("utf-16-le")
            + b"\x00" * 8 + b"Drawn by Alice Example!" + b"\x00" * 16)
    body += b"\x00" * (4096 - len(body))
    write_cfb(path, [("\x05SummaryInformation", ps), ("UFRxDoc", body)])


def test_cfb_roundtrip(tmp_path):
    src, dst = str(tmp_path / "a.ipt"), str(tmp_path / "b.ipt")
    make(src)
    assert olefile.isOleFile(src)
    ad = CfbAdapter()
    ctx = FileContext(logical_path="x/a.ipt", magic="cfb", tmp_dir=str(tmp_path))
    s = ad.scan(ctx, src)
    vals = {(o.kind, o.value) for o in s.observations}
    assert ("saved_account", "Alice Example") in vals and ("saved_account", "aexample") in vals
    assert any(r.raw.endswith("Bracket.ipt") for r in s.references)
    plan = DictPlan({"Alice Example": "Person Alpha1", "aexample": "user0001"},
                    paths={"Bracket.ipt": "Part-01.ipt"})
    r = ad.rewrite(ctx, src, dst, plan)
    assert r.status == REWRITTEN
    assert ad.verify(ctx, src, dst, plan).ok
    with olefile.OleFileIO(dst) as o:
        _, strs, _ = parse_propset(o.openstream("\x05SummaryInformation").read())
        v = {p.pid: p.value for p in strs}
        assert v == {2: "Bracket", 4: "Person Alpha1", 8: "user0001"}
        body = o.openstream("UFRxDoc").read()
        assert "C:\\Users\\user0001\\work\\Part-01.ipt".encode("utf-16-le") in body
        assert b"Drawn by Person Alpha1!" in body
    assert b"Alice" not in open(dst, "rb").read()
