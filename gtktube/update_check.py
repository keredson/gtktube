from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version


PYPI_JSON_URL = "https://pypi.org/pypi/gtktube/json"


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
    prefix_path = Path(prefix or sys.prefix)
    executable_path = Path(executable or sys.executable)
    paths = [prefix_path, executable_path]
    if any("pipx" in path.parts and "venvs" in path.parts for path in paths):
        return ["pipx", "upgrade", "gtktube"]
    return ["python3", "-m", "pip", "install", "--upgrade", "gtktube"]


def check_for_update(current_version: str, force: bool = False) -> UpdateInfo | None:
    latest_version = latest_pypi_version()
    if not force and not is_newer_version(current_version, latest_version):
        return None
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        project_url="https://pypi.org/project/gtktube/",
    )
