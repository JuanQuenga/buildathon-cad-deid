"""OpenSCAD source (.scad).  Never evaluated.

Edited: comments (// and /* */), string literals, and include/use/import file
references.  Identifiers, numbers and code structure are untouched.
"""
from __future__ import annotations

import hashlib
import re

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED)
from deid.formats._neutral import PlanCache, ObsCollector, ref_kind

_TOK = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*"|\b(?:include|use)\s*<([^>\n]*)>', re.S)


def _walk(text: str, fn):
    out = []
    pos = 0
    for m in _TOK.finditer(text):
        tok = m.group(0)
        if m.group(1) is not None:
            s, e = m.span(1)
            kind, val = "ref", m.group(1)
        elif tok.startswith("//"):
            s, e = m.start() + 2, m.end()
            kind, val = "comment", tok[2:]
        elif tok.startswith("/*"):
            s, e = m.start() + 2, m.end() - 2
            kind, val = "comment", tok[2:-2]
        else:
            s, e = m.start() + 1, m.end() - 1
            kind, val = "string", tok[1:-1]
            prev = text[max(0, m.start() - 40):m.start()]
            if re.search(r"\b(?:import|surface)\s*\(\s*(?:file\s*=\s*)?$", prev):
                kind = "ref"
        new = fn(kind, val)
        if new is not None and new != val:
            if kind == "comment" and tok.startswith("/*"):
                new = new.replace("*/", "* /")
            if kind == "comment" and tok.startswith("//"):
                new = new.replace("\n", " ")
            if kind in ("string", "ref") and tok.startswith('"'):
                new = new.replace("\\", "\\\\").replace('"', '\\"') if "\\" not in val else new.replace('"', '\\"')
            out.append(text[pos:s])
            out.append(new)
            pos = e
    out.append(text[pos:])
    return "".join(out)


def _decode(data: bytes):
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


class ScadAdapter(Adapter):
    name = "scad"
    version = "1"
    priority = 8
    extensions = (".scad",)
    path_independent = False

    def matches(self, ctx, head):
        return ctx.ext == ".scad" and ctx.magic in ("text", "unknown")

    def scan(self, ctx, src_path):
        text, _ = _decode(open(src_path, "rb").read())
        col = ObsCollector(cap=1000)

        def fn(kind, val):
            v = val.strip()
            if kind == "ref":
                col.ref(v, ref_kind(v), "scad.include")
            elif sum(c.isalpha() for c in v) >= 2:
                col.add("free_text", f"scad.{kind}", v, kind, 0.5)
            return None

        _walk(text, fn)
        return ScanResult(observations=col.obs, references=col.refs)

    def rewrite(self, ctx, src_path, dst_path, plan):
        data = open(src_path, "rb").read()
        text, enc = _decode(data)
        pc = PlanCache(plan, ctx)

        def fn(kind, val):
            if not val.strip():
                return None
            return pc.ref(val) if kind == "ref" else pc.text(val)

        new = _walk(text, fn)
        if new == text:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        with open(dst_path, "wb") as f:
            f.write(new.encode(enc, "replace"))
        return RewriteResult(status=REWRITTEN)

    def verify(self, ctx, src_path, dst_path, plan):
        def fp(p):
            t, _ = _decode(open(p, "rb").read())
            return hashlib.sha256(_walk(t, lambda k, v: "\0").encode("utf-8", "surrogatepass")).hexdigest()
        ok = fp(src_path) == fp(dst_path)
        return VerifyResult(ok=ok, notes=[] if ok else ["code outside comments/strings differs"])


register(ScadAdapter())
