"""Persist the widget's transparency and screen position."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WidgetSettings:
    transparency: int = 30
    x: int | None = None
    y: int | None = None


def settings_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CodexUsageMini" / "settings.json"


def _integer_in_range(value: object, minimum: int, maximum: int) -> bool:
    # bool is an int subclass, but is not a slider value or coordinate.
    return type(value) is int and minimum <= value <= maximum


def _validated_settings(data: object) -> WidgetSettings:
    settings = WidgetSettings()
    if not isinstance(data, dict):
        return settings

    transparency = data.get("transparency")
    if _integer_in_range(transparency, 0, 100):
        settings.transparency = transparency

    x, y = data.get("x"), data.get("y")
    if _integer_in_range(x, -(2**31), 2**31 - 1) and _integer_in_range(y, -(2**31), 2**31 - 1):
        settings.x, settings.y = x, y
    return settings


def load_settings() -> WidgetSettings:
    """Read settings without creating or changing any files."""
    try:
        with settings_path().open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return WidgetSettings()
    return _validated_settings(data)


def save_settings(settings: WidgetSettings) -> None:
    """Atomically replace the settings file, propagating filesystem failures."""
    settings = _validated_settings(
        {"transparency": settings.transparency, "x": settings.x, "y": settings.y}
    )
    data = {"transparency": settings.transparency, "x": settings.x, "y": settings.y}
    destination = settings_path()
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=".settings-",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            json.dump(data, target, ensure_ascii=False, indent=2, allow_nan=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        # Windows requires the temporary file to be closed before replacement.
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Keep the original write/replace error for the caller to log.
                pass
