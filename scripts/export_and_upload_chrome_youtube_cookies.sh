#!/usr/bin/env bash
set -euo pipefail

tmp_dir="$(mktemp -d)"
raw="$tmp_dir/raw-cookies.txt"
filtered="$tmp_dir/youtube-google-cookies.txt"
log="$tmp_dir/yt-dlp-cookie-export.log"

cleanup() {
  for file in "$raw" "$filtered" "$log"; do
    if [[ -f "$file" ]]; then
      : > "$file"
      unlink "$file"
    fi
  done
  rmdir "$tmp_dir" 2>/dev/null || true
}
trap cleanup EXIT

chmod 700 "$tmp_dir"

yt-dlp \
  --cookies-from-browser chrome \
  --cookies "$raw" \
  --skip-download \
  --ignore-no-formats-error \
  --no-warnings \
  "https://www.youtube.com/watch?v=dQw4w9WgXcQ" >"$log" 2>&1 || {
    if [[ -s "$raw" ]]; then
      echo "yt-dlp test request failed after exporting cookies; continuing with exported cookie jar."
    else
      sed -n '1,80p' "$log"
      exit 1
    fi
  }

awk '
  BEGIN { OFS = "\t" }
  /^#/ { print; next }
  NF >= 7 && ($1 ~ /(^|\.)youtube\.com$/ || $1 == ".google.com" || $1 == "google.com" || $1 == "accounts.google.com" || $1 == "www.google.com" || $1 == ".google.de" || $1 == "google.de" || $1 == "www.google.de" || $1 ~ /(^|\.)ytimg\.com$/) { print }
' "$raw" > "$filtered"

chmod 600 "$filtered"

echo "raw_lines=$(wc -l < "$raw")"
echo "filtered_lines=$(wc -l < "$filtered")"
echo "filtered_domains="
awk '!/^#/ && NF >= 7 { print $1 }' "$filtered" | sort -u | sed -n '1,40p'

if [[ "$(awk '!/^#/ && NF >= 7 { count++ } END { print count + 0 }' "$filtered")" -eq 0 ]]; then
  echo "No YouTube/Google cookies were exported from Chrome." >&2
  exit 1
fi

scp "$filtered" music-server:/opt/spotify-song-server/cookies/youtube.txt >/dev/null
ssh music-server '
  chmod 600 /opt/spotify-song-server/cookies/youtube.txt &&
  echo "server_cookie_file=$(ls -l /opt/spotify-song-server/cookies/youtube.txt)" &&
  echo "server_cookie_lines=$(wc -l < /opt/spotify-song-server/cookies/youtube.txt)"
'
