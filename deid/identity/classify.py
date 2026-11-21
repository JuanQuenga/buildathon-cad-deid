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
from .discover import TITLE_LABEL
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
    "wikipedia.org", "archive.org", "matplotlib.org", "mathworks.com", "monotype.com", "linotype.com",
    "fonts.com", "typekit.com", "fontawesome.com", "bambulab.com", "prusa3d.com", "ultimaker.com", "amazon.com", "openssl.org", "jquery.com", "bootstrapcdn.com", "w3.org", "git-scm.com", "apache.org", "eclipse.org", "debian.org",
    "ubuntu.com", "microsoft.com", "apple.com", "google.com", "mozilla.org", "gnu.org", "fsf.org",
)

BAD_VALUE = re.compile(
    r"^([A-Z0-9_]+-(ORG|PER|PERSON|ORGANIZATION|ORGANISATION|ADDR)\d*|\d[\d\s.:/_+-]*|[0-9a-f]{16,}|v?\d+(\.\d+)+.*|\d{4}-\d\d-\d\d.*|[\W_]+|.{1,2}|"
    r"(id|person|organization|org|name|first|last)[ _]?(\d+|id|name|first_name|last_name)?)$", re.I)
EXTRA_PLACEHOLDERS = {norm_key(x) for x in (
    "kicad stepup", "ksu", "ksu mcad", "kicad", "pcbnew", "occ", "occt", "open cascade", "freecad",
    "windows user", "sysadmin", "it", "it division", "user", "unspecified", "person last name",
    "person first name", "organization id", "organization name", "title", "none", "unknown user",
    "microsoft", "solidworks", "3dassist", "lenovo", "dell", "hp", "asus", "acer", "msi", "pc", "home",
    "owner", "admin", "administrator", "public", "default", "all users", "guest", "dev", "devel",
    "ubuntu", "pi", "runner", "vagrant", "docker", "build", "builder", "jenkins", "travis", "appveyor",
    "a company", "an author", "pc user", "managed by terraform", "step tools inc", "3d eye inc", "your name",
    "your company", "company name", "designer name", "author name", "user name", "username",
)}
TITLEBLOCK_STOP = {norm_key(x).replace(" ", "") for x in (
    "appv'd appvd approved appr chk'd chkd checked chk drawn drn dwn designed dsgn mfg q.a qa signature sign "
    "name date scale title sheet rev revision material finish weight mass dwg no size qty unless otherwise "
    "specified tolerances tolerance dimensions dimension inches mm third angle projection do not scale "
    "drawing part number description item comments notes eng engineer by of a b c d the and for "
    "ok yes no n/a tbd see note general"
).split()}
VENDOR_TOKENS = {"apple", "google", "alibre", "intergraph", "zemax", "hexagon", "bentley", "microstation", "vectorworks", "nemetschek",
                 "graphisoft", "esprit", "mastercam", "gibbscam", "hypermill", "openmind", "cimatron", "zwsoft",
                 "zwcad", "steptools", "sdrc", "transcendata", "kubotek", "ugs", "unigraphics", "datakit",
                 "cadexchanger", "elysium", "theorem", "terraform", "proficiency", "solidworks", "dassault", "systèmes", "systemes", "autodesk", "ptc", "siemens", "microsoft",
                 "adobe", "spatial", "parasolid", "catia", "inventor", "creo", "onshape", "freecad", "kicad"}
DATE_LIKE = re.compile(r"\d{1,2}:\d{2}|\d+\s*[年月日]|\b(19|20)\d\d[-/.]\d\d?[-/.]\d\d?\b|"
                       r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|"
                       r"march|april|june|july|august|september|october|november|december)\b.*\d", re.I)
URL_IN = re.compile(r"(?i)\bhttps?://([^/\s]+)(/[^\s]*)?")


def demojibake(v: str) -> str:
    """'Î¢ÈíÓÃ»§' (GBK/UTF-8 bytes read as Latin-1) -> original text, else v."""
    try:
        b = v.encode("latin-1")
    except UnicodeEncodeError:
        return v
    if all(c < 128 for c in b):
        return v
    for enc in ("utf-8", "gbk", "shift_jis", "cp1251"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return v


def _ok_value(v: str) -> bool:
    if not _ok_value1(v):
        return False
    d = demojibake(v)
    return d == v or _ok_value1(d)


def _ok_value1(v: str) -> bool:
    k = norm_key(v)
    if k and all(t in PLACEHOLDERS or t in EXTRA_PLACEHOLDERS for t in k.split()):
        return False
    if not k or k in PLACEHOLDERS or k in SOFTWARE or k in EXTRA_PLACEHOLDERS:
        return False
    if BAD_VALUE.match(v.strip()):
        return False
    if len(v) > 80 or "�" in v:
        return False
    toks = k.split()
    if set(toks) & VENDOR_TOKENS or k.replace(" ", "") in TITLEBLOCK_STOP or set(toks) <= TITLEBLOCK_STOP:
        return False
    if DATE_LIKE.search(v):
        return False
    if set(t for t in k.split() if not t.isdigit()) <= {"person", "first", "last", "name", "organization", "organisation", "id",
                          "unspecified", "none", "unknown", "author", "user", "company"}:
        return False
    letters = sum(ch.isalpha() for ch in v)
    return letters >= 2


def _name_like(v: str) -> bool:
    """Value next to a title-block label: accept person-name shapes only."""
    from .vocab import is_plain_token
    toks = re.findall(r"[^\W\d_]+", v)
    if (not toks or len(toks) > 4 or norm_key(v).replace(" ", "") in TITLEBLOCK_STOP
            or any(norm_key(t) in TITLEBLOCK_STOP and len(t) > 1 for t in toks)):
        return False
    if re.search(r"\d", v):
        return False
    if len(toks) == 1:
        return len(toks[0]) >= 3 and not is_plain_token(toks[0])
    # several tokens: at least one must not be a plain word (e.g. a surname), so
    # phrases such as 'TM Release' or 'Per Spec' next to a label are rejected
    return any(len(t) >= 3 and not is_plain_token(t) for t in toks)


def _domain_ok(d: str) -> bool:
    d = d.lower().strip(".")
    if d.startswith(("schemas.", "schema.", "ns.", "xmlns.", "purl.", "fonts.")):
        return False
    if "." not in d or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,12}", d):
        return False
    from .plan import is_infra_domain
    if is_infra_domain(d):
        return False
    for p in PROTECTED_DOMAINS + INFRA_DOMAINS:
        if d == p or d.endswith("." + p):
            return False
    return True


def classify(paths_obs, trust_fields=False):
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
                first = dom.split(".")[0]
                if (m and (dom == dom.lower() or dom == dom.upper()) and len(m.group(1)) >= 2
                        and re.search(r"[A-Za-z]{2}", m.group(1)) and not m.group(1)[-1] in "._-"
                        and not first.isdigit() and (len(first) >= 3 or dom.count(".") >= 2)):
                    add(m.group(0), "email", path, field)
                continue
            if kind == "domain":
                if _domain_ok(v):
                    add(v.lower(), "domain", path, field)
                continue
            if kind in ("username", "organization", "organisation"):
                kind = "account" if kind == "username" else "company"
            if kind in ("person", "company", "account") and (trust_fields or ID_FIELDS.search(field)):
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
                for em in EMAIL_RE.finditer(v):
                    add(em.group(0), "email", path, field)
                v = EMAIL_RE.sub(" ", v)
                for part in re.split(r"[\\;|]|\s*[(\[<]|[)\]>]", v):
                    part = part.strip().strip(",:-")
                    if part and _ok_value(part) and (".label." not in field or _name_like(part)):
                        add(part, kind, path, field)
    rows = []
    for (value, kind), (files, sources, fields) in agg.items():
        rows.append({"value": value, "kind": kind, "files": len(files), "sources": len(sources),
                     "fields": dict(fields.most_common(5))})
    rows.sort(key=lambda r: (-r["files"], r["value"]))
    return rows


def read_scan_db(db):
    """Stream (path, obs) from the pipeline scan DB (deid/api Observation rows)."""
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.execute("select path, kind, field, value from observations "
                      "where kind in ('person','company','organization','account','username','email','domain','url','free_text') "
                      "order by path")
    last, buf = None, []
    for path, kind, field, value in cur:
        if value is None:
            continue
        if path != last and buf:
            yield last, buf
            buf = []
        last = path
        if kind == "free_text":
            # names next to title-block labels, emails inside free text
            for m in TITLE_LABEL.finditer(value):
                buf.append(("person", field + ".label." + m.group(1).lower(), m.group(2), ""))
            if "@" in value:
                for m in EMAIL_RE.finditer(value):
                    buf.append(("email", field, m.group(0), ""))
            continue
        if kind == "url":
            m = URL_IN.search(value)
            if m:
                buf.append(("domain", field, m.group(1).lower(), ""))
            continue
        if kind in ("account", "username") and not field.startswith(("tar.", "zip.")):
            field = field  # adapter-asserted account fields
        buf.append((kind, field, value, ""))
    if buf:
        yield last, buf
    con.close()


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
    ap.add_argument("--in", dest="inp", action="append", default=[])
    ap.add_argument("--scan-db", action="append", default=[])
    ap.add_argument("--out", required=True)
    ap.add_argument("--stats")
    args = ap.parse_args(argv)
    def stream():
        yield from read_jsonl(args.inp)
    rows = classify(stream())
    for db in args.scan_db:
        rows += [dict(r, origin="scan") for r in classify(read_scan_db(db), trust_fields=True)]
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
