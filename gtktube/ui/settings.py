from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from gtktube.db.repositories import SPONSORBLOCK_CATEGORIES, SPONSORBLOCK_CATEGORY_LABELS
from gtktube.extractors.youtube import QUALITY_FORMATS


class SettingsMixin:
    def build_settings_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
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

        playback_title = Gtk.Label(label="Playback", xalign=0)
        playback_title.add_css_class("heading")
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

        sponsor_title = Gtk.Label(label="SponsorBlock", xalign=0)
        sponsor_title.add_css_class("heading")
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

        self.stack.add_named(page, "settings")

    def reload_settings(self) -> None:
        self.updating_settings = True
        self.feed_daily_limit_spin.set_value(
            self.service.repository.feed_daily_channel_limit()
        )
        self.feed_daily_limit_reset_button.set_visible(
            self.service.repository.has_feed_daily_channel_limit_override()
        )
        self.preferred_quality = self.service.repository.default_video_quality()
        self.default_quality_combo.set_active_id(self.preferred_quality)
        self.default_quality_reset_button.set_visible(
            self.service.repository.has_default_video_quality_override()
        )
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
