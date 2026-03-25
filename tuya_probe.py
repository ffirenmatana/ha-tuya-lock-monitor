"""
Tuya Local Device Probe — standalone GUI tool
Requires: pip install tinytuya
Run:      python tuya_probe.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk
from typing import Any

# ---------------------------------------------------------------------------
# DPS ↔ human-readable code map for DL031HA / jtmspro category
# ---------------------------------------------------------------------------
DPS_TO_CODE: dict[str, str] = {
    "1":  "unlock_fingerprint",
    "2":  "unlock_password",
    "3":  "unlock_temporary",
    "5":  "unlock_card",
    "8":  "alarm_lock",
    "9":  "unlock_request",
    "12": "residual_electricity",
    "13": "reverse_lock",
    "15": "unlock_app",
    "16": "hijack",
    "19": "doorbell",
    "32": "unlock_offline_pd",
    "33": "unlock_offline_clear",
    "44": "unlock_double_kit",
    "49": "remote_no_pd_setkey",
    "50": "remote_no_dp_key",
    "58": "normal_open_switch",
}
CODE_TO_DPS: dict[str, int] = {v: int(k) for k, v in DPS_TO_CODE.items()}

# DPS values that are writable (can be sent as commands)
WRITABLE_DPS = {
    "9":  ("unlock_request",      bool),
    "13": ("reverse_lock",        bool),
    "19": ("doorbell",            bool),
    "33": ("unlock_offline_clear",bool),
    "49": ("remote_no_pd_setkey", str),
    "50": ("remote_no_dp_key",    str),
    "58": ("normal_open_switch",  bool),
}

BOOL_TRUE_STR  = "true"
BOOL_FALSE_STR = "false"


# ---------------------------------------------------------------------------
# Background worker — runs tinytuya calls in a thread pool
# ---------------------------------------------------------------------------
class TuyaWorker:
    def __init__(self, log_fn):
        self._log = log_fn
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ ping
    def ping(self, ip: str, port: int = 6668, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    # ---------------------------------------------------------------- status
    def get_status(self, ip: str, device_id: str, local_key: str, version: float) -> dict:
        import tinytuya
        d = tinytuya.Device(dev_id=device_id, address=ip,
                            local_key=local_key, version=version)
        d.set_socketTimeout(5)
        return d.status()

    # ------------------------------------------------------------- set value
    def set_value(self, ip: str, device_id: str, local_key: str,
                  version: float, dp: int, value: Any) -> dict:
        import tinytuya
        d = tinytuya.Device(dev_id=device_id, address=ip,
                            local_key=local_key, version=version)
        d.set_socketTimeout(5)
        return d.set_value(dp, value)

    # ----------------------------------------------------------------- scan
    def scan(self, timeout: int = 8, max_retry: int = 6) -> list[dict]:
        import tinytuya
        devices = tinytuya.deviceScan(verbose=False, maxretry=max_retry)
        if isinstance(devices, dict):
            return list(devices.values())
        return devices or []


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class TuyaProbeApp(tk.Tk):
    POLL_INTERVAL_MS = 15_000   # auto-refresh interval

    def __init__(self):
        super().__init__()
        self.title("Tuya Local Device Probe")
        self.resizable(True, True)
        self.minsize(860, 620)

        self._worker = TuyaWorker(self._log)
        self._poll_job: str | None = None   # after() handle
        self._last_dps: dict[str, Any] = {}

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # =========================================================== UI building
    def _build_ui(self):
        # Top-level paned window: left = controls, right = log
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=1)

        self._build_connection_panel(left)
        self._build_status_panel(left)
        self._build_command_panel(left)
        self._build_scan_panel(left)
        self._build_log_panel(right)

    # ------------------------------------------------------- connection panel
    def _build_connection_panel(self, parent):
        lf = ttk.LabelFrame(parent, text="Connection", padding=8)
        lf.pack(fill=tk.X, padx=4, pady=(4, 2))

        fields = [
            ("IP Address",   "ip",         "192.168.1."),
            ("Device ID",    "device_id",  ""),
            ("Local Key",    "local_key",  ""),
        ]
        self._conn_vars: dict[str, tk.StringVar] = {}

        for row, (label, key, default) in enumerate(fields):
            ttk.Label(lf, text=label + ":").grid(
                row=row, column=0, sticky=tk.W, pady=2)
            var = tk.StringVar(value=default)
            self._conn_vars[key] = var
            show = "*" if key == "local_key" else ""
            ent = ttk.Entry(lf, textvariable=var, width=32, show=show)
            ent.grid(row=row, column=1, sticky=tk.EW, padx=(6, 0), pady=2)

        # Version selector
        ttk.Label(lf, text="Protocol Version:").grid(
            row=len(fields), column=0, sticky=tk.W, pady=2)
        self._version_var = tk.StringVar(value="3.4")
        ttk.Combobox(lf, textvariable=self._version_var,
                     values=["3.3", "3.4", "3.5"], width=8, state="readonly"
                     ).grid(row=len(fields), column=1, sticky=tk.W, padx=(6, 0), pady=2)

        lf.columnconfigure(1, weight=1)

        # Buttons row
        btn_frame = ttk.Frame(lf)
        btn_frame.grid(row=len(fields) + 1, column=0, columnspan=2,
                       sticky=tk.EW, pady=(8, 0))

        self._ping_btn    = ttk.Button(btn_frame, text="Ping",         command=self._do_ping)
        self._refresh_btn = ttk.Button(btn_frame, text="Get Status",   command=self._do_refresh)
        self._auto_var    = tk.BooleanVar(value=False)
        self._auto_cb     = ttk.Checkbutton(btn_frame, text=f"Auto ({self.POLL_INTERVAL_MS // 1000}s)",
                                            variable=self._auto_var,
                                            command=self._toggle_auto)

        self._ping_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._refresh_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._auto_cb.pack(side=tk.LEFT)

        # Ping status indicator
        self._ping_label = ttk.Label(btn_frame, text="●  Unknown",
                                     foreground="gray")
        self._ping_label.pack(side=tk.RIGHT, padx=4)

    # ---------------------------------------------------------- status panel
    def _build_status_panel(self, parent):
        lf = ttk.LabelFrame(parent, text="Device Status (DPS)", padding=8)
        lf.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        cols = ("dp", "code", "value")
        tree = ttk.Treeview(lf, columns=cols, show="headings", height=12)
        tree.heading("dp",    text="DPS #")
        tree.heading("code",  text="Code")
        tree.heading("value", text="Value")
        tree.column("dp",    width=55,  anchor=tk.CENTER, stretch=False)
        tree.column("code",  width=200, anchor=tk.W)
        tree.column("value", width=140, anchor=tk.W)

        vsb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Colour rows for bool values
        tree.tag_configure("true",  background="#d4edda")
        tree.tag_configure("false", background="#f8d7da")
        tree.tag_configure("num",   background="#fff3cd")
        tree.tag_configure("other", background="#e2e3e5")

        # Double-click row → pre-fill command panel
        tree.bind("<Double-1>", self._on_tree_double_click)
        self._tree = tree

    # --------------------------------------------------------- command panel
    def _build_command_panel(self, parent):
        lf = ttk.LabelFrame(parent, text="Send Command", padding=8)
        lf.pack(fill=tk.X, padx=4, pady=2)

        # DP selector
        ttk.Label(lf, text="DPS #:").grid(row=0, column=0, sticky=tk.W)
        self._cmd_dp_var = tk.StringVar()
        dp_values = [f"{dp}  ({info[0]})" for dp, info in WRITABLE_DPS.items()]
        self._dp_cb = ttk.Combobox(lf, textvariable=self._cmd_dp_var,
                                   values=dp_values, width=30)
        self._dp_cb.grid(row=0, column=1, sticky=tk.EW, padx=(6, 0))
        self._dp_cb.bind("<<ComboboxSelected>>", self._on_dp_select)

        # Value
        ttk.Label(lf, text="Value:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        self._cmd_val_var = tk.StringVar()
        self._cmd_val_entry = ttk.Entry(lf, textvariable=self._cmd_val_var, width=32)
        self._cmd_val_entry.grid(row=1, column=1, sticky=tk.EW,
                                 padx=(6, 0), pady=(4, 0))

        # bool helper buttons
        bool_frame = ttk.Frame(lf)
        bool_frame.grid(row=2, column=1, sticky=tk.W, pady=(2, 0))
        ttk.Button(bool_frame, text="Set TRUE",
                   command=lambda: self._cmd_val_var.set(BOOL_TRUE_STR)
                   ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bool_frame, text="Set FALSE",
                   command=lambda: self._cmd_val_var.set(BOOL_FALSE_STR)
                   ).pack(side=tk.LEFT)

        # Raw DP (for unlisted DPS)
        ttk.Label(lf, text="Raw DP:").grid(row=3, column=0, sticky=tk.W, pady=(8, 0))
        raw_frame = ttk.Frame(lf)
        raw_frame.grid(row=3, column=1, sticky=tk.EW, pady=(8, 0))
        self._raw_dp_var  = tk.StringVar()
        self._raw_val_var = tk.StringVar()
        ttk.Entry(raw_frame, textvariable=self._raw_dp_var,  width=6 ).pack(side=tk.LEFT)
        ttk.Label(raw_frame, text=" Value:").pack(side=tk.LEFT)
        ttk.Entry(raw_frame, textvariable=self._raw_val_var, width=18).pack(side=tk.LEFT, padx=(4, 0))

        lf.columnconfigure(1, weight=1)

        # Send buttons
        send_frame = ttk.Frame(lf)
        send_frame.grid(row=4, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        ttk.Button(send_frame, text="Send Named DPS",
                   command=self._do_send_named).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(send_frame, text="Send Raw DPS",
                   command=self._do_send_raw).pack(side=tk.LEFT)

    # ------------------------------------------------------------ scan panel
    def _build_scan_panel(self, parent):
        lf = ttk.LabelFrame(parent, text="LAN Scan (UDP broadcast)", padding=8)
        lf.pack(fill=tk.X, padx=4, pady=(2, 4))

        row = ttk.Frame(lf)
        row.pack(fill=tk.X)
        ttk.Button(row, text="Scan Network",
                   command=self._do_scan).pack(side=tk.LEFT)
        self._scan_status = ttk.Label(row, text="")
        self._scan_status.pack(side=tk.LEFT, padx=8)

        self._scan_var = tk.StringVar()
        self._scan_cb = ttk.Combobox(lf, textvariable=self._scan_var,
                                     state="readonly", width=60)
        self._scan_cb.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(lf, text="Use Selected Device",
                   command=self._use_scanned).pack(anchor=tk.W, pady=(4, 0))

        self._scanned_devices: list[dict] = []

    # --------------------------------------------------------------- log panel
    def _build_log_panel(self, parent):
        lf = ttk.LabelFrame(parent, text="Activity Log", padding=8)
        lf.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self._log_box = scrolledtext.ScrolledText(
            lf, state=tk.DISABLED, wrap=tk.WORD,
            font=("Consolas", 9), background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="white"
        )
        self._log_box.pack(fill=tk.BOTH, expand=True)

        # Tag colours
        self._log_box.tag_config("INFO",    foreground="#9cdcfe")
        self._log_box.tag_config("OK",      foreground="#4ec9b0")
        self._log_box.tag_config("WARN",    foreground="#ce9178")
        self._log_box.tag_config("ERROR",   foreground="#f44747")
        self._log_box.tag_config("CMD",     foreground="#dcdcaa")
        self._log_box.tag_config("ts",      foreground="#569cd6")

        ttk.Button(lf, text="Clear Log",
                   command=self._clear_log).pack(anchor=tk.E, pady=(4, 0))

    # =========================================================== logging
    def _log(self, message: str, level: str = "INFO"):
        ts = time.strftime("%H:%M:%S")
        self._log_box.configure(state=tk.NORMAL)
        self._log_box.insert(tk.END, f"[{ts}] ", "ts")
        self._log_box.insert(tk.END, f"[{level}] {message}\n", level)
        self._log_box.configure(state=tk.DISABLED)
        self._log_box.see(tk.END)

    def _clear_log(self):
        self._log_box.configure(state=tk.NORMAL)
        self._log_box.delete("1.0", tk.END)
        self._log_box.configure(state=tk.DISABLED)

    # =========================================================== helpers
    def _conn(self) -> tuple[str, str, str, float] | None:
        """Return (ip, device_id, local_key, version) or None if fields empty."""
        ip        = self._conn_vars["ip"].get().strip()
        device_id = self._conn_vars["device_id"].get().strip()
        local_key = self._conn_vars["local_key"].get().strip()
        version   = float(self._version_var.get())
        if not ip or not device_id or not local_key:
            messagebox.showwarning("Missing fields",
                                   "IP address, Device ID and Local Key are required.")
            return None
        return ip, device_id, local_key, version

    def _run_thread(self, target, *args):
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()

    def _set_ping_indicator(self, reachable: bool | None):
        if reachable is True:
            self._ping_label.configure(text="●  Reachable", foreground="#28a745")
        elif reachable is False:
            self._ping_label.configure(text="●  Unreachable", foreground="#dc3545")
        else:
            self._ping_label.configure(text="●  Unknown", foreground="gray")

    # =========================================================== actions
    # ------------------------------------------------------------------ ping
    def _do_ping(self):
        conn = self._conn()
        if conn is None:
            return
        ip, *_ = conn
        self._log(f"Pinging {ip}:6668 …")
        self._run_thread(self._ping_thread, ip)

    def _ping_thread(self, ip: str):
        ok = self._worker.ping(ip)
        self.after(0, self._set_ping_indicator, ok)
        if ok:
            self._log(f"Ping {ip} → reachable", "OK")
        else:
            self._log(f"Ping {ip} → no response on port 6668", "WARN")

    # --------------------------------------------------------------- refresh
    def _do_refresh(self):
        conn = self._conn()
        if conn is None:
            return
        ip, device_id, local_key, version = conn
        self._log(f"Fetching status from {ip} …")
        self._run_thread(self._refresh_thread, ip, device_id, local_key, version)

    def _refresh_thread(self, ip, device_id, local_key, version):
        try:
            result = self._worker.get_status(ip, device_id, local_key, version)
            self.after(0, self._handle_status_result, result)
        except Exception as exc:
            self.after(0, self._log, f"Get status failed: {exc}", "ERROR")

    def _handle_status_result(self, result: dict):
        if "Error" in result:
            self._log(f"Device error: {result.get('Error')}  (Payload: {result})", "ERROR")
            return

        dps: dict = result.get("dps", {})
        if not dps:
            self._log(f"No DPS data in response: {result}", "WARN")
            return

        self._last_dps = {str(k): v for k, v in dps.items()}
        self._log(f"Status received — {len(dps)} DPS values", "OK")
        self._populate_tree(self._last_dps)

    def _populate_tree(self, dps: dict[str, Any]):
        for row in self._tree.get_children():
            self._tree.delete(row)

        for dp_str in sorted(dps.keys(), key=lambda x: int(x)):
            value = dps[dp_str]
            code  = DPS_TO_CODE.get(dp_str, "—")
            tag   = "other"
            if isinstance(value, bool):
                tag = "true" if value else "false"
            elif isinstance(value, (int, float)):
                tag = "num"

            self._tree.insert("", tk.END,
                               values=(dp_str, code, value),
                               tags=(tag,))

    # ------------------------------------------------------ auto-refresh
    def _toggle_auto(self):
        if self._auto_var.get():
            self._log(f"Auto-refresh enabled ({self.POLL_INTERVAL_MS // 1000}s interval)")
            self._schedule_auto()
        else:
            self._cancel_auto()
            self._log("Auto-refresh disabled")

    def _schedule_auto(self):
        self._cancel_auto()
        self._poll_job = self.after(self.POLL_INTERVAL_MS, self._auto_poll)

    def _cancel_auto(self):
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None

    def _auto_poll(self):
        if self._auto_var.get():
            self._do_refresh()
            self._schedule_auto()

    # ------------------------------------ double-click tree → fill command
    def _on_tree_double_click(self, event):
        item = self._tree.selection()
        if not item:
            return
        vals = self._tree.item(item[0], "values")
        dp_str, code, value = vals
        # Try to match writable dropdown
        for combo_str in self._dp_cb["values"]:
            if combo_str.startswith(dp_str + " "):
                self._cmd_dp_var.set(combo_str)
                break
        else:
            # Fallback: put in raw field
            self._raw_dp_var.set(dp_str)
            self._raw_val_var.set(str(value))
            return
        self._cmd_val_var.set(str(value).lower() if isinstance(value, bool) else str(value))

    def _on_dp_select(self, _event=None):
        """Clear value field when a new DPS is chosen from dropdown."""
        self._cmd_val_var.set("")

    # ----------------------------------------------------- send named DPS
    def _do_send_named(self):
        conn = self._conn()
        if conn is None:
            return
        raw = self._cmd_dp_var.get().strip()
        if not raw:
            messagebox.showwarning("No DPS", "Select a DPS from the dropdown.")
            return
        dp_str = raw.split()[0]
        try:
            dp = int(dp_str)
        except ValueError:
            messagebox.showerror("Invalid DPS", f"Could not parse DP number from '{raw}'")
            return

        val_str = self._cmd_val_var.get().strip()
        expected_type = WRITABLE_DPS.get(dp_str, (None, str))[1]
        value = self._parse_value(val_str, expected_type)
        if value is None:
            return

        ip, device_id, local_key, version = conn
        self._log(f"CMD → DPS {dp} = {value!r}", "CMD")
        self._run_thread(self._send_thread, ip, device_id, local_key, version, dp, value)

    # ------------------------------------------------------ send raw DPS
    def _do_send_raw(self):
        conn = self._conn()
        if conn is None:
            return
        dp_str  = self._raw_dp_var.get().strip()
        val_str = self._raw_val_var.get().strip()
        if not dp_str or not val_str:
            messagebox.showwarning("Missing fields", "Raw DP and Value are both required.")
            return
        try:
            dp = int(dp_str)
        except ValueError:
            messagebox.showerror("Invalid DP", "DP must be an integer.")
            return

        # Auto-detect type: bool > int > float > str
        value: Any
        if val_str.lower() == "true":
            value = True
        elif val_str.lower() == "false":
            value = False
        else:
            try:
                value = int(val_str)
            except ValueError:
                try:
                    value = float(val_str)
                except ValueError:
                    value = val_str

        ip, device_id, local_key, version = conn
        self._log(f"CMD RAW → DPS {dp} = {value!r}  (type: {type(value).__name__})", "CMD")
        self._run_thread(self._send_thread, ip, device_id, local_key, version, dp, value)

    def _send_thread(self, ip, device_id, local_key, version, dp, value):
        try:
            result = self._worker.set_value(ip, device_id, local_key, version, dp, value)
            if result and "Error" in result:
                self.after(0, self._log,
                           f"Send failed: {result.get('Error')}  ({result})", "ERROR")
            else:
                self.after(0, self._log,
                           f"Send OK — DPS {dp} = {value!r}  response: {result}", "OK")
                time.sleep(0.5)
                # Auto-refresh after command
                self.after(0, self._do_refresh)
        except Exception as exc:
            self.after(0, self._log, f"Send error: {exc}", "ERROR")

    # ----------------------------------------------------------------- scan
    def _do_scan(self):
        self._scan_status.configure(text="Scanning…")
        self._scan_cb["values"] = []
        self._scanned_devices = []
        self._log("Starting LAN device scan (UDP broadcast, ~8 s) …")
        self._run_thread(self._scan_thread)

    def _scan_thread(self):
        try:
            devices = self._worker.scan()
            self.after(0, self._handle_scan_result, devices)
        except Exception as exc:
            self.after(0, self._log, f"Scan error: {exc}", "ERROR")
            self.after(0, self._scan_status.configure, {"text": "Error"})

    def _handle_scan_result(self, devices: list[dict]):
        self._scanned_devices = devices
        if not devices:
            self._scan_status.configure(text="No devices found")
            self._log("Scan complete — no Tuya devices found on LAN", "WARN")
            return

        self._log(f"Scan complete — {len(devices)} device(s) found", "OK")
        entries = []
        for d in devices:
            ip  = d.get("ip", "?")
            did = d.get("gwId", d.get("id", "?"))
            ver = d.get("version", "?")
            prod = d.get("productKey", "")
            name = d.get("name", "")
            label = f"{ip}  |  {did}  |  v{ver}"
            if name:
                label = f"{name}  —  " + label
            entries.append(label)
            self._log(f"  {label}", "INFO")

        self._scan_cb["values"] = entries
        self._scan_cb.current(0)
        self._scan_status.configure(text=f"{len(devices)} found")

    def _use_scanned(self):
        idx = self._scan_cb.current()
        if idx < 0 or idx >= len(self._scanned_devices):
            messagebox.showwarning("No selection", "Select a device from the scan list.")
            return
        d = self._scanned_devices[idx]
        ip  = d.get("ip", "")
        did = d.get("gwId", d.get("id", ""))
        ver = str(d.get("version", "3.4"))
        self._conn_vars["ip"].set(ip)
        self._conn_vars["device_id"].set(did)
        if ver in ["3.3", "3.4", "3.5"]:
            self._version_var.set(ver)
        self._log(f"Filled connection fields from scan — IP={ip}  ID={did}  v{ver}", "INFO")

    # ------------------------------------------------------------ value parse
    def _parse_value(self, val_str: str, expected_type: type) -> Any:
        if expected_type is bool:
            if val_str.lower() in ("true", "1", "yes", "on"):
                return True
            if val_str.lower() in ("false", "0", "no", "off"):
                return False
            messagebox.showerror("Invalid value",
                                 "Boolean DPS expects true/false (or 1/0, yes/no).")
            return None
        if expected_type is str:
            return val_str
        try:
            return expected_type(val_str)
        except (ValueError, TypeError):
            messagebox.showerror("Invalid value",
                                 f"Expected {expected_type.__name__}, got '{val_str}'.")
            return None

    # =========================================================== close
    def _on_close(self):
        self._cancel_auto()
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = TuyaProbeApp()

    # Nice styling
    style = ttk.Style(app)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TLabelframe.Label", font=("Segoe UI", 9, "bold"))
    style.configure("TButton", padding=(6, 3))

    app.mainloop()


if __name__ == "__main__":
    main()
