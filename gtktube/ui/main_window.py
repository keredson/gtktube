from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

from gtktube.extractors.youtube import ExtractorError
from gtktube.models import Channel, PlayableVideo, Video
from gtktube.services.library import LibraryService


T = TypeVar("T")
Gst.init(None)


class GTKTubeApplication(Gtk.Application):
    def __init__(self, service: LibraryService):
        super().__init__(
            application_id="local.gtktube.GTKTube",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.service = service

    def do_activate(self) -> None:
        window = MainWindow(self, self.service)
        window.present()


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: GTKTubeApplication, service: LibraryService):
        super().__init__(application=app, title="GTKTube")
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.current_playable: PlayableVideo | None = None
        self.pipeline: Gst.Element | None = None
        self.bus_watch_id: int | None = None
        self.range_start_seconds: int | None = None

        self.set_default_size(1100, 720)
        self.connect("close-request", self.on_close_request)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        header = Gtk.HeaderBar()
        self.status = Gtk.Label(label="Ready")
        header.pack_start(self.status)
        self.set_titlebar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
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
        self.status.set_text(text)

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

        self.feed_list = Gtk.ListBox()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.feed_list)
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

        self.search_list = Gtk.ListBox()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.search_list)
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

        self.history_list = Gtk.ListBox()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.history_list)
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
        page.append(self.video)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.append(controls)
        play = Gtk.Button(label="Play")
        play.connect("clicked", lambda _button: self.set_pipeline_state(Gst.State.PLAYING))
        controls.append(play)
        pause = Gtk.Button(label="Pause")
        pause.connect("clicked", lambda _button: self.set_pipeline_state(Gst.State.PAUSED))
        controls.append(pause)

        details = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.append(details)
        self.player_title = Gtk.Label(label="No video loaded", xalign=0, hexpand=True)
        self.player_title.set_wrap(True)
        details.append(self.player_title)
        self.player_subscribe = Gtk.Button(label="Subscribe to channel")
        self.player_subscribe.set_sensitive(False)
        self.player_subscribe.connect("clicked", self.on_player_subscribe_clicked)
        details.append(self.player_subscribe)

        self.stack.add_named(page, "player")

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
            self.populate_video_list(self.search_list, videos)
            self.reload_recent_searches()

        self.run_task("Searching...", lambda: self.service.search(query), done)

    def on_history_search_changed(self, _widget: Gtk.Widget) -> None:
        self.reload_history()

    def on_play_url_clicked(self, _widget: Gtk.Widget) -> None:
        url = self.url_entry.get_text().strip()
        if not url:
            return
        self.run_task("Resolving video...", lambda: self.service.play_url(url), self.load_playable)

    def on_player_subscribe_clicked(self, _button: Gtk.Button) -> None:
        if self.current_playable is None:
            return

        def done(_channel: Channel) -> None:
            self.player_subscribe.set_sensitive(False)
            self.reload_channels()

        self.run_task(
            "Subscribing...",
            lambda: self.service.subscribe_to_video_channel(self.current_playable.video),
            done,
        )

    def reload_feed(self) -> None:
        self.populate_video_list(self.feed_list, self.service.repository.subscription_feed())

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
        self.populate_video_list(self.history_list, self.service.repository.watch_history(query))

    def refresh_one_channel(self, channel: Channel) -> None:
        self.run_task(
            f"Refreshing {channel.title}...",
            lambda: self.service.refresh_channel(channel),
            lambda _videos: self.show_channel_videos(channel),
        )

    def show_channel_videos(self, channel: Channel) -> None:
        self.populate_video_list(self.feed_list, self.service.repository.channel_videos(channel.id))
        self.stack.set_visible_child_name("feed")

    def run_recent_search(self, query: str) -> None:
        self.search_entry.set_text(query)
        self.on_search_clicked(self.search_entry)

    def populate_video_list(self, listbox: Gtk.ListBox, videos: list[Video]) -> None:
        self.clear_listbox(listbox)
        for video in videos:
            listbox.append(self.video_row(video))

    def video_row(self, video: Video) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        row.set_child(box)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        box.append(text)
        title = Gtk.Label(label=video.title, xalign=0)
        title.set_wrap(True)
        text.append(title)

        meta = self.video_meta(video)
        subtitle = Gtk.Label(label=meta, xalign=0)
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        text.append(subtitle)

        play = Gtk.Button(label="Play")
        play.connect("clicked", lambda _button: self.play_video(video))
        box.append(play)
        return row

    def video_meta(self, video: Video) -> str:
        parts = []
        if video.channel_title:
            parts.append(video.channel_title)
        if video.published_at:
            parts.append(video.published_at)
        if video.percent_watched:
            parts.append(f"{round(video.percent_watched * 100)}% watched")
        if video.completed:
            parts.append("completed")
        return " · ".join(parts)

    def play_video(self, video: Video) -> None:
        self.stack.set_visible_child_name("player")
        self.run_task("Resolving video...", lambda: self.service.play_video(video), self.load_playable)

    def load_playable(self, playable: PlayableVideo) -> None:
        self.flush_watch_range()
        self.stop_pipeline()
        self.current_playable = playable
        self.player_title.set_text(playable.video.title)
        self.url_entry.set_text(playable.video.url)
        self.player_subscribe.set_sensitive(
            not self.service.repository.is_subscribed(playable.video.channel_id)
        )

        pipeline = self.create_pipeline(playable.stream_url)
        if pipeline is None:
            return
        self.pipeline = pipeline
        self.watch_pipeline_bus(pipeline)
        pipeline.set_state(Gst.State.PLAYING)

        resume = self.service.repository.resume_position(playable.video.id)
        if resume > 0:
            GLib.timeout_add(
                500,
                lambda: self.seek_media(resume),
            )
        self.range_start_seconds = self.current_position_seconds()
        self.stack.set_visible_child_name("player")

    def create_pipeline(self, uri: str) -> Gst.Element | None:
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
        pipeline.set_property("uri", uri)
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
            if debug:
                print(debug)
        elif message.type == Gst.MessageType.EOS:
            self.flush_watch_range()
            self.set_pipeline_state(Gst.State.PAUSED)

    def stop_pipeline(self) -> None:
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

    def seek_media(self, seconds: int) -> bool:
        if self.pipeline is not None:
            self.pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                seconds * Gst.SECOND,
            )
            self.range_start_seconds = seconds
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

    def clear_listbox(self, listbox: Gtk.ListBox) -> None:
        child = listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            listbox.remove(child)
            child = next_child
