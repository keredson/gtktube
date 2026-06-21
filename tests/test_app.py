from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from gtktube.app import (
    DESKTOP_FILENAME,
    OLD_DESKTOP_FILENAME,
    desktop_exec_for_launch,
    install_desktop_entry,
    launched_as_installed_command,
    main,
    parse_startup_options,
)
from gtktube.db.migrations import UnsupportedDatabaseSchema


class StartupOptionTests(unittest.TestCase):
    def test_removes_gtktube_flags_from_gtk_argv(self) -> None:
        options = parse_startup_options(
            [
                "gtktube",
                "--show-upgrade",
                "--show-deps-installer",
                "--install-desktop",
                "-v",
                "--gapplication-service",
            ]
        )

        self.assertTrue(options.show_upgrade)
        self.assertTrue(options.show_deps_installer)
        self.assertTrue(options.install_desktop)
        self.assertTrue(options.verbose)
        self.assertEqual(options.gtk_argv, ["gtktube", "--gapplication-service"])

    def test_parses_verbose_long_option(self) -> None:
        options = parse_startup_options(["gtktube", "--verbose"])

        self.assertTrue(options.verbose)
        self.assertEqual(options.gtk_argv, ["gtktube"])

    def test_parses_database_path_option(self) -> None:
        options = parse_startup_options(["gtktube", "--db", "~/tmp/gtktube.sqlite3"])

        self.assertEqual(options.database_path, Path("~/tmp/gtktube.sqlite3").expanduser())
        self.assertEqual(options.gtk_argv, ["gtktube"])

    def test_parses_database_path_equals_option(self) -> None:
        options = parse_startup_options(["gtktube", "--db=/tmp/gtktube.sqlite3"])

        self.assertEqual(options.database_path, Path("/tmp/gtktube.sqlite3"))
        self.assertEqual(options.gtk_argv, ["gtktube"])

    def test_database_path_option_requires_path(self) -> None:
        with self.assertRaises(ValueError):
            parse_startup_options(["gtktube", "--db"])

    def test_detects_installed_command_launch(self) -> None:
        self.assertTrue(launched_as_installed_command("/home/derek/.local/bin/gtktube"))
        self.assertFalse(launched_as_installed_command("/usr/bin/python3"))

    def test_desktop_exec_for_python_module_launch(self) -> None:
        self.assertTrue(desktop_exec_for_launch(["python", "-m", "gtktube"]).endswith(" -m gtktube"))

    def test_installs_desktop_entry_under_user_data_home(self) -> None:
        previous_data_home = os.environ.get("XDG_DATA_HOME")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["XDG_DATA_HOME"] = tmpdir
                old_desktop_file = Path(tmpdir) / "applications" / OLD_DESKTOP_FILENAME
                old_desktop_file.parent.mkdir(parents=True)
                old_desktop_file.write_text("old entry", encoding="utf-8")

                install_desktop_entry("/home/derek/.local/bin/gtktube")

                desktop_file = Path(tmpdir) / "applications" / DESKTOP_FILENAME
                icon_file = (
                    Path(tmpdir)
                    / "icons"
                    / "hicolor"
                    / "256x256"
                    / "apps"
                    / "gtktube.png"
                )
                self.assertTrue(desktop_file.exists())
                self.assertFalse(old_desktop_file.exists())
                self.assertTrue(icon_file.exists())
                self.assertIn(
                    "Exec=/home/derek/.local/bin/gtktube",
                    desktop_file.read_text(encoding="utf-8"),
                )
        finally:
            if previous_data_home is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = previous_data_home

    def test_newer_database_schema_launches_upgrade_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database_path = Path(tmpdir) / "gtktube.sqlite3"

            with (
                mock.patch("gtktube.app.install_desktop_entry"),
                mock.patch("gtktube.app.dependency_checks_pass", return_value=True),
                mock.patch(
                    "gtktube.app.migrate",
                    side_effect=UnsupportedDatabaseSchema(current=9, supported=8),
                ),
                mock.patch("gtktube.app.run_upgrade_tool", return_value=17) as upgrade,
            ):
                result = main(["gtktube", "--db", str(database_path)])

            self.assertEqual(result, 17)
            upgrade.assert_called_once()
            reason, gtk_argv = upgrade.call_args.args
            self.assertIn("schema 9", reason)
            self.assertIn("supports schema 8", reason)
            self.assertEqual(gtk_argv, ["gtktube"])


if __name__ == "__main__":
    unittest.main()
