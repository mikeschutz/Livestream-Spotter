import contextlib
import io
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import Mock, patch

from livestream_spotter.detectors import PHASE_TWO_DETECTORS
from livestream_spotter.events import Event
import main as main_module
from main import (
    build_event_sink,
    close_logging,
    configure_logging,
    connect_startup_services,
    select_detectors,
)


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
    def test_startup_attempts_obs_even_while_iracing_is_unavailable(self) -> None:
        call_order = []
        iracing = Mock()
        iracing.connect.side_effect = lambda: call_order.append("iracing") or False
        clock = Mock()
        clock.connect.side_effect = lambda: call_order.append("obs") or True

        with contextlib.redirect_stdout(io.StringIO()):
            result = connect_startup_services(iracing, clock)

        self.assertEqual(result, (True, False))
        self.assertEqual(call_order, ["iracing", "obs"])
        clock.connect.assert_called_once_with()
        iracing.connect.assert_called_once_with()

    def test_startup_status_uses_neutral_marker_while_iracing_waits(self) -> None:
        iracing = Mock()
        iracing.connect.return_value = False
        clock = Mock()
        clock.connect.return_value = True

        with (
            patch.object(main_module, "_STATUS_COLORS", {}),
            patch.object(main_module, "_STATUS_RESET", ""),
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                connect_startup_services(iracing, clock)

        out = buffer.getvalue()
        self.assertIn("[ OK ] OBS WebSocket", out)
        self.assertIn("[ .. ] iRacing", out)
        self.assertNotIn("[FAIL] iRacing", out)
        self.assertNotIn("\x1b[", out)  # never emit raw ANSI when degraded

    def test_startup_status_marks_obs_failure(self) -> None:
        iracing = Mock()
        iracing.connect.return_value = True
        clock = Mock()
        clock.connect.return_value = False

        with (
            patch.object(main_module, "_STATUS_COLORS", {}),
            patch.object(main_module, "_STATUS_RESET", ""),
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                connect_startup_services(iracing, clock)

        out = buffer.getvalue()
        self.assertIn("[FAIL] OBS WebSocket", out)
        self.assertIn("[ OK ] iRacing", out)

    def test_status_marker_is_color_wrapped_when_color_available(self) -> None:
        with (
            patch.object(main_module, "_STATUS_COLORS", {"ok": "<G>"}),
            patch.object(main_module, "_STATUS_RESET", "<R>"),
            patch.object(main_module, "_color_enabled", return_value=True),
        ):
            self.assertEqual(main_module._format_status("ok"), "<G>[ OK ]<R>")

    def test_status_marker_stays_plain_when_stdout_is_not_a_tty(self) -> None:
        # Redirected/non-tty stdout must never receive raw ANSI codes.
        with (
            patch.object(main_module, "_STATUS_COLORS", {"ok": "\x1b[32m"}),
            patch.object(main_module, "_STATUS_RESET", "\x1b[0m"),
        ):
            buffer = io.StringIO()  # StringIO.isatty() is False
            with contextlib.redirect_stdout(buffer):
                rendered = main_module._format_status("ok")
        self.assertEqual(rendered, "[ OK ]")
        self.assertNotIn("\x1b[", rendered)

    def test_obsws_connect_traceback_is_suppressed(self) -> None:
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        with tempfile.TemporaryDirectory() as temp_dir:
            configure_logging(Path(temp_dir) / "lastrun.log")
            sink = _Capture()
            logging.getLogger().addHandler(sink)
            try:
                # Mimic obsws-python logging its own TimeoutError traceback on a
                # failed connect (logger "obsws_python.baseclient.ObsClient").
                obs_logger = logging.getLogger("obsws_python.baseclient.ObsClient")
                try:
                    raise TimeoutError("timed out")
                except TimeoutError:
                    obs_logger.exception("Failed to connect to OBS")
                logging.getLogger("websocket").error("close status: 1001")
                # Our own clean failure line still gets through.
                logging.getLogger("livestream_spotter.obs.clock").warning(
                    "Could not reach OBS WebSocket at localhost:4455"
                )
            finally:
                logging.getLogger().removeHandler(sink)
                close_logging()

        names = [record.name for record in captured]
        messages = [record.getMessage() for record in captured]
        self.assertFalse(
            any(name.startswith("obsws_python") for name in names),
            f"obsws_python traceback leaked to handlers: {names}",
        )
        self.assertFalse(any(name.startswith("websocket") for name in names))
        self.assertTrue(
            any("Could not reach OBS WebSocket" in message for message in messages)
        )

    def test_lastrun_log_is_fresh_and_includes_debug(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "lastrun.log"
            log_path.write_text("stale run\n", encoding="utf-8")

            configure_logging(log_path)
            main_module.LOGGER.debug("resolved config path: test")
            close_logging()

            contents = log_path.read_text(encoding="utf-8")

        self.assertIn("resolved config path: test", contents)
        self.assertNotIn("stale run", contents)

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
