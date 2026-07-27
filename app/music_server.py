#!/usr/bin/env python3
import base64
import csv
import email.policy
import hashlib
import html
import http.cookies
import io
import json
import mimetypes
import os
import posixpath
import re
import shutil
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac"}
CHUNK_SIZE = 1024 * 1024
MAX_CHAT_MESSAGE_LENGTH = 6000
MAX_CHAT_HISTORY = 80
MAX_CSV_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_CSV_TRACKS = 10000
EDITABLE_APP_FILE = "app/music_server.py"


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def normalize_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def split_values(value: str, separator: str) -> list[str]:
    return [part.strip() for part in value.split(separator) if part.strip()]


def format_duration(ms: str) -> str:
    try:
        seconds = int(ms) // 1000
    except (TypeError, ValueError):
        return ""
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}"


def short_date(value: str) -> str:
    return (value or "")[:10]


class MusicServer(BaseHTTPRequestHandler):
    server_version = "SpotifySongServer/1.0"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_response(HTTPStatus.OK)
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        if not self.is_authorized():
            self.request_auth()
            return

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/":
            self.render_index(params)
            return
        if parsed.path == "/spotify/login":
            self.spotify_login(params)
            return
        if parsed.path == "/spotify/callback":
            self.spotify_callback(params)
            return
        if parsed.path == "/api/codex/history":
            self.codex_history()
            return
        if parsed.path == "/api/recommendations":
            self.music_recommendations(params)
            return
        if parsed.path.startswith("/cover/"):
            self.serve_cover(parsed.path.removeprefix("/cover/"))
            return
        if parsed.path.startswith("/files/"):
            self.stream_file(parsed.path.removeprefix("/files/"), as_attachment=False)
            return
        if parsed.path.startswith("/download/"):
            self.stream_file(parsed.path.removeprefix("/download/"), as_attachment=True)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/login":
            self.handle_login(self.read_urlencoded_form())
            return
        if parsed.path == "/logout":
            self.handle_logout()
            return

        if not self.is_authorized():
            self.request_auth()
            return

        if parsed.path == "/friends/import":
            self.friend_csv_import()
            return

        params = self.read_urlencoded_form()
        if parsed.path == "/api/codex/chat":
            self.codex_chat(params)
            return
        if parsed.path == "/api/codex/apply":
            self.codex_apply(params)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def read_urlencoded_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > MAX_CSV_UPLOAD_BYTES:
            return {}
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(body)

    def read_multipart_form(self) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("Das Formular wurde nicht als Datei-Upload gesendet.")
        if length <= 0 or length > MAX_CSV_UPLOAD_BYTES:
            raise ValueError("Die CSV-Datei ist leer oder größer als 8 MB.")
        body = self.rfile.read(length)
        message = BytesParser(policy=email.policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\n"
            b"MIME-Version: 1.0\r\n\r\n" + body
        )
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        if not message.is_multipart():
            raise ValueError("Der Datei-Upload konnte nicht gelesen werden.")
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename is not None:
                files[name] = (Path(filename).name, payload)
            else:
                charset = part.get_content_charset() or "utf-8"
                fields[name] = payload.decode(charset, errors="replace").strip()
        return fields, files

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def codex_history(self) -> None:
        self.send_json({"messages": self.server.load_chat_history(), "configured": self.server.codex_configured()})

    def music_recommendations(self, params: dict[str, list[str]]) -> None:
        source = params.get("source", [""])[0].strip()
        if not source.startswith("/files/"):
            self.send_json({"error": "Ungültiger Song."}, HTTPStatus.BAD_REQUEST)
            return
        path = self.resolve_music_path(source.removeprefix("/files/"))
        if path is None:
            self.send_json({"error": "Der Song wurde nicht gefunden."}, HTTPStatus.NOT_FOUND)
            return
        try:
            suggestions = self.server.recommendations_for(path, self.music_files())
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        self.send_json({"suggestions": suggestions, "source": source})

    def codex_chat(self, params: dict[str, list[str]]) -> None:
        message = params.get("message", [""])[0].strip()
        if not message:
            self.send_json({"error": "Bitte schreibe eine Nachricht."}, HTTPStatus.BAD_REQUEST)
            return
        if len(message) > MAX_CHAT_MESSAGE_LENGTH:
            self.send_json({"error": "Die Nachricht ist zu lang."}, HTTPStatus.BAD_REQUEST)
            return
        if not self.server.codex_configured():
            self.send_json(
                {"error": "Die angemeldete Codex-CLI ist auf dem Server nicht verfügbar."},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            result = self.server.reply_to_codex(message)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        self.send_json(result)

    def codex_apply(self, params: dict[str, list[str]]) -> None:
        proposal_id = params.get("proposal_id", [""])[0].strip()
        try:
            result = self.server.apply_codex_proposal(proposal_id)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(result)

    def is_authorized(self) -> bool:
        username = self.server.auth_user
        password = self.server.auth_password
        if not username or not password:
            return True
        token = self.session_token()
        return bool(token and self.server.has_auth_session(token))

    def session_token(self) -> str:
        return self.cookie_value("song_session")

    def cookie_value(self, name: str) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return ""
        return unquote(jar.get(name).value) if jar.get(name) else ""

    def request_auth(self) -> None:
        body = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Login</title>
  <style>
    :root { --bg: #f7f5ef; --surface: #ffffff; --line: #d8d3c7; --text: #1d2522; --muted: #69736e; --accent: #1db954; }
    body { margin: 0; font: 16px/1.45 system-ui, sans-serif; background: radial-gradient(circle at top, #efe8d2, var(--bg) 45%); color: var(--text); }
    main { width: min(420px, calc(100% - 24px)); margin: 0 auto; min-height: 100dvh; display: grid; align-content: center; }
    .panel { border: 1px solid var(--line); border-radius: 8px; background: color-mix(in srgb, var(--surface) 92%, transparent); padding: 22px; box-shadow: 0 18px 40px rgba(0,0,0,.08); }
    h1 { margin: 0 0 8px; font-size: 2rem; }
    p { margin: 0 0 18px; color: var(--muted); }
    label { display: block; margin: 0 0 6px; font-weight: 600; }
    input, button { width: 100%; font: inherit; border-radius: 8px; padding: 12px 14px; border: 1px solid var(--line); box-sizing: border-box; }
    input { margin: 0 0 12px; background: var(--surface); color: var(--text); }
    button { background: var(--accent); border-color: var(--accent); color: #07120d; font-weight: 700; }
    .hint { margin-top: 14px; font-size: .9rem; }
  </style>
</head>
<body>
  <main>
    <section class="panel">
      <h1>Songs</h1>
      <p>Mit dem Server-Passwort einloggen.</p>
      <form action="/login" method="post">
        <label for="username">User</label>
        <input id="username" name="username" autocomplete="username" required>
        <label for="password">Passwort</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
        <button type="submit">Einloggen</button>
      </form>
    </section>
  </main>
</body>
</html>"""
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_login(self, params: dict[str, list[str]]) -> None:
        username = params.get("username", [""])[0]
        password = params.get("password", [""])[0]
        if username == self.server.auth_user and password == self.server.auth_password:
            token = self.server.create_auth_session()
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", self.session_cookie(token, 2592000))
            self.end_headers()
            return
        self.request_auth()

    def handle_logout(self) -> None:
        token = self.session_token()
        if token:
            self.server.delete_auth_session(token)
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", self.session_cookie("", 0))
        self.end_headers()

    def session_cookie(self, token: str, max_age: int) -> str:
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        return f"song_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def music_files(self) -> list[Path]:
        root = self.server.music_root
        files: list[Path] = []
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                files.append(path)
        return sorted(files, key=lambda p: p.name.casefold())

    def render_index(self, params: dict[str, list[str]]) -> None:
        query = params.get("q", [""])[0].strip()
        genre = params.get("genre", [""])[0].strip()
        artist = params.get("artist", [""])[0].strip()
        sort = params.get("sort", ["title"])[0].strip() or "title"
        active_view = params.get("view", ["player"])[0].strip()
        if active_view not in {"player", "playlist", "friends", "discover"}:
            active_view = "player"
        selected_friend = params.get("friend", [self.cookie_value("friend_library")])[0].strip()
        all_files = self.music_files()
        all_entries = [(path, self.server.metadata_for(path)) for path in all_files]
        entries = list(all_entries)

        genre_options = sorted({
            item for _, meta in all_entries for item in meta.get("genres", [])
        }, key=str.casefold)
        artist_options = sorted({
            item for path, meta in all_entries for item in (meta.get("artists") or [self.artist_from_path(path)])
            if item
        }, key=str.casefold)

        normalized_query = normalize_key(query)
        if normalized_query:
            entries = [
                entry for entry in entries
                if normalized_query in normalize_key(self.search_text(*entry))
            ]
        if genre:
            entries = [
                (path, meta) for path, meta in entries
                if genre in meta.get("genres", [])
            ]
        if artist:
            entries = [
                (path, meta) for path, meta in entries
                if artist in meta.get("artists", [])
            ]

        entries = self.sort_entries(entries, sort)
        player_entries = self.sort_entries(all_entries, "title")
        track_indexes = {path: index for index, (path, _) in enumerate(player_entries)}

        total, used, free = shutil.disk_usage(self.server.music_root)
        rows = "\n".join(self.track_row(track_indexes[path], path, meta) for path, meta in entries)
        if not rows:
            rows = '<p class="empty">Noch keine passenden Audiodateien gefunden.</p>'
        player_json = json.dumps(
            [self.player_track(path, meta) for path, meta in player_entries],
            ensure_ascii=False,
        )
        player_json_script = player_json.replace("</", "<\\/")
        friends_rows, friend_queues = self.friends_rows(
            player_entries,
            track_indexes,
            selected_friend,
            params,
        )
        friend_queues_script = json.dumps(friend_queues, ensure_ascii=False).replace("</", "<\\/")
        tab_class = lambda name: "tab active" if active_view == name else "tab"
        panel_class = lambda name: "panel active" if active_view == name else "panel"

        page = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Songs</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f5ef;
      --text: #1d2522;
      --muted: #69736e;
      --line: #d8d3c7;
      --accent: #0f766e;
      --accent-strong: #1db954;
      --surface: #ffffff;
      --surface-2: #ebe7dc;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #131715;
        --text: #eef3ef;
        --muted: #a8b1ac;
        --line: #34413b;
        --accent: #5eead4;
        --accent-strong: #1ed760;
        --surface: #1b211f;
        --surface-2: #242c29;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 16px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    main {{
      width: min(980px, calc(100% - 24px));
      margin: 0 auto;
      padding: 20px 0 104px;
    }}
    .topbar {{
      display: flex;
      justify-content: flex-end;
      margin-bottom: 8px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
      border-bottom: 1px solid var(--line);
      padding-bottom: 16px;
      margin-bottom: 16px;
    }}
    h1 {{
      font-size: clamp(1.5rem, 4vw, 2.4rem);
      margin: 0;
      letter-spacing: 0;
    }}
    .meta {{
      color: var(--muted);
      text-align: right;
      font-size: .92rem;
    }}
    form {{
      display: grid;
      grid-template-columns: minmax(180px, 1fr) repeat(3, minmax(140px, .45fr)) auto;
      gap: 8px;
      margin: 16px 0 20px;
    }}
    input, select {{
      flex: 1;
      min-width: 0;
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--text);
      border-radius: 8px;
      padding: 12px 14px;
      font: inherit;
    }}
    select {{
      appearance: none;
      background-image:
        linear-gradient(45deg, transparent 50%, var(--muted) 50%),
        linear-gradient(135deg, var(--muted) 50%, transparent 50%);
      background-position:
        calc(100% - 17px) calc(50% + 1px),
        calc(100% - 12px) calc(50% + 1px);
      background-size: 5px 5px, 5px 5px;
      background-repeat: no-repeat;
      padding-right: 30px;
    }}
    button, a.download {{
      border: 1px solid var(--accent);
      color: var(--accent);
      background: transparent;
      border-radius: 8px;
      padding: 10px 12px;
      font: inherit;
      text-decoration: none;
      white-space: nowrap;
    }}
    button {{
      cursor: pointer;
    }}
    .tabs {{
      position: sticky;
      top: 0;
      z-index: 5;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      background: color-mix(in srgb, var(--bg) 92%, transparent);
      backdrop-filter: blur(16px);
      padding: 8px 0 12px;
      margin: 0 0 12px;
    }}
    .tab {{
      border-color: var(--line);
      color: var(--muted);
      background: var(--surface);
      font-weight: 650;
    }}
    .tab.active {{
      border-color: var(--accent-strong);
      color: #07120d;
      background: var(--accent-strong);
    }}
    .panel {{
      display: none;
    }}
    .panel.active {{
      display: block;
    }}
    .player-card {{
      min-height: min(620px, calc(100dvh - 220px));
      display: grid;
      align-content: center;
      gap: 22px;
      padding: 24px 0;
    }}
    .cover {{
      width: min(72vw, 340px);
      aspect-ratio: 1;
      margin: 0 auto;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background:
        linear-gradient(135deg, color-mix(in srgb, var(--accent-strong) 70%, white), #151515 85%),
        var(--surface-2);
      box-shadow: 0 22px 60px rgba(0, 0, 0, .28);
      color: #07120d;
      font-size: 5rem;
      font-weight: 800;
    }}
    .player-info {{
      text-align: center;
      min-width: 0;
    }}
    .player-title {{
      margin: 0 0 6px;
      font-size: clamp(1.35rem, 6vw, 2rem);
      font-weight: 760;
      overflow-wrap: anywhere;
    }}
    .player-artist {{
      color: var(--muted);
      overflow-wrap: anywhere;
    }}
    .progress {{
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr) 44px;
      gap: 10px;
      align-items: center;
      color: var(--muted);
      font-size: .82rem;
    }}
    input[type="range"] {{
      width: 100%;
      accent-color: var(--accent-strong);
    }}
    .player-controls {{
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 14px;
      flex-wrap: wrap;
    }}
    .icon-button {{
      width: 48px;
      height: 48px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      padding: 0;
      font-weight: 800;
      background: var(--surface);
      border-color: var(--line);
      color: var(--text);
    }}
    .play-button {{
      width: 64px;
      height: 64px;
      border-radius: 999px;
      border-color: var(--accent-strong);
      background: var(--accent-strong);
      color: #07120d;
      font-weight: 800;
    }}
    .player-download {{
      justify-self: center;
      min-width: 150px;
      text-align: center;
    }}
    .mini-player {{
      position: fixed;
      left: 12px;
      right: 12px;
      bottom: max(76px, calc(12px + env(safe-area-inset-bottom)));
      z-index: 10;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 10px;
      align-items: center;
      max-width: 980px;
      margin: 0 auto;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: color-mix(in srgb, var(--surface) 92%, transparent);
      backdrop-filter: blur(18px);
      box-shadow: 0 12px 35px rgba(0, 0, 0, .18);
    }}
    .mini-title {{
      font-weight: 700;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .mini-artist {{
      color: var(--muted);
      font-size: .84rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .track {{
      border-bottom: 1px solid var(--line);
      padding: 14px 0 16px;
    }}
    .track.current .track-title {{
      color: var(--accent);
    }}
    .track-title {{
      font-weight: 650;
      overflow-wrap: anywhere;
      margin-bottom: 4px;
    }}
    .track-meta {{
      color: var(--muted);
      font-size: .9rem;
      overflow-wrap: anywhere;
      margin-bottom: 10px;
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 0 0 10px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: .78rem;
      line-height: 1;
      padding: 6px 8px;
    }}
    .chip button {{
      width: 22px;
      height: 22px;
      min-width: 22px;
      border-radius: 999px;
      margin: 0;
      padding: 0;
      border-color: var(--line);
      color: var(--text);
      background: var(--surface);
      font-size: .72rem;
    }}
    .track-actions {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }}
    audio {{
      width: 100%;
      min-width: 0;
    }}
    .empty {{
      color: var(--muted);
      padding: 28px 0;
    }}
    .friend-empty, .friend-track {{
      border-bottom: 1px solid var(--line);
      padding: 16px 0;
    }}
    .friend-title {{
      font-weight: 750;
      margin-bottom: 4px;
    }}
    code {{
      color: var(--accent);
      overflow-wrap: anywhere;
    }}
    .codex-toggle {{
      position: fixed;
      right: 16px;
      bottom: max(16px, env(safe-area-inset-bottom));
      z-index: 21;
      min-width: 100px;
      border-color: var(--accent-strong);
      background: var(--accent-strong);
      color: #07120d;
      font-weight: 760;
      box-shadow: 0 10px 28px rgba(0, 0, 0, .2);
    }}
    .codex-chat {{
      position: fixed;
      right: 12px;
      bottom: max(76px, calc(12px + env(safe-area-inset-bottom)));
      z-index: 20;
      width: min(430px, calc(100% - 24px));
      height: min(590px, calc(100dvh - 96px));
      display: none;
      grid-template-rows: auto minmax(0, 1fr) auto;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 18px 52px rgba(0, 0, 0, .28);
    }}
    .codex-chat.open {{ display: grid; }}
    .codex-chat-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
    }}
    .codex-chat-title {{ font-weight: 760; }}
    .codex-chat-subtitle {{ color: var(--muted); font-size: .82rem; }}
    .codex-close {{ width: 36px; height: 36px; padding: 0; border-color: var(--line); color: var(--text); }}
    .codex-messages {{
      overflow-y: auto;
      padding: 12px;
      display: grid;
      align-content: start;
      gap: 10px;
    }}
    .codex-message {{
      max-width: 92%;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-size: .93rem;
    }}
    .codex-message.user {{ justify-self: end; background: var(--surface-2); }}
    .codex-message.assistant {{ justify-self: start; }}
    .codex-message.system {{ justify-self: center; color: var(--muted); font-size: .84rem; }}
    .codex-proposal {{
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid var(--line);
      font-size: .86rem;
    }}
    .codex-proposal button {{ margin-top: 8px; width: 100%; }}
    .codex-compose {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      padding: 12px;
      border-top: 1px solid var(--line);
    }}
    .codex-compose textarea {{
      min-height: 46px;
      max-height: 120px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--bg);
      color: var(--text);
      padding: 10px 12px;
      font: inherit;
    }}
    .codex-compose button {{ align-self: end; }}
    @media (max-width: 640px) {{
      header, form, .track-actions {{
        display: block;
      }}
      input, select, button {{
        width: 100%;
        margin-bottom: 8px;
      }}
      .meta {{
        margin-top: 8px;
        text-align: left;
      }}
      a.download {{
        display: inline-block;
        margin-top: 10px;
      }}
      .track-actions .icon-button {{
        display: inline-grid;
        margin: 0 8px 10px 0;
      }}
      .mini-player .icon-button {{
        width: 42px;
        height: 42px;
        margin: 0;
      }}
      .codex-chat {{
        left: 8px;
        right: 8px;
        width: auto;
        bottom: max(68px, calc(8px + env(safe-area-inset-bottom)));
        height: min(680px, calc(100dvh - 84px));
      }}
      .codex-compose {{ display: grid; }}
      .codex-compose button {{ width: auto; margin: 0; }}
      .codex-toggle {{ width: auto; margin: 0; right: 12px; }}
    }}

    /* Modern player surface */
    :root {{
      color-scheme: dark;
      --bg: #0b0d0c;
      --text: #f5f7f5;
      --muted: #929b96;
      --line: #2b312e;
      --accent: #6ee7a8;
      --accent-strong: #56e093;
      --warm: #ff8b7b;
      --surface: #141816;
      --surface-2: #1c221f;
      --surface-3: #242b27;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        color-scheme: light;
        --bg: #f3f5f2;
        --text: #151916;
        --muted: #69716c;
        --line: #d4dad6;
        --accent: #147a4d;
        --accent-strong: #27bd70;
        --warm: #d85c4f;
        --surface: #ffffff;
        --surface-2: #e9eeeb;
        --surface-3: #dfe6e1;
      }}
    }}
    html {{ min-height: 100%; background: var(--bg); }}
    body {{
      min-height: 100dvh;
      background: var(--bg);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    body[data-active-tab="player"] {{ height: 100dvh; overflow: hidden; }}
    main {{
      width: min(1180px, calc(100% - 40px));
      min-height: 100dvh;
      padding: 14px 0 108px;
      display: grid;
      grid-template-rows: 52px 48px minmax(0, 1fr);
      align-content: start;
    }}
    body[data-active-tab="player"] main {{
      height: 100dvh;
      min-height: 0;
      padding-bottom: 14px;
    }}
    .app-header {{
      min-width: 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      margin: 0;
      padding: 0;
      border: 0;
    }}
    .app-header form {{ display: block; margin: 0; }}
    .brand {{ display: flex; align-items: center; gap: 11px; min-width: 0; }}
    .brand-mark {{
      width: 34px;
      height: 34px;
      display: grid;
      place-items: center;
      flex: 0 0 auto;
      border: 1px solid color-mix(in srgb, var(--accent) 50%, var(--line));
      border-radius: 8px;
      color: var(--accent);
      background: var(--surface);
      font-size: .72rem;
      font-weight: 850;
    }}
    h1 {{ margin: 0; font-size: 1rem; line-height: 1.1; font-weight: 780; }}
    .brand-subtitle {{ margin-top: 3px; color: var(--muted); font-size: .74rem; }}
    .logout-button {{
      width: auto;
      margin: 0;
      padding: 7px 10px;
      border-color: var(--line);
      color: var(--muted);
      background: transparent;
      font-size: .78rem;
    }}
    .tabs {{
      position: relative;
      top: auto;
      z-index: 5;
      width: fit-content;
      grid-template-columns: repeat(4, auto);
      align-self: center;
      gap: 3px;
      margin: 0;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      backdrop-filter: none;
    }}
    .tab {{
      width: auto;
      min-width: 92px;
      margin: 0;
      padding: 7px 13px;
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      font-size: .82rem;
      font-weight: 680;
      transition: color .18s ease, background .18s ease;
    }}
    .tab.active {{
      border: 0;
      color: var(--text);
      background: var(--surface-3);
      box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--line) 75%, transparent);
    }}
    .panel {{ min-width: 0; }}
    .panel.active {{ display: block; animation: panel-in .24s ease both; }}
    @keyframes panel-in {{
      from {{ opacity: 0; transform: translateY(4px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    [data-panel="player"] {{ height: 100%; min-height: 0; overflow: hidden; }}
    .player-card {{
      width: 100%;
      height: 100%;
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(260px, .92fr) minmax(330px, 1.08fr);
      align-items: center;
      gap: clamp(34px, 6vw, 84px);
      padding: 18px clamp(12px, 4vw, 54px);
    }}
    .artwork-shell {{
      position: relative;
      width: min(100%, 430px, 52vh);
      aspect-ratio: 1;
      justify-self: end;
      isolation: isolate;
    }}
    .cover, .cover-fallback {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      border-radius: 8px;
    }}
    .cover {{
      z-index: 2;
      display: block;
      object-fit: cover;
      background: var(--surface-2);
      box-shadow: 0 26px 80px rgba(0, 0, 0, .44);
      opacity: 0;
      transition: opacity .3s ease;
    }}
    .cover.loaded {{ opacity: 1; }}
    .cover-fallback {{
      z-index: 1;
      display: grid;
      place-items: center;
      background: var(--surface-2);
      border: 1px solid var(--line);
      color: var(--accent);
      font-size: 4.5rem;
      font-weight: 850;
    }}
    .artwork-glow {{
      position: absolute;
      inset: 8% -5% -7%;
      z-index: 0;
      border-radius: 8px;
      background: color-mix(in srgb, var(--accent) 18%, transparent);
      filter: blur(42px);
      opacity: .6;
    }}
    .player-console {{
      width: min(100%, 520px);
      min-width: 0;
      display: grid;
      gap: 24px;
    }}
    .player-info {{ text-align: left; }}
    .eyebrow {{
      margin-bottom: 10px;
      color: var(--accent);
      font-size: .7rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .12em;
    }}
    .player-title {{
      display: -webkit-box;
      margin: 0 0 8px;
      overflow: hidden;
      overflow-wrap: anywhere;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      font-size: 2rem;
      line-height: 1.08;
      font-weight: 790;
      letter-spacing: 0;
    }}
    .player-artist {{
      color: var(--muted);
      font-size: .96rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .progress {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 6px 12px;
      color: var(--muted);
      font-size: .72rem;
      font-variant-numeric: tabular-nums;
    }}
    .progress input {{ grid-column: 1 / -1; grid-row: 1; }}
    .progress #current-time {{ grid-column: 1; grid-row: 2; justify-self: start; }}
    .progress #duration {{ grid-column: 2; grid-row: 2; justify-self: end; }}
    input[type="range"] {{
      width: 100%;
      min-height: 20px;
      margin: 0;
      padding: 0;
      border: 0;
      background: transparent;
      accent-color: var(--accent-strong);
    }}
    .player-controls {{
      display: grid;
      grid-template-columns: repeat(5, auto);
      justify-content: start;
      align-items: center;
      gap: 10px;
    }}
    .icon-button, .play-button {{
      margin: 0;
      border-radius: 999px;
      transition: transform .16s ease, border-color .16s ease, background .16s ease, color .16s ease;
    }}
    .icon-button:hover, .play-button:hover {{ transform: translateY(-1px); }}
    .icon-button {{
      width: 44px;
      height: 44px;
      border-color: var(--line);
      background: var(--surface);
      color: var(--text);
      font-size: 1rem;
    }}
    .icon-button[aria-pressed="true"] {{
      border-color: var(--accent);
      color: var(--accent);
      background: color-mix(in srgb, var(--accent) 10%, var(--surface));
    }}
    .play-button {{
      width: 62px;
      height: 62px;
      border-color: var(--accent-strong);
      background: var(--accent-strong);
      color: #07120d;
      font-size: 1.3rem;
      font-weight: 850;
      box-shadow: 0 10px 30px color-mix(in srgb, var(--accent-strong) 20%, transparent);
    }}
    .player-download {{ display: grid; min-width: 0; margin: 0; padding: 0; text-align: center; }}
    .player-options {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(150px, .55fr);
      gap: 22px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
    }}
    .volume-control, .crossfade-control {{
      min-width: 0;
      display: grid;
      align-items: center;
      gap: 8px;
    }}
    .volume-control {{ grid-template-columns: 1fr auto; }}
    .volume-control input {{ grid-column: 1 / -1; }}
    .crossfade-control {{ grid-template-columns: 1fr; }}
    .setting-label {{ color: var(--muted); font-size: .72rem; font-weight: 680; }}
    .setting-value {{ color: var(--text); font-size: .72rem; font-variant-numeric: tabular-nums; }}
    .crossfade-control select {{
      width: 100%;
      min-height: 36px;
      margin: 0;
      padding: 7px 30px 7px 10px;
      border-color: var(--line);
      background-color: var(--surface);
      font-size: .78rem;
    }}
    .mini-player {{
      left: 50%;
      right: auto;
      bottom: max(72px, calc(12px + env(safe-area-inset-bottom)));
      width: min(720px, calc(100% - 24px));
      max-width: none;
      transform: translateX(-50%);
      grid-template-columns: 46px minmax(0, 1fr) auto auto;
      padding: 8px;
      border-radius: 8px;
      background: color-mix(in srgb, var(--surface) 94%, transparent);
    }}
    body[data-active-tab="player"] .mini-player {{ display: none; }}
    .mini-cover-shell {{
      position: relative;
      width: 46px;
      height: 46px;
      overflow: hidden;
      border-radius: 6px;
      background: var(--surface-2);
      color: var(--accent);
      display: grid;
      place-items: center;
      font-weight: 800;
    }}
    .mini-cover {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; }}
    .mini-cover.loaded {{ opacity: 1; }}
    .track {{
      display: grid;
      grid-template-columns: 54px minmax(0, 1fr) auto auto;
      gap: 14px;
      align-items: center;
      padding: 10px 2px;
      border-bottom: 1px solid var(--line);
    }}
    .track-cover {{
      width: 54px;
      height: 54px;
      border-radius: 6px;
      object-fit: cover;
      background: var(--surface-2);
    }}
    .track-cover.missing {{ opacity: .18; }}
    .track-copy {{ min-width: 0; }}
    .track-title {{ margin: 0 0 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .track-meta {{ margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .track .chips {{ margin: 7px 0 0; }}
    .track-play {{ width: 40px; height: 40px; }}
    .track-download {{ margin: 0; padding: 8px 10px; border-color: var(--line); color: var(--muted); font-size: .76rem; }}
    [data-panel="playlist"] > form {{
      position: sticky;
      top: 0;
      z-index: 4;
      grid-template-columns: minmax(190px, 1fr) repeat(3, minmax(130px, .55fr)) auto;
      margin: 12px 0;
      padding: 10px 0;
      background: var(--bg);
    }}
    .chip {{ border-radius: 999px; background: var(--surface); }}
    [data-panel="friends"] {{ padding-top: 14px; }}
    .friend-import {{
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(280px, .9fr);
      gap: 36px;
      align-items: center;
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }}
    .friend-kicker {{
      margin-bottom: 5px;
      color: var(--accent);
      font-size: .7rem;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .friend-import h2, .friend-library h2 {{ margin: 0; font-size: clamp(1.35rem, 3vw, 2rem); }}
    .friend-import ol {{ margin: 18px 0 12px; padding-left: 20px; color: var(--text); }}
    .friend-import li {{ margin: 8px 0; padding-left: 4px; }}
    .friend-import p, .friend-library-head p {{ margin: 8px 0 0; color: var(--muted); }}
    .friend-import a {{ color: var(--accent); font-weight: 750; }}
    .csv-import, .spotify-import {{
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 8px;
      min-width: 0;
      margin: 0;
    }}
    .csv-import label {{ color: var(--muted); font-size: .76rem; font-weight: 750; }}
    .csv-import input {{
      width: 100%;
      min-width: 0;
      margin: 0 0 4px;
      border-color: var(--line);
      background: var(--bg);
    }}
    .csv-import input[type="file"] {{ padding: 9px; color: var(--muted); }}
    .csv-import input[type="file"]::file-selector-button {{
      margin-right: 10px;
      padding: 7px 10px;
      border: 0;
      border-radius: 6px;
      background: var(--surface-3);
      color: var(--text);
      font: inherit;
      font-weight: 700;
    }}
    .csv-import button, .spotify-import button, .friend-play-all {{
      margin: 4px 0 0;
      border-color: var(--accent);
      background: var(--accent);
      color: #07120d;
      font-weight: 800;
    }}
    .import-divider {{ display: grid; grid-template-columns: 1fr auto 1fr; gap: 10px; align-items: center; color: var(--muted); font-size: .75rem; }}
    .import-divider::before, .import-divider::after {{ content: ""; height: 1px; background: var(--line); }}
    .import-success {{
      margin: 14px 0 0;
      padding: 12px 14px;
      border-left: 3px solid var(--accent);
      background: color-mix(in srgb, var(--accent) 10%, transparent);
      color: var(--muted);
    }}
    .import-success strong {{ color: var(--text); }}
    .friend-switches {{
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding: 18px 0 10px;
      scrollbar-width: none;
    }}
    .friend-switches::-webkit-scrollbar {{ display: none; }}
    .friend-switch {{
      display: inline-flex;
      flex: 0 0 auto;
      gap: 9px;
      align-items: center;
      padding: 8px 11px;
      border: 1px solid var(--line);
      border-radius: 7px;
      color: var(--muted);
      text-decoration: none;
      font-weight: 750;
    }}
    .friend-switch span {{ color: var(--muted); font-size: .72rem; }}
    .friend-switch.active {{ border-color: var(--accent); background: var(--surface-2); color: var(--text); }}
    .friend-library {{ padding: 18px 0 110px; }}
    .friend-library-head {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: end;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--line);
    }}
    .friend-play-all {{ width: auto; white-space: nowrap; }}
    .friend-play-all:disabled {{ opacity: .45; cursor: wait; }}
    .friend-track {{
      display: grid;
      grid-template-columns: 48px minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      padding: 10px 2px;
      border-bottom: 1px solid var(--line);
    }}
    .friend-track .track-cover, .friend-cover-fallback {{ width: 48px; height: 48px; }}
    .friend-cover-fallback {{
      display: grid;
      place-items: center;
      border-radius: 6px;
      background: var(--surface-2);
      color: var(--muted);
      font-weight: 800;
    }}
    .friend-pending {{ color: var(--muted); font-size: .72rem; white-space: nowrap; }}
    [data-panel="discover"] {{ padding: 22px 0 110px; }}
    .discover-header {{
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: end;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--line);
    }}
    .discover-header h2 {{ margin: 0; font-size: clamp(1.55rem, 3vw, 2.35rem); }}
    .discover-header p {{ max-width: 560px; margin: 7px 0 0; color: var(--muted); }}
    .recommendation-status {{ color: var(--muted); font-size: .78rem; white-space: nowrap; }}
    .recommendation-status.loading {{ color: var(--accent); }}
    .youtube-stage {{
      display: grid;
      grid-template-columns: minmax(320px, .95fr) minmax(0, 1.05fr);
      gap: 28px;
      align-items: center;
      margin: 20px 0;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--line);
    }}
    .youtube-stage[hidden] {{ display: none; }}
    .youtube-frame {{
      width: 100%;
      aspect-ratio: 16 / 9;
      min-height: 200px;
      overflow: hidden;
      border-radius: 8px;
      background: #000;
    }}
    .youtube-frame iframe {{ width: 100%; height: 100%; display: block; }}
    .youtube-now-kicker {{ color: #ff5d5d; font-size: .68rem; font-weight: 850; text-transform: uppercase; }}
    .youtube-now h3 {{ margin: 7px 0 4px; font-size: clamp(1.3rem, 2.5vw, 2rem); }}
    .youtube-now p {{ margin: 0; color: var(--muted); }}
    .youtube-open {{
      display: inline-block;
      margin-top: 16px;
      color: var(--accent);
      font-size: .82rem;
      font-weight: 780;
      text-decoration: none;
    }}
    .recommendation-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      padding-top: 18px;
    }}
    .recommendation-card {{
      min-width: 0;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      transition: border-color .18s ease, transform .18s ease;
    }}
    .recommendation-card:hover {{ border-color: color-mix(in srgb, var(--accent) 55%, var(--line)); transform: translateY(-2px); }}
    .recommendation-thumb {{
      position: relative;
      width: 100%;
      aspect-ratio: 16 / 9;
      overflow: hidden;
      background: var(--surface-2);
    }}
    .recommendation-thumb img {{ width: 100%; height: 100%; display: block; object-fit: cover; }}
    .recommendation-thumb button {{
      position: absolute;
      inset: 0;
      width: 100%;
      margin: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: transparent;
    }}
    .recommendation-thumb button::after {{
      content: "▶";
      position: absolute;
      right: 10px;
      bottom: 10px;
      width: 38px;
      height: 38px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background: var(--accent);
      color: #07120d;
      box-shadow: 0 8px 22px rgba(0,0,0,.35);
    }}
    .recommendation-copy {{ padding: 11px 12px 13px; }}
    .recommendation-title {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 780; }}
    .recommendation-meta {{ margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--muted); font-size: .76rem; }}
    .tab-notice {{
      display: inline-block;
      width: 6px;
      height: 6px;
      margin-left: 5px;
      border-radius: 50%;
      background: var(--accent);
      vertical-align: middle;
    }}
    .tab-notice[hidden] {{ display: none; }}
    .codex-toggle {{
      min-width: 0;
      padding: 10px 14px;
      border-color: var(--warm);
      background: var(--warm);
      color: #1a0c09;
      font-size: .82rem;
    }}
    .codex-chat {{ border-color: var(--line); background: var(--surface); }}

    @media (max-width: 760px) {{
      body[data-active-tab="player"] {{ min-height: 100dvh; }}
      main, body[data-active-tab="player"] main {{
        width: calc(100% - 24px);
        grid-template-rows: 48px 44px minmax(0, 1fr);
        padding: 8px 0 max(92px, calc(76px + env(safe-area-inset-bottom)));
      }}
      body[data-active-tab="player"] main {{ padding-bottom: max(8px, env(safe-area-inset-bottom)); }}
      .brand-subtitle {{ display: none; }}
      .tabs {{ width: 100%; grid-template-columns: repeat(4, 1fr); }}
      .tab {{ min-width: 0; width: 100%; padding: 6px 8px; }}
      .player-card {{
        grid-template-columns: 1fr;
        grid-template-rows: minmax(120px, 36dvh) auto;
        align-content: center;
        gap: 14px;
        padding: 8px 4px 10px;
      }}
      .artwork-shell {{
        width: min(100%, 36dvh, 300px);
        max-height: 100%;
        justify-self: center;
      }}
      .player-console {{
        width: 100%;
        gap: 12px;
        align-self: start;
      }}
      .player-info {{ text-align: center; }}
      .eyebrow {{ margin-bottom: 5px; font-size: .62rem; }}
      .player-title {{ margin-bottom: 4px; font-size: 1.38rem; line-height: 1.1; }}
      .player-artist {{ font-size: .82rem; }}
      .progress {{ gap: 2px 8px; }}
      .player-controls {{ justify-content: center; gap: 8px; }}
      .icon-button {{ width: 40px; height: 40px; }}
      .play-button {{ width: 54px; height: 54px; margin: 0; }}
      .player-options {{
        grid-template-columns: minmax(0, 1fr) 118px;
        gap: 14px;
        padding-top: 10px;
      }}
      .setting-label, .setting-value {{ font-size: .66rem; }}
      .crossfade-control select {{ min-height: 32px; padding-top: 5px; padding-bottom: 5px; }}
      [data-panel="playlist"] > form {{
        position: relative;
        display: grid;
        grid-template-columns: 1fr 1fr;
      }}
      [data-panel="playlist"] > form input {{ grid-column: 1 / -1; }}
      [data-panel="playlist"] > form button {{ grid-column: 1 / -1; }}
      .track {{ grid-template-columns: 46px minmax(0, 1fr) 38px; gap: 10px; }}
      .track-cover {{ width: 46px; height: 46px; }}
      a.track-download {{ display: none; }}
      .track .chips {{ display: none; }}
      .track-play {{ width: 38px; height: 38px; }}
      [data-panel="friends"] {{ padding-top: 10px; }}
      .friend-import {{ grid-template-columns: 1fr; gap: 22px; padding: 20px 16px; }}
      .friend-import h2, .friend-library h2 {{ font-size: 1.35rem; }}
      .friend-import ol {{ font-size: .9rem; }}
      .csv-import input, .csv-import button, .spotify-import input, .spotify-import button {{ width: 100%; margin-left: 0; margin-right: 0; }}
      .friend-library-head {{ align-items: start; }}
      .friend-play-all {{ width: auto; margin: 0; padding: 9px 11px; font-size: .76rem; }}
      .friend-track {{ grid-template-columns: 44px minmax(0, 1fr) auto; gap: 10px; }}
      .friend-track .track-cover, .friend-cover-fallback {{ width: 44px; height: 44px; }}
      .friend-track .chips {{ display: none; }}
      [data-panel="discover"] {{ padding-top: 14px; }}
      .discover-header {{ display: block; }}
      .recommendation-status {{ display: block; margin-top: 8px; white-space: normal; }}
      .youtube-stage {{ grid-template-columns: 1fr; gap: 14px; }}
      .recommendation-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
      .recommendation-copy {{ padding: 9px 10px 11px; }}
      .mini-player {{ grid-template-columns: 42px minmax(0, 1fr) auto auto; }}
      .mini-cover-shell {{ width: 42px; height: 42px; }}
      .codex-chat {{ bottom: max(64px, calc(8px + env(safe-area-inset-bottom))); }}
    }}

    @media (max-height: 690px) and (max-width: 760px) {{
      .player-card {{ grid-template-rows: minmax(100px, 28dvh) auto; }}
      .artwork-shell {{ width: min(100%, 28dvh, 220px); }}
      .player-console {{ gap: 8px; }}
      .player-options {{ padding-top: 7px; }}
      .eyebrow {{ display: none; }}
    }}

    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{ animation-duration: .01ms !important; transition-duration: .01ms !important; }}
    }}
  </style>
</head>
<body data-active-tab="{active_view}">
  <main>
    <header class="app-header">
      <div class="brand">
        <span class="brand-mark">EP</span>
        <div>
          <h1>Library</h1>
          <div class="brand-subtitle">{len(all_files)} Songs · {free / (1024 ** 3):.1f} GB frei</div>
        </div>
      </div>
      <form action="/logout" method="post">
        <button class="logout-button" type="submit">Abmelden</button>
      </form>
    </header>
    <nav class="tabs" aria-label="Ansichten">
      <button class="{tab_class("player")}" type="button" data-tab="player">Player</button>
      <button class="{tab_class("playlist")}" type="button" data-tab="playlist">Playlist</button>
      <button class="{tab_class("friends")}" type="button" data-tab="friends">Freunde</button>
      <button class="{tab_class("discover")}" type="button" data-tab="discover">Neu<span class="tab-notice" id="recommendation-notice" hidden></span></button>
    </nav>
    <section class="{panel_class("player")}" data-panel="player">
      <div class="player-card">
        <div class="artwork-shell">
          <div class="cover-fallback" id="cover-fallback">S</div>
          <img class="cover" id="cover" alt="" decoding="async">
          <div class="artwork-glow" aria-hidden="true"></div>
        </div>
        <div class="player-console">
          <div class="player-info">
            <div class="eyebrow">Now playing</div>
            <h2 class="player-title" id="player-title">Kein Song ausgewählt</h2>
            <div class="player-artist" id="player-artist">Wähle einen Song aus der Playlist</div>
          </div>
          <div class="progress">
            <input id="progress" type="range" min="0" max="1000" value="0" aria-label="Position">
            <span id="current-time">0:00</span>
            <span id="duration">0:00</span>
          </div>
          <div class="player-controls">
            <button class="icon-button shuffle-button" type="button" id="shuffle" aria-label="Zufallsmodus" aria-pressed="false">&#8644;</button>
            <button class="icon-button" type="button" id="prev" aria-label="Vorheriger Song">&#9198;</button>
            <button class="play-button" type="button" id="play" aria-label="Abspielen">&#9654;</button>
            <button class="icon-button" type="button" id="next" aria-label="Nächster Song">&#9197;</button>
            <a class="icon-button player-download" id="player-download" href="#" aria-label="Song herunterladen">&#8595;</a>
          </div>
          <div class="player-options">
            <label class="volume-control" for="volume">
              <span class="setting-label">Lautstärke</span>
              <input id="volume" type="range" min="0" max="100" value="82" aria-label="Lautstärke">
              <span id="volume-value" class="setting-value">82%</span>
            </label>
            <label class="crossfade-control" for="crossfade">
              <span class="setting-label">Überblendung</span>
              <select id="crossfade" aria-label="Überblendung">
                <option value="0">Aus</option>
                <option value="3">3 Sek.</option>
                <option value="6">6 Sek.</option>
                <option value="9">9 Sek.</option>
              </select>
            </label>
          </div>
        </div>
      </div>
    </section>
    <section class="{panel_class("playlist")}" data-panel="playlist">
      <form action="/" method="get">
        <input name="q" value="{html.escape(query)}" placeholder="Suchen" autocomplete="off">
        {self.select("genre", "Alle Genres", genre, genre_options)}
        {self.select("artist", "Alle Artists", artist, artist_options)}
        {self.sort_select(sort)}
        <button type="submit">Suchen</button>
      </form>
      {rows}
    </section>
    <section class="{panel_class("friends")}" data-panel="friends">
      {friends_rows}
    </section>
    <section class="{panel_class("discover")}" data-panel="discover">
      <header class="discover-header">
        <div>
          <div class="friend-kicker">Für dich entdeckt</div>
          <h2>Neue Musik</h2>
          <p>YouTube-Mix-Vorschläge passend zum gehörten Song, ohne deine Library automatisch zu verändern.</p>
        </div>
        <span class="recommendation-status" id="recommendation-status">Höre einen Song, um Vorschläge zu laden.</span>
      </header>
      <div class="youtube-stage" id="youtube-stage" hidden>
        <div class="youtube-frame"><div id="youtube-player"></div></div>
        <div class="youtube-now">
          <div class="youtube-now-kicker">YouTube</div>
          <h3 id="youtube-title">Vorschlag</h3>
          <p id="youtube-artist">YouTube Music</p>
          <a class="youtube-open" id="youtube-open" href="https://www.youtube.com/" target="_blank" rel="noopener noreferrer">Auf YouTube öffnen ↗</a>
        </div>
      </div>
      <div class="recommendation-grid" id="recommendation-grid"></div>
    </section>
  </main>
  <div class="mini-player" id="mini-player">
    <div class="mini-cover-shell">
      <img class="mini-cover" id="mini-cover" alt="">
      <span id="mini-cover-fallback">S</span>
    </div>
    <div>
      <div class="mini-title" id="mini-title">Kein Song ausgewählt</div>
      <div class="mini-artist" id="mini-artist">Player</div>
    </div>
    <button class="icon-button" type="button" id="mini-play" aria-label="Abspielen">&#9654;</button>
    <button class="icon-button" type="button" id="mini-next" aria-label="Weiter">&#9197;</button>
  </div>
  <button class="codex-toggle" type="button" id="codex-toggle" aria-expanded="false">Codex</button>
  <aside class="codex-chat" id="codex-chat" aria-label="Codex-Chat">
    <div class="codex-chat-header">
      <div>
        <div class="codex-chat-title">Codex</div>
        <div class="codex-chat-subtitle">Gemeinsamer Website-Chat</div>
      </div>
      <button class="codex-close" type="button" id="codex-close" aria-label="Chat schließen">X</button>
    </div>
    <div class="codex-messages" id="codex-messages" aria-live="polite"></div>
    <form class="codex-compose" id="codex-form">
      <textarea id="codex-input" maxlength="6000" placeholder="Website-Änderung beschreiben oder etwas fragen" required></textarea>
      <button type="submit" id="codex-send">Senden</button>
    </form>
  </aside>
  <script id="tracks-data" type="application/json">{player_json_script}</script>
  <script id="friend-queues-data" type="application/json">{friend_queues_script}</script>
  <script>
    (() => {{
      const tracks = JSON.parse(document.getElementById("tracks-data").textContent || "[]");
      const friendQueues = JSON.parse(document.getElementById("friend-queues-data").textContent || "{{}}");
      const audioDecks = [new Audio(), new Audio()];
      audioDecks.forEach((deck) => {{ deck.preload = "metadata"; }});
      let audio = audioDecks[0];
      let current = tracks.length ? 0 : -1;
      let seeking = false;
      let transitioning = false;
      let fadeFrame = 0;
      let fadeToken = 0;
      let shuffleEnabled = false;
      let shufflePlayed = new Set();
      let userVolume = 0.82;
      let crossfadeSeconds = 0;
      let queue = tracks.map((_, index) => index);
      let queueScope = [...queue];
      let recommendationRequestedFor = "";
      let recommendationLoading = false;
      let youtubeApiPromise = null;
      let youtubePlayer = null;
      let youtubeActive = false;
      let youtubeSuggestion = null;
      let recommendationItems = [];
      const inlineAudios = Array.from(document.querySelectorAll("[data-inline-audio]"));
      const els = {{
        title: document.getElementById("player-title"),
        artist: document.getElementById("player-artist"),
        miniTitle: document.getElementById("mini-title"),
        miniArtist: document.getElementById("mini-artist"),
        miniCover: document.getElementById("mini-cover"),
        miniCoverFallback: document.getElementById("mini-cover-fallback"),
        cover: document.getElementById("cover"),
        coverFallback: document.getElementById("cover-fallback"),
        play: document.getElementById("play"),
        miniPlay: document.getElementById("mini-play"),
        next: document.getElementById("next"),
        miniNext: document.getElementById("mini-next"),
        prev: document.getElementById("prev"),
        shuffle: document.getElementById("shuffle"),
        progress: document.getElementById("progress"),
        currentTime: document.getElementById("current-time"),
        duration: document.getElementById("duration"),
        volume: document.getElementById("volume"),
        volumeValue: document.getElementById("volume-value"),
        crossfade: document.getElementById("crossfade"),
        download: document.getElementById("player-download"),
        recommendationNotice: document.getElementById("recommendation-notice"),
        recommendationStatus: document.getElementById("recommendation-status"),
        recommendationGrid: document.getElementById("recommendation-grid"),
        youtubeStage: document.getElementById("youtube-stage"),
        youtubeTitle: document.getElementById("youtube-title"),
        youtubeArtist: document.getElementById("youtube-artist"),
        youtubeOpen: document.getElementById("youtube-open"),
      }};
      const chat = {{
        panel: document.getElementById("codex-chat"),
        toggle: document.getElementById("codex-toggle"),
        close: document.getElementById("codex-close"),
        messages: document.getElementById("codex-messages"),
        form: document.getElementById("codex-form"),
        input: document.getElementById("codex-input"),
        send: document.getElementById("codex-send"),
      }};

      function addChatMessage(message) {{
        const item = document.createElement("article");
        item.className = "codex-message " + (message.role || "assistant");
        item.textContent = message.content || "";
        if (message.proposal && message.proposal.id) {{
          const proposal = document.createElement("div");
          proposal.className = "codex-proposal";
          const title = document.createElement("div");
          title.textContent = "Vorgeschlagene Änderung: " + (message.proposal.summary || "Website anpassen");
          const apply = document.createElement("button");
          apply.type = "button";
          apply.textContent = "Änderung anwenden";
          apply.addEventListener("click", () => applyProposal(message.proposal.id, apply));
          proposal.append(title, apply);
          item.append(proposal);
        }}
        chat.messages.append(item);
        chat.messages.scrollTop = chat.messages.scrollHeight;
      }}

      async function chatRequest(path, data) {{
        const response = await fetch(path, {{
          method: "POST",
          headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
          body: new URLSearchParams(data),
        }});
        const payload = await response.json().catch(() => ({{ error: "Ungültige Serverantwort." }}));
        if (!response.ok) throw new Error(payload.error || "Die Anfrage ist fehlgeschlagen.");
        return payload;
      }}

      async function applyProposal(proposalId, button) {{
        button.disabled = true;
        button.textContent = "Wird angewendet ...";
        try {{
          const result = await chatRequest("/api/codex/apply", {{ proposal_id: proposalId }});
          addChatMessage({{ role: "system", content: result.message || "Änderung angewendet. Die Seite wird neu geladen." }});
          window.setTimeout(() => window.location.reload(), 1600);
        }} catch (error) {{
          addChatMessage({{ role: "system", content: error.message }});
          button.disabled = false;
          button.textContent = "Änderung anwenden";
        }}
      }}

      async function loadChatHistory() {{
        try {{
          const response = await fetch("/api/codex/history", {{ cache: "no-store" }});
          const payload = await response.json();
          if (!payload.configured) {{
            addChatMessage({{ role: "system", content: "Codex wartet noch auf die Server-Konfiguration." }});
            return;
          }}
          (payload.messages || []).forEach(addChatMessage);
        }} catch (_) {{ addChatMessage({{ role: "system", content: "Chatverlauf konnte nicht geladen werden." }}); }}
      }}

      function setChatOpen(open) {{
        chat.panel.classList.toggle("open", open);
        chat.toggle.setAttribute("aria-expanded", String(open));
        chat.toggle.textContent = open ? "Schließen" : "Codex";
        if (open) {{
          chat.input.focus();
          chat.messages.scrollTop = chat.messages.scrollHeight;
        }}
      }}

      chat.toggle.addEventListener("click", () => setChatOpen(!chat.panel.classList.contains("open")));
      chat.close.addEventListener("click", () => setChatOpen(false));
      chat.form.addEventListener("submit", async (event) => {{
        event.preventDefault();
        const message = chat.input.value.trim();
        if (!message) return;
        addChatMessage({{ role: "user", content: message }});
        chat.input.value = "";
        chat.send.disabled = true;
        chat.send.textContent = "Denkt ...";
        try {{
          const result = await chatRequest("/api/codex/chat", {{ message }});
          addChatMessage(result.message);
        }} catch (error) {{
          addChatMessage({{ role: "system", content: error.message }});
        }} finally {{
          chat.send.disabled = false;
          chat.send.textContent = "Senden";
          chat.input.focus();
        }}
      }});

      function fmt(value) {{
        if (!Number.isFinite(value)) return "0:00";
        const minutes = Math.floor(value / 60);
        const seconds = Math.floor(value % 60).toString().padStart(2, "0");
        return minutes + ":" + seconds;
      }}

      function setTab(name) {{
        document.body.dataset.activeTab = name;
        if (name === "discover") els.recommendationNotice.hidden = true;
        document.querySelectorAll(".tab").forEach((tab) => {{
          tab.classList.toggle("active", tab.dataset.tab === name);
        }});
        document.querySelectorAll(".panel").forEach((panel) => {{
          panel.classList.toggle("active", panel.dataset.panel === name);
        }});
      }}

      function updateButtons() {{
        const text = audio.paused ? "▶" : "Ⅱ";
        els.play.textContent = text;
        els.miniPlay.textContent = text;
      }}

      function persistPlayerSettings() {{
        try {{
          window.localStorage.setItem("player-volume", String(userVolume));
          window.localStorage.setItem("player-crossfade", String(crossfadeSeconds));
          window.localStorage.setItem("player-shuffle", shuffleEnabled ? "1" : "0");
        }} catch (_) {{}}
      }}

      function applyVolume(value, persist = true) {{
        userVolume = Math.max(0, Math.min(1, value));
        if (!transitioning) audioDecks.forEach((deck) => {{ deck.volume = userVolume; }});
        inlineAudios.forEach((element) => {{ element.volume = userVolume; }});
        if (youtubePlayer?.setVolume) youtubePlayer.setVolume(Math.round(userVolume * 100));
        els.volume.value = String(Math.round(userVolume * 100));
        els.volumeValue.textContent = Math.round(userVolume * 100) + "%";
        if (persist) persistPlayerSettings();
      }}

      function pauseInlineAudios(except = null) {{
        inlineAudios.forEach((element) => {{
          if (except && element === except) return;
          element.pause();
        }});
      }}

      function setQueue(nextQueue) {{
        queue = nextQueue.length ? nextQueue : tracks.map((_, index) => index);
        queueScope = [...queue];
      }}

      function queueIndexFor(trackIndex) {{
        const index = queue.indexOf(trackIndex);
        return index >= 0 ? index : 0;
      }}

      function chooseInitialTrack() {{
        if (!tracks.length) return -1;
        let lastTrackSource = "";
        try {{
          lastTrackSource = window.sessionStorage.getItem("player-last-track-source") || "";
        }} catch (_) {{}}
        const candidates = tracks
          .map((track, index) => (track.src === lastTrackSource ? -1 : index))
          .filter((index) => index >= 0);
        const pool = candidates.length ? candidates : tracks.map((_, index) => index);
        return pool[Math.floor(Math.random() * pool.length)];
      }}

      function renderTrack() {{
        const track = tracks[current];
        if (!track) return;
        els.title.textContent = track.title;
        els.artist.textContent = [track.artist, track.album].filter(Boolean).join(" · ");
        els.miniTitle.textContent = track.title;
        els.miniArtist.textContent = track.artist || "Player";
        const initial = (track.title || "S").slice(0, 1).toUpperCase();
        els.coverFallback.textContent = initial;
        els.miniCoverFallback.textContent = initial;
        els.cover.classList.remove("loaded");
        els.miniCover.classList.remove("loaded");
        els.cover.src = track.cover;
        els.miniCover.src = track.cover;
        els.download.href = track.download;
        document.querySelectorAll("[data-track-index]").forEach((row) => {{
          row.classList.toggle("current", Number(row.dataset.trackIndex) === current);
        }});
      }}

      function stopYoutubeForLocal() {{
        if (youtubePlayer?.pauseVideo) youtubePlayer.pauseVideo();
        youtubeActive = false;
        youtubeSuggestion = null;
      }}

      function settleAudioTransition() {{
        fadeToken += 1;
        window.cancelAnimationFrame(fadeFrame);
        audioDecks.forEach((deck) => {{
          if (deck === audio) {{
            deck.volume = userVolume;
            return;
          }}
          deck.pause();
          deck.currentTime = 0;
          deck.volume = userVolume;
        }});
        transitioning = false;
        updateButtons();
      }}

      function loadTrack(index, autoplay = false, overlap = false) {{
        if (!tracks.length) return;
        const transitionToken = ++fadeToken;
        const safeIndex = ((index % queue.length) + queue.length) % queue.length;
        const nextTrackIndex = queue[safeIndex];
        const outgoing = audio;
        const shouldOverlap =
          overlap &&
          autoplay &&
          crossfadeSeconds > 0 &&
          document.visibilityState === "visible" &&
          !outgoing.paused &&
          outgoing.src &&
          current !== nextTrackIndex;
        const incoming = shouldOverlap ? audioDecks.find((deck) => deck !== outgoing) : outgoing;
        const previousTrack = current;
        if (!incoming) return;
        if (!shouldOverlap) {{
          window.cancelAnimationFrame(fadeFrame);
          audioDecks.forEach((deck) => {{
            if (deck !== incoming) {{
              deck.pause();
              deck.currentTime = 0;
            }}
          }});
        }}
        current = nextTrackIndex;
        audio = incoming;
        const track = tracks[current];
        if (autoplay) stopYoutubeForLocal();
        try {{
          window.sessionStorage.setItem("player-last-track-source", track.src);
        }} catch (_) {{}}
        if (incoming.src !== new URL(track.src, window.location.href).href) {{
          incoming.src = track.src;
        }}
        if (shuffleEnabled) shufflePlayed.add(current);
        pauseInlineAudios();
        renderTrack();
        if (shouldOverlap) {{
          transitioning = true;
          incoming.volume = 0;
          incoming.play().then(() => {{
            if (transitionToken !== fadeToken || !transitioning) return;
            const started = performance.now();
            const duration = Math.max(250, crossfadeSeconds * 1000);
            const fade = (now) => {{
              if (transitionToken !== fadeToken || !transitioning) return;
              const progress = Math.min(1, (now - started) / duration);
              outgoing.volume = userVolume * (1 - progress);
              incoming.volume = userVolume * progress;
              if (progress < 1) {{
                fadeFrame = window.requestAnimationFrame(fade);
                return;
              }}
              outgoing.pause();
              outgoing.currentTime = 0;
              outgoing.volume = userVolume;
              incoming.volume = userVolume;
              transitioning = false;
            }};
            fadeFrame = window.requestAnimationFrame(fade);
          }}).catch(() => {{
            if (transitionToken !== fadeToken) return;
            incoming.pause();
            incoming.volume = userVolume;
            audio = outgoing;
            current = previousTrack;
            renderTrack();
            transitioning = false;
          }});
        }} else {{
          incoming.volume = userVolume;
          transitioning = false;
          if (autoplay) incoming.play().catch(() => {{}});
        }}
      }}

      function togglePlay() {{
        if (youtubeActive && document.body.dataset.activeTab === "discover" && youtubePlayer) {{
          const state = youtubePlayer.getPlayerState();
          if (state === 1) youtubePlayer.pauseVideo();
          else youtubePlayer.playVideo();
          return;
        }}
        if (!tracks.length) return;
        if (current < 0) loadTrack(0);
        if (audio.paused) {{
          stopYoutubeForLocal();
          renderTrack();
          audio.play().catch(() => {{}});
        }} else {{
          window.cancelAnimationFrame(fadeFrame);
          fadeToken += 1;
          transitioning = false;
          audioDecks.forEach((deck) => {{
            deck.pause();
            deck.volume = userVolume;
          }});
          audio.pause();
        }}
      }}

      function chooseShuffleNext() {{
        if (queueScope.length < 2) return queueIndexFor(current);
        if (shufflePlayed.size >= queueScope.length) {{
          shufflePlayed.clear();
          if (current >= 0) shufflePlayed.add(current);
        }}
        const sourceGenres = new Set((tracks[current]?.genres || []).map((genre) => genre.toLowerCase()));
        const remaining = queueScope
          .filter((index) => index !== current && !shufflePlayed.has(index));
        const pool = remaining.length ? remaining : queueScope.filter((index) => index !== current);
        const scored = pool.map((index) => {{
          const overlapScore = (tracks[index].genres || []).reduce(
            (score, genre) => score + (sourceGenres.has(genre.toLowerCase()) ? 1 : 0),
            0,
          );
          return {{ index, overlapScore }};
        }});
        const bestScore = Math.max(0, ...scored.map((item) => item.overlapScore));
        const related = scored.filter((item) => item.overlapScore === bestScore).map((item) => item.index);
        const nextTrackIndex = related[Math.floor(Math.random() * related.length)];
        queue.push(nextTrackIndex);
        return queue.length - 1;
      }}

      function next(forceAutoplay = null, overlap = true) {{
        if (
          document.body.dataset.activeTab === "discover" &&
          youtubeSuggestion &&
          recommendationItems.length
        ) {{
          const index = recommendationItems.findIndex((item) => item.id === youtubeSuggestion.id);
          playRecommendation(recommendationItems[(index + 1) % recommendationItems.length]);
          return;
        }}
        const position = shuffleEnabled ? chooseShuffleNext() : queueIndexFor(current) + 1;
        loadTrack(position, forceAutoplay === null ? !audio.paused : forceAutoplay, overlap);
      }}

      function prev() {{
        if (
          document.body.dataset.activeTab === "discover" &&
          youtubeSuggestion &&
          recommendationItems.length
        ) {{
          const index = recommendationItems.findIndex((item) => item.id === youtubeSuggestion.id);
          playRecommendation(recommendationItems[(index - 1 + recommendationItems.length) % recommendationItems.length]);
          return;
        }}
        if (audio.currentTime > 4) {{
          audio.currentTime = 0;
          return;
        }}
        loadTrack(queueIndexFor(current) - 1, !audio.paused, false);
      }}

      function shuffle() {{
        if (!tracks.length) return;
        shuffleEnabled = !shuffleEnabled;
        if (shuffleEnabled) {{
          shufflePlayed = new Set(current >= 0 ? [current] : []);
          if (!queueScope.includes(current)) queueScope = tracks.map((_, index) => index);
          queue = current >= 0 ? [current] : [];
        }} else {{
          queue = [...queueScope];
          shufflePlayed.clear();
        }}
        els.shuffle.setAttribute("aria-pressed", String(shuffleEnabled));
        persistPlayerSettings();
      }}

      function playGenre(genre) {{
        const nextQueue = tracks
          .map((track, index) => (track.genres.includes(genre) ? index : -1))
          .filter((index) => index >= 0);
        if (!nextQueue.length) return;
        shuffleEnabled = false;
        shufflePlayed.clear();
        els.shuffle.setAttribute("aria-pressed", "false");
        setQueue(nextQueue);
        loadTrack(0, true);
        setTab("player");
      }}

      function youtubeApi() {{
        if (window.YT?.Player) return Promise.resolve(window.YT);
        if (youtubeApiPromise) return youtubeApiPromise;
        youtubeApiPromise = new Promise((resolve) => {{
          const previous = window.onYouTubeIframeAPIReady;
          window.onYouTubeIframeAPIReady = () => {{
            if (typeof previous === "function") previous();
            resolve(window.YT);
          }};
          const script = document.createElement("script");
          script.src = "https://www.youtube.com/iframe_api";
          script.async = true;
          document.head.append(script);
        }});
        return youtubeApiPromise;
      }}

      function updateYoutubeMiniPlayer(playing) {{
        if (!youtubeSuggestion) return;
        els.miniTitle.textContent = youtubeSuggestion.title;
        els.miniArtist.textContent = youtubeSuggestion.artist || "YouTube Music";
        els.miniCoverFallback.textContent = "YT";
        els.miniCover.classList.remove("loaded");
        els.miniCover.src = youtubeSuggestion.thumbnail;
        const text = playing ? "Ⅱ" : "▶";
        els.play.textContent = text;
        els.miniPlay.textContent = text;
      }}

      async function playRecommendation(suggestion) {{
        settleAudioTransition();
        audioDecks.forEach((deck) => {{
          deck.pause();
          deck.volume = userVolume;
        }});
        youtubeActive = true;
        youtubeSuggestion = suggestion;
        els.youtubeStage.hidden = false;
        els.youtubeTitle.textContent = suggestion.title;
        els.youtubeArtist.textContent = suggestion.artist || "YouTube Music";
        els.youtubeOpen.href = suggestion.url;
        els.recommendationStatus.textContent = "YouTube-Vorschlag ausgewählt";
        updateYoutubeMiniPlayer(false);
        const YTApi = await youtubeApi();
        if (youtubePlayer?.loadVideoById) {{
          youtubePlayer.setVolume(Math.round(userVolume * 100));
          youtubePlayer.loadVideoById(suggestion.id);
          return;
        }}
        youtubePlayer = new YTApi.Player("youtube-player", {{
          videoId: suggestion.id,
          playerVars: {{ autoplay: 1, controls: 1, playsinline: 1, rel: 0 }},
          events: {{
            onReady: (event) => {{
              event.target.setVolume(Math.round(userVolume * 100));
              event.target.playVideo();
            }},
            onStateChange: (event) => {{
              if (event.data === YTApi.PlayerState.PLAYING) {{
                audioDecks.forEach((deck) => deck.pause());
                youtubeActive = true;
                updateYoutubeMiniPlayer(true);
              }} else if (
                event.data === YTApi.PlayerState.PAUSED ||
                event.data === YTApi.PlayerState.ENDED
              ) {{
                updateYoutubeMiniPlayer(false);
              }}
            }},
            onError: (event) => {{
              youtubeActive = false;
              els.youtubeStage.dataset.error = String(event.data || "");
              els.recommendationStatus.textContent = "Einbettung gesperrt. Wähle einen anderen Vorschlag oder öffne YouTube.";
              updateButtons();
            }},
          }},
        }});
      }}

      function renderRecommendations(suggestions) {{
        recommendationItems = suggestions;
        els.recommendationGrid.replaceChildren();
        suggestions.forEach((suggestion) => {{
          const card = document.createElement("article");
          card.className = "recommendation-card";
          const thumb = document.createElement("div");
          thumb.className = "recommendation-thumb";
          const image = document.createElement("img");
          image.src = suggestion.thumbnail;
          image.alt = "";
          image.loading = "lazy";
          const play = document.createElement("button");
          play.type = "button";
          play.ariaLabel = suggestion.title + " auf YouTube abspielen";
          play.addEventListener("click", () => playRecommendation(suggestion));
          thumb.append(image, play);
          const copy = document.createElement("div");
          copy.className = "recommendation-copy";
          const title = document.createElement("div");
          title.className = "recommendation-title";
          title.textContent = suggestion.title;
          const meta = document.createElement("div");
          meta.className = "recommendation-meta";
          meta.textContent = [suggestion.artist, suggestion.duration].filter(Boolean).join(" · ");
          copy.append(title, meta);
          card.append(thumb, copy);
          els.recommendationGrid.append(card);
        }});
      }}

      async function loadRecommendations() {{
        const track = tracks[current];
        if (!track || recommendationLoading || recommendationRequestedFor === track.src) return;
        recommendationRequestedFor = track.src;
        recommendationLoading = true;
        els.recommendationStatus.classList.add("loading");
        els.recommendationStatus.textContent = "YouTube Music wird durchsucht …";
        try {{
          const response = await fetch("/api/recommendations?" + new URLSearchParams({{ source: track.src }}), {{
            cache: "no-store",
          }});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "Vorschläge konnten nicht geladen werden.");
          renderRecommendations(payload.suggestions || []);
          els.recommendationStatus.textContent = (payload.suggestions || []).length + " neue Vorschläge";
          if (document.body.dataset.activeTab !== "discover") els.recommendationNotice.hidden = false;
        }} catch (error) {{
          els.recommendationStatus.textContent = error.message;
        }} finally {{
          recommendationLoading = false;
          els.recommendationStatus.classList.remove("loading");
        }}
      }}

      document.querySelectorAll(".tab").forEach((tab) => {{
        tab.addEventListener("click", () => setTab(tab.dataset.tab));
      }});
      document.querySelectorAll("[data-play-index]").forEach((button) => {{
        button.addEventListener("click", () => {{
          shuffleEnabled = false;
          shufflePlayed.clear();
          els.shuffle.setAttribute("aria-pressed", "false");
          const ownerQueue = friendQueues[button.dataset.queueOwner || ""];
          setQueue(ownerQueue?.length ? ownerQueue : tracks.map((_, index) => index));
          loadTrack(queueIndexFor(Number(button.dataset.playIndex)), true);
          setTab("player");
        }});
      }});
      document.querySelectorAll("[data-friend-play]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const nextQueue = friendQueues[button.dataset.friendPlay || ""] || [];
          if (!nextQueue.length) return;
          shuffleEnabled = false;
          shufflePlayed.clear();
          els.shuffle.setAttribute("aria-pressed", "false");
          setQueue(nextQueue);
          loadTrack(0, true);
          setTab("player");
        }});
      }});
      document.querySelectorAll("[data-genre-play]").forEach((button) => {{
        button.addEventListener("click", () => {{
          playGenre(button.dataset.genrePlay || "");
        }});
      }});
      inlineAudios.forEach((element) => {{
        element.addEventListener("play", () => {{
          audioDecks.forEach((deck) => {{ deck.pause(); }});
          pauseInlineAudios(element);
        }});
      }});
      els.play.addEventListener("click", togglePlay);
      els.miniPlay.addEventListener("click", togglePlay);
      els.next.addEventListener("click", () => next());
      els.miniNext.addEventListener("click", () => next());
      els.prev.addEventListener("click", prev);
      els.shuffle.addEventListener("click", shuffle);
      els.cover.addEventListener("load", () => {{ els.cover.classList.add("loaded"); }});
      els.cover.addEventListener("error", () => {{ els.cover.classList.remove("loaded"); }});
      els.miniCover.addEventListener("load", () => {{ els.miniCover.classList.add("loaded"); }});
      els.miniCover.addEventListener("error", () => {{ els.miniCover.classList.remove("loaded"); }});
      els.progress.addEventListener("input", () => {{
        seeking = true;
      }});
      els.progress.addEventListener("change", () => {{
        if (Number.isFinite(audio.duration)) {{
          audio.currentTime = (Number(els.progress.value) / 1000) * audio.duration;
        }}
        seeking = false;
      }});
      els.volume.addEventListener("input", () => {{
        applyVolume(Number(els.volume.value) / 100);
      }});
      els.crossfade.addEventListener("change", () => {{
        crossfadeSeconds = Number(els.crossfade.value) || 0;
        persistPlayerSettings();
      }});
      document.addEventListener("visibilitychange", () => {{
        if (document.visibilityState === "hidden" && transitioning) settleAudioTransition();
      }});
      audioDecks.forEach((deck) => {{
        deck.addEventListener("play", () => {{
          if (deck === audio) {{
            if (youtubePlayer?.pauseVideo) youtubePlayer.pauseVideo();
            youtubeActive = false;
            updateButtons();
            pauseInlineAudios();
          }}
        }});
        deck.addEventListener("pause", () => {{
          if (deck === audio) updateButtons();
        }});
        deck.addEventListener("ended", () => {{
          if (deck === audio && !transitioning) next(true, false);
        }});
        deck.addEventListener("loadedmetadata", () => {{
          if (deck === audio) els.duration.textContent = fmt(deck.duration);
        }});
        deck.addEventListener("timeupdate", () => {{
          if (deck !== audio) return;
          els.currentTime.textContent = fmt(deck.currentTime);
          if (!seeking && Number.isFinite(deck.duration) && deck.duration > 0) {{
            els.progress.value = String(Math.round((deck.currentTime / deck.duration) * 1000));
          }}
          const remaining = deck.duration - deck.currentTime;
          if (
            crossfadeSeconds > 0 &&
            !transitioning &&
            !seeking &&
            !deck.paused &&
            Number.isFinite(remaining) &&
            remaining > 0 &&
            remaining <= crossfadeSeconds
          ) {{
            next(true, true);
          }}
          if (!deck.paused && deck.currentTime >= 35 && recommendationRequestedFor !== tracks[current]?.src) {{
            loadRecommendations();
          }}
        }});
      }});

      let touchStart = 0;
      document.addEventListener("touchstart", (event) => {{
        touchStart = event.changedTouches[0].clientX;
      }}, {{ passive: true }});
      document.addEventListener("touchend", (event) => {{
        const delta = event.changedTouches[0].clientX - touchStart;
        if (Math.abs(delta) < 70) return;
        const order = ["player", "playlist", "friends", "discover"];
        const active = document.querySelector(".tab.active")?.dataset.tab || "player";
        const index = order.indexOf(active);
        if (delta < 0 && index < order.length - 1) setTab(order[index + 1]);
        if (delta > 0 && index > 0) setTab(order[index - 1]);
      }}, {{ passive: true }});

      try {{
        const savedVolume = Number(window.localStorage.getItem("player-volume"));
        const savedCrossfade = Number(window.localStorage.getItem("player-crossfade"));
        shuffleEnabled = window.localStorage.getItem("player-shuffle") === "1";
        if (Number.isFinite(savedVolume)) userVolume = Math.max(0, Math.min(1, savedVolume));
        if ([0, 3, 6, 9].includes(savedCrossfade)) crossfadeSeconds = savedCrossfade;
      }} catch (_) {{}}
      applyVolume(userVolume, false);
      els.crossfade.value = String(crossfadeSeconds);
      current = chooseInitialTrack();
      els.shuffle.setAttribute("aria-pressed", String(shuffleEnabled));
      if (shuffleEnabled) {{
        shufflePlayed = new Set(current >= 0 ? [current] : []);
        queue = current >= 0 ? [current] : [];
      }}
      if (tracks.length) loadTrack(queueIndexFor(current), false);
      updateButtons();
      loadChatHistory();
    }})();
  </script>
</body>
</html>"""
        body = page.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def search_text(self, path: Path, meta: dict[str, object]) -> str:
        parts = [
            path.relative_to(self.server.music_root).as_posix(),
            str(meta.get("title", "")),
            str(meta.get("album", "")),
            str(meta.get("artist_display", "")),
            " ".join(meta.get("genres", [])),
        ]
        return " ".join(parts)

    def player_track(self, path: Path, meta: dict[str, object]) -> dict[str, object]:
        rel = path.relative_to(self.server.music_root).as_posix()
        return {
            "title": str(meta.get("title") or path.stem),
            "artist": str(meta.get("artist_display") or self.artist_from_path(path)),
            "album": str(meta.get("album") or ""),
            "genres": list(meta.get("genres", [])),
            "duration": str(meta.get("duration") or ""),
            "src": f"/files/{quote(rel)}",
            "cover": f"/cover/{quote(rel)}",
            "download": f"/download/{quote(rel)}",
        }

    def friends_rows(
        self,
        player_entries: list[tuple[Path, dict[str, object]]],
        track_indexes: dict[Path, int],
        selected_friend: str,
        params: dict[str, list[str]],
    ) -> tuple[str, dict[str, list[int]]]:
        songs = self.server.friend_songs()
        libraries: dict[str, dict[str, object]] = {}
        for row in songs:
            library = libraries.setdefault(
                row["key"],
                {"owner": row["owner"], "songs": []},
            )
            library["songs"].append(row)

        if selected_friend not in libraries:
            selected_friend = next(iter(libraries), "")

        by_stem = {normalize_key(path.stem): path for path, _ in player_entries}
        by_title: dict[str, list[Path]] = {}
        for path, meta in player_entries:
            title = normalize_key(str(meta.get("title") or path.stem))
            by_title.setdefault(title, []).append(path)

        friend_queues: dict[str, list[int]] = {}
        for key, library in libraries.items():
            queue: list[int] = []
            for row in library["songs"]:
                candidate = by_stem.get(normalize_key(f"{row['artist']} - {row['title']}"))
                if candidate is None:
                    title_matches = by_title.get(normalize_key(row["title"]), [])
                    candidate = title_matches[0] if len(title_matches) == 1 else None
                row["track_index"] = track_indexes.get(candidate) if candidate else None
                row["cover"] = (
                    f"/cover/{quote(candidate.relative_to(self.server.music_root).as_posix())}"
                    if candidate else ""
                )
                if row["track_index"] is not None and row["track_index"] not in queue:
                    queue.append(row["track_index"])
            friend_queues[key] = queue

        imported = params.get("imported", [""])[0]
        new_tracks = params.get("new", [""])[0]
        success = ""
        if imported.isdigit() and new_tracks.isdigit():
            success = (
                '<div class="import-success" role="status">'
                f'<strong>{html.escape(imported)} Songs gespeichert.</strong> '
                f'{html.escape(new_tracks)} davon wurden neu zur Download-Warteschlange hinzugefügt.'
                "</div>"
            )

        switches = "".join(
            f'<a class="friend-switch{" active" if key == selected_friend else ""}" '
            f'href="/?view=friends&amp;friend={quote(key)}">'
            f'{html.escape(str(library["owner"]))}'
            f'<span>{len(library["songs"])}</span></a>'
            for key, library in libraries.items()
        )
        library_html = ""
        if selected_friend:
            library = libraries[selected_friend]
            queue = friend_queues[selected_friend]
            pending = len(library["songs"]) - len(queue)
            play_disabled = "" if queue else " disabled"
            status = f"{len(queue)} abspielbar"
            if pending:
                status += f" · {pending} werden geladen"
            rows = "\n".join(
                self.friend_row(row, selected_friend)
                for row in library["songs"]
            )
            library_html = f"""
      <section class="friend-library">
        <div class="friend-library-head">
          <div>
            <div class="friend-kicker">Persönliche Library</div>
            <h2>{html.escape(str(library["owner"]))}</h2>
            <p>{status}</p>
          </div>
          <button class="friend-play-all" type="button" data-friend-play="{html.escape(selected_friend, quote=True)}"{play_disabled}>Library abspielen</button>
        </div>
        <div class="friend-song-list">{rows}</div>
      </section>"""
        else:
            library_html = """
      <div class="friend-empty">
        <div class="friend-title">Noch keine Freundes-Library</div>
        <p>Lade oben deinen Exportify-CSV-Export hoch. Danach erscheint deine persönliche Songliste hier.</p>
      </div>"""

        spotify_login = ""
        if self.server.spotify_configured():
            spotify_login = """
        <div class="import-divider"><span>oder</span></div>
        <form class="spotify-import" action="/spotify/login" method="get">
          <input name="friend" placeholder="Dein Name" autocomplete="name" required>
          <button type="submit">Direkt mit Spotify verbinden</button>
        </form>"""

        content = f"""
      <section class="friend-import">
        <div class="import-copy">
          <div class="friend-kicker">Deine Musik</div>
          <h2>Spotify-Songs importieren</h2>
          <ol>
            <li><a href="https://exportify.net/" target="_blank" rel="noopener noreferrer">Exportify öffnen</a> und mit Spotify anmelden.</li>
            <li>Bei <strong>Liked Songs</strong> auf <strong>Export</strong> tippen.</li>
            <li>Die heruntergeladene CSV hier auswählen.</li>
          </ol>
          <p>Erneutes Hochladen ergänzt deine Library. Songs werden serverweit nur einmal gespeichert.</p>
        </div>
        <form class="csv-import" action="/friends/import" method="post" enctype="multipart/form-data">
          <label for="friend-owner">Dein Name</label>
          <input id="friend-owner" name="owner" maxlength="60" autocomplete="name" placeholder="z. B. Anna" required>
          <label for="friend-playlist">Exportify CSV</label>
          <input id="friend-playlist" name="playlist" type="file" accept=".csv,text/csv" required>
          <button type="submit">Songs importieren</button>
        </form>
        {spotify_login}
      </section>
      {success}
      <nav class="friend-switches" aria-label="Freundes-Libraries">{switches}</nav>
      {library_html}"""
        return content, friend_queues

    def friend_row(self, row: dict[str, object], owner_key: str) -> str:
        title = html.escape(str(row.get("title", "")))
        artist = html.escape(str(row.get("artist", "")))
        album = html.escape(str(row.get("album", "")))
        track_index = row.get("track_index")
        genres = "".join(
            f'<span class="chip">{html.escape(item)}</span>'
            for item in split_values(str(row.get("genres", "")), ",")[:3]
        )
        meta = " · ".join(part for part in [artist, album] if part)
        if track_index is None:
            action = '<span class="friend-pending">Wird geladen</span>'
            cover = '<div class="friend-cover-fallback" aria-hidden="true">…</div>'
        else:
            index = int(track_index)
            cover = f'<img class="track-cover" src="{html.escape(str(row.get("cover", "")), quote=True)}" alt="" loading="lazy">'
            action = (
                f'<button class="icon-button track-play" type="button" data-play-index="{index}" '
                f'data-queue-owner="{html.escape(owner_key, quote=True)}" '
                'aria-label="In persönlicher Library abspielen">&#9654;</button>'
            )
        return f"""
      <section class="friend-track">
        {cover}
        <div class="track-copy">
          <div class="track-title">{title}</div>
          <div class="track-meta">{meta}</div>
          <div class="chips">{genres}</div>
        </div>
        {action}
      </section>"""

    def sort_entries(
        self,
        entries: list[tuple[Path, dict[str, object]]],
        sort: str,
    ) -> list[tuple[Path, dict[str, object]]]:
        if sort == "artist":
            return sorted(entries, key=lambda item: (str(item[1].get("artist_display", "")).casefold(), str(item[1].get("title", "")).casefold(), item[0].name.casefold()))
        if sort == "added_desc":
            return sorted(entries, key=lambda item: (str(item[1].get("added_at", "")), item[0].name.casefold()), reverse=True)
        if sort == "release_desc":
            return sorted(entries, key=lambda item: (str(item[1].get("release_date", "")), item[0].name.casefold()), reverse=True)
        if sort == "genre":
            return sorted(entries, key=lambda item: (", ".join(item[1].get("genres", [])).casefold(), item[0].name.casefold()))
        return sorted(entries, key=lambda item: (str(item[1].get("title", item[0].stem)).casefold(), item[0].name.casefold()))

    def select(self, name: str, label: str, selected: str, options: list[str]) -> str:
        items = [f'<option value="">{html.escape(label)}</option>']
        for option in options:
            attr = " selected" if option == selected else ""
            items.append(f'<option value="{html.escape(option, quote=True)}"{attr}>{html.escape(option)}</option>')
        return f'<select name="{name}">{"".join(items)}</select>'

    def sort_select(self, selected: str) -> str:
        options = [
            ("title", "Titel"),
            ("artist", "Artist"),
            ("genre", "Genre"),
            ("added_desc", "Neu hinzugefügt"),
            ("release_desc", "Release neu"),
        ]
        items = []
        for value, label in options:
            attr = " selected" if value == selected else ""
            items.append(f'<option value="{value}"{attr}>{label}</option>')
        return f'<select name="sort">{"".join(items)}</select>'

    def artist_from_path(self, path: Path) -> str:
        stem = path.stem
        if " - " in stem:
            return stem.split(" - ", 1)[0]
        return ""

    def track_row(self, index: int, path: Path, meta: dict[str, object]) -> str:
        rel = path.relative_to(self.server.music_root).as_posix()
        quoted = quote(rel)
        title = html.escape(str(meta.get("title") or path.stem))
        artist = html.escape(str(meta.get("artist_display") or self.artist_from_path(path)))
        album = html.escape(str(meta.get("album") or ""))
        release = html.escape(short_date(str(meta.get("release_date") or "")))
        duration = html.escape(str(meta.get("duration") or ""))
        meta_parts = [part for part in [artist, album, release, duration] if part]
        meta_line = " · ".join(meta_parts)
        chips = "".join(
            f'<span class="chip">{html.escape(item)}<button type="button" data-genre-play="{html.escape(item, quote=True)}" aria-label="Genre {html.escape(item)} abspielen">Play</button></span>'
            for item in meta.get("genres", [])[:4]
        )
        return f"""
    <section class="track" data-track-index="{index}">
      <img class="track-cover" src="/cover/{quoted}" alt="" loading="lazy" onerror="this.classList.add('missing')">
      <div class="track-copy">
        <div class="track-title">{title}</div>
        <div class="track-meta">{meta_line}</div>
        <div class="chips">{chips}</div>
      </div>
      <button class="icon-button track-play" type="button" data-play-index="{index}" aria-label="Im Player abspielen">&#9654;</button>
      <a class="download track-download" href="/download/{quoted}" aria-label="{title} herunterladen">Download</a>
    </section>"""

    def resolve_music_path(self, raw_path: str) -> Path | None:
        rel = posixpath.normpath(unquote(raw_path)).lstrip("/")
        root = self.server.music_root.resolve()
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if not candidate.is_file() or candidate.suffix.lower() not in AUDIO_EXTENSIONS:
            return None
        return candidate

    def stream_file(self, raw_path: str, as_attachment: bool) -> None:
        path = self.resolve_music_path(raw_path)
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        size = path.stat().st_size
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            try:
                unit, value = range_header.split("=", 1)
                if unit.strip() != "bytes":
                    raise ValueError
                first, _, last = value.partition("-")
                if first and last:
                    start = int(first)
                    end = int(last)
                elif first:
                    start = int(first)
                elif last:
                    suffix = int(last)
                    if suffix <= 0:
                        raise ValueError
                    start = max(size - suffix, 0)
                    end = size - 1
                else:
                    raise ValueError
                if start > end or start < 0 or end >= size:
                    raise ValueError
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if as_attachment:
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(path.name)}",
            )
        self.end_headers()

        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def serve_cover(self, raw_path: str) -> None:
        path = self.resolve_music_path(raw_path)
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Cover not found")
            return
        fingerprint = f"{path}:{path.stat().st_size}:{path.stat().st_mtime_ns}".encode("utf-8")
        cover_path = self.server.cover_cache / f"{hashlib.sha256(fingerprint).hexdigest()}.jpg"
        if not cover_path.exists():
            temporary = cover_path.with_suffix(".tmp.jpg")
            try:
                result = subprocess.run(
                    [
                        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                        "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
                        "-vf", "scale='min(900,iw)':-2", "-q:v", "3", str(temporary),
                    ],
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is None or result.returncode != 0 or not temporary.exists():
                temporary.unlink(missing_ok=True)
                self.send_error(HTTPStatus.NOT_FOUND, "Cover not found")
                return
            temporary.replace(cover_path)
        payload = cover_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def render_message(self, title: str, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        page = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; font: 16px/1.45 system-ui, sans-serif; background: #131715; color: #eef3ef; }}
    main {{ width: min(680px, calc(100% - 24px)); margin: 0 auto; padding: 40px 0; }}
    a {{ color: #5eead4; }}
    code {{ color: #5eead4; overflow-wrap: anywhere; }}
  </style>
</head>
<body><main><h1>{html.escape(title)}</h1>{body}<p><a href="/">Zurück</a></p></main></body></html>"""
        payload = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def friend_csv_import(self) -> None:
        try:
            fields, files = self.read_multipart_form()
            owner = fields.get("owner", "").strip()
            filename, payload = files.get("playlist", ("", b""))
            result = self.server.import_friend_csv(owner, filename, payload)
            self.server.start_download_if_needed()
        except ValueError as exc:
            self.render_message(
                "CSV-Import fehlgeschlagen",
                f"<p>{html.escape(str(exc))}</p>",
                HTTPStatus.BAD_REQUEST,
            )
            return
        except (OSError, csv.Error) as exc:
            self.render_message(
                "CSV-Import fehlgeschlagen",
                f"<p>Die Datei konnte nicht verarbeitet werden: {html.escape(str(exc))}</p>",
                HTTPStatus.BAD_REQUEST,
            )
            return

        query = urlencode({
            "view": "friends",
            "friend": result["key"],
            "imported": result["tracks"],
            "new": result["new_tracks"],
        })
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/?{query}")
        self.send_header(
            "Set-Cookie",
            f"friend_library={quote(str(result['key']))}; Path=/; SameSite=Lax; Max-Age=31536000",
        )
        self.end_headers()

    def spotify_login(self, params: dict[str, list[str]]) -> None:
        if not self.server.spotify_configured():
            self.render_message(
                "Spotify-Login nicht konfiguriert",
                "<p>Setze zuerst <code>SPOTIFY_CLIENT_ID</code>, <code>SPOTIFY_CLIENT_SECRET</code> und <code>SPOTIFY_REDIRECT_URI</code> in <code>/etc/spotify-song-server.env</code>.</p>",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        friend = params.get("friend", [""])[0].strip()
        state = self.server.save_spotify_state(friend)
        query = urlencode({
            "response_type": "code",
            "client_id": self.server.spotify_client_id,
            "scope": self.server.spotify_scope,
            "redirect_uri": self.server.spotify_redirect_uri,
            "state": state,
            "show_dialog": "true",
        })
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", f"https://accounts.spotify.com/authorize?{query}")
        self.end_headers()

    def spotify_callback(self, params: dict[str, list[str]]) -> None:
        if params.get("error"):
            self.render_message("Spotify-Login abgebrochen", f"<p>{html.escape(params['error'][0])}</p>", HTTPStatus.BAD_REQUEST)
            return
        code = params.get("code", [""])[0]
        state = params.get("state", [""])[0]
        state_data = self.server.pop_spotify_state(state)
        if not code or not state_data:
            self.render_message("Ungültiger Spotify-Callback", "<p>State oder Code fehlt.</p>", HTTPStatus.BAD_REQUEST)
            return
        try:
            token = self.server.exchange_spotify_code(code)
            result = self.server.import_spotify_library(token, state_data.get("friend", ""))
            self.server.start_download_if_needed()
        except Exception as exc:
            self.render_message("Spotify-Import fehlgeschlagen", f"<p><code>{html.escape(str(exc))}</code></p>", HTTPStatus.BAD_GATEWAY)
            return

        body = (
            f"<p>Importiert für <strong>{html.escape(result['owner'])}</strong>.</p>"
            f"<p>{result['tracks']} eindeutige Tracks gefunden, {result['new_tracks']} neue Download-Einträge ergänzt.</p>"
            f"<p>Quelle: Liked Songs, Top Tracks und Playlists.</p>"
        )
        self.render_message("Spotify-Import fertig", body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[MusicServer]) -> None:
        super().__init__(address, handler)
        self.base_dir = Path(env("BASE_DIR", "/opt/spotify-song-server"))
        self.music_root = Path(env("MUSIC_ROOT", "/opt/spotify-song-server/music"))
        self.music_root.mkdir(parents=True, exist_ok=True)
        self.cover_cache = Path(env("COVER_CACHE", "/opt/spotify-song-server/cache/covers"))
        self.cover_cache.mkdir(parents=True, exist_ok=True)
        self.recommendation_cache = Path(env(
            "RECOMMENDATION_CACHE",
            "/opt/spotify-song-server/cache/recommendations",
        ))
        self.recommendation_cache.mkdir(parents=True, exist_ok=True)
        self.auth_user = env("BASIC_AUTH_USER", "")
        self.auth_password = env("BASIC_AUTH_PASSWORD", "")
        self.metadata_csv = Path(env("METADATA_CSV", "/opt/spotify-song-server/data/Liked_Songs.csv"))
        self.friends_dir = Path(env("FRIENDS_DIR", "/opt/spotify-song-server/friends"))
        self.friends_dir.mkdir(parents=True, exist_ok=True)
        self.inputs_dir = Path(env("INPUTS_DIR", "/opt/spotify-song-server/inputs"))
        self.inputs_dir.mkdir(parents=True, exist_ok=True)
        self.track_file = Path(env("TRACK_FILE", str(self.inputs_dir / "tracks.txt")))
        self.spotify_state_dir = Path(env("SPOTIFY_STATE_DIR", "/opt/spotify-song-server/state/spotify"))
        self.spotify_state_dir.mkdir(parents=True, exist_ok=True)
        self.auth_state_dir = Path(env("AUTH_STATE_DIR", "/opt/spotify-song-server/state/auth"))
        self.auth_state_dir.mkdir(parents=True, exist_ok=True)
        self.codex_chat_dir = Path(env("CODEX_CHAT_DIR", "/opt/spotify-song-server/state/codex-chat"))
        self.codex_chat_dir.mkdir(parents=True, exist_ok=True)
        self.codex_history_file = self.codex_chat_dir / "history.json"
        self.codex_proposal_file = self.codex_chat_dir / "proposal.json"
        self.codex_backup_dir = self.codex_chat_dir / "backups"
        self.codex_backup_dir.mkdir(parents=True, exist_ok=True)
        self.codex_lock = threading.Lock()
        self.library_lock = threading.Lock()
        self.recommendation_lock = threading.Lock()
        self.codex_bin = env("CODEX_BIN", "codex")
        self.ytdlp_bin = env("YTDLP_BIN", str(self.base_dir / "venv/bin/yt-dlp"))
        self.codex_cli_model = env("CODEX_CLI_MODEL", "")
        self.codex_ready = self.check_codex_cli()
        self.spotify_client_id = env("SPOTIFY_CLIENT_ID", "")
        self.spotify_client_secret = env("SPOTIFY_CLIENT_SECRET", "")
        self.spotify_redirect_uri = env("SPOTIFY_REDIRECT_URI", "")
        self.spotify_scope = env(
            "SPOTIFY_SCOPE",
            "user-read-private user-library-read user-top-read playlist-read-private playlist-read-collaborative",
        )
        self.metadata_by_stem, self.metadata_by_title = self.load_metadata()

    def codex_configured(self) -> bool:
        return self.codex_ready

    def check_codex_cli(self) -> bool:
        if not shutil.which(self.codex_bin):
            return False
        try:
            status = subprocess.run(
                [self.codex_bin, "login", "status"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except OSError:
            return False
        return status.returncode == 0

    def load_chat_history(self) -> list[dict[str, object]]:
        with self.codex_lock:
            return self.load_chat_history_unlocked()

    def load_chat_history_unlocked(self) -> list[dict[str, object]]:
        try:
            raw = json.loads(self.codex_history_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        return [
            item for item in raw[-MAX_CHAT_HISTORY:]
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant", "system"}
            and isinstance(item.get("content"), str)
        ]

    def save_chat_history(self, messages: list[dict[str, object]]) -> None:
        temporary = self.codex_history_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(messages[-MAX_CHAT_HISTORY:], ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.codex_history_file)

    def reply_to_codex(self, user_message: str) -> dict[str, object]:
        with self.codex_lock:
            history = self.load_chat_history_unlocked()
            result = self.request_codex_response(history, user_message)
            user_entry: dict[str, object] = {"role": "user", "content": user_message}
            assistant_entry: dict[str, object] = {"role": "assistant", "content": result["reply"]}
            if result.get("proposal"):
                assistant_entry["proposal"] = result["proposal"]
            history.extend([user_entry, assistant_entry])
            self.save_chat_history(history)
            return {"message": assistant_entry}

    def request_codex_response(self, history: list[dict[str, object]], user_message: str) -> dict[str, object]:
        conversation = "\n".join(f"{item['role'].upper()}: {item['content']}" for item in history[-20:])
        instructions = (
            "Du bist Codex, der gemeinsame Assistent fuer diese private Musik-Webseite. "
            "Antworte auf Deutsch, konkret und knapp. Der Chatverlauf wird von allen eingeloggten Nutzern geteilt.\n\n"
            f"Du arbeitest in einer isolierten Kopie der Website. Wenn eine Aenderung klar beschrieben ist, "
            f"setze sie direkt in dieser Kopie ausschliesslich in {EDITABLE_APP_FILE} um. Ein Mensch bestaetigt "
            "die Uebernahme in die echte Website erst anschliessend im Browser. Beruehre niemals Authentifizierung, "
            "Passwoerter, API-Keys, Server-Bindung, externe Downloads oder Daten ausserhalb dieser Datei.\n\n"
            "Wenn Informationen fehlen oder keine Aenderung sinnvoll ist, aendere keine Datei und erklaere kurz warum. "
            "Erzeuge keinen Diff im Text; der Server erzeugt ihn aus deiner Dateiaenderung.\n\n"
            f"Bisheriger Chat:\n{conversation or '(noch leer)'}\n\n"
            f"NUTZER: {user_message}"
        )
        source = self.base_dir / EDITABLE_APP_FILE
        if not source.is_file():
            raise RuntimeError("Die Website-Datei wurde nicht gefunden.")
        work_root = Path(tempfile.mkdtemp(dir=self.codex_chat_dir, prefix="work-"))
        work_source = work_root / EDITABLE_APP_FILE
        work_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, work_source)
        output_file = work_root / "codex-response.txt"
        command = [
            self.codex_bin,
            "exec",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-C",
            str(work_root),
            "--output-last-message",
            str(output_file),
        ]
        if self.codex_cli_model:
            command.extend(["--model", self.codex_cli_model])
        command.append(instructions)
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")
                raise RuntimeError(f"Codex CLI-Fehler: {detail[:500] or 'unbekannter Fehler'}")
            reply = output_file.read_text(encoding="utf-8").strip() if output_file.exists() else ""
            diff = subprocess.run(
                ["diff", "-u", "--label", f"a/{EDITABLE_APP_FILE}", str(source), "--label", f"b/{EDITABLE_APP_FILE}", str(work_source)],
                capture_output=True,
                text=True,
                check=False,
            )
            if diff.returncode not in {0, 1}:
                raise RuntimeError("Der Aenderungsvorschlag konnte nicht verglichen werden.")
            patch = diff.stdout
            if not patch:
                return {"reply": reply or "Keine Aenderung vorgeschlagen.", "proposal": None}
            verify = subprocess.run([sys.executable, "-m", "py_compile", str(work_source)], capture_output=True, text=True, check=False)
            if verify.returncode != 0 or not self.valid_codex_patch(patch) or not self.codex_patch_applies(patch):
                return {"reply": (reply or "Codex konnte die Aenderung nicht sicher vorbereiten.") + "\n\nDer Aenderungsvorschlag wurde verworfen.", "proposal": None}
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Codex CLI konnte keine Antwort liefern.") from exc
        finally:
            shutil.rmtree(work_root, ignore_errors=True)
        proposal_data = {"id": secrets.token_urlsafe(18), "summary": (reply or "Website anpassen")[:240], "patch": patch, "created_at": int(time.time())}
        temporary = self.codex_proposal_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(proposal_data), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.codex_proposal_file)
        return {"reply": reply or "Vorschlag vorbereitet.", "proposal": {"id": proposal_data["id"], "summary": proposal_data["summary"]}}

    @staticmethod
    def valid_codex_patch(patch: str) -> bool:
        if not patch or len(patch) > 80000:
            return False
        lines = patch.splitlines()
        if f"--- a/{EDITABLE_APP_FILE}" not in lines or f"+++ b/{EDITABLE_APP_FILE}" not in lines:
            return False
        for line in lines:
            if line.startswith("--- ") and line != f"--- a/{EDITABLE_APP_FILE}":
                return False
            if line.startswith("+++ ") and line != f"+++ b/{EDITABLE_APP_FILE}":
                return False
            if line.startswith("diff --git ") and line != f"diff --git a/{EDITABLE_APP_FILE} b/{EDITABLE_APP_FILE}":
                return False
        return True

    def codex_patch_applies(self, patch: str) -> bool:
        check = subprocess.run(
            ["patch", "--batch", "--forward", "--dry-run", "-p1"],
            cwd=self.base_dir,
            input=patch,
            text=True,
            capture_output=True,
            check=False,
        )
        return check.returncode == 0

    def apply_codex_proposal(self, proposal_id: str) -> dict[str, str]:
        with self.codex_lock:
            try:
                proposal = json.loads(self.codex_proposal_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise RuntimeError("Es gibt keinen offenen Aenderungsvorschlag mehr.")
            if proposal_id != proposal.get("id") or not self.valid_codex_patch(str(proposal.get("patch") or "")):
                raise RuntimeError("Dieser Aenderungsvorschlag ist nicht mehr gueltig.")
            source = self.base_dir / EDITABLE_APP_FILE
            if not source.is_file():
                raise RuntimeError("Die Website-Datei wurde nicht gefunden.")
            patch = str(proposal["patch"])
            check = subprocess.run(["patch", "--batch", "--forward", "--dry-run", "-p1"], cwd=self.base_dir, input=patch, text=True, capture_output=True, check=False)
            if check.returncode != 0:
                raise RuntimeError("Der Vorschlag passt nicht mehr zum aktuellen Code und wurde nicht angewendet.")
            backup = self.codex_backup_dir / f"music_server-{int(time.time())}.py"
            shutil.copy2(source, backup)
            applied = subprocess.run(["patch", "--batch", "--forward", "-p1"], cwd=self.base_dir, input=patch, text=True, capture_output=True, check=False)
            if applied.returncode != 0:
                raise RuntimeError("Der Vorschlag konnte nicht angewendet werden.")
            verify = subprocess.run([sys.executable, "-m", "py_compile", str(source)], capture_output=True, text=True, check=False)
            if verify.returncode != 0:
                shutil.copy2(backup, source)
                raise RuntimeError("Der Vorschlag enthielt einen Python-Fehler und wurde automatisch zurueckgenommen.")
            self.codex_proposal_file.unlink(missing_ok=True)
            history = self.load_chat_history_unlocked()
            history.append({"role": "system", "content": "Eine vorgeschlagene Website-Aenderung wurde angewendet."})
            self.save_chat_history(history)
            subprocess.Popen(["sh", "-c", "sleep 1; systemctl restart spotify-song-server.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return {"message": "Änderung angewendet. Die Website wird jetzt neu geladen."}

    def load_metadata(self) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
        by_stem: dict[str, dict[str, object]] = {}
        by_title: dict[str, dict[str, object]] = {}
        csv_paths = [self.metadata_csv] + sorted(self.friends_dir.glob("*.csv"))
        for csv_path in csv_paths:
            if not csv_path.exists():
                continue
            try:
                with csv_path.open(newline="", encoding="utf-8-sig") as handle:
                    for row in csv.DictReader(handle):
                        title = (row.get("Track Name") or "").strip()
                        artists = split_values(row.get("Artist Name(s)") or "", ";")
                        artist_display = ", ".join(artists)
                        genres = split_values(row.get("Genres") or "", ",")
                        meta: dict[str, object] = {
                            "title": title,
                            "artists": artists,
                            "artist_display": artist_display,
                            "album": (row.get("Album Name") or "").strip(),
                            "genres": genres,
                            "release_date": (row.get("Release Date") or "").strip(),
                            "added_at": (row.get("Added At") or "").strip(),
                            "duration": format_duration(row.get("Duration (ms)") or ""),
                        }
                        if title and artist_display:
                            by_stem.setdefault(normalize_key(f"{artist_display} - {title}"), meta)
                        if title:
                            by_title.setdefault(normalize_key(title), meta)
            except (OSError, csv.Error):
                continue
        return by_stem, by_title

    def metadata_for(self, path: Path) -> dict[str, object]:
        stem_key = normalize_key(path.stem)
        meta = self.metadata_by_stem.get(stem_key)
        if meta:
            return meta
        if " - " in path.stem:
            title_key = normalize_key(path.stem.split(" - ", 1)[1])
            meta = self.metadata_by_title.get(title_key)
            if meta:
                return meta
        return {
            "title": path.stem,
            "artists": [path.stem.split(" - ", 1)[0]] if " - " in path.stem else [],
            "artist_display": path.stem.split(" - ", 1)[0] if " - " in path.stem else "",
            "album": "",
            "genres": [],
            "release_date": "",
            "added_at": "",
            "duration": "",
        }

    def recommendations_for(self, path: Path, music_files: list[Path]) -> list[dict[str, object]]:
        if not Path(self.ytdlp_bin).is_file():
            raise RuntimeError("Die YouTube-Suche ist auf dem Server nicht verfügbar.")
        meta = self.metadata_for(path)
        title = str(meta.get("title") or path.stem).strip()
        artist = str(meta.get("artist_display") or "").strip()
        if not artist and " - " in path.stem:
            artist = path.stem.split(" - ", 1)[0].strip()
        if not title:
            raise RuntimeError("Für diesen Song fehlen Suchinformationen.")

        newest_mtime = max((item.stat().st_mtime_ns for item in music_files), default=0)
        fingerprint = f"creator-v2:{normalize_key(artist)}:{normalize_key(title)}:{len(music_files)}:{newest_mtime}"
        cache_path = self.recommendation_cache / f"{hashlib.sha256(fingerprint.encode()).hexdigest()}.json"
        with self.recommendation_lock:
            if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 7 * 86400:
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(cached, list):
                        return cached
                except (OSError, json.JSONDecodeError):
                    pass

            search_query = " ".join(part for part in [artist, title] if part)
            search_url = "https://music.youtube.com/search?" + urlencode({"q": search_query}) + "#songs"
            search = self.run_ytdlp_json(search_url, 12)
            search_entries = [
                item for item in search.get("entries", [])
                if isinstance(item, dict) and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(item.get("id") or ""))
            ]
            if not search_entries:
                raise RuntimeError("YouTube Music hat keinen passenden Ausgangssong gefunden.")
            title_key = normalize_key(title)
            seed = next(
                (
                    item for item in search_entries
                    if title_key and title_key in normalize_key(str(item.get("title") or ""))
                ),
                search_entries[0],
            )
            seed_id = str(seed["id"])
            mix_url = f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}"
            mix = self.run_ytdlp_json(mix_url, 35)

            known_titles = {
                normalize_key(str(self.metadata_for(item).get("title") or item.stem))
                for item in music_files
            }
            known_stems = {normalize_key(item.stem) for item in music_files}
            blocked_terms = {
                "podcast", "interview", "reaction", "review", "tutorial", "documentary",
                "gameplay", "trailer", "news", "recap", "audiobook", "full movie",
                "behind the scenes", "making of", "live stream",
            }
            creator_names = [
                str(item).strip()
                for item in (meta.get("artists") or [])
                if str(item).strip()
            ]
            if not creator_names:
                creator_names = [part.strip() for part in artist.split(",") if part.strip()]
            creator_keys = {
                normalize_key(item)
                for item in creator_names
                if len(normalize_key(item)) >= 3
            }
            suggestions: list[dict[str, object]] = []
            seen_ids: set[str] = set()
            for mix_index, item in enumerate(mix.get("entries", [])):
                if not isinstance(item, dict):
                    continue
                video_id = str(item.get("id") or "")
                video_title = str(item.get("title") or "").strip()
                channel = str(item.get("channel") or item.get("uploader") or "YouTube Music").strip()
                duration = item.get("duration")
                if (
                    video_id == seed_id
                    or video_id in seen_ids
                    or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                    or not video_title
                    or not isinstance(duration, (int, float))
                    or duration < 60
                    or duration > 900
                    or str(item.get("live_status") or "") in {"is_live", "is_upcoming"}
                ):
                    continue
                content_key = normalize_key(f"{video_title} {channel}")
                if any(term in content_key for term in blocked_terms):
                    continue
                variants = {normalize_key(video_title)}
                if " - " in video_title:
                    variants.add(normalize_key(video_title.split(" - ", 1)[1]))
                variants = {
                    re.sub(
                        r"\b(official|music|audio|video|lyrics?|lyric|visualizer|hd|hq|remaster(?:ed)?)\b",
                        "",
                        variant,
                    ).strip()
                    for variant in variants
                }
                if any(
                    variant in known_titles
                    or variant in known_stems
                    or any(
                        len(known) >= 5 and (variant.endswith(known) or known == variant)
                        for known in known_titles
                    )
                    for variant in variants
                    if variant
                ):
                    continue
                channel_key = normalize_key(channel.removesuffix(" - Topic"))
                video_title_key = normalize_key(video_title)
                creator_score = 0
                if any(channel_key == key for key in creator_keys):
                    creator_score = 3
                elif any(
                    key in channel_key or channel_key in key
                    for key in creator_keys
                    if len(channel_key) >= 3
                ):
                    creator_score = 2
                elif any(video_title_key.startswith(f"{key} ") for key in creator_keys):
                    creator_score = 1
                seen_ids.add(video_id)
                suggestions.append({
                    "id": video_id,
                    "title": video_title,
                    "artist": channel.removesuffix(" - Topic"),
                    "duration": format_duration(str(int(duration * 1000))),
                    "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "_creator_score": creator_score,
                    "_mix_index": mix_index,
                })
            if not suggestions:
                raise RuntimeError("Im YouTube-Mix wurden keine neuen Musikvorschläge gefunden.")
            suggestions.sort(key=lambda item: (-int(item["_creator_score"]), int(item["_mix_index"])))
            suggestions = suggestions[:8]
            for suggestion in suggestions:
                suggestion.pop("_creator_score", None)
                suggestion.pop("_mix_index", None)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(suggestions, ensure_ascii=False), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(cache_path)
            return suggestions

    def run_ytdlp_json(self, url: str, playlist_end: int) -> dict[str, object]:
        command = [
            self.ytdlp_bin,
            "--dump-single-json",
            "--flat-playlist",
            "--playlist-end", str(playlist_end),
            "--no-warnings",
            "--skip-download",
            url,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Die YouTube-Suche hat zu lange gedauert.") from exc
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            message = detail[-1] if detail else "Unbekannter yt-dlp-Fehler"
            raise RuntimeError(f"YouTube-Suche fehlgeschlagen: {message[:240]}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("YouTube hat eine ungültige Suchantwort geliefert.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("YouTube hat keine Suchergebnisse geliefert.")
        return payload

    def friend_songs(self) -> list[dict[str, str]]:
        songs: list[dict[str, str]] = []
        for csv_path in sorted(self.friends_dir.glob("*.csv"), key=lambda path: path.name.casefold()):
            owner = csv_path.stem.replace("_", " ").replace("-", " ").strip() or "Freund"
            try:
                with csv_path.open(newline="", encoding="utf-8-sig") as handle:
                    seen: set[str] = set()
                    for row in csv.DictReader(handle):
                        title = (row.get("Track Name") or row.get("Title") or row.get("Track") or "").strip()
                        artist = (
                            row.get("Artist Name(s)")
                            or row.get("Artist")
                            or row.get("Artists")
                            or ""
                        ).replace(";", ", ").strip()
                        if not title:
                            continue
                        uri = self.spotify_track_url(
                            row.get("Track URI")
                            or row.get("Spotify URI")
                            or row.get("Spotify URL")
                            or ""
                        )
                        identity = uri or normalize_key(f"{artist} - {title}")
                        if not identity or identity in seen:
                            continue
                        seen.add(identity)
                        songs.append({
                            "title": title,
                            "artist": artist,
                            "album": (row.get("Album Name") or row.get("Album") or "").strip(),
                            "genres": (row.get("Genres") or row.get("Genre") or "").strip(),
                            "owner": (row.get("Added By") or owner).strip() or owner,
                            "key": csv_path.stem,
                            "uri": uri,
                        })
            except (OSError, csv.Error):
                continue
        return sorted(songs, key=lambda item: (item.get("owner", "").casefold(), item.get("artist", "").casefold(), item.get("title", "").casefold()))

    def spotify_track_url(self, value: str) -> str:
        value = (value or "").strip()
        if value.startswith("spotify:track:"):
            track_id = value.rsplit(":", 1)[-1]
        elif "open.spotify.com/track/" in value:
            track_id = value.split("/track/", 1)[-1].split("?", 1)[0].split("/", 1)[0]
        else:
            return ""
        if not re.fullmatch(r"[A-Za-z0-9]{10,40}", track_id):
            return ""
        return f"https://open.spotify.com/track/{track_id}"

    def friend_key(self, owner: str) -> str:
        stem = normalize_key(owner).replace(" ", "-")[:40] or "friend"
        digest = hashlib.sha256(owner.casefold().encode("utf-8")).hexdigest()[:8]
        return f"{stem}-{digest}"

    def normalized_friend_row(self, row: dict[str, str], owner: str) -> dict[str, str] | None:
        title = (row.get("Track Name") or row.get("Title") or row.get("Track") or "").strip()
        artists = (
            row.get("Artist Name(s)")
            or row.get("Artist")
            or row.get("Artists")
            or ""
        ).replace(", ", ";").strip()
        uri = self.spotify_track_url(
            row.get("Track URI")
            or row.get("Spotify URI")
            or row.get("Spotify URL")
            or row.get("Track URL")
            or row.get("URI")
            or ""
        )
        if not title or not artists or not uri:
            return None
        return {
            "Track URI": uri,
            "Track Name": title,
            "Artist Name(s)": artists,
            "Album Name": (row.get("Album Name") or row.get("Album") or "").strip(),
            "Genres": (row.get("Genres") or row.get("Genre") or "").strip(),
            "Release Date": (row.get("Release Date") or "").strip(),
            "Duration (ms)": (row.get("Duration (ms)") or row.get("Duration") or "").strip(),
            "Added At": (row.get("Added At") or "").strip(),
            "Added By": owner,
            "Source": (row.get("Source") or "Exportify CSV").strip(),
        }

    def import_friend_csv(self, owner: str, filename: str, payload: bytes) -> dict[str, object]:
        owner = re.sub(r"\s+", " ", owner).strip()
        if len(owner) < 2 or len(owner) > 60:
            raise ValueError("Bitte gib einen Namen mit 2 bis 60 Zeichen ein.")
        if not filename.lower().endswith(".csv"):
            raise ValueError("Bitte wähle eine CSV-Datei von Exportify aus.")
        if not payload:
            raise ValueError("Die ausgewählte CSV-Datei ist leer.")
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = payload.decode("cp1252")
            except UnicodeDecodeError as exc:
                raise ValueError("Die CSV-Datei muss UTF-8-kodiert sein.") from exc

        reader = csv.DictReader(io.StringIO(text))
        fieldnames = {normalize_key(name or "") for name in (reader.fieldnames or [])}
        if "track name" not in fieldnames or not ({"track uri", "spotify uri", "spotify url"} & fieldnames):
            raise ValueError("Die CSV enthält keine Exportify-Spalten für Track Name und Track URI.")

        imported: dict[str, dict[str, str]] = {}
        for number, row in enumerate(reader, start=1):
            if number > MAX_CSV_TRACKS:
                raise ValueError(f"Eine CSV darf höchstens {MAX_CSV_TRACKS} Songs enthalten.")
            normalized = self.normalized_friend_row(row, owner)
            if normalized:
                imported[normalized["Track URI"]] = normalized
        if not imported:
            raise ValueError("In der CSV wurden keine gültigen Spotify-Songs gefunden.")

        key = self.friend_key(owner)
        csv_path = self.friends_dir / f"{key}.csv"
        fieldnames_out = [
            "Track URI", "Track Name", "Artist Name(s)", "Album Name", "Genres",
            "Release Date", "Duration (ms)", "Added At", "Added By", "Source",
        ]
        with self.library_lock:
            merged: dict[str, dict[str, str]] = {}
            if csv_path.exists():
                with csv_path.open(newline="", encoding="utf-8-sig") as handle:
                    for row in csv.DictReader(handle):
                        normalized = self.normalized_friend_row(row, owner)
                        if normalized:
                            merged[normalized["Track URI"]] = normalized
            merged.update(imported)
            temporary = csv_path.with_suffix(".tmp")
            with temporary.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames_out)
                writer.writeheader()
                writer.writerows(sorted(
                    merged.values(),
                    key=lambda item: (item["Artist Name(s)"].casefold(), item["Track Name"].casefold()),
                ))
            temporary.chmod(0o600)
            temporary.replace(csv_path)
            new_tracks = self.append_track_urls_unlocked(list(merged))
            self.metadata_by_stem, self.metadata_by_title = self.load_metadata()
        return {
            "owner": owner,
            "key": key,
            "tracks": len(merged),
            "new_tracks": new_tracks,
        }

    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret and self.spotify_redirect_uri)

    def create_auth_session(self) -> str:
        token = secrets.token_urlsafe(24)
        path = self.auth_state_dir / token
        path.write_text(str(int(time.time())), encoding="utf-8")
        path.chmod(0o600)
        return token

    def has_auth_session(self, token: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", token or ""):
            return False
        path = self.auth_state_dir / token
        if not path.exists():
            return False
        try:
            created_at = int(path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return False
        return int(time.time()) - created_at < 2592000

    def delete_auth_session(self, token: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", token or ""):
            return
        path = self.auth_state_dir / token
        try:
            path.unlink()
        except OSError:
            return

    def save_spotify_state(self, friend: str) -> str:
        state = secrets.token_urlsafe(24)
        payload = {
            "friend": friend.strip(),
            "created_at": int(time.time()),
        }
        path = self.spotify_state_dir / f"{state}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        return state

    def pop_spotify_state(self, state: str) -> dict[str, str] | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", state or ""):
            return None
        path = self.spotify_state_dir / f"{state}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            path.unlink()
        except (OSError, json.JSONDecodeError):
            return None
        if int(time.time()) - int(payload.get("created_at", 0)) > 900:
            return None
        return payload

    def exchange_spotify_code(self, code: str) -> str:
        auth = base64.b64encode(f"{self.spotify_client_id}:{self.spotify_client_secret}".encode("utf-8")).decode("ascii")
        data = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.spotify_redirect_uri,
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=data,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        payload = self.spotify_json(request)
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Spotify token response did not contain access_token")
        return str(token)

    def spotify_get(self, token: str, url: str) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        return self.spotify_json(request)

    def spotify_json(self, request: urllib.request.Request) -> dict[str, object]:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Spotify API HTTP {exc.code}: {detail}") from exc

    def import_spotify_library(self, token: str, friend: str) -> dict[str, object]:
        me = self.spotify_get(token, "https://api.spotify.com/v1/me")
        owner = friend or str(me.get("display_name") or me.get("id") or "Spotify Freund")
        tracks: dict[str, dict[str, str]] = {}

        self.collect_saved_tracks(token, tracks, owner)
        self.collect_top_tracks(token, tracks, owner)
        self.collect_playlist_tracks(token, tracks, owner)

        safe_owner = re.sub(r"[^A-Za-z0-9._-]+", "_", owner).strip("_") or "spotify_friend"
        csv_path = self.friends_dir / f"{safe_owner}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            fieldnames = ["Track URI", "Track Name", "Artist Name(s)", "Album Name", "Genres", "Added By", "Source"]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in sorted(tracks.values(), key=lambda item: (item["Artist Name(s)"].casefold(), item["Track Name"].casefold())):
                writer.writerow(row)
        csv_path.chmod(0o600)

        new_tracks = self.append_track_urls([uri for uri in tracks if uri.startswith("spotify:track:")])
        return {"owner": owner, "tracks": len(tracks), "new_tracks": new_tracks}

    def collect_saved_tracks(self, token: str, tracks: dict[str, dict[str, str]], owner: str) -> None:
        url = "https://api.spotify.com/v1/me/tracks?limit=50"
        while url:
            payload = self.spotify_get(token, url)
            for item in payload.get("items", []):
                track = item.get("track") if isinstance(item, dict) else None
                self.add_spotify_track(tracks, track, owner, "Liked Songs")
            url = str(payload.get("next") or "")

    def collect_top_tracks(self, token: str, tracks: dict[str, dict[str, str]], owner: str) -> None:
        url = "https://api.spotify.com/v1/me/top/tracks?limit=50&time_range=medium_term"
        payload = self.spotify_get(token, url)
        for track in payload.get("items", []):
            self.add_spotify_track(tracks, track, owner, "Top Tracks")

    def collect_playlist_tracks(self, token: str, tracks: dict[str, dict[str, str]], owner: str) -> None:
        playlists_url = "https://api.spotify.com/v1/me/playlists?limit=50"
        while playlists_url:
            playlists = self.spotify_get(token, playlists_url)
            for playlist in playlists.get("items", []):
                if not isinstance(playlist, dict) or not playlist.get("id"):
                    continue
                name = str(playlist.get("name") or "Playlist")
                track_url = f"https://api.spotify.com/v1/playlists/{playlist['id']}/tracks?limit=100"
                while track_url:
                    page = self.spotify_get(token, track_url)
                    for item in page.get("items", []):
                        track = item.get("track") if isinstance(item, dict) else None
                        self.add_spotify_track(tracks, track, owner, f"Playlist: {name}")
                    track_url = str(page.get("next") or "")
            playlists_url = str(playlists.get("next") or "")

    def add_spotify_track(self, tracks: dict[str, dict[str, str]], track: object, owner: str, source: str) -> None:
        if not isinstance(track, dict) or track.get("type") != "track":
            return
        uri = str(track.get("uri") or "")
        if not uri.startswith("spotify:track:"):
            return
        artists = [
            str(artist.get("name"))
            for artist in track.get("artists", [])
            if isinstance(artist, dict) and artist.get("name")
        ]
        album = track.get("album") if isinstance(track.get("album"), dict) else {}
        existing = tracks.get(uri)
        if existing:
            sources = set(split_values(existing["Source"], " | "))
            sources.add(source)
            existing["Source"] = " | ".join(sorted(sources))
            return
        tracks[uri] = {
            "Track URI": uri,
            "Track Name": str(track.get("name") or ""),
            "Artist Name(s)": ";".join(artists),
            "Album Name": str(album.get("name") or ""),
            "Genres": "",
            "Added By": owner,
            "Source": source,
        }

    def append_track_urls(self, uris: list[str]) -> int:
        with self.library_lock:
            return self.append_track_urls_unlocked(uris)

    def append_track_urls_unlocked(self, uris: list[str]) -> int:
        existing: set[str] = set()
        if self.track_file.exists():
            existing = {line.strip() for line in self.track_file.read_text(encoding="utf-8").splitlines() if line.strip()}
        additions = []
        for uri in uris:
            url = self.spotify_track_url(uri)
            if not url:
                continue
            if url not in existing:
                additions.append(url)
                existing.add(url)
        if additions:
            with self.track_file.open("a", encoding="utf-8") as handle:
                for url in additions:
                    handle.write(url + "\n")
        return len(additions)

    def start_download_if_needed(self) -> None:
        try:
            subprocess.run(
                ["systemctl", "start", "--no-block", "spotify-song-download-ensure.service"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return


def main() -> int:
    host = env("HOST", "127.0.0.1")
    port = int(env("PORT", "8088"))
    server = Server((host, port), MusicServer)
    print(f"Serving {server.music_root} on http://{host}:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
