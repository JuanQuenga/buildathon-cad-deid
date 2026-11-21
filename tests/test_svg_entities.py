from lxml import etree

from deid import api
from deid.formats.svg import SvgAdapter
from tests.test_archives import FakePlan


def test_escaped_entities_survive(tmp_path):
    src = tmp_path / "a.svg"
    src.write_text('<svg xmlns="http://www.w3.org/2000/svg"><metadata>{"a":"&amp;copy; ACME"}</metadata>'
                   '<text title="R&amp;D &quot;ACME&quot;">ACME &amp; co</text></svg>')
    dst = tmp_path / "o.svg"
    ctx = api.FileContext(logical_path="x/a.svg", tmp_dir=str(tmp_path))
    assert SvgAdapter().rewrite(ctx, str(src), str(dst), FakePlan()).status == api.REWRITTEN
    etree.fromstring(dst.read_bytes())          # must stay well-formed
    out = dst.read_text()
    assert "&amp;copy;" in out and "ACME" not in out
