from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from gtktube.sponsorblock import SponsorBlockError


class SponsorBlockMixin:
    def load_sponsorblock_segments(self) -> None:
        self.sponsorblock_segments = []
        self.suppressed_sponsorblock_segments = set()
        self.last_auto_skipped_segment = None
        self.pending_sponsorblock_skip = None
        self.sponsorblock_timeline.queue_draw()
        if self.current_playable is None:
            return
        if not self.service.repository.sponsorblock_enabled():
            return
        video_id = self.current_playable.video.id
        categories = self.service.repository.sponsorblock_categories()
        cached_segments, fresh = self.service.repository.cached_sponsorblock_segments(
            video_id,
            categories,
        )
        self.sponsorblock_segments = cached_segments
        self.sponsorblock_timeline.queue_draw()
        if fresh:
            return

        future = self.executor.submit(
            self.sponsorblock.segments,
            video_id,
            categories,
        )

        def done() -> bool:
            try:
                segments = future.result()
            except SponsorBlockError as exc:
                self.log(f"SponsorBlock lookup failed: {exc}")
                return False
            if (
                self.current_playable is None
                or self.current_playable.video.id != video_id
            ):
                return False
            self.service.repository.store_sponsorblock_segments(
                video_id,
                categories,
                segments,
            )
            self.sponsorblock_segments = segments
            self.sponsorblock_timeline.queue_draw()
            return False

        future.add_done_callback(lambda _future: GLib.idle_add(done))

    def draw_sponsorblock_timeline(
        self,
        _area: Gtk.DrawingArea,
        context: object,
        width: int,
        height: int,
    ) -> None:
        duration = self.current_duration_seconds()
        if duration <= 0 or not self.sponsorblock_segments:
            return
        trough_inset = 12
        drawable_width = max(1, width - (trough_inset * 2))
        bar_height = 2
        y = max(0, int(height/2 - bar_height + 5))
        for segment in self.sponsorblock_segments:
            start = max(0.0, min(segment.start_seconds, float(duration)))
            end = max(0.0, min(segment.end_seconds, float(duration)))
            if end <= start:
                continue
            x = trough_inset + int(drawable_width * start / duration)
            segment_width = max(2, int(drawable_width * (end - start) / duration))
            red, green, blue = self.sponsorblock_segment_color(segment.category)
            context.set_source_rgba(red, green, blue, 0.85)
            context.rectangle(x, y, segment_width, bar_height)
            context.fill()

    def sponsorblock_segment_color(self, category: str) -> tuple[float, float, float]:
        colors = {
            "sponsor": (1.0, 0.72, 0.2),
            "selfpromo": (0.55, 0.8, 1.0),
            "interaction": (0.65, 0.55, 1.0),
            "intro": (0.45, 0.9, 0.55),
            "outro": (0.95, 0.45, 0.55),
            "preview": (0.95, 0.65, 0.95),
            "music_offtopic": (0.45, 0.9, 0.85),
            "filler": (0.75, 0.75, 0.75),
        }
        return colors.get(category, (1.0, 0.72, 0.2))

    def suppress_sponsorblock_for_seek(self, seconds: int) -> None:
        for segment in self.sponsorblock_segments:
            if segment.start_seconds - 5 <= seconds < segment.end_seconds:
                self.suppressed_sponsorblock_segments.add(segment.key)

    def maybe_skip_sponsorblock_segment(self, current: int) -> None:
        if self.player is None:
            return
        if not self.service.repository.sponsorblock_enabled():
            return
        if bool(getattr(self.player, "pause", False)):
            return
        for segment in self.sponsorblock_segments:
            if not segment.start_seconds <= current < segment.end_seconds:
                continue
            if segment.key in self.suppressed_sponsorblock_segments:
                return
            if segment.key == self.last_auto_skipped_segment:
                return
            self.last_auto_skipped_segment = segment.key
            self.set_status(f"Skipped SponsorBlock {segment.category}")
            target = int(segment.end_seconds)
            self.pending_sponsorblock_skip = {
                "category": segment.category,
                "target": target,
                "started": time.monotonic(),
                "reported": False,
            }
            self.log(
                "SponsorBlock skip start "
                f"category={segment.category} "
                f"current={current} "
                f"segment={segment.start_seconds:.3f}-{segment.end_seconds:.3f} "
                f"target={target}"
            )
            call_started = time.monotonic()
            self.seek_media(target, user_initiated=False)
            self.log(
                "SponsorBlock seek command returned "
                f"category={segment.category} "
                f"target={target} "
                f"elapsed={time.monotonic() - call_started:.3f}s"
            )
            return

    def maybe_log_sponsorblock_skip_ready(self, current: int) -> None:
        pending = self.pending_sponsorblock_skip
        if pending is None or bool(pending.get("reported")):
            return
        target = pending.get("target")
        started = pending.get("started")
        if not isinstance(target, int) or not isinstance(started, float):
            self.pending_sponsorblock_skip = None
            return
        if current < target:
            return
        pending["reported"] = True
        elapsed = time.monotonic() - started
        self.log(
            "SponsorBlock skip reached target "
            f"category={pending.get('category')} "
            f"target={target} "
            f"current={current} "
            f"elapsed={elapsed:.3f}s"
        )
