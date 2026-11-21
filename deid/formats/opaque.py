"""Opaque native binaries (docs-native): JT, U3D, CATIA V5 (.CATPart
.CATProduct .CATDrawing .cgr), Parasolid binary (.x_b .xmt_bin) when no
better adapter claims it, NX (SPLMSS), Creo .mf1, MS-Solids .brp.

scan(): printable ASCII / UTF-16LE strings are mined for contact details,
user-profile paths, file paths and CAD file references (kind "component",
field ``opaque.<fmt>#fixedlen``).

rewrite():
* U3D: block structure parsed (type, data size, metadata size); strings
  (U16 length-prefixed) in block metadata and data rewritten at the same
  byte length only.  ECMA-363 has no checksums, so equal-length edits keep
  the file valid.
* everything else: copied unchanged (``copied_unsupported``): compressed or
  checksummed sections we cannot re-encode safely.
"""
from __future__ import annotations

import re
import struct

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult, Reference,
                      REWRITTEN, VERIFIED_UNCHANGED, COPIED_UNSUPPORTED)
from deid.formats import _nativeutil as nu

EXTS = {".jt": "jt", ".u3d": "u3d", ".catpart": "catia", ".catproduct": "catia", ".catdrawing": "catia",
        ".cgr": "catia", ".catmaterial": "catia", ".x_b": "parasolid_bin", ".xmt_bin": "parasolid_bin",
        ".mf1": "mf1", ".brp": "brp"}
_ASC = re.compile(rb"[\x20-\x7e]{6,}")
_U16 = re.compile(rb"(?:[\x20-\x7e]\x00){5,}")
CADREF = re.compile(r"[^\\/:*?\"<>|\x00]{1,200}\.(catpart|catproduct|catdrawing|cgr|model|prt|asm|drw|jt|"
                    r"sldprt|sldasm|ipt|iam|step|stp|igs|iges|x_t|x_b|wrl|stl)\b", re.I)
MAX_SCAN = 256 << 20


def _fmt(ctx, head):
    if head.startswith(b"U3D\x00"):
        return "u3d"
    if head.startswith(b"V5_CFV2"):
        return "catia"
    if head.startswith(b"Version ") and b"JT" in head[:80]:
        return "jt"
    if head.startswith(b"SPLMSS"):
        return "nx"
    return EXTS.get(ctx.ext)


class OpaqueAdapter(Adapter):
    name = "opaque"
    version = "1"
    priority = 1   # lowest: real adapters for these formats win
    magics = ("jt", "u3d")
    path_independent = True

    def matches(self, ctx, head):
        f = _fmt(ctx, head)
        if f is None:
            return False
        if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or head[:4] == b"PK\x03\x04":
            return False
        return True

    def scan(self, ctx, src_path):
        with open(src_path, "rb") as f:
            data = f.read(MAX_SCAN)
        fmt = _fmt(ctx, data[:128]) or "binary"
        obs, refs = [], []
        seen = set()
        for rx, enc in ((_ASC, "latin-1"), (_U16, "utf-16-le")):
            for m in rx.finditer(data):
                s = m.group(0).decode(enc, "replace")
                if s in seen:
                    continue
                seen.add(s)
                if len(seen) > 20000:
                    break
                nu.contact_observations(s, f"opaque.{fmt}", obs, limit=50)
                for r in CADREF.finditer(s):
                    v = r.group(0).strip()
                    if len(v) > 4 and not v.startswith("."):
                        refs.append(Reference(v, "component", f"opaque.{fmt}#fixedlen"))
        if fmt == "u3d":
            for _, _, s in self._u3d_strings(data):
                nu.add_obs(obs, "free_text", "opaque.u3d.string", s, "U3D name/metadata", 0.2)
        if fmt == "jt":
            nu.add_obs(obs, "software", "opaque.jt.header", data[:80].decode("latin-1").strip(), "JT version", 0.1)
        return ScanResult(observations=nu.dedupe_obs(obs, 2000), references=nu.dedupe_refs(refs, 2000),
                          fingerprint={"format": fmt, "size": len(data)})

    # -- U3D -----------------------------------------------------------------
    @staticmethod
    def _u3d_blocks(data):
        p = 0
        n = len(data)
        while p + 12 <= n:
            typ, dsz, msz = struct.unpack_from("<III", data, p)
            ds = p + 12
            de = ds + dsz
            ms = de + ((-dsz) % 4)
            me = ms + msz
            if me > n or dsz > n or msz > n:
                return
            yield typ, ds, de, ms, me
            p = me + ((-msz) % 4)

    def _u3d_strings(self, data):
        """(offset, nbytes, text) of U16-length-prefixed printable strings."""
        out = []
        for typ, ds, de, ms, me in self._u3d_blocks(data):
            for a, b in ((ds, de), (ms, me)):
                i = a
                while i + 2 < b:
                    ln = data[i] | data[i + 1] << 8
                    if 3 <= ln <= 1024 and i + 2 + ln <= b:
                        s = data[i + 2:i + 2 + ln]
                        if all(32 <= c < 127 or c >= 0xC0 for c in s) and any(65 <= (c | 32) <= 122 for c in s):
                            try:
                                out.append((i + 2, ln, s.decode("utf-8")))
                                i += 2 + ln
                                continue
                            except UnicodeDecodeError:
                                pass
                    i += 1
        return out

    def rewrite(self, ctx, src_path, dst_path, plan):
        with open(src_path, "rb") as f:
            data = f.read()
        fmt = _fmt(ctx, data[:128])
        if fmt != "u3d":
            return RewriteResult(status=COPIED_UNSUPPORTED, notes=[f"{fmt}: opaque format, scanned only"])
        buf = None
        n = 0
        for off, ln, s in self._u3d_strings(data):
            ns = nu.sub_value(plan, s)
            if ns == s:
                continue
            nb = ns.encode("utf-8")
            if len(nb) != ln:
                try:
                    nb = plan.replace_bytes(s.encode("utf-8"), ("utf-8",), True)
                except Exception:
                    continue
                if len(nb) != ln:
                    continue
            if buf is None:
                buf = bytearray(data)
            buf[off:off + ln] = nb
            n += 1
        if buf is None:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        with open(dst_path, "wb") as f:
            f.write(buf)
        return RewriteResult(status=REWRITTEN, counts={"u3d_strings": n})

    def verify(self, ctx, src_path, dst_path, plan):
        a = open(src_path, "rb").read()
        b = open(dst_path, "rb").read()
        if len(a) != len(b):
            return VerifyResult(ok=False, notes=["size changed"])
        ba = [(t, ds, de, ms, me) for t, ds, de, ms, me in self._u3d_blocks(a)]
        bb = [(t, ds, de, ms, me) for t, ds, de, ms, me in self._u3d_blocks(b)]
        if ba != bb or not ba:
            return VerifyResult(ok=False, notes=["U3D block layout changed"])
        return VerifyResult(ok=True)


register(OpaqueAdapter())
