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


class _Stack:
    def __init__(self) -> None:
        self.visible_child_name: str | None = None

    def set_visible_child_name(self, name: str) -> None:
        self.visible_child_name = name


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
        self.repository = self

    def resume_position(self, _video_id: str) -> int:
        return 0

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


class _FakePlayer:
    def __init__(self) -> None:
        self.pause = False
        self.speed = 1.0
        self.loaded: tuple[str, dict[str, object]] | None = None

    def loadfile(self, stream_url: str, **options: object) -> None:
        self.loaded = (stream_url, options)


class _PlayerHarness(PlayerMixin):
    def __init__(self, temp_dir: Path) -> None:
        self.service = _Service()
        self.download_dir = temp_dir / "downloads"
        self.playback_cache_dir = temp_dir / "playback-cache"
        self.playback_request_id = 0
        self.playlist_pane = _Pane()
        self.playlist_current_index: int | None = 7
        self.preferred_quality = "1080p"
        self.preferred_playback_mode = "fetch"
        self.updating_quality = False
        self.quality_combo = _Combo()
        self.tasks: list[tuple[str, Callable[[], PlayableVideo]]] = []
        self.loaded: list[PlayableVideo] = []
        self.fake_player = _FakePlayer()
        self.player = None
        self.stack = _Stack()
        self.playback_rate = 1.0
        self.waited_for_buffer = False

    def navigate_to(self, _view: object) -> None:
        pass

    def update_playlist_rows(self) -> None:
        pass

    def show_player_loading(self, _video: Video) -> None:
        pass

    def verbose_log(self, _message: str) -> None:
        pass

    def prepare_for_player_startup(self) -> None:
        pass

    def start_playback_diag_timer(self) -> None:
        pass

    def load_sponsorblock_segments(self) -> None:
        pass

    def show_player_buffering(self, _message: str) -> None:
        pass

    def create_player(self, _playable: PlayableVideo) -> _FakePlayer:
        return self.fake_player

    def apply_selected_caption(self) -> None:
        pass

    def wait_for_playback_buffer(self, _player: object, _video_id: str) -> None:
        self.waited_for_buffer = True

    def show_full_player(self) -> None:
        pass

    def select_nav_page(self, _page: str) -> None:
        pass

    def run_task(
        self,
        label: str,
        work: Callable[[], PlayableVideo],
        done: Callable[[PlayableVideo], None],
        **_kwargs: object,
    ) -> None:
        self.tasks.append((label, work))

    def play_fetched_video(
        self,
        video: Video,
        quality: str,
        progress: Callable[[dict[str, object]], None] | None = None,
        record_play: bool = True,
        playlist_url: str | None = None,
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
    def test_uncached_playback_fetches_instead_of_streaming(self) -> None:
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
            self.assertEqual(label, "Fetching video...")
            playable = work()
            self.assertEqual(playable.resolved_quality, "cached 1080p")
            self.assertEqual(harness.service.play_video_calls, 0)

    def test_split_stream_playable_passes_audio_file_to_mpv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
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

            harness.start_playback(direct, resume_position=0)

            self.assertEqual(
                harness.fake_player.loaded,
                (
                    "https://example.test/video-only.mp4",
                    {"audio_file": "https://example.test/audio-only.m4a"},
                ),
            )
            self.assertTrue(harness.waited_for_buffer)
            self.assertEqual(harness.service.play_video_calls, 0)

    def test_streaming_mode_resolves_without_fetch(self) -> None:
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
                harness.parse_quality_option_id("fetch:1080p"),
                ("fetch", "1080p"),
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
                available_fetch_qualities=["360p", "720p", "1080p"],
            )

            harness.update_quality_control(playable)

            self.assertEqual(
                [item_id for item_id, _label in harness.quality_combo.items],
                [
                    "streaming:360p",
                    "streaming:720p",
                    "fetch:360p",
                    "fetch:720p",
                    "fetch:1080p",
                ],
            )
            self.assertEqual(harness.quality_combo.active_id, "fetch:1080p")

    def test_quality_control_prioritizes_preferred_method_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            harness.preferred_playback_mode = "fetch"
            harness.preferred_quality = "1080p"
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
                available_stream_qualities=["1080p"],
                available_fetch_qualities=["360p", "720p"],
            )

            harness.update_quality_control(playable)

            self.assertEqual(harness.quality_combo.active_id, "fetch:720p")


if __name__ == "__main__":
    unittest.main()
