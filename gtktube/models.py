from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    url: str
    kind: str = "video"
    channel_id: str | None = None
    channel_title: str | None = None
    thumbnail_url: str | None = None
    description: str | None = None
    duration_seconds: int | None = None
    published_at: str | None = None
    view_count: int | None = None
    availability: str | None = None
    percent_watched: float | None = None
    watch_ranges: list[tuple[int, int]] | None = None
    completed: bool = False
    history_id: int | None = None
    playlist_url: str | None = None


@dataclass(frozen=True)
class Channel:
    id: str
    title: str
    url: str
    handle: str | None = None
    thumbnail_url: str | None = None
    is_subscribed: bool = True


@dataclass(frozen=True)
class SearchResults:
    videos: list[Video]
    channels: list[Channel]


@dataclass(frozen=True)
class CaptionTrack:
    id: str
    label: str
    language: str
    url: str
    automatic: bool = False


@dataclass(frozen=True)
class VideoChapter:
    video_id: str
    title: str
    start_seconds: float
    end_seconds: float | None = None
    position: int = 0


@dataclass(frozen=True)
class PlayableVideo:
    video: Video
    stream_url: str
    quality: str
    audio_url: str | None = None
    resolved_quality: str | None = None
    available_stream_qualities: list[str] | None = None
    available_fetch_qualities: list[str] | None = None
    captions: list[CaptionTrack] | None = None
    chapters: list[VideoChapter] | None = None


@dataclass(frozen=True)
class SponsorBlockSegment:
    video_id: str
    category: str
    start_seconds: float
    end_seconds: float
    action_type: str | None = None
    uuid: str | None = None

    @property
    def key(self) -> str:
        if self.uuid:
            return self.uuid
        return f"{self.category}:{self.start_seconds:.3f}:{self.end_seconds:.3f}"
