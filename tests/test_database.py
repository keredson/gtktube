from __future__ import annotations

import sqlite3
import unittest

from gtktube.db.connection import connect
from gtktube.db.migrations import SCHEMA_VERSION, migrate
from gtktube.db.repositories import LibraryRepository
from gtktube.models import Channel, Video


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        migrate(self.connection)
        self.repository = LibraryRepository(self.connection)
        self.repository.upsert_channel(
            Channel(id="chan1", title="Channel One", url="https://example.test/channel")
        )
        self.repository.upsert_video(
            Video(
                id="vid1",
                channel_id="chan1",
                channel_title="Channel One",
                title="A Long Video",
                url="https://example.test/watch?v=vid1",
                description="Useful context about the video.",
                duration_seconds=100,
                view_count=12345,
            )
        )

    def test_migration_sets_user_version(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)

    def test_watch_progress_merges_duplicate_ranges_at_query_time(self) -> None:
        self.repository.add_watch_range("vid1", 0, 30)
        self.repository.add_watch_range("vid1", 0, 30)
        self.repository.add_watch_range("vid1", 20, 60)

        raw_count = self.connection.execute(
            "SELECT COUNT(*) FROM watch_ranges WHERE video_id = 'vid1'"
        ).fetchone()[0]
        progress = self.connection.execute(
            "SELECT covered_seconds, percent_watched FROM watch_progress WHERE video_id = 'vid1'"
        ).fetchone()

        self.assertEqual(raw_count, 3)
        self.assertEqual(progress["covered_seconds"], 60)
        self.assertAlmostEqual(progress["percent_watched"], 0.6)

    def test_watch_range_trigger_maintains_history_summary(self) -> None:
        self.repository.add_watch_range("vid1", 10, 20)
        self.repository.add_watch_range("vid1", 30, 40)

        row = self.connection.execute(
            """
            SELECT first_watched_at, last_watched_at, play_count
            FROM watch_history
            WHERE video_id = 'vid1'
            """
        ).fetchone()

        self.assertIsNotNone(row["first_watched_at"])
        self.assertIsNotNone(row["last_watched_at"])
        self.assertEqual(row["play_count"], 0)

    def test_completion_uses_merged_coverage(self) -> None:
        self.repository.add_watch_range("vid1", 0, 50)
        self.repository.add_watch_range("vid1", 50, 90)

        row = self.connection.execute(
            "SELECT completed FROM watch_history WHERE video_id = 'vid1'"
        ).fetchone()

        self.assertEqual(row["completed"], 1)

    def test_watch_history_searches_video_and_channel_title(self) -> None:
        self.repository.add_watch_range("vid1", 0, 10)

        by_video = self.repository.watch_history("Long")
        by_channel = self.repository.watch_history("Channel")

        self.assertEqual([video.id for video in by_video], ["vid1"])
        self.assertEqual([video.id for video in by_channel], ["vid1"])

    def test_video_metadata_round_trips_through_feed(self) -> None:
        feed = self.repository.subscription_feed()

        self.assertEqual(feed[0].description, "Useful context about the video.")
        self.assertEqual(feed[0].duration_seconds, 100)
        self.assertEqual(feed[0].view_count, 12345)


class ConnectionTests(unittest.TestCase):
    def test_connect_uses_row_factory(self) -> None:
        connection = connect(":memory:")
        try:
            migrate(connection)
            row = connection.execute("PRAGMA user_version").fetchone()
            self.assertEqual(row[0], SCHEMA_VERSION)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
