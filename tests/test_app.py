from __future__ import annotations

import unittest

from gtktube.app import parse_startup_options


class StartupOptionTests(unittest.TestCase):
    def test_removes_gtktube_flags_from_gtk_argv(self) -> None:
        options = parse_startup_options(
            [
                "gtktube",
                "--show-upgrade",
                "--show-deps-installer",
                "--gapplication-service",
            ]
        )

        self.assertTrue(options.show_upgrade)
        self.assertTrue(options.show_deps_installer)
        self.assertEqual(options.gtk_argv, ["gtktube", "--gapplication-service"])


if __name__ == "__main__":
    unittest.main()
