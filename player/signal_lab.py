from __future__ import annotations

import sys
import time
import tkinter as tk
import csv
import random
from queue import Empty
from pathlib import Path
from tkinter import messagebox, ttk

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player.session_model import SignalSession, discover_sessions, load_session, state_at
from player.audio_devices import AudioDeviceLatch
from player.audio_monitor import HeadphoneMonitor
from player.director_bridge import BRIDGE_HOST, BRIDGE_PORT, DirectorBridge
from player.perceptual import analyse_pair, semantic_state
from player.presets import NUMERIC_FIELDS, PresetStore, SignalPreset
from player.synth import render_session, render_states


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_ROOT = ROOT / "reference-sessions"
PREVIEW_ROOT = ROOT / "previews"
COMMISSIONING_ROOT = ROOT / "commissioning-results"

# Phase is preserved across heart-tempo changes, so a two-block interpolation
# gives immediate feedback without the former commissioning delay.
HEART_TEMPO_RESPONSE_SECONDS = 0.2


def clock(ms: int) -> str:
    seconds = max(0, ms // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def apply_heart_tempo(left: dict, right: dict, heart: dict, *, enabled: bool,
                      preset: SignalPreset, target: str = "both") -> tuple[dict, dict]:
    """Return headphone copies with only primary AM tempo overridden."""
    adjusted = (dict(left), dict(right))
    if not enabled or not heart.get("fresh"):
        return adjusted
    if target not in {"left", "right", "both"}:
        raise ValueError("heart tempo target must be left, right or both")
    target_hz = float(heart["target_cpm"]) / 60.0
    target_hz = min(preset.am_rate_max_hz, max(preset.am_rate_min_hz, target_hz))
    indexes = (0, 1) if target == "both" else ((0,) if target == "left" else (1,))
    for index in indexes:
        adjusted[index]["amFreq"] = round(target_hz, 4)
    return adjusted


class SignalLab(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Gwendolyn Signal Lab v0.7 — Heart Tempo Headphone Commissioning")
        self.geometry("1180x760")
        self.minsize(940, 620)
        self.session: SignalSession | None = None
        self.position_ms = 0
        self.playing = False
        self.director_display_active = False
        self.last_tick = time.monotonic()
        self.paths = discover_sessions(REFERENCE_ROOT)
        self.preset_store = PresetStore()
        self.device_latch = AudioDeviceLatch()
        self.headphone_monitor = HeadphoneMonitor()
        self.monitor_gain = tk.DoubleVar(value=10.0)
        self.heart_tempo_enabled = tk.BooleanVar(value=False)
        self.heart_tempo_target = tk.StringVar(value="right")
        self.heart_tempo_ratio_override: float | None = None
        self.commissioning_condition: tuple[str, float | None] | None = None
        self.commissioning_order: list[tuple[str, float | None]] = []
        self.commissioning_index = -1
        self.commissioning_rows: list[dict] = []
        self.commissioning_window: tk.Toplevel | None = None
        self.monitor_error_seen = ""
        self.control_vars: dict[str, tk.DoubleVar] = {}
        self.bridge: DirectorBridge | None = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._start_bridge()
        self.after(50, self._tick)
        self.after(100, self._poll_director)
        if self.paths:
            self.session_box.current(0)
            self._load_selected()

    def _build(self) -> None:
        style = ttk.Style(self)
        style.configure("Header.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))

        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="Gwendolyn Signal Lab v0.7", style="Header.TLabel").pack(side="left")
        self.monitor_banner = ttk.Label(top, text="10% HEADPHONE MONITOR — DISARMED", foreground="#a02b2b", style="Status.TLabel")
        self.monitor_banner.pack(side="right")

        chooser = ttk.Frame(self, padding=(12, 0, 12, 8))
        chooser.pack(fill="x")
        ttk.Label(chooser, text="Reference session:").pack(side="left")
        self.session_box = ttk.Combobox(chooser, state="readonly", width=68, values=[p.stem for p in self.paths])
        self.session_box.pack(side="left", padx=8, fill="x", expand=True)
        self.session_box.bind("<<ComboboxSelected>>", lambda _e: self._load_selected())
        ttk.Button(chooser, text="Reload", command=self._load_selected).pack(side="left")

        self.meta = ttk.Label(self, padding=(12, 0, 12, 8), text="")
        self.meta.pack(fill="x")

        preset_row = ttk.Frame(self, padding=(12, 0, 12, 8))
        preset_row.pack(fill="x")
        ttk.Label(preset_row, text="Signal preset:", style="Status.TLabel").pack(side="left")
        self.preset_box = ttk.Combobox(preset_row, state="readonly", width=30, values=self.preset_store.names())
        self.preset_box.set(self.preset_store.active().name)
        self.preset_box.pack(side="left", padx=6)
        self.preset_box.bind("<<ComboboxSelected>>", lambda _e: self._select_preset())
        ttk.Button(preset_row, text="Edit / Save As", command=self._edit_preset).pack(side="left")
        self.preset_summary = ttk.Label(preset_row, text="")
        self.preset_summary.pack(side="left", padx=10)
        self._update_preset_summary()

        device_row = ttk.Frame(self, padding=(12, 0, 12, 8))
        device_row.pack(fill="x")
        ttk.Label(device_row, text="Signal output device:", style="Status.TLabel").pack(side="left")
        self.device_box = ttk.Combobox(device_row, state="readonly", width=42)
        self.device_box.pack(side="left", padx=6)
        ttk.Button(device_row, text="Refresh", command=self._refresh_devices).pack(side="left")
        ttk.Button(device_row, text="Latch selection", command=self._latch_device).pack(side="left", padx=6)
        self.monitor_button = ttk.Button(device_row, text="Arm monitor", command=self._toggle_monitor)
        self.monitor_button.pack(side="left", padx=6)
        self.device_status = ttk.Label(device_row, text="", foreground="#8b3a3a")
        self.device_status.pack(side="left", padx=6)
        self._refresh_devices()

        monitor_row = ttk.Frame(self, padding=(12, 0, 12, 8))
        monitor_row.pack(fill="x")
        ttk.Label(monitor_row, text="Headphone output:", style="Status.TLabel").pack(side="left")
        ttk.Scale(monitor_row, from_=0, to=50, variable=self.monitor_gain,
                  command=self._monitor_gain_changed, length=260).pack(side="left", padx=8)
        self.monitor_gain_label = ttk.Label(monitor_row, text="10%", width=5)
        self.monitor_gain_label.pack(side="left")
        ttk.Label(monitor_row, text="0–50% safety-limited; changes are smoothed",
                  foreground="#666666").pack(side="left", padx=8)

        bridge_row = ttk.Frame(self, padding=(12, 0, 12, 8))
        bridge_row.pack(fill="x")
        ttk.Label(bridge_row, text="Director bridge:", style="Status.TLabel").pack(side="left")
        self.bridge_status = ttk.Label(bridge_row, text="STARTING", foreground="#885500")
        self.bridge_status.pack(side="left", padx=6)
        ttk.Button(bridge_row, text="Offline Neutral", command=self._director_neutral).pack(side="right")

        heart_row = ttk.Frame(self, padding=(12, 0, 12, 8))
        heart_row.pack(fill="x")
        ttk.Label(heart_row, text="Heart tempo:", style="Status.TLabel").pack(side="left")
        ttk.Checkbutton(
            heart_row, text="Apply to headphone primary movement",
            variable=self.heart_tempo_enabled, command=self._heart_tempo_changed,
        ).pack(side="left", padx=8)
        target_box = ttk.Combobox(
            heart_row, state="readonly", width=11,
            textvariable=self.heart_tempo_target,
            values=("right", "left", "both"),
        )
        target_box.pack(side="left")
        target_box.bind("<<ComboboxSelected>>", lambda _e: self._heart_tempo_changed())
        self.heart_tempo_status = ttk.Label(
            heart_row, text="DISABLED — awaiting Heart Phase Lab", foreground="#8b3a3a",
        )
        self.heart_tempo_status.pack(side="left", padx=6)
        ttk.Button(
            heart_row, text="Blinded A/B/C headphone test",
            command=self._open_commissioning,
        ).pack(side="right")

        self.canvas = tk.Canvas(self, background="#10151b", height=270, highlightthickness=0)
        self.canvas.pack(fill="x", padx=12)
        self.canvas.bind("<Configure>", lambda _e: self._draw_timeline())
        self.canvas.bind("<Button-1>", self._seek_canvas)

        transport = ttk.Frame(self, padding=12)
        transport.pack(fill="x")
        ttk.Button(transport, text="Play / Pause", command=self._toggle).pack(side="left")
        ttk.Button(transport, text="Stop", command=self._stop).pack(side="left", padx=6)
        self.position_label = ttk.Label(transport, text="00:00 / 00:00", width=18)
        self.position_label.pack(side="left", padx=8)
        self.scrub = ttk.Scale(transport, from_=0, to=1, command=self._scrubbed)
        self.scrub.pack(side="left", fill="x", expand=True)

        states = ttk.Frame(self, padding=(12, 0))
        states.pack(fill="x")
        self.left_state = self._state_panel(states, "LEFT — STAIRWAY E1 ↔ E4")
        self.right_state = self._state_panel(states, "RIGHT — MOANER TIP ↔ BASE")

        lower = ttk.Panedwindow(self, orient="horizontal")
        lower.pack(fill="both", expand=True, padx=12, pady=12)
        controls = ttk.LabelFrame(lower, text="Gwendolyn semantic command simulator", padding=10)
        log_frame = ttk.LabelFrame(lower, text="Command log", padding=8)
        lower.add(controls, weight=1)
        lower.add(log_frame, weight=2)

        specifications = [
            ("intensity", "Intensity", 0, 100, 45),
            ("texture", "Texture (rough → smooth)", 0, 100, 60),
            ("vibration_rate", "Vibration rate", 0, 100, 35),
            ("vibration_depth", "Vibration depth", 0, 100, 45),
            ("secondary_rate", "Secondary vibration rate", 0, 100, 30),
            ("secondary_depth", "Secondary vibration depth", 0, 100, 35),
            ("modulation", "FM movement", 0, 100, 20),
            ("transition", "Transition seconds", 1, 30, 10),
        ]
        for row, (key, label, low, high, initial) in enumerate(specifications):
            ttk.Label(controls, text=label).grid(row=row, column=0, sticky="w")
            var = tk.DoubleVar(value=initial)
            self.control_vars[key] = var
            ttk.Scale(controls, from_=low, to=high, variable=var).grid(row=row, column=1, sticky="ew", padx=8)
            value = ttk.Label(controls, width=5)
            value.grid(row=row, column=2)
            var.trace_add("write", lambda *_a, v=var, out=value: out.configure(text=f"{v.get():.0f}"))
            value.configure(text=f"{initial:.0f}")
        controls.columnconfigure(1, weight=1)
        ttk.Button(controls, text="Preview command for both lanes", command=self._preview_command).grid(row=len(specifications), column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(controls, text="Render semantic 10s WAV", command=self._render_semantic).grid(row=len(specifications)+1, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        ttk.Button(controls, text="Render current session 10s WAV", command=self._render_current).grid(row=len(specifications)+2, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        ttk.Label(controls, text="Files are rendered at 5% preview gain and never autoplay.", foreground="#8b3a3a").grid(row=len(specifications)+3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.log = tk.Text(log_frame, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        self._apply_preset_start_controls()

    def _state_panel(self, parent: ttk.Frame, title: str) -> ttk.Label:
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
        label = ttk.Label(frame, text="No active event", justify="left", font=("Consolas", 9))
        label.pack(anchor="w")
        return label

    def _refresh_devices(self) -> None:
        devices = self.device_latch.refresh()
        labels = [item.label for item in devices]
        self.device_box.configure(values=labels)
        selected = self.device_latch.selected
        if selected:
            self.device_box.set(selected.label)
            self.device_status.configure(text="LATCHED — AUDIO DISARMED", foreground="#176b32")
        else:
            self.device_box.set(labels[0] if labels else "")
            self.device_status.configure(
                text="Select and latch an output; audio remains disabled" if labels else "No Windows output devices found",
                foreground="#8b3a3a",
            )

    def _latch_device(self) -> None:
        label = self.device_box.get()
        match = next((item for item in self.device_latch.devices if item.label == label), None)
        if match is None:
            messagebox.showerror("Cannot latch device", "Refresh and select an available output device")
            return
        try:
            if self.headphone_monitor.armed:
                self.headphone_monitor.disarm()
                self.monitor_button.configure(text="Arm monitor")
            selected = self.device_latch.latch(match.index, match.name)
            self.device_status.configure(text="LATCHED — AUDIO DISARMED", foreground="#176b32")
            self._write_log(f"DEVICE latched {selected.label} | exact index/name match | AUDIO=DISABLED")
        except Exception as exc:
            messagebox.showerror("Cannot latch device", str(exc))

    def _toggle_monitor(self) -> None:
        if self.headphone_monitor.armed:
            self.headphone_monitor.disarm()
            self.monitor_button.configure(text="Arm monitor")
            self.monitor_banner.configure(text=f"{self.monitor_gain.get():.0f}% HEADPHONE MONITOR — DISARMED", foreground="#a02b2b")
            self.device_status.configure(text="LATCHED — MONITOR DISARMED", foreground="#8b3a3a")
            self._write_log("HEADPHONE MONITOR disarmed and playback stopped")
            self._publish_live_state()
            return
        if not self.device_latch.selected:
            messagebox.showerror("Cannot arm monitor", "Latch the intended headphone output first")
            return
        left, right = self._monitor_states()
        if not left or not right:
            messagebox.showerror("Cannot arm monitor", "No base JSON or Director state is available")
            return
        try:
            self.headphone_monitor.arm(
                self.device_latch.selected.index, self.device_latch.selected.name,
                left, right, gain=self.monitor_gain.get() / 100.0,
                start_paused=not self.playing,
            )
        except Exception as exc:
            messagebox.showerror("Cannot arm monitor", str(exc))
            return
        self.monitor_button.configure(text="Disarm / stop monitor")
        self.monitor_error_seen = ""
        state = "PLAYING" if self.playing else "PAUSED"
        self.monitor_banner.configure(text=f"{self.monitor_gain.get():.0f}% HEADPHONE MONITOR — {state}", foreground="#176b32")
        self.device_status.configure(text=f"LATCHED — MONITOR {state}", foreground="#176b32")
        self._write_log(f"HEADPHONE MONITOR armed at {self.monitor_gain.get():.0f}% on {self.device_latch.selected.label} | press Play to hear signal")
        self._publish_live_state(left, right)

    def _monitor_gain_changed(self, value: str) -> None:
        percent = min(50.0, max(0.0, float(value)))
        if hasattr(self, "monitor_gain_label"):
            self.monitor_gain_label.configure(text=f"{percent:.0f}%")
        self.headphone_monitor.set_gain(percent / 100.0)
        if self.headphone_monitor.armed:
            state = "PLAYING" if self.playing else "PAUSED"
            self.monitor_banner.configure(text=f"{percent:.0f}% HEADPHONE MONITOR — {state}")
        self._publish_live_state()

    def _publish_live_state(self, left: dict | None = None, right: dict | None = None) -> None:
        if not self.bridge:
            return
        if left is None or right is None:
            left, right = self._monitor_states()
        selected = self.device_latch.selected
        monitor = self.headphone_monitor.status()
        monitor["device"] = selected.label if selected else None
        source = "director_command" if self.director_display_active else "reference_session"
        self.bridge.state.update_live({
            "source": source if left and right else "none",
            "session": self.session.path.stem if self.session else None,
            "position_ms": int(self.position_ms),
            "duration_ms": int(self.session.duration_ms) if self.session else 0,
            "playing": bool(self.playing),
            "left_signal": dict(left) if left else None,
            "right_signal": dict(right) if right else None,
            "headphone_monitor": monitor,
        })

    def _monitor_states(self) -> tuple[dict, dict]:
        if self.director_display_active and self.bridge:
            snapshot = self.bridge.state.snapshot()
            command = snapshot.get("last_command") or {}
            if command.get("left_signal") and command.get("right_signal"):
                return self._heart_tempo_states(
                    dict(command["left_signal"]), dict(command["right_signal"]),
                    snapshot.get("heart_tempo") or {},
                )
        if self.session:
            left = state_at(self.session.left, self.position_ms)
            right = state_at(self.session.right, self.position_ms)
            if left and right:
                return self._heart_tempo_states(dict(left), dict(right))
        return {}, {}

    def _heart_tempo_states(self, left: dict, right: dict,
                            observed: dict | None = None) -> tuple[dict, dict]:
        """Override only headphone primary AM rate when explicitly enabled."""
        if not self.bridge:
            return dict(left), dict(right)
        heart = dict(observed or self.bridge.state.heart_tempo())
        if self.commissioning_condition is not None:
            _name, ratio = self.commissioning_condition
            if ratio is None:
                return dict(left), dict(right)
            if heart.get("fresh"):
                heart["ratio"] = ratio
                heart["target_cpm"] = float(heart["bpm"]) * ratio
            return apply_heart_tempo(
                left, right, heart, enabled=True, preset=self.preset_store.active(),
                target="right",
            )
        if self.heart_tempo_ratio_override is not None and heart.get("fresh"):
            heart["ratio"] = self.heart_tempo_ratio_override
            heart["target_cpm"] = float(heart["bpm"]) * self.heart_tempo_ratio_override
        return apply_heart_tempo(
            left, right, heart,
            enabled=bool(self.heart_tempo_enabled.get()), preset=self.preset_store.active(),
            target=self.heart_tempo_target.get(),
        )

    def _open_commissioning(self) -> None:
        if self.commissioning_window and self.commissioning_window.winfo_exists():
            self.commissioning_window.lift()
            return
        window = tk.Toplevel(self)
        self.commissioning_window = window
        window.title("Heart-tempo blinded headphone test")
        window.geometry("560x430")
        window.transient(self)
        window.protocol("WM_DELETE_WINDOW", self._close_commissioning)

        ttk.Label(window, text="Signal Lab blinded A/B/C test", style="Header.TLabel").pack(anchor="w", padx=16, pady=(16, 4))
        ttk.Label(
            window,
            text="Three randomized conditions: authored tempo, right-only heart 1:1 and right-only 90% relief.\n"
                 "The left lane remains authored throughout.",
            justify="left",
        ).pack(anchor="w", padx=16)
        self.trial_status = ttk.Label(window, text="Ready — connect fresh heart telemetry and arm the headphone monitor.", wraplength=520)
        self.trial_status.pack(fill="x", padx=16, pady=12)

        ratings = ttk.LabelFrame(window, text="Rate the current hidden condition (0–10)", padding=10)
        ratings.pack(fill="x", padx=16, pady=6)
        self.trial_ratings: dict[str, tk.IntVar] = {}
        for row, key in enumerate(("movement", "smoothness", "synchrony", "comfort")):
            ttk.Label(ratings, text=key.title(), width=14).grid(row=row, column=0, sticky="w")
            var = tk.IntVar(value=5)
            self.trial_ratings[key] = var
            ttk.Scale(ratings, from_=0, to=10, variable=var, length=300).grid(row=row, column=1, sticky="ew", padx=8)
            value = ttk.Label(ratings, text="5", width=3)
            value.grid(row=row, column=2)
            var.trace_add("write", lambda *_a, v=var, out=value: out.configure(text=str(round(v.get()))))
        ratings.columnconfigure(1, weight=1)

        buttons = ttk.Frame(window, padding=16)
        buttons.pack(fill="x")
        self.trial_start_button = ttk.Button(buttons, text="Start blinded set", command=self._start_commissioning)
        self.trial_start_button.pack(side="left")
        self.trial_record_button = ttk.Button(buttons, text="Record rating and continue", command=self._record_commissioning, state="disabled")
        self.trial_record_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Cancel", command=self._close_commissioning).pack(side="right")

    def _start_commissioning(self) -> None:
        if not self.bridge or not self.bridge.state.heart_tempo().get("fresh"):
            messagebox.showerror("Cannot start test", "Fresh Heart Phase Lab telemetry is required.")
            return
        if not self.headphone_monitor.armed or not self.playing:
            messagebox.showerror("Cannot start test", "Arm the headphone monitor and press Play first.")
            return
        self.heart_tempo_enabled.set(False)
        self.commissioning_order = [
            ("Authored tempo", None),
            ("Right-only heart tempo 1:1", 1.0),
            ("Right-only relief tempo 90%", 0.9),
        ]
        random.SystemRandom().shuffle(self.commissioning_order)
        self.commissioning_rows = []
        self.commissioning_index = 0
        self.trial_start_button.configure(state="disabled")
        self.trial_record_button.configure(state="normal")
        self._apply_commissioning_condition()
        self._write_log("BLINDED HEART-TEMPO set started | 3 randomized headphone-only conditions")

    def _apply_commissioning_condition(self) -> None:
        self.commissioning_condition = self.commissioning_order[self.commissioning_index]
        for var in self.trial_ratings.values():
            var.set(5)
        self.trial_status.configure(
            text=f"Hidden condition {self.commissioning_index + 1} of 3 is active. Listen as long as needed, then rate it."
        )
        left, right = self._monitor_states()
        if left and right:
            self.headphone_monitor.update_states(left, right, HEART_TEMPO_RESPONSE_SECONDS)

    def _record_commissioning(self) -> None:
        if self.commissioning_condition is None:
            return
        heart = self.bridge.state.heart_tempo() if self.bridge else {}
        name, ratio = self.commissioning_condition
        self.commissioning_rows.append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "trial": self.commissioning_index + 1,
            "condition": name,
            "ratio": "authored" if ratio is None else ratio,
            "bpm": heart.get("bpm", ""),
            **{key: round(var.get()) for key, var in self.trial_ratings.items()},
        })
        self.commissioning_index += 1
        if self.commissioning_index < len(self.commissioning_order):
            self._apply_commissioning_condition()
            return
        self._finish_commissioning()

    def _finish_commissioning(self) -> None:
        self.commissioning_condition = None
        left, right = self._monitor_states()
        if left and right:
            self.headphone_monitor.update_states(left, right, HEART_TEMPO_RESPONSE_SECONDS)
        COMMISSIONING_ROOT.mkdir(exist_ok=True)
        destination = COMMISSIONING_ROOT / f"heart-tempo-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.commissioning_rows[0]))
            writer.writeheader()
            writer.writerows(self.commissioning_rows)
        reveal = "\n".join(
            f"{row['trial']}. {row['condition']} — movement {row['movement']}, smoothness {row['smoothness']}, "
            f"synchrony {row['synchrony']}, comfort {row['comfort']}"
            for row in self.commissioning_rows
        )
        self.trial_status.configure(text=f"Set complete and saved.\n\n{reveal}")
        self.trial_record_button.configure(state="disabled")
        self.trial_start_button.configure(text="Start another set", state="normal")
        self._write_log(f"BLINDED HEART-TEMPO set complete | results={destination}")

    def _close_commissioning(self) -> None:
        self.commissioning_condition = None
        self.commissioning_order = []
        self.commissioning_index = -1
        left, right = self._monitor_states()
        if self.headphone_monitor.armed and left and right:
            self.headphone_monitor.update_states(left, right, HEART_TEMPO_RESPONSE_SECONDS)
        if self.commissioning_window and self.commissioning_window.winfo_exists():
            self.commissioning_window.destroy()
        self.commissioning_window = None

    def _heart_tempo_changed(self) -> None:
        self.heart_tempo_ratio_override = None
        enabled = bool(self.heart_tempo_enabled.get())
        self._write_log(
            "HEART TEMPO headphone primary movement enabled"
            if enabled else "HEART TEMPO disabled; returning smoothly to authored primary movement"
        )
        left, right = self._monitor_states()
        if self.headphone_monitor.armed and left and right:
            self.headphone_monitor.update_states(
                left, right, transition_seconds=HEART_TEMPO_RESPONSE_SECONDS
            )
        self._update_heart_tempo_status()

    def _update_heart_tempo_status(self) -> None:
        if not self.bridge:
            return
        heart = self.bridge.state.heart_tempo()
        if not self.heart_tempo_enabled.get():
            if heart.get("fresh"):
                self.heart_tempo_status.configure(
                    text=f"READY {heart['bpm']:.1f} bpm · target {heart['target_cpm']:.1f} cpm — DISABLED",
                    foreground="#885500",
                )
            else:
                self.heart_tempo_status.configure(
                    text="DISABLED — awaiting fresh Heart Phase Lab telemetry", foreground="#8b3a3a",
                )
            return
        if heart.get("fresh"):
            target_hz = float(heart["target_cpm"]) / 60.0
            self.heart_tempo_status.configure(
                text=f"ACTIVE {heart['bpm']:.1f} bpm × {heart['ratio']:.2f} = {heart['target_cpm']:.1f} cpm ({target_hz:.3f} Hz)",
                foreground="#176b32",
            )
        else:
            self.heart_tempo_status.configure(
                text="ENABLED BUT STALE — authored primary movement retained", foreground="#a02b2b",
            )

    def _load_selected(self) -> None:
        index = self.session_box.current()
        if index < 0:
            return
        try:
            self.session = load_session(self.paths[index])
        except Exception as exc:
            messagebox.showerror("Cannot load session", str(exc))
            return
        self.playing = False
        self.director_display_active = False
        self.position_ms = 0
        self.scrub.configure(to=max(1, self.session.duration_ms))
        self.meta.configure(text=f"Driver: {self.session.driver_name or 'Unknown'}   •   {self.session.comments or self.session.path.stem}   •   {clock(self.session.duration_ms)}   •   Left {len(self.session.left)} events   •   Right {len(self.session.right)} events")
        self._draw_timeline()
        self._update_position()
        self._write_log(f"LOADED {self.session.path.name} | duration={clock(self.session.duration_ms)}")

    def _draw_timeline(self) -> None:
        c = self.canvas
        c.delete("all")
        width = max(c.winfo_width(), 100)
        duration = self.session.duration_ms if self.session else 1
        c.create_text(12, 18, anchor="w", fill="#dce6ef", text="LEFT — Stairway E1 ↔ E4", font=("Segoe UI", 10, "bold"))
        c.create_text(12, 145, anchor="w", fill="#dce6ef", text="RIGHT — Moaner tip ↔ base", font=("Segoe UI", 10, "bold"))
        for y in (38, 165):
            c.create_rectangle(12, y, width - 12, y + 88, outline="#384653", fill="#17212b")
        if not self.session:
            return
        for events, base_y, color in ((self.session.left, 126, "#57b6ff"), (self.session.right, 253, "#ff9b62")):
            points: list[float] = []
            for event in events:
                x = 12 + (width - 24) * event.time_ms / duration
                volume = float(event.message.get("volume", 0))
                y = base_y - min(100, max(0, volume)) * 0.78
                points.extend((x, y))
            if len(points) >= 4:
                c.create_line(*points, fill=color, width=2)
            for event in events:
                x = 12 + (width - 24) * event.time_ms / duration
                c.create_line(x, base_y - 4, x, base_y, fill=color)
        self._draw_playhead()

    def _draw_playhead(self) -> None:
        self.canvas.delete("playhead")
        if not self.session or not self.session.duration_ms:
            return
        width = max(self.canvas.winfo_width(), 100)
        x = 12 + (width - 24) * self.position_ms / self.session.duration_ms
        self.canvas.create_line(x, 32, x, 258, fill="#f4e04d", width=2, tags="playhead")

    def _seek_canvas(self, event: tk.Event) -> None:
        if not self.session:
            return
        fraction = min(1.0, max(0.0, (event.x - 12) / max(1, self.canvas.winfo_width() - 24)))
        self.position_ms = int(self.session.duration_ms * fraction)
        self._update_position()

    def _scrubbed(self, value: str) -> None:
        if self.session:
            self.position_ms = int(float(value))
            self._update_position(set_scale=False)

    def _toggle(self) -> None:
        if not self.session:
            return
        if self.position_ms >= self.session.duration_ms:
            self.position_ms = 0
        self.playing = not self.playing
        if self.playing:
            self.headphone_monitor.resume()
        else:
            self.headphone_monitor.pause()
        if self.headphone_monitor.armed:
            state = "PLAYING" if self.playing else "PAUSED"
            self.monitor_banner.configure(text=f"{self.monitor_gain.get():.0f}% HEADPHONE MONITOR — {state}")
            self.device_status.configure(text=f"LATCHED — MONITOR {state}", foreground="#176b32")
        self._publish_live_state()
        self.last_tick = time.monotonic()

    def _stop(self) -> None:
        self.playing = False
        self.headphone_monitor.pause()
        self.position_ms = 0
        self._update_position()
        self._write_log("STOP simulation reset to 00:00")

    def _tick(self) -> None:
        now = time.monotonic()
        if self.headphone_monitor.last_error and self.headphone_monitor.last_error != self.monitor_error_seen:
            self.monitor_error_seen = self.headphone_monitor.last_error
            self.monitor_button.configure(text="Arm monitor")
            self.monitor_banner.configure(text="HEADPHONE MONITOR — OUTPUT ERROR", foreground="#a02b2b")
            self.device_status.configure(text=self.monitor_error_seen, foreground="#a02b2b")
            self._write_log(f"HEADPHONE MONITOR output error: {self.monitor_error_seen}")
        if self.playing and self.session:
            self.position_ms += int((now - self.last_tick) * 1000)
            if self.position_ms >= self.session.duration_ms:
                self.position_ms = self.session.duration_ms
                self.playing = False
                self.headphone_monitor.pause()
            self._update_position()
        self._update_heart_tempo_status()
        self.last_tick = now
        self.after(50, self._tick)

    def _update_position(self, set_scale: bool = True) -> None:
        duration = self.session.duration_ms if self.session else 0
        self.position_label.configure(text=f"{clock(self.position_ms)} / {clock(duration)}")
        if set_scale:
            self.scrub.set(self.position_ms)
        self._draw_playhead()
        if not self.session:
            return
        if self.director_display_active:
            return
        left = state_at(self.session.left, self.position_ms)
        right = state_at(self.session.right, self.position_ms)
        if left and right:
            left, right = self._heart_tempo_states(dict(left), dict(right))
        self._show_state(self.left_state, left)
        self._show_state(self.right_state, right)
        if self.headphone_monitor.armed and left and right:
            self.headphone_monitor.update_states(left, right)
        self._publish_live_state(left, right)

    @staticmethod
    def _show_state(label: ttk.Label, state: dict | None) -> None:
        if not state:
            label.configure(text="Waiting for first event")
            return
        label.configure(text=(
            f"Volume      {state.get('volume', '—')}\n"
            f"Carrier     {state.get('freq', '—')} Hz\n"
            f"AM          {state.get('amType', '—')}  {state.get('amFreq', '—')} Hz  depth {state.get('amDepth', '—')}\n"
            f"Secondary   {state.get('secondaryAmType', '—')}  {state.get('secondaryAmFreq', '—')} Hz  depth {state.get('secondaryAmDepth', '—')}\n"
            f"FM          {state.get('fmType', '—')}  {state.get('fmFreq', '—')} Hz  depth {state.get('fmDepth', '—')}\n"
            f"Ramp        target {state.get('rampTarget', '—')}  rate {state.get('rampRate', '—')}"
        ))

    def _preview_command(self) -> None:
        intensity = self.control_vars["intensity"].get()
        texture = self.control_vars["texture"].get()
        vibration_rate = self.control_vars["vibration_rate"].get()
        vibration_depth = self.control_vars["vibration_depth"].get()
        secondary_rate = self.control_vars["secondary_rate"].get()
        secondary_depth = self.control_vars["secondary_depth"].get()
        modulation = self.control_vars["modulation"].get()
        transition = self.control_vars["transition"].get()
        preset = self.preset_store.active()
        state = semantic_state(intensity=intensity, texture=texture, vibration_rate=vibration_rate, vibration_depth=vibration_depth,
                               secondary_rate=secondary_rate, secondary_depth=secondary_depth, modulation=modulation, preset=preset)
        reading = analyse_pair(state, state)
        command = (
            f"PREVIEW semantic → signal | both lanes | volume={intensity:.0f} | "
            f"carrier={state['freq']}Hz | AM=sine {state['amFreq']}Hz depth={vibration_depth:.0f}% | "
            f"2AM=sine {state['secondaryAmFreq']}Hz depth={state['secondaryAmDepth']}% | FM depth={state['fmDepth']}Hz | "
            f"transition={transition:.0f}s | preset={preset.name} | texture={reading.texture} | energy_proxy={reading.energy:.4f} | AUDIO=DISABLED"
        )
        self._write_log(command)

    def _semantic_pair(self) -> tuple[dict, dict]:
        state = semantic_state(
            intensity=self.control_vars["intensity"].get(),
            texture=self.control_vars["texture"].get(),
            vibration_rate=self.control_vars["vibration_rate"].get(),
            vibration_depth=self.control_vars["vibration_depth"].get(),
            secondary_rate=self.control_vars["secondary_rate"].get(),
            secondary_depth=self.control_vars["secondary_depth"].get(),
            modulation=self.control_vars["modulation"].get(),
            preset=self.preset_store.active(),
        )
        return dict(state), dict(state)

    def _save_preview(self, payload: bytes, label: str) -> None:
        PREVIEW_ROOT.mkdir(exist_ok=True)
        filename = f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.wav"
        destination = PREVIEW_ROOT / filename
        destination.write_bytes(payload)
        self._write_log(f"RENDERED {destination} | stereo PCM | preview gain=5% | NOT PLAYED")
        messagebox.showinfo("WAV preview rendered", f"Saved without playing:\n\n{destination}")

    def _render_semantic(self) -> None:
        try:
            left, right = self._semantic_pair()
            self._save_preview(render_states(left, right, duration_s=10.0), "semantic-preview")
        except Exception as exc:
            messagebox.showerror("Cannot render preview", str(exc))

    def _render_current(self) -> None:
        if not self.session:
            return
        try:
            payload = render_session(self.session, offset_s=self.position_ms / 1000.0, duration_s=10.0)
            self._save_preview(payload, "session-preview")
        except Exception as exc:
            messagebox.showerror("Cannot render preview", str(exc))

    def _write_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start_bridge(self) -> None:
        try:
            self.bridge = DirectorBridge(BRIDGE_HOST, BRIDGE_PORT, self.preset_store)
            self.bridge.start()
            self._publish_live_state()
            host, port = self.bridge.address
            self.bridge_status.configure(text=f"READY  http://{host}:{port}  •  OFFLINE ONLY", foreground="#176b32")
            self._write_log(f"DIRECTOR BRIDGE ready http://{host}:{port} | AUDIO=DISABLED")
        except OSError as exc:
            self.bridge_status.configure(text="UNAVAILABLE — PORT IN USE", foreground="#a02b2b")
            self._write_log(f"DIRECTOR BRIDGE unavailable: {exc}")

    def _poll_director(self) -> None:
        if self.bridge:
            while True:
                try:
                    event = self.bridge.state.events.get_nowait()
                except Empty:
                    break
                if event.get("type") == "command":
                    command = event["command"]
                    self.playing = False
                    self.director_display_active = True
                    left, right = self._heart_tempo_states(
                        command["left_signal"], command["right_signal"]
                    )
                    reading = command["perceptual"]
                    self._show_state(self.left_state, left)
                    self._show_state(self.right_state, right)
                    self._write_log(
                        f"DIRECTOR APPLIED OFFLINE id={command['request_id']} transition={command['transition_seconds']}s "
                        f"L={command['left_semantic']} R={command['right_semantic']} "
                        f"preset={command['preset_name']} character={reading['character']} energy_proxy={reading['energy']:.4f} "
                        f"| MONITOR={'ARMED' if self.headphone_monitor.armed else 'DISARMED'}"
                    )
                    if self.headphone_monitor.armed and self.device_latch.selected:
                        self.headphone_monitor.update_states(left, right, command["transition_seconds"])
                        self._write_log(
                            f"HEADPHONE MONITOR staged transition on {self.device_latch.selected.label} "
                            f"| one perceptual parameter at a time | intensity last"
                        )
                    self._publish_live_state(left, right)
                elif event.get("type") == "heart_tempo":
                    self._update_heart_tempo_status()
                    left, right = self._monitor_states()
                    if self.headphone_monitor.armed and left and right:
                        self.headphone_monitor.update_states(
                            left, right, transition_seconds=HEART_TEMPO_RESPONSE_SECONDS
                        )
                elif event.get("type") == "heart_tempo_control":
                    control = event.get("control") or {}
                    active = bool(control.get("active"))
                    if active:
                        self.heart_tempo_target.set(str(control["target"]))
                        self.heart_tempo_ratio_override = float(control["ratio"])
                        self.heart_tempo_enabled.set(True)
                    else:
                        self.heart_tempo_enabled.set(False)
                        self.heart_tempo_ratio_override = None
                    left, right = self._monitor_states()
                    if self.headphone_monitor.armed and left and right:
                        self.headphone_monitor.update_states(
                            left, right, transition_seconds=HEART_TEMPO_RESPONSE_SECONDS
                        )
                    self._publish_live_state(left, right)
                    self._update_heart_tempo_status()
                    self._write_log(
                        f"HEART TEMPO CONTROL {control.get('mode')} target={control.get('target')} "
                        f"ratio={control.get('ratio')} | verified by live readback"
                    )
                elif event.get("type") == "neutral":
                    self.playing = False
                    self.headphone_monitor.stop()
                    self.director_display_active = True
                    self.left_state.configure(text="Offline neutral — no applied Director state")
                    self.right_state.configure(text="Offline neutral — no applied Director state")
                    self._write_log(f"DIRECTOR NEUTRAL reason={event.get('reason')} | AUDIO=DISABLED")
                    self._publish_live_state({}, {})
        self.after(100, self._poll_director)

    def _director_neutral(self) -> None:
        if self.bridge:
            self.bridge.state.neutral("Signal Lab Offline Neutral button")

    def _update_preset_summary(self) -> None:
        preset = self.preset_store.active()
        self.preset_summary.configure(text=(
            f"{preset.frequency_min_hz:g}–{preset.frequency_max_hz:g} Hz  •  "
            f"volume {preset.volume_min:g}–{preset.volume_max:g}  •  "
            f"AM {preset.am_depth_min_percent:g}–{preset.am_depth_max_percent:g}%  •  "
            f"2AM {preset.secondary_am_depth_min_percent:g}–{preset.secondary_am_depth_max_percent:g}%"
        ))
        if "transition" in self.control_vars:
            self.control_vars["transition"].set(min(preset.transition_max_seconds, max(preset.transition_min_seconds, self.control_vars["transition"].get())))

    def _apply_preset_start_controls(self) -> None:
        if not self.control_vars:
            return
        preset = self.preset_store.active()
        def position(start: float, low: float, high: float) -> float:
            return 0.0 if high <= low else 100.0 * (start - low) / (high - low)
        self.control_vars["intensity"].set(position(preset.volume_start, preset.volume_min, preset.volume_max))
        self.control_vars["texture"].set(position(preset.frequency_start_hz, preset.frequency_min_hz, preset.frequency_max_hz))
        self.control_vars["transition"].set(preset.transition_min_seconds)

    def _select_preset(self) -> None:
        try:
            preset = self.preset_store.select(self.preset_box.get())
            self.director_display_active = True
            self.playing = False
            if self.bridge:
                self.bridge.state.neutral(f"Preset changed to {preset.name}")
            self._apply_preset_start_controls()
            self._update_preset_summary()
            self._write_log(f"PRESET selected {preset.name} | offline state neutralised | AUDIO=DISABLED")
        except Exception as exc:
            messagebox.showerror("Cannot select preset", str(exc))

    def _edit_preset(self) -> None:
        current = self.preset_store.active()
        window = tk.Toplevel(self)
        window.title("Signal preset — operator-owned envelope")
        window.transient(self)
        window.grab_set()
        values: dict[str, tk.StringVar] = {"name": tk.StringVar(value=current.name)}
        ttk.Label(window, text="Save with a new name to create another preset.", padding=10).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(window, text="Name").grid(row=1, column=0, sticky="w", padx=10, pady=2)
        ttk.Entry(window, textvariable=values["name"], width=24).grid(row=1, column=1, sticky="ew", padx=10, pady=2)
        for row, key in enumerate(NUMERIC_FIELDS, start=2):
            values[key] = tk.StringVar(value=f"{getattr(current, key):g}")
            ttk.Label(window, text=key.replace("_", " ").title()).grid(row=row, column=0, sticky="w", padx=10, pady=2)
            ttk.Entry(window, textvariable=values[key], width=16).grid(row=row, column=1, sticky="ew", padx=10, pady=2)

        def save() -> None:
            try:
                preset = SignalPreset(name=values["name"].get().strip(), **{key: float(values[key].get()) for key in NUMERIC_FIELDS})
                preset = self.preset_store.put(preset)
                self.preset_box.configure(values=self.preset_store.names())
                self.preset_box.set(preset.name)
                self._apply_preset_start_controls()
                self._update_preset_summary()
                if self.bridge:
                    self.bridge.state.neutral(f"Preset saved and selected: {preset.name}")
                self._write_log(f"PRESET saved {preset.name} | offline state neutralised | AUDIO=DISABLED")
                window.destroy()
            except Exception as exc:
                messagebox.showerror("Invalid preset", str(exc), parent=window)

        buttons = ttk.Frame(window, padding=10)
        buttons.grid(row=2 + len(NUMERIC_FIELDS), column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="Save and select", command=save).pack(side="right", padx=6)
        window.columnconfigure(1, weight=1)

    def _close(self) -> None:
        self.playing = False
        self.headphone_monitor.disarm()
        if self.bridge:
            self.bridge.close()
            self.bridge = None
        self.destroy()


if __name__ == "__main__":
    SignalLab().mainloop()
