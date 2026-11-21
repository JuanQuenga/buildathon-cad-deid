#!/usr/bin/env python3
"""EXPERIMENTAL, opt-in: pre-fill a new attempt folder by server-side copying objects whose
bytes are identical to an object already uploaded in an earlier attempt (same MD5 and size),
so that upload.py then only has to send the files that changed.

Uses the same Builders upload token and the GCS XML API that upload.py uses
(PUT with x-goog-copy-source). Whether the token permits copies is unknown until tried:
run first with --limit 1 and check the result. upload.py's own resume check (size + MD5
ETag) then skips every object copied here, and uploads everything else. The attempt folder
still ends up holding a full copy of the output.

  python3 tools/gcs_copy_unchanged.py \
     --from gs://builders-machina-submissions-1120/teams/<team>/attempt-1/ \
     --to   gs://builders-machina-submissions-1120/teams/<team>/attempt-2/ \
     --attempt out/attempt-2 --token-file ~/machina-token.txt [--limit 1] [--go]

Without --go it only reports how many objects/bytes could be copied.
Local MD5s are cached in <attempt>.md5cache.json.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

HOST = "storage.googleapis.com"


def parse(dest):
    assert dest.startswith("gs://") and dest.endswith("/"), "use gs://bucket/prefix/ ending in /"
    b, p = dest[5:].split("/", 1)
    return b, p


def enc(key):
    return "/".join(urllib.parse.quote(x, safe="") for x in key.split("/"))


class Tok:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.tok = open(path).read().strip()

    def get(self):
        return self.tok

    def refresh(self, stale):
        with self.lock:
            if self.tok != stale:
                return
            print(f"token expired: write a new token to {self.path}; waiting...", flush=True)
            for _ in range(7200):
                t = open(self.path).read().strip()
                if t and t != stale:
                    self.tok = t
                    return
                time.sleep(1)
            raise SystemExit("no new token")


def expired(status, body):
    return status in (401, 403) and (b"xpired" in body or b"nvalid" in body or b"uthentication" in body)


def list_prefix(bucket, prefix, tok):
    out = {}
    cont = None
    conn = http.client.HTTPSConnection(HOST, timeout=60)
    while True:
        q = {"list-type": "2", "prefix": prefix, "max-keys": "1000", "encoding-type": "url"}
        if cont:
            q["continuation-token"] = cont
        t = tok.get()
        conn.request("GET", f"/{bucket}/?{urllib.parse.urlencode(q)}", headers={"Authorization": f"Bearer {t}"})
        r = conn.getresponse()
        body = r.read()
        if expired(r.status, body):
            tok.refresh(t)
            continue
        if r.status != 200:
            raise SystemExit(f"list failed {r.status}: {body[:300]!r}")
        root = ET.fromstring(body)
        ns = root.tag.split("}", 1)[0] + "}" if root.tag.startswith("{") else ""
        for it in root.findall(f"{ns}Contents"):
            k = urllib.parse.unquote_plus(it.findtext(f"{ns}Key") or "")
            out[k] = (int(it.findtext(f"{ns}Size") or 0), (it.findtext(f"{ns}ETag") or "").strip('"').lower())
        if root.findtext(f"{ns}IsTruncated") != "true":
            break
        cont = root.findtext(f"{ns}NextContinuationToken")
        if not cont:
            break
    conn.close()
    return out


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def local_items(att):
    items = []
    for sub in ("corpus", "code"):
        d = os.path.join(att, sub)
        for root, _, files in os.walk(d):
            for f in files:
                p = os.path.join(root, f)
                if not os.path.islink(p):
                    items.append((p, f"{sub}/{os.path.relpath(p, d)}"))
    items.append((os.path.join(att, "README.txt"), "README.txt"))
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("--attempt", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args()
    sb, sp = parse(a.src)
    db_, dp = parse(a.dst)
    att = os.path.abspath(a.attempt)
    tok = Tok(a.token_file)
    cache_p = att + ".md5cache.json"
    cache = json.load(open(cache_p)) if os.path.exists(cache_p) else {}
    items = local_items(att)
    print(f"hashing {len(items)} local files (cached: {len(cache)})", flush=True)
    for p, k in items:
        st = os.stat(p)
        c = cache.get(k)
        if not c or c[0] != st.st_size or c[1] != int(st.st_mtime):
            cache[k] = [st.st_size, int(st.st_mtime), md5(p)]
    json.dump(cache, open(cache_p, "w"))
    src = list_prefix(sb, sp, tok)
    dst = list_prefix(db_, dp, tok)
    by_md5 = {}
    for k, (sz, et) in src.items():
        if len(et) == 32:
            by_md5.setdefault((et, sz), k)
    todo, nbytes, already = [], 0, 0
    for p, k in items:
        sz, _, m = cache[k]
        if dp + k in dst and dst[dp + k] == (sz, m):
            already += 1
            continue
        s = by_md5.get((m, sz))
        if s:
            todo.append((s, dp + k, sz))
            nbytes += sz
    total = sum(cache[k][0] for _, k in items)
    print(f"{len(items)} local objects ({total / 1e9:.1f} GB); {already} already present in the new folder; "
          f"{len(todo)} copyable server-side ({nbytes / 1e9:.1f} GB); "
          f"{len(items) - already - len(todo)} must be uploaded", flush=True)
    if a.limit:
        todo = todo[: a.limit]
    if not a.go:
        print("dry run (pass --go)")
        return 0
    local = threading.local()

    def copy(s, d, sz):
        for attempt in range(5):
            conn = getattr(local, "c", None) or http.client.HTTPSConnection(HOST, timeout=300)
            local.c = conn
            t = tok.get()
            try:
                conn.request("PUT", f"/{db_}/{enc(d)}", headers={
                    "Authorization": f"Bearer {t}", "Content-Length": "0",
                    "x-goog-copy-source": f"/{sb}/{enc(s)}"})
                r = conn.getresponse()
                body = r.read()
            except (OSError, http.client.HTTPException):
                local.c = None
                time.sleep(2 ** attempt)
                continue
            if expired(r.status, body):
                tok.refresh(t)
                continue
            if r.status == 200:
                return True, ""
            if r.status in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            return False, f"{r.status}: {body[:200]!r}"
        return False, "retries exhausted"
    ok = bad = 0
    with ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(copy, *x) for x in todo]
        for i, f in enumerate(as_completed(futs), 1):
            good, err = f.result()
            ok += good
            if not good:
                bad += 1
                if bad <= 5:
                    print("copy failed:", err, flush=True)
                if bad == 20 and ok == 0:
                    print("copies are not permitted with this token; stop and use upload.py only")
                    os._exit(3)
            if i % 1000 == 0:
                print(f"  {i}/{len(todo)} copied={ok} failed={bad}", flush=True)
    print(f"done: copied {ok}, failed {bad}. Now run upload.py for the same --dest; it skips copied objects.")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
