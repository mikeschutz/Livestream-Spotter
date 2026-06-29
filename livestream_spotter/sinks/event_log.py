"""Temporary console observability for emitted Events."""

from __future__ import annotations

import logging

from livestream_spotter.events import Event

LOGGER = logging.getLogger(__name__)


class LoggingEventSink:
    def write(self, event: Event) -> None:
        LOGGER.info(
            "event type=%s label=%r tier=%d video_ms=%d lap=%s",
            event.event_type,
            event.label,
            event.tier,
            event.video_ms,
            event.lap,
        )

    def close(self) -> None:
        pass
