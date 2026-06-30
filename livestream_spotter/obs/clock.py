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
    connected: bool = True


class VideoClock(Protocol):
    def connect(self) -> bool: ...

    def read(self) -> ClockReading: ...

    def disconnect(self) -> None: ...


def _default_client_factory(**kwargs: Any) -> Any:
    import obsws_python

    return obsws_python.ReqClient(**kwargs)


def _is_authentication_error(error: Exception) -> bool:
    try:
        from obsws_python.error import OBSSDKError, OBSSDKTimeoutError
    except ImportError:
        return False
    # OBSSDKTimeoutError subclasses OBSSDKError but is a reachability symptom,
    # not an auth rejection — exclude it so a future timeout-at-connect can't be
    # mislabeled as a bad password.
    return isinstance(error, OBSSDKError) and not isinstance(
        error, OBSSDKTimeoutError
    )


def _is_reachability_error(error: Exception) -> bool:
    if isinstance(error, (ConnectionRefusedError, TimeoutError, OSError)):
        return True
    try:
        from websocket import WebSocketAddressException, WebSocketTimeoutException
    except ImportError:
        return False
    return isinstance(error, (WebSocketAddressException, WebSocketTimeoutException))


def _connection_failure_message(config: ObsConfig, error: Exception) -> str:
    if _is_authentication_error(error):
        return (
            "Connected to OBS but authentication failed — check the password "
            "in config.toml matches OBS."
        )
    if _is_reachability_error(error):
        return (
            f"Could not reach OBS WebSocket at {config.host}:{config.port} — "
            "is the WebSocket server enabled in OBS? "
            "(Tools -> WebSocket Server Settings)"
        )
    return (
        f"Could not connect to OBS WebSocket at {config.host}:{config.port}: "
        f"{type(error).__name__}: {error}"
    )


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
        self._reported_failure: str | None = None
        self._next_attempt = 0.0

    @property
    def connected(self) -> bool:
        # Seam for the future always-on daemon supervisor (see AGENTS.md):
        # it polls this to stand the pipeline up/down as OBS comes and goes.
        return self._client is not None

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
            self._reported_failure = None
            self._next_attempt = 0.0
            LOGGER.info(
                "Connected to OBS WebSocket at %s:%d",
                self._config.host,
                self._config.port,
            )
            return True
        except Exception as error:
            self._client = None
            message = _connection_failure_message(self._config, error)
            LOGGER.debug("OBS connection attempt failed", exc_info=True)
            if message != self._reported_failure:
                LOGGER.warning(message)
                self._reported_failure = message
            return False

    def read(self) -> ClockReading:
        if not self.connect():
            return ClockReading(
                video_ms=None,
                output_active=False,
                source="obs",
                connected=False,
            )
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
            self._next_attempt = self._monotonic() + self._retry_seconds
            return ClockReading(
                video_ms=None,
                output_active=False,
                source="obs",
                connected=False,
            )

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
