"""Pure Phase 3 detectors for player-centric race events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from livestream_spotter.detectors.reliable import STATE_RACING
from livestream_spotter.events import EventIntent

Snapshot = Mapping[str, Any]

BLACK_FLAG = 0x00010000
MEATBALL_FLAG = 0x00100000
PLAYER_PENALTY_MASK = BLACK_FLAG | MEATBALL_FLAG
MAX_ADJACENT_LAP_FRACTION = 0.10
CONTACT_INFERENCE_MAX_F2_SECONDS = 0.70


def _array_value(values: Any, index: int) -> Any:
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        if 0 <= index < len(values):
            return values[index]
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _valid_int(value: Any, *, positive: bool = False) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if positive and value <= 0:
        return None
    return value


def _lap(snapshot: Snapshot) -> int | None:
    value = _valid_int(snapshot.get("Lap"))
    return value if value is not None and value >= 0 else None


def player_index(snapshot: Snapshot) -> int | None:
    driver_info = snapshot.get("DriverInfo")
    if not isinstance(driver_info, Mapping):
        return None
    value = _valid_int(driver_info.get("DriverCarIdx"))
    return value if value is not None and value >= 0 else None


def player_identity(snapshot: Snapshot) -> tuple[int, str] | None:
    index = player_index(snapshot)
    driver_info = snapshot.get("DriverInfo")
    if index is None or not isinstance(driver_info, Mapping):
        return None
    drivers = driver_info.get("Drivers")
    if not isinstance(drivers, Sequence) or isinstance(drivers, (str, bytes)):
        return None
    for driver in drivers:
        if isinstance(driver, Mapping) and driver.get("CarIdx") == index:
            name = driver.get("UserName")
            return (index, str(name)) if name is not None else None
    return None


def _player_class_id(snapshot: Snapshot) -> int | None:
    index = player_index(snapshot)
    driver_info = snapshot.get("DriverInfo")
    if index is None or not isinstance(driver_info, Mapping):
        return None
    drivers = driver_info.get("Drivers")
    if not isinstance(drivers, Sequence):
        return None
    for driver in drivers:
        if isinstance(driver, Mapping) and driver.get("CarIdx") == index:
            return _valid_int(driver.get("CarClassID"))
    return None


def _is_multiclass(snapshot: Snapshot) -> bool:
    player_class_id = _player_class_id(snapshot)
    driver_info = snapshot.get("DriverInfo")
    if player_class_id is None or not isinstance(driver_info, Mapping):
        return False
    drivers = driver_info.get("Drivers")
    if not isinstance(drivers, Sequence):
        return False
    return any(
        isinstance(driver, Mapping)
        and _valid_int(driver.get("CarClassID")) not in {None, player_class_id}
        for driver in drivers
    )


def _position_source(snapshot: Snapshot) -> tuple[str, str, str]:
    if _is_multiclass(snapshot):
        class_position = _valid_int(
            snapshot.get("PlayerCarClassPosition"), positive=True
        )
        if class_position is not None:
            return "class", "PlayerCarClassPosition", "CarIdxClassPosition"
    return "overall", "PlayerCarPosition", "CarIdxPosition"


def _car_indices_in_positions(
    snapshot: Snapshot,
    array_field: str,
    low_position: int,
    high_position: int,
    excluded_index: int | None,
) -> list[int]:
    positions = snapshot.get(array_field)
    if not isinstance(positions, Sequence):
        return []
    return [
        index
        for index, value in enumerate(positions)
        if index != excluded_index
        and (position := _valid_int(value, positive=True)) is not None
        and low_position <= position <= high_position
    ]


def _car_on_pit_road(snapshot: Snapshot, car_index: int) -> bool:
    return _array_value(snapshot.get("CarIdxOnPitRoad"), car_index) is True


def _player_in_pit_cycle(snapshot: Snapshot, index: int | None) -> bool:
    if snapshot.get("OnPitRoad") is True or snapshot.get("PitstopActive") is True:
        return True
    return index is not None and _car_on_pit_road(snapshot, index)


def _signed_lap_delta(target_pct: float, player_pct: float) -> float:
    return ((target_pct - player_pct + 0.5) % 1.0) - 0.5


def _on_track_nearby(snapshot: Snapshot, player_idx: int, target_idx: int) -> bool | None:
    distances = snapshot.get("CarIdxLapDistPct")
    player_pct = _number(_array_value(distances, player_idx))
    target_pct = _number(_array_value(distances, target_idx))
    if player_pct is None or target_pct is None:
        return None
    if not 0.0 <= player_pct < 1.0 or not 0.0 <= target_pct < 1.0:
        return False
    return abs(_signed_lap_delta(target_pct, player_pct)) <= MAX_ADJACENT_LAP_FRACTION


def _opponent_full_name(snapshot: Snapshot, car_idx: int | None) -> str | None:
    lookup = snapshot.get("DriverLookup")
    if car_idx is None or not isinstance(lookup, Mapping):
        return None
    driver = lookup.get(car_idx)
    if not isinstance(driver, Mapping):
        return None
    name = driver.get("name")
    if name is not None and str(name).strip():
        return str(name).strip()
    return None


def _opponent_label(snapshot: Snapshot, car_idx: int | None, generic: str) -> str:
    full_name = _opponent_full_name(snapshot, car_idx)
    if full_name is not None:
        return full_name
    lookup = snapshot.get("DriverLookup")
    if car_idx is None or not isinstance(lookup, Mapping):
        return generic
    driver = lookup.get(car_idx)
    if not isinstance(driver, Mapping):
        return generic
    number = driver.get("number")
    if number is not None and str(number).strip():
        number_text = str(number).strip()
        return number_text if number_text.startswith("#") else f"#{number_text}"
    return generic


def detect_overtake(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    scope, scalar_field, array_field = _position_source(current)
    previous_position = _valid_int(previous.get(scalar_field), positive=True)
    current_position = _valid_int(current.get(scalar_field), positive=True)
    if (
        previous_position is None
        or current_position is None
        or previous_position == current_position
    ):
        return []

    player_idx = player_index(current)
    if _player_in_pit_cycle(previous, player_idx) or _player_in_pit_cycle(
        current, player_idx
    ):
        return []

    if current_position < previous_position:
        low, high = current_position, previous_position - 1
        event_type = "overtake"
        generic_opponent = "car ahead"
    else:
        low, high = previous_position + 1, current_position
        event_type = "overtaken"
        generic_opponent = "car behind"

    target_indices = _car_indices_in_positions(
        previous, array_field, low, high, player_idx
    )
    for target_idx in target_indices:
        if _car_on_pit_road(previous, target_idx) or _car_on_pit_road(
            current, target_idx
        ):
            return []

    proximity_results: list[bool] = []
    if player_idx is not None:
        for target_idx in target_indices:
            nearby = _on_track_nearby(current, player_idx, target_idx)
            if nearby is not None:
                proximity_results.append(nearby)
    if proximity_results and not any(proximity_results):
        return []

    previous_positions = previous.get(array_field)
    opponent_idx = next(
        (
            target_idx
            for target_idx in target_indices
            if _valid_int(
                _array_value(previous_positions, target_idx), positive=True
            )
            == low
        ),
        target_indices[0] if target_indices else None,
    )
    opponent = _opponent_label(current, opponent_idx, generic_opponent)
    opponent_name = _opponent_full_name(current, opponent_idx)
    if event_type == "overtake":
        label = f"Passed {opponent} for P{current_position}"
    else:
        label = f"Passed by {opponent} — P{current_position}"

    return [
        EventIntent(
            event_type=event_type,
            label=label,
            tier=1,
            lap=_lap(current),
            meta={
                "from_position": previous_position,
                "to_position": current_position,
                "position_scope": scope,
                "other_car_indices": target_indices,
                "opponent_name": opponent_name,
            },
        )
    ]


def detect_incident(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    previous_count = _valid_int(previous.get("PlayerCarMyIncidentCount"))
    current_count = _valid_int(current.get("PlayerCarMyIncidentCount"))
    if previous_count is None or current_count is None:
        return []
    delta = current_count - previous_count
    if delta <= 0:
        return []
    if delta >= 4:
        label = "Major incident"
        severity = "major"
    elif delta >= 2:
        label = "Contact"
        severity = "contact"
    else:
        label = "Track limits / minor"
        severity = "minor"

    meta: dict[str, Any] = {
        "incident_delta": delta,
        "severity": severity,
    }
    if delta in {2, 4}:
        battle_candidates = detect_battle(previous, current)
        if battle_candidates:
            candidate = battle_candidates[0]
            gap = abs(float(candidate.meta["gap_seconds"]))
            if gap <= CONTACT_INFERENCE_MAX_F2_SECONDS:
                other_car_idx = int(candidate.meta["other_car_idx"])
                relation = str(candidate.meta["relation"])
                opponent = _opponent_label(
                    current, other_car_idx, f"car {relation}"
                )
                opponent_name = _opponent_full_name(current, other_car_idx)
                label = f"Contact with {opponent}"
                meta.update(
                    {
                        "other_car_idx": other_car_idx,
                        "opponent_name": opponent_name,
                        "inferred_contact": True,
                    }
                )
    return [
        EventIntent(
            event_type="incident",
            label=label,
            tier=1,
            lap=_lap(current),
            meta=meta,
        )
    ]


def detect_tow(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    previous_time = _number(previous.get("PlayerCarTowTime"))
    current_time = _number(current.get("PlayerCarTowTime"))
    if current_time is None or current_time <= 0:
        return []
    if previous_time is not None and previous_time > 0:
        return []
    return [
        EventIntent(
            event_type="tow",
            label="Towed to pits",
            tier=1,
            lap=_lap(current),
            meta={"tow_time": current_time},
        )
    ]


def detect_battle(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    if current.get("SessionState") != STATE_RACING:
        return []
    player_idx = player_index(current)
    positions = current.get("CarIdxPosition")
    if player_idx is None or not isinstance(positions, Sequence):
        return []
    player_position = _valid_int(_array_value(positions, player_idx), positive=True)
    if player_position is None or _player_in_pit_cycle(current, player_idx):
        return []

    laps = current.get("CarIdxLap")
    distances = current.get("CarIdxLapDistPct")
    f2_times = current.get("CarIdxF2Time")
    player_lap = _valid_int(_array_value(laps, player_idx))
    player_f2 = _number(_array_value(f2_times, player_idx))
    if player_lap is None or player_lap < 0 or player_f2 is None:
        return []

    candidates: list[tuple[float, str, int, float]] = []
    for relation, wanted_position in (
        ("ahead", player_position - 1),
        ("behind", player_position + 1),
    ):
        if wanted_position <= 0:
            continue
        target_idx = next(
            (
                index
                for index, position in enumerate(positions)
                if _valid_int(position, positive=True) == wanted_position
            ),
            None,
        )
        if target_idx is None or _car_on_pit_road(current, target_idx):
            continue
        target_lap = _valid_int(_array_value(laps, target_idx))
        if target_lap is None or target_lap < 0 or target_lap != player_lap:
            continue
        if _on_track_nearby(current, player_idx, target_idx) is not True:
            continue
        target_f2 = _number(_array_value(f2_times, target_idx))
        if target_f2 is None:
            continue
        gap = target_f2 - player_f2
        if (relation == "ahead" and gap >= 0) or (relation == "behind" and gap <= 0):
            continue
        target_pct = _number(_array_value(distances, target_idx))
        player_pct = _number(_array_value(distances, player_idx))
        if target_pct is None or player_pct is None:
            continue
        proximity = abs(_signed_lap_delta(target_pct, player_pct))
        candidates.append((abs(gap), relation, target_idx, proximity))

    if not candidates:
        return []
    _, relation, target_idx, proximity = min(candidates)
    target_f2 = _number(_array_value(f2_times, target_idx))
    assert target_f2 is not None
    gap = target_f2 - player_f2
    opponent = _opponent_label(current, target_idx, f"car {relation}")
    opponent_name = _opponent_full_name(current, target_idx)
    return [
        EventIntent(
            event_type="battle",
            label=f"Battle with {opponent}",
            tier=1,
            lap=_lap(current),
            meta={
                "other_car_idx": target_idx,
                "relation": relation,
                "gap_seconds": gap,
                "lap_distance_fraction": proximity,
                "opponent_name": opponent_name,
            },
        )
    ]


def detect_player_flag(previous: Snapshot, current: Snapshot) -> list[EventIntent]:
    index = player_index(current)
    if index is None:
        return []
    previous_flags = _valid_int(
        _array_value(previous.get("CarIdxSessionFlags"), index)
    ) or 0
    current_flags = _valid_int(
        _array_value(current.get("CarIdxSessionFlags"), index)
    ) or 0
    previous_active = bool(previous_flags & PLAYER_PENALTY_MASK)
    current_active = current_flags & PLAYER_PENALTY_MASK
    if previous_active or not current_active:
        return []
    if current_active == BLACK_FLAG:
        label = "Black flag"
    elif current_active == MEATBALL_FLAG:
        label = "Meatball flag"
    else:
        label = "Black / meatball flag"
    return [
        EventIntent(
            event_type="player_flag",
            label=label,
            tier=1,
            lap=_lap(current),
            meta={"player_car_idx": index, "session_flags": current_flags},
        )
    ]
