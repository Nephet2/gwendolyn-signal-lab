from __future__ import annotations

from io import BytesIO
from threading import RLock, Thread
import wave
from typing import Any, Callable

from .synth import render_states_continuous
from .pressure_control import PressureControl
from .funscript_balance import scale_stereo


MONITOR_GAIN = 0.10
MONITOR_MAX_GAIN = 0.50
MONITOR_BLOCK_SECONDS = 0.10
MONITOR_SAMPLE_RATE = 44100
MONITOR_STREAM_LATENCY = "high"

# Human operators normally alter one perceptual dimension, listen briefly, then
# move to the next. Keep intensity last so character changes can settle before
# level changes. Both lanes move together within each perceptual dimension.
STAGED_PARAMETER_GROUPS = (
    ("freq",),
    ("amFreq", "secondaryAmFreq"),
    ("amDepth", "secondaryAmDepth"),
    ("fmType", "fmDepth", "fmFreq"),
    ("volume", "rampTarget"),
)


class MonitorError(RuntimeError):
    pass


def _sounddevice():
    try:
        import sounddevice as sd
        return sd
    except Exception as exc:
        raise MonitorError("Python package 'sounddevice' is not available") from exc


def validate_output_device(index: int, name: str, query: Callable[[], Any] | None = None) -> int:
    devices = list((query or _sounddevice().query_devices)())
    if not 0 <= int(index) < len(devices):
        raise MonitorError("The latched PortAudio endpoint is no longer present")
    item = devices[int(index)]
    if int(item.get("max_output_channels", 0)) < 2 or str(item.get("name", "")) != str(name):
        raise MonitorError("The latched PortAudio index/name pair has changed")
    return int(index)


def staged_state_plan(left: dict, right: dict, target_left: dict, target_right: dict,
                      transition_seconds: float) -> list[tuple[dict, dict]]:
    """Build one-parameter-at-a-time, smoothly stepped monitor states."""
    current = [dict(left), dict(right)]
    targets = [dict(target_left), dict(target_right)]
    changed_groups = [
        group for group in STAGED_PARAMETER_GROUPS
        if any(current[lane].get(key) != targets[lane].get(key)
               for lane in (0, 1) for key in group)
    ]
    if not changed_groups:
        return [(dict(target_left), dict(target_right))]
    # Spread each dimension across enough monitor blocks to preserve the requested
    # transition duration. Short blocks keep transport and volume controls responsive.
    steps = max(2, int(round(
        float(transition_seconds) / (len(changed_groups) * MONITOR_BLOCK_SECONDS)
    )))
    plan: list[tuple[dict, dict]] = []
    for group in changed_groups:
        starts = [dict(current[0]), dict(current[1])]
        for step in range(1, steps + 1):
            fraction = step / steps
            staged = [dict(current[0]), dict(current[1])]
            for lane in (0, 1):
                for key in group:
                    if key not in targets[lane]:
                        continue
                    old = starts[lane].get(key, targets[lane][key])
                    new = targets[lane][key]
                    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
                        staged[lane][key] = old + (new - old) * fraction
                    elif step == steps:
                        staged[lane][key] = new
            plan.append((staged[0], staged[1]))
        for lane in (0, 1):
            for key in group:
                if key in targets[lane]:
                    current[lane][key] = targets[lane][key]
    # Carry any non-perceptual metadata into the settled final state.
    plan[-1] = (dict(target_left), dict(target_right))
    return plan


class HeadphoneMonitor:
    """Explicitly armed continuous audible monitor on one exact endpoint."""

    def __init__(self) -> None:
        self.armed = False
        self.pressure_control = PressureControl()
        self.last_error = ""
        self.underflow_count = 0
        self._lock = RLock()
        self._generation = 0
        self._left: dict = {}
        self._right: dict = {}
        self._target_left: dict = {}
        self._target_right: dict = {}
        self._plan: list[tuple[dict, dict]] = []
        self._paused = True
        self._gain = MONITOR_GAIN
        self._balance = (1.0, 1.0)
        self._analyser_blocks: tuple[bytes, ...] = ()

    def arm(self, device_index: int, device_name: str, left: dict, right: dict, *,
            gain: float = MONITOR_GAIN, start_paused: bool = True) -> None:
        self.pressure_control.target()
        if self.pressure_control.fault: raise MonitorError(self.pressure_control.fault)
        validate_output_device(device_index, device_name)
        with self._lock:
            self.armed = True
            self.last_error = ""
            self.underflow_count = 0
            self._left, self._right = dict(left), dict(right)
            self._target_left, self._target_right = dict(left), dict(right)
            self._plan = []
            self._paused = bool(start_paused)
            self._gain = min(MONITOR_MAX_GAIN, max(0.0, float(gain)))
            self._analyser_blocks = ()
            self._generation += 1
            generation = self._generation

        def worker() -> None:
            try:
                sd = _sounddevice()
                device = validate_output_device(device_index, device_name, sd.query_devices)
                phase_state: dict[str, dict[str, float]] = {}
                output_gain = 0.0
                output_balance = (1.0, 1.0)
                with sd.RawOutputStream(samplerate=MONITOR_SAMPLE_RATE, channels=2, dtype="int16", device=device,
                                        latency=MONITOR_STREAM_LATENCY) as stream:
                    while True:
                        with self._lock:
                            if not self.armed or generation != self._generation:
                                return
                            if self._plan:
                                self._left, self._right = self._plan.pop(0)
                            left_state, right_state = dict(self._left), dict(self._right)
                            paused = self._paused
                            target_gain = 0.0 if paused else self._gain
                            balance = self._balance
                        payload = render_states_continuous(
                            left_state, right_state, phase_state=phase_state,
                            duration_s=MONITOR_BLOCK_SECONDS,
                            sample_rate=MONITOR_SAMPLE_RATE, preview_gain=target_gain,
                            start_preview_gain=output_gain,
                        )
                        output_gain = target_gain
                        with wave.open(BytesIO(payload), "rb") as wav:
                            frames = wav.readframes(wav.getnframes())
                        frames = scale_stereo(frames, output_balance, balance)
                        output_balance = balance
                        frames = self.pressure_control.pcm16(frames)
                        underflowed = stream.write(frames)
                        with self._lock:
                            if generation != self._generation:
                                return
                            if underflowed:
                                self.underflow_count += 1
                            if not paused and not self._paused:
                                self._analyser_blocks = (*self._analyser_blocks[-1:], frames)
            except Exception as exc:
                with self._lock:
                    if generation == self._generation:
                        self.last_error = str(exc)
                        self.armed = False
                        self._analyser_blocks = ()

        Thread(target=worker, name="signal-lab-continuous-monitor", daemon=True).start()

    def update_states(self, left: dict, right: dict, transition_seconds: float = 10.0) -> None:
        with self._lock:
            target_left, target_right = dict(left), dict(right)
            if target_left == self._target_left and target_right == self._target_right:
                return
            self._target_left, self._target_right = target_left, target_right
            self._plan = staged_state_plan(
                self._left, self._right, target_left, target_right, transition_seconds
            )

    def set_gain(self, gain: float) -> None:
        with self._lock:
            self._gain = min(MONITOR_MAX_GAIN, max(0.0, float(gain)))

    def set_balance(self, balance: tuple[float, float]) -> None:
        with self._lock:
            self._balance = balance

    def analyser_snapshot(self) -> tuple[bytes, int] | None:
        with self._lock:
            blocks = self._analyser_blocks
        return (b"".join(blocks), MONITOR_SAMPLE_RATE) if blocks else None

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._analyser_blocks = ()

    def resume(self) -> None:
        with self._lock:
            if self.armed:
                self._paused = False

    def status(self) -> dict[str, float | bool]:
        with self._lock:
            return {
                "pressure_control": self.pressure_control.snapshot(),
                "armed": bool(self.armed),
                "paused": bool(self._paused),
                "audible": bool(self.armed and not self._paused and self._gain > 0),
                "underflow_count": self.underflow_count,
                "gain_percent": round(self._gain * 100.0, 1),
            }

    def disarm(self) -> None:
        with self._lock:
            self.armed = False
            self._paused = True
            self._generation += 1
            self._plan = []
            self._analyser_blocks = ()

    def stop(self) -> None:
        self.disarm()
