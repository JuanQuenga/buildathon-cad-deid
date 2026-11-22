"""Name pass shared by the neutral CAD text adapters (DXF, IGES, STEP).

Drawings (PDF) and their models must spell the same person / company the same way. The PDF adapter
replaces the plan's distinctive identity tokens wherever they stand (other word order, accents, field-only
entries) and, on drawings, unlabelled 'Given Surname' names; this wrapper applies the very same rules to
the free-text values of CAD files, with the plan's deterministic token aliases, so a name removed from a
drawing is removed from its model with the identical pseudonym.

    plan = cadnames.wrap(plan, names=True)   # names=True: also the unlabelled-name rule (DXF text)
"""
from __future__ import annotations

import os

from deid.identity.aliases import TOKEN_RE, norm_token


class NamePlan:
    def __init__(self, plan, names: bool = False):
        self._p = plan
        self._names = names
        self._g = None

    def __getattr__(self, name):
        return getattr(self._p, name)

    def _global(self):
        if self._g is None:
            try:
                from deid.formats.pdf import _global_sweep_tokens
                self._g = _global_sweep_tokens(self._p)
            except Exception:
                self._g = {}
        return self._g

    def _sweep(self, text: str) -> str:
        from deid.formats.pdf import _fold
        g = self._global()
        if not g:
            return text
        base = self._p
        aliases = getattr(base, "_alias_set", ())
        real = getattr(base, "_real_tokens", ())

        def sub(m):
            t = m.group(0)
            if _fold(t) not in g:
                return t
            k = norm_token(t)
            if k in aliases and k not in real:
                return t
            a = base._token_alias(k)
            if len(a) != len(t):
                return t
            return "".join(c.upper() if o.isupper() else c for o, c in zip(t, a))
        return TOKEN_RE.sub(sub, text)

    def replace_text(self, text):
        r = self._p.replace_text(text)
        if not r or os.environ.get("DEID_CAD_NAMES", "1") != "1":
            return r
        if self._names and os.environ.get("DEID_PDF_E14", "1") == "1":
            try:
                from deid.formats.pdf import e14_candidates, _e5_render, _e5_value_ok
                for v in sorted(set(e14_candidates(r)), key=len, reverse=True):
                    if not _e5_value_ok(v):
                        continue
                    a = _e5_render(self._p, v)
                    if a:
                        r = r.replace(v, a)
            except Exception:
                pass
        return self._sweep(r)


def wrap(plan, names: bool = False):
    if plan is None or not hasattr(plan, "replace_text") or isinstance(plan, NamePlan):
        return plan
    return NamePlan(plan, names)
