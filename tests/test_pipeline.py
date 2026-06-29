from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
import json
import tempfile
import unittest

from livestream_spotter.detectors.reliable import (
    FLAG_CAUTION,
    FLAG_CHECKERED,
    FLAG_GREEN_HELD,
    detect_caution,
    detect_checkered,
    detect_green,
    detect_pit,
    detect_restart,
)
from livestream_spotter.detectors.player import detect_battle
from livestream_spotter.events import Event
from livestream_spotter.obs.clock import ClockReading
from livestream_spotter.pipeline.bus import EventBus
from livestream_spotter.pipeline.poll_loop import PollLoop
from livestream_spotter.pipeline.snapshot import (
    SnapshotEnricher,
    adjacent_comparisons,
    build_driver_lookup,
    build_raw_record,
    signed_lap_delta,
)
from livestream_spotter.sinks.raw_dump import JsonlRawSink


def hand_built_snapshot(track_wetness=2, **overrides):
    values = {
        "SessionNum": 0,
        "SessionState": 3,
        "SessionFlags": 0,
        "SessionTime": 45.0,
        "Lap": 1,
        "OnPitRoad": False,
        "CarIdxPosition": [2, 1, 3],
        "CarIdxLapDistPct": [0.99, 0.02, 0.96],
        "CarIdxF2Time": [4.0, 0.0, 7.0],
        "DriverInfo": {
            "DriverCarIdx": 0,
            "DriverCarEstLapTime": 100.0,
            "Drivers": [
                {"CarIdx": 0, "UserName": "Player", "CarClassID": 10},
                {"CarIdx": 1, "UserName": "Ahead", "CarClassID": 10},
                {"CarIdx": 2, "UserName": "Behind", "CarClassID": 20},
            ],
        },
        "SessionInfo": {
            "Sessions": [{"SessionNum": 0, "SessionType": "Race"}]
        },
        "TrackWetness": track_wetness,
    }
    values.update(overrides)
    return values


def event(number: int) -> Event:
    return Event(number, f"type-{number}", "label", 3, None, 1.0, 0, {})


class CollectingSink:
    def __init__(self) -> None:
        self.items = []

    def write(self, item) -> None:
        self.items.append(item)

    def close(self) -> None:
        pass


class FakeIRacing:
    def __init__(self, snapshots) -> None:
        self.snapshots = iter(snapshots)

    def connect(self) -> bool:
        return True

    def capture(self, fields):
        return next(self.snapshots)


class FakeClock:
    def __init__(self, readings) -> None:
        self.readings = iter(readings)
        self.read_calls = 0

    def connect(self) -> bool:
        return True

    def read(self) -> ClockReading:
        self.read_calls += 1
        return next(self.readings)

    def disconnect(self) -> None:
        pass


class SnapshotTests(unittest.TestCase):
    def test_driver_lookup_captures_name_and_number(self) -> None:
        lookup = build_driver_lookup(
            {
                "Drivers": [
                    {"CarIdx": 7, "UserName": "J. Smith", "CarNumber": "44"}
                ]
            }
        )

        self.assertEqual(lookup[7], {"name": "J. Smith", "number": "44"})

    def test_driver_lookup_is_cached_until_session_changes(self) -> None:
        enricher = SnapshotEnricher()
        first = enricher.enrich(
            {
                "SessionNum": 0,
                "DriverInfo": {
                    "Drivers": [
                        {"CarIdx": 1, "UserName": "Original", "CarNumber": "1"}
                    ]
                },
            }
        )
        cached = enricher.enrich(
            {
                "SessionNum": 0,
                "DriverInfo": {
                    "Drivers": [
                        {"CarIdx": 1, "UserName": "Ignored", "CarNumber": "2"}
                    ]
                },
            }
        )
        refreshed = enricher.enrich(
            {
                "SessionNum": 1,
                "DriverInfo": {
                    "Drivers": [
                        {"CarIdx": 1, "UserName": "Refreshed", "CarNumber": "3"}
                    ]
                },
            }
        )

        self.assertEqual(first["DriverLookup"][1]["name"], "Original")
        self.assertEqual(cached["DriverLookup"][1]["name"], "Original")
        self.assertEqual(refreshed["DriverLookup"][1]["name"], "Refreshed")

    def test_signed_lap_delta_wraps_at_start_finish(self) -> None:
        self.assertAlmostEqual(signed_lap_delta(0.02, 0.99), 0.03)
        self.assertAlmostEqual(signed_lap_delta(0.96, 0.99), -0.03)

    def test_adjacent_rows_put_f2_and_derived_gap_side_by_side(self) -> None:
        rows = adjacent_comparisons(hand_built_snapshot())

        self.assertEqual([row["relation"] for row in rows], ["ahead", "behind"])
        self.assertEqual(rows[0]["car_idx_f2_time"], 0.0)
        self.assertEqual(rows[0]["f2_delta_vs_player_seconds"], -4.0)
        self.assertAlmostEqual(rows[0]["signed_lap_delta_pct"], 0.03)
        self.assertNotIn("lap_dist_estimated_gap_seconds", rows[0])
        self.assertEqual(rows[1]["car_class_id"], 20)
        self.assertAlmostEqual(rows[1]["signed_lap_delta_pct"], -0.03)

    def test_record_keeps_session_time_debug_only_and_obs_as_timeline(self) -> None:
        record = build_raw_record(
            hand_built_snapshot(track_wetness=None),
            ClockReading(video_ms=9000, output_active=True, source="obs"),
            datetime(2026, 1, 2, tzinfo=UTC),
        )

        self.assertEqual(record["timeline"]["video_ms"], 9000)
        self.assertEqual(record["timeline"]["obs_output_duration_ms"], 9000)
        self.assertEqual(record["iracing"]["telemetry"]["SessionTime"], 45.0)
        self.assertFalse(record["iracing"]["track_wetness"]["present"])

    def test_mock_timeline_is_not_labeled_as_obs_duration(self) -> None:
        record = build_raw_record(
            hand_built_snapshot(),
            ClockReading(video_ms=500, output_active=True, source="mock"),
        )

        self.assertEqual(record["timeline"]["video_ms"], 500)
        self.assertIsNone(record["timeline"]["obs_output_duration_ms"])


class EventAndBusTests(unittest.TestCase):
    def test_event_schema_is_exact(self) -> None:
        self.assertEqual(
            [field.name for field in fields(Event)],
            [
                "video_ms",
                "event_type",
                "label",
                "tier",
                "lap",
                "session_time",
                "lead_in_ms",
                "meta",
            ],
        )

    def test_event_bus_is_fifo(self) -> None:
        bus = EventBus()
        bus.publish(event(1))
        bus.publish(event(2))

        self.assertEqual(bus.take_nowait(), event(1))
        self.assertEqual(bus.take_nowait(), event(2))
        self.assertIsNone(bus.take_nowait())


class PipelineTests(unittest.TestCase):
    def make_loop(
        self,
        snapshots,
        readings,
        *,
        hold=True,
        raw_sink=None,
        raw_dump_hz=1.0,
        monotonic=lambda: 0.0,
        detectors=(detect_green,),
        race_only_player_events=True,
        battle_min_duration=9.0,
        battle_throttle_window=60.0,
    ):
        event_sink = CollectingSink()
        clock = FakeClock(readings)
        loop = PollLoop(
            iracing=FakeIRacing(snapshots),
            clock=clock,
            bus=EventBus(),
            event_sink=event_sink,
            detectors=detectors,
            lead_in_ms={"green": 2000, "battle": 2000},
            poll_hz=15,
            hold_until_stream_active=hold,
            raw_sink=raw_sink,
            raw_dump_hz=raw_dump_hz,
            monotonic=monotonic,
            race_only_player_events=race_only_player_events,
            battle_min_duration=battle_min_duration,
            battle_throttle_window=battle_throttle_window,
        )
        return loop, event_sink, clock

    def test_pipeline_attaches_obs_time_not_session_time(self) -> None:
        before = hand_built_snapshot(SessionState=3, SessionTime=10.0)
        after = hand_built_snapshot(SessionState=4, SessionTime=987.5, Lap=2)
        loop, sink, clock = self.make_loop(
            [before, after], [ClockReading(123456, True, "obs")]
        )

        self.assertFalse(loop.tick())
        self.assertTrue(loop.tick())

        emitted = sink.items[0]
        self.assertEqual(emitted.video_ms, 123456)
        self.assertEqual(emitted.session_time, 987.5)
        self.assertEqual(emitted.lead_in_ms, 2000)
        self.assertEqual(clock.read_calls, 1)

    def test_no_edge_means_no_clock_call_or_double_fire(self) -> None:
        racing = hand_built_snapshot(SessionState=4)
        loop, sink, clock = self.make_loop([racing, racing], [])

        loop.tick()
        loop.tick()

        self.assertEqual(sink.items, [])
        self.assertEqual(clock.read_calls, 0)

    def test_checkered_bit_setting_twice_emits_once(self) -> None:
        snapshots = [
            hand_built_snapshot(SessionState=4, SessionFlags=0),
            hand_built_snapshot(SessionState=4, SessionFlags=FLAG_CHECKERED),
            hand_built_snapshot(SessionState=4, SessionFlags=0),
            hand_built_snapshot(SessionState=4, SessionFlags=FLAG_CHECKERED),
        ]
        loop, sink, clock = self.make_loop(
            snapshots,
            [ClockReading(100, True, "obs")],
            detectors=(detect_checkered,),
        )

        for _ in snapshots:
            loop.tick()

        self.assertEqual([item.event_type for item in sink.items], ["checkered"])
        self.assertEqual(clock.read_calls, 1)

    def test_player_identity_is_logged_once(self) -> None:
        snapshots = [hand_built_snapshot(), hand_built_snapshot()]
        loop, _, _ = self.make_loop(snapshots, [], detectors=())

        with self.assertLogs(
            "livestream_spotter.pipeline.poll_loop", level="INFO"
        ) as logs:
            loop.tick()
            loop.tick()

        resolved = [message for message in logs.output if "Resolved player" in message]
        self.assertEqual(len(resolved), 1)
        self.assertIn("index=0", resolved[0])
        self.assertIn("Player", resolved[0])

    def test_pre_race_pit_event_is_suppressed(self) -> None:
        loop, sink, clock = self.make_loop(
            [
                hand_built_snapshot(SessionState=3, OnPitRoad=False),
                hand_built_snapshot(SessionState=3, OnPitRoad=True),
            ],
            [],
            detectors=(detect_pit,),
        )

        loop.tick()
        loop.tick()

        self.assertEqual(sink.items, [])
        self.assertEqual(clock.read_calls, 0)

    def test_race_only_gate_can_be_disabled(self) -> None:
        loop, sink, _ = self.make_loop(
            [
                hand_built_snapshot(SessionState=3, OnPitRoad=False),
                hand_built_snapshot(SessionState=3, OnPitRoad=True),
            ],
            [ClockReading(100, True, "obs")],
            detectors=(detect_pit,),
            race_only_player_events=False,
        )

        loop.tick()
        loop.tick()

        self.assertEqual([item.event_type for item in sink.items], ["pit_in"])

    def test_session_number_resets_race_phase_without_transition_detector(self) -> None:
        sessions = {
            "Sessions": [
                {"SessionNum": 0, "SessionType": "Race"},
                {"SessionNum": 1, "SessionType": "Practice"},
            ]
        }
        loop, sink, clock = self.make_loop(
            [
                hand_built_snapshot(
                    SessionNum=0, SessionState=3, OnPitRoad=False, SessionInfo=sessions
                ),
                hand_built_snapshot(
                    SessionNum=0, SessionState=4, OnPitRoad=False, SessionInfo=sessions
                ),
                hand_built_snapshot(
                    SessionNum=1, SessionState=3, OnPitRoad=True, SessionInfo=sessions
                ),
            ],
            [],
            detectors=(detect_pit,),
        )

        loop.tick()
        loop.tick()
        loop.tick()

        self.assertEqual(sink.items, [])
        self.assertEqual(clock.read_calls, 0)

    def test_battle_duration_and_throttle_emit_once_per_scrap(self) -> None:
        def battle_snapshot(session_time, nearby=True):
            return hand_built_snapshot(
                SessionState=4,
                SessionTime=float(session_time),
                Lap=5,
                OnPitRoad=False,
                CarIdxPosition=[2, 1, 0],
                CarIdxLap=[5, 5, -1],
                CarIdxLapDistPct=[0.50, 0.52 if nearby else 0.80, -1.0],
                CarIdxF2Time=[10.0, 9.6, 0.0],
                CarIdxOnPitRoad=[False, False, False],
            )

        snapshots = [
            battle_snapshot(0),
            battle_snapshot(1),
            battle_snapshot(2),
            battle_snapshot(3),
            battle_snapshot(4),
            battle_snapshot(5, nearby=False),
            battle_snapshot(6),
            battle_snapshot(7),
            battle_snapshot(8),
        ]
        loop, sink, clock = self.make_loop(
            snapshots,
            [ClockReading(300, True, "obs")],
            detectors=(detect_battle,),
            battle_min_duration=2.0,
            battle_throttle_window=60.0,
        )

        for _ in snapshots:
            loop.tick()

        self.assertEqual([item.event_type for item in sink.items], ["battle"])
        self.assertEqual(clock.read_calls, 1)

    def test_green_held_at_initial_start_does_not_emit_restart(self) -> None:
        loop, sink, _ = self.make_loop(
            [
                hand_built_snapshot(SessionState=3),
                hand_built_snapshot(SessionState=4, SessionFlags=FLAG_GREEN_HELD),
            ],
            [ClockReading(100, True, "obs")],
            detectors=(detect_green, detect_caution, detect_restart),
        )

        loop.tick()
        loop.tick()

        self.assertEqual([event.event_type for event in sink.items], ["green"])

    def test_restart_requires_caution_since_initial_green(self) -> None:
        loop, sink, _ = self.make_loop(
            [
                hand_built_snapshot(SessionState=3),
                hand_built_snapshot(SessionState=4),
                hand_built_snapshot(SessionState=4, SessionFlags=FLAG_CAUTION),
                hand_built_snapshot(SessionState=4, SessionFlags=0),
                hand_built_snapshot(SessionState=4, SessionFlags=FLAG_GREEN_HELD),
                hand_built_snapshot(SessionState=4, SessionFlags=0),
                hand_built_snapshot(SessionState=4, SessionFlags=FLAG_GREEN_HELD),
                hand_built_snapshot(SessionState=4, SessionFlags=FLAG_GREEN_HELD),
            ],
            [
                ClockReading(100, True, "obs"),
                ClockReading(200, True, "obs"),
                ClockReading(300, True, "obs"),
            ],
            detectors=(detect_green, detect_caution, detect_restart),
        )

        for _ in range(8):
            loop.tick()

        self.assertEqual(
            [event.event_type for event in sink.items],
            ["green", "caution", "restart"],
        )

    def test_event_is_held_until_stream_becomes_active(self) -> None:
        before = hand_built_snapshot(SessionState=3)
        after = hand_built_snapshot(SessionState=4, SessionTime=55.0)
        steady = hand_built_snapshot(SessionState=4, SessionTime=56.0)
        loop, sink, _ = self.make_loop(
            [before, after, steady],
            [
                ClockReading(None, False, "obs"),
                ClockReading(80, True, "obs"),
            ],
        )

        loop.tick()
        self.assertFalse(loop.tick())
        self.assertEqual(sink.items, [])
        self.assertTrue(loop.tick())
        self.assertEqual(sink.items[0].video_ms, 80)
        self.assertEqual(sink.items[0].session_time, 55.0)

    def test_inactive_event_is_dropped_when_hold_is_disabled(self) -> None:
        loop, sink, _ = self.make_loop(
            [
                hand_built_snapshot(SessionState=3),
                hand_built_snapshot(SessionState=4),
            ],
            [ClockReading(None, False, "obs")],
            hold=False,
        )

        loop.tick()
        self.assertFalse(loop.tick())
        self.assertEqual(sink.items, [])

    def test_raw_diagnostic_cadence_is_independent_of_polling(self) -> None:
        raw_sink = CollectingSink()
        times = iter([0.0, 0.5, 1.0])
        snapshots = [hand_built_snapshot()] * 3
        loop, _, clock = self.make_loop(
            snapshots,
            [
                ClockReading(0, True, "mock"),
                ClockReading(1000, True, "mock"),
            ],
            raw_sink=raw_sink,
            raw_dump_hz=1.0,
            monotonic=lambda: next(times),
        )

        loop.tick()
        loop.tick()
        loop.tick()

        self.assertEqual(len(raw_sink.items), 2)
        self.assertEqual(clock.read_calls, 2)

    def test_jsonl_sink_appends_one_object_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "raw.jsonl"
            sink = JsonlRawSink(path)
            sink.write({"tick": 1})
            sink.write({"tick": 2})
            sink.close()

            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records, [{"tick": 1}, {"tick": 2}])


if __name__ == "__main__":
    unittest.main()
