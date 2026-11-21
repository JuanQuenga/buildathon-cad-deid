"""Identity discovery, pseudonym plan, path map and reference resolution.

Public entry points (see docs/INTERFACE.md and docs/identity.md):
    build_plan(scan_db, manifest, plan_dir)  -> writes plan_dir/plan.json
    load_plan(plan_dir)                      -> IdentityPlan (deid.api.Plan)
"""
from .plan import IdentityPlan, Entry, load_plan, save_plan  # noqa: F401


def build_plan(scan_db, manifest, plan_dir, extra_tables=(), secret=None):
    from .build import build_plan as _bp
    return _bp(scan_db, manifest, plan_dir, extra_tables, secret)
