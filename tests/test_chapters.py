from pathlib import Path
import tempfile
import unittest

from livestream_spotter.events import Event
from livestream_spotter.sinks.chapters import (
    ChaptersSink,
    adjusted_timestamp_ms,
    format_timestamp,
)


def event(
    video_ms: int,
    label: str,
    *,
    tier: int = 3,
    lead_in_ms: int = 0,
) -> Event:
    return Event(video_ms, "test", label, tier, 1, 10.0, lead_in_ms, {})


class ChaptersSinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "nested" / "chapters.txt"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines()

    def test_file_is_seeded_immediately(self) -> None:
        ChaptersSink(self.path)

        self.assertEqual(self.lines(), ["00:00 Stream start"])

    def test_chapters_are_sorted_and_at_least_ten_seconds_apart(self) -> None:
        sink = ChaptersSink(self.path)
        sink.write(event(30_000, "Later"))
        sink.write(event(40_000, "Earlier", lead_in_ms=25_000))

        self.assertEqual(
            self.lines(),
            ["00:00 Stream start", "00:15 Earlier", "00:30 Later"],
        )

    def test_spacing_is_applied_after_lead_in_offset(self) -> None:
        sink = ChaptersSink(self.path)
        sink.write(event(12_000, "First"))
        sink.write(event(23_000, "Pulled closer", lead_in_ms=2_000))

        self.assertEqual(self.lines(), ["00:00 Stream start", "00:12 First"])

    def test_higher_priority_tier_replaces_colliding_chapter(self) -> None:
        sink = ChaptersSink(self.path)
        sink.write(event(12_000, "Caution", tier=3))
        sink.write(event(18_000, "Pit in", tier=1))

        self.assertEqual(self.lines(), ["00:00 Stream start", "00:18 Pit in"])

    def test_equal_tier_collision_keeps_earlier_adjusted_timestamp(self) -> None:
        sink = ChaptersSink(self.path)
        sink.write(event(12_000, "Earlier", tier=2))
        sink.write(event(18_000, "Later", tier=2))

        self.assertEqual(self.lines(), ["00:00 Stream start", "00:12 Earlier"])

    def test_event_inside_seed_spacing_cannot_replace_stream_start(self) -> None:
        sink = ChaptersSink(self.path)
        sink.write(event(9_999, "Too early", tier=1))

        self.assertEqual(self.lines(), ["00:00 Stream start"])

    def test_format_switches_to_hours_at_one_hour(self) -> None:
        self.assertEqual(format_timestamp(3_599_999), "59:59")
        self.assertEqual(format_timestamp(3_600_000), "1:00:00")

    def test_lead_in_clamps_at_zero(self) -> None:
        self.assertEqual(
            adjusted_timestamp_ms(event(1_000, "Early", lead_in_ms=6_000)), 0
        )

    def test_duration_regression_logs_once_and_keeps_writing(self) -> None:
        sink = ChaptersSink(self.path)

        with self.assertLogs(
            "livestream_spotter.sinks.chapters", level="CRITICAL"
        ) as logs:
            sink.write(event(30_000, "First"))
            sink.write(event(20_000, "Regression"))
            sink.write(event(10_000, "Still regressed"))

        self.assertEqual(len(logs.output), 1)
        self.assertIn("DECREASED", logs.output[0])
        self.assertTrue(self.path.exists())

    def test_atomic_rewrite_leaves_no_temporary_file(self) -> None:
        sink = ChaptersSink(self.path)
        sink.write(event(12_000, "Green"))

        self.assertFalse(self.path.with_name("chapters.txt.tmp").exists())


if __name__ == "__main__":
    unittest.main()
