#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${BASE_DIR:-/opt/spotify-song-server}"
VENV="${VENV:-$BASE_DIR/venv}"
TRACK_FILE="${TRACK_FILE:-$BASE_DIR/inputs/tracks.txt}"
MUSIC_DIR="${MUSIC_DIR:-$BASE_DIR/music}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs}"
COOKIE_FILE="${COOKIE_FILE:-$BASE_DIR/cookies/youtube.txt}"
LOG_FILE="$LOG_DIR/download.log"
FAILED_FILE="$LOG_DIR/failed-tracks.txt"
COMPLETED_FILE="$LOG_DIR/completed-tracks.txt"

mkdir -p "$MUSIC_DIR" "$LOG_DIR" "$(dirname "$COOKIE_FILE")"
touch "$LOG_FILE" "$FAILED_FILE" "$COMPLETED_FILE"

if [[ ! -x "$VENV/bin/spotdl" ]]; then
  echo "spotdl is missing at $VENV/bin/spotdl" | tee -a "$LOG_FILE" >&2
  exit 1
fi

if [[ ! -f "$TRACK_FILE" ]]; then
  echo "Track input file missing: $TRACK_FILE" | tee -a "$LOG_FILE" >&2
  exit 1
fi

total="$(grep -cvE '^\s*(#|$)' "$TRACK_FILE" || true)"
index=0

echo "[$(date -Is)] Starting spotDL download run for $total tracks" | tee -a "$LOG_FILE"
if [[ -f "$COOKIE_FILE" ]]; then
  echo "[$(date -Is)] Using YouTube cookie file: $COOKIE_FILE" | tee -a "$LOG_FILE"
else
  echo "[$(date -Is)] No YouTube cookie file found at $COOKIE_FILE" | tee -a "$LOG_FILE"
fi

while IFS= read -r track || [[ -n "$track" ]]; do
  [[ -z "${track// }" || "$track" =~ ^[[:space:]]*# ]] && continue
  index=$((index + 1))
  if grep -Fqx -- "$track" "$COMPLETED_FILE"; then
    echo "[$(date -Is)] [$index/$total] already complete" >>"$LOG_FILE"
    continue
  fi
  echo "[$(date -Is)] [$index/$total] $track" | tee -a "$LOG_FILE"

  args=(
    download "$track"
      --output "$MUSIC_DIR/{artists} - {title}.{output-ext}"
      --format mp3
      --bitrate 128k
      --threads 2
      --overwrite skip
      --print-errors
  )
  if [[ -f "$COOKIE_FILE" ]]; then
    args+=(--cookie-file "$COOKIE_FILE")
  fi

  tmp_log="$(mktemp)"
  if timeout 25m "$VENV/bin/spotdl" "${args[@]}" >"$tmp_log" 2>&1; then
    exit_code=0
  else
    exit_code=$?
  fi
  cat "$tmp_log" >>"$LOG_FILE"

  if [[ "$exit_code" -eq 0 ]] && ! grep -qiE 'AudioProviderError|Sign in to confirm|YT-DLP download error' "$tmp_log"; then
    printf '%s\n' "$track" >>"$COMPLETED_FILE"
    echo "[$(date -Is)] [$index/$total] done" | tee -a "$LOG_FILE"
  else
    echo "$track" >>"$FAILED_FILE"
    echo "[$(date -Is)] [$index/$total] failed with exit code $exit_code" | tee -a "$LOG_FILE"
  fi
  rm -f "$tmp_log"
done < "$TRACK_FILE"

sort -u -o "$COMPLETED_FILE" "$COMPLETED_FILE"
echo "[$(date -Is)] Download run finished. Failed entries: $(wc -l < "$FAILED_FILE")" | tee -a "$LOG_FILE"
