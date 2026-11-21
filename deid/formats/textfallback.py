"""Generic fallback for confidently plain-text files no other adapter claims,
plus Creo/Pro-E neutral text (.neu).

Modes:
* free-form text (known prose/config/data extensions, XML/JSON): decoded,
  ``plan.replace_text`` per chunk, re-encoded in the source encoding; line
  count must stay identical, XML stays well-formed, JSON stays parseable.
* unknown text formats: ``plan.replace_bytes(..., length_preserving=True)`` so
  any fixed-width layout or byte offsets are preserved (verify: same size).
* .neu: only non-numeric value lines change; the ``#UGC`` header line and all
  numeric records are byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION)
from deid.formats._neutral import PlanCache, ObsCollector, EMAIL_RE, URL_RE

FREE_EXT = {
    ".txt", ".text", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".ini",
    ".cfg", ".conf", ".yaml", ".yml", ".toml", ".log", ".readme", ".py", ".m", ".bat", ".sh", ".gcode",
    ".nc", ".ngc", ".tap", ".cnc", ".gco", ".g", ".bom", ".lst", ".rtf", ".tex", ".properties", ".url",
    ".desktop", ".kicad_pcb", ".kicad_sch", ".kicad_pro", ".sch", ".lib", ".net", ".pro", ".cir", ".sp",
    ".ino", ".c", ".h", ".cpp", ".hpp", ".js", ".ts", ".css", ".scss", ".java", ".cs", ".vb", ".lua",
    ".mtl", ".info", ".nfo", ".license", ".gitignore", ".gitattributes", ".ipj", ".bas", ".vbs",
}
CHUNK = 4 << 20


def _encoding(head: bytes) -> str:
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        head[:65536].decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as e:
        if e.start > len(head[:65536]) - 4:
            return "utf-8"
        return "cp1252"


def _confident_text(head: bytes) -> bool:
    if not head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            head[:4096 - (len(head[:4096]) % 2)].decode("utf-16")
            return True
        except UnicodeDecodeError:
            return False
    sample = head[:8192]
    if b"\x00" in sample:
        return False
    bad = sum(1 for c in sample if c < 9 or (13 < c < 32 and c != 27))
    return bad == 0


def _chunks(path):
    """Yield text-safe byte chunks cut at newline boundaries."""
    with open(path, "rb") as f:
        carry = b""
        while True:
            data = f.read(CHUNK)
            buf = carry + data
            if not data:
                if buf:
                    yield buf
                return
            cut = buf.rfind(b"\n") + 1
            if cut == 0:
                carry = buf
                if len(carry) > (64 << 20):
                    yield carry
                    carry = b""
                continue
            yield buf[:cut]
            carry = buf[cut:]


class TextFallbackAdapter(Adapter):
    name = "text"
    version = "1"
    priority = -50
    magics = ("text", "xml")
    extensions = ()
    path_independent = False
    timeout_s = 900.0

    def matches(self, ctx, head):
        if ctx.magic not in ("text", "xml"):
            return False
        return _confident_text(head)

    @staticmethod
    def _mode(ctx, head) -> str:
        if ctx.ext in FREE_EXT or head.lstrip().startswith((b"<?xml", b"{", b"[")) and ctx.ext in ("", ".json", ".xml"):
            return "free"
        if ctx.magic == "xml":
            return "free"
        return "fixed"

    def scan(self, ctx, src_path):
        with open(src_path, "rb") as f:
            head = f.read(1 << 16)
        if not head.strip():
            return ScanResult(status="source_exception", notes=["empty file"])
        enc = _encoding(head)
        col = ObsCollector(cap=500)
        # free text is too broad to emit wholesale: report contacts and a bounded sample of lines
        nlines = 0
        for chunk in _chunks(src_path):
            text = chunk.decode(enc if enc != "utf-16" else "utf-16", errors="replace")
            for m in EMAIL_RE.finditer(text):
                col.add("email", f"text{ctx.ext}", m.group(0), "regex", 0.9)
            for m in URL_RE.finditer(text):
                col.add("url", f"text{ctx.ext}", m.group(0), "regex", 0.5)
            nlines += text.count("\n")
            if col.dropped:
                break
        return ScanResult(observations=col.obs, fingerprint={"mode": self._mode(ctx, head), "encoding": enc,
                                                             "lines": nlines})

    def rewrite(self, ctx, src_path, dst_path, plan):
        with open(src_path, "rb") as f:
            head = f.read(1 << 16)
        if not head.strip():
            return RewriteResult(status=SOURCE_EXCEPTION, notes=["empty file"])
        enc = _encoding(head)
        mode = self._mode(ctx, head)
        if enc == "utf-16" and mode != "free":
            return RewriteResult(status=VERIFIED_UNCHANGED, notes=["utf-16 text of unknown format left unchanged"])
        changed = False
        tmp = dst_path + ".part"
        with open(tmp, "wb") as out:
            if enc == "utf-16":
                data = open(src_path, "rb").read()
                text = data.decode("utf-16")
                new = plan.replace_text(text)
                changed = new != text
                bom = data[:2]
                out.write(bom + new.encode("utf-16-le" if bom == b"\xff\xfe" else "utf-16-be"))
            else:
                first = True
                for chunk in _chunks(src_path):
                    if mode == "free":
                        e = enc if (first or enc != "utf-8-sig") else "utf-8"
                        text = chunk.decode(e, errors="surrogateescape")
                        new = plan.replace_text(text)
                        nb = chunk if new == text else new.encode(e, errors="surrogateescape")
                    else:
                        nb = plan.replace_bytes(chunk, encodings=(enc if enc != "utf-8-sig" else "utf-8",),
                                                length_preserving=True)
                    if nb != chunk:
                        changed = True
                    out.write(nb)
                    first = False
        if not changed:
            os.unlink(tmp)
            return RewriteResult(status=VERIFIED_UNCHANGED)
        os.replace(tmp, dst_path)
        return RewriteResult(status=REWRITTEN, notes=[f"mode={mode}"])

    def verify(self, ctx, src_path, dst_path, plan):
        with open(src_path, "rb") as f:
            head = f.read(1 << 16)
        mode = self._mode(ctx, head)
        notes = []
        if mode == "fixed" and os.path.getsize(src_path) != os.path.getsize(dst_path):
            notes.append("size changed in length-preserving mode")
        a = _line_count(src_path)
        b = _line_count(dst_path)
        if a != b:
            notes.append("line count changed")
        if not notes and os.path.getsize(src_path) < (64 << 20):
            sa, sb = _parses(src_path, ctx, head), _parses(dst_path, ctx, head)
            if sa and not sb:
                notes.append("structured text no longer parses")
        return VerifyResult(ok=not notes, notes=notes)


def _line_count(p):
    n = 0
    with open(p, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                return n
            n += b.count(b"\n")


def _parses(path, ctx, head) -> bool:
    s = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    data = open(path, "rb").read()
    try:
        if ctx.ext == ".json" or (s[:1] in (b"{", b"[") and ctx.ext in ("", ".json")):
            json.loads(data.decode(_encoding(head)))
            return True
        if s.startswith(b"<?xml") or ctx.magic == "xml" or ctx.ext in (".xml", ".ipj"):
            from lxml import etree
            etree.fromstring(data, etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True,
                                                   load_dtd=False))
            return True
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Creo / Pro-ENGINEER neutral text (.neu)
# ---------------------------------------------------------------------------
_NEU_LINE = re.compile(rb"^(\d+ [A-Za-z_][\w\[\]().]*[ \t]+)(.*?)([ \t]*\r?)$")
_NEU_NUM = re.compile(rb"^[-+0-9.,*eEdD \[\]]*$|^NULL$|^->$")


class NeuAdapter(Adapter):
    name = "neu"
    version = "1"
    priority = 20
    magics = ()
    extensions = (".neu",)
    path_independent = True

    def matches(self, ctx, head):
        first = head.split(b"\n", 1)[0]
        return first.startswith(b"#UGC") and b"NEUTRAL" in first[:40] and _confident_text(head[:4096])

    def _walk(self, data, fn):
        out = []
        pos = 0
        for m in re.finditer(rb"(?m)^[^\n]*", data):
            line = m.group(0)
            if line.startswith(b"#"):
                if line.startswith(b"#UGC"):
                    continue
                new = fn("comment", line[1:])
                if new is not None and new != line[1:]:
                    out.append(data[pos:m.start() + 1]); out.append(new); pos = m.end()
                continue
            lm = _NEU_LINE.match(line)
            if not lm or _NEU_NUM.match(lm.group(2)) or not re.search(rb"[A-Za-z]", lm.group(2)):
                continue
            new = fn("value", lm.group(2))
            if new is not None and new != lm.group(2):
                s = m.start() + lm.start(2)
                out.append(data[pos:s]); out.append(new); pos = m.start() + lm.end(2)
        out.append(data[pos:])
        return b"".join(out)

    def scan(self, ctx, src_path):
        data = open(src_path, "rb").read()
        col = ObsCollector(cap=1000)

        def fn(kind, raw):
            v = raw.decode("latin-1").strip()
            if sum(c.isalpha() for c in v) >= 2 and not re.fullmatch(r"d\d+|[A-Z_]+\d*", v):
                col.add("free_text", f"neu.{kind}", v, kind, 0.4)
            return None

        self._walk(data, fn)
        return ScanResult(observations=col.obs)

    def rewrite(self, ctx, src_path, dst_path, plan):
        data = open(src_path, "rb").read()
        pc = PlanCache(plan, ctx)

        def fn(kind, raw):
            v = raw.decode("latin-1")
            nv = pc.text(v)
            return None if nv == v else nv.encode("latin-1", "replace").replace(b"\n", b" ")

        new = self._walk(data, fn)
        if new == data:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        with open(dst_path, "wb") as f:
            f.write(new)
        return RewriteResult(status=REWRITTEN)

    def verify(self, ctx, src_path, dst_path, plan):
        def fp(p):
            d = open(p, "rb").read()
            return hashlib.sha256(self._walk(d, lambda k, r: b"\0")).hexdigest(), d.count(b"\n")
        ok = fp(src_path) == fp(dst_path)
        return VerifyResult(ok=ok, notes=[] if ok else ["numeric records differ"])


register(TextFallbackAdapter())
register(NeuAdapter())
