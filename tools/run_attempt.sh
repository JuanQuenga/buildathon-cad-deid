#!/usr/bin/env bash
# End-to-end, resumable production of one attempt folder (never uploads).
#   tools/run_attempt.sh INPUT_DIR MANIFEST WORK_DIR ATTEMPT_DIR [JOBS]
# Every step is incremental, so re-running after an interruption continues where it stopped.
# Set WAIT_FOR_DOWNLOAD=1 to wait until every manifest file is present with the right size.
set -euo pipefail
IN=$(realpath "$1"); MAN=$(realpath "$2"); WORK=$(realpath -m "$3"); ATT=$(realpath -m "$4"); JOBS=${5:-6}
REPO=$(cd "$(dirname "$0")/.." && pwd)
PY="$REPO/.venv/bin/python"
mkdir -p "$WORK"
exec 9>"$WORK/.run_attempt.lock"
flock -n 9 || { echo "another run_attempt.sh holds $WORK"; exit 1; }
log() { echo "$(date '+%F %T') $*" | tee -a "$WORK/run_attempt.log"; }
cd "$REPO"
A=(--input "$IN" --manifest "$MAN" --work "$WORK" --jobs "$JOBS")

if [ "${WAIT_FOR_DOWNLOAD:-0}" = 1 ]; then
  while true; do
    missing=$("$PY" - "$MAN" "$IN" <<'EOF'
import sys
from deid.pipeline.run import load_manifest
rows, missing = load_manifest(sys.argv[1], sys.argv[2], skip_missing=True)
print(missing)
EOF
)
    [ "$missing" = 0 ] && break
    log "download incomplete ($missing files): incremental scan of what is present"
    "$PY" -m deid.pipeline scan "${A[@]}" --skip-missing --mem-gb 3 >> "$WORK/scan.log" 2>&1 || true
    sleep 30
  done
fi

log "scan";     "$PY" -m deid.pipeline scan "${A[@]}" --mem-gb 3 >> "$WORK/scan.log" 2>&1
log "plan";     "$PY" -m deid.pipeline plan "${A[@]}" >> "$WORK/plan.log" 2>&1
log "rewrite";  "$PY" -m deid.pipeline rewrite "${A[@]}" --mem-gb 3 --out "$WORK/stage" >> "$WORK/rewrite.log" 2>&1
log "validate"; "$PY" -m deid.pipeline validate "${A[@]}" --out "$WORK/stage" --leak-scan full >> "$WORK/validate.log" 2>&1 \
  || log "validate reported hard failures (see $WORK/validate/report.json)"
if [ -e "$ATT" ]; then
  log "attempt dir exists, not packaging: $ATT"
else
  log "package"; "$PY" -m deid.pipeline package "${A[@]}" --stage "$WORK/stage" --out "$ATT" >> "$WORK/package.log" 2>&1
  log "preflight"; "$PY" tools/upload_attempt.py --attempt "$ATT" --dest gs://DRY-RUN/attempt/ \
      --plan-dir "$WORK/plan" --report "$WORK/validate/report.json" >> "$WORK/preflight.log" 2>&1 || true
fi
log "done"
