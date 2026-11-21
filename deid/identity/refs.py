"""Cross-file reference resolution and rewriting.

``resolve`` finds the ORIGINAL inventory path a raw reference points to
(relative/absolute, Windows/Unix/URI spellings, case-insensitive, Creo
version suffixes, unique-basename fallback inside the same source).
``map_reference`` rewrites the raw spelling so it points at the mapped
target, keeping the reference's style (separators, prefix, case, relative
vs absolute).  Dangling references are still pseudonymised with the same
dictionary, so identifying components never survive.
"""
from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote

from .plan import split_ext

SPLIT_RE = re.compile(r"([\\/]+)")
SCHEME_RE = re.compile(r"^(file:(?://)?/?|[a-z][a-z0-9+.-]{1,10}://)", re.I)
DRIVE_RE = re.compile(r"^[A-Za-z]:$")
CREO_RE = re.compile(r"\.(prt|asm|drw|frm|lay|sec|mfg)(\.\d+)?$", re.I)


def _ctx_dir(ctx_path: str):
    """(container prefix incl. '!/', directory inside it)."""
    if "!/" in ctx_path:
        cont, mem = ctx_path.rsplit("!/", 1)
        return cont + "!/", posixpath.dirname(mem)
    return "", posixpath.dirname(ctx_path)


def _namespace(path: str) -> str:
    if "!/" in path:
        return path.split("!/", 1)[0]
    parts = path.split("/")
    if parts[0] == "sources" and len(parts) > 3:
        return "/".join(parts[:3])
    return "/".join(parts[:2])


def _clean(raw: str) -> str:
    r = raw.strip().strip("\"'")
    r = SCHEME_RE.sub("", r) if r.lower().startswith("file:") else r
    if "%" in r:
        try:
            r = unquote(r)
        except Exception:
            pass
    return r.replace("\\", "/")


def resolve(plan, ctx_path: str, raw_ref: str):
    if not raw_ref or not raw_ref.strip():
        return None
    if re.match(r"^[a-z][a-z0-9+.-]{1,10}://", raw_ref.strip(), re.I) and not raw_ref.lower().startswith("file:"):
        return None  # web URL
    full, base = plan._index()
    r = _clean(raw_ref)
    prefix, cdir = _ctx_dir(ctx_path)
    is_abs = r.startswith("/") or bool(re.match(r"^[A-Za-z]:", r))
    cands = []
    if not is_abs:
        joined = posixpath.normpath(posixpath.join(cdir, r)) if cdir else posixpath.normpath(r)
        if not joined.startswith(".."):
            cands.append(prefix + joined)
    for c in cands:
        hit = full.get(c.lower())
        if hit:
            return hit
        m = CREO_RE.search(c)
        if m and not m.group(2):  # unversioned Creo name -> highest existing version
            best = None
            for v in range(1, 1000):
                h = full.get(f"{c}.{v}".lower())
                if h:
                    best = h
                elif best is not None and v > 60:
                    break
            if best:
                return best
    # basename fallback within the same namespace
    bn = r.rstrip("/").rsplit("/", 1)[-1].lower()
    if not bn:
        return None
    ns = _namespace(ctx_path)
    lst = [p for p in base.get(bn, ()) if _namespace(p) == ns]
    if not lst and CREO_RE.search(bn) and not CREO_RE.search(bn).group(2):
        lst = [p for k in (f"{bn}.{v}" for v in range(1, 60)) for p in base.get(k, ()) if _namespace(p) == ns]
        if lst:
            lst = [max(lst, key=lambda p: int(p.rsplit(".", 1)[-1]))]
    if len(lst) == 1:
        return lst[0]
    if len(lst) > 1:
        # prefer the candidate sharing the most trailing directory names with raw
        rc = [x.lower() for x in r.split("/") if x]

        def score(p):
            pc = [x.lower() for x in p.replace("!/", "/").split("/")]
            s = 0
            for a, b in zip(reversed(rc), reversed(pc)):
                if a != b:
                    break
                s += 1
            # tie-break: same directory as the referencing file
            same = posixpath.dirname(p) == posixpath.dirname(ctx_path)
            return (s, same)
        lst.sort(key=score, reverse=True)
        if score(lst[0]) > score(lst[1]):
            return lst[0]
    return None


def _transfer_case(src: str, mapped: str) -> str:
    if len(src) != len(mapped):
        return mapped
    out = []
    for s, m in zip(src, mapped):
        if s.isalpha() and m.isalpha():
            out.append(m.upper() if s.isupper() else m.lower())
        else:
            out.append(m)
    return "".join(out)


def map_reference(plan, ctx_path: str, raw_ref: str) -> str:
    if not raw_ref:
        return raw_ref
    target = None
    try:
        target = resolve(plan, ctx_path, raw_ref)
    except Exception:
        target = None
    # keep leading scheme verbatim
    m = SCHEME_RE.match(raw_ref)
    head = raw_ref[: m.end()] if m else ""
    body = raw_ref[len(head):]
    if head and not head.lower().startswith("file:"):
        return plan.replace_text(raw_ref)  # web URL: dictionary only
    pieces = SPLIT_RE.split(body)  # comps at even idx, seps at odd idx
    comps_idx = [i for i in range(0, len(pieces), 2)]
    new = list(pieces)
    aligned = set()
    if target:
        mapped = plan.map_path(target)
        o = target.replace("!/", "/").split("/")
        mp = mapped.replace("!/", "/").split("/")
        if len(o) == len(mp):
            j = len(o) - 1
            for i in reversed(comps_idx):
                rc = pieces[i]
                if j < 0 or not rc:
                    break
                dec = unquote(rc) if "%" in rc else rc
                oc, mc = o[j], mp[j]
                if dec.lower() == oc.lower():
                    if "%" in rc and dec != rc:
                        new[i] = plan.replace_text(rc)
                    else:
                        new[i] = _transfer_case(rc, mc)
                elif CREO_RE.search(oc) and dec.lower() == re.sub(r"\.\d+$", "", oc).lower():
                    mc2 = re.sub(r"\.\d+$", "", mc)
                    new[i] = _transfer_case(rc, mc2)
                else:
                    break
                aligned.add(i)
                j -= 1
    # everything not aligned with the target: dictionary + user-dir rules
    rest = []
    for i in comps_idx:
        if i in aligned or not pieces[i]:
            continue
        is_last = i == comps_idx[-1]
        if is_last:
            stem, suf = split_ext(pieces[i])
            new[i] = plan.replace_text(stem) + suf
        else:
            new[i] = plan.replace_text(pieces[i])
    out = head + "".join(new)
    # user directories need context across components (…\Users\<name>\…)
    out2 = plan.replace_text(out) if not aligned else _userdirs_only(plan, out)
    return out2


def _userdirs_only(plan, s: str) -> str:
    from .plan import USERDIR_RE
    return USERDIR_RE.sub(plan._userdir_sub, s)
