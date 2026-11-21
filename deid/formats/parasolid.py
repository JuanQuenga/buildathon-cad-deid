"""Parasolid text transmit files (.x_t .xmt_txt).  Binary .x_b/.xmt_bin are not claimed.

Only the textual header (``**PART1;`` .. ``**END_OF_HEADER``) is edited: its
``KEY=value;`` fields such as USER, SITE, MC_ID, KEY, FILE and application
fields.  The header is either one field per line or a stream wrapped at 80
columns; the layout is preserved (re-wrapped at 80 when wrapped).  The model
body (schema and entity data) is copied byte for byte.
"""
from __future__ import annotations

import hashlib
import os
import re

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION)
from deid.formats._neutral import PlanCache, ObsCollector, ref_kind

_END = b"**END_OF_HEADER"
_FIELD = re.compile(rb"([A-Z][A-Z0-9_]*)=([^;]*);")
KINDS = {b"USER": "account", b"SITE": "company", b"MC_ID": "account", b"KEY": "part_name",
         b"FILE": "path", b"APPL": "software", b"FRU": "software", b"MC": None, b"MC_MODEL": None,
         b"OS": None, b"OS_RELEASE": None, b"FORMAT": None, b"GUISE": None, b"DATE": None, b"SCH": None,
         b"USFLD_SIZE": None}
SKIP_VALUES = {b"", b"unknown", b"UNKNOWN", b"none"}


def _split(data: bytes):
    """Return (prefix, parts_text_lines, end_line_and_rest) or None."""
    i = data.find(b"**PART1;")
    j = data.find(_END)
    if i < 0 or j < 0 or j < i:
        return None
    # header region from PART1 to the line before END_OF_HEADER
    k = data.rfind(b"\n", 0, j) + 1
    return data[:i], data[i:k], data[k:]


def _eol(region: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in region else b"\n"


def _wrapped(region: bytes) -> bool:
    lines = region.splitlines()
    return len(lines) > 1 and all(len(l) == 80 for l in lines[:-1])


def _edit_block(block: bytes, fn, eol: bytes) -> bytes:
    if _wrapped(block):
        text = b"".join(block.splitlines())
        new = _FIELD.sub(lambda m: m.group(1) + b"=" + fn(m.group(1), m.group(2)) + b";", text)
        if new == text:
            return block
        return eol.join(new[k:k + 80] for k in range(0, len(new), 80)) + eol
    return _FIELD.sub(lambda m: m.group(1) + b"=" + fn(m.group(1), m.group(2)) + b";", block)


def _edit(region: bytes, fn) -> bytes:
    """Edit each **PARTn block separately (each may be wrapped at 80 columns)."""
    eol = _eol(region)
    starts = [m.start() for m in re.finditer(rb"(?m)^\*\*PART\d;", region)] or [0]
    if starts[0] != 0:
        starts.insert(0, 0)
    out = []
    for a, b in zip(starts, starts[1:] + [len(region)]):
        out.append(_edit_block(region[a:b], fn, eol))
    return b"".join(out)


class ParasolidTextAdapter(Adapter):
    name = "parasolid_text"
    version = "1"
    priority = 10
    magics = ("parasolid_text",)
    extensions = (".x_t", ".xmt_txt")
    path_independent = False
    timeout_s = 600.0

    def matches(self, ctx, head):
        if ctx.magic == "parasolid_text":
            return True
        return head.startswith(b"**ABCDEFGHIJKLMNOPQRSTUVWXYZ") or (
            ctx.ext in self.extensions and b"**PART1;" in head)

    def scan(self, ctx, src_path):
        data = open(src_path, "rb").read()
        sp = _split(data)
        if sp is None:
            return ScanResult(status="source_exception" if not data.strip() else "unsupported",
                              notes=["no Parasolid text header"])
        col = ObsCollector(cap=200)

        def fn(k, v):
            kind = KINDS.get(k, "free_text")
            if kind and v.strip() not in SKIP_VALUES:
                val = v.decode("latin-1").strip()
                if kind == "path":
                    col.ref(val, ref_kind(val), "parasolid.FILE")
                col.add(kind, f"parasolid.{k.decode()}", val, "header", 0.8 if kind != "free_text" else 0.4)
            return v

        _edit(sp[1], fn)
        return ScanResult(observations=col.obs, references=col.refs,
                          fingerprint={"body_sha256": hashlib.sha256(sp[2]).hexdigest()})

    def rewrite(self, ctx, src_path, dst_path, plan):
        data = open(src_path, "rb").read()
        sp = _split(data)
        if sp is None:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=["no Parasolid text header"])
        pc = PlanCache(plan, ctx)

        def fn(k, v):
            kind = KINDS.get(k, "free_text")
            if kind is None or v.strip() in SKIP_VALUES:
                return v
            val = v.decode("latin-1")
            if kind == "path":
                nv = pc.ref(val)
            elif kind == "software":
                nv = pc.text(val)
            elif kind == "free_text":
                nv = pc.text(val)
            else:
                nv = pc.sub(val, kind)
            nb = nv.encode("latin-1", "replace").replace(b";", b",").replace(b"\n", b" ").replace(b"\r", b" ")
            return nb

        region = _edit(sp[1], fn)
        if region == sp[1]:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        with open(dst_path, "wb") as f:
            f.write(sp[0] + region + sp[2])
        return RewriteResult(status=REWRITTEN)

    def verify(self, ctx, src_path, dst_path, plan):
        a = open(src_path, "rb").read()
        b = open(dst_path, "rb").read()
        sa, sb = _split(a), _split(b)
        if sb is None:
            return VerifyResult(ok=False, notes=["header not found in output"])
        notes = []
        if sa[0] != sb[0] or sa[2] != sb[2]:
            notes.append("body or preamble differs")
        ka = [m.group(1) for m in _FIELD.finditer(b"".join(sa[1].splitlines()))]
        kb = [m.group(1) for m in _FIELD.finditer(b"".join(sb[1].splitlines()))]
        if ka != kb:
            notes.append("header keys differ")
        if _wrapped(sa[1]) and not all(len(l) <= 80 for l in sb[1].splitlines()):
            notes.append("header line too long")
        return VerifyResult(ok=not notes, notes=notes)


register(ParasolidTextAdapter())
