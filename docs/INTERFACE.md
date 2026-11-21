# Interface contract (API v1)

The code lives in `deid/`. `deid/api.py` is the single source of truth for types;
this page explains how the pieces fit. Keep it stable: add optional fields only.

## Layout and ownership

| Path | Owner | What |
|---|---|---|
| `deid/api.py` | pipeline | types, `Adapter` base, registry, `sniff()` |
| `deid/pipeline/` | pipeline | orchestrator, worker pool, results DB, CLI |
| `deid/validate/` | pipeline | local acceptance gate |
| `deid/formats/archives.py` | pipeline | zip, tar, tgz, gz, .Z, 7z, rar |
| `deid/formats/{step,sat,iges,dxf,mesh,svg,ps,parasolid,unv,brep,scad}.py` | neutral | neutral CAD and vector formats |
| `deid/formats/{pdf,images,cfb,zippkg,dwg,solidworks,creo,catia,...}.py` | docs-native | documents, images, native CAD |
| `deid/identity/` | identity | identifier discovery, pseudonym plan, path map, reference resolution |
| `tools/` | pipeline | acquisition, benchmarks, upload wrapper |

Every module under `deid/formats/` is imported automatically by `deid.api.load_all()`.
A format module registers its adapter at import time:

```python
from deid.api import Adapter, register, ScanResult, RewriteResult, REWRITTEN, VERIFIED_UNCHANGED

class StepAdapter(Adapter):
    name = "step"; version = "1"; priority = 10
    magics = ("step",); extensions = (".step", ".stp")
    path_independent = True
    def scan(self, ctx, src_path): ...
    def rewrite(self, ctx, src_path, dst_path, plan): ...
    def verify(self, ctx, src_path, dst_path, plan): ...

register(StepAdapter())
```

An import error in one module does not stop the others (it is reported by `scan`).
Don't edit `deid/formats/__init__.py` beyond leaving it empty.

## Routing

`sniff(head_bytes, name)` gives a coarse content label (`zip`, `gzip`, `compress`, `7z`, `rar`, `tar`,
`cfb`, `pdf`, `png`, `jpeg`, `tiff`, `dwg`, `ps`, `eps`, `eps_binary`, `step`, `svg`, `xml`, `vrml`,
`brep`, `stl_ascii`, `stl_binary`, `parasolid_text`, `parasolid_binary`, `sab`, `jt`, `u3d`, `3ds`,
`creo`, `dxf_binary`, `text`, `binary`, `empty`). It is in `ctx.magic`. `route()` asks the adapters in
descending `priority` whether `matches(ctx, head)`; the default `matches` accepts a listed magic, or a
listed extension when the magic is only `text`/`unknown` (SAT, ASCII DXF, OBJ, IGES, UNV are plain text).
Override `matches` for anything subtler (e.g. a zip package adapter for `.fcstd/.3mf/.f3d/.odg` should
take `magic == "zip"` with those extensions at a priority above the generic zip adapter, which has
priority 0; generic archive handling never claims a zip with a package extension).

## Records

* `FileContext(logical_path, container_chain, magic, size, source_namespace, sha256, ...)`.
  Archive members have `logical_path = "<archive>!/<member>"`; nested: `a.zip!/b.tar!/c/d.step`.
  `ctx.member_name`, `ctx.ext`, `ctx.child(member)` help with that.
* `Observation(kind, field, value, evidence, confidence)`: `kind` from `api.KINDS` (open set),
  `field` dotted and adapter-namespaced (`step.FILE_NAME.author`, `pdf.info.Author`).
  Use `kind="free_text"` for human-entered text the identity miner should look at
  (descriptions, notes, title-block cells) when the adapter can't classify it.
* `Reference(raw, kind, field)`: exact spelling of a pointer to another file.
* `ScanResult(observations, references, fingerprint, status, notes, children)`;
  `fingerprint` is a JSON-serialisable dict of invariants (entity counts, page counts, bbox...)
  that `verify` can compare.
* `RewriteResult(status, counts, notes, children)`, status one of
  `rewritten`, `verified_unchanged`, `copied_unsupported`, `source_exception`.
* `VerifyResult(ok, notes, fingerprint)`.

Notes and counts must not contain raw identifiers: they feed `README.txt`.

## The plan

Built by identity from the scan DB; adapters only consume it (`api.Plan` protocol):

* `plan.version` — string; changes whenever any mapping changes (drives incremental re-runs).
* `plan.alias(value, kind=None) -> str | None` — pseudonym for an exact value, or None.
* `plan.replace_text(text) -> str` — replace all known identifiers inside free text.
* `plan.replace_bytes(data, encodings=("utf-8",), length_preserving=False) -> bytes`.
* `plan.map_path(logical_path) -> str` — same segment count and extensions; works on member paths.
* `plan.map_reference(ctx, raw_ref) -> str` — rewrite a reference keeping its spelling style.

Identity exposes two functions the pipeline calls:

```python
deid.identity.build_plan(scan_db: str, manifest: str, plan_dir: str) -> None   # `plan` step
deid.identity.load_plan(plan_dir: str) -> Plan                                  # in every worker
```

Until those exist the pipeline uses `api.NullPlan`.

## Pipeline flow and the golden rule

```
python -m deid.pipeline scan    --input IN --work WORK [--only-prefix P] [--limit N] [--jobs 6]
python -m deid.pipeline plan    --input IN --work WORK
python -m deid.pipeline rewrite --input IN --work WORK --out OUT/attempt-N [--only-prefix P]
python -m deid.pipeline validate --input IN --work WORK --out OUT/attempt-N
python -m deid.pipeline package --work WORK --out OUT/attempt-N --repo REPO
python -m deid.pipeline one --adapter step FILE   # debug: scan+rewrite+verify one file to /tmp-like dir
```

1. **scan**: every manifest file is sniffed, routed and `scan()`ned in a process pool with a per-file
   timeout. Results go to `WORK/scan.sqlite` (schema below). Scans are cached by
   `(sha256, adapter.name, adapter.version)` — paths don't change scan output except through
   references, which are stored per path.
2. **plan**: `deid.identity.build_plan(WORK/scan.sqlite, manifest, WORK/plan/)`.
3. **rewrite**: each file goes to `OUT/corpus/<plan.map_path(path)>`. The pipeline calls
   `adapter.rewrite()`, then `adapter.verify()` on REWRITTEN. **Any exception, timeout, verify
   failure or missing dst => the original bytes are copied to the mapped path** and the file is
   recorded as `fallback_copy`. Statuses other than REWRITTEN => the pipeline copies the original.
   An output always exists for every input. Results go to `WORK/results.sqlite`, keyed by path, with
   the key `(sha256, adapter, adapter.version, plan.version, path if not path_independent)`; a re-run
   only redoes files whose key changed.
4. **validate**: coverage, sizes, re-verify, reference graph, leak scan (see `deid/validate`).
5. **package**: `OUT/attempt-N/{corpus,code,README.txt}`.

## Scan DB schema (`WORK/scan.sqlite`)

```sql
files(path TEXT PRIMARY KEY, size INT, sha256 TEXT, magic TEXT, adapter TEXT, adapter_version TEXT,
      status TEXT, fingerprint TEXT /*json*/, notes TEXT /*json*/, error TEXT, seconds REAL)
observations(path TEXT, kind TEXT, field TEXT, value TEXT, evidence TEXT, confidence REAL)
refs(path TEXT, raw TEXT, kind TEXT, field TEXT)
```

`path` is the logical path (members: `archive!/member`). Indexes on `observations(value)`,
`observations(path)`, `refs(path)`. This DB is private (lives in `work/`), never in the repo.

## Archive members

`deid/formats/archives.py` extracts members to `ctx.tmp_dir`, builds `ctx.child(member)` contexts,
and calls `ctx.dispatch_scan` / `ctx.dispatch_rewrite`, which route members through the same
registry with the same golden rule. Member names are mapped with
`plan.map_path(member_logical_path)`. Package formats (FCStd, 3MF, ...) may use the same callbacks
for embedded files they don't understand themselves.

## Testing an adapter

`python -m deid.pipeline one --adapter NAME path/to/file` prints the scan result, rewrites with
`NullPlan` (or `--plan WORK/plan`) into a temp dir, runs verify and prints the outcome.
Sample files: `cad-deid/sample/files/` (private, outside the repo).
