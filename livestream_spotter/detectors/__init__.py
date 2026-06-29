"""Pure telemetry detectors."""

from livestream_spotter.detectors.player import (
    detect_battle,
    detect_incident,
    detect_overtake,
    detect_player_flag,
    detect_tow,
)
from livestream_spotter.detectors.reliable import (
    Detector,
    PHASE_TWO_DETECTORS,
    detect_caution,
    detect_checkered,
    detect_green,
    detect_pit,
    detect_restart,
    detect_session_transition,
    detect_white,
)

PHASE_THREE_DETECTORS: dict[str, Detector] = {
    "overtake": detect_overtake,
    "incident": detect_incident,
    "tow": detect_tow,
    "battle": detect_battle,
    "player_flag": detect_player_flag,
}

AVAILABLE_DETECTORS = {**PHASE_TWO_DETECTORS, **PHASE_THREE_DETECTORS}

__all__ = [
    "AVAILABLE_DETECTORS",
    "PHASE_TWO_DETECTORS",
    "PHASE_THREE_DETECTORS",
    "detect_battle",
    "detect_caution",
    "detect_checkered",
    "detect_green",
    "detect_incident",
    "detect_overtake",
    "detect_pit",
    "detect_player_flag",
    "detect_restart",
    "detect_session_transition",
    "detect_tow",
    "detect_white",
]
