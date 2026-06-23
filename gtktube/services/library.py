from __future__ import annotations

import random
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import as_completed
from dataclasses import replace
from pathlib import Path

from gtktube.db.repositories import LibraryRepository
from gtktube.daemon_executor import DaemonThreadPoolExecutor
from gtktube.extractors.youtube import (
    ExtractorError,
    QUALITY_FORMATS,
    RestrictedVideoError,
    YoutubeExtractor,
)
from gtktube.models import Channel, PlayableVideo, SearchResults, Video


PLAYBACK_CACHE_MAX_AGE_SECONDS = 3 * 24 * 60 * 60
PLAYBACK_CACHE_PART_MAX_AGE_SECONDS = 6 * 60 * 60
PLAYBACK_CACHE_MAX_BYTES = 10 * 1024 * 1024 * 1024


class LibraryService:
    def __init__(self, repository: LibraryRepository, extractor: YoutubeExtractor):
        self.repository = repository
        self.extractor = extractor

    def play_url(
        self, url: str, quality: str = "720p", record_play: bool = True
    ) -> PlayableVideo:
        playable = self.extractor.resolve_video(
            url,
            quality=quality,
            cookies_mode=self.repository.yt_dlp_cookies_mode(),
            cookies_browser=self.repository.yt_dlp_cookies_browser(),
        )
        self._store_video_and_channel(playable.video)
        self.repository.replace_video_chapters(
            playable.video.id, playable.chapters or []
        )
        if record_play:
            self.repository.record_play_started(playable.video.id)
        return playable

    def play_video(
        self,
        video: Video,
        quality: str = "720p",
        record_play: bool = True,
        playlist_url: str | None = None,
    ) -> PlayableVideo:
        try:
            playable = self.extractor.resolve_video(
                video.url,
                quality=quality,
                cookies_mode=self.repository.yt_dlp_cookies_mode(),
                cookies_browser=self.repository.yt_dlp_cookies_browser(),
            )
        except RestrictedVideoError:
            self.repository.set_video_availability(video.id, "subscriber_only")
            raise
        self._store_video_and_channel(playable.video)
        self.repository.replace_video_chapters(
            playable.video.id, playable.chapters or []
        )
        if record_play:
            self.repository.record_play_started(playable.video.id, playlist_url)
        return playable

    def record_watch_range(self, video_id: str, start_seconds: int, end_seconds: int) -> None:
        self.repository.add_watch_range(video_id, start_seconds, end_seconds)

    def subscribe(self, url: str) -> Channel:
        channel = self.extractor.resolve_channel(url)
        self.repository.upsert_channel(channel, subscribed=True)
        return channel

    def subscribe_with_initial_videos(
        self, url: str, limit: int = 30
    ) -> Channel:
        channel = self.subscribe(url)
        self.refresh_channel(
            channel,
            limit=limit,
            refresh_metadata=False,
            clear_new_indicator=True,
        )
        return self.repository.channel(channel.id) or channel

    def youtube_subscription_channels(
        self,
        browser: str,
        limit: int = 500,
    ) -> list[Channel]:
        if not browser:
            raise ExtractorError("YouTube channel import requires browser cookies")
        return self.extractor.subscription_channels(browser, limit=limit)

    def import_subscription_channels(self, channels: list[Channel]) -> int:
        for channel in channels:
            self.repository.upsert_channel(channel, subscribed=True)
        return len(channels)

    def subscribe_to_video_channel(self, video: Video) -> Channel:
        if video.channel_id and video.channel_title:
            channel = Channel(
                id=video.channel_id,
                title=video.channel_title,
                url=f"https://www.youtube.com/channel/{video.channel_id}",
                thumbnail_url=None,
            )
            self.repository.upsert_channel(channel, subscribed=True)
            return channel
        return self.subscribe(video.url)

    def unsubscribe_from_video_channel(self, video: Video) -> None:
        if video.channel_id:
            self.repository.unsubscribe_channel(video.channel_id)

    def unsubscribe_channel(self, channel: Channel) -> None:
        self.repository.unsubscribe_channel(channel.id)

    def add_watch_later(self, video: Video) -> None:
        self._store_video_and_channel(video)
        self.repository.add_watch_later(video.id)

    def remove_watch_later(self, video: Video) -> None:
        self.repository.remove_watch_later(video.id)

    def watch_later_videos(self, limit: int = 100) -> list[Video]:
        return self.repository.watch_later_videos(limit=limit)

    def is_watch_later(self, video: Video) -> bool:
        return self.repository.is_watch_later(video.id)

    def open_channel_url(self, url: str) -> Channel:
        channel = self.extractor.resolve_channel(url)
        self.repository.upsert_channel(
            channel,
            subscribed=self.repository.is_subscribed(channel.id),
        )
        return self.repository.channel(channel.id) or channel

    def resolve_playlist_url(self, url: str) -> dict[str, object]:
        result = self.extractor.resolve_playlist(url)
        return {"title": result["title"], "videos": result["videos"]}

    def refresh_channel(
        self,
        channel: Channel,
        limit: int = 30,
        start: int = 1,
        refresh_metadata: bool = True,
        clear_new_indicator: bool = False,
    ) -> list[Video]:
        was_subscribed = self.repository.is_subscribed(channel.id)
        if refresh_metadata:
            try:
                channel = self.extractor.resolve_channel(channel.url)
                self.repository.upsert_channel(channel, subscribed=was_subscribed)
            except ExtractorError:
                pass
        videos = self.extractor.channel_uploads(channel, limit=limit, start=start)
        self.repository.upsert_videos(videos)
        if start == 1:
            shorts = self.extractor.channel_shorts(channel, limit=limit, start=start)
            playlists = self.extractor.channel_playlists(channel, limit=limit, start=start)
            self.repository.upsert_videos(shorts)
            self.repository.upsert_videos(playlists)
        if clear_new_indicator:
            self.repository.clear_new_video_indicator(channel.id)
        self.repository.mark_channel_refresh(channel.id, success=True)
        return videos

    def channel_playlists(
        self, channel: Channel, limit: int = 30, start: int = 1
    ) -> list[Video]:
        playlists = self.extractor.channel_playlists(channel, limit=limit, start=start)
        self.repository.upsert_videos(playlists)
        return playlists

    def channel_shorts(
        self, channel: Channel, limit: int = 30, start: int = 1
    ) -> list[Video]:
        videos = self.extractor.channel_shorts(channel, limit=limit, start=start)
        self.repository.upsert_videos(videos)
        return videos

    def search_channel(
        self, channel: Channel, query: str, limit: int = 30
    ) -> list[Video]:
        videos = self.extractor.search_channel_videos(channel, query, limit=limit)
        self.repository.upsert_videos(videos)
        return videos

    def playlist_thumbnail(self, url: str) -> str | None:
        return self.extractor.playlist_thumbnail(url)

    def refresh_subscriptions(
        self,
        limit_per_channel: int = 30,
        max_workers: int | None = None,
        progress: Callable[[Channel, str], None] | None = None,
    ) -> None:
        channels = self.repository.subscribed_channels()
        if not channels:
            return
        channels = [
            channel
            for channel in channels
            if self.repository.channel_needs_refresh(channel.id)
        ]
        if not channels:
            return
        random.shuffle(channels)
        worker_count = min(
            len(channels),
            max(1, max_workers or self.repository.refresh_worker_count()),
        )

        def refresh(channel: Channel) -> None:
            if progress is not None:
                progress(channel, "start")
            try:
                is_initial_import = not self.repository.channel_videos(channel.id, 1)
                self.refresh_channel(
                    channel,
                    limit=limit_per_channel,
                    clear_new_indicator=is_initial_import,
                )
                if progress is not None:
                    progress(channel, "updated")
            except Exception:
                self.repository.mark_channel_refresh(channel.id, success=False)
                if progress is not None:
                    progress(channel, "failed")
                raise
            finally:
                if progress is not None:
                    progress(channel, "finish")

        errors: list[BaseException] = []
        with DaemonThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(refresh, channel) for channel in channels]
            for future in as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]

    def search(self, query: str, limit: int = 20) -> SearchResults:
        self.repository.add_search_history(query)
        results = self.extractor.search(query, limit=limit)
        channels: list[Channel] = []
        for channel in results.channels:
            if not channel.thumbnail_url:
                try:
                    channel = self.extractor.resolve_channel(channel.url)
                except ExtractorError:
                    pass
            self.repository.upsert_channel(channel, subscribed=False)
            channels.append(self.repository.channel(channel.id) or channel)
        return SearchResults(videos=results.videos, channels=channels)

    def _store_video_and_channel(self, video: Video) -> None:
        if video.channel_id and video.channel_title:
            self.repository.upsert_channel(
                Channel(
                    id=video.channel_id,
                    title=video.channel_title,
                    url=f"https://www.youtube.com/channel/{video.channel_id}",
                    thumbnail_url=None,
                ),
                subscribed=self.repository.is_subscribed(video.channel_id),
            )
        self.repository.upsert_video(video)

    def recommended_videos(self, limit: int = 100) -> list[Video]:
        browser = self.repository.yt_dlp_cookies_browser()
        videos = self.extractor.recommended_videos(browser, limit=limit)
        return self.repository.videos_with_watch_progress(videos)

    def refresh_video_metadata(self, video: Video) -> Video:
        refreshed = self.extractor.video_metadata(
            video.url,
            cookies_mode=self.repository.yt_dlp_cookies_mode(),
            cookies_browser=self.repository.yt_dlp_cookies_browser(),
        )
        merged = replace(
            video,
            channel_id=refreshed.channel_id or video.channel_id,
            channel_title=refreshed.channel_title or video.channel_title,
            thumbnail_url=refreshed.thumbnail_url or video.thumbnail_url,
            description=refreshed.description or video.description,
            duration_seconds=refreshed.duration_seconds or video.duration_seconds,
            published_at=refreshed.published_at or video.published_at,
            view_count=(
                refreshed.view_count
                if refreshed.view_count is not None
                else video.view_count
            ),
            availability=refreshed.availability or video.availability,
        )
        self._store_video_and_channel(merged)
        return self.repository.videos_with_watch_progress([merged])[0]

    def downloaded_files(self, target_dir: Path) -> list[Path]:
        if not target_dir.exists():
            return []
        files = [
            path
            for path in target_dir.iterdir()
            if path.is_file()
            and not path.name.endswith((".part", ".ytdl", ".temp", ".tmp"))
        ]
        return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)

    def downloaded_file_for_video(self, target_dir: Path, video_id: str) -> Path | None:
        needle = f"[{video_id}]"
        for path in self.downloaded_files(target_dir):
            if needle in path.name:
                return path
        return None

    def downloaded_videos(self, target_dir: Path) -> list[tuple[Video, Path]]:
        downloads: list[tuple[Video, Path]] = []
        for path in self.downloaded_files(target_dir):
            video_id = self.downloaded_video_id(path)
            if video_id is None:
                continue
            video = self.repository.video(video_id)
            if video is None:
                video = Video(
                    id=video_id,
                    title=self.downloaded_video_title(path, video_id),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                )
            downloads.append((video, path))
        return downloads

    def downloaded_video_id(self, path: Path) -> str | None:
        match = re.search(r"\[([A-Za-z0-9_-]{6,})\](?:\.[^.]+)?$", path.name)
        return match.group(1) if match else None

    def downloaded_video_title(self, path: Path, video_id: str) -> str:
        title = re.sub(rf"\s*\[{re.escape(video_id)}\](?:\.[^.]+)?$", "", path.name)
        return title.strip() or path.stem

    def download_video(
        self,
        video: Video,
        target_dir: Path,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> Path:
        self._store_video_and_channel(video)
        target_dir.mkdir(parents=True, exist_ok=True)
        existing = self.downloaded_file_for_video(target_dir, video.id)
        if existing is not None:
            return existing
        before = {path.resolve() for path in self.downloaded_files(target_dir)}
        self.extractor.download_video(
            video.url,
            target_dir,
            cookies_mode=self.repository.yt_dlp_cookies_mode(),
            cookies_browser=self.repository.yt_dlp_cookies_browser(),
            progress=progress,
        )
        files = self.downloaded_files(target_dir)
        for path in files:
            if path.resolve() not in before and f"[{video.id}]" in path.name:
                return path
        raise ExtractorError("Download finished but no output file was found")

    def playback_cache_file_for_video(
        self,
        target_dir: Path,
        video_id: str,
        quality: str,
    ) -> Path | None:
        needle = f"[{video_id}] [{quality}]"
        for path in self.downloaded_files(target_dir):
            if needle in path.name:
                self.touch_file(path)
                return path
        return None

    def fetch_playback_video(
        self,
        video: Video,
        quality: str,
        target_dir: Path,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> Path:
        self._store_video_and_channel(video)
        target_dir.mkdir(parents=True, exist_ok=True)
        self.prune_playback_cache(target_dir)
        selected_quality = quality if quality in QUALITY_FORMATS else "720p"
        existing = self.playback_cache_file_for_video(
            target_dir,
            video.id,
            selected_quality,
        )
        if existing is not None:
            return existing
        before = {path.resolve() for path in self.downloaded_files(target_dir)}
        self.extractor.download_video(
            video.url,
            target_dir,
            cookies_mode=self.repository.yt_dlp_cookies_mode(),
            cookies_browser=self.repository.yt_dlp_cookies_browser(),
            progress=progress,
            quality=selected_quality,
            output_template=f"%(title).200B [%(id)s] [{selected_quality}].%(ext)s",
        )
        for path in self.downloaded_files(target_dir):
            if (
                path.resolve() not in before
                and f"[{video.id}] [{selected_quality}]" in path.name
            ):
                self.touch_file(path)
                self.prune_playback_cache(target_dir)
                return path
        raise ExtractorError("Fetch finished but no output file was found")

    def play_cached_video(
        self,
        video: Video,
        path: Path,
        quality: str,
        record_play: bool = True,
        playlist_url: str | None = None,
    ) -> PlayableVideo:
        self.touch_file(path)
        self._store_video_and_channel(video)
        if record_play:
            self.repository.record_play_started(video.id, playlist_url)
        return PlayableVideo(
            video=video,
            stream_url=str(path),
            quality=quality,
            resolved_quality=f"cached {quality}",
            chapters=self.repository.video_chapters(video.id),
        )

    def prune_playback_cache(self, target_dir: Path) -> None:
        if not target_dir.exists():
            return
        now = time.time()
        files: list[Path] = []
        for path in target_dir.iterdir():
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            is_partial = path.name.endswith((".part", ".ytdl", ".temp", ".tmp"))
            max_age = (
                PLAYBACK_CACHE_PART_MAX_AGE_SECONDS
                if is_partial
                else PLAYBACK_CACHE_MAX_AGE_SECONDS
            )
            if now - stat.st_mtime > max_age:
                self.unlink_quietly(path)
                continue
            if not is_partial:
                files.append(path)
        files.sort(key=lambda path: path.stat().st_mtime)
        total_size = sum(path.stat().st_size for path in files if path.exists())
        while total_size > PLAYBACK_CACHE_MAX_BYTES and files:
            path = files.pop(0)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            self.unlink_quietly(path)
            total_size -= size

    def touch_file(self, path: Path) -> None:
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass

    def unlink_quietly(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def play_downloaded_video(
        self,
        video: Video,
        path: Path,
        record_play: bool = True,
        playlist_url: str | None = None,
    ) -> PlayableVideo:
        self._store_video_and_channel(video)
        if record_play:
            self.repository.record_play_started(video.id, playlist_url)
        quality = self.downloaded_video_quality(path)
        return PlayableVideo(
            video=video,
            stream_url=str(path),
            quality=quality,
            resolved_quality="downloaded",
            chapters=self.repository.video_chapters(video.id),
        )

    def downloaded_video_quality(self, path: Path) -> str:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return "local"
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=height",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            height = int(result.stdout.strip().splitlines()[0])
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            return "local"
        return f"{height}p" if height > 0 else "local"

    def import_youtube_watch_history(self, limit: int = 100) -> int:
        browser = self.repository.yt_dlp_cookies_browser()
        if not browser:
            raise ExtractorError("YouTube watch history import requires browser cookies")

        videos = self.extractor.watch_history(browser, limit=limit)
        for video in videos:
            self._store_video_and_channel(video)
            self.repository.mark_played(video.id, video.duration_seconds)

        self.repository.mark_youtube_watch_history_import()
        return len(videos)

    def supported_browsers(self) -> list[str]:
        return self.extractor.supported_browsers()
