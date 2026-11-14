"""Persist the Codex widget's appearance, screen position, movement lock, and pending resets."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_WIDGET_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WIDGET_DIR.parent
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from common.settings import (
    WidgetSettings,
    app_settings_path,
    load_settings_from_path,
    save_settings_to_path,
    write_json_atomic,
)


@dataclass(frozen=True)
class PendingReset:
    key: str
    executable: str
    credit_id: str


def settings_path() -> Path:
    return app_settings_path("CodexUsageMini")


def load_settings() -> WidgetSettings:
    """Read settings without creating or changing any files."""
    return load_settings_from_path(settings_path())


def save_settings(settings: WidgetSettings) -> None:
    """Atomically replace the settings file, propagating filesystem failures."""
    save_settings_to_path(settings, settings_path())


def load_pending_reset() -> PendingReset | None:
    path = settings_path().with_name("pending-reset.json")
    try:
        import json

        with path.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError("Invalid pending reset")
    key, executable, credit_id = (data.get(name) for name in ("key", "executable", "credit_id"))
    if not all(isinstance(value, str) and value.strip() for value in (key, executable, credit_id)):
        raise ValueError("Invalid pending reset")
    return PendingReset(key, executable, credit_id)


def save_pending_reset(request: PendingReset | None) -> None:
    path = settings_path().with_name("pending-reset.json")
    if request is None:
        path.unlink(missing_ok=True)
    else:
        write_json_atomic(
            path,
            {
                "key": request.key,
                "executable": request.executable,
                "credit_id": request.credit_id,
            },
        )
