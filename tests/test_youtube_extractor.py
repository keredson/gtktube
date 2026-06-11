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
        self.assertEqual(options["extract_flat"], "in_playlist")
        self.assertEqual(options["playliststart"], 31)
        self.assertEqual(options["playlistend"], 60)
        self.assertEqual(videos[0].id, "video31")


if __name__ == "__main__":
    unittest.main()
