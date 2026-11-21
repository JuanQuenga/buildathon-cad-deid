import struct

from _plan import DictPlan
from deid.api import FileContext, REWRITTEN
from deid.formats.opaque import OpaqueAdapter


def _blk(typ, data, meta=b""):
    pad = lambda b: b + b"\x00" * ((-len(b)) % 4)
    return struct.pack("<III", typ, len(data), len(meta)) + pad(data) + pad(meta)


def test_u3d_strings(tmp_path):
    name = b"bracket_alice"
    hdr = _blk(0x00443355, b"\x00" * 24)
    node = _blk(0xFFFFFF22, struct.pack("<H", len(name)) + name + b"\x00" * 8,
                struct.pack("<II", 1, 0) + struct.pack("<H", 6) + b"Author" + struct.pack("<H", 5) + b"alice")
    src, dst = tmp_path / "a.u3d", tmp_path / "b.u3d"
    src.write_bytes(hdr.replace(b"\x55\x33\x44\x00", b"U3D\x00", 1) + node)
    ad = OpaqueAdapter()
    ctx = FileContext(logical_path="a.u3d", magic="u3d")
    assert ad.matches(ctx, src.read_bytes())
    r = ad.rewrite(ctx, str(src), str(dst), DictPlan({"alice": "pp001"}))
    assert r.status == REWRITTEN
    out = dst.read_bytes()
    assert b"bracket_pp001" in out and b"alice" not in out and len(out) == len(src.read_bytes())
    assert ad.verify(ctx, str(src), str(dst), None).ok
