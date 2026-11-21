"""Per-document copies of saving accounts discovered in CFB metadata."""
from __future__ import annotations

import re

from deid.formats import _nativeutil as nu
from deid.identity.aliases import TOKEN_RE, apply_case
from deid.identity.plan import USERDIR_RE
from deid.identity.vocab import is_generic_token

_SAVE_LABELS = {
    "lastauthor", "lastsavedby", "lastsaveduser", "lastsavedusername", "lastsavingaccount",
    "lastmodifiedby", "lastmodifier", "lastwrittenby", "savedby", "savedbyuser", "savingaccount",
    "saveaccount", "username", "useraccount",
}
# These longer built-ins can safely match a complete text token in a CAD stream.
_DISTINCT_ACCOUNTS = {"administrator", "defaultuser", "localuser", "workstation"}


def is_save_label(name: str) -> bool:
    # SW localizes its labels and may include an English name in parentheses.
    names = re.split(r"[()]", name.casefold())
    return any(re.sub(r"[\W_]", "", part).removeprefix("sw") in _SAVE_LABELS for part in names)


def header_account(data: bytes):
    """The first account CString in the recognized legacy SolidWorks Header2 layout."""
    if data[39:42] != b"\xff\xfe\xff":
        return None
    field = next(nu.iter_cstrings_u16(data[39:], max_chars=128), None)
    if field is None or field[0] != 4:
        return None
    return 43, field[1], field[2]


def is_account(value: str) -> bool:
    value = value.strip()
    return (0 < len(value) <= 128 and value.isprintable()
            and any(c.isalnum() for c in value) and "/" not in value
            and value.count("\\") <= 1)


class AccountAliases:
    def __init__(self, plan, originals):
        self.aliases = {}
        text_matches = []
        for original in sorted(set(v.strip() for v in originals if is_account(v))):
            pivot = original.casefold()
            alias = plan.alias(original, "saved_account")
            if not alias or len(alias) != len(original) or alias.casefold() == pivot:
                continue
            # Unicode case expansion cannot safely render a fixed-width case variant.
            self.aliases[pivot] = (original, alias)
            toks = TOKEN_RE.findall(original)
            if len(original) >= 4 and (
                pivot in _DISTINCT_ACCOUNTS or len(toks) > 1 or
                any(not is_generic_token(t.casefold()) for t in toks)
            ):
                text_matches.append(original)
        self.pattern = re.compile(
            r"(?<!\w)(?:" + "|".join(re.escape(v) for v in sorted(text_matches, key=lambda v: (-len(v), v)))
            + r")(?!\w)", re.IGNORECASE
        ) if text_matches else None

    def __bool__(self):
        return bool(self.aliases)

    def exact(self, text: str) -> str:
        stripped = text.strip()
        found = self.aliases.get(stripped.casefold())
        if found is None:
            return text
        original, alias = found
        if stripped != original:
            lowered = alias.lower()
            if len(stripped) != len(lowered):
                return text
            alias = apply_case(stripped, lowered)
            if len(alias) != len(stripped):
                return text
        pad = len(text) - len(text.lstrip())
        return text[:pad] + alias + text[pad + len(stripped):]

    def profiles(self, text: str) -> str:
        def rewrite(m):
            return m.group(0)[:m.start(1) - m.start(0)] + self.exact(m.group(1))

        return USERDIR_RE.sub(rewrite, text)

    def text(self, text: str, *, exact=False) -> str:
        if exact:
            replacement = self.exact(text)
            if replacement != text:
                return replacement
        if self.pattern is not None:
            text = self.pattern.sub(lambda m: self.exact(m.group(0)), text)
        return self.profiles(text)

    def appears_in_slack(self, data: bytes) -> bool:
        low = data.lower()
        for original, _ in self.aliases.values():
            if len(original) < 3:
                continue
            for enc in ("utf-8", "utf-16-le", "cp1252"):
                try:
                    needle = original.encode(enc).lower()
                except UnicodeEncodeError:
                    continue
                if needle in low:
                    return True
        return False
