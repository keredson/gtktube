from __future__ import annotations

from gtktube.db.repositories import LibraryRepository
from gtktube.extractors.youtube import YoutubeExtractor
from gtktube.models import Channel, PlayableVideo, Video


class LibraryService:
    def __init__(self, repository: LibraryRepository, extractor: YoutubeExtractor):
        self.repository = repository
        self.extractor = extractor

    def play_url(self, url: str) -> PlayableVideo:
        playable = self.extractor.resolve_video(url)
        self._store_video_and_channel(playable.video)
        self.repository.record_play_started(playable.video.id)
        return playable

    def play_video(self, video: Video) -> PlayableVideo:
        playable = self.extractor.resolve_video(video.url)
        self._store_video_and_channel(playable.video)
        self.repository.record_play_started(playable.video.id)
        return playable

    def record_watch_range(self, video_id: str, start_seconds: int, end_seconds: int) -> None:
        self.repository.add_watch_range(video_id, start_seconds, end_seconds)

    def subscribe(self, url: str) -> Channel:
        channel = self.extractor.resolve_channel(url)
        self.repository.upsert_channel(channel, subscribed=True)
        return channel

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

    def refresh_channel(self, channel: Channel, limit: int = 30) -> list[Video]:
        videos = self.extractor.channel_uploads(channel, limit=limit)
        self.repository.upsert_videos(videos)
        self.repository.mark_channel_refresh(channel.id, success=True)
        return videos

    def refresh_subscriptions(self, limit_per_channel: int = 30) -> None:
        for channel in self.repository.subscribed_channels():
            try:
                self.refresh_channel(channel, limit=limit_per_channel)
            except Exception:
                self.repository.mark_channel_refresh(channel.id, success=False)
                raise

    def search(self, query: str, limit: int = 20) -> list[Video]:
        self.repository.add_search_history(query)
        return self.extractor.search(query, limit=limit)

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
