"""A deliberately small in-process Event queue."""

from __future__ import annotations

from queue import Empty, Queue

from livestream_spotter.events import Event


class EventBus:
    def __init__(self) -> None:
        self._queue: Queue[Event] = Queue()

    def publish(self, event: Event) -> None:
        self._queue.put_nowait(event)

    def take_nowait(self) -> Event | None:
        try:
            return self._queue.get_nowait()
        except Empty:
            return None
