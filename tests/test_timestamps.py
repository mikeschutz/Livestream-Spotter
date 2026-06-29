from pathlib import Path
import tempfile
import unittest

from livestream_spotter.events import Event
from livestream_spotter.sinks.timestamps import TimestampsSink


def event(
    video_ms: int,
    label: str,
    *,
    tier: int = 3,
    lead_in_ms: int = 0,
    event_type: str = "test",
    meta: dict | None = None,
) -> Event:
    return Event(
        video_ms,
        event_type,
        label,
        tier,
        1,
        10.0,
        lead_in_ms,
        meta or {},
    )


class TimestampsSinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "nested" / "timestamps.txt"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines()

    def test_no_seed_and_no_minimum_count(self) -> None:
        sink = TimestampsSink(self.path)
        self.assertEqual(self.lines(), [])

        sink.write(event(1_000, "Only event"))

        self.assertEqual(self.lines(), ["00:01 Only event"])

    def test_cosmetic_spacing_honors_configured_window(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=5_000)
        sink.write(event(1_000, "First"))
        sink.write(event(4_000, "Too close"))
        sink.write(event(6_000, "Far enough"))

        self.assertEqual(self.lines(), ["00:01 First", "00:06 Far enough"])

    def test_higher_priority_tier_wins_collision(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=5_000)
        sink.write(event(10_000, "Caution", tier=3))
        sink.write(event(12_000, "Pit in", tier=1))

        self.assertEqual(self.lines(), ["00:12 Pit in"])

    def test_lead_in_is_applied_once_and_clamped_at_zero(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)
        sink.write(event(1_000, "Clamped", lead_in_ms=6_000))
        sink.write(event(5_000, "Adjusted", lead_in_ms=2_000))

        self.assertEqual(self.lines(), ["00:00 Clamped", "00:03 Adjusted"])

    def test_format_switches_to_hours_at_one_hour(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)
        sink.write(event(3_599_999, "Under"))
        sink.write(event(3_600_000, "At"))

        self.assertEqual(self.lines(), ["59:59 Under", "1:00:00 At"])

    def test_duration_regression_warns_once_and_keeps_writing(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)

        with self.assertLogs(
            "livestream_spotter.sinks.timestamps", level="CRITICAL"
        ) as logs:
            sink.write(event(30_000, "First"))
            sink.write(event(20_000, "Regression"))
            sink.write(event(10_000, "Still regressed"))

        self.assertEqual(len(logs.output), 1)
        self.assertIn("timestamps.txt", logs.output[0])
        self.assertEqual(len(self.lines()), 3)

    def test_atomic_rewrite_leaves_no_temporary_file(self) -> None:
        sink = TimestampsSink(self.path)
        sink.write(event(12_000, "Green"))

        self.assertFalse(self.path.with_name("timestamps.txt.tmp").exists())

    def test_two_token_name_is_abbreviated_without_mutating_event(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)
        raw_event = event(
            10_000,
            "Battle with Seth Whitaker",
            meta={"opponent_name": "Seth Whitaker"},
        )

        sink.write(raw_event)

        self.assertEqual(self.lines(), ["00:10 Battle with Seth W."])
        self.assertEqual(raw_event.label, "Battle with Seth Whitaker")

    def test_three_token_name_uses_last_token_initial(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)
        sink.write(
            event(
                10_000,
                "Passed Joasir S Coelho for P3",
                meta={"opponent_name": "Joasir S Coelho"},
            )
        )

        self.assertEqual(self.lines(), ["00:10 Passed Joasir C. for P3"])

    def test_accented_name_is_abbreviated(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)
        sink.write(
            event(
                10_000,
                "Contact with Álvaro Núñez",
                meta={"opponent_name": "Álvaro Núñez"},
            )
        )

        self.assertEqual(self.lines(), ["00:10 Contact with Álvaro N."])

    def test_pit_in_out_publish_one_numbered_stop(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)

        sink.write(event(10_000, "Pit in", tier=1, event_type="pit_in"))
        self.assertEqual(self.lines(), [])
        sink.write(event(20_000, "Pit out", tier=1, event_type="pit_out"))

        self.assertEqual(self.lines(), ["00:10 Pit stop 1"])

    def test_tow_inside_pit_sequence_is_annotation(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)
        sink.write(event(10_000, "Pit in", tier=1, event_type="pit_in"))
        sink.write(event(15_000, "Towed to pits", tier=1, event_type="tow"))
        sink.write(event(20_000, "Pit out", tier=1, event_type="pit_out"))

        self.assertEqual(self.lines(), ["00:10 Pit stop 1 (towed)"])

    def test_pit_counter_increments_across_stops(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)
        sink.write(event(10_000, "Pit in", tier=1, event_type="pit_in"))
        sink.write(event(20_000, "Pit out", tier=1, event_type="pit_out"))
        sink.write(event(30_000, "Pit in", tier=1, event_type="pit_in"))
        sink.write(event(40_000, "Pit out", tier=1, event_type="pit_out"))

        self.assertEqual(
            self.lines(),
            ["00:10 Pit stop 1", "00:30 Pit stop 2"],
        )

    def test_checkered_flushes_open_pit_sequence(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)
        sink.write(event(10_000, "Pit in", tier=1, event_type="pit_in"))
        sink.write(
            event(30_000, "Checkered flag", tier=3, event_type="checkered")
        )

        self.assertEqual(
            self.lines(),
            ["00:10 Pit stop 1", "00:30 Checkered flag"],
        )

    def test_close_flushes_open_pit_without_pit_out(self) -> None:
        sink = TimestampsSink(self.path, min_spacing_ms=0)
        sink.write(event(10_000, "Pit in", tier=1, event_type="pit_in"))
        self.assertEqual(self.lines(), [])

        sink.close()
        self.assertEqual(self.lines(), ["00:10 Pit stop 1"])

        # A second close must not re-emit the already-flushed sequence.
        sink.close()
        self.assertEqual(self.lines(), ["00:10 Pit stop 1"])


if __name__ == "__main__":
    unittest.main()
