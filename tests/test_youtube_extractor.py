import unittest

from gtktube.extractors.youtube import YoutubeExtractor, is_restricted_video_error
from gtktube.models import Channel


class RestrictedVideoErrorTest(unittest.TestCase):
    def test_detects_members_only_video_error(self) -> None:
        message = (
            "ERROR: [youtube] IooHnhDG2jY: This video is available to this "
            "channel's members on level: MEET IN THE MIDDLE."
        )

        self.assertTrue(is_restricted_video_error(message))

    def test_non_restricted_error_is_not_members_only(self) -> None:
        self.assertFalse(is_restricted_video_error("ERROR: network timeout"))


class ChannelPaginationTest(unittest.TestCase):
    def test_channel_uploads_uses_flat_playlist_slice(self) -> None:
        class FakeYoutubeDL:
            calls: list[tuple[dict[str, object], str]] = []

            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(self, target: str, download: bool = False) -> dict[str, object]:
                self.calls.append((self.options, target))
                return {
                    "entries": [
                        {
                            "id": "video31",
                            "title": "Video 31",
                            "url": "video31",
                        }
                    ]
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        videos = extractor.channel_uploads(
            Channel(
                id="chan1",
                title="Channel One",
                url="https://www.youtube.com/channel/chan1",
            ),
            limit=30,
            start=31,
        )

        options, target = FakeYoutubeDL.calls[0]
        self.assertEqual(target, "https://www.youtube.com/channel/chan1/videos")
        self.assertEqual(options["extract_flat"], True)
        self.assertEqual(options["playliststart"], 31)
        self.assertEqual(options["playlistend"], 60)
        self.assertEqual(videos[0].id, "video31")

    def test_channel_shorts_uses_flat_playlist_slice(self) -> None:
        class FakeYoutubeDL:
            calls: list[tuple[dict[str, object], str]] = []

            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(self, target: str, download: bool = False) -> dict[str, object]:
                self.calls.append((self.options, target))
                return {
                    "entries": [
                        {
                            "id": "short11",
                            "title": "Short 11",
                            "url": "short11",
                        }
                    ]
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        videos = extractor.channel_shorts(
            Channel(
                id="chan1",
                title="Channel One",
                url="https://www.youtube.com/channel/chan1",
            ),
            limit=10,
            start=11,
        )

        options, target = FakeYoutubeDL.calls[0]
        self.assertEqual(target, "https://www.youtube.com/channel/chan1/shorts")
        self.assertEqual(options["extract_flat"], True)
        self.assertEqual(options["playliststart"], 11)
        self.assertEqual(options["playlistend"], 20)
        self.assertEqual(videos[0].id, "short11")


class CaptionExtractionTest(unittest.TestCase):
    def test_resolve_video_includes_manual_and_auto_captions(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(self, target: str, download: bool = False) -> dict[str, object]:
                return {
                    "id": "video1",
                    "title": "Video 1",
                    "url": "https://stream.example/video.mp4",
                    "subtitles": {
                        "en": [
                            {"ext": "json3", "url": "https://example.invalid/en.json3"},
                            {"ext": "vtt", "url": "https://example.invalid/en.vtt"},
                        ]
                    },
                    "automatic_captions": {
                        "es": [
                            {"ext": "vtt", "url": "https://example.invalid/es-auto.vtt"}
                        ]
                    },
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        playable = extractor.resolve_video("https://www.youtube.com/watch?v=video1")

        captions = playable.captions or []
        self.assertEqual([caption.id for caption in captions], [
            "subtitles:en",
            "automatic_captions:es",
        ])
        self.assertEqual(captions[0].url, "https://example.invalid/en.vtt")
        self.assertFalse(captions[0].automatic)
        self.assertTrue(captions[1].automatic)

    def test_resolve_video_ignores_auto_translated_captions(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(self, target: str, download: bool = False) -> dict[str, object]:
                return {
                    "id": "video1",
                    "title": "Video 1",
                    "url": "https://stream.example/video.mp4",
                    "automatic_captions": {
                        "en": [
                            {
                                "ext": "vtt",
                                "url": "https://www.youtube.com/api/timedtext?v=video1&lang=en&fmt=vtt",
                            }
                        ],
                        "fr": [
                            {
                                "ext": "vtt",
                                "url": "https://www.youtube.com/api/timedtext?v=video1&lang=en&fmt=vtt&tlang=fr",
                            }
                        ],
                        "zh-Hans": [
                            {
                                "ext": "vtt",
                                "url": "https://www.youtube.com/api/timedtext?v=video1&lang=en&fmt=vtt&tlang=zh-Hans",
                            }
                        ],
                    },
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        playable = extractor.resolve_video("https://www.youtube.com/watch?v=video1")

        captions = playable.captions or []
        self.assertEqual([caption.id for caption in captions], ["automatic_captions:en"])
        self.assertEqual(captions[0].label, "en auto")


if __name__ == "__main__":
    unittest.main()
