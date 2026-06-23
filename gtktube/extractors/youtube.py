from __future__ import annotations

import os
import re
import shutil
import urllib.parse
from dataclasses import replace
from typing import Any, Callable

from babel import Locale

from gtktube.models import (
    CaptionTrack,
    Channel,
    PlayableVideo,
    SearchResults,
    Video,
    VideoChapter,
)


class ExtractorError(RuntimeError):
    pass


class RestrictedVideoError(ExtractorError):
    pass


class QuietYtdlpLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def clean_ytdlp_error_message(message: str) -> str:
    message = ANSI_ESCAPE_RE.sub("", message)
    message = message.replace("\r", " ").replace("\n", " ")
    message = re.sub(r"\s+", " ", message).strip()
    message = re.sub(r"^ERROR:\s*", "", message, flags=re.IGNORECASE)
    message = re.sub(r"^\[youtube\]\s+\S+:\s*", "", message)
    return message or "Could not load video"


def playback_error_message(message: str) -> str:
    cleaned = clean_ytdlp_error_message(message)
    if is_restricted_video_error(cleaned):
        return "This video is members-only or otherwise restricted."
    return cleaned


def is_restricted_video_error(message: str) -> bool:
    text = clean_ytdlp_error_message(message).lower()
    return (
        "available to this channel's members" in text
        or "members-only" in text
        or "join this channel to get access" in text
    )


def is_unavailable_format_error(message: str) -> bool:
    return "requested format is not available" in clean_ytdlp_error_message(message).lower()


DEFAULT_PLAYBACK_FORMAT = (
    "bestvideo[vcodec^=avc1][height<=720]+bestaudio[acodec^=mp4a]/"
    "best[vcodec^=avc1][acodec^=mp4a][height<=720]/"
    "bestvideo[height<=720]+bestaudio/"
    "best[height<=720]/"
    "best[acodec!=none][vcodec!=none]"
)

QUALITY_FORMATS = {
    "360p": (
        "bestvideo[vcodec^=avc1][height<=360]+bestaudio[acodec^=mp4a]/"
        "best[vcodec^=avc1][acodec^=mp4a][height<=360]/"
        "bestvideo[height<=360]+bestaudio/"
        "best[height<=360]/"
        "best[acodec!=none][vcodec!=none][height<=360]"
    ),
    "480p": (
        "bestvideo[vcodec^=avc1][height<=480]+bestaudio[acodec^=mp4a]/"
        "best[vcodec^=avc1][acodec^=mp4a][height<=480]/"
        "bestvideo[height<=480]+bestaudio/"
        "best[height<=480]/"
        "best[acodec!=none][vcodec!=none][height<=480]"
    ),
    "720p": DEFAULT_PLAYBACK_FORMAT,
    "1080p": (
        "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]/"
        "best[vcodec^=avc1][acodec^=mp4a][height<=1080]/"
        "bestvideo[height<=1080]+bestaudio/"
        "best[height<=1080]/"
        "best[acodec!=none][vcodec!=none][height<=1080]"
    ),
    "best": (
        "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        "best[vcodec^=avc1][acodec^=mp4a]/"
        "bestvideo+bestaudio/"
        "best[acodec!=none][vcodec!=none]"
    ),
}


class YoutubeExtractor:
    def __init__(self) -> None:
        self._ydl_cls: type[Any] | None = None

    def _base_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "ignoreconfig": True,
            "logger": QuietYtdlpLogger(),
        }
        js_runtimes = self._available_js_runtimes()
        if js_runtimes:
            options["js_runtimes"] = js_runtimes
        return options

    def _available_js_runtimes(self) -> dict[str, dict[str, str]]:
        for name, binaries in (
            ("deno", ("deno",)),
            ("node", ("node", "nodejs")),
            ("quickjs", ("qjs", "quickjs")),
            ("bun", ("bun",)),
        ):
            for binary in binaries:
                path = shutil.which(binary)
                if path:
                    return {name: {"path": path}}
        return {}

    def supported_browsers(self) -> list[str]:
        try:
            from yt_dlp.cookies import SUPPORTED_BROWSERS
            return sorted(list(SUPPORTED_BROWSERS))
        except (ImportError, AttributeError) as exc:
            raise ExtractorError(f"Could not retrieve supported browsers from yt-dlp: {exc}") from exc

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
    ) -> dict[str, Any] | None:
        options: dict[str, Any] = {
            **self._base_options(),
            "noplaylist": False,
        }
        if ignore_errors:
            options["ignoreerrors"] = True
        if flat:
            options["extract_flat"] = True
        if start is not None:
            options["playliststart"] = start
        if limit is not None:
            options["playlistend"] = limit if start is None else start + limit - 1
        try:
            with self._youtube_dl()(options) as ydl:
                return ydl.extract_info(target, download=False)
        except Exception as exc:
            raise ExtractorError(playback_error_message(str(exc))) from exc

    def resolve_playlist(self, url: str) -> dict[str, Any]:
        start_video_id = playlist_start_video_id(url)
        info = self._extract(playlist_url(url), flat=True, ignore_errors=True)
        title = info.get("title") if isinstance(info, dict) else None
        entries = self._entries(info)
        videos = [
            self._video_from_info(entry)
            for entry in entries
            if entry is not None
        ]
        start_index = 0
        if start_video_id:
            for index, video in enumerate(videos):
                if video.id == start_video_id:
                    start_index = index
                    break
        return {
            "title": title or "Playlist",
            "videos": videos,
            "start_index": start_index,
        }

    def resolve_video(
        self,
        url: str,
        quality: str = "720p",
        cookies_mode: str = "never",
        cookies_browser: str = "firefox",
    ) -> PlayableVideo:
        selected_quality = quality if quality in QUALITY_FORMATS else "720p"
        custom_format = os.environ.get("GTKTUBE_YTDLP_FORMAT")
        options = {
            **self._base_options(),
            "noplaylist": True,
            "format": custom_format or QUALITY_FORMATS[selected_quality],
        }
        
        if cookies_mode == "always" and cookies_browser:
            options["cookiesfrombrowser"] = (cookies_browser,)

        try:
            with self._youtube_dl()(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            if is_restricted_video_error(str(exc)):
                if cookies_mode == "restricted_auto" and cookies_browser:
                    options["cookiesfrombrowser"] = (cookies_browser,)
                    try:
                        with self._youtube_dl()(options) as ydl:
                            info = ydl.extract_info(url, download=False)
                    except Exception as retry_exc:
                        raise RestrictedVideoError(
                            "Video is restricted and cookies failed: "
                            f"{playback_error_message(str(retry_exc))}"
                        ) from retry_exc
                elif cookies_mode == "restricted_prompt" and cookies_browser:
                    raise RestrictedVideoError("Video is members-only or otherwise restricted.") from exc
                else:
                    raise RestrictedVideoError("Video is members-only or otherwise restricted.") from exc
            else:
                if (
                    is_unavailable_format_error(str(exc))
                    and custom_format is None
                ):
                    fallback_options_list = []
                    fallback_options = dict(options)
                    fallback_options.pop("format", None)
                    fallback_options_list.append(fallback_options)
                    if "cookiesfrombrowser" in options:
                        no_cookies_options = dict(options)
                        no_cookies_options.pop("cookiesfrombrowser", None)
                        fallback_options_list.append(no_cookies_options)

                        no_cookies_default_options = dict(no_cookies_options)
                        no_cookies_default_options.pop("format", None)
                        fallback_options_list.append(no_cookies_default_options)

                    retry_error: Exception | None = None
                    for fallback_options in fallback_options_list:
                        try:
                            with self._youtube_dl()(fallback_options) as ydl:
                                info = ydl.extract_info(url, download=False)
                        except Exception as retry_exc:
                            retry_error = retry_exc
                            continue
                        break
                    else:
                        retry_error = retry_error or exc
                        raise ExtractorError(
                            playback_error_message(str(retry_error))
                        ) from retry_error
                else:
                    raise ExtractorError(playback_error_message(str(exc))) from exc

        video = self._video_from_info(info)
        if video.availability is None:
            video = replace(video, availability="public")

        stream_url, audio_url = self._stream_urls(info)
        if not stream_url:
            raise ExtractorError("No playable stream URL found")
        return PlayableVideo(
            video=video,
            stream_url=stream_url,
            quality=selected_quality,
            audio_url=audio_url,
            resolved_quality=self._resolved_quality(info),
            available_stream_qualities=self._available_qualities(
                info,
                require_audio=False,
            ),
            available_fetch_qualities=self._available_qualities(
                info,
                require_audio=False,
            ),
            captions=self._caption_tracks(info),
            chapters=self._chapters(info),
        )

    def video_metadata(
        self,
        url: str,
        cookies_mode: str = "never",
        cookies_browser: str = "firefox",
    ) -> Video:
        options = {
            **self._base_options(),
            "noplaylist": True,
        }
        if cookies_mode in {"always", "restricted_auto"} and cookies_browser:
            options["cookiesfrombrowser"] = (cookies_browser,)
        try:
            with self._youtube_dl()(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise ExtractorError(playback_error_message(str(exc))) from exc
        video = self._video_from_info(info)
        if video.availability is None:
            video = replace(video, availability="public")
        return video

    def download_video(
        self,
        url: str,
        target_dir: os.PathLike[str] | str,
        cookies_mode: str = "never",
        cookies_browser: str = "firefox",
        progress: Callable[[dict[str, Any]], None] | None = None,
        quality: str = "best",
        output_template: str = "%(title).200B [%(id)s].%(ext)s",
    ) -> None:
        target_template = os.fspath(
            os.path.join(
                os.fspath(target_dir),
                output_template,
            )
        )
        selected_quality = quality if quality in QUALITY_FORMATS else "best"
        options = {
            **self._base_options(),
            "skip_download": False,
            "noplaylist": True,
            "format": QUALITY_FORMATS[selected_quality],
            "outtmpl": target_template,
            "merge_output_format": "mp4",
            "continuedl": True,
            "nopart": False,
        }
        if progress is not None:
            options["progress_hooks"] = [progress]
        if cookies_mode in {"always", "restricted_auto"} and cookies_browser:
            options["cookiesfrombrowser"] = (cookies_browser,)
        try:
            with self._youtube_dl()(options) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as exc:
            if is_restricted_video_error(str(exc)):
                raise RestrictedVideoError(
                    "This video is members-only or otherwise restricted."
                ) from exc
            raise ExtractorError(playback_error_message(str(exc))) from exc

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
        entries = self._entries(info)
        videos: list[Video] = []
        for entry in entries:
            if not entry:
                continue
            video = self._video_from_info(entry, fallback_channel=channel)
            videos.append(video)
        return videos

    def channel_playlists(
        self, channel: Channel, limit: int = 30, start: int = 1
    ) -> list[Video]:
        target = channel.url.rstrip("/")
        if not target.endswith("/playlists"):
            target = f"{target}/playlists"
        info = self._extract(
            target,
            flat=True,
            limit=limit,
            start=start,
            ignore_errors=True,
        )
        entries = self._entries(info)
        playlists: list[Video] = []
        for entry in entries:
            if not entry:
                continue
            playlist = self._video_from_info(
                entry,
                fallback_channel=channel,
                kind="playlist",
            )
            playlists.append(playlist)
        return playlists

    def channel_shorts(
        self, channel: Channel, limit: int = 30, start: int = 1
    ) -> list[Video]:
        target = channel.url.rstrip("/")
        if not target.endswith("/shorts"):
            target = f"{target}/shorts"
        info = self._extract(
            target,
            flat=True,
            limit=limit,
            start=start,
            ignore_errors=True,
        )
        entries = self._entries(info)
        shorts: list[Video] = []
        for entry in entries:
            if not entry:
                continue
            short = self._video_from_info(
                entry,
                fallback_channel=channel,
                kind="short",
            )
            shorts.append(short)
        return shorts

    def search_channel_videos(
        self, channel: Channel, query: str, limit: int = 30
    ) -> list[Video]:
        encoded = urllib.parse.quote_plus(query)
        target = f"{channel.url.rstrip('/')}/search?query={encoded}"
        info = self._extract(
            target,
            flat=True,
            limit=limit,
            ignore_errors=True,
        )
        entries = self._entries(info)
        videos: list[Video] = []
        for entry in entries:
            if not entry:
                continue
            videos.append(self._video_from_info(entry, fallback_channel=channel))
        return videos

    def search(self, query: str, limit: int = 20) -> SearchResults:
        return SearchResults(
            videos=self.search_videos(query, limit=limit),
            channels=self.search_channels(query, limit=10),
        )

    def search_videos(self, query: str, limit: int = 20) -> list[Video]:
        info = self._extract(f"ytsearch{limit}:{query}", flat=True, limit=limit)
        entries = self._entries(info)
        return [self._video_from_info(entry) for entry in entries if entry]

    def search_channels(self, query: str, limit: int = 10) -> list[Channel]:
        encoded = urllib.parse.quote_plus(query)
        url = (
            "https://www.youtube.com/results?"
            f"search_query={encoded}&sp=EgIQAg%253D%253D"
        )
        info = self._extract(url, flat=True, limit=limit)
        entries = self._entries(info)
        channels: list[Channel] = []
        for entry in entries:
            if not entry:
                continue
            try:
                channels.append(self._channel_from_info(entry))
            except ExtractorError:
                continue
        return channels

    def recommended_videos(self, cookies_browser: str, limit: int = 100) -> list[Video]:
        options = {
            **self._base_options(),
            "extract_flat": "in_playlist",
            "cookiesfrombrowser": (cookies_browser,),
            "playlistend": limit,
        }
        try:
            with self._youtube_dl()(options) as ydl:
                info = ydl.extract_info(":ytrec", download=False)
                entries = self._entries(info)
                return [self._video_from_info(entry) for entry in entries if entry]
        except Exception as exc:
            raise ExtractorError(playback_error_message(str(exc))) from exc

    def subscription_channels(
        self,
        cookies_browser: str,
        limit: int = 500,
    ) -> list[Channel]:
        if not cookies_browser:
            raise ExtractorError("YouTube channel import requires browser cookies")
        options = {
            **self._base_options(),
            "extract_flat": "in_playlist",
            "cookiesfrombrowser": (cookies_browser,),
            "playlistend": limit,
        }
        try:
            with self._youtube_dl()(options) as ydl:
                info = ydl.extract_info(
                    "https://www.youtube.com/feed/channels",
                    download=False,
                )
        except Exception as exc:
            raise ExtractorError(playback_error_message(str(exc))) from exc

        channels: list[Channel] = []
        seen: set[str] = set()
        for entry in self._entries(info):
            if not entry:
                continue
            try:
                channel = self._channel_from_info(entry)
            except ExtractorError:
                continue
            if channel.id in seen:
                continue
            seen.add(channel.id)
            channels.append(channel)
        return channels

    def watch_history(self, cookies_browser: str, limit: int = 100) -> list[Video]:
        if not cookies_browser:
            raise ExtractorError("YouTube watch history import requires browser cookies")
        options = {
            **self._base_options(),
            "extract_flat": "in_playlist",
            "cookiesfrombrowser": (cookies_browser,),
            "playlistend": limit,
        }
        try:
            with self._youtube_dl()(options) as ydl:
                info = ydl.extract_info(":ythistory", download=False)
                entries = self._entries(info)
                return [self._video_from_info(entry) for entry in entries if entry]
        except Exception as exc:
            raise ExtractorError(playback_error_message(str(exc))) from exc

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
        self,
        info: dict[str, Any],
        fallback_channel: Channel | None = None,
        kind: str = "video",
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
            kind=kind,
            channel_id=str(channel_id) if channel_id else None,
            channel_title=str(channel_title) if channel_title else None,
            thumbnail_url=self._best_thumbnail(info),
            description=info.get("description"),
            duration_seconds=self._optional_int(info.get("duration")),
            published_at=self._published_at(info),
            view_count=self._optional_int(info.get("view_count")),
            availability=self._availability(info),
        )

    def _availability(self, info: dict[str, Any]) -> str | None:
        availability = info.get("availability")
        if availability is None:
            return None
        availability = str(availability)
        if availability in {
            "private",
            "premium_only",
            "subscriber_only",
            "needs_auth",
            "unlisted",
            "public",
        }:
            return availability
        return None

    def _best_thumbnail(self, info: dict[str, Any]) -> str | None:
        thumbnails = info.get("thumbnails") or []
        if thumbnails:
            # Sort by width or height if available to get the best quality
            def score(t: dict[str, Any]) -> int:
                return (t.get("width") or 0) * (t.get("height") or 0)
            
            sorted_thumbnails = sorted(thumbnails, key=score, reverse=True)
            for t in sorted_thumbnails:
                url = t.get("url")
                if url and ".webp" not in str(url).lower():
                    return self._absolute_url(str(url))
            
            # Fallback to the first one regardless of format
            if thumbnails:
                url = thumbnails[-1].get("url")
                if url:
                    return self._absolute_url(str(url))

        thumbnail = info.get("thumbnail")
        return self._absolute_url(str(thumbnail)) if thumbnail else None

    def playlist_thumbnail(self, url: str) -> str | None:
        try:
            info = self._extract(
                playlist_url(url), flat=True, limit=1, ignore_errors=True
            )
        except ExtractorError:
            return None
        if not isinstance(info, dict):
            return None
        thumbnail_url = self._best_thumbnail(info)
        if thumbnail_url:
            return thumbnail_url
        entries = self._entries(info)
        for entry in entries:
            if entry:
                return self._best_thumbnail(entry)
        return None

    def _entries(self, info: object) -> list[dict[str, Any]]:
        if not isinstance(info, dict):
            return []
        entries = info.get("entries") or []
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

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

    def _available_qualities(
        self,
        info: dict[str, Any],
        *,
        require_audio: bool,
    ) -> list[str]:
        heights: set[int] = set()
        for item in info.get("formats") or []:
            if not isinstance(item, dict):
                continue
            vcodec = item.get("vcodec")
            if not vcodec or vcodec == "none":
                continue
            if require_audio:
                acodec = item.get("acodec")
                if not acodec or acodec == "none":
                    continue
            height = self._optional_int(item.get("height"))
            if height:
                heights.add(height)
        qualities = [
            quality
            for quality in QUALITY_FORMATS
            if quality != "best"
            and (height := self._quality_height(quality)) is not None
            and height in heights
        ]
        return qualities

    def _quality_height(self, quality: str) -> int | None:
        if not quality.endswith("p"):
            return None
        return self._optional_int(quality[:-1])

    def _caption_tracks(self, info: dict[str, Any]) -> list[CaptionTrack]:
        tracks: list[CaptionTrack] = []
        seen_urls: set[str] = set()
        for source_key, automatic in (
            ("subtitles", False),
            ("automatic_captions", True),
        ):
            subtitles = info.get(source_key) or {}
            if not isinstance(subtitles, dict):
                continue
            for language, formats in subtitles.items():
                if not isinstance(formats, list):
                    continue
                formats = self._caption_formats_without_translation(formats)
                item = self._best_caption_format(formats)
                if item is None:
                    continue
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(str(url))
                language_name = self._caption_language_name(str(language))
                suffix = " (auto)" if automatic else ""
                tracks.append(
                    CaptionTrack(
                        id=f"{source_key}:{language}",
                        label=f"{language_name}{suffix}",
                        language=str(language),
                        url=str(url),
                        automatic=automatic,
                    )
                )
        return tracks

    def _caption_language_name(self, language: str) -> str:
        try:
            display_name = Locale.parse(language, sep="-").get_display_name("en")
        except Exception:
            return language
        if not display_name:
            return language
        return display_name.title()

    def _caption_formats_without_translation(
        self, formats: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in formats
            if not self._caption_url_is_translation(str(item.get("url") or ""))
        ]

    def _caption_url_is_translation(self, url: str) -> bool:
        if not url:
            return False
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return "tlang" in query

    def _best_caption_format(self, formats: list[dict[str, Any]]) -> dict[str, Any] | None:
        priorities = ("vtt", "webvtt", "srv3", "ttml")
        for extension in priorities:
            for item in formats:
                if item.get("url") and item.get("ext") == extension:
                    return item
        for item in formats:
            if item.get("url"):
                return item
        return None

    def _chapters(self, info: dict[str, Any]) -> list[VideoChapter]:
        video_id = str(info.get("id") or "")
        raw_chapters = info.get("chapters") or []
        if not video_id or not isinstance(raw_chapters, list):
            return []

        chapters: list[VideoChapter] = []
        for position, item in enumerate(raw_chapters):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            start = self._optional_float(item.get("start_time"))
            end = self._optional_float(item.get("end_time"))
            if not title or start is None or start < 0:
                continue
            if end is not None and end <= start:
                end = None
            chapters.append(
                VideoChapter(
                    video_id=video_id,
                    title=title,
                    start_seconds=start,
                    end_seconds=end,
                    position=len(chapters),
                )
            )
        return chapters

    def _optional_float(self, value: object) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

def is_playlist_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    return "list" in params


def playlist_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    playlist_ids = params.get("list")
    if not playlist_ids or not playlist_ids[0]:
        return url
    return urllib.parse.urlunparse(
        (
            parsed.scheme or "https",
            parsed.netloc or "www.youtube.com",
            "/playlist",
            "",
            urllib.parse.urlencode({"list": playlist_ids[0]}),
            "",
        )
    )


def playlist_start_video_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    video_ids = params.get("v")
    if video_ids and video_ids[0]:
        return video_ids[0]
    if parsed.netloc.lower().endswith("youtu.be"):
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts:
            return path_parts[0]
    return None
