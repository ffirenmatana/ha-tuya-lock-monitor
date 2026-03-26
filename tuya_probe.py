"""
Tuya Local Device Probe -- standalone GUI tool for DL031HA Series 2 (jtmspro)
Requires: pip install tinytuya
Run:      python tuya_probe.py
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from typing import Any

# ---------------------------------------------------------------------------
# Device mapping -- DL031HA Series 2 full DPS schema
# Each entry: dp_str -> {code, type, values, writable, description}
# ---------------------------------------------------------------------------
DEVICE_MAPPING: dict[str, dict] = {
    "1":  {"code": "unlock_fingerprint",   "type": "Integer",
           "values": {"min": 0, "max": 999, "step": 1},
           "writable": False, "description": "Fingerprint unlock count"},
    "2":  {"code": "unlock_password",      "type": "Integer",
           "values": {"min": 0, "max": 999, "step": 1},
           "writable": False, "description": "Password unlock count"},
    "3":  {"code": "unlock_temporary",     "type": "Integer",
           "values": {"min": 0, "max": 999, "step": 1},
           "writable": False, "description": "Temporary code unlock count"},
    "5":  {"code": "unlock_card",          "type": "Integer",
           "values": {"min": 0, "max": 999, "step": 1},
           "writable": False, "description": "Card unlock count"},
    "8":  {"code": "alarm_lock",           "type": "Enum",
           "values": {"range": ["wrong_finger", "wrong_password", "wrong_card", "wrong_face",
                                "tongue_bad", "too_hot", "unclosed_time", "tongue_not_out",
                                "pry", "key_in", "low_battery", "power_off", "shock", "defense"]},
           "writable": False, "description": "Last alarm type"},
    "9":  {"code": "unlock_request",       "type": "Integer",
           "values": {"min": 0, "max": 90, "step": 1},
           "writable": True,  "description": "Unlock request (seconds timeout)"},
    "12": {"code": "residual_electricity", "type": "Integer",
           "values": {"min": 0, "max": 100, "step": 1},
           "writable": False, "description": "Battery level (%)"},
    "13": {"code": "reverse_lock",         "type": "Boolean", "values": {},
           "writable": True,  "description": "Reverse lock / deadbolt snib"},
    "15": {"code": "unlock_app",           "type": "Integer",
           "values": {"min": 0, "max": 999, "step": 1},
           "writable": False, "description": "App unlock count"},
    "16": {"code": "hijack",               "type": "Boolean", "values": {},
           "writable": False, "description": "Hijack / duress alarm active"},
    "19": {"code": "doorbell",             "type": "Boolean", "values": {},
           "writable": True,  "description": "Doorbell trigger"},
    "32": {"code": "unlock_offline_pd",    "type": "Raw",     "values": {},
           "writable": False, "description": "Offline password data (read-only)"},
    "33": {"code": "unlock_offline_clear", "type": "Raw",     "values": {},
           "writable": True,  "description": "Clear offline passwords (send raw bytes)"},
    "44": {"code": "unlock_double_kit",    "type": "Raw",     "values": {},
           "writable": False, "description": "Double-authentication unlock event"},
    "49": {"code": "remote_no_pd_setkey",  "type": "Raw",     "values": {},
           "writable": True,  "description": "Set remote key (raw hex bytes)"},
    "50": {"code": "remote_no_dp_key",     "type": "Raw",     "values": {},
           "writable": True,  "description": "Remote key data (raw hex bytes)"},
    "58": {"code": "normal_open_switch",   "type": "Boolean", "values": {},
           "writable": True,  "description": "Passage mode / hold-open (unlock)"},
}

DPS_TO_CODE = {dp: m["code"] for dp, m in DEVICE_MAPPING.items()}

# Profiles saved next to this script
PROFILES_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tuya_probe_profiles.json"
)


# ---------------------------------------------------------------------------
# Profile storage helpers
# ---------------------------------------------------------------------------
def load_profiles() -> dict[str, dict]:
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_profiles(profiles: dict[str, dict]) -> None:
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)


# ---------------------------------------------------------------------------
# Background worker -- all tinytuya / socket calls run here (never on main thread)
# ---------------------------------------------------------------------------
class TuyaWorker:
    def ping(self, ip: str, port: int = 6668, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    def get_status(self, ip: str, device_id: str, local_key: str, version: float,
                    log_cb=None) -> dict:
        import tinytuya

        def _log(msg, lvl="INFO"):
            if log_cb:
                log_cb(msg, lvl)

        _log(f"  [DBG] Connecting to {ip}:6668  v{version}")
        d = tinytuya.Device(
            dev_id=device_id, address=ip, local_key=local_key, version=version,
            connection_timeout=2, connection_retry_limit=1, connection_retry_delay=0,
        )
        # socketPersistent keeps the TCP connection open after status() so that
        # updatedps() can reuse the same socket; disable retries BEFORE updatedps
        # so if the device already closed the connection it fails fast (<0.1 s)
        # instead of hanging 2 s trying to reconnect
        d.set_socketPersistent(True)
        combined: dict = {}
        err = ""
        try:
            _log("  [DBG] Calling status() ...")
            r = d.status()
            _log(f"  [DBG] status() raw response: {r}")
            if r and "dps" in r:
                combined.update({str(k): v for k, v in r["dps"].items()})
                _log(f"  [DBG] status() got {len(r['dps'])} DPS: {dict(r['dps'])}")
            elif r:
                err = r.get("Error", "unknown error")
                _log(f"  [DBG] status() error: {err}", "WARN")

            if combined:
                d.connection_retry_limit = 0
                d.retry = False
                all_dps = [int(k) for k in DEVICE_MAPPING.keys()]
                _log(f"  [DBG] Sending updatedps({all_dps}) ...")
                t_upd = time.time()
                try:
                    d.updatedps(all_dps)
                except Exception as upd_exc:
                    _log(f"  [DBG] updatedps() raised in {time.time()-t_upd:.2f}s: {upd_exc}", "WARN")
                _log(f"  [DBG] updatedps() sent in {time.time()-t_upd:.2f}s -- now reading responses")
                d.set_socketTimeout(1)
                deadline = time.time() + 1.5
                pkt_count = 0
                while time.time() < deadline:
                    t0 = time.time()
                    r = d.receive()
                    _log(f"  [DBG] receive() pkt {pkt_count+1} in {time.time()-t0:.2f}s: {r}")
                    if not r or "Error" in r or "dps" not in r:
                        _log("  [DBG] receive() loop ended")
                        break
                    pkt_count += 1
                    combined.update({str(k): v for k, v in r["dps"].items()})
                _log(f"  [DBG] done - {pkt_count} extra pkt(s), total DPS: {len(combined)}")
            else:
                _log("  [DBG] Skipping updatedps (no DPS from status())")
        except Exception as exc:
            _log(f"  [DBG] Exception: {exc}", "WARN")
            if not combined:
                err = str(exc)
        finally:
            try:
                d.close()
            except Exception:
                pass

        if combined:
            return {"dps": combined}
        return {"Error": err or "No DPS received from device"}

    def set_value(self, ip: str, device_id: str, local_key: str,
                  version: float, dp: int, value: Any) -> dict:
        import tinytuya
        d = tinytuya.Device(
            dev_id=device_id, address=ip, local_key=local_key, version=version,
            connection_timeout=2, connection_retry_limit=1, connection_retry_delay=0,
        )
        return d.set_value(dp, value)

    def scan(self, max_retry: int = 6) -> list[dict]:
        import tinytuya
        devices = tinytuya.deviceScan(verbose=False, maxretry=max_retry)
        if isinstance(devices, dict):
            return list(devices.values())
        return devices or []


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class TuyaProbeApp(tk.Tk):
    STATUS_POLL_INTERVAL = 15  # seconds between auto status fetches when reachable

    def __init__(self):
        super().__init__()
        self.title("Tuya Local Device Probe  --  DL031HA Series 2")
        self.resizable(True, True)
        self.minsize(1020, 680)

        self._worker = TuyaWorker()
        self._last_dps: dict[str, Any] = {}
        self._profiles: dict[str, dict] = load_profiles()
        # dp_str -> tk variable (Bool/Int/StringVar)
        self._dps_widgets: dict[str, Any] = {}

        # live monitor state
        self._ping_running: bool = False
        self._ping_thread: threading.Thread | None = None
        self._last_status_time: float = 0.0
        self._was_reachable: bool | None = None

        self._build_ui()
        self._refresh_profile_dropdown()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ UI
    def _build_ui(self):
        root_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        root_pane.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        left  = ttk.Frame(root_pane)
        right = ttk.Frame(root_pane)
        root_pane.add(left,  weight=2)
        root_pane.add(right, weight=3)

        self._build_connection_panel(left)

        nb = ttk.Notebook(left)
        nb.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 4))
        self._nb = nb

        status_tab    = ttk.Frame(nb)
        functions_tab = ttk.Frame(nb)
        scan_tab      = ttk.Frame(nb)
        raw_tab       = ttk.Frame(nb)

        nb.add(status_tab,    text="  Status  ")
        nb.add(functions_tab, text="  Functions  ")
        nb.add(scan_tab,      text="  LAN Scan  ")
        nb.add(raw_tab,       text="  Raw DPS  ")

        self._build_status_tab(status_tab)
        self._build_functions_tab(functions_tab)
        self._build_scan_tab(scan_tab)
        self._build_raw_tab(raw_tab)
        self._build_log_panel(right)

    # -------------------------------------------------- connection panel
    def _build_connection_panel(self, parent):
        lf = ttk.LabelFrame(parent, text="Connection", padding=8)
        lf.pack(fill=tk.X, padx=4, pady=(4, 2))

        # Profile selector row
        prof_row = ttk.Frame(lf)
        prof_row.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 6))
        ttk.Label(prof_row, text="Profile:").pack(side=tk.LEFT)
        self._profile_var = tk.StringVar()
        self._profile_cb = ttk.Combobox(prof_row, textvariable=self._profile_var,
                                         state="readonly", width=22)
        self._profile_cb.pack(side=tk.LEFT, padx=(6, 4))
        self._profile_cb.bind("<<ComboboxSelected>>", self._load_profile)
        ttk.Button(prof_row, text="Save",   command=self._save_profile,   width=6).pack(side=tk.LEFT, padx=2)
        ttk.Button(prof_row, text="Delete", command=self._delete_profile, width=6).pack(side=tk.LEFT, padx=2)

        # Connection fields
        fields = [
            ("Name",       "name",      "DL031HA Series 2"),
            ("IP Address", "ip",        ""),
            ("Device ID",  "device_id", ""),
            ("Local Key",  "local_key", ""),
        ]
        self._conn_vars: dict[str, tk.StringVar] = {}
        for row, (label, key, default) in enumerate(fields, start=1):
            ttk.Label(lf, text=label + ":").grid(row=row, column=0, sticky=tk.W, pady=2)
            var = tk.StringVar(value=default)
            self._conn_vars[key] = var
            show = "*" if key == "local_key" else ""
            ttk.Entry(lf, textvariable=var, width=34, show=show).grid(
                row=row, column=1, sticky=tk.EW, padx=(6, 0), pady=2)

        # Protocol version
        vrow = len(fields) + 1
        ttk.Label(lf, text="Protocol:").grid(row=vrow, column=0, sticky=tk.W, pady=2)
        self._version_var = tk.StringVar(value="3.4")
        ttk.Combobox(lf, textvariable=self._version_var,
                     values=["3.3", "3.4", "3.5"], width=8, state="readonly").grid(
            row=vrow, column=1, sticky=tk.W, padx=(6, 0), pady=2)

        lf.columnconfigure(1, weight=1)

        # Action buttons
        btn_frame = ttk.Frame(lf)
        btn_frame.grid(row=vrow + 1, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))

        ttk.Button(btn_frame, text="Ping",       command=self._do_ping).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_frame, text="Get Status", command=self._do_refresh).pack(side=tk.LEFT, padx=(0, 4))
        self._live_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btn_frame, text="Live Monitor",
                        variable=self._live_var,
                        command=self._toggle_live_monitor).pack(side=tk.LEFT)

        self._ping_label = ttk.Label(btn_frame, text="  Unknown", foreground="gray")
        self._ping_label.pack(side=tk.RIGHT, padx=4)

    # -------------------------------------------------- status tab
    def _build_status_tab(self, parent):
        cols = ("dp", "code", "type", "value", "description")
        tree = ttk.Treeview(parent, columns=cols, show="headings")
        tree.heading("dp",          text="DPS")
        tree.heading("code",        text="Code")
        tree.heading("type",        text="Type")
        tree.heading("value",       text="Current Value")
        tree.heading("description", text="Description")
        tree.column("dp",          width=45,  anchor=tk.CENTER, stretch=False)
        tree.column("code",        width=175, anchor=tk.W)
        tree.column("type",        width=65,  anchor=tk.CENTER, stretch=False)
        tree.column("value",       width=110, anchor=tk.W)
        tree.column("description", width=220, anchor=tk.W)

        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        tree.tag_configure("true",     background="#d4edda")
        tree.tag_configure("false",    background="#f8d7da")
        tree.tag_configure("num",      background="#fff3cd")
        tree.tag_configure("enum",     background="#d1ecf1")
        tree.tag_configure("raw",      background="#e2e3e5")
        tree.tag_configure("readonly", foreground="#555555")
        tree.tag_configure("writable", foreground="#000000", font=("Segoe UI", 9, "bold"))

        tree.bind("<Double-1>", self._on_status_double_click)
        self._tree = tree

    # ------------------------------------------------- functions tab
    def _build_functions_tab(self, parent):
        # Quick action buttons
        qa = ttk.LabelFrame(parent, text="Quick Actions", padding=8)
        qa.pack(fill=tk.X, padx=4, pady=(4, 2))

        actions = [
            ("Lock (normal)",       58, False, "Disable passage mode -- lock latches normally"),
            ("Unlock (passage)",    58, True,  "Enable passage mode -- hold lock open"),
            ("Ring Doorbell",       19, True,  "Trigger doorbell signal"),
            ("Enable Rev. Lock",    13, True,  "Engage deadbolt snib (reverse lock on)"),
            ("Disable Rev. Lock",   13, False, "Release deadbolt snib (reverse lock off)"),
        ]
        for col, (label, dp, value, tip) in enumerate(actions):
            btn = ttk.Button(qa, text=label,
                             command=lambda d=dp, v=value: self._quick_action(d, v))
            btn.grid(row=0, column=col, padx=4, pady=2, sticky=tk.EW)
            _add_tooltip(btn, tip)
        for col in range(len(actions)):
            qa.columnconfigure(col, weight=1)

        # Scrollable per-DPS control rows (writable only)
        ctrl_lf = ttk.LabelFrame(parent, text="DPS Controls (writable DPS only)", padding=8)
        ctrl_lf.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        canvas = tk.Canvas(ctrl_lf, highlightthickness=0)
        vsb    = ttk.Scrollbar(ctrl_lf, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = ttk.Frame(canvas)
        win   = canvas.create_window((0, 0), window=inner, anchor=tk.NW)

        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win, width=e.width))
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        writable = [(dp, m) for dp, m in DEVICE_MAPPING.items() if m["writable"]]
        for row_idx, (dp_str, meta) in enumerate(writable):
            self._build_dps_row(inner, row_idx, dp_str, meta)

        inner.columnconfigure(2, weight=1)

    def _build_dps_row(self, parent, row: int, dp_str: str, meta: dict):
        dp_int = int(dp_str)
        dtype  = meta["type"]
        vals   = meta["values"]

        # Separator between rows
        if row > 0:
            ttk.Separator(parent, orient=tk.HORIZONTAL).grid(
                row=row * 2 - 1, column=0, columnspan=4, sticky=tk.EW, pady=0)
        grid_row = row * 2

        ttk.Label(parent, text=f"DP {dp_str}", width=6, anchor=tk.E,
                  foreground="#555").grid(row=grid_row, column=0, padx=(4, 4), pady=5, sticky=tk.E)
        ttk.Label(parent, text=meta["code"], width=24, anchor=tk.W,
                  font=("Segoe UI", 9, "bold")).grid(
            row=grid_row, column=1, padx=(0, 8), pady=5, sticky=tk.W)

        if dtype == "Boolean":
            var = tk.BooleanVar(value=False)
            self._dps_widgets[dp_str] = var
            frame = ttk.Frame(parent)
            frame.grid(row=grid_row, column=2, sticky=tk.W)
            ttk.Radiobutton(frame, text="ON  / True",  variable=var, value=True ).pack(side=tk.LEFT, padx=4)
            ttk.Radiobutton(frame, text="OFF / False", variable=var, value=False).pack(side=tk.LEFT, padx=4)
            ttk.Button(frame, text="Send",
                       command=lambda d=dp_int, v=var: self._send_dps(d, v.get())
                       ).pack(side=tk.LEFT, padx=(12, 0))

        elif dtype == "Integer":
            mn  = vals.get("min", 0)
            mx  = vals.get("max", 100)
            stp = vals.get("step", 1)
            var = tk.IntVar(value=mn)
            self._dps_widgets[dp_str] = var
            frame = ttk.Frame(parent)
            frame.grid(row=grid_row, column=2, sticky=tk.EW)
            # slider
            sl = ttk.Scale(frame, from_=mn, to=mx, orient=tk.HORIZONTAL, variable=var,
                           command=lambda v, sv=var: sv.set(int(float(v))))
            sl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
            ttk.Spinbox(frame, from_=mn, to=mx, increment=stp,
                        textvariable=var, width=7).pack(side=tk.LEFT, padx=4)
            ttk.Button(frame, text="Send",
                       command=lambda d=dp_int, v=var: self._send_dps(d, v.get())
                       ).pack(side=tk.LEFT, padx=(8, 0))

        elif dtype == "Enum":
            choices = vals.get("range", [])
            var = tk.StringVar(value=choices[0] if choices else "")
            self._dps_widgets[dp_str] = var
            frame = ttk.Frame(parent)
            frame.grid(row=grid_row, column=2, sticky=tk.W)
            ttk.Combobox(frame, textvariable=var, values=choices,
                         state="readonly", width=22).pack(side=tk.LEFT, padx=4)
            ttk.Button(frame, text="Send",
                       command=lambda d=dp_int, v=var: self._send_dps(d, v.get())
                       ).pack(side=tk.LEFT, padx=(8, 0))

        elif dtype == "Raw":
            var = tk.StringVar(value="")
            self._dps_widgets[dp_str] = var
            frame = ttk.Frame(parent)
            frame.grid(row=grid_row, column=2, sticky=tk.EW)
            ttk.Entry(frame, textvariable=var, width=28,
                      font=("Consolas", 9)).pack(side=tk.LEFT, padx=4)
            ttk.Label(frame, text="hex", foreground="#888").pack(side=tk.LEFT)
            ttk.Button(frame, text="Send",
                       command=lambda d=dp_int, v=var: self._send_raw_hex(d, v.get())
                       ).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(parent, text=meta["description"],
                  foreground="#666", font=("Segoe UI", 8)).grid(
            row=grid_row, column=3, padx=(8, 4), pady=5, sticky=tk.W)

        parent.columnconfigure(3, weight=1)

    # ---------------------------------------------------- scan tab
    def _build_scan_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, padx=4, pady=4)
        ttk.Button(top, text="Scan Network (UDP ~8 s)",
                   command=self._do_scan).pack(side=tk.LEFT)
        self._scan_status = ttk.Label(top, text="")
        self._scan_status.pack(side=tk.LEFT, padx=10)

        cols = ("ip", "name", "id", "version")
        tree = ttk.Treeview(parent, columns=cols, show="headings", height=10)
        tree.heading("ip",      text="IP Address")
        tree.heading("name",    text="Name / Product")
        tree.heading("id",      text="Device / Gateway ID")
        tree.heading("version", text="Ver.")
        tree.column("ip",      width=130, anchor=tk.W)
        tree.column("name",    width=160, anchor=tk.W)
        tree.column("id",      width=210, anchor=tk.W)
        tree.column("version", width=55,  anchor=tk.CENTER, stretch=False)

        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0), pady=4)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, pady=4, padx=(0, 4))

        tree.bind("<Double-1>", self._on_scan_double_click)
        self._scan_tree = tree
        self._scanned: list[dict] = []

        ttk.Label(parent, text="Double-click a row to fill connection fields",
                  foreground="#888").pack(pady=2)

    # ----------------------------------------------------- raw DPS tab
    def _build_raw_tab(self, parent):
        lf = ttk.LabelFrame(parent, text="Send any DPS value", padding=10)
        lf.pack(fill=tk.X, padx=4, pady=6)

        ttk.Label(lf, text="DPS Number:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self._raw_dp_var = tk.StringVar()
        ttk.Entry(lf, textvariable=self._raw_dp_var, width=8).grid(
            row=0, column=1, sticky=tk.W, padx=6, pady=3)

        ttk.Label(lf, text="Value:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self._raw_val_var = tk.StringVar()
        ttk.Entry(lf, textvariable=self._raw_val_var, width=30).grid(
            row=1, column=1, sticky=tk.EW, padx=6, pady=3)

        ttk.Label(lf, text="Type hint:").grid(row=2, column=0, sticky=tk.W)
        self._raw_type_var = tk.StringVar(value="auto")
        ttk.Combobox(lf, textvariable=self._raw_type_var,
                     values=["auto", "bool", "int", "float", "str", "hex"],
                     state="readonly", width=10).grid(
            row=2, column=1, sticky=tk.W, padx=6)

        lf.columnconfigure(1, weight=1)

        ttk.Label(lf, text="auto: bool->int->float->str  |  hex: decode hex string to bytes",
                  foreground="#777", font=("Segoe UI", 8)).grid(
            row=3, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))

        ttk.Button(lf, text="Send Raw DPS",
                   command=self._do_send_raw).grid(
            row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))

        qb = ttk.LabelFrame(parent, text="Quick value helpers", padding=8)
        qb.pack(fill=tk.X, padx=4, pady=4)
        for label, val in [("Set True", "true"), ("Set False", "false")]:
            ttk.Button(qb, text=label,
                       command=lambda v=val: self._raw_val_var.set(v)).pack(side=tk.LEFT, padx=4)

        # DPS reference table
        ref_lf = ttk.LabelFrame(parent, text="DPS Reference (all DPS)", padding=4)
        ref_lf.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        rcols = ("dp", "code", "type", "writable", "description")
        rtree = ttk.Treeview(ref_lf, columns=rcols, show="headings", height=8)
        rtree.heading("dp",          text="DP")
        rtree.heading("code",        text="Code")
        rtree.heading("type",        text="Type")
        rtree.heading("writable",    text="Writable")
        rtree.heading("description", text="Description")
        rtree.column("dp",          width=40,  anchor=tk.CENTER, stretch=False)
        rtree.column("code",        width=165, anchor=tk.W)
        rtree.column("type",        width=65,  anchor=tk.CENTER, stretch=False)
        rtree.column("writable",    width=60,  anchor=tk.CENTER, stretch=False)
        rtree.column("description", width=220, anchor=tk.W)

        rvsb = ttk.Scrollbar(ref_lf, orient=tk.VERTICAL, command=rtree.yview)
        rtree.configure(yscrollcommand=rvsb.set)
        rtree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rvsb.pack(side=tk.RIGHT, fill=tk.Y)

        rtree.tag_configure("w",  background="#d4edda")
        rtree.tag_configure("ro", background="#f8f9fa")

        for dp_str, meta in DEVICE_MAPPING.items():
            tag = "w" if meta["writable"] else "ro"
            rtree.insert("", tk.END, values=(
                dp_str,
                meta["code"],
                meta["type"],
                "Yes" if meta["writable"] else "No",
                meta["description"],
            ), tags=(tag,))

        # Double-click ref row -> fill DP field
        def _ref_click(_e):
            sel = rtree.selection()
            if sel:
                dp_val = rtree.item(sel[0], "values")[0]
                self._raw_dp_var.set(dp_val)
        rtree.bind("<Double-1>", _ref_click)

    # ---------------------------------------------------- log panel
    def _build_log_panel(self, parent):
        lf = ttk.LabelFrame(parent, text="Activity Log", padding=8)
        lf.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self._log_box = scrolledtext.ScrolledText(
            lf, state=tk.DISABLED, wrap=tk.WORD,
            font=("Consolas", 9), background="#1e1e1e", foreground="#d4d4d4")
        self._log_box.pack(fill=tk.BOTH, expand=True)

        self._log_box.tag_config("INFO",  foreground="#9cdcfe")
        self._log_box.tag_config("OK",    foreground="#4ec9b0")
        self._log_box.tag_config("WARN",  foreground="#ce9178")
        self._log_box.tag_config("ERROR", foreground="#f44747")
        self._log_box.tag_config("CMD",   foreground="#dcdcaa")
        self._log_box.tag_config("ts",    foreground="#569cd6")

        ttk.Button(lf, text="Clear Log",
                   command=self._clear_log).pack(anchor=tk.E, pady=(4, 0))

    # ================================================================ logging
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

    # ================================================================ profiles
    def _refresh_profile_dropdown(self):
        names = sorted(self._profiles.keys())
        self._profile_cb["values"] = names
        if names:
            self._profile_var.set(names[0])
            self._load_selected_profile(names[0])

    def _load_profile(self, _event=None):
        self._load_selected_profile(self._profile_var.get())

    def _load_selected_profile(self, name: str):
        p = self._profiles.get(name)
        if not p:
            return
        for key, var in self._conn_vars.items():
            var.set(p.get(key, ""))
        self._version_var.set(p.get("version", "3.4"))
        self._log(f"Profile loaded: {name}", "INFO")

    def _save_profile(self):
        name = self._conn_vars["name"].get().strip()
        if not name:
            name = simpledialog.askstring("Profile Name",
                                          "Enter a name for this profile:",
                                          parent=self)
            if not name:
                return
            self._conn_vars["name"].set(name)

        data = {k: v.get() for k, v in self._conn_vars.items()}
        data["version"] = self._version_var.get()
        self._profiles[name] = data
        save_profiles(self._profiles)
        self._refresh_profile_dropdown()
        self._profile_var.set(name)
        self._log(f"Profile saved: {name}  ({PROFILES_FILE})", "OK")

    def _delete_profile(self):
        name = self._profile_var.get()
        if not name or name not in self._profiles:
            messagebox.showwarning("No profile", "Select a saved profile to delete.", parent=self)
            return
        if not messagebox.askyesno("Delete", f"Delete profile '{name}'?", parent=self):
            return
        del self._profiles[name]
        save_profiles(self._profiles)
        self._refresh_profile_dropdown()
        self._log(f"Profile deleted: {name}", "WARN")

    # ================================================================ helpers
    def _conn(self) -> tuple[str, str, str, float] | None:
        ip        = self._conn_vars["ip"].get().strip()
        device_id = self._conn_vars["device_id"].get().strip()
        local_key = self._conn_vars["local_key"].get().strip()
        version   = float(self._version_var.get())
        if not ip or not device_id or not local_key:
            messagebox.showwarning("Missing fields",
                                   "IP Address, Device ID and Local Key are required.",
                                   parent=self)
            return None
        return ip, device_id, local_key, version

    def _run_thread(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    def _set_ping_label(self, ok: bool | None):
        if ok is True:
            self._ping_label.configure(text="  Reachable",   foreground="#28a745")
        elif ok is False:
            self._ping_label.configure(text="  Unreachable", foreground="#dc3545")
        else:
            self._ping_label.configure(text="  Unknown",     foreground="gray")

    # ================================================================ actions

    # ---------------------------------------------------- ping
    def _do_ping(self):
        conn = self._conn()
        if not conn:
            return
        ip, *_ = conn
        self._log(f"Pinging {ip}:6668 ...")
        self._run_thread(self._ping_thread, ip)

    def _ping_thread(self, ip: str):
        ok = self._worker.ping(ip)
        self.after(0, self._set_ping_label, ok)
        level = "OK" if ok else "WARN"
        msg   = f"Ping {ip} -- {'reachable on port 6668' if ok else 'no response on port 6668'}"
        self.after(0, self._log, msg, level)

    # -------------------------------------------------- get status
    def _do_refresh(self):
        conn = self._conn()
        if not conn:
            return
        ip, device_id, local_key, version = conn
        self._log(f"Fetching status from {ip} ...")
        self._run_thread(self._refresh_thread, ip, device_id, local_key, version)

    def _refresh_thread(self, ip, device_id, local_key, version):
        def _log(msg, lvl="INFO"):
            self.after(0, self._log, msg, lvl)
        try:
            result = self._worker.get_status(ip, device_id, local_key, version, log_cb=_log)
            self.after(0, self._handle_status, result)
        except Exception as exc:
            self.after(0, self._log, f"Get status failed: {exc}", "ERROR")

    def _handle_status(self, result: dict):
        if "Error" in result:
            self._log(f"Device error: {result.get('Error')}  Payload: {result}", "ERROR")
            return
        dps: dict = result.get("dps", {})
        if not dps:
            self._log(f"No DPS in response: {result}", "WARN")
            return
        self._last_dps = {str(k): v for k, v in dps.items()}
        self._log(f"Status OK -- {len(dps)} DPS values", "OK")
        self._populate_status_tree(self._last_dps)
        self._sync_fn_controls(self._last_dps)

    def _populate_status_tree(self, dps: dict[str, Any]):
        for row in self._tree.get_children():
            self._tree.delete(row)
        for dp_str in sorted(dps.keys(), key=lambda x: int(x)):
            value    = dps[dp_str]
            meta     = DEVICE_MAPPING.get(dp_str, {})
            code     = meta.get("code", "unknown")
            dtype    = meta.get("type", "?")
            desc     = meta.get("description", "")
            writable = meta.get("writable", False)

            if isinstance(value, bool):
                tag = "true" if value else "false"
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                tag = "num"
            elif dtype == "Enum":
                tag = "enum"
            elif dtype == "Raw":
                tag = "raw"
            else:
                tag = "writable" if writable else "readonly"

            self._tree.insert("", tk.END,
                               values=(dp_str, code, dtype, value, desc),
                               tags=(tag,))

    def _sync_fn_controls(self, dps: dict[str, Any]):
        """Push live DPS values into the Functions tab controls."""
        for dp_str, widget_var in self._dps_widgets.items():
            value = dps.get(dp_str)
            if value is None:
                continue
            meta  = DEVICE_MAPPING.get(dp_str, {})
            dtype = meta.get("type", "")
            try:
                if dtype == "Boolean" and isinstance(widget_var, tk.BooleanVar):
                    widget_var.set(bool(value))
                elif dtype == "Integer" and isinstance(widget_var, tk.IntVar):
                    widget_var.set(int(value))
                elif isinstance(widget_var, tk.StringVar):
                    widget_var.set(str(value))
            except Exception:
                pass

    # -------------------------------------------------- live monitor
    def _toggle_live_monitor(self):
        if self._live_var.get():
            self._start_live_monitor()
        else:
            self._stop_live_monitor()

    def _start_live_monitor(self):
        conn = self._conn()
        if not conn:
            self._live_var.set(False)
            return
        ip, device_id, local_key, version = conn
        self._ping_running = True
        self._last_status_time = 0.0
        self._was_reachable = None
        self._ping_thread = threading.Thread(
            target=self._live_ping_loop,
            args=(ip, device_id, local_key, version),
            daemon=True,
        )
        self._ping_thread.start()
        self._log(f"Live Monitor ON  --  pinging {ip} every 1 s, status every {self.STATUS_POLL_INTERVAL} s", "INFO")

    def _stop_live_monitor(self):
        self._ping_running = False
        self._ping_thread = None
        self._live_var.set(False)
        self._log("Live Monitor OFF", "INFO")

    def _live_ping_loop(self, ip: str, device_id: str, local_key: str, version: float):
        while self._ping_running:
            # ping first — no leading sleep so Live Monitor start and reconnects
            # are acted on immediately without waiting a full second
            ok = self._worker.ping(ip)
            self.after(0, self._set_ping_label, ok)

            # log only on state transitions
            if ok and not self._was_reachable:
                if self._was_reachable is not None:  # suppress very first connect msg
                    self.after(0, self._log, f"Device back online at {ip}", "OK")
                # force immediate status fetch on (re)connect
                self._last_status_time = 0.0
            elif not ok and self._was_reachable:
                self.after(0, self._log, f"Device offline at {ip}", "WARN")
            self._was_reachable = ok

            if ok and (time.time() - self._last_status_time) >= self.STATUS_POLL_INTERVAL:
                self._last_status_time = time.time()
                self.after(0, self._log, f"Fetching status from {ip} ...", "INFO")
                def _log(msg, lvl="INFO"):
                    self.after(0, self._log, msg, lvl)
                try:
                    result = self._worker.get_status(ip, device_id, local_key, version, log_cb=_log)
                    self.after(0, self._handle_status, result)
                except Exception as exc:
                    self.after(0, self._log, f"Live status error: {exc}", "ERROR")

            # sleep at the end — preserves ~1 s cadence without delaying first/reconnect check
            if self._ping_running:
                time.sleep(1)

    # --------------------------------- status tree double-click
    def _on_status_double_click(self, _event):
        item = self._tree.selection()
        if not item:
            return
        dp_str = self._tree.item(item[0], "values")[0]
        meta   = DEVICE_MAPPING.get(dp_str, {})
        if not meta.get("writable"):
            self._log(f"DP {dp_str} ({meta.get('code','?')}) is read-only", "WARN")
            return
        self._nb.select(1)
        self._log(f"Switched to Functions tab for DP {dp_str} ({meta.get('code','')})", "INFO")

    # ------------------------------------------------ quick actions
    def _quick_action(self, dp: int, value: Any):
        conn = self._conn()
        if not conn:
            return
        ip, device_id, local_key, version = conn
        self._log(f"Quick action  DP {dp} = {value!r}", "CMD")
        self._run_thread(self._send_thread, ip, device_id, local_key, version, dp, value)

    # ------------------------------------------ send DPS (typed value)
    def _send_dps(self, dp: int, value: Any):
        conn = self._conn()
        if not conn:
            return
        ip, device_id, local_key, version = conn
        self._log(f"CMD  DP {dp} = {value!r}  ({type(value).__name__})", "CMD")
        self._run_thread(self._send_thread, ip, device_id, local_key, version, dp, value)

    # --------------------------------------------- send raw hex DPS
    def _send_raw_hex(self, dp: int, hex_str: str):
        hex_str = hex_str.strip().replace(" ", "")
        try:
            value = bytes.fromhex(hex_str)
        except ValueError:
            messagebox.showerror("Invalid hex",
                                 f"'{hex_str}' is not valid hex. Use e.g. 0a1b2c",
                                 parent=self)
            return
        conn = self._conn()
        if not conn:
            return
        ip, device_id, local_key, version = conn
        self._log(f"CMD RAW HEX  DP {dp} = {hex_str}  ({len(value)} bytes)", "CMD")
        self._run_thread(self._send_thread, ip, device_id, local_key, version, dp, value)

    def _send_thread(self, ip, device_id, local_key, version, dp, value):
        try:
            result = self._worker.set_value(ip, device_id, local_key, version, dp, value)
            if result and "Error" in result:
                self.after(0, self._log,
                           f"Send FAILED DP {dp}: {result.get('Error')}  ({result})", "ERROR")
            else:
                self.after(0, self._log,
                           f"Send OK  DP {dp} = {value!r}  resp: {result}", "OK")
                time.sleep(0.5)
                self.after(0, self._do_refresh)
        except Exception as exc:
            self.after(0, self._log, f"Send error DP {dp}: {exc}", "ERROR")

    # ------------------------------------------------- LAN scan
    def _do_scan(self):
        self._scan_status.configure(text="Scanning...")
        for row in self._scan_tree.get_children():
            self._scan_tree.delete(row)
        self._scanned = []
        self._log("LAN scan started (UDP broadcast ~8 s) ...")
        self._run_thread(self._scan_thread)

    def _scan_thread(self):
        try:
            devices = self._worker.scan()
            self.after(0, self._handle_scan, devices)
        except Exception as exc:
            self.after(0, self._log, f"Scan error: {exc}", "ERROR")
            self.after(0, lambda: self._scan_status.configure(text="Error"))

    def _handle_scan(self, devices: list[dict]):
        self._scanned = devices
        if not devices:
            self._scan_status.configure(text="No devices found")
            self._log("Scan complete -- no Tuya devices found on this subnet", "WARN")
            return
        self._log(f"Scan complete -- {len(devices)} device(s) found", "OK")
        for d in devices:
            ip   = d.get("ip", "")
            name = d.get("name", d.get("productKey", "Unknown"))
            did  = d.get("gwId", d.get("id", ""))
            ver  = d.get("version", "?")
            self._scan_tree.insert("", tk.END, values=(ip, name, did, ver))
            self._log(f"  {ip}  {name}  {did}  v{ver}", "INFO")
        self._scan_status.configure(
            text=f"{len(devices)} found -- double-click to use")

    def _on_scan_double_click(self, _event):
        item = self._scan_tree.selection()
        if not item:
            return
        ip, name, did, ver = self._scan_tree.item(item[0], "values")
        self._conn_vars["ip"].set(ip)
        self._conn_vars["device_id"].set(did)
        if str(ver) in ["3.3", "3.4", "3.5"]:
            self._version_var.set(str(ver))
        if name and name != "Unknown":
            self._conn_vars["name"].set(name)
        self._log(f"Filled from scan -- IP={ip}  ID={did}  v{ver}", "INFO")
        self._nb.select(0)

    # ------------------------------------------ raw DPS tab send
    def _do_send_raw(self):
        conn = self._conn()
        if not conn:
            return
        dp_str  = self._raw_dp_var.get().strip()
        val_str = self._raw_val_var.get().strip()
        if not dp_str or not val_str:
            messagebox.showwarning("Missing", "DPS number and value are required.", parent=self)
            return
        try:
            dp = int(dp_str)
        except ValueError:
            messagebox.showerror("Invalid DP", "DPS number must be an integer.", parent=self)
            return

        hint  = self._raw_type_var.get()
        value = self._coerce(val_str, hint)
        if value is None:
            return

        ip, device_id, local_key, version = conn
        self._log(f"CMD RAW  DP {dp} = {value!r}  (hint: {hint})", "CMD")
        self._run_thread(self._send_thread, ip, device_id, local_key, version, dp, value)

    def _coerce(self, val_str: str, hint: str) -> Any:
        if hint == "bool":
            if val_str.lower() in ("true",  "1", "yes", "on"):  return True
            if val_str.lower() in ("false", "0", "no",  "off"): return False
            messagebox.showerror("Bad bool", f"Cannot parse '{val_str}' as boolean.", parent=self)
            return None
        if hint == "int":
            try:   return int(val_str)
            except ValueError:
                messagebox.showerror("Bad int", f"Cannot parse '{val_str}' as int.", parent=self)
                return None
        if hint == "float":
            try:   return float(val_str)
            except ValueError:
                messagebox.showerror("Bad float", f"Cannot parse '{val_str}' as float.", parent=self)
                return None
        if hint == "str":
            return val_str
        if hint == "hex":
            try:   return bytes.fromhex(val_str.replace(" ", ""))
            except ValueError:
                messagebox.showerror("Bad hex", f"Cannot parse '{val_str}' as hex bytes.", parent=self)
                return None
        # auto
        if val_str.lower() == "true":  return True
        if val_str.lower() == "false": return False
        try:   return int(val_str)
        except ValueError: pass
        try:   return float(val_str)
        except ValueError: pass
        return val_str

    # ================================================================ close
    def _on_close(self):
        self._stop_live_monitor()
        self.destroy()


# ---------------------------------------------------------------------------
# Tooltip helper (module-level so it can be used without self)
# ---------------------------------------------------------------------------
def _add_tooltip(widget, text: str):
    tip = None
    def enter(_e):
        nonlocal tip
        x = widget.winfo_rootx() + 24
        y = widget.winfo_rooty() + 24
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(tip, text=text, background="#ffffe0", relief=tk.SOLID,
                 borderwidth=1, font=("Segoe UI", 8),
                 wraplength=300, justify=tk.LEFT, padx=4, pady=2).pack()
    def leave(_e):
        nonlocal tip
        if tip:
            tip.destroy()
            tip = None
    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


# ---------------------------------------------------------------------------
def main():
    app = TuyaProbeApp()
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
