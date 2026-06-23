import unittest
from unittest import mock

from gtktube.extractors.youtube import (
    QUALITY_FORMATS,
    RestrictedVideoError,
    YoutubeExtractor,
    clean_ytdlp_error_message,
    is_restricted_video_error,
    is_unavailable_format_error,
    playback_error_message,
    playlist_url,
)
from gtktube.models import Channel


class RestrictedVideoErrorTest(unittest.TestCase):
    def test_cleans_ansi_ytdlp_playback_error(self) -> None:
        message = (
            "\x1b[0;31mERROR:\x1b[0m [youtube] T7F9OK9Jgy8: "
            "Join this channel to get access to members-only content like "
            "this video, and other exclusive perks."
        )

        self.assertEqual(
            clean_ytdlp_error_message(message),
            "Join this channel to get access to members-only content like this "
            "video, and other exclusive perks.",
        )
        self.assertEqual(
            playback_error_message(message),
            "This video is members-only or otherwise restricted.",
        )

    def test_detects_members_only_video_error(self) -> None:
        message = (
            "ERROR: [youtube] IooHnhDG2jY: This video is available to this "
            "channel's members on level: MEET IN THE MIDDLE."
        )

        self.assertTrue(is_restricted_video_error(message))

    def test_non_restricted_error_is_not_members_only(self) -> None:
        self.assertFalse(is_restricted_video_error("ERROR: network timeout"))

    def test_resolve_video_raises_restricted_error_without_cookies(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(
                self, target: str, download: bool = False
            ) -> dict[str, object]:
                raise Exception(
                    "ERROR: [youtube] abc: This video is available to this "
                    "channel's members."
                )

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        with self.assertRaises(RestrictedVideoError):
            extractor.resolve_video("https://www.youtube.com/watch?v=abc")


class UnavailableFormatErrorTest(unittest.TestCase):
    def test_detects_unavailable_format_error(self) -> None:
        self.assertTrue(
            is_unavailable_format_error(
                "ERROR: [youtube] abc: Requested format is not available."
            )
        )

    def test_non_format_error_is_not_unavailable_format(self) -> None:
        self.assertFalse(is_unavailable_format_error("ERROR: network timeout"))


class JavascriptRuntimeTest(unittest.TestCase):
    def test_uses_first_available_javascript_runtime(self) -> None:
        extractor = YoutubeExtractor()

        with mock.patch(
            "gtktube.extractors.youtube.shutil.which",
            side_effect=lambda binary: (
                "/usr/bin/node" if binary == "node" else None
            ),
        ):
            self.assertEqual(
                extractor._available_js_runtimes(),
                {"node": {"path": "/usr/bin/node"}},
            )

    def test_omits_javascript_runtime_when_none_is_available(self) -> None:
        extractor = YoutubeExtractor()

        with mock.patch("gtktube.extractors.youtube.shutil.which", return_value=None):
            self.assertEqual(extractor._available_js_runtimes(), {})


class ChannelPaginationTest(unittest.TestCase):
    def test_subscription_channels_use_browser_cookies(self) -> None:
        class FakeYoutubeDL:
            calls: list[tuple[dict[str, object], str]] = []

            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(
                self,
                target: str,
                download: bool = False,
            ) -> dict[str, object]:
                self.calls.append((self.options, target))
                return {
                    "entries": [
                        {
                            "id": "chan1",
                            "title": "Channel One",
                            "url": "https://www.youtube.com/channel/chan1",
                            "webpage_url": "https://www.youtube.com/channel/chan1",
                        }
                    ]
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        channels = extractor.subscription_channels("firefox", limit=25)

        options, target = FakeYoutubeDL.calls[0]
        self.assertEqual(target, "https://www.youtube.com/feed/channels")
        self.assertEqual(options["cookiesfrombrowser"], ("firefox",))
        self.assertEqual(options["playlistend"], 25)
        self.assertEqual(channels[0].id, "chan1")
        self.assertEqual(channels[0].title, "Channel One")

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

    def test_channel_shorts_treats_none_extraction_as_empty(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(
                self, target: str, download: bool = False
            ) -> dict[str, object] | None:
                return None

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        videos = extractor.channel_shorts(
            Channel(
                id="chan1",
                title="Channel One",
                url="https://www.youtube.com/channel/chan1",
            )
        )

        self.assertEqual(videos, [])

    def test_resolve_playlist_treats_none_extraction_as_empty(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(
                self, target: str, download: bool = False
            ) -> dict[str, object] | None:
                return None

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        result = extractor.resolve_playlist("https://www.youtube.com/playlist?list=PL123")

        self.assertEqual(result, {"title": "Playlist", "videos": [], "start_index": 0})

    def test_playlist_url_uses_list_param_from_watch_url(self) -> None:
        self.assertEqual(
            playlist_url(
                "https://www.youtube.com/watch?v=uJapcLoN4UM"
                "&list=PLlCrV9TCfzMZzFYxcpM1jZMedw7FSinBc"
            ),
            "https://www.youtube.com/playlist?list=PLlCrV9TCfzMZzFYxcpM1jZMedw7FSinBc",
        )

    def test_resolve_playlist_extracts_canonical_playlist_url(self) -> None:
        targets: list[str] = []

        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(
                self, target: str, download: bool = False
            ) -> dict[str, object]:
                targets.append(target)
                return {"title": "Playlist", "entries": []}

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        extractor.resolve_playlist(
            "https://www.youtube.com/watch?v=uJapcLoN4UM"
            "&list=PLlCrV9TCfzMZzFYxcpM1jZMedw7FSinBc"
        )

        self.assertEqual(
            targets,
            [
                "https://www.youtube.com/playlist?list=PLlCrV9TCfzMZzFYxcpM1jZMedw7FSinBc"
            ],
        )

    def test_resolve_playlist_starts_at_video_from_watch_url(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(
                self, target: str, download: bool = False
            ) -> dict[str, object]:
                return {
                    "title": "Playlist",
                    "entries": [
                        {
                            "id": "first",
                            "title": "First",
                            "url": "https://www.youtube.com/watch?v=first",
                        },
                        {
                            "id": "uJapcLoN4UM",
                            "title": "Requested",
                            "url": "https://www.youtube.com/watch?v=uJapcLoN4UM",
                        },
                    ],
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        result = extractor.resolve_playlist(
            "https://www.youtube.com/watch?v=uJapcLoN4UM"
            "&list=PLlCrV9TCfzMZzFYxcpM1jZMedw7FSinBc"
        )

        self.assertEqual(result["start_index"], 1)

    def test_channel_playlists_are_tagged_as_playlists(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(self, target: str, download: bool = False) -> dict[str, object]:
                return {
                    "entries": [
                        {
                            "id": "playlist1",
                            "title": "Playlist 1",
                            "url": "https://www.youtube.com/playlist?list=playlist1",
                        }
                    ]
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        playlists = extractor.channel_playlists(
            Channel(
                id="chan1",
                title="Channel One",
                url="https://www.youtube.com/channel/chan1",
            ),
        )

        self.assertEqual(playlists[0].kind, "playlist")

    def test_channel_search_uses_channel_search_url(self) -> None:
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
                            "id": "result1",
                            "title": "Search Result",
                            "url": "result1",
                        }
                    ]
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        videos = extractor.search_channel_videos(
            Channel(
                id="chan1",
                title="Channel One",
                url="https://www.youtube.com/@channelone",
            ),
            "space test",
            limit=25,
        )

        options, target = FakeYoutubeDL.calls[0]
        self.assertEqual(
            target,
            "https://www.youtube.com/@channelone/search?query=space+test",
        )
        self.assertEqual(options["extract_flat"], True)
        self.assertEqual(options["playlistend"], 25)
        self.assertEqual(videos[0].id, "result1")
        self.assertEqual(videos[0].channel_id, "chan1")

    def test_playlist_thumbnail_uses_first_playlist_video_thumbnail(self) -> None:
        class FakeYoutubeDL:
            calls: list[tuple[dict[str, object], str]] = []

            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(
                self, target: str, download: bool = False
            ) -> dict[str, object]:
                self.calls.append((self.options, target))
                return {
                    "entries": [
                        {
                            "id": "video1",
                            "title": "First Playlist Video",
                            "thumbnail": "https://example.invalid/thumb.jpg",
                            "url": "video1",
                        }
                    ]
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        thumbnail_url = extractor.playlist_thumbnail(
            "https://www.youtube.com/playlist?list=PL123"
        )

        self.assertEqual(thumbnail_url, "https://example.invalid/thumb.jpg")
        self.assertEqual(
            FakeYoutubeDL.calls[0][1],
            "https://www.youtube.com/playlist?list=PL123",
        )


class CaptionExtractionTest(unittest.TestCase):
    def test_resolve_video_reports_available_stream_and_fetch_qualities(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(
                self,
                target: str,
                download: bool = False,
            ) -> dict[str, object]:
                return {
                    "id": "video1",
                    "title": "Video 1",
                    "url": "https://stream.example/video.mp4",
                    "formats": [
                        {
                            "height": 360,
                            "vcodec": "avc1",
                            "acodec": "mp4a",
                        },
                        {
                            "height": 720,
                            "vcodec": "avc1",
                            "acodec": "mp4a",
                        },
                        {
                            "height": 1080,
                            "vcodec": "avc1",
                            "acodec": "none",
                        },
                        {
                            "height": None,
                            "vcodec": "none",
                            "acodec": "mp4a",
                        },
                    ],
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        playable = extractor.resolve_video(
            "https://www.youtube.com/watch?v=video1",
            quality="1080p",
        )

        self.assertEqual(
            playable.available_stream_qualities,
            ["360p", "720p", "1080p"],
        )
        self.assertEqual(
            playable.available_fetch_qualities,
            ["360p", "720p", "1080p"],
        )

    def test_resolve_video_falls_back_when_quality_format_is_unavailable(self) -> None:
        class FakeYoutubeDL:
            calls: list[dict[str, object]] = []

            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(self, target: str, download: bool = False) -> dict[str, object]:
                self.calls.append(self.options)
                if len(self.calls) == 1:
                    raise Exception(
                        "ERROR: [youtube] video1: Requested format is not available."
                    )
                return {
                    "id": "video1",
                    "title": "Video 1",
                    "url": "https://stream.example/video.mp4",
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        playable = extractor.resolve_video(
            "https://www.youtube.com/watch?v=video1",
            quality="1080p",
        )

        self.assertEqual(playable.video.id, "video1")
        self.assertEqual(FakeYoutubeDL.calls[0]["format"], QUALITY_FORMATS["1080p"])
        self.assertNotIn("format", FakeYoutubeDL.calls[1])

    def test_resolve_video_can_drop_cookies_when_formats_are_unavailable(self) -> None:
        class FakeYoutubeDL:
            calls: list[dict[str, object]] = []

            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(self, target: str, download: bool = False) -> dict[str, object]:
                self.calls.append(self.options)
                if "cookiesfrombrowser" in self.options:
                    raise Exception(
                        "ERROR: [youtube] video1: Requested format is not available."
                    )
                return {
                    "id": "video1",
                    "title": "Video 1",
                    "url": "https://stream.example/video.mp4",
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        playable = extractor.resolve_video(
            "https://www.youtube.com/watch?v=video1",
            quality="1080p",
            cookies_mode="always",
            cookies_browser="chrome",
        )

        self.assertEqual(playable.video.id, "video1")
        self.assertIn("cookiesfrombrowser", FakeYoutubeDL.calls[0])
        self.assertIn("cookiesfrombrowser", FakeYoutubeDL.calls[1])
        self.assertNotIn("cookiesfrombrowser", FakeYoutubeDL.calls[2])
        self.assertEqual(FakeYoutubeDL.calls[2]["format"], QUALITY_FORMATS["1080p"])

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
        self.assertEqual([caption.label for caption in captions], [
            "English",
            "Spanish (auto)",
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
        self.assertEqual(captions[0].label, "English (auto)")

    def test_resolve_video_includes_chapters(self) -> None:
        class FakeYoutubeDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> "FakeYoutubeDL":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(
                self, target: str, download: bool = False
            ) -> dict[str, object]:
                return {
                    "id": "video1",
                    "title": "Video 1",
                    "url": "https://stream.example/video.mp4",
                    "availability": "subscriber_only",
                    "chapters": [
                        {
                            "title": "Intro",
                            "start_time": 0,
                            "end_time": 12.5,
                        },
                        {
                            "title": "Demo",
                            "start_time": 12.5,
                            "end_time": 60,
                        },
                    ],
                }

        extractor = YoutubeExtractor()
        extractor._ydl_cls = FakeYoutubeDL

        playable = extractor.resolve_video("https://www.youtube.com/watch?v=video1")

        chapters = playable.chapters or []
        self.assertEqual([chapter.title for chapter in chapters], ["Intro", "Demo"])
        self.assertEqual(chapters[0].video_id, "video1")
        self.assertEqual(chapters[0].start_seconds, 0)
        self.assertEqual(chapters[0].end_seconds, 12.5)
        self.assertEqual(chapters[1].position, 1)
        self.assertEqual(playable.video.availability, "subscriber_only")


if __name__ == "__main__":
    unittest.main()
