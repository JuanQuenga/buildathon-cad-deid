"""Corpus-wide dictionary matcher.

Entries are identity values split into normalised tokens.  A character trie
over all entries is compiled to one regular expression (case-insensitive),
with a flexible separator between tokens for "flex" entries:

    "Zorblax-Works" matches  Zorblax-Works, zorblax_works, ZORBLAX WORKS,
                               ZorblaxWorks, Zorblax%20Works

Boundaries: a match must not be preceded by a letter/digit, and must be
followed by a non-alphanumeric char, OR by a case/class transition
(camelCase 'acmeParts', letter->digit 'acme2') for entries of >= 4 chars.
"""
from __future__ import annotations

import re

SEP_FLEX = r"(?:[ _.,\-]|%20){0,2}"   # between tokens of a flex entry

_SEP = object()  # trie symbol for a flexible separator
_END = ""        # trie end marker key


def _entry_symbols(tokens, seps, flex):
    """Symbol sequence for the trie: casefolded chars and separators."""
    out = []
    for i, t in enumerate(tokens):
        if i:
            if flex:
                out.append(_SEP)
            else:
                for ch in seps[i - 1]:
                    out.append(("lit", ch))
        for ch in t:
            out.append(ch)
    return out


def _trie_to_regex(node):
    alts = []
    has_end = _END in node
    for sym, child in node.items():
        if sym == _END:
            continue
        if sym is _SEP:
            head = SEP_FLEX
        elif isinstance(sym, tuple):
            head = re.escape(sym[1])
        else:
            head = re.escape(sym)
        tail = _trie_to_regex(child)
        alts.append(head + tail)
    if not alts:
        return ""
    # Sort: longer alternatives first is not needed for a trie (prefix-free
    # per char), but separator vs char ordering matters: put chars first.
    if len(alts) == 1:
        body = alts[0]
        if has_end:
            return "(?:" + body + ")?"
        return body
    body = "(?:" + "|".join(alts) + ")"
    if has_end:
        body += "?"
    return body


class Matcher:
    """Find dictionary entries in text."""

    def __init__(self, entries):
        """entries: iterable of (tokens:list[str] casefolded, seps:list[str], flex:bool, min_len_ok:bool)"""
        self.trie = {}
        n = 0
        for tokens, seps, flex in entries:
            if not tokens:
                continue
            node = self.trie
            for sym in _entry_symbols(tokens, seps, flex):
                node = node.setdefault(sym, {})
            node[_END] = True
            n += 1
        self.n = n
        self.rx = None       # case-sensitive, run on text.lower() (fast path)
        self._rx_ci = None   # IGNORECASE fallback when lower() changes length
        self._pat = None
        if n:
            self._pat = r"(?<![^\W_])(?:" + _trie_to_regex(self.trie) + ")"
            self.rx = re.compile(self._pat)

    def finditer(self, text, lw=None):
        """Yield (start, end) of accepted matches (non-overlapping, leftmost-longest by trie)."""
        if self.rx is None:
            return
        n = len(text)
        if lw is None:
            lw = text.lower()
        if len(lw) == n:
            it = self.rx.finditer(lw)
        else:
            if self._rx_ci is None:
                self._rx_ci = re.compile(self._pat, re.IGNORECASE)
            it = self._rx_ci.finditer(text)
        for m in it:
            s, e = m.span()
            if e == s:
                continue
            if e < n:
                nxt = text[e]
                last = text[e - 1]
                if nxt.isalnum():
                    if (e - s) < 4:
                        continue
                    ok = False
                    if last.isalpha() and nxt.isdigit():
                        ok = True
                    elif last.isdigit() and nxt.isalpha():
                        ok = True
                    elif last.islower() and nxt.isupper():
                        ok = True
                    if not ok:
                        continue
            yield s, e
