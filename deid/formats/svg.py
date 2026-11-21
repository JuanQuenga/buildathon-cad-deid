"""SVG (and SVGZ is left to the gzip/archive adapter).

Lexical XML rewrite: the document is tokenised (tags, attributes, text,
comments, CDATA, PIs, DOCTYPE) and only text content, comments and selected
attribute values change.  Markup, geometry attributes (d, points, transform,
coordinates, style) and element ids are copied verbatim, so the output is the
input with string spans substituted.  Embedded data: URIs are left intact and
counted (raster payloads are the image adapter's concern).
"""
from __future__ import annotations

import hashlib
import os
import re

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION)
from deid.formats._neutral import PlanCache, ObsCollector, looks_like_reference, ref_kind

_TOK = re.compile(
    r"<!--(.*?)-->|<!\[CDATA\[(.*?)\]\]>|<\?.*?\?>|<!DOCTYPE(?:[^\[>]|\[.*?\])*>|"
    r"<(/?)([A-Za-z_][\w:.\-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>|([^<]+)|<", re.S)
_ATTR = re.compile(r"([A-Za-z_][\w:.\-]*)(\s*=\s*)(\"[^\"]*\"|'[^']*')")
_ENT = re.compile(r"&(#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);")

# attributes never touched (geometry, presentation, structure)
_SKIP_ATTRS = {
    "d", "points", "transform", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry", "dx", "dy",
    "width", "height", "viewBox", "style", "class", "id", "fill", "stroke", "stroke-width", "opacity",
    "font-size", "font-family", "font-weight", "text-anchor", "version", "baseProfile", "preserveAspectRatio",
    "xml:space", "offset", "stop-color", "gradientTransform", "gradientUnits", "patternUnits", "patternTransform",
    "clip-path", "mask", "filter", "marker-start", "marker-mid", "marker-end", "rotate", "textLength",
    "lengthAdjust", "pathLength", "stdDeviation", "in", "in2", "result", "mode", "operator", "k1", "k2", "k3",
    "k4", "values", "keyTimes", "begin", "dur", "fill-opacity", "stroke-opacity", "stroke-dasharray",
    "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "display", "visibility", "overflow",
    "enable-background", "inkscape:version", "sodipodi:type", "inkscape:groupmode", "inkscape:connector-curvature",
    "sodipodi:nodetypes", "inkscape:transform-center-x", "inkscape:transform-center-y", "inkscape:zoom",
    "inkscape:cx", "inkscape:cy", "inkscape:window-width", "inkscape:window-height", "inkscape:window-x",
    "inkscape:window-y", "inkscape:window-maximized", "inkscape:current-layer", "inkscape:pageopacity",
    "inkscape:pageshadow", "inkscape:document-units", "units", "unit", "sodipodi:cx", "sodipodi:cy",
    "sodipodi:rx", "sodipodi:ry", "sodipodi:start", "sodipodi:end", "sodipodi:open", "sodipodi:arg1",
    "sodipodi:arg2", "sodipodi:sides", "sodipodi:r1", "sodipodi:r2", "inkscape:flatsided", "inkscape:rounded",
    "inkscape:randomized", "inkscape:original-d", "inkscape:path-effect", "xmlns", "encoding", "standalone",
}
_REF_ATTRS = {"href", "xlink:href", "sodipodi:absref", "inkscape:export-filename", "sodipodi:docbase",
              "src", "rdf:resource", "rdf:about", "xml:base"}
_PERSON_CTX = ("creator", "contributor", "author")
_COMPANY_CTX = ("publisher", "rights", "owner")


def _unent(s: str) -> str:
    if "&" not in s:
        return s

    def r(m):
        g = m.group(1)
        if g[0] == "#":
            try:
                return chr(int(g[2:], 16) if g[1] in "xX" else int(g[1:]))
            except (ValueError, OverflowError):
                return m.group(0)
        # '&amp;' decodes to a private marker so _ent can tell a literal '&' (re-escaped) from an
        # undeclared/DTD entity reference that was left as is ('&amp;copy;' must not become '&copy;')
        return {"amp": _AMP, "lt": "<", "gt": ">", "quot": '"', "apos": "'"}[g]
    return _ENT.sub(r, s)


_AMP = "\ue000"


def _ent(s: str, quote: str = "") -> str:
    s = re.sub(r"&(?![A-Za-z_][\w.\-]*;|#[0-9]+;|#x[0-9a-fA-F]+;)", "&amp;", s).replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace(_AMP, "&amp;")
    if quote == '"':
        s = s.replace('"', "&quot;")
    elif quote == "'":
        s = s.replace("'", "&apos;")
    return s


def _detect_encoding(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    m = re.match(rb"\s*<\?xml[^>]*encoding\s*=\s*[\"']([A-Za-z0-9._\-]+)[\"']", data)
    if m:
        e = m.group(1).decode("ascii").lower()
        try:
            "".encode(e)
            return e
        except LookupError:
            return "utf-8"
    return "utf-8"


def _decode(data: bytes):
    e = _detect_encoding(data)
    try:
        return data.decode(e), e, False
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1", True


def _iter(text: str):
    """Yield (kind, match) for each token. kind in cmt, cdata, pi, doctype, tag, text, junk."""
    for m in _TOK.finditer(text):
        if m.group(1) is not None:
            yield "cmt", m
        elif m.group(2) is not None:
            yield "cdata", m
        elif m.group(4) is not None:
            yield "tag", m
        elif m.group(6) is not None:
            yield "text", m
        else:
            s = m.group(0)
            yield ("pi" if s.startswith("<?") else "doctype" if s.startswith("<!DOCTYPE") else "junk"), m


class _Walker:
    """Walk tokens; call ``fn(kind, value, ctx)`` for each editable span and splice results."""

    def __init__(self, text: str):
        self.text = text

    def run(self, fn, collect_only=False):
        out = []
        pos = 0
        stack: list = []
        t = self.text
        n_data = 0
        for kind, m in _iter(t):
            if kind == "tag":
                closing, name, attrs = m.group(3), m.group(4), m.group(5)
                if closing:
                    # pop to matching name
                    for i in range(len(stack) - 1, -1, -1):
                        if stack[i] == name:
                            del stack[i:]
                            break
                    continue
                if attrs and "=" in attrs:
                    base = m.start(5)
                    for am in _ATTR.finditer(attrs):
                        an = am.group(1)
                        if an in _SKIP_ATTRS or an.startswith("xmlns"):
                            continue
                        q = am.group(3)
                        raw = q[1:-1]
                        if not raw.strip():
                            continue
                        if raw.startswith("data:"):
                            n_data += 1
                            continue
                        val = _unent(raw)
                        new = fn("attr", val, (name, an, stack))
                        if new is not None and new != val and not collect_only:
                            s = base + am.start(3) + 1
                            out.append(t[pos:s])
                            out.append(_ent(new, q[0]))
                            pos = s + len(raw)
                if not attrs.rstrip().endswith("/"):
                    stack.append(name)
            elif kind == "text":
                raw = m.group(6)
                if not raw.strip():
                    continue
                val = _unent(raw)
                new = fn("text", val, (stack[-1] if stack else "", None, stack))
                if new is not None and new != val and not collect_only:
                    out.append(t[pos:m.start(6)])
                    out.append(_ent(new))
                    pos = m.end(6)
            elif kind == "cdata":
                raw = m.group(2)
                if not raw.strip():
                    continue
                new = fn("cdata", raw, (stack[-1] if stack else "", None, stack))
                if new is not None and new != raw and not collect_only:
                    out.append(t[pos:m.start(2)])
                    out.append(new.replace("]]>", "]] >"))
                    pos = m.end(2)
            elif kind == "cmt":
                raw = m.group(1)
                if not raw.strip():
                    continue
                new = fn("cmt", raw, ("", None, stack))
                if new is not None and new != raw and not collect_only:
                    out.append(t[pos:m.start(1)])
                    out.append(new.replace("--", "- -"))
                    pos = m.end(1)
        out.append(t[pos:])
        return "".join(out), n_data


def _kind_for(stack, attr_name=None) -> str:
    low = [s.lower() for s in stack]
    if any(any(k in s for k in _PERSON_CTX) for s in low):
        return "person"
    if any(any(k in s for k in _COMPANY_CTX) for s in low):
        return "company"
    return "free_text"


class SvgAdapter(Adapter):
    name = "svg"
    version = "2"
    priority = 10
    magics = ("svg",)
    extensions = (".svg",)
    path_independent = False
    timeout_s = 900.0

    def matches(self, ctx, head):
        if ctx.magic == "svg":
            return True
        return ctx.ext == ".svg" and ctx.magic in ("xml", "text") and b"<svg" in head

    def scan(self, ctx, src_path):
        data = open(src_path, "rb").read()
        if not data.strip():
            return ScanResult(status="source_exception", notes=["empty file"])
        text, enc, lossy = _decode(data)
        col = ObsCollector(cap=3000)

        def fn(kind, val, c):
            el, an, stack = c
            v = val.strip()
            if not v or not any(ch.isalpha() for ch in v):
                return None
            if kind == "attr":
                local = an.split(":")[-1]
                if an in _REF_ATTRS or local == "href":
                    if not v.startswith("#"):
                        col.ref(v, ref_kind(v), f"svg.@{an}")
                        col.add("path", f"svg.@{an}", v, el, 0.6)
                    return None
                if any(k in local.lower() for k in ("label", "name", "title", "author", "creator", "owner",
                                                     "company", "desc", "alt", "comment", "note")):
                    col.add(_kind_for(stack), f"svg.@{an}", v, el, 0.5)
                return None
            ctxname = "/".join(s.split(":")[-1] for s in stack[-3:])
            if kind == "cmt":
                col.add("free_text", "svg.comment", v, "comment", 0.5)
            else:
                k = _kind_for(stack)
                col.add(k, f"svg.{ctxname or 'root'}", v, el, 0.7 if k != "free_text" else 0.5)
            return None

        _, n_data = _Walker(text).run(fn, collect_only=True)
        notes = []
        if n_data:
            notes.append(f"{n_data} embedded data URIs left intact")
        if lossy:
            notes.append("undecodable bytes; latin-1 fallback")
        return ScanResult(observations=col.obs, references=col.refs,
                          fingerprint={"encoding": enc, "data_uris": n_data}, notes=notes)

    def rewrite(self, ctx, src_path, dst_path, plan):
        data = open(src_path, "rb").read()
        if not data.strip():
            return RewriteResult(status=SOURCE_EXCEPTION, notes=["empty file"])
        text, enc, lossy = _decode(data)
        pc = PlanCache(plan, ctx)

        def fn(kind, val, c):
            el, an, stack = c
            if not val.strip():
                return None
            if kind == "attr":
                local = an.split(":")[-1]
                if an in _REF_ATTRS or local == "href":
                    if val.startswith("#"):
                        return None
                    return pc.ref(val)
                return pc.sub(val, _kind_for(stack)) if _kind_for(stack) != "free_text" else pc.text(val)
            if kind == "text" and _kind_for(stack) != "free_text":
                return pc.sub(val, _kind_for(stack))
            return pc.text(val)

        new, _ = _Walker(text).run(fn)
        if new == text:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        out = new.encode(enc if enc != "utf-8-sig" else "utf-8-sig", errors="xmlcharrefreplace") \
            if not lossy else new.encode("latin-1", errors="xmlcharrefreplace")
        with open(dst_path, "wb") as f:
            f.write(out)
        return RewriteResult(status=REWRITTEN)

    def verify(self, ctx, src_path, dst_path, plan):
        a = svg_fingerprint(src_path)
        b = svg_fingerprint(dst_path)
        notes = [k for k in ("markup_sha256", "elements", "wellformed") if a[k] != b[k]]
        if a["wellformed"] and not b["wellformed"]:
            notes.append("output not well-formed")
        return VerifyResult(ok=not notes, notes=notes, fingerprint=b)


def svg_fingerprint(path: str) -> dict:
    data = open(path, "rb").read()
    text, enc, lossy = _decode(data)
    h = hashlib.sha256()
    elements = 0
    for kind, m in _iter(text):
        if kind == "tag":
            if not m.group(3):
                elements += 1
            # mask editable attribute values (same rule as the walker), keep the rest
            attrs = m.group(5)
            masked = _ATTR.sub(lambda am: am.group(1) + am.group(2) + (
                am.group(3) if (am.group(1) in _SKIP_ATTRS or am.group(1).startswith("xmlns")
                                or am.group(3)[1:].startswith("data:")) else "\0"), attrs)
            h.update(("<" + m.group(3) + m.group(4) + masked + ">").encode("utf-8", "surrogatepass"))
        elif kind in ("text", "cmt", "cdata"):
            h.update(b"\1")
        else:
            h.update(m.group(0).encode("utf-8", "surrogatepass"))
    wf = True
    try:
        from lxml import etree
        parser = etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True, load_dtd=False)
        etree.fromstring(data, parser)
    except Exception:
        wf = False
    return {"markup_sha256": h.hexdigest(), "elements": elements, "wellformed": wf}


register(SvgAdapter())
