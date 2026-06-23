from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
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
                ["python3-gi", "libmpv2;touch /tmp/pwned"]
            )

    def test_privileged_install_uses_single_visible_apt_command(self) -> None:
        args = install_deps.privileged_install_args(
            "pkexec",
            ["python3-gi", "libmpv2"],
        )

        self.assertEqual(
            args,
            [
                "pkexec",
                "sh",
                "-c",
                (
                    "apt-get update && apt-get install -y python3-gi "
                    "libmpv2"
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

    def test_package_plan_splits_installable_and_unavailable_packages(self) -> None:
        plan = install_deps.package_plan(
            ["python3-gi", "libmpv2", "nodejs"],
            available=lambda package: package != "libmpv2",
        )

        self.assertEqual(plan.installable, ["python3-gi", "nodejs"])
        self.assertEqual(plan.unavailable, ["libmpv2"])

    def test_fallback_does_not_run_apt_when_required_packages_are_unavailable(
        self,
    ) -> None:
        with (
            mock.patch(
                "gtktube.install_deps.package_plan",
                return_value=install_deps.PackagePlan(
                    installable=[],
                    unavailable=["libmpv2"],
                ),
            ),
            mock.patch("gtktube.install_deps.run_privileged_apt") as run_apt,
            redirect_stderr(io.StringIO()),
        ):
            result = install_deps.fallback_gui_install(["libmpv2"])

        self.assertEqual(result, 1)
        run_apt.assert_not_called()

    def test_fallback_reports_failure_after_partial_install_when_packages_unavailable(
        self,
    ) -> None:
        with (
            mock.patch(
                "gtktube.install_deps.package_plan",
                return_value=install_deps.PackagePlan(
                    installable=["python3-gi"],
                    unavailable=["libmpv2"],
                ),
            ),
            mock.patch(
                "gtktube.install_deps.shutil.which",
                side_effect=lambda command: "pkexec" if command == "pkexec" else None,
            ),
            mock.patch(
                "gtktube.install_deps.run_privileged_apt",
                return_value=mock.Mock(returncode=0),
            ) as run_apt,
            redirect_stderr(io.StringIO()),
        ):
            result = install_deps.fallback_gui_install(
                ["python3-gi", "libmpv2"]
            )

        self.assertEqual(result, 1)
        run_apt.assert_called_once()
        self.assertEqual(run_apt.call_args.args, ("pkexec", ["python3-gi"]))


if __name__ == "__main__":
    unittest.main()
