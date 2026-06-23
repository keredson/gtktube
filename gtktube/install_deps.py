#!/usr/bin/env python3
from __future__ import annotations

import re
import shutil
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from importlib import resources
from collections.abc import Callable

PACKAGE_NAME_RE = re.compile(
    r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9+.-]*)?$"
)
APT_UPDATE_ARGS = ["apt-get", "update"]
TEST_EMPTY_INSTALL_ARGS = ["sleep", "10"]

try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402
except (ImportError, ModuleNotFoundError, ValueError):
    GLib = None  # type: ignore[assignment]
    Gtk = None  # type: ignore[assignment]


def apt_packages() -> list[str]:
    text = resources.files("gtktube").joinpath("assets/apt-packages.txt").read_text(
        encoding="utf-8"
    )
    return validate_package_names(
        [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    )


def validate_package_names(packages: list[str]) -> list[str]:
    invalid = [
        package for package in packages if not PACKAGE_NAME_RE.fullmatch(package)
    ]
    if invalid:
        raise ValueError(f"Invalid apt package name: {invalid[0]}")
    return packages


PACKAGES = apt_packages()


@dataclass(frozen=True)
class PackagePlan:
    installable: list[str]
    unavailable: list[str]


def missing_packages() -> list[str]:
    missing: list[str] = []
    for package in PACKAGES:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", package],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0 or "install ok installed" not in result.stdout:
            missing.append(package)
    return missing


def package_available(package: str) -> bool:
    result = subprocess.run(
        ["apt-cache", "show", package],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def package_plan(
    packages: list[str],
    available: Callable[[str], bool] = package_available,
) -> PackagePlan:
    installable: list[str] = []
    unavailable: list[str] = []
    for package in validate_package_names(packages):
        if available(package):
            installable.append(package)
        else:
            unavailable.append(package)
    return PackagePlan(installable=installable, unavailable=unavailable)


def unavailable_message(unavailable: list[str]) -> str:
    packages = "\n".join(f"  {package}" for package in unavailable)
    return (
        "These required packages are not available from your configured apt "
        "repositories:\n\n"
        f"{packages}\n\n"
        "Add a repository that provides these packages or use a distribution "
        "release that includes them, then start GTKTube again."
    )


if Gtk is not None:

    class InstallDepsWindow(Gtk.ApplicationWindow):
        def __init__(self, app: Gtk.Application, *, test_empty_install: bool = False):
            super().__init__(application=app, title="GTKTube Dependencies")
            self.set_default_size(520, 320)
            self.test_empty_install = test_empty_install

            self.missing = missing_packages()
            self.plan = package_plan(self.missing)

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            root.set_margin_top(16)
            root.set_margin_bottom(16)
            root.set_margin_start(16)
            root.set_margin_end(16)
            self.set_child(root)

            heading = Gtk.Label()
            heading.set_xalign(0)
            heading.set_markup("<b>GTKTube system dependencies</b>")
            root.append(heading)

            self.message = Gtk.Label(wrap=True)
            self.message.set_xalign(0)
            root.append(self.message)

            scroller = Gtk.ScrolledWindow(vexpand=True)
            self.details = Gtk.TextView(editable=False, monospace=True)
            scroller.set_child(self.details)
            root.append(scroller)

            buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            buttons.set_halign(Gtk.Align.END)
            root.append(buttons)

            self.cancel_button = Gtk.Button(label="Cancel")
            self.cancel_button.connect("clicked", lambda *_: self.cancel_install())
            buttons.append(self.cancel_button)

            self.install_button = Gtk.Button(label="Install")
            self.install_button.add_css_class("suggested-action")
            self.install_button.connect("clicked", self.on_install_clicked)
            buttons.append(self.install_button)
            self.installing = False
            self.connect("close-request", self.on_close_request)

            self.refresh()

        def on_close_request(self, _window: Gtk.Window) -> bool:
            if self.installing:
                return True
            self.cancel_install()
            return True

        def cancel_install(self) -> None:
            app = self.get_application()
            if app is not None:
                app.install_cancelled = True
                app.quit()
            else:
                self.close()

        def refresh(self) -> None:
            self.plan = package_plan(self.missing)
            if not self.missing:
                self.message.set_text("All required Debian packages are installed.")
                if self.test_empty_install:
                    self.details.get_buffer().set_text(
                        shlex.join(TEST_EMPTY_INSTALL_ARGS)
                    )
                    self.install_button.set_sensitive(True)
                else:
                    self.details.get_buffer().set_text("")
                    self.install_button.set_sensitive(False)
                self.cancel_button.set_sensitive(True)
                return

            if not self.plan.installable:
                self.message.set_text(
                    "Some required Debian packages are missing, but apt cannot "
                    "find them in your configured repositories."
                )
                self.details.get_buffer().set_text(
                    unavailable_message(self.plan.unavailable)
                )
                self.install_button.set_sensitive(False)
                self.cancel_button.set_sensitive(True)
                return

            launcher = privileged_launcher()
            command = apt_command_display(self.plan.installable, launcher=launcher)
            details = command
            if self.plan.unavailable:
                details += "\n\n" + unavailable_message(self.plan.unavailable)
                self.message.set_text(
                    "Some missing Debian packages can be installed, but other "
                    "required packages are unavailable from apt."
                )
            else:
                self.message.set_text(
                    "Some Debian packages are missing. Install them to continue "
                    "launching GTKTube."
                )
            self.details.get_buffer().set_text(details)
            self.install_button.set_sensitive(launcher is not None)
            if launcher is None:
                self.message.set_text(
                    "Could not find pkexec or gksu. Run the command below in a terminal."
                )
            self.cancel_button.set_sensitive(True)

        def on_install_clicked(self, _button: Gtk.Button) -> None:
            if self.installing:
                return
            if not self.missing:
                if self.test_empty_install:
                    self.run_test_empty_install()
                return
            if not self.plan.installable:
                return

            launcher = privileged_launcher()
            if launcher is None:
                self.message.set_text(
                    "Could not find pkexec or gksu. Run the command below in a terminal."
                )
                return

            self.installing = True
            self.install_button.set_sensitive(False)
            self.cancel_button.set_sensitive(False)
            self.message.set_text("Installing packages...")
            self.details.get_buffer().set_text(
                "$ "
                f"{apt_command_display(self.plan.installable, launcher=launcher)}"
                "\n"
            )
            installable = list(self.plan.installable)

            def work() -> None:
                returncode = run_privileged_apt(
                    launcher,
                    installable,
                    emit_output=lambda text: GLib.idle_add(self.append_output, text),
                ).returncode
                GLib.idle_add(self.install_finished, returncode)

            threading.Thread(target=work, daemon=True).start()

        def run_test_empty_install(self) -> None:
            self.installing = True
            self.install_button.set_sensitive(False)
            self.cancel_button.set_sensitive(False)
            self.message.set_text("Installing packages...")
            self.details.get_buffer().set_text(
                f"$ {shlex.join(TEST_EMPTY_INSTALL_ARGS)}\n"
            )

            def work() -> None:
                returncode = subprocess.run(
                    TEST_EMPTY_INSTALL_ARGS,
                    check=False,
                ).returncode
                GLib.idle_add(self.install_finished, returncode)

            threading.Thread(target=work, daemon=True).start()

        def append_output(self, text: str) -> bool:
            buffer = self.details.get_buffer()
            buffer.insert(buffer.get_end_iter(), text)
            mark = buffer.create_mark(None, buffer.get_end_iter(), False)
            self.details.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
            return False

        def install_finished(self, returncode: int) -> bool:
            self.installing = False
            if returncode == 0:
                self.missing = missing_packages()
                self.plan = package_plan(self.missing)
                if not self.missing:
                    self.message.set_text("Dependencies installed. Starting GTKTube...")
                    app = self.get_application()
                    if app is not None:
                        app.install_succeeded = True
                        GLib.timeout_add(500, app.quit)
                    return False
                self.refresh()
                return False

            self.message.set_text(
                "Installation did not complete. The command output is shown below."
            )
            self.install_button.set_sensitive(True)
            self.cancel_button.set_sensitive(True)
            return False


def apt_command(packages: list[str], launcher: str | None = None) -> str:
    if launcher is not None:
        return shlex.join(privileged_install_args(launcher, packages))
    install_args = apt_install_args(packages)
    return f"{shlex.join(APT_UPDATE_ARGS)} && {shlex.join(install_args)}"


def apt_command_display(packages: list[str], launcher: str | None = None) -> str:
    command = (
        f"{shlex.join(APT_UPDATE_ARGS)} && \\\n"
        f"{shlex.join(apt_install_args(packages))}"
    )
    if launcher is None:
        return command
    return f"{launcher} sh -c {shlex.quote(command)}"


def apt_install_args(packages: list[str]) -> list[str]:
    return ["apt-get", "install", "-y", *validate_package_names(packages)]


def privileged_launcher() -> str | None:
    if shutil.which("pkexec"):
        return "pkexec"
    if shutil.which("gksu"):
        return "gksu"
    return None


def privileged_args(launcher: str, args: list[str]) -> list[str]:
    if launcher == "gksu":
        return ["gksu", shlex.join(args)]
    return ["pkexec", *args]


def privileged_install_args(launcher: str, packages: list[str]) -> list[str]:
    return privileged_args(launcher, ["sh", "-c", apt_command(packages)])


def run_command(
    args: list[str],
    emit_output: Callable[[str], object] | None = None,
) -> subprocess.CompletedProcess:
    if emit_output is None:
        return subprocess.run(args, check=False)

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        emit_output(line)
    returncode = process.wait()
    return subprocess.CompletedProcess(args, returncode)


def run_privileged_apt(
    launcher: str,
    packages: list[str],
    emit_output: Callable[[str], object] | None = None,
) -> subprocess.CompletedProcess:
    return run_command(
        privileged_install_args(launcher, packages),
        emit_output=emit_output,
    )


def fallback_gui_install(missing: list[str]) -> int:
    plan = package_plan(missing)
    command = apt_command_display(plan.installable) if plan.installable else ""
    message = (
        "GTKTube needs system packages for GTK4, PyGObject, and libmpv.\n\n"
        f"{command}"
    )
    if plan.unavailable:
        message += "\n\n" + unavailable_message(plan.unavailable)
    if not plan.installable:
        print(message, file=sys.stderr)
        return 1
    if shutil.which("zenity"):
        result = subprocess.run(
            [
                "zenity",
                "--question",
                "--title=GTKTube Dependencies",
                f"--text={message}\n\nInstall now?",
                "--width=560",
            ],
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    elif shutil.which("kdialog"):
        result = subprocess.run(
            ["kdialog", "--yesno", f"{message}\n\nInstall now?"],
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    else:
        print(message, file=sys.stderr)

    launcher = privileged_launcher()
    if launcher is None:
        print(
            "Could not find pkexec or gksu. Run the command above manually.",
            file=sys.stderr,
        )
        return 1
    result = run_privileged_apt(
        launcher, plan.installable, emit_output=sys.stderr.write
    )
    if plan.unavailable and result.returncode == 0:
        return 1
    return result.returncode


if Gtk is not None:

    class InstallDepsApp(Gtk.Application):
        def __init__(self, *, test_empty_install: bool = False):
            super().__init__(application_id="local.gtktube.InstallDeps")
            self.install_succeeded = False
            self.install_cancelled = False
            self.test_empty_install = test_empty_install

        def do_activate(self) -> None:
            window = InstallDepsWindow(
                self,
                test_empty_install=self.test_empty_install,
            )
            window.present()


def main(argv: list[str] | None = None) -> int:
    argv = argv or ["gtktube-deps-installer"]
    test_empty_install = "--test-empty-install" in argv
    gtk_argv = [arg for arg in argv if arg != "--test-empty-install"]
    if not shutil.which("dpkg-query"):
        print("This helper is intended for Debian/Ubuntu systems.", file=sys.stderr)
        return 2
    missing = missing_packages()
    if Gtk is None:
        if not missing:
            print("All required Debian packages are installed.")
            return 0
        return fallback_gui_install(missing)
    app = InstallDepsApp(test_empty_install=test_empty_install)
    app.run(gtk_argv)
    if app.install_succeeded:
        return 0
    if app.install_cancelled:
        return 1
    if not missing_packages():
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
