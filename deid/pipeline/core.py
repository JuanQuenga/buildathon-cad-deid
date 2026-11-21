"""Per-file processing with the golden rule (runs inside worker processes).

Used for top-level corpus files and, through ctx.dispatch_*, for archive members.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import signal
import tempfile
import time
import traceback
from dataclasses import asdict

from deid import api
from deid.api import (FileContext, RewriteResult, ScanResult, FALLBACK_COPY, REWRITTEN,
                      COPIED_UNSUPPORTED, SOURCE_EXCEPTION, VERIFIED_UNCHANGED)

FICLONE = 0x40049409


class Timeout(Exception):
    pass


def fastcopy(src: str, dst: str) -> None:
    """Copy file bytes; reflink on Btrfs/XFS when possible. Atomic via tmp+rename."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = dst + ".deid-tmp"
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        try:
            fcntl.ioctl(fo.fileno(), FICLONE, fi.fileno())
        except OSError:
            shutil.copyfileobj(fi, fo, 1 << 22)
    os.replace(tmp, dst)


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_head(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(api.HEAD_BYTES)


def namespace_of(logical_path: str) -> str:
    parts = logical_path.split("!/", 1)[0].split("/")
    if parts[0] == "sources" and len(parts) > 2:
        return "/".join(parts[:3])
    return parts[0]


class _alarm:
    """In-process soft timeout via SIGALRM (main thread only)."""

    def __init__(self, seconds: float):
        self.seconds = seconds

    def __enter__(self):
        self.prev = None
        if self.seconds and self.seconds > 0:
            try:
                self.prev = signal.signal(signal.SIGALRM, self._raise)
                self.prev_timer = signal.setitimer(signal.ITIMER_REAL, self.seconds)
            except ValueError:  # not main thread
                self.prev = None
        return self

    @staticmethod
    def _raise(signum, frame):
        raise Timeout("timeout")

    def __exit__(self, *a):
        if self.prev is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.prev)
        return False


def timeout_for(adapter, size: int) -> float:
    base = getattr(adapter, "timeout_s", 300.0) if adapter else 60.0
    return base + size / (2 * 1024 * 1024)  # + 1 s per 2 MiB


class Processor:
    """Holds the plan and does scan/rewrite for one file (and its members)."""

    def __init__(self, plan=None, tmp_root: str = None, depth_limit: int = 6):
        self.plan = plan or api.NullPlan()
        self.tmp_root = tmp_root or tempfile.gettempdir()
        self.depth_limit = depth_limit

    # -- context ------------------------------------------------------------
    def make_ctx(self, logical_path: str, src: str, chain=(), head=None, sha=None) -> tuple:
        head = read_head(src) if head is None else head
        size = os.path.getsize(src)
        magic = api.sniff(head, logical_path.rsplit("/", 1)[-1])
        ctx = FileContext(logical_path=logical_path, container_chain=tuple(chain), magic=magic,
                          size=size, source_namespace=namespace_of(logical_path),
                          sha256=sha or "")
        ctx.dispatch_scan = self.dispatch_scan
        ctx.dispatch_rewrite = self.dispatch_rewrite
        return ctx, head

    def _tmpdir(self, ctx):
        return tempfile.mkdtemp(prefix="j", dir=self.tmp_root)

    # -- scan ---------------------------------------------------------------
    def scan(self, ctx: FileContext, src: str, head: bytes) -> tuple:
        """Returns (adapter_name, adapter_version, ScanResult, error, seconds)."""
        t0 = time.monotonic()
        adapter = api.route(ctx, head) if ctx.magic != "empty" else None
        if adapter is None:
            st = "source_exception" if ctx.magic == "empty" else "unsupported"
            return ("", "", ScanResult(status=st), None, time.monotonic() - t0)
        own_tmp = not ctx.tmp_dir
        if own_tmp:
            ctx.tmp_dir = self._tmpdir(ctx)
        try:
            with _alarm(timeout_for(adapter, ctx.size) if not ctx.container_chain else 0):
                res = adapter.scan(ctx, src)
            if not isinstance(res, ScanResult):
                raise TypeError("scan() must return ScanResult")
            return (adapter.name, adapter.version, res, None, time.monotonic() - t0)
        except Exception as e:  # noqa
            return (adapter.name, adapter.version, ScanResult(status="error"),
                    f"{type(e).__name__}: {str(e)[:200]}", time.monotonic() - t0)
        finally:
            if own_tmp:
                shutil.rmtree(ctx.tmp_dir, ignore_errors=True)
                ctx.tmp_dir = ""

    def dispatch_scan(self, ctx: FileContext, src: str) -> ScanResult:
        """For containers: scan a member. ctx comes from parent_ctx.child(name)."""
        head = read_head(src)
        ctx.magic = api.sniff(head, ctx.member_name.rsplit("/", 1)[-1])
        ctx.size = os.path.getsize(src)
        ctx.dispatch_scan, ctx.dispatch_rewrite = self.dispatch_scan, self.dispatch_rewrite
        if len(ctx.container_chain) > self.depth_limit:
            return ScanResult(status="unsupported", notes=["nesting too deep"])
        saved_tmp = ctx.tmp_dir
        ctx.tmp_dir = ""  # members get their own scratch dir
        name, ver, res, err, secs = self.scan(ctx, src, head)
        ctx.tmp_dir = saved_tmp
        res.fingerprint.setdefault("_adapter", name)
        if err:
            res.notes.append("scan error: " + err)
        return res

    # -- rewrite ------------------------------------------------------------
    def rewrite(self, ctx: FileContext, src: str, dst: str, head: bytes) -> dict:
        """Golden rule: dst always exists afterwards. Returns a result dict."""
        t0 = time.monotonic()
        out = {"status": None, "adapter": "", "adapter_version": "", "counts": {}, "notes": [],
               "error": None, "children": []}
        adapter = api.route(ctx, head) if ctx.magic != "empty" else None
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if adapter is None:
            fastcopy(src, dst)
            out["status"] = SOURCE_EXCEPTION if ctx.magic == "empty" else COPIED_UNSUPPORTED
            if ctx.magic == "empty":
                out["notes"].append("empty source file")
            out["seconds"] = time.monotonic() - t0
            return out
        out["adapter"], out["adapter_version"] = adapter.name, adapter.version
        own_tmp = not ctx.tmp_dir
        if own_tmp:
            ctx.tmp_dir = self._tmpdir(ctx)
        tmp_dst = dst + ".deid-new"
        try:
            if os.path.exists(tmp_dst):
                os.unlink(tmp_dst)
            with _alarm(timeout_for(adapter, ctx.size) if not ctx.container_chain else 0):
                res = adapter.rewrite(ctx, src, tmp_dst, self.plan)
                if not isinstance(res, RewriteResult) or res.status not in api.ADAPTER_STATUSES:
                    raise TypeError(f"bad rewrite result {getattr(res, 'status', res)!r}")
                out["counts"], out["notes"] = dict(res.counts), list(res.notes)
                out["children"] = [(p, _rr_dict(r)) for p, r in res.children]
                if res.status == REWRITTEN:
                    if not os.path.exists(tmp_dst):
                        raise FileNotFoundError("adapter returned rewritten but wrote no output")
                    v = adapter.verify(ctx, src, tmp_dst, self.plan)
                    if not v.ok:
                        out["notes"] += [f"verify failed: {n}" for n in v.notes][:5] or ["verify failed"]
                        raise _VerifyFailed()
                    os.replace(tmp_dst, dst)
                    out["status"] = REWRITTEN
                else:
                    if os.path.exists(tmp_dst):
                        os.unlink(tmp_dst)
                    fastcopy(src, dst)
                    out["status"] = res.status
        except _VerifyFailed:
            _fallback(src, dst, tmp_dst)
            out["status"], out["error"] = FALLBACK_COPY, "verify_failed"
        except BaseException as e:  # noqa: includes Timeout
            if isinstance(e, KeyboardInterrupt):
                raise
            _fallback(src, dst, tmp_dst)
            out["status"] = FALLBACK_COPY
            out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            out["trace"] = traceback.format_exc(limit=6)[-1500:]
        finally:
            if own_tmp:
                shutil.rmtree(ctx.tmp_dir, ignore_errors=True)
                ctx.tmp_dir = ""
        out["seconds"] = time.monotonic() - t0
        return out

    def dispatch_rewrite(self, ctx: FileContext, src: str, dst: str, plan=None) -> RewriteResult:
        head = read_head(src)
        ctx.magic = api.sniff(head, ctx.member_name.rsplit("/", 1)[-1])
        ctx.size = os.path.getsize(src)
        ctx.dispatch_scan, ctx.dispatch_rewrite = self.dispatch_scan, self.dispatch_rewrite
        if len(ctx.container_chain) > self.depth_limit:
            fastcopy(src, dst)
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=["nesting too deep"])
        saved_tmp = ctx.tmp_dir
        ctx.tmp_dir = ""  # members get their own scratch dir
        r = self.rewrite(ctx, src, dst, head)
        ctx.tmp_dir = saved_tmp
        rr = RewriteResult(status=r["status"] if r["status"] != FALLBACK_COPY else COPIED_UNSUPPORTED,
                           counts=r["counts"], notes=r["notes"] + ([r["error"]] if r["error"] else []))
        rr.counts.setdefault("_adapter", r["adapter"])
        rr.counts["_status"] = r["status"]
        rr.children = r.get("children", [])
        return rr


class _VerifyFailed(Exception):
    pass


def _fallback(src, dst, tmp_dst):
    try:
        if os.path.exists(tmp_dst):
            os.unlink(tmp_dst)
    except OSError:
        pass
    fastcopy(src, dst)


def _rr_dict(r) -> dict:
    if isinstance(r, dict):
        return r
    d = {"status": r.status, "counts": r.counts, "notes": r.notes}
    if r.children:
        d["children"] = [(p, _rr_dict(c)) for p, c in r.children]
    return d


def scan_to_rows(path: str, res: ScanResult) -> dict:
    """Flatten a ScanResult (with children) into DB-ready rows."""
    obs, refs, files = [], [], []

    def walk(p, r: ScanResult):
        for o in r.observations:
            obs.append((p, o.kind, o.field, o.value, o.evidence, float(o.confidence)))
        for x in r.references:
            refs.append((p, x.raw, x.kind, x.field))
        for cp, cr in r.children:
            files.append((cp, cr))
            walk(cp, cr)
    walk(path, res)
    return {"obs": obs, "refs": refs,
            "children": [(cp, cr.status, json.dumps(cr.fingerprint, default=str, sort_keys=True),
                          json.dumps(cr.notes, default=str)) for cp, cr in files]}
