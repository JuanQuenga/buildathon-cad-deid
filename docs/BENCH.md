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

## Full corpus

(to be filled after the first full run)
