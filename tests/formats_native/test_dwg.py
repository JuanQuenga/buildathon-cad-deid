"""DWG primitives on synthetic data (no corpus files)."""
import random
import struct

from deid.formats.dwg import compress, decompress, page_checksum, parse_summary


def test_lz_roundtrip():
    rnd = random.Random(3)
    cases = [bytes(rnd.randrange(256) for _ in range(5000)), b"abcabcabc" * 4000,
             bytes(rnd.choice(b"xy") for _ in range(29696)), b"\x00" * 29696, b"1234"]
    for d in cases:
        c = compress(d)
        assert decompress(c, len(d)) == d
    assert len(compress(b"abcabcabc" * 4000)) < 200


def test_checksum_known():
    assert page_checksum(0, b"") == 0
    assert page_checksum(0, b"\x01\x02") == ((1 + 3) << 16) | 3


def _s(t):
    return struct.pack("<H", len(t) + 1) + (t + "\x00").encode("utf-16-le")


def test_summary_parse():
    buf = b"".join(_s(t) for t in ["Title", "", "Alice Example", "", "", "aexample", "", ""])
    buf += b"\x00" * 24 + struct.pack("<H", 1) + _s("Drawn By") + _s("Alice Example") + b"\x00" * 8
    f = {k: v for k, off, nb, v in parse_summary(buf, True)}
    assert f["author"] == "Alice Example" and f["lastsavedby"] == "aexample"
    assert f["custom:Drawn By"] == "Alice Example"
    k, off, nb, v = [x for x in parse_summary(buf, True) if x[0] == "author"][0]
    assert buf[off:off + nb].decode("utf-16-le") == "Alice Example"
