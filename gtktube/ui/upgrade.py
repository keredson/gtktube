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

        dialog.add_button("Not now", Gtk.ResponseType.CANCEL)
        dialog.add_button("Open PyPI", Gtk.ResponseType.HELP)
        dialog.add_button("Upgrade and restart", Gtk.ResponseType.ACCEPT)

        def response(_dialog: Gtk.Dialog, response_id: int) -> None:
            if response_id == Gtk.ResponseType.ACCEPT:
                self.run_upgrade_and_restart(dialog)
                return
            elif response_id == Gtk.ResponseType.HELP:
                Gtk.show_uri(self.parent, update.project_url, Gdk.CURRENT_TIME)
            dialog.destroy()

        dialog.connect("response", response)
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
        if dialog is not None and action is None:
            widget = dialog.get_widget_for_response(Gtk.ResponseType.ACCEPT)
            if isinstance(widget, Gtk.Button):
                action = widget
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
