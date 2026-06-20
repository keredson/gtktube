#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
from importlib import resources

try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk  # type: ignore  # noqa: E402
except (ImportError, ModuleNotFoundError, ValueError):
    Gtk = None  # type: ignore[assignment]


def apt_packages() -> list[str]:
    text = resources.files("gtktube").joinpath("assets/apt-packages.txt").read_text(
        encoding="utf-8"
    )
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


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

            command = apt_command(self.missing)
            self.message.set_text(
                "Some Debian packages are missing. The installer will use pkexec if "
                "available, otherwise gksu. If neither is installed, run this "
                "command manually:"
            )
            self.details.get_buffer().set_text(command)
            self.install_button.set_sensitive(True)

        def on_install_clicked(self, _button: Gtk.Button) -> None:
            if not self.missing:
                return

            command = apt_command(self.missing)
            launcher = privileged_launcher()
            if launcher is None:
                self.message.set_text(
                    "Could not find pkexec or gksu. Run the command below in a terminal."
                )
                return

            result = subprocess.run(privileged_args(launcher, command), check=False)
            if result.returncode == 0:
                self.missing = missing_packages()
                self.refresh()
            else:
                self.message.set_text(
                    "Installation did not complete. The command is still shown below."
                )


def apt_command(packages: list[str]) -> str:
    quoted = " ".join(packages)
    return f"apt-get update && apt-get install -y {quoted}"


def privileged_launcher() -> str | None:
    if shutil.which("pkexec"):
        return "pkexec"
    if shutil.which("gksu"):
        return "gksu"
    return None


def privileged_args(launcher: str, command: str) -> list[str]:
    if launcher == "gksu":
        return ["gksu", command]
    return ["pkexec", "sh", "-c", command]


def fallback_gui_install(missing: list[str]) -> int:
    command = apt_command(missing)
    message = (
        "GTKTube needs system packages for GTK4, PyGObject, and libmpv.\n\n"
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
        print("Could not find pkexec or gksu. Run the command above manually.", file=sys.stderr)
        return 1
    return subprocess.run(privileged_args(launcher, command), check=False).returncode


if Gtk is not None:

    class InstallDepsApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="local.gtktube.InstallDeps")

        def do_activate(self) -> None:
            window = InstallDepsWindow(self)
            window.present()


def main() -> int:
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
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
