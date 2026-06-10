from __future__ import annotations

import hashlib
import json
import locale
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from ctypes import CDLL, POINTER, byref, c_char_p, c_int, c_void_p
from ctypes.util import find_library
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from gtktube.extractors.youtube import ExtractorError, QUALITY_FORMATS
from gtktube.models import Channel, PlayableVideo, SearchResults, Video
from gtktube.paths import AppPaths
from gtktube.services.library import LibraryService


T = TypeVar("T")

PLAYBACK_RATES = [rate / 100 for rate in range(25, 401, 25)]
URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>\"]+")


APP_CSS = """
.sidebar {
  background: alpha(currentColor, 0.04);
  border-right: 1px solid alpha(currentColor, 0.14);
}

.sidebar-list {
  background: transparent;
  padding: 8px 6px;
}

.nav-row {
  border-radius: 7px;
  margin: 1px 0;
  padding: 7px 8px;
}

.channel-nav-row {
  border-radius: 6px;
  margin: 0;
  padding: 5px 8px 5px 28px;
}

.channel-nav-label {
  font-size: 0.92em;
}

.miniplayer {
  background: alpha(currentColor, 0.08);
  border-top: 1px solid alpha(currentColor, 0.18);
  padding: 8px 12px;
}

.queue-pane {
  background: alpha(currentColor, 0.02);
  border-left: 1px solid alpha(currentColor, 0.14);
}

.queue-row {
  padding: 6px;
  border-bottom: 1px solid alpha(currentColor, 0.06);
}

.queue-row:last-child {
  border-bottom: none;
}
"""


@dataclass(frozen=True)
class ViewState:
    page: str
    channel_id: str | None = None
    channel_title: str | None = None


class GTKTubeApplication(Gtk.Application):
    def __init__(self, service: LibraryService, paths: AppPaths):
        super().__init__(
            application_id="local.gtktube.GTKTube",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.service = service
        self.paths = paths

    def do_activate(self) -> None:
        window = MainWindow(self, self.service, self.paths)
        window.present()

    def do_shutdown(self) -> None:
        for window in self.get_windows():
            if isinstance(window, MainWindow):
                window.cleanup()
        Gtk.Application.do_shutdown(self)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(
        self, app: GTKTubeApplication, service: LibraryService, paths: AppPaths
    ):
        super().__init__(application=app, title="GTKTube")
        self.service = service
        self.paths = paths
        self.thumbnail_dir = paths.cache_dir / "thumbnails"
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.video_queue: list[Video] = []
        self.current_playable: PlayableVideo | None = None
        self.player: Any | None = None
        self.mpv_module: Any | None = None
        self.mpv_render_context: Any | None = None
        self.mpv_get_proc_address: Any | None = None
        self.range_start_seconds: int | None = None
        self.pending_seek_seconds: int | None = None
        self.pending_seek_attempts = 0
        self.updating_scrubber = False
        self.updating_subscribe_check = False
        self.updating_channel_subscribe_check = False
        self.updating_quality = False
        self.updating_speed = False
        self.playback_rate = 1.0
        self.description_link_generation = 0
        self.current_channel_url: str | None = None
        self.video_fullscreen = False
        self.fullscreen_return_view: ViewState | None = None
        self.status_text = "Ready"
        self.back_stack: list[ViewState] = []
        self.forward_stack: list[ViewState] = []
        self.current_view: ViewState | None = None
        self.suppress_nav_selection = False
        self.updating_recent_searches = False
        self.feed_limit = 100
        self.channel_video_limits: dict[str, int] = {}
        self.loading_more_videos = False
        self.grid_generations: dict[int, int] = {}
        self.cleaned_up = False
        self.gl = CDLL("libepoxy.so.0")
        self.libgl = self.load_library("GL")
        self.libegl = self.load_library("EGL")
        if self.libgl is not None:
            try:
                self.libgl.glGetIntegerv.argtypes = [c_int, POINTER(c_int)]
                self.libgl.glXGetProcAddressARB.restype = c_void_p
                self.libgl.glXGetProcAddressARB.argtypes = [c_char_p]
            except AttributeError:
                self.libgl = None
        if self.libegl is not None:
            try:
                self.libegl.eglGetProcAddress.restype = c_void_p
                self.libegl.eglGetProcAddress.argtypes = [c_char_p]
            except AttributeError:
                self.libegl = None

        self.restore_window_size()
        self.install_css()
        self.connect("close-request", self.on_close_request)
        shortcuts = Gtk.EventControllerKey()
        shortcuts.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        shortcuts.connect("key-pressed", self.on_key_pressed)
        self.add_controller(shortcuts)

        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(self.root)

        self.header = Gtk.HeaderBar()
        self.back_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("go-previous-symbolic")
        )
        self.back_button.set_tooltip_text("Back")
        self.back_button.set_sensitive(False)
        self.back_button.connect("clicked", self.on_back_clicked)
        self.header.pack_start(self.back_button)

        self.forward_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("go-next-symbolic")
        )
        self.forward_button.set_tooltip_text("Forward")
        self.forward_button.set_sensitive(False)
        self.forward_button.connect("clicked", self.on_forward_clicked)
        self.header.pack_start(self.forward_button)

        self.open_url_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("document-open-symbolic")
        )
        self.open_url_button.set_tooltip_text("Open URL")
        self.open_url_button.connect("clicked", self.on_open_url_clicked)
        self.header.pack_start(self.open_url_button)

        self.context_refresh_icon = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        self.context_refresh_spinner = Gtk.Spinner()
        self.context_refresh_button = Gtk.Button(child=self.context_refresh_icon)
        self.context_refresh_button.set_tooltip_text("Refresh")
        self.context_refresh_button.set_visible(False)
        self.context_refresh_button.connect("clicked", self.on_context_refresh_clicked)
        self.header.pack_end(self.context_refresh_button)

        self.context_unsubscribe_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("edit-delete-symbolic")
        )
        self.context_unsubscribe_button.set_tooltip_text("Unsubscribe")
        self.context_unsubscribe_button.set_visible(False)
        self.context_unsubscribe_button.connect(
            "clicked", self.on_context_unsubscribe_clicked
        )
        self.header.pack_end(self.context_unsubscribe_button)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title_box.set_halign(Gtk.Align.CENTER)
        title_box.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label="GTKTube")
        title.add_css_class("title")
        title.set_single_line_mode(True)
        title_box.append(title)
        self.header_subtitle = Gtk.Label(label="")
        self.header_subtitle.add_css_class("caption")
        self.header_subtitle.add_css_class("dim-label")
        self.header_subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        self.header_subtitle.set_single_line_mode(True)
        self.header_subtitle.set_max_width_chars(48)
        title_box.append(self.header_subtitle)
        self.header.set_title_widget(title_box)
        self.set_titlebar(self.header)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, vexpand=True)
        self.root.append(body)

        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.sidebar.add_css_class("sidebar")
        self.sidebar.set_size_request(210, -1)
        body.append(self.sidebar)

        nav_scroller = Gtk.ScrolledWindow(vexpand=True)
        nav_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sidebar.append(nav_scroller)

        nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        nav.add_css_class("sidebar-list")
        nav_scroller.set_child(nav)
        self.nav = nav
        self.nav_pages: dict[Gtk.ListBoxRow, str] = {}
        self.nav_channels: dict[Gtk.ListBoxRow, Channel] = {}
        self.page_rows: dict[str, Gtk.ListBoxRow] = {}
        self.channel_rows: dict[str, Gtk.ListBoxRow] = {}
        self.channel_nav_rows: list[Gtk.ListBoxRow] = []

        self.content_pane = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True, vexpand=True
        )
        body.append(self.content_pane)

        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.content_pane.append(self.stack)
        self.build_miniplayer()
        self.build_queue_pane()
        body.append(self.queue_pane)

        self.pages: dict[str, Gtk.Widget] = {}
        for key, title in [
            ("feed", "Feed"),
            ("search", "Search"),
            ("history", "History"),
            ("channels", "Channels"),
        ]:
            row = Gtk.ListBoxRow()
            row.set_child(self.nav_page_widget(key, title))
            self.nav_pages[row] = key
            self.page_rows[key] = row
            nav.append(row)

        nav.connect("row-selected", self.on_nav_selected)

        self.build_feed_page()
        self.build_channels_page()
        self.build_search_page()
        self.build_history_page()
        self.build_player_page()

        self.reload_all_local()
        self.navigate_to(ViewState("feed"), record=False)
        GLib.timeout_add_seconds(5, self.flush_watch_range)
        GLib.timeout_add_seconds(1, self.update_playback_controls)

    def install_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(APP_CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def nav_page_widget(self, key: str, title: str) -> Gtk.Widget:
        icons = {
            "feed": "view-list-symbolic",
            "search": "system-search-symbolic",
            "history": "document-open-recent-symbolic",
            "channels": "folder-symbolic",
        }
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        box.add_css_class("nav-row")
        icon = Gtk.Image.new_from_icon_name(icons[key])
        box.append(icon)
        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        box.append(label)
        return box

    def load_library(self, name: str) -> Any | None:
        path = find_library(name)
        if path is None:
            return None
        try:
            return CDLL(path)
        except OSError as exc:
            self.log(f"could not load lib{name}: {exc}")
            return None

    def on_close_request(self, *_args: object) -> bool:
        self.cleanup()
        return False

    def cleanup(self) -> None:
        if self.cleaned_up:
            return
        self.cleaned_up = True
        self.save_window_size()
        self.flush_watch_range()
        self.stop_pipeline()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def restore_window_size(self) -> None:
        size = self.read_window_size()
        if size is None:
            self.set_default_size(1100, 720)
            return
        width, height = size
        self.set_default_size(width, height)

    def read_window_size(self) -> tuple[int, int] | None:
        try:
            data = json.loads(self.window_state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            width = int(data["width"])
            height = int(data["height"])
        except (KeyError, TypeError, ValueError):
            return None
        if width < 640 or height < 480:
            return None
        return width, height

    def save_window_size(self) -> None:
        width = self.get_width()
        height = self.get_height()
        if width < 1 or height < 1:
            return
        data = {"width": width, "height": height}
        try:
            path = self.window_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def window_state_path(self) -> Path:
        return self.paths.config_dir / "window-state.json"

    def on_nav_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        if self.suppress_nav_selection:
            return
        if row in self.nav_pages:
            self.navigate_to(ViewState(self.nav_pages[row]))
            return
        channel = self.nav_channels.get(row)
        if channel is not None:
            self.navigate_to(ViewState("feed", channel.id, channel.title))

    def on_back_clicked(self, _button: Gtk.Button) -> None:
        self.go_back()

    def on_forward_clicked(self, _button: Gtk.Button) -> None:
        self.go_forward()

    def on_open_url_clicked(self, _button: Gtk.Button) -> None:
        self.show_open_url_dialog()

    def show_open_url_dialog(self) -> None:
        dialog = Gtk.Dialog(title="Open URL", transient_for=self, modal=True)
        dialog.set_default_size(560, -1)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Open", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        entry = Gtk.Entry(hexpand=True)
        entry.set_size_request(520, -1)
        entry.set_placeholder_text("YouTube channel or video URL")
        entry.set_activates_default(True)
        content.append(entry)

        def response(_dialog: Gtk.Dialog, response_id: int) -> None:
            url = entry.get_text().strip()
            dialog.close()
            if response_id == Gtk.ResponseType.OK and url:
                self.open_url(url)

        dialog.connect("response", response)
        dialog.present()
        entry.grab_focus()

    def open_url(self, url: str) -> None:
        url = self.normalized_url(url)
        if self.is_video_url(url):
            self.navigate_to(ViewState("player"))
            quality = self.selected_quality()
            self.run_task(
                "Resolving video...",
                lambda: self.service.play_url(url, quality=quality),
                self.load_playable,
            )
            return

        def done(channel: Channel) -> None:
            self.show_channel_videos(channel)
            self.reload_channels()

        self.run_task(
            "Opening channel...",
            lambda: self.service.open_channel_url(url),
            done,
        )

    def normalized_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme:
            return url
        return f"https://{url}"

    def is_video_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        path_parts = [part for part in parsed.path.split("/") if part]
        if host.endswith("youtu.be"):
            return bool(path_parts)
        if parsed.query and urllib.parse.parse_qs(parsed.query).get("v"):
            return True
        return bool(path_parts and path_parts[0] in {"shorts", "live", "embed"})

    def navigate_to(self, view: ViewState, record: bool = True) -> None:
        if self.current_view == view:
            self.apply_view_state(view)
            self.update_navigation_buttons()
            return
        if record and self.current_view is not None:
            self.back_stack.append(self.current_view)
            self.forward_stack.clear()
        self.current_view = view
        self.apply_view_state(view)
        self.update_navigation_buttons()

    def go_back(self) -> None:
        if not self.back_stack:
            return
        if self.current_view is not None:
            self.forward_stack.append(self.current_view)
        self.current_view = self.back_stack.pop()
        self.apply_view_state(self.current_view)
        self.update_navigation_buttons()

    def go_forward(self) -> None:
        if not self.forward_stack:
            return
        if self.current_view is not None:
            self.back_stack.append(self.current_view)
        self.current_view = self.forward_stack.pop()
        self.apply_view_state(self.current_view)
        self.update_navigation_buttons()

    def apply_view_state(self, view: ViewState) -> None:
        if view.page == "player":
            self.show_full_player()
        else:
            self.flush_watch_range()
            if self.current_playable is not None and self.player is not None:
                self.show_miniplayer()
            else:
                self.hide_miniplayer()
        self.update_header_subtitle(view)
        self.update_context_refresh_button(view)
        self.update_context_unsubscribe_button(view)
        if view.channel_id is not None:
            self.update_channel_header(view)
            self.populate_video_grid(
                self.feed_grid,
                self.service.repository.channel_videos(
                    view.channel_id,
                    self.channel_video_limits.get(view.channel_id, 30),
                ),
            )
            self.select_nav_channel(view.channel_id)
            self.stack.set_visible_child_name("feed")
            return

        self.set_feed_loading(False)
        self.channel_header.set_visible(False)
        if view.page == "feed":
            self.reload_feed()
        elif view.page == "channels":
            self.reload_channels()
        elif view.page == "history":
            self.reload_history()

        self.select_nav_page(view.page)
        self.stack.set_visible_child_name(view.page)

    def update_header_subtitle(self, view: ViewState | None = None) -> None:
        view = view or self.current_view
        if view is None:
            self.header_subtitle.set_text("")
            return
        if view.channel_id is not None:
            self.header_subtitle.set_text(view.channel_title or "Channel")
        elif view.page == "feed":
            self.header_subtitle.set_text("Feed")
        elif view.page == "search":
            self.header_subtitle.set_text("Search")
        elif view.page == "history":
            self.header_subtitle.set_text("History")
        elif view.page == "channels":
            self.header_subtitle.set_text("Channels")
        elif view.page == "player" and self.current_playable is not None:
            self.header_subtitle.set_text(self.current_playable.video.title)
        elif view.page == "player":
            self.header_subtitle.set_text("Player")
        else:
            self.header_subtitle.set_text("")

    def select_nav_page(self, page: str) -> None:
        row = self.page_rows.get(page)
        self.suppress_nav_selection = True
        if row is not None:
            self.nav.select_row(row)
        else:
            self.nav.unselect_all()
        self.suppress_nav_selection = False

    def select_nav_channel(self, channel_id: str) -> None:
        row = self.channel_rows.get(channel_id)
        self.suppress_nav_selection = True
        if row is not None:
            self.nav.select_row(row)
        else:
            self.nav.select_row(self.page_rows["feed"])
        self.suppress_nav_selection = False

    def update_navigation_buttons(self) -> None:
        self.back_button.set_sensitive(bool(self.back_stack))
        self.forward_button.set_sensitive(bool(self.forward_stack))

    def update_context_refresh_button(self, view: ViewState | None = None) -> None:
        view = view or self.current_view
        if view is None:
            self.set_context_refresh_loading(False)
            self.context_refresh_button.set_visible(False)
        elif view.channel_id is not None:
            self.context_refresh_button.set_tooltip_text("Refresh channel")
            self.context_refresh_button.set_visible(True)
        elif view.page in {"feed", "channels"}:
            self.context_refresh_button.set_tooltip_text("Refresh subscriptions")
            self.context_refresh_button.set_visible(True)
        else:
            self.set_context_refresh_loading(False)
            self.context_refresh_button.set_visible(False)

    def update_context_unsubscribe_button(self, view: ViewState | None = None) -> None:
        self.context_unsubscribe_button.set_visible(False)

    def on_context_refresh_clicked(self, _button: Gtk.Button) -> None:
        if self.current_view and self.current_view.channel_id is not None:
            channel = self.current_channel()
            if channel is not None:
                self.refresh_one_channel(channel)
            return
        if self.current_view and self.current_view.page in {"feed", "channels"}:
            self.on_refresh_subscriptions(self.context_refresh_button)

    def on_context_unsubscribe_clicked(self, _button: Gtk.Button) -> None:
        channel = self.current_channel()
        if channel is not None:
            self.unsubscribe_channel(channel)

    def set_context_refresh_loading(self, loading: bool) -> None:
        if loading:
            self.context_refresh_button.set_child(self.context_refresh_spinner)
            self.context_refresh_spinner.start()
            self.context_refresh_button.set_sensitive(False)
            return
        self.context_refresh_spinner.stop()
        self.context_refresh_button.set_child(self.context_refresh_icon)
        self.context_refresh_button.set_sensitive(True)

    def on_player_share_clicked(self, _button: Gtk.Button) -> None:
        if self.current_playable is None:
            return
        self.copy_to_clipboard(self.current_playable.video.url, "Copied video URL")
        self.player_share_icon.set_from_icon_name("emblem-ok-symbolic")
        GLib.timeout_add_seconds(1, self.restore_player_share_icon)

    def restore_player_share_icon(self) -> bool:
        self.player_share_icon.set_from_icon_name("edit-copy-symbolic")
        return False

    def copy_to_clipboard(self, text: str, message: str) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        display.get_clipboard().set(text)
        self.set_status(message)

    def on_channel_header_share_clicked(self, _button: Gtk.Button) -> None:
        if not self.current_channel_url:
            return
        self.copy_to_clipboard(self.current_channel_url, "Copied channel URL")
        self.channel_header_share_icon.set_from_icon_name("emblem-ok-symbolic")
        GLib.timeout_add_seconds(1, self.restore_channel_share_icon)

    def restore_channel_share_icon(self) -> bool:
        self.channel_header_share_icon.set_from_icon_name("edit-copy-symbolic")
        return False

    def update_channel_header(self, view: ViewState) -> None:
        if view.channel_id is None:
            self.set_feed_loading(False)
            self.channel_header.set_visible(False)
            self.current_channel_url = None
            return
        channel = self.service.repository.channel(view.channel_id)
        if channel is None:
            channel = Channel(
                id=view.channel_id,
                title=view.channel_title or "Channel",
                url=f"https://www.youtube.com/channel/{view.channel_id}",
                is_subscribed=False,
            )

        self.channel_header.set_visible(True)
        self.channel_header_title.set_text(channel.title)
        metadata = []
        if channel.handle:
            metadata.append(channel.handle)
        video_count = self.service.repository.channel_video_count(channel.id)
        if video_count:
            metadata.append(f"{video_count:,} loaded videos")
        self.channel_header_meta.set_text(" · ".join(metadata))
        self.current_channel_url = channel.url

        self.updating_channel_subscribe_check = True
        self.channel_header_subscribe.set_active(channel.is_subscribed)
        self.channel_header_subscribe.set_label(
            "Subscribed" if channel.is_subscribed else "Subscribe"
        )
        self.updating_channel_subscribe_check = False

        if channel.thumbnail_url:
            self.load_cached_image(
                channel.thumbnail_url,
                self.channel_header_thumbnail,
                suffix=self.thumbnail_cache_suffix(channel.thumbnail_url),
                width=72,
                height=72,
                log_label=f"channel header thumbnail {channel.id}",
            )
        else:
            self.channel_header_thumbnail.set_paintable(None)

    def current_channel(self) -> Channel | None:
        if self.current_view is None or self.current_view.channel_id is None:
            return None
        return self.service.repository.channel(self.current_view.channel_id)

    def set_status(self, text: str) -> None:
        self.status_text = text

    def log(self, message: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        print(f"{timestamp} gtktube: {message}", file=sys.stderr)

    def run_task(
        self,
        label: str,
        work: Callable[[], T],
        done: Callable[[T], None] | None = None,
        finished: Callable[[], None] | None = None,
    ) -> None:
        self.set_status(label)
        future = self.executor.submit(work)

        def finish() -> bool:
            try:
                result = future.result()
            except ExtractorError as exc:
                self.set_status(str(exc))
            except Exception as exc:
                self.set_status(f"Error: {exc}")
            else:
                if done is not None:
                    done(result)
                self.set_status("Ready")
            if finished is not None:
                finished()
            return False

        future.add_done_callback(lambda _future: GLib.idle_add(finish))

    def reload_all_local(self) -> None:
        self.reload_feed()
        self.reload_channels()
        self.reload_history()
        self.reload_recent_searches()

    def build_feed_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)

        feed_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.channel_header = self.build_channel_header()
        self.channel_header.set_visible(False)
        feed_content.append(self.channel_header)

        self.feed_grid = self.create_video_grid()
        feed_content.append(self.feed_grid)
        self.feed_loading_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.feed_loading_box.set_halign(Gtk.Align.CENTER)
        self.feed_loading_box.set_margin_top(8)
        self.feed_loading_box.set_margin_bottom(8)
        self.feed_loading_box.set_visible(False)
        spinner = Gtk.Spinner()
        spinner.start()
        self.feed_loading_box.append(spinner)
        self.feed_loading_label = Gtk.Label(label="Loading more...")
        self.feed_loading_label.add_css_class("dim-label")
        self.feed_loading_box.append(self.feed_loading_label)
        feed_content.append(self.feed_loading_box)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(feed_content)
        scroller.get_vadjustment().connect("value-changed", self.on_feed_scroll)
        page.append(scroller)

        self.stack.add_named(page, "feed")

    def build_channel_header(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        header.set_margin_top(4)
        header.set_margin_bottom(8)
        header.set_margin_start(8)
        header.set_margin_end(8)

        self.channel_header_thumbnail = Gtk.Picture()
        self.channel_header_thumbnail.set_size_request(72, 72)
        self.channel_header_thumbnail.set_can_shrink(False)
        self.channel_header_thumbnail.set_content_fit(Gtk.ContentFit.CONTAIN)
        header.append(self.channel_header_thumbnail)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        details.set_hexpand(True)
        header.append(details)

        self.channel_header_title = Gtk.Label(label="", xalign=0)
        self.channel_header_title.add_css_class("title-2")
        self.channel_header_title.set_ellipsize(Pango.EllipsizeMode.END)
        details.append(self.channel_header_title)

        self.channel_header_meta = Gtk.Label(label="", xalign=0)
        self.channel_header_meta.add_css_class("dim-label")
        self.channel_header_meta.set_wrap(True)
        details.append(self.channel_header_meta)

        self.channel_header_subscribe = Gtk.CheckButton(label="Subscribed")
        self.channel_header_subscribe.connect(
            "toggled", self.on_channel_header_subscribe_toggled
        )
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        details.append(actions)
        actions.append(self.channel_header_subscribe)

        self.channel_header_share_icon = Gtk.Image.new_from_icon_name(
            "edit-copy-symbolic"
        )
        self.channel_header_share_button = Gtk.Button(
            child=self.channel_header_share_icon
        )
        self.channel_header_share_button.set_tooltip_text("Copy channel URL")
        self.channel_header_share_button.connect(
            "clicked", self.on_channel_header_share_clicked
        )
        actions.append(self.channel_header_share_button)
        return header

    def build_channels_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)

        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.append(add_box)
        self.subscribe_entry = Gtk.Entry(hexpand=True)
        self.subscribe_entry.set_placeholder_text("Channel URL, handle URL, or video URL")
        self.subscribe_entry.connect("activate", self.on_subscribe_clicked)
        add_box.append(self.subscribe_entry)
        subscribe = Gtk.Button(label="Subscribe")
        subscribe.connect("clicked", self.on_subscribe_clicked)
        add_box.append(subscribe)

        self.channel_grid = self.create_channel_grid()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.channel_grid)
        page.append(scroller)

        self.stack.add_named(page, "channels")

    def build_search_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.append(search_box)
        self.search_combo = Gtk.ComboBoxText.new_with_entry()
        self.search_combo.set_hexpand(True)
        self.search_combo.connect("changed", self.on_recent_search_selected)
        self.search_entry = self.search_combo.get_child()
        if isinstance(self.search_entry, Gtk.Entry):
            self.search_entry.set_placeholder_text("Search YouTube")
            self.search_entry.connect("activate", self.on_search_clicked)
        search_box.append(self.search_combo)
        self.search_icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        self.search_spinner = Gtk.Spinner()
        self.search_button = Gtk.Button(
            child=self.search_icon
        )
        self.search_button.set_tooltip_text("Search")
        self.search_button.connect("clicked", self.on_search_clicked)
        search_box.append(self.search_button)

        search_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.search_channel_heading = Gtk.Label(label="Channels", xalign=0)
        self.search_channel_heading.add_css_class("heading")
        self.search_channel_heading.set_visible(False)
        search_results.append(self.search_channel_heading)

        self.search_channel_grid = self.create_channel_grid()
        self.search_channel_grid.set_visible(False)
        search_results.append(self.search_channel_grid)

        self.search_video_heading = Gtk.Label(label="Videos", xalign=0)
        self.search_video_heading.add_css_class("heading")
        self.search_video_heading.set_visible(False)
        search_results.append(self.search_video_heading)

        self.search_grid = self.create_video_grid()
        self.search_grid.set_visible(False)
        search_results.append(self.search_grid)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(search_results)
        page.append(scroller)

        self.stack.add_named(page, "search")

    def build_history_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.append(search_box)
        self.history_entry = Gtk.Entry(hexpand=True)
        self.history_entry.set_placeholder_text("Search watch history")
        self.history_entry.connect("activate", self.on_history_search_changed)
        search_box.append(self.history_entry)
        history_button = Gtk.Button(label="Search history")
        history_button.connect("clicked", self.on_history_search_changed)
        search_box.append(history_button)

        self.history_grid = self.create_video_grid()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.history_grid)
        page.append(scroller)

        self.stack.add_named(page, "history")

    def build_miniplayer(self) -> None:
        self.miniplayer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.miniplayer.add_css_class("miniplayer")
        self.miniplayer.set_vexpand(False)
        self.miniplayer.set_visible(False)

        self.miniplayer_video_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0
        )
        self.miniplayer_video_container.set_size_request(176, 99)
        self.miniplayer_video_container.set_hexpand(False)
        self.miniplayer_video_container.set_vexpand(False)
        self.miniplayer_video_container.set_valign(Gtk.Align.CENTER)
        self.miniplayer.append(self.miniplayer_video_container)

        self.miniplayer_controls_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True
        )
        self.miniplayer_controls_container.set_valign(Gtk.Align.CENTER)
        mini_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mini_header.set_valign(Gtk.Align.CENTER)
        self.miniplayer_info = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True
        )
        mini_header.append(self.miniplayer_info)
        self.miniplayer_title = Gtk.Label(label="", xalign=0, hexpand=True)
        self.miniplayer_title.set_single_line_mode(True)
        self.miniplayer_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.miniplayer_info.append(self.miniplayer_title)
        self.miniplayer_meta = Gtk.Label(label="", xalign=0, hexpand=True)
        self.miniplayer_meta.add_css_class("dim-label")
        self.miniplayer_meta.set_single_line_mode(True)
        self.miniplayer_meta.set_ellipsize(Pango.EllipsizeMode.END)
        self.miniplayer_info.append(self.miniplayer_meta)
        self.restore_player_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("go-up-symbolic")
        )
        self.restore_player_button.set_tooltip_text("Open full player")
        self.restore_player_button.connect("clicked", self.on_restore_player_clicked)
        mini_header.append(self.restore_player_button)
        self.miniplayer_controls_container.append(mini_header)
        self.miniplayer.append(self.miniplayer_controls_container)
        self.content_pane.append(self.miniplayer)

    def build_queue_pane(self) -> None:
        self.queue_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.queue_pane.add_css_class("queue-pane")
        self.queue_pane.set_size_request(320, -1)
        self.queue_pane.set_visible(False)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(12)
        header.set_margin_bottom(8)
        header.set_margin_start(12)
        header.set_margin_end(12)
        label = Gtk.Label(label="Queue")
        label.add_css_class("heading")
        header.append(label)
        self.queue_pane.append(header)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.queue_list = Gtk.ListBox()
        self.queue_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.queue_list.add_css_class("sidebar-list")
        scroller.set_child(self.queue_list)
        self.queue_pane.append(scroller)

    def build_player_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.video = Gtk.GLArea(hexpand=False, vexpand=False)
        self.video.set_auto_render(False)
        self.video.set_has_depth_buffer(False)
        self.video.set_has_stencil_buffer(False)
        self.video.set_size_request(176, 99)
        self.video.connect("realize", self.on_video_realize)
        self.video.connect("render", self.on_video_render)
        self.video.connect("unrealize", self.on_video_unrealize)
        video_click = Gtk.GestureClick()
        video_click.connect("released", self.on_video_clicked)
        self.video.add_controller(video_click)
        self.miniplayer_video_container.append(self.video)

        self.player_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.miniplayer_controls_container.append(self.player_controls)
        self.play_pause_icon = Gtk.Image.new_from_icon_name(
            "media-playback-start-symbolic"
        )
        self.play_pause_button = Gtk.Button(child=self.play_pause_icon)
        self.play_pause_button.set_tooltip_text("Play")
        self.play_pause_button.connect("clicked", self.on_play_pause_clicked)
        self.player_controls.append(self.play_pause_button)

        self.elapsed_label = Gtk.Label(label="0:00")
        self.player_controls.append(self.elapsed_label)

        self.scrubber = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.scrubber.set_hexpand(True)
        self.scrubber.set_draw_value(False)
        self.scrubber.connect("change-value", self.on_scrub_changed)
        self.player_controls.append(self.scrubber)

        self.duration_label = Gtk.Label(label="0:00")
        self.player_controls.append(self.duration_label)

        self.quality_combo = Gtk.ComboBoxText()
        for quality in QUALITY_FORMATS:
            self.quality_combo.append(quality, quality)
        self.quality_combo.set_active_id("720p")
        self.quality_combo.connect("changed", self.on_quality_changed)
        self.player_controls.append(self.quality_combo)

        self.speed_combo = Gtk.ComboBoxText()
        for rate in PLAYBACK_RATES:
            self.speed_combo.append(self.speed_id(rate), self.speed_label(rate))
        self.speed_combo.set_active_id(self.speed_id(self.playback_rate))
        self.speed_combo.connect("changed", self.on_speed_changed)
        self.player_controls.append(self.speed_combo)

        self.fullscreen_icon = Gtk.Image.new_from_icon_name("view-fullscreen-symbolic")
        self.fullscreen_button = Gtk.Button(child=self.fullscreen_icon)
        self.fullscreen_button.set_tooltip_text("Fullscreen video")
        self.fullscreen_button.connect("clicked", self.on_fullscreen_clicked)
        self.player_controls.append(self.fullscreen_button)

        self.close_player_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("window-close-symbolic")
        )
        self.close_player_button.set_tooltip_text("Close player")
        self.close_player_button.connect("clicked", self.on_close_player_clicked)
        self.player_controls.append(self.close_player_button)

        self.player_metadata = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.player_metadata.set_margin_top(4)
        self.player_metadata.set_margin_bottom(12)
        self.player_metadata.set_margin_start(12)
        self.player_metadata.set_margin_end(12)
        self.player_metadata.set_visible(False)
        self.miniplayer.append(self.player_metadata)
        self.player_title = Gtk.Label(label="No video loaded", xalign=0, hexpand=True)
        self.player_title.set_wrap(True)
        self.player_metadata.append(self.player_title)

        self.player_meta = Gtk.Label(label="", xalign=0)
        self.player_meta.set_wrap(True)
        self.player_meta.add_css_class("dim-label")
        self.player_metadata.append(self.player_meta)

        player_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        player_actions.set_valign(Gtk.Align.CENTER)
        self.player_metadata.append(player_actions)

        self.player_subscribe = Gtk.CheckButton(label="Subscribed")
        self.player_subscribe.set_sensitive(False)
        self.player_subscribe.connect("toggled", self.on_player_subscribe_toggled)
        player_actions.append(self.player_subscribe)

        self.player_share_icon = Gtk.Image.new_from_icon_name("edit-copy-symbolic")
        self.player_share_button = Gtk.Button(child=self.player_share_icon)
        self.player_share_button.set_tooltip_text("Copy video URL")
        self.player_share_button.set_sensitive(False)
        self.player_share_button.connect("clicked", self.on_player_share_clicked)
        player_actions.append(self.player_share_button)

        self.player_description = Gtk.TextView()
        self.player_description.set_editable(False)
        self.player_description.set_cursor_visible(False)
        self.player_description.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.player_description.set_left_margin(2)
        self.player_description.set_right_margin(2)
        self.description_link_tags: dict[str, str] = {}
        description_click = Gtk.GestureClick()
        description_click.set_button(1)
        description_click.connect("released", self.on_description_clicked)
        self.player_description.add_controller(description_click)

        description_scroller = Gtk.ScrolledWindow()
        description_scroller.set_size_request(-1, 140)
        description_scroller.set_child(self.player_description)

        description = Gtk.Expander(label="Description")
        description.set_child(description_scroller)
        self.player_metadata.append(description)

        self.stack.add_named(page, "player")

    def create_video_grid(self) -> Gtk.FlowBox:
        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_min_children_per_line(1)
        grid.set_max_children_per_line(16)
        grid.set_column_spacing(12)
        grid.set_row_spacing(12)
        grid.set_halign(Gtk.Align.CENTER)
        grid.set_valign(Gtk.Align.START)
        grid.set_margin_top(4)
        grid.set_margin_bottom(4)
        grid.set_margin_start(4)
        grid.set_margin_end(4)
        return grid

    def create_channel_grid(self) -> Gtk.FlowBox:
        grid = self.create_video_grid()
        grid.set_max_children_per_line(10)
        return grid

    def on_refresh_subscriptions(self, _button: Gtk.Button) -> None:
        def done(_result: None) -> None:
            self.reload_channels()
            self.reload_feed()

        def finished() -> None:
            self.set_context_refresh_loading(False)

        self.set_context_refresh_loading(True)
        self.run_task(
            "Refreshing subscriptions...",
            self.service.refresh_subscriptions,
            done,
            finished=finished,
        )

    def on_feed_scroll(self, adjustment: Gtk.Adjustment) -> None:
        if self.loading_more_videos:
            return
        if self.current_view is None:
            return
        if self.current_view.page != "feed":
            return
        bottom = adjustment.get_upper() - adjustment.get_page_size()
        if bottom <= 0:
            return
        if adjustment.get_value() >= bottom - 160:
            self.load_more_videos()

    def load_more_videos(self) -> None:
        if self.current_view and self.current_view.channel_id is not None:
            channel = self.current_channel()
            if channel is not None:
                self.load_more_channel_videos(channel)
            return
        self.loading_more_videos = True
        self.set_feed_loading(True, "Loading more...")
        previous_limit = self.feed_limit
        self.feed_limit += 100
        videos = self.service.repository.subscription_feed(self.feed_limit)
        self.append_video_grid_batched(
            self.feed_grid,
            videos[previous_limit:self.feed_limit],
            self.finish_local_feed_load,
        )

    def finish_local_feed_load(self) -> bool:
        self.loading_more_videos = False
        self.set_feed_loading(False)
        return False

    def load_more_channel_videos(self, channel: Channel) -> None:
        current_limit = self.channel_video_limits.get(channel.id, 30)
        next_limit = current_limit + 30
        self.channel_video_limits[channel.id] = next_limit
        self.loading_more_videos = True
        self.set_feed_loading(True, "Loading more...")

        def done(_videos: list[Video]) -> None:
            if self.current_view and self.current_view.channel_id == channel.id:
                videos = self.service.repository.channel_videos(channel.id, next_limit)
                self.append_video_grid_batched(
                    self.feed_grid,
                    videos[current_limit:next_limit],
                    self.finish_local_feed_load,
                )
                return
            self.finish_local_feed_load()

        def failed_done() -> bool:
            self.loading_more_videos = False
            self.set_feed_loading(False)
            return False

        def work() -> list[Video]:
            try:
                return self.service.refresh_channel(channel, limit=next_limit)
            except Exception:
                GLib.idle_add(failed_done)
                raise

        self.run_task(
            f"Loading more from {channel.title}...",
            work,
            done,
        )

    def on_subscribe_clicked(self, _widget: Gtk.Widget) -> None:
        url = self.subscribe_entry.get_text().strip()
        if not url:
            return

        def done(_channel: Channel) -> None:
            self.subscribe_entry.set_text("")
            self.reload_channels()
            self.reload_feed()

        self.run_task("Subscribing...", lambda: self.service.subscribe(url), done)

    def on_search_clicked(self, _widget: Gtk.Widget) -> None:
        query = self.search_entry.get_text().strip()
        if not query:
            return

        def done(results: SearchResults) -> None:
            self.populate_search_results(results)
            self.reload_recent_searches()

        def finished() -> None:
            self.search_spinner.stop()
            self.search_button.set_child(self.search_icon)
            self.search_button.set_sensitive(True)

        self.search_button.set_sensitive(False)
        self.search_button.set_child(self.search_spinner)
        self.search_spinner.start()
        self.run_task(
            "Searching...",
            lambda: self.service.search(query),
            done,
            finished=finished,
        )

    def on_history_search_changed(self, _widget: Gtk.Widget) -> None:
        self.reload_history()

    def on_player_subscribe_toggled(self, _button: Gtk.CheckButton) -> None:
        if self.updating_subscribe_check:
            return
        if self.current_playable is None:
            return

        active = self.player_subscribe.get_active()
        video = self.current_playable.video

        if active:
            def done_subscribe(_channel: Channel) -> None:
                self.updating_subscribe_check = True
                self.player_subscribe.set_active(True)
                self.updating_subscribe_check = False
                self.reload_channels()
                self.reload_feed()

            self.run_task(
                "Subscribing...",
                lambda: self.service.subscribe_to_video_channel(video),
                done_subscribe,
            )
        else:
            def done_unsubscribe(_result: None = None) -> None:
                self.update_subscribe_check(video)
                self.reload_channels()
                self.reload_feed()

            self.run_task(
                "Unsubscribing...",
                lambda: self.service.unsubscribe_from_video_channel(video),
                done_unsubscribe,
            )

    def on_channel_header_subscribe_toggled(self, button: Gtk.CheckButton) -> None:
        if self.updating_channel_subscribe_check:
            return
        channel = self.current_channel()
        if channel is None:
            return

        if button.get_active():
            def done_subscribe(_channel: Channel) -> None:
                self.reload_channels()
                if self.current_view is not None:
                    self.update_channel_header(self.current_view)
                    self.update_context_unsubscribe_button(self.current_view)

            self.run_task(
                f"Subscribing to {channel.title}...",
                lambda: self.service.subscribe(channel.url),
                done_subscribe,
            )
        else:
            def done_unsubscribe(_result: None = None) -> None:
                self.reload_channels()
                if self.current_view is not None:
                    self.update_channel_header(self.current_view)
                    self.update_context_unsubscribe_button(self.current_view)

            self.run_task(
                f"Unsubscribing from {channel.title}...",
                lambda: self.service.unsubscribe_channel(channel),
                done_unsubscribe,
            )

    def reload_feed(self) -> None:
        self.populate_video_grid(
            self.feed_grid,
            self.service.repository.subscription_feed(self.feed_limit),
        )

    def populate_search_results(self, results: SearchResults) -> None:
        self.clear_flowbox(self.search_channel_grid)
        self.clear_flowbox(self.search_grid)

        self.search_channel_heading.set_visible(bool(results.channels))
        self.search_channel_grid.set_visible(bool(results.channels))
        for channel in results.channels:
            self.search_channel_grid.append(
                self.channel_tile(channel)
            )

        self.search_video_heading.set_visible(bool(results.videos))
        self.search_grid.set_visible(bool(results.videos))
        for video in results.videos:
            self.search_grid.append(self.video_tile(video))

    def reload_channels(self) -> None:
        channels = self.service.repository.subscribed_channels()
        self.clear_flowbox(self.channel_grid)
        for channel in channels:
            self.channel_grid.append(self.channel_tile(channel))
        self.reload_channel_nav(channels)

    def reload_channel_nav(self, channels: list[Channel]) -> None:
        self.suppress_nav_selection = True
        for row in self.channel_nav_rows:
            self.nav.remove(row)
        self.channel_nav_rows = []
        self.nav_channels = {}
        self.channel_rows = {}

        for channel in channels:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            box.add_css_class("channel-nav-row")
            if channel.thumbnail_url:
                icon = Gtk.Picture()
                icon.set_size_request(24, 24)
                icon.set_can_shrink(False)
                icon.set_content_fit(Gtk.ContentFit.COVER)
                self.load_channel_nav_icon(channel, icon)
                box.append(icon)
            else:
                fallback_icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
                fallback_icon.add_css_class("dim-label")
                box.append(fallback_icon)
            label = Gtk.Label(label=channel.title, xalign=0, hexpand=True)
            label.add_css_class("channel-nav-label")
            label.add_css_class("dim-label")
            label.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(label)
            row.set_child(box)
            self.nav_channels[row] = channel
            self.channel_rows[channel.id] = row
            self.channel_nav_rows.append(row)
            self.nav.append(row)

        self.suppress_nav_selection = False
        if self.current_view and self.current_view.channel_id is not None:
            self.select_nav_channel(self.current_view.channel_id)

    def channel_tile(self, channel: Channel) -> Gtk.Widget:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tile.set_size_request(184, -1)
        tile.set_margin_top(6)
        tile.set_margin_bottom(6)
        tile.set_margin_start(6)
        tile.set_margin_end(6)

        open_button = Gtk.Button()
        open_button.connect("clicked", lambda _button: self.open_search_channel(channel))
        tile.append(open_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_size_request(172, -1)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        content.set_margin_start(6)
        content.set_margin_end(6)
        open_button.set_child(content)

        thumbnail_area = Gtk.Overlay()
        thumbnail_area.set_size_request(112, 112)
        thumbnail_area.set_halign(Gtk.Align.CENTER)
        thumbnail_placeholder = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
        thumbnail_placeholder.set_pixel_size(64)
        thumbnail_placeholder.set_size_request(112, 112)
        thumbnail_placeholder.set_halign(Gtk.Align.CENTER)
        thumbnail_placeholder.set_valign(Gtk.Align.CENTER)
        thumbnail_placeholder.add_css_class("dim-label")
        thumbnail_area.set_child(thumbnail_placeholder)
        if channel.thumbnail_url:
            thumbnail = Gtk.Picture()
            thumbnail.set_size_request(112, 112)
            thumbnail.set_can_shrink(False)
            thumbnail.set_halign(Gtk.Align.CENTER)
            thumbnail.set_valign(Gtk.Align.CENTER)
            thumbnail.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.load_channel_thumbnail(channel, thumbnail)
            thumbnail_area.add_overlay(thumbnail)
        content.append(thumbnail_area)

        title = Gtk.Label(label=channel.title, xalign=0.5)
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.set_lines(2)
        title.set_width_chars(20)
        title.set_max_width_chars(20)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        content.append(title)

        if channel.handle:
            handle = Gtk.Label(label=channel.handle, xalign=0.5)
            handle.add_css_class("dim-label")
            handle.set_width_chars(20)
            handle.set_max_width_chars(20)
            handle.set_ellipsize(Pango.EllipsizeMode.END)
            content.append(handle)

        return tile

    def open_search_channel(self, channel: Channel) -> None:
        self.service.repository.upsert_channel(channel, subscribed=False)
        self.show_channel_videos(channel)

    def load_channel_thumbnail(self, channel: Channel, picture: Gtk.Picture) -> None:
        if not channel.thumbnail_url:
            return
        self.load_cached_image(
            channel.thumbnail_url,
            picture,
            suffix=self.thumbnail_cache_suffix(channel.thumbnail_url),
            width=112,
            height=112,
            log_label=f"channel thumbnail {channel.id}",
        )

    def load_channel_nav_icon(self, channel: Channel, picture: Gtk.Picture) -> None:
        if not channel.thumbnail_url:
            return
        self.load_cached_image(
            channel.thumbnail_url,
            picture,
            suffix=self.thumbnail_cache_suffix(channel.thumbnail_url),
            width=24,
            height=24,
            log_label=f"channel nav icon {channel.id}",
        )

    def unsubscribe_channel(self, channel: Channel) -> None:
        def done(_result: None = None) -> None:
            self.reload_channels()
            self.reload_feed()
            if self.current_view and self.current_view.channel_id == channel.id:
                self.navigate_to(ViewState("feed"))
            if self.current_playable is not None:
                self.update_subscribe_check(self.current_playable.video)

        self.run_task(
            f"Unsubscribing from {channel.title}...",
            lambda: self.service.unsubscribe_channel(channel),
            done,
        )

    def reload_recent_searches(self) -> None:
        self.updating_recent_searches = True
        current_text = self.search_entry.get_text()
        current_position = self.search_entry.get_position()
        self.search_combo.remove_all()
        for query in self.service.repository.recent_searches():
            self.search_combo.append_text(query)
        self.search_combo.set_active(-1)
        self.search_entry.set_text(current_text)
        self.search_entry.set_position(current_position)
        self.updating_recent_searches = False

    def on_recent_search_selected(self, combo: Gtk.ComboBoxText) -> None:
        if self.updating_recent_searches:
            return
        if combo.get_active() < 0:
            return
        query = combo.get_active_text()
        if query:
            self.run_recent_search(query)

    def reload_history(self) -> None:
        query = self.history_entry.get_text().strip() if hasattr(self, "history_entry") else ""
        self.populate_video_grid(self.history_grid, self.service.repository.watch_history(query))

    def set_feed_loading(self, loading: bool, label: str = "Loading more...") -> None:
        self.feed_loading_label.set_text(label)
        self.feed_loading_box.set_visible(loading)

    def refresh_one_channel(self, channel: Channel) -> None:
        def done(_videos: list[Video]) -> None:
            self.reload_channels()
            if self.current_view and self.current_view.channel_id == channel.id:
                self.apply_view_state(self.current_view)

        def finished() -> None:
            if self.current_view and self.current_view.channel_id == channel.id:
                self.set_feed_loading(False)
            self.set_context_refresh_loading(False)

        if self.current_view and self.current_view.channel_id == channel.id:
            self.set_feed_loading(True, "Loading videos...")
            self.set_context_refresh_loading(True)

        self.run_task(
            f"Refreshing {channel.title}...",
            lambda: self.service.refresh_channel(
                channel,
                limit=self.channel_video_limits.get(channel.id, 30),
            ),
            done,
            finished=finished,
        )

    def show_channel_videos(self, channel: Channel) -> None:
        self.channel_video_limits.setdefault(channel.id, 30)
        self.navigate_to(ViewState("feed", channel.id, channel.title))
        if not self.service.repository.channel_videos(channel.id, 1):
            self.refresh_one_channel(channel)

    def run_recent_search(self, query: str) -> None:
        self.search_entry.set_text(query)
        self.on_search_clicked(self.search_entry)

    def populate_video_grid(self, grid: Gtk.FlowBox, videos: list[Video]) -> None:
        self.clear_flowbox(grid)
        self.grid_generations[id(grid)] = self.grid_generations.get(id(grid), 0) + 1
        for video in videos:
            grid.append(self.video_tile(video))

    def append_video_grid_batched(
        self,
        grid: Gtk.FlowBox,
        videos: list[Video],
        done: Callable[[], bool] | None = None,
    ) -> None:
        index = 0
        generation = self.grid_generations.get(id(grid), 0)

        def append_batch() -> bool:
            nonlocal index
            if self.grid_generations.get(id(grid), 0) != generation:
                return False
            end = min(index + 8, len(videos))
            for video in videos[index:end]:
                grid.append(self.video_tile(video))
            index = end
            if index < len(videos):
                return True
            if done is not None:
                done()
            return False

        GLib.idle_add(append_batch)

    def video_tile(self, video: Video) -> Gtk.Widget:
        button = Gtk.Button()
        button.set_size_request(232, -1)
        button.set_hexpand(False)
        button.set_halign(Gtk.Align.START)
        button.connect("clicked", lambda _button: self.play_video(video))

        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect(
            "pressed",
            lambda _gesture, _n_press, x, y: self.show_video_context_menu(
                button, video, x, y
            ),
        )
        button.add_controller(right_click)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(220, -1)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        button.set_child(box)

        thumbnail = Gtk.Picture()
        thumbnail.set_size_request(220, 165)
        thumbnail.set_can_shrink(False)
        thumbnail.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.load_thumbnail(video, thumbnail)
        box.append(thumbnail)

        title = Gtk.Label(label=video.title, xalign=0)
        title.set_wrap(True)
        title.set_lines(2)
        title.set_width_chars(28)
        title.set_max_width_chars(28)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(title)

        meta = self.video_meta(video)
        subtitle = Gtk.Label(label=meta, xalign=0)
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_lines(2)
        subtitle.set_width_chars(28)
        subtitle.set_max_width_chars(28)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(subtitle)

        return button

    def queue_tile(self, video: Video, index: int) -> Gtk.Widget:
        row = Gtk.ListBoxRow()
        row.add_css_class("queue-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_child(box)

        thumbnail = Gtk.Picture()
        thumbnail.set_size_request(80, 45)
        thumbnail.set_can_shrink(False)
        thumbnail.set_content_fit(Gtk.ContentFit.COVER)
        self.load_thumbnail(video, thumbnail)
        box.append(thumbnail)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        details.set_hexpand(True)
        box.append(details)

        title = Gtk.Label(label=video.title, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_single_line_mode(True)
        details.append(title)

        meta = self.video_meta(video)
        subtitle = Gtk.Label(label=meta, xalign=0)
        subtitle.add_css_class("caption")
        subtitle.add_css_class("dim-label")
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.set_single_line_mode(True)
        details.append(subtitle)

        # Context menu for queue items
        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect(
            "pressed",
            lambda _gesture, _n_press, x, y: self.show_queue_context_menu(
                row, video, index, x, y
            ),
        )
        row.add_controller(right_click)

        # Setup Drag and Drop
        self.setup_queue_dnd(row, index)

        return row

    def setup_queue_dnd(self, row: Gtk.ListBoxRow, index: int) -> None:
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(index))
        row.add_controller(source)

        target = Gtk.DropTarget.new(int, Gdk.DragAction.MOVE)
        target.connect("drop", lambda *args: self.on_queue_drop(index, *args))
        row.add_controller(target)

    def on_queue_drop(self, target_index: int, _target: Gtk.DropTarget, source_index: int, _x: float, _y: float) -> bool:
        if source_index == target_index:
            return False
        video = self.video_queue.pop(source_index)
        self.video_queue.insert(target_index, video)
        self.refresh_queue_ui()
        return True

    def refresh_queue_ui(self) -> None:
        self.clear_listbox(self.queue_list)
        for i, video in enumerate(self.video_queue):
            self.queue_list.append(self.queue_tile(video, i))
        self.queue_pane.set_visible(len(self.video_queue) > 0)

    def clear_listbox(self, listbox: Gtk.ListBox) -> None:
        child = listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            listbox.remove(child)
            child = next_child

    def show_video_context_menu(
        self, parent: Gtk.Widget, video: Video, x: float, y: float
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(parent)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        actions.set_margin_top(6)
        actions.set_margin_bottom(6)
        actions.set_margin_start(6)
        actions.set_margin_end(6)
        popover.set_child(actions)

        open_video = Gtk.Button(label="Open video")
        open_video.add_css_class("flat")
        open_video.set_halign(Gtk.Align.FILL)
        open_video.connect(
            "clicked", lambda _button: self.activate_video_menu(popover, video, "video")
        )
        actions.append(open_video)

        if video.channel_id and video.channel_title:
            open_channel = Gtk.Button(label="Open channel")
            open_channel.add_css_class("flat")
            open_channel.set_halign(Gtk.Align.FILL)
            open_channel.connect(
                "clicked",
                lambda _button: self.activate_video_menu(popover, video, "channel"),
            )
            actions.append(open_channel)

        add_queue = Gtk.Button(label="Add to queue")
        add_queue.add_css_class("flat")
        add_queue.set_halign(Gtk.Align.FILL)
        add_queue.connect(
            "clicked", lambda _button: self.activate_video_menu(popover, video, "queue")
        )
        actions.append(add_queue)

        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()

    def show_queue_context_menu(
        self, row: Gtk.ListBoxRow, video: Video, index: int, x: float, y: float
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(row)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        actions.set_margin_top(6)
        actions.set_margin_bottom(6)
        actions.set_margin_start(6)
        actions.set_margin_end(6)
        popover.set_child(actions)

        play_now = Gtk.Button(label="Play now")
        play_now.add_css_class("flat")
        play_now.set_halign(Gtk.Align.FILL)
        play_now.connect("clicked", lambda _: self.play_from_queue(popover, index))
        actions.append(play_now)

        remove_queue = Gtk.Button(label="Remove from queue")
        remove_queue.add_css_class("flat")
        remove_queue.set_halign(Gtk.Align.FILL)
        remove_queue.connect("clicked", lambda _: self.remove_from_queue(popover, index))
        actions.append(remove_queue)

        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()

    def add_to_queue(self, video: Video) -> None:
        self.video_queue.append(video)
        self.refresh_queue_ui()

    def remove_from_queue(self, popover: Gtk.Popover, index: int) -> None:
        popover.popdown()
        popover.unparent()
        if 0 <= index < len(self.video_queue):
            self.video_queue.pop(index)
            self.refresh_queue_ui()

    def play_from_queue(self, popover: Gtk.Popover, index: int) -> None:
        popover.popdown()
        popover.unparent()
        if 0 <= index < len(self.video_queue):
            video = self.video_queue.pop(index)
            self.refresh_queue_ui()
            self.play_video(video)

    def activate_video_menu(
        self, popover: Gtk.Popover, video: Video, action: str
    ) -> None:
        popover.popdown()
        popover.unparent()
        if action == "channel":
            self.open_video_channel(video)
        elif action == "queue":
            self.add_to_queue(video)
        else:
            self.play_video(video)

    def open_video_channel(self, video: Video) -> None:
        if not video.channel_id or not video.channel_title:
            return
        channel = self.service.repository.channel(video.channel_id) or Channel(
            id=video.channel_id,
            title=video.channel_title,
            url=f"https://www.youtube.com/channel/{video.channel_id}",
            is_subscribed=False,
        )
        self.service.repository.upsert_channel(channel, subscribed=False)
        self.show_channel_videos(channel)

    def load_thumbnail(self, video: Video, picture: Gtk.Picture) -> None:
        url = self.display_thumbnail_url(video)
        self.load_cached_image(
            url,
            picture,
            self.jpeg_thumbnail_url(url),
            suffix=".jpg",
            width=220,
            height=165,
        )

    def load_cached_image(
        self,
        url: str,
        picture: Gtk.Picture,
        download_url: str | None = None,
        suffix: str = ".img",
        width: int = 232,
        height: int = 174,
        log_label: str | None = None,
    ) -> None:
        url = self.absolute_media_url(url)
        download_url = self.absolute_media_url(download_url or url)
        path = self.thumbnail_path(url, suffix)
        if path.exists():
            if self.set_thumbnail_file(picture, path, width, height):
                return
            if log_label:
                self.log(f"{log_label} cached image decode failed path={path} url={url}")
            try:
                path.unlink()
            except OSError:
                pass

        future = self.executor.submit(self.download_thumbnail, download_url, path)

        def done() -> bool:
            try:
                downloaded = future.result()
            except Exception:
                return False
            if downloaded.exists() and picture.get_parent() is not None:
                if not self.set_thumbnail_file(picture, downloaded, width, height):
                    if log_label:
                        self.log(
                            f"{log_label} downloaded image decode failed "
                            f"path={downloaded} url={download_url}"
                        )
            return False

        future.add_done_callback(lambda _future: GLib.idle_add(done))

    def absolute_media_url(self, url: str) -> str:
        if url.startswith("//"):
            return f"https:{url}"
        return url

    def set_thumbnail_file(
        self, picture: Gtk.Picture, path: Path, width: int, height: int
    ) -> bool:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(path),
                width,
                height,
                True,
            )
        except GLib.Error:
            return False
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        picture.set_paintable(texture)
        return True

    def thumbnail_path(self, url: str, suffix: str = ".img") -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.thumbnail_dir / f"{digest}{suffix}"

    def thumbnail_cache_suffix(self, url: str) -> str:
        extension = Path(url.split("?", 1)[0]).suffix.lower()
        if extension in {".jpg", ".jpeg", ".png", ".webp"}:
            return extension
        return ".img"

    def download_thumbnail(self, url: str, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "GTKTube/0.1",
                "Accept": "image/*,*/*;q=0.5",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(2_000_000)
        except (TimeoutError, urllib.error.URLError):
            return path
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
        return path

    def display_thumbnail_url(self, video: Video) -> str:
        if video.id:
            return f"https://img.youtube.com/vi/{video.id}/hqdefault.jpg"
        return self.jpeg_thumbnail_url(video.thumbnail_url or "")

    def jpeg_thumbnail_url(self, url: str) -> str:
        if "i.ytimg.com/vi_webp/" in url:
            url = url.replace("i.ytimg.com/vi_webp/", "i.ytimg.com/vi/")
        url = (
            url.replace("maxresdefault.webp", "hqdefault.jpg")
            .replace("mqdefault.webp", "mqdefault.jpg")
            .replace("hqdefault.webp", "hqdefault.jpg")
        )
        return url.split("?", 1)[0]

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
        self.run_task(
            "Resolving video...",
            lambda: self.service.play_video(video, quality=quality),
            self.load_playable,
        )

    def load_playable(
        self, playable: PlayableVideo, resume_position: int | None = None
    ) -> None:
        self.flush_watch_range()
        self.stop_pipeline()
        self.current_playable = playable
        self.update_header_subtitle(ViewState("player"))
        self.updating_quality = True
        self.quality_combo.set_active_id(playable.quality)
        self.updating_quality = False
        self.update_player_metadata(playable.video)
        self.update_subscribe_check(playable.video)
        self.update_player_share_button()

        player = self.create_player(playable)
        if player is None:
            return
        self.player = player
        try:
            self.player.play(playable.video.url)
            self.player.pause = False
            self.player.speed = self.playback_rate
        except Exception as exc:
            self.set_status(f"Playback error: {exc}")
            self.log(f"mpv playback start failed: {exc}")
            self.stop_pipeline()
            return

        resume = (
            resume_position
            if resume_position is not None
            else self.service.repository.resume_position(playable.video.id)
        )
        if resume > 0:
            self.queue_seek_media(resume)
        self.range_start_seconds = self.current_position_seconds()
        self.show_full_player()
        self.select_nav_page("player")
        self.stack.set_visible_child_name("player")

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
            self.mpv_render_context.render(
                opengl_fbo={
                    "fbo": framebuffer.value,
                    "w": width,
                    "h": height,
                    "internal_format": 0,
                },
                flip_y=True,
            )
            self.mpv_render_context.report_swap()
        except Exception as exc:
            self.log(f"mpv render failed: {exc}")
        return True

    def on_video_unrealize(self, _area: Gtk.GLArea) -> None:
        self.free_mpv_render_context()

    def queue_video_render(self) -> bool:
        self.video.queue_render()
        return False

    def on_mpv_render_update(self) -> None:
        GLib.idle_add(self.queue_video_render)

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
            self.mpv_render_context = mpv.MpvRenderContext(
                player,
                "opengl",
                opengl_init_params={
                    "get_proc_address": self.mpv_get_proc_address,
                },
                advanced_control=True,
            )
            self.mpv_render_context.update_cb = self.on_mpv_render_update
        except Exception as exc:
            self.set_status(f"Could not create mpv renderer: {exc}")
            self.log(f"mpv render context creation failed: {exc}")
            self.mpv_render_context = None
            return False
        return True

    def free_mpv_render_context(self) -> None:
        if self.mpv_render_context is None:
            return
        try:
            self.mpv_render_context.update_cb = None
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
                "Missing mpv dependencies. Run ./scripts/install-deps-gui.py and "
                "install Python requirements."
            )
            self.log(f"mpv import failed: {exc}")
            return None

        try:
            player = mpv.MPV(
                input_default_bindings=True,
                input_vo_keyboard=True,
                osc=True,
                vo="libmpv",
                ytdl=True,
                ytdl_format=os.environ.get(
                    "GTKTUBE_YTDLP_FORMAT",
                    QUALITY_FORMATS.get(playable.quality, QUALITY_FORMATS["720p"]),
                ),
            )
            self.mpv_module = mpv
            player.register_event_callback(self.on_mpv_event)
            if not self.create_mpv_render_context(player, mpv):
                player.terminate()
                return None
            return player
        except Exception as exc:
            self.set_status(f"Could not create mpv player: {exc}")
            self.log(f"mpv player creation failed: {exc}")
            return None

    def on_mpv_event(self, event: Any) -> None:
        if event["event_id"] == self.mpv_module.MpvEventID.END_FILE:
            GLib.idle_add(self.on_mpv_end_file)

    def on_mpv_end_file(self) -> None:
        if self.video_queue:
            self.play_next_in_queue()

    def play_next_in_queue(self) -> None:
        if self.video_queue:
            video = self.video_queue.pop(0)
            self.refresh_queue_ui()
            self.play_video(video)

    def stop_pipeline(self) -> None:
        if self.video_fullscreen:
            self.close_video_fullscreen()
        self.hide_miniplayer()
        if self.player is None:
            self.free_mpv_render_context()
            return
        self.free_mpv_render_context()
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
        return self.quality_combo.get_active_id() or "720p"

    def on_quality_changed(self, _combo: Gtk.ComboBoxText) -> None:
        if self.updating_quality:
            return
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
        start_timer = self.pending_seek_seconds is None
        self.pending_seek_seconds = seconds
        self.pending_seek_attempts = 0
        if start_timer:
            GLib.timeout_add(250, self.flush_pending_seek)

    def flush_pending_seek(self) -> bool:
        if self.pending_seek_seconds is None:
            return False
        self.pending_seek_attempts += 1
        if self.seek_media_impl(self.pending_seek_seconds, defer_until_ready=False):
            self.pending_seek_seconds = None
            return False
        if self.pending_seek_attempts >= 40:
            self.log(f"mpv deferred seek abandoned: {self.pending_seek_seconds}")
            self.pending_seek_seconds = None
            return False
        return True

    def seek_media(self, seconds: int) -> bool:
        return self.seek_media_impl(seconds, defer_until_ready=True)

    def seek_media_impl(self, seconds: int, defer_until_ready: bool) -> bool:
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
                self.player.seek(seconds, reference="absolute", precision="keyframes")
            except Exception as exc:
                if defer_until_ready:
                    self.queue_seek_media(seconds)
                else:
                    self.log(f"mpv seek failed: {exc}")
                return False
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
        return True

    def format_time(self, seconds: int | None) -> str:
        if not seconds:
            return "0:00"
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02}:{secs:02}"
        return f"{minutes}:{secs:02}"

    def clear_flowbox(self, flowbox: Gtk.FlowBox) -> None:
        child = flowbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            flowbox.remove(child)
            child = next_child
