"""OpenCASCADE BRep text (.brep .brp when the content is OCCT BRep).

The format carries only topology, geometry and locations: no names, owners or
free text.  The adapter claims the file so it is recorded as understood and
copied unchanged (VERIFIED_UNCHANGED).  Opaque ``.brp`` content that is not
OCCT BRep is not claimed.
"""
from __future__ import annotations

import re

from deid.api import Adapter, register, ScanResult, RewriteResult, VerifyResult, VERIFIED_UNCHANGED

_SIG = re.compile(rb"\s*(?:DBRep_DrawableShape\s*)?CASCADE Topology V\d")


class BrepAdapter(Adapter):
    name = "brep"
    version = "1"
    priority = 10
    magics = ("brep",)
    extensions = (".brep", ".brp")
    path_independent = True

    def matches(self, ctx, head):
        return ctx.magic == "brep" or bool(_SIG.match(head))

    def scan(self, ctx, src_path):
        with open(src_path, "rb") as f:
            head = f.read(4096)
        ok = bool(_SIG.match(head))
        return ScanResult(status="ok" if ok else "source_exception",
                          fingerprint={"occt_brep": ok}, notes=[] if ok else ["no OCCT BRep signature"])

    def rewrite(self, ctx, src_path, dst_path, plan):
        return RewriteResult(status=VERIFIED_UNCHANGED, notes=["OCCT BRep carries no identifying text"])


register(BrepAdapter())
