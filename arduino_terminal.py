import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
from queue import Queue, Empty

import serial
import serial.tools.list_ports  # COM port list via pyserial [web:405][web:404]


DEFAULT_BAUDS = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]


def ts_mmss_mmm():
    t = time.perf_counter()
    total_ms = int(t * 1000)
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    m = total_s // 60
    return f"{m:02d}:{s:02d}.{ms:03d}"


def list_com_ports_verbose():
    out = []
    for p in serial.tools.list_ports.comports():  # [web:404]
        label = f"{p.device}  ({p.description})" if getattr(p, "description", None) else p.device
        out.append((p.device, label))
    out.sort(key=lambda x: x[0])
    return out


class SerialTerminalGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Serial Terminal for SHT45 (Temperature and Humidity Measurement)")
        self.root.geometry("1100x750")

        self.ser = None
        self.stop_event = threading.Event()
        self.reader_thread = None

        self.rx_q = Queue()
        self.ui_q = Queue()

        self.logging = False
        self.log_file = None
        self.log_path = None

        self.autoscroll_var = tk.BooleanVar(value=True)
        self.pause_var = tk.BooleanVar(value=False)
        self.crlf_var = tk.BooleanVar(value=True)
        self.timestamp_var = tk.BooleanVar(value=True)
        self.append_var = tk.BooleanVar(value=False)

        self.port_map = {}  # label -> device

        self._build_ui()
        self._refresh_ports()
        self._auto_refresh_ports()
        self._ui_loop()

    # ---------------- UI ----------------
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="COM Port:").pack(side=tk.LEFT)
        self.port_label_var = tk.StringVar(value="")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_label_var, width=34, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=(6, 12))

        ttk.Button(top, text="Refresh", command=self._refresh_ports).pack(side=tk.LEFT, padx=(0, 14))

        ttk.Label(top, text="Baud:").pack(side=tk.LEFT)
        self.baud_var = tk.StringVar(value="115200")
        self.baud_combo = ttk.Combobox(top, textvariable=self.baud_var, width=10, values=[str(b) for b in DEFAULT_BAUDS])
        self.baud_combo.pack(side=tk.LEFT, padx=(6, 14))

        ttk.Checkbutton(top, text="Send CRLF", variable=self.crlf_var).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(top, text="Timestamp", variable=self.timestamp_var).pack(side=tk.LEFT, padx=(0, 14))

        self.conn_btn = ttk.Button(top, text="Connect", command=self._toggle_connect)
        self.conn_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.start_log_btn = ttk.Button(top, text="Start log…", command=self._start_log, state=tk.DISABLED)
        self.start_log_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_log_btn = ttk.Button(top, text="Stop log", command=self._stop_log, state=tk.DISABLED)
        self.stop_log_btn.pack(side=tk.LEFT, padx=(0, 14))

        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(top, textvariable=self.status_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

        opts = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        opts.pack(fill=tk.X)

        ttk.Checkbutton(opts, text="Auto-scroll", variable=self.autoscroll_var).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(opts, text="Pause display", variable=self.pause_var).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(opts, text="Append log", variable=self.append_var).pack(side=tk.LEFT, padx=(0, 14))

        ttk.Button(opts, text="Clear screen", command=self._clear_screen).pack(side=tk.LEFT)

        mid = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        mid.pack(fill=tk.BOTH, expand=True)

        self.text = tk.Text(mid, wrap="none", height=25)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        yscroll = ttk.Scrollbar(mid, orient="vertical", command=self.text.yview)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.configure(yscrollcommand=yscroll.set)

        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill=tk.X)

        ttk.Label(bottom, text="TX:").pack(side=tk.LEFT)
        self.tx_var = tk.StringVar(value="")
        self.tx_entry = ttk.Entry(bottom, textvariable=self.tx_var)
        self.tx_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 8))
        self.tx_entry.bind("<Return>", lambda e: self._send())

        ttk.Button(bottom, text="Send", command=self._send).pack(side=tk.LEFT)

    # ---------------- port list ----------------
    def _refresh_ports(self):
        ports = list_com_ports_verbose()
        labels = [lbl for _, lbl in ports]
        self.port_map = {lbl: dev for dev, lbl in ports}
        self.port_combo["values"] = labels

        if labels:
            if self.port_label_var.get() not in labels:
                self.port_label_var.set(labels[0])
        else:
            self.port_label_var.set("")

    def _auto_refresh_ports(self):
        # Auto refresh only when disconnected (non-blocking via after loop). [web:400]
        if self.ser is None:
            self._refresh_ports()
        self.root.after(2000, self._auto_refresh_ports)

    # ---------------- connect/disconnect ----------------
    def _toggle_connect(self):
        if self.ser is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self):
        lbl = self.port_label_var.get().strip()
        if not lbl:
            messagebox.showwarning("No port", "Select a COM port.")
            return
        port = self.port_map.get(lbl, lbl.split()[0])

        try:
            baud = int(self.baud_var.get().strip())
        except Exception:
            messagebox.showwarning("Bad baud", "Enter a valid baudrate (e.g. 115200).")
            return

        try:
            self.ser = serial.Serial(port, baud, timeout=0.5)
        except Exception as e:
            self.ser = None
            messagebox.showerror("Connect failed", str(e))
            return

        self.stop_event.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

        self.conn_btn.configure(text="Disconnect")
        self.start_log_btn.configure(state=tk.NORMAL)
        self.status_var.set(f"Connected: {port} @ {baud}")
        self._append_text(f"[{ts_mmss_mmm()}] Connected: {port} @ {baud}\n")

    def _disconnect(self):
        self._stop_log()
        self.stop_event.set()

        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass

        self.ser = None
        self.conn_btn.configure(text="Connect")
        self.start_log_btn.configure(state=tk.DISABLED)
        self.stop_log_btn.configure(state=tk.DISABLED)
        self.status_var.set("Disconnected")
        self._append_text(f"[{ts_mmss_mmm()}] Disconnected\n")

    # ---------------- logging ----------------
    def _start_log(self):
        if self.ser is None:
            messagebox.showwarning("Not connected", "Connect to a COM port first.")
            return

        path = filedialog.asksaveasfilename(  # save dialog [web:409]
            title="Save log file",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("Log files", "*.log"), ("All files", "*.*")]
        )
        if not path:
            return

        mode = "a" if self.append_var.get() else "w"
        try:
            self.log_file = open(path, mode, encoding="utf-8")
        except Exception as e:
            self.log_file = None
            messagebox.showerror("File error", str(e))
            return

        self.log_path = path
        self.logging = True
        self.stop_log_btn.configure(state=tk.NORMAL)
        self.start_log_btn.configure(state=tk.DISABLED)
        self._append_text(f"[{ts_mmss_mmm()}] Logging started: {path} (mode={mode})\n")

    def _stop_log(self):
        if not self.logging:
            return

        self.logging = False
        try:
            if self.log_file:
                self.log_file.close()
        except Exception:
            pass

        self.log_file = None
        self.log_path = None

        if self.ser is not None:
            self.start_log_btn.configure(state=tk.NORMAL)
        self.stop_log_btn.configure(state=tk.DISABLED)
        self._append_text(f"[{ts_mmss_mmm()}] Logging stopped\n")

    # ---------------- terminal ----------------
    def _clear_screen(self):
        self.text.delete("1.0", tk.END)

    def _append_text(self, s: str):
        self.text.insert(tk.END, s)
        if self.autoscroll_var.get():
            self.text.see(tk.END)

    def _format_rx_line(self, text: str) -> str:
        if self.timestamp_var.get():
            return f"{ts_mmss_mmm()} {text}"
        return text

    # ---------------- serial I/O ----------------
    def _reader_loop(self):
        while not self.stop_event.is_set() and self.ser is not None:
            try:
                line = self.ser.readline()
            except Exception as e:
                self.ui_q.put(("ERROR", str(e)))
                break

            if not line:
                continue

            text = line.decode(errors="replace").rstrip("\r\n")
            out = self._format_rx_line(text)

            # send to UI
            self.rx_q.put(out)

            # write log (same content as displayed)
            if self.logging and self.log_file:
                try:
                    self.log_file.write(out + "\n")
                    self.log_file.flush()
                except Exception as e:
                    self.ui_q.put(("ERROR", f"Log write failed: {e}"))
                    self.logging = False

        self.ui_q.put(("READER_STOPPED", ""))

    def _send(self):
        if self.ser is None:
            return

        cmd = self.tx_var.get()
        if cmd == "":
            return

        ending = "\r\n" if self.crlf_var.get() else "\n"
        payload = (cmd + ending).encode("utf-8", errors="ignore")

        try:
            self.ser.write(payload)
            self.ser.flush()
        except Exception as e:
            messagebox.showerror("Send failed", str(e))
            return

        # echo TX to terminal
        tx_line = f"{ts_mmss_mmm()} [TX] {cmd}" if self.timestamp_var.get() else f"[TX] {cmd}"
        self._append_text(tx_line + "\n")

        if self.logging and self.log_file:
            try:
                self.log_file.write(tx_line + "\n")
                self.log_file.flush()
            except Exception:
                pass

        self.tx_var.set("")

    # ---------------- UI update loop ----------------
    def _ui_loop(self):
        # RX lines
        if not self.pause_var.get():
            for _ in range(300):
                try:
                    line = self.rx_q.get_nowait()
                except Empty:
                    break
                self._append_text(line + "\n")
        else:
            # keep queue bounded while paused
            for _ in range(300):
                try:
                    self.rx_q.get_nowait()
                except Empty:
                    break

        # UI events
        while True:
            try:
                typ, msg = self.ui_q.get_nowait()
            except Empty:
                break

            if typ == "ERROR":
                self._append_text(f"[{ts_mmss_mmm()}] ERROR: {msg}\n")
                self.status_var.set(f"ERROR: {msg}")
            elif typ == "READER_STOPPED":
                # if it stopped unexpectedly, reflect it
                if self.ser is not None and not self.stop_event.is_set():
                    self._append_text(f"[{ts_mmss_mmm()}] Serial reader stopped\n")

        self.root.after(50, self._ui_loop)


if __name__ == "__main__":
    root = tk.Tk()
    app = SerialTerminalGUI(root)

    def on_close():
        app._disconnect()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
