# Documents, images and native CAD formats (docs-native adapters)

This page covers the adapters in `deid/formats/` for PDF, raster images, OLE/CFB
documents, SOLIDWORKS, zip-based packages, DWG, Creo, Inventor projects and the
opaque binary formats. It gives aggregate figures only: no corpus values appear
here or in the tests, which use synthetic fixtures in `tests/formats_native/`.

**Ground rule: the file must stay usable.** Every edit sits inside a structure
the adapter has parsed. Every `rewrite` is followed by a strict `verify`. When
verify fails, or the adapter raises, the pipeline copies the original bytes
(`fallback_copy`). Formats that can't be edited safely return
`copied_unsupported` but are still scanned, so their identifiers and references
reach the identity plan.

**Length-preserving plan.** The identity plan's aliases keep the length of the
original value: same character count and character classes. For a path
component the UTF-8 and UTF-16 byte lengths match too. Binary containers rely
on this. Strings inside SOLIDWORKS, CFB, DWG, Creo and U3D streams are replaced
in place, so no length field, offset, sector chain or page map ever changes.
When a test plan returns a different length, the adapters fall back to
`plan.replace_bytes(..., length_preserving=True)`. As a last resort they
truncate or pad the pseudonym. They never leave the original value.

## Coverage

| Format (suffixes) | Corpus objects | What is rewritten | What is only scanned or left alone |
|---|---:|---|---|
| PDF `.pdf` (`pdf.py`) | 10,342 | Info dictionary. Every XMP packet (document, page and image level). Every text string in the object graph: outlines, annotations, form fields, link URIs, file specs, optional-content names, `/Alt`, `/ActualText`, JavaScript. Page content streams, Form XObjects and annotation appearance streams: identity text is removed glyph by glyph, keeping positions with TJ kerning, and redrawn in standard-14 Helvetica (WinAnsi) at the same position, scaled horizontally to fit the original width. Attachments are recursed through the registry and renamed with `map_path`. `/PieceInfo` private data and `/Thumb` page thumbnails are dropped. Full rewrite: no incremental updates, unreferenced objects removed. OCR of logo and title-strip images and of scanned pages (`DEID_PDF_OCR=auto`, the default). Pages without a text layer (scans, and vector-outline text such as AutoCAD SHX output: about 16% of a random 300-PDF slice of the input) are rendered at 150 dpi and OCR'd. Each identity match is covered with a box in the local background colour and the pseudonym in Helvetica, placed rotation-aware, which also makes it extractable text. At most 3 pages and 30 s per PDF. | Vertical CJK fonts and fonts with a predefined multibyte CMap other than Identity/UCS2 (counted as `text_spans_unrewritable`). Text drawn as vector outlines. |
| Raster images `.png .jpg .jpeg .tif` (`image.py`, `_ocr.py`) | 1,348 | PNG tEXt/zTXt/iTXt/eXIf. JPEG COM, Exif (including IFD1 thumbnail tags and XP* UTF-16 tags), XMP and IPTC. TIFF ASCII tags. No pixel re-encode for metadata-only edits. Pixel text is OCR'd with tesseract; words the plan matches are painted over with the local background colour and the pseudonym is drawn in the original ink colour at the word's height. Size, mode, DPI and ICC are preserved. | Logos without text. Text OCR can't read. |
| SOLIDWORKS 2015+ `.sldprt .sldasm .slddrw .prtdot .asmdot .drwdot .sldlfp .slddrt` (`solidworks.py`) | 22,931 + templates | Chunk container: ROL-obfuscated names, raw-deflate payloads, trailing TOC. XML streams (`docProps/*`, `Config-N-Properties.xml`, `ISolidWorksInformation.xml`, `swXmlContents/*`): any value. Binary CArchive streams (CMgr, Header2/ModelHeader, Biography, DisplayLists, MatesList, SwDocContentMgr ...): every MFC unicode CString with an identity, at the same length. Component and drawing references follow `plan.map_reference`. Edited streams are re-deflated and zero-padded back to their original compressed span (span preservation, as validated against SOLIDWORKS by the Apache-2.0 swformat project); only `usz` changes, in the chunk header and its TOC record. `~$x.SLDPRT` lock files (they contain only a username) are rewritten too. | Preview bitmaps/PNG (part and assembly previews show geometry only). Drawing sheet previews (see Limitations). |
| OLE/CFB: Inventor `.ipt .iam .idw .ipn`, Solid Edge `.par .psm .asm .dft`, legacy SOLIDWORKS, NX `.prt` in CFB (`cfb.py`) | ~8,500 | Every `\x05` property-set stream (Summary, DocumentSummary with user-defined section, Inventor Design Tracking and User Defined iProperties, Solid Edge sets): VT_LPSTR and VT_LPWSTR values, parsed per MS-OLEPS and rewritten at the same byte count. Other uncompressed streams: UTF-16LE and ASCII text runs that contain an identity, same length. File references go through `map_reference`. Written in place with `olefile.write_stream` (same size), so the CFB directory, FAT and chains are untouched. | `*__ZLB` zlib streams (legacy SOLIDWORKS), previews, sheet images, Parasolid partitions and display caches: scanned (ZLB decompressed for scanning), not modified. |
| Zip packages: FreeCAD `.fcstd`, `.3mf`, ODF `.odg` and others, Fusion `.f3d .f3z` (`zippkg.py`) | 6,227 | Member-by-member rewrite that keeps order and names. Unchanged members are copied as raw bytes, so unknown compression methods such as Fusion's zstd survive. Changed members are re-encoded with their original method (stored/deflate). zip64 local, central and end records are handled. XML members: text nodes and attribute values (FreeCAD internal object `name`s kept; `XLink file=` follows `map_reference`). JSON/INI/text members: `replace_text`. Fusion metadata `.dat`: uint32-length-prefixed strings, same length. Embedded images and nested packages are recursed through the registry. Mesh and slice payloads in big 3MF models are skipped for speed. | zstd members (Fusion geometry). Member names. |
| DWG R2004 family `AC1018 AC1024 AC1027 AC1032` (`dwg.py`) | ~700 of 758 | Own reader for the R2004 container, written from the ODA public specification: encrypted header, page map, section map, encrypted data-page headers, LZ77 decompress plus a compatible compressor, page checksums. `AcDb:SummaryInfo` (title, subject, author, keywords, comments, last saved by, hyperlink base, custom properties) rewritten at the same length; the data and header checksums of that page are recomputed (scheme confirmed on the originals); every other byte is identical. | Drawing entities (TEXT/MTEXT/ATTRIB in `AcDb:AcDbObjects`: these are scanned and reported). `AcDb:Preview`. R13–R2000 (`AC1012–AC1015`) and R2007 (`AC1021`): copied unchanged. |
| Creo / Pro/E `.prt .asm .drw .frm .lay .sec`, with or without `.N` (`creo.py`) | ~16,000 | `#UGC` ASCII header only: HOST machine name and CMNM model name, same length. CMNM follows the mapped file name, so a kept stem stays equal. | Sections use proprietary compression with per-section checksums in the TOC. Component references live there, so identity keeps the stems of Creo models that sit next to an assembly or drawing. |
| Inventor project `.ipj` (`ipj.py`) | 15 | UTF-16/UTF-8 XML values; workspace and library paths follow `map_reference`. | – |
| Opaque: JT, U3D, CATIA V5, NX, `.mf1`, `.brp`, Parasolid binary when no better adapter claims it (`opaque.py`) | ~2,600 | U3D: block structure parsed; U16-length-prefixed strings in block data and metadata, same length (ECMA-363 has no checksums). | JT, CATIA, NX, mf1, brp: scanned only (strings, contacts, CAD file references). |

## Verify (what "ok" means)

- **PDF**: reopens with qpdf/pikepdf; same page count; same MediaBox and CropBox on every page; first and last pages render with poppler (unless the source didn't render either); text still extracts. Pages where the plan still finds identities are reported as a note.
- **Image**: decodes; same size, mode, format, frame count and ICC profile.
- **SOLIDWORKS**: same file size; same chunk list (offset, compressed size, name) and same TOC record list; every edited chunk inflates to exactly its new `usz`; TOC `usz` matches; XML streams that parsed still parse; binary streams keep their length; no byte outside payloads changes except the `usz` fields.
- **CFB**: same file size; same directory (storages and streams); same stream sizes; edited property sets re-parse to the same section, property-id and type layout; compressed streams unchanged.
- **Zip packages**: same member names, order and methods; every member passes its CRC check; members we can't decode (zstd) are byte-identical; XML members still parse; ODF `mimetype` stays stored and first. Independently, `unzip -t` passed on all 114 rewritten sample packages.
- **DWG**: same size; output re-parses; same page map and section map; header fields of every page unchanged; the data checksum of every changed page matches and the page still decompresses; SummaryInfo re-reads; no byte outside the SummaryInfo page changes. Independent check during development: GNU LibreDWG 0.13.3 `dwgread` (built locally as a validation tool, never linked or shipped) exits with the same code on all 57 rewritten samples as on their originals, reports no new checksum warnings, and returns the new SummaryInfo values.
- **Creo**: same size; only header bytes differ; same header line count.
- **U3D**: same size and identical block layout.

## Measured results

Measured on the downloaded native samples (about 1,480 files: ≥ 50 per major format, plus co-located assembly, drawing and part directories). Also on a random slice of 2,808 files from the real input, about 120 per suffix group, using the identity dev plan.

| Adapter | Sample: rewritten / verify ok | Input slice: rewritten / verify ok | Rewrite time, p50 |
|---|---|---|---|
| pdf | 101 / 101 (auto plan), 79 / 79 (dev plan) | 100 / 100 and 118 / 118 (second slice); 115 / 115 on another 150 random input PDFs | 21.6 pages/s per core for rewrite + verify (1,137 pages in 52.7 s, auto OCR on) |
| solidworks | 197 / 197 (auto), 164 / 164 (dev); 0 references left unrewritten with the dev plan | 405 / 405 and 465 / 465 | 0.02–0.05 s (drawings with preview OCR ~0.7 s) |
| cfb | 287 / 287 | 547 / 549 and 758 / 760 (the same NX `.prt` pattern: a truncated last sector grew on write; verify caught it; fixed by truncating back and re-reading the streams) | 0.02 s |
| zippkg | 114 / 114 | 134 / 134 (3 large 3MF hit the old 120 s budget; fixed: 75 s → 4 s) and 167 / 167 with no timeouts | 0.07 s |
| image | 23 / 23 (40 OCR regions) | 20 / 20 and 46 / 46 | 1.0 s with OCR; mean 1.35 s per image |
| creo | 15 / 15 | 44 / 44 and 56 / 56 | < 0.01 s |
| dwg | 57 / 57 (112 SummaryInfo fields); LibreDWG `dwgread` agrees on 57 / 57 | all 729 input DWGs: 421 rewritten, 421 / 421 verify; 148 nothing to change; 154 unsupported version (R2000/R2007), 5 multi-page SummaryInfo, 1 undecodable page: copied | ~2 ms |
| ipj | synthetic test only (samples held no plan match) | 0 / 0 | – |
| opaque | scan only (U3D synthetic test) | – | – |

OCR cost is tesseract 5 at `--psm 11` with `OMP_THREAD_LIMIT=1`, images downscaled to at most 3,500 px on the long side. That's about 1–5 s per drawing-sized image (3,300 × 2,300). Set `DEID_OCR_CACHE=<dir>` so scan and rewrite OCR each image only once. Scan-time OCR is off by default (`DEID_IMAGE_OCR_SCAN=1` turns it on); rewrite-time OCR is on (`DEID_IMAGE_OCR=0` turns it off). For images embedded in PDFs, `DEID_PDF_OCR=auto` is the default. It OCRs logo- and title-strip-shaped images (aspect ≥ 2, ≤ 3 MP) and every image on pages with no text layer, at most 8 images and 30 s per PDF. On the sample this costs 0.47 s per PDF on average (worst 7 s); `all` costs 3.7 s per PDF; `0` turns it off. Matching is OCR-tolerant: confusable glyphs (I/l/1, O/0, rn/m ...) are normalised, and near matches of plan entries (≥ 8 characters, ratio ≥ 0.9) are accepted. Drawing sheet previews of SOLIDWORKS `.slddrw` files (modern `PreviewPNG`/`Preview` DIB, legacy CFB `PreviewPNG`/`Preview`/`Images/Sheet_N`) go through the same OCR redaction. A re-encoded preview is written only if it fits the original span (modern) or byte size (CFB, PNG zero-padded after IEND).

## References (`scan` output) and the fixed-length rule

Every reference found in a binary container is reported with `Reference.field` ending in `#fixedlen`, for example `sw.Contents/CMgr#fixedlen`, `cfb.UFRxDoc#fixedlen` or `creo.header.CMNM#fixedlen`. Kinds: `component` (assembly → part), `xref` (drawing → model, DWG xrefs and images), `other` (history/biography). Such a reference is rewritten only when `plan.map_reference` returns the same number of characters; identity guarantees this. If it doesn't, the reference is kept and a constraint record is written. **Constraint records** go to `$DEID_CONSTRAINTS_DIR/native-<pid>.jsonl` (private; suggested location `cad-deid/work/constraints/`), one JSON object per line:

```json
{"v": 1, "source": "<logical path of the referencing file>", "raw": "<reference as stored>",
 "keep": "basename", "reason": "<neutral text>", "format": "solidworks|cfb|..."}
```

Per-format reference update capability:

| Format | References rewritten |
|---|---|
| SOLIDWORKS 2015+ | yes: every CString path in every binary stream, plus XML streams (same length) |
| Legacy SOLIDWORKS, Inventor, Solid Edge (CFB) | yes in uncompressed streams and property sets (same length); not inside `__ZLB` streams |
| FreeCAD | yes (`XLink file=`, any length) |
| 3MF, ODF | yes (XML values, any length) |
| PDF | link URIs, file specs, attachment names |
| Inventor `.ipj` | yes (any length) |
| DWG | no (xrefs and image paths sit in object data: scanned only) |
| Creo, CATIA, JT, Parasolid binary, NX | no: identity keeps the stems of Creo and CATIA models next to an assembly or drawing |

## Limitations (what stays unrewritten and why)

- **DWG drawing content.** TEXT, MTEXT and attribute values (title blocks) live in `AcDb:AcDbObjects`: bit-packed objects with a CRC per object. A safe same-length edit there would also need the object map and those CRCs to be recomputed. They are reported to identity but not changed. Measured on 120 random input DWGs (decoded with LibreDWG for measurement only), 14 have entity text that contains a plan identity: an estimated ~90 DWGs corpus-wide. The preview thumbnail isn't changed either. DWG versions before R2004 and R2007 (`AC1021`) are copied unchanged.
- **Creo, CATIA, JT, NX and Parasolid-binary sections** are compressed or checksummed with undocumented schemes. Only the Creo header and U3D strings are edited.
- **SOLIDWORKS drawing sheet previews** are OCR-redacted only when the re-encoded image fits its span or size. In the sample 2 of 10 matching previews didn't fit and were left unchanged. Part and assembly previews aren't processed.
- **CFB `__ZLB` streams** (legacy SOLIDWORKS display lists and DWG header copies) aren't modified. A recompressed stream would need a different size.
- **Fusion 360 zstd members** aren't decoded (no zstd codec in the stdlib of the pinned Python). That affects about 1 in 8 `.f3d` files (newer Fusion); their deflate members are still processed.
- **Package members over 512 MB** (one 1.4 GB 3MF model in the input) are copied unchanged.
- **PDF**: text drawn as vector outlines is covered visually by the OCR overlay, but its path geometry stays in the content stream underneath (it is not text, so text search can't find it). Type3 and vertically written CJK spans with unusual encodings can't be rewritten.
- **OCR** depends on recognition quality. A misread name isn't matched, so its pixels stay unchanged.
- **Usability.** No proprietary CAD application was available for reopen tests on Linux. Container-level validation (re-parse, sizes, checksums, CRCs, XML parse) is the acceptance gate.

## Dependencies and tools

`pikepdf` (MPL-2.0, qpdf Apache-2.0), `pdfminer.six` (MIT: encodings, glyph list, standard-14 metrics), `olefile` (BSD), `Pillow` (MIT-CMU), `lxml` (BSD), `numpy` (BSD). External executables, never linked: `pdftotext`/`pdftoppm` (poppler, GPL; used only for checks), `tesseract` (Apache-2.0). The SOLIDWORKS container layout follows the findings of the Apache-2.0 swformat project (reimplemented, not vendored). The DWG container follows the ODA "Open Design Specification for .dwg files".
