"""Real and synthetic implementations of the video timeline clock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import time
from typing import Any, Protocol

from livestream_spotter.config import ObsConfig

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClockReading:
    """One observation of the stream timeline."""

    video_ms: int | None
    output_active: bool
    source: str


class VideoClock(Protocol):
    def connect(self) -> bool: ...

    def read(self) -> ClockReading: ...

    def disconnect(self) -> None: ...


def _default_client_factory(**kwargs: Any) -> Any:
    import obsws_python

    return obsws_python.ReqClient(**kwargs)


class ObsClock:
    """Read video time only from OBS GetStreamStatus.outputDuration."""

    def __init__(
        self,
        config: ObsConfig,
        client_factory: Callable[..., Any] = _default_client_factory,
        monotonic: Callable[[], float] = time.monotonic,
        retry_seconds: float = 2.0,
    ) -> None:
        self._config = config
        self._client_factory = client_factory
        self._monotonic = monotonic
        self._retry_seconds = retry_seconds
        self._client: Any | None = None
        self._reported_waiting = False
        self._next_attempt = 0.0

    def connect(self) -> bool:
        """Connect if OBS is available. Repeated calls are safe."""
        if self._client is not None:
            return True
        now = self._monotonic()
        if now < self._next_attempt:
            return False
        self._next_attempt = now + self._retry_seconds
        try:
            self._client = self._client_factory(
                host=self._config.host,
                port=self._config.port,
                password=self._config.password,
                timeout=2,
            )
            self._reported_waiting = False
            self._next_attempt = 0.0
            LOGGER.info("Connected to OBS WebSocket")
            return True
        except Exception as error:
            self._client = None
            if not self._reported_waiting:
                LOGGER.info("Waiting for OBS WebSocket: %s", error)
                self._reported_waiting = True
            return False

    def read(self) -> ClockReading:
        if not self.connect():
            return ClockReading(video_ms=None, output_active=False, source="obs")
        try:
            status = self._client.get_stream_status()
            active = bool(status.output_active)
            duration = int(status.output_duration) if active else None
            return ClockReading(
                video_ms=duration,
                output_active=active,
                source="obs",
            )
        except Exception as error:
            LOGGER.warning("OBS connection lost (%s); will retry", error)
            self.disconnect()
            return ClockReading(video_ms=None, output_active=False, source="obs")

    def disconnect(self) -> None:
        """Close the WebSocket. Repeated calls are safe."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            LOGGER.exception("Error while disconnecting from OBS")
        else:
            LOGGER.info("Disconnected from OBS WebSocket")


class MockClock:
    """Synthetic monotonic timeline for replay development without OBS."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._started_at: float | None = None

    def connect(self) -> bool:
        if self._started_at is None:
            self._started_at = self._monotonic()
            LOGGER.info("Using synthetic monotonic video clock")
        return True

    def read(self) -> ClockReading:
        self.connect()
        assert self._started_at is not None
        elapsed_ms = int((self._monotonic() - self._started_at) * 1000)
        return ClockReading(
            video_ms=max(0, elapsed_ms),
            output_active=True,
            source="mock",
        )

    def disconnect(self) -> None:
        self._started_at = None
