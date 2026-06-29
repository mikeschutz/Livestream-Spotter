import unittest

from livestream_spotter.detectors.reliable import (
    FLAG_CAUTION,
    FLAG_CAUTION_WAVING,
    FLAG_CHECKERED,
    FLAG_GREEN_HELD,
    FLAG_ONE_LAP_TO_GREEN,
    FLAG_WHITE,
    STATE_CHECKERED,
    STATE_RACING,
    detect_caution,
    detect_checkered,
    detect_green,
    detect_pit,
    detect_restart,
    detect_session_transition,
    detect_white,
)

FLAG_GREEN = 0x00000004


SESSIONS = [
    {"SessionNum": 0, "SessionType": "Practice"},
    {"SessionNum": 1, "SessionType": "Qualify"},
    {"SessionNum": 2, "SessionType": "Race"},
]


def snapshot(**overrides):
    values = {
        "SessionNum": 2,
        "SessionState": 3,
        "SessionFlags": 0,
        "SessionTime": 100.0,
        "SessionInfo": {"Sessions": SESSIONS},
        "Lap": 7,
        "OnPitRoad": False,
    }
    values.update(overrides)
    return values


class SessionTransitionTests(unittest.TestCase):
    def test_practice_to_qualify_and_qualify_to_race(self) -> None:
        practice = snapshot(SessionNum=0)
        qualify = snapshot(SessionNum=1)
        race = snapshot(SessionNum=2)

        first = detect_session_transition(practice, qualify)
        second = detect_session_transition(qualify, race)

        self.assertEqual(first[0].label, "Qualify session")
        self.assertEqual(second[0].label, "Race session")
        self.assertEqual(
            second[0].meta,
            {
                "from_session_num": 1,
                "to_session_num": 2,
                "from_session_type": "Qualify",
                "to_session_type": "Race",
            },
        )

    def test_steady_session_does_not_double_fire(self) -> None:
        self.assertEqual(
            detect_session_transition(snapshot(SessionNum=1), snapshot(SessionNum=1)),
            [],
        )

    def test_missing_session_number_does_not_fire(self) -> None:
        self.assertEqual(
            detect_session_transition(snapshot(SessionNum=None), snapshot()), []
        )


class GreenTests(unittest.TestCase):
    def test_entering_racing_starts_the_race(self) -> None:
        events = detect_green(
            snapshot(SessionState=3), snapshot(SessionState=STATE_RACING)
        )

        self.assertEqual([event.event_type for event in events], ["green"])
        self.assertEqual(events[0].lap, 7)

    def test_green_bit_then_racing_state_emits_exactly_one_start(self) -> None:
        before = snapshot(SessionState=3)
        flag_first = snapshot(SessionState=3, SessionFlags=FLAG_GREEN)
        racing = snapshot(SessionState=STATE_RACING, SessionFlags=FLAG_GREEN)

        events = detect_green(before, flag_first) + detect_green(flag_first, racing)

        self.assertEqual([event.event_type for event in events], ["green"])

    def test_steady_racing_and_restart_green_do_not_double_fire_start(self) -> None:
        previous = snapshot(SessionState=STATE_RACING)
        current = snapshot(SessionState=STATE_RACING, SessionFlags=FLAG_GREEN)

        self.assertEqual(detect_green(previous, current), [])
        self.assertEqual(detect_green(current, current), [])

    def test_non_race_session_does_not_fire(self) -> None:
        self.assertEqual(
            detect_green(
                snapshot(SessionNum=1, SessionState=3),
                snapshot(SessionNum=1, SessionState=STATE_RACING),
            ),
            [],
        )


class CautionTests(unittest.TestCase):
    def test_caution_rising_edge_fires_once(self) -> None:
        clear = snapshot(SessionState=STATE_RACING)
        caution = snapshot(SessionState=STATE_RACING, SessionFlags=FLAG_CAUTION)

        self.assertEqual(detect_caution(clear, caution)[0].event_type, "caution")
        self.assertEqual(detect_caution(caution, caution), [])

    def test_waving_variant_does_not_create_second_caution(self) -> None:
        caution = snapshot(SessionFlags=FLAG_CAUTION)
        waving = snapshot(SessionFlags=FLAG_CAUTION | FLAG_CAUTION_WAVING)

        self.assertEqual(detect_caution(caution, waving), [])

    def test_falling_edge_does_not_fire(self) -> None:
        self.assertEqual(
            detect_caution(snapshot(SessionFlags=FLAG_CAUTION), snapshot()), []
        )


class WhiteFlagTests(unittest.TestCase):
    def test_white_rising_edge_fires_once(self) -> None:
        clear = snapshot(SessionFlags=0)
        white = snapshot(SessionFlags=FLAG_WHITE)

        event = detect_white(clear, white)[0]

        self.assertEqual((event.event_type, event.tier), ("white", 3))
        self.assertEqual(event.label, "White flag — last lap")
        self.assertEqual(detect_white(white, white), [])

    def test_white_does_not_fire_outside_race_session(self) -> None:
        self.assertEqual(
            detect_white(
                snapshot(SessionNum=1, SessionFlags=0),
                snapshot(SessionNum=1, SessionFlags=FLAG_WHITE),
            ),
            [],
        )


class RestartTests(unittest.TestCase):
    def test_one_lap_to_green_rising_edge_fires_once(self) -> None:
        clear = snapshot(SessionState=STATE_RACING)
        restart = snapshot(
            SessionState=STATE_RACING, SessionFlags=FLAG_ONE_LAP_TO_GREEN
        )

        self.assertEqual(detect_restart(clear, restart)[0].event_type, "restart")
        self.assertEqual(detect_restart(restart, restart), [])

    def test_green_held_is_a_restart_signal(self) -> None:
        events = detect_restart(
            snapshot(SessionState=STATE_RACING),
            snapshot(SessionState=STATE_RACING, SessionFlags=FLAG_GREEN_HELD),
        )

        self.assertEqual(events[0].label, "Restart")

    def test_second_restart_bit_does_not_double_fire_while_signal_is_active(self) -> None:
        one_to_green = snapshot(
            SessionState=STATE_RACING, SessionFlags=FLAG_ONE_LAP_TO_GREEN
        )
        both = snapshot(
            SessionState=STATE_RACING,
            SessionFlags=FLAG_ONE_LAP_TO_GREEN | FLAG_GREEN_HELD,
        )

        self.assertEqual(detect_restart(one_to_green, both), [])

    def test_formation_lap_restart_signal_is_ignored(self) -> None:
        self.assertEqual(
            detect_restart(snapshot(), snapshot(SessionFlags=FLAG_ONE_LAP_TO_GREEN)),
            [],
        )


class CheckeredTests(unittest.TestCase):
    def test_checkered_state_rising_edge_fires_once(self) -> None:
        racing = snapshot(SessionState=STATE_RACING)
        checkered = snapshot(SessionState=STATE_CHECKERED)

        self.assertEqual(detect_checkered(racing, checkered)[0].event_type, "checkered")
        self.assertEqual(detect_checkered(checkered, checkered), [])

    def test_checkered_flag_rising_edge_fires(self) -> None:
        events = detect_checkered(snapshot(), snapshot(SessionFlags=FLAG_CHECKERED))

        self.assertEqual(len(events), 1)

    def test_flag_then_state_does_not_double_fire(self) -> None:
        flag = snapshot(SessionState=STATE_RACING, SessionFlags=FLAG_CHECKERED)
        state = snapshot(SessionState=STATE_CHECKERED, SessionFlags=FLAG_CHECKERED)

        self.assertEqual(detect_checkered(flag, state), [])


class PitTests(unittest.TestCase):
    def test_player_pit_entry_and_exit(self) -> None:
        on_track = snapshot(OnPitRoad=False)
        in_pits = snapshot(OnPitRoad=True)

        pit_in = detect_pit(on_track, in_pits)
        pit_out = detect_pit(in_pits, on_track)

        self.assertEqual((pit_in[0].event_type, pit_in[0].tier), ("pit_in", 1))
        self.assertEqual(pit_out[0].event_type, "pit_out")

    def test_steady_pit_state_does_not_double_fire(self) -> None:
        self.assertEqual(
            detect_pit(snapshot(OnPitRoad=True), snapshot(OnPitRoad=True)), []
        )

    def test_missing_pit_state_does_not_create_an_edge(self) -> None:
        self.assertEqual(detect_pit(snapshot(OnPitRoad=None), snapshot()), [])


if __name__ == "__main__":
    unittest.main()
