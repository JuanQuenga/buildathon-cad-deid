"""Golden rule: an output always exists; failures fall back to the original bytes."""
import os
import time

import pytest

from deid import api
from deid.pipeline import core
from deid.pipeline.pool import Pool


class Boom(api.Adapter):
    name = "t_boom"; version = "1"; priority = 1000

    def matches(self, ctx, head):
        return ctx.ext == ".boom"

    def rewrite(self, ctx, src, dst, plan):
        open(dst, "wb").write(b"partial")
        raise RuntimeError("kaboom")


class BadVerify(api.Adapter):
    name = "t_badverify"; version = "1"; priority = 1000

    def matches(self, ctx, head):
        return ctx.ext == ".badv"

    def rewrite(self, ctx, src, dst, plan):
        open(dst, "wb").write(b"changed")
        return api.RewriteResult(api.REWRITTEN)

    def verify(self, ctx, src, dst, plan):
        return api.VerifyResult(False, ["nope"])


class Slow(api.Adapter):
    name = "t_slow"; version = "1"; priority = 1000; timeout_s = 0.5

    def matches(self, ctx, head):
        return ctx.ext == ".slow"

    def rewrite(self, ctx, src, dst, plan):
        while True:
            time.sleep(0.05)


class Good(api.Adapter):
    name = "t_good"; version = "1"; priority = 1000

    def matches(self, ctx, head):
        return ctx.ext == ".good"

    def rewrite(self, ctx, src, dst, plan):
        open(dst, "wb").write(open(src, "rb").read().upper())
        return api.RewriteResult(api.REWRITTEN)


@pytest.fixture(autouse=True)
def reg():
    for a in (Boom(), BadVerify(), Slow(), Good()):
        api.register(a)
    yield
    api._REGISTRY[:] = [a for a in api._REGISTRY if not a.name.startswith("t_")]


@pytest.mark.parametrize("ext,status,content", [
    (".boom", "fallback_copy", b"original"),
    (".badv", "fallback_copy", b"original"),
    (".slow", "fallback_copy", b"original"),
    (".good", "rewritten", b"ORIGINAL"),
    (".unknownext", "copied_unsupported", b"\x00\x01\x02\x03binary"),
])
def test_golden_rule(tmp_path, ext, status, content):
    src = tmp_path / f"f{ext}"
    # binary bytes for the no-adapter case (the generic text adapter claims plain text)
    src.write_bytes(content if ext == ".unknownext" else b"original")
    proc = core.Processor(tmp_root=str(tmp_path))
    ctx, head = proc.make_ctx(f"x/f{ext}", str(src))
    dst = tmp_path / "out" / f"f{ext}"
    r = proc.rewrite(ctx, str(src), str(dst), head)
    assert r["status"] == status, r
    assert dst.read_bytes() == content
    assert not any(p.name.endswith((".deid-new", ".deid-tmp")) for p in (tmp_path / "out").iterdir())


def test_empty_file(tmp_path):
    src = tmp_path / "e.step"
    src.write_bytes(b"")
    proc = core.Processor(tmp_root=str(tmp_path))
    ctx, head = proc.make_ctx("x/e.step", str(src))
    r = proc.rewrite(ctx, str(src), str(tmp_path / "o.step"), head)
    assert r["status"] == "source_exception" and (tmp_path / "o.step").exists()


def _init(_):
    def h(p):
        if p == "hang":
            time.sleep(60)
        if p == "die":
            os._exit(1)
        return {"ok": p}
    return h


def test_pool_timeout_and_crash():
    pool = Pool(2, _init, (None,))
    try:
        res = {tid: r for tid, _, r in pool.run([("a", "x", 5), ("b", "hang", 0.5), ("c", "die", 5), ("d", "y", 5)])}
    finally:
        pool.close()
    assert res["a"] == {"ok": "x"} and res["d"] == {"ok": "y"}
    assert res["b"] == {"_pool_error": "timeout"}
    assert res["c"] == {"_pool_error": "crash"}
