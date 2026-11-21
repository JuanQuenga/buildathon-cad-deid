"""CLI: python -m deid.pipeline scan|plan|rewrite|validate|package|one ..."""
from __future__ import annotations

import argparse
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m deid.pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, out=False):
        p.add_argument("--input", required=True, help="raw corpus root (manifest paths below it)")
        p.add_argument("--work", required=True, help="private work dir (sqlite, plan, tmp)")
        p.add_argument("--manifest", default=None, help="manifest.tsv (default: <input>/.manifest.tsv "
                       "or $DEID_MANIFEST)")
        p.add_argument("--only-prefix", default="")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--skip-missing", action="store_true", help="ignore manifest rows not on disk")
        p.add_argument("--jobs", type=int, default=6)
        p.add_argument("--mem-gb", type=int, default=4, help="address-space limit per worker")
        p.add_argument("--force", action="store_true", help="ignore caches")
        p.add_argument("--plan", default=None, help="plan dir (default WORK/plan)")
        if out:
            p.add_argument("--out", required=True, help="attempt/stage dir; files go to OUT/corpus/")

    common(sub.add_parser("scan"))
    common(sub.add_parser("plan"))
    common(sub.add_parser("rewrite"), out=True)
    v = sub.add_parser("validate")
    common(v, out=True)
    v.add_argument("--leak-scan", default="fast", choices=["off", "fast", "full"])
    v.add_argument("--no-reverify", action="store_true")
    v.add_argument("--prev-work", default=None, help="reuse leak-scan findings of unchanged outputs from this work dir")
    pk = sub.add_parser("package")
    common(pk, out=True)
    pk.add_argument("--stage", required=True, help="stage dir with corpus/ produced by rewrite")
    pk.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    o = sub.add_parser("one")
    o.add_argument("files", nargs="+")
    o.add_argument("--adapter", default=None)
    o.add_argument("--logical", default=None, help="logical path to pretend the file has")
    o.add_argument("--work", default=None)
    o.add_argument("--plan", default=None)
    o.add_argument("--show", type=int, default=20)

    args = ap.parse_args(argv)
    if hasattr(args, "input"):
        args.input = os.path.abspath(args.input)
        args.work = os.path.abspath(args.work)
        if args.manifest is None:
            args.manifest = os.environ.get("DEID_MANIFEST") or os.path.join(args.input, ".manifest.tsv")
    if hasattr(args, "out") and args.out:
        args.out = os.path.abspath(args.out)

    from deid.pipeline import run
    if args.cmd == "scan":
        return run.cmd_scan(args)
    if args.cmd == "plan":
        return run.cmd_plan(args)
    if args.cmd == "rewrite":
        return run.cmd_rewrite(args)
    if args.cmd == "validate":
        from deid.validate import gate
        return gate.cmd_validate(args)
    if args.cmd == "package":
        from deid.pipeline import package
        return package.cmd_package(args)
    if args.cmd == "one":
        return run.cmd_one(args)


if __name__ == "__main__":
    sys.exit(main())
