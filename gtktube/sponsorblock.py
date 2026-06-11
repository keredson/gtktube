from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from gtktube.models import SponsorBlockSegment


class SponsorBlockError(RuntimeError):
    pass


class SponsorBlockClient:
    def __init__(self, base_url: str = "https://sponsor.ajay.app") -> None:
        self.base_url = base_url.rstrip("/")

    def segments(
        self, video_id: str, categories: list[str]
    ) -> list[SponsorBlockSegment]:
        if not categories:
            return []
        query = urllib.parse.urlencode(
            {
                "videoID": video_id,
                "categories": json.dumps(categories, separators=(",", ":")),
            }
        )
        request = urllib.request.Request(
            f"{self.base_url}/api/skipSegments?{query}",
            headers={"User-Agent": "GTKTube/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = response.read(1_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            raise SponsorBlockError(str(exc)) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise SponsorBlockError(str(exc)) from exc

        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SponsorBlockError("Invalid SponsorBlock response") from exc

        if not isinstance(payload, list):
            raise SponsorBlockError("Invalid SponsorBlock response")
        return [
            segment
            for item in payload
            if (segment := self._segment_from_item(video_id, item)) is not None
        ]

    def _segment_from_item(
        self, video_id: str, item: object
    ) -> SponsorBlockSegment | None:
        if not isinstance(item, dict):
            return None
        raw_segment = item.get("segment")
        if not isinstance(raw_segment, list) or len(raw_segment) != 2:
            return None
        try:
            start_seconds = float(raw_segment[0])
            end_seconds = float(raw_segment[1])
        except (TypeError, ValueError):
            return None
        if end_seconds <= start_seconds:
            return None
        category = item.get("category")
        if not category:
            return None
        return SponsorBlockSegment(
            video_id=video_id,
            category=str(category),
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            action_type=self._optional_string(item.get("actionType")),
            uuid=self._optional_string(item.get("UUID")),
        )

    def _optional_string(self, value: Any) -> str | None:
        return str(value) if value else None
