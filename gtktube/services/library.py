from __future__ import annotations

import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from gtktube.db.repositories import LibraryRepository
from gtktube.extractors.youtube import ExtractorError, YoutubeExtractor
from gtktube.models import Channel, PlayableVideo, SearchResults, Video


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
        if record_play:
            self.repository.record_play_started(playable.video.id)
        return playable

    def play_video(
        self, video: Video, quality: str = "720p", record_play: bool = True
    ) -> PlayableVideo:
        playable = self.extractor.resolve_video(
            video.url,
            quality=quality,
            cookies_mode=self.repository.yt_dlp_cookies_mode(),
            cookies_browser=self.repository.yt_dlp_cookies_browser(),
        )
        self._store_video_and_channel(playable.video)
        if record_play:
            self.repository.record_play_started(playable.video.id)
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
        if clear_new_indicator:
            self.repository.clear_new_video_indicator(channel.id)
        self.repository.mark_channel_refresh(channel.id, success=True)
        return videos

    def channel_playlists(
        self, channel: Channel, limit: int = 30, start: int = 1
    ) -> list[Video]:
        return self.extractor.channel_playlists(channel, limit=limit, start=start)

    def refresh_subscriptions(
        self,
        limit_per_channel: int = 30,
        max_workers: int | None = None,
        progress: Callable[[Channel, bool], None] | None = None,
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
                progress(channel, True)
            try:
                is_initial_import = not self.repository.channel_videos(channel.id, 1)
                self.refresh_channel(
                    channel,
                    limit=limit_per_channel,
                    clear_new_indicator=is_initial_import,
                )
            except Exception:
                self.repository.mark_channel_refresh(channel.id, success=False)
                raise
            finally:
                if progress is not None:
                    progress(channel, False)

        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
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
        return self.extractor.recommended_videos(browser, limit=limit)

    def supported_browsers(self) -> list[str]:
        return self.extractor.supported_browsers()
