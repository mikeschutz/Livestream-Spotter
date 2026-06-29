import unittest

from livestream_spotter.events import Event
from livestream_spotter.sinks.event_log import LoggingEventSink
from livestream_spotter.sinks.fanout import FanoutEventSink


class CollectingSink:
    def __init__(self) -> None:
        self.events = []
        self.closed = False

    def write(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


class LoggingEventSinkTests(unittest.TestCase):
    def test_log_contains_eyeball_fields(self) -> None:
        event = Event(1234, "pit_in", "Pit in", 1, 8, 99.5, 0, {})

        with self.assertLogs("livestream_spotter.sinks.event_log", level="INFO") as logs:
            LoggingEventSink().write(event)

        message = logs.output[0]
        self.assertIn("type=pit_in", message)
        self.assertIn("label='Pit in'", message)
        self.assertIn("tier=1", message)
        self.assertIn("video_ms=1234", message)
        self.assertIn("lap=8", message)


class FanoutEventSinkTests(unittest.TestCase):
    def test_event_and_close_reach_every_sink(self) -> None:
        first = CollectingSink()
        second = CollectingSink()
        sink = FanoutEventSink((first, second))
        event = Event(1234, "pit_in", "Pit in", 1, 8, 99.5, 0, {})

        sink.write(event)
        sink.close()

        self.assertEqual(first.events, [event])
        self.assertEqual(second.events, [event])
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)


if __name__ == "__main__":
    unittest.main()
