# Benchmarks

## 280-file sample (all 100 suffix groups, 327 MB), 2026-11-20 19:44 ET

Machine: 16 logical cores shared with other jobs (load average ~30 during the run), Btrfs,
`--jobs 6`. Commit a65fac3 + adapters present at that time.

| Stage | Wall |
|---|---:|
| scan | 29.8 s |
| plan | 3.7 s |
| rewrite (incl. per-file verify) | 67.8 s |
| validate (full leak scan incl. decoded streams) | 21.3 s |

Per adapter (CPU seconds summed over workers; "(none)" = no adapter, copied by reflink):

| adapter | files | MB | scan s (cpu) | rewrite s (cpu) | rewrite MB/s/core | statuses |
|---|---:|---:|---:|---:|---:|---|
| (none) | 186 | 121.0 | 0.0 | 0.5 | 255.3 | copied_unsupported=179, source_exception=7 |
| zip | 7 | 68.0 | 31.1 | 86.2 | 0.8 | source_exception=1, verified_unchanged=3, rewritten=3 |
| 7z | 4 | 54.6 | 4.6 | 7.7 | 7.0 | source_exception=3, verified_unchanged=1 |
| tar | 3 | 28.1 | 0.7 | 1.9 | 15.0 | rewritten=2, verified_unchanged=1 |
| gzip | 7 | 20.0 | 37.2 | 81.2 | 0.2 | verified_unchanged=4, rewritten=3 |
| zippkg | 15 | 19.8 | 21.9 | 49.1 | 0.4 | rewritten=6, verified_unchanged=8, copied_unsupported=1 |
| solidworks | 23 | 7.8 | 2.6 | 11.6 | 0.7 | rewritten=23 |
| step | 6 | 1.7 | 0.1 | 0.6 | 2.9 | rewritten=5, verified_unchanged=1 |
| stl | 3 | 1.5 | 0.0 | 0.0 | 48.0 | verified_unchanged=3 |
| obj | 3 | 1.5 | 0.1 | 0.1 | 17.6 | verified_unchanged=3 |
| rar | 3 | 0.8 | 0.8 | 2.2 | 0.4 | verified_unchanged=2, rewritten=1 |
| pdf | 3 | 0.7 | 0.6 | 1.6 | 0.5 | rewritten=3 |
| dxf | 3 | 0.6 | 0.1 | 1.7 | 0.4 | rewritten=1, verified_unchanged=2 |
| vrml | 3 | 0.3 | 0.1 | 0.0 | 8.0 | verified_unchanged=3 |
| svg | 3 | 0.2 | 0.1 | 0.1 | 2.7 | rewritten=1, verified_unchanged=2 |
| sat | 3 | 0.2 | 0.0 | 0.1 | 3.4 | verified_unchanged=3 |
| compress | 3 | 0.1 | 0.1 | 0.3 | 0.4 | verified_unchanged=3 |
| off | 2 | 0.0 | 0.0 | 0.0 | 0.2 | verified_unchanged=2 |

Notes
* Archive cost is dominated by per-member overhead (one tgz has 41k members: ~2 ms/member for
  scan and ~2 ms for rewrite) and recompression; the rewritten archive is verified by re-listing
  and CRC-checking every member.
* Unchanged copies are reflinks (FICLONE) on Btrfs: no data written, ~0 s.
* Duplicate content (35k manifest MD5 groups) is scanned once; path-independent adapters rewrite
  identical content once and reflink the result.

## Partial-corpus trials (2026-11-20 ~20:30 ET)

* Plan build (`deid.identity.build_plan`) on a scan DB of 62k files / 978k observations / 134k
  references: 16 s.
* Rewrite of 1,500 small `sources/github/` files with 2 jobs: 100 s wall (16 f/s, ~8 f/s/job):
  rewritten 1,310, verified_unchanged 180, copied_unsupported 10, no fallbacks.
* Full scan, big-file phase (largest first, 6 jobs): 70-200 MB/s depending on machine load.

## Full corpus, attempt-1 (2026-11-20, 188,722 files, 139.7 GB)

Shared 16-core machine (load 7-35 from other jobs), Btrfs, frozen code (branch `attempt-1`).

| Stage | Wall | Notes |
|---|---:|---|
| scan (final pass, 6 workers) | 12.5 min | 107k files rescanned after adapter version bumps; earlier passes overlapped the download |
| plan | 3.5 min | 8,336 entries |
| rewrite | ~60 min | 4 then 8 workers (restarts kept finished files); peak ~290 files/s on small files |
| validate (8 workers, full leak scan incl. decoded streams) | 15 min | |
| package | 20 s | reflinked corpus |

Two catastrophic regex backtracking bugs (UNV numeric lines, STEP header sniff on C sources)
were found at full scale and fixed with possessive quantifiers; before the fixes single archives
held a worker for 20-60 min.
