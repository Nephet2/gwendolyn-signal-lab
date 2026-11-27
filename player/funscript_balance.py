"""Read a single-axis funscript and attenuate the opposite stereo lane."""
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from functools import cached_property
import json
import math
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname


def matching_funscript(resource: str) -> Path | None:
    """Find the exact sidecar of a Windows-accessible video path."""
    resource = resource.strip()
    if resource.lower().startswith("file:"):
        uri = urlsplit(resource)
        prefix = f"//{uri.netloc}" if uri.netloc.lower() not in ("", "localhost") else ""
        resource = url2pathname(prefix + uri.path)
    path = Path(resource)
    if not path.is_absolute() or not path.name:
        return None
    candidate = path.with_suffix(".funscript")
    return candidate if candidate.is_file() else None


@dataclass(frozen=True)
class BalanceCurve:
    times: tuple[int, ...]
    positions: tuple[float, ...]

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        actions = data.get("actions") if isinstance(data, dict) else None
        if not isinstance(actions, list) or not actions:
            raise ValueError("Expected a funscript with a nonempty actions list.")
        points = {}
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError("Each action needs an at time and a pos value.")
            at, pos = action.get("at"), action.get("pos")
            if type(at) is not int or at < 0 or type(pos) not in (int, float) or not math.isfinite(pos) or not 0 <= pos <= 100:
                raise ValueError("Action times must be nonnegative milliseconds and positions must be 0–100.")
            points[at] = float(pos)
        pairs = sorted(points.items())
        return cls(tuple(at for at, _ in pairs), tuple(pos for _, pos in pairs))

    @cached_property
    def gradients(self):
        """Monotone tangents in position points per millisecond."""
        if len(self.times) == 1:
            return (0.0,)
        widths = [right - left for left, right in zip(self.times, self.times[1:])]
        slopes = [(right - left) / width
                  for left, right, width in zip(self.positions, self.positions[1:], widths)]
        result = [0.0]
        for index, (before, after) in enumerate(zip(slopes, slopes[1:])):
            if before * after <= 0:
                result.append(0.0)
                continue
            # A weighted harmonic mean keeps the cubic within the authored range.
            left, right = widths[index:index + 2]
            first, second = 2 * right + left, right + 2 * left
            result.append((first + second) / (first / before + second / after))
        return (*result, 0.0)

    def sample(self, time_ms, interpolation="Linear"):
        if interpolation not in ("Linear", "Gradient"):
            raise ValueError(f"Unknown interpolation: {interpolation}")
        right = bisect_right(self.times, time_ms)
        if right == 0:
            return self.positions[0], 0.0
        if right == len(self.times):
            return self.positions[-1], 0.0
        left = right - 1
        duration = self.times[right] - self.times[left]
        x = (time_ms - self.times[left]) / duration
        start, end = self.positions[left], self.positions[right]
        if interpolation == "Linear":
            return start + x * (end - start), 1000 * (end - start) / duration
        start_slope = self.gradients[left] * duration
        end_slope = self.gradients[right] * duration
        # Cubic Hermite in Horner form. Restore velocity to points per second.
        cubic = 2 * (start - end) + start_slope + end_slope
        quadratic = 3 * (end - start) - 2 * start_slope - end_slope
        position = start + x * (start_slope + x * (quadratic + x * cubic))
        speed = (start_slope + x * (2 * quadratic + 3 * x * cubic)) / duration
        return position, 1000 * speed

    def position(self, time_ms, interpolation="Linear"):
        return self.sample(time_ms, interpolation)[0]


BALANCE_CURVES = ("Equal power", "Original balance")


def gains(position, curve="Original balance"):
    fraction = min(1.0, max(0.0, position / 100))
    if curve == "Equal power":
        if fraction == 0:
            return 1.0, 0.0
        if fraction == 1:
            return 0.0, 1.0
        angle = fraction * math.pi / 2
        return math.cos(angle), math.sin(angle)
    if curve == "Original balance":
        return min(1.0, 2 * (1 - fraction)), min(1.0, 2 * fraction)
    raise ValueError(f"Unknown balance curve: {curve}")


def scale_stereo(frames, previous, current):
    if previous == current == (1.0, 1.0):
        return frames
    samples = array("h", frames)
    count = len(samples) // 2
    for i in range(count):
        fraction = (i + 1) / count
        for lane in range(2):
            gain = previous[lane] + (current[lane] - previous[lane]) * fraction
            samples[2*i + lane] = int(samples[2*i + lane] * gain)
    return samples.tobytes()
