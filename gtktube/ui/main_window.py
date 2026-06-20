from __future__ import annotations

import json
import sys
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, wait
from ctypes import CDLL, POINTER, c_char_p, c_int, c_void_p
from ctypes.util import find_library
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402


from gtktube.extractors.youtube import ExtractorError, QUALITY_FORMATS, is_playlist_url
from gtktube.models import Channel, PlayableVideo, SearchResults, SponsorBlockSegment, Video
from gtktube.paths import AppPaths
from gtktube.services.library import LibraryService
from gtktube.ui.chrome import ChromeMixin
from gtktube.ui.context_menus import ContextMenuMixin
from gtktube.ui.player import PLAYBACK_RATES, PlayerMixin
from gtktube.ui.settings import SettingsMixin
from gtktube.sponsorblock import SponsorBlockClient
from gtktube.ui.sponsorblock import SponsorBlockMixin
from gtktube.ui.styles import APP_CSS
from gtktube.ui.thumbnails import ThumbnailMixin
from gtktube.ui.types import VideoObject, ViewState
from gtktube.ui.video_grid import VideoGridMixin
from gtktube.update_check import UpdateInfo


T = TypeVar("T")
RECOMMENDED_CACHE_TTL_SECONDS = 15 * 60


class GTKTubeApplication(Gtk.Application):
    def __init__(
        self,
        service: LibraryService,
        paths: AppPaths,
        force_update_dialog: bool = False,
        enable_update_check: bool = True,
        verbose: bool = False,
        debug_modal: str | None = None,
    ):
        super().__init__(
            application_id="local.gtktube.GTKTube",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.service = service
        self.paths = paths
        self.force_update_dialog = force_update_dialog
        self.enable_update_check = enable_update_check
        self.verbose = verbose
        self.debug_modal = debug_modal
        self.activation_error: BaseException | None = None

    def do_activate(self) -> None:
        try:
            window = MainWindow(
                self,
                self.service,
                self.paths,
                force_update_dialog=self.force_update_dialog,
                enable_update_check=self.enable_update_check,
                verbose=self.verbose,
                debug_modal=self.debug_modal,
            )
            window.present()
        except Exception as exc:
            self.activation_error = exc
            sys.excepthook(type(exc), exc, exc.__traceback__)
            self.quit()

    def do_shutdown(self) -> None:
        for window in self.get_windows():
            if isinstance(window, MainWindow):
                window.cleanup()
        Gtk.Application.do_shutdown(self)


class MainWindow(
    Gtk.ApplicationWindow,
    ChromeMixin,
    SettingsMixin,
    ContextMenuMixin,
    ThumbnailMixin,
    PlayerMixin,
    VideoGridMixin,
    SponsorBlockMixin,
):
    def __init__(
        self,
        app: GTKTubeApplication,
        service: LibraryService,
        paths: AppPaths,
        force_update_dialog: bool = False,
        enable_update_check: bool = True,
        verbose: bool = False,
        debug_modal: str | None = None,
    ):
        super().__init__(application=app, title="GTKTube")
        self.service = service
        self.paths = paths
        self.force_update_dialog = force_update_dialog
        self.enable_update_check = enable_update_check
        self.verbose = verbose
        self.debug_modal = debug_modal
        self.thumbnail_dir = paths.cache_dir / "thumbnails"
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self.caption_dir = paths.cache_dir / "captions"
        self.caption_dir.mkdir(parents=True, exist_ok=True)
        self.mpv_cache_dir = paths.cache_dir / "mpv"
        self.mpv_cache_dir.mkdir(parents=True, exist_ok=True)
        self.playback_cache_dir = paths.cache_dir / "playback-cache"
        self.playback_cache_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir = paths.data_dir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.pending_futures: set[Future[Any]] = set()
        self.download_menu_items: dict[object, dict[str, Any]] = {}
        self.recommended_cache: list[Video] | None = None
        self.recommended_cache_browser: str | None = None
        self.recommended_cache_loaded_at = 0.0
        self.loading_recommended = False
        self.video_queue = Gio.ListStore(item_type=VideoObject)
        self.playlist_store = Gio.ListStore(item_type=VideoObject)
        self.playlist_current_index: int | None = None
        self.playlist_skip_set: set[int] = set()
        self.queue_quit_confirmed = False
        self.queue_quit_dialog: Gtk.Dialog | None = None
        self.dragging_index = -1
        self.current_playable: PlayableVideo | None = None
        self.playback_request_id = 0
        self.sponsorblock = SponsorBlockClient()
        self.sponsorblock_segments: list[SponsorBlockSegment] = []
        self.suppressed_sponsorblock_segments: set[str] = set()
        self.last_auto_skipped_segment: str | None = None
        self.pending_sponsorblock_skip: dict[str, object] | None = None
        self.player: Any | None = None
        self.mpv_module: Any | None = None
        self.mpv_render_context: Any | None = None
        self.mpv_render_generation = 0
        self.mpv_get_proc_address: Any | None = None
        self.range_start_seconds: int | None = None
        self.pending_seek_seconds: int | None = None
        self.pending_seek_attempts = 0
        self.pending_seek_timer_active = False
        self.updating_scrubber = False
        self.updating_subscribe_check = False
        self.updating_channel_subscribe_check = False
        self.updating_quality = False
        self.updating_speed = False
        self.updating_captions = False
        self.updating_settings = False
        self.playback_rate = 1.0
        self.preferred_playback_mode = self.service.repository.default_playback_mode()
        self.last_playback_diagnostics_at = 0.0
        self.last_playback_diagnostics_values: dict[str, object] = {}
        self.last_playback_diagnostics_paused = False
        self.mpv_property_observers: list[tuple[str, Any]] = []
        self.mpv_observed_time_pos: float | None = None
        self.mpv_observed_duration: float | None = None
        self.mpv_file_loaded = False
        self.mpv_stream_error_message: str | None = None
        self.mpv_stream_retry: tuple[int, str] | None = None
        self.mpv_end_handled = False
        self.selected_caption_id = "off"
        self.active_caption_url: str | None = None
        self.preferred_quality = self.service.repository.default_video_quality()
        self.current_channel_url: str | None = None
        self.video_fullscreen = False
        self.fullscreen_return_view: ViewState | None = None
        self.fullscreen_queue_pane_visible = False
        self.status_text = "Ready"
        self.back_stack: list[ViewState] = []
        self.forward_stack: list[ViewState] = []
        self.current_view: ViewState | None = None
        self.playback_inhibit_cookie: int | None = None
        self.suppress_nav_selection = False
        self.updating_recent_searches = False
        self.feed_limit = 100
        self.channel_video_limits: dict[str, int] = {}
        self.channel_video_search_channel_id: str | None = None
        self.loading_more_videos = False
        self.importing_youtube_history = False
        self.refreshing_subscriptions = False
        self.pending_feed_refresh = False
        self.pending_feed_refresh_channel_ids: set[str] = set()
        self.feed_tile_ids: list[str] = []
        self.feed_tile_widgets: dict[str, Gtk.Widget] = {}
        self.refreshing_channel_ids: set[str] = set()
        self.grid_generations: dict[int, int] = {}
        self.nav_generation = 0
        self.loaded_local_sections: set[str] = set()
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

        downloads_menu_button_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
        )
        downloads_menu_button_box.append(
            Gtk.Image.new_from_icon_name("folder-download-symbolic")
        )
        self.downloads_menu_progress_label = Gtk.Label(label="")
        self.downloads_menu_progress_label.add_css_class("dim-label")
        self.downloads_menu_progress_label.set_visible(False)
        downloads_menu_button_box.append(self.downloads_menu_progress_label)
        self.downloads_menu_button = Gtk.MenuButton(child=downloads_menu_button_box)
        self.downloads_menu_button.set_tooltip_text("Downloads")
        self.downloads_menu_button.set_visible(False)
        self.header.pack_end(self.downloads_menu_button)

        self.downloads_menu_popover = Gtk.Popover()
        self.downloads_menu_button.set_popover(self.downloads_menu_popover)
        downloads_menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        downloads_menu_box.set_margin_top(6)
        downloads_menu_box.set_margin_bottom(6)
        downloads_menu_box.set_margin_start(6)
        downloads_menu_box.set_margin_end(6)
        self.downloads_menu_popover.set_child(downloads_menu_box)
        self.active_downloads_list = Gtk.ListBox()
        self.active_downloads_list.set_selection_mode(Gtk.SelectionMode.NONE)
        downloads_menu_box.append(self.active_downloads_list)

        self.about_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("help-about-symbolic")
        )
        self.about_button.set_tooltip_text("About GTKTube")
        self.about_button.connect("clicked", self.on_about_clicked)
        self.header.pack_end(self.about_button)

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
        self.channel_nav_status_boxes: dict[str, Gtk.Box] = {}
        self.channel_nav_status_channels: dict[str, Channel] = {}

        self.content_pane = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True, vexpand=True
        )
        body.append(self.content_pane)

        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.content_pane.append(self.stack)
        self.build_miniplayer()
        self.build_queue_pane()
        body.append(self.queue_pane)
        self.build_playlist_pane()
        body.append(self.playlist_pane)

        self.pages: dict[str, Gtk.Widget] = {}
        for key, title in [
            ("feed", "Feed"),
            ("recommended", "Recommended"),
            ("search", "Search"),
            ("history", "History"),
            ("watch_later", "Watch Later"),
            ("downloads", "Downloads"),
            ("settings", "Settings"),
            ("channels", "Channels"),
        ]:
            row = Gtk.ListBoxRow()
            row.set_child(self.nav_page_widget(key, title))
            self.nav_pages[row] = key
            self.page_rows[key] = row
            nav.append(row)

        nav.connect("row-selected", self.on_nav_selected)

        self.build_feed_page()
        self.build_recommended_page()
        self.build_watch_later_page()
        self.build_downloads_page()
        self.build_settings_page()
        self.build_channels_page()
        self.build_search_page()
        self.build_history_page()
        self.build_player_page()
        self.video_queue.connect("items-changed", self.on_transport_items_changed)
        self.playlist_store.connect("items-changed", self.on_transport_items_changed)
        self.update_transport_navigation_buttons()

        self.update_recommended_nav_visibility()
        initial_view = self.initial_view_state()
        self.navigate_to(initial_view, record=False)
        self.schedule_deferred_local_reloads(initial_view)
        GLib.timeout_add_seconds(2, self.maybe_auto_refresh_subscriptions)
        GLib.timeout_add_seconds(5, self.flush_watch_range)
        GLib.timeout_add_seconds(1, self.update_playback_controls)
        GLib.timeout_add_seconds(10, self.maybe_import_youtube_watch_history_once)
        GLib.timeout_add_seconds(3600, self.maybe_import_youtube_watch_history)
        GLib.timeout_add_seconds(
            RECOMMENDED_CACHE_TTL_SECONDS,
            self.maybe_refresh_recommended_cache,
        )
        if self.enable_update_check:
            GLib.timeout_add_seconds(2, self.start_update_check)
        if self.debug_modal:
            GLib.idle_add(self.show_debug_modal, self.debug_modal)

    def show_debug_modal(self, name: str) -> bool:
        if name == "open-url":
            self.show_open_url_dialog()
        elif name == "import-subscriptions":
            self.show_import_subscription_channels_dialog()
        elif name == "sponsorblock":
            self.maybe_show_sponsorblock_prompt()
        elif name == "up-next-quit":
            self.video_queue.append(
                VideoObject(
                    Video(
                        id="debug-up-next",
                        title="Debug Up Next Video",
                        url="https://www.youtube.com/watch?v=debug-up-next",
                    )
                )
            )
            self.queue_pane.set_visible(True)
            self.show_queue_quit_dialog()
        elif name == "update":
            self.show_update_dialog(
                UpdateInfo(
                    current_version="0.0.0",
                    latest_version="999.0.0",
                    project_url="https://pypi.org/project/gtktube/",
                )
            )
        else:
            self.log(
                "unknown debug modal "
                f"{name!r}; expected one of open-url, import-subscriptions, "
                "sponsorblock, up-next-quit, update"
            )
        return False

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
            "recommended": "view-paged-symbolic",
            "search": "system-search-symbolic",
            "history": "document-open-recent-symbolic",
            "watch_later": "clock-symbolic",
            "downloads": "folder-download-symbolic",
            "settings": "preferences-system-symbolic",
            "channels": "folder-symbolic",
        }
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        box.add_css_class("nav-row")
        icon = Gtk.Image.new_from_icon_name(icons[key])
        box.append(icon)
        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        box.append(label)
        return box

    def update_recommended_nav_visibility(self) -> None:
        show = self.service.repository.show_recommended_videos()
        row = self.page_rows.get("recommended")
        if row:
            row.set_visible(show is not False)

    def on_recommended_show_clicked(self, _btn: Gtk.Button) -> None:
        self.service.repository.set_show_recommended_videos(True)
        self.update_recommended_nav_visibility()
        self.reload_settings()
        self.reload_recommended(force=True)

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
        if (
            not self.queue_quit_confirmed
            and self.video_queue.get_n_items() > 0
            and self.queued_videos_not_watch_later()
        ):
            self.show_queue_quit_dialog()
            return True
        self.cleanup()
        return False

    def show_queue_quit_dialog(self) -> None:
        if self.queue_quit_dialog is not None:
            self.queue_quit_dialog.present()
            return

        unsaved_count = len(self.queued_videos_not_watch_later())
        dialog = Gtk.Dialog(
            title="Save Up Next videos?",
            transient_for=self,
            modal=True,
        )

        content = dialog.get_content_area()
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_spacing(18)

        title = Gtk.Label(label="Save Up Next videos to Watch Later?", xalign=0)
        title.add_css_class("title-4")
        content.append(title)

        message = Gtk.Label(
            label="Save Up Next videos to Watch Later before quitting?",
            xalign=0,
            wrap=True,
        )
        content.append(message)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.append(footer)
        footer.append(Gtk.Box(hexpand=True))
        cancel_button = Gtk.Button(label="Cancel")
        discard_button = Gtk.Button(label="Discard Up Next")
        add_button = Gtk.Button(label="Add to Watch Later")
        add_button.add_css_class("suggested-action")
        add_button.set_sensitive(unsaved_count > 0)
        footer.append(cancel_button)
        footer.append(discard_button)
        footer.append(add_button)

        def finish_quit(save_queue: bool) -> None:
            self.queue_quit_dialog = None
            dialog.destroy()
            if save_queue:
                self.add_queue_to_watch_later()
            self.queue_quit_confirmed = True
            self.close()

        def cancel_quit() -> None:
            self.queue_quit_dialog = None
            dialog.destroy()

        cancel_button.connect("clicked", lambda _button: cancel_quit())
        discard_button.connect("clicked", lambda _button: finish_quit(False))
        add_button.connect("clicked", lambda _button: finish_quit(True))
        dialog.connect("close-request", lambda _dialog: (cancel_quit(), True)[1])
        self.queue_quit_dialog = dialog
        dialog.present()

    def queued_videos_not_watch_later(self) -> list[Video]:
        videos: list[Video] = []
        seen_ids: set[str] = set()
        for index in range(self.video_queue.get_n_items()):
            video = self.video_queue.get_item(index).video
            if video.id in seen_ids:
                continue
            seen_ids.add(video.id)
            if not self.service.is_watch_later(video):
                videos.append(video)
        return videos

    def add_queue_to_watch_later(self) -> None:
        videos = self.queued_videos_not_watch_later()
        for video in videos:
            self.service.add_watch_later(video)
        self.reload_watch_later()

    def cleanup(self) -> None:
        if self.cleaned_up:
            return
        self.cleaned_up = True
        self.save_window_size()
        self.flush_watch_range()
        self.stop_pipeline()
        for future in list(self.pending_futures):
            future.cancel()
        deadline = time.monotonic() + 5
        while self.pending_futures and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)
        if self.pending_futures:
            done, _pending = wait(self.pending_futures, timeout=0)
            self.pending_futures.difference_update(done)
        self.executor.shutdown(wait=not self.pending_futures, cancel_futures=True)

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


    def set_status(self, text: str) -> None:
        self.status_text = text

    def log(self, message: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        print(f"{timestamp} gtktube: {message}", file=sys.stderr)
        sys.stderr.flush()

    def verbose_log(self, message: str) -> None:
        if self.verbose:
            self.log(message)

    def run_task(
        self,
        label: str,
        work: Callable[[], T],
        done: Callable[[T], None] | None = None,
        finished: Callable[[], None] | None = None,
        error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.set_status(label)
        future = self.submit_background(work)
        if future is None:
            return

        def finish() -> bool:
            self.pending_futures.discard(future)
            if self.cleaned_up:
                return False
            try:
                result = future.result()
            except BaseException as exc:
                if future.cancelled():
                    self.set_status("Ready")
                elif isinstance(exc, Exception):
                    self.set_status(f"Error: {exc}")
                    if error is not None:
                        try:
                            error(exc)
                        except Exception:
                            self.log(
                                f"{label} error callback failed:\n"
                                f"{traceback.format_exc()}"
                            )

                    if isinstance(exc, ExtractorError):
                        self.log(f"{label} failed: {exc}")
                    else:
                        self.log(
                            f"{label} failed with unexpected exception:\n"
                            f"{traceback.format_exc()}"
                        )
                else:
                    raise
            else:
                if done is not None:
                    try:
                        done(result)
                    except Exception as exc:
                        self.set_status(f"Error: {exc}")
                        self.log(
                            f"{label} completion callback failed:\n"
                            f"{traceback.format_exc()}"
                        )
                    else:
                        self.set_status("Ready")
                else:
                    self.set_status("Ready")
            if finished is not None:
                try:
                    finished()
                except Exception:
                    self.log(
                        f"{label} finished callback failed:\n"
                        f"{traceback.format_exc()}"
                    )
            return False

        def schedule_finish(_future: Future[T]) -> None:
            if self.cleaned_up:
                self.pending_futures.discard(_future)
                return
            GLib.idle_add(finish)

        future.add_done_callback(schedule_finish)

    def submit_background(
        self,
        fn: Callable[..., T],
        *args: object,
        **kwargs: object,
    ) -> Future[T] | None:
        if self.cleaned_up:
            return None
        try:
            future = self.executor.submit(fn, *args, **kwargs)
        except RuntimeError as exc:
            if self.cleaned_up or "shutdown" in str(exc):
                return None
            raise
        self.pending_futures.add(future)
        return future

    def schedule_background_finish(
        self,
        future: Future[Any],
        callback: Callable[[], bool],
    ) -> None:
        def finish() -> bool:
            self.pending_futures.discard(future)
            if self.cleaned_up:
                return False
            return callback()

        def schedule(_future: Future[Any]) -> None:
            if self.cleaned_up:
                self.pending_futures.discard(_future)
                return
            GLib.idle_add(finish)

        future.add_done_callback(schedule)

    def reload_all_local(self) -> None:
        self.reload_feed()
        self.reload_channels()
        self.reload_history()
        self.reload_watch_later()
        self.reload_downloads()
        self.reload_recent_searches()

    def maybe_import_youtube_watch_history_once(self) -> bool:
        if self.cleaned_up:
            return False
        self.maybe_import_youtube_watch_history()
        return False

    def maybe_import_youtube_watch_history(self, force: bool = False) -> bool:
        if self.cleaned_up:
            return False
        if self.importing_youtube_history:
            return True
        if not self.service.repository.import_youtube_watch_history_enabled():
            return True
        if not self.service.repository.yt_dlp_cookies_browser():
            self.set_status("YouTube history import needs a browser cookie source")
            return True
        if not force and not self.service.repository.youtube_watch_history_import_due():
            return True

        self.importing_youtube_history = True

        def done(count: int) -> None:
            self.reload_feed()
            self.reload_history()
            self.reload_channels()
            self.set_status(f"Imported {count} YouTube history videos")

        def finished() -> None:
            self.importing_youtube_history = False

        self.run_task(
            "Importing YouTube watch history...",
            lambda: self.service.import_youtube_watch_history(limit=100),
            done,
            finished=finished,
        )
        return True

    def schedule_deferred_local_reloads(self, initial_view: ViewState) -> None:
        sections = [
            "feed",
            "channels",
            "history",
            "watch_later",
            "downloads",
            "recent_searches",
        ]
        if initial_view.page in sections:
            sections.remove(initial_view.page)

        def load_next() -> bool:
            if self.cleaned_up:
                return False
            try:
                while sections:
                    section = sections.pop(0)
                    if section in self.loaded_local_sections:
                        continue
                    if section == "feed":
                        self.reload_feed()
                    elif section == "channels":
                        self.reload_channels()
                    elif section == "history":
                        self.reload_history()
                    elif section == "watch_later":
                        self.reload_watch_later()
                    elif section == "downloads":
                        self.reload_downloads()
                    elif section == "recent_searches":
                        self.reload_recent_searches()
                    return bool(sections)
            except Exception:
                import traceback
                self.log(f"Error in deferred reload:\n{traceback.format_exc()}")
            return False

        GLib.timeout_add(250, load_next)

    def initial_view_state(self) -> ViewState:
        if not self.service.repository.subscribed_channels():
            return ViewState("search")
        return ViewState("feed")

    def reload_watch_later(self) -> None:
        self.loaded_local_sections.add("watch_later")
        videos = self.service.watch_later_videos()
        self.watch_later_videos = videos
        self.watch_later_add_all_button.set_sensitive(bool(videos))
        self.clear_flowbox(self.watch_later_grid)
        self.grid_generations[id(self.watch_later_grid)] = (
            self.grid_generations.get(id(self.watch_later_grid), 0) + 1
        )
        generation = self.grid_generations[id(self.watch_later_grid)]
        index = 0

        def append_batch() -> bool:
            nonlocal index
            if self.cleaned_up:
                return False
            if self.grid_generations.get(id(self.watch_later_grid), 0) != generation:
                return False
            
            batch_size = 12
            end = min(index + batch_size, len(videos))
            for video in videos[index:end]:
                self.append_video_tile(
                    self.watch_later_grid,
                    video,
                    on_context_menu=self.show_watch_later_context_menu,
                )
            
            index = end
            if index < len(videos):
                return True
            
            has_videos = len(videos) > 0
            self.watch_later_scroller.set_visible(has_videos)
            self.watch_later_empty_box.set_visible(not has_videos)
            return False

        GLib.idle_add(append_batch)

    def play_all_watch_later(self, _button: Gtk.Button) -> None:
        videos = getattr(self, "watch_later_videos", None)
        if videos is None:
            videos = self.service.watch_later_videos()
            self.watch_later_videos = videos
        if not videos:
            return
        while self.video_queue.get_n_items() > 0:
            self.video_queue.remove(0)
        for video in videos[1:]:
            self.video_queue.append(VideoObject(video))
        self.queue_pane.set_visible(self.video_queue.get_n_items() > 0)
        self.playlist_pane.set_visible(False)
        self.playlist_current_index = None
        self.update_playlist_rows()
        self.update_transport_navigation_buttons()
        self.verbose_log(
            "watch later play all "
            f"first_video={videos[0].id} "
            f"up_next_count={self.video_queue.get_n_items()}"
        )
        self.play_video(videos[0], hide_sidebar=False)

    def build_feed_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)

        self.channel_header = self.build_channel_header()
        self.channel_header.set_visible(False)
        self.channel_header.set_margin_bottom(10)
        page.append(self.channel_header)

        self.channel_tabs_notebook = Gtk.Notebook()
        self.channel_tabs_notebook.set_hexpand(True)
        self.channel_tabs_notebook.set_vexpand(True)
        self.channel_tabs_notebook.set_show_tabs(False)
        self.channel_tabs_notebook.set_show_border(False)
        self.channel_tabs_notebook.add_css_class("channel-tabs")
        page.append(self.channel_tabs_notebook)

        # Videos Tab
        videos_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.feed_grid = self.create_video_grid()
        videos_content.append(self.feed_grid)

        self.feed_empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, vexpand=True, hexpand=True
        )
        self.feed_empty_box.set_valign(Gtk.Align.CENTER)
        self.feed_empty_box.set_halign(Gtk.Align.CENTER)
        self.feed_empty_box.set_margin_top(80)
        self.feed_empty_box.set_visible(False)
        empty_icon = Gtk.Image.new_from_icon_name("view-list-symbolic")
        empty_icon.set_pixel_size(64)
        empty_icon.add_css_class("dim-label")
        self.feed_empty_box.append(empty_icon)
        empty_label = Gtk.Label(label="No videos in Feed")
        empty_label.add_css_class("title-4")
        empty_label.add_css_class("dim-label")
        self.feed_empty_box.append(empty_label)
        empty_help = Gtk.Label(
            label="Subscribe to channels, then refresh subscriptions.",
            xalign=0.5,
            wrap=True,
        )
        empty_help.add_css_class("dim-label")
        self.feed_empty_box.append(empty_help)
        videos_content.append(self.feed_empty_box)

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
        videos_content.append(self.feed_loading_box)

        videos_scroller = Gtk.ScrolledWindow(vexpand=True)
        videos_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        videos_scroller.set_child(videos_content)
        videos_scroller.get_vadjustment().connect("value-changed", self.on_feed_scroll)
        
        self.channel_tabs_notebook.append_page(videos_scroller, Gtk.Label(label="Videos"))

        # Shorts Tab
        self.channel_shorts_grid = self.create_video_grid()
        shorts_scroller = Gtk.ScrolledWindow(vexpand=True)
        shorts_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        shorts_scroller.set_child(self.channel_shorts_grid)

        self.channel_tabs_notebook.append_page(shorts_scroller, Gtk.Label(label="Shorts"))

        # Playlists Tab
        self.channel_playlists_grid = self.create_video_grid()
        playlists_scroller = Gtk.ScrolledWindow(vexpand=True)
        playlists_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        playlists_scroller.set_child(self.channel_playlists_grid)
        
        self.channel_tabs_notebook.append_page(playlists_scroller, Gtk.Label(label="Playlists"))

        # Search Tab
        search_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_box.set_margin_top(4)
        search_box.set_margin_start(4)
        search_box.set_margin_end(4)
        search_tab.append(search_box)
        self.channel_video_search_entry = Gtk.Entry(hexpand=True)
        self.channel_video_search_entry.set_placeholder_text("Search this channel")
        self.channel_video_search_entry.connect(
            "activate", self.on_channel_video_search_clicked
        )
        search_box.append(self.channel_video_search_entry)
        self.channel_video_search_icon = Gtk.Image.new_from_icon_name(
            "system-search-symbolic"
        )
        self.channel_video_search_spinner = Gtk.Spinner()
        self.channel_video_search_button = Gtk.Button(
            child=self.channel_video_search_icon
        )
        self.channel_video_search_button.set_tooltip_text("Search this channel")
        self.channel_video_search_button.connect(
            "clicked", self.on_channel_video_search_clicked
        )
        search_box.append(self.channel_video_search_button)

        search_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.channel_search_results_grid = self.create_video_grid()
        self.channel_search_results_grid.set_visible(False)
        search_content.append(self.channel_search_results_grid)

        self.channel_search_empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            vexpand=True,
            hexpand=True,
        )
        self.channel_search_empty_box.set_valign(Gtk.Align.CENTER)
        self.channel_search_empty_box.set_halign(Gtk.Align.CENTER)
        channel_search_empty_icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        channel_search_empty_icon.set_pixel_size(64)
        channel_search_empty_icon.add_css_class("dim-label")
        self.channel_search_empty_box.append(channel_search_empty_icon)
        self.channel_search_empty_title = Gtk.Label(label="Search this channel")
        self.channel_search_empty_title.add_css_class("title-4")
        self.channel_search_empty_title.add_css_class("dim-label")
        self.channel_search_empty_box.append(self.channel_search_empty_title)
        self.channel_search_empty_help = Gtk.Label(
            label="Enter a search term above to find videos from this channel.",
            xalign=0.5,
            wrap=True,
        )
        self.channel_search_empty_help.add_css_class("dim-label")
        self.channel_search_empty_box.append(self.channel_search_empty_help)
        search_content.append(self.channel_search_empty_box)

        search_scroller = Gtk.ScrolledWindow(vexpand=True)
        search_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        search_scroller.set_child(search_content)
        search_tab.append(search_scroller)

        self.channel_tabs_notebook.append_page(search_tab, Gtk.Label(label="Search"))

        self.stack.add_named(page, "feed")

    def build_recommended_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.recommended_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        page.append(self.recommended_stack)

        # Onboarding UI
        onboarding = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        onboarding.set_valign(Gtk.Align.CENTER)
        onboarding.set_halign(Gtk.Align.CENTER)
        onboarding.set_margin_top(80)
        onboarding.set_margin_start(40)
        onboarding.set_margin_end(40)

        title = Gtk.Label(label="Recommended Videos")
        title.add_css_class("title-2")
        onboarding.append(title)

        desc = Gtk.Label(
            label=(
                "Personalized recommendations from YouTube based on your viewing history. "
                "This requires extracting cookies from your browser to identify your account.\n\n"
                "You can also adjust these in Settings."
            ),
            wrap=True,
            max_width_chars=60,
            halign=Gtk.Align.CENTER,
            xalign=0.5
        )
        desc.add_css_class("dim-label")
        onboarding.append(desc)

        # Settings
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        settings_box.set_margin_top(12)
        onboarding.append(settings_box)

        # Browser
        browser_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        browser_row.set_valign(Gtk.Align.CENTER)
        settings_box.append(browser_row)
        
        browser_label = Gtk.Label(label="Which browser should GTKTube get cookies from?", xalign=0, hexpand=True)
        browser_row.append(browser_label)

        self.recommended_onboarding_browser_combo = Gtk.ComboBoxText()
        self.recommended_onboarding_browser_combo.set_valign(Gtk.Align.CENTER)
        self.recommended_onboarding_browser_combo.append("", "None")
        try:
            for b in self.service.supported_browsers():
                self.recommended_onboarding_browser_combo.append(b, b.capitalize())
        except ExtractorError as exc:
            self.log(str(exc))
        
        # Initialize from repository
        self.recommended_onboarding_browser_combo.set_active_id(self.service.repository.yt_dlp_cookies_browser())
        
        self.recommended_onboarding_browser_combo.connect("changed", self.on_recommended_onboarding_browser_changed)
        browser_row.append(self.recommended_onboarding_browser_combo)

        # Buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.CENTER)
        button_box.set_margin_top(12)
        onboarding.append(button_box)

        self.recommended_show_btn = Gtk.Button(label="Show recommendations")
        self.recommended_show_btn.add_css_class("suggested-action")
        self.recommended_show_btn.connect("clicked", self.on_recommended_show_clicked)
        button_box.append(self.recommended_show_btn)

        dismiss_btn = Gtk.Button(label="Hide Recommended")
        dismiss_btn.connect("clicked", self.on_recommended_dismiss_clicked)
        button_box.append(dismiss_btn)

        # Initial state
        self.recommended_show_btn.set_sensitive(bool(self.recommended_onboarding_browser_combo.get_active_id()))

        self.recommended_stack.add_named(onboarding, "onboarding")

        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        loading.set_valign(Gtk.Align.CENTER)
        loading.set_halign(Gtk.Align.CENTER)
        loading_spinner = Gtk.Spinner()
        loading_spinner.start()
        loading.append(loading_spinner)
        loading_label = Gtk.Label(label="Loading recommendations...")
        loading_label.add_css_class("dim-label")
        loading.append(loading_label)
        self.recommended_stack.add_named(loading, "loading")

        # Grid UI
        grid_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        grid_content.set_margin_top(12)
        grid_content.set_margin_start(12)
        grid_content.set_margin_end(12)
        self.recommended_grid = self.create_video_grid()
        grid_content.append(self.recommended_grid)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(grid_content)

        self.recommended_stack.add_named(scroller, "grid")

        self.stack.add_named(page, "recommended")

    def build_channel_header(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        header.set_margin_top(4)
        header.set_margin_bottom(8)
        header.set_margin_start(8)
        header.set_margin_end(8)

        self.channel_header_thumbnail = Gtk.Picture()
        self.channel_header_thumbnail.add_css_class("channel-avatar")
        self.channel_header_thumbnail.set_size_request(72, 72)
        self.channel_header_thumbnail.set_can_shrink(False)
        self.channel_header_thumbnail.set_content_fit(Gtk.ContentFit.COVER)
        self.clip_channel_avatar(self.channel_header_thumbnail)
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

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_box.set_margin_top(12)
        search_box.set_margin_start(12)
        search_box.set_margin_end(12)
        page.append(search_box)
        self.channel_search_entry = Gtk.Entry(hexpand=True)
        self.channel_search_entry.set_placeholder_text("Search subscribed channels")
        self.channel_search_entry.connect("activate", self.on_channel_search_clicked)
        search_box.append(self.channel_search_entry)
        channel_search_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("system-search-symbolic")
        )
        channel_search_button.set_tooltip_text("Search channels")
        channel_search_button.connect("clicked", self.on_channel_search_clicked)
        search_box.append(channel_search_button)

        channel_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        channel_results.set_margin_bottom(12)
        channel_results.set_margin_start(12)
        channel_results.set_margin_end(12)

        self.channel_grid = self.create_channel_grid()
        channel_results.append(self.channel_grid)

        self.channel_empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            vexpand=True,
            hexpand=True,
        )
        self.channel_empty_box.set_valign(Gtk.Align.CENTER)
        self.channel_empty_box.set_halign(Gtk.Align.CENTER)
        channel_empty_icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        channel_empty_icon.set_pixel_size(64)
        channel_empty_icon.add_css_class("dim-label")
        self.channel_empty_box.append(channel_empty_icon)
        self.channel_empty_title = Gtk.Label(label="No subscribed channels")
        self.channel_empty_title.add_css_class("title-4")
        self.channel_empty_title.add_css_class("dim-label")
        self.channel_empty_box.append(self.channel_empty_title)
        self.channel_empty_help = Gtk.Label(
            label="Open a channel URL or subscribe from a video to add channels."
        )
        self.channel_empty_help.add_css_class("dim-label")
        self.channel_empty_box.append(self.channel_empty_help)
        self.channel_import_button = Gtk.Button(label="Import channels")
        self.channel_import_button.add_css_class("suggested-action")
        self.channel_import_button.set_halign(Gtk.Align.CENTER)
        self.channel_import_button.connect(
            "clicked",
            self.on_import_subscription_channels_clicked,
        )
        self.channel_empty_box.append(self.channel_import_button)
        channel_results.append(self.channel_empty_box)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(channel_results)
        page.append(scroller)

        self.stack.add_named(page, "channels")

    def build_search_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_box.set_margin_top(12)
        search_box.set_margin_start(12)
        search_box.set_margin_end(12)
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
        search_results.set_margin_bottom(12)
        search_results.set_margin_start(12)
        search_results.set_margin_end(12)

        self.search_empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            vexpand=True,
            hexpand=True,
        )
        self.search_empty_box.set_valign(Gtk.Align.CENTER)
        self.search_empty_box.set_halign(Gtk.Align.CENTER)
        search_empty_icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        search_empty_icon.set_pixel_size(64)
        search_empty_icon.add_css_class("dim-label")
        self.search_empty_box.append(search_empty_icon)
        self.search_empty_title = Gtk.Label(label="Search YouTube")
        self.search_empty_title.add_css_class("title-4")
        self.search_empty_title.add_css_class("dim-label")
        self.search_empty_box.append(self.search_empty_title)
        self.search_empty_help = Gtk.Label(
            label="Enter a search term to find videos and channels."
        )
        self.search_empty_help.add_css_class("dim-label")
        self.search_empty_box.append(self.search_empty_help)
        search_results.append(self.search_empty_box)

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

    def build_watch_later_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(12)
        header.set_margin_start(12)
        header.set_margin_end(12)
        title = Gtk.Label(label="Watch Later", xalign=0, hexpand=True)
        title.add_css_class("title-4")
        header.append(title)
        self.watch_later_add_all_button = Gtk.Button(label="Play all")
        self.watch_later_add_all_button.set_sensitive(False)
        self.watch_later_add_all_button.connect(
            "clicked", self.play_all_watch_later
        )
        header.append(self.watch_later_add_all_button)
        page.append(header)

        self.watch_later_grid = self.create_video_grid()
        self.watch_later_grid.set_margin_top(4)
        self.watch_later_grid.set_margin_bottom(12)
        self.watch_later_grid.set_margin_start(12)
        self.watch_later_grid.set_margin_end(12)
        self.watch_later_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.watch_later_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.watch_later_scroller.set_child(self.watch_later_grid)
        page.append(self.watch_later_scroller)

        self.watch_later_empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, vexpand=True, hexpand=True
        )
        self.watch_later_empty_box.set_valign(Gtk.Align.CENTER)
        self.watch_later_empty_box.set_halign(Gtk.Align.CENTER)
        empty_icon = Gtk.Image.new_from_icon_name("clock-symbolic")
        empty_icon.set_pixel_size(64)
        empty_icon.add_css_class("dim-label")
        self.watch_later_empty_box.append(empty_icon)
        empty_label = Gtk.Label(label="No videos in Watch Later")
        empty_label.add_css_class("title-4")
        empty_label.add_css_class("dim-label")
        self.watch_later_empty_box.append(empty_label)
        page.append(self.watch_later_empty_box)

        self.stack.add_named(page, "watch_later")

    def build_downloads_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(12)
        header.set_margin_start(12)
        header.set_margin_end(12)
        title = Gtk.Label(label="Downloads", xalign=0, hexpand=True)
        title.add_css_class("title-4")
        header.append(title)

        refresh_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        )
        refresh_button.set_tooltip_text("Refresh downloads")
        refresh_button.connect("clicked", lambda _button: self.reload_downloads())
        header.append(refresh_button)

        open_folder_button = Gtk.Button(label="Open folder")
        open_folder_button.connect("clicked", self.on_open_downloads_folder)
        header.append(open_folder_button)
        page.append(header)

        downloads_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        downloads_content.set_margin_top(4)
        downloads_content.set_margin_bottom(12)
        downloads_content.set_margin_start(12)
        downloads_content.set_margin_end(12)

        self.downloads_grid = self.create_video_grid()
        downloads_content.append(self.downloads_grid)

        self.downloads_empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            vexpand=True,
            hexpand=True,
        )
        self.downloads_empty_box.set_valign(Gtk.Align.CENTER)
        self.downloads_empty_box.set_halign(Gtk.Align.CENTER)
        empty_icon = Gtk.Image.new_from_icon_name("folder-download-symbolic")
        empty_icon.set_pixel_size(64)
        empty_icon.add_css_class("dim-label")
        self.downloads_empty_box.append(empty_icon)
        empty_label = Gtk.Label(label="No downloads")
        empty_label.add_css_class("title-4")
        empty_label.add_css_class("dim-label")
        self.downloads_empty_box.append(empty_label)
        downloads_content.append(self.downloads_empty_box)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(downloads_content)
        page.append(scroller)

        self.stack.add_named(page, "downloads")

    def reload_downloads(self) -> None:
        self.loaded_local_sections.add("downloads")
        downloads = self.service.downloaded_videos(self.download_dir)
        self.clear_flowbox(self.downloads_grid)
        self.grid_generations[id(self.downloads_grid)] = (
            self.grid_generations.get(id(self.downloads_grid), 0) + 1
        )
        for video, path in downloads:
            self.append_video_tile(
                self.downloads_grid,
                video,
                on_context_menu=(
                    lambda parent, _video, x, y, v=video, p=path:
                    self.show_download_context_menu(parent, v, p, x, y)
                ),
            )
        has_downloads = bool(downloads)
        self.downloads_grid.set_visible(has_downloads)
        self.downloads_empty_box.set_visible(not has_downloads)

    def show_download_context_menu(
        self,
        parent: Gtk.Widget,
        video: Video,
        path: Path,
        x: float,
        y: float,
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(parent)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        actions.set_margin_top(6)
        actions.set_margin_bottom(6)
        actions.set_margin_start(6)
        actions.set_margin_end(6)
        popover.set_child(actions)

        copy_url = Gtk.Button(label="Copy URL")
        copy_url.add_css_class("flat")
        copy_url.set_halign(Gtk.Align.FILL)
        copy_url.connect(
            "clicked", lambda _button: self.activate_video_menu(popover, video, "copy")
        )
        actions.append(copy_url)

        open_video = Gtk.Button(label="Open video")
        open_video.add_css_class("flat")
        open_video.set_halign(Gtk.Align.FILL)
        open_video.connect(
            "clicked", lambda _button: self.activate_video_menu(popover, video, "video")
        )
        actions.append(open_video)

        delete_file = Gtk.Button(label="Delete download")
        delete_file.add_css_class("flat")
        delete_file.set_halign(Gtk.Align.FILL)
        delete_file.connect(
            "clicked", lambda _button: self.delete_download(popover, video, path)
        )
        actions.append(delete_file)

        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()

    def delete_download(
        self,
        popover: Gtk.Popover,
        video: Video,
        path: Path,
    ) -> None:
        popover.popdown()
        popover.unparent()
        try:
            path.unlink()
        except OSError as exc:
            self.set_status(f"Could not delete download: {exc}")
            return
        self.set_status(f"Deleted download for {video.title}")
        self.reload_downloads()

    def download_video(self, video: Video) -> None:
        download_id = object()
        self.download_menu_items[download_id] = {
            "video": video,
            "title": video.title,
            "status": "Starting",
            "progress": 0.0,
            "download_parts": {},
            "done": False,
        }
        self.update_active_downloads_menu(rebuild_rows=True)

        def progress(update: dict[str, object]) -> None:
            if self.cleaned_up:
                return
            GLib.idle_add(self.update_download_progress, download_id, update)

        self.set_status(f"Downloading {video.title}...")
        future = self.submit_background(
            self.service.download_video,
            video,
            self.download_dir,
            progress=progress,
        )
        if future is None:
            self.download_menu_items.pop(download_id, None)
            self.update_active_downloads_menu(rebuild_rows=True)
            return

        def done() -> bool:
            item = self.download_menu_items.get(download_id)
            try:
                path = future.result()
            except Exception as exc:
                if item is not None:
                    item["status"] = "Failed"
                    item["done"] = True
                    item["error"] = str(exc)
                    item["progress"] = 0.0
                    self.update_active_downloads_menu(rebuild_rows=True)
                self.set_status(f"Download failed: {exc}")
                self.log(f"Download failed for {video.id}: {exc}")
                return False
            if item is not None:
                item["status"] = ""
                item["done"] = True
                item["path"] = path
                item["progress"] = 1.0
                self.update_active_downloads_menu(rebuild_rows=True)
            self.reload_downloads()
            self.set_status(f"Downloaded {path.name}")
            return False

        self.schedule_background_finish(future, done)

    def update_download_progress(
        self,
        download_id: object,
        update: dict[str, object],
    ) -> bool:
        item = self.download_menu_items.get(download_id)
        if item is None or item.get("done"):
            return False
        status = str(update.get("status") or "")
        part = self.download_progress_part(update)
        if status == "downloading":
            downloaded = self.download_progress_number(update.get("downloaded_bytes"))
            total = self.download_progress_number(update.get("total_bytes"))
            if total <= 0:
                total = self.download_progress_number(update.get("total_bytes_estimate"))
            if total > 0:
                self.set_download_part_progress(item, part, downloaded / total)
            else:
                item["status"] = "Downloading"
        elif status == "finished":
            self.set_download_part_progress(item, part, 1.0)
            if float(item.get("progress") or 0.0) >= 1.0:
                item["status"] = "Processing"
        self.update_download_menu_item_widgets(item)
        self.update_active_downloads_menu(rebuild_rows=False)
        return False

    def download_progress_part(self, update: dict[str, object]) -> str:
        info = update.get("info_dict")
        if not isinstance(info, dict):
            return "single"
        vcodec = str(info.get("vcodec") or "")
        acodec = str(info.get("acodec") or "")
        has_video = bool(vcodec and vcodec != "none")
        has_audio = bool(acodec and acodec != "none")
        if has_video and not has_audio:
            return "video"
        if has_audio and not has_video:
            return "audio"
        return "single"

    def set_download_part_progress(
        self,
        item: dict[str, Any],
        part: str,
        fraction: float,
    ) -> None:
        parts = item.setdefault("download_parts", {})
        if not isinstance(parts, dict):
            parts = {}
            item["download_parts"] = parts
        parts[part] = max(0.0, min(1.0, fraction))
        if "single" in parts:
            total = float(parts["single"])
        else:
            total = (
                0.9 * float(parts.get("video", 0.0))
                + 0.1 * float(parts.get("audio", 0.0))
            )
        item["progress"] = max(float(item.get("progress") or 0.0), min(1.0, total))
        item["status"] = f"{round(float(item['progress']) * 100)}%"

    def download_progress_number(self, value: object) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        return 0.0

    def update_active_downloads_menu(self, rebuild_rows: bool = True) -> None:
        item_count = len(self.download_menu_items)
        active_count = sum(
            1 for item in self.download_menu_items.values() if not item.get("done")
        )
        self.downloads_menu_button.set_visible(item_count > 0)
        if item_count == 0:
            self.downloads_menu_progress_label.set_visible(False)
            self.downloads_menu_popover.popdown()
            return
        if active_count > 0:
            active_progress = [
                float(item.get("progress") or 0.0)
                for item in self.download_menu_items.values()
                if not item.get("done")
            ]
            aggregate_progress = sum(active_progress) / max(1, len(active_progress))
            self.downloads_menu_progress_label.set_label(
                f"{round(aggregate_progress * 100)}%"
            )
            self.downloads_menu_progress_label.set_visible(True)
            self.downloads_menu_button.set_tooltip_text(
                "Active download"
                if active_count == 1
                else f"{active_count} active downloads"
            )
        else:
            self.downloads_menu_progress_label.set_visible(False)
            self.downloads_menu_button.set_tooltip_text("Downloads")
        if rebuild_rows:
            self.clear_listbox(self.active_downloads_list)
            for item in reversed(list(self.download_menu_items.values())):
                self.active_downloads_list.append(self.active_download_row(item))
        else:
            for item in self.download_menu_items.values():
                self.update_download_menu_item_widgets(item)

    def update_download_menu_item_widgets(self, item: dict[str, Any]) -> None:
        status_label = item.get("status_label")
        if isinstance(status_label, Gtk.Label):
            status_label.set_label(str(item.get("status") or ""))
        progress_bar = item.get("progress_bar")
        if isinstance(progress_bar, Gtk.ProgressBar):
            progress_bar.set_fraction(float(item.get("progress") or 0.0))

    def active_download_row(self, item: dict[str, Any]) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        video = item.get("video")
        if isinstance(video, Video):
            if not item.get("error"):
                row.set_activatable(True)
                row.connect("activate", self.on_download_menu_row_activated, video)
        if isinstance(video, Video) and video.thumbnail_url:
            thumbnail = Gtk.Picture()
            thumbnail.set_size_request(57, 32)
            thumbnail.set_can_shrink(False)
            thumbnail.set_content_fit(Gtk.ContentFit.COVER)
            self.load_thumbnail(
                video,
                thumbnail,
                width=57,
                height=32,
                preserve_aspect=False,
            )
            box.append(thumbnail)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        content.set_hexpand(True)
        content.set_valign(Gtk.Align.CENTER)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(
            label=str(item.get("title") or "Download"),
            xalign=0,
            hexpand=True,
        )
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(38)
        header.append(title)

        status = Gtk.Label(label=str(item.get("status") or ""), xalign=1)
        status.add_css_class("dim-label")
        item["status_label"] = status
        header.append(status)
        error = item.get("error")
        if error:
            error_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            error_icon.set_tooltip_text(str(error))
            error_icon.add_css_class("dim-label")
            header.append(error_icon)
        content.append(header)

        if not item.get("done"):
            progress_bar = Gtk.ProgressBar()
            progress_bar.set_fraction(float(item.get("progress") or 0.0))
            progress_bar.set_size_request(280, -1)
            item["progress_bar"] = progress_bar
            content.append(progress_bar)
        else:
            item.pop("progress_bar", None)
        box.append(content)
        row.set_child(box)
        return row

    def on_download_menu_row_activated(
        self,
        _row: Gtk.ListBoxRow,
        video: Video,
    ) -> None:
        self.downloads_menu_popover.popdown()
        self.play_video(video)

    def clear_listbox(self, listbox: Gtk.ListBox) -> None:
        child = listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            listbox.remove(child)
            child = next_child

    def on_open_downloads_folder(self, _button: Gtk.Button) -> None:
        self.open_local_path(self.download_dir)

    def open_local_path(self, path: Path) -> None:
        try:
            Gtk.show_uri(self, path.resolve().as_uri(), Gdk.CURRENT_TIME)
        except Exception as exc:
            self.set_status(f"Could not open {path}: {exc}")

    def build_history_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_box.set_margin_top(12)
        search_box.set_margin_start(12)
        search_box.set_margin_end(12)
        page.append(search_box)
        self.history_entry = Gtk.Entry(hexpand=True)
        self.history_entry.set_placeholder_text("Search watch history")
        self.history_entry.connect("activate", self.on_history_search_changed)
        search_box.append(self.history_entry)
        history_button = Gtk.Button(label="Search history")
        history_button.connect("clicked", self.on_history_search_changed)
        search_box.append(history_button)

        history_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        history_results.set_margin_bottom(12)
        history_results.set_margin_start(12)
        history_results.set_margin_end(12)

        self.history_grid = self.create_video_grid()
        history_results.append(self.history_grid)

        self.history_empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            vexpand=True,
            hexpand=True,
        )
        self.history_empty_box.set_valign(Gtk.Align.CENTER)
        self.history_empty_box.set_halign(Gtk.Align.CENTER)
        history_empty_icon = Gtk.Image.new_from_icon_name("document-open-recent-symbolic")
        history_empty_icon.set_pixel_size(64)
        history_empty_icon.add_css_class("dim-label")
        self.history_empty_box.append(history_empty_icon)
        self.history_empty_title = Gtk.Label(label="No watch history")
        self.history_empty_title.add_css_class("title-4")
        self.history_empty_title.add_css_class("dim-label")
        self.history_empty_box.append(self.history_empty_title)
        self.history_empty_help = Gtk.Label(
            label="Videos you watch will appear here."
        )
        self.history_empty_help.add_css_class("dim-label")
        self.history_empty_box.append(self.history_empty_help)
        history_results.append(self.history_empty_box)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(history_results)
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
        self.close_player_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("window-close-symbolic")
        )
        self.close_player_button.set_tooltip_text("Close mini player")
        self.close_player_button.connect("clicked", self.on_close_player_clicked)
        mini_header.append(self.close_player_button)
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
        self.queue_pane.set_size_request(160, -1)
        self.queue_pane.set_hexpand(False)
        self.queue_pane.set_visible(False)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(14)
        header.set_margin_bottom(0)
        header.set_margin_start(12)
        header.set_margin_end(12)
        label = Gtk.Label(label="Up Next")
        label.add_css_class("heading")
        header.append(label)
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_button.add_css_class("flat")
        close_button.set_tooltip_text("Close")
        close_button.set_size_request(22, 22)
        close_button.set_valign(Gtk.Align.CENTER)
        close_button.connect("clicked", lambda _: self.queue_pane.set_visible(False))
        header.append(close_button)
        self.queue_pane.append(header)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.queue_list = Gtk.ListBox()
        self.queue_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.queue_list.add_css_class("sidebar-list")
        self.queue_list.bind_model(self.video_queue, self.create_queue_row)
        scroller.set_child(self.queue_list)
        self.queue_pane.append(scroller)

    def build_playlist_pane(self) -> None:
        self.playlist_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.playlist_pane.add_css_class("queue-pane")
        self.playlist_pane.set_size_request(160, -1)
        self.playlist_pane.set_visible(False)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(14)
        header.set_margin_bottom(0)
        header.set_margin_start(12)
        header.set_margin_end(12)
        label = Gtk.Label(label="Playlist")
        label.add_css_class("heading")
        header.append(label)
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_button.add_css_class("flat")
        close_button.set_tooltip_text("Close")
        close_button.set_size_request(22, 22)
        close_button.set_valign(Gtk.Align.CENTER)
        close_button.connect("clicked", lambda _: self.playlist_pane.set_visible(False))
        header.append(close_button)
        self.playlist_pane.append(header)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.playlist_list = Gtk.ListBox()
        self.playlist_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.playlist_list.add_css_class("sidebar-list")
        self.playlist_list.bind_model(self.playlist_store, self.create_playlist_row)
        scroller.set_child(self.playlist_list)
        self.playlist_pane.append(scroller)

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
        video_right_click = Gtk.GestureClick()
        video_right_click.set_button(3)
        video_right_click.connect("pressed", self.on_current_video_context_menu)
        self.video.add_controller(video_right_click)

        self.video_overlay = Gtk.Overlay()
        self.video_overlay.set_child(self.video)
        self.miniplayer_video_container.append(self.video_overlay)

        self.player_loading_overlay = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        self.player_loading_overlay.add_css_class("player-loading-overlay")
        self.player_loading_overlay.set_halign(Gtk.Align.FILL)
        self.player_loading_overlay.set_valign(Gtk.Align.FILL)
        self.player_loading_overlay.set_hexpand(True)
        self.player_loading_overlay.set_vexpand(True)
        self.player_loading_overlay.set_visible(False)
        self.player_loading_spinner = Gtk.Spinner()
        self.player_loading_spinner.set_halign(Gtk.Align.CENTER)
        self.player_loading_spinner.set_valign(Gtk.Align.END)
        self.player_loading_spinner.set_vexpand(True)
        self.player_loading_overlay.append(self.player_loading_spinner)
        self.player_loading_label = Gtk.Label(label="Resolving video...")
        self.player_loading_label.add_css_class("dim-label")
        self.player_loading_label.set_halign(Gtk.Align.CENTER)
        self.player_loading_label.set_valign(Gtk.Align.START)
        self.player_loading_label.set_vexpand(True)
        self.player_loading_overlay.append(self.player_loading_label)
        self.video_overlay.add_overlay(self.player_loading_overlay)

        self.player_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.miniplayer_controls_container.append(self.player_controls)
        self.play_pause_icon = Gtk.Image.new_from_icon_name(
            "media-playback-start-symbolic"
        )
        self.previous_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("media-skip-backward-symbolic")
        )
        self.previous_button.set_tooltip_text("Previous playlist video")
        self.previous_button.connect("clicked", self.on_previous_clicked)
        self.previous_button.set_visible(False)
        self.player_controls.append(self.previous_button)

        self.play_pause_button = Gtk.Button(child=self.play_pause_icon)
        self.play_pause_button.set_tooltip_text("Play")
        self.play_pause_button.connect("clicked", self.on_play_pause_clicked)
        self.player_controls.append(self.play_pause_button)

        self.next_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("media-skip-forward-symbolic")
        )
        self.next_button.set_tooltip_text("Next video")
        self.next_button.connect("clicked", self.on_next_clicked)
        self.next_button.set_visible(False)
        self.player_controls.append(self.next_button)

        self.elapsed_label = Gtk.Label(label="0:00")
        self.player_controls.append(self.elapsed_label)

        self.timeline_overlay = Gtk.Overlay(hexpand=True)
        self.sponsorblock_timeline = Gtk.DrawingArea(hexpand=True)
        self.sponsorblock_timeline.add_css_class("timeline-overlay")
        self.sponsorblock_timeline.set_draw_func(self.draw_sponsorblock_timeline)
        self.timeline_overlay.set_child(self.sponsorblock_timeline)

        self.scrubber = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.scrubber.set_hexpand(True)
        self.scrubber.set_draw_value(False)
        self.scrubber.connect("change-value", self.on_scrub_changed)
        self.timeline_overlay.add_overlay(self.scrubber)
        self.player_controls.append(self.timeline_overlay)

        self.duration_label = Gtk.Label(label="0:00")
        self.player_controls.append(self.duration_label)

        self.quality_combo = Gtk.ComboBoxText()
        self.populate_quality_combo()
        self.update_quality_combo_tooltip()
        self.quality_combo.connect("changed", self.on_quality_changed)
        self.player_controls.append(self.quality_combo)

        self.speed_combo = Gtk.ComboBoxText()
        for rate in PLAYBACK_RATES:
            self.speed_combo.append(self.speed_id(rate), self.speed_label(rate))
        self.speed_combo.set_active_id(self.speed_id(self.playback_rate))
        self.speed_combo.connect("changed", self.on_speed_changed)
        self.player_controls.append(self.speed_combo)

        self.caption_icon = Gtk.Image.new_from_icon_name(
            "media-view-subtitles-symbolic"
        )
        self.caption_button = Gtk.MenuButton(child=self.caption_icon)
        self.caption_button.set_tooltip_text("Subtitles")
        self.caption_button.set_sensitive(False)
        self.caption_button.set_visible(False)
        self.player_controls.append(self.caption_button)

        self.player_chapters_icon = Gtk.Image.new_from_icon_name(
            "view-list-symbolic"
        )
        self.player_chapters_button = Gtk.MenuButton(child=self.player_chapters_icon)
        self.player_chapters_button.set_tooltip_text("Chapters")
        self.player_chapters_button.set_sensitive(False)
        self.player_chapters_button.set_visible(False)
        self.player_controls.append(self.player_chapters_button)

        self.fullscreen_icon = Gtk.Image.new_from_icon_name("view-fullscreen-symbolic")
        self.fullscreen_button = Gtk.Button(child=self.fullscreen_icon)
        self.fullscreen_button.set_tooltip_text("Fullscreen video")
        self.fullscreen_button.connect("clicked", self.on_fullscreen_clicked)
        self.player_controls.append(self.fullscreen_button)

        self.player_metadata = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.player_metadata.set_margin_top(4)
        self.player_metadata.set_margin_bottom(12)
        self.player_metadata.set_margin_start(12)
        self.player_metadata.set_margin_end(12)
        self.player_metadata.set_visible(False)
        self.miniplayer.append(self.player_metadata)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.player_metadata.append(header_box)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        header_box.append(text_box)

        self.player_title = Gtk.Label(label="No video loaded", xalign=0, hexpand=True)
        self.player_title.set_wrap(True)
        text_box.append(self.player_title)

        self.player_meta = Gtk.Label(label="", xalign=0)
        self.player_meta.set_wrap(True)
        self.player_meta.add_css_class("dim-label")
        text_box.append(self.player_meta)

        self.player_chapter_label = Gtk.Label(label="", xalign=0)
        self.player_chapter_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.player_chapter_label.add_css_class("dim-label")
        self.player_chapter_label.set_visible(False)
        text_box.append(self.player_chapter_label)

        player_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        player_actions.set_valign(Gtk.Align.CENTER)
        header_box.append(player_actions)

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

        self.player_description_icon = Gtk.Image.new_from_icon_name(
            "text-x-generic-symbolic"
        )
        self.player_description_button = Gtk.ToggleButton(
            child=self.player_description_icon
        )
        self.player_description_button.set_tooltip_text("Show description")
        self.player_description_button.set_sensitive(False)
        self.player_description_button.connect(
            "toggled", self.on_player_description_toggled
        )
        player_actions.append(self.player_description_button)

        self.player_chapters_popover = Gtk.Popover()
        self.player_chapters_button.set_popover(self.player_chapters_popover)

        self.caption_popover = Gtk.Popover()
        self.caption_button.set_popover(self.caption_popover)
        captions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.caption_popover.set_child(captions_box)
        self.caption_list = Gtk.ListBox()
        self.caption_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.caption_list.connect("row-activated", self.on_caption_row_activated)
        captions_scroller = Gtk.ScrolledWindow(hexpand=True)
        captions_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        captions_scroller.set_propagate_natural_height(True)
        captions_scroller.set_propagate_natural_width(True)
        captions_scroller.set_min_content_width(260)
        captions_scroller.set_max_content_height(220)
        captions_scroller.set_child(self.caption_list)
        captions_box.append(captions_scroller)

        chapters_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        chapters_box.set_size_request(320, 260)
        self.player_chapters_popover.set_child(chapters_box)
        self.player_chapters_list = Gtk.ListBox()
        self.player_chapters_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.player_chapters_list.connect(
            "row-activated", self.on_chapter_row_activated
        )
        chapters_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        chapters_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        chapters_scroller.set_child(self.player_chapters_list)
        chapters_box.append(chapters_scroller)

        self.description_text = ""
        self.description_window: Gtk.Window | None = None

        self.stack.add_named(page, "player")

    def create_channel_grid(self) -> Gtk.FlowBox:
        grid = self.create_video_grid()
        grid.set_max_children_per_line(10)
        return grid

    def on_refresh_subscriptions(self, _button: Gtk.Button) -> None:
        self.refresh_subscriptions()

    def maybe_auto_refresh_subscriptions(self) -> bool:
        if self.cleaned_up:
            return False
        channels = self.service.repository.subscribed_channels()
        if not any(
            self.service.repository.channel_needs_refresh(channel.id)
            for channel in channels
        ):
            return False
        self.refresh_subscriptions(max_workers=1)
        return False

    def refresh_subscriptions(self, max_workers: int | None = None) -> None:
        if self.refreshing_subscriptions:
            return

        def done(_result: None) -> None:
            self.reload_channels()
            self.schedule_incremental_feed_refresh()

        def finished() -> None:
            self.refreshing_subscriptions = False
            self.refreshing_channel_ids.clear()
            self.reload_channels()
            self.schedule_incremental_feed_refresh()
            self.set_context_refresh_loading(False)

        def progress(channel: Channel, event: str) -> None:
            GLib.idle_add(self.on_subscription_refresh_progress, channel, event)

        self.refreshing_subscriptions = True
        self.set_context_refresh_loading(True)
        self.run_task(
            "Refreshing subscriptions...",
            lambda: self.service.refresh_subscriptions(
                max_workers=max_workers
                if max_workers is not None
                else self.service.repository.refresh_worker_count(),
                progress=progress,
            ),
            done,
            finished=finished,
        )

    def on_subscription_refresh_progress(
        self,
        channel: Channel,
        event: str,
    ) -> bool:
        if event == "start":
            self.set_channel_refreshing(channel.id, True)
            return False
        if event in {"finish", "failed"}:
            self.set_channel_refreshing(channel.id, False)
            if event == "failed":
                self.reload_channels()
            return False
        if event == "updated":
            self.set_channel_refreshing(channel.id, False)
            self.schedule_incremental_feed_refresh(channel.id)
            if self.current_view and self.current_view.channel_id == channel.id:
                self.apply_view_state(self.current_view)
            return False
        return False

    def schedule_incremental_feed_refresh(self, channel_id: str | None = None) -> None:
        if channel_id is not None:
            self.pending_feed_refresh_channel_ids.add(channel_id)
        if self.pending_feed_refresh:
            return
        self.pending_feed_refresh = True

        def refresh() -> bool:
            self.pending_feed_refresh = False
            channel_ids = set(self.pending_feed_refresh_channel_ids)
            self.pending_feed_refresh_channel_ids.clear()
            if self.cleaned_up:
                return False
            if self.current_view and self.current_view.page == "feed":
                if self.current_view.channel_id is None:
                    self.refresh_feed_tiles(channel_ids)
                else:
                    self.apply_view_state(self.current_view)
            return False

        GLib.timeout_add(500, refresh)

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
        self.loading_more_videos = True
        self.set_feed_loading(True, "Loading more...")

        cached_videos = self.service.repository.channel_videos(channel.id, next_limit)
        if len(cached_videos) > current_limit:
            self.channel_video_limits[channel.id] = next_limit
            self.append_video_grid_batched(
                self.feed_grid,
                cached_videos[current_limit:next_limit],
                self.finish_local_feed_load,
            )
            return

        def done(_videos: list[Video]) -> None:
            if self.current_view and self.current_view.channel_id == channel.id:
                self.channel_video_limits[channel.id] = next_limit
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
                return self.service.refresh_channel(
                    channel,
                    limit=30,
                    start=current_limit + 1,
                    refresh_metadata=False,
                )
            except Exception:
                GLib.idle_add(failed_done)
                raise

        self.run_task(
            f"Loading more from {channel.title}...",
            work,
            done,
        )

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

    def on_recommended_onboarding_browser_changed(self, combo: Gtk.ComboBoxText) -> None:
        browser = combo.get_active_id()
        self.recommended_show_btn.set_sensitive(bool(browser))
        if browser is not None:
            self.service.repository.set_yt_dlp_cookies_browser(browser)
            self.clear_recommended_cache()
            self.reload_settings()

    def on_recommended_show_clicked(self, _btn: Gtk.Button) -> None:
        self.service.repository.set_show_recommended_videos(True)
        self.update_recommended_nav_visibility()
        self.reload_settings()
        self.reload_recommended(force=True)

    def on_recommended_dismiss_clicked(self, _btn: Gtk.Button) -> None:
        self.service.repository.set_show_recommended_videos(False)
        self.update_recommended_nav_visibility()
        self.reload_settings()
        self.navigate_to(ViewState("feed"))

    def clear_recommended_cache(self) -> None:
        self.recommended_cache = None
        self.recommended_cache_browser = None
        self.recommended_cache_loaded_at = 0.0

    def recommended_cache_fresh(self, browser: str) -> bool:
        if self.recommended_cache is None:
            return False
        if self.recommended_cache_browser != browser:
            return False
        age = time.monotonic() - self.recommended_cache_loaded_at
        return age < RECOMMENDED_CACHE_TTL_SECONDS

    def maybe_refresh_recommended_cache(self) -> bool:
        if self.cleaned_up:
            return False
        if self.current_view is None or self.current_view.page != "recommended":
            return True
        show = self.service.repository.show_recommended_videos()
        browser = self.service.repository.yt_dlp_cookies_browser()
        if show is True and browser and not self.recommended_cache_fresh(browser):
            self.reload_recommended(force=True)
        return True

    def show_cached_recommended(self) -> None:
        if self.recommended_cache is None:
            return
        self.recommended_stack.set_visible_child_name("grid")
        self.clear_flowbox(self.recommended_grid)
        self.populate_video_grid(self.recommended_grid, self.recommended_cache)

    def update_recommended_cached_video(self, video: Video) -> None:
        if self.recommended_cache is None:
            return
        updated = False
        videos: list[Video] = []
        for cached in self.recommended_cache:
            if cached.id == video.id:
                videos.append(video)
                updated = True
            else:
                videos.append(cached)
        if not updated:
            return
        self.recommended_cache = videos
        if self.current_view is not None and self.current_view.page == "recommended":
            self.show_cached_recommended()

    def reload_recommended(self, force: bool = False) -> None:
        show = self.service.repository.show_recommended_videos()
        browser = self.service.repository.yt_dlp_cookies_browser()

        if show is True and browser:
            if not force and self.recommended_cache_fresh(browser):
                self.show_cached_recommended()
                return
            if self.loading_recommended:
                if self.recommended_cache is not None:
                    self.show_cached_recommended()
                else:
                    self.recommended_stack.set_visible_child_name("loading")
                return
            self.recommended_stack.set_visible_child_name("loading")
            self.clear_flowbox(self.recommended_grid)
            self.loading_recommended = True

            def done(videos: list[Video]) -> None:
                current_browser = self.service.repository.yt_dlp_cookies_browser()
                if current_browser != browser:
                    return
                self.recommended_cache = videos
                self.recommended_cache_browser = browser
                self.recommended_cache_loaded_at = time.monotonic()
                self.recommended_stack.set_visible_child_name("grid")
                self.populate_video_grid(self.recommended_grid, videos)

            def failed(exc: Exception) -> None:
                msg = f"Failed to load recommendations: {exc}"
                print(msg, file=sys.stderr)
                self.notify(msg)
                # Show onboarding again so they can check browser settings
                self.recommended_stack.set_visible_child_name("onboarding")
                self.recommended_onboarding_browser_combo.set_active_id(
                    self.service.repository.yt_dlp_cookies_browser()
                )

            def finished() -> None:
                self.loading_recommended = False

            self.run_task(
                "Fetching recommendations...",
                lambda: self.service.recommended_videos(limit=100),
                done,
                finished=finished,
                error=failed,
            )
        else:
            self.loading_recommended = False
            self.recommended_stack.set_visible_child_name("onboarding")
            self.recommended_onboarding_browser_combo.set_active_id(
                self.service.repository.yt_dlp_cookies_browser()
            )

    def on_history_search_changed(self, _widget: Gtk.Widget) -> None:
        self.reload_history()

    def on_channel_search_clicked(self, _widget: Gtk.Widget) -> None:
        self.reload_channels()

    def on_channel_video_search_clicked(self, _widget: Gtk.Widget) -> None:
        if not self.channel_video_search_button.get_sensitive():
            return
        channel = self.current_channel()
        if channel is None:
            return
        query = self.channel_video_search_entry.get_text().strip()
        if not query:
            self.reset_channel_video_search(channel.id)
            self.channel_tabs_notebook.set_current_page(0)
            return

        def done(videos: list[Video]) -> None:
            if not self.current_view or self.current_view.channel_id != channel.id:
                return
            self.populate_channel_search_results(query, videos)
            self.channel_tabs_notebook.set_current_page(3)

        def finished() -> None:
            self.channel_video_search_spinner.stop()
            self.channel_video_search_button.set_child(self.channel_video_search_icon)
            self.channel_video_search_button.set_sensitive(True)

        self.channel_video_search_button.set_sensitive(False)
        self.channel_video_search_button.set_child(self.channel_video_search_spinner)
        self.channel_video_search_spinner.start()
        self.channel_tabs_notebook.set_current_page(3)
        self.channel_search_results_grid.set_visible(False)
        self.channel_search_empty_box.set_visible(True)
        self.channel_search_empty_title.set_label("Searching...")
        self.channel_search_empty_help.set_label(f"Searching {channel.title}.")
        self.run_task(
            f"Searching {channel.title}...",
            lambda: self.service.search_channel(channel, query),
            done,
            finished=finished,
        )

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
                self.reload_feed()
                if self.current_view is not None:
                    self.update_channel_header(self.current_view)
                    self.update_context_unsubscribe_button(self.current_view)
                    if self.current_view.channel_id == channel.id:
                        self.apply_view_state(self.current_view)

            self.run_task(
                f"Subscribing to {channel.title}...",
                lambda: self.service.subscribe_with_initial_videos(
                    channel.url,
                    limit=self.channel_video_limits.get(channel.id, 30),
                ),
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
        self.loaded_local_sections.add("feed")
        videos = self.service.repository.subscription_feed(self.feed_limit)
        self.clear_flowbox(self.feed_grid)
        self.feed_tile_ids = []
        self.feed_tile_widgets = {}
        self.grid_generations[id(self.feed_grid)] = (
            self.grid_generations.get(id(self.feed_grid), 0) + 1
        )
        self.append_feed_grid_batched(videos)
        has_videos = bool(videos)
        self.feed_grid.set_visible(has_videos)
        self.feed_empty_box.set_visible(not has_videos)

    def append_feed_grid_batched(self, videos: list[Video]) -> None:
        index = 0
        generation = self.grid_generations.get(id(self.feed_grid), 0)

        def append_batch() -> bool:
            nonlocal index
            if self.cleaned_up:
                return False
            if self.grid_generations.get(id(self.feed_grid)) != generation:
                return False
            end = min(index + 8, len(videos))
            for video in videos[index:end]:
                wrapper = self.append_video_tile(self.feed_grid, video)
                self.feed_tile_ids.append(video.id)
                self.feed_tile_widgets[video.id] = wrapper
            index = end
            return index < len(videos)

        GLib.idle_add(append_batch)

    def refresh_feed_tiles(self, updated_channel_ids: set[str]) -> None:
        self.grid_generations[id(self.feed_grid)] = (
            self.grid_generations.get(id(self.feed_grid), 0) + 1
        )
        videos = self.service.repository.subscription_feed(self.feed_limit)
        has_videos = bool(videos)
        self.feed_grid.set_visible(has_videos)
        self.feed_empty_box.set_visible(not has_videos)
        if not has_videos:
            self.clear_flowbox(self.feed_grid)
            self.feed_tile_ids = []
            self.feed_tile_widgets = {}
            return
        if not self.feed_tile_ids:
            self.reload_feed()
            return

        videos_by_id = {video.id: video for video in videos}
        desired_ids = [video.id for video in videos]
        current_ids = list(self.feed_tile_ids)

        for video_id in list(current_ids):
            if video_id not in videos_by_id:
                wrapper = self.feed_tile_widgets.pop(video_id, None)
                if wrapper is not None:
                    self.feed_grid.remove(wrapper)
                current_ids.remove(video_id)

        for index, video_id in enumerate(desired_ids):
            video = videos_by_id[video_id]
            wrapper = self.feed_tile_widgets.get(video_id)
            should_replace = (
                wrapper is not None
                and video.channel_id in updated_channel_ids
            )
            if wrapper is None:
                wrapper = self.wrap_tile_widget(self.video_tile(video))
                self.feed_grid.insert(wrapper, index)
                self.feed_tile_widgets[video_id] = wrapper
                current_ids.insert(index, video_id)
                continue
            if should_replace:
                old_index = current_ids.index(video_id)
                self.feed_grid.remove(wrapper)
                wrapper = self.wrap_tile_widget(self.video_tile(video))
                self.feed_grid.insert(wrapper, index)
                self.feed_tile_widgets[video_id] = wrapper
                current_ids.pop(old_index)
                current_ids.insert(index, video_id)
                continue
            current_index = current_ids.index(video_id)
            if current_index != index:
                self.feed_grid.remove(wrapper)
                self.feed_grid.insert(wrapper, index)
                current_ids.pop(current_index)
                current_ids.insert(index, video_id)

        self.feed_tile_ids = desired_ids

    def populate_search_results(self, results: SearchResults) -> None:
        self.clear_flowbox(self.search_channel_grid)
        self.clear_flowbox(self.search_grid)

        has_results = bool(results.channels or results.videos)
        self.search_empty_box.set_visible(not has_results)
        if not has_results:
            self.search_empty_title.set_label("No results")
            self.search_empty_help.set_label("No videos or channels matched this search.")

        self.search_channel_heading.set_visible(bool(results.channels))
        self.search_channel_grid.set_visible(bool(results.channels))
        self.grid_generations[id(self.search_channel_grid)] = (
            self.grid_generations.get(id(self.search_channel_grid), 0) + 1
        )
        channel_generation = self.grid_generations[id(self.search_channel_grid)]
        channel_index = 0

        def append_channels_batch() -> bool:
            nonlocal channel_index
            if self.cleaned_up:
                return False
            if (
                self.grid_generations.get(id(self.search_channel_grid), 0)
                != channel_generation
            ):
                return False
            batch_size = 12
            end = min(channel_index + batch_size, len(results.channels))
            for channel in results.channels[channel_index:end]:
                self.search_channel_grid.append(self.channel_tile(channel))
            channel_index = end
            return channel_index < len(results.channels)

        GLib.idle_add(append_channels_batch)

        self.search_video_heading.set_visible(bool(results.videos))
        self.search_grid.set_visible(bool(results.videos))
        self.append_video_grid_batched(self.search_grid, results.videos)

    def populate_channel_search_results(
        self, query: str, videos: list[Video]
    ) -> None:
        self.clear_flowbox(self.channel_search_results_grid)
        self.grid_generations[id(self.channel_search_results_grid)] = (
            self.grid_generations.get(id(self.channel_search_results_grid), 0) + 1
        )
        has_results = bool(videos)
        self.channel_search_results_grid.set_visible(has_results)
        self.channel_search_empty_box.set_visible(not has_results)
        if not has_results:
            self.channel_search_empty_title.set_label("No channel results")
            self.channel_search_empty_help.set_label(
                f"No videos in this channel matched \"{query}\"."
            )
            return
        index = 0
        generation = self.grid_generations[id(self.channel_search_results_grid)]

        def append_batch() -> bool:
            nonlocal index
            if self.cleaned_up:
                return False
            if (
                self.grid_generations.get(id(self.channel_search_results_grid), 0)
                != generation
            ):
                return False
            end = min(index + 8, len(videos))
            for video in videos[index:end]:
                self.append_video_tile(
                    self.channel_search_results_grid,
                    video,
                    on_clicked=(
                        lambda _widget, result=video: self.open_url(result.url)
                        if is_playlist_url(result.url)
                        else self.play_video(result)
                    ),
                )
            index = end
            return index < len(videos)

        GLib.idle_add(append_batch)

    def reset_channel_video_search(self, channel_id: str | None = None) -> None:
        self.channel_video_search_channel_id = channel_id
        self.channel_video_search_entry.set_text("")
        self.clear_flowbox(self.channel_search_results_grid)
        self.grid_generations[id(self.channel_search_results_grid)] = (
            self.grid_generations.get(id(self.channel_search_results_grid), 0) + 1
        )
        self.channel_search_results_grid.set_visible(False)
        self.channel_search_empty_box.set_visible(True)
        self.channel_search_empty_title.set_label("Search this channel")
        self.channel_search_empty_help.set_label(
            "Enter a search term above to find videos from this channel."
        )

    def load_missing_tile_thumbnail(
        self,
        video: Video,
        picture: Gtk.Picture,
        width: int,
        height: int,
    ) -> None:
        if video.thumbnail_url or not is_playlist_url(video.url):
            return

        future = self.submit_background(self.service.playlist_thumbnail, video.url)
        if future is None:
            return

        def done() -> bool:
            if self.cleaned_up:
                return False
            try:
                thumbnail_url = future.result()
            except Exception as exc:
                self.verbose_log(f"playlist thumbnail lookup failed for {video.url}: {exc}")
                return False
            if thumbnail_url:
                self.load_cached_image(
                    thumbnail_url,
                    picture,
                    suffix=self.thumbnail_cache_suffix(thumbnail_url),
                    width=width,
                    height=height,
                    log_label=f"playlist thumbnail {video.id}",
                )
            return False

        self.schedule_background_finish(future, done)

    def reload_channels(self) -> None:
        self.loaded_local_sections.add("channels")
        channels = self.service.repository.subscribed_channels()
        query = (
            self.channel_search_entry.get_text().strip()
            if hasattr(self, "channel_search_entry")
            else ""
        )
        self.clear_flowbox(self.channel_grid)
        self.grid_generations[id(self.channel_grid)] = (
            self.grid_generations.get(id(self.channel_grid), 0) + 1
        )
        generation = self.grid_generations[id(self.channel_grid)]

        filtered_channels = [
            c for c in channels if self.channel_matches_filter(c)
        ]
        index = 0

        def append_batch() -> bool:
            nonlocal index
            if self.cleaned_up:
                return False
            if self.grid_generations.get(id(self.channel_grid), 0) != generation:
                return False

            batch_size = 12
            end = min(index + batch_size, len(filtered_channels))
            for channel in filtered_channels[index:end]:
                self.channel_grid.append(self.channel_tile(channel))
            
            index = end
            if index < len(filtered_channels):
                return True
            
            has_visible_channels = len(filtered_channels) > 0
            self.channel_grid.set_visible(has_visible_channels)
            self.channel_empty_box.set_visible(not has_visible_channels)
            if not has_visible_channels:
                if query:
                    self.channel_empty_title.set_label("No channel results")
                    self.channel_empty_help.set_label(
                        "No subscribed channels matched this search."
                    )
                    self.channel_import_button.set_visible(False)
                else:
                    self.channel_empty_title.set_label("No subscribed channels")
                    self.channel_empty_help.set_label(
                        "Open a channel URL or subscribe from a video to add channels."
                    )
                    self.channel_import_button.set_visible(not channels)
            return False

        GLib.idle_add(append_batch)
        self.reload_channel_nav(channels)

    def channel_matches_filter(self, channel: Channel) -> bool:
        query = (
            self.channel_search_entry.get_text().strip().lower()
            if hasattr(self, "channel_search_entry")
            else ""
        )
        if not query:
            return True
        haystack = " ".join(
            part
            for part in (channel.title, channel.handle, channel.url)
            if part
        ).lower()
        return query in haystack

    def reload_channel_nav(self, channels: list[Channel]) -> None:
        self.suppress_nav_selection = True
        for row in self.channel_nav_rows:
            self.nav.remove(row)
        self.channel_nav_rows = []
        self.nav_channels = {}
        self.channel_rows = {}
        self.channel_nav_status_boxes = {}
        self.channel_nav_status_channels = {}
        self.nav_generation += 1
        generation = self.nav_generation
        new_video_counts = self.service.repository.new_video_counts_by_channel()

        index = 0

        def append_batch() -> bool:
            nonlocal index
            if self.cleaned_up:
                return False
            if self.nav_generation != generation:
                return False

            batch_size = 20
            end = min(index + batch_size, len(channels))
            for channel in channels[index:end]:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
                box.add_css_class("channel-nav-row")
                if channel.thumbnail_url:
                    icon = Gtk.Picture()
                    icon.add_css_class("channel-avatar")
                    icon.set_size_request(24, 24)
                    icon.set_can_shrink(False)
                    icon.set_content_fit(Gtk.ContentFit.COVER)
                    self.clip_channel_avatar(icon)
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
                status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                box.append(status_box)
                self.channel_nav_status_boxes[channel.id] = status_box
                self.channel_nav_status_channels[channel.id] = channel
                self.update_channel_nav_status(
                    channel,
                    status_box,
                    new_video_counts.get(channel.id, 0),
                )
                row.set_child(box)
                right_click = Gtk.GestureClick()
                right_click.set_button(3)
                right_click.connect(
                    "pressed",
                    lambda _gesture, _n_press, x, y, channel=channel, row=row: (
                        self.show_channel_context_menu(row, channel, x, y)
                    ),
                )
                row.add_controller(right_click)
                self.nav_channels[row] = channel
                self.channel_rows[channel.id] = row
                self.channel_nav_rows.append(row)
                self.nav.append(row)

            index = end
            if index < len(channels):
                return True

            if self.current_view and self.current_view.channel_id is not None:
                self.select_nav_channel(self.current_view.channel_id)
            else:
                GLib.idle_add(self.release_nav_selection_suppression)
            return False

        GLib.idle_add(append_batch)

    def set_channel_refreshing(self, channel_id: str, active: bool) -> bool:
        if active:
            self.refreshing_channel_ids.add(channel_id)
        else:
            self.refreshing_channel_ids.discard(channel_id)
        status_box = self.channel_nav_status_boxes.get(channel_id)
        channel = self.channel_nav_status_channels.get(channel_id)
        if status_box is not None and channel is not None:
            self.update_channel_nav_status(channel, status_box)
        return False

    def update_channel_nav_status(
        self,
        channel: Channel,
        status_box: Gtk.Box,
        new_video_count: int | None = None,
    ) -> None:
        self.clear_box(status_box)
        if channel.id in self.refreshing_channel_ids:
            spinner = Gtk.Spinner()
            spinner.set_tooltip_text(f"Refreshing {channel.title}")
            spinner.start()
            status_box.append(spinner)
            return
        if new_video_count is None:
            new_video_count = self.service.repository.new_video_counts_by_channel().get(
                channel.id,
                0,
            )
        if not new_video_count:
            return
        badge = Gtk.Label(label=str(new_video_count))
        badge.add_css_class("caption")
        badge.add_css_class("accent")
        badge.set_tooltip_text(f"{new_video_count} new videos")
        status_box.append(badge)

    def clear_box(self, box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            box.remove(child)
            child = next_child

    def channel_tile(self, channel: Channel) -> Gtk.Widget:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tile.set_size_request(184, -1)
        tile.set_margin_top(6)
        tile.set_margin_bottom(6)
        tile.set_margin_start(6)
        tile.set_margin_end(6)

        open_button = Gtk.Button()
        open_button.connect("clicked", lambda _button: self.open_search_channel(channel))
        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect(
            "pressed",
            lambda _gesture, _n_press, x, y: self.show_channel_context_menu(
                open_button, channel, x, y
            ),
        )
        open_button.add_controller(right_click)
        tile.append(open_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_size_request(172, -1)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        content.set_margin_start(6)
        content.set_margin_end(6)
        open_button.set_child(content)

        thumbnail_area = Gtk.Overlay()
        thumbnail_area.add_css_class("channel-avatar")
        thumbnail_area.set_size_request(112, 112)
        thumbnail_area.set_halign(Gtk.Align.CENTER)
        self.clip_channel_avatar(thumbnail_area)
        thumbnail_placeholder = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
        thumbnail_placeholder.set_pixel_size(64)
        thumbnail_placeholder.set_size_request(112, 112)
        thumbnail_placeholder.set_halign(Gtk.Align.CENTER)
        thumbnail_placeholder.set_valign(Gtk.Align.CENTER)
        thumbnail_placeholder.add_css_class("dim-label")
        thumbnail_area.set_child(thumbnail_placeholder)
        if channel.thumbnail_url:
            thumbnail = Gtk.Picture()
            thumbnail.add_css_class("channel-avatar")
            thumbnail.set_size_request(112, 112)
            thumbnail.set_can_shrink(False)
            thumbnail.set_halign(Gtk.Align.CENTER)
            thumbnail.set_valign(Gtk.Align.CENTER)
            thumbnail.set_content_fit(Gtk.ContentFit.COVER)
            self.clip_channel_avatar(thumbnail)
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

    def clip_channel_avatar(self, widget: Gtk.Widget) -> None:
        try:
            widget.set_overflow(Gtk.Overflow.HIDDEN)
        except (AttributeError, TypeError):
            pass

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
        self.loaded_local_sections.add("recent_searches")
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
        self.loaded_local_sections.add("history")
        query = self.history_entry.get_text().strip() if hasattr(self, "history_entry") else ""
        try:
            videos = self.service.repository.watch_history(query)
        except Exception:
            import traceback
            self.log(f"Failed to load watch history:\n{traceback.format_exc()}")
            return

        self.clear_flowbox(self.history_grid)
        self.grid_generations[id(self.history_grid)] = (
            self.grid_generations.get(id(self.history_grid), 0) + 1
        )
        generation = self.grid_generations[id(self.history_grid)]
        index = 0

        def append_batch() -> bool:
            nonlocal index
            if self.cleaned_up:
                return False
            if self.grid_generations.get(id(self.history_grid), 0) != generation:
                return False
            
            batch_size = 12
            end = min(index + batch_size, len(videos))
            for video in videos[index:end]:
                try:
                    self.append_video_tile(
                        self.history_grid,
                        video,
                        on_context_menu=self.show_history_context_menu,
                    )
                except Exception:
                    import traceback
                    self.log(f"Failed to render history tile for {video.id}:\n{traceback.format_exc()}")
            
            index = end
            if index < len(videos):
                return True
            
            has_videos = bool(videos)
            self.history_grid.set_visible(has_videos)
            self.history_empty_box.set_visible(not has_videos)
            if has_videos:
                return False
            if query:
                self.history_empty_title.set_label("No history results")
                self.history_empty_help.set_label("No watched videos matched this search.")
            else:
                self.history_empty_title.set_label("No watch history")
                self.history_empty_help.set_label("Videos you watch will appear here.")
            return False

        GLib.idle_add(append_batch)

    def set_feed_loading(self, loading: bool, label: str = "Loading more...") -> None:
        self.feed_loading_label.set_text(label)
        self.feed_loading_box.set_visible(loading)

    def refresh_one_channel(
        self, channel: Channel, clear_initial_new_indicator: bool = False
    ) -> None:
        def done(_videos: list[Video]) -> None:
            self.reload_channels()
            if self.current_view and self.current_view.channel_id == channel.id:
                self.apply_view_state(self.current_view)

        def finished() -> None:
            self.set_channel_refreshing(channel.id, False)
            if self.current_view and self.current_view.channel_id == channel.id:
                self.set_feed_loading(False)
            self.set_context_refresh_loading(False)

        self.set_channel_refreshing(channel.id, True)
        if self.current_view and self.current_view.channel_id == channel.id:
            self.set_feed_loading(True, "Loading videos...")
            self.set_context_refresh_loading(True)

        self.run_task(
            f"Refreshing {channel.title}...",
            lambda: self.service.refresh_channel(
                channel,
                limit=self.channel_video_limits.get(channel.id, 30),
                clear_new_indicator=clear_initial_new_indicator,
            ),
            done,
            finished=finished,
        )

    def show_channel_videos(self, channel: Channel) -> None:
        self.channel_video_limits.setdefault(channel.id, 30)
        self.navigate_to(ViewState("feed", channel.id, channel.title))
        has_local_videos = bool(self.service.repository.channel_videos(channel.id, 1))
        if self.service.repository.channel_needs_refresh(channel.id):
            self.refresh_one_channel(
                channel,
                clear_initial_new_indicator=not has_local_videos,
            )

    def run_recent_search(self, query: str) -> None:
        self.search_entry.set_text(query)
        self.on_search_clicked(self.search_entry)

    def create_queue_row(self, item: VideoObject) -> Gtk.Widget:
        video = item.video
        row = Gtk.ListBoxRow()
        row.add_css_class("queue-row")

        tile = self.queue_video_tile(
            video,
            on_clicked=lambda _: self.play_from_queue_index(row.get_index()),
            on_context_menu=lambda _w, _v, x, y: self.show_queue_context_menu(
                row, video, x, y
            ),
        )
        tile.set_halign(Gtk.Align.FILL)
        row.set_child(tile)

        # Setup Drag and Drop
        self.setup_queue_dnd(row)

        return row

    def play_from_queue_index(self, index: int) -> None:
        if 0 <= index < self.video_queue.get_n_items():
            item = self.video_queue.get_item(index)
            self.video_queue.remove(index)
            self.queue_pane.set_visible(self.video_queue.get_n_items() > 0)
            self.update_transport_navigation_buttons()
            self.play_video(item.video, hide_sidebar=False)

    def setup_queue_dnd(self, row: Gtk.ListBoxRow) -> None:
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(row.get_index()))
        source.connect("drag-begin", lambda *_: self.on_drag_begin(row))
        row.add_controller(source)

        target = Gtk.DropTarget.new(int, Gdk.DragAction.MOVE)
        target.connect("drop", lambda *args: self.on_queue_drop(row, *args))
        target.connect("motion", lambda *args: self.on_queue_motion(row, *args))
        row.add_controller(target)

    def on_drag_begin(self, row: Gtk.ListBoxRow) -> None:
        self.dragging_index = row.get_index()

    def on_queue_motion(self, target_row: Gtk.ListBoxRow, _target: Gtk.DropTarget, _x: float, _y: float) -> Gdk.DragAction:
        target_index = target_row.get_index()
        if self.dragging_index != -1 and self.dragging_index != target_index:
            item = self.video_queue.get_item(self.dragging_index)
            self.video_queue.remove(self.dragging_index)
            self.video_queue.insert(target_index, item)
            self.dragging_index = target_index
        return Gdk.DragAction.MOVE

    def on_queue_drop(self, target_row: Gtk.ListBoxRow, _target: Gtk.DropTarget, source_index: int, _x: float, _y: float) -> bool:
        self.dragging_index = -1
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

    def create_playlist_row(self, item: VideoObject) -> Gtk.Widget:
        video = item.video
        row = Gtk.ListBoxRow()
        row.add_css_class("queue-row")
        tile = self.queue_video_tile(
            video,
            on_clicked=lambda _: self.play_playlist_item(row.get_index()),
            on_context_menu=lambda _w, _v, x, y: self.show_playlist_context_menu(
                row, video, x, y
            ),
        )
        tile.set_halign(Gtk.Align.FILL)
        row.set_child(tile)
        return row

    def play_playlist_item(self, index: int) -> None:
        if index < 0 or index >= self.playlist_store.get_n_items():
            return
        self.playlist_current_index = index
        item = self.playlist_store.get_item(index)
        self.play_video(item.video, hide_sidebar=False)
        self.update_playlist_rows()

    def toggle_playlist_skip(self, popover: Gtk.Popover, index: int) -> None:
        popover.popdown()
        popover.unparent()
        if index in self.playlist_skip_set:
            self.playlist_skip_set.discard(index)
        else:
            self.playlist_skip_set.add(index)
        self.update_playlist_rows()

    def update_playlist_rows(self) -> None:
        current = self.playlist_list.get_first_child()
        idx = 0
        while current is not None:
            box = current.get_child()
            if box is not None:
                if idx in self.playlist_skip_set:
                    current.add_css_class("skipped")
                else:
                    current.remove_css_class("skipped")
                if idx == self.playlist_current_index:
                    current.add_css_class("current")
                else:
                    current.remove_css_class("current")
            idx += 1
            current = current.get_next_sibling()
        self.update_transport_navigation_buttons()
