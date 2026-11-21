"""Independent geometry check of rewritten STEP/IGES files with OpenCASCADE (OCP).

Not a pytest module (needs the optional LGPL `cadquery-ocp` package):
    python tests/formats_neutral/occt_check.py OUT.tsv FILE...

Each file is rewritten with an aggressive stub plan built from its own scan
observations (every observed string gets a different-length pseudonym), then the
original and the rewritten file are loaded with OCCT and compared: transfer
status, number of root shapes, solids/shells/faces/edges/vertices, bounding box
and total volume.  Prints aggregate results only (no file contents)."""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from deid import api  # noqa: E402
from deid.formats import step, iges  # noqa: E402,F401

from OCP.STEPControl import STEPControl_Reader  # noqa: E402
from OCP.IGESControl import IGESControl_Reader  # noqa: E402
from OCP.IFSelect import IFSelect_RetDone  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopAbs import TopAbs_SOLID, TopAbs_SHELL, TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX  # noqa: E402
from OCP.Bnd import Bnd_Box  # noqa: E402
from OCP.BRepBndLib import BRepBndLib  # noqa: E402
from OCP.GProp import GProp_GProps  # noqa: E402
from OCP.BRepGProp import BRepGProp  # noqa: E402


class StubPlan:
    version = "occt-check"

    def __init__(self, values):
        vals = sorted({v for v in values if v and len(v.strip()) >= 3 and len(v) < 200}, key=len, reverse=True)
        self.m = {v: "Q" + hashlib.md5(v.encode()).hexdigest()[:len(v) % 7 + 4] for v in vals}
        self.rx = re.compile("|".join(re.escape(v) for v in vals)) if vals else None

    def alias(self, v, kind=None):
        return self.m.get(v)

    def replace_text(self, t):
        return self.rx.sub(lambda m: self.m[m.group(0)], t) if self.rx else t

    def replace_bytes(self, d, encodings=("utf-8",), length_preserving=False):
        return d

    def map_path(self, p):
        return p

    def map_reference(self, ctx, r):
        return self.replace_text(r)


def count(shape, kind):
    n = 0
    e = TopExp_Explorer(shape, kind)
    while e.More():
        n += 1
        e.Next()
    return n


def summary(path, kind):
    r = STEPControl_Reader() if kind == "step" else IGESControl_Reader()
    st = r.ReadFile(path)
    if st != IFSelect_RetDone:
        return {"read": int(st)}
    r.TransferRoots()
    shape = r.OneShape()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        bb = ()
    else:
        lo, hi = box.CornerMin(), box.CornerMax()
        bb = tuple(round(v, 6) for v in (lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()))
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return {"read": 0, "roots": r.NbShapes(), "solids": count(shape, TopAbs_SOLID),
            "shells": count(shape, TopAbs_SHELL), "faces": count(shape, TopAbs_FACE),
            "edges": count(shape, TopAbs_EDGE), "vertices": count(shape, TopAbs_VERTEX),
            "bbox": bb, "volume": round(props.Mass(), 6)}


def main(out_tsv, files):
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for p in files:
            with open(p, "rb") as f:
                head = f.read(api.HEAD_BYTES)
            ctx = api.FileContext(logical_path=p, magic=api.sniff(head, p), size=os.path.getsize(p), tmp_dir=td)
            ad = api.route(ctx, head)
            if ad is None or ad.name not in ("step", "iges"):
                continue
            sr = ad.scan(ctx, p)
            plan = StubPlan([o.value for o in sr.observations if o.kind != "software"])
            dst = os.path.join(td, "out" + ctx.ext)
            t0 = time.time()
            rr = ad.rewrite(ctx, p, dst, plan)
            if rr.status != api.REWRITTEN:
                rows.append((ad.name, ctx.size, rr.status, "", ""))
                continue
            vr = ad.verify(ctx, p, dst, plan)
            a = summary(p, ad.name)
            b = summary(dst, ad.name)
            same = "same" if a == b else "DIFF"
            rows.append((ad.name, ctx.size, "rewritten", "verify_ok" if vr.ok else "verify_fail", same,
                         a.get("faces", -1), a.get("solids", -1), round(time.time() - t0, 1)))
            os.unlink(dst)
    with open(out_tsv, "w") as f:
        for r in rows:
            f.write("\t".join(map(str, r)) + "\n")
    from collections import Counter
    c = Counter((r[0], r[2], r[3], r[4]) for r in rows)
    for k, v in sorted(c.items()):
        print(v, *k)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
