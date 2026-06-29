"""Pure construction of Phase 1 raw telemetry records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from livestream_spotter.obs.clock import ClockReading


TELEMETRY_FIELDS = (
    "SessionNum",
    "SessionState",
    "SessionFlags",
    "SessionLapsRemain",
    "SessionTime",
    "SessionTimeOfDay",
    "Lap",
    "LapBestLapTime",
    "LapDeltaToSessionBestLap",
    "PlayerCarPosition",
    "PlayerCarClassPosition",
    "PlayerCarMyIncidentCount",
    "PlayerCarTowTime",
    "OnPitRoad",
    "FuelLevel",
    "PlayerTireCompound",
    "PitstopActive",
    "PitSvFlags",
    "PitSvFuel",
    "PitSvTireCompound",
    "PlayerCarPitSvStatus",
    "CarIdxPosition",
    "CarIdxClassPosition",
    "CarIdxLap",
    "CarIdxLapDistPct",
    "CarIdxF2Time",
    "CarIdxOnPitRoad",
    "CarIdxSessionFlags",
    "CarIdxTireCompound",
    "TrackWetness",
    "WeatherDeclaredWet",
    "DriverInfo",
    "SessionInfo",
)

DRIVER_LOOKUP_FIELD = "DriverLookup"


def signed_lap_delta(target_pct: float, player_pct: float) -> float:
    """Return shortest signed fractional-lap delta from player to target."""
    return ((target_pct - player_pct + 0.5) % 1.0) - 0.5


def _array_value(values: Any, index: int) -> Any:
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        if 0 <= index < len(values):
            return values[index]
    return None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _player_index(driver_info: Any) -> int | None:
    if not isinstance(driver_info, Mapping):
        return None
    value = driver_info.get("DriverCarIdx")
    return value if isinstance(value, int) and value >= 0 else None


def _driver_rows(driver_info: Any) -> list[dict[str, Any]]:
    if not isinstance(driver_info, Mapping):
        return []
    drivers = driver_info.get("Drivers")
    if not isinstance(drivers, Sequence):
        return []
    rows = []
    for driver in drivers:
        if not isinstance(driver, Mapping):
            continue
        rows.append(
            {
                "car_idx": driver.get("CarIdx"),
                "user_name": driver.get("UserName"),
                "car_number": driver.get("CarNumber"),
                "car_class_id": driver.get("CarClassID"),
            }
        )
    return rows


def build_driver_lookup(driver_info: Any) -> dict[int, dict[str, Any]]:
    """Build the detector-facing roster lookup from parsed DriverInfo."""
    if not isinstance(driver_info, Mapping):
        return {}
    drivers = driver_info.get("Drivers")
    if not isinstance(drivers, Sequence) or isinstance(drivers, (str, bytes)):
        return {}
    lookup: dict[int, dict[str, Any]] = {}
    for driver in drivers:
        if not isinstance(driver, Mapping):
            continue
        car_idx = driver.get("CarIdx")
        if not isinstance(car_idx, int) or isinstance(car_idx, bool) or car_idx < 0:
            continue
        lookup[car_idx] = {
            "name": driver.get("UserName"),
            "number": driver.get("CarNumber"),
        }
    return lookup


class SnapshotEnricher:
    """Attach a roster lookup, refreshing it only when the session changes."""

    def __init__(self) -> None:
        self._initialized = False
        self._session_num: Any = None
        self._driver_lookup: dict[int, dict[str, Any]] = {}

    def enrich(self, values: Mapping[str, Any]) -> dict[str, Any]:
        session_num = values.get("SessionNum")
        if (
            not self._initialized
            or session_num != self._session_num
            or not self._driver_lookup
        ):
            self._driver_lookup = build_driver_lookup(values.get("DriverInfo"))
            self._session_num = session_num
            self._initialized = True
        enriched = dict(values)
        enriched[DRIVER_LOOKUP_FIELD] = self._driver_lookup
        return enriched


def _estimated_lap_time(driver_info: Any) -> float | None:
    if not isinstance(driver_info, Mapping):
        return None
    return _as_number(driver_info.get("DriverCarEstLapTime"))


def _session_type(session_info: Any, session_num: Any) -> str | None:
    if not isinstance(session_info, Mapping):
        return None
    sessions = session_info.get("Sessions")
    if not isinstance(sessions, Sequence):
        return None
    for session in sessions:
        if isinstance(session, Mapping) and session.get("SessionNum") == session_num:
            value = session.get("SessionType")
            return str(value) if value is not None else None
    return None


def adjacent_comparisons(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Show primary F2 gap and raw lap proximity for adjacent cars."""
    driver_info = values.get("DriverInfo")
    player_idx = _player_index(driver_info)
    positions = values.get("CarIdxPosition")
    if player_idx is None:
        return []
    player_position = _array_value(positions, player_idx)
    if not isinstance(player_position, int) or player_position <= 0:
        return []

    lap_distances = values.get("CarIdxLapDistPct")
    f2_times = values.get("CarIdxF2Time")
    player_pct = _as_number(_array_value(lap_distances, player_idx))
    player_f2 = _as_number(_array_value(f2_times, player_idx))
    drivers = {
        row["car_idx"]: row
        for row in _driver_rows(driver_info)
        if isinstance(row.get("car_idx"), int)
    }

    comparisons: list[dict[str, Any]] = []
    for relation, wanted_position in (
        ("ahead", player_position - 1),
        ("behind", player_position + 1),
    ):
        if wanted_position <= 0 or not isinstance(positions, Sequence):
            continue
        target_idx = next(
            (
                index
                for index, position in enumerate(positions)
                if position == wanted_position
            ),
            None,
        )
        if target_idx is None:
            continue

        target_pct = _as_number(_array_value(lap_distances, target_idx))
        target_f2 = _as_number(_array_value(f2_times, target_idx))
        lap_delta = (
            signed_lap_delta(target_pct, player_pct)
            if target_pct is not None and player_pct is not None
            else None
        )
        comparisons.append(
            {
                "relation": relation,
                "car_idx": target_idx,
                "position": wanted_position,
                "user_name": drivers.get(target_idx, {}).get("user_name"),
                "car_class_id": drivers.get(target_idx, {}).get("car_class_id"),
                "car_idx_f2_time": target_f2,
                "player_car_idx_f2_time": player_f2,
                "f2_delta_vs_player_seconds": (
                    target_f2 - player_f2
                    if target_f2 is not None and player_f2 is not None
                    else None
                ),
                "car_idx_lap_dist_pct": target_pct,
                "player_lap_dist_pct": player_pct,
                "signed_lap_delta_pct": lap_delta,
            }
        )
    return comparisons


def build_raw_record(
    values: Mapping[str, Any],
    clock: ClockReading,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a JSON-ready record without performing I/O."""
    captured_at = captured_at or datetime.now(UTC)
    driver_info = values.get("DriverInfo")
    session_num = values.get("SessionNum")
    track_wetness = values.get("TrackWetness")

    scalar_fields = {
        field: values.get(field)
        for field in TELEMETRY_FIELDS
        if field not in {"DriverInfo", "SessionInfo", "TrackWetness"}
    }
    return {
        "schema_version": 1,
        "captured_at_utc": captured_at.isoformat(),
        "timeline": {
            "source": clock.source,
            "output_active": clock.output_active,
            "video_ms": clock.video_ms,
            "obs_output_duration_ms": (
                clock.video_ms if clock.source == "obs" else None
            ),
        },
        "iracing": {
            "player_car_idx": _player_index(driver_info),
            "session_type": _session_type(values.get("SessionInfo"), session_num),
            "driver_car_est_lap_time": _estimated_lap_time(driver_info),
            "track_wetness": {
                "present": track_wetness is not None,
                "value": track_wetness,
            },
            "drivers": _driver_rows(driver_info),
            "telemetry": scalar_fields,
            "adjacent_cars": adjacent_comparisons(values),
        },
    }
