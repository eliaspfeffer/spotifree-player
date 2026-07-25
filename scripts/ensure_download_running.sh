#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${BASE_DIR:-/opt/spotify-song-server}"
TRACK_FILE="${TRACK_FILE:-$BASE_DIR/inputs/tracks.txt}"
MUSIC_DIR="${MUSIC_DIR:-$BASE_DIR/music}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs}"
LOG_FILE="$LOG_DIR/ensure-download.log"
COMPLETED_FILE="$LOG_DIR/completed-tracks.txt"

mkdir -p "$LOG_DIR"
touch "$COMPLETED_FILE"

total="$(grep -cvE '^\s*(#|$)' "$TRACK_FILE" 2>/dev/null || echo 0)"
files="$(find "$MUSIC_DIR" -type f 2>/dev/null | wc -l | tr -d ' ')"
pending="$(
  awk '
    NR == FNR { if ($0 != "") completed[$0] = 1; next }
    $0 !~ /^[[:space:]]*(#|$)/ && !completed[$0] { pending += 1 }
    END { print pending + 0 }
  ' "$COMPLETED_FILE" "$TRACK_FILE" 2>/dev/null
)"

state="$(systemctl is-active spotify-song-download.service 2>/dev/null || true)"
if [[ "$state" == "active" || "$state" == "activating" ]]; then
  echo "[$(date -Is)] download already running ($state); files=$files total=$total pending=$pending" >>"$LOG_FILE"
  exit 0
fi

if [[ "$pending" -gt 0 ]]; then
  echo "[$(date -Is)] starting download; files=$files total=$total pending=$pending" >>"$LOG_FILE"
  systemctl start --no-block spotify-song-download.service
else
  echo "[$(date -Is)] no start needed; files=$files total=$total pending=$pending" >>"$LOG_FILE"
fi
