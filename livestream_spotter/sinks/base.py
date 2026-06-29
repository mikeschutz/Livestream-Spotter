"""Minimal sink contracts through PLAN Phase 2."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from livestream_spotter.events import Event


class EventSink(Protocol):
    def write(self, event: Event) -> None: ...

    def close(self) -> None: ...


class RawSink(Protocol):
    def write(self, record: Mapping[str, Any]) -> None: ...

    def close(self) -> None: ...
