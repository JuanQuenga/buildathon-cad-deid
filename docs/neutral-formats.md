# Neutral CAD and vector formats

Adapters in `deid/formats/` (owner: neutral).  All of them follow the same rules:

* **Surgical edits only.**  Files are tokenised with a syntax-aware lexer; only string
  tokens that can carry identity (names, free text, comments, file references) are passed
  to the plan.  Every other byte (numbers, entity ids, handles, whitespace, line endings,
  ordering, encodings) is copied verbatim.  Nothing is round-tripped through a CAD kernel.
* **Field-positional identities** (e.g. STEP `FILE_NAME.author`, `PERSON.last_name`, IGES
  Global author/organisation, DXF `$LASTSAVEDBY`) go through `plan.alias(value, kind)`
  first, then `plan.replace_text`.  File references go through `plan.map_reference`
  (falling back to `replace_text` when unresolved).  Everything else uses
  `plan.replace_text` on the decoded string, re-encoded in the file's own convention.
* **Format-required vocabulary is protected** (STEP schema/AP context/units/roles,
  DXF table types/class names/reserved names/structural markers, SAT kernel strings).
* **`verify` is strict**: it re-tokenises source and output and compares the
  string-masked token streams byte for byte plus format counters.  Any difference makes
  the pipeline copy the original instead.
* Streaming or mmap with bounded memory; the 1.42 GB STEP files peak at ~380 MB RSS.
* Content detection, not extension: each `matches()` checks a signature.

| Module | Formats | What changes | Verify |
|---|---|---|---|
| `step.py` | STEP AP203/214/242 (`.step .stp`) | quoted strings (Part 21 codec: `''`, `\\`, `\S\`, `\X\hh`, `\X2\..\X0\`, `\X4\`, raw UTF-8) and `/* */` comments; FILE_NAME/FILE_DESCRIPTION, PRODUCT, PRODUCT_DEFINITION(_FORMATION), NAUO, PERSON, ORGANIZATION, ADDRESS, DOCUMENT(_FILE), representation/body/layer/style names, property values | masked token stream hash (different tokeniser pass), string and record counts, ASCII preserved |
| `sat.py` | ACIS SAT text (all versions incl. 1.x header-less) | header producer string (counted), `@n` strings (>= 7.0, lengths updated), pre-7.0 counted strings in name/string/gssl attribute records | masked stream (strings + length prefixes masked), string count |
| `iges.py` | IGES 5.x fixed format | Start section, Global Hollerith fields (3-6, 12, 21-22...), Hollerith strings of 212/213/308/314/320/406/408/410/416 entities (212 NC counts updated); records re-flowed in place, or P section re-sequenced with DE pointers and T counts fixed | DE fields, non-string P tokens and Global numeric fields identical; T counts; 80 columns |
| `dxf.py` | ASCII DXF R12-R2018 | TEXT/MTEXT/ATTRIB/ATTDEF values, layer/block/style names (consistently everywhere incl. `$CLAYER`), XDATA strings, XRECORD values, IMAGEDEF/xref paths, dictionary path keys, selected header vars, 999 comments; codepage + `\U+XXXX` aware | code lines and non-editable values identical; pair/entity counts; ezdxf load summary (<= 8 MB) |
| `mesh.py` | STL binary/ASCII, OBJ, MTL, OFF, VRML 1/2 | STL 80-byte header (in place) and solid names; OBJ comments, o/g names, mtllib/usemtl (same mapping as MTL newmtl), map_* paths; VRML comments, strings, url/Inline refs, DEF/USE names (same mapping both sides) | binary STL body byte-identical; masked line streams; VRML DEF/USE pairing preserved |
| `svg.py` | SVG | text nodes, comments, CDATA, metadata (RDF/DC creator/publisher), name/label/title attributes, `sodipodi:docname`, export paths, hrefs (not `#fragments`, not `data:`) | markup with editable values masked identical; element count; still well-formed |
| `ps.py` | PS, EPS, DOS binary EPS | DSC comments (`%%Title/%%For/%%Creator`...), % comments, literal strings outside data regions (BeginData/Binary, top-level `currentfile` image data, eexec, ASCII85, AI9 data stream); DOS EPS offsets rewritten, previews copied | masked stream, string/comment counts, preview hashes |
| `parasolid.py` | Parasolid text header (`.x_t .xmt_txt`, also the text header of `.x_b`) | `KEY=value;` header fields (USER, SITE, MC_ID, KEY, FILE...) with 80-column re-wrap | body bytes identical, header keys identical |
| `unv.py` | I-DEAS universal | non-numeric records (model file name/description, group names, ID lines) | numeric records identical, line counts |
| `brep.py` | OCCT BRep | nothing (no text) - claimed as verified_unchanged | - |
| `scad.py` | OpenSCAD | comments, strings, include/use/import paths | code outside comments/strings identical |
| `textfallback.py` | Creo `.neu` text; any confidently plain-text file nobody else claims | `.neu`: non-numeric value lines and comments; free-form text (txt/csv/json/xml/...): `replace_text`; unknown text formats: length-preserving `replace_bytes` | numeric `.neu` records identical; line count; JSON/XML still parse; size unchanged in fixed mode |

Not claimed (pipeline copies, documented): binary DXF, compressed IGES, `.3ds` (1 file), SAB.

## Independent geometry check (OCCT)

`tests/formats_neutral/occt_check.py` rewrites each file with an *aggressive* stub plan
(every observed string replaced by a different-length pseudonym) and compares original and
output with OpenCASCADE 7.x (`cadquery-ocp`, used for validation only): transfer status,
root count, solids/shells/faces/edges/vertices, bounding box and volume.

| Run (2026-11-20) | Files | Rewritten | verify ok | OCCT summary identical |
|---|---:|---:|---:|---:|
| STEP, random corpus files < 8 MB | 60 | 60 | 60 | 60 |
| IGES, random corpus files < 8 MB | 30 | 29 | 29 | 29 (1 had nothing to change) |

## Corpus-scale runs (stub plan, all adapters, `verify` after every rewrite)

Random samples of already-downloaded corpus files (2026-11-20, ~half the corpus on disk),
aggressive stub plan, 6 worker processes on a shared 16-core machine.  "verify ok" counts
files whose rewrite passed the adapter's strict verify; the rest are files with nothing to
change.  CPU seconds are summed over workers.

| Formats | Files | Bytes | Rewritten + verify ok | Verify fail | Unchanged | CPU scan / rewrite / verify |
|---|---:|---:|---:|---:|---:|---|
| STEP `.step .stp` | 4,000 | 2.15 GB | 3,994 | 0 | 1 (+5 non-STEP text) | 71 / 85 / 99 s |
| SAT | 3,000 | 0.42 GB | 2,770 | 0 | 229 | 13 / 25 / 21 s |
| STL | 1,500 | 1.53 GB | 1,084 | 0 | 416 | 4 / 12 / 5 s |
| SVG | 1,500 | 0.14 GB | 1,195 | 0 | 305 | 16 / 23 / 43 s |
| VRML | 800 | 0.13 GB | 796 | 1 (stub-plan alias collision, falls back to copy) | 3 empty | 4 / 7 / 5 s |
| DXF | 1,502 | 0.25 GB | 1,376 | 0 | 125 | 18 / 27 / 219 s (ezdxf) |
| IGES | 688 | 3.84 GB | 681 | 0 | 6 (+1 non-80-column, copied) | 41 / 78 / 105 s |
| OBJ/MTL | 421 | 4.02 GB | 416 | 0 | 2 (+3 binary, not OBJ) | 46 / 41 / 74 s |
| PS/EPS | 378 | 0.02 GB | 378 | 0 | 0 | 1 / 3 / 1 s |
| UNV | 250 | 0.12 GB | 250 | 0 | 0 | 13 / 14 / 14 s |
| Parasolid text header | 400 | 0.32 GB | 400 | 0 | 0 | 2 / 2 / 1 s |
| .neu/.brep/.off/.scad | 155 | 0.03 GB | 79 | 0 | 50 (+21 binary .neu/.brp, not claimed) | 2 / 2 / 2 s |

Largest file (1.42 GB STEP assembly): scan 23 s, rewrite 26 s (54 MB/s), verify 70 s,
peak RSS 380 MB.  Extrapolated full corpus for these formats: about 2.5 CPU-hours
(scan + rewrite + verify), i.e. well under an hour on 6 cores.

Earlier runs found and fixed: unescaped quotes in a SolidWorks STEP export (tokens with
invalid delimiters are now never edited and the file is noted as a source exception),
ambiguous pre-7.0 SAT counted strings, Illustrator EPS private data and procedure
definitions that mention `currentfile`, and DXF structural markers (`CONTEXT_DATA{`).

## Identities typically carried (aggregate, from the pipeline scan DB)

* STEP: `FILE_NAME` name (original path, often a Windows user profile), author,
  organization, authorisation; `FILE_DESCRIPTION`; PRODUCT id/name/description (part
  numbers and names, ~95% of files); NAUO names and reference designators (assemblies);
  body/shape representation names; PERSON/ORGANIZATION records in AP203/AP214 exports
  (~25% of files); property values (`DESCRIPTIVE_REPRESENTATION_ITEM`); header comments
  with distribution URLs.
* IGES: Global author, organization, sender/receiver product ids, file name with path;
  406 property and 308 subfigure names; 314 colour names.
* SAT: producer string (sometimes with a source path), name/string attributes,
  AP203-derived person/organization attributes in old exports.
* Parasolid: USER, SITE, KEY, FILE header fields.
* DXF: `$LASTSAVEDBY`, text/MTEXT and title-block attributes, layer/block names, image
  and xref paths; SVG: Inkscape docname/export paths, RDF creator, text; PS/EPS:
  `%%For/%%Title/%%Creator`; OBJ/VRML/STL: exporter comments and object names; UNV:
  model file paths.
