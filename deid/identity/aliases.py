"""Deterministic, format-preserving pseudonyms.

Design (see docs/identity.md):

* An alias depends only on (secret key, normalised token key, attempt counter),
  never on processing order, so parallel workers and re-runs agree.
* Values are split into *tokens* (maximal runs of letters/digits) and
  *separators* (everything else).  Each token is aliased independently and
  separators are kept verbatim, so "ACME Corp", "acme_corp" and "ACME-CORP"
  all render from the same two token aliases and only differ in case and
  separators, exactly like their originals.
* Every character keeps its class and case: upper->upper, lower->lower,
  digit->digit, and non-ASCII letters map inside a block whose UTF-8,
  UTF-16 and (where applicable) Latin-1 encoded lengths are identical.
  Hence ``len(alias) == len(original)`` in characters *and* in bytes for
  every common encoding: safe for in-place edits in binary formats.
* Letters are generated as pronounceable consonant/vowel sequences so a
  company still looks like a name and a person like a person.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata

CONS = "bcdfghjklmnprstvz"
VOWS = "aeiou"
# Latin-1 letters (2 bytes UTF-8, 1 byte latin-1, 2 bytes UTF-16).
LAT1_LOWER = "àáâäçèéêëìíîïñòóôöùúûü"
LAT1_UPPER = "ÀÁÂÄÇÈÉÊËÌÍÎÏÑÒÓÔÖÙÚÛÜ"

# token = run of unicode letters/digits; we treat '_' as a separator
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def norm_token(tok: str) -> str:
    """Normalisation key of a single token: NFC + lower() (length-stable)."""
    return unicodedata.normalize("NFC", tok).lower()


def norm_key(value: str) -> str:
    """Normalisation key of a whole value: its tokens, lower-cased, joined
    by a single space.  'ACME_Corp', 'acme corp' and 'Acme-Corp' share
    the key 'acme corp'."""
    return " ".join(norm_token(t) for t in TOKEN_RE.findall(value))


class _Stream:
    """Deterministic pseudo-random byte stream from HMAC-SHA256."""

    def __init__(self, key: bytes, msg: str):
        self.key = key
        self.msg = msg.encode("utf-8")
        self.buf = b""
        self.n = 0

    def byte(self) -> int:
        if not self.buf:
            self.buf = hmac.new(self.key, self.msg + b"\x00" + str(self.n).encode(), hashlib.sha256).digest()
            self.n += 1
        b, self.buf = self.buf[0], self.buf[1:]
        return b

    def choice(self, seq):
        return seq[self.byte() % len(seq)]


def _map_nonascii_letter(ch: str, st: _Stream) -> str:
    o = ord(ch)
    if ch in LAT1_LOWER:
        return st.choice(LAT1_LOWER)
    if ch in LAT1_UPPER:
        return st.choice(LAT1_UPPER)
    if 0x00C0 <= o <= 0x00FF:  # other latin-1 letters (ß, ø, ...)
        return st.choice(LAT1_LOWER if ch.islower() else LAT1_UPPER)
    if 0x0100 <= o <= 0x017F:  # Latin Extended-A, pairs upper/lower
        base = 0x0100 + 2 * (st.byte() % 0x38)
        if base in (0x0130, 0x0138, 0x0148, 0x0149):  # avoid odd code points
            base = 0x0102
        return chr(base + (1 if ch.islower() else 0))
    if 0x0410 <= o <= 0x042F:  # Cyrillic upper
        return chr(0x0410 + st.byte() % 32)
    if 0x0430 <= o <= 0x044F:  # Cyrillic lower
        return chr(0x0430 + st.byte() % 32)
    if 0x0391 <= o <= 0x03A9:
        return chr(0x0391 + st.byte() % 17)
    if 0x03B1 <= o <= 0x03C9:
        return chr(0x03B1 + st.byte() % 17)
    if 0x3041 <= o <= 0x3096:  # Hiragana
        return chr(0x3041 + (st.byte() * 256 + st.byte()) % 0x56)
    if 0x30A1 <= o <= 0x30FA:  # Katakana
        return chr(0x30A1 + (st.byte() * 256 + st.byte()) % 0x5A)
    if 0x4E00 <= o <= 0x9FA5:  # CJK unified (BMP, 3 bytes UTF-8)
        return chr(0x4E00 + (st.byte() * 256 + st.byte()) % 0x51A6)
    if 0xAC00 <= o <= 0xD7A3:  # Hangul syllables
        return chr(0xAC00 + (st.byte() * 256 + st.byte()) % 0x2BA4)
    return ch  # unknown script: kept (documented limitation)


def alias_token(token: str, key: bytes, attempt: int = 0) -> str:
    """Alias for one token, char-class and case preserving.

    Generated from the casefolded token so case variants share letters; the
    original's per-character case is then applied."""
    nk = norm_token(token)
    st = _Stream(key, f"tok:{attempt}:{nk}")
    out = []
    want_vowel = st.byte() % 3 == 0  # start with vowel 1/3 of the time
    for ch in token:
        if "0" <= ch <= "9":
            out.append(chr(48 + st.byte() % 10))
            want_vowel = st.byte() % 3 == 0
        elif ch.isascii() and ch.isalpha():
            c = st.choice(VOWS if want_vowel else CONS)
            # mostly alternate; sometimes double consonant for realism
            want_vowel = (not want_vowel) if st.byte() % 5 else want_vowel
            out.append(c.upper() if ch.isupper() else c)
        elif ch.isalpha():
            out.append(_map_nonascii_letter(ch, st))
        elif ch.isdigit():  # non-ASCII digit: keep class within same block
            out.append(ch)
        else:
            out.append(ch)
    res = "".join(out)
    return res


def render(value: str, token_alias) -> str:
    """Apply ``token_alias(token) -> alias`` to each token of value, keeping
    separators verbatim."""
    return TOKEN_RE.sub(lambda m: token_alias(m.group(0)), value)


def apply_case(template: str, alias_lower: str) -> str:
    """Re-case alias_lower (same length as template) after template."""
    return "".join(a.upper() if t.isupper() else a for t, a in zip(template, alias_lower))
