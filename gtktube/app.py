from __future__ import annotations

import locale
import signal
import sys

from .db.connection import connect
from .db.migrations import migrate
from .db.repositories import LibraryRepository
from .extractors.youtube import YoutubeExtractor
from .paths import AppPaths
from .services.library import LibraryService


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


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv
    if not dependency_checks_pass():
        launch_dependency_installer()
        if not dependency_checks_pass():
            return 2

    paths = AppPaths.discover()
    paths.ensure()
    print(f"gtktube: database={paths.database_path}", file=sys.stderr)

    connection = connect(paths.database_path)
    migrate(connection)
    repository = LibraryRepository(connection)
    extractor = YoutubeExtractor()
    service = LibraryService(repository, extractor)

    from .ui.main_window import GTKTubeApplication

    app = GTKTubeApplication(service, paths)
    previous_sigint = signal.getsignal(signal.SIGINT)

    def on_sigint(_signum: int, _frame: object) -> None:
        app.quit()

    signal.signal(signal.SIGINT, on_sigint)
    try:
        return app.run(argv)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
