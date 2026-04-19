#!/usr/bin/env bash
# Runs RT-DETR+ByteTrack over all 5 CCTV clips, then synthesizes POS data.
#
# Usage:
#   bash pipeline/run.sh                  # process all clips → POST to localhost
#   API_URL=http://localhost:8000 bash pipeline/run.sh
#
# Requires: pip install -r requirements-pipeline.txt (torch, ultralytics, supervision)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_URL="${API_URL:-http://localhost:8000}"
CCTV_DIR="${CCTV_DIR:-../CCTV Footage}"
LAYOUT="${LAYOUT:-config/store_layout.json}"
FPS="${FPS:-5}"
STORE="${STORE_ID:-STORE_001}"

# Reset events file so re-runs are idempotent (DB itself is still idempotent on event_id).
mkdir -p data
: > data/events.jsonl

# Camera → clip mapping derived from Phase 0 recon (see store_layout.json).
declare -a MAPPING=(
    "CAM_ENTRY_01|CAM 3.mp4"
    "CAM_FLOOR_SKIN|CAM 1.mp4"
    "CAM_FLOOR_MAKEUP|CAM 2.mp4"
    "CAM_FLOOR_ACCESS|CAM 5.mp4"
    "CAM_STOCKROOM|CAM 4.mp4"
)

START_TS="${START_TS:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
echo "[run.sh] start_ts=$START_TS  api=$API_URL  fps=$FPS  store=$STORE"

for entry in "${MAPPING[@]}"; do
    IFS='|' read -r cam clip <<< "$entry"
    clip_path="$CCTV_DIR/$clip"
    if [[ ! -f "$clip_path" ]]; then
        echo "[run.sh] SKIP $cam — missing $clip_path"
        continue
    fi
    echo "[run.sh] processing $cam <- $clip"
    python -m pipeline.detect \
        --clip "$clip_path" \
        --camera-id "$cam" \
        --store-id "$STORE" \
        --layout "$LAYOUT" \
        --api-url "$API_URL" \
        --jsonl "data/events.jsonl" \
        --start-ts "$START_TS" \
        --fps "$FPS"
done

echo "[run.sh] synthesizing POS transactions"
python -m pipeline.synth_pos \
    --events "data/events.jsonl" \
    --output "data/pos_transactions.csv" \
    --api-url "$API_URL" \
    --post

echo "[run.sh] done. Events in data/events.jsonl, POS in data/pos_transactions.csv"
