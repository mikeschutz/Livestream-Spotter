"""Shared mechanics for text timestamp renderers."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import NamedTuple

from livestream_spotter.events import Event


class TimestampEntry(NamedTuple):
    timestamp_ms: int
    label: str
    tier: int
    sequence: int


def format_timestamp(timestamp_ms: int) -> str:
    total_seconds = max(0, timestamp_ms) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def adjusted_timestamp_ms(event: Event) -> int:
    return max(0, event.video_ms - event.lead_in_ms)


def select_entries(
    candidates: list[TimestampEntry],
    min_spacing_ms: int,
    seed_at_zero: bool,
) -> list[TimestampEntry]:
    """Sort, dedupe by spacing, and prefer Tier 1 over 2 over 3.

    Equal-tier collisions keep the earlier adjusted timestamp. Exact timestamp
    ties keep first arrival.
    """
    ordered = sorted(
        candidates,
        key=lambda entry: (entry.timestamp_ms, entry.sequence),
    )
    selected: list[TimestampEntry] = []
    for candidate in ordered:
        previous_ms = selected[-1].timestamp_ms if selected else None
        if previous_ms is None and seed_at_zero:
            previous_ms = 0
        if (
            previous_ms is None
            or candidate.timestamp_ms - previous_ms >= min_spacing_ms
        ):
            selected.append(candidate)
            continue
        if selected and candidate.tier < selected[-1].tier:
            selected[-1] = candidate
    return selected


class TimestampFileSink:
    def __init__(
        self,
        path: Path,
        min_spacing_ms: int,
        *,
        seed_line: str | None,
        logger: logging.Logger,
    ) -> None:
        if min_spacing_ms < 0:
            raise ValueError("timestamp minimum spacing cannot be negative")
        self._path = path
        self._min_spacing_ms = min_spacing_ms
        self._seed_line = seed_line
        self._logger = logger
        self._candidates: list[TimestampEntry] = []
        self._previous_video_ms: int | None = None
        self._warned_duration_regression = False
        self._rewrite()

    def write(self, event: Event) -> None:
        self._observe_event(event)
        self._append_event(event)

    def _observe_event(self, event: Event) -> None:
        if (
            self._previous_video_ms is not None
            and event.video_ms < self._previous_video_ms
            and not self._warned_duration_regression
        ):
            self._logger.critical(
                "OBS outputDuration DECREASED from %d ms to %d ms; "
                "%s for this session may be unreliable",
                self._previous_video_ms,
                event.video_ms,
                self._path.name,
            )
            self._warned_duration_regression = True
        self._previous_video_ms = event.video_ms

    def _append_event(self, event: Event, *, label: str | None = None) -> None:
        self._candidates.append(
            TimestampEntry(
                adjusted_timestamp_ms(event),
                event.label if label is None else label,
                event.tier,
                len(self._candidates),
            )
        )
        self._rewrite()

    def close(self) -> None:
        pass

    def _rewrite(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f"{self._path.name}.tmp")
        lines = [self._seed_line] if self._seed_line is not None else []
        lines.extend(
            f"{format_timestamp(entry.timestamp_ms)} {entry.label}"
            for entry in select_entries(
                self._candidates,
                self._min_spacing_ms,
                seed_at_zero=self._seed_line is not None,
            )
        )
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            if lines:
                output.write("\n".join(lines))
                output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self._path)
