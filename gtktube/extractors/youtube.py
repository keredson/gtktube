from __future__ import annotations

import os
from typing import Any

from gtktube.models import Channel, PlayableVideo, Video


class ExtractorError(RuntimeError):
    pass


DEFAULT_PLAYBACK_FORMAT = (
    "best[protocol^=http][ext=mp4][vcodec^=avc1][acodec^=mp4a][height<=720]/"
    "best[protocol^=http][ext=mp4][acodec!=none][vcodec!=none][height<=720]/"
    "best[protocol^=http][acodec!=none][vcodec!=none][height<=480]/"
    "best[acodec!=none][vcodec!=none]"
)


class YoutubeExtractor:
    def __init__(self) -> None:
        self._ydl_cls: type[Any] | None = None

    def _youtube_dl(self) -> type[Any]:
        if self._ydl_cls is None:
            try:
                from yt_dlp import YoutubeDL
            except ModuleNotFoundError as exc:
                raise ExtractorError(
                    "yt-dlp is not installed. Install Python dependencies with "
                    "`python3 -m pip install -r requirements.txt`."
                ) from exc
            self._ydl_cls = YoutubeDL
        return self._ydl_cls

    def _extract(self, target: str, *, flat: bool = False, limit: int | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
        }
        if flat:
            options["extract_flat"] = "in_playlist"
        if limit is not None:
            options["playlistend"] = limit
        try:
            with self._youtube_dl()(options) as ydl:
                return ydl.extract_info(target, download=False)
        except Exception as exc:
            raise ExtractorError(str(exc)) from exc

    def resolve_video(self, url: str) -> PlayableVideo:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "format": os.environ.get("GTKTUBE_YTDLP_FORMAT", DEFAULT_PLAYBACK_FORMAT),
        }
        try:
            with self._youtube_dl()(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise ExtractorError(str(exc)) from exc

        stream_url = info.get("url")
        if not stream_url:
            raise ExtractorError("No playable stream URL found")
        return PlayableVideo(video=self._video_from_info(info), stream_url=stream_url)

    def resolve_channel(self, url: str) -> Channel:
        info = self._extract(url, flat=True, limit=1)

        channel_id = (
            info.get("channel_id")
            or info.get("uploader_id")
            or info.get("id")
        )
        channel_title = (
            info.get("channel")
            or info.get("uploader")
            or info.get("title")
            or channel_id
        )
        channel_url = info.get("channel_url") or info.get("uploader_url") or info.get("webpage_url") or url
        if not channel_id:
            raise ExtractorError("Could not resolve a channel ID")

        return Channel(
            id=str(channel_id),
            title=str(channel_title),
            url=str(channel_url),
            handle=info.get("channel"),
            thumbnail_url=self._best_thumbnail(info),
        )

    def channel_uploads(self, channel: Channel, limit: int = 30) -> list[Video]:
        target = channel.url.rstrip("/")
        if not target.endswith("/videos"):
            target = f"{target}/videos"
        info = self._extract(target, flat=True, limit=limit)
        entries = info.get("entries") or []
        videos: list[Video] = []
        for entry in entries:
            if not entry:
                continue
            video = self._video_from_info(entry, fallback_channel=channel)
            videos.append(video)
        return videos

    def search(self, query: str, limit: int = 20) -> list[Video]:
        info = self._extract(f"ytsearch{limit}:{query}", flat=True, limit=limit)
        entries = info.get("entries") or []
        return [self._video_from_info(entry) for entry in entries if entry]

    def _video_from_info(
        self, info: dict[str, Any], fallback_channel: Channel | None = None
    ) -> Video:
        video_id = info.get("id")
        if not video_id:
            url = info.get("url") or info.get("webpage_url")
            if not url:
                raise ExtractorError("Video result did not include an ID or URL")
            video_id = str(url).rsplit("/", 1)[-1]

        webpage_url = info.get("webpage_url") or info.get("url")
        if webpage_url and not str(webpage_url).startswith("http"):
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        if not webpage_url:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"

        channel_id = info.get("channel_id") or info.get("uploader_id")
        channel_title = info.get("channel") or info.get("uploader")
        if fallback_channel is not None:
            channel_id = channel_id or fallback_channel.id
            channel_title = channel_title or fallback_channel.title

        return Video(
            id=str(video_id),
            title=str(info.get("title") or "Untitled video"),
            url=str(webpage_url),
            channel_id=str(channel_id) if channel_id else None,
            channel_title=str(channel_title) if channel_title else None,
            thumbnail_url=self._best_thumbnail(info),
            duration_seconds=self._optional_int(info.get("duration")),
            published_at=self._published_at(info),
        )

    def _best_thumbnail(self, info: dict[str, Any]) -> str | None:
        thumbnails = info.get("thumbnails") or []
        if thumbnails:
            return thumbnails[-1].get("url")
        thumbnail = info.get("thumbnail")
        return str(thumbnail) if thumbnail else None

    def _published_at(self, info: dict[str, Any]) -> str | None:
        timestamp = info.get("timestamp")
        if isinstance(timestamp, int):
            from datetime import UTC, datetime

            return datetime.fromtimestamp(timestamp, UTC).date().isoformat()
        upload_date = info.get("upload_date")
        if isinstance(upload_date, str) and len(upload_date) == 8:
            return f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        return None

    def _optional_int(self, value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
