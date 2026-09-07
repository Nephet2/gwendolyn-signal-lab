import json
import tempfile
import unittest
from pathlib import Path

from player.audio_devices import AudioDevice, AudioDeviceLatch


class AudioDeviceLatchTests(unittest.TestCase):
    def test_exact_device_selection_persists(self):
        devices = [AudioDevice(2, "Bluetooth Signal"), AudioDevice(5, "Speakers")]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "device.json"
            latch = AudioDeviceLatch(path, lambda: devices)
            selected = latch.latch(2, "Bluetooth Signal")
            self.assertEqual("Bluetooth Signal", selected.name)
            restored = AudioDeviceLatch(path, lambda: devices)
            self.assertEqual(selected, restored.selected)
            self.assertFalse(json.loads(path.read_text())["audio_enabled"])

    def test_changed_index_or_name_does_not_silently_retarget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "device.json"
            AudioDeviceLatch(path, lambda: [AudioDevice(2, "Bluetooth Signal")]).latch(2, "Bluetooth Signal")
            changed = AudioDeviceLatch(path, lambda: [AudioDevice(2, "Speakers")])
            self.assertIsNone(changed.selected)

    def test_unknown_device_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            latch = AudioDeviceLatch(Path(folder) / "device.json", lambda: [AudioDevice(1, "Known")])
            with self.assertRaises(ValueError):
                latch.latch(9, "Missing")


if __name__ == "__main__":
    unittest.main()
