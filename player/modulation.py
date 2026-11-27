"""A bounded, smoothly changing overlay; authored timing and envelopes stay intact."""
import math
import threading
import time


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Expected a finite number')
    if not low <= value <= high:
        raise ValueError(f'Value must be between {low} and {high}')
    return float(value)


class SourceModulation:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.active = False
        self.start = self.target = {'left': {'freq': 0., 'volume': 0.}, 'right': {'freq': 0., 'volume': 0.}}
        self.started = 0.
        self.duration = 1.
        self.limits = None

    def arm(self, left, right, preset):
        with self.lock:
            if self.active:
                return
            self.baseline = {'left': dict(left), 'right': dict(right)}
            self.limits = preset
            self.start = {lane: {'freq': 0., 'volume': 0.} for lane in ('left', 'right')}
            self.target = {lane: dict(values) for lane, values in self.start.items()}
            self.started = self.clock()
            self.duration = 1.
            self.active = True

    def _current(self):
        fraction = min(1., max(0., (self.clock() - self.started) / self.duration))
        fraction = fraction * fraction * (3 - 2 * fraction)
        return {lane: {key: self.start[lane][key] + (self.target[lane][key] - self.start[lane][key]) * fraction
                       for key in ('freq', 'volume')} for lane in ('left', 'right')}

    def command(self, payload, presets):
        with self.lock:
            if not self.active:
                raise ValueError('Source modulation is not armed')
            kind = payload.get('kind')
            lane = payload.get('lane', 'both')
            if lane not in ('left', 'right', 'both'):
                raise ValueError('lane must be left, right, or both')
            p = self.limits
            duration = number(payload.get('transition_seconds'), p.transition_min_seconds, p.transition_max_seconds)
            changes = {}
            if kind in ('texture', 'intensity'):
                value = number(payload.get('value'), 0, 100)
                key, low, high = ('freq', p.frequency_min_hz, p.frequency_max_hz) if kind == 'texture' else ('volume', p.volume_min, p.volume_max)
                changes[key] = low + (high - low) * value / 100
            elif kind == 'preset':
                chosen = presets.get(payload.get('preset'))
                if chosen is None:
                    raise ValueError('Unknown operator-owned preset')
                # Presets supply starting texture/intensity, never expand the latched limits.
                changes = {'freq': min(p.frequency_max_hz, max(p.frequency_min_hz, chosen.frequency_start_hz)),
                           'volume': min(p.volume_max, max(p.volume_min, chosen.volume_start))}
            else:
                raise ValueError('kind must be texture, intensity, or preset')
            target = {side: dict(values) for side, values in self.target.items()}
            for side in ('left', 'right') if lane == 'both' else (lane,):
                for key, value in changes.items():
                    target[side][key] = value - float(self.baseline[side].get(key, value))
            self.start, self.target = self._current(), target
            self.started, self.duration = self.clock(), duration
            return self.snapshot()

    def transform(self, left, right):
        with self.lock:
            if not self.active:
                return dict(left), dict(right)
            offsets, p = self._current(), self.limits
            result = []
            for side, source in (('left', left), ('right', right)):
                state = dict(source)
                # The configured minimum is a control target, not a floor for
                # authored fades: reducing a quiet frame must never raise it.
                for key, low, high in (('freq', p.frequency_min_hz, p.frequency_max_hz), ('volume', 0., p.volume_max)):
                    if offsets[side][key] != 0 and key in state:
                        # Preserve authored silent sections.
                        if key == 'volume' and float(state[key]) <= 0:
                            continue
                        state[key] = min(high, max(low, float(state[key]) + offsets[side][key]))
                if offsets[side]['volume'] and float(source.get('rampTarget', 0)) > 0:
                    state['rampTarget'] = min(p.volume_max, max(0., float(source['rampTarget']) + offsets[side]['volume']))
                # Existing FM must also fit around the shifted carrier.
                if offsets[side]['freq'] and 'fmDepth' in state:
                    state['fmDepth'] = max(0., min(float(state['fmDepth']), float(state['freq']) - p.frequency_min_hz,
                                                  p.frequency_max_hz - float(state['freq'])))
                result.append(state)
            return tuple(result)

    def stop(self):
        with self.lock:
            self.active = False
        return {'active': False}

    def snapshot(self):
        with self.lock:
            return {'active': self.active, 'offsets': self._current() if self.active else {},
                    'target_offsets': self.target if self.active else {}, 'transition_seconds': self.duration,
                    'source_preserved': True}
