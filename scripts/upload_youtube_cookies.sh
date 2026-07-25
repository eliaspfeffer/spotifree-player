#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/youtube-cookies.txt" >&2
  exit 2
fi

cookie_file="$1"
if [[ ! -f "$cookie_file" ]]; then
  echo "Cookie file not found: $cookie_file" >&2
  exit 1
fi

ssh music-server 'mkdir -p /opt/spotify-song-server/cookies && chmod 700 /opt/spotify-song-server/cookies'
scp "$cookie_file" music-server:/opt/spotify-song-server/cookies/youtube.txt
ssh music-server 'chmod 600 /opt/spotify-song-server/cookies/youtube.txt && systemctl start --no-block spotify-song-download.service'

echo "Uploaded cookies and started spotify-song-download.service"

