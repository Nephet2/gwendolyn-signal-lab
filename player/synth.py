from __future__ import annotations

from array import array
from bisect import bisect_right
from io import BytesIO
import math
import wave
from typing import Any

from .session_model import SignalEvent, SignalSession


TAU = math.tau


def _number(state: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = state.get(key, default)
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def waveform(name: str, phase: float) -> float:
    cycle = (phase / TAU) % 1.0
    name = str(name or "none").lower()
    if name == "none":
        return 0.0
    if name == "sine":
        return math.sin(phase)
    if name == "square":
        return 1.0 if cycle < 0.5 else -1.0
    if name == "triangle":
        return 1.0 - 4.0 * abs(cycle - 0.5)
    if name == "sawtooth":
        return 2.0 * cycle - 1.0
    raise ValueError(f"Unsupported waveform: {name}")


class ChannelRenderer:
    def __init__(self, events: tuple[SignalEvent, ...], offset_ms: int = 0) -> None:
        if not events:
            raise ValueError("Cannot render an empty signal lane")
        starts = [event.time_ms for event in events]
        self.events = events
        self.index = max(0, bisect_right(starts, offset_ms) - 1)
        self.carrier_phase = 0.0
        self.am_phase = 0.0
        self.fm_phase = 0.0
        self.secondary_am_phase = 0.0

    def _state(self, position_ms: float) -> tuple[dict[str, Any], float]:
        while self.index + 1 < len(self.events) and position_ms >= self.events[self.index + 1].time_ms:
            self.index += 1
        event = self.events[self.index]
        state = event.message
        volume = _number(state, "volume")
        target = _number(state, "rampTarget", volume)
        rate = max(0.0, _number(state, "rampRate"))
        if rate:
            elapsed_s = max(0.0, (position_ms - event.time_ms) / 1000.0)
            movement = rate * elapsed_s
            volume = min(target, volume + movement) if target >= volume else max(target, volume - movement)
        return state, volume

    def sample(self, position_ms: float, sample_rate: int) -> float:
        state, volume = self._state(position_ms)
        if not bool(state.get("active", False)):
            return 0.0
        fm = waveform(state.get("fmType", "none"), self.fm_phase) * _number(state, "fmDepth")
        carrier_hz = min(sample_rate * 0.45, max(1.0, _number(state, "freq", 500.0) + fm))
        self.carrier_phase = (self.carrier_phase + TAU * carrier_hz / sample_rate) % TAU
        self.am_phase = (self.am_phase + TAU * max(0.0, _number(state, "amFreq")) / sample_rate) % TAU
        self.fm_phase = (self.fm_phase + TAU * max(0.0, _number(state, "fmFreq")) / sample_rate) % TAU
        self.secondary_am_phase = (self.secondary_am_phase + TAU * max(0.0, _number(state, "secondaryAmFreq")) / sample_rate) % TAU
        depth = min(1.0, max(0.0, _number(state, "amDepth") / 100.0))
        envelope = 1.0 - depth / 2.0 + waveform(state.get("amType", "none"), self.am_phase) * depth / 2.0
        secondary_depth = min(1.0, max(0.0, _number(state, "secondaryAmDepth") / 100.0))
        secondary_envelope = 1.0 - secondary_depth / 2.0 + waveform(state.get("secondaryAmType", "none"), self.secondary_am_phase) * secondary_depth / 2.0
        return math.sin(self.carrier_phase) * min(1.0, max(0.0, volume / 100.0)) * max(0.0, envelope) * max(0.0, secondary_envelope)


def _encode(left: ChannelRenderer, right: ChannelRenderer, *, offset_s: float,
            duration_s: float, sample_rate: int, preview_gain: float,
            start_preview_gain: float | None = None) -> bytes:
    frames = int(round(duration_s * sample_rate))
    pcm = array("h")
    for index in range(frames):
        position_ms = (offset_s + index / sample_rate) * 1000.0
        if start_preview_gain is None or frames <= 1:
            gain = preview_gain
        else:
            gain = start_preview_gain + (preview_gain - start_preview_gain) * index / (frames - 1)
        for renderer in (left, right):
            value = min(1.0, max(-1.0, renderer.sample(position_ms, sample_rate) * gain))
            pcm.append(int(round(value * 32767)))
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


def render_session(session: SignalSession, *, offset_s: float, duration_s: float = 10.0,
                   sample_rate: int = 44100, preview_gain: float = 0.05) -> bytes:
    if not 8000 <= int(sample_rate) <= 96000:
        raise ValueError("sample_rate must be between 8000 and 96000")
    if not 0.001 <= float(preview_gain) <= 0.10:
        raise ValueError("preview_gain must be between 0.001 and 0.10")
    offset_s = max(0.0, float(offset_s))
    remaining = session.duration_ms / 1000.0 - offset_s
    duration_s = min(float(duration_s), remaining, 30.0)
    if duration_s <= 0:
        raise ValueError("Preview position is beyond the session duration")
    return _encode(
        ChannelRenderer(session.left, int(offset_s * 1000)),
        ChannelRenderer(session.right, int(offset_s * 1000)),
        offset_s=offset_s, duration_s=duration_s,
        sample_rate=sample_rate, preview_gain=preview_gain,
    )


def render_states(left_state: dict[str, Any], right_state: dict[str, Any], *,
                  duration_s: float = 10.0, sample_rate: int = 44100,
                  preview_gain: float = 0.05) -> bytes:
    duration_ms = int(round(duration_s * 1000))
    left = (SignalEvent(0, duration_ms, dict(left_state)),)
    right = (SignalEvent(0, duration_ms, dict(right_state)),)
    return _encode(ChannelRenderer(left), ChannelRenderer(right), offset_s=0.0,
                   duration_s=duration_s, sample_rate=sample_rate, preview_gain=preview_gain)


def render_states_continuous(left_state: dict[str, Any], right_state: dict[str, Any], *,
                             phase_state: dict[str, dict[str, float]],
                             duration_s: float = 10.0, sample_rate: int = 44100,
                             preview_gain: float = 0.05,
                             start_preview_gain: float | None = None) -> bytes:
    """Render a state block while preserving oscillator phases between calls.

    ``render_states`` intentionally creates a self-contained preview.  A live
    monitor, however, writes many adjacent blocks to one output stream.  Resetting
    the carrier and modulation phases at every block boundary creates a waveform
    discontinuity that is heard as a click.  This variant updates the caller-owned
    phase dictionary so consecutive blocks remain sample-continuous.
    """
    duration_ms = int(round(duration_s * 1000))
    renderers = (
        ChannelRenderer((SignalEvent(0, duration_ms, dict(left_state)),)),
        ChannelRenderer((SignalEvent(0, duration_ms, dict(right_state)),)),
    )
    phase_names = ("carrier_phase", "am_phase", "fm_phase", "secondary_am_phase")
    for lane_name, renderer in zip(("left", "right"), renderers):
        saved = phase_state.get(lane_name, {})
        for name in phase_names:
            setattr(renderer, name, float(saved.get(name, 0.0)))

    payload = _encode(renderers[0], renderers[1], offset_s=0.0,
                      duration_s=duration_s, sample_rate=sample_rate,
                      preview_gain=preview_gain,
                      start_preview_gain=start_preview_gain)

    for lane_name, renderer in zip(("left", "right"), renderers):
        phase_state[lane_name] = {
            name: float(getattr(renderer, name)) for name in phase_names
        }
    return payload
