from _plan import DictPlan
from deid.api import FileContext, REWRITTEN
from deid.formats.ipj import IpjAdapter

XML = ('<?xml version="1.0" encoding="utf-16" standalone="no" ?>\r\n<InventorProject schemarevid="14">'
       '<ProjectOptions><Owner>Alice Example</Owner></ProjectOptions><WorkspacePath><Path>.\\alice_work\\</Path>'
       '</WorkspacePath></InventorProject>\r\n')


def test_ipj(tmp_path):
    src, dst = tmp_path / "p.ipj", tmp_path / "o.ipj"
    src.write_bytes(b"\xff\xfe" + XML.encode("utf-16-le"))
    ad = IpjAdapter()
    ctx = FileContext(logical_path="x/p.ipj")
    s = ad.scan(ctx, str(src))
    assert ("person", "Alice Example") in {(o.kind, o.value) for o in s.observations}
    plan = DictPlan({"Alice Example": "Person Alpha"}, paths={"alice_work": "proj_0001"})
    r = ad.rewrite(ctx, str(src), str(dst), plan)
    assert r.status == REWRITTEN and ad.verify(ctx, str(src), str(dst), plan).ok
    t = dst.read_bytes()[2:].decode("utf-16-le")
    assert "<Owner>Person Alpha</Owner>" in t and ".\\proj_0001\\" in t
