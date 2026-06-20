from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from gtktube.db.repositories import SPONSORBLOCK_CATEGORIES, SPONSORBLOCK_CATEGORY_LABELS
from gtktube.extractors.youtube import ExtractorError
from gtktube.models import Channel
from gtktube.ui.player import USER_SELECTABLE_QUALITIES


class SettingsMixin:
    def build_settings_page(self) -> None:
        self.settings_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page = self.settings_page
        page.set_margin_top(18)
        page.set_margin_bottom(18)
        page.set_margin_start(18)
        page.set_margin_end(18)

        title = Gtk.Label(label="Feed", xalign=0)
        title.add_css_class("heading")
        page.append(title)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_valign(Gtk.Align.CENTER)
        page.append(row)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        row.append(labels)
        label = Gtk.Label(label="Videos per channel per day", xalign=0)
        labels.append(label)
        help_label = Gtk.Label(
            label=(
                "Limits how many videos from one channel can appear in the "
                "main feed for a single publish day."
            ),
            xalign=0,
            wrap=True,
        )
        help_label.add_css_class("dim-label")
        labels.append(help_label)

        adjustment = Gtk.Adjustment(
            value=3,
            lower=1,
            upper=25,
            step_increment=1,
            page_increment=5,
        )
        self.feed_daily_limit_reset_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("edit-undo-symbolic")
        )
        self.feed_daily_limit_reset_button.set_tooltip_text(
            "Reset to the app default"
        )
        self.feed_daily_limit_reset_button.connect(
            "clicked",
            self.on_feed_daily_limit_reset_clicked,
        )
        row.append(self.feed_daily_limit_reset_button)

        self.feed_daily_limit_spin = Gtk.SpinButton(
            adjustment=adjustment,
            climb_rate=1,
            digits=0,
        )
        self.feed_daily_limit_spin.set_numeric(True)
        self.feed_daily_limit_spin.connect(
            "value-changed",
            self.on_feed_daily_limit_changed,
        )
        row.append(self.feed_daily_limit_spin)

        refresh_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        refresh_row.set_valign(Gtk.Align.CENTER)
        page.append(refresh_row)

        refresh_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True
        )
        refresh_row.append(refresh_labels)
        refresh_label = Gtk.Label(label="Concurrent channel refreshes", xalign=0)
        refresh_labels.append(refresh_label)
        refresh_help = Gtk.Label(
            label=(
                "How many subscribed channels to pull at once when refreshing "
                "the feed."
            ),
            xalign=0,
            wrap=True,
        )
        refresh_help.add_css_class("dim-label")
        refresh_labels.append(refresh_help)

        refresh_adjustment = Gtk.Adjustment(
            value=10,
            lower=1,
            upper=20,
            step_increment=1,
            page_increment=5,
        )
        self.refresh_workers_reset_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("edit-undo-symbolic")
        )
        self.refresh_workers_reset_button.set_tooltip_text(
            "Reset to the app default"
        )
        self.refresh_workers_reset_button.connect(
            "clicked",
            self.on_refresh_workers_reset_clicked,
        )
        refresh_row.append(self.refresh_workers_reset_button)

        self.refresh_workers_spin = Gtk.SpinButton(
            adjustment=refresh_adjustment,
            climb_rate=1,
            digits=0,
        )
        self.refresh_workers_spin.set_numeric(True)
        self.refresh_workers_spin.connect(
            "value-changed",
            self.on_refresh_workers_changed,
        )
        refresh_row.append(self.refresh_workers_spin)

        playback_title = Gtk.Label(label="Playback", xalign=0)
        playback_title.add_css_class("heading")
        playback_title.set_margin_top(24)
        page.append(playback_title)

        quality_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        quality_row.set_valign(Gtk.Align.CENTER)
        page.append(quality_row)

        quality_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True
        )
        quality_row.append(quality_labels)
        quality_label = Gtk.Label(label="Default video quality", xalign=0)
        quality_labels.append(quality_label)
        quality_help = Gtk.Label(
            label="Used when opening videos before choosing a quality in the player.",
            xalign=0,
            wrap=True,
        )
        quality_help.add_css_class("dim-label")
        quality_labels.append(quality_help)

        self.default_quality_reset_button = Gtk.Button(
            child=Gtk.Image.new_from_icon_name("edit-undo-symbolic")
        )
        self.default_quality_reset_button.set_tooltip_text(
            "Reset to the app default"
        )
        self.default_quality_reset_button.connect(
            "clicked",
            self.on_default_quality_reset_clicked,
        )
        quality_row.append(self.default_quality_reset_button)

        self.default_quality_combo = Gtk.ComboBoxText()
        for quality in USER_SELECTABLE_QUALITIES:
            self.default_quality_combo.append(
                self.quality_option_id("streaming", quality),
                f"⇄ {quality}",
            )
        for quality in USER_SELECTABLE_QUALITIES:
            self.default_quality_combo.append(
                self.quality_option_id("prefetch", quality),
                f"↓ {quality}",
            )
        self.default_quality_combo.connect(
            "changed",
            self.on_default_quality_changed,
        )
        quality_row.append(self.default_quality_combo)

        privacy_title = Gtk.Label(label="Privacy & Cookies", xalign=0)
        privacy_title.add_css_class("heading")
        privacy_title.set_margin_top(24)
        page.append(privacy_title)

        self.privacy_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.privacy_row.set_valign(Gtk.Align.CENTER)
        page.append(self.privacy_row)

        privacy_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True
        )
        self.privacy_row.append(privacy_labels)
        privacy_label = Gtk.Label(label="Use browser cookies to watch videos...", xalign=0)
        privacy_labels.append(privacy_label)
        self.privacy_help = Gtk.Label(
            label="",
            xalign=0,
            wrap=True,
        )
        self.privacy_help.add_css_class("dim-label")
        privacy_labels.append(self.privacy_help)

        self.cookies_mode_combo = Gtk.ComboBoxText()
        self.cookies_mode_combo.set_valign(Gtk.Align.CENTER)
        self.cookies_mode_combo.append("never", "never")
        self.cookies_mode_combo.append("restricted_prompt", "when a video is restricted (prompt)")
        self.cookies_mode_combo.append("restricted_auto", "when a video is restricted (automatic)")
        self.cookies_mode_combo.append("always", "always")
        self.cookies_mode_combo.connect(
            "changed",
            self.on_cookies_mode_changed,
        )
        self.privacy_row.append(self.cookies_mode_combo)

        self.recommended_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.recommended_row.set_valign(Gtk.Align.CENTER)
        page.append(self.recommended_row)

        recommended_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True
        )
        self.recommended_row.append(recommended_labels)
        recommended_label = Gtk.Label(label="Show YouTube recommended videos", xalign=0)
        recommended_labels.append(recommended_label)
        recommended_help = Gtk.Label(
            label="Requires browser cookies to fetch your personalized home feed.",
            xalign=0,
            wrap=True,
        )
        recommended_help.add_css_class("dim-label")
        recommended_labels.append(recommended_help)

        self.recommended_switch = Gtk.Switch()
        self.recommended_switch.set_valign(Gtk.Align.CENTER)
        self.recommended_switch.connect("state-set", self.on_recommended_setting_changed)
        self.recommended_row.append(self.recommended_switch)

        self.youtube_history_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12
        )
        self.youtube_history_row.set_valign(Gtk.Align.CENTER)
        page.append(self.youtube_history_row)

        youtube_history_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True
        )
        self.youtube_history_row.append(youtube_history_labels)
        youtube_history_label = Gtk.Label(
            label="Import YouTube watch history",
            xalign=0,
        )
        youtube_history_labels.append(youtube_history_label)
        youtube_history_help = Gtk.Label(
            label=(
                "Hourly and optional. Requires browser cookies, then marks "
                "videos from YouTube's watch history as watched locally."
            ),
            xalign=0,
            wrap=True,
        )
        youtube_history_help.add_css_class("dim-label")
        youtube_history_labels.append(youtube_history_help)

        self.youtube_history_import_switch = Gtk.Switch()
        self.youtube_history_import_switch.set_valign(Gtk.Align.CENTER)
        self.youtube_history_import_switch.connect(
            "state-set",
            self.on_youtube_history_import_setting_changed,
        )
        self.youtube_history_row.append(self.youtube_history_import_switch)

        self.import_channels_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12
        )
        self.import_channels_row.set_valign(Gtk.Align.CENTER)
        page.append(self.import_channels_row)

        import_channels_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True
        )
        self.import_channels_row.append(import_channels_labels)
        import_channels_label = Gtk.Label(
            label="Import YouTube subscriptions",
            xalign=0,
        )
        import_channels_labels.append(import_channels_label)
        import_channels_help = Gtk.Label(
            label=(
                "One-time import. Requires browser cookies, then adds your "
                "YouTube subscriptions as GTKTube channels."
            ),
            xalign=0,
            wrap=True,
        )
        import_channels_help.add_css_class("dim-label")
        import_channels_labels.append(import_channels_help)

        self.import_channels_button = Gtk.Button(label="Import channels")
        self.import_channels_button.set_valign(Gtk.Align.CENTER)
        self.import_channels_button.connect(
            "clicked",
            self.on_import_subscription_channels_clicked,
        )
        self.import_channels_row.append(self.import_channels_button)

        self.browser_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.browser_row.set_valign(Gtk.Align.CENTER)
        page.append(self.browser_row)

        browser_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True
        )
        self.browser_row.append(browser_labels)
        browser_label = Gtk.Label(label="Browser Cookies", xalign=0)
        browser_labels.append(browser_label)
        self.browser_help = Gtk.Label(
            label=(
                "Using cookies allows you to watch age-restricted and members-only videos, "
                "but allows YouTube to track your viewing history."
            ),
            xalign=0,
            wrap=True,
        )
        self.browser_help.add_css_class("dim-label")
        browser_labels.append(self.browser_help)

        self.cookies_browser_combo = Gtk.ComboBoxText()
        self.cookies_browser_combo.set_valign(Gtk.Align.CENTER)
        self.cookies_browser_combo.append("", "None")
        try:
            for browser in self.service.supported_browsers():
                self.cookies_browser_combo.append(browser, browser.capitalize())
        except ExtractorError as exc:
            if hasattr(self, "log"):
                getattr(self, "log")(str(exc))
        self.cookies_browser_combo.connect(
            "changed",
            self.on_cookies_browser_changed,
        )
        self.browser_row.append(self.cookies_browser_combo)

        sponsor_title = Gtk.Label(label="SponsorBlock", xalign=0)
        sponsor_title.add_css_class("heading")
        sponsor_title.set_margin_top(24)
        page.append(sponsor_title)

        sponsor_help = Gtk.Label(
            label=(
                "Optional. When enabled, GTKTube sends the current YouTube "
                "video ID to SponsorBlock to look up community segment ranges."
            ),
            xalign=0,
            wrap=True,
        )
        sponsor_help.add_css_class("dim-label")
        page.append(sponsor_help)

        self.sponsorblock_enabled_check = Gtk.CheckButton(label="Enable SponsorBlock")
        self.sponsorblock_enabled_check.connect(
            "toggled",
            self.on_sponsorblock_setting_changed,
        )
        page.append(self.sponsorblock_enabled_check)

        category_label = Gtk.Label(label="Auto-skip categories", xalign=0)
        category_label.add_css_class("dim-label")
        page.append(category_label)

        self.sponsorblock_category_checks: dict[str, Gtk.CheckButton] = {}
        category_grid = Gtk.FlowBox()
        category_grid.set_selection_mode(Gtk.SelectionMode.NONE)
        category_grid.set_max_children_per_line(4)
        category_grid.set_column_spacing(8)
        category_grid.set_row_spacing(4)
        for category in SPONSORBLOCK_CATEGORIES:
            check = Gtk.CheckButton(
                label=SPONSORBLOCK_CATEGORY_LABELS.get(category, category)
            )
            check.connect("toggled", self.on_sponsorblock_setting_changed)
            self.sponsorblock_category_checks[category] = check
            category_grid.append(check)
        page.append(category_grid)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(page)
        self.stack.add_named(scrolled, "settings")

    def reload_settings(self) -> None:
        self.updating_settings = True
        self.feed_daily_limit_spin.set_value(
            self.service.repository.feed_daily_channel_limit()
        )
        self.feed_daily_limit_reset_button.set_visible(
            self.service.repository.has_feed_daily_channel_limit_override()
        )
        self.refresh_workers_spin.set_value(
            self.service.repository.refresh_worker_count()
        )
        self.refresh_workers_reset_button.set_visible(
            self.service.repository.has_refresh_worker_count_override()
        )
        self.preferred_playback_mode = self.service.repository.default_playback_mode()
        self.preferred_quality = self.service.repository.default_video_quality()
        self.default_quality_combo.set_active_id(
            self.quality_option_id(
                self.preferred_playback_mode,
                self.preferred_quality,
            )
        )
        self.default_quality_reset_button.set_visible(
            self.service.repository.has_default_video_quality_override()
        )
        self.cookies_mode_combo.set_active_id(
            self.service.repository.yt_dlp_cookies_mode()
        )
        self.cookies_browser_combo.set_active_id(
            self.service.repository.yt_dlp_cookies_browser()
        )
        show_recommended = self.service.repository.show_recommended_videos()
        self.recommended_switch.set_active(show_recommended is True)
        self.youtube_history_import_switch.set_active(
            self.service.repository.import_youtube_watch_history_enabled()
        )
        self._update_privacy_help()
        self.sponsorblock_enabled_check.set_active(
            self.service.repository.sponsorblock_enabled()
        )
        enabled_categories = set(self.service.repository.sponsorblock_categories())
        for category, check in self.sponsorblock_category_checks.items():
            check.set_active(category in enabled_categories)
        self.updating_settings = False

    def on_feed_daily_limit_changed(self, spin: Gtk.SpinButton) -> None:
        if self.updating_settings:
            return
        limit = spin.get_value_as_int()
        self.service.repository.set_feed_daily_channel_limit(limit)
        self.feed_daily_limit_reset_button.set_visible(True)
        self.feed_limit = 100
        if self.current_view and self.current_view.page == "feed":
            self.reload_feed()

    def on_feed_daily_limit_reset_clicked(self, _button: Gtk.Button) -> None:
        self.service.repository.clear_feed_daily_channel_limit()
        self.reload_settings()
        self.feed_limit = 100
        if self.current_view and self.current_view.page == "feed":
            self.reload_feed()

    def on_refresh_workers_changed(self, spin: Gtk.SpinButton) -> None:
        if self.updating_settings:
            return
        self.service.repository.set_refresh_worker_count(spin.get_value_as_int())
        self.refresh_workers_reset_button.set_visible(True)

    def on_refresh_workers_reset_clicked(self, _button: Gtk.Button) -> None:
        self.service.repository.clear_refresh_worker_count()
        self.reload_settings()

    def on_default_quality_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self.updating_settings:
            return
        option = self.parse_quality_option_id(combo.get_active_id())
        if option is None:
            return
        mode, quality = option
        self.preferred_playback_mode = mode
        self.preferred_quality = quality
        self.service.repository.set_default_video_quality(quality, mode=mode)
        self.default_quality_reset_button.set_visible(True)
        self.updating_quality = True
        self.quality_combo.set_active_id(
            self.quality_option_id(mode, quality)
        )
        self.updating_quality = False
        self.update_quality_combo_tooltip()

    def on_default_quality_reset_clicked(self, _button: Gtk.Button) -> None:
        self.service.repository.clear_default_video_quality()
        self.reload_settings()
        self.updating_quality = True
        self.quality_combo.set_active_id(
            self.quality_option_id(
                self.preferred_playback_mode,
                self.preferred_quality,
            )
        )
        self.updating_quality = False
        self.update_quality_combo_tooltip()

    def _update_privacy_help(self) -> None:
        mode = self.cookies_mode_combo.get_active_id()
        show_recommended = self.recommended_switch.get_active()
        import_history = self.youtube_history_import_switch.get_active()
        needs_cookies = (mode != "never") or show_recommended or import_history
        self.browser_row.set_sensitive(needs_cookies)

        if mode == "never":
            self.privacy_help.set_label(
                "Never using cookies gives you the most privacy, but your watch history will not appear on youtube.com and you'll be blocked from watching restricted videos."
            )
        elif mode == "restricted_prompt":
            self.privacy_help.set_label(
                "You will be prompted to use cookies when an age-restricted or members-only video is encountered. Your watch history won't appear on youtube.com for most videos."
            )
        elif mode == "restricted_auto":
            self.privacy_help.set_label(
                "Cookies will be used automatically to play age-restricted and members-only videos. Your watch history won't appear on youtube.com for most videos."
            )
        elif mode == "always":
            self.privacy_help.set_label(
                "Always using cookies means your watch history will appear on youtube.com and you can watch restricted videos, but YouTube can track your viewing habits."
            )

    def on_cookies_mode_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self.updating_settings:
            return
        mode = combo.get_active_id()
        if mode:
            self.service.repository.set_yt_dlp_cookies_mode(mode)
            self._update_privacy_help()

    def on_cookies_browser_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self.updating_settings:
            return
        browser = combo.get_active_id()
        if browser is not None:
            self.service.repository.set_yt_dlp_cookies_browser(browser)
            if hasattr(self, "clear_recommended_cache"):
                getattr(self, "clear_recommended_cache")()
            self._update_privacy_help()

            if browser and self.service.repository.show_recommended_videos() is True:
                if hasattr(self, "reload_recommended"):
                    getattr(self, "reload_recommended")(force=True)

            if (
                browser
                and self.service.repository.import_youtube_watch_history_enabled()
                and hasattr(self, "maybe_import_youtube_watch_history")
            ):
                getattr(self, "maybe_import_youtube_watch_history")(force=True)

    def on_recommended_setting_changed(self, switch: Gtk.Switch, state: bool) -> bool:
        if self.updating_settings:
            return False
        self.service.repository.set_show_recommended_videos(state)
        self._update_privacy_help()
        if hasattr(self, "update_recommended_nav_visibility"):
            getattr(self, "update_recommended_nav_visibility")()
        return False

    def on_youtube_history_import_setting_changed(
        self, switch: Gtk.Switch, state: bool
    ) -> bool:
        if self.updating_settings:
            return False
        self.service.repository.set_import_youtube_watch_history_enabled(state)
        self._update_privacy_help()
        if state and hasattr(self, "maybe_import_youtube_watch_history"):
            getattr(self, "maybe_import_youtube_watch_history")(force=True)
        return False

    def on_import_subscription_channels_clicked(self, _button: Gtk.Button) -> None:
        self.show_import_subscription_channels_dialog()

    def show_import_subscription_channels_dialog(self) -> None:
        dialog = Gtk.Dialog(
            title="Import YouTube subscriptions",
            transient_for=self,
            modal=True,
        )
        back_button = Gtk.Button(label="Back")
        back_button.set_visible(False)
        preview_button = Gtk.Button(label="Preview")
        preview_button.add_css_class("suggested-action")
        preview_button.set_sensitive(False)
        import_button = Gtk.Button(label="Import selected")
        import_button.add_css_class("suggested-action")
        import_button.set_visible(False)
        import_button.set_sensitive(False)
        cancel_button = Gtk.Button(label="Cancel")
        dialog.set_default_size(540, -1)

        content = dialog.get_content_area()
        content.set_margin_top(24)
        content.set_margin_bottom(18)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_spacing(18)

        stack = Gtk.Stack()
        stack.set_hhomogeneous(False)
        stack.set_vhomogeneous(False)
        content.append(stack)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.set_valign(Gtk.Align.CENTER)
        content.append(footer)

        footer.append(back_button)
        footer_spacer = Gtk.Box(hexpand=True)
        footer.append(footer_spacer)
        footer.append(cancel_button)
        footer.append(preview_button)
        footer.append(import_button)

        intro_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        intro_page.set_halign(Gtk.Align.CENTER)
        intro_page.set_valign(Gtk.Align.START)
        intro_page.set_size_request(500, -1)
        stack.add_named(intro_page, "intro")

        intro_title = Gtk.Label(label="Import channels from YouTube", xalign=0)
        intro_title.add_css_class("title-4")
        intro_title.set_max_width_chars(54)
        intro_page.append(intro_title)

        intro_text = Gtk.Label(
            label=(
                "GTKTube can use browser cookies one time to read the "
                "subscriptions from your signed-in YouTube account. After the "
                "preview loads, choose which new channels to add. Existing "
                "GTKTube subscriptions are skipped."
            ),
            xalign=0,
            wrap=True,
        )
        intro_text.set_max_width_chars(64)
        intro_page.append(intro_text)

        browser_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        browser_row.set_valign(Gtk.Align.CENTER)
        intro_page.append(browser_row)

        browser_label = Gtk.Label(label="Browser cookies", xalign=0, hexpand=True)
        browser_row.append(browser_label)

        browser_combo = Gtk.ComboBoxText()
        browser_combo.set_valign(Gtk.Align.CENTER)
        browser_combo.append("", "Choose browser")
        try:
            for browser in self.service.supported_browsers():
                browser_combo.append(browser, browser.capitalize())
        except ExtractorError as exc:
            self.log(str(exc))
        browser_combo.set_active_id(self.service.repository.yt_dlp_cookies_browser())
        browser_row.append(browser_combo)

        intro_help = Gtk.Label(
            label=(
                "The browser choice is only used for this import and does not "
                "change the saved cookie preference."
            ),
            xalign=0,
            wrap=True,
        )
        intro_help.set_max_width_chars(64)
        intro_help.add_css_class("dim-label")
        intro_page.append(intro_help)

        intro_loading_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        intro_loading_row.set_valign(Gtk.Align.CENTER)
        intro_loading_spinner = Gtk.Spinner()
        intro_loading_row.append(intro_loading_spinner)
        intro_loading_label = Gtk.Label(
            label="Loading YouTube subscriptions...",
            xalign=0,
        )
        intro_loading_label.add_css_class("dim-label")
        intro_loading_row.append(intro_loading_label)
        intro_loading_row.set_visible(False)
        intro_page.append(intro_loading_row)

        intro_error = Gtk.Label(label="", xalign=0, wrap=True)
        intro_error.set_max_width_chars(64)
        intro_error.add_css_class("error")
        intro_error.set_visible(False)
        intro_page.append(intro_error)

        preview_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        stack.add_named(preview_page, "preview")

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        preview_page.append(actions)
        select_all_button = Gtk.Button(label="Select all")
        select_none_button = Gtk.Button(label="Select none")
        select_all_button.set_sensitive(False)
        select_none_button.set_sensitive(False)
        actions.append(select_all_button)
        actions.append(select_none_button)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_row.set_valign(Gtk.Align.CENTER)
        preview_page.append(status_row)
        spinner = Gtk.Spinner()
        status_row.append(spinner)
        status_label = Gtk.Label(
            label="",
            xalign=0,
            hexpand=True,
        )
        status_label.add_css_class("dim-label")
        status_row.append(status_label)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(420)
        scroller.set_child(listbox)
        preview_page.append(scroller)

        preview_channels: list[Channel] = []
        channel_checks: dict[str, Gtk.CheckButton] = {}

        def update_import_button() -> None:
            import_button.set_sensitive(
                any(check.get_active() for check in channel_checks.values())
            )

        def clear_preview() -> None:
            preview_channels.clear()
            channel_checks.clear()
            self.clear_listbox(listbox)
            import_button.set_sensitive(False)
            select_all_button.set_sensitive(False)
            select_none_button.set_sensitive(False)

        def append_channel_row(channel: Channel) -> None:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(8)
            box.set_margin_end(8)

            check = Gtk.CheckButton()
            check.set_active(True)
            check.connect("toggled", lambda _check: update_import_button())
            channel_checks[channel.id] = check
            box.append(check)

            if channel.thumbnail_url:
                icon = Gtk.Picture()
                icon.add_css_class("channel-avatar")
                icon.set_size_request(32, 32)
                icon.set_can_shrink(False)
                icon.set_content_fit(Gtk.ContentFit.COVER)
                self.load_channel_nav_icon(channel, icon)
                box.append(icon)
            else:
                placeholder = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
                placeholder.add_css_class("channel-avatar")
                placeholder.set_pixel_size(32)
                placeholder.set_size_request(32, 32)
                box.append(placeholder)

            labels = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=3,
                hexpand=True,
            )
            title = Gtk.Label(label=channel.title, xalign=0, hexpand=True)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            labels.append(title)
            url = Gtk.Label(label=channel.url, xalign=0)
            url.add_css_class("dim-label")
            url.set_ellipsize(Pango.EllipsizeMode.END)
            labels.append(url)
            box.append(labels)
            row.set_child(box)
            listbox.append(row)

        def set_preview_controls_sensitive(sensitive: bool) -> None:
            browser_combo.set_sensitive(sensitive)
            preview_button.set_sensitive(
                sensitive and bool(browser_combo.get_active_id())
            )
            cancel_button.set_sensitive(sensitive)
            select_all_button.set_sensitive(sensitive and bool(channel_checks))
            select_none_button.set_sensitive(sensitive and bool(channel_checks))
            if sensitive:
                update_import_button()
            else:
                import_button.set_sensitive(False)

        def set_intro_loading(loading: bool) -> None:
            browser_combo.set_sensitive(not loading)
            preview_button.set_sensitive(
                (not loading) and bool(browser_combo.get_active_id())
            )
            cancel_button.set_sensitive(not loading)
            intro_loading_row.set_visible(loading)
            if loading:
                intro_error.set_visible(False)
                intro_loading_spinner.start()
            else:
                intro_loading_spinner.stop()

        def show_intro_step() -> None:
            stack.set_visible_child_name("intro")
            back_button.set_visible(False)
            preview_button.set_visible(True)
            import_button.set_visible(False)
            cancel_button.set_sensitive(True)
            browser_combo.set_sensitive(True)
            preview_button.set_sensitive(bool(browser_combo.get_active_id()))
            dialog.set_default_size(540, -1)

        def show_preview_step() -> None:
            intro_error.set_visible(False)
            stack.set_visible_child_name("preview")
            back_button.set_visible(True)
            preview_button.set_visible(False)
            import_button.set_visible(True)
            dialog.set_default_size(680, -1)

        def import_error_message(exc: Exception) -> str:
            message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(exc)).strip()
            if message.startswith("ERROR:"):
                message = message.removeprefix("ERROR:").strip()
            lower = message.lower()
            if "could not find" in lower and "cookies database" in lower:
                browser = browser_combo.get_active_text() or "selected browser"
                return (
                    f"Could not find a cookies database for {browser}. Choose a "
                    "browser where you are signed in to YouTube, then try again."
                )
            return f"Could not load subscriptions: {message}"

        def load_preview() -> None:
            browser = browser_combo.get_active_id()
            if not browser:
                return
            clear_preview()
            set_intro_loading(True)
            status_label.set_label("Loading YouTube subscriptions...")

            def done(channels: list[Channel]) -> None:
                new_channels = [
                    channel
                    for channel in channels
                    if not self.service.repository.is_subscribed(channel.id)
                ]
                preview_channels.extend(new_channels)
                for channel in new_channels:
                    append_channel_row(channel)
                if new_channels:
                    status_label.set_label(
                        f"Found {len(new_channels)} new YouTube subscriptions."
                    )
                else:
                    status_label.set_label("Found 0 new YouTube subscriptions.")
                show_preview_step()
                set_preview_controls_sensitive(True)

            def failed(exc: Exception) -> None:
                clear_preview()
                intro_error.set_label(import_error_message(exc))
                intro_error.set_visible(True)
                show_intro_step()

            def finished() -> None:
                set_intro_loading(False)

            self.run_task(
                "Loading YouTube subscriptions...",
                lambda: self.service.youtube_subscription_channels(
                    browser,
                    limit=1000,
                ),
                done,
                finished=finished,
                error=failed,
            )

        def set_all(active: bool) -> None:
            for check in channel_checks.values():
                check.set_active(active)
            update_import_button()

        def import_selected_channels() -> None:
            selected = [
                channel
                for channel in preview_channels
                if channel_checks.get(channel.id)
                and channel_checks[channel.id].get_active()
            ]
            if not selected:
                status_label.set_label("Select at least one channel to import.")
                return
            set_preview_controls_sensitive(False)
            spinner.start()
            status_label.set_label("Importing selected channels...")
            imported_count: int | None = None

            def done(count: int) -> None:
                nonlocal imported_count
                imported_count = count
                self.reload_channels()
                self.feed_limit = 100
                if self.current_view and self.current_view.page == "feed":
                    self.reload_feed()

            def finished() -> None:
                if imported_count is not None:
                    self.set_status(
                        f"Imported {imported_count} YouTube subscriptions"
                    )
                    dialog.destroy()
                    return
                spinner.stop()
                set_preview_controls_sensitive(True)

            self.run_task(
                "Importing YouTube subscriptions...",
                lambda: self.service.import_subscription_channels(selected),
                done,
                finished=finished,
            )

        back_button.connect(
            "clicked",
            lambda _button: (clear_preview(), show_intro_step()),
        )
        browser_combo.connect(
            "changed",
            lambda combo: preview_button.set_sensitive(bool(combo.get_active_id())),
        )
        preview_button.connect("clicked", lambda _button: load_preview())
        import_button.connect("clicked", lambda _button: import_selected_channels())
        cancel_button.connect("clicked", lambda _button: dialog.destroy())
        select_all_button.connect("clicked", lambda _button: set_all(True))
        select_none_button.connect("clicked", lambda _button: set_all(False))
        show_intro_step()
        dialog.present()

    def on_sponsorblock_setting_changed(self, _widget: Gtk.Widget) -> None:
        if self.updating_settings:
            return
        self.service.repository.set_sponsorblock_enabled(
            self.sponsorblock_enabled_check.get_active()
        )
        self.service.repository.set_sponsorblock_categories(
            [
                category
                for category, check in self.sponsorblock_category_checks.items()
                if check.get_active()
            ]
        )
        self.load_sponsorblock_segments()
