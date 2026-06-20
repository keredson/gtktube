from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime, timedelta

from gtktube.db.connection import connect
from gtktube.db.migrations import SCHEMA_VERSION, UnsupportedDatabaseSchema, migrate
from gtktube.db.repositories import LibraryRepository
from gtktube.models import Channel, SponsorBlockSegment, Video, VideoChapter


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
                availability="subscriber_only",
            )
        )

    def tearDown(self) -> None:
        self.connection.close()

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

    def test_video_chapters_round_trip(self) -> None:
        self.repository.replace_video_chapters(
            "vid1",
            [
                VideoChapter(
                    video_id="vid1",
                    title="Intro",
                    start_seconds=0,
                    end_seconds=12.5,
                    position=0,
                ),
                VideoChapter(
                    video_id="vid1",
                    title="Demo",
                    start_seconds=12.5,
                    end_seconds=None,
                    position=1,
                ),
            ],
        )

        chapters = self.repository.video_chapters("vid1")

        self.assertEqual([chapter.title for chapter in chapters], ["Intro", "Demo"])
        self.assertEqual(chapters[0].start_seconds, 0)
        self.assertEqual(chapters[0].end_seconds, 12.5)
        self.assertIsNone(chapters[1].end_seconds)

    def test_external_video_list_can_be_enriched_with_watch_progress(self) -> None:
        self.repository.add_watch_range("vid1", 0, 33)
        self.repository.add_watch_range("vid1", 66, 100)

        videos = self.repository.videos_with_watch_progress(
            [
                Video(
                    id="vid1",
                    channel_id="chan1",
                    channel_title="Channel One",
                    title="External Video",
                    url="https://example.test/watch?v=vid1",
                )
            ]
        )

        self.assertEqual(videos[0].watch_ranges, [(0, 33), (66, 100)])
        self.assertAlmostEqual(videos[0].percent_watched or 0, 0.67)

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

    def test_remove_watch_history_clears_ranges_and_summary(self) -> None:
        self.repository.add_watch_range("vid1", 0, 10)
        self.repository.mark_played("vid1", 100)

        self.repository.remove_watch_history("vid1")

        self.assertEqual(self.repository.watch_history(), [])
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM watch_history WHERE video_id = 'vid1'"
            ).fetchone()
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM watch_ranges WHERE video_id = 'vid1'"
            ).fetchone()[0],
            0,
        )

    def test_video_metadata_round_trips_through_feed(self) -> None:
        feed = self.repository.subscription_feed()

        self.assertEqual(feed[0].description, "Useful context about the video.")
        self.assertEqual(feed[0].duration_seconds, 100)
        self.assertEqual(feed[0].view_count, 12345)
        self.assertEqual(feed[0].availability, "subscriber_only")

    def test_video_availability_updates_when_video_becomes_public(self) -> None:
        self.repository.upsert_video(
            Video(
                id="vid1",
                channel_id="chan1",
                channel_title="Channel One",
                title="A Long Video",
                url="https://example.test/watch?v=vid1",
                availability="public",
            )
        )

        feed = self.repository.subscription_feed()

        self.assertEqual(feed[0].availability, "public")

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

    def test_default_video_quality_uses_code_default_until_overridden(self) -> None:
        self.assertEqual(self.repository.default_video_quality(), "720p")
        self.assertEqual(self.repository.default_playback_mode(), "streaming")
        self.assertFalse(self.repository.has_default_video_quality_override())

        self.repository.set_default_video_quality("1080p", mode="prefetch")

        self.assertEqual(self.repository.default_video_quality(), "1080p")
        self.assertEqual(self.repository.default_playback_mode(), "prefetch")
        self.assertTrue(self.repository.has_default_video_quality_override())
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = 'default_video_quality'"
        ).fetchone()
        self.assertEqual(row["value"], "prefetch:1080p")

        self.repository.clear_default_video_quality()

        self.assertEqual(self.repository.default_video_quality(), "720p")
        self.assertEqual(self.repository.default_playback_mode(), "streaming")
        self.assertFalse(self.repository.has_default_video_quality_override())

    def test_default_video_quality_accepts_legacy_quality_only_values(self) -> None:
        self.repository.set_setting("default_video_quality", "1080p")

        self.assertEqual(self.repository.default_video_quality(), "1080p")
        self.assertEqual(self.repository.default_playback_mode(), "streaming")

    def test_default_video_quality_treats_best_as_unselectable(self) -> None:
        self.repository.set_setting("default_video_quality", "prefetch:best")

        self.assertEqual(self.repository.default_video_quality(), "720p")
        self.assertEqual(self.repository.default_playback_mode(), "prefetch")

    def test_default_video_quality_ignores_unknown_quality(self) -> None:
        self.repository.set_default_video_quality("potato")

        self.assertEqual(self.repository.default_video_quality(), "720p")
        self.assertEqual(self.repository.default_playback_mode(), "streaming")
        self.assertFalse(self.repository.has_default_video_quality_override())

    def test_youtube_watch_history_import_is_off_by_default(self) -> None:
        row = self.connection.execute(
            """
            SELECT value
            FROM settings
            WHERE key = 'import_youtube_watch_history_enabled'
            """
        ).fetchone()

        self.assertIsNone(row)
        self.assertFalse(self.repository.import_youtube_watch_history_enabled())

    def test_youtube_watch_history_import_setting_round_trips(self) -> None:
        self.repository.set_import_youtube_watch_history_enabled(True)
        self.assertTrue(self.repository.import_youtube_watch_history_enabled())

        self.repository.set_import_youtube_watch_history_enabled(False)
        self.assertFalse(self.repository.import_youtube_watch_history_enabled())

    def test_youtube_watch_history_import_due_tracks_last_import(self) -> None:
        self.assertTrue(self.repository.youtube_watch_history_import_due())

        self.repository.mark_youtube_watch_history_import()
        self.assertFalse(self.repository.youtube_watch_history_import_due())

        old_import = (datetime.now(UTC) - timedelta(hours=2)).isoformat(
            timespec="seconds"
        )
        self.repository.set_setting("youtube_watch_history_last_import_at", old_import)

        self.assertTrue(self.repository.youtube_watch_history_import_due())

    def test_refresh_worker_count_uses_code_default_until_overridden(self) -> None:
        self.assertEqual(self.repository.refresh_worker_count(), 10)
        self.assertFalse(self.repository.has_refresh_worker_count_override())

        self.repository.set_refresh_worker_count(5)

        self.assertEqual(self.repository.refresh_worker_count(), 5)
        self.assertTrue(self.repository.has_refresh_worker_count_override())

        self.repository.clear_refresh_worker_count()

        self.assertEqual(self.repository.refresh_worker_count(), 10)
        self.assertFalse(self.repository.has_refresh_worker_count_override())

    def test_refresh_worker_count_is_clamped(self) -> None:
        self.repository.set_refresh_worker_count(99)
        self.assertEqual(self.repository.refresh_worker_count(), 20)

        self.repository.set_refresh_worker_count(0)
        self.assertEqual(self.repository.refresh_worker_count(), 1)

    def test_channel_needs_refresh_when_never_successfully_checked(self) -> None:
        self.assertTrue(self.repository.channel_needs_refresh("chan1"))

    def test_channel_needs_refresh_after_max_age(self) -> None:
        checked_at = (datetime.now(UTC) - timedelta(hours=7)).isoformat(
            timespec="seconds"
        )
        self.connection.execute(
            """
            UPDATE channels
            SET last_successful_check_at = ?
            WHERE id = ?
            """,
            (checked_at, "chan1"),
        )

        self.assertTrue(self.repository.channel_needs_refresh("chan1"))

    def test_channel_does_not_need_refresh_when_recently_checked(self) -> None:
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.connection.execute(
            """
            UPDATE channels
            SET last_successful_check_at = ?
            WHERE id = ?
            """,
            (checked_at, "chan1"),
        )

        self.assertFalse(self.repository.channel_needs_refresh("chan1"))

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

    def test_subscription_feed_excludes_shorts(self) -> None:
        self.repository.upsert_video(
            Video(
                id="short1",
                channel_id="chan1",
                title="Short",
                url="https://www.youtube.com/shorts/short1",
                kind="short",
                published_at="2026-06-10T10:00:00+00:00",
                view_count=1000,
            )
        )

        feed = self.repository.subscription_feed()

        self.assertIn("vid1", [video.id for video in feed])
        self.assertNotIn("short1", [video.id for video in feed])

    def test_subscription_feed_orders_missing_published_dates_by_discovery(self) -> None:
        self.connection.execute(
            "UPDATE videos SET published_at = ? WHERE id = ?",
            ("2026-06-13", "vid1"),
        )
        self.repository.upsert_video(
            Video(
                id="fresh_without_date",
                channel_id="chan1",
                title="Fresh Without Date",
                url="https://example.test/fresh_without_date",
            )
        )

        feed = self.repository.subscription_feed()

        self.assertEqual(feed[0].id, "fresh_without_date")

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

    def test_new_video_counts_ignore_initial_subscription_baseline(self) -> None:
        self.repository.upsert_video(
            Video(
                id="new_video",
                channel_id="chan1",
                title="New Video",
                url="https://example.test/new_video",
            )
        )

        self.repository.clear_new_video_indicator("chan1")

        self.assertEqual(self.repository.new_video_counts_by_channel(), {})

    def test_new_video_counts_clear_when_video_is_watched(self) -> None:
        self.connection.execute(
            """
            UPDATE channels
            SET new_videos_cleared_at = ?
            WHERE id = ?
            """,
            ("2000-01-01T00:00:00+00:00", "chan1"),
        )
        self.repository.record_play_started("vid1")
        self.repository.upsert_video(
            Video(
                id="new_video",
                channel_id="chan1",
                title="New Video",
                url="https://example.test/new_video",
            )
        )

        self.assertEqual(self.repository.new_video_counts_by_channel(), {"chan1": 1})

        self.repository.record_play_started("new_video")

        self.assertEqual(self.repository.new_video_counts_by_channel(), {})

    def test_sponsorblock_defaults_are_disabled_and_sponsor_only(self) -> None:
        self.assertFalse(self.repository.sponsorblock_enabled())
        self.assertFalse(self.repository.sponsorblock_prompt_shown())
        self.assertEqual(self.repository.sponsorblock_categories(), ["sponsor"])

    def test_sponsorblock_settings_round_trip(self) -> None:
        self.repository.set_sponsorblock_enabled(True)
        self.repository.set_sponsorblock_categories(["sponsor", "intro", "bad"])
        self.repository.set_sponsorblock_prompt_shown()

        self.assertTrue(self.repository.sponsorblock_enabled())
        self.assertTrue(self.repository.sponsorblock_prompt_shown())
        self.assertEqual(
            self.repository.sponsorblock_categories(),
            ["sponsor", "intro"],
        )

    def test_sponsorblock_segment_cache_tracks_empty_fetches(self) -> None:
        segments, fresh = self.repository.cached_sponsorblock_segments(
            "vid1",
            ["sponsor"],
        )
        self.assertEqual(segments, [])
        self.assertFalse(fresh)

        self.repository.store_sponsorblock_segments("vid1", ["sponsor"], [])

        segments, fresh = self.repository.cached_sponsorblock_segments(
            "vid1",
            ["sponsor"],
        )
        self.assertEqual(segments, [])
        self.assertTrue(fresh)

    def test_sponsorblock_segment_cache_round_trips_segments(self) -> None:
        self.repository.store_sponsorblock_segments(
            "vid1",
            ["sponsor"],
            [
                SponsorBlockSegment(
                    video_id="vid1",
                    category="sponsor",
                    start_seconds=10.5,
                    end_seconds=20.25,
                    action_type="skip",
                    uuid="segment-1",
                )
            ],
        )

        segments, fresh = self.repository.cached_sponsorblock_segments(
            "vid1",
            ["sponsor"],
        )

        self.assertTrue(fresh)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].uuid, "segment-1")
        self.assertEqual(segments[0].start_seconds, 10.5)


class ConnectionTests(unittest.TestCase):
    def test_connect_uses_row_factory(self) -> None:
        connection = connect(":memory:")
        try:
            migrate(connection)
            row = connection.execute("PRAGMA user_version").fetchone()
            self.assertEqual(row[0], SCHEMA_VERSION)
        finally:
            connection.close()

    def test_migrate_rejects_newer_schema_with_typed_error(self) -> None:
        connection = connect(":memory:")
        try:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

            with self.assertRaises(UnsupportedDatabaseSchema) as raised:
                migrate(connection)

            self.assertEqual(raised.exception.current, SCHEMA_VERSION + 1)
            self.assertEqual(raised.exception.supported, SCHEMA_VERSION)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
