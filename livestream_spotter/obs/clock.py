"""Real and synthetic implementations of the video timeline clock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Literal, Protocol

from livestream_spotter.config import ObsConfig

LOGGER = logging.getLogger(__name__)

TimestampSource = Literal["auto", "stream", "record"]
OutputSource = Literal["stream", "record"]


@dataclass(frozen=True)
class ClockReading:
    """One observation of the OBS output timeline."""

    video_ms: int | None
    output_active: bool
    source: str
    connected: bool = True


class VideoClock(Protocol):
    @property
    def connected(self) -> bool: ...

    def connect(self) -> bool: ...

    def read(self) -> ClockReading: ...

    def disconnect(self) -> None: ...


@dataclass
class _OutputSample:
    active: bool = False
    duration_ms: int = 0
    sampled_at_monotonic: float | None = None
    paused: bool = False
    paused_at_monotonic: float | None = None


def _default_request_client_factory(**kwargs: Any) -> Any:
    import obsws_python

    return obsws_python.ReqClient(**kwargs)


def _default_event_client_factory(**kwargs: Any) -> Any:
    import obsws_python

    return obsws_python.EventClient(subs=obsws_python.Subs.OUTPUTS, **kwargs)


def _is_authentication_error(error: Exception) -> bool:
    try:
        from obsws_python.error import OBSSDKError, OBSSDKTimeoutError
    except ImportError:
        return False
    # OBSSDKTimeoutError subclasses OBSSDKError but is a reachability symptom,
    # not an auth rejection -- exclude it so a future timeout-at-connect can't be
    # mislabeled as a bad password.
    return isinstance(error, OBSSDKError) and not isinstance(
        error, OBSSDKTimeoutError
    )


def _is_reachability_error(error: Exception) -> bool:
    if isinstance(error, (ConnectionRefusedError, TimeoutError, OSError)):
        return True
    try:
        from obsws_python.error import OBSSDKTimeoutError
    except ImportError:
        pass
    else:
        if isinstance(error, OBSSDKTimeoutError):
            return True
    try:
        from websocket import (
            WebSocketAddressException,
            WebSocketConnectionClosedException,
            WebSocketTimeoutException,
        )
    except ImportError:
        return False
    return isinstance(
        error,
        (
            WebSocketAddressException,
            WebSocketConnectionClosedException,
            WebSocketTimeoutException,
        ),
    )


def _connection_failure_message(config: ObsConfig, error: Exception) -> str:
    if _is_authentication_error(error):
        return (
            "Connected to OBS but authentication failed -- check the password "
            "in config.toml matches OBS."
        )
    if _is_reachability_error(error):
        return (
            f"Could not reach OBS WebSocket at {config.host}:{config.port} -- "
            "is the WebSocket server enabled in OBS? "
            "(Tools -> WebSocket Server Settings)"
        )
    return (
        f"Could not connect to OBS WebSocket at {config.host}:{config.port}: "
        f"{type(error).__name__}: {error}"
    )


def _pick_source(
    configured: TimestampSource,
    stream_active: bool,
    record_active: bool,
) -> OutputSource | None:
    if configured == "stream":
        return "stream" if stream_active else None
    if configured == "record":
        return "record" if record_active else None
    if stream_active:
        return "stream"
    if record_active:
        return "record"
    return None


class ObsClock:
    """Project OBS output time from event-driven stream/record samples."""

    def __init__(
        self,
        config: ObsConfig,
        timestamp_source: TimestampSource = "auto",
        client_factory: Callable[..., Any] = _default_request_client_factory,
        event_client_factory: Callable[..., Any] = _default_event_client_factory,
        monotonic: Callable[[], float] = time.monotonic,
        retry_seconds: float = 2.0,
    ) -> None:
        self._config = config
        self._timestamp_source = timestamp_source
        self._request_client_factory = client_factory
        self._event_client_factory = event_client_factory
        self._monotonic = monotonic
        self._retry_seconds = retry_seconds
        self._request_client: Any | None = None
        self._event_client: Any | None = None
        self._healthy = False
        self._stream = _OutputSample()
        self._record = _OutputSample()
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._connect_failure_warned = False
        self._next_attempt = 0.0
        self._last_selected_source: OutputSource | None = None

    @property
    def connected(self) -> bool:
        # Seam for the Stage 2 lifecycle supervisor: connected always means the
        # request client and a live event subscription are both ready.
        with self._lock:
            healthy = (
                self._healthy
                and self._request_client is not None
                and self._event_client is not None
            )
            event_client = self._event_client
        if not healthy:
            return False

        # obsws-python exposes no close callback. Its public worker thread exits
        # when recv() sees WebSocketConnectionClosedException or OSError, so its
        # liveness is the passive connection signal (no OBS request required).
        worker = getattr(event_client, "worker", None)
        try:
            worker_alive = worker is not None and worker.is_alive()
        except (AttributeError, RuntimeError):
            worker_alive = False
        if worker_alive:
            return True

        self._mark_disconnected()
        return False

    def connect(self) -> bool:
        """Connect, subscribe, and seed both output samples. Safe to repeat."""
        if self.connected:
            return True
        now = self._monotonic()
        if now < self._next_attempt:
            return False
        self._next_attempt = now + self._retry_seconds

        # A callback request failure can leave clients awaiting cleanup. This is
        # performed from the caller thread, never obsws-python's event thread.
        self._close_clients(log_success=False)
        kwargs = {
            "host": self._config.host,
            "port": self._config.port,
            "password": self._config.password,
            "timeout": 2,
        }
        try:
            request_client = self._request_client_factory(**kwargs)
            with self._lock:
                self._request_client = request_client
            event_client = self._event_client_factory(**kwargs)
            with self._lock:
                self._event_client = event_client
                self._healthy = True
            # obsws-python maps these snake_case method names to the protocol's
            # StreamStateChanged and RecordStateChanged event names.
            event_client.callback.register(
                [self.on_stream_state_changed, self.on_record_state_changed]
            )
            self._sample_stream()
            self._sample_record()
        except Exception as error:
            self._close_clients(log_success=False)
            if not (
                _is_reachability_error(error)
                or _is_authentication_error(error)
            ):
                raise
            message = _connection_failure_message(self._config, error)
            with self._lock:
                should_warn = not self._connect_failure_warned
                self._connect_failure_warned = True
            if should_warn:
                LOGGER.warning(message)
            return False

        if not self.connected:
            self._close_clients(log_success=False)
            return False

        with self._lock:
            self._connect_failure_warned = False
        self._next_attempt = 0.0
        LOGGER.info(
            "Connected to OBS WebSocket at %s:%d",
            self._config.host,
            self._config.port,
        )
        self._log_initial_output_status()
        return True

    def _log_initial_output_status(self) -> None:
        with self._lock:
            recording = self._record.active
            streaming = self._stream.active
        LOGGER.info(
            "OBS connected; recording=%s, streaming=%s",
            "active" if recording else "inactive",
            "active" if streaming else "inactive",
        )

    def active_source(self) -> OutputSource | None:
        """Return the configured source selection without performing I/O."""
        with self._lock:
            return _pick_source(
                self._timestamp_source,
                self._stream.active,
                self._record.active,
            )

    def is_output_active(self) -> bool:
        return self.active_source() is not None

    def now_ms(self) -> int | None:
        """Return locally projected OBS output time without a WebSocket call."""
        switched: tuple[OutputSource, OutputSource] | None = None
        with self._lock:
            selected = _pick_source(
                self._timestamp_source,
                self._stream.active,
                self._record.active,
            )
            if selected is None:
                return None
            if (
                self._last_selected_source is not None
                and self._last_selected_source != selected
            ):
                switched = (self._last_selected_source, selected)
            self._last_selected_source = selected
            sample = self._stream if selected == "stream" else self._record
            if sample.sampled_at_monotonic is None:
                return None
            projection_at = (
                sample.paused_at_monotonic
                if sample.paused and sample.paused_at_monotonic is not None
                else self._monotonic()
            )
            elapsed_ms = int(
                max(0.0, projection_at - sample.sampled_at_monotonic) * 1000
            )
            video_ms = sample.duration_ms + elapsed_ms

        if switched is not None:
            LOGGER.info("Timestamp source switched: %s -> %s", *switched)
        return video_ms

    def read(self) -> ClockReading:
        # Preserve the current retry seam until Stage 2 owns reconnection. Once
        # connected, now_ms() is pure local math and performs no OBS requests.
        if not self.connected and not self.connect():
            return ClockReading(None, False, "obs", connected=False)
        video_ms = self.now_ms()
        return ClockReading(
            video_ms=video_ms,
            output_active=video_ms is not None,
            source="obs",
            connected=self.connected,
        )

    def on_stream_state_changed(self, event: Any) -> None:
        """Handle obsws-python's StreamStateChanged callback."""
        output_active = getattr(event, "output_active", None)
        output_state = getattr(event, "output_state", None)
        # OBS WebSocket exposes no stream pause/resume event or paused status.
        # Streaming pause is therefore intentionally unsupported in this stage.
        if output_active is not None and not bool(output_active):
            self._set_output_inactive("stream")
        else:
            self._resample_from_event("stream")
        self._log_output_state_change("streaming", output_state, output_active)

    def on_record_state_changed(self, event: Any) -> None:
        """Handle record start/stop/pause/resume from RecordStateChanged."""
        output_active = getattr(event, "output_active", None)
        output_state = getattr(event, "output_state", None)
        # OBS pause/resume are outputState variants of RecordStateChanged, not
        # separate RecordPaused/RecordResumed events. GetRecordStatus exposes
        # outputPaused, so every record state change re-seeds it authoritatively.
        if output_active is not None and not bool(output_active):
            self._set_output_inactive("record")
        else:
            self._resample_from_event("record")
        self._log_output_state_change("recording", output_state, output_active)

    @staticmethod
    def _log_output_state_change(
        output_name: str,
        output_state: Any,
        output_active: Any,
    ) -> None:
        action: str | None = None
        if isinstance(output_state, str):
            prefix = "OBS_WEBSOCKET_OUTPUT_"
            action = output_state.removeprefix(prefix).lower()
        if not action:
            if output_active is None:
                action = "state changed"
            else:
                action = "started" if bool(output_active) else "stopped"
        LOGGER.info("OBS %s %s", output_name, action)

    def _set_output_inactive(self, source: OutputSource) -> None:
        with self._lock:
            sample = self._stream if source == "stream" else self._record
            sample.active = False
            sample.paused = False
            sample.paused_at_monotonic = None

    def _resample_from_event(self, source: OutputSource) -> None:
        try:
            if source == "stream":
                self._sample_stream()
            else:
                self._sample_record()
        except Exception:
            LOGGER.debug("OBS state resample failed", exc_info=True)
            self._mark_disconnected()
            self._next_attempt = self._monotonic() + self._retry_seconds

    def _mark_disconnected(self) -> None:
        """Invalidate connection/output state once after an unexpected loss."""
        with self._lock:
            was_healthy = self._healthy
            self._healthy = False
            self._connect_failure_warned = False
            self._invalidate_output_cache_locked()
        if was_healthy:
            LOGGER.info("OBS WebSocket disconnected")

    def _invalidate_output_cache_locked(self) -> None:
        self._stream = _OutputSample()
        self._record = _OutputSample()
        self._last_selected_source = None

    def _sample_stream(self) -> None:
        with self._request_lock:
            client = self._request_client
            if client is None:
                raise ConnectionError("OBS request client is unavailable")
            status = client.get_stream_status()
            sampled_at = self._monotonic()
        self._update_sample(
            self._stream,
            active=bool(status.output_active),
            duration_ms=int(status.output_duration),
            sampled_at=sampled_at,
            paused=False,
        )

    def _sample_record(self) -> None:
        with self._request_lock:
            client = self._request_client
            if client is None:
                raise ConnectionError("OBS request client is unavailable")
            status = client.get_record_status()
            sampled_at = self._monotonic()
        self._update_sample(
            self._record,
            active=bool(status.output_active),
            duration_ms=int(status.output_duration),
            sampled_at=sampled_at,
            paused=bool(getattr(status, "output_paused", False)),
        )

    def _update_sample(
        self,
        sample: _OutputSample,
        *,
        active: bool,
        duration_ms: int,
        sampled_at: float,
        paused: bool,
    ) -> None:
        with self._lock:
            sample.active = active
            if active or sample.sampled_at_monotonic is None:
                sample.duration_ms = duration_ms
                sample.sampled_at_monotonic = sampled_at
            sample.paused = active and paused
            sample.paused_at_monotonic = sampled_at if sample.paused else None

    def disconnect(self) -> None:
        """Close both OBS WebSockets. Repeated calls are safe."""
        self._close_clients(log_success=True)

    def _close_clients(self, *, log_success: bool) -> None:
        with self._lock:
            request_client, self._request_client = self._request_client, None
            event_client, self._event_client = self._event_client, None
            was_connected = request_client is not None or event_client is not None
            self._healthy = False
            self._invalidate_output_cache_locked()
        for client in (event_client, request_client):
            if client is None:
                continue
            try:
                client.disconnect()
            except Exception:
                LOGGER.exception("Error while disconnecting from OBS")
        if log_success and was_connected:
            LOGGER.info("Disconnected from OBS WebSocket")


class MockClock:
    """Synthetic monotonic timeline for replay development without OBS."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._started_at: float | None = None

    @property
    def connected(self) -> bool:
        return self._started_at is not None

    def connect(self) -> bool:
        if self._started_at is None:
            self._started_at = self._monotonic()
            LOGGER.info("Using synthetic monotonic video clock")
        return True

    def active_source(self) -> str | None:
        return "mock" if self.connected else None

    def is_output_active(self) -> bool:
        return self.connected

    def now_ms(self) -> int | None:
        if self._started_at is None:
            return None
        return max(0, int((self._monotonic() - self._started_at) * 1000))

    def read(self) -> ClockReading:
        self.connect()
        return ClockReading(
            video_ms=self.now_ms(),
            output_active=True,
            source="mock",
        )

    def disconnect(self) -> None:
        self._started_at = None
