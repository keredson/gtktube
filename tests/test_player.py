from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import CancelledError, Future
from pathlib import Path
from typing import Callable

from gtktube.models import CaptionTrack, PlayableVideo, SponsorBlockSegment, Video
from gtktube.ui import player as player_module
from gtktube.ui.player import Gdk, PlayerMixin
from gtktube.ui.sponsorblock import SponsorBlockMixin


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


class _VideoItem:
    def __init__(self, video: Video) -> None:
        self.video = video


class _ListStore:
    def __init__(self) -> None:
        self.items: list[_VideoItem] = []

    def append(self, item: _VideoItem) -> None:
        self.items.append(item)

    def get_n_items(self) -> int:
        return len(self.items)

    def get_item(self, index: int) -> _VideoItem:
        return self.items[index]

    def remove(self, index: int) -> None:
        del self.items[index]


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


class _WidgetState:
    def __init__(self) -> None:
        self.visible: bool | None = None
        self.hexpand: bool | None = None
        self.vexpand: bool | None = None
        self.valign: object | None = None
        self.margins = {
            "top": 0,
            "bottom": 0,
            "start": 0,
            "end": 0,
        }

    def set_visible(self, visible: bool) -> None:
        self.visible = visible

    def set_margin_top(self, margin: int) -> None:
        self.margins["top"] = margin

    def set_margin_bottom(self, margin: int) -> None:
        self.margins["bottom"] = margin

    def set_margin_start(self, margin: int) -> None:
        self.margins["start"] = margin

    def set_margin_end(self, margin: int) -> None:
        self.margins["end"] = margin

    def set_hexpand(self, hexpand: bool) -> None:
        self.hexpand = hexpand

    def set_vexpand(self, vexpand: bool) -> None:
        self.vexpand = vexpand

    def set_valign(self, valign: object) -> None:
        self.valign = valign


class _InfoWithParent:
    def __init__(self, parent: _WidgetState) -> None:
        self.parent = parent

    def get_parent(self) -> _WidgetState:
        return self.parent


class _Service:
    def __init__(self) -> None:
        self.play_video_calls = 0
        self.fetch_playback_video_calls = 0
        self.play_cached_video_calls = 0
        self.fetch_playback_video_args: list[tuple[str, str]] = []
        self.cached_playback: set[tuple[str, str]] = set()
        self.repository = self
        self.default_quality: tuple[str, str] | None = None

    def resume_position(self, _video_id: str) -> int:
        return 0

    def set_default_video_quality(self, _quality: str, mode: str = "streaming") -> None:
        self.default_quality = (_quality, mode)

    def sponsorblock_enabled(self) -> bool:
        return True

    def downloaded_file_for_video(self, _target_dir: Path, _video_id: str) -> Path | None:
        return None

    def playback_cache_file_for_video(
        self,
        target_dir: Path,
        video_id: str,
        quality: str,
    ) -> Path | None:
        if (video_id, quality) in self.cached_playback:
            return target_dir / f"cached-{quality}.mp4"
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
            available_stream_qualities=["1080p"],
            available_fetch_qualities=["1080p"],
            captions=[
                CaptionTrack(
                    id="subtitles:en",
                    label="English",
                    language="en",
                    url="https://example.test/en.vtt",
                )
            ],
        )

    def fetch_playback_video(
        self,
        _video: Video,
        quality: str,
        target_dir: Path,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> Path:
        self.fetch_playback_video_calls += 1
        self.fetch_playback_video_args.append((_video.id, quality))
        self.cached_playback.add((_video.id, quality))
        if progress is not None:
            progress({"status": "finished"})
        return target_dir / f"cached-{quality}.mp4"

    def play_cached_video(
        self,
        video: Video,
        path: Path,
        quality: str,
        record_play: bool = True,
        playlist_url: str | None = None,
        captions: list[CaptionTrack] | None = None,
        available_stream_qualities: list[str] | None = None,
        available_fetch_qualities: list[str] | None = None,
    ) -> PlayableVideo:
        self.play_cached_video_calls += 1
        return PlayableVideo(
            video=video,
            stream_url=str(path),
            quality=quality,
            resolved_quality=f"cached {quality}",
            available_stream_qualities=available_stream_qualities,
            available_fetch_qualities=available_fetch_qualities,
            captions=captions,
        )


class _FakePlayer:
    def __init__(self) -> None:
        self.pause = False
        self.speed = 1.0
        self.loaded: tuple[str, dict[str, object]] | None = None

    def loadfile(self, stream_url: str, **options: object) -> None:
        self.loaded = (stream_url, options)


class _FakeMpvModule:
    class MpvEventID:
        START_FILE = object()
        FILE_LOADED = object()
        END_FILE = object()


class _UnreadablePausePlayer:
    @property
    def pause(self) -> bool:
        raise AssertionError("player.pause must not be read")

    @pause.setter
    def pause(self, _value: bool) -> None:
        pass


class _PlayerHarness(PlayerMixin, SponsorBlockMixin):
    def __init__(self, temp_dir: Path) -> None:
        self.service = _Service()
        self.download_dir = temp_dir / "downloads"
        self.playback_cache_dir = temp_dir / "playback-cache"
        self.playback_request_id = 0
        self.playlist_pane = _Pane()
        self.queue_pane = _Pane()
        self.playlist_current_index: int | None = 7
        self.preferred_quality = "1080p"
        self.preferred_playback_mode = "fetch"
        self.updating_quality = False
        self.quality_combo = _Combo()
        self.tasks: list[tuple[str, Callable[[], PlayableVideo]]] = []
        self.loaded: list[PlayableVideo] = []
        self.fake_player = _FakePlayer()
        self.player = None
        self.mpv_module = _FakeMpvModule()
        self.mpv_event_callbacks: dict[object, object] = {}
        self.mpv_observed_time_pos = None
        self.mpv_observed_duration = None
        self.mpv_observed_properties: dict[str, object] = {}
        self.stack = _Stack()
        self.playback_rate = 1.0
        self.waited_for_buffer = False
        self.cleaned_up = False
        self.playback_end_handled = False
        self.current_playable: PlayableVideo | None = None
        self.pending_playback_video: Video | None = None
        self.pending_playback_playlist_url: str | None = None
        self.prefetch_request_id = 0
        self.prefetch_playback_key: tuple[str, str] | None = None
        self.playback_inhibit_cookie: int | None = None
        self.video_queue = _ListStore()
        self.playlist_store = _ListStore()
        self.playlist_skip_set: set[int] = set()
        self.current_playlist_url: str | None = None
        self.relative_seeks: list[int] = []
        self.media_seeks: list[tuple[int, bool]] = []
        self.status_messages: list[str] = []
        self.log_messages: list[str] = []
        self.verbose_messages: list[str] = []
        self.stop_pipeline_calls: list[tuple[bool, bool, bool]] = []
        self.sponsorblock_segments: list[SponsorBlockSegment] = []
        self.suppressed_sponsorblock_segments: set[str] = set()
        self.last_auto_skipped_segment: str | None = None
        self.pending_sponsorblock_skip: dict[str, object] | None = None
        self.video_fullscreen = False
        self.fullscreen_calls = 0

    def navigate_to(self, _view: object) -> None:
        pass

    def update_playlist_rows(self) -> None:
        pass

    def show_player_loading(self, _video: Video) -> None:
        pass

    def flush_watch_range(self) -> bool:
        return True

    def current_position_seconds(self) -> int:
        return 17

    def verbose_log(self, _message: str) -> None:
        self.verbose_messages.append(_message)

    def log(self, message: str) -> None:
        self.log_messages.append(message)

    def set_status(self, message: str) -> None:
        self.status_messages.append(message)

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

    def submit_background(
        self,
        fn: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> Future[object]:
        future: Future[object] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def schedule_background_finish(
        self,
        _future: Future[object],
        callback: Callable[[], bool],
    ) -> None:
        callback()

    def update_transport_navigation_buttons(self) -> None:
        pass

    def update_header_subtitle(self, _view: object) -> None:
        pass

    def update_player_metadata(self, _video: Video) -> None:
        pass

    def refresh_current_video_metadata_if_needed(
        self,
        _playable: PlayableVideo,
    ) -> None:
        pass

    def update_subscribe_check(self, _video: Video) -> None:
        pass

    def update_caption_tracks(self, _playable: PlayableVideo | None) -> None:
        pass

    def update_chapters(self, _playable: PlayableVideo | None) -> None:
        pass

    def update_player_share_button(self) -> None:
        pass

    def reload_channels(self) -> None:
        pass

    def maybe_show_sponsorblock_prompt(
        self,
        _continue_playback: Callable[[], None] | None = None,
    ) -> bool:
        return False

    def stop_pipeline(
        self,
        restore_stack: bool = True,
        keep_player_visible: bool = False,
        preserve_fullscreen: bool = False,
    ) -> None:
        self.stop_pipeline_calls.append(
            (restore_stack, keep_player_visible, preserve_fullscreen)
        )

    def fullscreen(self) -> None:
        self.fullscreen_calls += 1

    def seek_relative(self, delta_seconds: int) -> None:
        self.relative_seeks.append(delta_seconds)

    def seek_media(self, seconds: int, user_initiated: bool = True) -> None:
        self.media_seeks.append((seconds, user_initiated))

    def load_playable_if_current(
        self,
        playable: PlayableVideo,
        request_id: int,
    ) -> None:
        self.loaded.append(playable)

    def load_playable_at(self, playable: PlayableVideo, position: int) -> None:
        self.loaded.append(
            PlayableVideo(
                video=playable.video,
                stream_url=playable.stream_url,
                quality=playable.quality,
                resolved_quality=f"{playable.resolved_quality}@{position}",
            )
        )


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
            self.assertEqual(harness.service.play_video_calls, 1)
            self.assertEqual(harness.service.fetch_playback_video_calls, 1)
            self.assertEqual(harness.service.play_cached_video_calls, 1)
            self.assertEqual(
                [caption.label for caption in playable.captions or []],
                ["English"],
            )

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

    def test_player_seek_shortcuts_use_asymmetric_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))

            self.assertTrue(harness.handle_player_shortcut(Gdk.KEY_Left))
            self.assertTrue(harness.handle_player_shortcut(Gdk.KEY_j))
            self.assertTrue(harness.handle_player_shortcut(Gdk.KEY_Right))
            self.assertTrue(harness.handle_player_shortcut(Gdk.KEY_l))

            self.assertEqual(harness.relative_seeks, [-10, -10, 20, 20])

    def test_sponsorblock_skip_uses_observed_pause_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            harness.player = _UnreadablePausePlayer()
            harness.mpv_observed_properties["pause"] = False
            harness.sponsorblock_segments = [
                SponsorBlockSegment(
                    video_id="video1",
                    category="sponsor",
                    start_seconds=10.0,
                    end_seconds=20.0,
                )
            ]

            harness.maybe_skip_sponsorblock_segment(12)

            self.assertEqual(harness.media_seeks, [(20, False)])

    def test_clearing_observed_playback_state_resets_seek_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            harness.mpv_observed_time_pos = 935.5
            harness.mpv_observed_duration = 1465.3
            harness.mpv_observed_properties["pause"] = True

            harness.clear_mpv_observed_playback_state()

            self.assertEqual(PlayerMixin.current_position_seconds(harness), 0)
            self.assertEqual(PlayerMixin.current_duration_seconds(harness), 0)
            self.assertEqual(harness.mpv_observed_properties, {})

    def test_stale_duplicate_eof_handler_does_not_stop_next_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            old_video = Video(
                id="old",
                title="Old Video",
                url="https://example.test/watch?v=old",
            )
            next_video = Video(
                id="next",
                title="Next Video",
                url="https://example.test/watch?v=next",
            )
            harness.playback_request_id = 7
            harness.current_playable = PlayableVideo(
                video=old_video,
                stream_url="https://example.test/old.mp4",
                quality="1080p",
                resolved_quality="1080p",
            )
            harness.video_queue.append(_VideoItem(next_video))

            harness.handle_playback_end_file(
                "mpv-property:eof-reached",
                7,
                "old",
            )
            harness.playback_end_handled = False
            harness.current_playable = None
            harness.handle_playback_end_file(
                "mpv-property:core-idle",
                7,
                "old",
            )

            self.assertEqual(harness.playback_request_id, 8)
            self.assertEqual(harness.video_queue.get_n_items(), 0)
            self.assertEqual(harness.stop_pipeline_calls, [])
            self.assertTrue(
                any(
                    "stale_request=7" in message
                    for message in harness.verbose_messages
                )
            )

    def test_stale_mpv_end_file_event_does_not_stop_current_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            old_player = object()
            new_player = object()
            harness.player = new_player
            harness.playback_request_id = 8
            harness.current_playable = PlayableVideo(
                video=Video(
                    id="next",
                    title="Next Video",
                    url="https://example.test/watch?v=next",
                ),
                stream_url="https://example.test/next.mp4",
                quality="1080p",
                resolved_quality="1080p",
            )

            result = harness.handle_mpv_event(
                old_player,
                harness.mpv_module.MpvEventID.END_FILE,
                "eof",
                None,
                7,
                "old",
            )

            self.assertFalse(result)
            self.assertFalse(harness.playback_end_handled)
            self.assertEqual(harness.stop_pipeline_calls, [])
            self.assertTrue(
                any(
                    "stale player" in message
                    for message in harness.verbose_messages
                )
            )

    def test_fetch_mode_prefetches_next_queue_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            current = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )
            next_video = Video(
                id="video2",
                title="Video Two",
                url="https://example.test/watch?v=video2",
            )
            harness.current_playable = PlayableVideo(
                video=current,
                stream_url="https://example.test/current.mp4",
                quality="1080p",
                resolved_quality="cached 1080p",
            )
            harness.video_queue.append(_VideoItem(next_video))

            harness.schedule_next_playback_prefetch()

            self.assertEqual(harness.service.fetch_playback_video_args, [("video2", "1080p")])

    def test_load_playable_prefetches_existing_queue_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            current = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )
            next_video = Video(
                id="video2",
                title="Video Two",
                url="https://example.test/watch?v=video2",
            )
            harness.video_queue.append(_VideoItem(next_video))

            harness.load_playable(
                PlayableVideo(
                    video=current,
                    stream_url="https://example.test/current.mp4",
                    quality="1080p",
                    resolved_quality="1080p",
                )
            )

            self.assertEqual(harness.service.fetch_playback_video_args, [("video2", "1080p")])

    def test_fullscreen_chrome_keeps_compact_controls_after_full_player_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            harness.header = _Pane()
            harness.sidebar = _Pane()
            harness.queue_pane = _Pane()
            harness.player_metadata = _WidgetState()
            harness.player_controls = _WidgetState()
            harness.miniplayer_controls_container = _WidgetState()
            mini_header = _WidgetState()
            harness.miniplayer_info = _InfoWithParent(mini_header)
            harness.fullscreen_icon = type(
                "Icon",
                (),
                {"set_from_icon_name": lambda self, _name: None},
            )()
            harness.fullscreen_button = type(
                "Button",
                (),
                {"set_tooltip_text": lambda self, _text: None},
            )()

            harness.apply_full_player_controls_chrome()
            harness.miniplayer_controls_container.set_valign(player_module.Gtk.Align.FILL)
            mini_header.set_visible(True)
            self.assertTrue(harness.player_metadata.visible)
            self.assertEqual(
                harness.player_controls.margins,
                {"top": 8, "bottom": 8, "start": 12, "end": 12},
            )

            harness.video_fullscreen = True
            harness.apply_video_fullscreen_chrome()

            self.assertEqual(harness.fullscreen_calls, 0)
            self.assertFalse(mini_header.visible)
            self.assertFalse(harness.player_metadata.visible)
            self.assertEqual(
                harness.miniplayer_controls_container.valign,
                player_module.Gtk.Align.CENTER,
            )
            self.assertEqual(
                harness.player_controls.margins,
                {"top": 0, "bottom": 0, "start": 0, "end": 0},
            )

    def test_fetch_mode_prefetches_next_playlist_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            current = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )
            next_video = Video(
                id="video2",
                title="Video Two",
                url="https://example.test/watch?v=video2",
            )
            harness.current_playable = PlayableVideo(
                video=current,
                stream_url="https://example.test/current.mp4",
                quality="1080p",
                resolved_quality="cached 1080p",
            )
            harness.playlist_current_index = 0
            harness.playlist_store.append(_VideoItem(current))
            harness.playlist_store.append(_VideoItem(next_video))

            harness.schedule_next_playback_prefetch()

            self.assertEqual(harness.service.fetch_playback_video_args, [("video2", "1080p")])

    def test_streaming_mode_does_not_prefetch_next_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            harness.preferred_playback_mode = "streaming"
            current = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )
            next_video = Video(
                id="video2",
                title="Video Two",
                url="https://example.test/watch?v=video2",
            )
            harness.current_playable = PlayableVideo(
                video=current,
                stream_url="https://example.test/current.mp4",
                quality="1080p",
                resolved_quality="1080p",
            )
            harness.video_queue.append(_VideoItem(next_video))

            harness.schedule_next_playback_prefetch()

            self.assertEqual(harness.service.fetch_playback_video_args, [])

    def test_quality_change_cancels_stale_fetch_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            video = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )
            harness.current_playable = PlayableVideo(
                video=video,
                stream_url="https://example.test/combined.mp4",
                quality="1080p",
                resolved_quality="1080p",
            )
            harness.quality_combo.append("fetch:720p", "Fetch 720p")
            harness.quality_combo.set_active_id("fetch:720p")

            harness.on_quality_changed(harness.quality_combo)

            self.assertEqual(harness.playback_request_id, 1)
            self.assertEqual(len(harness.tasks), 1)
            label, work = harness.tasks[0]
            self.assertEqual(label, "Fetching 720p...")

            harness.playback_request_id += 1
            with self.assertRaises(CancelledError):
                work()

    def test_mode_change_while_fetching_starts_streaming_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _PlayerHarness(Path(temp))
            video = Video(
                id="video1",
                title="Video One",
                url="https://example.test/watch?v=video1",
            )

            harness.play_video(video)
            self.assertEqual(harness.playback_request_id, 1)
            self.assertEqual(len(harness.tasks), 1)
            fetch_label, fetch_work = harness.tasks[0]
            self.assertEqual(fetch_label, "Fetching video...")

            harness.quality_combo.append("streaming:1080p", "Stream 1080p")
            harness.quality_combo.set_active_id("streaming:1080p")
            harness.on_quality_changed(harness.quality_combo)

            self.assertEqual(harness.preferred_playback_mode, "streaming")
            self.assertEqual(harness.playback_request_id, 2)
            self.assertEqual(len(harness.tasks), 2)
            stream_label, stream_work = harness.tasks[1]
            self.assertEqual(stream_label, "Resolving video...")
            self.assertEqual(stream_work().resolved_quality, "1080p")

            with self.assertRaises(CancelledError):
                fetch_work()

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
