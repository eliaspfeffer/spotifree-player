#!/usr/bin/env bash
set -euo pipefail

SERVER_SSH="${SERVER_SSH:-music-server}"
BASE_DIR="${BASE_DIR:-/opt/spotify-song-server}"

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/youtube-cookies.txt" >&2
  exit 2
fi

cookie_file="$1"
if [[ ! -f "$cookie_file" ]]; then
  echo "Cookie file not found: $cookie_file" >&2
  exit 1
fi

ssh "$SERVER_SSH" "mkdir -p '$BASE_DIR/cookies' && chmod 700 '$BASE_DIR/cookies'"
scp "$cookie_file" "$SERVER_SSH:$BASE_DIR/cookies/youtube.txt"
ssh "$SERVER_SSH" "chmod 600 '$BASE_DIR/cookies/youtube.txt' && systemctl start --no-block spotify-song-download.service"

echo "Uploaded cookies and started spotify-song-download.service"
