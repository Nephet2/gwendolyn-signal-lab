import json
import tempfile
import unittest
from pathlib import Path

from player.presets import PresetStore, SignalPreset, validate_preset


class PresetTests(unittest.TestCase):
    def test_store_persists_active_preset(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "presets.json"
            store = PresetStore(path)
            store.put(SignalPreset(name="Test envelope", volume_min=25, volume_max=55, volume_start=35))
            reloaded = PresetStore(path)
            self.assertEqual("Test envelope", reloaded.active().name)
            self.assertEqual(55, reloaded.active().volume_max)

    def test_start_values_must_be_inside_ranges(self):
        with self.assertRaises(ValueError):
            validate_preset(SignalPreset(frequency_min_hz=500, frequency_max_hz=900, frequency_start_hz=1000))

    def test_file_is_human_readable_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "presets.json"
            PresetStore(path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(1, data["version"])
            self.assertTrue(data["presets"])


if __name__ == "__main__":
    unittest.main()
