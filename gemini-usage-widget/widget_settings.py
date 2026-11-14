"""Persist the Gemini widget's appearance, screen position, and movement lock."""

from __future__ import annotations

import sys
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
)


def settings_path() -> Path:
    return app_settings_path("GeminiUsageMini")


def load_settings() -> WidgetSettings:
    """Read settings without creating or changing any files."""
    return load_settings_from_path(settings_path())


def save_settings(settings: WidgetSettings) -> None:
    """Atomically replace the settings file, propagating filesystem failures."""
    save_settings_to_path(settings, settings_path())
