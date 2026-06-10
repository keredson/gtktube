import unittest

from gtktube.extractors.youtube import is_restricted_video_error


class RestrictedVideoErrorTest(unittest.TestCase):
    def test_detects_members_only_video_error(self) -> None:
        message = (
            "ERROR: [youtube] IooHnhDG2jY: This video is available to this "
            "channel's members on level: MEET IN THE MIDDLE."
        )

        self.assertTrue(is_restricted_video_error(message))

    def test_non_restricted_error_is_not_members_only(self) -> None:
        self.assertFalse(is_restricted_video_error("ERROR: network timeout"))


if __name__ == "__main__":
    unittest.main()
