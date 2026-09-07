from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SignalEvent:
    time_ms: int
    duration_ms: int
    message: dict[str, Any]


@dataclass(frozen=True)
class SignalSession:
    path: Path
    driver_name: str
    comments: str
    left: tuple[SignalEvent, ...]
    right: tuple[SignalEvent, ...]
    bottle: tuple[SignalEvent, ...]

    @property
    def duration_ms(self) -> int:
        endings = [lane[-1].time_ms + lane[-1].duration_ms for lane in (self.left, self.right) if lane]
        return max(endings, default=0)


def _timeline(items: list[dict[str, Any]], lane: str) -> tuple[SignalEvent, ...]:
    elapsed = 0
    events: list[SignalEvent] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"{lane}[{index}] must be an object")
        delay = item.get("stamp")
        message = item.get("message")
        if not isinstance(delay, (int, float)) or delay < 0:
            raise ValueError(f"{lane}[{index}].stamp must be a non-negative number")
        if not isinstance(message, dict):
            raise ValueError(f"{lane}[{index}].message must be an object")
        duration = int(round(delay))
        events.append(SignalEvent(elapsed, duration, dict(message)))
        elapsed += duration
    return tuple(events)


def load_session(path: str | Path) -> SignalSession:
    path = Path(path)
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Session root must be an object")
    meta = data.get("meta", {})
    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")
    return SignalSession(
        path=path,
        driver_name=str(meta.get("driverName", "")),
        comments=str(meta.get("driverComments", "")),
        left=_timeline(data.get("left", []), "left"),
        right=_timeline(data.get("right", []), "right"),
        bottle=_timeline(data.get("bottle", []), "bottle"),
    )


def discover_sessions(root: str | Path) -> list[Path]:
    return sorted(Path(root).rglob("*.json"), key=lambda p: p.name.casefold())


def state_at(events: tuple[SignalEvent, ...], time_ms: int) -> dict[str, Any] | None:
    current = None
    for event in events:
        if event.time_ms > time_ms:
            break
        current = event.message
    return current
