from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from gtktube.models import Channel, Video
from gtktube.ui.types import VideoObject


class ContextMenuMixin:
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

        add_watch_later = Gtk.Button(label="Add to watch later")
        add_watch_later.add_css_class("flat")
        add_watch_later.set_halign(Gtk.Align.FILL)
        add_watch_later.connect(
            "clicked",
            lambda _button: self.activate_video_menu(popover, video, "watch_later"),
        )
        actions.append(add_watch_later)

        mark_played = Gtk.Button(label="Mark as played")
        mark_played.add_css_class("flat")
        mark_played.set_halign(Gtk.Align.FILL)
        mark_played.connect(
            "clicked", lambda _button: self.activate_video_menu(popover, video, "played")
        )
        actions.append(mark_played)

        not_interested = Gtk.Button(label="Not interested")
        not_interested.add_css_class("flat")
        not_interested.set_halign(Gtk.Align.FILL)
        not_interested.connect(
            "clicked",
            lambda _button: self.activate_video_menu(popover, video, "not_interested"),
        )
        actions.append(not_interested)

        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()

    def show_channel_context_menu(
        self, parent: Gtk.Widget, channel: Channel, x: float, y: float
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(parent)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        actions.set_margin_top(6)
        actions.set_margin_bottom(6)
        actions.set_margin_start(6)
        actions.set_margin_end(6)
        popover.set_child(actions)

        open_channel = Gtk.Button(label="Open channel")
        open_channel.add_css_class("flat")
        open_channel.set_halign(Gtk.Align.FILL)
        open_channel.connect(
            "clicked",
            lambda _button: self.activate_channel_menu(popover, channel, "open"),
        )
        actions.append(open_channel)

        subscribed = self.service.repository.is_subscribed(channel.id)
        subscription_label = "Unsubscribe" if subscribed else "Subscribe"
        subscription_action = "unsubscribe" if subscribed else "subscribe"
        subscription = Gtk.Button(label=subscription_label)
        subscription.add_css_class("flat")
        subscription.set_halign(Gtk.Align.FILL)
        subscription.connect(
            "clicked",
            lambda _button: self.activate_channel_menu(
                popover, channel, subscription_action
            ),
        )
        actions.append(subscription)

        copy_url = Gtk.Button(label="Copy channel URL")
        copy_url.add_css_class("flat")
        copy_url.set_halign(Gtk.Align.FILL)
        copy_url.connect(
            "clicked",
            lambda _button: self.activate_channel_menu(popover, channel, "copy"),
        )
        actions.append(copy_url)

        clear_new = Gtk.Button(label="Clear new videos")
        clear_new.add_css_class("flat")
        clear_new.set_halign(Gtk.Align.FILL)
        clear_new.connect(
            "clicked",
            lambda _button: self.activate_channel_menu(popover, channel, "clear_new"),
        )
        actions.append(clear_new)

        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()

    def activate_channel_menu(
        self, popover: Gtk.Popover, channel: Channel, action: str
    ) -> None:
        popover.popdown()
        popover.unparent()
        if action == "open":
            self.open_search_channel(channel)
            return
        if action == "copy":
            self.copy_to_clipboard(channel.url, "Copied channel URL")
            return
        if action == "unsubscribe":
            self.unsubscribe_channel(channel)
            return
        if action == "clear_new":
            self.service.repository.clear_new_video_indicator(channel.id)
            self.reload_channels()
            return
        if action == "subscribe":
            def done(_channel: Channel) -> None:
                self.reload_channels()
                self.reload_feed()
                if self.current_view and self.current_view.channel_id == channel.id:
                    self.apply_view_state(self.current_view)

            self.run_task(
                f"Subscribing to {channel.title}...",
                lambda: self.service.subscribe_with_initial_videos(
                    channel.url,
                    limit=self.channel_video_limits.get(channel.id, 30),
                ),
                done,
            )

    def show_watch_later_context_menu(
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

        mark_played = Gtk.Button(label="Mark as played")
        mark_played.add_css_class("flat")
        mark_played.set_halign(Gtk.Align.FILL)
        mark_played.connect(
            "clicked", lambda _button: self.activate_video_menu(popover, video, "played")
        )
        actions.append(mark_played)

        not_interested = Gtk.Button(label="Not interested")
        not_interested.add_css_class("flat")
        not_interested.set_halign(Gtk.Align.FILL)
        not_interested.connect(
            "clicked",
            lambda _button: self.activate_video_menu(popover, video, "not_interested"),
        )
        actions.append(not_interested)

        remove_watch_later = Gtk.Button(label="Remove from watch later")
        remove_watch_later.add_css_class("flat")
        remove_watch_later.set_halign(Gtk.Align.FILL)
        remove_watch_later.connect(
            "clicked",
            lambda _: self.activate_watch_later_menu(popover, video, "remove"),
        )
        actions.append(remove_watch_later)

        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()

    def activate_watch_later_menu(
        self, popover: Gtk.Popover, video: Video, action: str
    ) -> None:
        popover.popdown()
        popover.unparent()
        if action == "remove":
            self.service.remove_watch_later(video)
            self.reload_watch_later()

    def show_history_context_menu(
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

        add_watch_later = Gtk.Button(label="Add to watch later")
        add_watch_later.add_css_class("flat")
        add_watch_later.set_halign(Gtk.Align.FILL)
        add_watch_later.connect(
            "clicked",
            lambda _button: self.activate_video_menu(popover, video, "watch_later"),
        )
        actions.append(add_watch_later)

        remove_history = Gtk.Button(label="Remove from history")
        remove_history.add_css_class("flat")
        remove_history.set_halign(Gtk.Align.FILL)
        remove_history.connect(
            "clicked",
            lambda _button: self.activate_history_menu(popover, video, "remove"),
        )
        actions.append(remove_history)

        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()

    def activate_history_menu(
        self, popover: Gtk.Popover, video: Video, action: str
    ) -> None:
        popover.popdown()
        popover.unparent()
        if action == "remove":
            self.service.repository.remove_watch_history(video.id)
            self.reload_history()

    def show_queue_context_menu(
        self, row: Gtk.ListBoxRow, video: Video, x: float, y: float
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
        play_now.connect(
            "clicked", lambda _: self.play_from_queue(popover, row.get_index())
        )
        actions.append(play_now)

        remove_queue = Gtk.Button(label="Remove from queue")
        remove_queue.add_css_class("flat")
        remove_queue.set_halign(Gtk.Align.FILL)
        remove_queue.connect(
            "clicked", lambda _: self.remove_from_queue(popover, row.get_index())
        )
        actions.append(remove_queue)

        rectangle = Gdk.Rectangle()
        rectangle.x = int(x)
        rectangle.y = int(y)
        rectangle.width = 1
        rectangle.height = 1
        popover.set_pointing_to(rectangle)
        popover.popup()

    def add_to_queue(self, video: Video) -> None:
        self.video_queue.append(VideoObject(video))
        self.queue_pane.set_visible(True)

    def remove_from_queue(self, popover: Gtk.Popover, index: int) -> None:
        popover.popdown()
        popover.unparent()
        if 0 <= index < self.video_queue.get_n_items():
            self.video_queue.remove(index)
            self.queue_pane.set_visible(self.video_queue.get_n_items() > 0)

    def play_from_queue(self, popover: Gtk.Popover, index: int) -> None:
        popover.popdown()
        popover.unparent()
        self.play_from_queue_index(index)

    def activate_video_menu(
        self, popover: Gtk.Popover, video: Video, action: str
    ) -> None:
        popover.popdown()
        popover.unparent()
        if action == "channel":
            self.open_video_channel(video)
        elif action == "queue":
            self.add_to_queue(video)
        elif action == "watch_later":
            self.service.add_watch_later(video)
            self.reload_watch_later()
        elif action == "played":
            self.service.repository.mark_played(video.id, video.duration_seconds)
            self.reload_history()
            self.reload_channels()
            self.reload_visible_video_grid()
        elif action == "not_interested":
            self.service.repository.hide_video(video.id)
            self.reload_visible_video_grid()
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
