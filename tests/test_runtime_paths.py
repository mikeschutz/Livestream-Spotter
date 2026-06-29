from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from livestream_spotter.config import load_config
from livestream_spotter.runtime_paths import (
    missing_config_message,
    resolve_config_path,
)


CONFIG = b"""
[runtime]
poll_hz = 15
hold_until_stream_active = false
[obs]
host = "localhost"
port = 4455
password = ""
[output]
raw_dump_path = "raw_dump.jsonl"
timestamps_path = "timestamps.txt"
chapters_path = "chapters.txt"
[detectors]
enabled = ["green"]
"""


class RuntimePathTests(unittest.TestCase):
    def test_source_run_keeps_relative_config_path(self) -> None:
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(resolve_config_path(Path("config.toml")), Path("config.toml"))

    def test_frozen_run_resolves_relative_config_beside_exe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe_path = Path(temp_dir) / "livestream-spotter.exe"
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(exe_path)),
            ):
                self.assertEqual(
                    resolve_config_path(Path("config.toml")),
                    Path(temp_dir) / "config.toml",
                )

    def test_frozen_absolute_config_path_is_not_rebased(self) -> None:
        absolute = Path("C:/custom/config.toml")
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(resolve_config_path(absolute), absolute)

    def test_frozen_config_makes_outputs_resolve_beside_exe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_bytes(CONFIG)
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(root / "livestream-spotter.exe")),
            ):
                config = load_config(resolve_config_path(Path("config.toml")))

            self.assertEqual(config.raw_dump_path, root / "raw_dump.jsonl")
            self.assertEqual(config.timestamps_path, root / "timestamps.txt")
            self.assertEqual(config.chapters_path, root / "chapters.txt")

    def test_missing_frozen_config_message_tells_user_where_to_put_it(self) -> None:
        with patch.object(sys, "frozen", True, create=True):
            message = missing_config_message(Path("C:/release/config.toml"))

        self.assertIn("Place config.toml beside livestream-spotter.exe", message)


if __name__ == "__main__":
    unittest.main()
