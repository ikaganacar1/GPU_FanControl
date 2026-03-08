#!/usr/bin/env python3
"""GPU Fan Control - Dual GPU fan controller with tkinter GUI.

Uses pynvml via a root helper subprocess for fan control (no Coolbits needed).
Starts minimized to taskbar on boot. Close button minimizes, Quit button exits.
"""

import subprocess
import signal
import sys
import os
import json
import threading
import time
import math
import io
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

import pynvml
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
PYTHON = Path(sys.executable).resolve()
HELPER = SCRIPT_DIR / "fan_helper.py"
CONFIG_PATH = Path.home() / ".config" / "gpu-fancontrol" / "config.json"

DEFAULT_CURVES = {
    "RTX 3090": [(0, 30), (50, 35), (60, 50), (65, 60), (70, 75), (75, 85), (80, 100)],
    "RTX 5070": [(0, 30), (50, 30), (60, 40), (65, 50), (70, 65), (75, 80), (80, 100)],
    "default":  [(0, 30), (50, 35), (60, 50), (70, 75), (80, 100)],
}

POLL_INTERVAL_MS = 3000

# Dark theme colors (Catppuccin Mocha)
BG = "#1e1e2e"
BG_PANEL = "#2a2a3d"
BG_INPUT = "#363650"
FG = "#cdd6f4"
FG_DIM = "#6c7086"
ACCENT = "#89b4fa"
GREEN = "#a6e3a1"
YELLOW = "#f9e2af"
RED = "#f38ba8"
ORANGE = "#fab387"
BORDER = "#45475a"

# ---------------------------------------------------------------------------
# GPU detection via pynvml
# ---------------------------------------------------------------------------

def detect_gpus() -> list[dict]:
    pynvml.nvmlInit()
    gpus = []
    for i in range(pynvml.nvmlDeviceGetCount()):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        name = pynvml.nvmlDeviceGetName(h)
        temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        try:
            num_fans = pynvml.nvmlDeviceGetNumFans(h)
            fan_speed = pynvml.nvmlDeviceGetFanSpeed_v2(h, 0) if num_fans > 0 else 0
        except Exception:
            num_fans = 0
            fan_speed = 0
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            mem_used = mem.used / (1024 ** 3)
            mem_total = mem.total / (1024 ** 3)
        except Exception:
            mem_used = 0.0
            mem_total = 0.0
        try:
            power_usage = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
        except Exception:
            power_usage = 0.0
        try:
            power_limit = pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0
        except Exception:
            power_limit = 0.0
        gpus.append({
            "index": i, "name": name, "temp": temp,
            "fan_speed": fan_speed, "num_fans": num_fans,
            "mem_used": mem_used, "mem_total": mem_total,
            "power_usage": power_usage, "power_limit": power_limit,
        })
    pynvml.nvmlShutdown()
    return gpus


def poll_gpu_stats(gpus: list[dict]):
    pynvml.nvmlInit()
    for gpu in gpus:
        h = pynvml.nvmlDeviceGetHandleByIndex(gpu["index"])
        gpu["temp"] = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        try:
            if gpu["num_fans"] > 0:
                gpu["fan_speed"] = pynvml.nvmlDeviceGetFanSpeed_v2(h, 0)
        except Exception:
            pass
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            gpu["mem_used"] = mem.used / (1024 ** 3)
            gpu["mem_total"] = mem.total / (1024 ** 3)
        except Exception:
            pass
        try:
            gpu["power_usage"] = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
        except Exception:
            pass
    pynvml.nvmlShutdown()

# ---------------------------------------------------------------------------
# Root helper communication
# ---------------------------------------------------------------------------

class FanHelper:
    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return True, ""
            try:
                self._proc = subprocess.Popen(
                    ["sudo", "-n", str(PYTHON), str(HELPER)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                )
                return True, ""
            except Exception as e:
                return False, str(e)

    def stop(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._send_raw({"cmd": "quit"})
                try:
                    self._proc.wait(timeout=3)
                except Exception:
                    self._proc.kill()
            self._proc = None

    def set_fan(self, gpu: int, fan: int, speed: int) -> bool:
        return self._send({"cmd": "set", "gpu": gpu, "fan": fan, "speed": speed})

    def reset_all(self) -> bool:
        return self._send({"cmd": "reset_all"})

    def _send(self, cmd: dict) -> bool:
        with self._lock:
            return self._send_raw(cmd)

    def _send_raw(self, cmd: dict) -> bool:
        if not self._proc or self._proc.poll() is not None:
            return False
        try:
            self._proc.stdin.write(json.dumps(cmd) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline().strip()
            if line:
                return json.loads(line).get("ok", False)
            return False
        except Exception:
            return False

# ---------------------------------------------------------------------------
# Fan curve interpolation
# ---------------------------------------------------------------------------

def interpolate_curve(curve: list[tuple[int, int]], temp: int) -> int:
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, s0 = curve[i]
        t1, s1 = curve[i + 1]
        if t0 <= temp <= t1:
            if t1 == t0:
                return s1
            return int(s0 + (temp - t0) / (t1 - t0) * (s1 - s0))
    return curve[-1][1]

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

# ---------------------------------------------------------------------------
# Icon
# ---------------------------------------------------------------------------

def create_icon_image() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (137, 180, 250)
    cx, cy = size // 2, size // 2
    r = size // 2 - 4
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=3)
    hr = 8
    draw.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=color)
    for angle_deg in [0, 90, 180, 270]:
        a = math.radians(angle_deg + 15)
        x1 = cx + int(hr * math.cos(a))
        y1 = cy + int(hr * math.sin(a))
        x2 = cx + int((r - 2) * math.cos(a))
        y2 = cy + int((r - 2) * math.sin(a))
        draw.line([x1, y1, x2, y2], fill=color, width=4)
    return img

# ---------------------------------------------------------------------------
# Temperature color
# ---------------------------------------------------------------------------

def temp_color(temp: int) -> str:
    if temp >= 80: return RED
    if temp >= 70: return ORANGE
    if temp >= 60: return YELLOW
    if temp >= 50: return ACCENT
    return GREEN

# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class GPUFanControlApp:
    def __init__(self, start_minimized=False):
        self.gpus = detect_gpus()
        self.fan_control_enabled = False
        self.running = True
        self.helper = FanHelper()
        self.start_minimized = start_minimized

        # Drag state for curve canvas interaction
        self._drag_states = {}   # gpu_idx -> point index being dragged, or None
        self._dragging = set()   # gpu_idxes currently being dragged (suppresses trace callbacks)

        # Per-GPU state
        self.gpu_states = {}
        cfg = load_config()
        for gpu in self.gpus:
            idx = gpu["index"]
            name = gpu["name"]
            saved = cfg.get(f"gpu_{idx}", {})
            default_curve = DEFAULT_CURVES["default"]
            for key in DEFAULT_CURVES:
                if key in name:
                    default_curve = DEFAULT_CURVES[key]
                    break
            self.gpu_states[idx] = {
                "mode": saved.get("mode", "auto"),
                "manual_speed": saved.get("manual_speed", 50),
                "curve": [tuple(p) for p in saved.get("curve", default_curve)],
                "current_speed": 0,
            }

    def run(self):
        self.root = tk.Tk()
        self.root.title("GPU Fan Control")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Window icon
        try:
            img = create_icon_image()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self._icon = tk.PhotoImage(data=buf.getvalue())
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass

        self._build_ui()

        # Center window
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"+{x}+{y}")

        if self.start_minimized:
            self.root.iconify()

        # Start polling
        self._poll()

        self.root.mainloop()

    def _build_ui(self):
        self.gui_widgets = {}

        main = tk.Frame(self.root, bg=BG, padx=16, pady=16)
        main.pack(fill="both", expand=True)

        # Header
        header = tk.Frame(main, bg=BG)
        header.pack(fill="x", pady=(0, 12))

        tk.Label(header, text="GPU Fan Control", font=("Sans", 18, "bold"),
                 fg=ACCENT, bg=BG).pack(side="left")

        # Quit button (actually exits)
        tk.Button(
            header, text="Quit", font=("Sans", 9),
            bg=BG_INPUT, fg=FG_DIM, activebackground=RED, activeforeground=BG,
            relief="flat", padx=10, pady=2, cursor="hand2",
            command=self._on_quit,
        ).pack(side="right")

        self.status_label = tk.Label(header, text="DISABLED", font=("Sans", 11, "bold"),
                                     fg=FG_DIM, bg=BG)
        self.status_label.pack(side="right", padx=(0, 8))

        self.toggle_btn = tk.Button(
            header, text="Enable", font=("Sans", 10, "bold"),
            bg=ACCENT, fg=BG, activebackground=GREEN, activeforeground=BG,
            relief="flat", padx=16, pady=4, cursor="hand2",
            command=self._toggle_control,
        )
        self.toggle_btn.pack(side="right", padx=(0, 8))

        # GPU panels
        panels_frame = tk.Frame(main, bg=BG)
        panels_frame.pack(fill="both", expand=True)

        for i, gpu in enumerate(self.gpus):
            self._build_gpu_panel(panels_frame, gpu, i)

    def _build_gpu_panel(self, parent, gpu, col):
        idx = gpu["index"]
        state = self.gpu_states[idx]

        panel = tk.Frame(parent, bg=BG_PANEL, relief="flat", bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
        panel.grid(row=0, column=col, padx=(0 if col == 0 else 8, 0),
                   sticky="nsew", ipadx=16, ipady=12)
        parent.columnconfigure(col, weight=1)

        w = {}
        self.gui_widgets[idx] = w

        short_name = gpu["name"].replace("NVIDIA GeForce ", "")
        tk.Label(panel, text=short_name, font=("Sans", 14, "bold"),
                 fg=FG, bg=BG_PANEL).pack(anchor="w", padx=12, pady=(8, 2))

        fan_text = f"{gpu['num_fans']} fan{'s' if gpu['num_fans'] != 1 else ''}"
        tk.Label(panel, text=f"GPU {idx} \u2022 {fan_text}",
                 font=("Sans", 9), fg=FG_DIM, bg=BG_PANEL).pack(anchor="w", padx=12)

        # Temperature + fan speed
        temp_frame = tk.Frame(panel, bg=BG_PANEL)
        temp_frame.pack(fill="x", padx=12, pady=(12, 4))

        w["temp_label"] = tk.Label(temp_frame, text=f"{gpu['temp']}\u00b0C",
                                   font=("Sans", 36, "bold"),
                                   fg=temp_color(gpu["temp"]), bg=BG_PANEL)
        w["temp_label"].pack(side="left")

        fan_info = tk.Frame(temp_frame, bg=BG_PANEL)
        fan_info.pack(side="right", anchor="e")
        tk.Label(fan_info, text="FAN", font=("Sans", 9), fg=FG_DIM, bg=BG_PANEL).pack()
        w["fan_label"] = tk.Label(fan_info, text=f"{gpu['fan_speed']}%",
                                  font=("Sans", 20, "bold"), fg=FG, bg=BG_PANEL)
        w["fan_label"].pack()

        # Fan speed bar
        bar_frame = tk.Frame(panel, bg=BG_INPUT, height=8)
        bar_frame.pack(fill="x", padx=12, pady=(0, 8))
        bar_frame.pack_propagate(False)
        w["fan_bar"] = tk.Frame(bar_frame, bg=ACCENT, height=8)
        w["fan_bar"].place(relx=0, rely=0, relheight=1.0,
                           relwidth=max(0.01, gpu["fan_speed"] / 100))

        # Memory + Power stats row
        stats_frame = tk.Frame(panel, bg=BG_PANEL)
        stats_frame.pack(fill="x", padx=12, pady=(0, 8))

        mem_col = tk.Frame(stats_frame, bg=BG_PANEL)
        mem_col.pack(side="left", expand=True, anchor="w")
        tk.Label(mem_col, text="VRAM", font=("Sans", 9), fg=FG_DIM, bg=BG_PANEL).pack(anchor="w")
        mem_used = gpu.get("mem_used", 0.0)
        mem_total = gpu.get("mem_total", 0.0)
        w["mem_label"] = tk.Label(mem_col,
                                  text=f"{mem_used:.1f} / {mem_total:.0f} GB",
                                  font=("Sans", 11, "bold"), fg=FG, bg=BG_PANEL)
        w["mem_label"].pack(anchor="w")

        pwr_col = tk.Frame(stats_frame, bg=BG_PANEL)
        pwr_col.pack(side="right", anchor="e")
        tk.Label(pwr_col, text="POWER", font=("Sans", 9), fg=FG_DIM, bg=BG_PANEL).pack(anchor="e")
        power_usage = gpu.get("power_usage", 0.0)
        power_limit = gpu.get("power_limit", 0.0)
        w["pwr_label"] = tk.Label(pwr_col,
                                  text=f"{power_usage:.0f} / {power_limit:.0f} W",
                                  font=("Sans", 11, "bold"), fg=FG, bg=BG_PANEL)
        w["pwr_label"].pack(anchor="e")

        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x", padx=12, pady=4)

        # Mode selector
        mode_frame = tk.Frame(panel, bg=BG_PANEL)
        mode_frame.pack(fill="x", padx=12, pady=(4, 8))

        tk.Label(mode_frame, text="Mode", font=("Sans", 10), fg=FG_DIM,
                 bg=BG_PANEL).pack(side="left")

        w["mode_var"] = tk.StringVar(value=state["mode"])
        for label, val in [("Auto", "auto"), ("Manual", "manual")]:
            tk.Radiobutton(mode_frame, text=label, variable=w["mode_var"],
                           value=val, bg=BG_PANEL, fg=FG, selectcolor=BG_INPUT,
                           activebackground=BG_PANEL, activeforeground=FG,
                           font=("Sans", 10),
                           command=lambda i=idx: self._on_mode_change(i)
                           ).pack(side="left", padx=(12 if val == "auto" else 4, 4))

        # Manual slider
        w["manual_frame"] = mf = tk.Frame(panel, bg=BG_PANEL)
        tk.Label(mf, text="Fan Speed", font=("Sans", 10), fg=FG_DIM,
                 bg=BG_PANEL).pack(anchor="w", padx=12)
        sf = tk.Frame(mf, bg=BG_PANEL)
        sf.pack(fill="x", padx=12, pady=4)
        w["manual_var"] = tk.IntVar(value=state["manual_speed"])
        w["manual_label"] = tk.Label(sf, text=f"{state['manual_speed']}%",
                                     font=("Sans", 12, "bold"), fg=ACCENT,
                                     bg=BG_PANEL, width=5)
        w["manual_label"].pack(side="right")
        tk.Scale(sf, from_=20, to=100, orient="horizontal",
                 variable=w["manual_var"], showvalue=False,
                 bg=BG_PANEL, fg=FG, troughcolor=BG_INPUT, highlightthickness=0,
                 activebackground=ACCENT, sliderrelief="flat", length=200,
                 command=lambda v, i=idx: self._on_manual_change(i, int(v)),
                 ).pack(side="left", fill="x", expand=True)

        # Auto curve editor
        w["curve_frame"] = cf = tk.Frame(panel, bg=BG_PANEL)
        tk.Label(cf, text="Fan Curve", font=("Sans", 10), fg=FG_DIM,
                 bg=BG_PANEL).pack(anchor="w", padx=12)

        w["curve_canvas"] = tk.Canvas(cf, width=260, height=120,
                                      bg=BG_INPUT, highlightthickness=0,
                                      cursor="crosshair")
        w["curve_canvas"].pack(padx=12, pady=4)
        w["curve_canvas"].bind("<ButtonPress-1>",
                               lambda e, i=idx: self._curve_mouse_down(e, i))
        w["curve_canvas"].bind("<B1-Motion>",
                               lambda e, i=idx: self._curve_mouse_drag(e, i))
        w["curve_canvas"].bind("<ButtonRelease-1>",
                               lambda e, i=idx: self._curve_mouse_up(e, i))

        pf = tk.Frame(cf, bg=BG_PANEL)
        pf.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(pf, text="Temp\u00b0C:", font=("Sans", 8), fg=FG_DIM,
                 bg=BG_PANEL).grid(row=0, column=0, sticky="w")
        tk.Label(pf, text="Fan %:", font=("Sans", 8), fg=FG_DIM,
                 bg=BG_PANEL).grid(row=1, column=0, sticky="w")

        w["curve_entries"] = []
        for j, (t, s) in enumerate(state["curve"]):
            tv = tk.StringVar(value=str(t))
            sv = tk.StringVar(value=str(s))
            tk.Entry(pf, textvariable=tv, width=4, font=("Sans", 8),
                     bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat",
                     justify="center").grid(row=0, column=j + 1, padx=1)
            tk.Entry(pf, textvariable=sv, width=4, font=("Sans", 8),
                     bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat",
                     justify="center").grid(row=1, column=j + 1, padx=1)
            w["curve_entries"].append((tv, sv))
            tv.trace_add("write", lambda *a, i=idx: self._on_curve_change(i))
            sv.trace_add("write", lambda *a, i=idx: self._on_curve_change(i))

        self._show_mode_frame(idx)
        self._draw_curve(idx)

    # -----------------------------------------------------------------------
    # Window management
    # -----------------------------------------------------------------------

    def _on_close(self):
        """Close button minimizes to taskbar."""
        self.root.iconify()

    def _on_quit(self):
        """Actually quit the app."""
        self.running = False
        if self.fan_control_enabled:
            self.helper.reset_all()
            self.helper.stop()
        self._save_config()
        self.root.destroy()

    # -----------------------------------------------------------------------
    # Fan control
    # -----------------------------------------------------------------------

    def _toggle_control(self):
        if self.fan_control_enabled:
            self._disable_fan_control()
        else:
            self._enable_fan_control()

    def _enable_fan_control(self):
        ok, err = self.helper.start()
        if not ok:
            messagebox.showwarning(
                "Fan Control Error",
                "Could not start fan control helper.\n\n"
                "Make sure passwordless sudo is set up:\n"
                "  sudo bash ~/gpu_control/setup_sudoers.sh\n\n"
                f"Error: {err}"
            )
            return
        self.fan_control_enabled = True
        self._update_status()

    def _disable_fan_control(self):
        if self.fan_control_enabled:
            self.helper.reset_all()
            self.helper.stop()
        self.fan_control_enabled = False
        self._update_status()

    def _update_status(self):
        if self.fan_control_enabled:
            self.status_label.config(text="ACTIVE", fg=GREEN)
            self.toggle_btn.config(text="Disable", bg=RED)
        else:
            self.status_label.config(text="DISABLED", fg=FG_DIM)
            self.toggle_btn.config(text="Enable", bg=ACCENT)

    # -----------------------------------------------------------------------
    # Polling (runs on tkinter's after loop - no threading issues)
    # -----------------------------------------------------------------------

    def _poll(self):
        if not self.running:
            return
        try:
            poll_gpu_stats(self.gpus)

            if self.fan_control_enabled:
                for gpu in self.gpus:
                    idx = gpu["index"]
                    state = self.gpu_states.get(idx)
                    if not state:
                        continue
                    if state["mode"] == "auto":
                        target = interpolate_curve(state["curve"], gpu["temp"])
                    else:
                        target = state["manual_speed"]
                    state["current_speed"] = target
                    for fan in range(gpu["num_fans"]):
                        self.helper.set_fan(idx, fan, target)

            self._update_readings()
        except Exception:
            pass

        self.root.after(POLL_INTERVAL_MS, self._poll)

    def _update_readings(self):
        for gpu in self.gpus:
            idx = gpu["index"]
            w = self.gui_widgets.get(idx)
            if not w:
                continue
            temp = gpu["temp"]
            fan = gpu["fan_speed"]

            w["temp_label"].config(text=f"{temp}\u00b0C", fg=temp_color(temp))
            w["fan_label"].config(text=f"{fan}%")
            w["fan_bar"].place_configure(relwidth=max(0.01, fan / 100))

            if fan >= 80: bar_color = RED
            elif fan >= 60: bar_color = ORANGE
            elif fan >= 40: bar_color = YELLOW
            else: bar_color = GREEN
            w["fan_bar"].config(bg=bar_color)

            mem_used = gpu.get("mem_used", 0.0)
            mem_total = gpu.get("mem_total", 0.0)
            w["mem_label"].config(text=f"{mem_used:.1f} / {mem_total:.0f} GB")

            power_usage = gpu.get("power_usage", 0.0)
            power_limit = gpu.get("power_limit", 0.0)
            w["pwr_label"].config(text=f"{power_usage:.0f} / {power_limit:.0f} W")

            self._draw_curve(idx)

    # -----------------------------------------------------------------------
    # Mode / curve controls
    # -----------------------------------------------------------------------

    def _show_mode_frame(self, gpu_idx):
        w = self.gui_widgets[gpu_idx]
        if self.gpu_states[gpu_idx]["mode"] == "manual":
            w["curve_frame"].pack_forget()
            w["manual_frame"].pack(fill="x")
        else:
            w["manual_frame"].pack_forget()
            w["curve_frame"].pack(fill="x")

    def _on_mode_change(self, gpu_idx):
        self.gpu_states[gpu_idx]["mode"] = self.gui_widgets[gpu_idx]["mode_var"].get()
        self._show_mode_frame(gpu_idx)
        self._save_config()

    def _on_manual_change(self, gpu_idx, value):
        self.gpu_states[gpu_idx]["manual_speed"] = value
        self.gui_widgets[gpu_idx]["manual_label"].config(text=f"{value}%")
        self._save_config()

    def _on_curve_change(self, gpu_idx):
        if gpu_idx in self._dragging:
            return
        w = self.gui_widgets[gpu_idx]
        curve = []
        try:
            for tv, sv in w["curve_entries"]:
                t = int(tv.get())
                s = max(0, min(100, int(sv.get())))
                curve.append((t, s))
            curve.sort(key=lambda p: p[0])
            self.gpu_states[gpu_idx]["curve"] = curve
            self._draw_curve(gpu_idx)
            self._save_config()
        except (ValueError, Exception):
            pass

    # -----------------------------------------------------------------------
    # Curve canvas drag interaction
    # -----------------------------------------------------------------------

    _CURVE_CW, _CURVE_CH, _CURVE_PAD = 260, 120, 20

    def _canvas_to_curve(self, x, y):
        """Convert canvas pixel coords to (temp, fan_speed) clamped 0-100."""
        pw = self._CURVE_CW - 2 * self._CURVE_PAD
        ph = self._CURVE_CH - 2 * self._CURVE_PAD
        t = max(0, min(100, round((x - self._CURVE_PAD) / pw * 100)))
        s = max(0, min(100, round((self._CURVE_CH - self._CURVE_PAD - y) / ph * 100)))
        return t, s

    def _curve_points_px(self, gpu_idx):
        """Return canvas pixel coords for all curve points."""
        pw = self._CURVE_CW - 2 * self._CURVE_PAD
        ph = self._CURVE_CH - 2 * self._CURVE_PAD
        return [
            (self._CURVE_PAD + (t / 100) * pw,
             self._CURVE_CH - self._CURVE_PAD - (s / 100) * ph)
            for t, s in self.gpu_states[gpu_idx]["curve"]
        ]

    def _curve_mouse_down(self, event, gpu_idx):
        """Select nearest curve point within 15px for dragging."""
        points = self._curve_points_px(gpu_idx)
        best_i, best_d = None, 15
        for i, (px, py) in enumerate(points):
            d = ((event.x - px) ** 2 + (event.y - py) ** 2) ** 0.5
            if d < best_d:
                best_d = d
                best_i = i
        self._drag_states[gpu_idx] = best_i

    def _curve_mouse_drag(self, event, gpu_idx):
        """Move the selected point, re-sort curve, redraw."""
        point_idx = self._drag_states.get(gpu_idx)
        if point_idx is None:
            return

        t, s = self._canvas_to_curve(event.x, event.y)
        self._dragging.add(gpu_idx)

        curve = list(self.gpu_states[gpu_idx]["curve"])
        curve[point_idx] = (t, s)
        curve.sort(key=lambda p: p[0])

        # Track the dragged point through the sort (in case it crossed another)
        for new_i, (ct, cs) in enumerate(curve):
            if ct == t and cs == s:
                self._drag_states[gpu_idx] = new_i
                break

        self.gpu_states[gpu_idx]["curve"] = curve
        self._draw_curve(gpu_idx)

    def _curve_mouse_up(self, event, gpu_idx):
        """Sync entry widgets with final dragged curve and save."""
        self._dragging.discard(gpu_idx)
        if self._drag_states.get(gpu_idx) is not None:
            curve = self.gpu_states[gpu_idx]["curve"]
            w = self.gui_widgets[gpu_idx]
            for j, (tv, sv) in enumerate(w["curve_entries"]):
                if j < len(curve):
                    tv.set(str(curve[j][0]))
                    sv.set(str(curve[j][1]))
            self._save_config()
        self._drag_states[gpu_idx] = None

    def _draw_curve(self, gpu_idx):
        w = self.gui_widgets.get(gpu_idx)
        if not w:
            return
        canvas = w["curve_canvas"]
        canvas.delete("all")

        cw, ch, pad = 260, 120, 20
        pw, ph = cw - 2 * pad, ch - 2 * pad

        for t in range(0, 101, 20):
            x = pad + (t / 100) * pw
            canvas.create_line(x, pad, x, ch - pad, fill=BORDER, dash=(2, 4))
            canvas.create_text(x, ch - 6, text=f"{t}", fill=FG_DIM, font=("Sans", 7))
        for s in range(0, 101, 25):
            y = ch - pad - (s / 100) * ph
            canvas.create_line(pad, y, cw - pad, y, fill=BORDER, dash=(2, 4))
            canvas.create_text(8, y, text=f"{s}", fill=FG_DIM, font=("Sans", 7), anchor="w")

        curve = self.gpu_states[gpu_idx]["curve"]
        if len(curve) < 2:
            return

        points = [(pad + (t / 100) * pw, ch - pad - (s / 100) * ph) for t, s in curve]

        for i in range(len(points) - 1):
            canvas.create_line(*points[i], *points[i + 1], fill=ACCENT, width=2)
        drag_i = self._drag_states.get(gpu_idx)
        for i, (x, y) in enumerate(points):
            if i == drag_i:
                canvas.create_oval(x - 6, y - 6, x + 6, y + 6,
                                   fill=GREEN, outline="white", width=1)
            else:
                canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                                   fill=ACCENT, outline="")

        # Current temp marker
        for gpu in self.gpus:
            if gpu["index"] == gpu_idx:
                temp = gpu["temp"]
                speed = interpolate_curve(curve, temp)
                tx = pad + (temp / 100) * pw
                ty = ch - pad - (speed / 100) * ph
                canvas.create_oval(tx - 5, ty - 5, tx + 5, ty + 5,
                                   fill=temp_color(temp), outline="white", width=1)
                break

    def _save_config(self):
        cfg = {}
        for idx, state in self.gpu_states.items():
            cfg[f"gpu_{idx}"] = {
                "mode": state["mode"],
                "manual_speed": state["manual_speed"],
                "curve": state["curve"],
            }
        save_config(cfg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    minimized = "--minimized" in sys.argv
    app = GPUFanControlApp(start_minimized=minimized)

    def signal_handler(sig, frame):
        app._on_quit()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    app.run()


if __name__ == "__main__":
    main()
