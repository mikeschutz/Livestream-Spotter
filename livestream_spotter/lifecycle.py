"""Application lifecycle state machine and main-loop supervisor."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
import logging
import threading
import time
from typing import Protocol

from livestream_spotter.iracing import IRacingClient, is_iracing_session_active
from livestream_spotter.obs.clock import VideoClock

LOGGER = logging.getLogger(__name__)

OBS_RETRY_INTERVAL_SECONDS = 10.0


class LifecycleState(StrEnum):
    IDLE = "IDLE"
    WAITING_FOR_OBS = "WAITING_FOR_OBS"
    WAITING_FOR_IRACING = "WAITING_FOR_IRACING"
    ACTIVE = "ACTIVE"


class LifecycleTrigger(StrEnum):
    IRACING_SESSION_START = "iracing_session_start"
    IRACING_SESSION_END = "iracing_session_end"
    OBS_CONNECT = "obs_connect"
    OBS_DISCONNECT = "obs_disconnect"
    STARTUP = "startup"


class LifecyclePipeline(Protocol):
    def activate(self) -> None: ...

    def deactivate(self) -> None: ...

    def reset_session(self) -> None: ...

    def tick(self) -> bool: ...


TransitionResolver = Callable[[bool, bool], LifecycleState]
TransitionCallback = Callable[[LifecycleState, LifecycleState], None]


def _idle_session_start(
    _iracing_active: bool,
    obs_connected: bool,
) -> LifecycleState:
    return (
        LifecycleState.ACTIVE
        if obs_connected
        else LifecycleState.WAITING_FOR_OBS
    )


def _waiting_for_iracing_session_start(
    _iracing_active: bool,
    _obs_connected: bool,
) -> LifecycleState:
    return LifecycleState.ACTIVE


def _waiting_for_iracing_obs_disconnect(
    _iracing_active: bool,
    _obs_connected: bool,
) -> LifecycleState:
    return LifecycleState.IDLE


def _waiting_for_obs_connect(
    _iracing_active: bool,
    _obs_connected: bool,
) -> LifecycleState:
    return LifecycleState.ACTIVE


def _waiting_for_obs_session_end(
    _iracing_active: bool,
    _obs_connected: bool,
) -> LifecycleState:
    return LifecycleState.IDLE


def _active_session_end(
    _iracing_active: bool,
    obs_connected: bool,
) -> LifecycleState:
    return (
        LifecycleState.WAITING_FOR_IRACING
        if obs_connected
        else LifecycleState.IDLE
    )


def _active_obs_disconnect(
    iracing_active: bool,
    _obs_connected: bool,
) -> LifecycleState:
    return (
        LifecycleState.WAITING_FOR_OBS
        if iracing_active
        else LifecycleState.IDLE
    )


# This table contains exactly the non-startup arrows in SPEC.md's state diagram.
TRANSITION_TABLE: dict[
    LifecycleState,
    dict[LifecycleTrigger, TransitionResolver],
] = {
    LifecycleState.IDLE: {
        LifecycleTrigger.IRACING_SESSION_START: _idle_session_start,
    },
    LifecycleState.WAITING_FOR_IRACING: {
        LifecycleTrigger.IRACING_SESSION_START: (
            _waiting_for_iracing_session_start
        ),
        LifecycleTrigger.OBS_DISCONNECT: _waiting_for_iracing_obs_disconnect,
    },
    LifecycleState.WAITING_FOR_OBS: {
        LifecycleTrigger.OBS_CONNECT: _waiting_for_obs_connect,
        LifecycleTrigger.IRACING_SESSION_END: _waiting_for_obs_session_end,
    },
    LifecycleState.ACTIVE: {
        LifecycleTrigger.IRACING_SESSION_END: _active_session_end,
        LifecycleTrigger.OBS_DISCONNECT: _active_obs_disconnect,
    },
}


class LifecycleStateMachine:
    """Thread-safe implementation of the lifecycle graph in SPEC.md."""

    def __init__(
        self,
        *,
        iracing_active: bool,
        obs_connected: bool,
        on_transition: TransitionCallback | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._on_transition = on_transition
        self._state = self._startup_state(iracing_active, obs_connected)
        LOGGER.info(
            "Lifecycle: STARTUP → %s (trigger: %s)",
            self._state,
            LifecycleTrigger.STARTUP,
        )

    @staticmethod
    def _startup_state(
        iracing_active: bool,
        obs_connected: bool,
    ) -> LifecycleState:
        if iracing_active and obs_connected:
            return LifecycleState.ACTIVE
        if iracing_active:
            return LifecycleState.WAITING_FOR_OBS
        if obs_connected:
            return LifecycleState.WAITING_FOR_IRACING
        return LifecycleState.IDLE

    @property
    def current_state(self) -> LifecycleState:
        with self._lock:
            return self._state

    def on_signal(
        self,
        signal_name: LifecycleTrigger | str,
        *,
        iracing_active: bool,
        obs_connected: bool,
    ) -> LifecycleState:
        trigger = LifecycleTrigger(signal_name)
        if trigger is LifecycleTrigger.STARTUP:
            return self.current_state

        with self._lock:
            from_state = self._state
            resolver = TRANSITION_TABLE.get(from_state, {}).get(trigger)
            if resolver is None:
                to_state = from_state
            else:
                to_state = resolver(iracing_active, obs_connected)
                if to_state != from_state:
                    self._state = to_state

        if to_state == from_state:
            LOGGER.debug(
                "Lifecycle signal ignored in %s: %s",
                from_state,
                trigger,
            )
            return from_state

        LOGGER.info(
            "Lifecycle: %s → %s (trigger: %s)",
            from_state,
            to_state,
            trigger,
        )
        if self._on_transition is not None:
            self._on_transition(from_state, to_state)
        return to_state


def _is_obs_connected(clock: VideoClock) -> bool:
    connected = getattr(clock, "connected", False)
    return bool(connected() if callable(connected) else connected)


class LifecycleController:
    """Drive lifecycle signals and the active-only telemetry pipeline."""

    def __init__(
        self,
        *,
        iracing: IRacingClient,
        clock: VideoClock,
        pipeline: LifecyclePipeline,
        poll_hz: float,
        startup_iracing_active: bool,
        startup_obs_connected: bool,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._iracing = iracing
        self._clock = clock
        self._pipeline = pipeline
        self._interval = 1.0 / poll_hz
        self._monotonic = monotonic
        self._iracing_active = startup_iracing_active
        self._obs_connected = startup_obs_connected
        startup_at = self._monotonic()
        self._last_obs_attempt_at = startup_at
        self.state_machine = LifecycleStateMachine(
            iracing_active=startup_iracing_active,
            obs_connected=startup_obs_connected,
            on_transition=self._on_transition,
        )
        self._enter_initial_state()

    @property
    def current_state(self) -> LifecycleState:
        return self.state_machine.current_state

    def _enter_initial_state(self) -> None:
        if self.current_state is LifecycleState.ACTIVE:
            self._pipeline.activate()
        elif self.current_state is LifecycleState.WAITING_FOR_OBS:
            self._log_obs_polling()

    @staticmethod
    def _log_obs_polling() -> None:
        LOGGER.debug("Polling OBS every 10s until it becomes available")

    def _on_transition(
        self,
        from_state: LifecycleState,
        to_state: LifecycleState,
    ) -> None:
        if from_state is LifecycleState.ACTIVE:
            self._pipeline.deactivate()
        if to_state is LifecycleState.WAITING_FOR_OBS:
            self._log_obs_polling()
        if to_state is LifecycleState.ACTIVE:
            self._pipeline.activate()

    def tick(self) -> bool:
        """Observe both signals, retry OBS when due, and tick only if active."""
        self._observe_iracing()
        self._observe_obs()
        self._retry_obs_if_due()
        if self.current_state is not LifecycleState.ACTIVE:
            return False
        return self._pipeline.tick()

    def _observe_iracing(self) -> None:
        observed = is_iracing_session_active(self._iracing)
        self._update_iracing_signal(observed)

        if observed:
            return
        self._iracing.connect()
        self._update_iracing_signal(is_iracing_session_active(self._iracing))

    def _update_iracing_signal(self, active: bool) -> None:
        if active == self._iracing_active:
            return
        self._iracing_active = active
        trigger = (
            LifecycleTrigger.IRACING_SESSION_START
            if active
            else LifecycleTrigger.IRACING_SESSION_END
        )
        self.state_machine.on_signal(
            trigger,
            iracing_active=active,
            obs_connected=_is_obs_connected(self._clock),
        )
        if not active:
            self._pipeline.reset_session()

    def _observe_obs(self) -> None:
        self._update_obs_signal(_is_obs_connected(self._clock))

    def _update_obs_signal(self, connected: bool) -> None:
        if connected == self._obs_connected:
            return
        self._obs_connected = connected
        trigger = (
            LifecycleTrigger.OBS_CONNECT
            if connected
            else LifecycleTrigger.OBS_DISCONNECT
        )
        self.state_machine.on_signal(
            trigger,
            iracing_active=is_iracing_session_active(self._iracing),
            obs_connected=connected,
        )

    def _retry_obs_if_due(self) -> None:
        if self.current_state is not LifecycleState.WAITING_FOR_OBS:
            return
        now = self._monotonic()
        if now - self._last_obs_attempt_at < OBS_RETRY_INTERVAL_SECONDS:
            return

        self._last_obs_attempt_at = now
        connected = self._clock.connect()
        self._update_obs_signal(bool(connected and _is_obs_connected(self._clock)))

    def run(self, once: bool = False) -> None:
        while True:
            started = self._monotonic()
            self.tick()
            if once:
                return
            time.sleep(max(0.0, self._interval - (self._monotonic() - started)))
