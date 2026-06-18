from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from gtktube.db.repositories import SPONSORBLOCK_CATEGORIES, SPONSORBLOCK_CATEGORY_LABELS
from gtktube.extractors.youtube import QUALITY_FORMATS


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
        for quality in QUALITY_FORMATS:
            self.default_quality_combo.append(quality, quality)
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
        self.preferred_quality = self.service.repository.default_video_quality()
        self.default_quality_combo.set_active_id(self.preferred_quality)
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
        quality = combo.get_active_id()
        if not quality:
            return
        self.preferred_quality = quality
        self.service.repository.set_default_video_quality(quality)
        self.default_quality_reset_button.set_visible(True)
        self.updating_quality = True
        self.quality_combo.set_active_id(quality)
        self.updating_quality = False

    def on_default_quality_reset_clicked(self, _button: Gtk.Button) -> None:
        self.service.repository.clear_default_video_quality()
        self.reload_settings()
        self.updating_quality = True
        self.quality_combo.set_active_id(self.preferred_quality)
        self.updating_quality = False

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
