#!/usr/bin/env bash
# Fast attempt factory: produce out/attempt-N incrementally from attempt-PREV. Never uploads.
#
#   tools/make_attempt.sh N PREV COMMIT "hypothesis text" [--replan] [--jobs J] [--mem-gb G]
#
#   BASE   (env, required) the private working area holding input/, work/, out/
#   N      new attempt number;  PREV  attempt to build on (its work dir work/a<PREV>, out/attempt-<PREV>)
#   COMMIT code to freeze as branch attempt-N (worktree work/code-attempt-N)
#
# Steps: freeze code -> clone work/a<PREV> (scan + results DBs, plan, stage: reflinks, seconds) ->
# invalidate results of adapters whose module changed between the two commits (and everything when
# pipeline/identity code changed or --replan) -> scan (incremental) -> [plan] -> rewrite (only
# files whose key changed) -> validate (gate) -> package -> MD5 manifest for server-side copies ->
# diff vs PREV -> out/attempt-N.summary.txt -> prints the upload commands for the coordinator.
set -euo pipefail
N=$1; PREV=$2; COMMIT=$3; HYP=$4; shift 4
REPLAN=0; JOBS=${JOBS:-6}; MEM=${MEM_GB:-4}
while [ $# -gt 0 ]; do case "$1" in
  --replan) REPLAN=1; shift;; --jobs) JOBS=$2; shift 2;; --mem-gb) MEM=$2; shift 2;;
  *) echo "unknown option $1"; exit 2;; esac; done
: "${BASE:?set BASE to the working area (input/, work/, out/)}"
REPO=$(cd "$(dirname "$0")/.." && pwd)
PY="nice -n 10 ionice -c 3 $REPO/.venv/bin/python"
MAN=${DEID_MANIFEST:-$BASE/sample/manifest.tsv}
PW=$BASE/work/a$PREV; [ "$PREV" = 1 ] && [ ! -d "$PW" ] && PW=$BASE/work/full
W=$BASE/work/a$N; WT=$BASE/work/code-attempt-$N; OUT=$BASE/out/attempt-$N
[ -e "$OUT" ] && { echo "refusing: $OUT exists (attempts are immutable)"; exit 2; }
mkdir -p "$W"
log() { echo "$(date '+%F %T') $*" | tee -a "$W/make_attempt.log"; }
T0=$(date +%s)

# 1. freeze code
if [ ! -d "$WT" ]; then git -C "$REPO" worktree add -q -b "attempt-$N" "$WT" "$COMMIT"; fi
NEWSHA=$(git -C "$WT" rev-parse HEAD)
PREVSHA=$("$REPO/.venv/bin/python" -c "import sqlite3,sys;print(dict(sqlite3.connect(sys.argv[1]).execute('select k,v from meta')).get('git_sha',''))" "$PW/results.sqlite")
log "attempt-$N: code $NEWSHA (prev attempt-$PREV code $PREVSHA); hypothesis: $HYP"

# 2. clone previous state (reflinks: instant, no extra space)
if [ ! -f "$W/results.sqlite" ]; then
  for f in scan.sqlite results.sqlite; do cp --reflink=auto "$PW/$f" "$W/$f"; done
  cp -a --reflink=auto "$PW/plan" "$W/plan"
  [ -d "$W/stage" ] || cp -a --reflink=always "$PW/stage" "$W/stage"
  log "cloned work state from $PW"
fi

# 3. invalidate what the code change can affect
CHANGED=$(git -C "$REPO" diff --name-only "$PREVSHA" "$NEWSHA" -- deid | tr '\n' ' ')
log "changed modules: ${CHANGED:-none}"
FULL=0
case " $CHANGED " in *" deid/api.py "*|*" deid/pipeline/core.py "*|*" deid/formats/_"*) FULL=1;; esac
if [ "${KEEP_PLAN:-0}" != 1 ]; then case " $CHANGED " in *" deid/identity/"*) [ "$REPLAN" = 1 ] || { log "identity code changed: forcing --replan"; REPLAN=1; };; esac; else log "KEEP_PLAN=1: plan copied unchanged from the previous attempt"; fi
if [ "$REPLAN" = 1 ]; then rm -rf "$W/plan"; FULL=1; fi
cd "$WT"
$PY - "$W" "$FULL" $CHANGED <<'EOF'
import sys, sqlite3
work, full, changed = sys.argv[1], sys.argv[2] == "1", sys.argv[3:]
from deid import api
api.load_all()
mods = {m[:-3].replace("/", ".") for m in changed if m.startswith("deid/formats/") and m.endswith(".py")}
names = {a.name for a in api.adapters() if a.__class__.__module__ in mods}
con = sqlite3.connect(f"{work}/results.sqlite")
if full:
    n = con.execute("DELETE FROM results").rowcount
elif names:
    n = con.execute(f"DELETE FROM results WHERE adapter IN ({','.join('?' * len(names))})",
                    tuple(sorted(names))).rowcount
    # archives/packages: redo only those that contain a member handled by a changed adapter
    import json as _json
    sc = sqlite3.connect(f"file:{work}/scan.sqlite?mode=ro", uri=True)
    tops = set()
    for path, fp in sc.execute("SELECT path, fingerprint FROM files WHERE parent IS NOT NULL"):
        try:
            if _json.loads(fp or "{}").get("_adapter") in names:
                tops.add(path.split("!/", 1)[0])
        except ValueError:
            pass
    for t in tops:
        n += con.execute("DELETE FROM results WHERE path=?", (t,)).rowcount
else:
    n = 0
con.commit()
print(f"invalidated {n} results ({'all' if full else sorted(names)})")
EOF
export DEID_OCR_CACHE=${DEID_OCR_CACHE:-$BASE/work/full/ocr-cache} DEID_CONSTRAINTS_DIR=$W/constraints
export TMPDIR=$W/tmp; mkdir -p "$TMPDIR"   # keep tool temp files off /tmp (tmpfs quota)
mkdir -p "$DEID_CONSTRAINTS_DIR"
A="--input $BASE/input --manifest $MAN --work $W --jobs $JOBS --mem-gb $MEM"
log "scan";  $PY -m deid.pipeline scan $A >> "$W/scan.log" 2>&1
if [ ! -f "$W/plan/plan.json" ]; then log "plan"; $PY -m deid.pipeline plan $A >> "$W/plan.log" 2>&1; fi
T1=$(date +%s)
log "rewrite"; $PY -m deid.pipeline rewrite $A --out "$W/stage" >> "$W/rewrite.log" 2>&1
T2=$(date +%s)
# the validator always comes from the main repo (frozen attempt code may predate its options)
log "validate"; (cd "$REPO" && $PY -m deid.pipeline validate $A --out "$W/stage" --leak-scan full --prev-work "$PW") >> "$W/validate.log" 2>&1 || log "validate reported hard failures"
T3=$(date +%s)
log "package"; $PY -m deid.pipeline package $A --stage "$W/stage" --out "$OUT" >> "$W/package.log" 2>&1
log "md5 manifest"; $PY "$REPO"/tools/md5_manifest.py --attempt "$OUT" --work "$W" --manifest "$MAN" --jobs "$JOBS" >> "$W/md5.log" 2>&1
log "diff vs attempt-$PREV"; $PY "$REPO"/tools/diff_attempts.py --a-work "$PW" --a-corpus "$BASE/out/attempt-$PREV/corpus" \
    --b-work "$W" --b-corpus "$OUT/corpus" --out "$W/diff" --jobs "$JOBS" >> "$W/diff.log" 2>&1
log "preflight"; $PY "$REPO"/tools/upload_attempt.py --attempt "$OUT" --dest gs://DRY-RUN/attempt-$N/ --plan-dir "$W/plan" \
    --report "$W/validate/report.json" > "$W/preflight.log" 2>&1 || true
T4=$(date +%s)
$PY "$REPO"/tools/attempt_summary.py --n "$N" --prev "$PREV" --work "$W" --hypothesis "$HYP" --code "$NEWSHA" \
    --timing "scan+plan $((T1-T0))s, rewrite $((T2-T1))s, validate $((T3-T2))s, package+md5+diff $((T4-T3))s, total $((T4-T0))s" \
    --preflight "$W/preflight.log" > "$OUT.summary.txt"
log "done in $((T4-T0))s -> $OUT ; summary: $OUT.summary.txt"
T=${TEAM_PREFIX:-<TEAM_PREFIX>}
cat <<EOC | tee "$OUT.push.txt"
# push commands for attempt-$N (run after attempt-$PREV is fully uploaded; not run here)
# 1) server-side copy of every object already uploaded in attempt-$PREV (matched by MD5+size, any key):
python3 $REPO/tools/gcs_copy_unchanged.py --from $T/attempt-$PREV/ --to $T/attempt-$N/ --attempt $OUT --token-file $BASE/work/submit/machina-token.txt --go
# 2) upload the rest (upload.py skips objects already present with the same MD5):
python3 $BASE/starter/upload.py --dest $T/attempt-$N/ --corpus $OUT/corpus --code $OUT/code --readme $OUT/README.txt --token-file $BASE/work/submit/machina-token.txt
# 3) verify before submitting (read-only; complete = "0 copyable server-side; 0 must be uploaded"):
python3 $REPO/tools/gcs_copy_unchanged.py --from $T/attempt-$PREV/ --to $T/attempt-$N/ --attempt $OUT --token-file $BASE/work/submit/machina-token.txt
EOC
