import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from player.director_bridge import CommandError, DirectorBridge, DirectorState, validate_command
from player.presets import PresetStore, SignalPreset
from player.signal_lab import apply_heart_tempo


COMMAND = {
    "request_id": "unit-test",
    "reason": "test",
    "transition_seconds": 8,
    "left": {"intensity": 40, "texture": 60, "vibration_rate": 30, "vibration_depth": 50},
    "right": {"intensity": 50, "texture": 40, "vibration_rate": 45, "vibration_depth": 35},
}


class DirectorBridgeTests(unittest.TestCase):
    def test_validation_produces_independent_lanes(self):
        with tempfile.TemporaryDirectory() as folder:
            applied = validate_command(COMMAND, PresetStore(Path(folder) / "presets.json"))
            self.assertEqual(920, applied.left_signal["freq"])
            self.assertEqual(780, applied.right_signal["freq"])
            self.assertFalse(applied.audio_enabled)

    def test_out_of_range_command_is_rejected(self):
        invalid = json.loads(json.dumps(COMMAND))
        invalid["left"]["intensity"] = 101
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(CommandError):
                validate_command(invalid, PresetStore(Path(folder) / "presets.json"))

    def test_local_http_command_state_and_neutral(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = DirectorBridge(port=0, preset_store=PresetStore(Path(folder) / "presets.json"))
            bridge.start()
            host, port = bridge.address
            base = f"http://{host}:{port}"
            try:
                request = Request(base + "/v1/director/command", data=json.dumps(COMMAND).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=2) as response:
                    result = json.load(response)
                self.assertEqual("applied_offline", result["status"])
                self.assertFalse(result["command"]["audio_enabled"])
                with urlopen(base + "/v1/state", timeout=2) as response:
                    state = json.load(response)
                self.assertEqual("unit-test", state["last_command"]["request_id"])
                neutral = Request(base + "/v1/director/neutral", data=b'{"reason":"test complete"}', headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(neutral, timeout=2) as response:
                    result = json.load(response)
                self.assertEqual("neutral_offline", result["status"])
            finally:
                bridge.close()

    def test_live_playback_and_monitor_telemetry_is_exposed(self):
        with tempfile.TemporaryDirectory() as folder:
            state_store = DirectorState(PresetStore(Path(folder) / "presets.json"))
            state_store.update_live({
                "source": "reference_session", "session": "example",
                "position_ms": 1250, "duration_ms": 5000, "playing": True,
                "left_signal": {"freq": 620, "amFreq": 0.4, "fmFreq": 1},
                "right_signal": {"freq": 3000, "amFreq": 0.4, "fmFreq": 1},
                "headphone_monitor": {
                    "armed": True, "audible": True, "gain_percent": 15,
                    "device": "Headphones",
                },
            })
            state = state_store.snapshot()
            self.assertFalse(state["physical_output_enabled"])
            self.assertEqual(620, state["live"]["left_signal"]["freq"])
            self.assertEqual(3000, state["live"]["right_signal"]["freq"])
            self.assertTrue(state["live"]["headphone_monitor"]["audible"])
            self.assertEqual(15, state["live"]["headphone_monitor"]["gain_percent"])

    def test_heart_tempo_is_bounded_observational_and_fresh(self):
        state_store = DirectorState()
        result = state_store.update_heart_tempo({"bpm": 45, "ratio": 0.9, "confidence": 93})
        self.assertEqual("heart_tempo_observed", result["status"])
        self.assertEqual(40.5, result["target_cpm"])
        observed = state_store.heart_tempo()
        self.assertTrue(observed["fresh"])
        self.assertTrue(observed["available"])

    def test_heart_tempo_accepts_double_ratio(self):
        state_store = DirectorState()
        result = state_store.update_heart_tempo({"bpm": 60, "ratio": 2.0, "confidence": 93})
        self.assertEqual(120.0, result["target_cpm"])

    def test_heart_tempo_rejects_out_of_range_values(self):
        state_store = DirectorState()
        with self.assertRaises(CommandError):
            state_store.update_heart_tempo({"bpm": 45, "ratio": 0.5, "confidence": 93})
        with self.assertRaises(CommandError):
            state_store.update_heart_tempo({"bpm": 250, "ratio": 0.9, "confidence": 93})

    def test_director_heart_tempo_control_is_bounded_and_requires_fresh_data(self):
        state_store = DirectorState()
        with self.assertRaises(CommandError):
            state_store.command_heart_tempo({"target": "left", "mode": "escalation"})
        state_store.update_heart_tempo({"bpm": 45, "ratio": 1.0, "confidence": 100})
        result = state_store.command_heart_tempo({"target": "left", "mode": "escalation"})
        self.assertTrue(result["control"]["active"])
        self.assertEqual(1.0, result["control"]["ratio"])
        self.assertEqual("left", state_store.snapshot()["heart_tempo_control"]["target"])
        off = state_store.command_heart_tempo({"mode": "off"})
        self.assertFalse(off["control"]["active"])

    def test_director_double_heart_tempo_control(self):
        state_store = DirectorState()
        state_store.update_heart_tempo({"bpm": 60, "ratio": 2.0, "confidence": 100})
        result = state_store.command_heart_tempo({"target": "right", "mode": "double"})
        self.assertTrue(result["control"]["active"])
        self.assertEqual(2.0, result["control"]["ratio"])
        self.assertEqual("right", result["control"]["target"])

    def test_director_heart_tempo_http_endpoint(self):
        bridge = DirectorBridge(port=0)
        bridge.start()
        host, port = bridge.address
        try:
            bridge.state.update_heart_tempo({"bpm": 48, "ratio": 1.0, "confidence": 100})
            request = Request(
                f"http://{host}:{port}/v1/director/heart-tempo",
                data=json.dumps({"target": "right", "mode": "relief"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urlopen(request, timeout=2) as response:
                result = json.load(response)
            self.assertEqual(0.9, result["control"]["ratio"])
            self.assertEqual("right", bridge.state.snapshot()["heart_tempo_control"]["target"])
        finally:
            bridge.close()

    def test_heart_tempo_changes_only_primary_am_rate(self):
        left = {"freq": 700, "volume": 40, "amFreq": 2.0, "amDepth": 30,
                "secondaryAmFreq": 1.2, "fmFreq": 0.4}
        right = {**left, "freq": 900, "amFreq": 3.0}
        heart = {"fresh": True, "target_cpm": 40.5}
        adjusted_left, adjusted_right = apply_heart_tempo(
            left, right, heart, enabled=True, preset=SignalPreset()
        )
        self.assertEqual(0.675, adjusted_left["amFreq"])
        self.assertEqual(0.675, adjusted_right["amFreq"])
        for key in ("freq", "volume", "amDepth", "secondaryAmFreq", "fmFreq"):
            self.assertEqual(left[key], adjusted_left[key])
            self.assertEqual(right[key], adjusted_right[key])
        self.assertEqual(2.0, left["amFreq"])
        self.assertEqual(3.0, right["amFreq"])

    def test_heart_tempo_can_target_one_lane(self):
        left = {"amFreq": 0.4, "freq": 620, "volume": 50}
        right = {"amFreq": 0.8, "freq": 550, "volume": 60}
        heart = {"fresh": True, "target_cpm": 45}
        adjusted_left, adjusted_right = apply_heart_tempo(
            left, right, heart, enabled=True, preset=SignalPreset(), target="right",
        )
        self.assertEqual(left, adjusted_left)
        self.assertEqual(0.75, adjusted_right["amFreq"])
        self.assertEqual(550, adjusted_right["freq"])
        self.assertEqual(60, adjusted_right["volume"])

    def test_stale_or_disabled_heart_tempo_preserves_authored_states(self):
        left = {"amFreq": 2.0}
        right = {"amFreq": 3.0}
        preset = SignalPreset()
        self.assertEqual((left, right), apply_heart_tempo(
            left, right, {"fresh": False, "target_cpm": 40.5}, enabled=True, preset=preset
        ))
        self.assertEqual((left, right), apply_heart_tempo(
            left, right, {"fresh": True, "target_cpm": 40.5}, enabled=False, preset=preset
        ))

    def test_heart_tempo_http_requires_exact_phase_lab_origin(self):
        bridge = DirectorBridge(port=0)
        bridge.start()
        host, port = bridge.address
        base = f"http://{host}:{port}"
        payload = json.dumps({"bpm": 45, "ratio": 0.9, "confidence": 93}).encode()
        try:
            allowed = Request(base + "/v1/heart-tempo", data=payload, headers={
                "Content-Type": "application/json", "Origin": "http://127.0.0.1:8766",
            }, method="POST")
            with urlopen(allowed, timeout=2) as response:
                result = json.load(response)
                self.assertEqual("http://127.0.0.1:8766", response.headers["Access-Control-Allow-Origin"])
            self.assertEqual(40.5, result["target_cpm"])
            blocked = Request(base + "/v1/heart-tempo", data=payload, headers={
                "Content-Type": "application/json", "Origin": "http://example.invalid",
            }, method="POST")
            with self.assertRaises(HTTPError) as denied:
                urlopen(blocked, timeout=2)
            self.assertEqual(403, denied.exception.code)
        finally:
            bridge.close()

    def test_bridge_refuses_non_local_bind(self):
        with self.assertRaises(ValueError):
            DirectorBridge(host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
