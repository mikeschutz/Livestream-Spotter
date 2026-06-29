"""Deliver each Event to a small fixed set of sinks."""

from __future__ import annotations

from collections.abc import Sequence

from livestream_spotter.events import Event
from livestream_spotter.sinks.base import EventSink


class FanoutEventSink:
    def __init__(self, sinks: Sequence[EventSink]) -> None:
        self._sinks = tuple(sinks)

    def write(self, event: Event) -> None:
        for sink in self._sinks:
            sink.write(event)

    def close(self) -> None:
        for sink in self._sinks:
            sink.close()
