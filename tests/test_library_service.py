from __future__ import annotations

import sqlite3
import unittest

from gtktube.db.migrations import migrate
from gtktube.db.repositories import LibraryRepository
from gtktube.models import Channel, Video
from gtktube.services.library import LibraryService


class FakeExtractor:
    def __init__(self, videos: list[Video]):
        self.videos = videos
        self.browser: str | None = None

    def watch_history(self, cookies_browser: str, limit: int = 100) -> list[Video]:
        self.browser = cookies_browser
        return self.videos[:limit]


class RefreshExtractor:
    def resolve_channel(self, url: str) -> Channel:
        return Channel(id="chan1", title="Channel One", url=url)

    def channel_uploads(
        self, channel: Channel, limit: int = 30, start: int = 1
    ) -> list[Video]:
        return [
            Video(
                id="video1",
                title="Video One",
                url="https://example.test/video1",
                channel_id=channel.id,
                channel_title=channel.title,
            )
        ]

    def channel_shorts(
        self, channel: Channel, limit: int = 30, start: int = 1
    ) -> list[Video]:
        return [
            Video(
                id="short1",
                title="Short One",
                url="https://example.test/short1",
                kind="short",
                channel_id=channel.id,
                channel_title=channel.title,
            )
        ]

    def channel_playlists(
        self, channel: Channel, limit: int = 30, start: int = 1
    ) -> list[Video]:
        return [
            Video(
                id="playlist1",
                title="Playlist One",
                url="https://example.test/playlist1",
                kind="playlist",
                channel_id=channel.id,
                channel_title=channel.title,
            )
        ]


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

    def test_refresh_channel_stores_videos_shorts_and_playlists(self) -> None:
        channel = Channel(
            id="chan1",
            title="Channel One",
            url="https://example.test/channel",
        )
        self.repository.upsert_channel(channel, subscribed=True)
        service = LibraryService(self.repository, RefreshExtractor())  # type: ignore[arg-type]

        videos = service.refresh_channel(channel, refresh_metadata=False)

        self.assertEqual([video.id for video in videos], ["video1"])
        self.assertEqual(
            [video.id for video in self.repository.channel_videos("chan1")],
            ["video1"],
        )
        self.assertEqual(
            [video.id for video in self.repository.channel_shorts("chan1")],
            ["short1"],
        )
        self.assertEqual(
            [video.id for video in self.repository.channel_playlists("chan1")],
            ["playlist1"],
        )


if __name__ == "__main__":
    unittest.main()
