from __future__ import annotations

from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Queue
from threading import Lock, Thread
import time
from typing import Any
from urllib.parse import urlparse

from .perceptual import analyse_pair, semantic_state
from .presets import PresetStore


BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 18766
SEMANTIC_KEYS = ("intensity", "texture", "vibration_rate", "vibration_depth")
OPTIONAL_SEMANTIC_KEYS = ("secondary_rate", "secondary_depth", "modulation")
HEART_TEMPO_ORIGIN = "http://127.0.0.1:8766"


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class AppliedCommand:
    request_id: str
    received_at: float
    transition_seconds: float
    left_semantic: dict[str, float]
    right_semantic: dict[str, float]
    left_signal: dict[str, Any]
    right_signal: dict[str, Any]
    perceptual: dict[str, Any]
    reason: str
    preset_name: str
    audio_enabled: bool = False


def _semantic(value: Any, name: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise CommandError(f"{name} must be an object")
    result: dict[str, float] = {}
    for key in SEMANTIC_KEYS:
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise CommandError(f"{name}.{key} must be a number from 0 to 100")
        number = float(raw)
        if not 0 <= number <= 100:
            raise CommandError(f"{name}.{key} must be between 0 and 100")
        result[key] = round(number, 3)
    for key in OPTIONAL_SEMANTIC_KEYS:
        raw = value.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= float(raw) <= 100:
            raise CommandError(f"{name}.{key} must be between 0 and 100")
        result[key] = round(float(raw), 3)
    return result


def validate_command(payload: Any, preset_store: PresetStore | None = None) -> AppliedCommand:
    if not isinstance(payload, dict):
        raise CommandError("Command must be a JSON object")
    common = payload.get("both")
    left = _semantic(payload.get("left", common), "left")
    right = _semantic(payload.get("right", common), "right")
    transition = payload.get("transition_seconds", 10)
    if isinstance(transition, bool) or not isinstance(transition, (int, float)):
        raise CommandError("transition_seconds must be numeric")
    transition = float(transition)
    store = preset_store or PresetStore()
    preset = store.active()
    if not preset.transition_min_seconds <= transition <= preset.transition_max_seconds:
        raise CommandError(f"transition_seconds must be between {preset.transition_min_seconds:g} and {preset.transition_max_seconds:g} for preset {preset.name}")
    request_id = str(payload.get("request_id") or f"director-{time.time_ns()}")[:100]
    reason = str(payload.get("reason") or "Director semantic command")[:300]
    left_signal = semantic_state(**left, preset=preset)
    right_signal = semantic_state(**right, preset=preset)
    reading = analyse_pair(left_signal, right_signal)
    return AppliedCommand(
        request_id=request_id,
        received_at=time.time(),
        transition_seconds=round(transition, 3),
        left_semantic=left,
        right_semantic=right,
        left_signal=left_signal,
        right_signal=right_signal,
        perceptual=asdict(reading),
        reason=reason,
        preset_name=preset.name,
    )


class DirectorState:
    def __init__(self, preset_store: PresetStore | None = None) -> None:
        self._lock = Lock()
        self._last: AppliedCommand | None = None
        self._live: dict[str, Any] = {
            "source": "none", "session": None, "position_ms": 0,
            "duration_ms": 0, "playing": False, "left_signal": None,
            "right_signal": None,
            "headphone_monitor": {
                "armed": False, "audible": False, "gain_percent": 0.0,
                "device": None,
            },
        }
        self._heart_tempo: dict[str, Any] = {
            "available": False, "bpm": None, "ratio": None,
            "target_cpm": None, "confidence": 0.0, "received_at": 0.0,
        }
        self._heart_tempo_control: dict[str, Any] = {
            "active": False, "target": None, "mode": "off", "ratio": None,
            "requested_at": 0.0,
        }
        self.events: Queue[dict[str, Any]] = Queue()
        self.preset_store = preset_store or PresetStore()

    def apply(self, payload: Any) -> dict[str, Any]:
        command = validate_command(payload, self.preset_store)
        with self._lock:
            self._last = command
        result = {"ok": True, "status": "applied_offline", "command": asdict(command)}
        self.events.put({"type": "command", **result})
        return result

    def neutral(self, reason: str = "Local neutral") -> dict[str, Any]:
        with self._lock:
            previous = self._last
            self._last = None
        result = {
            "ok": True,
            "status": "neutral_offline",
            "reason": str(reason)[:300],
            "previous_request_id": previous.request_id if previous else None,
            "audio_enabled": False,
        }
        self.events.put({"type": "neutral", **result})
        return result

    def update_live(self, payload: dict[str, Any]) -> None:
        """Publish operator-owned playback and monitor telemetry to read clients."""
        allowed = {
            "source", "session", "position_ms", "duration_ms", "playing",
            "left_signal", "right_signal", "headphone_monitor",
        }
        clean = {key: payload.get(key) for key in allowed if key in payload}
        clean["updated_at"] = time.time()
        with self._lock:
            self._live.update(clean)

    def update_heart_tempo(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CommandError("Heart tempo must be a JSON object")
        values: dict[str, float] = {}
        for key, low, high in (("bpm", 30.0, 240.0), ("ratio", 0.8, 2.0),
                               ("confidence", 0.0, 100.0)):
            raw = payload.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise CommandError(f"heart_tempo.{key} must be numeric")
            number = float(raw)
            if not low <= number <= high:
                raise CommandError(f"heart_tempo.{key} must be between {low:g} and {high:g}")
            values[key] = number
        received_at = time.time()
        heart_tempo = {
            "available": values["confidence"] >= 70.0,
            "bpm": round(values["bpm"], 3),
            "ratio": round(values["ratio"], 3),
            "target_cpm": round(values["bpm"] * values["ratio"], 3),
            "confidence": round(values["confidence"], 1),
            "received_at": received_at,
        }
        with self._lock:
            self._heart_tempo = heart_tempo
        self.events.put({"type": "heart_tempo", **heart_tempo})
        return {"ok": True, "status": "heart_tempo_observed", **heart_tempo}

    def heart_tempo(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._heart_tempo)
        age = max(0.0, time.time() - float(result.get("received_at") or 0.0))
        result["age_seconds"] = round(age, 3)
        result["fresh"] = bool(result.get("available") and age <= 3.0)
        return result

    def command_heart_tempo(self, payload: Any) -> dict[str, Any]:
        """Request a bounded GUI-owned heart-tempo override."""
        if not isinstance(payload, dict):
            raise CommandError("Heart-tempo control must be an object")
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in {"off", "escalation", "double", "relief"}:
            raise CommandError("heart_tempo_control.mode must be off, escalation, double or relief")
        target = None if mode == "off" else str(payload.get("target") or "").strip().lower()
        if target not in {None, "left", "right"}:
            raise CommandError("heart_tempo_control.target must be left or right")
        heart = self.heart_tempo()
        if mode != "off" and not heart.get("fresh"):
            raise CommandError("Fresh, confident heart telemetry is required")
        ratio = None if mode == "off" else {"escalation": 1.0, "double": 2.0, "relief": 0.9}[mode]
        control = {
            "active": mode != "off", "target": target, "mode": mode,
            "ratio": ratio, "requested_at": time.time(),
        }
        with self._lock:
            self._heart_tempo_control = control
        result = {"ok": True, "status": "heart_tempo_control_requested", "control": dict(control)}
        self.events.put({"type": "heart_tempo_control", **result})
        return result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            last = asdict(self._last) if self._last else None
            live = json.loads(json.dumps(self._live))
            heart_tempo = dict(self._heart_tempo)
            heart_tempo_control = dict(self._heart_tempo_control)
        age = max(0.0, time.time() - float(heart_tempo.get("received_at") or 0.0))
        heart_tempo["age_seconds"] = round(age, 3)
        heart_tempo["fresh"] = bool(heart_tempo.get("available") and age <= 3.0)
        return {
            "ok": True,
            "mode": "offline_simulation",
            "audio_enabled": False,
            "physical_output_enabled": False,
            "live": live,
            "heart_tempo": heart_tempo,
            "heart_tempo_control": heart_tempo_control,
            "last_command": last,
            "active_preset": asdict(self.preset_store.active()),
        }


def _handler(state: DirectorState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GwendolynSignalLab/0.5"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin", "")
            if origin == HEART_TEMPO_ORIGIN:
                self.send_header("Access-Control-Allow-Origin", HEART_TEMPO_ORIGIN)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(encoded)

        def _body(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise CommandError("Invalid Content-Length") from exc
            if length <= 0 or length > 64 * 1024:
                raise CommandError("JSON body must be between 1 byte and 64 KiB")
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CommandError("Body must be valid UTF-8 JSON") from exc

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._send(200, {"ok": True, "service": "Gwendolyn Signal Lab Director Bridge", "version": "0.4", "mode": "offline_simulation", "audio_enabled": False})
            elif path == "/v1/state":
                self._send(200, state.snapshot())
            else:
                self._send(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            try:
                payload = self._body()
                path = urlparse(self.path).path
                if path == "/v1/director/command":
                    self._send(200, state.apply(payload))
                elif path == "/v1/director/neutral":
                    reason = payload.get("reason", "Local neutral") if isinstance(payload, dict) else "Local neutral"
                    self._send(200, state.neutral(reason))
                elif path == "/v1/director/heart-tempo":
                    self._send(200, state.command_heart_tempo(payload))
                elif path == "/v1/heart-tempo":
                    if self.headers.get("Origin", "") != HEART_TEMPO_ORIGIN:
                        self._send(403, {"ok": False, "error": "origin_not_allowed"})
                    else:
                        self._send(200, state.update_heart_tempo(payload))
                else:
                    self._send(404, {"ok": False, "error": "not_found"})
            except CommandError as exc:
                self._send(400, {"ok": False, "error": "invalid_command", "message": str(exc), "audio_enabled": False})
            except Exception:
                self._send(500, {"ok": False, "error": "internal_error", "audio_enabled": False})

        def do_OPTIONS(self) -> None:
            path = urlparse(self.path).path
            if path != "/v1/heart-tempo" or self.headers.get("Origin", "") != HEART_TEMPO_ORIGIN:
                self._send(403, {"ok": False, "error": "origin_not_allowed"})
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", HEART_TEMPO_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

    return Handler


class DirectorBridge:
    def __init__(self, host: str = BRIDGE_HOST, port: int = BRIDGE_PORT, preset_store: PresetStore | None = None) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Director Bridge may bind only to localhost")
        self.state = DirectorState(preset_store)
        self.server = ThreadingHTTPServer((host, port), _handler(self.state))
        self.thread = Thread(target=self.server.serve_forever, name="signal-lab-director", daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        return self.server.server_address

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
