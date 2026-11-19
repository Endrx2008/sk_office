# Copyright (C) 2026 Enrico Miot (EndrX)
# Licensed under GNU GPL v3 or later — see the LICENSE file or <https://www.gnu.org/licenses/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""XDG-path JSON settings for the sk_office extensions, like the other SKAI
tools (config_dir()/settings.json, tolerant load, indented save).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .ollama_client import DEFAULT_HOST


def config_dir(app_name: str) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / app_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file(app_name: str) -> Path:
    return config_dir(app_name) / "settings.json"


@dataclass
class Settings:
    ollama_host: str = DEFAULT_HOST

    @classmethod
    def load(cls, app_name: str) -> "Settings":
        path = config_file(app_name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            return cls()

    def save(self, app_name: str) -> None:
        config_file(app_name).write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8"
        )
