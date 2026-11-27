"""Compare the two authored-motion interpretations with the same time axis."""
from bisect import bisect_left, bisect_right
import math
import tkinter as tk
from tkinter import ttk


LINEAR = "#d2dae5"
GRADIENT = "#66e6c2"


class MotionComparison(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.title("Funscript motion comparison")
        self.geometry("850x485")
        self.minsize(620, 395)
        self.center = owner.position_ms
        controls = ttk.Frame(self, padding=(12, 10))
        controls.pack(fill="x")
        ttk.Label(controls, text="Audition").pack(side="left")
        choice = ttk.Combobox(controls, state="readonly", width=10, values=("Linear", "Gradient"),
                              textvariable=owner.balance_interpolation)
        choice.pack(side="left", padx=(6, 18))
        choice.bind("<<ComboboxSelected>>", owner._change_balance_interpolation)
        ttk.Label(controls, text="Window (s)").pack(side="left")
        self.span = tk.StringVar(value="10")
        zoom = ttk.Combobox(controls, state="readonly", width=5,
                            values=("2", "5", "10", "20", "60"), textvariable=self.span)
        zoom.pack(side="left", padx=6)
        zoom.bind("<<ComboboxSelected>>", lambda _e: self.redraw())
        ttk.Button(controls, text="Center on playhead", command=self.recenter).pack(side="left", padx=8)
        self.detail = ttk.Label(self, padding=(12, 0, 12, 7))
        self.detail.pack(anchor="w")
        self.canvas = tk.Canvas(self, background="#111a21", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.after_idle(self.redraw)

    def recenter(self):
        self.center = self.owner.position_ms
        self.redraw()

    def redraw(self):
        canvas = self.canvas
        canvas.delete("all")
        self._bounds = None
        curve = self.owner.balance_curve
        self.detail.configure(text="White · Linear     Green · Gradient     Same authored points and timestamps")
        if curve is None:
            canvas.create_text(20, 20, anchor="nw", text="Open a balance Funscript in Signal Lab.", fill=LINEAR)
            return
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 180 or height < 130:
            return
        span = min(int(self.span.get()) * 1000, max(1, curve.times[-1]))
        start = max(0, min(curve.times[-1] - span, self.center - span / 2))
        end = start + span
        left, right = 68, width - 14
        position_top, position_bottom = 27, height / 2 - 18
        speed_top, speed_bottom = height / 2 + 20, height - 32

        def x_of(at):
            return left + (at - start) / span * (right - left)

        times = [start + span * index / 600 for index in range(601)]
        # Include authored instants and the immediately preceding linear velocity.
        first, last = bisect_left(curve.times, start), bisect_right(curve.times, end)
        authored = list(zip(curve.times[first:last], curve.positions[first:last]))
        for at, _ in authored:
            times.extend((max(start, at - 0.001), at))
        times.sort()
        series = [(name, color, [curve.sample(at, name) for at in times])
                  for name, color in (("Linear", LINEAR), ("Gradient", GRADIENT))]
        max_speed = max((abs(speed) for _, _, samples in series for _, speed in samples), default=0)
        limit = max(1.0, 10 ** math.floor(math.log10(max(1.0, max_speed))) / 2)
        limit *= max(1, math.ceil(max_speed / limit))

        def y_pos(position):
            return position_bottom - (position_bottom - position_top) * position / 100

        def y_speed(speed):
            return speed_bottom - (speed_bottom - speed_top) * (speed + limit) / (2 * limit)

        canvas.create_text(left, 9, anchor="w", text="Position (%)", fill=LINEAR)
        canvas.create_text(left, speed_top - 17, anchor="w", text="Velocity (percentage points/sec)", fill=LINEAR)
        for labels, y_at in (((0, 50, 100), y_pos), ((-limit, 0, limit), y_speed)):
            for value in labels:
                y = y_at(value)
                canvas.create_text(left - 7, y, anchor="e", text=f"{value:g}", fill=LINEAR)
                canvas.create_line(left, y, right, y, fill="#34434e")
        for index in range(5):
            at = start + span * index / 4
            x = x_of(at)
            canvas.create_line(x, position_top, x, position_bottom, fill="#273842")
            canvas.create_line(x, speed_top, x, speed_bottom, fill="#273842")
            canvas.create_text(x, speed_bottom + 12, text=f"{at / 1000:g}s", fill=LINEAR)
        for _, color, samples in series:
            positions, velocities = [], []
            for at, (position, speed) in zip(times, samples):
                x = x_of(at)
                positions.extend((x, y_pos(position)))
                velocities.extend((x, y_speed(speed)))
            canvas.create_line(*positions, fill=color, width=2)
            canvas.create_line(*velocities, fill=color, width=2)
        for at, position in authored:
            x, y = x_of(at), y_pos(position)
            canvas.create_oval(x-2, y-2, x+2, y+2, fill="#f1bf75", outline="")
        self._bounds = (start, end, x_of, position_top, speed_bottom)
        self.update_playhead()

    def update_playhead(self):
        canvas = self.canvas
        canvas.delete("current")
        if not self.owner.balance_curve or getattr(self, "_bounds", None) is None:
            return
        start, end, x_of, top, bottom = self._bounds
        if start <= self.owner.position_ms <= end:
            x = x_of(self.owner.position_ms)
            canvas.create_line(x, top, x, bottom, fill="#f1bf75", dash=(2, 4), tags="current")
