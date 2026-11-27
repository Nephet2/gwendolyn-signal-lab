from __future__ import annotations

import os
import json
import math
import sys
import time
import tkinter as tk
import csv
import random
from dataclasses import replace
from queue import Empty
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player.session_model import SignalSession, discover_sessions, load_session, state_at
from player.audio_devices import AudioDeviceLatch
from player.audio_monitor import HeadphoneMonitor
from player.mini_analyser import MiniAnalyser
from player.motion_comparison import MotionComparison
from player.funscript_balance import BALANCE_CURVES, BalanceCurve, gains, matching_funscript
from player.crossover_editor import CrossoverEditor, preset as crossover_preset, validate_pair
from player.reference_rotation import ReferenceRotation
from player.spatial_curves_math import evaluate
from player.video_clock import VideoClock
from player.texture_modes import (FIELDS, MODES, LEVEL_BAND_PERCENT, Levels, banded_level,
                                  clamp_offset, intensity_offset, manual_state, texture_only)
from player.director_bridge import BRIDGE_HOST, BRIDGE_PORT, DirectorBridge
from player.perceptual import analyse_pair, semantic_state
from player.presets import NUMERIC_FIELDS, PresetStore, SignalPreset
from player.synth import render_session, render_states


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_SETTINGS = ROOT / "output_settings.json"


def load_output_settings(path=None):
    try:
        data = json.loads((path or OUTPUT_SETTINGS).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def saved_level(settings, key, default, maximum):
    value = settings.get(key)
    if type(value) not in (int, float) or not math.isfinite(value):
        return default
    return min(maximum, max(0.0, value))
REFERENCE_ROOT = Path(os.environ.get("SIGNAL_LAB_REFERENCE_ROOT", ROOT / "reference-sessions"))
PREVIEW_ROOT = ROOT / "previews"
COMMISSIONING_ROOT = ROOT / "commissioning-results"
REFERENCE_PARAMETERS = {
    "Carrier": ("freq", "Hz", None),
    "AM rate": ("amFreq", "Hz", "amType"),
    "AM depth": ("amDepth", "%", "amType"),
    "Secondary AM rate": ("secondaryAmFreq", "Hz", "secondaryAmType"),
    "Secondary AM depth": ("secondaryAmDepth", "%", "secondaryAmType"),
    "FM rate": ("fmFreq", "Hz", "fmType"),
    "FM depth": ("fmDepth", "Hz", "fmType"),
    "Authored volume": ("volume", "%", None),
    "Authored ramp target": ("rampTarget", "%", None),
    "Authored ramp rate": ("rampRate", "%/s", None),
}
REFERENCE_COLORS = ("#70d3ff", "#ffc17a", "#f59ab5", "#a3e894", "#c3a6ff",
                    "#ffe47c", "#69e5d7", "#ff947d", "#b5c7ff", "#d5e2ed")
TEXTURE_SLIDER_FIELDS = {
    "texture": ("freq", "fmType", "fmDepth", "fmFreq"),
    "vibration_rate": ("amFreq",),
    "vibration_depth": ("amType", "amDepth"),
    "secondary_rate": ("secondaryAmFreq", "fmFreq"),
    "secondary_depth": ("secondaryAmType", "secondaryAmDepth"),
    "modulation": ("fmType", "fmDepth", "fmFreq"),
}


def reference_value(state, key):
    import math
    value = state.get(key) if state else None
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None

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
        self.title("Signal Lab")
        self.geometry("1110x650")
        self.minsize(940, 620)
        self.session: SignalSession | None = None
        self.position_ms = 0
        self.playing = False
        self.director_display_active = False
        if getattr(self, "bridge", None):
            self.bridge.state.modulation.stop()
        self.last_tick = time.monotonic()
        self.paths = discover_sessions(REFERENCE_ROOT)
        self.preset_store = PresetStore()
        self.device_latch = AudioDeviceLatch()
        self.headphone_monitor = HeadphoneMonitor()
        output = load_output_settings()
        self.output_settings_job = None
        self.close_request = os.environ.get("SIGNAL_LAB_CLOSE_FILE")
        self.balance_curve = None
        shape = output.get("crossover")
        self.balance_interpolation = tk.StringVar(value="Linear")
        self.motion_comparison_window = None
        self.balance_shape = tk.StringVar(value=shape if shape in (*BALANCE_CURVES, "Custom") else "Equal power")
        try:
            self.custom_crossover = validate_pair(output.get("custom_crossover"))
        except ValueError:
            self.custom_crossover = crossover_preset("Equal power")
        self.crossover_editor = None
        self.balance_auto_resource = None
        self.balance_notice = "No balance funscript"
        self.video_clock = None
        self.video_resource = None
        self.video_pending_resource = None
        self.texture_mode = tk.StringVar(value="Reference JSON")
        self.manual_pair = (manual_state(), manual_state())
        self.levels = Levels(left_trim=saved_level(output, "left_trim", 100, 100),
                             right_trim=saved_level(output, "right_trim", 100, 100))
        self.calibration = tk.BooleanVar(value=False)
        self.calibration_ended = False
        self.reference_receipt = None
        self.trim_save_status = "Restored" if "left_trim" in output and "right_trim" in output else "Default"
        self.director_texture = None
        self.director_texture_request_id = None
        self.reference_lanes = {}
        self.reference_rotation = ReferenceRotation(
            Path(os.environ.get("SIGNAL_LAB_REFERENCE_ROOT", REFERENCE_ROOT)))
        self.rotate_references = tk.BooleanVar(value=False)
        # Gwendolyn level band: off until the operator ticks it for this run.
        self.level_band = tk.BooleanVar(value=False)
        self.level_offsets = [0.0, 0.0]
        self.level_request_id = None
        # Intensity of the last applied texture, and the band anchor (its baseline intensity).
        self.last_intensity = None
        self.level_anchor = None
        # While held (the level overlay), commands do not move the level.
        self.level_hold = False
        self._pressure_fault_seen = False
        self.monitor_gain = tk.DoubleVar(value=saved_level(output, "monitor_gain", 10, 50))
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
        self.right_control_vars: dict[str, tk.DoubleVar] = {}
        self.texture_scales = []
        self.manual_slider_snapshot = None
        self.bridge: DirectorBridge | None = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bridge_status.configure(text="Standalone balance trial", foreground="#176b32")
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
        ttk.Label(top, text="Signal Lab", style="Header.TLabel").pack(side="left")
        self.monitor_banner = ttk.Label(top, text=f"Monitor {self.monitor_gain.get():.0f}% · DISARMED", foreground="#a02b2b", style="Status.TLabel")
        self.monitor_banner.pack(side="right")

        book = ttk.Notebook(self)
        book.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        session_page, trace_page, settings_page, log_frame = (ttk.Frame(book) for _ in range(4))
        for page, title in ((session_page, "Session"), (trace_page, "Reference traces"),
                            (settings_page, "Settings"), (log_frame, "Command log")):
            book.add(page, text=title)

        book.insert(1, MiniAnalyser(book, self.headphone_monitor), text="Analyser")

        chooser = ttk.Frame(session_page, padding=(12, 0, 12, 8))
        chooser.pack(fill="x")
        ttk.Label(chooser, text="Texture JSON").pack(side="left")
        self.session_box = ttk.Combobox(chooser, state="readonly", width=68, values=[p.stem for p in self.paths])
        self.session_box.pack(side="left", padx=8, fill="x", expand=True)
        self.session_box.bind("<<ComboboxSelected>>", lambda _e: self._load_selected())
        ttk.Button(chooser, text="Open…", command=self._open_json).pack(side="left", padx=(0, 6))
        ttk.Button(chooser, text="Reload JSON", command=self._load_selected).pack(side="left")

        self.meta = ttk.Label(trace_page, padding=(12, 0, 12, 8), text="")
        self.meta.pack(fill="x")

        texture_row = ttk.Frame(session_page, padding=(12, 0, 12, 4))
        texture_row.pack(fill="x")
        ttk.Label(texture_row, text="Texture source").pack(side="left")
        selector = ttk.Combobox(texture_row, textvariable=self.texture_mode, values=MODES,
                                width=17, state="readonly")
        selector.pack(side="left", padx=6)
        selector.bind("<<ComboboxSelected>>", self._change_texture_mode)
        ttk.Button(texture_row, text="Manual texture…", command=self._edit_texture).pack(side="left")
        self.texture_status = ttk.Label(texture_row, text="")
        self.texture_status.pack(side="left", padx=8)

        balance_row = ttk.Frame(session_page, padding=(12, 0, 12, 4))
        balance_row.pack(fill="x")
        ttk.Button(balance_row, text="Open balance script…", command=self._open_balance).pack(side="left")
        ttk.Button(balance_row, text="Clear script", command=self._clear_balance).pack(side="left", padx=6)
        crossover = ttk.Combobox(balance_row, textvariable=self.balance_shape, values=(*BALANCE_CURVES, "Custom"),
                                 state="readonly", width=16)
        crossover.pack(side="left")
        crossover.bind("<<ComboboxSelected>>", self._crossover_selected)
        ttk.Button(balance_row, text="Edit crossover…", command=self._edit_crossover).pack(side="left", padx=5)
        ttk.Label(balance_row, text="Motion").pack(side="left", padx=(8, 0))
        interpolation = ttk.Combobox(balance_row, textvariable=self.balance_interpolation,
                                    values=("Linear", "Gradient"), state="readonly", width=10)
        interpolation.pack(side="left", padx=5)
        interpolation.bind("<<ComboboxSelected>>", self._change_balance_interpolation)
        ttk.Button(balance_row, text="Compare motion", command=self._show_motion_comparison).pack(side="left")
        self.balance_label = ttk.Label(session_page, padding=(12, 2, 12, 2), wraplength=900)
        self.balance_label.pack(fill="x")
        balance_view_row = ttk.Frame(session_page, padding=(12, 0, 12, 3))
        balance_view_row.pack(fill="x")
        self.balance_view = tk.StringVar(value="Channel gains")
        view_box = ttk.Combobox(balance_view_row, textvariable=self.balance_view,
                                values=("Channel gains", "Funscript position"), width=18, state="readonly")
        view_box.pack(side="left")
        view_box.bind("<<ComboboxSelected>>", lambda _e: self._draw_balance())
        self.balance_legend = ttk.Label(balance_view_row, text="Green: Left (A)    Orange: Right (B)")
        self.balance_legend.pack(side="left", padx=8)
        self.balance_canvas = tk.Canvas(session_page, height=52, background="#111a21", highlightthickness=0)
        self.balance_canvas.pack(fill="x", padx=12, pady=(0, 6))
        self.balance_canvas.bind("<Configure>", lambda _e: self._draw_balance())

        sync = ttk.Frame(session_page, padding=(12, 0, 12, 6))
        sync.pack(fill="x")
        self.video_source = ttk.Combobox(sync, state="readonly", values=("MPC-HC", "HereSphere"), width=12)
        self.video_source.set("MPC-HC")
        self.video_source.pack(side="left")
        self.video_host = ttk.Entry(sync, width=16)
        self.video_host.insert(0, "127.0.0.1")
        self.video_host.pack(side="left", padx=6)
        self.video_port = ttk.Entry(sync, width=7)
        self.video_port.insert(0, "13579")
        self.video_port.pack(side="left")
        self.video_source.bind("<<ComboboxSelected>>", self._video_defaults)
        ttk.Button(sync, text="Follow video", command=self._follow_video).pack(side="left", padx=6)
        ttk.Button(sync, text="Disconnect", command=self._disconnect_video).pack(side="left")
        self.video_status = ttk.Label(sync, text="Internal clock")
        self.video_status.pack(side="left", padx=8)

        preset_row = ttk.Frame(settings_page, padding=(12, 0, 12, 8))
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

        device_row = ttk.Frame(session_page, padding=(12, 0, 12, 8))
        device_row.pack(fill="x")
        ttk.Label(device_row, text="Signal output", style="Status.TLabel").pack(side="left")
        self.device_box = ttk.Combobox(device_row, state="readonly", width=42)
        self.device_box.pack(side="left", padx=6)
        self.device_box.bind("<<ComboboxSelected>>", self._output_selection_changed)
        ttk.Button(device_row, text="Refresh", command=self._refresh_devices).pack(side="left")
        ttk.Button(device_row, text="Confirm device", command=self._latch_device).pack(side="left", padx=6)
        self.monitor_button = ttk.Button(device_row, text="Arm monitor", command=self._toggle_monitor)
        self.monitor_button.pack(side="left", padx=6)
        self.device_status = ttk.Label(device_row, text="", foreground="#8b3a3a")
        self.device_status.pack(side="left", padx=6)
        self._refresh_devices()

        monitor_row = ttk.Frame(session_page, padding=(12, 0, 12, 8))
        monitor_row.pack(fill="x")
        ttk.Label(monitor_row, text="Monitor level", style="Status.TLabel").pack(side="left")
        ttk.Scale(monitor_row, from_=0, to=50, variable=self.monitor_gain,
                  command=self._monitor_gain_changed, length=260).pack(side="left", padx=8)
        self.monitor_gain_label = ttk.Label(monitor_row, text=f"{self.monitor_gain.get():.0f}%", width=5)
        self.monitor_gain_label.pack(side="left")
        ttk.Label(monitor_row, text="0–50% safety-limited; changes are smoothed",
                  foreground="#666666").pack(side="left", padx=8)


        trim_row = ttk.Frame(session_page, padding=(12, 0, 12, 8))
        trim_row.pack(fill="x")
        ttk.Label(trim_row, text="Channel trims", style="Status.TLabel").pack(side="left")
        self.trim_labels = {}
        for lane in ("left", "right"):
            ttk.Label(trim_row, text={"left": "Left (A)", "right": "Right (B)"}[lane]).pack(side="left", padx=(8, 0))
            scale = ttk.Scale(trim_row, from_=0, to=100, length=155)
            scale.set(getattr(self.levels, f"{lane}_trim"))
            scale.pack(side="left", padx=5)
            label = ttk.Label(trim_row, text=f"{scale.get():.0f}%", width=5)
            label.pack(side="left")
            self.trim_labels[lane] = label
            scale.configure(command=lambda value, lane=lane: self._output_trim_changed(lane, value))
        self.calibration_button = ttk.Checkbutton(trim_row, text="Calibrate before session", variable=self.calibration,
                                                  command=self._calibration_changed)
        self.calibration_button.pack(side="left", padx=8)
        self.trim_status = ttk.Label(trim_row, text=self.trim_save_status)
        self.trim_status.pack(side="left")
        self.setup_status = ttk.Label(session_page, padding=(12, 1), style="Status.TLabel")
        self.setup_status.pack(fill="x")

        bridge_row = ttk.Frame(settings_page, padding=(12, 0, 12, 8))
        ttk.Label(bridge_row, text="Director bridge:", style="Status.TLabel").pack(side="left")
        self.bridge_status = ttk.Label(bridge_row, text="STARTING", foreground="#885500")
        self.bridge_status.pack(side="left", padx=6)
        ttk.Button(bridge_row, text="Pressure sensor…", command=lambda: self.headphone_monitor.pressure_control.show(self, self._pressure_stop)).pack(side="right", padx=6)
        ttk.Button(bridge_row, text="Reconnect Director", command=self._connect_director).pack(side="left", padx=5)
        ttk.Button(bridge_row, text="Offline Neutral", command=self._director_neutral).pack(side="right")

        heart_row = ttk.Frame(settings_page, padding=(12, 0, 12, 8))
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

        reference_row = ttk.Frame(trace_page, padding=(12, 0, 12, 4))
        reference_row.pack(fill="x")
        ttk.Label(reference_row, text="Reference JSON · each parameter has its own scale").grid(
            row=0, column=0, columnspan=5, sticky="w")
        self.reference_selected = {}
        for index, name in enumerate(REFERENCE_PARAMETERS):
            checked = tk.BooleanVar(value=name == "Carrier")
            self.reference_selected[name] = checked
            ttk.Checkbutton(reference_row, text=name, variable=checked, command=self._draw_timeline).grid(
                row=1+index//5, column=index%5, sticky="w", padx=(0, 12))
        self.reference_readout = ttk.Label(reference_row, justify="left", wraplength=1100)
        self.reference_readout.grid(row=3, column=0, columnspan=5, sticky="w")
        self.canvas = tk.Canvas(trace_page, background="#10151b", height=270, highlightthickness=0)
        self.canvas.pack(fill="x", padx=12)
        self.canvas.bind("<Configure>", lambda _e: self._draw_timeline())
        self.canvas.bind("<Button-1>", self._seek_canvas)

        transport = ttk.Frame(session_page, padding=12)
        transport.pack(fill="x")
        self.play_button = ttk.Button(transport, text="Play", command=self._toggle)
        self.play_button.pack(side="left")
        ttk.Button(transport, text="Stop", command=self._stop).pack(side="left", padx=6)
        self.position_label = ttk.Label(transport, text="00:00 / 00:00", width=18)
        self.position_label.pack(side="left", padx=8)
        self.scrub = ttk.Scale(transport, from_=0, to=1, command=self._scrubbed)
        self.scrub.pack(side="left", fill="x", expand=True)

        states = ttk.Frame(session_page, padding=(12, 0))
        states.pack(fill="x")
        self.states_frame = states
        self.left_state = self._state_panel(states, "LEFT (A)")
        self.right_state = self._state_panel(states, "RIGHT (B)")

        controls = ttk.LabelFrame(settings_page, text="Manual texture and preview", padding=8)
        controls.pack(fill="x", padx=12, pady=5)

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
        self.link_texture = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Link channels", variable=self.link_texture).grid(row=0, column=0, sticky="w")
        ttk.Label(controls, text="Left (A)").grid(row=0, column=1)
        ttk.Label(controls, text="Right (B)").grid(row=0, column=2)
        for row, (key, label, low, high, initial) in enumerate(specifications, 1):
            if key == "intensity":
                label = "Preview intensity"
            ttk.Label(controls, text=label).grid(row=row, column=0, sticky="w")
            is_texture = key in TEXTURE_SLIDER_FIELDS
            for lane, variables in enumerate((self.control_vars, self.right_control_vars)):
                if lane and not is_texture:
                    break
                cell = ttk.Frame(controls)
                cell.grid(row=row, column=lane+1, columnspan=1 if is_texture else 2, sticky="ew", padx=3)
                var = tk.DoubleVar(value=initial)
                variables[key] = var
                on_slide = (lambda _v, k=key, i=lane: self._texture_slider_changed(k, i)) if is_texture else None
                scale = ttk.Scale(cell, from_=low, to=high, variable=var, command=on_slide)
                scale.pack(side="left", fill="x", expand=True)
                if is_texture:
                    self.texture_scales.append(scale)
                value = ttk.Label(cell, width=4, text=f"{initial:.0f}")
                value.pack(side="right")
                var.trace_add("write", lambda *_a, v=var, out=value: out.configure(text=f"{v.get():.0f}"))
        controls.columnconfigure(1, weight=1, uniform="lanes")
        controls.columnconfigure(2, weight=1, uniform="lanes")
        ttk.Button(controls, text="Preview left and right settings", command=self._preview_command).grid(row=len(specifications)+1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(controls, text="Render semantic 10s WAV", command=self._render_semantic).grid(row=len(specifications)+2, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        ttk.Button(controls, text="Render original JSON 10s WAV", command=self._render_current).grid(row=len(specifications)+3, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        ttk.Label(controls, text="Files are rendered at 5% preview gain and never autoplay.", foreground="#8b3a3a").grid(row=len(specifications)+4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.log = tk.Text(log_frame, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        device_row.pack_configure(before=chooser)
        monitor_row.pack_configure(after=device_row)
        trim_row.pack_configure(after=monitor_row)
        self.setup_status.pack_configure(after=trim_row)
        transport.pack_configure(after=self.setup_status)
        self._apply_preset_start_controls()
        self.manual_pair = self._semantic_pair()
        self._update_balance()

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
        if not selected and self.headphone_monitor.armed:
            self.headphone_monitor.disarm()
            self.playing = False
        if selected:
            self.device_box.set(selected.label)
            self.device_status.configure(text="LATCHED — AUDIO DISARMED", foreground="#176b32")
        else:
            self.device_box.set(labels[0] if labels else "")
            self.device_status.configure(
                text="Select and latch an output; audio remains disabled" if labels else "No Windows output devices found",
                foreground="#8b3a3a",
            )

    def _output_selection_changed(self, _event=None):
        selected = self.device_latch.selected
        if selected and self.device_box.get() != selected.label:
            self.headphone_monitor.disarm()
            self.playing = False
            self._publish_live_state()

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
            self.playing = False
            self.monitor_button.configure(text="Arm monitor")
            self.monitor_banner.configure(text=f"{self.monitor_gain.get():.0f}% HEADPHONE MONITOR — DISARMED", foreground="#a02b2b")
            self.device_status.configure(text="LATCHED — MONITOR DISARMED", foreground="#8b3a3a")
            self._write_log("HEADPHONE MONITOR disarmed and playback stopped")
            self._publish_live_state()
            return
        selected = self.device_latch.selected
        if not selected or self.device_box.get() != selected.label:
            messagebox.showerror("Cannot arm monitor", "Confirm the selected signal output first")
            return
        left, right = self._monitor_states()
        if not left or not right:
            if self.texture_mode.get() == "Gwendolyn" and self.bridge:
                silent = dict(manual_state(), active=False, volume=0, rampTarget=0, rampRate=0)
                left, right = dict(silent), dict(silent)
            else:
                messagebox.showerror("Cannot arm monitor", "Load a reference JSON or connect the Director first.")
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

    def monitor_state(self):
        if self.headphone_monitor.last_error and not self.headphone_monitor.armed:
            return "Output error"
        if not self.headphone_monitor.armed:
            return "Disarmed"
        if self._can_calibrate() and self.calibration.get():
            return "Calibration playing" if self.playing else "Calibration ready"
        if self._can_calibrate():
            return "Waiting for Director texture" if self.playing else "Armed · paused"
        return "Playing" if self.playing else "Armed · paused"

    def _refresh_monitor_status(self) -> None:
        self._refresh_setup_status()
        if self.headphone_monitor.last_error and not self.headphone_monitor.armed:
            state, detail, colour = "OUTPUT ERROR", self.headphone_monitor.last_error, "#a02b2b"
        elif not self.headphone_monitor.armed:
            state, detail, colour = "DISARMED", "Click Arm monitor to enable output", "#a02b2b"
        elif self._can_calibrate() and self.calibration.get():
            state, colour = ("CALIBRATION PLAYING" if self.playing else "CALIBRATION READY"), "#946520"
            detail = self.device_latch.selected.label
        elif self.texture_mode.get() == "Gwendolyn" and self.director_texture is None and not self.reference_lanes:
            detail = "Armed · tell Gwendolyn to start Conducted Session" if self.playing else "Armed · start playback first"
            state, colour = "WAITING FOR TEXTURE", "#946520"
        elif not self.playing:
            state, detail, colour = "PAUSED", "Armed · press Play or resume the video", "#946520"
        else:
            state, detail, colour = "PLAYING", "Monitor playing", "#176b32"
        self.monitor_banner.configure(text=f"HEADPHONE MONITOR — {state}", foreground=colour)
        self.device_status.configure(text=detail, foreground=colour)
        self.monitor_button.configure(text="Disarm / stop monitor" if self.headphone_monitor.armed else "Arm monitor")

    def _monitor_gain_changed(self, value: str) -> None:
        percent = min(50.0, max(0.0, float(value)))
        if hasattr(self, "monitor_gain_label"):
            self.monitor_gain_label.configure(text=f"{percent:.0f}%")
        self.headphone_monitor.set_gain(percent / 100.0)
        if self.headphone_monitor.armed:
            state = "PLAYING" if self.playing else "PAUSED"
            self.monitor_banner.configure(text=f"{percent:.0f}% HEADPHONE MONITOR — {state}")
        self._publish_live_state()
        self._schedule_output_save()

    def _output_trim_changed(self, lane: str, value: str) -> None:
        percent = min(100.0, max(0.0, float(value)))
        self.levels = replace(self.levels, **{f"{lane}_trim": percent})
        self.trim_labels[lane].configure(text=f"{percent:.0f}%")
        self._update_balance()
        self.trim_status.configure(text="Saving…")
        self._schedule_output_save()

    def _schedule_output_save(self):
        if self.output_settings_job is not None:
            self.after_cancel(self.output_settings_job)
        self.output_settings_job = self.after(300, self._save_output_settings)

    def _save_output_settings(self):
        self.output_settings_job = None
        data = dict(monitor_gain=self.monitor_gain.get(), left_trim=self.levels.left_trim,
                    right_trim=self.levels.right_trim, crossover=self.balance_shape.get(),
                    custom_crossover=self.custom_crossover)
        temporary = OUTPUT_SETTINGS.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temporary.replace(OUTPUT_SETTINGS)
            self.trim_status.configure(text="Saved")
        except OSError as error:
            self.trim_status.configure(text="Save failed")
            self._write_log(f"Could not save output settings: {error}")

    def _calibration_changed(self):
        if self.calibration.get() and not self._can_calibrate():
            self.calibration.set(False)
            return
        self.calibration_ended = not self.calibration.get()
        if self._can_calibrate():
            if self.calibration.get():
                self.texture_status.configure(text="Calibration selected · arm monitor and press Play")
            else:
                self.playing = False
                self.headphone_monitor.pause()
                self._connect_director()
        self._update_position()

    def _can_calibrate(self):
        return (self.texture_mode.get() == "Gwendolyn"
                and self.director_texture is None and not self.reference_lanes)

    def _refresh_setup_status(self):
        allowed = self._can_calibrate()
        calibrating = allowed and self.calibration.get()
        self.calibration_button.configure(state="normal" if allowed else "disabled")
        self.play_button.configure(
            text=("Video controls playback" if self.video_clock else "Pause" if self.playing else "Play"),
            state="disabled" if self.video_clock else "normal")
        selected = self.device_latch.selected
        confirmed = selected and self.device_box.get() == selected.label
        if not confirmed:
            step = "Select the signal output and click Confirm device"
        elif not self.headphone_monitor.armed:
            step = "Click Arm monitor"
        elif calibrating:
            step = "Calibration playing" if self.playing else (
                "Play in the video player to calibrate" if self.video_clock else "Click Play to start calibration")
        elif allowed and not self.playing:
            step = "Select Calibrate before session or start playback for the Director"
        elif allowed:
            step = "Ask Natasha to start the Conducted Session"
        else:
            step = ""
        if self.calibration_ended and not calibrating:
            if self.director_texture or self.reference_lanes:
                step = "Calibration ended · Director texture active"
            else:
                step = f"Calibration ended · waiting for Director. {step}"
        self.setup_status.configure(text=step)

    def _publish_live_state(self, left: dict | None = None, right: dict | None = None) -> None:
        if not self.bridge:
            return
        if left is None or right is None:
            left, right = self._monitor_states()
        selected = self.device_latch.selected
        monitor = self.headphone_monitor.status()
        monitor["device"] = selected.label if selected else None
        waiting = self.director_display_active and not (left and right)
        ready_for_texture = bool(waiting and self.playing and monitor.get("armed")
                                 and not monitor.get("paused") and monitor.get("audible"))
        if waiting:
            monitor["audible"] = False
        source = ("calibration" if self.calibration.get() and self._can_calibrate()
                  else "director_command" if self.director_display_active else "reference_session")
        self.bridge.state.update_live({
            "source": source if left and right else "none",
            "session": self.session.path.stem if self.session else None,
            "position_ms": int(self.position_ms),
            "duration_ms": int(self.session.duration_ms) if self.session else 0,
            "reference_playing": bool(self.playing),
            "ready_for_first_texture": ready_for_texture,
            "director_texture_request_id": self.director_texture_request_id,
            "reference_receipt": self.reference_receipt,
            "monitor_state": self.monitor_state(),
            "texture_only_level": self.preset_store.active().volume_start if self.director_display_active else None,
            "level_band": self._level_band_readback() if self.director_display_active else None,
            "playing": bool(
                left and right and monitor.get("armed")
                and not monitor.get("paused") and monitor.get("audible")
            ) if self.director_display_active else bool(self.playing),
            "left_signal": dict(left) if left else None,
            "right_signal": dict(right) if right else None,
            "headphone_monitor": monitor,
        })

    def _monitor_states(self) -> tuple[dict, dict]:
        mode = self.texture_mode.get()
        if mode == "Manual" or (self.calibration.get() and self._can_calibrate()):
            preset = self.preset_store.active()
            left, right = (dict(lane) for lane in self.manual_pair)
            for lane in (left, right):
                lane["freq"] = min(preset.frequency_max_hz, max(preset.frequency_min_hz, lane["freq"]))
                lane["fmDepth"] = min(lane["fmDepth"], lane["freq"]-preset.frequency_min_hz,
                                      preset.frequency_max_hz-lane["freq"])
        elif mode == "Gwendolyn":
            left, right = self.director_texture or ({}, {})
            if self.reference_lanes:
                pair = [left or self.manual_pair[0], right or self.manual_pair[1]]
                for lane, session in self.reference_lanes.items():
                    events = session.left if lane == 0 else session.right
                    pair[lane] = state_at(events, self.position_ms) or pair[lane]
                left, right = pair
        elif self.session:
            left = state_at(self.session.left, self.position_ms)
            right = state_at(self.session.right, self.position_ms)
        else:
            return {}, {}
        levels = self._lane_levels()
        return tuple(texture_only(state, level) for state, level in zip((left, right), levels))

    def _band_active(self) -> bool:
        return bool(self.texture_mode.get() == "Gwendolyn" and self.level_band.get())

    def _lane_levels(self) -> tuple[float, float]:
        preset = self.preset_store.active()
        if not self._band_active():
            return preset.volume_start, preset.volume_start
        return tuple(banded_level(preset.volume_start, offset, preset.volume_min, preset.volume_max)
                     for offset in self.level_offsets)

    def _level_band_readback(self) -> dict:
        preset = self.preset_store.active()
        low, high = (banded_level(preset.volume_start, sign * LEVEL_BAND_PERCENT, preset.volume_min, preset.volume_max)
                     for sign in (-1, 1))
        left, right = self._lane_levels()
        return {"enabled": self._band_active(), "percent": LEVEL_BAND_PERCENT, "baseline": preset.volume_start,
                "min": low, "max": high, "left_offset_percent": self.level_offsets[0],
                "right_offset_percent": self.level_offsets[1], "left_level": left, "right_level": right,
                "request_id": self.level_request_id,
                "left_anchor_intensity": (self.level_anchor or (None, None))[0],
                "right_anchor_intensity": (self.level_anchor or (None, None))[1],
                "hold": self.level_hold}

    def _reset_level_band(self) -> None:
        # Back at the baseline: whatever is playing now becomes the band's anchor.
        self.level_offsets = [0.0, 0.0]
        self.level_request_id = None
        self.level_anchor = self.last_intensity
        self.level_hold = False

    def _restage_levels(self, transition_seconds: float) -> None:
        left, right = self._monitor_states()
        if self.headphone_monitor.armed and left and right:
            self.headphone_monitor.update_states(left, right, transition_seconds=transition_seconds)
        self._show_state(self.left_state, left)
        self._show_state(self.right_state, right)
        self._publish_live_state(left, right)

    def _level_band_changed(self) -> None:
        self._reset_level_band()
        self._write_log(f"LEVEL BAND {'ON' if self.level_band.get() else 'OFF'} | baseline level")
        self._restage_levels(1.0)

    def _change_texture_mode(self, _event=None) -> None:
        self.calibration.set(False)
        self.calibration_ended = False
        self.reference_receipt = None
        self.playing = False
        self.headphone_monitor.pause()
        self.headphone_monitor.disarm()
        self.monitor_button.configure(text="Arm monitor")
        self.monitor_banner.configure(text="HEADPHONE MONITOR — DISARMED", foreground="#a02b2b")
        self.director_texture = None
        self.director_texture_request_id = None
        self.reference_lanes.clear()
        self.last_intensity = None
        self._reset_level_band()
        self.director_display_active = self.texture_mode.get() == "Gwendolyn"
        variables = (self.control_vars, self.right_control_vars)
        if self.director_display_active and self.manual_slider_snapshot is None:
            self.manual_slider_snapshot = [{key: lane[key].get() for key in TEXTURE_SLIDER_FIELDS}
                                           for lane in variables]
        elif not self.director_display_active and self.manual_slider_snapshot is not None:
            for index, lane in enumerate(self.manual_slider_snapshot):
                self._reflect_texture_sliders(index, lane)
            self.manual_slider_snapshot = None
        for scale in self.texture_scales:
            scale.state(["disabled" if self.director_display_active else "!disabled"])
        if self.director_display_active:
            self._connect_director()
        else:
            if self.bridge:
                self.bridge.close()
                self.bridge = None
            self.bridge_status.configure(text="Standalone balance trial", foreground="#176b32")
            self.texture_status.configure(text="")
        self._draw_timeline()
        self._update_position()
        self._update_rotation()

    def _update_rotation(self):
        self.reference_rotation.enable(self.texture_mode.get() == "Gwendolyn" and self.rotate_references.get())

    def _connect_director(self) -> None:
        if self.texture_mode.get() != "Gwendolyn":
            self.texture_status.configure(text="Select Gwendolyn to connect the Director")
            return
        if self.bridge is None:
            self._start_bridge()
        if self.bridge is None:
            status = "Port in use · close regular Signal Lab and retry"
        elif self.director_texture:
            status = "Director texture active"
        else:
            status = "Connected · waiting for first texture"
        self.texture_status.configure(text=status)

    def _texture_slider_changed(self, key, lane) -> None:
        variables = (self.control_vars, self.right_control_vars)
        linked = self.link_texture.get()
        if linked:
            variables[1-lane][key].set(variables[lane][key].get())
        if self.texture_mode.get() != "Manual":
            return
        pair = [dict(state) for state in self.manual_pair]
        for index in ((0, 1) if linked else (lane,)):
            state = self._semantic_state(index)
            pair[index].update({field: state[field] for field in TEXTURE_SLIDER_FIELDS[key]})
        self.manual_pair = tuple(pair)
        self._update_position()

    def _reflect_texture_sliders(self, lane, semantic) -> None:
        variables = self.control_vars if lane == 0 else self.right_control_vars
        for key in TEXTURE_SLIDER_FIELDS:
            value = reference_value(semantic, key)
            if value is not None and value != variables[key].get():
                variables[key].set(value)

    def _reflect_reference_lane(self, lane, state) -> None:
        if not state:
            return
        preset = self.preset_store.active()

        def invert(value, low, high):
            return min(100, max(0, 100*(value-low)/(high-low))) if value is not None and high > low else 0

        fields = {
            "texture": ("freq", preset.frequency_min_hz, preset.frequency_max_hz),
            "vibration_rate": ("amFreq", preset.am_rate_min_hz, preset.am_rate_max_hz),
            "vibration_depth": ("amDepth", preset.am_depth_min_percent, preset.am_depth_max_percent),
            "secondary_rate": ("secondaryAmFreq", preset.secondary_am_rate_min_hz, preset.secondary_am_rate_max_hz),
            "secondary_depth": ("secondaryAmDepth", preset.secondary_am_depth_min_percent, preset.secondary_am_depth_max_percent),
        }
        semantic = {key: invert(reference_value(state, field), low, high)
                    for key, (field, low, high) in fields.items()}
        depth, carrier = reference_value(state, "fmDepth"), reference_value(state, "freq")
        semantic["modulation"] = invert(100*depth/carrier, preset.fm_depth_min_percent,
                                        preset.fm_depth_max_percent) if depth and carrier else 0
        self._reflect_texture_sliders(lane, semantic)

    def _edit_texture(self) -> None:
        import math

        dialog = tk.Toplevel(self)
        dialog.title("Manual texture and shared levels")
        ttk.Label(dialog, text="Manual texture").grid(row=0, column=0, padx=8, pady=5, sticky="w")
        for column, name in enumerate(("Left", "Right"), 1):
            ttk.Label(dialog, text=name).grid(row=0, column=column, padx=8)
        entries = [{}, {}]
        for row, (label, key, _) in enumerate(FIELDS, 1):
            ttk.Label(dialog, text=label).grid(row=row, column=0, padx=8, sticky="w")
            for column, state in enumerate(self.manual_pair, 1):
                entry = ttk.Entry(dialog, width=9)
                entry.insert(0, str(state[key]))
                entry.grid(row=row, column=column, padx=6, pady=2)
                entries[column-1][key] = entry
        level_entries = {}
        level_fields = (("Shared ramp start (%)", "start"), ("Shared ramp end (%)", "end"),
                        ("Shared ramp duration (s)", "seconds"))
        for row, (label, key) in enumerate(level_fields, len(FIELDS)+1):
            ttk.Label(dialog, text=label).grid(row=row, column=0, padx=8, sticky="w")
            entry = ttk.Entry(dialog, width=9)
            entry.insert(0, str(getattr(self.levels, key)))
            entry.grid(row=row, column=1, padx=6, pady=2)
            level_entries[key] = entry

        def apply():
            try:
                pair = tuple(dict(manual_state(), **{key: float(entry.get()) for key, entry in lane.items()})
                             for lane in entries)
                levels = {key: float(entry.get()) for key, entry in level_entries.items()}
                preset = self.preset_store.active()
                for lane in pair:
                    if any(not math.isfinite(lane[key]) or lane[key] < 0 for _, key, _ in FIELDS):
                        raise ValueError("Texture values must be finite and nonnegative.")
                    if not preset.frequency_min_hz <= lane["freq"] <= preset.frequency_max_hz:
                        raise ValueError("Carrier must be inside the selected preset frequency range.")
                    if lane["fmDepth"] > min(lane["freq"]-preset.frequency_min_hz,
                                              preset.frequency_max_hz-lane["freq"]):
                        raise ValueError("FM must keep the carrier inside the preset frequency range.")
                    if max(lane["amDepth"], lane["secondaryAmDepth"]) > 100:
                        raise ValueError("AM depths must be 0–100%.")
                if any(not math.isfinite(value) or value < 0 or (key != "seconds" and value > 100)
                       for key, value in levels.items()):
                    raise ValueError("Ramp levels must be 0–100%, with a nonnegative duration.")
            except ValueError as error:
                messagebox.showerror("Check texture and levels", str(error), parent=dialog)
                return
            self.manual_pair = pair
            self.levels = replace(self.levels, **levels)
            self._update_position()
            dialog.destroy()

        ttk.Button(dialog, text="Apply", command=apply).grid(row=len(FIELDS)+len(level_fields)+1,
                                                             column=0, columnspan=3, pady=8)

    def _clamp_base_volume(self, state):
        state = dict(state)
        if not state or state.get("active") is False:
            return state
        preset = self.preset_store.active()
        for key in ("volume", "rampTarget"):
            if key in state:
                state[key] = min(preset.volume_max, max(preset.volume_min, float(state[key])))
        return state

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

    def _open_json(self) -> None:
        filename = filedialog.askopenfilename(
            parent=self, title="Open Signal Lab JSON session",
            initialdir=str(self.session.path.parent if self.session else REFERENCE_ROOT),
            filetypes=[("JSON sessions", "*.json"), ("All files", "*.*")],
        )
        if filename:
            self._load_path(Path(filename))

    def _load_selected(self) -> None:
        index = self.session_box.current()
        if 0 <= index < len(self.paths):
            self._load_path(self.paths[index])

    def _open_balance(self) -> None:
        filename = filedialog.askopenfilename(parent=self, title="Open stereo balance funscript",
                                              filetypes=[("Funscript", "*.funscript *.json"), ("All files", "*.*")])
        if not filename:
            return
        path = Path(filename)
        try:
            self.balance_curve = BalanceCurve.load(path)
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot load balance funscript", str(error), parent=self)
            return
        self.balance_name = path.name
        self.balance_auto_resource = None
        self._draw_balance()
        self._update_balance()
        self._update_position()

    def _clear_balance(self) -> None:
        self.balance_curve = None
        self.balance_auto_resource = None
        self.balance_notice = "No balance funscript"
        self._draw_balance()
        self._update_position()

    def _load_video_balance(self, resource) -> None:
        self.balance_curve = None
        self.balance_auto_resource = resource
        try:
            path = matching_funscript(resource)
            if path is not None:
                self.balance_curve = BalanceCurve.load(path)
                self.balance_name = path.name
            else:
                self.balance_notice = "No matching funscript · open one manually"
        except (OSError, ValueError, OverflowError) as error:
            self.balance_notice = f"Could not load matching funscript · {error}"
        self._draw_balance()
        self._update_balance()

    def _video_defaults(self, _event=None) -> None:
        self.video_port.delete(0, "end")
        self.video_port.insert(0, "13579" if self.video_source.get() == "MPC-HC" else "23554")

    def _follow_video(self) -> None:
        if self.texture_mode.get() == "Reference JSON" and not self.session:
            self.video_status.configure(text="Load a reference session first")
            return
        try:
            port = int(self.video_port.get())
            host = self.video_host.get().strip()
            if not host or not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.video_status.configure(text="Enter a host and a valid port")
            return
        self._disconnect_video()
        self.playing = False
        self.headphone_monitor.pause()
        self.video_clock = VideoClock(self.video_source.get(), host, port)
        self.video_status.configure(text="Waiting for video")

    def _disconnect_video(self) -> None:
        if self.video_clock is not None:
            self.video_clock.stop.set()
            self.video_clock = None
            self.video_resource = None
            self.video_pending_resource = None
            self.playing = False
            self.headphone_monitor.pause()
        self.video_status.configure(text="Internal clock")

    def _follow_tick(self, now) -> None:
        tick = self.video_clock.latest
        if tick is None or now-tick.received > 1.8:
            self.playing = False
            self.headphone_monitor.pause()
            self.video_status.configure(text="Waiting for video clock · paused")
            return
        if self.video_pending_resource is not None or (
            self.video_resource is not None and self.video_resource != tick.resource
        ):
            self.playing = False
            self.headphone_monitor.pause()
            if self.video_pending_resource != tick.resource:
                self.video_pending_resource = tick.resource
                self._load_video_balance(tick.resource)
            self.video_status.configure(text="Video changed · check your script and click Follow video")
            return
        if self.video_resource is None and (self.balance_curve is None or self.balance_auto_resource is not None):
            self._load_video_balance(tick.resource)
        self.video_resource = tick.resource
        elapsed = min(.25, max(0.0, now-tick.received)) if tick.playing else 0
        position = tick.position_ms + round(elapsed*tick.rate*1000)
        self.position_ms = position
        self.playing = tick.playing
        if not self.playing:
            self.headphone_monitor.pause()
        self.video_status.configure(text=f"{self.video_clock.player} · {'playing' if self.playing else 'paused'}")
        self._update_position()
        if self.playing:
            self.headphone_monitor.resume()

    def _balance_position(self, position_ms):
        return self.balance_curve.position(position_ms, self.balance_interpolation.get())

    def _change_balance_interpolation(self, _event=None):
        self._draw_balance()
        self._update_position()

    def _show_motion_comparison(self):
        window = self.motion_comparison_window
        if window is not None and window.winfo_exists():
            window.deiconify()
            window.lift()
            window.recenter()
        else:
            self.motion_comparison_window = MotionComparison(self)

    def _draw_balance(self) -> None:
        canvas = self.balance_canvas
        canvas.delete("all")
        window = self.motion_comparison_window
        if window is not None and window.winfo_exists():
            window.redraw()
        show_position = self.balance_view.get() == "Funscript position"
        self.balance_legend.configure(text="White: authored position (0 = left, 100 = right)" if show_position
                                      else "Green: Left (A)    Orange: Right (B) · before trim/ramp")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 80:
            return
        canvas.create_text(4, 7, anchor="w", text="100%", fill="#c2cdd6")
        canvas.create_text(4, height-7, anchor="w", text="0%", fill="#c2cdd6")
        canvas.create_line(36, height/2, width-8, height/2, fill="#40515e", dash=(3, 4))
        if self.balance_curve:
            duration = max(1, self.balance_curve.times[-1], self.session.duration_ms if self.session else 0)
            coords = [[]] if show_position else [[], []]
            for index in range(401):
                part = index/400
                pos = self._balance_position(round(duration*part))
                values = (pos/100,) if show_position else self._crossover_gains(pos)
                for lane, gain in enumerate(values):
                    coords[lane].extend((36+(width-44)*part, height-7-(height-14)*gain))
            colors = ("#e2edf4",) if show_position else ("#55dfbc", "#ffb572")
            for points, colour in zip(coords, colors):
                canvas.create_line(*points, fill=colour)
        self._update_balance()

    def _update_balance(self) -> None:
        canvas = self.balance_canvas
        canvas.delete("playhead")
        window = self.motion_comparison_window
        if window is not None and window.winfo_exists():
            window.update_playhead()
        mode = self.texture_mode.get()
        if self._can_calibrate() and self.calibration.get():
            source = "Calibration"
        elif mode == "Gwendolyn":
            source = "Director" if self.director_texture or self.reference_lanes else "Waiting for Director"
        elif mode == "Reference JSON":
            source = self.session.path.name if self.session else "Choose a texture JSON"
        else:
            source = "Manual"
        if self.session and mode != "Reference JSON":
            source += f" · {self.session.path.name} loaded (select Reference JSON to play)"
        script = f"{self.balance_notice} · {self.balance_shape.get()} inactive"
        if self.balance_curve:
            position = self._balance_position(self.position_ms)
            balance = self._crossover_gains(position)
            script = f"{self.balance_name} · {self.balance_shape.get()} · {self.balance_interpolation.get()}"
        else:
            balance = (1.0, 1.0)
        effective = self.levels.apply(balance, self.position_ms)
        self.headphone_monitor.set_balance(effective)
        self.balance_label.configure(
            text=f"Texture: {source}\nBalance: {script} · After trim/ramp L {effective[0]:.0%} / R {effective[1]:.0%}")
        if not self.balance_curve:
            return
        width = canvas.winfo_width()
        if width >= 80:
            duration = max(1, self.balance_curve.times[-1], self.session.duration_ms if self.session else 0)
            x = 36+(width-44)*min(1, self.position_ms/duration)
            canvas.create_line(x, 4, x, canvas.winfo_height()-4, fill="#55dfbc", tags="playhead")

    def _crossover_gains(self, position):
        shape = self.balance_shape.get()
        if shape == "Custom":
            return tuple(evaluate(curve, position/100) for curve in self.custom_crossover)
        return gains(position, shape)

    def _edit_crossover(self):
        if self.crossover_editor and self.crossover_editor.window.winfo_exists():
            self.crossover_editor.window.lift()
            return
        self.crossover_editor = CrossoverEditor(
            self, self.custom_crossover, self._activate_crossover,
            lambda: self._balance_position(self.position_ms) if self.balance_curve else None,
        )

    def _activate_crossover(self, curves):
        self.custom_crossover = curves
        self.balance_shape.set("Custom")
        self._crossover_selected()

    def _crossover_selected(self, _event=None):
        self._draw_balance()
        self._update_balance()
        self._schedule_output_save()

    def _load_path(self, path: Path) -> None:
        try:
            session = load_session(path)
            if not session.left or not session.right or session.duration_ms <= 0:
                raise ValueError("A session must contain left and right timelines with a positive duration.")
        except Exception as exc:
            messagebox.showerror("Cannot load session", f"{path}\n\n{exc}", parent=self)
            return
        # Validate first, then disarm before replacing the active session.
        self.headphone_monitor.disarm()
        self.monitor_button.configure(text="Arm monitor")
        self.monitor_banner.configure(
            text=f"{self.monitor_gain.get():.0f}% HEADPHONE MONITOR — DISARMED",
            foreground="#a02b2b",
        )
        self.session = session
        if path not in self.paths:
            self.paths.append(path)
        self.session_box.configure(values=[str(p) for p in self.paths])
        self.session_box.current(self.paths.index(path))
        self.playing = False
        self.director_display_active = self.texture_mode.get() == "Gwendolyn"
        if getattr(self, "bridge", None):
            self.bridge.state.modulation.stop()
        if self.video_clock is None:
            self.position_ms = 0
        self.meta.configure(text=f"Driver: {self.session.driver_name or 'Unknown'}   •   {self.session.comments or self.session.path.stem}   •   {clock(self.session.duration_ms)}   •   Left {len(self.session.left)} events   •   Right {len(self.session.right)} events")
        self._draw_timeline()
        self._draw_balance()
        self._update_position()
        self._write_log(f"LOADED {self.session.path.name} | duration={clock(self.session.duration_ms)}")
        self._publish_live_state()

    def _selected_reference_parameters(self):
        return [(name, *REFERENCE_PARAMETERS[name], color)
                for name, color in zip(REFERENCE_PARAMETERS, REFERENCE_COLORS)
                if self.reference_selected[name].get()]

    def _draw_timeline(self) -> None:
        c = self.canvas
        c.delete("all")
        width = max(c.winfo_width(), 100)
        duration = max(1, self._reference_duration())
        selected = self._selected_reference_parameters()
        for lane, y in (("LEFT (A)", 18), ("RIGHT (B)", 145)):
            c.create_text(12, y, anchor="w", fill="#dce6ef", text=lane, font=("Segoe UI", 10, "bold"))
        for y in (38, 165):
            c.create_rectangle(12, y, width-12, y+88, outline="#384653", fill="#17212b")
        if not self.session:
            self._draw_playhead()
            return
        chart_left = 52
        for name, key, unit, _, color in selected:
            lanes = self.session.left, self.session.right
            values = [value for events in lanes for event in events
                      if (value := reference_value(event.message, key)) is not None]
            if not values:
                continue
            low = min(values) if key == "freq" else min(0, min(values))
            high = max(values)
            if low == high:
                low, high = low-1 if low else 0, high+1
            for events, base_y in zip(lanes, (126, 253)):
                if len(selected) == 1:
                    for level, y in ((high, base_y-78), (low, base_y-4)):
                        c.create_text(chart_left-5, y, anchor="e", text=f"{level:g}", fill="#a8bac7")
                        c.create_line(chart_left, y, width-12, y, fill="#324456")
                for index, event in enumerate(events):
                    value = reference_value(event.message, key)
                    if value is None:
                        continue
                    end = events[index+1].time_ms if index+1 < len(events) else duration
                    x = chart_left + (width-chart_left-12) * event.time_ms / duration
                    end_x = chart_left + (width-chart_left-12) * end / duration
                    y = base_y-4-(value-low)/(high-low)*74
                    c.create_line(x, y, end_x, y, fill=color, width=2, tags="reference_trace")
                    if index+1 < len(events):
                        next_value = reference_value(events[index+1].message, key)
                        if next_value is not None and next_value != value:
                            next_y = base_y-4-(next_value-low)/(high-low)*74
                            c.create_line(end_x, y, end_x, next_y, fill=color, tags="reference_trace")
        self._draw_playhead()

    def _draw_playhead(self) -> None:
        self.canvas.delete("playhead")
        self.canvas.delete("reference_readout")
        duration = self._reference_duration()
        if duration:
            width = max(self.canvas.winfo_width(), 100)
            x = 52 + (width-64) * min(1, self.position_ms / duration)
            self.canvas.create_line(x, 32, x, 258, fill="#f4e04d", width=2, tags="playhead")
        if not self.session:
            self.reference_readout.configure(text="")
            return
        selected = self._selected_reference_parameters()
        lanes = self.session.left, self.session.right
        states = [state_at(events, self.position_ms) for events in lanes]
        lines = []
        for name, key, unit, shape, color in selected:
            readings = []
            for state in states:
                value = reference_value(state, key)
                text = "—" if value is None else f"{value:g} {unit}"
                if shape and state and state.get(shape):
                    text += f" {state[shape]}"
                readings.append(text)
            lines.append(f"{name}  A {readings[0]} / B {readings[1]}")
        self.reference_readout.configure(text="   |   ".join(lines))
        if selected:
            width = max(self.canvas.winfo_width(), 100)
            for i, (name, *_, color) in enumerate(selected):
                x = 95 + i*(width-105)/len(selected)
                if (width-105)/len(selected) > 56:
                    self.canvas.create_text(x, 18, anchor="w", text=name, fill=color, tags="reference_readout")

    def _reference_duration(self) -> int:
        return max(self._duration(), self.session.duration_ms if self.session else 0)

    def _seek_canvas(self, event: tk.Event) -> None:
        if not self._duration() or self.video_clock is not None:
            return
        fraction = min(1.0, max(0.0, (event.x-52) / max(1, self.canvas.winfo_width()-64)))
        self.position_ms = min(self._duration(), int(self._reference_duration() * fraction))
        self._update_position()

    def _scrubbed(self, value: str) -> None:
        if self._duration() and self.video_clock is None:
            self.position_ms = int(float(value))
            self._update_position(set_scale=False)

    def _toggle(self) -> None:
        if self.video_clock is not None:
            self.video_status.configure(text="Use Play / Pause in the video player")
            return
        duration = self._duration()
        if not duration and self.texture_mode.get() == "Reference JSON":
            return
        if duration and self.position_ms >= duration and self.texture_mode.get() == "Reference JSON":
            self.position_ms = 0
        self._update_balance()
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
        self._disconnect_video()
        self.reference_rotation.enable(False)
        self._update_rotation()
        if self.bridge:
            self.bridge.state.modulation.stop()
        self.playing = False
        self.headphone_monitor.pause()
        self.position_ms = 0
        self._update_position()
        self._write_log("STOP simulation reset to 00:00")

    def _pressure_stop(self):
        self._stop()
        self.headphone_monitor.disarm()
        self.monitor_button.configure(text="Arm monitor")

    def _tick(self) -> None:
        if self.close_request and Path(self.close_request).exists():
            self._close()
            return
        pressure = self.headphone_monitor.pressure_control
        pressure.target()
        if pressure.fault and not self._pressure_fault_seen:
            self._pressure_fault_seen = True
            self._pressure_stop()
            self._write_log(pressure.fault)
        elif not pressure.fault:
            self._pressure_fault_seen = False
        # Sensor telemetry must advance even when no session is playing.
        if self.bridge:
            monitor = self.headphone_monitor.status()
            selected = self.device_latch.selected
            monitor["device"] = selected.label if selected else None
            if self.director_display_active and self.director_texture is None and not self.reference_lanes:
                self._publish_live_state()
            else:
                self.bridge.state.update_live({"headphone_monitor": monitor, "monitor_state": self.monitor_state()})
        now = time.monotonic()
        if self.headphone_monitor.last_error and self.headphone_monitor.last_error != self.monitor_error_seen:
            self.monitor_error_seen = self.headphone_monitor.last_error
            self.monitor_button.configure(text="Arm monitor")
            self.monitor_banner.configure(text="HEADPHONE MONITOR — OUTPUT ERROR", foreground="#a02b2b")
            self.device_status.configure(text=self.monitor_error_seen, foreground="#a02b2b")
            self._write_log(f"HEADPHONE MONITOR output error: {self.monitor_error_seen}")
        duration = self._duration()
        if self.video_clock is not None:
            self._follow_tick(now)
        elif self.playing:
            self.position_ms += int((now - self.last_tick) * 1000)
            if duration and self.position_ms >= duration and self.texture_mode.get() == "Reference JSON":
                self.position_ms = duration
                if self.bridge:
                    self.bridge.state.modulation.stop()
                self.playing = False
                self.headphone_monitor.pause()
            self._update_position()
        self._update_heart_tempo_status()
        self._refresh_monitor_status()
        self.last_tick = now
        self.after(50, self._tick)

    def _duration(self) -> int:
        script = self.balance_curve.times[-1] if self.balance_curve else 0
        reference = self.session.duration_ms if self.session and self.texture_mode.get() == "Reference JSON" else 0
        return max(script, reference)

    def _update_position(self, set_scale: bool = True) -> None:
        duration = self._duration()
        self.position_label.configure(text=f"{clock(self.position_ms)} / {clock(duration)}")
        if set_scale:
            self.scrub.configure(to=max(1, duration, self.position_ms))
            self.scrub.set(self.position_ms)
        self._draw_playhead()
        self._update_balance()
        left, right = self._monitor_states()
        if self.director_display_active:
            for lane in self.reference_lanes:
                self._reflect_reference_lane(lane, (left, right)[lane])
        self._show_state(self.left_state, left)
        self._show_state(self.right_state, right)
        if self.headphone_monitor.armed and left and right:
            transition = 0.1 if self.texture_mode.get() == "Reference JSON" else 1.0
            self.headphone_monitor.update_states(left, right, transition_seconds=transition)
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
        left, right = self._semantic_pair()
        reading = analyse_pair(left, right)
        lanes = " | ".join(
            f"{label} carrier={state['freq']:g}Hz AM={state['amFreq']:g}Hz/{state['amDepth']:g}% "
            f"2AM={state['secondaryAmFreq']:g}Hz/{state['secondaryAmDepth']:g}% "
            f"FM depth={state['fmDepth']:g}Hz"
            for label, state in (("L", left), ("R", right))
        )
        self._write_log(f"PREVIEW | {lanes} | texture={reading.texture} | AUDIO=DISABLED")

    def _semantic_pair(self) -> tuple[dict, dict]:
        return self._semantic_state(0), self._semantic_state(1)

    def _semantic_state(self, lane) -> dict:
        variables = self.control_vars if lane == 0 else self.right_control_vars
        return semantic_state(intensity=self.control_vars["intensity"].get(),
                              preset=self.preset_store.active(),
                              **{key: variables[key].get() for key in TEXTURE_SLIDER_FIELDS})

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
            self.bridge.state.reference_rotation = self.reference_rotation
            self.bridge.start()
            self._publish_live_state()
            host, port = self.bridge.address
            self.bridge_status.configure(text=f"READY  http://{host}:{port}  •  OFFLINE ONLY", foreground="#176b32")
            self._write_log(f"DIRECTOR BRIDGE ready http://{host}:{port} | AUDIO=DISABLED")
        except OSError as exc:
            self.bridge = None
            self.bridge_status.configure(text="UNAVAILABLE — PORT IN USE", foreground="#a02b2b")
            self._write_log(f"DIRECTOR BRIDGE unavailable: {exc}")

    def _poll_director(self) -> None:
        if self.bridge:
            while True:
                try:
                    event = self.bridge.state.events.get_nowait()
                except Empty:
                    break
                if event.get("type") == "reference_texture":
                    if not self.playing or not self.rotate_references.get():
                        self._write_log("REFERENCE rotation skipped while paused or disabled")
                        continue
                    lane = event["lane"]
                    self.director_texture_request_id = None
                    self.calibration_ended = self.calibration_ended or self.calibration.get()
                    self.calibration.set(False)
                    self.reference_receipt = {"token": event["token"], "lane": lane, "name": event["session"].path.stem}
                    self.reference_lanes[0 if lane == "left" else 1] = event["session"]
                    left, right = self._monitor_states()
                    lane_index = 0 if lane == "left" else 1
                    self._reflect_reference_lane(lane_index, (left, right)[lane_index])
                    if self.headphone_monitor.armed:
                        self.headphone_monitor.update_states(left, right, transition_seconds=6.0)
                    self._show_state(self.left_state, left)
                    self._show_state(self.right_state, right)
                    self.texture_status.configure(text=f"Director reference active · {lane}")
                    self._write_log(f"REFERENCE rotation | {lane} | {event['session'].path.name} | {clock(self.position_ms)}")
                    self._publish_live_state(left, right)
                elif event.get("type") == "command":
                    command = event["command"]
                    self.director_display_active = True
                    first_texture = self.director_texture is None
                    was_calibrating = self.calibration.get()
                    self.calibration_ended = self.calibration_ended or was_calibrating
                    self.calibration.set(False)
                    self.reference_receipt = None
                    self.reference_lanes.clear()
                    self.director_texture = command["left_signal"], command["right_signal"]
                    self.director_texture_request_id = command["request_id"]
                    intensities = (float(command["left_semantic"]["intensity"]),
                                   float(command["right_semantic"]["intensity"]))
                    if self._band_active():
                        # The command's semantic intensity, relative to the band's anchor, places
                        # each lane inside the band. A first texture becomes the anchor itself.
                        if self.level_anchor is None:
                            self.level_anchor = intensities
                        if not self.level_hold:
                            self.level_offsets = [intensity_offset(i, a) for i, a in zip(intensities, self.level_anchor)]
                            self.level_request_id = command["request_id"]
                    self.last_intensity = intensities
                    self._reflect_texture_sliders(0, command["left_semantic"])
                    self._reflect_texture_sliders(1, command["right_semantic"])
                    left, right = self._monitor_states()
                    self.texture_status.configure(text="Director texture active")
                    self._update_balance()
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
                        transition = 0.1 if first_texture and not was_calibrating else command["transition_seconds"]
                        self.headphone_monitor.update_states(left, right, transition)
                        self._write_log(
                            f"HEADPHONE MONITOR staged transition on {self.device_latch.selected.label} "
                            f"| one perceptual parameter at a time | intensity last"
                        )
                    self._publish_live_state(left, right)
                elif event.get("type") == "level":
                    if not (self._band_active() and self.playing and (self.director_texture or self.reference_lanes)):
                        self._write_log("LEVEL request skipped: band off, paused or no texture yet")
                        continue
                    self.level_offsets = [clamp_offset(event["left"]), clamp_offset(event["right"])]
                    self.level_request_id = event["request_id"]
                    if event.get("hold") is not None:
                        self.level_hold = bool(event["hold"])
                    self._write_log(f"LEVEL id={event['request_id']} L={self.level_offsets[0]:+g}% "
                                    f"R={self.level_offsets[1]:+g}% transition={event['transition_seconds']:g}s")
                    self._restage_levels(float(event["transition_seconds"]))
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
                    self.calibration.set(False)
                    self.calibration_ended = False
                    self.reference_receipt = None
                    self._stop()
                    self.headphone_monitor.stop()
                    self.director_texture = None
                    self.director_texture_request_id = None
                    self.reference_lanes.clear()
                    self.last_intensity = None
                    self._reset_level_band()
                    self.monitor_button.configure(text="Arm monitor")
                    self.monitor_banner.configure(text="HEADPHONE MONITOR — DISARMED", foreground="#a02b2b")
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
        self.right_control_vars["texture"].set(self.control_vars["texture"].get())
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
        self._disconnect_video()
        self.playing = False
        self.headphone_monitor.disarm()
        if self.output_settings_job is not None:
            self.after_cancel(self.output_settings_job)
        self._save_output_settings()
        self.headphone_monitor.pressure_control.close()
        if self.bridge:
            self.bridge.close()
            self.bridge = None
        self.destroy()


if __name__ == "__main__":
    SignalLab().mainloop()
