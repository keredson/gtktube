#!/usr/bin/env python3
from __future__ import annotations

import re
import shutil
import shlex
import subprocess
import sys
import threading
from importlib import resources
from collections.abc import Callable

PACKAGE_NAME_RE = re.compile(
    r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9+.-]*)?$"
)
APT_UPDATE_ARGS = ["apt-get", "update"]

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


if Gtk is not None:

    class InstallDepsWindow(Gtk.ApplicationWindow):
        def __init__(self, app: Gtk.Application):
            super().__init__(application=app, title="GTKTube Dependencies")
            self.set_default_size(520, 320)

            self.missing = missing_packages()

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

            self.install_button = Gtk.Button(label="Install")
            self.install_button.connect("clicked", self.on_install_clicked)
            buttons.append(self.install_button)

            close_button = Gtk.Button(label="Close")
            close_button.connect("clicked", lambda *_: self.close())
            buttons.append(close_button)

            self.refresh()

        def refresh(self) -> None:
            if not self.missing:
                self.message.set_text("All required Debian packages are installed.")
                self.details.get_buffer().set_text("")
                self.install_button.set_sensitive(False)
                return

            launcher = privileged_launcher()
            command = apt_command(self.missing, launcher=launcher)
            self.message.set_text(
                "Some Debian packages are missing. Install them to continue launching "
                "GTKTube."
            )
            self.details.get_buffer().set_text(command)
            self.install_button.set_sensitive(launcher is not None)
            if launcher is None:
                self.message.set_text(
                    "Could not find pkexec or gksu. Run the command below in a terminal."
                )

        def on_install_clicked(self, _button: Gtk.Button) -> None:
            if not self.missing:
                return

            launcher = privileged_launcher()
            if launcher is None:
                self.message.set_text(
                    "Could not find pkexec or gksu. Run the command below in a terminal."
                )
                return

            self.install_button.set_sensitive(False)
            self.message.set_text("Installing packages...")
            self.details.get_buffer().set_text(
                f"$ {shlex.join(privileged_install_args(launcher, self.missing))}\n"
            )

            def work() -> None:
                returncode = run_privileged_apt(
                    launcher,
                    self.missing,
                    emit_output=lambda text: GLib.idle_add(self.append_output, text),
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
            if returncode == 0:
                self.missing = missing_packages()
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
            return False


def apt_command(packages: list[str], launcher: str | None = None) -> str:
    if launcher is not None:
        return shlex.join(privileged_install_args(launcher, packages))
    install_args = apt_install_args(packages)
    return f"{shlex.join(APT_UPDATE_ARGS)} && {shlex.join(install_args)}"


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
    command = apt_command(missing)
    message = (
        "GTKTube needs system packages for GTK4, PyGObject, Clapper, and GStreamer.\n\n"
        f"{command}"
    )
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
    return run_privileged_apt(launcher, missing, emit_output=sys.stderr.write).returncode


if Gtk is not None:

    class InstallDepsApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="local.gtktube.InstallDeps")
            self.install_succeeded = False

        def do_activate(self) -> None:
            window = InstallDepsWindow(self)
            window.present()


def main(argv: list[str] | None = None) -> int:
    argv = argv or ["gtktube-deps-installer"]
    if not shutil.which("dpkg-query"):
        print("This helper is intended for Debian/Ubuntu systems.", file=sys.stderr)
        return 2
    missing = missing_packages()
    if Gtk is None:
        if not missing:
            print("All required Debian packages are installed.")
            return 0
        return fallback_gui_install(missing)
    app = InstallDepsApp()
    app.run(argv)
    if app.install_succeeded or not missing_packages():
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
