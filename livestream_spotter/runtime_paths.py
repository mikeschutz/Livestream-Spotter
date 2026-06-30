"""Resolve user-editable files for source and frozen runs."""

from __future__ import annotations

from pathlib import Path
import sys


CONFIG_FILENAME = "config.toml"
LASTRUN_LOG_FILENAME = "lastrun.log"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    return Path(sys.executable).resolve().parent


def resolve_config_path(path: Path) -> Path:
    """Resolve the config path without pointing frozen builds at _MEI temp data."""
    if is_frozen() and not path.is_absolute():
        return executable_dir() / path
    return path


def resolve_lastrun_log_path(config_path: Path) -> Path:
    """Place the per-run log beside the resolved config (and frozen exe)."""
    return config_path.resolve().parent / LASTRUN_LOG_FILENAME


def missing_config_message(path: Path) -> str:
    if is_frozen():
        return (
            f"Config file not found: {path}. Place {CONFIG_FILENAME} beside "
            "livestream-spotter.exe, edit it for your OBS settings, and run again."
        )
    return f"Config file not found: {path}"


def invalid_config_message(path: Path, error: Exception) -> str:
    detail = f"missing required key {error}" if isinstance(error, KeyError) else str(error)
    return f"Config file is invalid ({path}): {detail}. Fix {CONFIG_FILENAME} and run again."
