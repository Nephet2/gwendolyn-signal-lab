"""Two-lane editor using Vector's bounded curve interpolation."""
from copy import deepcopy
import json
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from .funscript_balance import gains
from .spatial_curves_math import evaluate, validate_curve

PROFILES = Path(__file__).resolve().parents[1] / "crossover_profiles.json"
PRESETS = ("Equal power", "Original balance", "Linear crossover", "Narrow crossover")
COLORS = ("#7ddfcb", "#f1b095")


def preset(name):
    if name == "Linear crossover":
        return [{"points": [[0, 100], [100, 0]], "interpolation": "linear"},
                {"points": [[0, 0], [100, 100]], "interpolation": "linear"}]
    if name == "Narrow crossover":
        return [{"points": [[0, 100], [40, 100], [60, 0], [100, 0]], "interpolation": "smooth"},
                {"points": [[0, 0], [40, 0], [60, 100], [100, 100]], "interpolation": "smooth"}]
    xs = (0, 25, 50, 75, 100)
    return [{"points": [[x, gains(x, name)[lane] * 100] for x in xs],
             "interpolation": "smooth" if name == "Equal power" else "linear"} for lane in range(2)]


def validate_pair(curves):
    if not isinstance(curves, list) or len(curves) != 2:
        raise ValueError("A profile needs two curves.")
    return [validate_curve(curve) for curve in curves]


def load_profiles(path=PROFILES):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    profiles = {}
    for name, entry in data.items():
        try:
            profiles[name] = {"description": str(entry.get("description", "")),
                              "curves": validate_pair(entry["curves"])}
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return profiles


class CrossoverEditor:
    def __init__(self, parent, curves, apply, position):
        self.window = tk.Toplevel(parent)
        self.window.title("Custom crossover · Channel A and B")
        self.window.geometry("850x360")
        self.window.minsize(680, 310)
        self.curves = validate_pair(curves)
        self.apply = apply
        self.position = position
        self.profiles = load_profiles()
        self.drag = None
        self.live = tk.BooleanVar(value=False)
        self.pending_apply = None
        self.canvases = []
        self.interpolation = []

        top = ttk.Frame(self.window, padding=8)
        top.pack(fill="x")
        self.preset_name = tk.StringVar(value=PRESETS[0])
        ttk.Combobox(top, textvariable=self.preset_name, values=PRESETS, state="readonly", width=20).pack(side="left")
        ttk.Button(top, text="Load preset", command=lambda: self.replace(preset(self.preset_name.get()))).pack(side="left", padx=5)
        ttk.Label(top, text="Drag points · double-click to add · right-click to remove").pack(side="left", padx=8)
        ttk.Button(top, text="Use custom crossover", command=self.activate).pack(side="right")
        ttk.Checkbutton(top, text="Live apply", variable=self.live, command=self.toggle_live).pack(side="right", padx=5)

        bar = ttk.Frame(self.window, padding=(8, 0, 8, 8))
        bar.pack(fill="x")
        self.names = ttk.Combobox(bar, values=sorted(self.profiles), width=20, state="readonly")
        self.names.pack(side="left")
        ttk.Button(bar, text="Load profile", command=self.load_profile).pack(side="left", padx=4)
        self.name = tk.StringVar()
        self.description = tk.StringVar()
        ttk.Label(bar, text="Name").pack(side="left", padx=(7, 3))
        ttk.Entry(bar, textvariable=self.name, width=17).pack(side="left")
        ttk.Label(bar, text="Description").pack(side="left", padx=(7, 3))
        ttk.Entry(bar, textvariable=self.description).pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="Save profile", command=self.save_profile).pack(side="right", padx=(5, 0))

        graphs = ttk.Frame(self.window, padding=(8, 0, 8, 8))
        graphs.pack(fill="both", expand=True)
        for lane, label in enumerate(("A / Left", "B / Right")):
            panel = ttk.LabelFrame(graphs, text=label, padding=5)
            panel.pack(side="left", fill="both", expand=True, padx=3)
            var = tk.StringVar(value=self.curves[lane]["interpolation"])
            self.interpolation.append(var)
            chooser = ttk.Combobox(panel, textvariable=var, values=("linear", "smooth"), state="readonly", width=9)
            chooser.pack(anchor="e")
            chooser.bind("<<ComboboxSelected>>", lambda e, i=lane: self.set_interpolation(i))
            canvas = tk.Canvas(panel, background="#101722", highlightthickness=0, width=310, height=180)
            self.canvases.append(canvas)
            canvas.pack(fill="both", expand=True)
            for event, action in (("<Configure>", self.draw_event), ("<Button-1>", self.press),
                                  ("<B1-Motion>", self.move), ("<Double-Button-1>", self.add),
                                  ("<Button-3>", self.remove)):
                canvas.bind(event, lambda e, i=lane, fn=action: fn(i, e))
        self.status = ttk.Label(self.window, text="Position → gain (%) · changes stay in the editor until applied.", padding=(10, 0, 10, 6))
        self.status.pack(anchor="w")
        self.window.after(100, self.refresh_marker)

    def bounds(self, lane):
        canvas = self.canvases[lane]
        return 36, 12, max(38, canvas.winfo_width()-13), max(14, canvas.winfo_height()-23)

    def xy(self, lane, x, y):
        l, t, r, b = self.bounds(lane)
        return l+(r-l)*x/100, b-(b-t)*y/100

    def percentages(self, lane, event):
        l, t, r, b = self.bounds(lane)
        return (min(100, max(0, (event.x-l)*100/(r-l))),
                min(100, max(0, (b-event.y)*100/(b-t))))

    def near(self, lane, event):
        for i, point in enumerate(self.curves[lane]["points"]):
            x, y = self.xy(lane, *point)
            if (x-event.x)**2+(y-event.y)**2 <= 9**2:
                return i
        return None

    def draw_event(self, lane, _event):
        self.draw(lane)

    def draw(self, lane):
        c = self.canvases[lane]
        c.delete("all")
        l, t, r, b = self.bounds(lane)
        for tick in (0, 25, 50, 75, 100):
            x, y = self.xy(lane, tick, tick)
            c.create_line(x, t, x, b, fill="#324456")
            c.create_line(l, y, r, y, fill="#324456")
            c.create_text(x, b+11, text=str(tick), fill="#a8bac7")
            c.create_text(l-5, y, text=str(tick), fill="#a8bac7", anchor="e")
        curve = self.curves[lane]
        coords = [coord for x in range(101) for coord in self.xy(lane, x, evaluate(curve, x/100)*100)]
        c.create_line(*coords, fill=COLORS[lane], width=2)
        for point in curve["points"]:
            x, y = self.xy(lane, *point)
            c.create_oval(x-4, y-4, x+4, y+4, fill=COLORS[lane], outline="#101722")

    def refresh_marker(self):
        if not self.window.winfo_exists():
            return
        pos = self.position()
        for lane, c in enumerate(self.canvases):
            c.delete("position")
            if pos is not None:
                x, y = self.xy(lane, pos, evaluate(self.curves[lane], pos/100)*100)
                _, t, _, b = self.bounds(lane)
                c.create_line(x, t, x, b, fill=COLORS[lane], dash=(2, 3), tags="position")
                c.create_oval(x-4, y-4, x+4, y+4, outline="white", fill=COLORS[lane], tags="position")
        self.window.after(100, self.refresh_marker)

    def press(self, lane, event):
        self.drag = self.near(lane, event)

    def move(self, lane, event):
        points = self.curves[lane]["points"]
        i = self.drag
        if i is None:
            return
        x, y = self.percentages(lane, event)
        if i in (0, len(points)-1):
            x = points[i][0]
        else:
            x = min(points[i+1][0]-.1, max(points[i-1][0]+.1, x))
        points[i] = [x, y]
        self.draw(lane)
        self.changed()

    def add(self, lane, event):
        points = self.curves[lane]["points"]
        x, y = self.percentages(lane, event)
        if len(points) < 24 and all(abs(x-p[0]) >= .1 for p in points):
            points.append([x, y])
            points.sort()
            self.drag = points.index([x, y])
            self.draw(lane)
            self.changed()

    def remove(self, lane, event):
        points = self.curves[lane]["points"]
        i = self.near(lane, event)
        if i is not None and 0 < i < len(points)-1:
            del points[i]
            self.drag = None
            self.draw(lane)
            self.changed()

    def set_interpolation(self, lane):
        self.curves[lane]["interpolation"] = self.interpolation[lane].get()
        self.draw(lane)
        self.changed()

    def replace(self, curves):
        self.curves = validate_pair(curves)
        self.drag = None
        for lane in range(2):
            self.interpolation[lane].set(self.curves[lane]["interpolation"])
            self.draw(lane)
        self.changed()

    def changed(self):
        if self.live.get() and self.pending_apply is None:
            self.pending_apply = self.window.after(45, self.apply_live)

    def apply_live(self):
        self.pending_apply = None
        if self.live.get():
            self.activate()

    def toggle_live(self):
        if self.live.get():
            self.activate()
        else:
            self.status.configure(text="Live apply off · use the button to apply further edits.")

    def activate(self):
        self.apply(deepcopy(validate_pair(self.curves)))
        self.status.configure(text="Live apply on · changes ramp during playback." if self.live.get()
                              else "Custom crossover active · playback continues with a short gain ramp.")

    def load_profile(self):
        name = self.names.get()
        if name in self.profiles:
            entry = self.profiles[name]
            self.name.set(name)
            self.description.set(entry["description"])
            self.replace(entry["curves"])
            self.status.configure(text=f"Loaded {name}." if self.live.get()
                                  else f"Loaded {name}. Click Use custom crossover to apply.")

    def save_profile(self):
        name = self.name.get().strip()
        if not 1 <= len(name) <= 80 or len(self.description.get()) > 500:
            messagebox.showerror("Check profile", "Enter a name of 1–80 characters and a description up to 500.", parent=self.window)
            return
        profiles = dict(self.profiles, **{name: {"description": self.description.get().strip(),
                                                "curves": validate_pair(self.curves)}})
        try:
            temp = PROFILES.with_suffix(".tmp")
            temp.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")
            temp.replace(PROFILES)
        except OSError as error:
            messagebox.showerror("Cannot save profile", str(error), parent=self.window)
            return
        self.profiles = profiles
        self.names.configure(values=sorted(profiles))
        self.names.set(name)
        self.status.configure(text=f"Saved {name}.")
