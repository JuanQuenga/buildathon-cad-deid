"""Shared helpers for the neutral-format adapters (step, sat, iges, dxf, mesh,
svg, ps, parasolid, unv, brep, scad, text fallback).

Nothing here registers an adapter.  Notes/counts never carry raw values.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Iterable, Optional

from deid.api import Observation, Reference

# ---------------------------------------------------------------------------
# Plan access with memoisation (big files repeat the same strings millions of times)
# ---------------------------------------------------------------------------


class PlanCache:
    """Wraps a Plan; memoises alias/replace_text/map_reference per value.

    ``sub(value, kind)``: exact alias for ``kind`` first, then free-text
    replacement.  ``ref(value)``: reference mapping, then free-text replacement
    when the reference was not resolved (keeps embedded names consistent).
    """

    __slots__ = ("plan", "ctx", "_alias", "_text", "_ref", "changed")

    def __init__(self, plan, ctx=None):
        self.plan = plan
        self.ctx = ctx
        self._alias: dict = {}
        self._text: dict = {}
        self._ref: dict = {}
        self.changed = 0

    def text(self, value: str) -> str:
        r = self._text.get(value)
        if r is None:
            if not value or not value.strip():
                r = value
            else:
                try:
                    r = self.plan.replace_text(value)
                except Exception:
                    r = value
                if not isinstance(r, str):
                    r = value
            self._text[value] = r
        return r

    def sub(self, value: str, kind: Optional[str] = None) -> str:
        if not value or not value.strip():
            return value
        key = (value, kind)
        r = self._alias.get(key)
        if r is None:
            a = None
            try:
                a = self.plan.alias(value, kind)
            except Exception:
                a = None
            if a is None:
                s = value.strip()
                if s != value and s:
                    try:
                        a2 = self.plan.alias(s, kind)
                    except Exception:
                        a2 = None
                    if a2 is not None:
                        i = value.index(s)
                        a = value[:i] + a2 + value[i + len(s):]
            r = a if isinstance(a, str) else self.text(value)
            self._alias[key] = r
        return r

    def ref(self, value: str) -> str:
        r = self._ref.get(value)
        if r is None:
            r = value
            try:
                m = self.plan.map_reference(self.ctx, value)
                if isinstance(m, str):
                    r = m
            except Exception:
                r = value
            if r == value:
                r = self.text(value)
            self._ref[value] = r
        return r


# ---------------------------------------------------------------------------
# Reference / identity heuristics
# ---------------------------------------------------------------------------

FILE_EXT_RE = re.compile(
    r"\.(?:step|stp|stpz|sat|sab|igs|iges|stl|obj|mtl|wrl|vrml|x3d|dxf|dwg|svg|pdf|png|jpe?g|gif|bmp|tiff?|"
    r"sldprt|sldasm|slddrw|slddrt|sldlfp|prt|asm|drw|neu|ipt|iam|idw|ipn|ipj|catpart|catproduct|catdrawing|"
    r"par|psm|dft|x_t|x_b|xmt_txt|xmt_bin|jt|3mf|f3d|f3z|fcstd|brep|brp|unv|scad|3ds|off|ply|dae|fbx|glb|gltf|"
    r"zip|7z|rar|tar|gz|tgz|txt|csv|xls|xlsx|doc|docx|ps|eps|hpgl|plt|u3d|prc|mf1|ckd|dat)"
    r"(?:\.\d+)?$", re.I)

PATHY_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|\.\.?[\\/]|file:|~[\\/])|[\\/]")
URL_RE = re.compile(r"(?i)^(?:https?|ftp|file)://")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def looks_like_reference(s: str) -> bool:
    """A string that names another file (path, URI, or bare CAD filename)."""
    t = s.strip()
    if not t or len(t) > 1024 or "\n" in t:
        return False
    if URL_RE.match(t):
        return True
    if FILE_EXT_RE.search(t):
        return True
    return bool(PATHY_RE.search(t)) and ("\\" in t or t.count("/") >= 2) and " " not in t.strip("/\\")[:0]


def ref_kind(s: str) -> str:
    return "uri" if URL_RE.match(s.strip()) else "path"


class ObsCollector:
    """Deduplicating observation/reference collector with a size cap."""

    def __init__(self, cap: int = 4000):
        self.cap = cap
        self.obs: list = []
        self.refs: list = []
        self._seen: set = set()
        self._rseen: set = set()
        self.dropped = 0

    def add(self, kind: str, field: str, value: str, evidence: str = "", confidence: float = 1.0):
        if not value or not value.strip():
            return
        key = (kind, field, value)
        if key in self._seen:
            return
        if len(self.obs) >= self.cap:
            self.dropped += 1
            return
        self._seen.add(key)
        self.obs.append(Observation(kind=kind, field=field, value=value, evidence=evidence[:120],
                                    confidence=confidence))

    def ref(self, raw: str, kind: str, field: str):
        key = (raw, field)
        if key in self._rseen or not raw:
            return
        self._rseen.add(key)
        self.refs.append(Reference(raw=raw, kind=kind, field=field))


# ---------------------------------------------------------------------------
# Text decoding helpers
# ---------------------------------------------------------------------------


def dec(b: bytes) -> tuple[str, str]:
    """Decode bytes as UTF-8 if valid, else cp1252/latin-1.  Returns (text, encoding)."""
    try:
        return b.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        try:
            return b.decode("cp1252"), "cp1252"
        except UnicodeDecodeError:
            return b.decode("latin-1"), "latin-1"


def enc(s: str, encoding: str) -> bytes:
    try:
        return s.encode(encoding)
    except UnicodeEncodeError:
        return s.encode(encoding, errors="replace") if encoding != "utf-8" else s.encode("utf-8", "surrogateescape")


def file_sha256(path: str, bufsize: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_head(path: str, n: int = 8192) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def is_text_bytes(b: bytes) -> bool:
    if b"\x00" in b:
        return False
    if not b:
        return True
    bad = sum(1 for c in b if c < 9 or 13 < c < 32 and c != 26 and c != 27)
    return bad <= len(b) * 0.002
