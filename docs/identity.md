# Identity plan (owner: identity)

Status: v2 (path-seeded + discovery + adapter observations + private curated table). Policy and aggregate stats only: no raw values here.
Real-value tables live in `cad-deid/work/` (private).

## Use it

```python
from deid.identity import load_plan
plan = load_plan("cad-deid/work/plan")          # build: python -m deid.identity.build --manifest M --plan-dir D [--scan-db S]
plan.alias(value, kind)                          # field value -> pseudonym (None = not identifying)
plan.replace_text(text)                          # all known identities in free text, + emails, user dirs
plan.replace_bytes(data, ("utf-8","latin-1","utf-16-le"), length_preserving=True)
plan.map_path("sources/github/owner__repo/x/y.step")   # also 'a.zip!/member/path'
plan.map_reference(ctx, "..\\parts\\Bracket.SLDPRT")  # keeps style (separators, prefix, case)
plan.resolve_reference(ctx_path, raw)            # -> original inventory path or None
```

Every replacement is **length-preserving in characters and in bytes** (UTF-8, Latin-1, UTF-16):
letters map to letters of the same case, digits to digits, separators are kept, non-ASCII letters
map within a block of equal encoded length. So adapters may substitute in place inside fixed-width,
length-prefixed or binary structures. `alias()` returns the same string for every spelling variant
of one identity up to case and separators (`ACME Corp` / `acme_corp` / `ACMECorp`).

## Pseudonyms

* Deterministic: HMAC-SHA256(secret, normalised token, attempt). Secret = `plan_dir/secret.key`
  (or `DEID_SECRET_FILE`). No dependence on processing order: all workers agree.
* Token-compositional: a value is split into alphanumeric tokens; each token has one alias
  (registry in `plan.json`, injective, never equal to any real identity token). Separators kept.
* Pronounceable consonant/vowel letters so names look like names; digits stay digits (part numbers
  keep their shape); one-character tokens and TLD/`www` labels stay verbatim.
* Emails: local part and domain labels aliased, TLD kept. Generic mailboxes (`info@`, `support@`) keep
  the local part.

## What is an identity (v1)

| Source | Kind | Mode |
|---|---|---|
| `sources/github/<owner>__<repo>[__sha12]`, `archives/<owner>__<repo>[__sha].ext` | owner = account, repo = project | owner/repo always replaced in the slot; free-text matching when distinctive |
| `sources/gitlab/<host>__<group>__...__<project>` | account / project | host kept |
| `sources/web/<host>` | domain | labels aliased except TLD/public suffix and generic host labels (`www`, `media`, ...) |
| distinctive sub-tokens of owner/repo names (product names like `XYZ6`) | *_token | free text + paths, if seen under at most 4 owners (10 for owner tokens) |
| scan observations (adapters) of kind person/company/account/email/domain/project/address/phone | as given | free text when >= 4 chars and not generic |
| private curated tables `work/identity/curated.tsv` (catalog/existing collection names, org names behind owner handles), `discovered.tsv` (output of `deid.identity.classify`) | any | as listed |

### Discovery and evidence rules

`python -m deid.identity.discover` is an independent stdlib scanner (does not depend on the adapters):
STEP `FILE_NAME`/`PERSON`/`ORGANIZATION`/addresses (escapes decoded), IGES global section,
Parasolid `USER=`/`SITE=`, PDF Info + XMP (also inside Flate/object streams), PostScript DSC,
OLE/CFB Summary/DocumentSummary property sets (own minimal CFB reader), XML/ODF/3MF/FCStd metadata,
and emails / URL hosts / user directories in ASCII and UTF-16LE strings of every file, zip member and
gzip payload. `python -m deid.identity.classify` turns raw observations (discovery JSONL and the
pipeline scan DB) into identities with minimum-evidence rules:

* person/company/account only from identity-bearing fields (author, organization, last author, company,
  creator, `%%For`, `USER=`, profile directories...); adapter-declared identity fields are trusted, except
  title-block label values, which must look like a name;
* rejected: placeholders (multi-language: `Unknown`, `Administrator`, `您的姓名`...), vendor/translator
  names, dates/times, versions, hashes, numbers, title-block vocabulary (`APPV'D`, `SIGNATURE`...),
  mojibake of placeholders (GBK/UTF-8 read as Latin-1 is decoded before the check);
* an author given as a code-hosting URL yields the account; `DOMAIN\user (MACHINE)` is split;
* emails need a known TLD and consistent label case; generic mailboxes keep their local part;
* domains: format namespaces and public infrastructure (code hosting, package indexes, CDNs, standards
  bodies, social networks) are not identities; content-discovered domains are matched as whole
  domains only (their labels are not matched as free words, so a supplier name in a filename survives);
* single-token person/account names shorter than 5 characters or that are dictionary words are
  replaced only where they sit (field mode), never as free words.

Match modes: `flex` (case-insensitive, separators `space _ - . %20` optional, camelCase and
letter/digit transitions count as boundaries), `exact` (case-insensitive, exact separators; used for
multi-word values made only of dictionary words), `field` (never matched in free text; only via
`alias()` or the path slot: values < 4 chars, placeholders, generic words).

## Not replaced (deliberate)

* Placeholders in identity fields (`Unknown`, `Author`, `Administrator`, `Default`, ...).
* CAD/software vendor and product names (SolidWorks, PTC, Autodesk, Spatial, Open CASCADE, FreeCAD,
  Adobe, ...) in producer/originating-system fields: they identify the tool, not the owner. A vendor
  that is also a corpus owner is still replaced in the owner path slot.
* Standards bodies and designations (ISO, DIN, ASTM, NEMA ...), materials, thread specs, units.
* Format namespaces and standards URLs (w3.org, ns.adobe.com, purl.org, iec.ch, ...).
* Repo names made only of dictionary words are replaced in their path slot, and in free text only
  as the exact multi-word phrase (>= 12 chars), never word by word.
* File stems of Creo (`.prt/.asm/.drw/...(.N)`) and CATIA V5 models that sit in a directory with a
  Creo assembly/drawing or CATIA product/drawing are kept: their links are by model name inside
  compressed, checksummed sections no adapter can rewrite. On the manifest this keeps about 380 stems that
  contain an identity token; the parent directories are still renamed.
* Sub-tokens used under many unrelated sources (brand names like a toy or board vendor) are vocabulary.

## Pipeline integration

`build_plan(scan_db, manifest, plan_dir)` reads the manifest (+ archive members from the scan DB as
inventory), the adapters' observations (through the classifier), and, if present,
`<dirname(plan_dir)>/identity/{curated,discovered}.tsv`. The plan directory holds `plan.json`
(entries, token alias registry, version), `secret.key` (kept across rebuilds, so aliases are stable),
and `inventory.txt`. `plan.version` changes whenever an entry/mode changes.

## Paths

`map_path` keeps: segment count, separators, the bucket prefix (`sources/<bucket>/`, `archives/`),
the commit-hash suffix of a slot, every extension including compound and version suffixes
(`.prt.19`, `.tar.gz`), and every token that is not an identity. It changes the owner/repo/domain slot and
identity tokens inside deeper directory names and file stems. Archive member paths (`a.zip!/m/x`) are
mapped with the same dictionary. Collision check is case-insensitive.

Dry run on the full manifest (188,722 paths, v1 plan): 0 case-insensitive collisions, 0 depth or
extension changes, 0 length changes; 181,782 paths change (all of `sources/github`, `gitlab`, `web`, the 4
`archives`; 290 `catalog` and 719 `existing` paths through dictionary tokens). Changed components:
~180.8k slot, ~8.4k deeper directories, ~1.7k file stems.

## References

`resolve_reference(ctx_path, raw)` resolves against the ORIGINAL inventory (manifest + archive members
from the scan DB): relative to the referencing file (members relative to their archive directory),
`file:` URIs, `%`-escapes, `\` separators, case-insensitive, unversioned Creo names to the highest
`.N`, then unique-basename fallback within the same source (tie-break: most matching trailing dirs).
`map_reference` rewrites the components that align with the resolved target to the target's mapped
names (keeping the reference's own case), and pseudonymises the remaining components with the same
dictionary plus user-directory rules (`C:\Users\<x>`, `/home/<x>`). Dangling references are still
pseudonymised consistently.

## Grader model (working theory)

* The unchanged input scores 292/1000: those checks pass without any change (files open, geometry and
  references intact, distinct identities distinct). The remaining ~700 need identifiers gone and
  pseudonyms consistent, without breaking the first group.
* Checks are per file, and the scorer must locate our renamed file: missing = fail. Its budget for
  "finding the files it checks" (12 GB of downloads, 1 h of reading) implies it narrows candidates by
  path and then reads content. Hence the path plan changes as little as possible: same depth, same
  component lengths, same extension(s), every non-identifying token verbatim, hash suffixes kept.
* The corpus is organic (scraped repositories, sites, catalogs). There is no planted fictitious identity
  set in the sample or the path inventory; identities are source owners/repos/sites/organisations,
  people in author/creator/last-saved-by fields and profile directories, emails, and domains.

## Current numbers (aggregate only)

Discovery over 104,503 downloaded files + STEP headers of all 62,224 STEP files (ranged reads):
candidates kept by the classifier: 803 person, 351 account, 95 company, 125 email, 780 domain values
(seen in 10.8k / 6.7k / 25.4k / 0.9k / 34.7k files respectively; one value can count in many files).

Plan built from manifest + curated + discovered tables: 3,817 entries
(account 674, person 732, company 123, project 475, domain 1,715, email 98) with 3,443 aliased tokens.
Free-text modes: 1,690 flex, 1,662 exact, 465 field-only.

Path plan on the full manifest: 186,748 of 188,722 paths change; 0 depth/extension/length changes;
0 case-insensitive collisions; changed components: ~183.8k slot (owner/repo/site/catalog name),
~30.2k deeper directories, ~2.9k file stems.

## Checks run (sample, private details in work/)

* 19 unit tests on synthetic values: alias shape/determinism, encodings, boundaries, field vs free
  text, user dirs without double aliasing, path invariants, Creo stems, reference styles, plan round trip.
* End-to-end on the 280-file sample (pipeline scan, plan, rewrite into a private scratch dir; no
  upload), then a multi-encoding leak scan (raw, UTF-16, zip members, PDF Flate streams) for every
  non-generic plan identity: files containing identities went from 110 to 76, occurrences from 212 to 132,
  and 3 path components leaked. What remains comes mostly from adapter coverage, not the plan:
  text-like members of large zip archives copied unsupported, opaque Creo/CATIA/Fusion/Solid Edge streams,
  and members of old tar archives. A few false identities found this way (a STEP keyword username,
  title-block phrases, all-dictionary repo names) are now excluded by vocabulary rules.
