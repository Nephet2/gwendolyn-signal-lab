"""Bounded, non-repeating reference selection with one-shot announcement tokens."""
from pathlib import Path
from random import SystemRandom
from threading import Lock
from time import monotonic
from uuid import uuid4

from .session_model import load_session


class ReferenceRotation:
    def __init__(self, root):
        self.root = Path(root)
        self.enabled = False
        self.lock = Lock()
        self.remaining = []
        self.last = None
        self.pending = None
        self.random = SystemRandom()

    def enable(self, enabled):
        with self.lock:
            self.enabled = enabled
            if not enabled:
                self.pending = None

    def reserve(self):
        with self.lock:
            if not self.enabled:
                return {"ok": False, "error": "rotation_disabled"}
            if not self.remaining:
                self.remaining = sorted(self.root.glob("*.json"))
                self.random.shuffle(self.remaining)
                if len(self.remaining) > 1 and self.remaining[-1] == self.last:
                    self.remaining[-1], self.remaining[0] = self.remaining[0], self.remaining[-1]
            while self.remaining:
                path = self.remaining.pop()
                try:
                    session = load_session(path)
                except (OSError, ValueError, TypeError, OverflowError):
                    continue
                if not session.left or not session.right or path == self.last:
                    continue
                self.last = path
                lane = self.random.choice(("left", "right"))
                token = uuid4().hex
                self.pending = token, monotonic()+120, lane, session
                return {"ok": True, "token": token, "lane": lane}
            return {"ok": False, "error": "no_different_reference"}

    def commit(self, token):
        with self.lock:
            if not self.enabled or self.pending is None:
                return None
            expected, expires, lane, session = self.pending
            if token != expected or monotonic() > expires:
                return None
            self.pending = None
            return lane, session
