import csv
import tempfile
import threading
import unittest
from pathlib import Path

from app.music_server import Server


FIELDNAMES = [
    "Track URI",
    "Track Name",
    "Artist Name(s)",
    "Album Name",
    "Genres",
    "Release Date",
    "Duration (ms)",
    "Added At",
    "Added By",
    "Source",
]


class RecommendationLikeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.server = Server.__new__(Server)
        self.server.friends_dir = root / "friends"
        self.server.friends_dir.mkdir()
        self.server.inputs_dir = root / "inputs"
        self.server.inputs_dir.mkdir()
        self.server.track_file = self.server.inputs_dir / "tracks.txt"
        self.server.metadata_csv = root / "missing.csv"
        self.server.library_lock = threading.Lock()
        self.server.metadata_by_stem = {}
        self.server.metadata_by_title = {}
        self.source = root / "music" / "Seed Artist - Seed Song.mp3"
        self.source.parent.mkdir()
        self.source.touch()
        self.library_key = "elias-12345678"
        self.library_path = self.server.friends_dir / f"{self.library_key}.csv"
        self.write_library([
            {
                "Track URI": "spotify:track:0VjIjW4GlUZAMYd2vXMi3b",
                "Track Name": "Blinding Lights",
                "Artist Name(s)": "The Weeknd",
                "Added By": "Elias",
                "Source": "Exportify CSV",
            }
        ])
        self.suggestion = {
            "id": "abcdefghijk",
            "title": "Demo Artist - Midnight (Official Audio)",
            "artist": "Demo Artist",
            "duration": "3:12",
            "duration_seconds": 192,
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
        }
        self.server.recommendations_for = lambda _path, _files: [self.suggestion]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_library(self, rows: list[dict[str, str]]) -> None:
        with self.library_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def read_library(self) -> list[dict[str, str]]:
        with self.library_path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_normalizes_supported_download_urls(self) -> None:
        self.assertEqual(
            self.server.download_track_url("spotify:track:0VjIjW4GlUZAMYd2vXMi3b"),
            "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b",
        )
        self.assertEqual(
            self.server.download_track_url("https://youtu.be/abcdefghijk?t=4"),
            "https://www.youtube.com/watch?v=abcdefghijk",
        )
        self.assertEqual(self.server.download_track_url("https://example.com/watch?v=abcdefghijk"), "")
        self.assertEqual(
            self.server.clean_recommendation_title("Demo Artist - Midnight (Lyric Video)", "Demo Artist"),
            "Midnight",
        )

    def test_like_adds_personal_row_and_spotdl_queue_entry_once(self) -> None:
        first = self.server.add_recommendation_to_library(
            self.source,
            [],
            self.suggestion["id"],
            self.library_key,
        )
        second = self.server.add_recommendation_to_library(
            self.source,
            [],
            self.suggestion["id"],
            self.library_key,
        )

        self.assertTrue(first["queued"])
        self.assertFalse(first["already_liked"])
        self.assertFalse(second["queued"])
        self.assertTrue(second["already_liked"])
        rows = self.read_library()
        liked = next(row for row in rows if row["Source"] == "YouTube recommendation")
        self.assertEqual(liked["Track Name"], "Midnight")
        self.assertEqual(liked["Artist Name(s)"], "Demo Artist")
        self.assertEqual(liked["Added By"], "Elias")
        self.assertEqual(liked["Duration (ms)"], "192000")
        self.assertEqual(
            self.server.track_file.read_text(encoding="utf-8").splitlines(),
            ["https://www.youtube.com/watch?v=abcdefghijk"],
        )
        liked_song = next(song for song in self.server.friend_songs() if song["title"] == "Midnight")
        self.assertEqual(liked_song["uri"], "https://www.youtube.com/watch?v=abcdefghijk")

    def test_deduplicates_recommendation_against_existing_spotify_song(self) -> None:
        self.write_library([
            {
                "Track URI": "spotify:track:0VjIjW4GlUZAMYd2vXMi3b",
                "Track Name": "Midnight",
                "Artist Name(s)": "Demo Artist",
                "Added By": "Elias",
                "Source": "Exportify CSV",
            }
        ])

        result = self.server.add_recommendation_to_library(
            self.source,
            [],
            self.suggestion["id"],
            self.library_key,
        )

        self.assertTrue(result["already_liked"])
        self.assertFalse(result["queued"])
        self.assertEqual(len(self.read_library()), 1)
        self.assertFalse(self.server.track_file.exists())

    def test_rejects_recommendation_not_returned_by_server(self) -> None:
        with self.assertRaisesRegex(ValueError, "nicht mehr verfügbar"):
            self.server.add_recommendation_to_library(
                self.source,
                [],
                "zyxwvutsrqp",
                self.library_key,
            )


if __name__ == "__main__":
    unittest.main()
