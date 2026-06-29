from types import SimpleNamespace
import unittest

from livestream_spotter.config import ObsConfig
from livestream_spotter.iracing import IRacingClient
from livestream_spotter.obs.clock import MockClock, ObsClock


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

        self.assertFalse(client.connect())
        self.assertFalse(client.connect())
        self.assertEqual(sdk.shutdown_calls, 1)

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

