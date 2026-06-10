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

    def test_video_includes_merged_watch_ranges_for_indicators(self) -> None:
        self.repository.add_watch_range("vid1", 0, 33)
        self.repository.add_watch_range("vid1", 66, 100)

        video = self.repository.subscription_feed()[0]

        self.assertEqual(video.watch_ranges, [(0, 33), (66, 100)])

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

    def test_mark_played_records_full_watch_range(self) -> None:
        self.repository.mark_played("vid1", 100)

        history = self.connection.execute(
            """
            SELECT completed, completed_at
            FROM watch_history
            WHERE video_id = 'vid1'
            """
        ).fetchone()
        progress = self.connection.execute(
            """
            SELECT covered_seconds, percent_watched, watch_range_string
            FROM watch_progress
            WHERE video_id = 'vid1'
            """
        ).fetchone()

        self.assertEqual(history["completed"], 1)
        self.assertIsNotNone(history["completed_at"])
        self.assertEqual(progress["covered_seconds"], 100)
        self.assertEqual(progress["percent_watched"], 1.0)
        self.assertEqual(progress["watch_range_string"], "0-100")

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

    def test_feed_daily_channel_limit_default_is_not_persisted(self) -> None:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = 'feed_daily_channel_limit'"
        ).fetchone()

        self.assertIsNone(row)
        self.assertEqual(self.repository.feed_daily_channel_limit(), 3)
        self.assertFalse(self.repository.has_feed_daily_channel_limit_override())

    def test_feed_daily_channel_limit_can_reset_to_code_default(self) -> None:
        self.repository.set_feed_daily_channel_limit(8)
        self.assertEqual(self.repository.feed_daily_channel_limit(), 8)
        self.assertTrue(self.repository.has_feed_daily_channel_limit_override())

        self.repository.clear_feed_daily_channel_limit()

        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = 'feed_daily_channel_limit'"
        ).fetchone()
        self.assertIsNone(row)
        self.assertEqual(self.repository.feed_daily_channel_limit(), 3)
        self.assertFalse(self.repository.has_feed_daily_channel_limit_override())

    def test_feed_daily_channel_limit_prefers_unwatched_high_view_videos(self) -> None:
        self.connection.execute(
            "UPDATE videos SET published_at = ? WHERE id = ?",
            ("2026-06-09T09:00:00+00:00", "vid1"),
        )
        self.repository.upsert_channel(
            Channel(id="chan2", title="Channel Two", url="https://example.test/chan2")
        )
        for video in [
            Video(
                id="watched_high",
                channel_id="chan1",
                title="Watched High",
                url="https://example.test/watched_high",
                published_at="2026-06-10T10:00:00+00:00",
                view_count=1000,
            ),
            Video(
                id="unwatched_low",
                channel_id="chan1",
                title="Unwatched Low",
                url="https://example.test/unwatched_low",
                published_at="2026-06-10T11:00:00+00:00",
                view_count=10,
            ),
            Video(
                id="unwatched_high",
                channel_id="chan1",
                title="Unwatched High",
                url="https://example.test/unwatched_high",
                published_at="2026-06-10T09:00:00+00:00",
                view_count=500,
            ),
            Video(
                id="next_day",
                channel_id="chan1",
                title="Next Day",
                url="https://example.test/next_day",
                published_at="2026-06-11T09:00:00+00:00",
                view_count=1,
            ),
            Video(
                id="other_channel",
                channel_id="chan2",
                title="Other Channel",
                url="https://example.test/other_channel",
                published_at="2026-06-10T09:00:00+00:00",
                view_count=1,
            ),
        ]:
            self.repository.upsert_video(video)
        self.repository.add_watch_range("watched_high", 0, 10)

        feed = self.repository.subscription_feed(daily_channel_limit=1)

        self.assertIn("unwatched_high", [video.id for video in feed])
        self.assertIn("next_day", [video.id for video in feed])
        self.assertIn("other_channel", [video.id for video in feed])
        self.assertNotIn("watched_high", [video.id for video in feed])
        self.assertNotIn("unwatched_low", [video.id for video in feed])

    def test_hidden_videos_are_excluded_before_feed_daily_limit(self) -> None:
        self.connection.execute(
            "UPDATE videos SET published_at = ?, view_count = ? WHERE id = ?",
            ("2026-06-10T08:00:00+00:00", 1000, "vid1"),
        )
        self.repository.upsert_video(
            Video(
                id="replacement",
                channel_id="chan1",
                title="Replacement",
                url="https://example.test/replacement",
                published_at="2026-06-10T09:00:00+00:00",
                view_count=10,
            )
        )

        self.repository.hide_video("vid1")
        feed = self.repository.subscription_feed(daily_channel_limit=1)

        self.assertEqual([video.id for video in feed], ["replacement"])


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
