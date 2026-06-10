from __future__ import annotations

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


def ensure_pygobject() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
    except (ImportError, ModuleNotFoundError, ValueError) as exc:
        print(PYGOBJECT_HELP, file=sys.stderr)
        print(f"Original import error: {exc}", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv
    if not ensure_pygobject():
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
    return app.run(argv)
