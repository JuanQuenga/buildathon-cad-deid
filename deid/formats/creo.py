"""PTC Creo / Pro/ENGINEER native models (.prt .asm .drw .frm .lay .sec, with
or without the ``.N`` version suffix), the ``#UGC`` container.

Layout: a fixed-width, backslash-continued ASCII header (``#UGC:2 <TYPE> ...``,
``#- VERS``, ``#- HOST``, ``#- CMNM <len3><model name>`` ...,
``#-END_OF_UGC_HEADER``), a TOC whose entries carry per-section checksums,
and sections that are compressed with a proprietary scheme (binary files) or
plain records (old text files).  Component and drawing references live inside
those checksummed sections, so they cannot be rewritten here.

Rewrite: only the header values (HOST machine name, CMNM model name, DBID)
are changed, at exactly the same length (identity aliases are
length-preserving), keeping the padding and line structure; everything else
is byte-identical.  Scan reports the header values and the model name as a
"fixedlen" self reference so identity can keep Creo names stable.
"""
from __future__ import annotations

import re

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult, Reference,
                      REWRITTEN, VERIFIED_UNCHANGED)
from deid.formats import _nativeutil as nu

CREO_EXT_RE = re.compile(r"\.(prt|asm|drw|frm|lay|sec|mfg|neu|dgm|mrk|rep|pic)(\.\d+)?$", re.I)
_HDR_END = b"#-END_OF_UGC_HEADER"
_FIELD = re.compile(rb"#- (HOST|CMNM|DBID|LINK|MACH) ([^\\\n]*?)( *\\?)(\r?\n)")


class CreoAdapter(Adapter):
    name = "creo"
    version = "1"
    priority = 10
    magics = ("creo",)
    path_independent = False   # CMNM follows the mapped file name

    def matches(self, ctx, head):
        return head.startswith(b"#UGC")

    def _header(self, data: bytes):
        end = data.find(_HDR_END, 0, 65536)
        return end if end > 0 else -1

    def scan(self, ctx, src_path):
        with open(src_path, "rb") as f:
            head = f.read(65536)
        end = self._header(head)
        if end < 0:
            return ScanResult(status="unsupported", notes=["no UGC header"])
        obs, refs = [], []
        first = head.split(b"\n", 1)[0].decode("latin-1")
        fp = {"type": (first.split() + ["", ""])[1], "text": b"\x00" not in head}
        for m in _FIELD.finditer(head[:end]):
            k, v = m.group(1).decode(), m.group(2).decode("latin-1").strip()
            if not v:
                continue
            if k == "HOST":
                nu.add_obs(obs, "account", "creo.header.HOST", v, "machine name", 0.7)
            elif k == "CMNM":
                name = v[3:] if v[:3].isdigit() else v
                nu.add_obs(obs, "part_name", "creo.header.CMNM", name, "model name", 0.4)
                refs.append(Reference(name, "other", "creo.header.CMNM#fixedlen"))
            elif k == "DBID" and v not in ("PSA",):
                nu.add_obs(obs, "free_text", "creo.header.DBID", v, "database id", 0.3)
        return ScanResult(observations=obs, references=refs, fingerprint=fp)

    def rewrite(self, ctx, src_path, dst_path, plan):
        with open(src_path, "rb") as f:
            data = f.read()
        end = self._header(data)
        if end < 0:
            return RewriteResult(status=VERIFIED_UNCHANGED, notes=["no UGC header"])
        buf = bytearray(data)
        n = 0
        for m in _FIELD.finditer(data[:end]):
            k = m.group(1).decode()
            raw = m.group(2)
            v = raw.decode("latin-1")
            if not v.strip() or k in ("MACH",):
                continue
            if k == "CMNM" and v[:3].isdigit():
                pre, val = v[:3], v[3:]
                nv = None
                base = re.sub(r"\.\d+$", "", ctx.member_name.rsplit("/", 1)[-1])
                try:
                    mbase = re.sub(r"\.\d+$", "", plan.map_path(ctx.logical_path).rsplit("/", 1)[-1])
                except Exception:
                    mbase = base
                vs = val.rstrip()
                if vs.lower() == base.lower() and len(mbase) == len(base):
                    # model name == file name: keep them equal after mapping
                    nv = (mbase if vs == base else mbase.lower() if vs == base.lower() else mbase) + val[len(vs):]
                if nv is None or nv == val:
                    nv = plan.replace_text(val) if mbase.lower() != base.lower() or vs.lower() != base.lower() else val
                new = pre + nv if len(nv) == len(val) else None
            else:
                nv = nu.sub_value(plan, v, "account" if k == "HOST" else None)
                new = nv if len(nv) == len(v) else None
            if new is None or new == v:
                continue
            try:
                nb = new.encode("latin-1")
            except UnicodeEncodeError:
                continue
            if len(nb) != len(raw):
                continue
            buf[m.start(2):m.end(2)] = nb
            n += 1
        if not n:
            return RewriteResult(status=VERIFIED_UNCHANGED)
        with open(dst_path, "wb") as f:
            f.write(buf)
        return RewriteResult(status=REWRITTEN, counts={"header_fields": n})

    def verify(self, ctx, src_path, dst_path, plan):
        a = open(src_path, "rb").read()
        b = open(dst_path, "rb").read()
        if len(a) != len(b):
            return VerifyResult(ok=False, notes=["size changed"])
        end = self._header(a)
        if end < 0 or a[end:] != b[end:] or self._header(b) != end:
            return VerifyResult(ok=False, notes=["content outside header changed"])
        if a[:end].count(b"\n") != b[:end].count(b"\n"):
            return VerifyResult(ok=False, notes=["header layout changed"])
        return VerifyResult(ok=True)


register(CreoAdapter())
