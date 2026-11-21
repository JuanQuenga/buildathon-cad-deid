"""Generic (non-identifying) vocabularies: protected terms, placeholders,
host labels, org designators.  Matching LOGIC only; no corpus values.

All entries are compared after ``aliases.norm_key`` (casefolded tokens joined
by single spaces)."""
from __future__ import annotations

import os

from .aliases import norm_key

# Values that sit in identity fields but identify nobody.
PLACEHOLDERS = {norm_key(x) for x in """
unknown|none|null|nil|n/a|na|-|author|authors|user|users|owner|admin|administrator|root|default|
defaultuser|default user|guest|public|all users|someone|anonymous|organization|organisation|company|
company name|your company|your name|name|designer|engineer|drawn|checked|approved|title|untitled|
unnamed|test|tester|me|myself|nobody|no name|no author|not specified|unspecified|undefined|
open cascade|open cascade shape model|cad|step|iges|solid|part|part1|assembly|assembly1|
drawing|drawing1|sheet|sheet1|model|document|doc|file|project|desktop|documents|my documents|
downloads|temp|tmp|appdata|local|roaming|program files|windows|system32|share|shared|
everyone|staff|wheel|users group|local service|network service|system|
您的姓名|未指定|未知|无|匿名|管理员|默认|姓名|用户|微软用户|微软中国|微软|作者|公司|单位|ユーザー|ユーザ|名前|会社|benutzer|usuario|utilisateur|utente|
administrador|administrateur|amministratore|benutzername|nom|nombre|firma|societe|société|empresa|azienda|
config|configuration|config control|config control design|configcontroldesign|noname|no_name|nonamed|
design owner|design_owner|designowner|design supplier|design_supplier|creator|creators|approver|approverid|
approver id|approver_id|checker|checkerid|checker id|reviewer|drafter|draftsman|authorid|author id|
ownerid|owner id|designerid|designer id|creatorid|customer|client|vendor|supplier|manufacturer|
person and organization|person_and_organization|personandorganization|organization role|
id owner|part owner|creator role|classification officer|security classification|
""".replace("\n", "").split("|") if x.strip()}

# Software / CAD vendors and products.  They appear in format-required or
# producer fields (STEP originating_system, PDF /Producer, OLE AppName).
# A vendor name identifies the tool, not the corpus owner: kept verbatim.
SOFTWARE = {norm_key(x) for x in """
solidworks|solid works|solidworks corporation|dassault|dassault systemes|dassault systèmes|ds solidworks|
catia|3dexperience|enovia|delmia|abaqus|ptc|creo|creo parametric|pro/engineer|pro engineer|proe|wildfire|
parametric technology corporation|autodesk|autocad|inventor|fusion|fusion 360|fusion360|revit|3ds max|
siemens|siemens plm|siemens plm software|ugs|unigraphics|nx|solid edge|solidedge|parasolid|jt|teamcenter|
spatial|spatial corp|spatial corporation|acis|hoops|tech soft 3d|open cascade|opencascade|occt|
freecad|onshape|rhino|rhinoceros|mcneel|robert mcneel|grasshopper|sketchup|trimble|blender|openscad|
kicad|eagle|altium|cadsoft|librecad|qcad|draftsight|bricscad|zwcad|ironcad|keycreator|kubotek|
cadquery|build123d|salome|gmsh|ansys|comsol|nastran|patran|hypermesh|altair|msc|
adobe|acrobat|adobe acrobat|distiller|adobe pdf library|ghostscript|artifex|microsoft|microsoft word|
microsoft excel|microsoft office|word|excel|powerpoint|visio|libreoffice|openoffice|pdfcreator|
pdf24|bullzip|cutepdf|primopdf|nitro|foxit|pdfsharp|itext|reportlab|pdftex|latex|tex|cairo|skia|
quartz|mac os x|macos|apple|windows|linux|google|chrome|inkscape|gimp|imagemagick|qt|poppler|
st-developer|step tools|steptools|theorem|datakit|cadexchanger|cad exchanger|elysium|transmagic|
okino|polytrans|meshlab|cura|ultimaker cura|prusaslicer|slic3r|simplify3d|netfabb|materialise|
magics|3d builder|3mf consortium|3dsmax|maya|lightwave|cinema 4d|sketchfab|thingiverse|grabcad|
github|gitlab|bitbucket|sourceforge|google drive|dropbox|onedrive|
nvidia|jetson|jetson nano|jetson xavier|jetson orin|raspberry|raspberry pi|raspberrypi|arduino|arduino uno|
arduino nano|arduino mega|esp32|esp8266|stm32|teensy|beaglebone|realsense|intel realsense|kinect|dynamixel|
ros|ros2|nema17|nema 17|nema23|nema 23|intel|amd|xilinx|nucleo|pixhawk|ardupilot|px4|vesc|gopro|logitech|

iso|din|ansi|asme|jis|gb|bs|en|iec|astm|sae|nema|ieee|w3c|xmp|adobe xmp|
""".replace("\n", "").split("|") if x.strip()}

# Labels of a host name that identify nobody.
HOST_GENERIC = {
    "www", "www2", "www3", "ftp", "media", "files", "file", "cdn", "static", "assets", "img", "images",
    "docs", "doc", "download", "downloads", "dl", "content", "contenthub", "web", "m", "mobile",
    "api", "raw", "s3", "blob", "storage", "cloud", "prod", "dev", "en", "de", "fr", "us", "uk",
    "com", "org", "net", "edu", "gov", "mil", "int", "io", "co", "ac", "info", "biz", "eu",
}

# Org-form designators kept verbatim inside an aliased organisation name.
ORG_DESIGNATORS = {
    "inc", "corp", "corporation", "co", "company", "ltd", "llc", "llp", "lp", "plc", "gmbh", "ag", "kg",
    "sa", "sas", "sarl", "srl", "spa", "bv", "nv", "ab", "as", "oy", "pty", "pvt", "kk", "group",
    "university", "universitat", "universität", "université", "universidad", "universita", "institute",
    "institut", "laboratory", "laboratories", "lab", "labs", "college", "school", "foundation",
}

# Generic engineering / CAD / file vocabulary that must never be treated as an
# identity token on its own (multi-language, compact).
ENGINEERING = set("""
part parts asm assy assembly assemblies drawing drawings drw dwg sheet model models cad cam step stp iges igs
stl obj sat brep native neutral export exports import source sources src lib library libraries catalog
mechanical mechanics mech electrical electronics hardware design designs project projects prototype proto
version ver rev revision release master main dev develop branch final old new copy backup bak tmp temp test
tests sample samples example examples demo demos tutorial tutorials training doc docs documentation manual
bracket plate shaft gear gears bolt bolts nut nuts screw screws washer washers bearing bearings spring
housing cover base frame motor mount mounts holder clamp pin pins spacer flange pulley belt wheel arm leg
link joint axis axle tube pipe profile rail block body cap lid case box panel support stand top bottom left
right front back side upper lower inner outer center centre middle small large big mini micro std standard
metric imperial inch mm cm aluminum aluminium steel stainless brass plastic abs pla petg nylon wood rubber
iso din ansi asme jis en gb hex socket head flat round square cylinder cone sphere ring disc disk
robot robotics robotic machine machines tool tools fixture jig enclosure printer printed print printing
pcb board connector cable sensor camera lens optical optics laser led light lamp
pdf png jpg jpeg svg tif tiff zip tar gz rar 7z
modell zeichnung teil baugruppe welle zahnrad deckel platte halter schraube mutter lager
piece pieces plan plans assemblage assemblages moule
pcurve bspline nurbs spline polyline lwpolyline mtext text dimension hatch insert solid3d region body3d
vertex edge face shell loop wire surface curve point line circle arc ellipse plane axis placement
toroidal conical cylindrical spherical planar degenerate composite trimmed offset swept extruded revolved
brep facet facets mesh tessellated triangulated manifold advanced faceted product shape representation
dxf dwg iges igs stp step sat sab parasolid xt xb jt u3d stl obj vrml wrl ply fbx gltf glb usd ifc
endsec endsolid facetnormal outerloop endloop endfacet header data iso
dof nema gt2 gt3 m2 m3 m4 m5 m6 m8 m10 m12 rpi esp32 arduino raspberry pi
""".split())


def _load_wordlist():
    """English word list (dwyl/english-words 'words_alpha', Unlicense),
    shipped gzipped next to this module."""
    import gzip
    words = set()
    here = os.path.join(os.path.dirname(__file__), "data", "words_alpha.txt.gz")
    try:
        with gzip.open(here, "rt", encoding="utf-8", errors="ignore") as f:
            for line in f:
                w = line.strip().lower()
                if w.isalpha():
                    words.add(w)
    except OSError:
        pass
    return words


_WORDS = None


def english_words():
    global _WORDS
    if _WORDS is None:
        _WORDS = _load_wordlist()
    return _WORDS


def is_word(w: str) -> bool:
    """Plain dictionary word, with simple inflections."""
    w = w.lower()
    if w in ENGINEERING or w in HOST_GENERIC:
        return True
    W = english_words()
    if len(w) >= 4 and w in W:
        return True
    for suf in ("s", "es", "ed", "ing", "er", "ers", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 4 and w[: -len(suf)] in W:
            return True
    return False


_CAMEL_RE = __import__("re").compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+|[^\W\d_]+")


def subwords(tok: str):
    """Split a raw token on camelCase and letter/digit transitions."""
    return _CAMEL_RE.findall(tok)


def is_compound_of_words(w: str) -> bool:
    w = w.lower()
    for i in range(3, len(w) - 2):
        a, b = w[:i], w[i:]
        if (is_word(a) or a in ENGINEERING) and (is_word(b) or b in ENGINEERING):
            return True
    return False


def is_plain_token(tok: str) -> bool:
    """Generic at value level: digits, <=2 chars, or a plain word."""
    t = tok.lower()
    return t.isdigit() or len(t) <= 2 or is_word(t)


def is_generic_token(tok: str) -> bool:
    """Generic at sub-token level: also camelCase / compounds of words and
    engineering-word+digits (e.g. 'ToolChanger', 'benchtop', 'assembly2').
    A dictionary word glued to digits ('PAROL6', 'Atom01') is NOT generic:
    that shape is typical of product names."""
    import re as _re
    t = tok.lower()
    if is_plain_token(t):
        return True
    if _re.match(r"^\d+d[a-z]{3,}$", t) and (is_word(t.lstrip("0123456789")[1:]) or is_compound_of_words(t.lstrip("0123456789")[1:])):
        return True
    if _re.search(r"\d", t):
        alpha = _re.findall(r"[^\W\d_]+", t)
        return bool(alpha) and all(len(a) <= 2 or a in ENGINEERING for a in alpha)
    t2 = _re.sub(r"^\d+d(?=[a-z]{3})", "", t)  # '3dmodels' -> 'models'
    if t2 != t and (is_word(t2) or is_compound_of_words(t2)):
        return True
    parts = subwords(tok)
    if len(parts) > 1 and all(len(p) <= 2 or is_word(p) for p in parts):
        return True
    return is_compound_of_words(t)


def is_protected_value(key: str) -> bool:
    return key in PLACEHOLDERS or key in SOFTWARE
