"""Archive round-trips with a fake plan that renames 'Secret' and replaces 'ACME'."""
import gzip
import io
import os
import subprocess
import tarfile
import zipfile

import pytest

from deid import api
from deid.formats import archives
from deid.pipeline import core


class FakePlan:
    version = "fake1"

    def alias(self, value, kind=None):
        return {"jdoe": "user0001", "ACME": "Company-A"}.get(value)

    def replace_text(self, text):
        return text.replace("ACME", "Company-A").replace("jdoe", "user0001")

    def replace_bytes(self, data, encodings=("utf-8",), length_preserving=False):
        return data.replace(b"ACME", b"Company-A")

    def map_path(self, p):
        return p.replace("Secret", "Part0001")

    def map_reference(self, ctx, raw):
        return raw.replace("Secret", "Part0001")


class TextAdapter(api.Adapter):
    """Test-only adapter: .txt members get replace_text."""
    name = "test_txt"
    version = "1"
    priority = 100

    def matches(self, ctx, head):
        return ctx.ext == ".txt"

    def rewrite(self, ctx, src, dst, plan):
        data = open(src, encoding="utf-8").read()
        new = plan.replace_text(data)
        if new == data:
            return api.RewriteResult(api.VERIFIED_UNCHANGED)
        open(dst, "w", encoding="utf-8").write(new)
        return api.RewriteResult(api.REWRITTEN)


@pytest.fixture(autouse=True)
def reg():
    api.register(TextAdapter())
    yield
    api._REGISTRY[:] = [a for a in api._REGISTRY if a.name != "test_txt"]


def run(tmp_path, name, data):
    src = tmp_path / name
    src.write_bytes(data)
    proc = core.Processor(plan=FakePlan(), tmp_root=str(tmp_path))
    ctx, head = proc.make_ctx(f"sources/x/{name}", str(src))
    dst = tmp_path / ("out-" + name)
    r = proc.rewrite(ctx, str(src), str(dst), head)
    assert r["status"] == "rewritten", r
    return dst.read_bytes(), r


def _zip_bytes(files, comment=b""):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
        z.comment = comment
    return b.getvalue()


def test_zip(tmp_path):
    data = _zip_bytes({"dir/Secret.txt": "made by ACME", "keep.bin": b"\x00\x01"}, b"ACME zip")
    out, r = run(tmp_path, "a.zip", data)
    z = zipfile.ZipFile(io.BytesIO(out))
    assert z.namelist() == ["dir/Part0001.txt", "keep.bin"]
    assert z.read("dir/Part0001.txt") == b"made by Company-A"
    assert z.comment == b"Company-A zip"
    assert b"ACME" not in out and b"Secret" not in out


def test_nested_tgz_in_zip(tmp_path):
    tb = io.BytesIO()
    with tarfile.open(fileobj=tb, mode="w:gz") as t:
        d = b"ACME inside"
        ti = tarfile.TarInfo("Secret/readme.txt"); ti.size = len(d); ti.uname = "jdoe"
        t.addfile(ti, io.BytesIO(d))
    data = _zip_bytes({"inner.tgz": tb.getvalue()})
    out, r = run(tmp_path, "b.zip", data)
    z = zipfile.ZipFile(io.BytesIO(out))
    with tarfile.open(fileobj=io.BytesIO(z.read("inner.tgz")), mode="r:gz") as t:
        m = t.getmembers()
        assert [x.name for x in m] == ["Part0001/readme.txt"]
        assert m[0].uname == "user0001"
        assert t.extractfile(m[0]).read() == b"Company-A inside"


def test_gzip_fname(tmp_path):
    b = io.BytesIO()
    with gzip.GzipFile(filename="Secret.txt", mode="wb", fileobj=b, mtime=5) as g:
        g.write(b"ACME")
    out, r = run(tmp_path, "Secret.txt.gz", b.getvalue())
    assert gzip.decompress(out) == b"Company-A"
    assert b"Secret" not in out


def test_compress_Z(tmp_path):
    data = archives.lzw_compress(b"hello ACME " * 1000)
    out, r = run(tmp_path, "x.txt.Z", data)
    assert archives.lzw_decompress(out) == b"hello Company-A " * 1000
    p = subprocess.run(["gzip", "-dc"], input=out, capture_output=True)
    assert p.stdout == b"hello Company-A " * 1000


def test_7z_and_rar_roundtrip(tmp_path):
    import libarchive
    p7 = tmp_path / "src.7z"
    with libarchive.file_writer(str(p7), "7zip") as a:
        a.add_file_from_memory("Secret/a.txt", 4, b"ACME")
    out, r = run(tmp_path, "c.7z", p7.read_bytes())
    (tmp_path / "o.7z").write_bytes(out)
    with libarchive.file_reader(str(tmp_path / "o.7z")) as a:
        got = {e.pathname: b"".join(e.get_blocks()) for e in a if e.isreg}
    assert got == {"Part0001/a.txt": b"Company-A"}
    # RAR4 stored writer readable by libarchive
    f = tmp_path / "m.txt"; f.write_bytes(b"payload" * 100)
    archives.write_rar4_stored(str(tmp_path / "w.rar"), [("d", None, 0), ("d/m.txt", str(f), 1700000000)])
    with libarchive.file_reader(str(tmp_path / "w.rar")) as a:
        got = {e.pathname: b"".join(e.get_blocks()) for e in a if e.isreg}
    assert list(got.values()) == [b"payload" * 100]
    assert list(got)[0].replace("\\", "/") == "d/m.txt"
