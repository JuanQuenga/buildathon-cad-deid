"""Turn raw discovery observations into a reviewed identity table.

    python -m deid.identity.classify --in WORK/identity/discovery.jsonl [--in more.jsonl]
        --out WORK/identity/discovered.tsv --stats WORK/identity/discovered-stats.json

Minimum-evidence rules (precision first):
* person/company/account values only from identity-bearing FIELDS (author,
  organization, last author, company, creator, %%For, USER=, user dirs, ...),
  never from free text;
* drop placeholders, software/vendor names, dates, versions, hashes, numbers,
  URLs (an author given as a URL yields its account/domain instead);
* emails: plausible syntax and TLD; local part + domain become identities;
* domains: drop format namespaces and public infrastructure (code hosting,
  package indexes, CDNs, standards bodies).
Output TSV: value, kind, mode('' = builder decides), keep, files, sources, fields.
"""
from __future__ import annotations

import argparse
import collections
import json
import re

from .aliases import norm_key
from .plan import EMAIL_RE, PROTECTED_DOMAINS
from .vocab import PLACEHOLDERS, SOFTWARE

ID_FIELDS = re.compile(
    r"(FILE_NAME\.(author|organization|authorisation)|step\.PERSON|step\.ORGANIZATION|"
    r"pdf\.(info|objstm)\.(Author|Company|Manager|Owner)|xmp\.(dc:creator|pdf:Author|dc:publisher|"
    r"xmpRights:Owner|cc:attributionName|dc:contributor)|cfb\.(Author|LastAuthor|Manager|Company)|"
    r"ps\.DSC\.(For|Author|Company)|parasolid\.(USER|SITE)|iges\.G\.(author|organization)|userdir|"
    r"meta:initial-creator|dc:creator|lastModifiedBy|fcprop\.(CreatedBy|LastModifiedBy|Company)|"
    r"\.Author$|\.Designer$|\.Company$|\.Manager$|sw\.prop\.(Author|DrawnBy|Drawn By|Company|Designer|Engineer|"
    r"CheckedBy|Checked By|ApprovedBy|Approved By|Customer|Owner)|sw\.Header\d*\.author|tar\.(uname|gname)|"
    r"label\.(drawn|drawn by|designed|designed by|designer|author|engineer|draftsman|gezeichnet|bearbeitet|"
    r"dessin[eé]|auteur|dibuj[oó]|disegnato|nome|nombre|customer|client|company|owner))", re.I)

INFRA_DOMAINS = (
    "githubusercontent.com", "github.io", "githubassets.com", "pythonhosted.org", "pypi.org", "npmjs.com",
    "npmjs.org", "jsdelivr.net", "unpkg.com", "cloudflare.com", "shields.io", "shopify.com", "amazonaws.com",
    "googleusercontent.com", "gstatic.com", "youtube.com", "youtu.be", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "linkedin.com", "spdx.org", "nextjs.org", "react.dev", "typescriptlang.org", "vercel.com",
    "vercel-storage.com", "figma.com", "digicert.com", "verisign.com", "globalsign.com", "letsencrypt.org",
    "localhost", "readthedocs.io", "readthedocs.org", "stackoverflow.com", "doi.org", "arxiv.org",
    "schema.org", "openxmlformats.org", "autodesk.com", "kicad.org", "python.org", "nodejs.org",
    "wikipedia.org", "archive.org", "w3.org", "git-scm.com", "apache.org", "eclipse.org", "debian.org",
    "ubuntu.com", "microsoft.com", "apple.com", "google.com", "mozilla.org", "gnu.org", "fsf.org",
)

BAD_VALUE = re.compile(
    r"^(\d[\d\s.:/_+-]*|[0-9a-f]{16,}|v?\d+(\.\d+)+.*|\d{4}-\d\d-\d\d.*|[\W_]+|.{1,2}|"
    r"(id|person|organization|org|name|first|last)[ _]?(\d+|id|name|first_name|last_name)?)$", re.I)
EXTRA_PLACEHOLDERS = {norm_key(x) for x in (
    "kicad stepup", "ksu", "ksu mcad", "kicad", "pcbnew", "occ", "occt", "open cascade", "freecad",
    "windows user", "sysadmin", "it", "it division", "user", "unspecified", "person last name",
    "person first name", "organization id", "organization name", "title", "none", "unknown user",
    "microsoft", "solidworks", "3dassist", "lenovo", "dell", "hp", "asus", "acer", "msi", "pc", "home",
    "owner", "admin", "administrator", "public", "default", "all users", "guest", "dev", "devel",
    "ubuntu", "pi", "runner", "vagrant", "docker", "build", "builder", "jenkins", "travis", "appveyor",
)}
URL_IN = re.compile(r"(?i)\bhttps?://([^/\s]+)(/[^\s]*)?")


def _ok_value(v: str) -> bool:
    k = norm_key(v)
    if not k or k in PLACEHOLDERS or k in SOFTWARE or k in EXTRA_PLACEHOLDERS:
        return False
    if BAD_VALUE.match(v.strip()):
        return False
    if len(v) > 80 or "�" in v:
        return False
    if set(k.split()) <= {"person", "first", "last", "name", "organization", "organisation", "id",
                          "unspecified", "none", "unknown", "author", "user", "company"}:
        return False
    letters = sum(ch.isalpha() for ch in v)
    return letters >= 2


def _domain_ok(d: str) -> bool:
    d = d.lower().strip(".")
    if "." not in d or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,12}", d):
        return False
    for p in PROTECTED_DOMAINS + INFRA_DOMAINS:
        if d == p or d.endswith("." + p):
            return False
    return True


def classify(paths_obs):
    """paths_obs: iterable of (path, [(kind, field, value, ev), ...]).  Returns rows."""
    agg = collections.defaultdict(lambda: [set(), set(), collections.Counter()])

    def add(value, kind, path, field):
        a = agg[(value, kind)]
        a[0].add(path)
        parts = path.split("/")
        a[1].add(parts[2].split("__")[0].lower() if parts[0] == "sources" and len(parts) > 2 else parts[0])
        a[2][re.sub(r"^(zip>)+", "", field)] += 1

    for path, obs in paths_obs:
        for kind, field, value, _ev in obs:
            v = value.strip()
            if kind == "email" or EMAIL_RE.fullmatch(v):
                m = EMAIL_RE.search(v)
                dom = m.group(2) if m else ""
                if m and (dom == dom.lower() or dom == dom.upper()) and len(m.group(1)) >= 2:
                    add(m.group(0), "email", path, field)
                continue
            if kind == "domain":
                if _domain_ok(v):
                    add(v.lower(), "domain", path, field)
                continue
            if kind in ("person", "company", "account") and ID_FIELDS.search(field):
                mu = URL_IN.search(v)
                if mu:  # author given as URL: the account in a code-hosting URL is the identity
                    host, rest = mu.group(1).lower(), (mu.group(2) or "")
                    seg = [s for s in rest.split("/") if s]
                    if host.endswith(("github.com", "gitlab.com", "bitbucket.org")) and seg:
                        if _ok_value(seg[0]):
                            add(seg[0], "account", path, field)
                    elif _domain_ok(host):
                        add(host, "domain", path, field)
                    continue
                # "DOMAIN\\user (MACHINE)" style
                for part in re.split(r"[\\]|\s*\(|\)", v):
                    part = part.strip()
                    if part and _ok_value(part):
                        add(part, kind, path, field)
    rows = []
    for (value, kind), (files, sources, fields) in agg.items():
        rows.append({"value": value, "kind": kind, "files": len(files), "sources": len(sources),
                     "fields": dict(fields.most_common(5))})
    rows.sort(key=lambda r: (-r["files"], r["value"]))
    return rows


def read_jsonl(paths):
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                yield d["path"], d["obs"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stats")
    args = ap.parse_args(argv)
    rows = classify(read_jsonl(args.inp))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# value\tkind\tmode\tkeep\tfiles\tsources\tfields  (PRIVATE)\n")
        for r in rows:
            v = r["value"].replace("\t", " ").replace("\n", " ")
            f.write(f"{v}\t{r['kind']}\t\t\t{r['files']}\t{r['sources']}\t{json.dumps(r['fields'])}\n")
    st = collections.Counter(r["kind"] for r in rows)
    if args.stats:
        with open(args.stats, "w") as f:
            json.dump({"by_kind": st, "files_by_kind": {k: sum(r["files"] for r in rows if r["kind"] == k) for k in st}}, f, indent=1)
    print(dict(st))


if __name__ == "__main__":
    main()
