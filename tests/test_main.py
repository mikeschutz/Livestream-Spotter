import unittest
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

from livestream_spotter.detectors import PHASE_TWO_DETECTORS
from livestream_spotter.events import Event
import main as main_module
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


class MainTests(unittest.TestCase):
    def test_missing_config_returns_clear_error_without_traceback(self) -> None:
        args = SimpleNamespace(
            config=Path("config.toml"),
            mock_clock=True,
            once=True,
        )
        with (
            patch.object(main_module, "parse_args", return_value=args),
            patch.object(main_module, "load_config", side_effect=FileNotFoundError),
            self.assertLogs("livestream_spotter", level="CRITICAL") as logs,
        ):
            exit_code = main_module.main()

        self.assertEqual(exit_code, 2)
        self.assertIn("Config file not found", logs.output[0])

    def test_invalid_config_returns_clear_error_without_traceback(self) -> None:
        cases = {
            "malformed toml": b"this is = = not valid toml",
            "missing section": b'[obs]\nhost = "x"\nport = 4455\npassword = ""\n',
        }
        for name, payload in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_bytes(payload)
                args = SimpleNamespace(
                    config=config_path, mock_clock=True, once=True
                )
                with (
                    patch.object(main_module, "parse_args", return_value=args),
                    self.assertLogs("livestream_spotter", level="CRITICAL") as logs,
                ):
                    exit_code = main_module.main()

                self.assertEqual(exit_code, 2)
                self.assertIn("Config file is invalid", logs.output[0])


if __name__ == "__main__":
    unittest.main()
