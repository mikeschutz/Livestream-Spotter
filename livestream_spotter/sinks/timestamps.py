"""Permissive plain-timestamp renderer used by default."""

from __future__ import annotations

import logging
from pathlib import Path

from livestream_spotter.events import Event
from livestream_spotter.sinks.timestamp_core import TimestampFileSink

LOGGER = logging.getLogger(__name__)


def abbreviate_name(full_name: str) -> str:
    """Render first token plus the last token's initial."""
    tokens = full_name.split()
    if len(tokens) < 2:
        return full_name.strip()
    return f"{tokens[0]} {tokens[-1][0].upper()}."


class TimestampsSink(TimestampFileSink):
    """Render unseeded timestamps with optional cosmetic spacing."""

    def __init__(self, path: Path, min_spacing_ms: int = 2_000) -> None:
        self._open_pit: Event | None = None
        self._open_pit_towed = False
        self._pit_count = 0
        super().__init__(path, min_spacing_ms, seed_line=None, logger=LOGGER)

    def write(self, event: Event) -> None:
        self._observe_event(event)

        if event.event_type == "pit_in":
            self._flush_open_pit()
            self._open_pit = event
            self._open_pit_towed = False
            return

        if event.event_type == "tow" and self._open_pit is not None:
            self._open_pit_towed = True
            return

        if event.event_type == "pit_out" and self._open_pit is not None:
            self._flush_open_pit()
            return

        if event.event_type in {"checkered", "session_transition", "green"}:
            self._flush_open_pit()
        if event.event_type in {"session_transition", "green"}:
            self._pit_count = 0

        self._append_event(event, label=self._published_label(event))

    def close(self) -> None:
        self._flush_open_pit()
        super().close()

    def _published_label(self, event: Event) -> str:
        full_name = event.meta.get("opponent_name")
        if not isinstance(full_name, str) or not full_name.strip():
            return event.label
        return event.label.replace(full_name, abbreviate_name(full_name))

    def _flush_open_pit(self) -> None:
        if self._open_pit is None:
            return
        self._pit_count += 1
        label = f"Pit stop {self._pit_count}"
        if self._open_pit_towed:
            label += " (towed)"
        self._append_event(self._open_pit, label=label)
        self._open_pit = None
        self._open_pit_towed = False
