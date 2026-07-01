from collections import deque
import unittest

from livestream_spotter.lifecycle import (
    LifecycleController,
    LifecycleState,
    LifecycleStateMachine,
    LifecycleTrigger,
)


class FakeIRacing:
    def __init__(self, connected: bool, connect_results=()) -> None:
        self.connected = connected
        self.connect_results = deque(connect_results)
        self.connect_calls = 0

    def connect(self) -> bool:
        self.connect_calls += 1
        if self.connect_results:
            self.connected = self.connect_results.popleft()
        return self.connected


class FakeClock:
    def __init__(self, connected: bool, connect_results=()) -> None:
        self.connected = connected
        self.connect_results = deque(connect_results)
        self.connect_calls = 0

    def connect(self) -> bool:
        self.connect_calls += 1
        if self.connect_results:
            self.connected = self.connect_results.popleft()
        return self.connected


class FakePipeline:
    def __init__(self) -> None:
        self.active = False
        self.activate_calls = 0
        self.deactivate_calls = 0
        self.reset_session_calls = 0
        self.tick_calls = 0

    def activate(self) -> None:
        self.active = True
        self.activate_calls += 1

    def deactivate(self) -> None:
        self.active = False
        self.deactivate_calls += 1

    def reset_session(self) -> None:
        self.reset_session_calls += 1

    def tick(self) -> bool:
        self.tick_calls += 1
        return True


def machine(iracing: bool, obs: bool) -> LifecycleStateMachine:
    return LifecycleStateMachine(iracing_active=iracing, obs_connected=obs)


def signal(
    state_machine: LifecycleStateMachine,
    trigger: LifecycleTrigger,
    *,
    iracing: bool,
    obs: bool,
) -> LifecycleState:
    return state_machine.on_signal(
        trigger,
        iracing_active=iracing,
        obs_connected=obs,
    )


class StartupTests(unittest.TestCase):
    def test_startup_four_way_matrix(self) -> None:
        cases = {
            (False, False): LifecycleState.IDLE,
            (False, True): LifecycleState.WAITING_FOR_IRACING,
            (True, False): LifecycleState.WAITING_FOR_OBS,
            (True, True): LifecycleState.ACTIVE,
        }
        for (iracing, obs), expected in cases.items():
            with self.subTest(iracing=iracing, obs=obs):
                self.assertEqual(machine(iracing, obs).current_state, expected)


class TransitionTableTests(unittest.TestCase):
    def test_idle_session_start_with_obs_up_goes_active(self) -> None:
        state_machine = machine(False, False)
        self.assertEqual(
            signal(
                state_machine,
                LifecycleTrigger.IRACING_SESSION_START,
                iracing=True,
                obs=True,
            ),
            LifecycleState.ACTIVE,
        )

    def test_idle_session_start_with_obs_down_waits_for_obs(self) -> None:
        state_machine = machine(False, False)
        self.assertEqual(
            signal(
                state_machine,
                LifecycleTrigger.IRACING_SESSION_START,
                iracing=True,
                obs=False,
            ),
            LifecycleState.WAITING_FOR_OBS,
        )

    def test_waiting_for_iracing_session_start_goes_active(self) -> None:
        state_machine = machine(False, True)
        self.assertEqual(
            signal(
                state_machine,
                LifecycleTrigger.IRACING_SESSION_START,
                iracing=True,
                obs=True,
            ),
            LifecycleState.ACTIVE,
        )

    def test_waiting_for_iracing_obs_disconnect_goes_idle(self) -> None:
        state_machine = machine(False, True)
        self.assertEqual(
            signal(
                state_machine,
                LifecycleTrigger.OBS_DISCONNECT,
                iracing=False,
                obs=False,
            ),
            LifecycleState.IDLE,
        )

    def test_waiting_for_obs_connect_goes_active(self) -> None:
        state_machine = machine(True, False)
        self.assertEqual(
            signal(
                state_machine,
                LifecycleTrigger.OBS_CONNECT,
                iracing=True,
                obs=True,
            ),
            LifecycleState.ACTIVE,
        )

    def test_waiting_for_obs_session_end_goes_idle(self) -> None:
        state_machine = machine(True, False)
        self.assertEqual(
            signal(
                state_machine,
                LifecycleTrigger.IRACING_SESSION_END,
                iracing=False,
                obs=False,
            ),
            LifecycleState.IDLE,
        )

    def test_active_session_end_with_obs_up_waits_for_iracing(self) -> None:
        state_machine = machine(True, True)
        self.assertEqual(
            signal(
                state_machine,
                LifecycleTrigger.IRACING_SESSION_END,
                iracing=False,
                obs=True,
            ),
            LifecycleState.WAITING_FOR_IRACING,
        )

    def test_active_obs_disconnect_waits_for_obs(self) -> None:
        state_machine = machine(True, True)
        self.assertEqual(
            signal(
                state_machine,
                LifecycleTrigger.OBS_DISCONNECT,
                iracing=True,
                obs=False,
            ),
            LifecycleState.WAITING_FOR_OBS,
        )

    def test_active_combined_disconnect_goes_idle(self) -> None:
        state_machine = machine(True, True)
        self.assertEqual(
            signal(
                state_machine,
                LifecycleTrigger.IRACING_SESSION_END,
                iracing=False,
                obs=False,
            ),
            LifecycleState.IDLE,
        )

    def test_duplicate_or_out_of_state_signal_is_idempotent(self) -> None:
        transitions = []
        state_machine = LifecycleStateMachine(
            iracing_active=False,
            obs_connected=False,
            on_transition=lambda old, new: transitions.append((old, new)),
        )

        signal(
            state_machine,
            LifecycleTrigger.IRACING_SESSION_END,
            iracing=False,
            obs=False,
        )
        signal(
            state_machine,
            LifecycleTrigger.IRACING_SESSION_END,
            iracing=False,
            obs=False,
        )

        self.assertEqual(state_machine.current_state, LifecycleState.IDLE)
        self.assertEqual(transitions, [])

    def test_transition_log_has_consistent_trigger_format(self) -> None:
        state_machine = machine(False, False)

        with self.assertLogs("livestream_spotter.lifecycle", level="INFO") as logs:
            signal(
                state_machine,
                LifecycleTrigger.IRACING_SESSION_START,
                iracing=True,
                obs=False,
            )

        self.assertIn(
            "Lifecycle: IDLE → WAITING_FOR_OBS "
            "(trigger: iracing_session_start)",
            logs.output[0],
        )


class ControllerTests(unittest.TestCase):
    def make_controller(
        self,
        *,
        iracing: FakeIRacing,
        clock: FakeClock,
        pipeline: FakePipeline,
        now: list[float],
    ) -> LifecycleController:
        return LifecycleController(
            iracing=iracing,
            clock=clock,
            pipeline=pipeline,
            poll_hz=15.0,
            startup_iracing_active=iracing.connected,
            startup_obs_connected=clock.connected,
            monotonic=lambda: now[0],
        )

    def test_obs_retry_uses_fixed_ten_second_wall_clock_interval(self) -> None:
        now = [0.0]
        iracing = FakeIRacing(True)
        clock = FakeClock(False, [False, False])
        pipeline = FakePipeline()
        with self.assertLogs("livestream_spotter.lifecycle", level="DEBUG") as logs:
            controller = self.make_controller(
                iracing=iracing,
                clock=clock,
                pipeline=pipeline,
                now=now,
            )
        polling_logs = [
            record.getMessage()
            for record in logs.records
            if record.getMessage()
            == "Polling OBS every 10s until it becomes available"
        ]
        self.assertEqual(len(polling_logs), 1)

        now[0] = 9.999
        controller.tick()
        self.assertEqual(clock.connect_calls, 0)
        now[0] = 10.0
        with self.assertNoLogs("livestream_spotter.lifecycle", level="DEBUG"):
            controller.tick()
        self.assertEqual(clock.connect_calls, 1)
        now[0] = 19.999
        with self.assertNoLogs("livestream_spotter.lifecycle", level="DEBUG"):
            controller.tick()
            self.assertEqual(clock.connect_calls, 1)
            now[0] = 20.0
            controller.tick()
        self.assertEqual(clock.connect_calls, 2)
        self.assertEqual(controller.current_state, LifecycleState.WAITING_FOR_OBS)
        self.assertEqual(pipeline.tick_calls, 0)

    def test_iracing_end_stops_obs_retry_immediately(self) -> None:
        now = [0.0]
        iracing = FakeIRacing(True, [False])
        clock = FakeClock(False, [True])
        pipeline = FakePipeline()
        controller = self.make_controller(
            iracing=iracing,
            clock=clock,
            pipeline=pipeline,
            now=now,
        )

        iracing.connected = False
        now[0] = 10.0
        controller.tick()

        self.assertEqual(controller.current_state, LifecycleState.IDLE)
        self.assertEqual(clock.connect_calls, 0)
        self.assertEqual(pipeline.reset_session_calls, 1)

    def test_mid_session_obs_disconnect_reconnect_cycle(self) -> None:
        now = [0.0]
        iracing = FakeIRacing(True)
        clock = FakeClock(True, [True])
        pipeline = FakePipeline()
        controller = self.make_controller(
            iracing=iracing,
            clock=clock,
            pipeline=pipeline,
            now=now,
        )

        self.assertEqual(controller.current_state, LifecycleState.ACTIVE)
        self.assertEqual(pipeline.activate_calls, 1)
        clock.connected = False
        now[0] = 1.0
        with self.assertLogs("livestream_spotter.lifecycle", level="DEBUG") as logs:
            controller.tick()
        self.assertEqual(controller.current_state, LifecycleState.WAITING_FOR_OBS)
        polling_logs = [
            record.getMessage()
            for record in logs.records
            if record.getMessage()
            == "Polling OBS every 10s until it becomes available"
        ]
        self.assertEqual(len(polling_logs), 1)
        self.assertEqual(pipeline.deactivate_calls, 1)
        self.assertEqual(pipeline.reset_session_calls, 0)
        self.assertEqual(pipeline.tick_calls, 0)

        now[0] = 10.0
        controller.tick()
        self.assertEqual(controller.current_state, LifecycleState.ACTIVE)
        self.assertEqual(pipeline.activate_calls, 2)
        self.assertEqual(pipeline.tick_calls, 1)

    def test_connected_obs_stays_warm_between_iracing_sessions(self) -> None:
        now = [0.0]
        iracing = FakeIRacing(True, [False, True])
        clock = FakeClock(True)
        pipeline = FakePipeline()
        controller = self.make_controller(
            iracing=iracing,
            clock=clock,
            pipeline=pipeline,
            now=now,
        )

        iracing.connected = False
        controller.tick()
        self.assertEqual(
            controller.current_state,
            LifecycleState.WAITING_FOR_IRACING,
        )
        self.assertTrue(clock.connected)
        self.assertEqual(clock.connect_calls, 0)
        self.assertEqual(pipeline.reset_session_calls, 1)

        controller.tick()
        self.assertEqual(controller.current_state, LifecycleState.ACTIVE)
        self.assertEqual(pipeline.activate_calls, 2)


if __name__ == "__main__":
    unittest.main()
