from types import SimpleNamespace
import unittest
from unittest.mock import patch

from livestream_spotter.config import ObsConfig
from livestream_spotter.iracing import IRacingClient
from livestream_spotter.obs.clock import (
    MockClock,
    ObsClock,
    _is_authentication_error,
)


class FakeSdk:
    def __init__(self, starts: bool = True) -> None:
        self.starts = starts
        self.is_connected = starts
        self.shutdown_calls = 0
        self.freeze_calls = 0
        self.unfreeze_calls = 0
        self.values = {"SessionTime": 12.5}

    def startup(self) -> bool:
        return self.starts

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.is_connected = False

    def freeze_var_buffer_latest(self) -> None:
        self.freeze_calls += 1

    def unfreeze_var_buffer_latest(self) -> None:
        self.unfreeze_calls += 1

    def __getitem__(self, field: str):
        return self.values.get(field)


class FakeObsClient:
    def __init__(self, statuses) -> None:
        self.statuses = iter(statuses)
        self.disconnect_calls = 0

    def get_stream_status(self):
        status = next(self.statuses)
        if isinstance(status, Exception):
            raise status
        return status

    def disconnect(self) -> None:
        self.disconnect_calls += 1


class ConnectionTests(unittest.TestCase):
    def test_iracing_capture_is_frozen_and_disconnect_is_idempotent(self) -> None:
        sdk = FakeSdk()
        client = IRacingClient(sdk_factory=lambda: sdk)

        self.assertTrue(client.connect())
        self.assertEqual(client.capture(("SessionTime",)), {"SessionTime": 12.5})
        self.assertEqual((sdk.freeze_calls, sdk.unfreeze_calls), (1, 1))

        client.disconnect()
        client.disconnect()
        self.assertEqual(sdk.shutdown_calls, 1)

    def test_iracing_absence_is_a_retryable_state(self) -> None:
        sdk = FakeSdk(starts=False)
        client = IRacingClient(
            sdk_factory=lambda: sdk,
            monotonic=lambda: 0.0,
            retry_seconds=2.0,
        )

        with self.assertLogs("livestream_spotter.iracing", level="INFO") as logs:
            self.assertFalse(client.connect())
        self.assertFalse(client.connect())
        self.assertEqual(sdk.shutdown_calls, 1)
        self.assertIn(
            "Waiting for iRacing session (start iRacing when ready)",
            logs.output[0],
        )
        self.assertNotIn("failed", logs.output[0].lower())

    def test_timeout_is_not_classified_as_authentication_error(self) -> None:
        from obsws_python.error import OBSSDKError, OBSSDKTimeoutError

        # A bare auth error stays auth; a timeout subclass must not.
        self.assertTrue(_is_authentication_error(OBSSDKError("auth rejected")))
        self.assertFalse(
            _is_authentication_error(OBSSDKTimeoutError("connect timed out"))
        )

    def test_obs_connection_success_reports_endpoint(self) -> None:
        obs = FakeObsClient([])
        clock = ObsClock(
            ObsConfig("studio-pc", 4455, ""),
            client_factory=lambda **_: obs,
        )

        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as logs:
            self.assertTrue(clock.connect())

        self.assertIn(
            "Connected to OBS WebSocket at studio-pc:4455",
            logs.output[0],
        )
        clock.disconnect()

    def test_obs_connection_refused_reports_server_settings_hint(self) -> None:
        def refuse(**_):
            raise ConnectionRefusedError("no listener")

        clock = ObsClock(
            ObsConfig("studio-pc", 4456, ""),
            client_factory=refuse,
            monotonic=lambda: 0.0,
        )

        with self.assertLogs("livestream_spotter.obs.clock", level="WARNING") as logs:
            self.assertFalse(clock.connect())

        self.assertIn(
            "Could not reach OBS WebSocket at studio-pc:4456",
            logs.output[0],
        )
        self.assertIn("Tools -> WebSocket Server Settings", logs.output[0])

    def test_obs_auth_rejection_reports_password_hint(self) -> None:
        def reject(**_):
            raise RuntimeError("authentication rejected")

        clock = ObsClock(
            ObsConfig("localhost", 4455, "wrong"),
            client_factory=reject,
            monotonic=lambda: 0.0,
        )

        with (
            patch(
                "livestream_spotter.obs.clock._is_authentication_error",
                return_value=True,
            ),
            self.assertLogs(
                "livestream_spotter.obs.clock", level="WARNING"
            ) as logs,
        ):
            self.assertFalse(clock.connect())

        self.assertIn(
            "Connected to OBS but authentication failed",
            logs.output[0],
        )
        self.assertIn("password in config.toml matches OBS", logs.output[0])

    def test_obs_reads_only_active_output_duration(self) -> None:
        obs = FakeObsClient(
            [
                SimpleNamespace(output_active=False, output_duration=999),
                SimpleNamespace(output_active=True, output_duration=1234),
            ]
        )
        clock = ObsClock(
            ObsConfig("localhost", 4455, ""),
            client_factory=lambda **_: obs,
        )

        inactive = clock.read()
        self.assertEqual(obs.disconnect_calls, 0)
        active = clock.read()

        self.assertFalse(inactive.output_active)
        self.assertIsNone(inactive.video_ms)
        self.assertEqual(active.video_ms, 1234)
        self.assertEqual(active.source, "obs")
        clock.disconnect()
        clock.disconnect()
        self.assertEqual(obs.disconnect_calls, 1)

    def test_obs_drop_disconnects_and_returns_unavailable_reading(self) -> None:
        obs = FakeObsClient([OSError("gone")])
        clock = ObsClock(
            ObsConfig("localhost", 4455, ""),
            client_factory=lambda **_: obs,
        )

        reading = clock.read()

        self.assertFalse(reading.output_active)
        self.assertIsNone(reading.video_ms)
        self.assertEqual(obs.disconnect_calls, 1)

    def test_obs_drop_spaces_reconnect_attempts(self) -> None:
        now = [0.0]
        clients = iter(
            [
                FakeObsClient([OSError("gone")]),
                FakeObsClient([]),
            ]
        )
        factory_calls = 0

        def factory(**_):
            nonlocal factory_calls
            factory_calls += 1
            return next(clients)

        clock = ObsClock(
            ObsConfig("localhost", 4455, ""),
            client_factory=factory,
            monotonic=lambda: now[0],
            retry_seconds=2.0,
        )

        clock.read()
        self.assertFalse(clock.connect())
        self.assertEqual(factory_calls, 1)

        now[0] = 2.0
        self.assertTrue(clock.connect())
        self.assertEqual(factory_calls, 2)
        clock.disconnect()

    def test_mock_clock_is_monotonic_and_resets_on_disconnect(self) -> None:
        readings = iter([10.0, 10.25, 20.0, 20.1])
        clock = MockClock(monotonic=lambda: next(readings))

        self.assertTrue(clock.connect())
        self.assertEqual(clock.read().video_ms, 250)
        clock.disconnect()
        self.assertTrue(clock.connect())
        self.assertEqual(clock.read().video_ms, 100)


if __name__ == "__main__":
    unittest.main()
