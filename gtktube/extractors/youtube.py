from __future__ import annotations

import os
import urllib.parse
from typing import Any

from gtktube.models import Channel, PlayableVideo, SearchResults, Video


class ExtractorError(RuntimeError):
    pass


def is_restricted_video_error(message: str) -> bool:
    text = message.lower()
    return (
        "available to this channel's members" in text
        or "members-only" in text
        or "join this channel to get access" in text
    )


DEFAULT_PLAYBACK_FORMAT = (
    "bestvideo[height<=720]+bestaudio/"
    "best[height<=720]/"
    "best[acodec!=none][vcodec!=none]"
)

QUALITY_FORMATS = {
    "360p": (
        "bestvideo[height<=360]+bestaudio/"
        "best[height<=360]/"
        "best[acodec!=none][vcodec!=none][height<=360]"
    ),
    "480p": (
        "bestvideo[height<=480]+bestaudio/"
        "best[height<=480]/"
        "best[acodec!=none][vcodec!=none][height<=480]"
    ),
    "720p": DEFAULT_PLAYBACK_FORMAT,
    "1080p": (
        "bestvideo[height<=1080]+bestaudio/"
        "best[height<=1080]/"
        "best[acodec!=none][vcodec!=none][height<=1080]"
    ),
    "best": (
        "bestvideo+bestaudio/"
        "best[acodec!=none][vcodec!=none]"
    ),
}


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

    def _extract(
        self,
        target: str,
        *,
        flat: bool = False,
        limit: int | None = None,
        start: int | None = None,
        ignore_errors: bool = False,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
        }
        if ignore_errors:
            options["ignoreerrors"] = True
        if flat:
            options["extract_flat"] = "in_playlist"
        if start is not None:
            options["playliststart"] = start
        if limit is not None:
            options["playlistend"] = limit if start is None else start + limit - 1
        try:
            with self._youtube_dl()(options) as ydl:
                return ydl.extract_info(target, download=False)
        except Exception as exc:
            raise ExtractorError(str(exc)) from exc

    def resolve_video(self, url: str, quality: str = "720p") -> PlayableVideo:
        selected_quality = quality if quality in QUALITY_FORMATS else "720p"
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "format": os.environ.get(
                "GTKTUBE_YTDLP_FORMAT",
                QUALITY_FORMATS[selected_quality],
            ),
        }
        try:
            with self._youtube_dl()(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            if is_restricted_video_error(str(exc)):
                raise ExtractorError("Video is members-only or otherwise restricted.") from exc
            raise ExtractorError(str(exc)) from exc

        stream_url, audio_url = self._stream_urls(info)
        if not stream_url:
            raise ExtractorError("No playable stream URL found")
        return PlayableVideo(
            video=self._video_from_info(info),
            stream_url=stream_url,
            quality=selected_quality,
            audio_url=audio_url,
            resolved_quality=self._resolved_quality(info),
        )

    def resolve_channel(self, url: str) -> Channel:
        info = self._extract(url, flat=True, limit=1)
        thumbnail_url = self._best_thumbnail(info)

        if not thumbnail_url:
            try:
                detailed_info = self._extract(url, flat=False, limit=1)
            except ExtractorError:
                detailed_info = {}
            thumbnail_url = self._best_thumbnail(detailed_info)
            info = {**detailed_info, **info}

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
            thumbnail_url=thumbnail_url,
        )

    def channel_uploads(
        self, channel: Channel, limit: int = 30, start: int = 1
    ) -> list[Video]:
        target = channel.url.rstrip("/")
        if not target.endswith("/videos"):
            target = f"{target}/videos"
        info = self._extract(
            target,
            flat=True,
            limit=limit,
            start=start,
            ignore_errors=True,
        )
        entries = info.get("entries") or []
        videos: list[Video] = []
        for entry in entries:
            if not entry:
                continue
            video = self._video_from_info(entry, fallback_channel=channel)
            videos.append(video)
        return videos

    def search(self, query: str, limit: int = 20) -> SearchResults:
        return SearchResults(
            videos=self.search_videos(query, limit=limit),
            channels=self.search_channels(query, limit=10),
        )

    def search_videos(self, query: str, limit: int = 20) -> list[Video]:
        info = self._extract(f"ytsearch{limit}:{query}", flat=True, limit=limit)
        entries = info.get("entries") or []
        return [self._video_from_info(entry) for entry in entries if entry]

    def search_channels(self, query: str, limit: int = 10) -> list[Channel]:
        encoded = urllib.parse.quote_plus(query)
        url = (
            "https://www.youtube.com/results?"
            f"search_query={encoded}&sp=EgIQAg%253D%253D"
        )
        info = self._extract(url, flat=True, limit=limit)
        entries = info.get("entries") or []
        channels: list[Channel] = []
        for entry in entries:
            if not entry:
                continue
            try:
                channels.append(self._channel_from_info(entry))
            except ExtractorError:
                continue
        return channels

    def _channel_from_info(self, info: dict[str, Any]) -> Channel:
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
        channel_url = (
            info.get("channel_url")
            or info.get("uploader_url")
            or info.get("url")
            or info.get("webpage_url")
        )
        if channel_url and not str(channel_url).startswith("http"):
            channel_url = f"https://www.youtube.com/channel/{channel_id}"
        if not channel_id or not channel_url:
            raise ExtractorError("Could not resolve a channel result")
        thumbnail_url = self._best_thumbnail(info)
        if not thumbnail_url:
            try:
                return self.resolve_channel(str(channel_url))
            except ExtractorError:
                pass
        return Channel(
            id=str(channel_id),
            title=str(channel_title),
            url=str(channel_url),
            handle=info.get("uploader_id") or info.get("channel"),
            thumbnail_url=thumbnail_url,
            is_subscribed=False,
        )

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
            description=info.get("description"),
            duration_seconds=self._optional_int(info.get("duration")),
            published_at=self._published_at(info),
            view_count=self._optional_int(info.get("view_count")),
        )

    def _best_thumbnail(self, info: dict[str, Any]) -> str | None:
        thumbnails = info.get("thumbnails") or []
        if thumbnails:
            urls = [thumbnail.get("url") for thumbnail in thumbnails]
            for url in reversed(urls):
                if url and ".webp" not in str(url).lower():
                    return self._absolute_url(str(url))
            if urls[-1]:
                return self._absolute_url(str(urls[-1]))
        thumbnail = info.get("thumbnail")
        return self._absolute_url(str(thumbnail)) if thumbnail else None

    def _absolute_url(self, url: str) -> str:
        if url.startswith("//"):
            return f"https:{url}"
        return url

    def _published_at(self, info: dict[str, Any]) -> str | None:
        timestamp = info.get("timestamp") or info.get("release_timestamp")
        if isinstance(timestamp, (int, float)):
            from datetime import UTC, datetime

            return datetime.fromtimestamp(timestamp, UTC).date().isoformat()
        for key in ("upload_date", "release_date", "modified_date"):
            date = info.get(key)
            if isinstance(date, str) and len(date) == 8:
                return f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
        timestamp_text = info.get("timestamp")
        if isinstance(timestamp_text, str) and len(timestamp_text) >= 10:
            return timestamp_text[:10]
        return None

    def _optional_int(self, value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _stream_urls(self, info: dict[str, Any]) -> tuple[str | None, str | None]:
        requested = info.get("requested_downloads") or info.get("requested_formats") or []
        if requested:
            video_url: str | None = None
            audio_url: str | None = None
            for item in requested:
                url = item.get("url")
                if not url:
                    continue
                vcodec = item.get("vcodec")
                acodec = item.get("acodec")
                if vcodec and vcodec != "none":
                    video_url = str(url)
                elif acodec and acodec != "none":
                    audio_url = str(url)
            if video_url:
                return video_url, audio_url
        url = info.get("url")
        return str(url) if url else None, None

    def _resolved_quality(self, info: dict[str, Any]) -> str | None:
        height = self._optional_int(info.get("height"))
        if height:
            return f"{height}p"
        requested = info.get("requested_downloads") or info.get("requested_formats") or []
        heights = [
            self._optional_int(item.get("height"))
            for item in requested
            if item.get("vcodec") and item.get("vcodec") != "none"
        ]
        heights = [height for height in heights if height]
        if heights:
            return f"{max(heights)}p"
        format_note = info.get("format_note")
        if format_note:
            return str(format_note)
        format_id = info.get("format_id")
        return str(format_id) if format_id else None
