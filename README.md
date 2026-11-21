# deid — CAD corpus de-identification pipeline

De-identifies a large engineering corpus (STEP, SAT, IGES, DXF/DWG, meshes, PDF, images,
SolidWorks/Inventor/Creo/CATIA natives, archives) with **consistent, typed pseudonyms** while
keeping every file usable in its native format and every cross-file reference pointing at the
renamed target. Built for the Builders Buildathon "CAD De-ID" (Machina) track. MIT licensed.

## How it works

```
manifest ─► scan (per-format adapters, process pool) ─► scan.sqlite (observations, references)
          ─► plan (deid.identity: discovery, typed pseudonyms, path map, reference resolution)
          ─► rewrite (adapters edit in place; every edit re-verified; failures fall back to the
                      original bytes) ─► stage/corpus
          ─► validate (coverage, limits, reference graph, multi-encoding leak scan)
          ─► package (attempt-N/{corpus,code,README.txt})
```

* `deid/api.py` — the adapter contract, registry and content sniffing (see `docs/INTERFACE.md`).
* `deid/pipeline/` — orchestrator: process pool with hard per-file timeouts and memory limits,
  content-hash caching (duplicates processed once), incremental re-runs keyed by
  (content, adapter version, plan version, output path), private sqlite state in the work dir.
* `deid/identity/` — identifier discovery, pseudonym plan, path map, reference resolver.
* `deid/formats/` — format adapters; `archives.py` handles zip / tar / tar.gz / gzip / `.Z` / 7z / rar
  recursively, routing every member through the same registry and writing each archive back in its
  own format (own LZW codec for `.Z`; RAR written as RAR 4.x stored entries).
* `deid/validate/` — the local acceptance gate run before any attempt.
* `tools/acquire.py` — verified, resumable parallel download of the input;
  `tools/upload_attempt.py` — preflight + wrapper around the organisers' `upload.py`.

**Golden rule** (enforced by the pipeline, not the adapters): every input file yields exactly one
output file. An adapter exception, timeout, crash or failed verification means the original bytes are
written to the mapped path and the event is recorded.

## Run

```bash
uv sync --frozen --extra dev
export DEID_MANIFEST=/path/to/manifest.tsv
python3 tools/acquire.py --manifest $DEID_MANIFEST --dest INPUT --jobs 16
.venv/bin/python -m deid.pipeline scan     --input INPUT --work WORK --jobs 6
.venv/bin/python -m deid.pipeline plan     --input INPUT --work WORK
.venv/bin/python -m deid.pipeline rewrite  --input INPUT --work WORK --out STAGE --jobs 6
.venv/bin/python -m deid.pipeline validate --input INPUT --work WORK --out STAGE --leak-scan full
.venv/bin/python -m deid.pipeline package  --input INPUT --work WORK --stage STAGE --out ATTEMPT
python3 tools/upload_attempt.py --attempt ATTEMPT --dest gs://.../attempt-N/ --plan-dir WORK/plan   # dry run
```

`--only-prefix P` / `--limit N` / `--skip-missing` restrict a run (e.g. to a sample).
`python -m deid.pipeline one FILE [--adapter NAME]` scans + rewrites one file for debugging.
`WORK/` holds private state (scan DB, plan with the pseudonym key, logs) and never goes into a submission.

## Dependencies

Python 3.13 (locked with `uv.lock`):

| Package | License | Use |
|---|---|---|
| ezdxf | MIT | DXF read/write |
| pikepdf (qpdf) | MPL-2.0 (qpdf Apache-2.0) | PDF structure |
| pdfminer.six | MIT | PDF font encodings / metrics |
| olefile | BSD-2 | OLE/CFB streams (SolidWorks legacy, Inventor, Solid Edge) |
| Pillow | MIT-CMU | images |
| lxml | BSD-3 | XML / SVG |
| numpy | BSD-3 | numeric checks |
| libarchive-c | CC0 | bindings to system libarchive (BSD-2): 7z/rar reading, 7z writing |
| pyahocorasick | BSD-3 | multi-pattern leak scan |
| pytest (dev) | MIT | tests |

System: libarchive ≥ 3.7, git, curl (acquisition), gzip(1) (fast `.Z` decoding; own LZW codec as
fallback), optional tesseract (Apache-2.0) for OCR of raster title blocks. SQLite via Python's stdlib.
No copyleft library is linked or shipped (a GPL DWG reader was used only as a local, unshipped checker).

Environment: `DEID_OCR_CACHE` (OCR each image once across scan and rewrite) and
`DEID_CONSTRAINTS_DIR` (log of references that could not be rewritten) — `tools/run_attempt.sh`
sets both under the work dir. `tools/run_attempt.sh INPUT MANIFEST WORK ATTEMPT [JOBS]` runs every
step end to end (resumable; never uploads).

## Tests

`.venv/bin/python -m pytest -q tests`
