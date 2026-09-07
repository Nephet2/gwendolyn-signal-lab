import json
import tempfile
import unittest
from pathlib import Path

from player.session_model import discover_sessions, load_session, state_at


ROOT = Path(__file__).resolve().parents[1]


class SessionModelTests(unittest.TestCase):
    def test_all_reference_sessions_load_and_lanes_align(self):
        paths = discover_sessions(ROOT / "reference-sessions")
        # The public repository ships one neutral example. Users may add any
        # number of private reference sessions without changing this test.
        self.assertGreaterEqual(len(paths), 1)
        for path in paths:
            session = load_session(path)
            self.assertTrue(session.left, path.name)
            self.assertTrue(session.right, path.name)
            left_end = session.left[-1].time_ms + session.left[-1].duration_ms
            right_end = session.right[-1].time_ms + session.right[-1].duration_ms
            difference = abs(left_end - right_end)
            self.assertLess(difference, 5 * 60 * 1000, path.name)

    def test_stamp_is_event_duration_and_first_state_starts_at_zero(self):
        data = {
            "meta": {"driverName": "Test", "driverComments": ""},
            "left": [
                {"stamp": 1000, "message": {"volume": 10}},
                {"stamp": 2500, "message": {"volume": 20}},
            ],
            "right": [],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            session = load_session(path)
        self.assertEqual([0, 1000], [event.time_ms for event in session.left])
        self.assertEqual([1000, 2500], [event.duration_ms for event in session.left])
        self.assertEqual(3500, session.duration_ms)
        self.assertEqual(10, state_at(session.left, 500)["volume"])
        self.assertEqual(20, state_at(session.left, 2000)["volume"])

    def test_negative_stamp_is_rejected(self):
        data = {"meta": {}, "left": [{"stamp": -1, "message": {}}], "right": []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_session(path)


if __name__ == "__main__":
    unittest.main()
