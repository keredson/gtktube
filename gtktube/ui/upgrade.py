from __future__ import annotations

import os
import subprocess
from concurrent.futures import Executor
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from gtktube.update_check import (
    UpdateInfo,
    restart_command_args,
    upgrade_command,
    upgrade_command_args,
)


class UpgradeController:
    def __init__(
        self,
        parent: Gtk.Window,
        executor: Executor,
        set_status: Callable[[str], None],
        cleanup: Callable[[], None] | None = None,
    ):
        self.parent = parent
        self.executor = executor
        self.set_status = set_status
        self.cleanup = cleanup

    def show_update_dialog(self, update: UpdateInfo) -> None:
        dialog = Gtk.Dialog(
            title="GTKTube update available",
            transient_for=self.parent,
            modal=True,
        )
        dialog.set_default_size(460, -1)
        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        title = Gtk.Label(label="A newer GTKTube release is available", xalign=0)
        title.add_css_class("heading")
        content.append(title)

        message = Gtk.Label(
            label=(
                f"Installed: {update.current_version}\n"
                f"Latest on PyPI: {update.latest_version}"
            ),
            xalign=0,
            wrap=True,
        )
        content.append(message)

        self.append_upgrade_command(content)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.append(footer)
        footer.append(Gtk.Box(hexpand=True))
        not_now_button = Gtk.Button(label="Not now")
        open_pypi_button = Gtk.Button(label="Open PyPI")
        upgrade_button = Gtk.Button(label="Upgrade and restart")
        upgrade_button.add_css_class("suggested-action")
        footer.append(not_now_button)
        footer.append(open_pypi_button)
        footer.append(upgrade_button)

        def open_pypi() -> None:
            Gtk.show_uri(self.parent, update.project_url, Gdk.CURRENT_TIME)
            dialog.destroy()

        not_now_button.connect("clicked", lambda _button: dialog.destroy())
        open_pypi_button.connect("clicked", lambda _button: open_pypi())
        upgrade_button.connect(
            "clicked",
            lambda _button: self.run_upgrade_and_restart(dialog, upgrade_button),
        )
        dialog.present()

    def append_upgrade_command(self, container: Gtk.Box) -> None:
        command_label = Gtk.Label(label=upgrade_command(), xalign=0)
        command_label.add_css_class("dim-label")
        command_label.set_selectable(True)
        container.append(command_label)

    def run_upgrade_and_restart(
        self,
        dialog: Gtk.Dialog | None = None,
        action: Gtk.Button | None = None,
    ) -> None:
        if isinstance(action, Gtk.Button):
            action.set_sensitive(False)
            spinner = Gtk.Spinner()
            spinner.start()
            action.set_child(spinner)
        command = upgrade_command_args()

        def work() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                command,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        future = self.executor.submit(work)

        def finish() -> bool:
            try:
                result = future.result()
            except Exception as exc:
                self.set_status(f"Upgrade failed: {exc}")
                if isinstance(action, Gtk.Button):
                    action.set_sensitive(True)
                    action.set_label("Upgrade and restart")
                return False
            if result.returncode != 0:
                output = (result.stderr or result.stdout or "").strip()
                self.set_status(f"Upgrade failed: {output or result.returncode}")
                if isinstance(action, Gtk.Button):
                    action.set_sensitive(True)
                    action.set_label("Upgrade and restart")
                return False
            if dialog is not None:
                dialog.destroy()
            self.restart_application()
            return False

        future.add_done_callback(lambda _future: GLib.idle_add(finish))

    def restart_application(self) -> None:
        command = restart_command_args()
        if self.cleanup is not None:
            self.cleanup()
        executable = command[0]
        if os.path.sep in executable:
            os.execv(executable, command)
        os.execvp(executable, command)
