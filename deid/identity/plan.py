"""The identity plan consumed by every adapter (``deid.api.Plan`` protocol).

A plan is a list of identity *entries* (original value, kind, match mode)
plus a secret key.  Everything else (token aliases, regexes, path map) is
derived deterministically, so every worker that loads the same plan
directory produces byte-identical output.

Plan directory layout (PRIVATE, lives under cad-deid/work/, never in git):
    plan.json        entries + metadata + token alias registry
    secret.key       32 random bytes (or env DEID_SECRET_FILE)
    inventory.txt    original logical paths (manifest + archive members)
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .aliases import TOKEN_RE, alias_token, apply_case, norm_key, norm_token
from .matcher import Matcher
from .vocab import HOST_GENERIC, ORG_DESIGNATORS, PLACEHOLDERS, SOFTWARE, is_generic_token

PLAN_FORMAT = 1

# Kinds whose values are aliased by alias() even when not pre-registered.
# part_number / part_name / document_number are NOT generated: a generic part
# name ("Bracket") identifies nobody, and generating would desynchronise the
# name from the (dictionary-mapped) file name.  They go through the dictionary.
IDENTITY_KINDS = {
    "person", "company", "organization", "organisation", "account", "username",
    "address", "phone", "email", "domain", "url",
}

# Match modes
FLEX = "flex"     # free text, case-insensitive, flexible separators
EXACT = "exact"   # free text, case-insensitive, exact separators
FIELD = "field"   # only via alias()/structural slots (too short/generic for free text)

_TLDS = ("com|org|net|edu|gov|mil|int|info|biz|io|co|us|uk|de|fr|it|es|nl|be|ch|at|se|no|dk|fi|pl|cz|sk|hu|"
         "ro|pt|gr|ie|ru|ua|cn|jp|kr|tw|hk|sg|in|au|nz|ca|mx|br|ar|cl|za|il|tr|ir|eu|me|tv|cc|ws|"
         "ai|app|dev|tech|xyz|online|site|store|cloud|email|mail|local|lan|corp|ac|si|hr|rs|lt|lv|ee|"
         "is|lu|my|th|vn|id|ph|pk|bd|eg|sa|ae|qa|ng|ke|ma|tn|co\\.uk|ac\\.uk|edu\\.au|com\\.au")
EMAIL_RE = re.compile(
    r"(?<![\w.%+-])([A-Za-z0-9][A-Za-z0-9._%+-]{1,63})@((?:[A-Za-z0-9-]{2,63}\.)+(?:" + _TLDS + "|"
    + _TLDS.upper() + r"))(?![\w-])")
# user directory in Windows / Unix paths: group(1)=prefix incl separator, group(2)=user
USERDIR_RE = re.compile(
    r"(?i)(?:^|[\\/]{1,2})(?:users|documents and settings|home|export/home|utilisateurs|benutzer|usuarios|"
    r"utenti|gebruikers|anv\u00e4ndare|u\u017cytkownicy|uzivatele|\u7528\u6237)[\\/]{1,2}"
    r"([^\\/\x00-\x1f\"'<>|:*?]{1,64}?)(?=[\\/])")
_USERDIR_HINT = re.compile(r"(?:users|settings|home|isateurs|benutzer|usuarios|utenti|gebruikers|ndare|kownicy|ivatele|\u7528\u6237)[\\/]")  # on lower()
HOSTLIKE_RE = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,12}")
SHA_PIECE_RE = re.compile(r"^[0-9a-f]{12}$")
VERSION_SUFFIX_RE = re.compile(r"\.\d+$")


def _hash(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


@dataclass
class Entry:
    value: str
    kind: str
    mode: str = FLEX
    keep: tuple = ()          # token keys rendered verbatim (TLDs, 'www', ...)
    count: int = 0            # evidence: number of files/observations
    sources: int = 0          # evidence: number of distinct sources
    origin: str = ""          # 'path.github.owner', 'step.FILE_NAME.author', 'curated', ...

    @property
    def key(self):
        return norm_key(self.value)

    @property
    def tokens(self):
        return [norm_token(t) for t in TOKEN_RE.findall(self.value)]

    @property
    def compact(self):
        return "".join(self.tokens)


def split_ext(name: str):
    """Split a file name into (stem, protected_suffix).  Keeps the last
    extension, Creo version numbers ('.prt.19'), '.tar.gz' style compounds."""
    if name.startswith(".") and name.count(".") == 1:
        return name, ""
    stem = name
    suf = ""
    m = VERSION_SUFFIX_RE.search(stem)
    if m and "." in stem[: m.start()]:
        suf = m.group(0) + suf
        stem = stem[: m.start()]
    i = stem.rfind(".")
    if i > 0 and len(stem) - i <= 16:
        suf = stem[i:] + suf
        stem = stem[:i]
        if suf.lower().startswith((".gz", ".bz2", ".xz", ".z")) and stem.lower().endswith(".tar"):
            suf = stem[-4:] + suf
            stem = stem[:-4]
    return stem, suf


class IdentityPlan:
    """Implements deid.api.Plan."""

    def __init__(self, entries: Iterable[Entry], secret: bytes, inventory: Iterable[str] = (),
                 token_aliases: Optional[dict] = None, meta: Optional[dict] = None):
        self.secret = secret
        self.meta = meta or {}
        self.entries = {}
        for e in entries:
            k = e.compact
            if not k:
                continue
            old = self.entries.get(k)
            if old is None:
                self.entries[k] = e
            else:  # merge: strongest mode wins, keep-set = intersection
                rank = {FIELD: 0, EXACT: 1, FLEX: 2}
                if rank[e.mode] > rank[old.mode]:
                    old.mode = e.mode
                old.keep = tuple(sorted(set(old.keep) & set(e.keep)))
                old.count += e.count
                old.sources = max(old.sources, e.sources)
        # A domain whose registrable part is also a project/account name ('step.parts'
        # as repo name and as web site) renders that part like the name: no kept TLD.
        for e in list(self.entries.values()):
            if e.kind != "domain" or not e.keep:
                continue
            labels = e.value.lower().split(".")
            for k in (2, 3):
                if len(labels) >= k:
                    reg = labels[-k:]
                    other = self.entries.get("".join(norm_token(t) for t in TOKEN_RE.findall(".".join(reg))))
                    if other is not None and other.kind != "domain":
                        regtoks = {norm_token(t) for t in TOKEN_RE.findall(".".join(reg))}
                        e.keep = tuple(x for x in e.keep if x not in regtoks)
        self._tok = dict(token_aliases or {})
        self._tok_lock = threading.Lock()
        self._used = set(self._tok.values())
        self._real_tokens = set()
        for e in self.entries.values():
            self._real_tokens.update(e.tokens)
        if token_aliases is None:
            self._build_token_registry()
        self.inventory = list(inventory)
        self._inv_index = None
        self._matcher = None
        self._map_cache = {}
        self.version = self.meta.get("version") or _hash(
            "plan", PLAN_FORMAT, hashlib.sha256(secret).hexdigest(),
            *sorted(f"{k}|{e.mode}|{','.join(e.keep)}" for k, e in self.entries.items()))

    # ------------------------------------------------------------------ tokens
    def _build_token_registry(self):
        toks = sorted(self._real_tokens)
        for t in toks:
            self._token_alias(t)

    def _token_alias(self, tok_key: str) -> str:
        """Lower-case alias for a normalised token (injective, deterministic
        given the plan's token set)."""
        a = self._tok.get(tok_key)
        if a is not None:
            return a
        with self._tok_lock:
            a = self._tok.get(tok_key)
            if a is not None:
                return a
            if len(tok_key) <= 1 or tok_key.isdigit() and len(tok_key) <= 1:
                a = tok_key
            else:
                a = None
                for attempt in range(64):
                    cand = alias_token(tok_key, self.secret, attempt).lower()
                    if cand == tok_key or cand in self._used or cand in self._real_tokens:
                        continue
                    a = cand
                    break
                if a is None:  # exhausted tiny space (2-char tokens): accept collision
                    a = alias_token(tok_key, self.secret, 0).lower()
            self._tok[tok_key] = a
            self._used.add(a)
            return a

    def _render_tokens(self, text: str, keep=()) -> str:
        """Alias every token of text (case/separators preserved)."""
        def sub(m):
            t = m.group(0)
            k = norm_token(t)
            if k in keep or len(k) <= 1 or k in ORG_DESIGNATORS:
                return t
            a = self._token_alias(k)
            if len(a) != len(t):
                return t
            return apply_case(t, a)
        return TOKEN_RE.sub(sub, text)

    def _render_entry(self, entry: Entry, span: str) -> str:
        """Render matched text (which spells entry with arbitrary case and
        separators, possibly with tokens glued together)."""
        alias_chars = []
        for t in entry.tokens:
            if t in entry.keep or len(t) <= 1 or (t in ORG_DESIGNATORS and len(entry.tokens) > 1):
                alias_chars.append(t)
            else:
                alias_chars.append(self._token_alias(t))
        ref = "".join(entry.tokens)
        al = "".join(alias_chars)
        if len(ref) != len(al):
            return self._render_tokens(span, entry.keep)
        out = []
        j = 0
        for ch in span:
            if j < len(ref) and ch.isalnum() and ch.lower() == ref[j]:
                a = al[j]
                out.append(a.upper() if ch.isupper() else a)
                j += 1
            else:
                out.append(ch)
        if j != len(ref):  # alignment failed: fall back to token rendering
            return self._render_tokens(span, entry.keep)
        return "".join(out)

    # ------------------------------------------------------------------ matcher
    @property
    def matcher(self) -> Matcher:
        if self._matcher is None:
            items = []
            for e in self.entries.values():
                if e.mode == FIELD:
                    continue
                toks = e.tokens
                seps = re.split(r"[^\W_]+", e.value)[1:-1]
                items.append((toks, [s.lower() for s in seps], e.mode == FLEX))
            self._matcher = Matcher(items)
        return self._matcher

    def _lookup_span(self, span: str) -> Optional[Entry]:
        return self.entries.get("".join(norm_token(t) for t in TOKEN_RE.findall(span)))

    # ------------------------------------------------------------------ API
    def alias(self, value: str, kind: Optional[str] = None) -> Optional[str]:
        if value is None:
            return None
        v = value
        stripped = v.strip()
        if not stripped:
            return None
        key = norm_key(stripped)
        if not key or key in PLACEHOLDERS or key in SOFTWARE:
            return None
        k = (kind or "").lower()
        if k == "email" or (not k and EMAIL_RE.fullmatch(stripped)):
            return EMAIL_RE.sub(self._email_sub, v)
        if k == "domain":
            return self._render_domain(v)
        if k == "url":
            r = self.replace_text(v)
            return r if r != v else None
        e = self.entries.get("".join(key.split()))
        if e is not None:
            return self._render_entry(e, v)
        if k in IDENTITY_KINDS:
            if not any(ch.isalpha() for ch in key) and k != "phone":
                return None
            return self._render_tokens(v)
        r = self.replace_text(v)
        return r if r != v else None

    def replace_text(self, text: str) -> str:
        if not text:
            return text
        lw = text.lower()
        spans = []  # (start, end, replacement)
        for s, e in self.matcher.finditer(text, lw):
            span = text[s:e]
            ent = self._lookup_span(span)
            if ent is not None:
                spans.append((s, e, self._render_entry(ent, span)))
        extra = []
        if "@" in text:
            for m in EMAIL_RE.finditer(text):
                extra.append((m.start(), m.end(), self._email_sub(m)))
        if _USERDIR_HINT.search(lw):
            for m in USERDIR_RE.finditer(text):
                r = self._userdir_sub(m)
                pre = m.start(1) - m.start(0)
                extra.append((m.start(1), m.end(1), r[pre:]))
        if extra:
            # emails / user dirs win over dictionary hits they contain; a user dir
            # already covered by a dictionary hit keeps the dictionary alias
            out = []
            for s, e, r in extra:
                inside = [x for x in spans if x[0] >= s and x[1] <= e]
                covered = any(x[0] <= s and x[1] >= e for x in spans)
                if covered:
                    continue
                if inside or not any(x[0] < e and s < x[1] for x in spans):
                    spans = [x for x in spans if not (x[0] >= s and x[1] <= e)]
                    out.append((s, e, r))
            spans.extend(out)
            spans.sort()
        if not spans:
            return text
        res = []
        last = 0
        for s, e, r in spans:
            if s < last:
                continue
            res.append(text[last:s])
            res.append(r)
            last = e
        res.append(text[last:])
        return "".join(res)

    def replace_bytes(self, data: bytes, encodings=("utf-8",), length_preserving: bool = False) -> bytes:
        """Length is always preserved: every alias has the original's
        character classes and encoded length."""
        for enc in encodings:
            e = enc.lower().replace("_", "-")
            if e in ("utf-8", "utf8", "ascii"):
                t = data.decode("utf-8", "surrogateescape")
                r = self.replace_text(t)
                if r != t:
                    nb = r.encode("utf-8", "surrogateescape")
                    if len(nb) == len(data) or not length_preserving:
                        data = nb
            elif e in ("latin-1", "latin1", "iso-8859-1", "cp1252", "windows-1252"):
                t = data.decode("latin-1")
                r = self.replace_text(t)
                if r != t:
                    try:
                        data = r.encode("latin-1")
                    except UnicodeEncodeError:
                        pass
            elif e in ("utf-16-le", "utf-16le", "utf16le", "utf-16"):
                for off in (0, 1):
                    end = off + ((len(data) - off) // 2) * 2
                    chunk = data[off:end]
                    t = chunk.decode("utf-16-le", "surrogatepass")
                    r = self.replace_text(t)
                    if r != t:
                        nb = r.encode("utf-16-le", "surrogatepass")
                        if len(nb) == len(chunk):
                            data = data[:off] + nb + data[end:]
            elif e in ("utf-16-be", "utf-16be"):
                for off in (0, 1):
                    end = off + ((len(data) - off) // 2) * 2
                    chunk = data[off:end]
                    t = chunk.decode("utf-16-be", "surrogatepass")
                    r = self.replace_text(t)
                    if r != t:
                        nb = r.encode("utf-16-be", "surrogatepass")
                        if len(nb) == len(chunk):
                            data = data[:off] + nb + data[end:]
            else:
                try:
                    t = data.decode(enc)
                    r = self.replace_text(t)
                    if r != t:
                        nb = r.encode(enc)
                        if len(nb) == len(data) or not length_preserving:
                            data = nb
                except (UnicodeError, LookupError):
                    pass
        return data

    # ------------------------------------------------------------------ helpers
    def _email_sub(self, m):
        local, dom = m.group(1), m.group(2)
        if norm_key(local) in GENERIC_MAILBOXES:
            if self._domain_protected(dom):
                return m.group(0)
            return local + "@" + self._render_domain(dom)
        ent = self.entries.get("".join(norm_token(t) for t in TOKEN_RE.findall(local)))
        la = self._render_entry(ent, local) if ent else self._render_tokens(local)
        return la + "@" + self._render_domain(dom)

    def _userdir_sub(self, m):
        user = m.group(1)
        key = norm_key(user)
        if not key or key in PLACEHOLDERS:
            return m.group(0)
        ent = self.entries.get("".join(key.split()))
        ua = self._render_entry(ent, user) if ent else self._render_tokens(user)
        return m.group(0)[: m.start(1) - m.start(0)] + ua

    def _domain_protected(self, dom: str) -> bool:
        d = dom.lower().rstrip(".")
        for p in PROTECTED_DOMAINS:
            if d == p or d.endswith("." + p):
                return True
        return False

    def _render_domain(self, dom: str) -> str:
        if self._domain_protected(dom):
            return dom
        labels = dom.split(".")
        out = []
        n = len(labels)
        low = dom.lower()
        plat = 0  # trailing labels of a hosting platform suffix (kept)
        for suf in HOSTING_SUFFIXES:
            if low.endswith("." + suf):
                plat = suf.count(".") + 1
        for i, lab in enumerate(labels):
            lk = lab.lower()
            if i == n - 1 or lk in HOST_GENERIC or i >= n - plat:
                out.append(lab)
            else:
                ent = self.entries.get("".join(norm_token(t) for t in TOKEN_RE.findall(lab)))
                out.append(self._render_entry(ent, lab) if ent else self._render_tokens(lab))
        return ".".join(out)

    # ------------------------------------------------------------------ paths
    def _map_component(self, comp: str, is_file: bool) -> str:
        if is_file:
            stem, suf = split_ext(comp)
            return self.replace_text(stem) + suf
        return self.replace_text(comp)

    def _map_slot(self, bucket: str, comp: str, is_file: bool) -> str:
        """Structural identity slot: sources/<bucket>/<SLOT>/... or archives/<SLOT>."""
        stem, suf = split_ext(comp) if is_file else (comp, "")
        if bucket in ("github", "archives", "gitlab"):
            pieces = stem.split("__")
            out = []
            for i, p in enumerate(pieces):
                if not p:
                    out.append(p)
                elif i == len(pieces) - 1 and SHA_PIECE_RE.match(p) and len(pieces) > 1:
                    out.append(p)
                elif bucket == "gitlab" and i == 0 and "." in p:
                    out.append(p)  # host
                elif norm_key(p) in PLACEHOLDERS:
                    out.append(p)
                else:
                    ent = self.entries.get("".join(norm_token(t) for t in TOKEN_RE.findall(p)))
                    out.append(self._render_entry(ent, p) if ent else self._render_tokens(p))
            return "__".join(out) + suf
        if bucket == "web" or (not is_file and HOSTLIKE_RE.fullmatch(stem)):
            return self._render_domain(stem) + suf
        # catalog and others: dictionary only
        return self.replace_text(stem) + suf

    def map_top(self, path: str) -> str:
        r = self._map_cache.get(path)
        if r is not None:
            return r
        parts = path.split("/")
        out = []
        n = len(parts)
        for i, c in enumerate(parts):
            is_file = i == n - 1
            if not c:
                out.append(c)
                continue
            if parts[0] == "sources" and i == 2 and n > 3:
                out.append(self._map_slot(parts[1], c, is_file))
            elif parts[0] == "sources" and i <= 1:
                out.append(c)
            elif parts[0] == "archives" and i == 0:
                out.append(c)
            elif parts[0] == "archives" and i == 1:
                out.append(self._map_slot("archives", c, is_file))
            else:
                out.append(self._map_component(c, is_file))
        r = "/".join(out)
        if len(self._map_cache) < 2_000_000:
            self._map_cache[path] = r
        return r

    def map_member(self, member: str) -> str:
        parts = member.split("/")
        n = len(parts)
        return "/".join(self._map_component(c, i == n - 1) if c else c for i, c in enumerate(parts))

    def map_path(self, logical_path: str) -> str:
        segs = logical_path.split("!/")
        out = [self.map_top(segs[0])]
        for s in segs[1:]:
            out.append(self.map_member(s))
        return "!/".join(out)

    # ------------------------------------------------------------------ references
    def _index(self):
        if self._inv_index is None:
            full = {}
            base = {}
            for p in self.inventory:
                full.setdefault(p.lower(), p)
                b = p.rsplit("/", 1)[-1].lower()
                base.setdefault(b, []).append(p)
            self._inv_index = (full, base)
        return self._inv_index

    def resolve_reference(self, ctx_path: str, raw_ref: str) -> Optional[str]:
        """Original logical path the reference points to, or None."""
        from .refs import resolve
        return resolve(self, ctx_path, raw_ref)

    def map_reference(self, ctx, raw_ref: str) -> str:
        from .refs import map_reference
        ctx_path = getattr(ctx, "logical_path", ctx) if ctx is not None else ""
        return map_reference(self, ctx_path, raw_ref)

    # ------------------------------------------------------------------ persistence
    def to_json(self) -> dict:
        return {
            "format": PLAN_FORMAT,
            "version": self.version,
            "meta": self.meta,
            "entries": [
                {"value": e.value, "kind": e.kind, "mode": e.mode, "keep": list(e.keep),
                 "count": e.count, "sources": e.sources, "origin": e.origin}
                for _, e in sorted(self.entries.items())
            ],
            "token_aliases": dict(sorted(self._tok.items())),
        }


HOSTING_SUFFIXES = ("wordpress.com", "blogspot.com", "github.io", "gitlab.io", "netlify.app", "herokuapp.com",
                    "s3.amazonaws.com", "cloudfront.net", "sourceforge.net", "sourceforge.io", "googleusercontent.com",
                    "azurewebsites.net", "pages.dev", "vercel.app", "readthedocs.io", "wixsite.com", "weebly.com",
                    "squarespace.com", "tumblr.com", "medium.com", "substack.com", "co.uk", "ac.uk", "org.uk",
                    "com.au", "edu.au", "co.jp", "ac.jp", "com.cn", "edu.cn", "edu.tr", "ac.in", "co.in", "ac.at",
                    "ac.be", "ac.il", "co.il", "com.br", "com.tw", "edu.tw", "co.nz", "ac.nz", "co.kr", "ac.kr")

GENERIC_MAILBOXES = {"info", "noreply", "no reply", "support", "sales", "contact", "admin", "webmaster",
                     "postmaster", "office", "mail", "service", "help", "hello", "team"}

# Domains that are format-required namespaces or standards bodies.
PROTECTED_DOMAINS = (
    "w3.org", "adobe.com", "purl.org", "iec.ch", "iso.org", "microsoft.com", "openxmlformats.org",
    "oasis-open.org", "xml.org", "xmlsoap.org", "apple.com", "omg.org", "steptools.com",
    "example.com", "example.org", "example.net", "schemas.android.com", "freecadweb.org", "freecad.org",
    "opencascade.com", "opencascade.org", "3mf.io", "schemas.3mf.io", "ietf.org", "ecma-international.org",
    "khronos.org", "github.com", "gitlab.com", "google.com", "googleapis.com", "creativecommons.org",
    "gnu.org", "opensource.org", "mozilla.org", "python.org", "solidworks.com", "autodesk.com",
    "ptc.com", "spatial.com", "siemens.com", "plm.automation.siemens.com", "3ds.com", "w3c.org",
    "inkscape.org", "sodipodi.sourceforge.net", "sourceforge.net", "wikipedia.org", "wikimedia.org",
    "ns.adobe.com", "dublincore.org", "openoffice.org", "libreoffice.org", "oasis-open.org",
)


# ---------------------------------------------------------------------- I/O
def load_plan(plan_dir: str) -> IdentityPlan:
    with open(os.path.join(plan_dir, "plan.json"), encoding="utf-8") as f:
        d = json.load(f)
    secret = _read_secret(plan_dir)
    entries = [Entry(value=x["value"], kind=x["kind"], mode=x["mode"], keep=tuple(x.get("keep", ())),
                     count=x.get("count", 0), sources=x.get("sources", 0), origin=x.get("origin", ""))
               for x in d["entries"]]
    inv = []
    ip = os.path.join(plan_dir, "inventory.txt")
    if os.path.exists(ip):
        with open(ip, encoding="utf-8") as f:
            inv = [l.rstrip("\n") for l in f if l.strip()]
    meta = dict(d.get("meta", {}))
    meta["version"] = d.get("version")
    return IdentityPlan(entries, secret, inv, token_aliases=d.get("token_aliases"), meta=meta)


def _read_secret(plan_dir: str) -> bytes:
    p = os.environ.get("DEID_SECRET_FILE") or os.path.join(plan_dir, "secret.key")
    with open(p, "rb") as f:
        return f.read()


def save_plan(plan: IdentityPlan, plan_dir: str):
    os.makedirs(plan_dir, exist_ok=True)
    sp = os.path.join(plan_dir, "secret.key")
    if not os.path.exists(sp) and not os.environ.get("DEID_SECRET_FILE"):
        with open(sp, "wb") as f:
            f.write(plan.secret)
        os.chmod(sp, 0o600)
    tmp = os.path.join(plan_dir, "plan.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan.to_json(), f, ensure_ascii=False, indent=0)
    os.replace(tmp, os.path.join(plan_dir, "plan.json"))
    if plan.inventory:
        tmp = os.path.join(plan_dir, "inventory.txt.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for p in plan.inventory:
                f.write(p + "\n")
        os.replace(tmp, os.path.join(plan_dir, "inventory.txt"))
