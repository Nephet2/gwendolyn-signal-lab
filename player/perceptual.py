from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .presets import SignalPreset


@dataclass(frozen=True)
class PerceptualReading:
    character: str
    energy: float
    texture: str
    carrier_mean_hz: float
    carrier_separation_hz: float
    explanation: str


def _number(state: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = state.get(key, default)
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def semantic_state(
    *, intensity: float, texture: float, vibration_rate: float,
    vibration_depth: float, secondary_rate: float | None = None,
    secondary_depth: float | None = None, modulation: float = 0.0,
    preset: SignalPreset | None = None, active: bool = True,
) -> dict[str, Any]:
    """Translate semantic controls into values inside an operator-owned preset."""
    preset = preset or SignalPreset()
    intensity = min(100.0, max(0.0, float(intensity)))
    texture = min(100.0, max(0.0, float(texture)))
    vibration_rate = min(100.0, max(0.0, float(vibration_rate)))
    vibration_depth = min(100.0, max(0.0, float(vibration_depth)))
    secondary_rate = vibration_rate if secondary_rate is None else min(100.0, max(0.0, float(secondary_rate)))
    secondary_depth = vibration_depth if secondary_depth is None else min(100.0, max(0.0, float(secondary_depth)))
    modulation = min(100.0, max(0.0, float(modulation)))
    scale = lambda low, high, value: low + (high - low) * value / 100.0
    carrier = round(scale(preset.frequency_min_hz, preset.frequency_max_hz, texture), 3)
    volume = round(scale(preset.volume_min, preset.volume_max, intensity), 3)
    am_rate = round(scale(preset.am_rate_min_hz, preset.am_rate_max_hz, vibration_rate), 3)
    am_depth = round(scale(preset.am_depth_min_percent, preset.am_depth_max_percent, vibration_depth), 3)
    sam_rate = round(scale(preset.secondary_am_rate_min_hz, preset.secondary_am_rate_max_hz, secondary_rate), 3)
    sam_depth = round(scale(preset.secondary_am_depth_min_percent, preset.secondary_am_depth_max_percent, secondary_depth), 3)
    fm_percent = scale(preset.fm_depth_min_percent, preset.fm_depth_max_percent, modulation) if modulation > 0 else 0.0
    fm_depth = carrier * fm_percent / 100.0
    fm_depth = round(min(fm_depth, carrier - preset.frequency_min_hz, preset.frequency_max_hz - carrier), 3)
    return {
        "active": bool(active),
        "volume": volume,
        "freq": carrier,
        "amType": "sine",
        "amDepth": am_depth,
        "amFreq": am_rate,
        "secondaryAmType": "sine",
        "secondaryAmDepth": sam_depth,
        "secondaryAmFreq": sam_rate,
        "fmType": "sine" if fm_depth > 0 else "none",
        "fmDepth": fm_depth,
        "fmFreq": round(max(0.05, sam_rate / 2.0), 3) if fm_depth > 0 else 0.0,
        "rampTarget": volume,
        "rampRate": 0.0,
    }


def analyse_pair(left: dict[str, Any], right: dict[str, Any]) -> PerceptualReading:
    lf, rf = _number(left, "freq", 500), _number(right, "freq", 500)
    mean = (lf + rf) / 2.0
    separation = abs(lf - rf)
    volume = (_number(left, "volume") + _number(right, "volume")) / 200.0
    frequency_factor = 0.8 + 0.4 * min(1.0, max(0.0, (mean - 500.0) / 700.0))
    am_depth = (_number(left, "amDepth") + _number(right, "amDepth")) / 200.0
    am_rate = min(1.0, (_number(left, "amFreq") + _number(right, "amFreq")) / 10.0)
    energy = round(volume * frequency_factor * (1.0 + 0.25 * am_depth + 0.12 * am_rate), 4)

    ld, rd = _number(left, "fmDepth"), _number(right, "fmDepth")
    lr, rr = _number(left, "fmFreq"), _number(right, "fmFreq")
    high, low = max(ld, rd), min(ld, rd)
    if high <= 0.001:
        character = "neutral"
        explanation = "AM shapes vibration; no active FM relationship."
    elif abs(lr - rr) >= max(0.02, (lr + rr) * 0.12):
        character = "drifting"
        explanation = "Different FM rates make the two lanes slowly change relationship."
    elif low / high <= 0.35:
        character = "pressure-tapping"
        explanation = "Strong FM-depth asymmetry creates a more articulated relationship."
    elif low / high <= 0.75:
        character = "articulated"
        explanation = "Moderate FM-depth asymmetry separates the lane textures."
    else:
        character = "blended"
        explanation = "Closely matched FM settings make the lanes feel more continuous."

    if mean < 675:
        texture_name = "rougher"
    elif mean < 950:
        texture_name = "balanced"
    else:
        texture_name = "smoother"
    return PerceptualReading(character, energy, texture_name, round(mean, 2), round(separation, 2), explanation)
