import unittest

from player.audio_monitor import (
    MONITOR_BLOCK_SECONDS, MONITOR_GAIN, MONITOR_MAX_GAIN, HeadphoneMonitor,
    MonitorError, staged_state_plan,
    validate_output_device,
)


class AudioMonitorTests(unittest.TestCase):
    def test_monitor_defaults_to_safe_adjustable_gain_and_responsive_blocks(self):
        self.assertEqual(0.10, MONITOR_GAIN)
        self.assertEqual(0.50, MONITOR_MAX_GAIN)
        self.assertEqual(0.10, MONITOR_BLOCK_SECONDS)

    def test_monitor_gain_is_safety_clamped(self):
        monitor = HeadphoneMonitor()
        monitor.set_gain(0.75)
        self.assertEqual(0.50, monitor._gain)
        monitor.set_gain(-1)
        self.assertEqual(0.0, monitor._gain)

    def test_exact_stereo_output_name_is_required(self):
        devices = lambda: [
            {"name": "Stereo Headphones", "max_output_channels": 2},
            {"name": "Stereo Headphones Hands-Free", "max_output_channels": 1},
        ]
        self.assertEqual(0, validate_output_device(0, "Stereo Headphones", devices))
        with self.assertRaises(MonitorError):
            validate_output_device(0, "Headphones", devices)

    def test_transition_changes_character_before_intensity(self):
        old = {"freq": 600, "amFreq": 1, "amDepth": 20, "fmType": "none",
               "fmDepth": 0, "fmFreq": 0, "volume": 40, "rampTarget": 40}
        new = {"freq": 1000, "amFreq": 3, "amDepth": 50, "fmType": "sine",
               "fmDepth": 80, "fmFreq": 1, "volume": 70, "rampTarget": 70}
        plan = staged_state_plan(old, old, new, new, 10)
        self.assertGreaterEqual(len(plan), 10)
        self.assertNotEqual(old["freq"], plan[0][0]["freq"])
        self.assertEqual(old["volume"], plan[0][0]["volume"])
        self.assertEqual(new, plan[-1][0])


if __name__ == "__main__":
    unittest.main()
