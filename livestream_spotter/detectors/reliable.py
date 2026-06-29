"""Reliable Phase 2 detectors over adjacent telemetry snapshots."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from livestream_spotter.detectors.debounce import rising_edge
from livestream_spotter.events import EventIntent

Snapshot = Mapping[str, Any]
Detector = Callable[[Snapshot, Snapshot], list[EventIntent]]

# Values defined by the iRacing SDK telemetry contract.
STATE_RACING = 4
STATE_CHECKERED = 5

FLAG_CHECKERED = 0x00000001
FLAG_WHITE = 0x00000002
FLAG_ONE_LAP_TO_GREEN = 0x00000200
FLAG_GREEN_HELD = 0x00000400
FLAG_CAUTION = 0x00004000
FLAG_CAUTION_WAVING = 0x00008000

CAUTION_MASK = FLAG_CAUTION | FLAG_CAUTION_WAVING
RESTART_MASK = FLAG_ONE_LAP_TO_GREEN | FLAG_GREEN_HELD


def _int_value(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _lap(snapshot: Snapshot) -> int | None:
    value = _int_value(snapshot.get("Lap"))
    return value if value is not None and value >= 0 else None


def _flags(snapshot: Snapshot) -> int:
    return _int_value(snapshot.get("SessionFlags")) or 0


def _session_type(snapshot: Snapshot) -> str | None:
    session_num = snapshot.get("SessionNum")
    session_info = snapshot.get("SessionInfo")
    if not isinstance(session_info, Mapping):
        return None
    sessions = session_info.get("Sessions")
    if not isinstance(sessions, Sequence) or isinstance(sessions, (str, bytes)):
        return None
    for session in sessions:
        if isinstance(session, Mapping) and session.get("SessionNum") == session_num:
            value = session.get("SessionType")
            return str(value) if value is not None else None
    return None


def is_race_session(snapshot: Snapshot) -> bool:
    session_type = _session_type(snapshot)
    return session_type is not None and session_type.casefold() == "race"


def detect_session_transition(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    previous_num = _int_value(previous.get("SessionNum"))
    current_num = _int_value(current.get("SessionNum"))
    if previous_num is None or current_num is None or previous_num == current_num:
        return []

    previous_type = _session_type(previous)
    current_type = _session_type(current)
    label = f"{current_type} session" if current_type else "Session transition"
    return [
        EventIntent(
            event_type="session_transition",
            label=label,
            tier=3,
            lap=_lap(current),
            meta={
                "from_session_num": previous_num,
                "to_session_num": current_num,
                "from_session_type": previous_type,
                "to_session_type": current_type,
            },
        )
    ]


def detect_green(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    if not is_race_session(current):
        return []
    previous_state = _int_value(previous.get("SessionState"))
    current_state = _int_value(current.get("SessionState"))
    entered_racing = previous_state != STATE_RACING and current_state == STATE_RACING
    if not entered_racing:
        return []
    return [
        EventIntent(
            event_type="green",
            label="Green flag — race start",
            tier=3,
            lap=_lap(current),
            meta={},
        )
    ]


def detect_caution(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    if not rising_edge(_flags(previous), _flags(current), CAUTION_MASK):
        return []
    return [
        EventIntent(
            event_type="caution",
            label="Caution",
            tier=3,
            lap=_lap(current),
            meta={"session_flags": _flags(current)},
        )
    ]


def detect_white(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    if not is_race_session(current):
        return []
    if not rising_edge(_flags(previous), _flags(current), FLAG_WHITE):
        return []
    return [
        EventIntent(
            event_type="white",
            label="White flag — last lap",
            tier=3,
            lap=_lap(current),
            meta={"session_flags": _flags(current)},
        )
    ]


def detect_restart(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    if _int_value(current.get("SessionState")) != STATE_RACING:
        return []
    if not rising_edge(_flags(previous), _flags(current), RESTART_MASK):
        return []
    one_to_green = bool(_flags(current) & FLAG_ONE_LAP_TO_GREEN)
    return [
        EventIntent(
            event_type="restart",
            label="Restart — one lap to green" if one_to_green else "Restart",
            tier=2,
            lap=_lap(current),
            meta={"session_flags": _flags(current)},
        )
    ]


def detect_checkered(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    if not is_race_session(current):
        return []
    previous_active = (
        _int_value(previous.get("SessionState")) == STATE_CHECKERED
        or bool(_flags(previous) & FLAG_CHECKERED)
    )
    current_active = (
        _int_value(current.get("SessionState")) == STATE_CHECKERED
        or bool(_flags(current) & FLAG_CHECKERED)
    )
    if previous_active or not current_active:
        return []
    return [
        EventIntent(
            event_type="checkered",
            label="Checkered flag",
            tier=3,
            lap=_lap(current),
            meta={},
        )
    ]


def detect_pit(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    previous_on_pit_road = previous.get("OnPitRoad")
    current_on_pit_road = current.get("OnPitRoad")
    if not isinstance(previous_on_pit_road, bool) or not isinstance(
        current_on_pit_road, bool
    ):
        return []
    if previous_on_pit_road == current_on_pit_road:
        return []

    entered = current_on_pit_road
    return [
        EventIntent(
            event_type="pit_in" if entered else "pit_out",
            label="Pit in" if entered else "Pit out",
            tier=1,
            lap=_lap(current),
            meta={"on_pit_road": current_on_pit_road},
        )
    ]


PHASE_TWO_DETECTORS: dict[str, Detector] = {
    "session_transition": detect_session_transition,
    "green": detect_green,
    "caution": detect_caution,
    "white": detect_white,
    "restart": detect_restart,
    "checkered": detect_checkered,
    "pit": detect_pit,
}
