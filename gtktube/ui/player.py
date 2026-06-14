from __future__ import annotations

import locale
import os
import re
from ctypes import byref, c_int, c_void_p
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from gtktube.extractors.youtube import QUALITY_FORMATS
from gtktube.models import PlayableVideo, Video
from gtktube.ui.types import ViewState


PLAYBACK_RATES = [rate / 100 for rate in range(25, 401, 25)]
URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>\"]+")


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

    def play_video(self, video: Video) -> None:
        self.navigate_to(ViewState("player"))
        quality = self.selected_quality()
        self.verbose_log(
            "playback requested "
            f"video={video.id} quality={quality} url={video.url}"
        )
        self.run_task(
            "Resolving video...",
            lambda: self.service.play_video(video, quality=quality),
            self.load_playable,
        )

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
        self.update_subscribe_check(playable.video)
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
            load_options = {}
            if playable.audio_url:
                load_options["audio_file"] = playable.audio_url
            self.player.loadfile(playable.stream_url, **load_options)
        except Exception as exc:
            self.set_status(f"Playback error: {exc}")
            self.log(f"mpv loadfile failed {stream_context}: {exc}")
            self.stop_pipeline()
            return
        try:
            self.player.pause = False
            self.player.speed = self.playback_rate
            self.verbose_log(f"mpv play command accepted video={playable.video.id}")
        except Exception as exc:
            self.set_status(f"Playback error: {exc}")
            self.log(f"mpv playback option failed {stream_context}: {exc}")
            self.stop_pipeline()
            return

        resume = (
            resume_position
            if resume_position is not None
            else self.service.repository.resume_position(playable.video.id)
        )
        if resume > 0:
            self.verbose_log(
                "queueing resume seek after file-loaded "
                f"video={playable.video.id} seconds={resume}"
            )
            self.queue_seek_after_file_loaded(resume)
        self.range_start_seconds = self.current_position_seconds()
        self.show_full_player()
        self.select_nav_page("player")
        self.stack.set_visible_child_name("player")

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

    def set_description_text(self, text: str) -> None:
        buffer = self.player_description.get_buffer()
        buffer.set_text(text)
        self.description_link_tags = {}
        self.description_link_generation += 1
        for index, match in enumerate(URL_PATTERN.finditer(text)):
            start_offset = match.start()
            end_offset = self.link_end_offset(text, match.end())
            url = text[start_offset:end_offset]
            tag_name = f"description-link-{self.description_link_generation}-{index}"
            tag = buffer.create_tag(
                tag_name,
                underline=Pango.Underline.SINGLE,
                foreground="#62a0ea",
            )
            start = buffer.get_iter_at_offset(start_offset)
            end = buffer.get_iter_at_offset(end_offset)
            buffer.apply_tag(tag, start, end)
            self.description_link_tags[tag_name] = self.normalized_url(url)

    def link_end_offset(self, text: str, end: int) -> int:
        while end > 0 and text[end - 1] in ".,;:!?)]}":
            end -= 1
        return end

    def on_description_clicked(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
    ) -> None:
        buffer_x, buffer_y = self.player_description.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET,
            int(x),
            int(y),
        )
        found, text_iter = self.player_description.get_iter_at_location(
            buffer_x,
            buffer_y,
        )
        if not found:
            return
        for tag in text_iter.get_tags():
            name = tag.props.name
            uri = self.description_link_tags.get(name)
            if uri:
                Gtk.show_uri(self, uri, Gdk.CURRENT_TIME)
                return

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
        try:
            player = mpv.MPV(
                input_default_bindings=True,
                input_vo_keyboard=True,
                osc=True,
                vo="libmpv",
                ytdl=False,
                cache="yes",
                cache_secs=60,
                demuxer_readahead_secs=20,
                demuxer_max_bytes="300MiB",
                demuxer_max_back_bytes="100MiB",
                demuxer_seekable_cache="yes",
                ytdl_format=ytdl_format,
                log_handler=self.on_mpv_log,
                loglevel="warn" if self.verbose else "error",
            )
            self.mpv_module = mpv
            self.verbose_log(
                "mpv player created "
                f"version={getattr(player, 'mpv_version', 'unknown')} "
                f"video={playable.video.id} ytdl_format={ytdl_format!r}"
            )
            player.register_event_callback(self.on_mpv_event)
            if not self.create_mpv_render_context(player, mpv):
                self.log(f"mpv renderer unavailable video={playable.video.id}")
                player.terminate()
                return None
            return player
        except Exception as exc:
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
            if self.pending_seek_seconds is not None:
                self.start_pending_seek_timer(delay_ms=100)
        elif event_id == self.mpv_module.MpvEventID.END_FILE:
            message = (
                "mpv event end-file "
                f"reason={getattr(event, 'reason', 'unknown')} "
                f"error={getattr(event, 'error', None)}"
            )
            if getattr(event, "error", None):
                self.log(message)
            else:
                self.verbose_log(message)
            GLib.idle_add(self.on_mpv_end_file)

    def on_mpv_end_file(self) -> None:
        if self.video_queue.get_n_items() > 0:
            self.play_next_in_queue()

    def play_next_in_queue(self) -> None:
        if self.video_queue.get_n_items() > 0:
            item = self.video_queue.get_item(0)
            self.video_queue.remove(0)
            self.queue_pane.set_visible(self.video_queue.get_n_items() > 0)
            self.play_video(item.video)

    def stop_pipeline(self, restore_stack: bool = True) -> None:
        if self.video_fullscreen:
            self.close_video_fullscreen()
        if restore_stack:
            self.hide_miniplayer()
        else:
            self.miniplayer.set_visible(False)
        if self.player is None:
            self.free_mpv_render_context()
            return
        self.free_mpv_render_context()
        try:
            self.player.unregister_event_callback(self.on_mpv_event)
        except (ValueError, AttributeError) as exc:
            self.verbose_log(f"mpv event callback unregister skipped: {exc}")
        try:
            self.player.terminate()
        except Exception as exc:
            self.log(f"mpv terminate failed: {exc}")
        self.player = None
        self.mpv_module = None
        self.range_start_seconds = None
        self.pending_seek_seconds = None
        self.update_play_pause_button()

    def on_close_player_clicked(self, _button: Gtk.Button) -> None:
        self.close_current_video()

    def on_restore_player_clicked(self, _button: Gtk.Button) -> None:
        self.navigate_to(ViewState("player"))

    def close_current_video(self) -> None:
        self.flush_watch_range()
        self.stop_pipeline()
        self.current_playable = None
        self.player_title.set_text("No video loaded")
        self.player_meta.set_text("")
        self.miniplayer_title.set_text("")
        self.miniplayer_meta.set_text("")
        self.set_description_text("")
        self.update_subscribe_check(
            Video(id="", channel_id="", title="", url="")
        )
        self.update_player_share_button()

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

    def toggle_play_pause(self) -> None:
        if self.player is None:
            return
        try:
            self.player.pause = not bool(getattr(self.player, "pause", False))
        except Exception as exc:
            self.log(f"mpv pause toggle failed: {exc}")
        self.update_play_pause_button()

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
            except Exception as exc:
                self.log(f"mpv speed change failed: {exc}")
        self.set_status(f"Playback speed {self.speed_label(rate)}")

    def load_playable_at(self, playable: PlayableVideo, position: int) -> None:
        self.load_playable(playable, resume_position=position)

    def queue_seek_media(self, seconds: int) -> None:
        self.pending_seek_seconds = seconds
        self.pending_seek_attempts = 0
        self.start_pending_seek_timer()

    def queue_seek_after_file_loaded(self, seconds: int) -> None:
        self.pending_seek_seconds = seconds
        self.pending_seek_attempts = 0

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
        self.maybe_log_sponsorblock_skip_ready(current)
        self.maybe_skip_sponsorblock_segment(current)
        return True
