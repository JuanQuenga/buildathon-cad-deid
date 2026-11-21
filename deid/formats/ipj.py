"""Autodesk Inventor project files (.ipj): UTF-16 (or UTF-8) XML.

Text nodes and attribute values go through the plan; <Path>/<Location>-like
values (workspaces, libraries, content-center folders) through
``plan.map_reference`` so they keep pointing at the renamed folders.
The encoding, BOM, line endings and element order are preserved.
"""
from __future__ import annotations

import re

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult, Reference,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION)
from deid.formats import _nativeutil as nu

_PATHY = re.compile(r"^(?:[A-Za-z]:\\|\\\\|\.{1,2}[\\/]|[^<>:\"|?*]*[\\/])")


def _decode(b: bytes):
    for bom, enc in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"\xef\xbb\xbf", "utf-8")):
        if b.startswith(bom):
            return b[len(bom):].decode(enc), enc, bom
    if b[:4] == b"<\x00?\x00":
        return b.decode("utf-16-le"), "utf-16-le", b""
    return b.decode("utf-8"), "utf-8", b""


def _xml_ok(text: str) -> bool:
    try:
        from lxml import etree
        etree.fromstring(re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip("﻿")).encode("utf-8"))
        return True
    except Exception:
        return False


class IpjAdapter(Adapter):
    name = "ipj"
    version = "1"
    priority = 20
    extensions = (".ipj",)
    path_independent = False

    def matches(self, ctx, head):
        return ctx.ext == ".ipj"

    def scan(self, ctx, src_path):
        try:
            t, enc, bom = _decode(open(src_path, "rb").read())
        except UnicodeDecodeError:
            return ScanResult(status="unsupported", notes=["not decodable"])
        obs, refs = [], []
        for m in re.finditer(r"<(\w+)>([^<]+)</\1>", t):
            tag, v = m.group(1), nu.xml_unescape(m.group(2)).strip()
            if not v:
                continue
            if tag in ("Owner",):
                nu.add_obs(obs, "person", f"ipj.{tag}", v, "project owner", 0.8)
            elif tag in ("Path", "Location", "IncludedProject", "ImportedCompFolder", "ImportedAsmFolder") or _PATHY.match(v):
                refs.append(Reference(v, "path", f"ipj.{tag}"))
                nu.contact_observations(v, f"ipj.{tag}", obs)
            elif tag in ("Name", "ProjectName", "ReleaseID", "Description"):
                nu.add_obs(obs, "free_text", f"ipj.{tag}", v, "project field", 0.4)
        return ScanResult(observations=nu.dedupe_obs(obs), references=nu.dedupe_refs(refs),
                          fingerprint={"encoding": enc})

    def rewrite(self, ctx, src_path, dst_path, plan):
        raw = open(src_path, "rb").read()
        try:
            t, enc, bom = _decode(raw)
        except UnicodeDecodeError:
            return RewriteResult(status=SOURCE_EXCEPTION, notes=["not decodable"])

        def vf(v, where):
            if _PATHY.match(v) or nu.looks_like_ref(v):
                try:
                    nv = plan.map_reference(ctx, v)
                except Exception:
                    nv = v
                if nv != v:
                    return nv
            return nu.sub_value(plan, v)

        nt, n = nu.rewrite_xml_values(t, vf)
        if not n or nt == t:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        if _xml_ok(t) and not _xml_ok(nt):
            return RewriteResult(status=VERIFIED_UNCHANGED, notes=["edit would break XML"])
        with open(dst_path, "wb") as f:
            f.write(bom + nt.encode(enc))
        return RewriteResult(status=REWRITTEN, counts={"values": n})

    def verify(self, ctx, src_path, dst_path, plan):
        try:
            t, enc, bom = _decode(open(dst_path, "rb").read())
            t0, enc0, bom0 = _decode(open(src_path, "rb").read())
        except Exception:
            return VerifyResult(ok=False, notes=["not decodable"])
        if (enc, bom) != (enc0, bom0):
            return VerifyResult(ok=False, notes=["encoding changed"])
        if _xml_ok(t0) and not _xml_ok(t):
            return VerifyResult(ok=False, notes=["xml no longer parses"])
        return VerifyResult(ok=True)


register(IpjAdapter())
