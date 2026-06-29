"""Stable event payload and detector intention types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple


@dataclass
class Event:
    video_ms: int
    event_type: str
    label: str
    tier: int
    lap: int | None
    session_time: float
    lead_in_ms: int
    meta: dict


class EventIntent(NamedTuple):
    """An unstamped event produced by a pure detector."""

    event_type: str
    label: str
    tier: int
    lap: int | None
    meta: dict[str, Any]
