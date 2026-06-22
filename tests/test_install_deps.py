from __future__ import annotations

import unittest
from unittest import mock

from gtktube import install_deps


class InstallDepsTests(unittest.TestCase):
    def test_pkexec_uses_direct_argv_without_shell(self) -> None:
        args = install_deps.privileged_args(
            "pkexec", ["apt-get", "install", "-y", "python3-gi"]
        )

        self.assertEqual(args, ["pkexec", "apt-get", "install", "-y", "python3-gi"])
        self.assertNotIn("sh", args)
        self.assertNotIn("-c", args)

    def test_rejects_package_names_with_shell_metacharacters(self) -> None:
        with self.assertRaises(ValueError):
            install_deps.apt_install_args(
                ["python3-gi", "libclapper-gtk-0.0-0;touch /tmp/pwned"]
            )

    def test_privileged_install_uses_single_visible_apt_command(self) -> None:
        args = install_deps.privileged_install_args(
            "pkexec",
            ["python3-gi", "libclapper-gtk-0.0-0"],
        )

        self.assertEqual(
            args,
            [
                "pkexec",
                "sh",
                "-c",
                (
                    "apt-get update && apt-get install -y python3-gi "
                    "libclapper-gtk-0.0-0"
                ),
            ],
        )

    def test_run_privileged_apt_runs_one_command(self) -> None:
        completed = mock.Mock(returncode=0)

        with mock.patch(
            "gtktube.install_deps.subprocess.run", return_value=completed
        ) as run:
            result = install_deps.run_privileged_apt("pkexec", ["python3-gi"])

        self.assertIs(result, completed)
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(
            args, ["pkexec", "sh", "-c", "apt-get update && apt-get install -y python3-gi"]
        )


if __name__ == "__main__":
    unittest.main()
