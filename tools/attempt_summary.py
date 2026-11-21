#!/usr/bin/env python3
"""One-page private summary of an attempt: hypothesis, statuses, gate, leak groups, refs, diff."""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3

GROUPS = [
    ("Creo (.prt/.asm/.drw[.N])", lambda e: e in (".prt.N", ".asm.N", ".drw.N", ".prt", ".asm", ".drw", ".neu")),
    ("PDF", lambda e: e == ".pdf"),
    ("SolidWorks", lambda e: e.startswith(".sld") or e in (".prtdot", ".asmdot", ".drwdot")),
    ("Inventor/SolidEdge (CFB)", lambda e: e in (".ipt", ".iam", ".idw", ".ipn", ".par", ".dft", ".psm")),
    ("Fusion/3MF/FCStd packages", lambda e: e in (".f3d", ".f3z", ".3mf", ".fcstd", ".odg")),
    ("CATIA/JT/U3D/other native", lambda e: e in (".catpart", ".catproduct", ".catdrawing", ".jt", ".u3d", ".mf1", ".brp", ".x_b")),
    ("Neutral CAD (STEP/IGES/SAT/DXF/mesh)", lambda e: e in (".step", ".stp", ".igs", ".iges", ".sat", ".dxf", ".stl", ".obj", ".wrl", ".x_t", ".xmt_txt", ".unv", ".brep")),
    ("DWG", lambda e: e == ".dwg"),
    ("Archives", lambda e: e in (".zip", ".tar", ".gz", ".tgz", ".z", ".7z", ".rar", ".resume-partial")),
]


def ext_of(p):
    base = p.rsplit("/", 1)[-1]
    parts = base.split(".")
    if len(parts) >= 3 and parts[-1].isdigit():
        return "." + parts[-2].lower() + ".N"
    return "." + parts[-1].lower() if len(parts) > 1 else ""


def group(e):
    for name, f in GROUPS:
        if f(e):
            return name
    return "Images/SVG/PS/other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", required=True)
    ap.add_argument("--prev", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--hypothesis", default="")
    ap.add_argument("--code", default="")
    ap.add_argument("--timing", default="")
    ap.add_argument("--preflight", default="")
    a = ap.parse_args()
    W = a.work
    rep = json.load(open(os.path.join(W, "validate", "report.json")))
    con = sqlite3.connect(f"file:{W}/results.sqlite?mode=ro", uri=True)
    st = collections.Counter(s for (s,) in con.execute("SELECT status FROM results"))
    leak_files = collections.defaultdict(set)
    lp = os.path.join(W, "validate", "leaks.tsv")
    if os.path.exists(lp):
        for line in open(lp, encoding="utf-8", errors="replace"):
            p = line.split("\t", 1)[0]
            leak_files[group(ext_of(p))].add(p)
    plan = json.load(open(os.path.join(W, "plan", "plan.json")))
    L = [f"ATTEMPT {a.n} (built on attempt {a.prev})  code {a.code[:12]}  plan {plan.get('version')}",
         f"Hypothesis: {a.hypothesis}", f"Timing: {a.timing}", "",
         "Gate: " + ("PASS" if not rep.get("hard_failures") else "FAIL " + ", ".join(rep["hard_failures"])),
         f"  outputs {rep['coverage'].get('ok', 0)}/{rep['inputs']}, objects {rep['limits'].get('objects')}, "
         f"bytes {rep['limits'].get('bytes')}, over 2 GB {rep['limits'].get('over_2GB', 0)}",
         "  statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(st.items())),
         "  references: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.get("refs", {}).items())),
         "  aliases: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.get("aliases", {}).items())),
         f"  plan entries: {len(plan['entries'])} ({collections.Counter(e['mode'] for e in plan['entries'])})", "",
         "Residual identifier hits (files) by group:"]
    tot = 0
    for name, _ in GROUPS + [("Images/SVG/PS/other", None)]:
        n = len(leak_files.get(name, ()))
        tot += n
        if n:
            L.append(f"  {name}: {n}")
    L.append(f"  total files with hits: {tot}")
    dp = os.path.join(W, "diff", "diff_summary.txt")
    L.append("")
    L.extend(open(dp).read().rstrip("\n").split("\n")[:40] if os.path.exists(dp) else ["(no diff)"])
    if a.preflight and os.path.exists(a.preflight):
        L.append("")
        L.append("Preflight: " + " | ".join(x.strip() for x in open(a.preflight) if x.startswith(("FAIL", "WARN"))) or "Preflight: clean")
    print("\n".join(L))


if __name__ == "__main__":
    main()
