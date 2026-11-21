# Identity plan (owner: identity)

Status: v1 (path-seeded plan, working API). Policy and aggregate stats only: no raw values here.
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
| private curated tables `work/identity/curated.tsv`, `discovered.tsv` | any | as listed |

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
* Sub-tokens used under many unrelated sources (brand names like a toy or board vendor) are vocabulary.

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
