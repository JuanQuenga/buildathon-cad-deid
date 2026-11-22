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
import re

from deid.identity.aliases import TOKEN_RE, norm_token


_SHORT_RE = re.compile(r"(?<![\w~])[A-Za-z0-9_'\-]{1,6}~\d(?![\w~])")
_HDR_RE = re.compile(r"(?m)((?:USER|SITE|AUTHOR|OWNER)=)([^;\n\r]{2,40})(?=;|$)")
_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


_KINDS = {"person", "company", "account", "project", "project_token", "account_token", "email_local"}
_GLOBAL: dict = {}


class NamePlan:
    def __init__(self, plan, names: bool = False, sweep: bool = True):
        self._p = plan
        self._names = names
        self._do_sweep = sweep
        self._g = None

    def __getattr__(self, name):
        return getattr(self._p, name)

    def _global(self):
        """Distinctive plan identity tokens (as the PDF adapter's global sweep), software / hardware product
        names and placeholders excluded; computed here so PDF outputs do not depend on this module."""
        if self._g is None:
            g = _GLOBAL.get(id(self._p))
            if g is None:
                g = {}
                try:
                    from deid.formats.pdf import _fold, _spread, _E5_ENG_WORDS
                    from deid.identity.vocab import is_word, SOFTWARE
                    sp = _spread()
                    sw = {t for k in SOFTWARE for t in k.split()} - {"ulti\u006daker"}
                    for e in getattr(self._p, "entries", {}).values():
                        if getattr(e, "kind", "") not in _KINDS:
                            continue
                        for t in TOKEN_RE.findall(e.value):
                            f = _fold(t)
                            if (len(f) < 4 or not f.isalpha() or is_word(f) or f in _E5_ENG_WORDS
                                    or sp.get(f, 0) > 6 or f in sw or re.fullmatch(r"x+|y{2,4}", f)):
                                continue
                            g.setdefault(f, f)
                except Exception:
                    g = {}
                _GLOBAL.clear()
                _GLOBAL[id(self._p)] = g
            self._g = g
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

    def map_reference(self, ctx, value):
        """References keep the plan's mapping; an absolute drive/UNC path (a location on the author's
        machine, never a corpus file) also gets the name sweep (M:\\daten\\Huber\\... ). """
        m = self._p.map_reference(ctx, value)
        if (self._do_sweep and os.environ.get("DEID_CAD_NAMES", "1") == "1" and isinstance(value, str)
                and _ABS_RE.match(value)):
            base = m if isinstance(m, str) else value
            cut = max(base.rfind("\\"), base.rfind("/")) + 1    # the file name itself is never touched
            return self._sweep(base[:cut]) + base[cut:]
        return m

    def _short_names(self, text: str) -> str:
        """Windows 8.3 short names of accounts / user folders (ADMINI~1, OBERLA~1) that the plan holds as
        entries: the plan's own alias of that entry (the flexible matcher does not take '~')."""
        if "~" not in text:
            return text
        ents = getattr(self._p, "entries", None)
        if not ents:
            return text

        def sub(m):
            v = m.group(0)
            e = ents.get("".join(norm_token(t) for t in TOKEN_RE.findall(v)))
            if e is None or getattr(e, "kind", "") not in ("account", "person", "account_token", "email_local"):
                return v
            try:
                a = self._p.alias(v, e.kind)
            except Exception:
                return v
            return a if a and len(a) == len(v) else v
        return _SHORT_RE.sub(sub, text)

    def _entry_alias(self, v: str):
        ents = getattr(self._p, "entries", None)
        if not ents or not v.strip():
            return None
        e = ents.get("".join(norm_token(t) for t in TOKEN_RE.findall(v)))
        if e is None or getattr(e, "kind", "") not in ("account", "person", "email_local"):
            return None
        from deid.identity.vocab import english_words, ENGINEERING, SOFTWARE, PLACEHOLDERS
        from deid.identity.aliases import norm_key
        k = norm_key(v)
        if (k in english_words() or k in ENGINEERING or k in SOFTWARE or k in PLACEHOLDERS
                or not any(ch.isalpha() for ch in v) or len(k.replace(" ", "")) < 4
                or re.fullmatch(r"[Xx#\s]+", v)
                or all(re.fullmatch(r"[Xx#]+|[Yy]{2,4}|[Mm]{1,4}|[Dd]{1,4}|[Hh]{1,2}|[Ss]{1,2}", t) for t in TOKEN_RE.findall(v))):
            return None
        try:
            from deid.formats.pdf import _TB_GENERIC, _label_kind, _E5_ENG_WORDS
            if k in _TB_GENERIC or k in _E5_ENG_WORDS or _label_kind(v):
                return None
        except Exception:
            pass
        try:
            a = self._p.alias(v, e.kind)
        except Exception:
            return None
        return a if a and a != v and len(a) == len(v) else None

    def _exact_fields(self, text: str) -> str:
        """Values that *are* a known identity, field-only plan entries included (saving accounts such as
        'Andres'): Parasolid transmit headers 'USER=Andres;' / 'SITE=...;' and standalone strings."""
        def hdr(m):
            a = self._entry_alias(m.group(2))
            return m.group(1) + a if a else m.group(0)
        r = _HDR_RE.sub(hdr, text)
        st = r.strip()
        if st and st == r and 2 <= len(st) <= 40:
            a = self._entry_alias(st)
            if a:
                r = a
        return r

    def replace_text(self, text):
        r = self._p.replace_text(text)
        if not r or os.environ.get("DEID_CAD_NAMES", "1") != "1":
            return r
        r = self._short_names(r)
        r = self._exact_fields(r)
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
        return self._sweep(r) if self._do_sweep else r


def wrap(plan, names: bool = False, sweep: bool = True):
    """sweep=False (native binary streams): only the exact rules (8.3 short names of known accounts,
    USER=/SITE= values, standalone strings equal to a known person / account), never word tokens."""
    if plan is None or not hasattr(plan, "replace_text") or isinstance(plan, NamePlan):
        return plan
    return NamePlan(plan, names, sweep)
