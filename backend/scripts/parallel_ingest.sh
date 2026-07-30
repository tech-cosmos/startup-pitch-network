#!/usr/bin/env bash
# Parallel pitch ingestion: split *.mp4 in a dir into N chunks and run N
# ingest_pitch.py processes concurrently. Starts are staggered to keep
# concurrent Neo4j MERGEs on shared nodes (Technology etc.) from colliding.
#
# Usage: bash scripts/parallel_ingest.sh <video_dir> [num_workers]
set -euo pipefail

DIR="${1:?video dir required}"
N="${2:-4}"
cd "$(dirname "$0")/.."   # backend/

LOGDIR="/tmp/pitch_ingest_logs"
mkdir -p "$LOGDIR"

i=0
pids=()
for chunk in $(seq 0 $((N-1))); do
  files=$(ls "$DIR"/*.mp4 | awk -v n="$N" -v c="$chunk" 'NR % n == c')
  [ -z "$files" ] && continue
  (
    sleep $((chunk * 15))   # stagger
    # shellcheck disable=SC2086
    uv run python scripts/ingest_pitch.py $files
  ) > "$LOGDIR/worker_$chunk.log" 2>&1 &
  pids+=($!)
  i=$((i+1))
done

echo "launched $i workers (logs: $LOGDIR/worker_*.log)"
for pid in "${pids[@]}"; do wait "$pid" || true; done

echo "=== all workers done ==="
grep -h "Wrote pitch" "$LOGDIR"/worker_*.log | wc -l | xargs echo "pitches written:"
grep -h "Failed to ingest" "$LOGDIR"/worker_*.log || echo "no failures"
