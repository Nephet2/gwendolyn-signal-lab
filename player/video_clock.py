"""Read-only playback clocks for the isolated Signal Lab trial."""
from dataclasses import dataclass
import json
import math
import socket
import struct
import time
from threading import Event, Thread

from .mpc_clock import poll_mpc


@dataclass(frozen=True)
class Tick:
    position_ms: int
    playing: bool
    resource: str
    received: float
    rate: float = 1.0


def here_tick(data):
    if not isinstance(data, dict):
        raise ValueError("Expected a timestamp object")
    resource = data.get("resource") or data.get("path")
    position, state = data.get("currentTime"), data.get("playerState")
    if not isinstance(resource, str) or not resource.strip():
        return None
    if type(position) not in (int, float) or not math.isfinite(position) or position < 0 or type(state) is not int:
        return None
    rate = data.get("playbackSpeed", 1.0)
    if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
        rate = 1.0
    return Tick(round(position * 1000), state == 0, resource, time.monotonic(), rate)


class VideoClock:
    def __init__(self, player, host, port):
        self.player, self.host, self.port = player, host, port
        self.latest = None
        self.stop = Event()
        Thread(target=self.run, daemon=True, name="signal-lab-video-clock").start()

    def run(self):
        while not self.stop.is_set():
            try:
                if self.player == "MPC-HC":
                    state = poll_mpc(self.host, self.port)
                    if (state.connected and state.media_path and state.position_seconds is not None
                            and math.isfinite(state.position_seconds) and state.position_seconds >= 0
                            and state.state in ("playing", "paused")):
                        rate = state.rate if math.isfinite(state.rate) and state.rate > 0 else 1.0
                        self.latest = Tick(round(state.position_seconds * 1000), state.state == "playing",
                                           state.media_path, time.monotonic(), rate)
                    else:
                        self.latest = None
                else:
                    self.read_heresphere()
            except (OSError, ValueError, UnicodeError, OverflowError):
                self.latest = None
            self.stop.wait(.15 if self.player == "MPC-HC" else .5)

    def read_heresphere(self):
        with socket.create_connection((self.host, self.port), timeout=.75) as sock:
            sock.settimeout(.25)
            pending = bytearray()
            next_ping = time.monotonic()
            while not self.stop.is_set():
                now = time.monotonic()
                if now >= next_ping:
                    sock.sendall(bytes(4))
                    next_ping = now + 1
                try:
                    block = sock.recv(8192)
                except socket.timeout:
                    continue
                if not block:
                    break
                pending.extend(block)
                while len(pending) >= 4:
                    length = struct.unpack_from("<i", pending)[0]
                    if not 0 <= length <= 65536:
                        raise ValueError("Invalid HereSphere timestamp length")
                    if len(pending) < 4 + length:
                        break
                    if length:
                        self.latest = here_tick(json.loads(bytes(pending[4:4+length])))
                    del pending[:4+length]
            self.latest = None
