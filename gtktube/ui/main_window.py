from __future__ import annotations

import hashlib
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gst, Gtk, Pango  # noqa: E402

from gtktube.extractors.youtube import ExtractorError, QUALITY_FORMATS
from gtktube.models import Channel, PlayableVideo, Video
from gtktube.paths import AppPaths
from gtktube.services.library import LibraryService


T = TypeVar("T")
Gst.init(None)


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
        self.current_playable: PlayableVideo | None = None
        self.pipeline: Gst.Element | None = None
        self.bus_watch_id: int | None = None
        self.range_start_seconds: int | None = None
        self.updating_scrubber = False
        self.updating_subscribe_check = False
        self.updating_quality = False
        self.fullscreen_window: Gtk.Window | None = None
        self.fullscreen_picture: Gtk.Picture | None = None
        self.status_text = "Ready"

        self.set_default_size(1100, 720)
        self.connect("close-request", self.on_close_request)
        shortcuts = Gtk.EventControllerKey()
        shortcuts.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        shortcuts.connect("key-pressed", self.on_key_pressed)
        self.add_controller(shortcuts)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, vexpand=True)
        root.append(body)

        nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        nav.set_size_request(180, -1)
        body.append(nav)
        self.nav_pages: dict[Gtk.ListBoxRow, str] = {}

        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        body.append(self.stack)

        self.pages: dict[str, Gtk.Widget] = {}
        for key, title in [
            ("feed", "Feed"),
            ("channels", "Channels"),
            ("search", "Search"),
            ("history", "History"),
            ("player", "Player"),
        ]:
            row = Gtk.ListBoxRow()
            row.set_child(Gtk.Label(label=title, xalign=0))
            self.nav_pages[row] = key
            nav.append(row)

        nav.connect("row-selected", self.on_nav_selected)

        self.build_feed_page()
        self.build_channels_page()
        self.build_search_page()
        self.build_history_page()
        self.build_player_page()

        first = nav.get_row_at_index(0)
        if first is not None:
            nav.select_row(first)
        self.reload_all_local()
        GLib.timeout_add_seconds(5, self.flush_watch_range)
        GLib.timeout_add_seconds(1, self.update_playback_controls)

    def on_close_request(self, *_args: object) -> bool:
        self.flush_watch_range()
        self.stop_pipeline()
        self.executor.shutdown(wait=False, cancel_futures=True)
        return False

    def on_nav_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        self.stack.set_visible_child_name(self.nav_pages[row])

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

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.append(toolbar)

        refresh = Gtk.Button(label="Refresh subscriptions")
        refresh.connect("clicked", self.on_refresh_subscriptions)
        toolbar.append(refresh)

        self.feed_grid = self.create_video_grid()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.feed_grid)
        page.append(scroller)

        self.stack.add_named(page, "feed")

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

        self.channel_list = Gtk.ListBox()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.channel_list)
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
        self.search_entry = Gtk.Entry(hexpand=True)
        self.search_entry.set_placeholder_text("Search YouTube")
        self.search_entry.connect("activate", self.on_search_clicked)
        search_box.append(self.search_entry)
        search_button = Gtk.Button(label="Search")
        search_button.connect("clicked", self.on_search_clicked)
        search_box.append(search_button)

        self.recent_searches = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE)
        page.append(self.recent_searches)

        self.search_grid = self.create_video_grid()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.search_grid)
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

    def build_player_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)

        url_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.append(url_box)
        self.url_entry = Gtk.Entry(hexpand=True)
        self.url_entry.set_placeholder_text("YouTube video URL")
        self.url_entry.connect("activate", self.on_play_url_clicked)
        url_box.append(self.url_entry)
        play_button = Gtk.Button(label="Play URL")
        play_button.connect("clicked", self.on_play_url_clicked)
        url_box.append(play_button)

        self.video = Gtk.Picture(hexpand=True, vexpand=True)
        self.video.set_can_shrink(True)
        self.video.set_size_request(-1, 360)
        video_click = Gtk.GestureClick()
        video_click.connect("released", self.on_video_clicked)
        self.video.add_controller(video_click)
        page.append(self.video)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.append(controls)
        self.play_pause_icon = Gtk.Image.new_from_icon_name(
            "media-playback-start-symbolic"
        )
        self.play_pause_button = Gtk.Button(child=self.play_pause_icon)
        self.play_pause_button.set_tooltip_text("Play")
        self.play_pause_button.connect("clicked", self.on_play_pause_clicked)
        controls.append(self.play_pause_button)

        self.elapsed_label = Gtk.Label(label="0:00")
        controls.append(self.elapsed_label)

        self.scrubber = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.scrubber.set_hexpand(True)
        self.scrubber.set_draw_value(False)
        self.scrubber.connect("change-value", self.on_scrub_changed)
        controls.append(self.scrubber)

        self.duration_label = Gtk.Label(label="0:00")
        controls.append(self.duration_label)

        self.quality_combo = Gtk.ComboBoxText()
        for quality in QUALITY_FORMATS:
            self.quality_combo.append(quality, quality)
        self.quality_combo.set_active_id("720p")
        self.quality_combo.connect("changed", self.on_quality_changed)
        controls.append(self.quality_combo)

        self.fullscreen_icon = Gtk.Image.new_from_icon_name("view-fullscreen-symbolic")
        self.fullscreen_button = Gtk.Button(child=self.fullscreen_icon)
        self.fullscreen_button.set_tooltip_text("Fullscreen video")
        self.fullscreen_button.connect("clicked", self.on_fullscreen_clicked)
        controls.append(self.fullscreen_button)

        metadata = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        metadata.set_margin_top(4)
        page.append(metadata)
        self.player_title = Gtk.Label(label="No video loaded", xalign=0, hexpand=True)
        self.player_title.set_wrap(True)
        metadata.append(self.player_title)

        self.player_meta = Gtk.Label(label="", xalign=0)
        self.player_meta.set_wrap(True)
        self.player_meta.add_css_class("dim-label")
        metadata.append(self.player_meta)

        self.player_description = Gtk.Label(label="", xalign=0)
        self.player_description.set_wrap(True)
        self.player_description.set_selectable(True)
        self.player_description.set_ellipsize(Pango.EllipsizeMode.END)

        description_scroller = Gtk.ScrolledWindow()
        description_scroller.set_size_request(-1, 140)
        description_scroller.set_child(self.player_description)

        description = Gtk.Expander(label="Description")
        description.set_child(description_scroller)
        metadata.append(description)

        self.player_subscribe = Gtk.CheckButton(label="Subscribed")
        self.player_subscribe.set_sensitive(False)
        self.player_subscribe.connect("toggled", self.on_player_subscribe_toggled)
        metadata.append(self.player_subscribe)

        self.stack.add_named(page, "player")

    def create_video_grid(self) -> Gtk.FlowBox:
        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_min_children_per_line(1)
        grid.set_max_children_per_line(8)
        grid.set_column_spacing(12)
        grid.set_row_spacing(12)
        grid.set_halign(Gtk.Align.START)
        grid.set_valign(Gtk.Align.START)
        grid.set_margin_top(4)
        grid.set_margin_bottom(4)
        grid.set_margin_start(4)
        grid.set_margin_end(4)
        return grid

    def on_refresh_subscriptions(self, _button: Gtk.Button) -> None:
        self.run_task(
            "Refreshing subscriptions...",
            self.service.refresh_subscriptions,
            lambda _result: self.reload_feed(),
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

        def done(videos: list[Video]) -> None:
            self.populate_video_grid(self.search_grid, videos)
            self.reload_recent_searches()

        self.run_task("Searching...", lambda: self.service.search(query), done)

    def on_history_search_changed(self, _widget: Gtk.Widget) -> None:
        self.reload_history()

    def on_play_url_clicked(self, _widget: Gtk.Widget) -> None:
        url = self.url_entry.get_text().strip()
        if not url:
            return
        quality = self.selected_quality()
        self.run_task(
            "Resolving video...",
            lambda: self.service.play_url(url, quality=quality),
            self.load_playable,
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

    def reload_feed(self) -> None:
        self.populate_video_grid(self.feed_grid, self.service.repository.subscription_feed())

    def reload_channels(self) -> None:
        self.clear_listbox(self.channel_list)
        for channel in self.service.repository.subscribed_channels():
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(8)
            box.set_margin_end(8)
            row.set_child(box)
            label = Gtk.Label(label=channel.title, xalign=0, hexpand=True)
            label.set_wrap(True)
            box.append(label)
            refresh = Gtk.Button(label="Refresh")
            refresh.connect("clicked", lambda _b, c=channel: self.refresh_one_channel(c))
            box.append(refresh)
            view = Gtk.Button(label="View")
            view.connect("clicked", lambda _b, c=channel: self.show_channel_videos(c))
            box.append(view)
            self.channel_list.append(row)

    def reload_recent_searches(self) -> None:
        child = self.recent_searches.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.recent_searches.remove(child)
            child = next_child
        for query in self.service.repository.recent_searches():
            button = Gtk.Button(label=query)
            button.connect("clicked", lambda _b, q=query: self.run_recent_search(q))
            self.recent_searches.append(button)

    def reload_history(self) -> None:
        query = self.history_entry.get_text().strip() if hasattr(self, "history_entry") else ""
        self.populate_video_grid(self.history_grid, self.service.repository.watch_history(query))

    def refresh_one_channel(self, channel: Channel) -> None:
        self.run_task(
            f"Refreshing {channel.title}...",
            lambda: self.service.refresh_channel(channel),
            lambda _videos: self.show_channel_videos(channel),
        )

    def show_channel_videos(self, channel: Channel) -> None:
        self.populate_video_grid(self.feed_grid, self.service.repository.channel_videos(channel.id))
        self.stack.set_visible_child_name("feed")

    def run_recent_search(self, query: str) -> None:
        self.search_entry.set_text(query)
        self.on_search_clicked(self.search_entry)

    def populate_video_grid(self, grid: Gtk.FlowBox, videos: list[Video]) -> None:
        self.clear_flowbox(grid)
        for video in videos:
            grid.append(self.video_tile(video))

    def video_tile(self, video: Video) -> Gtk.Widget:
        button = Gtk.Button()
        button.set_size_request(244, -1)
        button.set_hexpand(False)
        button.set_halign(Gtk.Align.START)
        button.connect("clicked", lambda _button: self.play_video(video))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(232, -1)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        button.set_child(box)

        thumbnail = Gtk.Picture()
        thumbnail.set_size_request(232, 174)
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

    def load_thumbnail(self, video: Video, picture: Gtk.Picture) -> None:
        url = self.display_thumbnail_url(video)
        path = self.thumbnail_path(url)
        if path.exists():
            if self.set_thumbnail_file(picture, path):
                return
            try:
                path.unlink()
            except OSError:
                pass

        future = self.executor.submit(self.download_thumbnail, url, path)

        def done() -> bool:
            try:
                downloaded = future.result()
            except Exception:
                return False
            if downloaded.exists() and picture.get_parent() is not None:
                self.set_thumbnail_file(picture, downloaded)
            return False

        future.add_done_callback(lambda _future: GLib.idle_add(done))

    def set_thumbnail_file(self, picture: Gtk.Picture, path: Path) -> bool:
        try:
            header = path.read_bytes()[:12]
        except OSError:
            return False
        if header.startswith(b"RIFF") and b"WEBP" in header:
            return False
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(path),
                464,
                348,
                True,
            )
        except GLib.Error:
            return False
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        picture.set_paintable(texture)
        return True

    def thumbnail_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.thumbnail_dir / f"{digest}.jpg"

    def download_thumbnail(self, url: str, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        request = urllib.request.Request(
            self.jpeg_thumbnail_url(url),
            headers={
                "User-Agent": "GTKTube/0.1",
                "Accept": "image/jpeg,image/*;q=0.8,*/*;q=0.5",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(2_000_000)
        except (TimeoutError, urllib.error.URLError):
            return path
        if data.startswith(b"RIFF") and b"WEBP" in data[:12]:
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
        if video.published_at:
            parts.append(video.published_at)
        if video.duration_seconds:
            parts.append(self.format_time(video.duration_seconds))
        if video.view_count is not None:
            parts.append(f"{video.view_count:,} views")
        if video.percent_watched:
            parts.append(f"{round(video.percent_watched * 100)}% watched")
        if video.completed:
            parts.append("completed")
        return " · ".join(parts)

    def play_video(self, video: Video) -> None:
        self.stack.set_visible_child_name("player")
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
        self.updating_quality = True
        self.quality_combo.set_active_id(playable.quality)
        self.updating_quality = False
        self.update_player_metadata(playable.video)
        self.url_entry.set_text(playable.video.url)
        self.update_subscribe_check(playable.video)

        pipeline = self.create_pipeline(playable)
        if pipeline is None:
            return
        self.pipeline = pipeline
        self.watch_pipeline_bus(pipeline)
        pipeline.set_state(Gst.State.PLAYING)

        resume = (
            resume_position
            if resume_position is not None
            else self.service.repository.resume_position(playable.video.id)
        )
        if resume > 0:
            GLib.timeout_add(
                500,
                lambda: self.seek_media(resume),
            )
        self.range_start_seconds = self.current_position_seconds()
        self.stack.set_visible_child_name("player")

    def update_player_metadata(self, video: Video) -> None:
        self.player_title.set_text(video.title)
        meta = self.video_meta(video)
        if self.current_playable and self.current_playable.resolved_quality:
            meta = f"{meta} · {self.current_playable.resolved_quality}".strip(" ·")
        self.player_meta.set_text(meta)
        self.player_description.set_text(video.description or "")

    def update_subscribe_check(self, video: Video) -> None:
        subscribed = self.service.repository.is_subscribed(video.channel_id)
        self.updating_subscribe_check = True
        self.player_subscribe.set_active(subscribed)
        self.updating_subscribe_check = False
        self.player_subscribe.set_sensitive(bool(video.channel_id or video.url))

    def create_pipeline(self, playable: PlayableVideo) -> Gst.Element | None:
        sink = Gst.ElementFactory.make("gtk4paintablesink", "video_sink")
        if sink is None:
            self.set_status(
                "Missing GStreamer gtk4paintablesink. Install gstreamer1.0-gtk4."
            )
            return None

        paintable = sink.get_property("paintable")
        if paintable is not None:
            self.video.set_paintable(paintable)

        pipeline = Gst.ElementFactory.make("playbin", "player")
        if pipeline is None:
            self.set_status("Could not create GStreamer playbin")
            return None
        pipeline.set_property("uri", playable.stream_url)
        pipeline.set_property("video-sink", sink)
        return pipeline

    def watch_pipeline_bus(self, pipeline: Gst.Element) -> None:
        bus = pipeline.get_bus()
        if bus is None:
            return
        bus.add_signal_watch()
        self.bus_watch_id = bus.connect("message", self.on_gst_message)

    def on_gst_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            self.set_status(f"Playback error: {error.message}")
            self.log(f"playback error message={error.message} debug={debug or ''}")
            if debug:
                self.log(f"playback debug={debug}")
        elif message.type == Gst.MessageType.EOS:
            self.flush_watch_range()
            self.set_pipeline_state(Gst.State.PAUSED)
        elif (
            message.type == Gst.MessageType.STATE_CHANGED
            and message.src == self.pipeline
        ):
            self.update_play_pause_button()

    def stop_pipeline(self) -> None:
        if self.fullscreen_window is not None:
            self.close_video_fullscreen()
        if self.pipeline is None:
            return
        bus = self.pipeline.get_bus()
        if bus is not None:
            bus.remove_signal_watch()
            if self.bus_watch_id is not None:
                try:
                    bus.disconnect(self.bus_watch_id)
                except TypeError:
                    pass
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self.bus_watch_id = None
        self.range_start_seconds = None

    def set_pipeline_state(self, state: Gst.State) -> None:
        if self.pipeline is not None:
            self.pipeline.set_state(state)
            self.update_play_pause_button()

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
        if self.pipeline is None:
            return
        _ret, state, _pending = self.pipeline.get_state(0)
        if state == Gst.State.PLAYING:
            self.set_pipeline_state(Gst.State.PAUSED)
        else:
            self.set_pipeline_state(Gst.State.PLAYING)

    def update_play_pause_button(self) -> None:
        if self.pipeline is None:
            self.play_pause_icon.set_from_icon_name("media-playback-start-symbolic")
            self.play_pause_button.set_tooltip_text("Play")
            return
        _ret, state, _pending = self.pipeline.get_state(0)
        if state == Gst.State.PLAYING:
            self.play_pause_icon.set_from_icon_name("media-playback-pause-symbolic")
            self.play_pause_button.set_tooltip_text("Pause")
        else:
            self.play_pause_icon.set_from_icon_name("media-playback-start-symbolic")
            self.play_pause_button.set_tooltip_text("Play")

    def on_fullscreen_clicked(self, _button: Gtk.Button) -> None:
        if self.fullscreen_window is None:
            self.open_video_fullscreen()
        else:
            self.close_video_fullscreen()

    def open_video_fullscreen(self) -> None:
        paintable = self.video.get_paintable()
        if paintable is None:
            return

        picture = Gtk.Picture(hexpand=True, vexpand=True)
        picture.set_paintable(paintable)
        picture.set_can_shrink(True)
        picture_click = Gtk.GestureClick()
        picture_click.connect("released", self.on_video_clicked)
        picture.add_controller(picture_click)

        window = Gtk.Window(title="GTKTube")
        window.set_transient_for(self)
        window.set_child(picture)
        window.connect("close-request", self.on_fullscreen_close_request)

        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self.on_fullscreen_key_pressed)
        window.add_controller(key_controller)

        self.fullscreen_window = window
        self.fullscreen_picture = picture
        self.fullscreen_icon.set_from_icon_name("view-restore-symbolic")
        self.fullscreen_button.set_tooltip_text("Exit fullscreen video")
        window.fullscreen()
        window.present()

    def close_video_fullscreen(self) -> None:
        window = self.fullscreen_window
        if window is None:
            return
        self.fullscreen_window = None
        self.fullscreen_picture = None
        self.fullscreen_icon.set_from_icon_name("view-fullscreen-symbolic")
        self.fullscreen_button.set_tooltip_text("Fullscreen video")
        window.close()

    def on_fullscreen_close_request(self, _window: Gtk.Window) -> bool:
        self.fullscreen_window = None
        self.fullscreen_picture = None
        self.fullscreen_icon.set_from_icon_name("view-fullscreen-symbolic")
        self.fullscreen_button.set_tooltip_text("Fullscreen video")
        return False

    def on_fullscreen_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close_video_fullscreen()
            return True
        return self.handle_player_shortcut(keyval)

    def on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        focus = self.get_focus()
        if isinstance(focus, Gtk.Entry):
            return False
        return self.handle_player_shortcut(keyval)

    def handle_player_shortcut(self, keyval: int) -> bool:
        if keyval in (Gdk.KEY_space, Gdk.KEY_k, Gdk.KEY_K):
            self.on_play_pause_clicked(self.play_pause_button)
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

    def load_playable_at(self, playable: PlayableVideo, position: int) -> None:
        self.load_playable(playable, resume_position=position)

    def seek_media(self, seconds: int) -> bool:
        if self.pipeline is not None:
            self.flush_watch_range()
            duration = self.current_duration_seconds()
            if duration > 0:
                seconds = max(0, min(seconds, duration))
            self.pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                seconds * Gst.SECOND,
            )
            self.range_start_seconds = seconds
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
        if self.current_playable is None or self.pipeline is None:
            return True
        current = self.current_position_seconds()
        start = self.range_start_seconds
        if start is not None and current > start:
            self.service.record_watch_range(self.current_playable.video.id, start, current)
            self.range_start_seconds = current
            self.reload_history()
            self.reload_feed()
        return True

    def current_position_seconds(self) -> int:
        if self.pipeline is None:
            return 0
        success, position = self.pipeline.query_position(Gst.Format.TIME)
        if not success:
            return 0
        return max(0, int(position / Gst.SECOND))

    def current_duration_seconds(self) -> int:
        if self.pipeline is None:
            return 0
        success, duration = self.pipeline.query_duration(Gst.Format.TIME)
        if not success:
            return 0
        return max(0, int(duration / Gst.SECOND))

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

    def clear_listbox(self, listbox: Gtk.ListBox) -> None:
        child = listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            listbox.remove(child)
            child = next_child

    def clear_flowbox(self, flowbox: Gtk.FlowBox) -> None:
        child = flowbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            flowbox.remove(child)
            child = next_child
