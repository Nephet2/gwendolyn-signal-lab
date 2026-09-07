from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
from threading import RLock
from typing import Any


PRESET_PATH = Path(__file__).resolve().parents[1] / "signal_presets.json"


@dataclass(frozen=True)
class SignalPreset:
    name: str = "Balanced 500–1200"
    frequency_min_hz: float = 500.0
    frequency_max_hz: float = 1200.0
    frequency_start_hz: float = 700.0
    volume_min: float = 20.0
    volume_max: float = 60.0
    volume_start: float = 40.0
    fm_depth_min_percent: float = 10.0
    fm_depth_max_percent: float = 30.0
    am_depth_min_percent: float = 12.0
    am_depth_max_percent: float = 36.0
    am_rate_min_hz: float = 0.2
    am_rate_max_hz: float = 5.0
    secondary_am_depth_min_percent: float = 8.0
    secondary_am_depth_max_percent: float = 24.0
    secondary_am_rate_min_hz: float = 0.05
    secondary_am_rate_max_hz: float = 1.5
    transition_min_seconds: float = 5.0
    transition_max_seconds: float = 15.0
    session_duration_minutes: float = 45.0


NUMERIC_FIELDS = tuple(field.name for field in fields(SignalPreset) if field.name != "name")


def validate_preset(value: SignalPreset | dict[str, Any]) -> SignalPreset:
    if isinstance(value, SignalPreset):
        preset = value
    elif isinstance(value, dict):
        try:
            preset = SignalPreset(name=str(value.get("name") or "Unnamed preset")[:80], **{
                key: float(value[key]) for key in NUMERIC_FIELDS
            })
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Preset contains a missing or non-numeric field") from exc
    else:
        raise ValueError("Preset must be an object")
    if not preset.name.strip():
        raise ValueError("Preset name is required")
    pairs = (
        ("frequency", preset.frequency_min_hz, preset.frequency_max_hz),
        ("volume", preset.volume_min, preset.volume_max),
        ("FM depth", preset.fm_depth_min_percent, preset.fm_depth_max_percent),
        ("AM depth", preset.am_depth_min_percent, preset.am_depth_max_percent),
        ("AM rate", preset.am_rate_min_hz, preset.am_rate_max_hz),
        ("secondary AM depth", preset.secondary_am_depth_min_percent, preset.secondary_am_depth_max_percent),
        ("secondary AM rate", preset.secondary_am_rate_min_hz, preset.secondary_am_rate_max_hz),
        ("transition", preset.transition_min_seconds, preset.transition_max_seconds),
    )
    for label, low, high in pairs:
        if low < 0 or high < low:
            raise ValueError(f"{label} maximum must be at least its non-negative minimum")
    if not preset.frequency_min_hz <= preset.frequency_start_hz <= preset.frequency_max_hz:
        raise ValueError("Starting frequency must be inside its range")
    if not preset.volume_min <= preset.volume_start <= preset.volume_max:
        raise ValueError("Starting volume must be inside its range")
    if preset.frequency_max_hz > 5000:
        raise ValueError("Maximum frequency cannot exceed 5000 Hz")
    if preset.volume_max > 100:
        raise ValueError("Maximum volume cannot exceed 100")
    for value in (preset.fm_depth_max_percent, preset.am_depth_max_percent, preset.secondary_am_depth_max_percent):
        if value > 100:
            raise ValueError("Modulation depth percentages cannot exceed 100")
    if preset.transition_min_seconds < 1 or preset.transition_max_seconds > 60:
        raise ValueError("Transition range must remain between 1 and 60 seconds")
    if not 1 <= preset.session_duration_minutes <= 480:
        raise ValueError("Session duration must be between 1 and 480 minutes")
    return preset


class PresetStore:
    def __init__(self, path: str | Path = PRESET_PATH) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._presets: dict[str, SignalPreset] = {}
        self._active = ""
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    presets = [validate_preset(item) for item in data.get("presets", [])]
                    self._presets = {item.name: item for item in presets}
                    requested = str(data.get("active") or "")
                    self._active = requested if requested in self._presets else next(iter(self._presets), "")
                except Exception:
                    self._presets = {}
            if not self._presets:
                default = SignalPreset()
                self._presets = {default.name: default}
                self._active = default.name
                self.save()

    def save(self) -> None:
        with self._lock:
            payload = {"version": 1, "active": self._active, "presets": [asdict(item) for item in self._presets.values()]}
            self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def names(self) -> list[str]:
        with self._lock:
            return list(self._presets)

    def active(self) -> SignalPreset:
        with self._lock:
            return self._presets[self._active]

    def select(self, name: str) -> SignalPreset:
        with self._lock:
            if name not in self._presets:
                raise KeyError(name)
            self._active = name
            self.save()
            return self._presets[name]

    def put(self, preset: SignalPreset | dict[str, Any]) -> SignalPreset:
        validated = validate_preset(preset)
        with self._lock:
            self._presets[validated.name] = validated
            self._active = validated.name
            self.save()
        return validated

