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
            install_deps.apt_install_args(["python3-gi", "libmpv2;touch /tmp/pwned"])

    def test_run_privileged_apt_runs_update_then_install_as_argv(self) -> None:
        completed = mock.Mock(returncode=0)

        with mock.patch(
            "gtktube.install_deps.subprocess.run", return_value=completed
        ) as run:
            result = install_deps.run_privileged_apt("pkexec", ["python3-gi"])

        self.assertIs(result, completed)
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(["pkexec", "apt-get", "update"], check=False),
                mock.call(
                    ["pkexec", "apt-get", "install", "-y", "python3-gi"],
                    check=False,
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
