#!/usr/bin/env python3
import argparse
import csv
from pathlib import Path


def spotify_track_url(uri: str) -> str | None:
    uri = (uri or "").strip()
    if uri.startswith("spotify:track:"):
        return "https://open.spotify.com/track/" + uri.rsplit(":", 1)[-1]
    if uri.startswith("https://open.spotify.com/track/"):
        return uri
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert Spotify liked-songs CSV rows to spotDL track inputs."
    )
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--urls-out", type=Path, default=Path("tracks.txt"))
    parser.add_argument("--queries-out", type=Path, default=Path("track_queries.txt"))
    args = parser.parse_args()

    seen_urls: set[str] = set()
    urls: list[str] = []
    queries: list[str] = []

    with args.csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            url = spotify_track_url(row.get("Track URI", ""))
            title = (row.get("Track Name") or "").strip()
            artists = (row.get("Artist Name(s)") or "").replace(";", ", ").strip()

            if url and url not in seen_urls:
                urls.append(url)
                seen_urls.add(url)
            if title and artists:
                queries.append(f"{artists} - {title}")

    args.urls_out.parent.mkdir(parents=True, exist_ok=True)
    args.queries_out.parent.mkdir(parents=True, exist_ok=True)
    args.urls_out.write_text("\n".join(urls) + "\n", encoding="utf-8")
    args.queries_out.write_text("\n".join(queries) + "\n", encoding="utf-8")
    print(f"Wrote {len(urls)} Spotify track URLs to {args.urls_out}")
    print(f"Wrote {len(queries)} search queries to {args.queries_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

