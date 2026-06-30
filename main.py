"""Livestream Spotter command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from pathlib import Path

from livestream_spotter import __version__
from livestream_spotter.config import load_config
from livestream_spotter.detectors import AVAILABLE_DETECTORS
from livestream_spotter.iracing import IRacingClient
from livestream_spotter.obs.clock import MockClock, ObsClock
from livestream_spotter.pipeline.bus import EventBus
from livestream_spotter.pipeline.poll_loop import PollLoop
from livestream_spotter.runtime_paths import (
    invalid_config_message,
    missing_config_message,
    resolve_config_path,
    resolve_lastrun_log_path,
)
from livestream_spotter.sinks.chapters import ChaptersSink
from livestream_spotter.sinks.event_log import LoggingEventSink
from livestream_spotter.sinks.fanout import FanoutEventSink
from livestream_spotter.sinks.raw_dump import JsonlRawSink
from livestream_spotter.sinks.timestamps import TimestampsSink

LOGGER = logging.getLogger("livestream_spotter")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

try:  # Optional colored status markers; degrade to plain text if unavailable.
    from colorama import Fore, Style

    try:
        from colorama import just_fix_windows_console

        just_fix_windows_console()
    except ImportError:  # older colorama
        from colorama import init as _colorama_init

        _colorama_init()
    _STATUS_COLORS = {"ok": Fore.GREEN, "fail": Fore.RED, "wait": Fore.CYAN}
    _STATUS_RESET = Style.RESET_ALL
except Exception:  # colorama missing — never emit raw ANSI, just plain markers.
    _STATUS_COLORS = {}
    _STATUS_RESET = ""

_STATUS_MARKERS = {"ok": "[ OK ]", "fail": "[FAIL]", "wait": "[ .. ]"}


def _color_enabled() -> bool:
    """Color only when colorama loaded AND stdout is a real terminal.

    The isatty gate keeps raw ANSI out of redirected output (e.g. a tester
    piping the console to a file), which just_fix_windows_console does not strip.
    """
    if not _STATUS_COLORS:
        return False
    try:
        return bool(sys.stdout is not None and sys.stdout.isatty())
    except Exception:
        return False


def _format_status(state: str) -> str:
    """Return a status marker, color-wrapped only when color is available."""
    marker = _STATUS_MARKERS[state]
    color = _STATUS_COLORS.get(state)
    if color and _color_enabled():
        return f"{color}{marker}{_STATUS_RESET}"
    return marker


def configure_logging(log_path: Path) -> None:
    """Log INFO to the console and a fresh DEBUG trace to lastrun.log."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(LOG_FORMAT)

    for handler in tuple(root_logger.handlers):
        if getattr(handler, "_livestream_spotter_handler", False):
            root_logger.removeHandler(handler)
            handler.close()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    console_handler._livestream_spotter_handler = True  # type: ignore[attr-defined]
    root_logger.addHandler(console_handler)

    try:
        file_handler = logging.FileHandler(
            log_path,
            mode="w",
            encoding="utf-8",
        )
    except OSError as error:
        LOGGER.warning("Could not create run log at %s: %s", log_path, error)
        return
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler._livestream_spotter_handler = True  # type: ignore[attr-defined]
    root_logger.addHandler(file_handler)

    # Quiet the obsws-python library logger. At INFO it prints the connection
    # repr (including the password); on a failed connect its ObsClient child
    # logs the raw TimeoutError traceback at ERROR *before* we report the
    # failure cleanly. We surface both outcomes ourselves, so silence the
    # library below CRITICAL to avoid leaking the password and dumping a scary
    # traceback on an expected, handled failure.
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)


def close_logging() -> None:
    """Close only the handlers installed by configure_logging()."""
    root_logger = logging.getLogger()
    for handler in tuple(root_logger.handlers):
        if getattr(handler, "_livestream_spotter_handler", False):
            root_logger.removeHandler(handler)
            handler.close()


def connect_startup_services(iracing: IRacingClient, clock) -> tuple[bool, bool]:
    """Attempt both startup connections independently before polling."""
    obs_connected = clock.connect()
    iracing_connected = iracing.connect()
    LOGGER.debug(
        "Startup connection results: OBS=%s, iRacing=%s",
        "connected" if obs_connected else "not connected",
        "connected" if iracing_connected else "waiting",
    )
    _report_startup_status(obs_connected, iracing_connected)
    return obs_connected, iracing_connected


def _report_startup_status(obs_connected: bool, iracing_connected: bool) -> None:
    """Print an at-a-glance connection summary with status markers.

    The 'why' lines (endpoint, failure hint) are logged by the clients
    themselves; this is only the visual marker pass, printed to the console so
    no ANSI ever reaches lastrun.log. iRacing not being up yet is the expected
    state at launch, so it reads as neutral 'waiting', not a red failure.
    """
    if obs_connected:
        print(f"{_format_status('ok')} OBS WebSocket — connected")
    else:
        print(
            f"{_format_status('fail')} OBS WebSocket — "
            "not connected (see details above)"
        )
    if iracing_connected:
        print(f"{_format_status('ok')} iRacing — session active")
    else:
        print(f"{_format_status('wait')} iRacing — waiting for session")


def select_detectors(enabled_names: frozenset[str]):
    unknown = sorted(enabled_names - AVAILABLE_DETECTORS.keys())
    for name in unknown:
        LOGGER.warning("Unknown enabled detector %r; ignoring it", name)
    return tuple(
        detector
        for name, detector in AVAILABLE_DETECTORS.items()
        if name in enabled_names
    )


def build_event_sink(config) -> FanoutEventSink:
    sinks = [LoggingEventSink()]
    if "timestamps" in config.enabled_renderers:
        sinks.append(
            TimestampsSink(
                config.timestamps_path,
                config.timestamp_min_spacing_ms,
            )
        )
    if "chapters" in config.enabled_renderers:
        sinks.append(
            ChaptersSink(
                config.chapters_path,
                config.chapter_min_spacing_ms,
            )
        )
    return FanoutEventSink(sinks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument(
        "--version",
        action="version",
        version=f"livestream-spotter {__version__}",
    )
    parser.add_argument(
        "--mock-clock",
        action="store_true",
        help="Use elapsed monotonic time instead of connecting to OBS.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one heartbeat and exit (useful for Phase 0 verification).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = resolve_config_path(args.config)
    log_path = resolve_lastrun_log_path(config_path)
    configure_logging(log_path)
    LOGGER.debug("Resolved config path: %s", config_path.resolve())
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        LOGGER.critical(missing_config_message(config_path))
        close_logging()
        return 2
    except (tomllib.TOMLDecodeError, KeyError) as error:
        LOGGER.critical(invalid_config_message(config_path, error))
        close_logging()
        return 2
    LOGGER.info("Livestream Spotter %s", __version__)
    LOGGER.info("Config loaded from %s; polling at %.2f Hz", config_path, config.poll_hz)
    LOGGER.debug(
        "OBS endpoint: %s:%d; authentication expected: %s",
        config.obs.host,
        config.obs.port,
        "yes" if config.obs.password else "no",
    )

    iracing = IRacingClient()
    clock = MockClock() if args.mock_clock else ObsClock(config.obs)
    connect_startup_services(iracing, clock)
    event_sink = build_event_sink(config)
    raw_sink = JsonlRawSink(config.raw_dump_path) if config.raw_dump_enabled else None
    detectors = select_detectors(config.enabled_detectors)
    poll_loop = PollLoop(
        iracing=iracing,
        clock=clock,
        bus=EventBus(),
        event_sink=event_sink,
        detectors=detectors,
        lead_in_ms=config.lead_in_ms,
        poll_hz=config.poll_hz,
        hold_until_stream_active=config.hold_until_stream_active,
        race_only_player_events=config.race_only_player_events,
        battle_gap_threshold=config.battle_gap_threshold,
        battle_min_duration=config.battle_min_duration,
        battle_throttle_window=config.battle_throttle_window,
        incident_minimum_delta=config.incident_minimum_delta,
        raw_sink=raw_sink,
        raw_dump_hz=config.raw_dump_hz,
    )
    try:
        poll_loop.run(once=args.once)
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
        return 0
    finally:
        event_sink.close()
        if raw_sink is not None:
            raw_sink.close()
        iracing.disconnect()
        clock.disconnect()
        close_logging()


if __name__ == "__main__":
    raise SystemExit(main())
