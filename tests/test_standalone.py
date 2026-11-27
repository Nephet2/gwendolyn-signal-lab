"""Standalone edition: user-controlled JSON playback with funscript channel balance, no Director."""
import math
import socket
import tempfile
import unittest
from pathlib import Path

from player.funscript_balance import BalanceCurve, gains, matching_funscript
from player.session_model import load_session, state_at
from player.texture_modes import MODES

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "reference-sessions" / "Balance-Demo.funscript"
EXAMPLE = ROOT / "reference-sessions" / "example-session.json"
DIRECTOR_ONLY = ("Gwendolyn", "Director", "Rotate references", "Pressure sensor", "Heart tempo",
                 "Blinded A/B/C")


class StandaloneModes(unittest.TestCase):
    def test_only_user_controlled_sources(self):
        self.assertEqual(MODES, ("Manual", "Reference JSON"))


class FunscriptBalance(unittest.TestCase):
    def test_equal_power_crossover(self):
        self.assertEqual(gains(0, "Equal power"), (1.0, 0.0))
        self.assertEqual(gains(100, "Equal power"), (0.0, 1.0))
        left, right = gains(50, "Equal power")
        self.assertAlmostEqual(left, math.sqrt(0.5), places=6)
        self.assertAlmostEqual(right, math.sqrt(0.5), places=6)

    def test_original_balance_keeps_both_lanes_full_at_centre(self):
        self.assertEqual(gains(50, "Original balance"), (1.0, 1.0))

    def test_demo_script_sweeps_left_then_right(self):
        curve = BalanceCurve.load(DEMO)
        self.assertEqual(curve.position(0), 50)
        self.assertEqual(curve.position(2000), 0)
        self.assertEqual(curve.position(6000), 100)
        self.assertEqual(curve.position(4000), 50)
        self.assertEqual(curve.position(99999), 50)  # last position holds

    def test_gradient_interpolation_stays_in_range(self):
        curve = BalanceCurve.load(DEMO)
        for ms in range(0, 8001, 50):
            self.assertTrue(0 <= curve.position(ms, "Gradient") <= 100)

    def test_rejects_bad_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / "bad.funscript"
            bad.write_text('{"actions": [{"at": 0, "pos": 150}]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                BalanceCurve.load(bad)

    def test_sidecar_matches_video_name(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "clip.name.mp4"
            video.write_bytes(b"")
            self.assertIsNone(matching_funscript(str(video)))
            script = Path(folder) / "clip.name.funscript"
            script.write_text('{"actions": [{"at": 0, "pos": 50}]}', encoding="utf-8")
            self.assertEqual(matching_funscript(str(video)), script)


class ExampleSession(unittest.TestCase):
    def test_example_plays_on_both_lanes(self):
        session = load_session(EXAMPLE)
        left, right = state_at(session.left, 31000), state_at(session.right, 31000)
        self.assertEqual(left["freq"], 800)
        self.assertEqual(right["freq"], 850)


class Window(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter
            tkinter.Tk().destroy()
        except Exception as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")

    def test_window_hides_director_controls_and_starts_no_bridge(self):
        from player.director_bridge import BRIDGE_HOST, BRIDGE_PORT
        from player.signal_lab import SignalLab
        app = SignalLab()
        try:
            app.update_idletasks()
            self.assertEqual(app.title(), "Signal Lab")
            self.assertIsNone(app.bridge)
            self.assertEqual(app.texture_mode.get(), "Reference JSON")
            for widget in _walk(app):
                text = _text(widget)
                if any(word in text for word in DIRECTOR_ONLY):
                    self.assertFalse(_shown(widget), f"visible Director-only control: {text!r}")
            with socket.socket() as probe:
                probe.settimeout(0.2)
                self.assertNotEqual(probe.connect_ex((BRIDGE_HOST, BRIDGE_PORT)), 0,
                                    "something is listening on the Director port")
        finally:
            app.destroy()

    def test_manual_mode_runs_without_a_json(self):
        from player.signal_lab import SignalLab
        app = SignalLab()
        try:
            app.texture_mode.set("Manual")
            app._change_texture_mode()
            left, right = app._monitor_states()
            self.assertTrue(left.get("active") and right.get("active"))
            self.assertIsNone(app.bridge)
        finally:
            app.destroy()


    def test_balance_script_drives_channel_gains_during_playback(self):
        import time
        from unittest import mock
        from player import signal_lab
        app = signal_lab.SignalLab()
        seen = []
        try:
            app.headphone_monitor.set_balance = lambda pair: seen.append((app.position_ms, tuple(pair)))
            with mock.patch.object(signal_lab.filedialog, "askopenfilename", return_value=str(DEMO)):
                app._open_balance()
            app.balance_shape.set("Equal power")
            app._toggle()  # Play on the internal clock; the monitor stays disarmed
            deadline = time.monotonic() + 7.0
            while time.monotonic() < deadline:
                app.update()
                time.sleep(0.02)
            app._toggle()
        finally:
            app.destroy()
        left_only = [pair for ms, pair in seen if 1900 <= ms <= 2100]
        right_only = [pair for ms, pair in seen if 5900 <= ms <= 6100]
        self.assertTrue(left_only and right_only, f"playback did not reach 2 s and 6 s: {seen[-1:]}")
        self.assertGreater(left_only[0][0], 0.95)
        self.assertLess(left_only[0][1], 0.1)
        self.assertLess(right_only[0][0], 0.1)
        self.assertGreater(right_only[0][1], 0.95)


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _text(widget):
    try:
        return str(widget.cget("text"))
    except Exception:
        return ""


def _shown(widget):
    """True when the widget and every ancestor up to the toplevel are placed by a geometry manager."""
    while widget is not None and widget.winfo_toplevel() is not widget:
        if not widget.winfo_manager():
            return False
        widget = widget.master
    return True


if __name__ == "__main__":
    unittest.main()
