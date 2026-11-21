"""STEP / ISO 10303-21 (.step .stp, also .stpz is not handled here).

Surgical streaming rewrite: only quoted string tokens and comments may change;
every other byte (entity ids, numbers, whitespace, line endings, ordering) is
copied verbatim.  Strings are decoded per Part 21 (``''``, ``\\\\``, ``\\S\\``,
``\\X\\hh``, ``\\X2\\..\\X0\\``, ``\\X4\\..\\X0\\``, ``\\Px\\``; raw UTF-8 / latin-1
bytes tolerated) and re-encoded in the file's own style.

Tokeniser: a single regex consumes, in C, everything that cannot carry identity
(non-string bytes, empty strings ``''`` and a few vocabulary strings) and stops
at the next interesting token (a non-empty string or a comment).  Python only
runs once per interesting token, so geometry-only stretches stream at memory
speed.  The record context (entity keyword and argument path) is parsed only
for interesting tokens.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

from deid.api import (Adapter, register, ScanResult, RewriteResult, VerifyResult,
                      REWRITTEN, VERIFIED_UNCHANGED, SOURCE_EXCEPTION, COPIED_UNSUPPORTED)
from deid.formats._neutral import PlanCache, ObsCollector, looks_like_reference, ref_kind

CHUNK = 8 << 20

# Interesting token = non-empty string or comment, preceded by skippable bytes.
# Skippable: runs of non-quote/non-slash bytes, empty strings '' (not followed
# by another quote) and single slashes that do not open a comment.
_BORING = rb"NONE| +|\?|\*"
TOK = re.compile(
    rb"(?:[^'/]++|''(?!')|'(?:" + _BORING + rb")'(?!')|/(?!\*))*+"
    rb"('(?:[^']++|'')*+'|/\*.*?\*/)", re.S)
_MTOK = re.compile(rb"'[^']*'|/\*.*?\*/", re.S)
SKIP = re.compile(rb"(?:[^'/]++|''(?!')|/(?!\*))*+")

# Context parsing of a record prefix
_CTX_TOK = re.compile(rb"'(?:[^']|'')*'|/\*.*?\*/|[A-Za-z_][A-Za-z0-9_\-]*|[(),]|#\d+|[^'(),A-Za-z_#/]+|.", re.S)
_REC_HEAD = re.compile(rb"\s*(?:#\d+\s*=\s*)?")

# Field names for well-known entities (argument index -> name)
FIELDS = {
    "FILE_DESCRIPTION": ("description", "implementation_level"),
    "FILE_NAME": ("name", "time_stamp", "author", "organization", "preprocessor_version",
                  "originating_system", "authorisation"),
    "FILE_SCHEMA": ("schema_identifiers",),
    "PRODUCT": ("id", "name", "description", "frame_of_reference"),
    "PRODUCT_DEFINITION": ("id", "description", "formation", "frame_of_reference"),
    "PRODUCT_DEFINITION_FORMATION": ("id", "description", "of_product"),
    "PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE": ("id", "description", "of_product", "make_or_buy"),
    "NEXT_ASSEMBLY_USAGE_OCCURRENCE": ("id", "name", "description", "relating", "related", "reference_designator"),
    "PERSON": ("id", "last_name", "first_name", "middle_names", "prefix_titles", "suffix_titles"),
    "ORGANIZATION": ("id", "name", "description"),
    "PERSON_AND_ORGANIZATION_ROLE": ("name",),
    "APPROVAL": ("status", "level"),
    "APPROVAL_STATUS": ("name",),
    "DOCUMENT": ("id", "name", "description", "kind"),
    "DOCUMENT_FILE": ("id", "name", "description", "kind", "source", "representation_types"),
    "PRODUCT_RELATED_PRODUCT_CATEGORY": ("name", "description", "products"),
    "PROPERTY_DEFINITION": ("name", "description", "definition"),
    "DESCRIPTIVE_REPRESENTATION_ITEM": ("name", "description"),
    "ADDRESS": ("internal_location", "street_number", "street", "postal_box", "town", "region",
                "postal_code", "country", "facsimile_number", "telephone_number",
                "electronic_mail_address", "telex_number"),
}
for _a in ("PERSONAL_ADDRESS", "ORGANIZATIONAL_ADDRESS"):
    FIELDS[_a] = FIELDS["ADDRESS"]

# (entity, field) -> identity kind for plan.alias
KIND_BY_FIELD = {
    ("FILE_NAME", "author"): "person",
    ("FILE_NAME", "organization"): "company",
    ("FILE_NAME", "authorisation"): "person",
    ("FILE_NAME", "preprocessor_version"): "software",
    ("FILE_NAME", "originating_system"): "software",
    ("FILE_DESCRIPTION", "description"): "free_text",
    ("PRODUCT", "id"): "part_number",
    ("PRODUCT", "name"): "part_name",
    ("PRODUCT", "description"): "free_text",
    ("PRODUCT_DEFINITION", "id"): "part_number",
    ("PRODUCT_DEFINITION", "description"): "free_text",
    ("PRODUCT_DEFINITION_FORMATION", "description"): "free_text",
    ("PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE", "description"): "free_text",
    ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "id"): "free_text",
    ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "name"): "part_name",
    ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "description"): "free_text",
    ("NEXT_ASSEMBLY_USAGE_OCCURRENCE", "reference_designator"): "part_name",
    ("PERSON", "id"): "person",
    ("PERSON", "last_name"): "person",
    ("PERSON", "first_name"): "person",
    ("PERSON", "middle_names"): "person",
    ("PERSON", "prefix_titles"): "person",
    ("PERSON", "suffix_titles"): "person",
    ("ORGANIZATION", "id"): "company",
    ("ORGANIZATION", "name"): "company",
    ("ORGANIZATION", "description"): "free_text",
    ("DOCUMENT", "id"): "document_number",
    ("DOCUMENT", "name"): "part_name",
    ("DOCUMENT", "description"): "free_text",
    ("DOCUMENT_FILE", "id"): "path",
    ("DOCUMENT_FILE", "name"): "part_name",
    ("DOCUMENT_FILE", "description"): "free_text",
    ("PRODUCT_RELATED_PRODUCT_CATEGORY", "name"): "part_name",
    ("PRODUCT_RELATED_PRODUCT_CATEGORY", "description"): "free_text",
    ("DESCRIPTIVE_REPRESENTATION_ITEM", "description"): "free_text",
}
for _a in ("ADDRESS", "PERSONAL_ADDRESS", "ORGANIZATIONAL_ADDRESS"):
    for _f in FIELDS["ADDRESS"]:
        KIND_BY_FIELD[(_a, _f)] = ("phone" if _f in ("facsimile_number", "telephone_number", "telex_number")
                                   else "email" if _f == "electronic_mail_address" else "address")

# Names of representation-like entities: argument 0 is a human name (often the part name).
NAMED_REPR = {
    "SHAPE_REPRESENTATION", "ADVANCED_BREP_SHAPE_REPRESENTATION", "MANIFOLD_SURFACE_SHAPE_REPRESENTATION",
    "FACETED_BREP_SHAPE_REPRESENTATION", "GEOMETRICALLY_BOUNDED_SURFACE_SHAPE_REPRESENTATION",
    "GEOMETRICALLY_BOUNDED_WIREFRAME_SHAPE_REPRESENTATION", "EDGE_BASED_WIREFRAME_SHAPE_REPRESENTATION",
    "SHELL_BASED_WIREFRAME_SHAPE_REPRESENTATION", "TESSELLATED_SHAPE_REPRESENTATION",
    "MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS", "FACETED_BREP", "SHELL_BASED_SURFACE_MODEL",
    "TESSELLATED_SOLID", "TESSELLATED_SHELL", "PRESENTATION_LAYER_ASSIGNMENT",
    "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION", "DRAUGHTING_MODEL", "REPRESENTATION",
    "PRODUCT_DEFINITION_SHAPE", "SHAPE_ASPECT", "GEOMETRIC_CURVE_SET", "GEOMETRIC_SET",
    "CONTEXT_DEPENDENT_SHAPE_REPRESENTATION", "MAPPED_ITEM", "REPRESENTATION_MAP", "STYLED_ITEM",
    "OVER_RIDING_STYLED_ITEM", "COLOUR_RGB", "FILL_AREA_STYLE", "FILL_AREA_STYLE_COLOUR",
    "SURFACE_STYLE_USAGE", "CURVE_STYLE", "PRESENTATION_STYLE_ASSIGNMENT", "ITEM_DEFINED_TRANSFORMATION",
    "SHAPE_REPRESENTATION_RELATIONSHIP", "PROPERTY_DEFINITION", "PROPERTY_DEFINITION_REPRESENTATION",
    "DATUM", "DATUM_FEATURE", "TEXT_LITERAL", "ANNOTATION_TEXT_OCCURRENCE", "DESCRIPTIVE_REPRESENTATION_ITEM",
    "CAMERA_MODEL_D3", "MATERIAL_DESIGNATION", "GENERAL_PROPERTY", "ID_ATTRIBUTE", "NAME_ATTRIBUTE",
    "DESCRIPTION_ATTRIBUTE", "IDENTIFICATION_ROLE", "IDENTIFIER", "LABEL", "TEXT", "EXTERNAL_SOURCE",
}

# Pure geometry/topology entities: string arg 0 is a machine label (POINT2, EDGE3, F16...).
GEOMETRY = {
    "CARTESIAN_POINT", "DIRECTION", "VECTOR", "LINE", "CIRCLE", "ELLIPSE", "PLANE", "AXIS1_PLACEMENT",
    "AXIS2_PLACEMENT_2D", "AXIS2_PLACEMENT_3D", "VERTEX_POINT", "EDGE_CURVE", "ORIENTED_EDGE", "EDGE_LOOP",
    "VERTEX_LOOP", "POLY_LOOP", "FACE_BOUND", "FACE_OUTER_BOUND", "ADVANCED_FACE", "FACE_SURFACE",
    "CLOSED_SHELL", "OPEN_SHELL", "ORIENTED_CLOSED_SHELL", "CONNECTED_FACE_SET", "CYLINDRICAL_SURFACE",
    "CONICAL_SURFACE", "SPHERICAL_SURFACE", "TOROIDAL_SURFACE", "DEGENERATE_TOROIDAL_SURFACE",
    "B_SPLINE_CURVE_WITH_KNOTS", "B_SPLINE_SURFACE_WITH_KNOTS", "QUASI_UNIFORM_CURVE", "QUASI_UNIFORM_SURFACE",
    "RATIONAL_B_SPLINE_CURVE", "RATIONAL_B_SPLINE_SURFACE", "B_SPLINE_CURVE", "B_SPLINE_SURFACE",
    "BOUNDED_CURVE", "BOUNDED_SURFACE", "CURVE", "SURFACE", "GEOMETRIC_REPRESENTATION_ITEM",
    "REPRESENTATION_ITEM", "TRIMMED_CURVE", "PCURVE", "SURFACE_CURVE", "SEAM_CURVE", "INTERSECTION_CURVE",
    "DEFINITIONAL_REPRESENTATION", "SURFACE_OF_LINEAR_EXTRUSION", "SURFACE_OF_REVOLUTION",
    "OFFSET_SURFACE", "OFFSET_CURVE_3D", "COMPOSITE_CURVE", "COMPOSITE_CURVE_SEGMENT", "POLYLINE",
    "RECTANGULAR_TRIMMED_SURFACE", "CURVE_BOUNDED_SURFACE", "BOUNDARY_CURVE", "UNIFORM_CURVE",
    "UNIFORM_SURFACE", "BEZIER_CURVE", "BEZIER_SURFACE", "CARTESIAN_TRANSFORMATION_OPERATOR_3D",
    "COORDINATES_LIST", "TRIANGULATED_FACE", "COMPLEX_TRIANGULATED_FACE", "TRIANGULATED_SURFACE_SET",
    "COMPLEX_TRIANGULATED_SURFACE_SET", "SUBEDGE", "EDGE", "VERTEX", "FACE", "LOOP", "PATH",
    "LENGTH_MEASURE_WITH_UNIT", "PLANE_ANGLE_MEASURE_WITH_UNIT", "UNCERTAINTY_MEASURE_WITH_UNIT",
    "DIMENSIONAL_EXPONENTS", "SI_UNIT", "CONVERSION_BASED_UNIT", "NAMED_UNIT", "LENGTH_UNIT",
    "PLANE_ANGLE_UNIT", "SOLID_ANGLE_UNIT", "GLOBAL_UNIT_ASSIGNED_CONTEXT",
    "GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT", "GEOMETRIC_REPRESENTATION_CONTEXT", "REPRESENTATION_CONTEXT",
    "PARAMETRIC_REPRESENTATION_CONTEXT", "DRAUGHTING_PRE_DEFINED_COLOUR", "DRAUGHTING_PRE_DEFINED_CURVE_FONT",
    "APPLICATION_PROTOCOL_DEFINITION", "APPLICATION_CONTEXT", "PRODUCT_CONTEXT", "MECHANICAL_CONTEXT",
    "PRODUCT_DEFINITION_CONTEXT", "DESIGN_CONTEXT", "PRODUCT_CATEGORY", "PRODUCT_TYPE", "DATE_TIME_ROLE",
    "DATE_ROLE", "APPROVAL_ROLE", "APPROVAL_STATUS", "SECURITY_CLASSIFICATION_LEVEL",
    "PERSON_AND_ORGANIZATION_ROLE", "DOCUMENT_TYPE", "OBJECT_ROLE", "ROLE_ASSOCIATION",
    "CALENDAR_DATE", "LOCAL_TIME", "COORDINATED_UNIVERSAL_TIME_OFFSET", "DATE_AND_TIME",
    "SURFACE_SIDE_STYLE", "SURFACE_STYLE_FILL_AREA", "MEASURE_REPRESENTATION_ITEM",
    "VALUE_REPRESENTATION_ITEM", "QUALIFIED_REPRESENTATION_ITEM", "PRECISION_QUALIFIER",
}


# Format-required vocabulary: never passed to the plan (schema names, AP context strings,
# units, predefined colours, role/status enumerations, timestamps).
PROTECTED_FIELDS = {("FILE_DESCRIPTION", "implementation_level"), ("FILE_NAME", "time_stamp"),
                    ("FILE_SCHEMA", "schema_identifiers")}
PROTECTED_ENTITIES = {
    "FILE_SCHEMA", "APPLICATION_PROTOCOL_DEFINITION", "APPLICATION_CONTEXT", "CALENDAR_DATE", "LOCAL_TIME",
    "DATE_AND_TIME", "COORDINATED_UNIVERSAL_TIME_OFFSET", "SI_UNIT", "CONVERSION_BASED_UNIT", "NAMED_UNIT",
    "DIMENSIONAL_EXPONENTS", "DRAUGHTING_PRE_DEFINED_COLOUR", "DRAUGHTING_PRE_DEFINED_CURVE_FONT",
    "PRE_DEFINED_MARKER", "PRE_DEFINED_SYMBOL", "PRE_DEFINED_TEXT_FONT", "PRODUCT_CONTEXT",
    "MECHANICAL_CONTEXT", "PRODUCT_DEFINITION_CONTEXT", "DESIGN_CONTEXT", "PRODUCT_CATEGORY", "PRODUCT_TYPE",
    "SECURITY_CLASSIFICATION_LEVEL", "APPROVAL_STATUS", "DATE_TIME_ROLE", "DATE_ROLE", "APPROVAL_ROLE",
    "PERSON_AND_ORGANIZATION_ROLE", "OBJECT_ROLE", "DOCUMENT_TYPE", "TYPE_QUALIFIER", "PRECISION_QUALIFIER",
    "UNCERTAINTY_MEASURE_WITH_UNIT", "LENGTH_UNIT", "PLANE_ANGLE_UNIT", "SOLID_ANGLE_UNIT", "MASS_UNIT",
    "AREA_UNIT", "VOLUME_UNIT", "TIME_UNIT", "THERMODYNAMIC_TEMPERATURE_UNIT", "RATIO_UNIT",
    "GLOBAL_UNIT_ASSIGNED_CONTEXT", "GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT", "GEOMETRIC_REPRESENTATION_CONTEXT",
    "REPRESENTATION_CONTEXT", "PARAMETRIC_REPRESENTATION_CONTEXT", "CAMERA_MODEL_D3",
    "SURFACE_SIDE_STYLE", "SURFACE_STYLE_FILL_AREA", "SURFACE_STYLE_USAGE", "CURVE_STYLE_FONT",
}


def _field_name(ent: Optional[str], idx: int) -> str:
    names = FIELDS.get(ent or "")
    if names and idx < len(names):
        return names[idx]
    return f"arg{idx}"


# ---------------------------------------------------------------------------
# Part 21 string codec
# ---------------------------------------------------------------------------
_DIRECTIVE = re.compile(rb"\\(?:X2\\((?:[0-9A-Fa-f]{4})+)\\X0\\|X4\\((?:[0-9A-Fa-f]{8})+)\\X0\\|X\\([0-9A-Fa-f]{2})|S\\(.)|P([A-I])\\|(\\))", re.S)


class StrStyle:
    __slots__ = ("raw8", "bs_literal")

    def __init__(self, raw8: bool, bs_literal: bool):
        self.raw8 = raw8            # non-ASCII written as raw UTF-8 bytes
        self.bs_literal = bs_literal  # lone backslashes written as-is (non-conforming writers)


def decode_p21(raw: bytes) -> tuple[str, StrStyle]:
    """Decode the content between the quotes of a Part 21 string."""
    if b"\\" not in raw and b"''" not in raw:
        if raw.isascii():
            return raw.decode("ascii"), StrStyle(False, False)
        try:
            return raw.decode("utf-8"), StrStyle(True, False)
        except UnicodeDecodeError:
            return raw.decode("latin-1"), StrStyle(True, False)
    raw8 = not raw.isascii()
    s = raw.replace(b"''", b"'")
    out = []
    pos = 0
    bs_literal = False
    page = 0
    for m in _DIRECTIVE.finditer(s):
        if m.start() > pos:
            seg = s[pos:m.start()]
            if b"\\" in seg:
                bs_literal = True
            out.append(_dec8(seg))
        if m.group(1) is not None:
            h = m.group(1)
            out.append("".join(chr(int(h[i:i + 4], 16)) for i in range(0, len(h), 4)))
        elif m.group(2) is not None:
            h = m.group(2)
            out.append("".join(chr(min(int(h[i:i + 8], 16), 0x10FFFF)) for i in range(0, len(h), 8)))
        elif m.group(3) is not None:
            out.append(chr(int(m.group(3), 16)))
        elif m.group(4) is not None:
            out.append(chr(m.group(4)[0] + 128))
        elif m.group(5) is not None:
            page = m.group(5)  # noqa: F841 (code page switch; latin-1 assumed)
        else:
            out.append("\\")
        pos = m.end()
    if pos < len(s):
        seg = s[pos:]
        if b"\\" in seg:
            bs_literal = True
        out.append(_dec8(seg))
    return "".join(out), StrStyle(raw8, bs_literal)


def _dec8(b: bytes) -> str:
    if b.isascii():
        return b.decode("ascii")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def encode_p21(text: str, style: StrStyle) -> bytes:
    out = []
    buf = []  # pending non-ASCII run for \X2\

    def flush():
        if buf:
            cps = [ord(c) for c in buf]
            if max(cps) > 0xFFFF:
                out.append("\\X4\\" + "".join("%08X" % c for c in cps) + "\\X0\\")
            else:
                out.append("\\X2\\" + "".join("%04X" % c for c in cps) + "\\X0\\")
            buf.clear()

    for c in text:
        o = ord(c)
        if c == "'":
            flush(); out.append("''")
        elif c == "\\":
            flush(); out.append("\\" if style.bs_literal else "\\\\")
        elif 32 <= o < 127 or c in "\t\r\n":
            flush(); out.append(c)
        elif style.raw8 and o >= 128:
            flush(); out.append(c)
        else:
            buf.append(c)
    flush()
    s = "".join(out)
    return s.encode("utf-8") if style.raw8 else s.encode("ascii")


# ---------------------------------------------------------------------------
# Record context
# ---------------------------------------------------------------------------

def record_context(prefix: bytes) -> tuple[Optional[str], list]:
    """Parse the bytes of a record before a string token.

    Returns (entity keyword of the innermost named group, path) where path is a
    list of (keyword|None, arg_index) from outermost to innermost."""
    stack: list = []
    last_kw = None
    m = _REC_HEAD.match(prefix)
    pos = m.end() if m else 0
    for t in _CTX_TOK.findall(prefix, pos):
        c = t[:1]
        if c == b"(":
            stack.append([last_kw, 0])
            last_kw = None
        elif c == b")":
            if stack:
                stack.pop()
            last_kw = None
        elif c == b",":
            if stack:
                stack[-1][1] += 1
            last_kw = None
        elif c == b"'" or t.startswith(b"/*"):
            last_kw = None
        elif c.isalpha() or c == b"_":
            last_kw = t.decode("ascii", "replace").upper()
        elif t.strip():
            last_kw = None
    ent = None
    for kw, _ in reversed(stack):
        if kw:
            ent = kw
            break
    return ent, stack


def describe(prefix: Optional[bytes]) -> tuple[str, str, Optional[str]]:
    """-> (entity, field name, alias kind or None)."""
    if prefix is None or len(prefix) > 65536:
        return "?", "?", None
    ent, stack = record_context(prefix)
    if not stack:
        return ent or "?", "?", None
    # argument index within the named entity
    idx = 0
    nested = False
    for i in range(len(stack) - 1, -1, -1):
        if stack[i][0]:
            idx = stack[i][1]
            nested = i != len(stack) - 1
            break
    e = ent or "?"
    f = _field_name(ent, idx)
    if e in PROTECTED_ENTITIES or (e, f) in PROTECTED_FIELDS:
        return e, f, "protected"
    kind = KIND_BY_FIELD.get((e, f))
    if kind is None and idx == 0 and e in NAMED_REPR and not nested:
        kind = "part_name"
    return e, f, kind


# ---------------------------------------------------------------------------
# Core streaming engine
# ---------------------------------------------------------------------------

class _Stream:
    """Iterate interesting tokens of a STEP file; emits passthrough and token events."""

    def __init__(self, f, chunk=CHUNK):
        self.f = f
        self.chunk = chunk

    def __iter__(self):
        """Yields (kind, buf, start, end, rec_start) with kind in
        'pass' (bytes buf[start:end] copied verbatim), 'str', 'cmt' (token buf[start:end]);
        rec_start is the record start offset in buf (or -1 when unknown)."""
        carry = b""
        eof = False
        while True:
            data = self.f.read(self.chunk) if not eof else b""
            if not data:
                eof = True
            buf = carry + data
            if not buf:
                return
            n = len(buf)
            pos = 0
            rec = 0
            # Mask doubled quotes (empty strings and escapes) and the commonest vocabulary
            # string; the remaining quotes pair up exactly like Part 21 string delimiters,
            # so a search for single-quoted runs finds only non-empty strings, at C speed.
            mk = buf.replace(b"''", b"\0\0").replace(b"'NONE'", b"\0NONE\0")
            last_e = 0
            for m in _MTOK.finditer(mk):
                s, e = m.span()
                if not eof and e >= n - 1:
                    break
                j = buf.rfind(b";", pos, s)
                if j >= 0:
                    rec = j + 1
                if s > pos:
                    yield "pass", buf, pos, s, -1
                yield ("cmt" if buf[s] == 0x2F else "str"), buf, s, e, rec
                pos = e
            if eof:
                if pos < n:
                    yield "pass", buf, pos, n, -1
                return
            # tail from pos holds no complete token; cut at the last ';' before any
            # unclosed quote or comment opener
            lim = n - 2
            q = mk.find(b"'", pos, lim)
            if q >= 0:
                lim = q
            c = mk.find(b"/*", pos, lim)
            if c >= 0:
                lim = c
            cut = buf.rfind(b";", pos, lim)
            cut = cut + 1 if cut >= 0 else pos
            if cut > pos:
                yield "pass", buf, pos, cut, -1
            carry = buf[cut:]
            if len(carry) > 512 << 20:
                raise ValueError("unterminated token over 512 MB")


_WS = b" \t\r\n"
_LEAD = re.compile(rb"(?:\s|\xef\xbb\xbf|/\*.*?\*/)*ISO-10303-21\s*;", re.S)


def _has_header(head: bytes) -> bool:
    """ISO-10303-21 signature, allowing a BOM and leading comments."""
    return bool(_LEAD.match(head))


def _delimited(buf, s: int, e: int) -> bool:
    """A real Part 21 string is preceded by '(' or ',' and followed by ',' or ')'
    (whitespace allowed).  Tokens produced by an unescaped quote in a malformed
    file fail this test and are never edited."""
    i = s - 1
    lim = max(0, s - 256)
    while i >= lim and buf[i] in _WS:
        i -= 1
    if i < lim or buf[i] not in b"(,":
        # header comments may precede a string: /* name */ 'x'
        if not (i >= 1 and buf[i] == 0x2F and buf[i - 1] == 0x2A):
            return False
    j = e
    lim = min(len(buf), e + 256)
    while j < lim and buf[j] in _WS:
        j += 1
    return j < lim and buf[j] in b",)"


def _is_ascii_file(path: str) -> bool:
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                return True
            if not b.isascii():
                return False


class StepAdapter(Adapter):
    name = "step"
    version = "4"
    priority = 10
    magics = ("step",)
    extensions = (".step", ".stp", ".p21")
    path_independent = False   # FILE_NAME.name / external references are mapped relative to the path
    timeout_s = 3600.0

    def matches(self, ctx, head):
        if ctx.magic == "step":
            return True
        return _has_header(head)

    # -- scan -------------------------------------------------------------
    def scan(self, ctx, src_path):
        col = ObsCollector(cap=3000)
        nstr = 0
        ents = 0
        malformed = 0
        header_ok = False
        with open(src_path, "rb") as f:
            head = f.read(64)  # noqa
            header_ok = _has_header(head + f.read(65536))
            f.seek(0)
            for kind, buf, s, e, rec in _Stream(f):
                if kind == "pass":
                    continue
                nstr += 1
                if kind == "cmt":
                    txt = _dec8(buf[s + 2:e - 2])
                    if any(ch.isalpha() for ch in txt):
                        col.add("free_text", "step.comment", txt.strip(), "comment", 0.5)
                    continue
                if not _delimited(buf, s, e):
                    malformed += 1
                    continue
                raw = buf[s + 1:e - 1]
                val, _ = decode_p21(raw)
                if not val.strip():
                    continue
                ent, fld, kind_ = describe(buf[rec:s] if rec >= 0 else None)
                if kind_ == "protected" or (ent in GEOMETRY and kind_ is None):
                    continue
                field = f"step.{ent}.{fld}"
                if looks_like_reference(val):
                    col.ref(val, ref_kind(val), field)
                    col.add("path", field, val, ent, 0.8)
                    continue
                col.add(kind_ or "free_text", field, val, ent, 0.9 if kind_ else 0.5)
        status = "ok" if header_ok and not malformed else "source_exception"
        notes = [] if header_ok else ["missing ISO-10303-21 header"]
        if malformed:
            notes.append(f"{malformed} string tokens with invalid delimiters (unescaped quotes in source)")
        if col.dropped:
            notes.append(f"observations capped ({col.dropped} more)")
        return ScanResult(observations=col.obs, references=col.refs,
                          fingerprint={"interesting_tokens": nstr},
                          status=status, notes=notes)

    # -- rewrite ----------------------------------------------------------
    def rewrite(self, ctx, src_path, dst_path, plan):
        pc = PlanCache(plan, ctx)
        changed = 0
        seen = 0
        malformed = 0
        cache: dict = {}      # (raw, kind) -> new token bytes
        tmp = dst_path + ".part"
        try:
            with open(src_path, "rb") as f, open(tmp, "wb", buffering=1 << 20) as out:
                w = out.write
                for kind, buf, s, e, rec in _Stream(f):
                    if kind == "pass":
                        w(buf[s:e])
                        continue
                    tok = buf[s:e]
                    seen += 1
                    if kind == "cmt":
                        new = self._sub_comment(tok, pc)
                    elif not _delimited(buf, s, e):
                        malformed += 1       # unescaped quote in the source: leave as is
                        new = tok
                    else:
                        ent, fld, kind_ = describe(buf[rec:s] if rec >= 0 else None)
                        if kind_ == "protected":
                            w(tok)
                            continue
                        key = (tok, kind_)
                        new = cache.get(key)
                        if new is None:
                            new = self._sub_string(tok[1:-1], kind_, pc)
                            if len(cache) < 200000:
                                cache[key] = new
                    if new != tok:
                        changed += 1
                    w(new)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        if changed == 0:
            os.unlink(tmp)
            return RewriteResult(status=VERIFIED_UNCHANGED, counts={"tokens": seen})
        os.replace(tmp, dst_path)
        notes = [f"{malformed} malformed string tokens left unchanged"] if malformed else []
        return RewriteResult(status=REWRITTEN, counts={"tokens": seen, "strings_replaced": changed}, notes=notes)

    @staticmethod
    def _sub_string(raw: bytes, kind: Optional[str], pc: PlanCache) -> bytes:
        val, style = decode_p21(raw)
        if not val.strip():
            return b"'" + raw + b"'"
        if looks_like_reference(val):
            new = pc.ref(val)
        elif kind:
            new = pc.sub(val, kind)
        else:
            new = pc.text(val)
        if new == val:
            return b"'" + raw + b"'"
        return b"'" + encode_p21(new, style) + b"'"

    @staticmethod
    def _sub_comment(tok: bytes, pc: PlanCache) -> bytes:
        body = tok[2:-2]
        if body.isascii():
            txt = body.decode("ascii")
            new = pc.text(txt)
            if new == txt:
                return tok
            nb = new.encode("ascii", "replace")
        else:
            try:
                txt = body.decode("utf-8"); encd = "utf-8"
            except UnicodeDecodeError:
                txt = body.decode("latin-1"); encd = "latin-1"
            new = pc.text(txt)
            if new == txt:
                return tok
            nb = new.encode(encd, "replace")
        nb = nb.replace(b"*/", b"* /")
        return b"/*" + nb + b"*/"

    # -- verify -----------------------------------------------------------
    def verify(self, ctx, src_path, dst_path, plan):
        if not os.path.exists(dst_path):
            return VerifyResult(ok=False, notes=["missing output"])
        a = masked_fingerprint(src_path)
        b = masked_fingerprint(dst_path)
        notes = []
        ok = True
        for k in ("masked_sha256", "strings", "records"):
            if a[k] != b[k]:
                ok = False
                notes.append(f"{k} differs")
        if a["ascii"] and not b["ascii"]:
            ok = False
            notes.append("output not ASCII")
        return VerifyResult(ok=ok, notes=notes, fingerprint=b)


# ---------------------------------------------------------------------------
# Independent verification tokeniser (different regex, search semantics):
# hashes every byte outside string tokens and comments.
# ---------------------------------------------------------------------------
_VTOK = re.compile(rb"'[^']*(?:''[^']*)*'|/\*.*?\*/", re.S)
_VBOUND = re.compile(rb";[ \t]*\r?\n[ \t]*#")


def masked_fingerprint(path: str, chunk: int = 32 << 20) -> dict:
    h = hashlib.sha256()
    nstr = ncmt = nrec = 0
    ascii_ = True
    carry = b""
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            buf = carry + data
            if not data:
                part, carry = buf, b""
            else:
                # cut at the last record boundary ";\n#" (cannot sit inside a valid string)
                cut = -1
                for m in _VBOUND.finditer(buf, max(0, len(buf) - (4 << 20))):
                    cut = m.start() + 1
                if cut < 0:
                    carry = buf
                    if len(carry) > 1 << 30:
                        part, carry = buf, b""
                    else:
                        continue
                else:
                    part, carry = buf[:cut], buf[cut:]
            if ascii_ and not part.isascii():
                ascii_ = False
            masked, k = _MTOK.subn(b"\x00", part.replace(b"''", b"\x01\x01"))
            nstr += k
            h.update(masked)
            nrec += masked.count(b";")
            if not data:
                break
    return {"masked_sha256": h.hexdigest(), "strings": nstr, "comments": ncmt,
            "records": nrec, "ascii": ascii_}


register(StepAdapter())
