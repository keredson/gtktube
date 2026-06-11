from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from gtktube.models import Video


class VideoGridMixin:
    def create_video_grid(self) -> Gtk.FlowBox:
        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_homogeneous(False)
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

    def video_tile(
        self,
        video: Video,
        on_clicked: Callable[[Gtk.Widget], None] | None = None,
        on_context_menu: Callable[[Gtk.Widget, Video, float, float], None] | None = None,
    ) -> Gtk.Widget:
        tile_width = 232
        thumbnail_height = 131
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tile.add_css_class("video-tile")
        tile.set_size_request(tile_width, -1)
        tile.set_hexpand(False)
        tile.set_halign(Gtk.Align.START)
        tile.set_focusable(True)

        left_click = Gtk.GestureClick()
        left_click.set_button(1)
        left_click.connect(
            "released",
            lambda _gesture, _n_press, _x, _y: (
                on_clicked(tile) if on_clicked else self.play_video(video)
            ),
        )
        tile.add_controller(left_click)

        key_controller = Gtk.EventControllerKey()
        key_controller.connect(
            "key-pressed",
            lambda _controller, keyval, _keycode, _state: self.activate_video_tile(
                tile,
                video,
                on_clicked,
                keyval,
            ),
        )
        tile.add_controller(key_controller)

        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect(
            "pressed",
            lambda _gesture, _n_press, x, y: (
                on_context_menu(tile, video, x, y)
                if on_context_menu
                else self.show_video_context_menu(tile, video, x, y)
            ),
        )
        tile.add_controller(right_click)

        thumbnail = Gtk.Picture()
        thumbnail.set_size_request(tile_width, thumbnail_height)
        thumbnail.set_can_shrink(False)
        thumbnail.set_content_fit(Gtk.ContentFit.COVER)
        self.load_thumbnail(video, thumbnail)

        thumb_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        thumb_container.append(thumbnail)

        if video.watch_ranges:
            progress = Gtk.DrawingArea()
            progress.set_size_request(-1, 3)
            progress.set_draw_func(
                lambda _area, cr, width, height, v=video: self.draw_video_progress(
                    cr, width, height, v
                )
            )
            thumb_container.append(progress)

        tile.append(thumb_container)

        title = Gtk.Label(label=video.title, xalign=0)
        title.set_wrap(True)
        title.set_lines(2)
        title.set_width_chars(28)
        title.set_max_width_chars(28)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_margin_start(6)
        title.set_margin_end(6)
        tile.append(title)

        meta = self.video_meta(video)
        subtitle = Gtk.Label(label=meta, xalign=0)
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_lines(2)
        subtitle.set_width_chars(28)
        subtitle.set_max_width_chars(28)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.set_margin_start(6)
        subtitle.set_margin_end(6)
        subtitle.set_margin_bottom(6)
        tile.append(subtitle)

        return tile

    def activate_video_tile(
        self,
        tile: Gtk.Widget,
        video: Video,
        on_clicked: Callable[[Gtk.Widget], None] | None,
        keyval: int,
    ) -> bool:
        if keyval not in {Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space}:
            return False
        if on_clicked is not None:
            on_clicked(tile)
        else:
            self.play_video(video)
        return True

    def draw_video_progress(
        self,
        cr: Any,
        width: int,
        height: int,
        video: Video,
    ) -> None:
        if not video.duration_seconds or not video.watch_ranges:
            return

        cr.set_source_rgba(1, 1, 1, 0.2)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        cr.set_source_rgb(1, 0, 0)
        for start, end in video.watch_ranges:
            x = (start / video.duration_seconds) * width
            w = ((end - start) / video.duration_seconds) * width
            cr.rectangle(x, 0, max(w, 1), height)
            cr.fill()

