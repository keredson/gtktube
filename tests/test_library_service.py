from __future__ import annotations

import sqlite3
import unittest

from gtktube.db.migrations import migrate
from gtktube.db.repositories import LibraryRepository
from gtktube.models import Video
from gtktube.services.library import LibraryService


class FakeExtractor:
    def __init__(self, videos: list[Video]):
        self.videos = videos
        self.browser: str | None = None

    def watch_history(self, cookies_browser: str, limit: int = 100) -> list[Video]:
        self.browser = cookies_browser
        return self.videos[:limit]


class LibraryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        migrate(self.connection)
        self.repository = LibraryRepository(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_import_youtube_watch_history_marks_videos_watched(self) -> None:
        extractor = FakeExtractor(
            [
                Video(
                    id="watched1",
                    title="Watched One",
                    url="https://example.test/watch?v=watched1",
                    channel_id="chan1",
                    channel_title="Channel One",
                    duration_seconds=120,
                )
            ]
        )
        service = LibraryService(self.repository, extractor)  # type: ignore[arg-type]
        self.repository.set_yt_dlp_cookies_browser("firefox")

        count = service.import_youtube_watch_history()

        self.assertEqual(count, 1)
        self.assertEqual(extractor.browser, "firefox")
        video = self.repository.watch_history()[0]
        self.assertEqual(video.id, "watched1")
        self.assertTrue(video.completed)
        self.assertEqual(video.watch_ranges, [(0, 120)])
        self.assertIsNotNone(self.repository.youtube_watch_history_last_import_at())


if __name__ == "__main__":
    unittest.main()
