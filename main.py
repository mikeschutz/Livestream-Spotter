"""Livestream Spotter command-line entry point."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from livestream_spotter.config import load_config
from livestream_spotter.detectors import AVAILABLE_DETECTORS
from livestream_spotter.iracing import IRacingClient
from livestream_spotter.obs.clock import MockClock, ObsClock
from livestream_spotter.pipeline.bus import EventBus
from livestream_spotter.pipeline.poll_loop import PollLoop
from livestream_spotter.sinks.chapters import ChaptersSink
from livestream_spotter.sinks.event_log import LoggingEventSink
from livestream_spotter.sinks.fanout import FanoutEventSink
from livestream_spotter.sinks.raw_dump import JsonlRawSink
from livestream_spotter.sinks.timestamps import TimestampsSink

LOGGER = logging.getLogger("livestream_spotter")


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    LOGGER.info("Config loaded; polling at %.2f Hz", config.poll_hz)

    iracing = IRacingClient()
    clock = MockClock() if args.mock_clock else ObsClock(config.obs)
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


if __name__ == "__main__":
    raise SystemExit(main())
