"""Resilient lifecycle wrapper for the iRacing shared-memory SDK."""

from __future__ import annotations

from collections.abc import Callable
import logging
import time
from typing import Any

LOGGER = logging.getLogger(__name__)


def _default_sdk_factory() -> Any:
    import irsdk

    return irsdk.IRSDK()


class IRacingClient:
    """Own an IRSDK instance without treating sim absence as an error."""

    def __init__(
        self,
        sdk_factory: Callable[[], Any] = _default_sdk_factory,
        monotonic: Callable[[], float] = time.monotonic,
        retry_seconds: float = 2.0,
    ) -> None:
        self._sdk_factory = sdk_factory
        self._monotonic = monotonic
        self._retry_seconds = retry_seconds
        self._sdk: Any | None = None
        self._reported_waiting = False
        self._next_attempt = 0.0

    @property
    def connected(self) -> bool:
        if self._sdk is None:
            return False
        try:
            return bool(self._sdk.is_connected)
        except Exception:
            LOGGER.exception("iRacing connection check failed; reconnecting")
            self.disconnect()
            return False

    def connect(self) -> bool:
        """Connect if possible; return False while no session is available."""
        if self.connected:
            return True

        now = self._monotonic()
        if now < self._next_attempt:
            return False
        self._next_attempt = now + self._retry_seconds

        self.disconnect()
        try:
            sdk = self._sdk_factory()
            if not sdk.startup():
                sdk.shutdown()
                if not self._reported_waiting:
                    LOGGER.info(
                        "Waiting for iRacing session (start iRacing when ready)"
                    )
                    self._reported_waiting = True
                return False
            self._sdk = sdk
            self._next_attempt = 0.0
            self._reported_waiting = False
            LOGGER.info("Connected to iRacing telemetry")
            return True
        except Exception:
            LOGGER.exception("Could not connect to iRacing; will retry")
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Release shared memory. Repeated calls are safe."""
        sdk, self._sdk = self._sdk, None
        if sdk is None:
            return
        try:
            sdk.shutdown()
        except Exception:
            LOGGER.exception("Error while disconnecting from iRacing")
        else:
            LOGGER.info("Disconnected from iRacing telemetry")

    def read(self, field: str) -> Any:
        if not self.connected:
            return None
        try:
            return self._sdk[field]
        except Exception:
            LOGGER.exception("Failed reading iRacing field %s; reconnecting", field)
            self.disconnect()
            return None

    def capture(self, fields: tuple[str, ...]) -> dict[str, Any] | None:
        """Freeze one telemetry buffer and copy the requested fields."""
        if not self.connected:
            return None
        try:
            self._sdk.freeze_var_buffer_latest()
            return {field: self._sdk[field] for field in fields}
        except Exception:
            LOGGER.exception("Failed capturing iRacing telemetry; reconnecting")
            self.disconnect()
            return None
        finally:
            if self._sdk is not None:
                try:
                    self._sdk.unfreeze_var_buffer_latest()
                except Exception:
                    LOGGER.exception("Failed releasing iRacing telemetry buffer")
                    self.disconnect()


def is_iracing_session_active(ir: IRacingClient) -> bool:
    """Return the single authoritative iRacing active-session signal."""
    return ir.connected
