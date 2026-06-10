from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    url: str
    channel_id: str | None = None
    channel_title: str | None = None
    thumbnail_url: str | None = None
    duration_seconds: int | None = None
    published_at: str | None = None
    percent_watched: float | None = None
    completed: bool = False


@dataclass(frozen=True)
class Channel:
    id: str
    title: str
    url: str
    handle: str | None = None
    thumbnail_url: str | None = None
    is_subscribed: bool = True


@dataclass(frozen=True)
class PlayableVideo:
    video: Video
    stream_url: str
