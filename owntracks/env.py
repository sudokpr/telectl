from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> dict[str, str]:
    env_path = path or PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return {**values, **os.environ}


def env_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def project_path(value: str | None, default: str) -> Path:
    raw = value or default
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path
