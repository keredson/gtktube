# GTKTube Design Document

## Overview

GTKTube is a local-first GTK4 desktop application for browsing and watching
YouTube videos without using a Google account. It uses `yt-dlp` as the YouTube
extraction layer, stores subscriptions and viewing state in a local SQLite
database, and presents a feed made only from channels the user has explicitly
subscribed to.

The application intentionally avoids OAuth, Google account integration,
personalized recommendations, comments, likes, and server-side sync.

## Goals

- Play YouTube videos inside a native Python/GTK4 desktop app using embedded
  MPV playback.
- Use `yt-dlp` as a Python library to resolve video metadata and stream URLs.
- Store all application state locally in SQLite.
- Let users subscribe to YouTube channels by URL, handle, channel ID, or video
  URL.
- Show recent videos from all subscriptions or from a single channel.
- Track watched videos, last watched time, and watched time ranges locally.
- Provide search without requiring a YouTube API key or Google login.
- Keep the UI focused on subscriptions, search, and playback.

## Non-Goals

- No Google OAuth.
- No YouTube Data API dependency.
- No cloud sync.
- No personalized recommendation feed.
- No comments, likes, dislikes, subscriptions through the user's Google
  account, or notification bell behavior.
- No background downloading by default.
- No subscription import/export in the first version.
- No playlist browsing in the first version.
- No SponsorBlock-style integrations in the first version.
- No cookies or account-derived browser session support in the first version.
- No attempt to replace every YouTube website feature.

## Architecture

The app is split into five main layers:

1. GTK application shell
2. Playback layer
3. YouTube extraction layer
4. Local persistence layer
5. Background refresh layer

```text
GTK UI
  |
  |-- Video lists, channel pages, search, player controls
  |
Application services
  |
  |-- Subscription service
  |-- Feed service
  |-- Search service
  |-- Watch history service
  |
SQLite database
  |
yt-dlp extraction
  |
YouTube web endpoints
```

The database is the source of truth for subscriptions, known videos, and watch
state. `yt-dlp` is treated as an external extractor that can fail, return partial
metadata, or need updates as YouTube changes.

## Technology Choices

### Language and UI

- Python 3
- PyGObject
- GTK4
- libadwaita, if the target platform supports it cleanly

GTK4 provides the native application shell, list views, navigation, dialogs, and
keyboard shortcuts. libadwaita is preferred for modern GNOME-style navigation
and adaptive layouts, but the app should avoid depending on libadwaita-specific
patterns where plain GTK4 is sufficient.

### Video Playback

The playback stack is MPV embedded in the GTK player surface.
Playback must remain visually integrated with the app; it must not launch or
manage a separate playback window.

The app should resolve fresh stream URLs through `yt-dlp` immediately before
playback. Resolved media URLs should not be persisted because they can expire.

MPV handles local files, combined audio/video stream URLs, and separate
audio/video stream URLs directly.

Fullscreen is video-only. Activating fullscreen should move or render only the
video surface into a fullscreen presentation and should not fullscreen the
entire application chrome, navigation, metadata, or controls.

### MPV Threading and Property Safety

MPV property access from the GTK main thread can block indefinitely when mpv is
loading, idling, shutting down, or transitioning between files. Treat all
ctypes-backed mpv property reads and writes as potentially blocking calls, even
simple attributes such as `pause`, `speed`, `sid`, and duration/cache
properties.

Do not add polling loops, GLib timeout callbacks, idle callbacks, property
observers, or UI refresh paths that repeatedly call mpv getters or setters to
"confirm" state. This has caused UI hangs with stacks blocked inside
`mpv_get_property` and `mpv_set_property_string`.

The safe pattern is:

- Write mpv properties only at explicit command boundaries, such as initial
  `loadfile`, user play/pause, user seek, selected speed change, or caption
  selection.
- Keep UI state from observed property callbacks and cached values in
  `mpv_observed_properties`; do not query mpv synchronously just to update UI.
- Ignore stale callbacks by checking the current player/request before acting.
- Never respond to an mpv property callback by immediately writing another mpv
  property as a state correction.
- Never add delayed "confirmation" timers that re-set mpv properties after
  startup. If startup ordering is wrong, fix the startup sequence or saved
  resume state instead.

When debugging autoplay or transition bugs, prefer logs and request/player
guards over extra mpv property calls. A video that resumes at or past its
duration should be handled by sanitizing the saved resume position before
`loadfile`, not by repeatedly forcing `pause = False`.

### YouTube Extraction

Use `yt-dlp` as a Python library, not as a shell command.

Responsibilities:

- Resolve video metadata.
- Resolve stream URLs.
- Resolve channel metadata.
- Fetch recent channel uploads.
- Run search queries using `ytsearch` or equivalent extractor support.

The app should isolate all `yt-dlp` usage behind a small internal adapter so
that extractor options, error handling, and future workarounds are centralized.

### Persistence

Use SQLite through Python's standard `sqlite3` module initially.

Schema versioning should use SQLite's built-in `PRAGMA user_version`, not an
application metadata table. Migrations should run inside explicit transactions,
check the current `user_version`, apply each migration in order, and set
`user_version` only after the migration succeeds.

## Core User Flows

### Subscribe to Channel

1. User opens the subscribe dialog.
2. User enters a channel URL, handle URL, channel ID URL, or video URL.
3. The app asks `yt-dlp` to resolve metadata.
4. The app canonicalizes the result to a stable channel ID and channel URL.
5. The app stores or updates the channel in SQLite.
6. The app fetches recent uploads in the background.
7. The channel appears in the subscription list.

If resolution fails, the app shows a clear error and does not create a partial
subscription.

### Browse Subscription Feed

1. User opens the main feed.
2. The app queries locally cached videos from subscribed channels.
3. Videos are sorted by published date, then discovered date as fallback.
4. The app shows thumbnails, title, channel name, duration, publish date, and
   watched state.
5. Background refresh may update the list without blocking initial display.

The feed never includes recommendations from unsubscribed channels unless the
user explicitly searches for them.

On first launch, if there are no subscribed channels, the app should open the
Search view instead of the Feed view. With no subscriptions the feed is
necessarily empty, and Search is the most direct path to finding a channel or
video to start from.

### Browse One Channel

1. User selects a subscribed channel.
2. The app shows locally cached videos for that channel.
3. The user can refresh that channel manually.
4. The user can unsubscribe from that channel.

Unsubscribing removes the subscription record but does not have to delete video
or watch-history rows immediately. Keeping history allows old watched state to
remain useful if the user resubscribes.

### Play Video

1. User selects a video.
2. The app loads locally cached metadata immediately.
3. The app resolves a fresh playable stream URL through `yt-dlp`.
4. The playback layer starts the stream.
5. The app records playback start in watch history.
6. The app periodically records watched time ranges for the video.
7. The player shows a subscribe button for the video's channel when the channel
   is not already subscribed.
8. On completion or near-completion, the app marks the video watched.

Playback failures should distinguish extraction errors, network errors, expired
stream URLs, and GTK media errors where practical.

### Search

1. User enters a search query.
2. The app stores the query in local search history.
3. The app asks `yt-dlp` for search results.
4. Results are shown separately from the subscription feed.
5. Playing a search result stores that video in the local database.
6. Subscribing from a search result resolves and stores the result's channel.

Search results should not be bulk-inserted into the main video table unless the
user interacts with them.

### Browse and Search Watch History

1. User opens the watch history view.
2. The app shows videos with local watch activity, sorted by most recently
   watched.
3. The user can search watch history by video title, channel title, and watched
   date text where practical.
4. Results are returned from SQLite without using the network.
5. Selecting a history result opens the player. If resume behavior is enabled,
   the app resumes from the end of the most recent watched range.

Watch-history search is separate from YouTube search. It only searches local
viewing data.

## UI Conventions

Views that present list or grid content must include an empty-state placeholder.
This applies to new features by default, especially feed, search, history,
watch-later, channel, and subscription-management views.

Empty states should distinguish between:

- First-use or truly empty data, such as no subscribed channels or no watch
  history.
- Filtered or searched empty results, such as no channels matching a local
  search or no YouTube results matching a query.

The placeholder should be inside the same scrollable content area as the normal
results so spacing, margins, and navigation remain consistent when content
appears.

UI copy that includes numeric counts must use correct singular and plural
wording. Avoid hard-coded plural nouns or pronouns in count-dependent text; use
the shared pluralization helper for simple English nouns, and adjust related
pronouns such as "it" or "them" when the sentence depends on the count.

## Data Model

Schema versioning is stored in SQLite `PRAGMA user_version`.

For derived summaries such as percent watched, completed status, play counts,
and last watched time, prefer SQLite views or triggers where practical. Python
repositories should read these database-level summaries instead of duplicating
the same calculation in multiple UI or service paths.

### `channels`

Stores subscribed and previously known channels.

```sql
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
```

Notes:

- `id` should be the canonical YouTube channel ID when available.
- `is_subscribed` allows unsubscribe without destroying old history.
- `last_checked_at` and `last_successful_check_at` help refresh scheduling.

### `videos`

Stores known videos.

```sql
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
```

Notes:

- `id` is the YouTube video ID.
- `published_at` can be missing or unreliable for some extractor responses.
- `discovered_at` provides a stable local fallback sort key.

### `watch_history`

Stores one summary row per watched video.

```sql
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
```

### `watch_ranges`

Stores watched time ranges for each video. Ranges are half-open intervals:
`start_seconds` is inclusive and `end_seconds` is exclusive. Rows are an
append-style record of viewing activity, so duplicate or overlapping ranges are
valid and can represent multiple watches of the same segment.

```sql
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
```

Suggested indexes:

```sql
CREATE INDEX idx_watch_ranges_video_id
ON watch_ranges(video_id);

CREATE INDEX idx_watch_ranges_last_watched_at
ON watch_ranges(last_watched_at DESC);
```

Coverage calculations should merge overlapping or adjacent ranges at query time.
Completion should be based on merged range coverage, for example at least 90
percent of the known duration, rather than a single last-watched second. Raw
range rows should be preserved so repeated watches remain visible to future
history or analytics features.

### `watch_progress`

Provides per-video watch summary fields for feeds, watch history, and completed
state decisions.

```sql
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
        ELSE MIN(1.0, CAST(COALESCE(c.covered_seconds, 0) AS REAL) / v.duration_seconds)
    END AS percent_watched
FROM videos v
LEFT JOIN covered c ON c.video_id = v.id;
```

This view intentionally merges ranges only for summary calculations. It does not
delete, rewrite, or deduplicate raw `watch_ranges` rows.

### Watch Summary Triggers

`watch_history` should be maintained with triggers where practical so every
inserted range consistently updates first/last watched timestamps.

```sql
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
        first_watched_at = MIN(watch_history.first_watched_at, NEW.last_watched_at),
        last_watched_at = MAX(watch_history.last_watched_at, NEW.last_watched_at),
        updated_at = NEW.updated_at;
END;
```

`play_count` should not be incremented by the range insert trigger because a
single playback session can write multiple watched ranges. The first version can
increment `play_count` explicitly when playback starts. If more precise session
tracking is needed later, add a `watch_sessions` table and maintain `play_count`
from session insert triggers.

Completion can be refreshed after range inserts by application code using the
`watch_progress` view and the configured completion threshold, or by a trigger
if the completion threshold is stored in SQLite. The first version can keep the
threshold in settings and have the repository issue one explicit update after
inserting a range.

### `search_history`

Stores local search queries so the app can show recent searches.

```sql
CREATE TABLE search_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    searched_at TEXT NOT NULL
);
```

Suggested index:

```sql
CREATE INDEX idx_search_history_searched_at
ON search_history(searched_at DESC);
```

Search history should store the user's query text and timestamp, not every
result returned by YouTube. Results are persisted only when the user plays a
video or subscribes to a channel from a result.

### `refresh_jobs`

Tracks background refresh attempts.

```sql
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
```

This table is optional for a very small first version, but useful once refreshes
become asynchronous and user-visible.

## Local Files

Recommended application data layout:

```text
~/.local/share/gtktube/
  gtktube.sqlite3
  thumbnails/
    channels/
    videos/

~/.cache/gtktube/
  extraction/
  media/

~/.config/gtktube/
  settings.json
```

The exact locations should use platform-appropriate XDG helpers instead of
hard-coded paths.

## Background Refresh

Refreshing subscriptions should be asynchronous and conservative.

Rules:

- Do not block app startup on network refresh.
- Show cached data immediately.
- Refresh stale channels in the background.
- Allow manual refresh for all subscriptions or one channel.
- Limit concurrent `yt-dlp` operations.
- Avoid hammering YouTube when many subscriptions exist.
- Store enough timing information to avoid repeated failed refresh loops.

A simple first policy:

- Refresh a channel at most once every 6 hours automatically.
- Allow manual refresh regardless of the automatic interval.
- Run no more than 2 channel refreshes concurrently.

## Error Handling

Expected error classes:

- Invalid URL or unsupported input.
- Channel resolution failed.
- Video extraction failed.
- No playable format found.
- Network unavailable.
- YouTube rate limiting or bot checks.
- GTK media playback failure.
- SQLite migration or write failure.

The UI should show clear, local errors. It should avoid exposing raw stack traces
unless a debug mode is enabled.

## Privacy Model

GTKTube stores data locally and does not authenticate to Google.

Local data includes:

- Subscribed channels.
- Known videos from those channels.
- Watched videos.
- Watched time ranges.
- Search history.
- Cached thumbnails.

Network requests still go to YouTube and related media/CDN hosts through
`yt-dlp` and the media playback layer. The app is not anonymous and does not
hide traffic from YouTube, the user's ISP, or the network.

## UI Structure

Primary views:

- All subscriptions feed
- Channel list
- Channel detail
- Search
- Player
- Watch history
- Settings

The watch history view should include a search field and filter controls for at
least all videos, partially watched videos, and completed videos.

Suggested main layout:

- Left sidebar for subscriptions and top-level navigation.
- Main content area for feed/search/channel results.
- Player page or split player/details view.

Video rows/cards should show:

- Thumbnail
- Title
- Channel
- Duration
- Published date
- Watched or partially watched state

Player controls should include:

- Play/pause
- Seek bar
- Elapsed and total time
- Volume
- Fullscreen
- Retry extraction/playback
- Subscribe to this channel
- Open original YouTube URL in browser

## Settings

Initial settings:

- Maximum concurrent refreshes
- Automatic refresh interval
- Thumbnail cache size
- Preferred format policy
- Completion threshold
- Resume playback from most recent watched range
- Debug logging

Format policy examples:

- Best quality
- Best up to 1080p
- Best up to 720p
- Audio only, later if desired

## Internal Modules

Possible package structure:

```text
gtktube/
  app.py
  ui/
    main_window.py
    feed_view.py
    channel_view.py
    search_view.py
    player_view.py
  services/
    subscriptions.py
    feed.py
    search.py
    playback.py
    refresh.py
  extractors/
    youtube.py
  db/
    connection.py
    migrations.py
    repositories.py
  media/
    player.py
  cache/
    thumbnails.py
  models.py
```

The `extractors.youtube` module should be the only place that imports `yt_dlp`.
The rest of the app should talk to typed application-level methods such as
`resolve_video`, `resolve_channel`, `fetch_channel_uploads`, and `search`.

## Implementation Phases

### Phase 1: Minimal Playback

- Create GTK4 application shell.
- Add URL entry.
- Resolve a video URL through `yt-dlp`.
- Play it through embedded MPV rendered into GTK.
- Show title and basic metadata.

### Phase 2: SQLite and Watch History

- Add database initialization and migrations.
- Store played videos.
- Track watched ranges and completed state.
- Add local watch history view.
- Add local watch-history search.

### Phase 3: Subscriptions

- Add channel subscription flow.
- Add a subscribe button on the player for the current video's channel.
- Store channels.
- Fetch recent uploads for one subscribed channel.
- Show channel-specific video list.

### Phase 4: Subscription Feed

- Merge videos from all subscribed channels.
- Add background refresh.
- Add thumbnail caching.
- Add watched indicators.

### Phase 5: Search

- Add search UI.
- Store local search history.
- Show `yt-dlp` search results.
- Allow playback from search results.
- Allow subscribing to a result's channel.

### Phase 6: Polish and Reliability

- Add settings.
- Improve media error handling.
- Add retry behavior.
- Add database backup.
- Add packaging.

## Testing Strategy

Unit tests:

- Database migrations.
- `PRAGMA user_version` migration ordering.
- Repository behavior.
- Channel and video upserts.
- Watch-history calculations.
- Watch-range coverage and completion calculations with duplicate ranges.
- Watch-history search queries.
- Search-history inserts and ordering.
- URL normalization.

Integration tests:

- `yt-dlp` adapter with mocked extractor responses.
- Feed queries against a temporary SQLite database.
- Refresh scheduling behavior.

Manual tests:

- Subscribe by channel URL.
- Subscribe by handle URL.
- Subscribe by video URL.
- Refresh subscriptions.
- Play a video.
- Resume partially watched video.
- Search watch history and reopen a result.
- Search and play a result.
- Reopen a recent search from search history.
- Simulate extraction failure.
- Simulate offline mode.

Live YouTube tests should be limited because they are network-dependent and can
be flaky. Most automated tests should mock `yt-dlp` responses.

## Packaging

Possible packaging targets:

- Flatpak for Linux desktop distribution.
- Source install for development.
- Later, distro packaging if useful.

Flatpak will need careful handling for:

- Network access.
- libmpv availability and GTK video rendering.
- Python dependencies.
- `yt-dlp` updates.
- XDG data/cache/config directories.

## Risks

### YouTube Compatibility

YouTube changes frequently. `yt-dlp` can break temporarily, or specific videos
may fail extraction.

Mitigation:

- Keep `yt-dlp` isolated behind an adapter.
- Show actionable extraction errors.
- Make updating `yt-dlp` straightforward.

### Playback Reliability

Some resolved formats may not play cleanly through the chosen media layer, or
may require tuning of MPV and `yt-dlp` format selection.

Mitigation:

- Use direct MPV playback for combined and split audio/video streams.
- Fetch and remux streams before playback when the user chooses fetch mode.
- Keep format selection conservative enough for reliable MPV playback while
  still allowing higher-quality cached streams.

### API-Like Usage Without an API

Channel refresh and search depend on `yt-dlp` extraction behavior rather than a
formal YouTube API contract.

Mitigation:

- Cache aggressively.
- Refresh conservatively.
- Treat missing fields as normal.
- Avoid hard dependencies on fragile metadata.

### Rate Limiting and Bot Checks

Heavy refresh behavior could trigger rate limits or bot checks.

Mitigation:

- Limit refresh concurrency.
- Avoid full-library refreshes on every startup.
- Back off after failures.

## Open Questions

- Should unsubscribing hide old videos only, or eventually delete cached videos
  and thumbnails?
