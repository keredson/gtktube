from __future__ import annotations

import os
import locale
import shutil
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path

from .db.connection import connect
from .db.migrations import UnsupportedDatabaseSchema, migrate
from .db.repositories import LibraryRepository
from .extractors.youtube import YoutubeExtractor
from .paths import AppPaths
from .services.library import LibraryService


DESKTOP_ID = "local.gtktube.GTKTube"
DESKTOP_FILENAME = f"{DESKTOP_ID}.desktop"
OLD_DESKTOP_FILENAME = "gtktube.desktop"

PYGOBJECT_HELP = """\
GTKTube needs PyGObject from your system packages.

On Debian/Ubuntu:
  ./scripts/install-apt-deps.sh

If you run GTKTube from a virtualenv, recreate it with access to system
site-packages so it can see python3-gi:
  rm -rf .venv
  python3 -m venv --system-site-packages .venv
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m gtktube
"""

MPV_HELP = """\
GTKTube needs libmpv and the Python mpv bindings.

On Debian/Ubuntu:
  ./scripts/install-apt-deps.sh

Then install Python dependencies in your environment:
  python3 -m pip install -r requirements.txt
"""


class StartupOptions:
    def __init__(
        self,
        gtk_argv: list[str],
        show_upgrade: bool = False,
        show_deps_installer: bool = False,
        database_path: Path | None = None,
        install_desktop: bool = False,
        verbose: bool = False,
    ):
        self.gtk_argv = gtk_argv
        self.show_upgrade = show_upgrade
        self.show_deps_installer = show_deps_installer
        self.database_path = database_path
        self.install_desktop = install_desktop
        self.verbose = verbose


def parse_startup_options(argv: list[str]) -> StartupOptions:
    gtk_argv = [argv[0]]
    show_upgrade = False
    show_deps_installer = False
    database_path: Path | None = None
    install_desktop = False
    verbose = False
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--show-upgrade":
            show_upgrade = True
        elif arg == "--show-deps-installer":
            show_deps_installer = True
        elif arg == "--install-desktop":
            install_desktop = True
        elif arg in {"-v", "--verbose"}:
            verbose = True
        elif arg == "--db":
            index += 1
            if index >= len(argv):
                raise ValueError("--db requires a path")
            database_path = Path(argv[index]).expanduser()
        elif arg.startswith("--db="):
            database_path = Path(arg.split("=", 1)[1]).expanduser()
        else:
            gtk_argv.append(arg)
        index += 1
    return StartupOptions(
        gtk_argv=gtk_argv,
        show_upgrade=show_upgrade,
        show_deps_installer=show_deps_installer,
        database_path=database_path,
        install_desktop=install_desktop,
        verbose=verbose,
    )


def user_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def desktop_exec(argv0: str) -> str:
    executable = Path(argv0)
    if executable.is_absolute():
        return str(executable)
    resolved = shutil.which(argv0)
    if resolved:
        return resolved
    return argv0


def launched_as_installed_command(argv0: str) -> bool:
    return Path(argv0).name == "gtktube"


def desktop_exec_for_launch(argv: list[str]) -> str:
    if launched_as_installed_command(argv[0]):
        return desktop_exec(argv[0])
    return f"{sys.executable} -m gtktube"


def desktop_entry_text(exec_path: str, icon_path: str) -> str:
    return f"""\
[Desktop Entry]
Type=Application
Name=GTKTube
Comment=Local privacy-first YouTube player
Exec={exec_path}
Icon={icon_path}
Terminal=false
Categories=AudioVideo;Video;Player;GTK;
Keywords=YouTube;Video;Player;Subscriptions;GTK;
StartupNotify=true
StartupWMClass={DESKTOP_ID}
"""


def install_desktop_entry(exec_path: str) -> None:
    data_home = user_data_home()
    applications_dir = data_home / "applications"
    icon_dir = data_home / "icons" / "hicolor" / "256x256" / "apps"
    applications_dir.mkdir(parents=True, exist_ok=True)
    icon_dir.mkdir(parents=True, exist_ok=True)

    icon_path = icon_dir / "gtktube.png"
    with resources.as_file(resources.files("gtktube") / "assets" / "gtktube.png") as icon:
        shutil.copyfile(icon, icon_path)

    old_desktop_file = applications_dir / OLD_DESKTOP_FILENAME
    if old_desktop_file.exists():
        old_desktop_file.unlink()

    desktop_file = applications_dir / DESKTOP_FILENAME
    desktop_file.write_text(
        desktop_entry_text(exec_path, str(icon_path)),
        encoding="utf-8",
    )
    desktop_file.chmod(0o644)


def configure_mpv_locale() -> None:
    locale.setlocale(locale.LC_NUMERIC, "C")


def ensure_pygobject() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
    except (ImportError, ModuleNotFoundError, ValueError) as exc:
        print(PYGOBJECT_HELP, file=sys.stderr)
        print(f"Original import error: {exc}", file=sys.stderr)
        return False
    return True


def ensure_mpv() -> bool:
    configure_mpv_locale()
    try:
        import mpv  # noqa: F401
    except (ImportError, ModuleNotFoundError, OSError) as exc:
        print(MPV_HELP, file=sys.stderr)
        print(f"Original import error: {exc}", file=sys.stderr)
        return False
    return True


def dependency_checks_pass() -> bool:
    return ensure_pygobject() and ensure_mpv()


def launch_dependency_installer() -> None:
    from .install_deps import main as install_deps_main

    install_deps_main()


def run_upgrade_tool(reason: str, gtk_argv: list[str]) -> int:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, Gtk

    from .ui.upgrade import UpgradeController

    class UpgradeApplication(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id="local.gtktube.GTKTube.Upgrade",
                flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            )
            self.executor = ThreadPoolExecutor(max_workers=1)
            self.status: Gtk.Label | None = None

        def do_activate(self) -> None:
            window = Gtk.ApplicationWindow(application=self, title="GTKTube upgrade")
            window.set_default_size(520, 220)

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            content.set_margin_top(16)
            content.set_margin_bottom(16)
            content.set_margin_start(16)
            content.set_margin_end(16)
            window.set_child(content)

            title = Gtk.Label(label="GTKTube needs an upgrade", xalign=0)
            title.add_css_class("title-2")
            content.append(title)

            message = Gtk.Label(label=reason, xalign=0, wrap=True)
            content.append(message)

            controller = UpgradeController(
                window,
                self.executor,
                self.set_status,
                self.quit,
            )
            controller.append_upgrade_command(content)

            status = Gtk.Label(label="", xalign=0, wrap=True)
            status.add_css_class("dim-label")
            content.append(status)
            self.status = status

            buttons = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=8,
                halign=Gtk.Align.END,
            )
            content.append(buttons)

            close_button = Gtk.Button(label="Close")
            close_button.connect("clicked", lambda _button: self.quit())
            buttons.append(close_button)

            upgrade_button = Gtk.Button(label="Upgrade and restart")
            upgrade_button.add_css_class("suggested-action")
            buttons.append(upgrade_button)

            def upgrade(_button: Gtk.Button) -> None:
                controller.run_upgrade_and_restart(action=upgrade_button)

            upgrade_button.connect("clicked", upgrade)
            window.present()

        def set_status(self, text: str) -> None:
            if self.status is not None:
                self.status.set_text(text)

        def do_shutdown(self) -> None:
            self.executor.shutdown(wait=False, cancel_futures=True)
            Gtk.Application.do_shutdown(self)

    app = UpgradeApplication()
    return app.run(gtk_argv)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv

    def exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        print("gtktube: UNCAUGHT EXCEPTION", file=sys.stderr)
        import traceback
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stderr)

    sys.excepthook = exception_handler

    options = parse_startup_options(argv)
    installed_command = launched_as_installed_command(argv[0])
    if installed_command or options.install_desktop:
        try:
            install_desktop_entry(desktop_exec_for_launch(argv))
        except OSError as exc:
            print(f"gtktube: could not install desktop entry: {exc}", file=sys.stderr)
    if options.install_desktop:
        return 0
    if options.show_deps_installer:
        launch_dependency_installer()
    if not dependency_checks_pass():
        if not options.show_deps_installer:
            launch_dependency_installer()
        if not dependency_checks_pass():
            return 2

    paths = AppPaths.discover()
    if options.database_path is not None:
        paths = AppPaths(
            data_dir=paths.data_dir,
            cache_dir=paths.cache_dir,
            config_dir=paths.config_dir,
            database_path=options.database_path,
        )
    paths.ensure()
    paths.database_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"gtktube: database={paths.database_path}", file=sys.stderr)

    connection = connect(paths.database_path)
    try:
        migrate(connection)
    except UnsupportedDatabaseSchema as exc:
        connection.close()
        reason = (
            f"The database uses schema {exc.current}, but this GTKTube install "
            f"only supports schema {exc.supported}. Upgrade GTKTube to open it."
        )
        return run_upgrade_tool(reason, options.gtk_argv)
    repository = LibraryRepository(connection)
    extractor = YoutubeExtractor()
    service = LibraryService(repository, extractor)

    from .ui.main_window import GTKTubeApplication

    app = GTKTubeApplication(
        service,
        paths,
        force_update_dialog=options.show_upgrade,
        enable_update_check=installed_command or options.show_upgrade,
        verbose=options.verbose,
    )
    previous_sigint = signal.getsignal(signal.SIGINT)

    def on_sigint(_signum: int, _frame: object) -> None:
        app.quit()

    signal.signal(signal.SIGINT, on_sigint)
    try:
        return app.run(options.gtk_argv)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
