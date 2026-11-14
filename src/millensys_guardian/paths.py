from __future__ import annotations

import sys
from pathlib import Path


def install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    path = install_dir() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    # Runtime state is intentionally folder-contained. Never restore configuration
    # from ProgramData or another installation when the local database is removed.
    return data_dir() / "monitoring.db"


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
