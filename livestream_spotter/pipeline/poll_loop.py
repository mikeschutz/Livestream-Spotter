"""Phase 2 telemetry -> detector -> Event bus pipeline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
import math
import time
from typing import Any, NamedTuple

from livestream_spotter.detectors.player import player_identity
from livestream_spotter.detectors.reliable import (
    Detector,
    STATE_RACING,
    is_race_session,
)
from livestream_spotter.events import Event, EventIntent
from livestream_spotter.iracing import IRacingClient
from livestream_spotter.obs.clock import VideoClock
from livestream_spotter.pipeline.bus import EventBus
from livestream_spotter.pipeline.snapshot import (
    TELEMETRY_FIELDS,
    SnapshotEnricher,
    build_raw_record,
)
from livestream_spotter.sinks.base import EventSink, RawSink

LOGGER = logging.getLogger(__name__)

RACE_ONLY_EVENT_TYPES = {
    "pit_in",
    "pit_out",
    "overtake",
    "overtaken",
    "incident",
    "tow",
    "battle",
    "player_flag",
}


class _PendingIntent(NamedTuple):
    intent: EventIntent
    session_time: float


def _debug_session_time(snapshot: Mapping[str, Any]) -> float:
    value = snapshot.get("SessionTime")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return math.nan


class PollLoop:
    def __init__(
        self,
        iracing: IRacingClient,
        clock: VideoClock,
        bus: EventBus,
        event_sink: EventSink,
        detectors: Sequence[Detector],
        lead_in_ms: Mapping[str, int],
        poll_hz: float,
        hold_until_stream_active: bool,
        raw_sink: RawSink | None = None,
        raw_dump_hz: float = 1.0,
        monotonic=time.monotonic,
        race_only_player_events: bool = True,
        battle_gap_threshold: float = 0.6,
        battle_min_duration: float = 9.0,
        battle_throttle_window: float = 60.0,
        incident_minimum_delta: int = 1,
    ) -> None:
        self._iracing = iracing
        self._clock = clock
        self._bus = bus
        self._event_sink = event_sink
        self._detectors = tuple(detectors)
        self._lead_in_ms = dict(lead_in_ms)
        self._interval = 1.0 / poll_hz
        self._hold_until_stream_active = hold_until_stream_active
        self._raw_sink = raw_sink
        self._raw_interval = 1.0 / raw_dump_hz
        self._monotonic = monotonic
        self._race_only_player_events = race_only_player_events
        self._battle_gap_threshold = battle_gap_threshold
        self._battle_min_duration = battle_min_duration
        self._battle_throttle_window = battle_throttle_window
        self._incident_minimum_delta = incident_minimum_delta
        self._next_raw_dump_at: float | None = None
        self._snapshot_enricher = SnapshotEnricher()
        self._previous: Mapping[str, Any] | None = None
        self._session_num: Any = None
        self._held: list[_PendingIntent] = []
        self._race_started = False
        self._caution_since_green = False
        self._checkered_emitted = False
        self._battle_car_idx: int | None = None
        self._battle_started_at: float | None = None
        self._battle_emitted = False
        self._battle_last_emitted_at: float | None = None
        self._reported_player_identity = False
        self._reported_holding = False
        self._reported_track_wetness_absent = False

    def tick(self) -> bool:
        """Process one telemetry sample; return whether any Event was emitted."""
        emitted = self._stamp_and_publish([]) if self._held else 0

        if not self._iracing.connect():
            self._previous = None
            return bool(emitted)

        current = self._iracing.capture(TELEMETRY_FIELDS)
        if current is None:
            self._previous = None
            return bool(emitted)
        current = self._snapshot_enricher.enrich(current)

        self._report_player_identity(current)
        current_session_num = current.get("SessionNum")
        session_changed = (
            self._session_num is not None
            and current_session_num != self._session_num
        )
        if session_changed:
            self._reset_race_phase()
        self._session_num = current_session_num

        self._write_raw_if_due(current)

        previous, self._previous = self._previous, current
        if previous is None:
            self._reset_active_battle()
            self._race_started = (
                is_race_session(current)
                and current.get("SessionState") == STATE_RACING
            )
            self._caution_since_green = False
            return bool(emitted)

        intentions: list[EventIntent] = []
        for detector in self._detectors:
            intentions.extend(detector(previous, current))
        intentions = self._apply_pipeline_gates(
            previous,
            current,
            intentions,
            session_changed=session_changed,
        )
        emitted += self._stamp_and_publish(intentions, current)
        return bool(emitted)

    def _apply_pipeline_gates(
        self,
        previous: Mapping[str, Any],
        current: Mapping[str, Any],
        intentions: Sequence[EventIntent],
        *,
        session_changed: bool,
    ) -> list[EventIntent]:
        """Apply stateful race policy without making detectors stateful."""
        restart_allowed = self._race_started and self._caution_since_green
        entered_racing = (
            is_race_session(current)
            and (
                session_changed
                or previous.get("SessionState") != STATE_RACING
            )
            and current.get("SessionState") == STATE_RACING
        )
        if entered_racing:
            self._race_started = True
            self._caution_since_green = False

        gated = [
            intent
            for intent in intentions
            if intent.event_type != "restart" or restart_allowed
        ]

        checkered_gated: list[EventIntent] = []
        for intent in gated:
            if intent.event_type == "checkered":
                if self._checkered_emitted:
                    continue
                self._checkered_emitted = True
            checkered_gated.append(intent)
        gated = checkered_gated

        if (
            self._race_started
            and any(intent.event_type == "caution" for intent in intentions)
        ):
            self._caution_since_green = True
        if restart_allowed and any(
            intent.event_type == "restart" for intent in intentions
        ):
            self._caution_since_green = False

        if self._race_only_player_events and not self._race_started:
            gated = [
                intent
                for intent in gated
                if intent.event_type not in RACE_ONLY_EVENT_TYPES
            ]

        gated = [
            intent
            for intent in gated
            if intent.event_type != "incident"
            or int(intent.meta.get("incident_delta", 0))
            >= self._incident_minimum_delta
        ]
        return self._gate_battle_intentions(gated, current)

    def _gate_battle_intentions(
        self,
        intentions: Sequence[EventIntent],
        current: Mapping[str, Any],
    ) -> list[EventIntent]:
        others = [intent for intent in intentions if intent.event_type != "battle"]
        candidates = [
            intent
            for intent in intentions
            if intent.event_type == "battle"
            and abs(float(intent.meta.get("gap_seconds", math.inf)))
            <= self._battle_gap_threshold
        ]
        if not self._race_started or not candidates:
            self._reset_active_battle()
            return others

        candidate = min(
            candidates,
            key=lambda intent: abs(float(intent.meta["gap_seconds"])),
        )
        car_idx = int(candidate.meta["other_car_idx"])
        # SessionTime is safe for detector duration only; Event.video_ms still
        # comes exclusively from the OBS clock in _stamp_and_publish().
        session_time = _debug_session_time(current)
        if math.isnan(session_time):
            self._reset_active_battle()
            return others

        if (
            self._battle_car_idx != car_idx
            or self._battle_started_at is None
            or session_time < self._battle_started_at
        ):
            self._battle_car_idx = car_idx
            self._battle_started_at = session_time
            self._battle_emitted = False
            return others

        if self._battle_emitted:
            return others
        if session_time - self._battle_started_at < self._battle_min_duration:
            return others
        if (
            self._battle_last_emitted_at is not None
            and session_time - self._battle_last_emitted_at
            < self._battle_throttle_window
        ):
            return others

        self._battle_emitted = True
        self._battle_last_emitted_at = session_time
        return [*others, candidate]

    def _reset_active_battle(self) -> None:
        self._battle_car_idx = None
        self._battle_started_at = None
        self._battle_emitted = False

    def _reset_race_phase(self) -> None:
        self._race_started = False
        self._caution_since_green = False
        self._checkered_emitted = False
        self._reset_active_battle()
        self._battle_last_emitted_at = None

    def _report_player_identity(self, snapshot: Mapping[str, Any]) -> None:
        if self._reported_player_identity:
            return
        identity = player_identity(snapshot)
        if identity is None:
            return
        LOGGER.info("Resolved player car index=%d driver=%s", identity[0], identity[1])
        self._reported_player_identity = True

    def run(self, once: bool = False) -> None:
        try:
            while True:
                started = self._monotonic()
                self.tick()
                if once:
                    return
                time.sleep(max(0.0, self._interval - (self._monotonic() - started)))
        finally:
            self._drain_bus()

    def _stamp_and_publish(
        self,
        intentions: Sequence[EventIntent],
        snapshot: Mapping[str, Any] | None = None,
    ) -> int:
        pending = list(self._held)
        pending.extend(
            _PendingIntent(intent, _debug_session_time(snapshot or {}))
            for intent in intentions
        )
        if not pending:
            return 0

        reading = self._clock.read()
        if not reading.output_active or reading.video_ms is None:
            if self._hold_until_stream_active:
                self._held = pending
                if not self._reported_holding:
                    LOGGER.info("Holding Events until the OBS stream is active")
                    self._reported_holding = True
            else:
                self._held.clear()
            return 0

        self._held.clear()
        self._reported_holding = False
        for item in pending:
            intent = item.intent
            self._bus.publish(
                Event(
                    video_ms=reading.video_ms,
                    event_type=intent.event_type,
                    label=intent.label,
                    tier=intent.tier,
                    lap=intent.lap,
                    session_time=item.session_time,
                    lead_in_ms=self._lead_in_ms.get(intent.event_type, 0),
                    meta=dict(intent.meta),
                )
            )
        self._drain_bus()
        return len(pending)

    def _write_raw_if_due(self, values: Mapping[str, Any]) -> None:
        if self._raw_sink is None:
            return
        now = self._monotonic()
        if self._next_raw_dump_at is not None and now < self._next_raw_dump_at:
            return
        self._next_raw_dump_at = now + self._raw_interval
        record = build_raw_record(values, self._clock.read())
        track_wetness = record["iracing"]["track_wetness"]
        if not track_wetness["present"] and not self._reported_track_wetness_absent:
            LOGGER.warning("TrackWetness is absent from this iRacing build/session")
            self._reported_track_wetness_absent = True
        elif track_wetness["present"]:
            self._reported_track_wetness_absent = False
        self._raw_sink.write(record)

    def _drain_bus(self) -> None:
        while (event := self._bus.take_nowait()) is not None:
            self._event_sink.write(event)
