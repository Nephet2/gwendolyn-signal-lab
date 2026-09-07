import io
import unittest
import wave
from array import array

from player.perceptual import analyse_pair, semantic_state
from player.presets import SignalPreset
from player.synth import render_states, render_states_continuous, waveform


class PerceptualSynthTests(unittest.TestCase):
    def test_semantic_bounds_map_to_preferred_carrier_range(self):
        rough = semantic_state(intensity=40, texture=0, vibration_rate=0, vibration_depth=0)
        smooth = semantic_state(intensity=40, texture=100, vibration_rate=100, vibration_depth=100)
        self.assertEqual(500, rough["freq"])
        self.assertEqual(1200, smooth["freq"])
        self.assertEqual(0.2, rough["amFreq"])
        self.assertEqual(5.0, smooth["amFreq"])

    def test_perceptual_character_detects_fm_asymmetry(self):
        left = semantic_state(intensity=50, texture=50, vibration_rate=40, vibration_depth=50)
        right = dict(left)
        left.update(fmType="sine", fmDepth=20, fmFreq=0.2)
        right.update(fmType="sine", fmDepth=4, fmFreq=0.2)
        self.assertEqual("pressure-tapping", analyse_pair(left, right).character)

    def test_render_is_stereo_pcm_and_bounded(self):
        state = semantic_state(intensity=100, texture=50, vibration_rate=50, vibration_depth=100)
        payload = render_states(state, state, duration_s=0.1, sample_rate=8000, preview_gain=0.05)
        with wave.open(io.BytesIO(payload), "rb") as wav:
            self.assertEqual(2, wav.getnchannels())
            self.assertEqual(2, wav.getsampwidth())
            self.assertEqual(8000, wav.getframerate())
            self.assertEqual(800, wav.getnframes())
            samples = array("h", wav.readframes(wav.getnframes()))
        self.assertLessEqual(max(abs(value) for value in samples), 1639)

    def test_waveform_rejects_unknown_shape(self):
        with self.assertRaises(ValueError):
            waveform("mystery", 0.0)

    def test_continuous_blocks_match_one_long_render(self):
        state = semantic_state(intensity=55, texture=41, vibration_rate=37,
                               vibration_depth=63, secondary_rate=29,
                               secondary_depth=47, modulation=52)
        phase_state = {}
        first = render_states_continuous(
            state, state, phase_state=phase_state, duration_s=0.137,
            sample_rate=8000, preview_gain=0.05,
        )
        second = render_states_continuous(
            state, state, phase_state=phase_state, duration_s=0.163,
            sample_rate=8000, preview_gain=0.05,
        )
        whole = render_states(
            state, state, duration_s=0.300, sample_rate=8000,
            preview_gain=0.05,
        )

        def samples(payload):
            with wave.open(io.BytesIO(payload), "rb") as wav:
                return array("h", wav.readframes(wav.getnframes()))

        self.assertEqual(samples(whole), samples(first) + samples(second))

    def test_preset_relative_mapping_and_secondary_am(self):
        preset = SignalPreset(volume_min=30, volume_max=50, volume_start=40,
                              frequency_min_hz=600, frequency_max_hz=1000, frequency_start_hz=700)
        state = semantic_state(intensity=50, texture=50, vibration_rate=50, vibration_depth=50,
                               secondary_rate=100, secondary_depth=100, modulation=50, preset=preset)
        self.assertEqual(40, state["volume"])
        self.assertEqual(800, state["freq"])
        self.assertEqual(1.5, state["secondaryAmFreq"])
        self.assertEqual(24, state["secondaryAmDepth"])
        self.assertEqual("sine", state["fmType"])


if __name__ == "__main__":
    unittest.main()
