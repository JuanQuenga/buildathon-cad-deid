"""Archives: zip, tar (+gz/tgz), gzip, UNIX compress (.Z), 7z, rar.

Members go through the same registry via ctx.dispatch_scan / ctx.dispatch_rewrite,
so nested archives and every format adapter apply inside archives. Member names
are mapped with plan.map_path("<archive logical path>!/<member>").

Archives are written back in the same format: zip->zip, tar->tar, gzip->gzip,
.Z->.Z (own LZW encoder), 7z->7z (libarchive). RAR is written as a RAR 4.x
archive with stored (uncompressed) entries (own writer; no free RAR compressor
exists). Encrypted or truncated archives are copied unchanged and reported as
source exceptions.

Safety: members are never extracted under their own names (numbered scratch
files only), so traversal names are harmless; symlinks are not followed;
sizes are bounded (bomb guard).
"""
from __future__ import annotations

import gzip
import io
import os
import shutil
import stat
import struct
import tarfile
import time
import zipfile
import zlib

from deid import api
from deid.api import (Adapter, FileContext, Observation, Reference, RewriteResult, ScanResult,
                      VerifyResult, register, REWRITTEN, SOURCE_EXCEPTION, COPIED_UNSUPPORTED,
                      VERIFIED_UNCHANGED)

MAX_MEMBER = 8 << 30          # 8 GiB per member
MAX_TOTAL = 40 << 30          # 40 GiB expanded per archive
MAX_RATIO = 2000              # expanded / compressed
MAX_MEMBERS = 400_000
CHUNK = 1 << 20

# zip package suffixes handled by docs-native package adapters (higher priority);
# when those adapters are absent, the generic zip adapter handles them as zips.
ZIP_PACKAGE_EXTS = (".fcstd", ".3mf", ".f3d", ".f3z", ".odg", ".docx", ".xlsx", ".pptx")


class BombError(Exception):
    pass


class SourceDefect(Exception):
    """Archive cannot be read completely (encrypted, truncated, corrupt)."""


def _member_out_name(plan, ctx: FileContext, member: str) -> str:
    """Mapped member name inside the rewritten archive."""
    if not member:
        return member
    trail = member.endswith("/")
    m = member.rstrip("/")
    if not m:
        return member
    mapped = plan.map_path(f"{ctx.logical_path}!/{m}")
    new = mapped.rsplit("!/", 1)[-1] if "!/" in mapped else m
    return new + ("/" if trail else "")


def _text(plan, s):
    if not s:
        return s
    try:
        return plan.replace_text(s)
    except Exception:
        return s


class _Budget:
    def __init__(self, compressed_size):
        self.total = 0
        self.limit = min(MAX_TOTAL, max(compressed_size, 1 << 20) * MAX_RATIO)

    def add(self, n):
        self.total += n
        if self.total > self.limit:
            raise BombError("expansion limit exceeded")


def _copy_stream(fi, path, budget, limit=MAX_MEMBER):
    n = 0
    with open(path, "wb") as fo:
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            n += len(b)
            if n > limit:
                raise BombError("member too large")
            budget.add(len(b))
            fo.write(b)
    return n


class _Members:
    """Helper that scans/rewrites one extracted member through the registry."""

    def __init__(self, ctx, plan=None):
        self.ctx, self.plan = ctx, plan
        self.i = 0
        self.children = []
        self.counts = {"members": 0, "members_rewritten": 0, "members_renamed": 0}

    def paths(self):
        self.i += 1
        return (os.path.join(self.ctx.tmp_dir, f"m{self.i}"), os.path.join(self.ctx.tmp_dir, f"o{self.i}"))

    def scan(self, name, src):
        child = self.ctx.child(name)
        if self.ctx.dispatch_scan is None:
            return
        res = self.ctx.dispatch_scan(child, src)
        self.children.append((child.logical_path, res))

    def rewrite(self, name, src, dst):
        child = self.ctx.child(name)
        self.counts["members"] += 1
        if self.ctx.dispatch_rewrite is None:
            shutil.copyfile(src, dst)
            return None
        r = self.ctx.dispatch_rewrite(child, src, dst, self.plan)
        st = r.counts.get("_status", r.status) if r.counts else r.status
        if r.status == REWRITTEN:
            self.counts["members_rewritten"] += 1
        self.counts[f"member_{st}"] = self.counts.get(f"member_{st}", 0) + 1
        self.children.append((child.logical_path, r))
        return r


def _observe_name(obs, field, value):
    if value:
        obs.append(Observation(kind="free_text", field=field, value=value, confidence=0.3))


# ===========================================================================
# ZIP
# ===========================================================================
_KEEP_EXTRA = {0x5455, 0x000A}   # extended timestamp, NTFS times


def _clean_extra(extra: bytes) -> bytes:
    out, i = [], 0
    while i + 4 <= len(extra):
        hid, ln = struct.unpack("<HH", extra[i:i + 4])
        if hid in _KEEP_EXTRA and i + 4 + ln <= len(extra):
            out.append(extra[i:i + 4 + ln])
        i += 4 + ln
    return b"".join(out)


def _zip_decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp437")


class ZipAdapter(Adapter):
    name = "zip"
    version = "1"
    priority = 0
    magics = ("zip",)
    extensions = (".zip",)
    timeout_s = 3600.0

    def matches(self, ctx, head):
        return ctx.magic == "zip"

    def _open(self, src):
        try:
            z = zipfile.ZipFile(src)
        except (zipfile.BadZipFile, OSError, ValueError) as e:
            raise SourceDefect(f"unreadable zip ({type(e).__name__})")
        if any(i.flag_bits & 0x1 for i in z.infolist()):
            z.close()
            raise SourceDefect("encrypted zip members")
        if len(z.infolist()) > MAX_MEMBERS:
            z.close()
            raise SourceDefect("too many members")
        return z

    def scan(self, ctx, src):
        try:
            z = self._open(src)
        except SourceDefect as e:
            return ScanResult(status="source_exception", notes=[str(e)])
        obs = []
        m = _Members(ctx)
        budget = _Budget(ctx.size)
        with z:
            _observe_name(obs, "zip.comment", _zip_decode(z.comment))
            n = 0
            for info in z.infolist():
                _observe_name(obs, "zip.member_comment", _zip_decode(info.comment))
                if info.is_dir() or _zip_is_symlink(info):
                    continue
                src_m, _ = m.paths()
                try:
                    with z.open(info) as fi:
                        _copy_stream(fi, src_m, budget)
                except NotImplementedError:
                    return self._scan_libarchive(ctx, src, obs)
                except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as e:
                    return ScanResult(status="source_exception", observations=obs, children=m.children,
                                      notes=[f"unreadable member ({type(e).__name__})"])
                m.scan(info.filename, src_m)
                os.unlink(src_m)
                n += 1
        return ScanResult(observations=obs, children=m.children,
                          fingerprint={"members": len(z.infolist()), "files": n})

    def _scan_libarchive(self, ctx, src, obs):
        m = _Members(ctx)

        def on_file(meta, p):
            if p:
                m.scan(meta["name"], p)
                os.unlink(p)
        try:
            _la_entries(src, ctx.tmp_dir, _Budget(ctx.size), on_file)
        except (SourceDefect, BombError) as e:
            return ScanResult(status="source_exception", observations=obs, children=m.children, notes=[str(e)])
        return ScanResult(observations=obs, children=m.children, fingerprint={"files": len(m.children)})

    def rewrite(self, ctx, src, dst, plan):
        try:
            z = self._open(src)
        except SourceDefect as e:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[str(e)])
        try:
            with z:
                return self._rewrite_zipfile(ctx, z, dst, plan)
        except NotImplementedError:
            # a compression method zipfile cannot read (e.g. zstd 93, deflate64 9): libarchive
            return self._rewrite_libarchive(ctx, src, dst, plan)

    def _rewrite_zipfile(self, ctx, z, dst, plan):
        m = _Members(ctx, plan)
        budget = _Budget(ctx.size)
        changed = False
        with zipfile.ZipFile(dst, "w", allowZip64=True) as zo:
            if z.comment:
                c = _text(plan, _zip_decode(z.comment))
                changed |= c != _zip_decode(z.comment)
                zo.comment = c.encode("utf-8")
            for info in z.infolist():
                new_name = _member_out_name(plan, ctx, info.filename)
                if new_name != info.filename:
                    m.counts["members_renamed"] += 1
                    changed = True
                ni = zipfile.ZipInfo(new_name, date_time=info.date_time)
                ni.external_attr = info.external_attr
                ni.create_system = info.create_system
                ni.compress_type = info.compress_type if info.compress_type in (
                    zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA) \
                    else zipfile.ZIP_DEFLATED
                # extras such as the Info-ZIP unicode path (0x7075) repeat the old name: drop on rename
                ni.extra = _clean_extra(info.extra) if new_name != info.filename else info.extra
                if info.comment:
                    c = _text(plan, _zip_decode(info.comment))
                    changed |= c != _zip_decode(info.comment)
                    ni.comment = c.encode("utf-8")
                if info.is_dir():
                    zo.writestr(ni, b"")
                    continue
                if _zip_is_symlink(info):
                    target = z.read(info).decode("utf-8", "replace")
                    nt = plan.map_reference(ctx.child(info.filename), target)
                    changed |= nt != target
                    zo.writestr(ni, nt.encode("utf-8"))
                    continue
                src_m, dst_m = m.paths()
                try:
                    with z.open(info) as fi:
                        _copy_stream(fi, src_m, budget)
                except NotImplementedError:
                    raise
                except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as e:
                    raise SourceDefect(f"unreadable member ({type(e).__name__})")
                r = m.rewrite(info.filename, src_m, dst_m)
                changed |= r is not None and r.status == REWRITTEN
                with open(dst_m, "rb") as fi, zo.open(ni, "w", force_zip64=os.path.getsize(dst_m) > (1 << 31)) as fo:
                    shutil.copyfileobj(fi, fo, CHUNK)
                os.unlink(src_m)
                os.unlink(dst_m)
        st = REWRITTEN if changed else VERIFIED_UNCHANGED
        return RewriteResult(status=st, counts=m.counts, children=m.children)

    def _rewrite_libarchive(self, ctx, src, dst, plan):
        m = _Members(ctx, plan)
        infos = {}
        with zipfile.ZipFile(src) as z:
            for i in z.infolist():
                infos.setdefault(i.filename, i)
        state = {"changed": False}
        zo = zipfile.ZipFile(dst, "w", allowZip64=True)

        def on_file(meta, p):
            name = meta["name"]
            info = infos.get(name) or infos.get(name + "/")
            new_name = _member_out_name(plan, ctx, info.filename if info else name)
            if info is None or new_name != info.filename:
                state["changed"] = True
                m.counts["members_renamed"] += 1
            ni = zipfile.ZipInfo(new_name, date_time=info.date_time if info else (1980, 1, 1, 0, 0, 0))
            if info:
                ni.external_attr, ni.create_system = info.external_attr, info.create_system
                ni.extra = _clean_extra(info.extra)
            ni.compress_type = zipfile.ZIP_DEFLATED
            if p is None:
                zo.writestr(ni, b"")
                return
            outp = p + ".out"
            r = m.rewrite(name, p, outp)
            state["changed"] |= r is not None and r.status == REWRITTEN
            with open(outp, "rb") as fi, zo.open(ni, "w", force_zip64=os.path.getsize(outp) > (1 << 31)) as fo:
                shutil.copyfileobj(fi, fo, CHUNK)
            os.unlink(p)
            os.unlink(outp)
        try:
            with zo:
                _la_entries(src, ctx.tmp_dir, _Budget(ctx.size), on_file)
        except SourceDefect as e:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[str(e)])
        m.counts["recompressed_deflate"] = 1
        # recompression (zstd/deflate64 -> deflate) alone is not a reason to rewrite
        st = REWRITTEN if state["changed"] else VERIFIED_UNCHANGED
        return RewriteResult(status=st, counts=m.counts, children=m.children)

    def verify(self, ctx, src, dst, plan):
        try:
            with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
                bad = b.testzip()
                if bad is not None:
                    return VerifyResult(False, ["crc error in output"])
                na, nb = len(a.infolist()), len(b.infolist())
                if na != nb:
                    return VerifyResult(False, [f"member count {na} -> {nb}"])
                exp = [_member_out_name(plan, ctx, i.filename) for i in a.infolist()]
                got = [i.filename for i in b.infolist()]
                if exp != got:
                    return VerifyResult(False, ["member names differ from plan"])
            return VerifyResult(True, fingerprint={"members": nb})
        except Exception as e:
            return VerifyResult(False, [f"{type(e).__name__}"])


def _zip_is_symlink(info):
    return info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16)


# ===========================================================================
# TAR (plain, and as the payload of gzip/compress handled by GzipAdapter)
# ===========================================================================
def _is_tar_header(head: bytes) -> bool:
    if len(head) < 512:
        return False
    blk = head[:512]
    if blk[257:262] == b"ustar":
        return True
    try:
        chk = int(blk[148:156].replace(b"\x00", b" ").strip() or b"-1", 8)
    except ValueError:
        return False
    s = sum(blk) - sum(blk[148:156]) + 8 * 32
    return chk == s and blk[0] != 0


def _tar_format(head: bytes):
    if head[257:265] == b"ustar  \x00":
        return tarfile.GNU_FORMAT
    return tarfile.PAX_FORMAT


class TarAdapter(Adapter):
    name = "tar"
    version = "1"
    priority = 0
    magics = ("tar",)
    extensions = (".tar",)
    timeout_s = 3600.0

    def matches(self, ctx, head):
        return ctx.magic == "tar" or (ctx.magic in ("binary", "text") and _is_tar_header(head))

    def scan(self, ctx, src):
        obs = []
        m = _Members(ctx)
        budget = _Budget(ctx.size)
        try:
            with tarfile.open(src, "r:") as t:
                n = 0
                for ti in t:
                    for k in ("uname", "gname"):
                        v = getattr(ti, k)
                        if v:
                            obs.append(Observation(kind="account", field=f"tar.{k}", value=v, confidence=0.6))
                    for k, v in (ti.pax_headers or {}).items():
                        if k not in ("path", "linkpath", "size", "mtime", "atime", "ctime", "uid", "gid"):
                            _observe_name(obs, f"tar.pax.{k}", v)
                    if not ti.isreg():
                        continue
                    n += 1
                    src_m, _ = m.paths()
                    _copy_stream(t.extractfile(ti), src_m, budget)
                    m.scan(ti.name, src_m)
                    os.unlink(src_m)
        except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
            return ScanResult(status="source_exception", observations=obs, children=m.children,
                              notes=[f"unreadable tar ({type(e).__name__})"])
        return ScanResult(observations=obs, children=m.children, fingerprint={"files": n})

    def rewrite(self, ctx, src, dst, plan):
        with open(src, "rb") as f:
            head = f.read(512)
        fmt = _tar_format(head)
        m = _Members(ctx, plan)
        budget = _Budget(ctx.size)
        try:
            tin = tarfile.open(src, "r:")
        except (tarfile.TarError, OSError) as e:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[f"unreadable tar ({type(e).__name__})"])
        changed = False
        with tin, tarfile.open(dst, "w:", format=fmt, encoding="utf-8") as tout:
            for ti in tin:
                no = tarfile.TarInfo(_member_out_name(plan, ctx, ti.name))
                if no.name != ti.name:
                    m.counts["members_renamed"] += 1
                    changed = True
                no.mode, no.mtime, no.type = ti.mode, ti.mtime, ti.type
                no.uid, no.gid = ti.uid, ti.gid
                no.uname = (plan.alias(ti.uname, "account") or _text(plan, ti.uname)) if ti.uname else ""
                no.gname = (plan.alias(ti.gname, "account") or _text(plan, ti.gname)) if ti.gname else ""
                no.devmajor, no.devminor = ti.devmajor, ti.devminor
                pax = {k: _text(plan, v) for k, v in (ti.pax_headers or {}).items()
                       if k not in ("path", "linkpath", "uname", "gname", "size")}
                no.pax_headers = pax
                if ti.islnk():
                    no.linkname = _member_out_name(plan, ctx, ti.linkname)
                elif ti.issym():
                    no.linkname = plan.map_reference(ctx.child(ti.name), ti.linkname)
                changed |= (no.uname != ti.uname or no.gname != ti.gname or no.linkname != ti.linkname
                            or any(pax.get(k) != v for k, v in (ti.pax_headers or {}).items() if k in pax))
                if not ti.isreg():
                    tout.addfile(no)
                    continue
                src_m, dst_m = m.paths()
                _copy_stream(tin.extractfile(ti), src_m, budget)
                r = m.rewrite(ti.name, src_m, dst_m)
                changed |= r is not None and r.status == REWRITTEN
                no.size = os.path.getsize(dst_m)
                with open(dst_m, "rb") as fo:
                    tout.addfile(no, fo)
                os.unlink(src_m)
                os.unlink(dst_m)
        return RewriteResult(status=REWRITTEN if changed else VERIFIED_UNCHANGED, counts=m.counts,
                             children=m.children)

    def verify(self, ctx, src, dst, plan):
        try:
            with tarfile.open(src, "r:") as a, tarfile.open(dst, "r:") as b:
                ia, ib = a.getmembers(), b.getmembers()
                if len(ia) != len(ib):
                    return VerifyResult(False, [f"member count {len(ia)} -> {len(ib)}"])
                for x, y in zip(ia, ib):
                    if x.type != y.type or _member_out_name(plan, ctx, x.name) != y.name:
                        return VerifyResult(False, ["member list differs"])
                    if y.isreg():
                        f = b.extractfile(y)
                        n = 0
                        while True:
                            c = f.read(CHUNK)
                            if not c:
                                break
                            n += len(c)
                        if n != y.size:
                            return VerifyResult(False, ["truncated member"])
            return VerifyResult(True, fingerprint={"members": len(ib)})
        except Exception as e:
            return VerifyResult(False, [type(e).__name__])


# ===========================================================================
# GZIP and UNIX compress: single-stream wrappers around one payload
# ===========================================================================
def _gzip_header(path):
    """Parse the first gzip member header: (mtime, fname bytes|None, comment|None, xfl, os)."""
    with open(path, "rb") as f:
        h = f.read(10)
        if len(h) < 10 or h[:2] != b"\x1f\x8b":
            raise SourceDefect("not gzip")
        flg, mtime, xfl, osb = h[3], struct.unpack("<I", h[4:8])[0], h[8], h[9]
        if flg & 4:
            xlen = struct.unpack("<H", f.read(2))[0]
            f.read(xlen)

        def z():
            b = bytearray()
            while True:
                c = f.read(1)
                if not c or c == b"\x00":
                    return bytes(b)
                b += c
        fname = z() if flg & 8 else None
        comment = z() if flg & 16 else None
    return mtime, fname, comment, xfl, osb


def _inner_name(ctx: FileContext, fname):
    base = ctx.member_name.rsplit("/", 1)[-1]
    low = base.lower()
    if low.endswith(".tgz"):
        return base[:-4] + ".tar"
    for suf in (".gz", ".z"):
        if low.endswith(suf):
            return base[: -len(suf)]
    if fname:
        return fname.decode("latin-1").rsplit("/", 1)[-1]
    return base + ".out"


class _StreamAdapter(Adapter):
    """Common logic for single-payload compressors."""
    timeout_s = 3600.0

    def _decompress(self, src, out, budget):
        raise NotImplementedError

    def _compress(self, src, dst, ctx, plan, meta):
        raise NotImplementedError

    def _meta(self, src):
        return {}

    def scan(self, ctx, src):
        m = _Members(ctx)
        obs = []
        try:
            meta = self._meta(src)
            for k in ("fname", "comment"):
                if meta.get(k):
                    _observe_name(obs, f"{self.name}.{k}", meta[k].decode("latin-1"))
            src_m, _ = m.paths()
            self._decompress(src, src_m, _Budget(ctx.size))
        except (SourceDefect, OSError, EOFError, zlib.error, ValueError) as e:
            return ScanResult(status="source_exception", observations=obs,
                              notes=[f"unreadable {self.name} stream ({type(e).__name__})"])
        m.scan(_inner_name(ctx, meta.get("fname")), src_m)
        os.unlink(src_m)
        return ScanResult(observations=obs, children=m.children, fingerprint={"payload": 1})

    def rewrite(self, ctx, src, dst, plan):
        m = _Members(ctx, plan)
        try:
            meta = self._meta(src)
            src_m, dst_m = m.paths()
            self._decompress(src, src_m, _Budget(ctx.size))
        except (SourceDefect, OSError, EOFError, zlib.error, ValueError) as e:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[f"unreadable {self.name} stream ({type(e).__name__})"])
        inner = _inner_name(ctx, meta.get("fname"))
        r = m.rewrite(inner, src_m, dst_m)
        renamed = bool(meta.get("fname")) and _member_out_name(plan, ctx, inner) != inner
        if (r is not None and r.status != REWRITTEN and not renamed and not meta.get("comment")):
            return RewriteResult(status=VERIFIED_UNCHANGED, counts=m.counts, children=m.children)
        self._compress(dst_m, dst, ctx, plan, meta)
        os.unlink(src_m)
        os.unlink(dst_m)
        return RewriteResult(status=REWRITTEN, counts=m.counts, children=m.children)

    def verify(self, ctx, src, dst, plan):
        try:
            p = os.path.join(ctx.tmp_dir, "verify.bin")
            self._decompress(dst, p, _Budget(os.path.getsize(dst)))
            os.unlink(p)
            return VerifyResult(True)
        except Exception as e:
            return VerifyResult(False, [type(e).__name__])


class GzipAdapter(_StreamAdapter):
    name = "gzip"
    version = "1"
    priority = 0
    magics = ("gzip",)
    extensions = (".gz", ".tgz")

    def matches(self, ctx, head):
        return ctx.magic == "gzip"

    def _meta(self, src):
        mtime, fname, comment, xfl, osb = _gzip_header(src)
        return {"mtime": mtime, "fname": fname, "comment": comment}

    def _decompress(self, src, out, budget):
        with gzip.open(src, "rb") as fi:
            _copy_stream(fi, out, budget)

    def _compress(self, payload, dst, ctx, plan, meta):
        fname = ""
        if meta.get("fname"):
            inner = _inner_name(ctx, meta["fname"])
            fname = _member_out_name(plan, ctx, inner).rsplit("/", 1)[-1]
        # gzip FCOMMENT is not written (Python's writer has no comment field): comment dropped.
        with open(payload, "rb") as fi, open(dst, "wb") as raw:
            with gzip.GzipFile(filename=fname, mode="wb", fileobj=raw, compresslevel=6,
                               mtime=meta.get("mtime", 0)) as fo:
                shutil.copyfileobj(fi, fo, CHUNK)


# ---- UNIX compress (LZW) ----------------------------------------------------
def lzw_decompress(data: bytes) -> bytes:
    if len(data) < 3 or data[:2] != b"\x1f\x9d":
        raise SourceDefect("not compress")
    maxbits = data[2] & 0x1F
    block = bool(data[2] & 0x80)
    if maxbits < 9 or maxbits > 16:
        raise SourceDefect("bad maxbits")
    maxmaxcode = 1 << maxbits
    prefix = [0] * maxmaxcode
    suffix = bytearray(maxmaxcode)
    for i in range(256):
        suffix[i] = i
    n_bits = 9
    maxcode = (1 << n_bits) - 1
    free_ent = 257 if block else 256
    out = bytearray()
    bits = data[3:] + b"\x00\x00\x00"
    nbits_total = (len(data) - 3) * 8
    posbits = 0
    base = 0            # bit offset where the current code width started
    oldcode = -1
    finchar = 0
    stack = bytearray()
    bitmask = (1 << n_bits) - 1

    def align(pos, nb):
        rel = pos - base
        g = nb << 3
        rel = (rel - 1) + (g - (rel - 1 + g) % g)
        return base + rel
    while True:
        if free_ent > maxcode:
            posbits = align(posbits, n_bits)
            base = posbits
            n_bits += 1
            maxcode = maxmaxcode if n_bits == maxbits else (1 << n_bits) - 1
            bitmask = (1 << n_bits) - 1
        if posbits + n_bits > nbits_total:
            break
        p = posbits >> 3
        w = bits[p] | (bits[p + 1] << 8) | (bits[p + 2] << 16)
        code = (w >> (posbits & 7)) & bitmask
        posbits += n_bits
        if oldcode == -1:
            if code >= 256:
                raise SourceDefect("corrupt compress data")
            oldcode = finchar = code
            out.append(code)
            continue
        if code == 256 and block:
            free_ent = 256
            posbits = align(posbits, n_bits)
            base = posbits
            n_bits = 9
            maxcode = (1 << n_bits) - 1
            bitmask = (1 << n_bits) - 1
            continue
        incode = code
        stack.clear()
        if code >= free_ent:
            if code > free_ent:
                raise SourceDefect("corrupt compress data")
            stack.append(finchar)
            code = oldcode
        while code >= 256:
            stack.append(suffix[code])
            code = prefix[code]
        finchar = suffix[code]
        stack.append(finchar)
        stack.reverse()
        out += stack
        if free_ent < maxmaxcode:
            prefix[free_ent] = oldcode
            suffix[free_ent] = finchar
            free_ent += 1
        oldcode = incode
    return bytes(out)


def lzw_compress(data: bytes, maxbits: int = 16) -> bytes:
    """UNIX compress compatible output (block mode, no CLEAR codes)."""
    out = bytearray(b"\x1f\x9d" + bytes([0x80 | maxbits]))
    if not data:
        return bytes(out)
    maxmaxcode = 1 << maxbits
    table = {}
    free_ent = 257
    n_bits = 9
    maxcode = (1 << n_bits) - 1
    acc = 0          # bit accumulator for the current group
    accbits = 0
    group = bytearray()

    def emit(code):
        nonlocal acc, accbits, n_bits, maxcode
        acc |= code << accbits
        accbits += n_bits
        while accbits >= 8:
            group.append(acc & 0xFF)
            acc >>= 8
            accbits -= 8
        if len(group) == n_bits and accbits == 0:
            out.extend(group)
            group.clear()
        if free_ent > maxcode:
            # flush the partial group padded to n_bits bytes, then widen
            if group or accbits:
                if accbits:
                    group.append(acc & 0xFF)
                    acc = 0
                    accbits = 0
                group.extend(b"\x00" * (n_bits - len(group)))
                out.extend(group)
                group.clear()
            n_bits += 1
            maxcode = maxmaxcode if n_bits == maxbits else (1 << n_bits) - 1

    ent = data[0]
    for c in data[1:]:
        k = (ent << 8) | c
        v = table.get(k)
        if v is not None:
            ent = v
            continue
        emit(ent)
        if free_ent < maxmaxcode:
            table[k] = free_ent
            free_ent += 1
        ent = c
    emit(ent)
    if accbits:
        group.append(acc & 0xFF)
    out.extend(group)
    return bytes(out)


class CompressAdapter(_StreamAdapter):
    name = "compress"
    version = "1"
    priority = 0
    magics = ("compress",)
    extensions = (".z",)

    def matches(self, ctx, head):
        return ctx.magic == "compress"

    def _decompress(self, src, out, budget):
        with open(src, "rb") as f:
            data = f.read()
        res = lzw_decompress(data)
        budget.add(len(res))
        with open(out, "wb") as f:
            f.write(res)

    def _compress(self, payload, dst, ctx, plan, meta):
        with open(payload, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(lzw_compress(data))

    def verify(self, ctx, src, dst, plan):
        try:
            with open(dst, "rb") as f:
                lzw_decompress(f.read())
            return VerifyResult(True)
        except Exception as e:
            return VerifyResult(False, [type(e).__name__])


# ===========================================================================
# 7z and RAR via libarchive (read), 7z via libarchive (write), RAR4 stored writer
# ===========================================================================
def _la_entries(src, tmp_dir, budget, on_file):
    """Iterate libarchive entries; regular files are extracted to numbered files.
    on_file(entry_meta, path_or_None). Raises SourceDefect on read errors."""
    import libarchive
    try:
        with libarchive.file_reader(src) as arc:
            i = 0
            for e in arc:
                meta = {"name": e.pathname, "isdir": e.isdir, "isreg": e.isreg, "issym": e.issym,
                        "linkpath": e.linkpath, "mtime": e.mtime, "perm": e.perm, "size": e.size}
                if e.isreg:
                    i += 1
                    p = os.path.join(tmp_dir, f"la{i}")
                    n = 0
                    with open(p, "wb") as fo:
                        for blk in e.get_blocks():
                            n += len(blk)
                            if n > MAX_MEMBER:
                                raise BombError("member too large")
                            budget.add(len(blk))
                            fo.write(blk)
                    on_file(meta, p)
                else:
                    on_file(meta, None)
    except BombError:
        raise
    except SourceDefect:
        raise
    except Exception as e:  # libarchive.ArchiveError and friends
        raise SourceDefect(f"unreadable archive ({type(e).__name__}: {str(e)[:60]})")


class _LibarchiveAdapter(Adapter):
    timeout_s = 3600.0
    write_format = None

    def scan(self, ctx, src):
        m = _Members(ctx)
        files = []

        def on_file(meta, p):
            if p:
                files.append(1)
                m.scan(meta["name"], p)
                os.unlink(p)
        try:
            _la_entries(src, ctx.tmp_dir, _Budget(ctx.size), on_file)
        except (SourceDefect, BombError) as e:
            return ScanResult(status="source_exception", children=m.children, notes=[str(e)])
        if not files and ctx.size > 64:
            # libarchive returned nothing (e.g. encrypted header): verify it really is empty
            pass
        return ScanResult(children=m.children, fingerprint={"files": len(files)})

    def rewrite(self, ctx, src, dst, plan):
        m = _Members(ctx, plan)
        entries = []

        state = {"changed": False}

        def on_file(meta, p):
            new = _member_out_name(plan, ctx, meta["name"] + ("/" if meta["isdir"] and not meta["name"].endswith("/") else ""))
            if new.rstrip("/") != meta["name"].rstrip("/"):
                m.counts["members_renamed"] += 1
                state["changed"] = True
            if p:
                outp = p + ".out"
                r = m.rewrite(meta["name"], p, outp)
                state["changed"] |= r is not None and r.status == REWRITTEN
                os.unlink(p)
                entries.append((meta, new, outp))
            else:
                entries.append((meta, new, None))
        try:
            _la_entries(src, ctx.tmp_dir, _Budget(ctx.size), on_file)
        except SourceDefect as e:
            for _, _, p in entries:
                if p and os.path.exists(p):
                    os.unlink(p)
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[str(e)])
        if not entries:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=["archive lists no entries (encrypted header?)"])
        if not state["changed"]:
            for _, _, p in entries:
                if p and os.path.exists(p):
                    os.unlink(p)
            return RewriteResult(status=VERIFIED_UNCHANGED, counts=m.counts, children=m.children)
        self._write(dst, entries, ctx, plan)
        for _, _, p in entries:
            if p and os.path.exists(p):
                os.unlink(p)
        return RewriteResult(status=REWRITTEN, counts=m.counts, children=m.children)

    def verify(self, ctx, src, dst, plan):
        n_src, n_dst = [], []
        try:
            _la_entries(src, ctx.tmp_dir, _Budget(ctx.size), lambda meta, p: (n_src.append(meta["isreg"]), p and os.unlink(p)))
            _la_entries(dst, ctx.tmp_dir, _Budget(ctx.size * 10 + (1 << 30)), lambda meta, p: (n_dst.append(meta["isreg"]), p and os.unlink(p)))
        except Exception as e:
            return VerifyResult(False, [type(e).__name__])
        if sum(n_src) != sum(n_dst):
            return VerifyResult(False, [f"file count {sum(n_src)} -> {sum(n_dst)}"])
        return VerifyResult(True, fingerprint={"files": sum(n_dst)})


class SevenZipAdapter(_LibarchiveAdapter):
    name = "7z"
    version = "1"
    priority = 0
    magics = ("7z",)
    extensions = (".7z",)

    def matches(self, ctx, head):
        return ctx.magic == "7z"

    def _write(self, dst, entries, ctx, plan):
        import libarchive
        from libarchive.entry import FileType
        with libarchive.file_writer(dst, "7zip") as arc:
            for meta, new, p in entries:
                mt = int(meta["mtime"]) if meta.get("mtime") else None
                kw = {"mtime": mt} if mt else {}
                if meta["isdir"]:
                    arc.add_file_from_memory(new.rstrip("/"), 0, b"", filetype=FileType.DIRECTORY,
                                             permission=meta.get("perm") or 0o755, **kw)
                elif p:
                    size = os.path.getsize(p)

                    def chunks(path=p):
                        with open(path, "rb") as f:
                            while True:
                                b = f.read(CHUNK)
                                if not b:
                                    return
                                yield b
                    arc.add_file_from_memory(new, size, chunks(), permission=meta.get("perm") or 0o644, **kw)


def _crc32(b, crc=0):
    return zlib.crc32(b, crc) & 0xFFFFFFFF


def _dos_time(t):
    lt = time.gmtime(max(int(t or 0), 315532800))
    return ((lt.tm_year - 1980) << 25) | (lt.tm_mon << 21) | (lt.tm_mday << 16) | \
        (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)


def write_rar4_stored(dst, entries):
    """Write a RAR 4.x archive with stored (method 0x30) entries.
    entries: list of (name, path_or_None_for_dir, mtime)."""
    with open(dst, "wb") as f:
        f.write(b"Rar!\x1a\x07\x00")
        # main archive header
        body = struct.pack("<BHH", 0x73, 0x0000, 13) + b"\x00" * 6
        f.write(struct.pack("<H", _crc32(body) & 0xFFFF) + body)
        for name, path, mtime in entries:
            nm = name.replace("/", "\\").rstrip("\\")
            try:
                nb = nm.encode("ascii")
                flags = 0x8000
            except UnicodeEncodeError:
                nb = nm.encode("utf-8")
                # RAR4 unicode name: ascii part, NUL, then encoded unicode; use the simplest
                # valid form (empty ascii part is not allowed) -> store utf-8 in both via LHD_UNICODE
                # with "high byte" encoding is complex; store UTF-8 bytes without the unicode flag.
                flags = 0x8000
            size = os.path.getsize(path) if path else 0
            if path is None:
                flags |= 0x00E0  # directory
                attr = 0x10
            else:
                attr = 0x20
            large = size > 0xFFFFFFFF
            if large:
                flags |= 0x0100
            crc = 0
            if path:
                with open(path, "rb") as fi:
                    while True:
                        b = fi.read(CHUNK)
                        if not b:
                            break
                        crc = _crc32(b, crc)
            hsize = 7 + 25 + len(nb) + (8 if large else 0)
            body = struct.pack("<BHH", 0x74, flags, hsize)
            body += struct.pack("<IIBIIBBHI", size & 0xFFFFFFFF, size & 0xFFFFFFFF, 2, crc,
                                _dos_time(mtime), 20, 0x30, len(nb), attr)
            if large:
                body += struct.pack("<II", size >> 32, size >> 32)
            body += nb
            f.write(struct.pack("<H", _crc32(body) & 0xFFFF) + body)
            if path:
                with open(path, "rb") as fi:
                    shutil.copyfileobj(fi, f, CHUNK)
        # end of archive
        body = struct.pack("<BHH", 0x7B, 0x4000, 7)
        f.write(struct.pack("<H", _crc32(body) & 0xFFFF) + body)


class RarAdapter(_LibarchiveAdapter):
    name = "rar"
    version = "1"
    priority = 0
    magics = ("rar",)
    extensions = (".rar",)

    def matches(self, ctx, head):
        return ctx.magic == "rar"

    def _write(self, dst, entries, ctx, plan):
        write_rar4_stored(dst, [(new, p, meta.get("mtime")) for meta, new, p in entries
                                if p or meta["isdir"]])


register(ZipAdapter())
register(TarAdapter())
register(GzipAdapter())
register(CompressAdapter())
register(SevenZipAdapter())
register(RarAdapter())
