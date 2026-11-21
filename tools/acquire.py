#!/usr/bin/env python3
"""Resumable, verified parallel download of the Machina De-ID input corpus.

Usage:
  python tools/acquire.py --manifest manifest.tsv --dest input/ [--jobs 16]
         [--prefix P] [--limit N] [--log work/acquire.log]

Manifest columns: path, bytes, md5 (blank for multipart uploads), url path.
Each file is fetched with curl (resume with -C -) from one of the mirrors,
alternating between them; a file is "done" when its size matches and, where
the manifest has an MD5, the MD5 matches. Verified files are recorded in
<dest>/.acquire-done so reruns skip them without re-hashing.
"""
import argparse, hashlib, os, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

BASES = [
    "https://machina-input.joinbuilders.org",
    "https://machina-deid-v1-challenge.s3.us-west-2.amazonaws.com/releases/v1",
]
UA = "curl/8.10.1"


def md5_file(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--prefix", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-size", type=int, default=0, help="only files with at least this many bytes")
    ap.add_argument("--max-size", type=int, default=-1, help="only files below this many bytes")
    ap.add_argument("--smallest-first", action="store_true")
    args = ap.parse_args()
    dest = os.path.abspath(args.dest)
    os.makedirs(dest, exist_ok=True)
    done_path = os.path.join(dest, ".acquire-done")
    done = set()
    if os.path.exists(done_path):
        done = set(l.rstrip("\n") for l in open(done_path, encoding="utf-8"))
    # several instances may run on disjoint size ranges; they share the done list (append-only)
    rows = []
    for line in open(args.manifest, encoding="utf-8"):
        if line.startswith("#"):
            continue
        path, size, md5, url = line.rstrip("\n").split("\t")
        if not path.startswith(args.prefix):
            continue
        if int(size) < args.min_size or (args.max_size >= 0 and int(size) >= args.max_size):
            continue
        rows.append((path, int(size), md5, url))
        if args.limit and len(rows) >= args.limit:
            break
    todo = [r for r in rows if r[0] not in done]
    # big files first so the long tail is small ones in parallel
    todo.sort(key=lambda r: r[1] if args.smallest_first else -r[1])
    total = sum(r[1] for r in rows)
    have = sum(r[1] for r in rows if r[0] in done)
    lock = threading.Lock()
    state = {"bytes": have, "files": len(rows) - len(todo), "fail": 0, "t0": time.time(), "b0": have}
    donef = open(done_path, "a", encoding="utf-8")
    print(f"{len(rows)} files {total} bytes; {len(todo)} to fetch", flush=True)

    def fetch(i, row):
        path, size, md5, url = row
        out = os.path.join(dest, path)
        if os.path.realpath(out) != out and not os.path.realpath(out).startswith(dest + os.sep):
            raise RuntimeError("unsafe path")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        ok = False
        for attempt in range(6):
            base = BASES[(i + attempt) % len(BASES)]
            if size == 0:
                open(out, "wb").close()
            else:
                if os.path.exists(out) and os.path.getsize(out) > size:
                    os.unlink(out)
                if not (os.path.exists(out) and os.path.getsize(out) == size):
                    subprocess.run(["curl", "-fsS", "-A", UA, "--retry", "5", "--retry-connrefused",
                                    "--retry-delay", "2", "--connect-timeout", "20", "--speed-limit", "10000",
                                    "--speed-time", "60", "-C", "-", "-o", out, f"{base}/{url}"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out) and os.path.getsize(out) == size:
                if not md5 or md5_file(out) == md5:
                    ok = True
                    break
                os.unlink(out)  # wrong content: restart from scratch
            time.sleep(1 + attempt)
        with lock:
            if ok:
                donef.write(path + "\n"); donef.flush()
                state["bytes"] += size; state["files"] += 1
            else:
                state["fail"] += 1
                print(f"FAIL {path}", file=sys.stderr, flush=True)
        return ok

    def reporter():
        while True:
            time.sleep(30)
            with lock:
                el = time.time() - state["t0"]
                rate = (state["bytes"] - state["b0"]) * 8 / 1e6 / max(el, 1)
                rem = (total - state["bytes"]) * 8 / 1e6 / max(rate, 0.001)
                print(f"{time.strftime('%H:%M:%S')} files {state['files']}/{len(rows)} bytes {state['bytes']}/{total} "
                      f"({100*state['bytes']/max(total,1):.1f}%) {rate:.0f} Mb/s eta {rem/3600:.2f} h fail {state['fail']}",
                      flush=True)
    threading.Thread(target=reporter, daemon=True).start()
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = [ex.submit(fetch, i, r) for i, r in enumerate(todo)]
        for f in as_completed(futs):
            f.result()
    print(f"DONE files {state['files']}/{len(rows)} fail {state['fail']}", flush=True)
    sys.exit(1 if state["fail"] else 0)


if __name__ == "__main__":
    main()
