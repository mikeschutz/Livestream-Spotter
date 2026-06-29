"""Parked strict YouTube chapter renderer."""

from __future__ import annotations

import logging
from pathlib import Path

from livestream_spotter.sinks.timestamp_core import (
    TimestampFileSink,
    adjusted_timestamp_ms,
    format_timestamp,
)

LOGGER = logging.getLogger(__name__)

__all__ = ["ChaptersSink", "adjusted_timestamp_ms", "format_timestamp"]


class ChaptersSink(TimestampFileSink):
    """Render strict chapters with a seed and hard ten-second minimum."""

    def __init__(self, path: Path, min_spacing_ms: int = 10_000) -> None:
        if min_spacing_ms < 10_000:
            raise ValueError("chapter minimum spacing must be at least 10 seconds")
        super().__init__(
            path,
            min_spacing_ms,
            seed_line="00:00 Stream start",
            logger=LOGGER,
        )
