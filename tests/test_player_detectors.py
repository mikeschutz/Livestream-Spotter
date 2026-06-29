import unittest

from livestream_spotter.detectors.player import (
    BLACK_FLAG,
    MEATBALL_FLAG,
    detect_battle,
    detect_incident,
    detect_overtake,
    detect_player_flag,
    detect_tow,
)


def snapshot(**overrides):
    values = {
        "SessionNum": 0,
        "SessionState": 4,
        "SessionTime": 20.0,
        "Lap": 5,
        "PlayerCarPosition": 3,
        "PlayerCarClassPosition": 3,
        "PlayerCarMyIncidentCount": 0,
        "PlayerCarTowTime": 0.0,
        "OnPitRoad": False,
        "PitstopActive": False,
        "CarIdxPosition": [3, 2, 4],
        "CarIdxClassPosition": [3, 2, 4],
        "CarIdxLap": [5, 5, 5],
        "CarIdxLapDistPct": [0.50, 0.52, 0.48],
        "CarIdxF2Time": [10.0, 9.5, 10.4],
        "CarIdxOnPitRoad": [False, False, False],
        "CarIdxSessionFlags": [0, 0, 0],
        "DriverInfo": {
            "DriverCarIdx": 0,
            "Drivers": [
                {"CarIdx": 0, "UserName": "Player", "CarClassID": 10},
                {"CarIdx": 1, "UserName": "Ahead", "CarClassID": 10},
                {"CarIdx": 2, "UserName": "Behind", "CarClassID": 10},
            ],
        },
    }
    values.update(overrides)
    return values


class OvertakeTests(unittest.TestCase):
    def test_position_gain_and_loss_are_distinct(self) -> None:
        before = snapshot()
        gained = snapshot(
            PlayerCarPosition=2,
            PlayerCarClassPosition=2,
            CarIdxPosition=[2, 3, 4],
            CarIdxClassPosition=[2, 3, 4],
        )

        overtake = detect_overtake(before, gained)
        overtaken = detect_overtake(gained, before)

        self.assertEqual(overtake[0].event_type, "overtake")
        self.assertEqual(overtake[0].meta["to_position"], 2)
        self.assertEqual(overtaken[0].event_type, "overtaken")

    def test_overtake_label_uses_driver_name(self) -> None:
        before = snapshot()
        after = snapshot(
            PlayerCarPosition=2,
            PlayerCarClassPosition=2,
            CarIdxPosition=[2, 3, 4],
            CarIdxClassPosition=[2, 3, 4],
            DriverLookup={1: {"name": "J. Smith", "number": "44"}},
        )

        event = detect_overtake(before, after)[0]

        self.assertEqual(event.label, "Passed J. Smith for P2")

    def test_overtake_label_falls_back_to_car_number(self) -> None:
        before = snapshot()
        after = snapshot(
            PlayerCarPosition=2,
            PlayerCarClassPosition=2,
            CarIdxPosition=[2, 3, 4],
            CarIdxClassPosition=[2, 3, 4],
            DriverLookup={1: {"name": None, "number": "44"}},
        )

        event = detect_overtake(before, after)[0]

        self.assertEqual(event.label, "Passed #44 for P2")

    def test_overtake_label_falls_back_to_generic_without_none(self) -> None:
        before = snapshot()
        after = snapshot(
            PlayerCarPosition=2,
            PlayerCarClassPosition=2,
            CarIdxPosition=[2, 3, 4],
            CarIdxClassPosition=[2, 3, 4],
            DriverLookup={},
        )

        event = detect_overtake(before, after)[0]

        self.assertEqual(event.label, "Passed car ahead for P2")
        self.assertNotIn("None", event.label)

    def test_multiclass_uses_class_position(self) -> None:
        drivers = [
            {"CarIdx": 0, "UserName": "Player", "CarClassID": 10},
            {"CarIdx": 1, "UserName": "Class rival", "CarClassID": 10},
            {"CarIdx": 2, "UserName": "Other class", "CarClassID": 20},
        ]
        before = snapshot(
            PlayerCarPosition=5,
            PlayerCarClassPosition=3,
            CarIdxPosition=[5, 2, 3],
            CarIdxClassPosition=[3, 2, 0],
            DriverInfo={"DriverCarIdx": 0, "Drivers": drivers},
        )
        after = snapshot(
            PlayerCarPosition=5,
            PlayerCarClassPosition=2,
            CarIdxPosition=[5, 2, 3],
            CarIdxClassPosition=[2, 3, 0],
            DriverInfo={"DriverCarIdx": 0, "Drivers": drivers},
        )

        events = detect_overtake(before, after)

        self.assertEqual(events[0].meta["position_scope"], "class")

    def test_pit_shuffle_does_not_emit_overtake(self) -> None:
        before = snapshot(CarIdxOnPitRoad=[False, True, False])
        after = snapshot(
            PlayerCarPosition=2,
            PlayerCarClassPosition=2,
            CarIdxPosition=[2, 3, 4],
            CarIdxClassPosition=[2, 3, 4],
            CarIdxOnPitRoad=[False, True, False],
        )

        self.assertEqual(detect_overtake(before, after), [])

    def test_distant_position_change_is_rejected_when_proximity_is_available(self) -> None:
        before = snapshot(CarIdxLapDistPct=[0.10, 0.40, 0.08])
        after = snapshot(
            PlayerCarPosition=2,
            PlayerCarClassPosition=2,
            CarIdxPosition=[2, 3, 4],
            CarIdxClassPosition=[2, 3, 4],
            CarIdxLapDistPct=[0.11, 0.40, 0.08],
        )

        self.assertEqual(detect_overtake(before, after), [])

    def test_invalid_positions_are_filtered(self) -> None:
        self.assertEqual(
            detect_overtake(snapshot(), snapshot(PlayerCarPosition=0)), []
        )


class IncidentTests(unittest.TestCase):
    def test_one_two_and_four_points_have_severity_labels(self) -> None:
        isolated = {
            "CarIdxLapDistPct": [0.10, 0.40, 0.70],
            "CarIdxF2Time": [10.0, 6.0, 14.0],
        }
        minor = detect_incident(
            snapshot(PlayerCarMyIncidentCount=0, **isolated),
            snapshot(PlayerCarMyIncidentCount=1, **isolated),
        )[0]
        contact = detect_incident(
            snapshot(PlayerCarMyIncidentCount=1, **isolated),
            snapshot(PlayerCarMyIncidentCount=3, **isolated),
        )[0]
        major = detect_incident(
            snapshot(PlayerCarMyIncidentCount=3, **isolated),
            snapshot(PlayerCarMyIncidentCount=7, **isolated),
        )[0]

        self.assertEqual(minor.label, "Track limits / minor")
        self.assertEqual(contact.label, "Contact")
        self.assertEqual(major.label, "Major incident")
        self.assertEqual(minor.meta["incident_delta"], 1)
        self.assertEqual(contact.meta["incident_delta"], 2)
        self.assertEqual(major.meta["incident_delta"], 4)

    def test_two_point_contact_with_adjacent_car_uses_full_name(self) -> None:
        current = snapshot(
            PlayerCarMyIncidentCount=2,
            DriverLookup={2: {"name": "Seth Whitaker", "number": "44"}},
        )

        incident = detect_incident(snapshot(), current)[0]

        self.assertEqual(incident.label, "Contact with Seth Whitaker")
        self.assertEqual(incident.meta["opponent_name"], "Seth Whitaker")
        self.assertTrue(incident.meta["inferred_contact"])

    def test_two_point_contact_without_nearby_car_stays_generic(self) -> None:
        current = snapshot(
            PlayerCarMyIncidentCount=2,
            CarIdxLapDistPct=[0.10, 0.40, 0.70],
            CarIdxF2Time=[10.0, 6.0, 14.0],
        )

        incident = detect_incident(snapshot(), current)[0]

        self.assertEqual(incident.label, "Contact")
        self.assertNotIn("inferred_contact", incident.meta)

    def test_counter_reset_does_not_emit(self) -> None:
        self.assertEqual(
            detect_incident(
                snapshot(PlayerCarMyIncidentCount=8),
                snapshot(PlayerCarMyIncidentCount=0),
            ),
            [],
        )


class TowTests(unittest.TestCase):
    def test_tow_time_rising_edge_fires_once(self) -> None:
        clear = snapshot(PlayerCarTowTime=0.0)
        towing = snapshot(PlayerCarTowTime=12.0)

        self.assertEqual(detect_tow(clear, towing)[0].event_type, "tow")
        self.assertEqual(detect_tow(towing, towing), [])


class BattleTests(unittest.TestCase):
    def test_f2_difference_is_primary_gap(self) -> None:
        event = detect_battle(snapshot(), snapshot())[0]

        self.assertEqual(event.meta["other_car_idx"], 2)
        self.assertAlmostEqual(event.meta["gap_seconds"], 0.4)

    def test_battle_label_uses_driver_name(self) -> None:
        current = snapshot(
            DriverLookup={2: {"name": "A. Driver", "number": "12"}}
        )

        event = detect_battle(snapshot(), current)[0]

        self.assertEqual(event.label, "Battle with A. Driver")

    def test_battle_label_falls_back_to_car_number(self) -> None:
        current = snapshot(DriverLookup={2: {"name": "", "number": 12}})

        event = detect_battle(snapshot(), current)[0]

        self.assertEqual(event.label, "Battle with #12")

    def test_battle_label_falls_back_to_generic_without_none(self) -> None:
        current = snapshot(DriverLookup={})

        event = detect_battle(snapshot(), current)[0]

        self.assertEqual(event.label, "Battle with car behind")
        self.assertNotIn("None", event.label)

    def test_non_racing_state_is_rejected(self) -> None:
        self.assertEqual(detect_battle(snapshot(), snapshot(SessionState=3)), [])

    def test_lapped_car_with_close_f2_but_far_on_track_is_rejected(self) -> None:
        current = snapshot(
            CarIdxPosition=[2, 1, 0],
            CarIdxLap=[5, 4, -1],
            CarIdxLapDistPct=[0.10, 0.60, -1.0],
            CarIdxF2Time=[10.0, 9.7, 0.0],
        )

        self.assertEqual(detect_battle(snapshot(), current), [])

    def test_same_lap_but_distant_car_is_rejected(self) -> None:
        current = snapshot(
            CarIdxPosition=[2, 1, 0],
            CarIdxLap=[5, 5, -1],
            CarIdxLapDistPct=[0.10, 0.40, -1.0],
            CarIdxF2Time=[10.0, 9.7, 0.0],
        )

        self.assertEqual(detect_battle(snapshot(), current), [])


class PlayerFlagTests(unittest.TestCase):
    def test_black_and_meatball_rising_edges_are_tier_one(self) -> None:
        black = detect_player_flag(
            snapshot(), snapshot(CarIdxSessionFlags=[BLACK_FLAG, 0, 0])
        )[0]
        meatball = detect_player_flag(
            snapshot(), snapshot(CarIdxSessionFlags=[MEATBALL_FLAG, 0, 0])
        )[0]

        self.assertEqual((black.label, black.tier), ("Black flag", 1))
        self.assertEqual(meatball.label, "Meatball flag")

    def test_steady_flag_does_not_double_fire(self) -> None:
        flagged = snapshot(CarIdxSessionFlags=[BLACK_FLAG, 0, 0])

        self.assertEqual(detect_player_flag(flagged, flagged), [])


if __name__ == "__main__":
    unittest.main()
