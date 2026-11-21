"""Synthetic fixtures for the neutral-format adapters (no corpus data)."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from deid import api  # noqa: E402

api.load_all()

# Synthetic identities (invented) and their pseudonyms.
NAMES = {
    "Jane Roe": "Person Alpha",
    "Roe": "Alpha",
    "Jane": "Person",
    "Acme Widgets": "Company Beta",
    "ACME-4711": "PART-0001",
    "acme.example": "beta.example",
    "jroe": "user01",
    "Bracket_Main": "Part_Gamma",
}


class StubPlan:
    version = "test"

    def __init__(self, mapping=NAMES):
        self.m = dict(mapping)
        keys = sorted(self.m, key=len, reverse=True)
        self.rx = re.compile("|".join(re.escape(k) for k in keys))

    def alias(self, value, kind=None):
        return self.m.get(value)

    def replace_text(self, text):
        return self.rx.sub(lambda m: self.m[m.group(0)], text)

    def replace_bytes(self, data, encodings=("utf-8",), length_preserving=False):
        for k, v in self.m.items():
            kb, vb = k.encode(), v.encode()
            if length_preserving:
                vb = (vb + b" " * len(kb))[:len(kb)]
            data = data.replace(kb, vb)
        return data

    def map_path(self, p):
        return self.replace_text(p)

    def map_reference(self, ctx, raw):
        return self.replace_text(raw)


@pytest.fixture
def plan():
    return StubPlan()


def run_adapter(tmp_path, name, filename, data, plan, expect_adapter=None):
    src = tmp_path / filename
    src.write_bytes(data)
    head = data[: api.HEAD_BYTES]
    ctx = api.FileContext(logical_path=filename, magic=api.sniff(head, filename), size=len(data),
                          tmp_dir=str(tmp_path))
    ad = api.route(ctx, head)
    assert ad is not None, f"no adapter for {filename} ({ctx.magic})"
    assert ad.name == (expect_adapter or name)
    sr = ad.scan(ctx, str(src))
    dst = tmp_path / ("out_" + filename)
    rr = ad.rewrite(ctx, str(src), str(dst), plan)
    vr = None
    if rr.status == api.REWRITTEN:
        vr = ad.verify(ctx, str(src), str(dst), plan)
        assert vr.ok, vr.notes
        out = dst.read_bytes()
    else:
        out = data
    return sr, rr, vr, out
