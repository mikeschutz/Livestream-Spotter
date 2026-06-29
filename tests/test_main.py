import unittest
from pathlib import Path
from types import SimpleNamespace
import tempfile

from livestream_spotter.detectors import PHASE_TWO_DETECTORS
from livestream_spotter.events import Event
from main import build_event_sink, select_detectors


class DetectorSelectionTests(unittest.TestCase):
    def test_unknown_enabled_detector_logs_warning(self) -> None:
        with self.assertLogs("livestream_spotter", level="WARNING") as logs:
            selected = select_detectors(frozenset({"green", "pits"}))

        self.assertEqual(selected, (PHASE_TWO_DETECTORS["green"],))
        self.assertIn("pits", logs.output[0])


class RendererSelectionTests(unittest.TestCase):
    def test_plain_timestamps_is_the_only_default_file_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                enabled_renderers=frozenset({"timestamps"}),
                timestamps_path=root / "timestamps.txt",
                timestamp_min_spacing_ms=2_000,
                chapters_path=root / "chapters.txt",
                chapter_min_spacing_ms=10_000,
            )
            sink = build_event_sink(config)

            sink.write(Event(12_000, "green", "Green", 3, 1, 5.0, 2_000, {}))

            self.assertEqual(
                (root / "timestamps.txt").read_text().splitlines(),
                ["00:10 Green"],
            )
            self.assertFalse((root / "chapters.txt").exists())


if __name__ == "__main__":
    unittest.main()
