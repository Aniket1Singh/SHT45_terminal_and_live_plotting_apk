import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import time
import os
import re
import csv
from queue import Queue, Empty
from collections import deque
import bisect

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk


# ---------------- CONFIG ----------------
LOG_FILE_DEFAULT = r"C:\Users\Aniket\OneDrive\Desktop\Terminal log\terminal.log"

READ_POLL_SEC = 0.05
PLOT_REFRESH_MS = 120

MAX_POINTS = 250000          # keep a lot in memory (10h OK)
DRAW_MAX_POINTS = 2500       # draw at most this many (downsample above this)
MAX_OVERLAYS = 10

LABEL_INLET = "Sensor at inlet point"
LABEL_OUTLET = "Sensor at outlet point"

TEMP_MIN_C = -40.0
TEMP_MAX_C = 125.0
HUM_MIN_PCT = 0.0
HUM_MAX_PCT = 100.0

MAX_TEMP_STEP_C = 5.0
MAX_HUM_STEP_PCT = 20.0

EXPORT_MAX_DT_SEC = 0.30

VIEW_WINDOW_SEC = 180.0      # rolling window shown
RESCALE_EVERY_N = 30         # recompute limits every N new samples
Y_PAD_FRAC = 0.08
MIN_YSPAN = 0.3

STATIC_DT_SEC = 0.25

LINE_RE = re.compile(
    r"^\s*(?P<pc>\S+)\s+(?P<m>\d+):(?P<s>\d+)\.(?P<ms>\d+)\s*,\s*"
    r"(?P<sensor>Sensor\s*[12])\s*,\s*(?P<temp>-?\d+(?:\.\d+)?)\s*,\s*(?P<rh>-?\d+(?:\.\d+)?)\s*$"
)


def fmt_mmss(x, pos):
    x = max(0.0, float(x))
    m = int(x // 60)
    s = int(x % 60)
    return f"{m:02d}:{s:02d}"


def sec_to_mmss_mmm(tsec: float) -> str:
    tsec = max(0.0, float(tsec))
    total_ms = int(round(tsec * 1000.0))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    m = total_s // 60
    return f"{m:02d}:{s:02d}.{ms:03d}"


def to_seconds(m, s, ms):
    return int(m) * 60.0 + int(s) + int(ms) / 1000.0


def safe_float(x):
    try:
        if x is None:
            return None
        s = str(x).strip()
        if s == "" or s.lower() in ("nan", "none"):
            return None
        return float(s)
    except Exception:
        return None


def nearest_by_time(t_list, v_list, t, max_dt):
    if not t_list:
        return None
    i = bisect.bisect_left(t_list, t)
    candidates = []
    if i < len(t_list):
        candidates.append(i)
    if i > 0:
        candidates.append(i - 1)
    best = None
    best_dt = None
    for j in candidates:
        dt = abs(t_list[j] - t)
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = v_list[j]
    return best if (best_dt is not None and best_dt <= max_dt) else None


def downsample_xy(xs, ys, max_points):
    n = len(xs)
    if n <= max_points:
        return xs, ys
    stride = max(1, n // max_points)
    return xs[::stride], ys[::stride]


class StreamBuffers:
    def __init__(self, maxlen=MAX_POINTS):
        self.t1 = deque(maxlen=maxlen); self.T1 = deque(maxlen=maxlen); self.H1 = deque(maxlen=maxlen)
        self.t2 = deque(maxlen=maxlen); self.T2 = deque(maxlen=maxlen); self.H2 = deque(maxlen=maxlen)

    def clear(self):
        self.t1.clear(); self.T1.clear(); self.H1.clear()
        self.t2.clear(); self.T2.clear(); self.H2.clear()

    def push(self, t, sid, temp, hum):
        if sid == 1:
            self.t1.append(t); self.T1.append(temp); self.H1.append(hum)
        else:
            self.t2.append(t); self.T2.append(temp); self.H2.append(hum)


class OverlayItem:
    def __init__(self, name, color, maxlen=MAX_POINTS):
        self.name = name
        self.color = color
        self.buffers = StreamBuffers(maxlen=maxlen)
        self.path = None
        self.live = False
        self.running = False
        self.file_pos = 0
        self._carry = ""
        self.lines = None

        # time base for overlay relative seconds
        self.t_start_abs = None


class DataMonitor:
    def __init__(self):
        self.file_path = None
        self.running = False
        self.main = StreamBuffers(maxlen=MAX_POINTS)

        self.file_pos = 0
        self.queue = Queue()
        self.event_q = Queue()

        self._carry = ""
        self._last_good_temp = {1: None, 2: None}
        self._last_good_hum = {1: None, 2: None}

        self.overlays = []
        self.overlay_threads = []

        # relative time base: first sample -> 0
        self.main_t_start_abs = None

    def reset_main(self):
        self.main.clear()
        self.queue = Queue()
        self._carry = ""
        self._last_good_temp = {1: None, 2: None}
        self._last_good_hum = {1: None, 2: None}
        self.file_pos = 0
        self.main_t_start_abs = None

    def start(self):
        if not self.file_path:
            return
        self.running = True
        self.reset_main()
        threading.Thread(target=self.read_loop_main, daemon=True).start()

    def stop(self):
        self.running = False

    def _valid_ranges(self, temp, hum):
        return (TEMP_MIN_C <= temp <= TEMP_MAX_C) and (HUM_MIN_PCT <= hum <= HUM_MAX_PCT)

    def _valid_step(self, sid, temp, hum):
        lt = self._last_good_temp[sid]
        lh = self._last_good_hum[sid]
        if lt is None or lh is None:
            return True
        if MAX_TEMP_STEP_C is not None and abs(temp - lt) > MAX_TEMP_STEP_C:
            return False
        if MAX_HUM_STEP_PCT is not None and abs(hum - lh) > MAX_HUM_STEP_PCT:
            return False
        return True

    def parse_log_line_abs_seconds(self, s: str):
        """
        Return (t_abs_sec, sid, temp, hum).
        """
        if not s:
            return None
        if "TS,Sensor,TempC,RH" in s:
            return None

        parts = [p.strip() for p in s.split(",")]
        if len(parts) in (4, 5):
            try:
                # support your exported CSV rows too (either t in col0 or col1)
                if len(parts) == 5:
                    t_abs = float(parts[1]); sensor_txt = parts[2]; temp = float(parts[3]); hum = float(parts[4])
                else:
                    t_abs = float(parts[0]); sensor_txt = parts[1]; temp = float(parts[2]); hum = float(parts[3])
                if sensor_txt.lower().startswith("sensor"):
                    sid = 1 if sensor_txt.replace(" ", "").endswith("1") else 2
                    if not self._valid_ranges(temp, hum):
                        return None
                    if not self._valid_step(sid, temp, hum):
                        return None
                    self._last_good_temp[sid] = temp
                    self._last_good_hum[sid] = hum
                    return (t_abs, sid, temp, hum)
            except Exception:
                pass

        m = LINE_RE.match(s)
        if not m:
            return None

        t_abs = to_seconds(m.group("m"), m.group("s"), m.group("ms"))
        sid = 1 if m.group("sensor").replace(" ", "").endswith("1") else 2
        temp = float(m.group("temp"))
        hum = float(m.group("rh"))

        if not self._valid_ranges(temp, hum):
            return None
        if not self._valid_step(sid, temp, hum):
            return None

        self._last_good_temp[sid] = temp
        self._last_good_hum[sid] = hum
        return (t_abs, sid, temp, hum)

    def read_loop_main(self):
        while self.running and self.file_path and not os.path.exists(self.file_path):
            time.sleep(READ_POLL_SEC)

        last_size = 0
        while self.running and self.file_path:
            try:
                try:
                    size = os.path.getsize(self.file_path)
                except FileNotFoundError:
                    time.sleep(READ_POLL_SEC)
                    continue

                truncated = (size < self.file_pos) or (size < last_size)
                if truncated:
                    self.file_pos = 0
                    self._carry = ""
                    self._last_good_temp = {1: None, 2: None}
                    self._last_good_hum = {1: None, 2: None}
                    self.main_t_start_abs = None
                    self.event_q.put(("RESET", "Main file truncated/cleared"))
                last_size = size

                with open(self.file_path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(self.file_pos)
                    chunk = f.read()
                    self.file_pos = f.tell()

                if chunk:
                    chunk = self._carry + chunk
                    lines = chunk.splitlines(keepends=True)

                    if lines and (not lines[-1].endswith("\n")) and (not lines[-1].endswith("\r")):
                        self._carry = lines.pop(-1)
                    else:
                        self._carry = ""

                    for ln in lines:
                        parsed = self.parse_log_line_abs_seconds(ln.strip())
                        if parsed is not None:
                            self.queue.put(parsed)

            except Exception as e:
                self.event_q.put(("ERROR", str(e)))

            time.sleep(READ_POLL_SEC)

    # ---------- overlays ----------
    def add_overlay(self, name, color):
        if len(self.overlays) >= MAX_OVERLAYS:
            raise RuntimeError(f"Max overlays reached ({MAX_OVERLAYS}).")
        ov = OverlayItem(name=name, color=color, maxlen=MAX_POINTS)
        self.overlays.append(ov)
        return ov

    def remove_overlay(self, ov: OverlayItem):
        ov.running = False
        if ov in self.overlays:
            self.overlays.remove(ov)

    def clear_overlays(self):
        for ov in list(self.overlays):
            ov.running = False
        self.overlays.clear()

    def start_live_overlay(self, ov: OverlayItem, path: str):
        ov.path = path
        ov.live = True
        ov.running = True
        ov.file_pos = 0
        ov._carry = ""
        ov.t_start_abs = None

        def _tail():
            while ov.running and ov.path and not os.path.exists(ov.path):
                time.sleep(READ_POLL_SEC)

            last_size = 0
            while ov.running and ov.path:
                try:
                    try:
                        size = os.path.getsize(ov.path)
                    except FileNotFoundError:
                        time.sleep(READ_POLL_SEC)
                        continue

                    if size < ov.file_pos or size < last_size:
                        ov.file_pos = 0
                        ov._carry = ""
                        ov.t_start_abs = None
                    last_size = size

                    with open(ov.path, "r", encoding="utf-8", errors="ignore") as f:
                        f.seek(ov.file_pos)
                        chunk = f.read()
                        ov.file_pos = f.tell()

                    if chunk:
                        chunk = ov._carry + chunk
                        lines = chunk.splitlines(keepends=True)

                        if lines and (not lines[-1].endswith("\n")) and (not lines[-1].endswith("\r")):
                            ov._carry = lines.pop(-1)
                        else:
                            ov._carry = ""

                        for ln in lines:
                            parsed = self.parse_log_line_abs_seconds(ln.strip())
                            if parsed is None:
                                continue
                            t_abs, sid, temp, hum = parsed
                            if ov.t_start_abs is None:
                                ov.t_start_abs = t_abs
                            t_rel = t_abs - ov.t_start_abs
                            ov.buffers.push(t_rel, sid, temp, hum)
                except Exception:
                    pass

                time.sleep(READ_POLL_SEC)

        th = threading.Thread(target=_tail, daemon=True)
        self.overlay_threads.append(th)
        th.start()


class LivePlotterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("2x2 Live Plotter (fast rolling window + overlays)")
        self.root.geometry("1450x930")
        self.root.configure(bg="#0B1220")

        self.monitor = DataMonitor()
        self._new_points_since_rescale = 0
        self._dirty = True

        self.overlay_color_cycle = [
            "#64748B", "#DC2626", "#0EA5E9", "#16A34A", "#F97316",
            "#A855F7", "#14B8A6", "#EAB308", "#EC4899", "#334155"
        ]

        # Queue for background overlay loading (UI remains responsive). [web:400]
        self.overlay_load_q = Queue()

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Top.TFrame", background="#0B1220")
        style.configure("Card.TLabelframe", background="#0B1220", foreground="#E5E7EB")
        style.configure("Card.TLabelframe.Label", background="#0B1220", foreground="#E5E7EB")
        style.configure("Top.TLabel", background="#0B1220", foreground="#E5E7EB")
        style.configure("Top.TButton", padding=(10, 6))

        top = ttk.Frame(root, style="Top.TFrame", padding=10)
        top.pack(fill=tk.X)

        file_box = ttk.LabelFrame(top, text="Main log file", style="Card.TLabelframe", padding=10)
        file_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        ttk.Button(file_box, text="Load…", style="Top.TButton", command=self.load_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(file_box, text="Default", style="Top.TButton", command=self.use_default).pack(side=tk.LEFT, padx=4)

        self.file_var = tk.StringVar(value="(no file selected)")
        ttk.Label(file_box, textvariable=self.file_var, style="Top.TLabel", width=50).pack(side=tk.LEFT, padx=10)

        ctrl_box = ttk.LabelFrame(top, text="Main controls", style="Card.TLabelframe", padding=10)
        ctrl_box.pack(side=tk.LEFT, padx=(0, 10))

        self.status_var = tk.StringVar(value="idle")
        ttk.Button(ctrl_box, text="Start main", style="Top.TButton", command=self.start).pack(side=tk.LEFT, padx=4)
        ttk.Button(ctrl_box, text="Stop main", style="Top.TButton", command=self.stop).pack(side=tk.LEFT, padx=4)
        ttk.Button(ctrl_box, text="Reset main", style="Top.TButton", command=self.reset_main).pack(side=tk.LEFT, padx=4)

        ttk.Label(ctrl_box, text="Status:", style="Top.TLabel").pack(side=tk.LEFT, padx=(10, 3))
        ttk.Label(ctrl_box, textvariable=self.status_var, style="Top.TLabel").pack(side=tk.LEFT)

        row2 = ttk.Frame(root, style="Top.TFrame", padding=(10, 0, 10, 8))
        row2.pack(fill=tk.X)

        ov_box = ttk.LabelFrame(row2, text=f"Overlays (max {MAX_OVERLAYS})", style="Card.TLabelframe", padding=8)
        ov_box.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.ov_list = tk.Listbox(ov_box, selectmode=tk.MULTIPLE, height=4)
        self.ov_list.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        btns = ttk.Frame(ov_box, style="Top.TFrame")
        btns.pack(side=tk.LEFT)

        ttk.Button(btns, text="Add static CSV…", style="Top.TButton", command=self.add_overlay_static_csv).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="Add live log…", style="Top.TButton", command=self.add_overlay_live).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="Remove selected", style="Top.TButton", command=self.remove_selected_overlays).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="Clear all", style="Top.TButton", command=self.clear_all_overlays).pack(fill=tk.X, pady=2)

        right_box = ttk.Frame(row2, style="Top.TFrame")
        right_box.pack(side=tk.RIGHT)

        ttk.Button(right_box, text="Export CSV…", style="Top.TButton", command=self.export_csv).pack(side=tk.LEFT, padx=4)

        ttk.Button(right_box, text="Home (rolling)", style="Top.TButton", command=self.home_rolling).pack(side=tk.LEFT, padx=4)
        ttk.Button(right_box, text="Full view", style="Top.TButton", command=self.home_full).pack(side=tk.LEFT, padx=4)

        self.latest_var = tk.StringVar(value="Latest: --")
        ttk.Label(right_box, textvariable=self.latest_var, style="Top.TLabel").pack(side=tk.LEFT, padx=(12, 0))

        self.hover_var = tk.StringVar(value="Hover: --")
        ttk.Label(right_box, textvariable=self.hover_var, style="Top.TLabel").pack(side=tk.LEFT, padx=(12, 0))

        plt.style.use("seaborn-v0_8-whitegrid")

        plot_frame = ttk.Frame(root, padding=(10, 6, 10, 10))
        plot_frame.pack(fill=tk.BOTH, expand=True)

        self.fig, self.ax = plt.subplots(2, 2, figsize=(12.2, 7.4), sharex=True)
        self.fig.patch.set_facecolor("#F8FAFC")

        for r in range(2):
            for c in range(2):
                self.ax[r][c].set_facecolor("#FFFFFF")
                self.ax[r][c].xaxis.set_major_formatter(FuncFormatter(fmt_mmss))

        self.ax_in_T = self.ax[0][0]
        self.ax_in_H = self.ax[0][1]
        self.ax_out_T = self.ax[1][0]
        self.ax_out_H = self.ax[1][1]

        self.ax_in_T.set_title(f"{LABEL_INLET} — Temperature")
        self.ax_in_H.set_title(f"{LABEL_INLET} — Humidity")
        self.ax_out_T.set_title(f"{LABEL_OUTLET} — Temperature")
        self.ax_out_H.set_title(f"{LABEL_OUTLET} — Humidity")

        (self.in_T_line,) = self.ax_in_T.plot([], [], lw=2.0, color="#2563EB", label="Inlet Temp")
        (self.in_H_line,) = self.ax_in_H.plot([], [], lw=2.0, color="#10B981", label="Inlet Hum")
        (self.out_T_line,) = self.ax_out_T.plot([], [], lw=2.0, color="#7C3AED", label="Outlet Temp")
        (self.out_H_line,) = self.ax_out_H.plot([], [], lw=2.0, color="#F59E0B", label="Outlet Hum")

        self.ax_in_T.set_ylabel("Temp (°C)")
        self.ax_out_T.set_ylabel("Temp (°C)")
        self.ax_in_H.set_ylabel("Hum (%)")
        self.ax_out_H.set_ylabel("Hum (%)")
        self.ax_out_T.set_xlabel("Time (mm:ss)")
        self.ax_out_H.set_xlabel("Time (mm:ss)")

        for a in (self.ax_in_T, self.ax_in_H, self.ax_out_T, self.ax_out_H):
            a.legend(loc="best")

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Toolbar gives Zoom-rect + Pan + Back/Forward + Home shortcuts. [web:278]
        toolbar_frame = ttk.Frame(root, padding=(10, 0, 10, 10))
        toolbar_frame.pack(fill=tk.X)
        self.toolbar = NavigationToolbar2Tk(self.canvas, toolbar_frame, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Hover behavior
        self.canvas.mpl_connect("motion_notify_event", self._on_motion_clear_if_outside)
        self._cursor = None
        self._hover_map = {}
        self.update_hover_bindings()

        self._full_view = False
        self.update_plot()

    # ---------- file + main controls ----------
    def use_default(self):
        self.monitor.file_path = LOG_FILE_DEFAULT
        self.file_var.set(self.monitor.file_path)

    def load_file(self):
        path = filedialog.askopenfilename(
            title="Select main log file",
            filetypes=[("Log/Text/CSV", "*.log *.txt *.csv"), ("All files", "*.*")]
        )
        if path:
            self.monitor.file_path = path
            self.file_var.set(path)

    def start(self):
        if not self.monitor.file_path:
            messagebox.showwarning("No file", "Select a main log file (Load…/Default).")
            return
        self.monitor.start()
        self.status_var.set("running (main)")
        self._new_points_since_rescale = 0
        self._dirty = True

    def stop(self):
        self.monitor.stop()
        self.status_var.set("stopped (main)")

    def reset_main(self):
        self.monitor.reset_main()
        self._new_points_since_rescale = 0
        self._dirty = True
        self._apply_main_lines()
        self._apply_overlay_lines()
        self._apply_limits()
        self.canvas.draw_idle()

    # ---------- overlays ----------
    def add_overlay_static_csv(self):
        if len(self.monitor.overlays) >= MAX_OVERLAYS:
            messagebox.showwarning("Max overlays", f"Max overlays reached: {MAX_OVERLAYS}")
            return

        path = filedialog.askopenfilename(
            title="Select exported CSV (static overlay)",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return

        idx = len(self.monitor.overlays)
        color = self.overlay_color_cycle[idx % len(self.overlay_color_cycle)]
        name = f"OV{idx+1} static: {os.path.basename(path)}"

        ov = self.monitor.add_overlay(name=name, color=color)
        self._create_overlay_lines(ov)
        self.ov_list.insert(tk.END, ov.name)

        # Parse in background to avoid UI lag. [web:400]
        threading.Thread(target=self._load_static_csv_worker, args=(ov, path), daemon=True).start()
        self.status_var.set("loading overlay...")

    def _load_static_csv_worker(self, ov: OverlayItem, path: str):
        ts_col = "Timestamp (MM:SS.mmm)"
        inlet_T_col = f"{LABEL_INLET} Temp (°C)"
        inlet_H_col = f"{LABEL_INLET} Hum (%)"
        outlet_T_col = f"{LABEL_OUTLET} Temp (°C)"
        outlet_H_col = f"{LABEL_OUTLET} Hum (%)"

        def parse_ts_mmss_mmm(s):
            if s is None:
                return None
            s = str(s).strip()
            m = re.match(r"^\s*(\d+):(\d+)\.(\d+)\s*$", s)
            if not m:
                return None
            mm = int(m.group(1)); ss = int(m.group(2)); ms = int(m.group(3))
            return mm * 60.0 + ss + ms / 1000.0

        t1 = []; T1 = []; H1 = []
        t2 = []; T2 = []; H2 = []

        with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            r = csv.DictReader(f)
            if not r.fieldnames:
                raise ValueError("CSV has no header row.")
            missing = [c for c in (inlet_T_col, inlet_H_col, outlet_T_col, outlet_H_col) if c not in r.fieldnames]
            if missing:
                raise ValueError("Static overlay CSV missing columns: " + ", ".join(missing))
            has_ts = ts_col in r.fieldnames

            idx = 0
            for row in r:
                if has_ts:
                    t = parse_ts_mmss_mmm(row.get(ts_col))
                    if t is None:
                        t = idx * STATIC_DT_SEC
                else:
                    t = idx * STATIC_DT_SEC
                idx += 1

                Tin = safe_float(row.get(inlet_T_col)); Hin = safe_float(row.get(inlet_H_col))
                Tout = safe_float(row.get(outlet_T_col)); Hout = safe_float(row.get(outlet_H_col))

                if Tin is not None and Hin is not None:
                    t1.append(t); T1.append(Tin); H1.append(Hin)
                if Tout is not None and Hout is not None:
                    t2.append(t); T2.append(Tout); H2.append(Hout)

        # push parsed arrays back to GUI thread
        self.overlay_load_q.put(("STATIC_LOADED", ov, t1, T1, H1, t2, T2, H2))

    def add_overlay_live(self):
        if len(self.monitor.overlays) >= MAX_OVERLAYS:
            messagebox.showwarning("Max overlays", f"Max overlays reached: {MAX_OVERLAYS}")
            return

        path = filedialog.askopenfilename(
            title="Select overlay log file (LIVE tail)",
            filetypes=[("Log/Text/CSV", "*.log *.txt *.csv"), ("All files", "*.*")]
        )
        if not path:
            return

        idx = len(self.monitor.overlays)
        color = self.overlay_color_cycle[idx % len(self.overlay_color_cycle)]
        name = f"OV{idx+1} LIVE: {os.path.basename(path)}"

        ov = self.monitor.add_overlay(name=name, color=color)
        self._create_overlay_lines(ov)
        self.ov_list.insert(tk.END, ov.name)

        self.monitor.start_live_overlay(ov, path)
        self.update_hover_bindings()
        self._dirty = True

    def remove_selected_overlays(self):
        sel = list(self.ov_list.curselection())
        if not sel:
            return
        for i in sel[::-1]:
            ov = self.monitor.overlays[i]
            ov.running = False
            self._remove_overlay_lines(ov)
            self.monitor.remove_overlay(ov)
            self.ov_list.delete(i)
        self.update_hover_bindings()
        self._dirty = True

    def clear_all_overlays(self):
        for ov in list(self.monitor.overlays):
            ov.running = False
            self._remove_overlay_lines(ov)
        self.monitor.clear_overlays()
        self.ov_list.delete(0, tk.END)
        self.update_hover_bindings()
        self._dirty = True

    # ---------- hover ----------
    def _on_motion_clear_if_outside(self, event):
        if event.inaxes is None:
            self.hover_var.set("Hover: --")

    def update_hover_bindings(self):
        try:
            import mplcursors
        except Exception:
            self.hover_var.set("Hover: install mplcursors (pip install mplcursors)")
            self._cursor = None
            return

        artists = []
        self._hover_map.clear()

        def add_artist(artist, src, ch):
            if artist is None:
                return
            artists.append(artist)
            self._hover_map[artist] = (src, ch)

        add_artist(self.in_T_line, "MAIN", "Inlet Temp")
        add_artist(self.in_H_line, "MAIN", "Inlet Hum")
        add_artist(self.out_T_line, "MAIN", "Outlet Temp")
        add_artist(self.out_H_line, "MAIN", "Outlet Hum")

        for ov in self.monitor.overlays:
            if ov.lines:
                add_artist(ov.lines["inT"], ov.name, "Inlet Temp")
                add_artist(ov.lines["inH"], ov.name, "Inlet Hum")
                add_artist(ov.lines["outT"], ov.name, "Outlet Temp")
                add_artist(ov.lines["outH"], ov.name, "Outlet Hum")

        if not artists:
            return

        try:
            if self._cursor is not None:
                self._cursor.remove()
        except Exception:
            pass

        # hover=2 transient hover (disappears when leaving). [web:361]
        self._cursor = mplcursors.cursor(artists, hover=2)

        @self._cursor.connect("add")
        def _on_add(sel):
            src, ch = self._hover_map.get(sel.artist, ("?", "?"))
            x, y = sel.target
            sel.annotation.set_text(f"{src}\n{ch}\n t={sec_to_mmss_mmm(float(x))}\n y={float(y):.3f}")
            self.hover_var.set(f"Hover: {src} | {ch} | t={sec_to_mmss_mmm(float(x))} | y={float(y):.3f}")

        @self._cursor.connect("remove")
        def _on_remove(sel):
            self.hover_var.set("Hover: --")

    # ---------- view controls ----------
    def home_rolling(self):
        self._full_view = False
        self._dirty = True

    def home_full(self):
        self._full_view = True
        self._dirty = True

    # ---------- export ----------
    def export_csv(self):
        path = filedialog.asksaveasfilename(
            title="Export CSV (timestamp + 4 columns)",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return

        try:
            t_in = list(self.monitor.main.t1); T_in = list(self.monitor.main.T1); H_in = list(self.monitor.main.H1)
            t_out = list(self.monitor.main.t2); T_out = list(self.monitor.main.T2); H_out = list(self.monitor.main.H2)

            times = sorted(set(t_in + t_out))
            if not times:
                messagebox.showwarning("No data", "No data available to export.")
                return

            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    "Timestamp (MM:SS.mmm)",
                    f"{LABEL_INLET} Temp (°C)",
                    f"{LABEL_INLET} Hum (%)",
                    f"{LABEL_OUTLET} Temp (°C)",
                    f"{LABEL_OUTLET} Hum (%)"
                ])

                for t in times:
                    inlet_temp = nearest_by_time(t_in, T_in, t, EXPORT_MAX_DT_SEC)
                    inlet_hum = nearest_by_time(t_in, H_in, t, EXPORT_MAX_DT_SEC)
                    outlet_temp = nearest_by_time(t_out, T_out, t, EXPORT_MAX_DT_SEC)
                    outlet_hum = nearest_by_time(t_out, H_out, t, EXPORT_MAX_DT_SEC)

                    w.writerow([
                        sec_to_mmss_mmm(t),
                        "" if inlet_temp is None else f"{inlet_temp:.3f}",
                        "" if inlet_hum is None else f"{inlet_hum:.3f}",
                        "" if outlet_temp is None else f"{outlet_temp:.3f}",
                        "" if outlet_hum is None else f"{outlet_hum:.3f}",
                    ])
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ---------- plotting helpers ----------
    def _create_overlay_lines(self, ov: OverlayItem):
        ls = "--"
        alpha = 0.85
        (l_in_T,) = self.ax_in_T.plot([], [], lw=1.2, ls=ls, color=ov.color, alpha=alpha, label=ov.name)
        (l_in_H,) = self.ax_in_H.plot([], [], lw=1.2, ls=ls, color=ov.color, alpha=alpha, label=ov.name)
        (l_out_T,) = self.ax_out_T.plot([], [], lw=1.2, ls=ls, color=ov.color, alpha=alpha, label=ov.name)
        (l_out_H,) = self.ax_out_H.plot([], [], lw=1.2, ls=ls, color=ov.color, alpha=alpha, label=ov.name)

        ov.lines = {"inT": l_in_T, "inH": l_in_H, "outT": l_out_T, "outH": l_out_H}

        for a in (self.ax_in_T, self.ax_in_H, self.ax_out_T, self.ax_out_H):
            a.legend(loc="best")

    def _remove_overlay_lines(self, ov: OverlayItem):
        if not ov.lines:
            return
        for ln in ov.lines.values():
            try:
                ln.remove()
            except Exception:
                pass
        ov.lines = None
        for a in (self.ax_in_T, self.ax_in_H, self.ax_out_T, self.ax_out_H):
            a.legend(loc="best")

    def _apply_main_lines(self):
        # convert deques to lists once per frame (fast enough)
        t1 = list(self.monitor.main.t1); T1 = list(self.monitor.main.T1); H1 = list(self.monitor.main.H1)
        t2 = list(self.monitor.main.t2); T2 = list(self.monitor.main.T2); H2 = list(self.monitor.main.H2)

        t1d, T1d = downsample_xy(t1, T1, DRAW_MAX_POINTS)
        _,  H1d = downsample_xy(t1, H1, DRAW_MAX_POINTS)
        t2d, T2d = downsample_xy(t2, T2, DRAW_MAX_POINTS)
        _,  H2d = downsample_xy(t2, H2, DRAW_MAX_POINTS)

        self.in_T_line.set_data(t1d, T1d)
        self.in_H_line.set_data(t1d, H1d)
        self.out_T_line.set_data(t2d, T2d)
        self.out_H_line.set_data(t2d, H2d)

    def _apply_overlay_lines(self):
        for ov in self.monitor.overlays:
            if not ov.lines:
                continue
            t1 = list(ov.buffers.t1); T1 = list(ov.buffers.T1); H1 = list(ov.buffers.H1)
            t2 = list(ov.buffers.t2); T2 = list(ov.buffers.T2); H2 = list(ov.buffers.H2)

            t1d, T1d = downsample_xy(t1, T1, DRAW_MAX_POINTS)
            _,  H1d = downsample_xy(t1, H1, DRAW_MAX_POINTS)
            t2d, T2d = downsample_xy(t2, T2, DRAW_MAX_POINTS)
            _,  H2d = downsample_xy(t2, H2, DRAW_MAX_POINTS)

            ov.lines["inT"].set_data(t1d, T1d)
            ov.lines["inH"].set_data(t1d, H1d)
            ov.lines["outT"].set_data(t2d, T2d)
            ov.lines["outH"].set_data(t2d, H2d)

    def _fast_ylim(self, xs, ys, x0, x1):
        if not xs:
            return None
        ys_win = [y for x, y in zip(xs, ys) if x0 <= x <= x1]
        if not ys_win:
            return None
        ymin = min(ys_win); ymax = max(ys_win)
        span = max(MIN_YSPAN, ymax - ymin)
        pad = span * Y_PAD_FRAC
        return (ymin - pad, ymax + pad)

    def _get_latest_time(self):
        tmax = 0.0
        if self.monitor.main.t1:
            tmax = max(tmax, self.monitor.main.t1[-1])
        if self.monitor.main.t2:
            tmax = max(tmax, self.monitor.main.t2[-1])
        for ov in self.monitor.overlays:
            if ov.buffers.t1:
                tmax = max(tmax, ov.buffers.t1[-1])
            if ov.buffers.t2:
                tmax = max(tmax, ov.buffers.t2[-1])
        return tmax

    def _apply_limits(self):
        t_now = self._get_latest_time()
        if self._full_view:
            x0, x1 = 0.0, max(1.0, t_now)
        else:
            x1 = max(0.0, t_now)
            x0 = max(0.0, x1 - VIEW_WINDOW_SEC)

        for ax in (self.ax_in_T, self.ax_in_H, self.ax_out_T, self.ax_out_H):
            ax.set_xlim(x0, x1)

        # Use MAIN full data for y-lims when available; otherwise overlays.
        def pick_series(main_deq_x, main_deq_y, overlay_getter):
            if main_deq_x:
                return list(main_deq_x), list(main_deq_y)
            # fallback: concatenate overlay lines (cheap)
            xs = []; ys = []
            for ov in self.monitor.overlays:
                xyo = overlay_getter(ov)
                if xyo is None:
                    continue
                x_, y_ = xyo
                xs.extend(x_); ys.extend(y_)
            return xs, ys

        xin, yin = pick_series(self.monitor.main.t1, self.monitor.main.T1,
                               lambda ov: (list(ov.buffers.t1), list(ov.buffers.T1)) if ov.buffers.t1 else None)
        yl = self._fast_ylim(xin, yin, x0, x1)
        if yl: self.ax_in_T.set_ylim(*yl)

        xin, yin = pick_series(self.monitor.main.t1, self.monitor.main.H1,
                               lambda ov: (list(ov.buffers.t1), list(ov.buffers.H1)) if ov.buffers.t1 else None)
        yl = self._fast_ylim(xin, yin, x0, x1)
        if yl: self.ax_in_H.set_ylim(*yl)

        xout, yout = pick_series(self.monitor.main.t2, self.monitor.main.T2,
                                 lambda ov: (list(ov.buffers.t2), list(ov.buffers.T2)) if ov.buffers.t2 else None)
        yl = self._fast_ylim(xout, yout, x0, x1)
        if yl: self.ax_out_T.set_ylim(*yl)

        xout, yout = pick_series(self.monitor.main.t2, self.monitor.main.H2,
                                 lambda ov: (list(ov.buffers.t2), list(ov.buffers.H2)) if ov.buffers.t2 else None)
        yl = self._fast_ylim(xout, yout, x0, x1)
        if yl: self.ax_out_H.set_ylim(*yl)

    # ---------- main update loop ----------
    def _handle_events(self):
        while True:
            try:
                ev, msg = self.monitor.event_q.get_nowait()
            except Empty:
                break
            if ev == "RESET":
                self.reset_main()
            elif ev == "ERROR":
                self.status_var.set(f"error: {msg}")

    def _handle_overlay_loads(self):
        handled = False
        while True:
            try:
                typ, ov, t1, T1, H1, t2, T2, H2 = self.overlay_load_q.get_nowait()
            except Empty:
                break
            if typ == "STATIC_LOADED":
                ov.buffers.clear()
                for t, te, hu in zip(t1, T1, H1):
                    ov.buffers.push(t, 1, te, hu)
                for t, te, hu in zip(t2, T2, H2):
                    ov.buffers.push(t, 2, te, hu)
                handled = True
        if handled:
            self.status_var.set("idle")
            self.update_hover_bindings()
            self._dirty = True

    def update_plot(self):
        self._handle_events()
        self._handle_overlay_loads()

        got = 0
        last = None

        while True:
            try:
                t_abs, sid, temp, hum = self.monitor.queue.get_nowait()
            except Empty:
                break

            # set relative time base
            if self.monitor.main_t_start_abs is None:
                self.monitor.main_t_start_abs = t_abs
            t_rel = t_abs - self.monitor.main_t_start_abs

            self.monitor.main.push(t_rel, sid, temp, hum)
            last = (sid, temp, hum)
            got += 1

        if got > 0:
            self._new_points_since_rescale += got
            self._dirty = True
            if last is not None:
                sid, temp, hum = last
                label = LABEL_INLET if sid == 1 else LABEL_OUTLET
                self.latest_var.set(f"Latest: {label}  {temp:.2f} °C, {hum:.2f} %")

        # Update artists only if needed (cuts lag)
        if self._dirty:
            self._apply_main_lines()
            self._apply_overlay_lines()

            if (self._new_points_since_rescale >= RESCALE_EVERY_N) and not getattr(self.toolbar, "mode", ""):
                self._apply_limits()
                self._new_points_since_rescale = 0

            # Also set limits when only overlays exist / main stopped
            if not self.monitor.running and not getattr(self.toolbar, "mode", ""):
                self._apply_limits()

            self.canvas.draw_idle()
            self._dirty = False

        self.root.after(PLOT_REFRESH_MS, self.update_plot)


if __name__ == "__main__":
    root = tk.Tk()
    app = LivePlotterApp(root)
    root.mainloop()
