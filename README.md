# Spotifree Player

A private, mobile-first music player for a shared server. Authenticated users
can stream or download the available library, import their own Spotify exports,
and listen to a personal queue.

The deployment target for this setup is `music-server` under
`/opt/spotify-song-server`.

## Friend import

Friends need only the website and its password:

1. Open the `Freunde` tab.
2. Follow the link to [Exportify](https://exportify.net/) and sign in to Spotify.
3. Export `Liked Songs` as CSV.
4. Enter a display name, select the CSV, and press `Songs importieren`.

Every display name gets a personal library. Re-uploading another export with
the same name merges it into that library. Tracks are deduplicated by Spotify
track ID both within that library and in the global download queue. The audio
folder is shared, so the same song is stored only once even when several people
have it in their libraries.

Downloaded tracks become playable in the personal library after a refresh.
Pending tracks remain visible as `Wird geladen`. `Library abspielen` starts only
the selected person's playable tracks.

## Components

- `app/music_server.py`: password login, player, CSV import, personal libraries,
  Spotify OAuth integration, and shared Codex chat
- `scripts/run_spotdl_downloads.sh`: spotDL worker with per-track completion
  state and retries
- `scripts/ensure_download_running.sh`: starts the worker when URLs are pending
- `systemd/`: web server, worker, and periodic worker check units
- `caddy/Caddyfile`: HTTPS reverse proxy for `music.example.com`

Runtime data, credentials, cookies, downloaded audio, and local browser test
profiles are intentionally excluded from Git.

## Server configuration

The service reads `/etc/spotify-song-server.env`. At minimum, configure:

```sh
HOST=127.0.0.1
PORT=8088
BASIC_AUTH_USER=change-me
BASIC_AUTH_PASSWORD=change-me
BASE_DIR=/opt/spotify-song-server
```

Optional Spotify OAuth variables:

```sh
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
SPOTIFY_REDIRECT_URI=https://music.example.com/spotify/callback
```

The CSV route works without Spotify developer credentials.

## Codex chat

The web UI includes a shared, password-protected Codex chat. It uses the
Codex CLI already logged in on `music-server`, not an OpenAI API key. It uses the
default model available to that Codex login. Set an explicit CLI model only if
that account has access to it:

```sh
CODEX_CLI_MODEL=gpt-5.6-terra
```

Restart the app afterwards:

```sh
systemctl restart spotify-song-server.service
```

All signed-in users see the same persisted chat history. Codex can create a
website patch proposal, but an authenticated user must explicitly press
`Änderung anwenden`. Proposals are restricted to `app/music_server.py`, are
syntax-checked before activation, and the prior version is retained under
`/opt/spotify-song-server/state/codex-chat/backups`.

## Usage

Only import and download music that you are allowed to copy and share. Spotify,
YouTube, and other providers may impose additional terms on account and content
use.
