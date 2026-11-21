"""Adapter interface, plan protocol and format registry.

This module is the contract between the pipeline (owner: pipeline), the
identity/plan builder (owner: identity) and the format adapters (owners:
neutral, docs-native, pipeline for archives).  See docs/INTERFACE.md.

Keep this file dependency-free (stdlib only) and backwards compatible:
add optional fields with defaults, never rename or remove.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Protocol, runtime_checkable

API_VERSION = "1"

# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------
REWRITTEN = "rewritten"                  # dst written with changes
VERIFIED_UNCHANGED = "verified_unchanged"  # adapter understood the file, nothing to change
COPIED_UNSUPPORTED = "copied_unsupported"  # adapter (or no adapter) cannot edit; original bytes copied
SOURCE_EXCEPTION = "source_exception"      # input itself is broken/encrypted/empty; copied unchanged + documented
# Set by the pipeline only (never returned by an adapter):
FALLBACK_COPY = "fallback_copy"            # adapter raised / timed out / failed verify -> original bytes copied

ADAPTER_STATUSES = {REWRITTEN, VERIFIED_UNCHANGED, COPIED_UNSUPPORTED, SOURCE_EXCEPTION}

# Observation kinds (open set; identity decides what to do with each).
KINDS = (
    "person", "company", "email", "domain", "url", "phone", "address", "account",
    "project", "part_number", "part_name", "document_number", "path", "software",
    "free_text",  # a human-entered free-text field the identity miner may inspect
)


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------
@dataclass
class FileContext:
    """Where a file sits.  For top-level corpus files ``logical_path`` is the
    manifest path (e.g. ``sources/github/owner__repo/a/b.step``).  For archive
    members it is ``<archive logical path>!/<member path>`` (nested:
    ``a.zip!/inner.tar!/x/y.step``); ``container_chain`` lists the outer
    logical paths, outermost first.
    """
    logical_path: str
    container_chain: tuple = ()
    magic: str = "unknown"          # label from sniff(), e.g. "zip", "step", "cfb"
    size: int = 0
    source_namespace: str = ""      # e.g. "sources/github/owner__repo" (identity may refine)
    sha256: str = ""                # content hash of the source bytes (filled by pipeline)
    # Optional callbacks injected by the pipeline (not serialised).  Archive
    # and package adapters use them to process members through the registry.
    #   dispatch_scan(member_ctx, member_src_path) -> ScanResult
    #   dispatch_rewrite(member_ctx, member_src_path, member_dst_path, plan) -> RewriteResult
    dispatch_scan: Optional[Callable[..., "ScanResult"]] = field(default=None, repr=False, compare=False)
    dispatch_rewrite: Optional[Callable[..., "RewriteResult"]] = field(default=None, repr=False, compare=False)
    tmp_dir: str = ""               # private scratch directory for this job (outside out/)
    deadline: float = 0.0           # time.monotonic() deadline; adapters doing loops may check it

    @property
    def ext(self) -> str:
        """Lower-cased last suffix incl. dot ('' if none). '.gz' for x.tar.gz."""
        base = self.member_name.rsplit("/", 1)[-1]
        i = base.rfind(".")
        return base[i:].lower() if i > 0 else ""

    @property
    def member_name(self) -> str:
        """Path inside the innermost container, or the logical path itself."""
        return self.logical_path.rsplit("!/", 1)[-1]

    def child(self, member_name: str, magic: str = "unknown", size: int = 0) -> "FileContext":
        return FileContext(
            logical_path=f"{self.logical_path}!/{member_name}",
            container_chain=self.container_chain + (self.logical_path,),
            magic=magic, size=size, source_namespace=self.source_namespace,
            dispatch_scan=self.dispatch_scan, dispatch_rewrite=self.dispatch_rewrite,
            tmp_dir=self.tmp_dir, deadline=self.deadline,
        )


@dataclass
class Observation:
    """An identifying (or possibly identifying) value seen in a file.

    kind:     one of KINDS (open set)
    field:    where it was found, dotted, adapter-namespaced, e.g.
              "step.FILE_NAME.author", "pdf.info.Author", "zip.comment", "path.segment"
    value:    the exact original string as it appears (decoded to str)
    evidence: short neutral context (e.g. the record type, a label like "DRAWN BY")
    confidence: 0..1, adapter's belief that this is identifying
    """
    kind: str
    field: str
    value: str
    evidence: str = ""
    confidence: float = 1.0


@dataclass
class Reference:
    """A pointer from this file to another file (or archive member).

    raw:  exact spelling as stored (e.g. "..\\parts\\Bracket.SLDPRT", "file:///C:/x/y.stp")
    kind: "path" | "uri" | "member" | "xref" | "component" | "image" | "include" | "other"
    field: where it was found (as in Observation.field)
    """
    raw: str
    kind: str = "path"
    field: str = ""


@dataclass
class ScanResult:
    observations: list = field(default_factory=list)   # list[Observation]
    references: list = field(default_factory=list)     # list[Reference]
    fingerprint: dict = field(default_factory=dict)    # format invariants, JSON-serialisable
    status: str = "ok"                                 # "ok" | "unsupported" | "source_exception"
    notes: list = field(default_factory=list)          # neutral notes (no raw identifiers)
    children: list = field(default_factory=list)       # (member logical path, ScanResult) for containers


@dataclass
class RewriteResult:
    status: str                                        # one of ADAPTER_STATUSES
    counts: dict = field(default_factory=dict)         # e.g. {"strings_replaced": 12}
    notes: list = field(default_factory=list)          # neutral notes (no raw identifiers)
    children: list = field(default_factory=list)       # (member logical path, RewriteResult)


@dataclass
class VerifyResult:
    ok: bool
    notes: list = field(default_factory=list)
    fingerprint: dict = field(default_factory=dict)    # fingerprint of dst, if computed


# ---------------------------------------------------------------------------
# The plan (implemented by deid.identity; NullPlan below is the no-op)
# ---------------------------------------------------------------------------
@runtime_checkable
class Plan(Protocol):
    version: str

    def alias(self, value: str, kind: Optional[str] = None) -> Optional[str]:
        """Pseudonym for an exact identifying value, or None if not identifying."""

    def replace_text(self, text: str) -> str:
        """Replace every known identifier occurring inside free text."""

    def replace_bytes(self, data: bytes, encodings: Iterable[str] = ("utf-8",),
                      length_preserving: bool = False) -> bytes:
        """Byte-level replacement in the given encodings.  With
        length_preserving=True each replacement is padded/truncated to the
        original byte length (for fixed-size binary fields)."""

    def map_path(self, logical_path: str) -> str:
        """Original logical path -> output logical path.  Same number of '/'
        segments, same extension(s), non-identifying segments unchanged.
        Works for member paths ('a.zip!/x/y.step')."""

    def map_reference(self, ctx: FileContext, raw_ref: str) -> str:
        """Rewrite a reference spelled as in ``raw_ref`` (relative, absolute,
        Windows, URI, ...) found in the file at ctx, preserving its style."""


class NullPlan:
    """Identity plan that changes nothing (for tests and the baseline)."""
    version = "null"

    def alias(self, value, kind=None):
        return None

    def replace_text(self, text):
        return text

    def replace_bytes(self, data, encodings=("utf-8",), length_preserving=False):
        return data

    def map_path(self, logical_path):
        return logical_path

    def map_reference(self, ctx, raw_ref):
        return raw_ref


# ---------------------------------------------------------------------------
# Adapter base class
# ---------------------------------------------------------------------------
class Adapter:
    """Subclass, set the class attributes and ``register(MyAdapter())``.

    Rules:
    * ``rewrite`` writes ``dst_path`` only when returning REWRITTEN.  For any
      other status it may leave dst absent: the pipeline copies the original
      bytes (reflink) itself.  dst's parent directory already exists.
    * Never touch src.  Use ctx.tmp_dir for scratch.  Stream big files.
    * Raise on anything unexpected: the pipeline catches, records the
      exception class, and falls back to copying the original bytes.
    * ``verify`` is called by the pipeline after every REWRITTEN result; a
      False result also means fallback to the original bytes.
    * Output must be deterministic for (src bytes, plan.version, adapter version,
      and the logical path if ``path_independent`` is False).
    * Notes/counts must not contain raw identifiers (they go to README.txt).
    """
    name: str = "base"
    version: str = "0"
    priority: int = 0                 # higher wins when several adapters match
    magics: tuple = ()                # sniff() labels this adapter handles
    extensions: tuple = ()            # lower-case suffixes incl. dot (hint only)
    path_independent: bool = False    # True if rewrite output doesn't depend on ctx.logical_path
    timeout_s: float = 300.0          # per-file wall clock budget (pipeline scales by size)

    def matches(self, ctx: FileContext, head: bytes) -> bool:
        if self.magics and ctx.magic in self.magics:
            return True
        return bool(self.extensions) and ctx.ext in self.extensions and ctx.magic in ("unknown", "text")

    def scan(self, ctx: FileContext, src_path: str) -> ScanResult:
        return ScanResult(status="unsupported")

    def rewrite(self, ctx: FileContext, src_path: str, dst_path: str, plan: Plan) -> RewriteResult:
        return RewriteResult(status=COPIED_UNSUPPORTED)

    def verify(self, ctx: FileContext, src_path: str, dst_path: str, plan: Plan) -> VerifyResult:
        ok = os.path.exists(dst_path) and (os.path.getsize(dst_path) > 0 or os.path.getsize(src_path) == 0)
        return VerifyResult(ok=ok)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_REGISTRY: list = []


def register(adapter: Adapter) -> Adapter:
    if any(a.name == adapter.name for a in _REGISTRY):
        _REGISTRY[:] = [a for a in _REGISTRY if a.name != adapter.name]
    _REGISTRY.append(adapter)
    _REGISTRY.sort(key=lambda a: -a.priority)
    return adapter


def adapters() -> list:
    return list(_REGISTRY)


def load_all() -> list:
    """Import every module under deid.formats so they self-register.
    Import errors are returned (module, error) rather than raised."""
    import importlib
    import pkgutil
    errors = []
    try:
        import deid.formats as pkg
    except Exception as e:  # pragma: no cover
        return [("deid.formats", repr(e))]
    for m in sorted(pkgutil.walk_packages(pkg.__path__, "deid.formats.")):
        try:
            importlib.import_module(m.name)
        except Exception as e:
            errors.append((m.name, f"{type(e).__name__}: {e}"))
    return errors


def route(ctx: FileContext, head: bytes) -> Optional[Adapter]:
    for a in _REGISTRY:
        try:
            if a.matches(ctx, head):
                return a
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Magic sniffing (route by content, not just extension)
# ---------------------------------------------------------------------------
HEAD_BYTES = 8192


def sniff(head: bytes, name: str = "") -> str:
    """Return a coarse content label for routing."""
    h = head
    low = name.lower()
    if not h:
        return "empty"
    if h.startswith(b"PK\x03\x04") or h.startswith(b"PK\x05\x06") or h.startswith(b"PK\x07\x08"):
        return "zip"
    if h.startswith(b"\x1f\x8b"):
        return "gzip"
    if h.startswith(b"\x1f\x9d"):
        return "compress"
    if h.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if h.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if h.startswith(b"BZh") and len(h) > 3 and h[3:4].isdigit():
        return "bzip2"
    if h.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if len(h) >= 262 and h[257:262] == b"ustar":
        return "tar"
    if h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "cfb"
    if h.startswith(b"%PDF") or (b"%PDF-" in h[:1024]):
        return "pdf"
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if h.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if h.startswith(b"II*\x00") or h.startswith(b"MM\x00*"):
        return "tiff"
    if h.startswith(b"GIF8"):
        return "gif"
    if h.startswith(b"BM") and low.endswith(".bmp"):
        return "bmp"
    if h.startswith(b"AC10") and h[4:6].isdigit():
        return "dwg"
    if h.startswith(b"\xc5\xd0\xd3\xc6"):
        return "eps_binary"
    if h.startswith(b"%!PS"):
        return "eps" if b"EPSF" in h[:40] else "ps"
    s = h.lstrip(b"\xef\xbb\xbf \t\r\n")
    if s.startswith(b"ISO-10303-21"):
        return "step"
    if s.startswith(b"<?xml") or s.startswith(b"<svg") or s.startswith(b"<!DOCTYPE svg"):
        if b"<svg" in h:
            return "svg"
        return "xml"
    if s.startswith(b"#VRML") or s.startswith(b"#X3D"):
        return "vrml"
    if s.startswith(b"DBRep_DrawableShape") or s.startswith(b"CASCADE Topology"):
        return "brep"
    if s[:6] in (b"solid ", b"solid\n", b"solid\r") and low.endswith(".stl"):
        return "stl_ascii"
    if s.startswith(b"**ABS") or s.startswith(b"**PART") or s.startswith(b"**PARASOLID"):
        return "parasolid_text"
    if s.startswith(b"PS\x00") or h[:2] == b"PS" and low.endswith((".x_b", ".xmt_bin")):
        return "parasolid_binary"
    if h.startswith(b"ACIS BinaryFile") or h.startswith(b"ACIS BinFile"):
        return "sab"
    if s.startswith(b"Version Test JT") or h.startswith(b"Version ") and low.endswith(".jt"):
        return "jt"
    if h.startswith(b"U3D\x00"):
        return "u3d"
    if h.startswith(b"MM") and low.endswith(".3ds"):
        return "3ds"
    if h.startswith(b"#UGC:") or h.startswith(b"#UGC"):
        return "creo"
    if low.endswith(".stl") and len(h) >= 84:
        return "stl_binary"
    if low.endswith(".dxf") and h.startswith(b"AutoCAD Binary DXF"):
        return "dxf_binary"
    # text heuristic
    sample = h[:4096]
    if b"\x00" not in sample:
        try:
            sample.decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            nonprint = sum(1 for c in sample if c < 9 or 13 < c < 32)
            if nonprint < len(sample) * 0.01:
                return "text"
    return "binary"
