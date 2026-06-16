from __future__ import annotations

import json
import sys
import traceback
from ctypes import CDLL, POINTER, c_char_p, c_int, c_void_p
from ctypes.util import find_library
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402


from gtktube.extractors.youtube import ExtractorError, QUALITY_FORMATS
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


T = TypeVar("T")


class GTKTubeApplication(Gtk.Application):
    def __init__(
        self,
        service: LibraryService,
        paths: AppPaths,
        force_update_dialog: bool = False,
        enable_update_check: bool = True,
        verbose: bool = False,
    ):
        super().__init__(
            application_id="local.gtktube.GTKTube",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.service = service
        self.paths = paths
        self.force_update_dialog = force_update_dialog
        self.enable_update_check = enable_update_check
        self.verbose = verbose

    def do_activate(self) -> None:
        window = MainWindow(
            self,
            self.service,
            self.paths,
            force_update_dialog=self.force_update_dialog,
            enable_update_check=self.enable_update_check,
            verbose=self.verbose,
        )
        window.present()

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
    ):
        super().__init__(application=app, title="GTKTube")
        self.service = service
        self.paths = paths
        self.force_update_dialog = force_update_dialog
        self.enable_update_check = enable_update_check
        self.verbose = verbose
        self.thumbnail_dir = paths.cache_dir / "thumbnails"
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self.caption_dir = paths.cache_dir / "captions"
        self.caption_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.video_queue = Gio.ListStore(item_type=VideoObject)
        self.playlist_store = Gio.ListStore(item_type=VideoObject)
        self.playlist_current_index: int | None = None
        self.playlist_skip_set: set[int] = set()
        self.dragging_index = -1
        self.current_playable: PlayableVideo | None = None
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
        self.selected_caption_id = "off"
        self.active_caption_url: str | None = None
        self.preferred_quality = self.service.repository.default_video_quality()
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
        self.importing_youtube_history = False
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
        self.build_settings_page()
        self.build_channels_page()
        self.build_search_page()
        self.build_history_page()
        self.build_player_page()

        self.update_recommended_nav_visibility()
        initial_view = self.initial_view_state()
        self.navigate_to(initial_view, record=False)
        self.schedule_deferred_local_reloads(initial_view)
        GLib.timeout_add_seconds(5, self.flush_watch_range)
        GLib.timeout_add_seconds(1, self.update_playback_controls)
        GLib.timeout_add_seconds(10, self.maybe_import_youtube_watch_history_once)
        GLib.timeout_add_seconds(3600, self.maybe_import_youtube_watch_history)
        if self.enable_update_check:
            GLib.timeout_add_seconds(2, self.start_update_check)

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
        self.reload_recommended()

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
        future = self.executor.submit(work)

        def finish() -> bool:
            try:
                result = future.result()
            except Exception as exc:
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

        future.add_done_callback(lambda _future: GLib.idle_add(finish))

    def reload_all_local(self) -> None:
        self.reload_feed()
        self.reload_channels()
        self.reload_history()
        self.reload_watch_later()
        self.reload_recent_searches()

    def maybe_import_youtube_watch_history_once(self) -> bool:
        self.maybe_import_youtube_watch_history()
        return False

    def maybe_import_youtube_watch_history(self, force: bool = False) -> bool:
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
        sections = ["feed", "channels", "history", "watch_later", "recent_searches"]
        if initial_view.page in sections:
            sections.remove(initial_view.page)

        def load_next() -> bool:
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
        self.clear_flowbox(self.watch_later_grid)
        self.grid_generations[id(self.watch_later_grid)] = (
            self.grid_generations.get(id(self.watch_later_grid), 0) + 1
        )
        generation = self.grid_generations[id(self.watch_later_grid)]
        index = 0

        def append_batch() -> bool:
            nonlocal index
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

        self.watch_later_grid = self.create_video_grid()
        self.watch_later_grid.set_margin_top(12)
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
        for quality in QUALITY_FORMATS:
            self.quality_combo.append(quality, quality)
        self.quality_combo.set_active_id(self.preferred_quality)
        self.quality_combo.connect("changed", self.on_quality_changed)
        self.player_controls.append(self.quality_combo)

        self.speed_combo = Gtk.ComboBoxText()
        for rate in PLAYBACK_RATES:
            self.speed_combo.append(self.speed_id(rate), self.speed_label(rate))
        self.speed_combo.set_active_id(self.speed_id(self.playback_rate))
        self.speed_combo.connect("changed", self.on_speed_changed)
        self.player_controls.append(self.speed_combo)

        self.caption_combo = Gtk.ComboBoxText()
        self.caption_combo.append("off", "None")
        self.caption_combo.set_active_id("off")
        self.caption_combo.set_tooltip_text("Subtitles")
        self.caption_combo.connect("changed", self.on_caption_changed)
        self.caption_combo.set_visible(False)
        self.player_controls.append(self.caption_combo)

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

        self.description_text = ""
        self.description_window: Gtk.Window | None = None

        self.stack.add_named(page, "player")

    def create_channel_grid(self) -> Gtk.FlowBox:
        grid = self.create_video_grid()
        grid.set_max_children_per_line(10)
        return grid

    def on_refresh_subscriptions(self, _button: Gtk.Button) -> None:
        def done(_result: None) -> None:
            self.reload_channels()
            self.reload_feed()

        def finished() -> None:
            self.refreshing_channel_ids.clear()
            self.reload_channels()
            self.set_context_refresh_loading(False)

        def progress(channel: Channel, active: bool) -> None:
            GLib.idle_add(self.set_channel_refreshing, channel.id, active)

        self.set_context_refresh_loading(True)
        self.run_task(
            "Refreshing subscriptions...",
            lambda: self.service.refresh_subscriptions(
                max_workers=self.service.repository.refresh_worker_count(),
                progress=progress,
            ),
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
            self.reload_settings()

    def on_recommended_show_clicked(self, _btn: Gtk.Button) -> None:
        self.service.repository.set_show_recommended_videos(True)
        self.update_recommended_nav_visibility()
        self.reload_settings()
        self.reload_recommended()

    def on_recommended_dismiss_clicked(self, _btn: Gtk.Button) -> None:
        self.service.repository.set_show_recommended_videos(False)
        self.update_recommended_nav_visibility()
        self.reload_settings()
        self.navigate_to(ViewState("feed"))

    def reload_recommended(self) -> None:
        show = self.service.repository.show_recommended_videos()
        browser = self.service.repository.yt_dlp_cookies_browser()

        if show is True and browser:
            self.recommended_stack.set_visible_child_name("grid")
            self.clear_flowbox(self.recommended_grid)

            def done(videos: list[Video]) -> None:
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

            self.run_task(
                "Fetching recommendations...",
                lambda: self.service.recommended_videos(limit=100),
                done,
                error=failed,
            )
        else:
            self.recommended_stack.set_visible_child_name("onboarding")
            self.recommended_onboarding_browser_combo.set_active_id(
                self.service.repository.yt_dlp_cookies_browser()
            )

    def on_history_search_changed(self, _widget: Gtk.Widget) -> None:
        self.reload_history()

    def on_channel_search_clicked(self, _widget: Gtk.Widget) -> None:
        self.reload_channels()

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
        self.grid_generations[id(self.feed_grid)] = (
            self.grid_generations.get(id(self.feed_grid), 0) + 1
        )
        self.append_video_grid_batched(self.feed_grid, videos)
        has_videos = bool(videos)
        self.feed_grid.set_visible(has_videos)
        self.feed_empty_box.set_visible(not has_videos)

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
                else:
                    self.channel_empty_title.set_label("No subscribed channels")
                    self.channel_empty_help.set_label(
                        "Open a channel URL or subscribe from a video to add channels."
                    )
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
            self.play_video(item.video)

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
