from pathlib import Path
import tempfile
import unittest

from livestream_spotter.config import load_config


CONFIG = b"""
[runtime]
poll_hz = 20
hold_until_stream_active = false
[obs]
host = "obs-box"
port = 4456
password = "secret"
[output]
raw_dump_path = "capture/raw.jsonl"
timestamps_path = "output/timestamps.txt"
chapters_path = "output/chapters.txt"
[diagnostics]
raw_dump_enabled = true
raw_dump_hz = 0.5
[lead_in_seconds]
green = 1.5
restart = 3
[chapters]
min_spacing = 12.5
[timestamps]
min_spacing = 1.5
[detectors]
enabled = ["green", "restart"]
"""


class ConfigTests(unittest.TestCase):
    def test_loads_phase_two_settings_and_resolves_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_bytes(CONFIG)

            config = load_config(config_path)

            self.assertEqual(config.poll_hz, 20)
            self.assertFalse(config.hold_until_stream_active)
            self.assertTrue(config.race_only_player_events)
            self.assertTrue(config.raw_dump_enabled)
            self.assertEqual(config.raw_dump_hz, 0.5)
            self.assertEqual(config.obs.host, "obs-box")
            self.assertEqual(config.obs.port, 4456)
            self.assertEqual(
                config.raw_dump_path,
                config_path.parent / "capture" / "raw.jsonl",
            )
            self.assertEqual(
                config.timestamps_path,
                config_path.parent / "output" / "timestamps.txt",
            )
            self.assertEqual(
                config.chapters_path,
                config_path.parent / "output" / "chapters.txt",
            )
            self.assertEqual(config.timestamp_min_spacing_ms, 1500)
            self.assertEqual(config.chapter_min_spacing_ms, 12500)
            self.assertEqual(config.enabled_renderers, frozenset({"timestamps"}))
            self.assertEqual(config.lead_in_ms["green"], 1500)
            self.assertEqual(config.lead_in_ms["restart"], 3000)
            self.assertEqual(config.lead_in_ms["pit_in"], 0)
            self.assertEqual(config.lead_in_ms["incident"], 6000)
            self.assertEqual(config.lead_in_ms["white"], 0)
            self.assertEqual(config.enabled_detectors, frozenset({"green", "restart"}))
            self.assertEqual(config.battle_gap_threshold, 0.6)
            self.assertEqual(config.battle_min_duration, 9.0)
            self.assertEqual(config.battle_throttle_window, 60.0)
            self.assertEqual(config.incident_minimum_delta, 1)

    def test_raw_dump_is_off_by_default(self) -> None:
        without_diagnostics = CONFIG.replace(
            b"[diagnostics]\nraw_dump_enabled = true\nraw_dump_hz = 0.5\n", b""
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_bytes(without_diagnostics)

            config = load_config(config_path)

            self.assertFalse(config.raw_dump_enabled)
            self.assertEqual(config.raw_dump_hz, 1.0)

    def test_race_only_player_events_can_be_disabled(self) -> None:
        configured = CONFIG.replace(
            b"hold_until_stream_active = false",
            b"hold_until_stream_active = false\nrace_only_player_events = false",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_bytes(configured)

            config = load_config(config_path)

            self.assertFalse(config.race_only_player_events)

    def test_rejects_non_positive_poll_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_bytes(CONFIG.replace(b"poll_hz = 20", b"poll_hz = 0"))

            with self.assertRaisesRegex(ValueError, "poll_hz"):
                load_config(config_path)

    def test_rejects_negative_lead_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_bytes(CONFIG.replace(b"green = 1.5", b"green = -1"))

            with self.assertRaisesRegex(ValueError, "green"):
                load_config(config_path)

    def test_rejects_chapter_spacing_below_youtube_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_bytes(
                CONFIG.replace(b"min_spacing = 12.5", b"min_spacing = 9.9")
            )

            with self.assertRaisesRegex(ValueError, "min_spacing"):
                load_config(config_path)

    def test_rejects_negative_timestamp_spacing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_bytes(
                CONFIG.replace(b"min_spacing = 1.5", b"min_spacing = -1")
            )

            with self.assertRaisesRegex(ValueError, "timestamps.min_spacing"):
                load_config(config_path)

    def test_chapters_renderer_can_be_selected(self) -> None:
        configured = CONFIG.replace(
            b'raw_dump_path = "capture/raw.jsonl"',
            b'raw_dump_path = "capture/raw.jsonl"\nrenderers = ["chapters"]',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_bytes(configured)

            config = load_config(config_path)

            self.assertEqual(config.enabled_renderers, frozenset({"chapters"}))


if __name__ == "__main__":
    unittest.main()
