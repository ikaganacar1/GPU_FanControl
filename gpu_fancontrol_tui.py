#!/usr/bin/env python3
"""GPU Fan Control TUI - Headless terminal UI for dual GPU fan control.

Standalone Textual-based TUI with the same capabilities as gpu_fancontrol.py.
Uses pynvml via a root helper subprocess for fan control (no Coolbits needed).

Keyboard controls:
  e        Toggle fan control on/off
  m        Toggle mode (auto/manual)
  1-4      Select profile (Silent/Balanced/Aggressive/Max) in auto mode
  +/-      Adjust fan speed in manual mode
  q        Quit
"""

import subprocess
import signal
import sys
import os
import json
import threading
import time
import ctypes
from pathlib import Path

import pynvml
import psutil

from textual.app import App, ComposeResult
from textual.widgets import Static
from textual import work
from rich.text import Text
from rich.table import Table
from rich.console import Group

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
PYTHON = Path(sys.executable).resolve()
HELPER = SCRIPT_DIR / "fan_helper.py"
CONFIG_PATH = Path.home() / ".config" / "gpu-fancontrol" / "config.json"

PROFILES = {
    "Silent":     [(0, 30), (50, 30), (60, 35), (65, 42), (70, 55), (75, 72), (80, 100)],
    "Balanced":   [(0, 30), (50, 35), (60, 50), (65, 60), (70, 75), (75, 85), (80, 100)],
    "Aggressive": [(0, 40), (50, 50), (55, 65), (60, 75), (65, 85), (70, 95), (75, 100)],
    "Max":        [(0, 60), (40, 70), (50, 80), (55, 90), (60, 100), (70, 100), (80, 100)],
}

PROFILE_KEYS = list(PROFILES.keys())

POLL_INTERVAL_S = 3

# Catppuccin Mocha
ACCENT = "#89b4fa"
GREEN = "#a6e3a1"
YELLOW = "#f9e2af"
RED = "#f38ba8"
ORANGE = "#fab387"
DIM = "#6c7086"
FG = "#cdd6f4"
BG = "#1e1e2e"
BORDER_CLR = "#45475a"

# ---------------------------------------------------------------------------
# GPU detection via pynvml
# ---------------------------------------------------------------------------

def get_fan_min_max(handle) -> tuple[int, int]:
    try:
        mn, mx = ctypes.c_uint(), ctypes.c_uint()
        pynvml.nvmlDeviceGetMinMaxFanSpeed(handle, ctypes.byref(mn), ctypes.byref(mx))
        return mn.value, mx.value
    except Exception:
        return 0, 100


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
        fan_min, fan_max = get_fan_min_max(h)
        gpus.append({
            "index": i, "name": name, "temp": temp,
            "fan_speed": fan_speed, "num_fans": num_fans,
            "mem_used": mem_used, "mem_total": mem_total,
            "power_usage": power_usage, "power_limit": power_limit,
            "fan_min": fan_min, "fan_max": fan_max,
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
# Helpers
# ---------------------------------------------------------------------------

def temp_color(temp: float) -> str:
    if temp >= 80: return RED
    if temp >= 70: return ORANGE
    if temp >= 60: return YELLOW
    if temp >= 50: return ACCENT
    return GREEN


def bar(value: float, max_val: float, width: int = 20, color: str = GREEN) -> Text:
    if max_val <= 0:
        filled = 0
    else:
        filled = int(value / max_val * width)
    filled = max(0, min(width, filled))
    empty = width - filled
    t = Text()
    t.append("\u2588" * filled, style=color)
    t.append("\u2591" * empty, style=DIM)
    return t


def get_cpu_temp() -> float:
    try:
        temps = psutil.sensors_temperatures()
        for sensor in ("k10temp", "coretemp", "cpu_thermal", "acpitz"):
            if sensor not in temps:
                continue
            entries = temps[sensor]
            for preferred in ("Tctl", "Tccd1", "Package id 0", ""):
                for e in entries:
                    if preferred in e.label:
                        return e.current
    except Exception:
        pass
    return 0.0


def format_speed(bps: float) -> str:
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def poll_sys_stats(stats: dict, last_net, last_time: float):
    stats["cpu_temp"] = get_cpu_temp()
    try:
        freq = psutil.cpu_freq()
        stats["cpu_freq"] = freq.current / 1000.0
        stats["cpu_freq_max"] = freq.max / 1000.0
    except Exception:
        pass
    mem = psutil.virtual_memory()
    stats["ram_used"] = mem.used / (1024 ** 3)
    stats["ram_total"] = mem.total / (1024 ** 3)
    stats["ram_percent"] = mem.percent
    try:
        net = psutil.net_io_counters()
        now = time.time()
        dt = now - last_time
        if dt > 0 and last_net is not None:
            stats["net_down"] = max(0.0, (net.bytes_recv - last_net.bytes_recv) / dt)
            stats["net_up"]   = max(0.0, (net.bytes_sent - last_net.bytes_sent) / dt)
        return net, now
    except Exception:
        return last_net, last_time

# ---------------------------------------------------------------------------
# Rich rendering — single table grid
# ---------------------------------------------------------------------------

def render_all(gpus, sys_stats, fan_state, fan_control_enabled, target_speed):
    """Render the entire UI as a single Rich Table grid."""
    num_gpus = len(gpus)
    cols = max(num_gpus, 2)

    tbl = Table(
        show_header=False, show_footer=False,
        border_style=BORDER_CLR, expand=True,
        padding=(0, 1), show_lines=True,
    )
    for _ in range(cols):
        tbl.add_column(ratio=1)

    # ── Row 1: Header ──
    header = Text()
    header.append(" GPU Fan Control ", style=f"bold {ACCENT}")
    header.append("   ")
    if fan_control_enabled:
        header.append(" ACTIVE ", style=f"bold {BG} on {GREEN}")
    else:
        header.append(" DISABLED ", style=f"bold {FG} on {DIM}")
    tbl.add_row(header, end_section=True)

    # ── Row 2: System stats ──
    s = sys_stats
    cpu_t = s["cpu_temp"]
    cpu_c = temp_color(cpu_t)
    ram_pct = s["ram_percent"]
    ram_c = RED if ram_pct >= 90 else ORANGE if ram_pct >= 75 else YELLOW if ram_pct >= 50 else GREEN
    freq = s["cpu_freq"]
    freq_max = s["cpu_freq_max"]

    sys_left = Text()
    sys_left.append("CPU ", style=f"bold {DIM}")
    sys_left.append(f"{cpu_t:.0f}\u00b0C", style=f"bold {cpu_c}")
    sys_left.append("   RAM ", style=f"bold {DIM}")
    sys_left.append(f"{s['ram_used']:.1f}/{s['ram_total']:.0f}GB ", style=FG)
    sys_left.append(f"{ram_pct:.0f}%", style=ram_c)
    sys_left.append(" ")
    sys_left.append_text(bar(ram_pct, 100, 10, ram_c))
    sys_left.append("\n")
    sys_left.append("FREQ ", style=f"bold {DIM}")
    sys_left.append(f"{freq:.2f}/{freq_max:.2f}GHz ", style=FG)
    sys_left.append_text(bar(freq, freq_max, 10, ACCENT))

    sys_right = Text()
    sys_right.append("\u2193 DL  ", style=f"bold {GREEN}")
    sys_right.append(format_speed(s["net_down"]), style=f"bold {GREEN}")
    sys_right.append("\n")
    sys_right.append("\u2191 UL  ", style=f"bold {ACCENT}")
    sys_right.append(format_speed(s["net_up"]), style=f"bold {ACCENT}")

    if cols == 2:
        tbl.add_row(sys_left, sys_right, end_section=True)
    else:
        tbl.add_row(sys_left, sys_right, *[""] * (cols - 2), end_section=True)

    # ── Row 3: GPU panels ──
    gpu_cells = []
    for gpu in gpus:
        fan = gpu["fan_speed"]
        fan_c = RED if fan >= 80 else ORANGE if fan >= 60 else YELLOW if fan >= 40 else GREEN
        tc = temp_color(gpu["temp"])
        short_name = gpu["name"].replace("NVIDIA GeForce ", "")
        fan_cnt = f"{gpu['num_fans']}fan"

        cell = Text()
        cell.append(f"{short_name}", style=f"bold {FG}")
        cell.append(f"  GPU{gpu['index']} {fan_cnt}", style=DIM)
        cell.append("\n")
        cell.append(f"{gpu['temp']}\u00b0C", style=f"bold {tc}")
        cell.append("  Fan ", style=DIM)
        cell.append(f"{fan}%", style=f"bold {fan_c}")
        cell.append(" ")
        cell.append_text(bar(fan, 100, 12, fan_c))
        cell.append("\n")
        cell.append("VRAM ", style=DIM)
        cell.append(f"{gpu['mem_used']:.1f}/{gpu['mem_total']:.0f}GB", style=FG)
        cell.append("  PWR ", style=DIM)
        cell.append(f"{gpu['power_usage']:.0f}/{gpu['power_limit']:.0f}W", style=FG)
        gpu_cells.append(cell)

    while len(gpu_cells) < cols:
        gpu_cells.append("")
    tbl.add_row(*gpu_cells, end_section=True)

    # ── Row 4: Fan control ──
    mode = fan_state["mode"]
    ctl = Text()
    ctl.append("Mode: ", style=DIM)
    if mode == "auto":
        ctl.append("[auto]", style=f"bold {GREEN}")
        ctl.append("  manual", style=DIM)
    else:
        ctl.append(" auto ", style=DIM)
        ctl.append(" [manual]", style=f"bold {ORANGE}")
    ctl.append("   ")
    ctl.append("[m]", style=f"bold {ACCENT}")
    ctl.append(" switch", style=DIM)

    if target_speed > 0 and fan_control_enabled:
        ctl.append("   Target: ", style=DIM)
        ctl.append(f"{target_speed}%", style=f"bold {GREEN}")

    ctl.append("\n")

    if mode == "auto":
        profile = fan_state.get("profile", "Balanced")
        for i, pname in enumerate(PROFILE_KEYS):
            ctl.append(f"[{i+1}]", style=f"bold {ACCENT}")
            if pname == profile:
                ctl.append(f"[{pname}]", style=f"bold {BG} on {ACCENT}")
            else:
                ctl.append(f" {pname} ", style=DIM)
            ctl.append("  ")
    else:
        speed = fan_state["manual_speed"]
        ctl.append("Speed: ", style=DIM)
        ctl.append(f"{speed}%  ", style=f"bold {ACCENT}")
        ctl.append_text(bar(speed, 100, 20, ACCENT))
        ctl.append("  ")
        ctl.append("[-]", style=f"bold {ACCENT}")
        ctl.append("/")
        ctl.append("[+]", style=f"bold {ACCENT}")

    tbl.add_row(ctl, end_section=True)

    # ── Row 5: Help line ──
    helpline = Text()
    helpline.append("[e]", style=f"bold {ACCENT}")
    helpline.append("toggle  ", style=DIM)
    helpline.append("[m]", style=f"bold {ACCENT}")
    helpline.append("mode  ", style=DIM)
    helpline.append("[1-4]", style=f"bold {ACCENT}")
    helpline.append("profile  ", style=DIM)
    helpline.append("[+/-]", style=f"bold {ACCENT}")
    helpline.append("speed  ", style=DIM)
    helpline.append("[q]", style=f"bold {ACCENT}")
    helpline.append("quit", style=DIM)
    tbl.add_row(helpline)

    return tbl


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class GPUFanControlTUI(App):
    CSS = """
    Screen {
        background: """ + BG + """;
    }
    #display {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
    }
    """

    BINDINGS = [
        ("q", "quit_app", "Quit"),
        ("e", "toggle_control", "Enable/Disable"),
        ("m", "toggle_mode", "Mode"),
        ("1", "profile_1", "Silent"),
        ("2", "profile_2", "Balanced"),
        ("3", "profile_3", "Aggressive"),
        ("4", "profile_4", "Max"),
        ("plus,equal", "speed_up", "+Speed"),
        ("minus,underscore", "speed_down", "-Speed"),
    ]

    def __init__(self):
        super().__init__()
        self.gpus = detect_gpus()
        self.fan_control_enabled = False
        self.helper = FanHelper()
        self.target_speed = 0

        self._sys_stats = {
            "cpu_temp": 0.0,
            "cpu_freq": 0.0, "cpu_freq_max": 0.0,
            "ram_used": 0.0, "ram_total": 0.0, "ram_percent": 0.0,
            "net_down": 0.0, "net_up": 0.0,
        }
        try:
            self._last_net = psutil.net_io_counters()
        except Exception:
            self._last_net = None
        self._last_net_time = time.time()

        # Global fan state (applies to all GPUs)
        cfg = load_config()
        saved = cfg.get("fan_state", cfg.get("gpu_0", {}))
        self.fan_state = {
            "mode": saved.get("mode", "auto"),
            "manual_speed": saved.get("manual_speed", 50),
            "curve": [tuple(p) for p in saved.get("curve", PROFILES["Balanced"])],
            "profile": saved.get("profile", "Balanced"),
        }

    def compose(self) -> ComposeResult:
        yield Static(id="display")

    def on_mount(self) -> None:
        self.set_interval(POLL_INTERVAL_S, self._poll)
        self._poll()

    # -------------------------------------------------------------------
    # Rendering
    # -------------------------------------------------------------------

    def _render_display(self) -> None:
        display = self.query_one("#display", Static)
        tbl = render_all(
            self.gpus, self._sys_stats, self.fan_state,
            self.fan_control_enabled, self.target_speed,
        )
        display.update(tbl)

    # -------------------------------------------------------------------
    # Polling
    # -------------------------------------------------------------------

    @work(thread=True)
    def _poll(self) -> None:
        try:
            poll_gpu_stats(self.gpus)
            self._last_net, self._last_net_time = poll_sys_stats(
                self._sys_stats, self._last_net, self._last_net_time)

            if self.fan_control_enabled:
                state = self.fan_state
                for gpu in self.gpus:
                    if state["mode"] == "auto":
                        target = interpolate_curve(state["curve"], gpu["temp"])
                    else:
                        target = state["manual_speed"]
                    target = max(gpu.get("fan_min", 0),
                                 min(gpu.get("fan_max", 100), target))
                    self.target_speed = target
                    for fan in range(gpu["num_fans"]):
                        self.helper.set_fan(gpu["index"], fan, target)
            else:
                self.target_speed = 0

            self.call_from_thread(self._render_display)
        except Exception:
            pass

    # -------------------------------------------------------------------
    # Fan control toggle
    # -------------------------------------------------------------------

    def action_toggle_control(self) -> None:
        if self.fan_control_enabled:
            self._disable_fan_control()
        else:
            self._enable_fan_control()
        self._render_display()

    def _enable_fan_control(self) -> None:
        ok, err = self.helper.start()
        if not ok:
            self.notify(
                f"Could not start fan helper. Ensure passwordless sudo.\n{err}",
                severity="error", timeout=8,
            )
            return
        self.fan_control_enabled = True

    def _disable_fan_control(self) -> None:
        if self.fan_control_enabled:
            self.helper.reset_all()
            self.helper.stop()
        self.fan_control_enabled = False
        self.target_speed = 0

    # -------------------------------------------------------------------
    # Mode / Profile / Speed
    # -------------------------------------------------------------------

    def action_toggle_mode(self) -> None:
        self.fan_state["mode"] = "manual" if self.fan_state["mode"] == "auto" else "auto"
        self._save_config()
        self._render_display()

    def _apply_profile(self, profile_idx: int) -> None:
        if self.fan_state["mode"] != "auto":
            return
        pname = PROFILE_KEYS[profile_idx]
        self.fan_state["curve"] = [tuple(p) for p in PROFILES[pname]]
        self.fan_state["profile"] = pname
        self._save_config()
        self._render_display()

    def action_profile_1(self) -> None:
        self._apply_profile(0)

    def action_profile_2(self) -> None:
        self._apply_profile(1)

    def action_profile_3(self) -> None:
        self._apply_profile(2)

    def action_profile_4(self) -> None:
        self._apply_profile(3)

    def action_speed_up(self) -> None:
        self._adjust_speed(5)

    def action_speed_down(self) -> None:
        self._adjust_speed(-5)

    def _adjust_speed(self, delta: int) -> None:
        if self.fan_state["mode"] != "manual":
            return
        speed = self.fan_state["manual_speed"]
        self.fan_state["manual_speed"] = max(0, min(100, speed + delta))
        self._save_config()
        self._render_display()

    # -------------------------------------------------------------------
    # Config
    # -------------------------------------------------------------------

    def _save_config(self) -> None:
        cfg = {
            "fan_state": {
                "mode": self.fan_state["mode"],
                "manual_speed": self.fan_state["manual_speed"],
                "curve": self.fan_state["curve"],
                "profile": self.fan_state.get("profile"),
            }
        }
        save_config(cfg)

    # -------------------------------------------------------------------
    # Quit
    # -------------------------------------------------------------------

    def action_quit_app(self) -> None:
        if self.fan_control_enabled:
            self.helper.reset_all()
            self.helper.stop()
        self._save_config()
        self.exit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = GPUFanControlTUI()

    def signal_handler(sig, frame):
        if app.fan_control_enabled:
            app.helper.reset_all()
            app.helper.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    app.run()


if __name__ == "__main__":
    main()
