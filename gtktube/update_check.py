from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version


PYPI_JSON_URL = "https://pypi.org/pypi/gtktube/json"
ONE_SHOT_RESTART_FLAGS = {
    "--show-upgrade",
    "--show-deps-installer",
    "--install-desktop",
}


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    project_url: str


def latest_pypi_version(timeout: float = 5.0) -> str:
    request = urllib.request.Request(
        PYPI_JSON_URL,
        headers={"Accept": "application/json", "User-Agent": "GTKTube"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload["info"]["version"])


def is_newer_version(current_version: str, latest_version: str) -> bool:
    if current_version == "0+unknown":
        return False
    try:
        return Version(latest_version) > Version(current_version)
    except InvalidVersion:
        return False


def upgrade_command(prefix: str | None = None, executable: str | None = None) -> str:
    return " ".join(upgrade_command_args(prefix=prefix, executable=executable))


def upgrade_command_args(
    prefix: str | None = None, executable: str | None = None
) -> list[str]:
    if installed_with_pipx(prefix=prefix, executable=executable):
        return ["pipx", "upgrade", "gtktube"]
    return ["python3", "-m", "pip", "install", "--upgrade", "gtktube"]


def installed_with_pipx(
    prefix: str | None = None, executable: str | None = None
) -> bool:
    prefix_path = Path(prefix or sys.prefix)
    executable_path = Path(executable or sys.executable)
    paths = [prefix_path, executable_path]
    return any("pipx" in path.parts and "venvs" in path.parts for path in paths)


def restart_command_args(
    argv: list[str] | None = None,
    prefix: str | None = None,
    executable: str | None = None,
) -> list[str]:
    argv = list(argv or sys.argv)
    restart_args = restart_startup_args(argv[1:])
    if installed_with_pipx(prefix=prefix, executable=executable):
        command = argv[0] if argv and Path(argv[0]).name == "gtktube" else "gtktube"
        return [command, *restart_args]
    if argv:
        return [executable or sys.executable, argv[0], *restart_args]
    return [executable or sys.executable]


def restart_startup_args(args: list[str]) -> list[str]:
    restart_args: list[str] = []
    for arg in args:
        if arg in ONE_SHOT_RESTART_FLAGS:
            continue
        restart_args.append(arg)
    return restart_args


def check_for_update(current_version: str, force: bool = False) -> UpdateInfo | None:
    latest_version = latest_pypi_version()
    if not force and not is_newer_version(current_version, latest_version):
        return None
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        project_url="https://pypi.org/project/gtktube/",
    )
