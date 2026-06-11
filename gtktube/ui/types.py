from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("GObject", "2.0")
from gi.repository import GObject  # noqa: E402

from gtktube.models import Video


class VideoObject(GObject.Object):
    def __init__(self, video: Video):
        super().__init__()
        self.video = video


@dataclass(frozen=True)
class ViewState:
    page: str
    channel_id: str | None = None
    channel_title: str | None = None
