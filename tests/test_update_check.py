from __future__ import annotations

import unittest

from gtktube.update_check import is_newer_version, upgrade_command


class UpdateCheckTests(unittest.TestCase):
    def test_detects_newer_pypi_version(self) -> None:
        self.assertTrue(is_newer_version("0.1.0", "0.1.1"))

    def test_ignores_same_or_older_versions(self) -> None:
        self.assertFalse(is_newer_version("0.1.0", "0.1.0"))
        self.assertFalse(is_newer_version("0.1.0", "0.0.9"))

    def test_ignores_unknown_local_version(self) -> None:
        self.assertFalse(is_newer_version("0+unknown", "99.0.0"))

    def test_ignores_invalid_versions(self) -> None:
        self.assertFalse(is_newer_version("0.1.0", "not-a-version"))

    def test_uses_pipx_upgrade_for_pipx_environment(self) -> None:
        command = upgrade_command(
            prefix="/home/derek/.local/share/pipx/venvs/gtktube",
            executable="/home/derek/.local/share/pipx/venvs/gtktube/bin/python",
        )
        self.assertEqual(command, "pipx upgrade gtktube")

    def test_uses_pip_upgrade_outside_pipx_environment(self) -> None:
        command = upgrade_command(
            prefix="/home/derek/projects/gtktube/.venv",
            executable="/home/derek/projects/gtktube/.venv/bin/python",
        )
        self.assertEqual(command, "python3 -m pip install --upgrade gtktube")


if __name__ == "__main__":
    unittest.main()
