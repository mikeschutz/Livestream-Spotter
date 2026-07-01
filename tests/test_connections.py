from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from livestream_spotter.config import ObsConfig
from livestream_spotter.iracing import IRacingClient, is_iracing_session_active
from livestream_spotter.obs.clock import (
    MockClock,
    ObsClock,
    _is_authentication_error,
    _is_reachability_error,
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


def output_status(active: bool, duration: int, paused: bool = False):
    return SimpleNamespace(
        output_active=active,
        output_duration=duration,
        output_paused=paused,
    )


class FakeObsRequestClient:
    def __init__(self, stream_statuses, record_statuses) -> None:
        self.stream_statuses = deque(stream_statuses)
        self.record_statuses = deque(record_statuses)
        self.stream_calls = 0
        self.record_calls = 0
        self.disconnect_calls = 0

    @staticmethod
    def _next(statuses):
        status = statuses.popleft()
        if isinstance(status, Exception):
            raise status
        return status

    def get_stream_status(self):
        self.stream_calls += 1
        return self._next(self.stream_statuses)

    def get_record_status(self):
        self.record_calls += 1
        return self._next(self.record_statuses)

    def disconnect(self) -> None:
        self.disconnect_calls += 1


class FakeCallback:
    def __init__(self) -> None:
        self.handlers = []

    def register(self, handlers) -> None:
        self.handlers.extend(handlers)


class FakeWorker:
    def __init__(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


class FakeObsEventClient:
    def __init__(self) -> None:
        self.callback = FakeCallback()
        self.worker = FakeWorker()
        self.disconnect_calls = 0

    def emit(self, name: str, **event_data) -> None:
        for handler in self.callback.handlers:
            if handler.__name__ == name:
                handler(SimpleNamespace(**event_data))
                return
        raise AssertionError(f"No callback named {name}")

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        worker = getattr(self, "worker", None)
        if worker is not None:
            worker.alive = False


def make_clock(
    request_client: FakeObsRequestClient,
    event_client: FakeObsEventClient,
    now: list[float],
    *,
    timestamp_source="auto",
) -> ObsClock:
    return ObsClock(
        ObsConfig("localhost", 4455, ""),
        timestamp_source=timestamp_source,
        client_factory=lambda **_: request_client,
        event_client_factory=lambda **_: event_client,
        monotonic=lambda: now[0],
    )


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

    def test_iracing_session_active_uses_sdk_connection_check(self) -> None:
        sdk = FakeSdk()
        client = IRacingClient(sdk_factory=lambda: sdk)

        self.assertFalse(is_iracing_session_active(client))
        client.connect()
        self.assertTrue(is_iracing_session_active(client))
        sdk.is_connected = False
        self.assertFalse(is_iracing_session_active(client))

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

        self.assertTrue(_is_authentication_error(OBSSDKError("auth rejected")))
        self.assertFalse(
            _is_authentication_error(OBSSDKTimeoutError("connect timed out"))
        )
        self.assertTrue(_is_reachability_error(OBSSDKTimeoutError("timed out")))

    def test_obs_connect_seeds_both_sources_and_registers_callbacks(self) -> None:
        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(True, 1_000)],
        )
        events = FakeObsEventClient()
        clock = make_clock(request, events, [10.0])

        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as logs:
            self.assertTrue(clock.connect())

        self.assertIn("Connected to OBS WebSocket at localhost:4455", logs.output[0])
        self.assertTrue(
            any(
                "OBS connected; recording=active, streaming=inactive" in line
                for line in logs.output
            )
        )
        self.assertEqual((request.stream_calls, request.record_calls), (1, 1))
        self.assertEqual(
            [handler.__name__ for handler in events.callback.handlers],
            ["on_stream_state_changed", "on_record_state_changed"],
        )
        clock.disconnect()
        clock.disconnect()
        self.assertEqual(request.disconnect_calls, 1)
        self.assertEqual(events.disconnect_calls, 1)

    def test_dead_event_worker_marks_disconnected_and_invalidates_cache(self) -> None:
        request = FakeObsRequestClient(
            [output_status(True, 5_000)],
            [output_status(False, 0)],
        )
        events = FakeObsEventClient()
        clock = make_clock(request, events, [10.0])
        self.assertTrue(clock.connect())
        self.assertEqual(clock.active_source(), "stream")

        events.worker.alive = False
        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as logs:
            self.assertFalse(clock.connected)

        self.assertIn("OBS WebSocket disconnected", logs.output[0])
        self.assertIsNone(clock.active_source())
        self.assertIsNone(clock.now_ms())

    def test_missing_event_worker_is_not_reported_as_connected(self) -> None:
        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(False, 0)],
        )
        events = FakeObsEventClient()
        events.worker = None
        clock = make_clock(request, events, [0.0])

        self.assertFalse(clock.connect())
        self.assertFalse(clock.connected)

    def test_reconnect_reseeds_and_reregisters_event_callbacks(self) -> None:
        first_request = FakeObsRequestClient(
            [output_status(True, 1_000)],
            [output_status(False, 0)],
        )
        second_request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(True, 8_000)],
        )
        first_events = FakeObsEventClient()
        second_events = FakeObsEventClient()
        requests = iter([first_request, second_request])
        event_clients = iter([first_events, second_events])
        clock = ObsClock(
            ObsConfig("localhost", 4455, ""),
            client_factory=lambda **_: next(requests),
            event_client_factory=lambda **_: next(event_clients),
            monotonic=lambda: 10.0,
        )
        self.assertTrue(clock.connect())

        first_events.worker.alive = False
        self.assertFalse(clock.connected)
        self.assertTrue(clock.connect())

        self.assertEqual(first_request.disconnect_calls, 1)
        self.assertEqual(first_events.disconnect_calls, 1)
        self.assertEqual(
            (second_request.stream_calls, second_request.record_calls),
            (1, 1),
        )
        self.assertEqual(clock.active_source(), "record")
        self.assertEqual(
            [handler.__name__ for handler in second_events.callback.handlers],
            ["on_stream_state_changed", "on_record_state_changed"],
        )

    def test_obs_connection_refused_reports_server_settings_hint(self) -> None:
        def refuse(**_):
            raise ConnectionRefusedError("no listener")

        clock = ObsClock(
            ObsConfig("studio-pc", 4456, ""),
            client_factory=refuse,
            event_client_factory=lambda **_: FakeObsEventClient(),
            monotonic=lambda: 0.0,
        )

        with self.assertLogs("livestream_spotter.obs.clock", level="WARNING") as logs:
            self.assertFalse(clock.connect())

        self.assertIn("Could not reach OBS WebSocket at studio-pc:4456", logs.output[0])
        self.assertIn("Tools -> WebSocket Server Settings", logs.output[0])

    def test_only_first_connect_failure_in_cycle_logs_without_traceback(self) -> None:
        now = [0.0]

        def refuse(**_):
            raise ConnectionRefusedError("no listener")

        clock = ObsClock(
            ObsConfig("localhost", 4455, ""),
            client_factory=refuse,
            event_client_factory=lambda **_: FakeObsEventClient(),
            monotonic=lambda: now[0],
            retry_seconds=2.0,
        )

        with self.assertLogs("livestream_spotter.obs.clock", level="DEBUG") as logs:
            self.assertFalse(clock.connect())
            now[0] = 2.0
            self.assertFalse(clock.connect())
            now[0] = 4.0
            self.assertFalse(clock.connect())

        warnings = [
            record for record in logs.records if record.levelname == "WARNING"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertIn("Could not reach OBS WebSocket", warnings[0].getMessage())
        self.assertIsNone(warnings[0].exc_info)
        self.assertEqual(logs.records, warnings)

    def test_unexpected_connect_exception_propagates(self) -> None:
        def explode(**_):
            raise ValueError("unexpected response shape")

        clock = ObsClock(
            ObsConfig("localhost", 4455, ""),
            client_factory=explode,
            event_client_factory=lambda **_: FakeObsEventClient(),
            monotonic=lambda: 0.0,
        )

        with self.assertRaisesRegex(ValueError, "unexpected response shape"):
            clock.connect()

    def test_successful_connect_resets_failure_warning_cycle(self) -> None:
        now = [0.0]
        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(False, 0)],
        )
        outcomes = iter(
            [
                ConnectionRefusedError("first outage"),
                request,
                ConnectionRefusedError("second outage"),
            ]
        )

        def request_factory(**_):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        clock = ObsClock(
            ObsConfig("localhost", 4455, ""),
            client_factory=request_factory,
            event_client_factory=lambda **_: FakeObsEventClient(),
            monotonic=lambda: now[0],
            retry_seconds=2.0,
        )

        with self.assertLogs("livestream_spotter.obs.clock", level="DEBUG") as logs:
            self.assertFalse(clock.connect())
            now[0] = 2.0
            self.assertTrue(clock.connect())
            clock.disconnect()
            now[0] = 4.0
            self.assertFalse(clock.connect())

        warnings = [
            record for record in logs.records if record.levelname == "WARNING"
        ]
        self.assertEqual(len(warnings), 2)

    def test_obs_auth_rejection_reports_password_hint(self) -> None:
        def reject(**_):
            raise RuntimeError("authentication rejected")

        clock = ObsClock(
            ObsConfig("localhost", 4455, "wrong"),
            client_factory=reject,
            event_client_factory=lambda **_: FakeObsEventClient(),
            monotonic=lambda: 0.0,
        )

        with (
            patch(
                "livestream_spotter.obs.clock._is_authentication_error",
                return_value=True,
            ),
            self.assertLogs("livestream_spotter.obs.clock", level="WARNING") as logs,
        ):
            self.assertFalse(clock.connect())

        self.assertIn("Connected to OBS but authentication failed", logs.output[0])
        self.assertIn("password in config.toml matches OBS", logs.output[0])

    def test_stream_offset_math_uses_no_hot_path_requests(self) -> None:
        now = [10.0]
        request = FakeObsRequestClient(
            [output_status(True, 5_000)],
            [output_status(False, 0)],
        )
        clock = make_clock(request, FakeObsEventClient(), now)
        self.assertTrue(clock.connect())

        now[0] = 11.25
        self.assertEqual(clock.read().video_ms, 6_250)
        now[0] = 12.0
        self.assertEqual(clock.read().video_ms, 7_000)
        self.assertEqual((request.stream_calls, request.record_calls), (1, 1))

    def test_recording_only_uses_record_timeline(self) -> None:
        now = [20.0]
        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(True, 30_000)],
        )
        clock = make_clock(request, FakeObsEventClient(), now)
        clock.connect()

        now[0] = 20.5
        self.assertEqual(clock.active_source(), "record")
        self.assertEqual(clock.read().video_ms, 30_500)

    def test_record_stop_event_immediately_marks_output_inactive(self) -> None:
        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(True, 30_000)],
        )
        events = FakeObsEventClient()
        clock = make_clock(request, events, [20.0])
        self.assertTrue(clock.connect())
        self.assertEqual(clock.active_source(), "record")

        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as logs:
            events.emit(
                "on_record_state_changed",
                output_active=False,
                output_state="OBS_WEBSOCKET_OUTPUT_STOPPED",
            )

        self.assertIn("OBS recording stopped", logs.output[0])
        self.assertIsNone(clock.active_source())
        self.assertIsNone(clock.now_ms())
        self.assertTrue(clock.connected)
        self.assertEqual(request.record_calls, 1)

    def test_obsws_callback_dispatches_protocol_record_event_name(self) -> None:
        from obsws_python.callback import Callback

        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(True, 30_000)],
        )
        events = FakeObsEventClient()
        events.callback = Callback()
        clock = make_clock(request, events, [20.0])
        self.assertTrue(clock.connect())

        events.callback.trigger(
            "RecordStateChanged",
            {
                "outputActive": False,
                "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED",
            },
        )

        self.assertIsNone(clock.active_source())
        self.assertIsNone(clock.now_ms())

    def test_record_start_event_resamples_fresh_output_duration(self) -> None:
        now = [20.0]
        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(False, 0), output_status(True, 500)],
        )
        events = FakeObsEventClient()
        clock = make_clock(request, events, now)
        self.assertTrue(clock.connect())
        self.assertIsNone(clock.active_source())

        now[0] = 21.0
        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as logs:
            events.emit(
                "on_record_state_changed",
                output_active=True,
                output_state="OBS_WEBSOCKET_OUTPUT_STARTED",
            )

        self.assertIn("OBS recording started", logs.output[0])
        self.assertEqual(request.record_calls, 2)
        self.assertEqual(clock.active_source(), "record")
        self.assertEqual(clock.now_ms(), 500)

    def test_auto_prefers_stream_when_both_outputs_are_active(self) -> None:
        now = [5.0]
        request = FakeObsRequestClient(
            [output_status(True, 1_000)],
            [output_status(True, 9_000)],
        )
        clock = make_clock(request, FakeObsEventClient(), now)
        clock.connect()

        self.assertEqual(clock.active_source(), "stream")
        self.assertEqual(clock.read().video_ms, 1_000)

    def test_explicit_source_does_not_fall_back(self) -> None:
        now = [5.0]
        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(True, 9_000)],
        )
        clock = make_clock(
            request,
            FakeObsEventClient(),
            now,
            timestamp_source="stream",
        )
        clock.connect()

        self.assertIsNone(clock.active_source())
        self.assertIsNone(clock.read().video_ms)

    def test_explicit_record_uses_record_when_both_outputs_are_active(self) -> None:
        now = [5.0]
        request = FakeObsRequestClient(
            [output_status(True, 1_000)],
            [output_status(True, 9_000)],
        )
        clock = make_clock(
            request,
            FakeObsEventClient(),
            now,
            timestamp_source="record",
        )
        clock.connect()

        self.assertEqual(clock.active_source(), "record")
        self.assertEqual(clock.read().video_ms, 9_000)

    def test_auto_switches_from_record_to_stream_and_logs(self) -> None:
        now = [10.0]
        request = FakeObsRequestClient(
            [output_status(False, 0), output_status(True, 2_000)],
            [output_status(True, 8_000)],
        )
        events = FakeObsEventClient()
        clock = make_clock(request, events, now)
        clock.connect()
        self.assertEqual(clock.read().video_ms, 8_000)

        now[0] = 11.0
        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as event_logs:
            events.emit(
                "on_stream_state_changed",
                output_active=True,
                output_state="OBS_WEBSOCKET_OUTPUT_STARTED",
            )
        self.assertIn("OBS streaming started", event_logs.output[0])
        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as logs:
            reading = clock.read()

        self.assertEqual(reading.video_ms, 2_000)
        self.assertIn("Timestamp source switched: record -> stream", logs.output[0])

    def test_auto_switches_from_stream_to_record_and_logs(self) -> None:
        now = [10.0]
        request = FakeObsRequestClient(
            [output_status(True, 1_000), output_status(False, 1_500)],
            [output_status(True, 8_000)],
        )
        events = FakeObsEventClient()
        clock = make_clock(request, events, now)
        clock.connect()
        self.assertEqual(clock.read().video_ms, 1_000)

        now[0] = 10.5
        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as event_logs:
            events.emit(
                "on_stream_state_changed",
                output_active=False,
                output_state="OBS_WEBSOCKET_OUTPUT_STOPPED",
            )
        self.assertIn("OBS streaming stopped", event_logs.output[0])
        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as logs:
            reading = clock.read()

        self.assertEqual(clock.active_source(), "record")
        self.assertEqual(reading.video_ms, 8_500)
        self.assertIn("Timestamp source switched: stream -> record", logs.output[0])

    def test_record_pause_freezes_projection_until_resampled_on_resume(self) -> None:
        now = [10.0]
        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [
                output_status(True, 10_000),
                output_status(True, 12_000, paused=True),
                output_status(True, 12_000, paused=False),
            ],
        )
        events = FakeObsEventClient()
        clock = make_clock(request, events, now)
        clock.connect()

        now[0] = 12.0
        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as logs:
            events.emit(
                "on_record_state_changed",
                output_active=True,
                output_state="OBS_WEBSOCKET_OUTPUT_PAUSED",
            )
        now[0] = 30.0
        self.assertEqual(clock.read().video_ms, 12_000)

        with self.assertLogs("livestream_spotter.obs.clock", level="INFO") as resume_logs:
            events.emit(
                "on_record_state_changed",
                output_active=True,
                output_state="OBS_WEBSOCKET_OUTPUT_RESUMED",
            )
        now[0] = 31.0
        self.assertEqual(clock.read().video_ms, 13_000)
        self.assertEqual(request.record_calls, 3)
        self.assertIn("OBS recording paused", logs.output[0])
        self.assertIn("OBS recording resumed", resume_logs.output[0])

    def test_seed_failure_disconnects_both_clients(self) -> None:
        request = FakeObsRequestClient(
            [OSError("gone")],
            [output_status(False, 0)],
        )
        events = FakeObsEventClient()
        clock = make_clock(request, events, [0.0])

        self.assertFalse(clock.connect())
        self.assertFalse(clock.connected)
        self.assertEqual(request.disconnect_calls, 1)
        self.assertEqual(events.disconnect_calls, 1)

    def test_event_subscription_failure_disconnects_request_client(self) -> None:
        request = FakeObsRequestClient(
            [output_status(False, 0)],
            [output_status(False, 0)],
        )

        def fail_subscription(**_):
            raise OSError("event socket unavailable")

        clock = ObsClock(
            ObsConfig("localhost", 4455, ""),
            client_factory=lambda **_: request,
            event_client_factory=fail_subscription,
            monotonic=lambda: 0.0,
        )

        self.assertFalse(clock.connect())
        self.assertEqual(request.disconnect_calls, 1)

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
