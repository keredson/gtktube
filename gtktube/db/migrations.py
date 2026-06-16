from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 9


def migrate(connection: sqlite3.Connection) -> None:
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than supported {SCHEMA_VERSION}"
        )
    if current < 1:
        _migrate_1(connection)
        current = 1
    if current < 2:
        _migrate_2(connection)
        current = 2
    if current < 3:
        _migrate_3(connection)
        current = 3
    if current < 4:
        _migrate_4(connection)
        current = 4
    if current < 5:
        _migrate_5(connection)
        current = 5
    if current < 6:
        _migrate_6(connection)
        current = 6
    if current < 7:
        _migrate_7(connection)
        current = 7
    if current < 8:
        _migrate_8(connection)
        current = 8
    if current < 9:
        _migrate_9(connection)


def _migrate_1(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                handle TEXT,
                thumbnail_url TEXT,
                thumbnail_path TEXT,
                description TEXT,
                is_subscribed INTEGER NOT NULL DEFAULT 1,
                subscribed_at TEXT,
                unsubscribed_at TEXT,
                last_checked_at TEXT,
                last_successful_check_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE videos (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                thumbnail_url TEXT,
                thumbnail_path TEXT,
                description TEXT,
                duration_seconds INTEGER,
                published_at TEXT,
                discovered_at TEXT NOT NULL,
                live_status TEXT,
                view_count INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (channel_id) REFERENCES channels(id)
            );

            CREATE INDEX idx_videos_channel_published
            ON videos(channel_id, published_at DESC, discovered_at DESC);

            CREATE TABLE watch_history (
                video_id TEXT PRIMARY KEY,
                first_watched_at TEXT,
                last_watched_at TEXT,
                completed INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                play_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (video_id) REFERENCES videos(id)
            );

            CREATE INDEX idx_watch_history_last_watched_at
            ON watch_history(last_watched_at DESC);

            CREATE TABLE watch_ranges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                start_seconds INTEGER NOT NULL,
                end_seconds INTEGER NOT NULL,
                last_watched_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (video_id) REFERENCES videos(id),
                CHECK (start_seconds >= 0),
                CHECK (end_seconds > start_seconds)
            );

            CREATE INDEX idx_watch_ranges_video_id
            ON watch_ranges(video_id);

            CREATE INDEX idx_watch_ranges_last_watched_at
            ON watch_ranges(last_watched_at DESC);

            CREATE TABLE search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                searched_at TEXT NOT NULL
            );

            CREATE INDEX idx_search_history_searched_at
            ON search_history(searched_at DESC);

            CREATE TABLE refresh_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE VIEW watch_progress AS
            WITH ordered_ranges AS (
                SELECT
                    id,
                    video_id,
                    start_seconds,
                    end_seconds,
                    MAX(end_seconds) OVER (
                        PARTITION BY video_id
                        ORDER BY start_seconds, end_seconds, id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                    ) AS previous_max_end
                FROM watch_ranges
            ),
            range_groups AS (
                SELECT
                    id,
                    video_id,
                    start_seconds,
                    end_seconds,
                    SUM(
                        CASE
                            WHEN previous_max_end IS NULL
                              OR start_seconds > previous_max_end
                            THEN 1
                            ELSE 0
                        END
                    ) OVER (
                        PARTITION BY video_id
                        ORDER BY start_seconds, end_seconds, id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS group_id
                FROM ordered_ranges
            ),
            merged_ranges AS (
                SELECT
                    video_id,
                    MIN(start_seconds) AS start_seconds,
                    MAX(end_seconds) AS end_seconds
                FROM range_groups
                GROUP BY video_id, group_id
            ),
            covered AS (
                SELECT
                    video_id,
                    SUM(end_seconds - start_seconds) AS covered_seconds
                FROM merged_ranges
                GROUP BY video_id
            )
            SELECT
                v.id AS video_id,
                COALESCE(c.covered_seconds, 0) AS covered_seconds,
                v.duration_seconds,
                CASE
                    WHEN v.duration_seconds IS NULL OR v.duration_seconds <= 0 THEN NULL
                    ELSE MIN(
                        1.0,
                        CAST(COALESCE(c.covered_seconds, 0) AS REAL) / v.duration_seconds
                    )
                END AS percent_watched
            FROM videos v
            LEFT JOIN covered c ON c.video_id = v.id;

            CREATE TRIGGER watch_ranges_after_insert_summary
            AFTER INSERT ON watch_ranges
            BEGIN
                INSERT INTO watch_history (
                    video_id,
                    first_watched_at,
                    last_watched_at,
                    updated_at
                )
                VALUES (
                    NEW.video_id,
                    NEW.last_watched_at,
                    NEW.last_watched_at,
                    NEW.updated_at
                )
                ON CONFLICT(video_id) DO UPDATE SET
                    first_watched_at = MIN(
                        watch_history.first_watched_at,
                        NEW.last_watched_at
                    ),
                    last_watched_at = MAX(
                        watch_history.last_watched_at,
                        NEW.last_watched_at
                    ),
                    updated_at = NEW.updated_at;
            END;

            PRAGMA user_version = 1;
            """
        )


def _migrate_2(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            PRAGMA user_version = 2;
            """
        )


def _migrate_3(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            CREATE TABLE watch_later (
                video_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL,
                FOREIGN KEY (video_id) REFERENCES videos(id)
            );

            CREATE INDEX idx_watch_later_added_at
            ON watch_later(added_at DESC);

            PRAGMA user_version = 3;
            """
        )


def _migrate_4(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            DROP VIEW watch_progress;

            CREATE VIEW watch_progress AS
            WITH ordered_ranges AS (
                SELECT
                    id,
                    video_id,
                    start_seconds,
                    end_seconds,
                    MAX(end_seconds) OVER (
                        PARTITION BY video_id
                        ORDER BY start_seconds, end_seconds, id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                    ) AS previous_max_end
                FROM watch_ranges
            ),
            range_groups AS (
                SELECT
                    id,
                    video_id,
                    start_seconds,
                    end_seconds,
                    SUM(
                        CASE
                            WHEN previous_max_end IS NULL
                              OR start_seconds > previous_max_end
                            THEN 1
                            ELSE 0
                        END
                    ) OVER (
                        PARTITION BY video_id
                        ORDER BY start_seconds, end_seconds, id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS group_id
                FROM ordered_ranges
            ),
            merged_ranges AS (
                SELECT
                    video_id,
                    MIN(start_seconds) AS start_seconds,
                    MAX(end_seconds) AS end_seconds
                FROM range_groups
                GROUP BY video_id, group_id
            ),
            concatenated AS (
                SELECT
                    video_id,
                    GROUP_CONCAT(start_seconds || '-' || end_seconds) AS ranges
                FROM (
                    SELECT video_id, start_seconds, end_seconds
                    FROM merged_ranges
                    ORDER BY video_id, start_seconds, end_seconds
                )
                GROUP BY video_id
            ),
            covered AS (
                SELECT
                    video_id,
                    SUM(end_seconds - start_seconds) AS covered_seconds
                FROM merged_ranges
                GROUP BY video_id
            )
            SELECT
                v.id AS video_id,
                COALESCE(c.covered_seconds, 0) AS covered_seconds,
                v.duration_seconds,
                CASE
                    WHEN v.duration_seconds IS NULL OR v.duration_seconds <= 0 THEN NULL
                    ELSE MIN(
                        1.0,
                        CAST(COALESCE(c.covered_seconds, 0) AS REAL) / v.duration_seconds
                    )
                END AS percent_watched,
                con.ranges AS watch_range_string
            FROM videos v
            LEFT JOIN covered c ON c.video_id = v.id
            LEFT JOIN concatenated con ON con.video_id = v.id;

            PRAGMA user_version = 4;
            """
        )


def _migrate_5(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            PRAGMA user_version = 5;
            """
        )


def _migrate_6(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            CREATE TABLE hidden_videos (
                video_id TEXT PRIMARY KEY,
                hidden_at TEXT NOT NULL,
                reason TEXT,
                FOREIGN KEY (video_id) REFERENCES videos(id)
            );

            CREATE INDEX idx_hidden_videos_hidden_at
            ON hidden_videos(hidden_at DESC);

            PRAGMA user_version = 6;
            """
        )


def _migrate_7(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            ALTER TABLE channels
            ADD COLUMN new_videos_cleared_at TEXT;

            UPDATE channels
            SET new_videos_cleared_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE is_subscribed = 1
              AND new_videos_cleared_at IS NULL;

            CREATE INDEX idx_videos_channel_discovered
            ON videos(channel_id, discovered_at DESC);

            PRAGMA user_version = 7;
            """
        )


def _migrate_8(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            CREATE TABLE sponsorblock_segments (
                video_id TEXT NOT NULL,
                category TEXT NOT NULL,
                action_type TEXT,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                uuid TEXT,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (video_id, category, start_seconds, end_seconds)
            );

            CREATE INDEX idx_sponsorblock_segments_video
            ON sponsorblock_segments(video_id);

            CREATE TABLE sponsorblock_segment_fetches (
                video_id TEXT NOT NULL,
                categories_key TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (video_id, categories_key)
            );

            PRAGMA user_version = 8;
            """
        )


def _migrate_9(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            ALTER TABLE videos
            ADD COLUMN kind TEXT NOT NULL DEFAULT 'video';

            UPDATE videos
            SET kind = 'short'
            WHERE url LIKE '%/shorts/%';

            CREATE INDEX idx_videos_kind_channel_published
            ON videos(kind, channel_id, published_at DESC, discovered_at DESC);

            PRAGMA user_version = 9;
            """
        )
