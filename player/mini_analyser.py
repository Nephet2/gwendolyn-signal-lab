"""Compact views of the stereo PCM written by Signal Lab's monitor."""
import tkinter as tk
from tkinter import ttk

import numpy as np


COLORS = ("#55dfbc", "#ffab67")
BACKGROUND = "#101820"
GRID = "#344754"
TEXT = "#b4c6d2"
FONT = ("Segoe UI", 9)


def dbfs(value):
    return 20 * np.log10(np.maximum(value, 1e-6))


class MiniAnalyser(ttk.Frame):
    def __init__(self, parent, monitor):
        super().__init__(parent, padding=10)
        self.monitor = monitor
        self.snapshot = None
        self.mode = tk.StringVar(value="Waveform")
        self.zoom = tk.StringVar(value="1×")
        self.frozen = tk.BooleanVar(value=False)
        self.status = tk.StringVar()
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Signal Lab output · digital").pack(side="left", padx=(0, 10))
        ttk.Combobox(bar, textvariable=self.mode, width=12, state="readonly",
                     values=("Waveform", "Spectrum", "Lissajous")).pack(side="left")
        ttk.Label(bar, text="Zoom").pack(side="left", padx=(12, 4))
        self.zoom_box = ttk.Combobox(bar, textvariable=self.zoom, width=4, state="readonly",
                                     values=("1×", "4×", "16×", "64×"))
        self.zoom_box.pack(side="left")
        self.freeze_button = ttk.Checkbutton(bar, text="Freeze", variable=self.frozen, command=self.refresh)
        self.freeze_button.pack(side="right")
        self.meters = tk.Canvas(self, height=56, bg=BACKGROUND, highlightthickness=0)
        self.meters.pack(fill="x", pady=(8, 3))
        self.chart = tk.Canvas(self, height=225, bg=BACKGROUND, highlightthickness=0)
        self.chart.pack(fill="x")
        ttk.Label(self, textvariable=self.status).pack(anchor="w", pady=(6, 0))
        self.after(120, self.tick)

    def tick(self):
        if self.winfo_viewable():
            self.refresh()
        self.after(120, self.tick)

    def refresh(self):
        if not self.frozen.get():
            self.snapshot = self.monitor.analyser_snapshot()
        self.freeze_button.configure(state="normal" if self.snapshot is not None else "disabled")
        self.zoom_box.configure(state="disabled" if self.mode.get() == "Spectrum" else "readonly")
        self.meters.delete("all")
        self.chart.delete("all")
        if self.snapshot is None:
            self.status.set("Waiting for output · arm the monitor and press Play")
            self.draw_levels(None)
            return
        frames, rate = self.snapshot
        tracks = np.frombuffer(frames, dtype="<i2").reshape(-1, 2).T.astype(float) / 32768
        self.draw_levels(tracks)
        self.status.set(("Frozen" if self.frozen.get() else "Live") +
                        " · L (A) green / R (B) orange · after crossover, trims and monitor level")
        if self.chart.winfo_width() < 100:
            return
        mode = self.mode.get()
        if mode == "Spectrum":
            self.draw_spectrum(tracks, rate)
        elif mode == "Lissajous":
            self.draw_lissajous(tracks, rate)
        else:
            self.draw_waveform(tracks, rate)

    def draw_levels(self, tracks):
        c = self.meters
        peaks = rms = (-120, -120)
        if tracks is not None:
            peaks = dbfs(np.max(np.abs(tracks), axis=1))
            rms = dbfs(np.sqrt(np.mean(tracks ** 2, axis=1)))
        left, right = 28, max(29, c.winfo_width() - 112)

        def x(db):
            return left + (right - left) * np.clip((db + 72) / 72, 0, 1)

        for i, (level, peak, color) in enumerate(zip(rms, peaks, COLORS)):
            y = 12 + i * 20
            c.create_text(13, y, text="LR"[i], fill=color, font=FONT)
            c.create_rectangle(left, y-5, right, y+5, fill=GRID, outline="")
            if tracks is not None:
                c.create_rectangle(left, y-5, x(level), y+5, fill=color, outline="")
                c.create_line(x(peak), y-7, x(peak), y+7, fill="white")
            label = f"{level:.0f} dBFS RMS" if tracks is not None else "— dBFS RMS"
            c.create_text(right+7, y, text=label, fill=TEXT, anchor="w", font=FONT)
        for db in (-72, -48, -24, 0):
            c.create_text(x(db), 48, text=str(db), fill=TEXT, font=FONT)

    def plot_area(self):
        return 55, 12, self.chart.winfo_width()-23, self.chart.winfo_height()-26

    def draw_waveform(self, tracks, rate):
        c = self.chart
        left, top, right, bottom = self.plot_area()
        zoom = int(self.zoom.get()[:-1])
        span = 20
        for ms in range(0, span+1, 5):
            x = left + (right-left) * ms/span
            c.create_line(x, top, x, bottom, fill=GRID)
            c.create_text(x, bottom+12, text=f"{ms} ms", fill=TEXT, font=FONT)
        for fraction, label in ((0, f"+{1/zoom:.3g}"), (.5, "0"), (1, f"−{1/zoom:.3g}")):
            y = top + fraction * (bottom-top)
            c.create_line(left, y, right, y, fill=GRID)
            c.create_text(left-6, y, text=label, anchor="e", fill=TEXT, font=FONT)
        for track, color in zip(tracks, COLORS):
            signal = np.clip(track[-rate*span//1000:] * zoom, -1, 1)
            xs = np.linspace(left, right, len(signal))
            ys = top + (1-signal) * (bottom-top)/2
            c.create_line(*np.column_stack((xs, ys)).ravel(), fill=color)

    def draw_spectrum(self, tracks, rate):
        c = self.chart
        left, top, right, bottom = self.plot_area()
        low, high = 20, min(rate/2, 20000)

        def x(hz):
            return left + (right-left) * np.log(hz/low) / np.log(high/low)

        for hz in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            if hz <= high:
                c.create_line(x(hz), top, x(hz), bottom, fill=GRID)
                label = f"{hz//1000}k" if hz >= 1000 else str(hz)
                c.create_text(x(hz), bottom+12, text=label, fill=TEXT, font=FONT)
        for db in (0, -30, -60, -90):
            y = top - db/90 * (bottom-top)
            c.create_line(left, y, right, y, fill=GRID)
            c.create_text(left-6, y, text=f"{db} dB", anchor="e", fill=TEXT, font=FONT)
        window = np.hanning(tracks.shape[1])
        hz = np.fft.rfftfreq(window.size, 1/rate)
        valid = (hz >= low) & (hz <= high)
        for track, color in zip(tracks, COLORS):
            level = dbfs(np.abs(np.fft.rfft(track * window)) * 2 / window.sum())
            ys = top - np.clip(level[valid], -90, 0)/90 * (bottom-top)
            c.create_line(*np.column_stack((x(hz[valid]), ys)).ravel(), fill=color)

    def draw_lissajous(self, tracks, rate):
        c = self.chart
        cx, cy = c.winfo_width()/2, c.winfo_height()/2-6
        radius = cy - 15
        for value in (-1, -.5, 0, .5, 1):
            x, y = cx+value*radius, cy-value*radius
            c.create_line(x, cy-radius, x, cy+radius, fill=GRID)
            c.create_line(cx-radius, y, cx+radius, y, fill=GRID)
        c.create_text(cx, cy+radius+13, text="Left (X)", fill=COLORS[0], font=FONT)
        c.create_text(cx-radius-8, cy, text="Right (Y)", anchor="e", fill=COLORS[1], font=FONT)
        zoom = int(self.zoom.get()[:-1])
        left, right = np.clip(tracks[:, -rate//50:] * zoom, -1, 1)
        c.create_line(*np.column_stack((cx+radius*left, cy-radius*right)).ravel(), fill=COLORS[0])

