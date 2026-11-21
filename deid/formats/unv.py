"""I-DEAS universal files (.unv .uff).

Datasets are delimited by ``    -1`` lines.  Numeric records (every token a
number) are never touched; only text records (model/file names, program
names, group names, ID lines) pass through the plan.  Binary datasets (``58b``)
and everything after them are copied verbatim.  Line count and order are
preserved; text records keep the 80-column limit.
"""
from __future__ import annotations

import hashlib
import os
import re

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION)
from deid.formats._neutral import PlanCache, ObsCollector, looks_like_reference, ref_kind

_NUM = re.compile(rb"^[ \t]*(?:[-+]?(?:\d+\.?\d*|\.\d+)(?:[EeDd][-+]?\d+)?[ \t]*)*$")
_DELIM = re.compile(rb"^ {4}-1[ \t]*$")
_DSID = re.compile(rb"^[ \t]*(\d+)([a-zA-Z]?)")
FIELD_151 = {0: ("model_file", "path"), 1: ("model_description", "free_text"), 2: ("program", "software"),
             5: ("program_saved", "software")}


def _iter(data: bytes):
    """Yield (kind, line_start, line_end, dataset_id, index_in_dataset) for text lines;
    kind 'text'.  Stops at binary datasets."""
    lines = data.split(b"\n")
    pos = 0
    in_ds = False
    ds = None
    idx = -1
    for ln in lines:
        body = ln[:-1] if ln.endswith(b"\r") else ln
        end = pos + len(body)
        if _DELIM.match(body):
            in_ds = not in_ds
            ds = None
            idx = -1
            pos += len(ln) + 1
            continue
        if in_ds and ds is None:
            m = _DSID.match(body)
            if m:
                ds = m.group(1).decode()
                if m.group(2):
                    yield "binary", pos, len(data), ds, -1
                    return
            pos += len(ln) + 1
            idx = -1
            continue
        idx += 1
        if body.strip() and not _NUM.match(body):
            yield "text", pos, end, ds, idx
        pos += len(ln) + 1


def _dec(b):
    try:
        return b.decode("ascii"), "ascii"
    except UnicodeDecodeError:
        return b.decode("latin-1"), "latin-1"


class UnvAdapter(Adapter):
    name = "unv"
    version = "1"
    priority = 8
    magics = ()
    extensions = (".unv", ".uff")
    path_independent = True
    timeout_s = 900.0

    def matches(self, ctx, head):
        if ctx.magic not in ("text", "unknown"):
            return False
        return bool(re.match(rb"\s*-1\s*\r?\n\s*\d+\s*\r?\n", head)) and (
            ctx.ext in self.extensions or b"    -1" in head[:20])

    def scan(self, ctx, src_path):
        data = open(src_path, "rb").read()
        if not data.strip():
            return ScanResult(status="source_exception", notes=["empty file"])
        col = ObsCollector(cap=2000)
        notes = []
        for kind, s, e, ds, idx in _iter(data):
            if kind == "binary":
                notes.append("binary dataset present; rest copied verbatim")
                break
            v, _ = _dec(data[s:e])
            v = v.strip()
            if sum(c.isalpha() for c in v) < 2:
                continue
            fname, k = FIELD_151.get(idx, (f"line{idx}", "free_text")) if ds == "151" else (f"ds{ds}", "free_text")
            if k == "path" or looks_like_reference(v):
                col.ref(v, ref_kind(v), f"unv.{ds}.{fname}")
            col.add("path" if k == "path" else k, f"unv.{ds}.{fname}", v, f"dataset {ds}", 0.5)
        return ScanResult(observations=col.obs, references=col.refs, notes=notes)

    def rewrite(self, ctx, src_path, dst_path, plan):
        data = open(src_path, "rb").read()
        if not data.strip():
            return RewriteResult(status=SOURCE_EXCEPTION, notes=["empty file"])
        pc = PlanCache(plan, ctx)
        out = []
        pos = 0
        n = 0
        for kind, s, e, ds, idx in _iter(data):
            if kind == "binary":
                break
            raw = data[s:e]
            v, enc = _dec(raw)
            if sum(c.isalpha() for c in v) < 2:
                continue
            k = FIELD_151.get(idx, (None, "free_text"))[1] if ds == "151" else "free_text"
            if k == "path" or looks_like_reference(v.strip()):
                lead = v[:len(v) - len(v.lstrip())]
                trail = v[len(v.rstrip()):]
                nv = lead + pc.ref(v.strip()) + trail
            elif k == "software":
                continue
            else:
                nv = pc.text(v)
            if nv == v:
                continue
            nb = nv.encode(enc, "replace")
            if len(raw) <= 80 < len(nb):
                nb = nb[:80].rstrip() if len(nb.rstrip()) > 80 else nb.rstrip()
            out.append(data[pos:s])
            out.append(nb)
            pos = e
            n += 1
        if not n:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        out.append(data[pos:])
        with open(dst_path, "wb") as f:
            f.write(b"".join(out))
        return RewriteResult(status=REWRITTEN, counts={"lines_replaced": n})

    def verify(self, ctx, src_path, dst_path, plan):
        a = unv_fingerprint(open(src_path, "rb").read())
        b = unv_fingerprint(open(dst_path, "rb").read())
        notes = [f"{k} differs" for k in a if a[k] != b[k]]
        return VerifyResult(ok=not notes, notes=notes)


def unv_fingerprint(data: bytes) -> dict:
    h = hashlib.sha256()
    pos = 0
    nt = 0
    for kind, s, e, ds, idx in _iter(data):
        if kind == "binary":
            break
        h.update(data[pos:s])
        h.update(b"\0")
        pos = e
        nt += 1
    h.update(data[pos:])
    return {"masked_sha256": h.hexdigest(), "text_lines": nt, "lines": data.count(b"\n")}


register(UnvAdapter())
