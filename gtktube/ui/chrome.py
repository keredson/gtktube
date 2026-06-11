from __future__ import annotations

import urllib.parse
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from gtktube import __version__
from gtktube.models import Channel
from gtktube.ui.types import ViewState
from gtktube.update_check import UpdateInfo, check_for_update, upgrade_command


class ChromeMixin:
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
            self.feed_grid.set_visible(True)
            self.feed_empty_box.set_visible(False)
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
        elif view.page == "watch_later":
            self.reload_watch_later()
        elif view.page == "settings":
            self.reload_settings()

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
        elif view.page == "watch_later":
            self.header_subtitle.set_text("Watch Later")
        elif view.page == "settings":
            self.header_subtitle.set_text("Settings")
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

    def on_about_clicked(self, _button: Gtk.Button) -> None:
        icon_path = Path(__file__).resolve().parent.parent / "assets" / "gtktube.png"
        dialog = Gtk.AboutDialog(
            transient_for=self,
            modal=True,
            program_name="GTKTube",
            version=__version__,
            comments=(
                "A GPLv3 local privacy-first Python/GTK4 YouTube player."
            ),
            website="https://github.com/keredson/gtktube",
            website_label="https://github.com/keredson/gtktube",
            license_type=Gtk.License.GPL_3_0,
        )
        try:
            dialog.set_logo(Gdk.Texture.new_from_filename(str(icon_path)))
        except GLib.Error:
            pass
        dialog.present()

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

    def start_update_check(self) -> bool:
        future = self.executor.submit(
            check_for_update,
            __version__,
            self.force_update_dialog,
        )

        def finish() -> bool:
            try:
                update = future.result()
            except Exception as exc:
                self.log(f"update check failed: {exc}")
                if self.force_update_dialog and not self.cleaned_up:
                    self.show_update_dialog(
                        UpdateInfo(
                            current_version=__version__,
                            latest_version="unknown",
                            project_url="https://pypi.org/project/gtktube/",
                        )
                    )
                return False
            if update is not None and not self.cleaned_up:
                self.show_update_dialog(update)
            return False

        future.add_done_callback(lambda _future: GLib.idle_add(finish))
        return False

    def show_update_dialog(self, update: UpdateInfo) -> None:
        dialog = Gtk.Dialog(
            title="GTKTube update available",
            transient_for=self,
            modal=True,
        )
        dialog.set_default_size(460, -1)
        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        title = Gtk.Label(label="A newer GTKTube release is available", xalign=0)
        title.add_css_class("heading")
        content.append(title)

        message = Gtk.Label(
            label=(
                f"Installed: {update.current_version}\n"
                f"Latest on PyPI: {update.latest_version}"
            ),
            xalign=0,
            wrap=True,
        )
        content.append(message)

        command = upgrade_command()
        command_label = Gtk.Label(label=command, xalign=0)
        command_label.add_css_class("dim-label")
        command_label.set_selectable(True)
        content.append(command_label)

        dialog.add_button("Not now", Gtk.ResponseType.CANCEL)
        dialog.add_button("Open PyPI", Gtk.ResponseType.HELP)
        dialog.add_button("Copy command", Gtk.ResponseType.ACCEPT)

        def response(_dialog: Gtk.Dialog, response_id: int) -> None:
            if response_id == Gtk.ResponseType.ACCEPT:
                self.copy_to_clipboard(command, "Copied upgrade command")
            elif response_id == Gtk.ResponseType.HELP:
                Gtk.show_uri(self, update.project_url, Gdk.CURRENT_TIME)
            dialog.destroy()

        dialog.connect("response", response)
        dialog.present()

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
