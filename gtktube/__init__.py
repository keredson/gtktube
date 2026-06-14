__all__ = ["__version__"]

import importlib.metadata
from pathlib import Path


def _get_version() -> str:
    # 1. Try to get version from pyproject.toml (if we are in a source checkout)
    try:
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        if pyproject_path.exists():
            import tomllib

            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
                return str(data["project"]["version"])
    except Exception:
        pass

    # 2. Try to get version from installed package metadata
    try:
        return importlib.metadata.version("gtktube")
    except importlib.metadata.PackageNotFoundError:
        pass

    return "0+unknown"


__version__ = _get_version()
