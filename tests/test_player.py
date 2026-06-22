from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Callable

from gtktube.models import PlayableVideo, Video
from gtktube.ui.player import PlayerMixin


class _Pane:
    def __init__(self) -> None:
        self.visible: bool | None = None

    def set_visible(self, visible: bool) -> None:
        self.visible = visible


class _Combo:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.active_id: str | None = None
        self.tooltip: str | None = None
        self.visible: bool | None = None

    def remove_all(self) -> None:
        self.items.clear()
        self.active_id = None

    def append(self, item_id: str, label: str) -> None:
        self.items.append((item_id, label))

    def set_active_id(self, item_id: str) -> bool:
        if item_id not in {existing_id for existing_id, _label in self.items}:
            return False
        self.active_id = item_id
        return True

    def get_active_id(self) -> str | None:
        return self.active_id

    def set_tooltip_text(self, tooltip: str) -> None:
        self.tooltip = tooltip

    def set_visible(self, visible: bool) -> None:
        self.visible = visible


class _Service:
    def __init__(self) -> None:
        self.play_video_calls = 0

    def downloaded_file_for_video(self, _target_dir: Path, _video_id: str) -> Path | None:
        return None

    def playback_cache_file_for_video(
        self,
        _target_dir: Path,
        _video_id: str,
        _quality: str,
    ) -> Path | None:
        return None

    def play_video(self, *_args: object, **_kwargs: object) -> PlayableVideo:
        self.play_video_calls += 1
        video = _args[0]
        assert isinstance(video, Video)
        return PlayableVideo(
            video=video,
            stream_url="https://example.test/combined.mp4",
            quality="1080p",
            resolved_quality="1080p",
        )


class _PlayerHarness(PlayerMixin):
    def __init__(self, temp_dir: Path) -> None:
        self.service = _Service()
        self.download_dir = temp_dir / "downloads"
        self.playback_cache_dir = temp_dir / "playback-cache"
        self.playback_request_id = 0
        self.playlist_pane = _Pane()
        self.playlist_current_index: int | None = 7
        self.preferred_quality = "1080p"
        self.preferred_playback_mode = "prefetch"
        self.updating_quality = False
        self.quality_combo = _Combo()
        self.tasks: list[tuple[str, Callable[[], PlayableVideo]]] = []
        self.loaded: list[PlayableVideo] = []

    def navigate_to(self, _view: object) -> None:
        pass

    def update_playlist_rows(self) -> None:
        pass

    def show_player_loading(self, _video: Video) -> None:
        pass

    def verbose_log(self, _message: str) -> None:
        pass

    def run_task(
        self,
        label: str,
        work: Callable[[], PlayableVideo],
        done: Callable[[PlayableVideo], None],
        **_kwargs: object,
    ) -> None:
        self.tasks.append((label, work))

    def play_prefetched_video(
        self,
        video: Video,
        quality: str,
        progress: Callable[[dict[str, object]], None] | None = None,
        record_play: bool = True,
    ) -> PlayableVideo:
        return PlayableVideo(
            video=video,
            stream_url=str(self.playback_cache_dir / "cached.mp4"),
            quality=quality,
            resolved_quality=f"cached {quality}",
        )

    def load_playable_if_current(
        self,
        playable: PlayableVideo,
        request_id: int,
    ) -> None:
        self.loaded.append(playable)


class PlayerMixinTests(unittest.TestCase):
    def test_uncached_playback_prefetches_instead_of_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            video = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )

            harness.play_video(video)

            self.assertEqual(harness.playlist_pane.visible, False)
            self.assertIsNone(harness.playlist_current_index)
            self.assertEqual(len(harness.tasks), 1)
            label, work = harness.tasks[0]
            self.assertEqual(label, "Pre-fetching video...")
            playable = work()
            self.assertEqual(playable.resolved_quality, "cached 1080p")
            self.assertEqual(harness.service.play_video_calls, 0)

    def test_direct_stream_playable_redirects_to_prefetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            harness.preferred_playback_mode = "streaming"
            video = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )
            direct = PlayableVideo(
                video=video,
                stream_url="https://example.test/video-only.mp4",
                audio_url="https://example.test/audio-only.m4a",
                quality="1080p",
                resolved_quality="1080p",
            )

            harness.load_playable(direct)

            self.assertEqual(len(harness.tasks), 1)
            label, work = harness.tasks[0]
            self.assertEqual(label, "Pre-fetching video...")
            self.assertEqual(work().resolved_quality, "cached 1080p")
            self.assertEqual(harness.service.play_video_calls, 0)

    def test_streaming_mode_resolves_without_prefetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            harness.preferred_playback_mode = "streaming"
            video = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )

            harness.play_video(video)

            self.assertEqual(len(harness.tasks), 1)
            label, work = harness.tasks[0]
            self.assertEqual(label, "Resolving video...")
            self.assertEqual(work().resolved_quality, "1080p")
            self.assertEqual(harness.service.play_video_calls, 1)

    def test_quality_option_preserves_streaming_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))

            self.assertEqual(
                harness.parse_quality_option_id("streaming:1080p"),
                ("streaming", "1080p"),
            )
            self.assertEqual(
                harness.parse_quality_option_id("prefetch:1080p"),
                ("prefetch", "1080p"),
            )

    def test_quality_control_uses_available_playable_qualities(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            video = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )
            playable = PlayableVideo(
                video=video,
                stream_url="https://example.test/combined.mp4",
                quality="1080p",
                resolved_quality="1080p",
                available_stream_qualities=["360p", "720p"],
                available_prefetch_qualities=["360p", "720p", "1080p"],
            )

            harness.update_quality_control(playable)

            self.assertEqual(
                [item_id for item_id, _label in harness.quality_combo.items],
                [
                    "streaming:360p",
                    "streaming:720p",
                    "prefetch:360p",
                    "prefetch:720p",
                    "prefetch:1080p",
                ],
            )
            self.assertEqual(harness.quality_combo.active_id, "prefetch:1080p")


if __name__ == "__main__":
    unittest.main()
