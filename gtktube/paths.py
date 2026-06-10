from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_ID = "gtktube"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    cache_dir: Path
    config_dir: Path
    database_path: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))

        data_dir = data_home / APP_ID
        cache_dir = cache_home / APP_ID
        config_dir = config_home / APP_ID
        return cls(
            data_dir=data_dir,
            cache_dir=cache_dir,
            config_dir=config_dir,
            database_path=data_dir / "gtktube.sqlite3",
        )

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "thumbnails" / "channels").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "thumbnails" / "videos").mkdir(parents=True, exist_ok=True)
