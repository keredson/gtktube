from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Iterable

from gtktube.extractors.youtube import QUALITY_FORMATS
from gtktube.models import Channel, Video


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LibraryRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._lock = threading.RLock()

    def upsert_channel(self, channel: Channel, subscribed: bool = True) -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO channels (
                    id, title, url, handle, thumbnail_url, is_subscribed,
                    subscribed_at, new_videos_cleared_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    handle = COALESCE(excluded.handle, channels.handle),
                    thumbnail_url = COALESCE(excluded.thumbnail_url, channels.thumbnail_url),
                    is_subscribed = CASE
                        WHEN excluded.is_subscribed = 1 THEN 1
                        ELSE channels.is_subscribed
                    END,
                    subscribed_at = CASE
                        WHEN excluded.is_subscribed = 1
                        THEN COALESCE(channels.subscribed_at, excluded.subscribed_at)
                        ELSE channels.subscribed_at
                    END,
                    new_videos_cleared_at = CASE
                        WHEN excluded.is_subscribed = 1
                        THEN COALESCE(channels.new_videos_cleared_at, excluded.new_videos_cleared_at)
                        ELSE channels.new_videos_cleared_at
                    END,
                    unsubscribed_at = CASE
                        WHEN excluded.is_subscribed = 1 THEN NULL
                        ELSE channels.unsubscribed_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    channel.id,
                    channel.title,
                    channel.url,
                    channel.handle,
                    channel.thumbnail_url,
                    1 if subscribed else 0,
                    now if subscribed else None,
                    now if subscribed else None,
                    now,
                    now,
                ),
            )

    def unsubscribe_channel(self, channel_id: str) -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE channels
                SET is_subscribed = 0, unsubscribed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, channel_id),
            )

    def subscribed_channels(self) -> list[Channel]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT id, title, url, handle, thumbnail_url, is_subscribed
                FROM channels
                WHERE is_subscribed = 1
                ORDER BY title COLLATE NOCASE
                """
            ).fetchall()
        return [self._channel_from_row(row) for row in rows]

    def channel(self, channel_id: str) -> Channel | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT id, title, url, handle, thumbnail_url, is_subscribed
                FROM channels
                WHERE id = ?
                """,
                (channel_id,),
            ).fetchone()
        return self._channel_from_row(row) if row else None

    def is_subscribed(self, channel_id: str | None) -> bool:
        if not channel_id:
            return False
        with self._lock:
            row = self.connection.execute(
                "SELECT is_subscribed FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()
        return bool(row and row["is_subscribed"])

    def mark_channel_refresh(self, channel_id: str, success: bool) -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE channels
                SET last_checked_at = ?,
                    last_successful_check_at = CASE WHEN ? THEN ? ELSE last_successful_check_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, 1 if success else 0, now, now, channel_id),
            )

    def channel_needs_refresh(self, channel_id: str, max_age_hours: int = 6) -> bool:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT last_successful_check_at
                FROM channels
                WHERE id = ?
                """,
                (channel_id,),
            ).fetchone()
        if row is None or row["last_successful_check_at"] is None:
            return True
        try:
            checked_at = datetime.fromisoformat(
                str(row["last_successful_check_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            return True
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - checked_at > timedelta(hours=max_age_hours)

    def upsert_video(self, video: Video) -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO videos (
                    id, channel_id, title, url, thumbnail_url, duration_seconds,
                    published_at, view_count, description, discovered_at,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    channel_id = COALESCE(excluded.channel_id, videos.channel_id),
                    title = excluded.title,
                    url = excluded.url,
                    thumbnail_url = COALESCE(excluded.thumbnail_url, videos.thumbnail_url),
                    duration_seconds = COALESCE(excluded.duration_seconds, videos.duration_seconds),
                    published_at = COALESCE(excluded.published_at, videos.published_at),
                    view_count = COALESCE(excluded.view_count, videos.view_count),
                    description = COALESCE(excluded.description, videos.description),
                    updated_at = excluded.updated_at
                """,
                (
                    video.id,
                    video.channel_id,
                    video.title,
                    video.url,
                    video.thumbnail_url,
                    video.duration_seconds,
                    video.published_at,
                    video.view_count,
                    video.description,
                    now,
                    now,
                    now,
                ),
            )

    def upsert_videos(self, videos: Iterable[Video]) -> None:
        with self._lock:
            for video in videos:
                self.upsert_video(video)

    def setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else default

    def has_setting(self, key: str) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        return bool(row)

    def int_setting(self, key: str, default: int) -> int:
        try:
            return int(self.setting(key, str(default)))
        except ValueError:
            return default

    def set_setting(self, key: str, value: str) -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def clear_setting(self, key: str) -> None:
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM settings WHERE key = ?", (key,))

    def feed_daily_channel_limit(self) -> int:
        return max(1, self.int_setting("feed_daily_channel_limit", 3))

    def has_feed_daily_channel_limit_override(self) -> bool:
        return self.has_setting("feed_daily_channel_limit")

    def set_feed_daily_channel_limit(self, limit: int) -> None:
        self.set_setting("feed_daily_channel_limit", str(max(1, limit)))

    def clear_feed_daily_channel_limit(self) -> None:
        self.clear_setting("feed_daily_channel_limit")

    def default_video_quality(self) -> str:
        quality = self.setting("default_video_quality", "720p")
        return quality if quality in QUALITY_FORMATS else "720p"

    def has_default_video_quality_override(self) -> bool:
        return self.has_setting("default_video_quality")

    def set_default_video_quality(self, quality: str) -> None:
        if quality in QUALITY_FORMATS:
            self.set_setting("default_video_quality", quality)

    def clear_default_video_quality(self) -> None:
        self.clear_setting("default_video_quality")

    def new_video_counts_by_channel(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT
                    c.id AS channel_id,
                    COUNT(v.id) AS count
                FROM channels c
                JOIN videos v ON v.channel_id = c.id
                LEFT JOIN watch_history wh ON wh.video_id = v.id
                WHERE c.is_subscribed = 1
                  AND c.new_videos_cleared_at IS NOT NULL
                  AND v.discovered_at > c.new_videos_cleared_at
                  AND wh.video_id IS NULL
                GROUP BY c.id
                """
            ).fetchall()
        return {row["channel_id"]: int(row["count"]) for row in rows}

    def clear_new_video_indicator(self, channel_id: str) -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE channels
                SET new_videos_cleared_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, channel_id),
            )

    def subscription_feed(
        self,
        limit: int = 100,
        daily_channel_limit: int | None = None,
    ) -> list[Video]:
        daily_channel_limit = daily_channel_limit or self.feed_daily_channel_limit()
        return self._videos_query(
            """
            WITH ranked_videos AS (
                SELECT
                    v.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            v.channel_id,
                            date(COALESCE(v.published_at, v.discovered_at))
                        ORDER BY
                            CASE WHEN wh.video_id IS NULL THEN 0 ELSE 1 END,
                            COALESCE(wh.completed, 0) ASC,
                            COALESCE(wp.percent_watched, 0) ASC,
                            COALESCE(v.view_count, 0) DESC,
                            CASE WHEN v.published_at IS NULL THEN 1 ELSE 0 END,
                            COALESCE(v.published_at, v.discovered_at) DESC,
                            v.id
                    ) AS channel_day_rank
                FROM videos v
                LEFT JOIN watch_progress wp ON wp.video_id = v.id
                LEFT JOIN watch_history wh ON wh.video_id = v.id
                LEFT JOIN hidden_videos hv ON hv.video_id = v.id
                WHERE hv.video_id IS NULL
            )
            SELECT
                v.id, v.title, v.url, v.channel_id, c.title AS channel_title,
                v.thumbnail_url, v.description, v.duration_seconds,
                v.published_at, v.view_count,
                COALESCE(wp.percent_watched, 0) AS percent_watched,
                wp.watch_range_string,
                COALESCE(wh.completed, 0) AS completed
            FROM ranked_videos v
            JOIN channels c ON c.id = v.channel_id
            LEFT JOIN watch_progress wp ON wp.video_id = v.id
            LEFT JOIN watch_history wh ON wh.video_id = v.id
            WHERE c.is_subscribed = 1
              AND v.channel_day_rank <= ?
            ORDER BY
                CASE WHEN v.published_at IS NULL THEN 1 ELSE 0 END,
                COALESCE(v.published_at, v.discovered_at) DESC
            LIMIT ?
            """,
            (daily_channel_limit, limit),
        )

    def channel_videos(self, channel_id: str, limit: int = 100) -> list[Video]:
        return self._videos_query(
            """
            SELECT
                v.id, v.title, v.url, v.channel_id, c.title AS channel_title,
                v.thumbnail_url, v.description, v.duration_seconds,
                v.published_at, v.view_count,
                COALESCE(wp.percent_watched, 0) AS percent_watched,
                wp.watch_range_string,
                COALESCE(wh.completed, 0) AS completed
            FROM videos v
            LEFT JOIN channels c ON c.id = v.channel_id
            LEFT JOIN watch_progress wp ON wp.video_id = v.id
            LEFT JOIN watch_history wh ON wh.video_id = v.id
            WHERE v.channel_id = ?
            ORDER BY
                CASE WHEN v.published_at IS NULL THEN 1 ELSE 0 END,
                COALESCE(v.published_at, v.discovered_at) DESC
            LIMIT ?
            """,
            (channel_id, limit),
        )

    def channel_video_count(self, channel_id: str) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM videos WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def watch_history(self, query: str = "", limit: int = 100) -> list[Video]:
        pattern = f"%{query.strip()}%"
        params: tuple[object, ...]
        where = ""
        if query.strip():
            where = "AND (v.title LIKE ? OR c.title LIKE ? OR wh.last_watched_at LIKE ?)"
            params = (pattern, pattern, pattern, limit)
        else:
            params = (limit,)

        return self._videos_query(
            f"""
            SELECT
                v.id, v.title, v.url, v.channel_id, c.title AS channel_title,
                v.thumbnail_url, v.description, v.duration_seconds,
                v.published_at, v.view_count,
                COALESCE(wp.percent_watched, 0) AS percent_watched,
                wp.watch_range_string,
                COALESCE(wh.completed, 0) AS completed
            FROM watch_history wh
            JOIN videos v ON v.id = wh.video_id
            LEFT JOIN channels c ON c.id = v.channel_id
            LEFT JOIN watch_progress wp ON wp.video_id = v.id
            WHERE wh.last_watched_at IS NOT NULL
            {where}
            ORDER BY wh.last_watched_at DESC
            LIMIT ?
            """,
            params,
        )

    def add_search_history(self, query: str) -> None:
        stripped = query.strip()
        if not stripped:
            return
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO search_history (query, searched_at) VALUES (?, ?)",
                (stripped, utcnow()),
            )

    def recent_searches(self, limit: int = 20) -> list[str]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT query
                FROM search_history
                GROUP BY query
                ORDER BY MAX(searched_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row["query"] for row in rows]

    def add_watch_later(self, video_id: str) -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO watch_later (video_id, added_at)
                VALUES (?, ?)
                ON CONFLICT(video_id) DO NOTHING
                """,
                (video_id, now),
            )

    def remove_watch_later(self, video_id: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "DELETE FROM watch_later WHERE video_id = ?", (video_id,)
            )

    def is_watch_later(self, video_id: str) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM watch_later WHERE video_id = ?", (video_id,)
            ).fetchone()
        return bool(row)

    def watch_later_videos(self, limit: int = 100) -> list[Video]:
        return self._videos_query(
            """
            SELECT
                v.id, v.title, v.url, v.channel_id, c.title AS channel_title,
                v.thumbnail_url, v.description, v.duration_seconds,
                v.published_at, v.view_count,
                COALESCE(wp.percent_watched, 0) AS percent_watched,
                wp.watch_range_string,
                COALESCE(wh.completed, 0) AS completed
            FROM watch_later wl
            JOIN videos v ON v.id = wl.video_id
            LEFT JOIN channels c ON c.id = v.channel_id
            LEFT JOIN watch_progress wp ON wp.video_id = v.id
            LEFT JOIN watch_history wh ON wh.video_id = v.id
            ORDER BY wl.added_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def record_play_started(self, video_id: str) -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO watch_history (video_id, first_watched_at, last_watched_at, play_count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    last_watched_at = excluded.last_watched_at,
                    play_count = watch_history.play_count + 1,
                    updated_at = excluded.updated_at
                """,
                (video_id, now, now, now),
            )
            self.connection.execute(
                "DELETE FROM watch_later WHERE video_id = ?", (video_id,)
            )

    def mark_played(self, video_id: str, duration_seconds: int | None = None) -> None:
        now = utcnow()
        with self._lock, self.connection:
            if duration_seconds is not None and duration_seconds > 0:
                self.connection.execute(
                    """
                    INSERT INTO watch_ranges (
                        video_id, start_seconds, end_seconds, last_watched_at,
                        created_at, updated_at
                    )
                    VALUES (?, 0, ?, ?, ?, ?)
                    """,
                    (video_id, duration_seconds, now, now, now),
                )
            self.connection.execute(
                """
                INSERT INTO watch_history (
                    video_id, first_watched_at, last_watched_at, completed,
                    completed_at, play_count, updated_at
                )
                VALUES (?, ?, ?, 1, ?, 0, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    first_watched_at = COALESCE(watch_history.first_watched_at, excluded.first_watched_at),
                    completed = 1,
                    completed_at = COALESCE(watch_history.completed_at, excluded.completed_at),
                    last_watched_at = excluded.last_watched_at,
                    updated_at = excluded.updated_at
                """,
                (video_id, now, now, now, now),
            )

    def hide_video(self, video_id: str, reason: str = "not_interested") -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO hidden_videos (video_id, hidden_at, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    hidden_at = excluded.hidden_at,
                    reason = excluded.reason
                """,
                (video_id, now, reason),
            )

    def add_watch_range(
        self,
        video_id: str,
        start_seconds: int,
        end_seconds: int,
        completion_threshold: float = 0.9,
    ) -> None:
        now = utcnow()
        if end_seconds <= start_seconds:
            return
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO watch_ranges (
                    video_id, start_seconds, end_seconds, last_watched_at,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (video_id, start_seconds, end_seconds, now, now, now),
            )
            self.connection.execute(
                """
                UPDATE watch_history
                SET completed = 1,
                    completed_at = COALESCE(completed_at, ?),
                    updated_at = ?
                WHERE video_id = ?
                  AND (
                    SELECT percent_watched
                    FROM watch_progress
                    WHERE video_id = ?
                  ) >= ?
                """,
                (now, now, video_id, video_id, completion_threshold),
            )

    def resume_position(self, video_id: str) -> int:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT end_seconds
                FROM watch_ranges
                WHERE video_id = ?
                ORDER BY last_watched_at DESC, id DESC
                LIMIT 1
                """,
                (video_id,),
            ).fetchone()
        return int(row["end_seconds"]) if row else 0

    def _videos_query(
        self, sql: str, params: tuple[object, ...] = ()
    ) -> list[Video]:
        with self._lock:
            rows = self.connection.execute(sql, params).fetchall()
        return [self._video_from_row(row) for row in rows]

    def _video_from_row(self, row: sqlite3.Row) -> Video:
        ranges = []
        if "watch_range_string" in row.keys() and row["watch_range_string"]:
            for part in row["watch_range_string"].split(","):
                try:
                    start, end = part.split("-")
                    ranges.append((int(start), int(end)))
                except (ValueError, TypeError):
                    continue

        return Video(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            channel_id=row["channel_id"],
            channel_title=row["channel_title"],
            thumbnail_url=row["thumbnail_url"],
            description=row["description"],
            duration_seconds=row["duration_seconds"],
            published_at=row["published_at"],
            view_count=row["view_count"],
            percent_watched=row["percent_watched"],
            watch_ranges=ranges or None,
            completed=bool(row["completed"]),
        )

    def _channel_from_row(self, row: sqlite3.Row) -> Channel:
        return Channel(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            handle=row["handle"],
            thumbnail_url=row["thumbnail_url"],
            is_subscribed=bool(row["is_subscribed"]),
        )
