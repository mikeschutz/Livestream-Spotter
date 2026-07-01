"""Load application configuration through the text output renderers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import tomllib

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObsConfig:
    host: str
    port: int
    password: str


@dataclass(frozen=True)
class AppConfig:
    poll_hz: float
    timestamp_source: str
    hold_until_output_active: bool
    race_only_player_events: bool
    raw_dump_enabled: bool
    raw_dump_hz: float
    raw_dump_path: Path
    timestamps_path: Path
    chapters_path: Path
    timestamp_min_spacing_ms: int
    chapter_min_spacing_ms: int
    enabled_renderers: frozenset[str]
    lead_in_ms: dict[str, int]
    enabled_detectors: frozenset[str]
    battle_gap_threshold: float
    battle_min_duration: float
    battle_throttle_window: float
    incident_minimum_delta: int
    obs: ObsConfig


def load_config(path: Path) -> AppConfig:
    """Load and validate settings used by the current end-to-end product."""
    with path.open("rb") as config_file:
        values = tomllib.load(config_file)

    runtime = values["runtime"]
    obs = values["obs"]
    output = values["output"]
    diagnostics = values.get("diagnostics", {})
    battle = values.get("battle", {})
    incidents = values.get("incidents", {})
    timestamps = values.get("timestamps", {})
    chapters = values.get("chapters", {})
    poll_hz = float(runtime["poll_hz"])
    raw_dump_hz = float(diagnostics.get("raw_dump_hz", 1.0))
    timestamp_min_spacing = float(timestamps.get("min_spacing", 2.0))
    chapter_min_spacing = float(chapters.get("min_spacing", 10.0))
    battle_gap_threshold = float(battle.get("gap_threshold", 0.6))
    battle_min_duration = float(battle.get("min_duration", 9.0))
    battle_throttle_window = float(battle.get("throttle_window", 60.0))
    incident_minimum_delta = int(incidents.get("minimum_delta", 1))
    port = int(obs["port"])
    timestamp_source = runtime.get("timestamp_source", "auto")
    legacy_hold_present = "hold_until_stream_active" in runtime
    if legacy_hold_present:
        LOGGER.warning(
            "Config key 'hold_until_stream_active' is deprecated; use "
            "'hold_until_output_active' instead. The legacy name will be "
            "removed in 0.2.0."
        )
        if "hold_until_output_active" in runtime:
            LOGGER.warning(
                "Config keys 'hold_until_stream_active' and "
                "'hold_until_output_active' are both set; using "
                "'hold_until_output_active'."
            )
    if "hold_until_output_active" in runtime:
        hold_until_output_active = runtime["hold_until_output_active"]
    else:
        hold_until_output_active = runtime.get("hold_until_stream_active", False)

    if poll_hz <= 0:
        raise ValueError("runtime.poll_hz must be greater than zero")
    if raw_dump_hz <= 0:
        raise ValueError("diagnostics.raw_dump_hz must be greater than zero")
    if timestamp_min_spacing < 0:
        raise ValueError("timestamps.min_spacing cannot be negative")
    if chapter_min_spacing < 10.0:
        raise ValueError("chapters.min_spacing must be at least 10 seconds")
    if battle_gap_threshold <= 0:
        raise ValueError("battle.gap_threshold must be greater than zero")
    if battle_min_duration < 0 or battle_throttle_window < 0:
        raise ValueError("battle duration and throttle cannot be negative")
    if incident_minimum_delta <= 0:
        raise ValueError("incidents.minimum_delta must be greater than zero")
    if not 1 <= port <= 65535:
        raise ValueError("obs.port must be between 1 and 65535")
    if timestamp_source not in {"auto", "stream", "record"}:
        raise ValueError(
            "runtime.timestamp_source must be 'auto', 'stream', or 'record'"
        )

    output_path = Path(output["raw_dump_path"])
    if not output_path.is_absolute():
        output_path = path.resolve().parent / output_path
    timestamps_path = Path(output.get("timestamps_path", "timestamps.txt"))
    if not timestamps_path.is_absolute():
        timestamps_path = path.resolve().parent / timestamps_path
    chapters_path = Path(output.get("chapters_path", "chapters.txt"))
    if not chapters_path.is_absolute():
        chapters_path = path.resolve().parent / chapters_path

    renderer_values = output.get("renderers", ["timestamps"])
    known_renderers = {"timestamps", "chapters"}
    if not isinstance(renderer_values, list) or not all(
        isinstance(name, str) for name in renderer_values
    ):
        raise ValueError("output.renderers must be a list of names")
    unknown_renderers = set(renderer_values) - known_renderers
    if unknown_renderers:
        names = ", ".join(sorted(unknown_renderers))
        raise ValueError(f"unknown output renderer(s): {names}")

    lead_in_defaults = {
        "session_transition": 0.0,
        "green": 2.0,
        "caution": 0.0,
        "white": 0.0,
        "restart": 2.0,
        "checkered": 0.0,
        "pit_in": 0.0,
        "pit_out": 0.0,
        "overtake": 2.0,
        "overtaken": 2.0,
        "incident": 6.0,
        "tow": 6.0,
        "battle": 2.0,
        "player_flag": 0.0,
    }
    configured_lead_ins = values.get("lead_in_seconds", {})
    lead_in_ms: dict[str, int] = {}
    for event_type, default_seconds in lead_in_defaults.items():
        seconds = float(configured_lead_ins.get(event_type, default_seconds))
        if seconds < 0:
            raise ValueError(f"lead_in_seconds.{event_type} cannot be negative")
        lead_in_ms[event_type] = round(seconds * 1000)

    default_detectors = (
        "session_transition",
        "green",
        "caution",
        "white",
        "restart",
        "checkered",
        "pit",
        "overtake",
        "incident",
        "tow",
        "battle",
        "player_flag",
    )
    enabled_values = values.get("detectors", {}).get("enabled", default_detectors)
    if not isinstance(enabled_values, list) or not all(
        isinstance(name, str) for name in enabled_values
    ):
        raise ValueError("detectors.enabled must be a list of names")

    return AppConfig(
        poll_hz=poll_hz,
        timestamp_source=timestamp_source,
        hold_until_output_active=bool(hold_until_output_active),
        race_only_player_events=bool(runtime.get("race_only_player_events", True)),
        raw_dump_enabled=bool(diagnostics.get("raw_dump_enabled", False)),
        raw_dump_hz=raw_dump_hz,
        raw_dump_path=output_path,
        timestamps_path=timestamps_path,
        chapters_path=chapters_path,
        timestamp_min_spacing_ms=round(timestamp_min_spacing * 1000),
        chapter_min_spacing_ms=round(chapter_min_spacing * 1000),
        enabled_renderers=frozenset(renderer_values),
        lead_in_ms=lead_in_ms,
        enabled_detectors=frozenset(enabled_values),
        battle_gap_threshold=battle_gap_threshold,
        battle_min_duration=battle_min_duration,
        battle_throttle_window=battle_throttle_window,
        incident_minimum_delta=incident_minimum_delta,
        obs=ObsConfig(
            host=str(obs["host"]),
            port=port,
            password=str(obs["password"]),
        ),
    )
