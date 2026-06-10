from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

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


def check_for_update(current_version: str) -> UpdateInfo | None:
    latest_version = latest_pypi_version()
    if not is_newer_version(current_version, latest_version):
        return None
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        project_url="https://pypi.org/project/gtktube/",
    )
