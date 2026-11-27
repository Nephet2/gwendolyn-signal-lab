"""Separate texture selection from the shared level and balance controls."""
from dataclasses import dataclass


MODES = ("Manual", "Reference JSON")
FIELDS = (
    ("Carrier (Hz)", "freq", 700),
    ("AM rate (Hz)", "amFreq", 2),
    ("AM depth (%)", "amDepth", 20),
    ("Secondary AM rate (Hz)", "secondaryAmFreq", .5),
    ("Secondary AM depth (%)", "secondaryAmDepth", 10),
    ("FM rate (Hz)", "fmFreq", .25),
    ("FM depth (Hz)", "fmDepth", 0),
)


@dataclass(frozen=True)
class Levels:
    left_trim: float = 100
    right_trim: float = 100
    start: float = 100
    end: float = 100
    seconds: float = 0

    def apply(self, balance, position_ms):
        fraction = min(1, max(0, position_ms / (self.seconds * 1000))) if self.seconds else 1
        shared = (self.start + fraction * (self.end - self.start)) / 100
        return balance[0] * shared * self.left_trim / 100, balance[1] * shared * self.right_trim / 100


def manual_state():
    return {**{key: default for _, key, default in FIELDS},
            "active": True, "amType": "sine", "secondaryAmType": "sine", "fmType": "sine"}


def texture_only(state, level):
    if not state:
        return {}
    return dict(state, volume=level, rampTarget=level, rampRate=0)


# Gwendolyn's level band (opt-in in the trial window): each lane may sit up to 15% above or
# below the preset's starting level. The player enforces it, whatever a client sends.
LEVEL_BAND_PERCENT = 15.0


def clamp_offset(offset):
    return max(-LEVEL_BAND_PERCENT, min(LEVEL_BAND_PERCENT, float(offset)))


def intensity_offset(intensity, anchor):
    """Director semantic intensity 0-100 onto the band. The anchor (the intensity playing when the
    band started) is the baseline; 100 is the top of the band and 0 the bottom."""
    intensity = min(100.0, max(0.0, float(intensity)))
    anchor = min(100.0, max(0.0, float(anchor)))
    span = (100.0 - anchor) if intensity >= anchor else anchor
    if span <= 0:
        return 0.0
    return round(clamp_offset((intensity - anchor) / span * LEVEL_BAND_PERCENT), 3)


def banded_level(baseline, offset, low=0.0, high=100.0):
    return round(min(high, max(low, float(baseline) * (1.0 + clamp_offset(offset) / 100.0))), 3)
