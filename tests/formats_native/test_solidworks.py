"""Synthetic modern-SOLIDWORKS container (structure only, no real data)."""
import struct
import zlib

from _plan import DictPlan
from deid.api import FileContext, REWRITTEN
from deid.formats import solidworks as sw

KEY = 3


def _ror(b, k):
    return ((b >> k) | (b << (8 - k))) & 0xFF


def _name(s):
    return bytes(_ror(c, KEY) for c in s.encode())


def _cstr(s):
    return b"\xff\xfe\xff" + bytes([len(s)]) + s.encode("utf-16-le")


def _chunk(name, payload, slack=0):
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    comp = co.compress(payload) + co.flush() + b"\x00" * slack
    nm = _name(name)
    hdr = b"VALA" + sw.MARKER + b"\x01\x00\x00\x00" + struct.pack("<IIII", 0x10000, len(comp), len(payload), len(nm))
    return hdr + nm, comp


def _toc(name, csz, usz, off):
    nm = _name(name)
    return (b"VALA" + sw.MARKER + b"\x00\x00\x00\x00" + struct.pack("<IIII", 0, csz, usz, len(nm))
            + b"\x00" * 10 + struct.pack("<I", off - 8) + nm)


def make_sw(path):
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties '
            'xmlns:cp="x" xmlns:dc="y"><dc:lastModifiedBy>Alice Example</dc:lastModifiedBy>'
            '<dc:title>Widget</dc:title></cp:coreProperties>').encode()
    custom = ('<?xml version="1.0"?><Properties xmlns:vt="v"><property name="DrawnBy" pid="2">'
              '<vt:lpwstr>Alice Example</vt:lpwstr></property><property name="Material" pid="3">'
              '<vt:lpwstr>AISI 304</vt:lpwstr></property></Properties>').encode()
    hdr2 = b"\xff\xff\x01\x00\x0a\x00moHeader_c" + _cstr("aexample") + b"\x00" * 8
    cmgr = b"\x00\x01" + _cstr("C:\\Users\\aexample\\proj\\Bracket.SLDPRT") + _cstr("Default") + b"\x00\x00"
    file = bytearray(b"\x00\x00\x00\x00\x00\x00\x00" + bytes([KEY]) + b"\x00" * 24)
    recs = []
    for nm, pl in (("docProps/core.xml", core), ("docProps/custom.xml", custom), ("Header2", hdr2),
                   ("Contents/CMgr", cmgr)):
        h, comp = _chunk(nm, pl, slack=16)
        recs.append((nm, len(comp), len(pl), len(file)))
        file += h + comp
    for nm, csz, usz, off in recs:
        file += _toc(nm, csz, usz, off)
    open(path, "wb").write(bytes(file))


def test_sw_roundtrip(tmp_path):
    src, dst = str(tmp_path / "a.SLDASM"), str(tmp_path / "b.SLDASM")
    make_sw(src)
    chunks, toc = sw.parse_chunks(open(src, "rb").read())
    assert [c.name for c in chunks] == ["docProps/core.xml", "docProps/custom.xml", "Header2", "Contents/CMgr"]
    assert len(toc) == 4
    ad = sw.SolidWorksAdapter()
    ctx = FileContext(logical_path="p/a.SLDASM", tmp_dir=str(tmp_path))
    assert ad.matches(ctx, open(src, "rb").read(8192))
    s = ad.scan(ctx, src)
    vals = {(o.kind, o.value) for o in s.observations}
    assert ("person", "Alice Example") in vals
    assert ("account", "aexample") in vals
    assert any(r.raw.endswith("Bracket.SLDPRT") and r.field.endswith("#fixedlen") for r in s.references)
    plan = DictPlan({"Alice Example": "Person Alpha", "aexample": "user0001"},
                    paths={"Bracket.SLDPRT": "Part-01.SLDPRT"})
    r = ad.rewrite(ctx, src, dst, plan)
    assert r.status == REWRITTEN, r
    assert ad.verify(ctx, src, dst, plan).ok
    out = open(dst, "rb").read()
    assert len(out) == len(open(src, "rb").read())
    streams = {}
    for c in sw.parse_chunks(out)[0]:
        streams[c.name] = sw._inflate(out, c)
        assert len(streams[c.name]) == c.usz
    assert b"Person Alpha" in streams["docProps/core.xml"]
    assert b"AISI 304" in streams["docProps/custom.xml"]
    assert "user0001".encode("utf-16-le") in streams["Header2"]
    assert "C:\\Users\\user0001\\proj\\Part-01.SLDPRT".encode("utf-16-le") in streams["Contents/CMgr"]
    assert b"Alice" not in b"".join(streams.values())


def test_sw_lockfile(tmp_path):
    src, dst = tmp_path / "~$a.SLDPRT", tmp_path / "o"
    src.write_bytes(b"aexample\x00")
    ad = sw.SolidWorksLockAdapter()
    ctx = FileContext(logical_path="x/~$a.SLDPRT")
    assert ad.matches(ctx, src.read_bytes())
    r = ad.rewrite(ctx, str(src), str(dst), DictPlan({"aexample": "user0001"}))
    assert r.status == REWRITTEN and dst.read_bytes() == b"user0001\x00"
