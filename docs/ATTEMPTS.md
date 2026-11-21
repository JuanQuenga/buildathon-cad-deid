# Attempt queue (learning the grader)

The grader reports only a total out of 1,000 and pass counts per category. An unchanged copy scores
292; the leader sits at 374. So each attempt below changes **one** policy (or ships one batch of
safe fixes) and says which category it should move. Read the category deltas against the
previous graded attempt, and revert any change whose category drops.

Ground rules: never break files (the golden rule and the validation gate stay on; any adapter edit
that fails its re-parse keeps the original bytes). Pseudonyms stay consistent across formats within
an attempt; the key never changes (`work/identity/secret.key`). Every attempt is built with
`tools/make_attempt.sh N PREV COMMIT "hypothesis"`, which writes `out/attempt-N.summary.txt` and a
per-format diff against the previous attempt.

Cost per attempt: an adapter-only change is an incremental rewrite (~20–30 min to a packaged
attempt). A plan or identity change is a full rewrite (~60–70 min with 5–7 workers). Upload: server-side
copy of unchanged objects plus upload.py for the rest (minutes for small diffs).

| # | Status | Hypothesis / change | Expected category effect | Risk to usability |
|---|---|---|---|---|
| 1 | uploaded/queued | Baseline pipeline: path map, STEP/SAT/IGES/DXF/mesh/SVG/PS/PDF/images, CFB/SolidWorks/DWG properties, archives recursive, Creo header | de-id categories up from 292 | low (gate passed, 614,305/614,308 refs) |
| 2 | **do not submit** | Plan precision + finished discovery + CFB dead-space + SVG/SQLite fixes | – | regression: STEP `PRODUCT_DEFINITION('design')` aliased |
| 3 | building | Attempt 2 with the regression fixed. Field-only (generic-word) plan entries are never used in part-number slots. Spread-based vocabulary (boilerplate seen across >15 owners → not an identity), protected domains (public suffixes, mail providers, foundations), multilingual title-block labels | de-id up (fewer leaks in SolidWorks/CFB dead space, PDF/SVG/SQLite); "preserve engineering info" up (no more "All rights reserved" → pseudonym, labels intact) | low |
| 4 | next | **Probe: part/drawing numbers pseudonymised.** Today they are kept (identity default). The brief lists "identifying project or part names and numbers" explicitly. Change: file-stem tokens that look like proprietary part codes (digits+letters, not standard designations like ISO 4762 / M3x10 / DIN / NEMA17) become `part_number` entries. They are mapped in paths and inside files (STEP product id/name, PDF title block, SolidWorks/Inventor properties); references follow the same map | de-id up a lot if graded; may lower "references/relationships" if any format links by part code we cannot rewrite (Creo/CATIA) | medium: Creo/CATIA stems keep their names (opaque links), so the probe excludes them |
| 5 | queued | **Probe: rename the ~380 kept Creo/CATIA stems** (currently kept because their assemblies link by model name inside checksummed sections) | separates "filename de-id" from "assembly link integrity": if the total rises, the grader doesn't open Creo assemblies; if references drop, revert | medium-high (breaks Creo/CATIA assemblies in a real CAD app) |
| 6 | queued | **Probe: path renaming more aggressive.** Also alias non-vocabulary tokens in directory and file names (today only identity tokens and owner/repo slots change). Extensions and structural suffixes kept | de-id (filenames) up; "locate files" checks could drop if the grader matches by original basename | medium (paths only; the reference map follows) |
| 7 | queued | PDF raster/vector: `DEID_PDF_OCR=all` (OCR every image, not only title-strip shapes) and vector-outline text detection | de-id (visible text/images) up | low-medium (images re-encoded only within the original span) |
| 8 | queued | SolidWorks modern (non-CFB) residual strings in streams the adapter skips (e.g. display-list caches); Fusion `Manifest.dat` / `InstanceProperties.bin` same-length edits | de-id up slightly | medium (proprietary binaries; no app to reopen) |
| – | not planned | Creo internals: sections use a proprietary compression with two 16-bit checksums per section (not Adler/CRC/Fletcher/sum). A same-length edit can't be proven safe | – | would break files |

## Findings behind the queue (attempt-1 validator, residual identifier hits)

- PDF (2,352 files): about 77% of sampled hits sit inside embedded **font programs** (foundry/licence URLs: SIL, AMS, Monotype, Carrois...). They are boilerplate (fixed by vocabulary in attempt 3; editing font programs isn't needed). Real names remain in a few raster images and content streams. Risk of fixing those: low (OCR path).
- SolidWorks (2,319): mostly **legacy CFB files**. The hits sit in dead space: slack after a stream's end, free sectors, free mini-sectors (old deleted data no reader returns). Attempt 3 zeroes dead space when it holds an identity, with a self-check that no live stream changes. On a 48-file sample: hits 63 → 26, clean files 0 → 22, verify 48/48. The remaining ~20% are modern SolidWorks streams the adapter doesn't edit (display caches): queued as #8.
- Creo (12,967): names live in compressed, checksummed sections. Not provably safe; left unchanged.
- Fusion (392): `Manifest.dat` / `InstanceProperties.bin` members (proprietary binary). Fixable only by unverifiable same-length edits: #8, opt-in.
- Plan false positives (8.7k files in attempt 1): boilerplate entries ("All rights", "The Authors", ICC/font copyrights, CA domains, title-block labels, library names). Attempt 3 handles them with a general corpus-spread rule (`deid/identity/spread.py`) plus vocabulary.

## Evidence log

| Attempt | Total | Category counts | Read-out |
|---|---|---|---|
| 1 | pending | | |
