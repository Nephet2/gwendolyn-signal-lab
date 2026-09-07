from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from threading import RLock
from typing import Callable


DEVICE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "audio_device.json"


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    hostapi: str = ""

    @property
    def label(self) -> str:
        suffix = f" [{self.hostapi}]" if self.hostapi else ""
        return f"{self.index}: {self.name}{suffix}"


def enumerate_output_devices() -> list[AudioDevice]:
    """Enumerate the exact PortAudio endpoints used by the monitor."""
    try:
        import sounddevice as sd
        devices = list(sd.query_devices())
        apis = list(sd.query_hostapis())
    except Exception:
        return []
    result: list[AudioDevice] = []
    for index, item in enumerate(devices):
        if int(item.get("max_output_channels", 0)) < 2:
            continue
        api_index = int(item.get("hostapi", -1))
        api_name = str(apis[api_index].get("name", "")) if 0 <= api_index < len(apis) else ""
        result.append(AudioDevice(index, str(item.get("name") or f"Output {index}"), api_name))
    return result


class AudioDeviceLatch:
    """Persist an exact index/name pair; never opens or writes to the device."""

    def __init__(
        self,
        path: str | Path = DEVICE_CONFIG_PATH,
        enumerator: Callable[[], list[AudioDevice]] = enumerate_output_devices,
    ) -> None:
        self.path = Path(path)
        self.enumerator = enumerator
        self._lock = RLock()
        self.devices: list[AudioDevice] = []
        self.selected: AudioDevice | None = None
        self._saved_index: int | None = None
        self._saved_name = ""
        self._saved_hostapi = ""
        self._load()
        self.refresh()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._saved_index = int(data["selected_index"])
            self._saved_name = str(data["selected_name"])
            self._saved_hostapi = str(data.get("selected_hostapi") or "")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._saved_index = None
            self._saved_name = ""
            self._saved_hostapi = ""

    def refresh(self) -> list[AudioDevice]:
        with self._lock:
            self.devices = list(self.enumerator())
            self.selected = next(
                (item for item in self.devices if item.index == self._saved_index and item.name == self._saved_name
                 and (not self._saved_hostapi or item.hostapi == self._saved_hostapi)),
                None,
            )
            return list(self.devices)

    def latch(self, index: int, name: str) -> AudioDevice:
        with self._lock:
            match = next((item for item in self.devices if item.index == int(index) and item.name == str(name)), None)
            if match is None:
                raise ValueError("That output device is no longer available; refresh and select it again")
            self.selected = match
            self._saved_index = match.index
            self._saved_name = match.name
            self._saved_hostapi = match.hostapi
            self.path.write_text(json.dumps({
                "version": 1,
                "selected_index": match.index,
                "selected_name": match.name,
                "selected_hostapi": match.hostapi,
                "audio_enabled": False,
            }, indent=2), encoding="utf-8")
            return match

    def state(self) -> dict:
        return {
            "selected": asdict(self.selected) if self.selected else None,
            "available": [asdict(item) for item in self.devices],
            "audio_enabled": False,
        }
