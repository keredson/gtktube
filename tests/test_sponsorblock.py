import unittest

from gtktube.sponsorblock import SponsorBlockClient


class SponsorBlockClientTest(unittest.TestCase):
    def test_segment_parser_accepts_valid_segment(self) -> None:
        client = SponsorBlockClient()

        segment = client._segment_from_item(
            "video1",
            {
                "category": "sponsor",
                "actionType": "skip",
                "segment": [1.5, 12.25],
                "UUID": "abc",
            },
        )

        self.assertIsNotNone(segment)
        assert segment is not None
        self.assertEqual(segment.video_id, "video1")
        self.assertEqual(segment.category, "sponsor")
        self.assertEqual(segment.start_seconds, 1.5)
        self.assertEqual(segment.end_seconds, 12.25)
        self.assertEqual(segment.uuid, "abc")

    def test_segment_parser_rejects_invalid_ranges(self) -> None:
        client = SponsorBlockClient()

        self.assertIsNone(
            client._segment_from_item(
                "video1",
                {
                    "category": "sponsor",
                    "segment": [12, 12],
                },
            )
        )


if __name__ == "__main__":
    unittest.main()
