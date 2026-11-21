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


def _cstring(value):
    return b"\xff\xfe\xff" + bytes([len(value)]) + value.encode("utf-16-le")


def test_hidden_solidworks_saver_and_header_use_same_alias(tmp_path):
    from deid.formats.cfb import FMTID_USER

    class FieldOnlyPlan(DictPlan):
        def alias(self, value, kind=None):
            return super().alias(value, kind) if kind == "saved_account" else None

        def replace_text(self, text):
            return text

        def replace_bytes(self, data, encodings=("utf-8",), length_preserving=False):
            return data

    src, dst = tmp_path / "a.sldprt", tmp_path / "b.sldprt"
    login, alias = "qx", "vz"
    plan = FieldOnlyPlan({login: alias})
    folder = r"C:\Users\qx\work"
    hidden = propset(uuid.UUID(FMTID_USER).bytes_le, [(19, login), (20, login), (21, folder)],
                     names={19: "SW-Lokaler Name(Last Saved By)", 20: "Material", 21: "Folder Name"},
                     pad=False)
    header = bytes(39) + _cstring(login.upper())
    preview = b"opaque preview " + login.encode()
    write_cfb(src, [("ISolidWorksInformation", hidden), ("Header2", header), ("Preview", preview)])
    ctx = FileContext(logical_path="a.sldprt", magic="cfb", tmp_dir=str(tmp_path))
    adapter = CfbAdapter()

    scan = adapter.scan(ctx, str(src))
    assert {(o.kind, o.value) for o in scan.observations} >= {
        ("saved_account", login), ("saved_account", login.upper()),
    }
    assert adapter.rewrite(ctx, str(src), str(dst), plan).status == REWRITTEN
    assert adapter.verify(ctx, str(src), str(dst), plan).ok
    with olefile.OleFileIO(src) as old, olefile.OleFileIO(dst) as new:
        assert old.listdir() == new.listdir()
        assert new.get_size("ISolidWorksInformation") < new.minisectorcutoff
        _, props, _ = parse_propset(new.openstream("ISolidWorksInformation").read())
        assert {p.pid: p.value for p in props} == {19: alias, 20: login, 21: r"C:\Users\vz\work"}
        assert new.openstream("Header2").read() == bytes(39) + _cstring(alias.upper())
        assert new.openstream("Preview").read() == preview


def test_unicode_codepage_and_inventor_save_history(tmp_path):
    from deid.formats.cfb import FMTID_INV_TRACKING_CONTROL
    from deid.identity.plan import IdentityPlan

    src, dst = tmp_path / "a.ipt", tmp_path / "b.ipt"
    plan = IdentityPlan([], b"synthetic key for CFB tests")
    login = "Qxzévue"
    alias = plan.alias(login, "saved_account")
    assert alias != login
    key = "\x05{" + FMTID_INV_TRACKING_CONTROL + "}"
    # The directory entry name cannot exceed 31 UTF-16 code units.
    key = key[:30]
    body = propset(uuid.UUID(FMTID_INV_TRACKING_CONTROL).bytes_le,
                   [(16, login), (17, "unchanged")], codepage=1200, pad=False)
    copy = _cstring(login)
    write_cfb(src, [(key, body), ("UFRxDoc", copy), ("Thumbnail", copy)])
    ctx = FileContext(logical_path="a.ipt", magic="cfb", tmp_dir=str(tmp_path))
    adapter = CfbAdapter()

    assert adapter.rewrite(ctx, str(src), str(dst), plan).status == REWRITTEN
    assert adapter.verify(ctx, str(src), str(dst), plan).ok
    with olefile.OleFileIO(dst) as output:
        _, props, _ = parse_propset(output.openstream(key).read())
        assert {p.pid: p.value for p in props} == {16: alias, 17: "unchanged"}
        assert output.openstream("UFRxDoc").read() == _cstring(alias)
        assert output.openstream("Thumbnail").read() == copy


def test_exact_zlb_xml_roundtrip():
    import re
    import struct
    import zlib

    from deid.formats import cfb_accounts, cfb_zlb
    from deid.identity.plan import IdentityPlan

    plan = IdentityPlan([], b"synthetic key for CFB tests")
    login = "qx"
    aliases = cfb_accounts.AccountAliases(plan, [login])
    alias = plan.alias(login, "saved_account")
    raw = b'<doc lastSavedBy="qx" record="Bracket.ipt"/>'
    packed = cfb_zlb._deflate_exact(raw, len(zlib.compress(raw)) + 18)
    assert packed is not None
    wrapper = cfb_zlb.MAGIC + struct.pack("<II", len(raw), len(packed)) + packed + bytes(8)
    new = cfb_zlb.rewrite(wrapper, aliases, re.compile(r"\.ipt"))
    assert new is not None
    assert new[:24] == wrapper[:24] and new[-8:] == wrapper[-8:]
    assert len(new) == len(wrapper)
    assert cfb_zlb.payload(new) == raw.replace(b'="qx"', b'="' + alias.encode() + b'"')
    assert cfb_zlb.rewrite(wrapper + b"opaque next record", aliases, re.compile(r"\.ipt")) is None
