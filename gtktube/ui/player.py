from __future__ import annotations

import locale
import os
import re
import hashlib
import time
import urllib.error
import urllib.request
from ctypes import byref, c_int, c_void_p
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from gtktube.extractors.youtube import QUALITY_FORMATS
from gtktube.models import CaptionTrack, PlayableVideo, Video, VideoChapter
from gtktube.ui.types import ViewState


PLAYBACK_RATES = [rate / 100 for rate in range(25, 401, 25)]
URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>\"]+")
BUFFER_CHECK_INTERVAL_MS = 250
BUFFER_START_TIMEOUT_SECONDS = 20.0
MPV_CACHE_ON_DISK = True
MPV_CACHE_SECS = 120
MPV_DEMUXER_READAHEAD_SECS = 60
MPV_DEMUXER_MAX_BYTES = "512MiB"
MPV_DEMUXER_MAX_BACK_BYTES = "64MiB"
MPV_DEMUXER_CACHE_UNLINK_FILES = "immediate"


class PlayerMixin:
    def video_meta(self, video: Video) -> str:
        parts = []
        if video.channel_title:
            parts.append(video.channel_title)
        parts.extend(self.video_detail_meta_parts(video))
        return " · ".join(parts)

    def video_meta_without_channel(self, video: Video) -> str:
        return " · ".join(self.video_detail_meta_parts(video))

    def video_detail_meta_parts(self, video: Video) -> list[str]:
        parts = []
        if video.published_at:
            parts.append(f"Posted {video.published_at}")
        if video.duration_seconds:
            parts.append(self.format_time(video.duration_seconds))
        if video.view_count is not None:
            parts.append(f"{video.view_count:,} views")
        if video.percent_watched:
            parts.append(f"{round(video.percent_watched * 100)}% watched")
        if video.completed:
            parts.append("completed")
        return parts

    def play_video(self, video: Video, hide_sidebar: bool = True) -> None:
        self.navigate_to(ViewState("player"))
        # Mutual exclusivity: Playing a random video hides the playlist
        if hide_sidebar:
            self.playlist_pane.set_visible(False)
            self.playlist_current_index = None
            self.update_playlist_rows()
        quality = self.selected_quality()
        self.playback_request_id += 1
        request_id = self.playback_request_id
        self.mpv_stream_retry = None
        self.show_player_loading(video)
        self.verbose_log(
            "playback requested "
            f"video={video.id} quality={quality} url={video.url}"
        )
        self.run_task(
            "Resolving video...",
            lambda: self.service.play_video(video, quality=quality),
            lambda playable: self.load_playable_if_current(playable, request_id),
            error=lambda exc: self.show_player_error_if_current(exc, request_id),
        )

    def load_playable_if_current(
        self, playable: PlayableVideo, request_id: int
    ) -> None:
        if request_id != self.playback_request_id:
            self.verbose_log(
                "ignoring stale playback resolve "
                f"video={playable.video.id} request={request_id} "
                f"current={self.playback_request_id}"
            )
            return
        self.load_playable(playable)

    def clear_player_loading_if_current(self, request_id: int) -> None:
        if request_id == self.playback_request_id:
            self.set_player_loading(False)

    def show_player_error_if_current(
        self, exc: Exception, request_id: int
    ) -> None:
        if request_id != self.playback_request_id:
            return
        self.show_player_error(str(exc) or "Could not load video")
        self.reload_visible_video_grid()

    def show_player_loading(self, video: Video) -> None:
        self.flush_watch_range()
        self.stop_pipeline(restore_stack=False)
        self.current_playable = None
        self.active_caption_url = None
        self.player_title.set_text(video.title)
        self.player_meta.set_text("Resolving video...")
        self.player_chapter_label.set_text("")
        self.player_chapter_label.set_visible(False)
        self.miniplayer_title.set_text(video.title)
        self.miniplayer_meta.set_text("Resolving video...")
        self.set_description_text("")
        self.update_caption_tracks(None)
        self.update_chapters(None)
        self.update_player_share_button()
        self.elapsed_label.set_text("0:00")
        self.duration_label.set_text("0:00")
        self.updating_scrubber = True
        self.scrubber.set_range(0, 1)
        self.scrubber.set_value(0)
        self.updating_scrubber = False
        self.set_player_loading(True)
        self.show_full_player()
        self.select_nav_page("player")
        self.stack.set_visible_child_name("player")

    def set_player_loading(self, loading: bool) -> None:
        self.player_loading_overlay.set_visible(loading)
        if loading:
            self.player_loading_label.set_text("Resolving video...")
            self.player_loading_spinner.start()
        else:
            self.player_loading_spinner.stop()

    def show_player_buffering(self, message: str) -> None:
        self.player_loading_label.set_text(message)
        self.player_loading_overlay.set_visible(True)
        self.player_loading_spinner.start()

    def show_player_error(self, message: str) -> None:
        self.player_loading_spinner.stop()
        self.player_loading_label.set_text(message)
        self.player_loading_overlay.set_visible(True)
        self.player_meta.set_text("Playback unavailable")
        self.miniplayer_meta.set_text("Playback unavailable")

    def load_playable(
        self, playable: PlayableVideo, resume_position: int | None = None
    ) -> None:
        self.verbose_log(
            "playback resolved "
            f"video={playable.video.id} requested_quality={playable.quality} "
            f"resolved_quality={playable.resolved_quality or 'unknown'} "
            f"has_video_stream={bool(playable.stream_url)} "
            f"has_audio_stream={bool(playable.audio_url)}"
        )
        self.flush_watch_range()
        self.stop_pipeline(restore_stack=False)
        self.current_playable = playable
        self.update_header_subtitle(ViewState("player"))
        self.updating_quality = True
        self.quality_combo.set_active_id(playable.quality)
        self.updating_quality = False
        self.update_player_metadata(playable.video)
        recommended_updater = getattr(self, "update_recommended_cached_video", None)
        if callable(recommended_updater):
            recommended_updater(playable.video)
        self.refresh_current_video_metadata_if_needed(playable)
        self.update_subscribe_check(playable.video)
        self.update_caption_tracks(playable)
        self.update_chapters(playable)
        self.update_player_share_button()
        self.reload_channels()
        self.show_full_player()
        self.select_nav_page("player")
        self.stack.set_visible_child_name("player")

        if self.maybe_show_sponsorblock_prompt(
            lambda: self.start_playback(playable, resume_position)
        ):
            return
        self.start_playback(playable, resume_position)

    def start_playback(
        self, playable: PlayableVideo, resume_position: int | None = None
    ) -> None:
        self.verbose_log(
            "playback starting "
            f"video={playable.video.id} quality={playable.quality} "
            f"resume={resume_position if resume_position is not None else 'auto'}"
        )
        self.load_sponsorblock_segments()

        resume = (
            resume_position
            if resume_position is not None
            else self.service.repository.resume_position(playable.video.id)
        )
        if resume > 0:
            self.suppress_sponsorblock_for_seek(resume)

        self.mpv_file_loaded = False
        self.mpv_stream_error_message = None
        self.mpv_end_handled = False
        self.show_player_buffering("Opening stream...")
        player = self.create_player(playable)
        if player is None:
            self.hide_miniplayer()
            return
        self.player = player
        stream_context = (
            f"video={playable.video.id} quality={playable.quality} "
            f"separate_audio={bool(playable.audio_url)}"
        )
        try:
            self.verbose_log(f"mpv starting playback {stream_context}")
            self.player.pause = True
            self.player.speed = self.playback_rate
            load_options = {}
            if playable.audio_url:
                load_options["audio_file"] = playable.audio_url
            if resume > 0:
                load_options["start"] = resume
                self.verbose_log(
                    "resuming playback "
                    f"video={playable.video.id} seconds={resume}"
                )
            self.player.loadfile(playable.stream_url, **load_options)
        except Exception as exc:
            self.set_status(f"Playback error: {exc}")
            self.log(f"mpv loadfile failed {stream_context}: {exc}")
            self.stop_pipeline()
            return

        self.range_start_seconds = resume if resume > 0 else 0
        self.apply_selected_caption()
        self.wait_for_playback_buffer(player, playable.video.id)
        self.show_full_player()
        self.select_nav_page("player")
        self.stack.set_visible_child_name("player")

    def wait_for_playback_buffer(self, player: Any, video_id: str) -> None:
        target = self.playback_buffer_target_seconds()
        started_at = time.monotonic()
        message = self.playback_buffer_message(0.0, target)
        self.set_status(message)
        self.show_player_buffering(message)
        self.verbose_log(
            "mpv buffering before playback "
            f"video={video_id} target={target:g}s rate={self.playback_rate:g}"
        )
        GLib.timeout_add(
            BUFFER_CHECK_INTERVAL_MS,
            self.maybe_start_buffered_playback,
            player,
            video_id,
            started_at,
        )

    def playback_buffer_target_seconds(self) -> float:
        return max(12.0, min(30.0, 10.0 * max(1.0, self.playback_rate)))

    def maybe_start_buffered_playback(
        self,
        player: Any,
        video_id: str,
        started_at: float,
    ) -> bool:
        if player is not self.player:
            return False

        if self.mpv_stream_error_message:
            self.show_mpv_playback_error(self.mpv_stream_error_message)
            return False

        elapsed = time.monotonic() - started_at
        target = self.playback_buffer_target_seconds()
        cache_duration = self.mpv_float_property("demuxer-cache-duration") or 0.0
        message = self.playback_buffer_message(cache_duration, target)
        if self.player_loading_label.get_text() != message:
            self.set_status(message)
            self.show_player_buffering(message)
        cache_state = self.mpv_property("demuxer-cache-state")
        underrun = (
            isinstance(cache_state, dict)
            and bool(cache_state.get("underrun"))
        )
        has_open_stream = self.mpv_file_loaded or cache_duration > 0
        ready = has_open_stream and cache_duration >= target
        timed_out = elapsed >= BUFFER_START_TIMEOUT_SECONDS
        if not ready and not timed_out:
            return True

        if not has_open_stream:
            self.show_mpv_playback_error("Could not open video stream.")
            return False

        try:
            player.pause = False
            player.speed = self.playback_rate
        except Exception as exc:
            self.set_status(f"Playback error: {exc}")
            self.log(f"mpv buffered playback start failed video={video_id}: {exc}")
            self.stop_pipeline()
            return False

        self.last_playback_diagnostics_at = 0.0
        self.last_playback_diagnostics_values = {}
        self.last_playback_diagnostics_paused = False
        self.mpv_file_loaded = True
        self.update_playback_inhibition()
        self.set_player_loading(False)
        self.set_status("Ready")
        self.verbose_log(
            "mpv play command accepted "
            f"video={video_id} buffered={cache_duration:.3f}s "
            f"target={target:g}s elapsed={elapsed:.3f}s "
            f"underrun={underrun} reason={'target' if ready else 'timeout'}"
        )
        return False

    def playback_buffer_message(self, cache_duration: float, target: float) -> str:
        percent = 0
        if target > 0:
            ratio = min(1.0, max(0.0, cache_duration / target))
            percent = round(ratio * 100)
        return f"Buffering {percent}%..."

    def show_mpv_playback_error(self, message: str) -> bool:
        self.set_status(f"Playback error: {message}")
        self.stop_pipeline(restore_stack=False, keep_player_visible=True)
        self.show_full_player()
        self.select_nav_page("player")
        self.stack.set_visible_child_name("player")
        self.show_player_error(message)
        return False

    def retry_mpv_stream_open_error(self, message: str) -> bool:
        if self.current_playable is None:
            return self.show_mpv_playback_error(message)
        video = self.current_playable.video
        request_id = self.playback_request_id
        retry_key = (request_id, video.id)
        if self.mpv_stream_retry == retry_key:
            return self.show_mpv_playback_error(message)
        self.mpv_stream_retry = retry_key

        position = self.range_start_seconds
        if position is None:
            position = self.current_position_seconds()
        quality = self.current_playable.quality or self.selected_quality()
        self.verbose_log(
            "retrying playback after stream open failure "
            f"video={video.id} quality={quality} position={position}"
        )
        self.stop_pipeline(restore_stack=False, keep_player_visible=True)
        self.show_full_player()
        self.select_nav_page("player")
        self.stack.set_visible_child_name("player")
        self.show_player_buffering("Refreshing stream URL...")

        def done(playable: PlayableVideo) -> None:
            if request_id != self.playback_request_id:
                return
            self.load_playable(playable, resume_position=position)

        def failed(exc: Exception) -> None:
            if request_id != self.playback_request_id:
                return
            self.show_mpv_playback_error(str(exc) or message)

        self.run_task(
            "Refreshing stream URL...",
            lambda: self.service.play_video(
                video,
                quality=quality,
                record_play=False,
            ),
            done,
            error=failed,
        )
        return False

    def maybe_show_sponsorblock_prompt(
        self, after_response: Callable[[], None] | None = None
    ) -> bool:
        repository = self.service.repository
        if repository.sponsorblock_enabled() or repository.sponsorblock_prompt_shown():
            return False

        dialog = Gtk.Dialog(
            title="Enable SponsorBlock?",
            transient_for=self,
            modal=True,
        )
        dialog.set_default_size(480, -1)
        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        title = Gtk.Label(label="Skip sponsor segments automatically?", xalign=0)
        title.add_css_class("heading")
        content.append(title)

        message = Gtk.Label(
            label=(
                "GTKTube can use SponsorBlock, a community-maintained database, "
                "to find sponsor segments in videos and skip them while you watch.\n\n"
                "If you enable it, GTKTube will send the current YouTube video ID "
                "to SponsorBlock when loading a video. It does not send your "
                "account information or watch history. You can change this later "
                "in Settings."
            ),
            xalign=0,
            wrap=True,
        )
        content.append(message)

        dialog.add_button("Not now", Gtk.ResponseType.CANCEL)
        dialog.add_button("Enable SponsorBlock", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        def response(_dialog: Gtk.Dialog, response_id: int) -> None:
            repository.set_sponsorblock_prompt_shown()
            if response_id == Gtk.ResponseType.ACCEPT:
                repository.set_sponsorblock_enabled(True)
                self.reload_settings()
            dialog.destroy()
            if after_response is not None:
                after_response()

        dialog.connect("response", response)
        dialog.present()
        return True

    def update_player_metadata(self, video: Video) -> None:
        self.player_title.set_text(video.title)
        meta = self.video_meta(video)
        if self.current_playable and self.current_playable.resolved_quality:
            meta = f"{meta} · {self.current_playable.resolved_quality}".strip(" ·")
        self.player_meta.set_text(meta)
        self.miniplayer_title.set_text(video.title)
        self.miniplayer_meta.set_text(meta)
        self.set_description_text(video.description or "")
        self.update_player_share_button()

    def refresh_current_video_metadata_if_needed(self, playable: PlayableVideo) -> None:
        video = playable.video
        if (
            video.channel_title
            and video.duration_seconds
            and video.published_at
            and video.view_count is not None
            and video.description
        ):
            return
        video_id = video.id
        future = self.submit_background(self.service.refresh_video_metadata, video)
        if future is None:
            return

        def done() -> bool:
            if self.cleaned_up:
                return False
            try:
                refreshed = future.result()
            except Exception as exc:
                self.verbose_log(f"metadata refresh failed video={video_id}: {exc}")
                return False
            if self.current_playable is None or self.current_playable.video.id != video_id:
                return False
            self.current_playable = replace(
                self.current_playable,
                video=refreshed,
            )
            self.update_player_metadata(refreshed)
            self.update_subscribe_check(refreshed)
            self.update_player_share_button()
            recommended_updater = getattr(self, "update_recommended_cached_video", None)
            if callable(recommended_updater):
                recommended_updater(refreshed)
            return False

        self.schedule_background_finish(future, done)

    def update_chapters(self, playable: PlayableVideo | None) -> None:
        chapters = playable.chapters if playable and playable.chapters else []
        self.clear_chapter_rows()
        for chapter in chapters:
            self.player_chapters_list.append(self.chapter_row(chapter))
        has_chapters = bool(chapters)
        self.player_chapters_button.set_visible(has_chapters)
        self.player_chapters_button.set_sensitive(has_chapters)
        self.player_chapter_label.set_visible(has_chapters)
        if has_chapters:
            self.update_active_chapter(self.current_position_seconds())
        else:
            self.player_chapter_label.set_text("")
        self.sponsorblock_timeline.queue_draw()

    def clear_chapter_rows(self) -> None:
        child = self.player_chapters_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.player_chapters_list.remove(child)
            child = next_child

    def chapter_row(self, chapter: VideoChapter) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)

        time_label = Gtk.Label(
            label=self.format_time(int(chapter.start_seconds)),
            xalign=0,
        )
        time_label.add_css_class("dim-label")
        time_label.set_width_chars(7)
        box.append(time_label)

        title_label = Gtk.Label(label=chapter.title, xalign=0, hexpand=True)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(title_label)

        row.set_child(box)
        return row

    def on_chapter_row_activated(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow
    ) -> None:
        chapters = self.current_playable.chapters if self.current_playable else []
        index = row.get_index()
        if not chapters or index < 0 or index >= len(chapters):
            return
        chapter = chapters[index]
        self.player_chapters_popover.popdown()
        self.seek_media(
            int(chapter.start_seconds),
            user_initiated=True,
            precision="exact",
        )

    def active_chapter(
        self, chapters: list[VideoChapter], position_seconds: int
    ) -> VideoChapter | None:
        active = None
        for chapter in chapters:
            if chapter.start_seconds <= position_seconds:
                active = chapter
            else:
                break
        return active

    def update_active_chapter(self, position_seconds: int) -> None:
        chapters = self.current_playable.chapters if self.current_playable else []
        if not chapters:
            self.player_chapter_label.set_text("")
            self.player_chapter_label.set_visible(False)
            return
        active = self.active_chapter(chapters, position_seconds)
        if active is None:
            self.player_chapter_label.set_text("")
            return
        self.player_chapter_label.set_text(f"Chapter: {active.title}")
        self.player_chapter_label.set_tooltip_text(active.title)

        child = self.player_chapters_list.get_first_child()
        while child is not None:
            if child.get_index() == active.position:
                self.player_chapters_list.select_row(child)
                break
            child = child.get_next_sibling()

    def set_description_text(self, text: str) -> None:
        self.description_text = text
        has_description = bool(text.strip())
        if not has_description and self.description_window is not None:
            self.description_window.close()
        self.player_description_button.set_sensitive(has_description)
        if self.description_window is not None and has_description:
            self.populate_description_window()

    def description_text_view(self, text: str) -> Gtk.TextView:
        text_view = Gtk.TextView()
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_left_margin(8)
        text_view.set_right_margin(8)
        text_view.set_top_margin(8)
        text_view.set_bottom_margin(8)
        link_tags: dict[str, str] = {}
        buffer = text_view.get_buffer()
        buffer.set_text(text)
        for index, match in enumerate(URL_PATTERN.finditer(text)):
            start_offset = match.start()
            end_offset = self.link_end_offset(text, match.end())
            url = text[start_offset:end_offset]
            tag_name = f"description-link-{index}"
            tag = buffer.create_tag(
                tag_name,
                underline=Pango.Underline.SINGLE,
                foreground="#62a0ea",
            )
            start = buffer.get_iter_at_offset(start_offset)
            end = buffer.get_iter_at_offset(end_offset)
            buffer.apply_tag(tag, start, end)
            link_tags[tag_name] = self.normalized_url(url)

        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect(
            "released",
            lambda gesture, n_press, x, y, view=text_view, tags=link_tags: (
                self.on_description_view_clicked(gesture, n_press, x, y, view, tags)
            ),
        )
        text_view.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect(
            "motion",
            lambda _controller, x, y, view=text_view, tags=link_tags: (
                self.on_description_view_motion(view, tags, x, y)
            ),
        )
        motion.connect(
            "leave",
            lambda _controller, view=text_view: view.set_cursor(None),
        )
        text_view.add_controller(motion)
        return text_view

    def link_end_offset(self, text: str, end: int) -> int:
        while end > 0 and text[end - 1] in ".,;:!?)]}":
            end -= 1
        return end

    def on_player_description_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            button.set_tooltip_text("Hide description")
            self.show_description_window()
        else:
            button.set_tooltip_text("Show description")
            if self.description_window is not None:
                self.description_window.close()

    def show_description_window(self) -> None:
        if not self.description_text.strip():
            self.player_description_button.set_active(False)
            return
        if self.description_window is not None:
            self.description_window.present()
            return

        window = Gtk.Window(title="Description")
        window.set_transient_for(self)
        window.set_modal(False)
        window.set_default_size(680, 520)
        window.connect("close-request", self.on_description_window_close)

        self.description_window = window
        self.populate_description_window()
        window.present()

    def populate_description_window(self) -> None:
        if self.description_window is None:
            return
        if self.current_playable is not None:
            self.description_window.set_title(
                f"Description - {self.current_playable.video.title}"
            )
        else:
            self.description_window.set_title("Description")

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.description_text_view(self.description_text))
        self.description_window.set_child(scroller)

    def on_description_window_close(self, _window: Gtk.Window) -> bool:
        self.description_window = None
        self.player_description_button.set_tooltip_text("Show description")
        if self.player_description_button.get_active():
            self.player_description_button.set_active(False)
        return False

    def on_description_view_clicked(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        text_view: Gtk.TextView,
        link_tags: dict[str, str],
    ) -> None:
        uri = self.description_uri_at_location(text_view, link_tags, x, y)
        if uri:
            Gtk.show_uri(self, uri, Gdk.CURRENT_TIME)

    def on_description_view_motion(
        self,
        text_view: Gtk.TextView,
        link_tags: dict[str, str],
        x: float,
        y: float,
    ) -> None:
        if self.description_uri_at_location(text_view, link_tags, x, y):
            text_view.set_cursor_from_name("pointer")
        else:
            text_view.set_cursor(None)

    def description_uri_at_location(
        self,
        text_view: Gtk.TextView,
        link_tags: dict[str, str],
        x: float,
        y: float,
    ) -> str | None:
        buffer_x, buffer_y = text_view.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET,
            int(x),
            int(y),
        )
        found, text_iter = text_view.get_iter_at_location(
            buffer_x,
            buffer_y,
        )
        if not found:
            return None
        for tag in text_iter.get_tags():
            name = tag.props.name
            uri = link_tags.get(name)
            if uri:
                return uri
        return None

    def update_subscribe_check(self, video: Video) -> None:
        subscribed = self.service.repository.is_subscribed(video.channel_id)
        self.updating_subscribe_check = True
        self.player_subscribe.set_active(subscribed)
        self.updating_subscribe_check = False
        self.player_subscribe.set_sensitive(bool(video.channel_id or video.url))

    def update_player_share_button(self) -> None:
        self.player_share_button.set_sensitive(self.current_playable is not None)
        self.restore_player_share_icon()

    def show_full_player(self) -> None:
        self.stack.set_visible(False)
        self.miniplayer.remove_css_class("miniplayer")
        self.miniplayer.set_orientation(Gtk.Orientation.VERTICAL)
        self.miniplayer.set_spacing(0)
        self.miniplayer.set_hexpand(True)
        self.miniplayer.set_vexpand(True)
        self.miniplayer_video_container.set_hexpand(True)
        self.miniplayer_video_container.set_vexpand(True)
        self.miniplayer_video_container.set_valign(Gtk.Align.FILL)
        self.miniplayer_video_container.set_size_request(-1, -1)
        self.video_overlay.set_size_request(-1, 360)
        self.miniplayer_controls_container.set_hexpand(True)
        self.miniplayer_controls_container.set_vexpand(False)
        self.miniplayer_controls_container.set_valign(Gtk.Align.FILL)
        self.miniplayer_info.get_parent().set_visible(False)
        self.player_metadata.set_visible(True)
        self.video.set_hexpand(True)
        self.video.set_vexpand(True)
        self.video.set_valign(Gtk.Align.FILL)
        self.video.set_size_request(-1, 360)
        self.player_controls.set_margin_top(8)
        self.player_controls.set_margin_bottom(8)
        self.player_controls.set_margin_start(12)
        self.player_controls.set_margin_end(12)
        self.miniplayer.set_visible(True)
        self.video.queue_resize()
        self.video.queue_render()

    def show_miniplayer(self) -> None:
        if self.video_fullscreen:
            self.close_video_fullscreen()
        self.stack.set_visible(True)
        self.miniplayer.add_css_class("miniplayer")
        self.miniplayer.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.miniplayer.set_spacing(8)
        self.miniplayer.set_hexpand(True)
        self.miniplayer.set_vexpand(False)
        self.miniplayer_video_container.set_hexpand(False)
        self.miniplayer_video_container.set_vexpand(False)
        self.miniplayer_video_container.set_valign(Gtk.Align.CENTER)
        self.miniplayer_video_container.set_size_request(176, 99)
        self.video_overlay.set_size_request(176, 99)
        self.miniplayer_controls_container.set_hexpand(True)
        self.miniplayer_controls_container.set_vexpand(False)
        self.miniplayer_controls_container.set_valign(Gtk.Align.CENTER)
        self.miniplayer_info.get_parent().set_visible(True)
        self.player_metadata.set_visible(False)
        self.video.set_hexpand(False)
        self.video.set_vexpand(False)
        self.video.set_valign(Gtk.Align.CENTER)
        self.video.set_size_request(176, 99)
        self.player_controls.set_margin_top(0)
        self.player_controls.set_margin_bottom(0)
        self.player_controls.set_margin_start(0)
        self.player_controls.set_margin_end(0)
        self.miniplayer.set_visible(True)
        self.video.queue_resize()
        self.video.queue_render()

    def hide_miniplayer(self) -> None:
        self.miniplayer.set_visible(False)
        self.stack.set_visible(True)

    def schedule_video_render_context_reset(self) -> None:
        GLib.idle_add(self.reset_video_render_context, 0)

    def reset_video_render_context(self, attempts: int = 0) -> bool:
        if (
            self.player is None
            or self.mpv_module is None
            or not self.video.get_realized()
        ):
            return False
        if self.video.get_allocated_width() <= 0 or self.video.get_allocated_height() <= 0:
            if attempts < 5:
                GLib.timeout_add(50, self.reset_video_render_context, attempts + 1)
            return False
        if self.mpv_render_context is not None:
            return False
        self.create_mpv_render_context(self.player, self.mpv_module)
        self.video.queue_render()
        return False

    def on_video_realize(self, _area: Gtk.GLArea) -> None:
        if (
            self.player is not None
            and self.mpv_render_context is None
            and self.mpv_module is not None
        ):
            self.schedule_video_render_context_reset()

    def on_video_render(self, area: Gtk.GLArea, _context: Gdk.GLContext) -> bool:
        if self.mpv_render_context is None:
            return True
        width = area.get_allocated_width() * area.get_scale_factor()
        height = area.get_allocated_height() * area.get_scale_factor()
        if width <= 0 or height <= 0:
            return True

        if self.libgl is None:
            self.log("mpv render skipped: libGL is unavailable")
            return True
        framebuffer = c_int()
        self.libgl.glGetIntegerv(0x8CA6, byref(framebuffer))
        try:
            # Tell mpv to update its internal state for the new frame
            if hasattr(self.mpv_render_context, "update"):
                self.mpv_render_context.update()
                
            self.mpv_render_context.render(
                opengl_fbo={
                    "fbo": framebuffer.value,
                    "w": int(width),
                    "h": int(height),
                    "internal_format": 0,
                },
                flip_y=True,
            )
            self.mpv_render_context.report_swap()
        except Exception as exc:
            self.log(f"mpv render failed: {exc}")
        finally:
            self.mpv_render_queued = False
        
        return True

    def on_video_unrealize(self, _area: Gtk.GLArea) -> None:
        self.free_mpv_render_context()

    def queue_video_render(self, generation: int) -> bool:
        if generation != self.mpv_render_generation:
            self.mpv_render_queued = False
            return False
        self.video.queue_render()
        return False

    def on_mpv_render_update(self, generation: int) -> None:
        if getattr(self, "mpv_render_queued", False):
            return
        self.mpv_render_queued = True
        GLib.idle_add(self.queue_video_render, generation)

    def get_gl_proc_address(self, _ctx: object, name: bytes) -> int:
        if self.libgl is not None:
            address = self.libgl.glXGetProcAddressARB(name)
            if address:
                return int(address)
        if self.libegl is not None:
            address = self.libegl.eglGetProcAddress(name)
            if address:
                return int(address)
        try:
            return int(c_void_p.in_dll(self.gl, name.decode("ascii")).value or 0)
        except (UnicodeDecodeError, ValueError):
            return 0

    def create_mpv_render_context(self, player: Any, mpv: Any) -> bool:
        if not self.video.get_realized():
            return True
        self.video.make_current()
        error = self.video.get_error()
        if error is not None:
            self.set_status(f"OpenGL error: {error.message}")
            self.log(f"gtk glarea error: {error.message}")
            return False

        self.mpv_get_proc_address = mpv.MpvGlGetProcAddressFn(
            self.get_gl_proc_address
        )
        try:
            self.mpv_render_generation += 1
            generation = self.mpv_render_generation
            self.mpv_render_context = mpv.MpvRenderContext(
                player,
                "opengl",
                opengl_init_params={
                    "get_proc_address": self.mpv_get_proc_address,
                },
                advanced_control=True,
            )
            self.mpv_render_context.update_cb = lambda: self.on_mpv_render_update(
                generation
            )
        except Exception as exc:
            self.set_status(f"Could not create mpv renderer: {exc}")
            self.log(f"mpv render context creation failed: {exc}")
            self.mpv_render_context = None
            return False
        return True

    def free_mpv_render_context(self) -> None:
        if self.mpv_render_context is None:
            return
        self.mpv_render_generation += 1
        try:
            self.mpv_render_context.update_cb = None
            if self.video.get_realized():
                self.video.make_current()
            self.mpv_render_context.free()
        except Exception as exc:
            self.log(f"mpv render context free failed: {exc}")
        self.mpv_render_context = None

    def create_player(self, playable: PlayableVideo) -> Any | None:
        locale.setlocale(locale.LC_NUMERIC, "C")
        try:
            import mpv
        except (ImportError, ModuleNotFoundError, OSError) as exc:
            self.set_status(
                "Missing mpv dependencies. Restart GTKTube to launch the dependency "
                "installer, then install Python requirements."
            )
            self.log(f"mpv import failed: {exc}")
            return None

        ytdl_format = os.environ.get(
            "GTKTUBE_YTDLP_FORMAT",
            QUALITY_FORMATS.get(playable.quality, QUALITY_FORMATS["720p"]),
        )
        player = None
        try:
            player = mpv.MPV(
                input_default_bindings=True,
                input_vo_keyboard=True,
                osc=True,
                vo="libmpv",
                ytdl=False,
                cache="yes",
                cache_on_disk=MPV_CACHE_ON_DISK,
                cache_secs=MPV_CACHE_SECS,
                demuxer_cache_dir=str(self.mpv_cache_dir),
                demuxer_cache_unlink_files=MPV_DEMUXER_CACHE_UNLINK_FILES,
                demuxer_readahead_secs=MPV_DEMUXER_READAHEAD_SECS,
                demuxer_max_bytes=MPV_DEMUXER_MAX_BYTES,
                demuxer_max_back_bytes=MPV_DEMUXER_MAX_BACK_BYTES,
                demuxer_seekable_cache="yes",
                ytdl_format=ytdl_format,
                log_handler=self.on_mpv_log,
                loglevel="warn" if self.verbose else "error",
            )
            self.mpv_module = mpv
            self.verbose_log(
                "mpv player created "
                f"version={getattr(player, 'mpv_version', 'unknown')} "
                f"video={playable.video.id} ytdl_format={ytdl_format!r} "
                f"cache_on_disk={MPV_CACHE_ON_DISK} "
                f"demuxer_cache_dir={self.mpv_cache_dir} "
                f"demuxer_cache_unlink_files={MPV_DEMUXER_CACHE_UNLINK_FILES} "
                f"cache_secs={MPV_CACHE_SECS} "
                f"demuxer_readahead_secs={MPV_DEMUXER_READAHEAD_SECS} "
                f"demuxer_max_bytes={MPV_DEMUXER_MAX_BYTES} "
                f"demuxer_max_back_bytes={MPV_DEMUXER_MAX_BACK_BYTES} "
                f"rss={self.process_rss_label()}"
            )
            player.register_event_callback(self.on_mpv_event)
            self.register_mpv_property_observers(player)
            if not self.create_mpv_render_context(player, mpv):
                self.log(f"mpv renderer unavailable video={playable.video.id}")
                self.unregister_mpv_property_observers(player)
                try:
                    player.unregister_event_callback(self.on_mpv_event)
                except (ValueError, AttributeError) as exc:
                    self.verbose_log(
                        f"mpv event callback unregister skipped: {exc}"
                    )
                player.terminate()
                return None
            return player
        except Exception as exc:
            if player is not None:
                self.unregister_mpv_property_observers(player)
                try:
                    player.unregister_event_callback(self.on_mpv_event)
                except (ValueError, AttributeError) as callback_exc:
                    self.verbose_log(
                        "mpv event callback unregister skipped: "
                        f"{callback_exc}"
                    )
                try:
                    player.terminate()
                except Exception as terminate_exc:
                    self.log(f"mpv terminate failed: {terminate_exc}")
            self.set_status(f"Could not create mpv player: {exc}")
            self.log(f"mpv player creation failed: {exc}")
            return None

    def on_mpv_log(self, level: str, prefix: str, text: str) -> None:
        message = text.strip()
        if not message:
            return
        log_message = f"mpv[{level}][{prefix}] {message}"
        if level in {"error", "fatal"}:
            self.log(log_message)
            if "HTTP error 403" in message:
                self.mpv_stream_error_message = (
                    "Video stream was rejected by YouTube (HTTP 403). "
                    "Try replaying the video to resolve a fresh stream URL."
                )
                GLib.idle_add(
                    self.retry_mpv_stream_open_error,
                    self.mpv_stream_error_message,
                )
            elif "Failed to open " in message and self.mpv_stream_error_message is None:
                self.mpv_stream_error_message = "Could not open video stream."
                GLib.idle_add(
                    self.retry_mpv_stream_open_error,
                    self.mpv_stream_error_message,
                )
        else:
            self.verbose_log(log_message)

    def on_mpv_event(self, event: Any) -> None:
        if self.mpv_module is None:
            return
        event_id = event.event_id
        if event_id == self.mpv_module.MpvEventID.START_FILE:
            self.verbose_log("mpv event start-file")
        elif event_id == self.mpv_module.MpvEventID.FILE_LOADED:
            self.verbose_log("mpv event file-loaded")
            self.mpv_file_loaded = True
            if self.pending_seek_seconds is not None:
                self.start_pending_seek_timer(delay_ms=100)
        elif event_id == self.mpv_module.MpvEventID.END_FILE:
            reason = getattr(event, "reason", "unknown")
            error = getattr(event, "error", None)
            message = (
                "mpv event end-file "
                f"reason={reason} "
                f"error={error}"
            )
            if self.mpv_end_file_failed(reason, error):
                self.log(message)
                GLib.idle_add(
                    self.retry_mpv_stream_open_error,
                    self.mpv_stream_error_message or "Playback failed.",
                )
                return
            else:
                self.verbose_log(message)
            GLib.idle_add(self.handle_mpv_end_file, "event")

    def register_mpv_property_observers(self, player: Any) -> None:
        self.mpv_property_observers = []
        self.mpv_observed_time_pos = None
        self.mpv_observed_duration = None
        names = (
            "eof-reached",
            "idle-active",
            "core-idle",
            "time-pos",
            "duration",
            "pause",
        )
        for name in names:
            def observer(
                property_name: str,
                value: object,
                observed_player: Any = player,
            ) -> None:
                self.on_mpv_property_changed(observed_player, property_name, value)

            try:
                player.observe_property(name, observer)
            except Exception as exc:
                self.verbose_log(f"mpv property observer failed name={name}: {exc}")
            else:
                self.mpv_property_observers.append((name, observer))

    def unregister_mpv_property_observers(self, player: Any) -> None:
        for name, observer in self.mpv_property_observers:
            try:
                player.unobserve_property(name, observer)
            except Exception as exc:
                self.verbose_log(
                    "mpv property observer unregister skipped "
                    f"name={name}: {exc}"
                )
        self.mpv_property_observers = []

    def on_mpv_property_changed(
        self,
        player: Any,
        property_name: str,
        value: object,
    ) -> None:
        if player is not self.player:
            return
        if property_name == "time-pos":
            try:
                self.mpv_observed_time_pos = (
                    None if value is None else float(value)
                )
            except (TypeError, ValueError):
                self.mpv_observed_time_pos = None
        elif property_name == "duration":
            try:
                self.mpv_observed_duration = (
                    None if value is None else float(value)
                )
            except (TypeError, ValueError):
                self.mpv_observed_duration = None

        should_log = property_name in {
            "eof-reached",
            "idle-active",
            "core-idle",
            "pause",
        }
        if should_log or (property_name == "time-pos" and value in (None, 0, 0.0)):
            self.verbose_log(
                "mpv property change "
                f"video={self.current_playable.video.id if self.current_playable else 'none'} "
                f"name={property_name} "
                f"value={value!r} "
                f"observed_time_pos={self.mpv_observed_time_pos!r} "
                f"observed_duration={self.mpv_observed_duration!r} "
                f"file_loaded={self.mpv_file_loaded} "
                f"end_handled={self.mpv_end_handled} "
                f"queue_count={self.video_queue.get_n_items()} "
                f"playlist_index={self.playlist_current_index}"
            )

        observed_near_end = self.mpv_observed_near_end()
        playback_observed = self.mpv_file_loaded or observed_near_end
        if self.mpv_end_handled or not playback_observed:
            return
        eof_signal = property_name == "eof-reached" and (
            bool(value) or (value is None and observed_near_end)
        )
        idle_signal = (
            property_name in {"idle-active", "core-idle"}
            and bool(value)
            and observed_near_end
        )
        if not eof_signal and not idle_signal:
            return
        self.verbose_log(
            "mpv eof detected from property observer "
            f"name={property_name} "
            f"value={value!r} "
            f"queue_count={self.video_queue.get_n_items()} "
            f"playlist_index={self.playlist_current_index}"
        )
        GLib.idle_add(self.handle_mpv_end_file, f"property:{property_name}")

    def mpv_observed_near_end(self) -> bool:
        if self.mpv_observed_time_pos is None or self.mpv_observed_duration is None:
            return False
        if self.mpv_observed_duration <= 0:
            return False
        return self.mpv_observed_time_pos >= max(
            0.0,
            self.mpv_observed_duration - 10.0,
        )

    def mpv_end_file_failed(self, reason: object, error: object) -> bool:
        reason_text = str(reason).lower()
        if "eof" in reason_text:
            return False
        if error in (None, 0, "", "success"):
            return False
        return True

    def on_mpv_end_file(self) -> None:
        if self.video_queue.get_n_items() > 0:
            self.verbose_log(
                "mpv end-file advancing to queued video "
                f"queue_count={self.video_queue.get_n_items()}"
            )
            self.play_next_in_queue()
            return
        next_playlist_index = self.playlist_next_index()
        if next_playlist_index is not None:
            self.verbose_log("mpv end-file advancing playlist")
            self.play_playlist_item(next_playlist_index)
            return
        self.verbose_log("mpv end-file stopping player")
        self.stop_pipeline(restore_stack=False, keep_player_visible=True)

    def handle_mpv_end_file(self, source: str) -> bool:
        if self.mpv_end_handled:
            self.verbose_log(
                "mpv eof handler ignored "
                f"source={source} already_handled=True"
            )
            return False
        self.mpv_end_handled = True
        self.verbose_log(
            "mpv eof handler "
            f"source={source} "
            f"queue_count={self.video_queue.get_n_items()} "
            f"playlist_index={self.playlist_current_index} "
            f"current_video={self.current_playable.video.id if self.current_playable else 'none'}"
        )
        self.uninhibit_playback("eof-handler")
        self.on_mpv_end_file()
        return False

    def playlist_previous_index(self) -> int | None:
        if self.playlist_current_index is None:
            return None
        previous_idx = self.playlist_current_index - 1
        while previous_idx >= 0:
            if previous_idx not in self.playlist_skip_set:
                return previous_idx
            previous_idx -= 1
        return None

    def playlist_next_index(self) -> int | None:
        if self.playlist_current_index is None:
            return None
        items_total = self.playlist_store.get_n_items()
        next_idx = self.playlist_current_index + 1
        while next_idx < items_total:
            if next_idx not in self.playlist_skip_set:
                return next_idx
            next_idx += 1
        return None

    def update_transport_navigation_buttons(self) -> None:
        if not hasattr(self, "previous_button") or not hasattr(self, "next_button"):
            return
        self.previous_button.set_visible(self.playlist_previous_index() is not None)
        self.next_button.set_visible(
            self.video_queue.get_n_items() > 0
            or self.playlist_next_index() is not None
        )

    def on_transport_items_changed(self, *_args: object) -> None:
        self.update_transport_navigation_buttons()

    def on_previous_clicked(self, _button: Gtk.Button) -> None:
        self.play_previous_in_playlist()

    def on_next_clicked(self, _button: Gtk.Button) -> None:
        if self.video_queue.get_n_items() > 0:
            self.play_next_in_queue()
        else:
            self.play_next_in_playlist()

    def play_next_in_queue(self) -> None:
        if self.video_queue.get_n_items() > 0:
            item = self.video_queue.get_item(0)
            self.verbose_log(
                "up next selecting next video "
                f"video={item.video.id} "
                f"queue_count_before={self.video_queue.get_n_items()}"
            )
            self.video_queue.remove(0)
            self.queue_pane.set_visible(self.video_queue.get_n_items() > 0)
            self.update_transport_navigation_buttons()
            self.play_video(item.video, hide_sidebar=False)

    def play_previous_in_playlist(self) -> None:
        previous_idx = self.playlist_previous_index()
        if previous_idx is not None:
            self.play_playlist_item(previous_idx)

    def play_next_in_playlist(self) -> None:
        next_idx = self.playlist_next_index()
        if next_idx is not None:
            self.play_playlist_item(next_idx)

    def stop_pipeline(
        self,
        restore_stack: bool = True,
        keep_player_visible: bool = False,
    ) -> None:
        self.uninhibit_playback("stop-pipeline")
        if self.video_fullscreen:
            self.close_video_fullscreen()
        if not keep_player_visible:
            if restore_stack:
                self.hide_miniplayer()
            else:
                self.miniplayer.set_visible(False)
        if self.player is None:
            self.free_mpv_render_context()
            return
        player = self.player
        self.player = None
        self.free_mpv_render_context()
        self.unregister_mpv_property_observers(player)
        try:
            player.unregister_event_callback(self.on_mpv_event)
        except (ValueError, AttributeError) as exc:
            self.verbose_log(f"mpv event callback unregister skipped: {exc}")
        try:
            player.terminate()
        except Exception as exc:
            self.log(f"mpv terminate failed: {exc}")
        finally:
            self.release_unused_native_memory()
        self.mpv_module = None
        self.active_caption_url = None
        self.range_start_seconds = None
        self.pending_seek_seconds = None
        self.last_playback_diagnostics_at = 0.0
        self.last_playback_diagnostics_values = {}
        self.last_playback_diagnostics_paused = False
        self.mpv_file_loaded = False
        self.mpv_stream_error_message = None
        self.mpv_end_handled = False
        self.update_play_pause_button()
        self.update_transport_navigation_buttons()
        self.verbose_log(f"mpv player stopped rss={self.process_rss_label()}")

    def process_rss_bytes(self) -> int | None:
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as status:
                for line in status:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) * 1024
        except OSError:
            return None
        return None

    def process_rss_label(self) -> str:
        rss = self.process_rss_bytes()
        if rss is None:
            return "unknown"
        return f"{rss / (1024 * 1024):.1f}MiB"

    def release_unused_native_memory(self) -> None:
        try:
            import ctypes

            libc = ctypes.CDLL(None)
            trim = getattr(libc, "malloc_trim", None)
            if trim is not None:
                trim(0)
        except Exception:
            pass

    def on_close_player_clicked(self, _button: Gtk.Button) -> None:
        self.close_current_video()

    def on_restore_player_clicked(self, _button: Gtk.Button) -> None:
        self.navigate_to(ViewState("player"))

    def close_current_video(self) -> None:
        self.playback_request_id += 1
        self.flush_watch_range()
        self.stop_pipeline()
        self.current_playable = None
        self.active_caption_url = None
        self.set_player_loading(False)
        self.player_title.set_text("No video loaded")
        self.player_meta.set_text("")
        self.miniplayer_title.set_text("")
        self.miniplayer_meta.set_text("")
        self.set_description_text("")
        if self.description_window is not None:
            self.description_window.close()
        self.update_subscribe_check(
            Video(id="", channel_id="", title="", url="")
        )
        self.update_player_share_button()
        self.update_caption_tracks(None)
        self.update_chapters(None)

    def on_play_pause_clicked(self, _button: Gtk.Button) -> None:
        self.toggle_play_pause()

    def on_video_clicked(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
    ) -> None:
        self.toggle_play_pause()

    def on_current_video_context_menu(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
    ) -> None:
        if self.current_playable is None:
            return
        self.show_video_context_menu(self.video, self.current_playable.video, x, y)

    def toggle_play_pause(self) -> None:
        if self.player is None:
            if self.current_playable is not None:
                self.start_playback(self.current_playable, resume_position=0)
            return
        try:
            self.player.pause = not bool(getattr(self.player, "pause", False))
        except Exception as exc:
            self.log(f"mpv pause toggle failed: {exc}")
        self.update_play_pause_button()
        self.update_playback_inhibition()

    def update_play_pause_button(self) -> None:
        if self.player is None:
            self.play_pause_icon.set_from_icon_name("media-playback-start-symbolic")
            self.play_pause_button.set_tooltip_text("Play")
            return
        if bool(getattr(self.player, "pause", False)):
            self.play_pause_icon.set_from_icon_name("media-playback-start-symbolic")
            self.play_pause_button.set_tooltip_text("Play")
        else:
            self.play_pause_icon.set_from_icon_name("media-playback-pause-symbolic")
            self.play_pause_button.set_tooltip_text("Pause")

    def update_playback_inhibition(self) -> None:
        if self.player is None or bool(getattr(self.player, "pause", False)):
            self.uninhibit_playback("player-paused-or-missing")
            return
        self.inhibit_playback()

    def inhibit_playback(self) -> None:
        if self.playback_inhibit_cookie is not None:
            return
        app = self.get_application()
        if app is None:
            return
        cookie = app.inhibit(
            self,
            Gtk.ApplicationInhibitFlags.IDLE,
            "GTKTube is playing video",
        )
        if cookie:
            self.playback_inhibit_cookie = int(cookie)
            self.verbose_log("screensaver inhibited for playback")

    def uninhibit_playback(self, reason: str = "unspecified") -> bool:
        cookie = self.playback_inhibit_cookie
        if cookie is None:
            return False
        app = self.get_application()
        if app is not None:
            app.uninhibit(cookie)
        self.playback_inhibit_cookie = None
        self.verbose_log(f"screensaver inhibition released reason={reason}")
        return False

    def on_fullscreen_clicked(self, _button: Gtk.Button) -> None:
        if self.video_fullscreen:
            self.close_video_fullscreen()
        else:
            self.open_video_fullscreen()

    def open_video_fullscreen(self) -> None:
        if self.video_fullscreen:
            return

        self.fullscreen_return_view = (
            self.current_view
            if self.current_view is not None and self.current_view.page != "player"
            else None
        )
        if self.fullscreen_return_view is not None:
            self.show_full_player()
            self.stack.set_visible_child_name("player")

        self.video_fullscreen = True
        self.header.set_visible(False)
        self.sidebar.set_visible(False)
        self.fullscreen_queue_pane_visible = self.queue_pane.get_visible()
        self.queue_pane.set_visible(False)
        self.player_metadata.set_visible(False)
        self.fullscreen_icon.set_from_icon_name("view-restore-symbolic")
        self.fullscreen_button.set_tooltip_text("Exit fullscreen video")
        self.fullscreen()

    def close_video_fullscreen(self) -> None:
        if not self.video_fullscreen:
            return
        self.video_fullscreen = False
        self.header.set_visible(True)
        self.sidebar.set_visible(True)
        self.queue_pane.set_visible(
            self.fullscreen_queue_pane_visible and self.video_queue.get_n_items() > 0
        )
        self.fullscreen_queue_pane_visible = False
        self.player_metadata.set_visible(True)
        self.fullscreen_icon.set_from_icon_name("view-fullscreen-symbolic")
        self.fullscreen_button.set_tooltip_text("Fullscreen video")
        self.unfullscreen()
        return_view = self.fullscreen_return_view
        self.fullscreen_return_view = None
        if return_view is not None:
            self.apply_view_state(return_view)
        else:
            self.show_full_player()

    def on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if self.video_fullscreen and keyval == Gdk.KEY_Escape:
            self.close_video_fullscreen()
            return True
        if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_o, Gdk.KEY_O):
            self.show_open_url_dialog()
            return True
        if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            self.close()
            return True
        if self.focus_is_text_input():
            return False
        if self.handle_navigation_shortcut(keyval, state):
            return True
        if self.current_view is None or self.current_view.page != "player":
            return False
        return self.handle_player_shortcut(keyval, state)

    def focus_is_text_input(self) -> bool:
        focus = self.get_focus()
        while focus is not None:
            if isinstance(focus, Gtk.Editable) or isinstance(focus, Gtk.TextView):
                return True
            focus = focus.get_parent()
        return False

    def handle_navigation_shortcut(
        self, keyval: int, state: Gdk.ModifierType
    ) -> bool:
        if not state & Gdk.ModifierType.ALT_MASK:
            return False
        if keyval == Gdk.KEY_Left:
            self.go_back()
            return True
        if keyval == Gdk.KEY_Right:
            self.go_forward()
            return True
        return False

    def handle_player_shortcut(
        self, keyval: int, state: Gdk.ModifierType = Gdk.ModifierType(0)
    ) -> bool:
        if keyval in (Gdk.KEY_space, Gdk.KEY_k, Gdk.KEY_K):
            self.on_play_pause_clicked(self.play_pause_button)
            return True
        if keyval == Gdk.KEY_less or (
            keyval == Gdk.KEY_comma and state & Gdk.ModifierType.SHIFT_MASK
        ):
            self.adjust_playback_rate(-0.25)
            return True
        if keyval == Gdk.KEY_greater or (
            keyval == Gdk.KEY_period and state & Gdk.ModifierType.SHIFT_MASK
        ):
            self.adjust_playback_rate(0.25)
            return True
        if keyval == Gdk.KEY_Left:
            self.seek_relative(-5)
            return True
        if keyval == Gdk.KEY_Right:
            self.seek_relative(5)
            return True
        if keyval in (Gdk.KEY_j, Gdk.KEY_J):
            self.seek_relative(-10)
            return True
        if keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self.seek_relative(10)
            return True
        if keyval == Gdk.KEY_Home:
            self.seek_media(0)
            return True
        if keyval == Gdk.KEY_End:
            duration = self.current_duration_seconds()
            if duration > 0:
                self.seek_media(duration)
            return True
        if keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.on_fullscreen_clicked(self.fullscreen_button)
            return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self.toggle_subtitles()
            return True
        if Gdk.KEY_0 <= keyval <= Gdk.KEY_9:
            duration = self.current_duration_seconds()
            if duration > 0:
                digit = keyval - Gdk.KEY_0
                self.seek_media(int(duration * digit / 10))
            return True
        return False

    def selected_quality(self) -> str:
        return self.preferred_quality

    def on_quality_changed(self, _combo: Gtk.ComboBoxText) -> None:
        if self.updating_quality:
            return
        quality = self.quality_combo.get_active_id()
        if quality:
            self.preferred_quality = quality

        if self.current_playable is None:
            return
        position = self.current_position_seconds()
        video = self.current_playable.video
        quality = self.selected_quality()
        self.flush_watch_range()
        self.run_task(
            f"Switching to {quality}...",
            lambda: self.service.play_video(video, quality=quality, record_play=False),
            lambda playable: self.load_playable_at(playable, position),
        )

    def speed_id(self, rate: float) -> str:
        return f"{rate:.2f}"

    def speed_label(self, rate: float) -> str:
        return f"{rate:g}x"

    def selected_speed(self) -> float:
        active_id = self.speed_combo.get_active_id()
        if active_id is None:
            return self.playback_rate
        try:
            return float(active_id)
        except ValueError:
            return self.playback_rate

    def on_speed_changed(self, _combo: Gtk.ComboBoxText) -> None:
        if self.updating_speed:
            return
        self.set_playback_rate(self.selected_speed())

    def update_caption_tracks(self, playable: PlayableVideo | None) -> None:
        previous_selection = self.selected_caption_id
        self.updating_captions = True
        self.clear_caption_rows()
        self.caption_list.append(self.caption_row("Subtitles: Off"))
        captions = playable.captions if playable and playable.captions else []
        for track in captions:
            self.caption_list.append(self.caption_row(track.label))
        active_id = (
            previous_selection
            if previous_selection != "off"
            and any(track.id == previous_selection for track in captions)
            else "off"
        )
        self.selected_caption_id = active_id
        self.update_caption_selection()
        self.caption_button.set_visible(bool(captions))
        self.caption_button.set_sensitive(bool(captions))
        self.updating_captions = False

    def clear_caption_rows(self) -> None:
        child = self.caption_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.caption_list.remove(child)
            child = next_child

    def caption_row(self, label: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        title_label = Gtk.Label(label=label, xalign=0, hexpand=True)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(title_label)
        row.set_child(box)
        return row

    def update_caption_selection(self) -> None:
        captions = self.current_playable.captions if self.current_playable else []
        selected_index = 0
        if self.selected_caption_id != "off":
            for index, track in enumerate(captions, start=1):
                if track.id == self.selected_caption_id:
                    selected_index = index
                    break
        child = self.caption_list.get_first_child()
        while child is not None:
            if child.get_index() == selected_index:
                self.caption_list.select_row(child)
                break
            child = child.get_next_sibling()
        selected_track = self.selected_caption_track()
        if selected_track is None:
            self.caption_button.set_tooltip_text("Subtitles: Off")
        else:
            self.caption_button.set_tooltip_text(f"Subtitles: {selected_track.label}")

    def toggle_subtitles(self) -> None:
        captions = self.current_playable.captions if self.current_playable else []
        if not captions:
            return
        if self.selected_caption_id == "off":
            self.selected_caption_id = captions[0].id
        else:
            self.selected_caption_id = "off"
        self.update_caption_selection()
        self.apply_selected_caption()

    def selected_caption_track(self) -> CaptionTrack | None:
        if self.current_playable is None or self.selected_caption_id == "off":
            return None
        for track in self.current_playable.captions or []:
            if track.id == self.selected_caption_id:
                return track
        return None

    def on_caption_row_activated(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow
    ) -> None:
        if self.updating_captions:
            return
        captions = self.current_playable.captions if self.current_playable else []
        index = row.get_index()
        if index <= 0:
            self.selected_caption_id = "off"
        elif index - 1 < len(captions):
            self.selected_caption_id = captions[index - 1].id
        else:
            return
        self.caption_popover.popdown()
        self.update_caption_selection()
        self.apply_selected_caption()

    def apply_selected_caption(self) -> None:
        if self.player is None:
            return
        track = self.selected_caption_track()
        if track is None:
            self.active_caption_url = None
            try:
                self.player.sid = "no"
            except Exception as exc:
                self.log(f"mpv caption disable failed: {exc}")
            return
        if self.active_caption_url == track.url:
            return
        self.active_caption_url = track.url
        future = self.submit_background(self.download_caption_track, track)
        if future is None:
            return

        def done() -> bool:
            if self.cleaned_up:
                return False
            try:
                path = future.result()
            except Exception as exc:
                if self.active_caption_url == track.url:
                    self.active_caption_url = None
                    self.log(f"caption download failed: {exc}")
                return False
            if self.player is None or self.active_caption_url != track.url:
                return False
            try:
                self.player.command("sub-add", str(path), "select", track.label)
            except Exception as exc:
                self.log(f"mpv caption load failed: {exc}")
            return False

        self.schedule_background_finish(future, done)

    def download_caption_track(self, track: CaptionTrack) -> Path:
        path = self.caption_cache_path(track)
        if path.exists() and path.stat().st_size > 0:
            return path
        request = urllib.request.Request(
            track.url,
            headers={
                "User-Agent": "GTKTube/0.1",
                "Accept": "text/vtt,text/*,*/*;q=0.5",
            },
        )
        tmp_path = path.with_suffix(".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(4_000_000)
        except (TimeoutError, urllib.error.URLError) as exc:
            raise RuntimeError(str(exc)) from exc
        if not data:
            raise RuntimeError("empty caption response")
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
        return path

    def caption_cache_path(self, track: CaptionTrack) -> Path:
        digest = hashlib.sha256(track.url.encode("utf-8")).hexdigest()
        return self.caption_dir / f"{digest}.vtt"

    def adjust_playback_rate(self, delta: float) -> None:
        next_rate = min(PLAYBACK_RATES, key=lambda rate: abs(rate - self.playback_rate))
        next_rate = round(next_rate + delta, 2)
        next_rate = max(PLAYBACK_RATES[0], min(PLAYBACK_RATES[-1], next_rate))
        self.set_playback_rate(next_rate)

    def set_playback_rate(self, rate: float) -> None:
        rate = round(rate, 2)
        if rate not in PLAYBACK_RATES:
            rate = min(PLAYBACK_RATES, key=lambda candidate: abs(candidate - rate))
        self.playback_rate = rate
        self.updating_speed = True
        self.speed_combo.set_active_id(self.speed_id(rate))
        self.updating_speed = False
        if self.player is not None:
            try:
                self.player.speed = rate
                self.last_playback_diagnostics_at = 0.0
                self.last_playback_diagnostics_values = {}
                self.last_playback_diagnostics_paused = False
            except Exception as exc:
                self.log(f"mpv speed change failed: {exc}")
        self.set_status(f"Playback speed {self.speed_label(rate)}")

    def load_playable_at(self, playable: PlayableVideo, position: int) -> None:
        self.load_playable(playable, resume_position=position)

    def queue_seek_media(self, seconds: int) -> None:
        self.pending_seek_seconds = seconds
        self.pending_seek_attempts = 0
        self.start_pending_seek_timer()

    def start_pending_seek_timer(self, delay_ms: int = 250) -> None:
        if self.pending_seek_timer_active:
            return
        self.pending_seek_timer_active = True
        GLib.timeout_add(delay_ms, self.flush_pending_seek)

    def flush_pending_seek(self) -> bool:
        if self.pending_seek_seconds is None:
            self.pending_seek_timer_active = False
            return False
        self.pending_seek_attempts += 1
        if self.seek_media_impl(
            self.pending_seek_seconds,
            defer_until_ready=False,
            user_initiated=False,
        ):
            self.pending_seek_seconds = None
            self.pending_seek_timer_active = False
            return False
        if self.pending_seek_attempts >= 40:
            self.log(f"mpv deferred seek abandoned: {self.pending_seek_seconds}")
            self.pending_seek_seconds = None
            self.pending_seek_timer_active = False
            return False
        return True

    def seek_media(
        self,
        seconds: int,
        user_initiated: bool = True,
        precision: str = "keyframes",
    ) -> bool:
        return self.seek_media_impl(
            seconds,
            defer_until_ready=True,
            user_initiated=user_initiated,
            precision=precision,
        )

    def seek_media_impl(
        self,
        seconds: int,
        defer_until_ready: bool,
        user_initiated: bool = False,
        precision: str = "keyframes",
    ) -> bool:
        if self.player is not None:
            self.flush_watch_range()
            duration = self.current_duration_seconds()
            if duration <= 0:
                if defer_until_ready:
                    self.queue_seek_media(seconds)
                return False
            if duration > 0:
                seconds = max(0, min(seconds, duration))
            try:
                self.player.seek(seconds, reference="absolute", precision=precision)
            except Exception as exc:
                if defer_until_ready:
                    self.queue_seek_media(seconds)
                else:
                    self.log(f"mpv seek failed: {exc}")
                return False
            if user_initiated:
                self.suppress_sponsorblock_for_seek(seconds)
            self.range_start_seconds = seconds
            self.update_playback_controls()
            return True
        return False

    def seek_relative(self, delta_seconds: int) -> None:
        self.seek_media(self.current_position_seconds() + delta_seconds)

    def on_scrub_changed(
        self,
        _range: Gtk.Range,
        _scroll: Gtk.ScrollType,
        value: float,
    ) -> bool:
        if self.updating_scrubber:
            return False
        self.seek_media(int(value))
        return False

    def flush_watch_range(self) -> bool:
        if self.cleaned_up:
            return False
        if self.current_playable is None or self.player is None:
            return True
        current = self.current_position_seconds()
        start = self.range_start_seconds
        if start is not None and current > start:
            self.service.record_watch_range(self.current_playable.video.id, start, current)
            self.range_start_seconds = current
            self.reload_history()
            self.reload_visible_video_grid()
        return True

    def reload_visible_video_grid(self) -> None:
        if self.current_view is None:
            return
        if self.current_view.channel_id is not None:
            self.populate_video_grid(
                self.feed_grid,
                self.service.repository.channel_videos(
                    self.current_view.channel_id,
                    self.channel_video_limits.get(self.current_view.channel_id, 30),
                ),
            )
            return
        if self.current_view.page == "feed":
            self.reload_feed()
            return
        if self.current_view.page == "watch_later":
            self.reload_watch_later()
            return
        if self.current_view.page == "history":
            self.reload_history()
            return

    def current_position_seconds(self) -> int:
        if self.player is None:
            return 0
        try:
            position = getattr(self.player, "time_pos", None)
        except Exception:
            return 0
        if position is None:
            return 0
        return max(0, int(position))

    def current_duration_seconds(self) -> int:
        if self.player is None:
            return 0
        try:
            duration = getattr(self.player, "duration", None)
        except Exception:
            return 0
        if duration is None:
            return 0
        return max(0, int(duration))

    def update_playback_controls(self) -> bool:
        if self.cleaned_up:
            return False
        current = self.current_position_seconds()
        duration = self.current_duration_seconds()
        upper = max(duration, current, 1)

        self.updating_scrubber = True
        self.scrubber.set_range(0, upper)
        self.scrubber.set_value(current)
        self.updating_scrubber = False

        self.elapsed_label.set_text(self.format_time(current))
        self.duration_label.set_text(self.format_time(duration))
        self.update_play_pause_button()
        self.update_transport_navigation_buttons()
        self.update_active_chapter(current)
        self.sponsorblock_timeline.queue_draw()
        self.maybe_log_playback_diagnostics(current, duration)
        self.maybe_log_sponsorblock_skip_ready(current)
        self.maybe_skip_sponsorblock_segment(current)
        return True

    def maybe_log_playback_diagnostics(self, current: int, duration: int) -> None:
        if not self.verbose or self.player is None:
            return
        if not self.mpv_file_loaded or duration <= 0:
            return
        speed = self.mpv_property("speed")
        if self.playback_rate == 1.0 and speed in (None, 1, 1.0):
            return
        now = time.monotonic()
        if now - self.last_playback_diagnostics_at < 5:
            return

        names = (
            "speed",
            "pause",
            "time-pos",
            "duration",
            "cache-buffering-state",
            "demuxer-cache-duration",
            "demuxer-cache-time",
            "demuxer-cache-state",
            "avsync",
            "mistimed-frame-count",
            "vo-delayed-frame-count",
            "decoder-frame-drop-count",
            "frame-drop-count",
        )
        values = {
            name: self.mpv_property(name)
            for name in names
        }
        paused = bool(values.get("pause"))
        if paused and self.last_playback_diagnostics_paused:
            self.last_playback_diagnostics_at = now
            return

        delta_names = (
            "mistimed-frame-count",
            "vo-delayed-frame-count",
            "decoder-frame-drop-count",
            "frame-drop-count",
        )
        parts = [
            f"video={self.current_playable.video.id if self.current_playable else 'none'}",
            f"ui_rate={self.playback_rate:g}",
            f"ui_pos={current}/{duration}",
        ]
        for name, value in values.items():
            if value is not None:
                parts.append(f"{name}={value!r}")
                previous = self.last_playback_diagnostics_values.get(name)
                if name in delta_names and isinstance(value, int) and isinstance(previous, int):
                    parts.append(f"{name}-delta={value - previous}")
        self.last_playback_diagnostics_at = now
        self.last_playback_diagnostics_values = values
        self.last_playback_diagnostics_paused = paused
        self.verbose_log("mpv playback diagnostics " + " ".join(parts))

    def mpv_property(self, name: str) -> object | None:
        if self.player is None:
            return None
        try:
            if hasattr(self.player, "get_property"):
                return self.player.get_property(name)
        except Exception:
            pass
        try:
            return getattr(self.player, name.replace("-", "_"))
        except Exception:
            return None

    def mpv_float_property(self, name: str) -> float | None:
        value = self.mpv_property(name)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
