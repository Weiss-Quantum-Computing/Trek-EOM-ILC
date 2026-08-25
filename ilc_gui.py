#!/usr/bin/env python3
"""EOM-ILC GUI -- panel driver for the pre-distortion / ILC loop, with plots.

The third bench panel, sibling to the BK4063B AWG GUI and Scope Grab. Where
those talk to one instrument each, this one drives the loop that sits between
them: init a session from a target, step it from captured files, or run the
closed bench loop (upload -> capture -> update) hands-off -- and see the
inputs and outputs plotted at every step, which is the point of the window.

It is a front end, not a re-implementation: the maths comes from `eomilc`,
the state file is the same `drive_<stem>.state.npz` that `run_ilc.py` and
`ilc_bench.py` read and write, and the bench mode calls `ilc_bench`'s own
helpers (channel verification, fixed-mapping upload, alignment check). Manual
CLI steps and GUI steps interleave freely on the same state.

Run with the Anaconda interpreter -- it is the only one here with scipy,
pandas and pyvisa together:

    C:\\ProgramData\\anaconda3\\pythonw.exe ilc_gui.py

Bench mode needs the AWG GUI and Scope Grab CLOSED first: both hold their
VISA sessions open.
"""
from __future__ import annotations

import contextlib
import datetime
import glob
import io
import json
import os
import queue
import re
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import numpy as np
    import pandas as pd
    import scipy  # noqa: F401 -- eomilc.plant needs it; fail early and clearly
except ModuleNotFoundError as _e:
    _r = tk.Tk(); _r.withdraw()
    messagebox.showerror(
        "EOM-ILC GUI",
        f"Missing package: {_e.name}.\n\nRun this with the Anaconda "
        f"interpreter -- C:\\ProgramData\\anaconda3\\pythonw.exe -- which is "
        f"the only one on this machine with scipy, pandas and pyvisa together.")
    raise SystemExit(1)

import matplotlib
matplotlib.use("TkAgg")
matplotlib.rcParams["font.size"] = 8
from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                               NavigationToolbar2Tk)
from matplotlib.figure import Figure

from eomilc import scope as scopeio, plant as plantmod, ilc, outputs
from eomilc.config import CHANNELS, LIMITS, HV_PER_MON
import ilc_bench            # the debugged bench helpers; main() is guarded
import run_ilc              # load_target and the state-file conventions

# Remembered between sessions, kept out of the repo so a git pull cannot
# clobber it -- same convention as the sibling panels.
CONFIG_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                           "EOM-ILC-GUI", "config.json")

RUN_DIR = os.path.join(HERE, "run")
SIBLINGS = os.path.dirname(HERE)               # the folder holding all the bench repos
AWG_WAVEFORMS = run_ilc.AWG_WAVEFORMS          # env-overridable, one definition

# The stored-name cap on the 4063B (15 chars for <name>.bin, so 11 typed;
# past it the front panel wedges until a power cycle). Bench mode re-reads it
# from the AWG GUI module so there is one live definition on this bench; this
# constant only guards `init`, which runs before that module is loaded.
NAME_LIMIT = 11

# Per-channel bench wiring -- the pairing every characterisation script uses:
# (AWG ch, scope drive ch, scope monitor ch). Colours match the AWG GUI's
# CH1/CH2 so a glance at any panel identifies the channel on all of them.
CH_DEFAULTS = {
    "EO1": dict(mon_col="CH3", awg_ch=1, scope_ch=3,
                frf="frf_WIDE_X1.csv", colour="#1f77b4"),
    "EO2": dict(mon_col="CH4", awg_ch=2, scope_ch=4,
                frf="frf_WIDE_X2.csv", colour="#d62728"),
}
TARGET_COLOUR = "#222222"
PRED_COLOUR = "#8a8a8a"
GHOST_ALPHA = 0.35

# The model ladder: what the update divides the error by, in increasing order
# of how much of the chain it knows about.  The first three are parametric
# (eomilc.plant.Plant with the unused terms zeroed -- a gain-only Plant IS the
# zeroth-order case); the last is the measured inverse, whose reach is set by
# the taper band rather than by f_cut.
MODEL_LABELS = (
    ("gain only (0th order)", "static"),
    ("one pole (1st order)", "one_pole"),
    ("second order (resonant)", "resonant"),
    ("measured FRF (nonparametric)", "frf"),
)
LABEL2KEY = dict(MODEL_LABELS)
KEY2LABEL = {k: l for l, k in MODEL_LABELS}
PARAMS_FOR = {"static": ("gain",), "one_pole": ("gain", "tau"),
              "resonant": ("gain", "fn", "zeta"), "frf": ()}
DESC_FOR = {"static": "gain only", "one_pole": "one pole",
            "resonant": "2nd order"}


# ---------------------------------------------------------------- session
class Session:
    """One loaded ILC state: the Loop plus everything the state file carries
    that the Loop does not (drive, iteration counter, stem, alignment)."""

    def __init__(self, state_path, loop, t, u, iteration, stem, full_scale, t_off):
        self.state_path = state_path
        self.loop = loop
        self.t = t
        self.u = u
        self.iteration = int(iteration)
        self.stem = str(stem)
        self.full_scale = float(full_scale)
        self.t_off = float(t_off)
        self.snapshots = []          # [{it, y, m}] measurements seen this session

    @property
    def channel(self):
        return self.loop.channel.name


def load_session(path) -> Session:
    st = run_ilc.load_state(path)
    loop = run_ilc.build_loop(st)
    return Session(state_path=os.path.abspath(path), loop=loop,
                   t=st["t"], u=st["u"], iteration=int(st["iteration"]),
                   stem=str(st["name"]), full_scale=float(st["full_scale"]),
                   t_off=float(st["t_offset"]))


def save_session(s: Session):
    """Same keys as run_ilc / ilc_bench write, so the CLIs can resume this."""
    lp = s.loop
    np.savez(s.state_path, t=s.t, target=lp.target, u=s.u, dt=lp.dt,
             channel=lp.channel.name, gain=lp.plant.gain, tau=lp.plant.tau,
             offset=lp.plant.offset, tau2=lp.plant.tau2, fn=lp.plant.fn,
             zeta=lp.plant.zeta, full_scale=s.full_scale, name=s.stem,
             gamma=lp.gamma, f_cut=lp.f_cut, iteration=s.iteration,
             t_offset=s.t_off, history=np.array(lp.history, dtype=object))


def read_captures(pattern, mon_col, t, t_off):
    """Average every capture the glob matches, with the span guard from
    run_ilc: one zoomed file extrapolated flat once manufactured 172 V of
    fake error out of a real 26 V, so a short capture is a hard refusal."""
    files = sorted(glob.glob(pattern))
    if not files:
        raise RuntimeError(f"no scope files matched {pattern!r}")
    traces = []
    for f in files:
        tr = scopeio.load(f)
        lo, hi = tr.t[0] - t_off, tr.t[-1] - t_off
        if lo > t[0] + 1e-4 or hi < t[-1] - 1e-4:
            raise RuntimeError(
                f"{os.path.basename(f)} spans {lo*1e3:.2f}..{hi*1e3:.2f} ms but "
                f"the waveform runs {t[0]*1e3:.2f}..{t[-1]*1e3:.2f} ms. A zoomed "
                f"or mismatched capture matched the glob -- tighten the pattern "
                f"so only full-window captures of THIS iteration match.")
        traces.append(scopeio.resample(tr.t, tr[mon_col], t, t_offset=t_off))
    return ilc.averaged(traces), files


class _QueueWriter(io.TextIOBase):
    """Routes print() output from the library helpers into the GUI log."""

    def __init__(self, q):
        self.q, self.buf = q, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(("log", line))
        return len(s)

    def flush(self):
        if self.buf:
            self.q.put(("log", self.buf))
            self.buf = ""


# -------------------------------------------------------------------- app
class App:
    def __init__(self, root):
        self.root = root
        root.title("EOM-ILC -- iterative learning control")
        self.msgs = queue.Queue()
        self.stop_evt = threading.Event()
        self.busy = False
        self.session: Session | None = None
        self._modules = None          # (scope_grab, awg_gui) once bench-loaded
        self.cfg = self._load_config()
        root.geometry(self.cfg.get("geometry", "1380x880"))

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.pump)

    # ---------------------------------------------------------------- config
    def _load_config(self):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_config(self):
        c = dict(self.cfg)
        c.update(geometry=self.root.winfo_geometry(),
                 state=self.state_var.get(), target=self.target_var.get(),
                 measured=self.meas_var.get(), frf=self.frf_var.get(),
                 f_use=self.fuse_var.get(), f_max=self.fmax_var.get(),
                 model=self.model_var.get(),
                 repeats=self.repeats_var.get(), iterations=self.iters_var.get())
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(c, f, indent=1)
        except OSError:
            pass

    def on_close(self):
        if self.busy and not messagebox.askyesno(
                "Busy", "A run is still going. Close anyway?\n"
                        "(Instruments are closed by the worker's own cleanup.)"):
            return
        self.stop_evt.set()
        self._save_config()
        self.root.destroy()

    # ------------------------------------------------------------------- ui
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=4)
        outer.pack(fill="both", expand=True)

        left = ttk.Frame(outer)
        left.pack(side="left", fill="y", padx=(0, 6))
        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True)

        # ---- session -------------------------------------------------
        sf = ttk.LabelFrame(left, text="Session", padding=4)
        sf.pack(fill="x", pady=(0, 4))
        self.state_var = tk.StringVar(value=self.cfg.get("state", ""))
        self._path_row(sf, 0, "State", self.state_var,
                       lambda: self._browse(self.state_var, "State files",
                                            "*.state.npz", RUN_DIR))
        b = ttk.Button(sf, text="Load state", command=self.do_load)
        b.grid(row=0, column=3, padx=2)
        self._actions = [b]

        ttk.Separator(sf).grid(row=1, column=0, columnspan=4, sticky="ew", pady=3)

        self.target_var = tk.StringVar(value=self.cfg.get("target", ""))
        self._path_row(sf, 2, "Target", self.target_var,
                       lambda: self._browse(self.target_var, "Target CSV",
                                            "*.csv", os.path.join(HERE, "waveforms")))
        self.channel_var = tk.StringVar(value="EO1")
        self.stem_var = tk.StringVar(value="")
        r3 = ttk.Frame(sf); r3.grid(row=3, column=0, columnspan=4, sticky="ew", pady=1)
        ttk.Label(r3, text="Channel").pack(side="left")
        cb = ttk.Combobox(r3, textvariable=self.channel_var, width=5,
                          values=list(CHANNELS), state="readonly")
        cb.pack(side="left", padx=(2, 8))
        cb.bind("<<ComboboxSelected>>", lambda e: self._apply_channel_defaults())
        ttk.Label(r3, text="Name stem").pack(side="left")
        ttk.Entry(r3, textvariable=self.stem_var, width=9).pack(side="left", padx=2)
        ttk.Label(r3, text=f"(<= {NAME_LIMIT - 4} chars; '_iNN' is appended)"
                  ).pack(side="left")

        r4 = ttk.Frame(sf); r4.grid(row=4, column=0, columnspan=4, sticky="ew", pady=1)
        self.gamma_var = tk.StringVar(value="0.6")
        self.fcut_var = tk.StringVar(value="5000")
        self.toff_var = tk.StringVar(value="0.0")
        self.fs_var = tk.StringVar(value="10.0")
        for lab, var, w in (("gamma", self.gamma_var, 5),
                            ("f_cut Hz", self.fcut_var, 7),
                            ("t-offset us", self.toff_var, 6),
                            ("full scale V", self.fs_var, 5)):
            ttk.Label(r4, text=lab).pack(side="left", padx=(0, 2))
            ttk.Entry(r4, textvariable=var, width=w).pack(side="left", padx=(0, 8))

        b = ttk.Button(sf, text="Init first shot (model-based)", command=self.do_init)
        b.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(3, 0))
        self._actions.append(b)

        self.summary = ttk.Label(sf, text="no session loaded", justify="left",
                                 font=("Consolas", 8))
        self.summary.grid(row=6, column=0, columnspan=4, sticky="w", pady=(4, 0))
        sf.columnconfigure(1, weight=1)

        # ---- inverse model -------------------------------------------
        vf = ttk.LabelFrame(left, text="Inverse model (what the update "
                                       "divides the error by)", padding=4)
        vf.pack(fill="x", pady=(0, 4))
        self.model_var = tk.StringVar(
            value=self.cfg.get("model", KEY2LABEL["frf"]))
        if self.model_var.get() not in LABEL2KEY:
            self.model_var.set(KEY2LABEL["frf"])
        r0 = ttk.Frame(vf); r0.grid(row=0, column=0, columnspan=4, sticky="ew")
        ttk.Label(r0, text="Model").pack(side="left")
        mc = ttk.Combobox(r0, textvariable=self.model_var, state="readonly",
                          width=30, values=[l for l, _ in MODEL_LABELS])
        mc.pack(side="left", padx=(2, 0))
        mc.bind("<<ComboboxSelected>>", lambda e: self._update_model_fields())

        r1 = ttk.Frame(vf); r1.grid(row=1, column=0, columnspan=4,
                                    sticky="ew", pady=1)
        self.pgain_var = tk.StringVar()
        self.ptau_var = tk.StringVar()
        self.pfn_var = tk.StringVar()
        self.pzeta_var = tk.StringVar()
        self._param_vars = {"gain": self.pgain_var, "tau": self.ptau_var,
                            "fn": self.pfn_var, "zeta": self.pzeta_var}
        self._param_entries = {}
        for lab, key, w in (("gain", "gain", 7), ("tau us", "tau", 6),
                            ("fn Hz", "fn", 6), ("zeta", "zeta", 6)):
            ttk.Label(r1, text=lab).pack(side="left", padx=(0, 2))
            e = ttk.Entry(r1, textvariable=self._param_vars[key], width=w)
            e.pack(side="left", padx=(0, 8))
            self._param_entries[key] = e

        r2 = ttk.Frame(vf); r2.grid(row=2, column=0, columnspan=4,
                                    sticky="ew", pady=(2, 1))
        b = ttk.Button(r2, text="From calibration", command=self.do_calib)
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)
        b = ttk.Button(r2, text="Fit from measurement", command=self.do_fit)
        b.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._actions.append(b)

        self.frf_var = tk.StringVar(value=self.cfg.get("frf", ""))
        self._path_row(vf, 3, "FRF", self.frf_var,
                       lambda: self._browse(self.frf_var, "FRF CSV",
                                            "frf_*.csv", RUN_DIR))
        r4 = ttk.Frame(vf); r4.grid(row=4, column=0, columnspan=4, sticky="ew")
        self.fuse_var = tk.StringVar(value=self.cfg.get("f_use", "50e3"))
        self.fmax_var = tk.StringVar(value=self.cfg.get("f_max", "75e3"))
        ttk.Label(r4, text="full strength to Hz").pack(side="left")
        ttk.Entry(r4, textvariable=self.fuse_var, width=7).pack(side="left", padx=(2, 8))
        ttk.Label(r4, text="taper to zero at Hz").pack(side="left")
        ttk.Entry(r4, textvariable=self.fmax_var, width=7).pack(side="left", padx=2)
        b = ttk.Button(r4, text="Show FRF", command=self.do_show_frf)
        b.pack(side="right")
        self._actions.append(b)
        vf.columnconfigure(1, weight=1)
        self._update_model_fields()

        # ---- manual step ---------------------------------------------
        mf = ttk.LabelFrame(left, text="Step from captured files", padding=4)
        mf.pack(fill="x", pady=(0, 4))
        self.meas_var = tk.StringVar(value=self.cfg.get("measured", ""))
        self._path_row(mf, 0, "Captures", self.meas_var, self._browse_measured)
        r1 = ttk.Frame(mf); r1.grid(row=1, column=0, columnspan=4, sticky="ew", pady=1)
        ttk.Label(r1, text="monitor col").pack(side="left")
        self.moncol_var = tk.StringVar(value="CH3")
        ttk.Combobox(r1, textvariable=self.moncol_var, width=5,
                     values=("CH1", "CH2", "CH3", "CH4")).pack(side="left", padx=(2, 8))
        self.refit_var = tk.BooleanVar(value=False)
        self.force_var = tk.BooleanVar(value=False)
        self.zerobase_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text="refit plant", variable=self.refit_var).pack(side="left")
        ttk.Checkbutton(r1, text="force", variable=self.force_var).pack(side="left")
        ttk.Checkbutton(r1, text="zero baseline (not for MKJ)",
                        variable=self.zerobase_var).pack(side="left")
        self.step_btn = ttk.Button(mf, text="Step  (average captures -> update drive)",
                                   command=self.do_step)
        self.step_btn.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(3, 0))
        self._actions.append(self.step_btn)
        mf.columnconfigure(1, weight=1)

        # ---- bench loop ----------------------------------------------
        bf = ttk.LabelFrame(left, text="Bench loop (upload -> capture -> update)",
                            padding=4)
        bf.pack(fill="x", pady=(0, 4))
        r0 = ttk.Frame(bf); r0.grid(row=0, column=0, sticky="ew")
        self.awgch_var = tk.StringVar(value="1")
        self.scopech_var = tk.StringVar(value="3")
        self.iters_var = tk.StringVar(value=str(self.cfg.get("iterations", "2")))
        self.repeats_var = tk.StringVar(value=str(self.cfg.get("repeats", "64")))
        self.wait_var = tk.StringVar(value="30")
        for lab, var, w in (("AWG ch", self.awgch_var, 3),
                            ("scope ch", self.scopech_var, 3),
                            ("iterations", self.iters_var, 3),
                            ("repeats", self.repeats_var, 4),
                            ("wait s", self.wait_var, 4)):
            ttk.Label(r0, text=lab).pack(side="left", padx=(0, 2))
            ttk.Entry(r0, textvariable=var, width=w).pack(side="left", padx=(0, 6))
        self.skip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bf, text="skip setup checks (don't)",
                        variable=self.skip_var).grid(row=1, column=0, sticky="w")
        r2 = ttk.Frame(bf); r2.grid(row=2, column=0, sticky="ew", pady=(3, 0))
        self.bench_btn = ttk.Button(r2, text="Run bench loop", command=self.do_bench)
        self.bench_btn.pack(side="left", fill="x", expand=True)
        self._actions.append(self.bench_btn)
        self.stop_btn = ttk.Button(r2, text="Stop", command=self.stop_evt.set,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(4, 0))
        ttk.Label(bf, text="Close the AWG GUI and Scope Grab first -- both hold\n"
                           "their VISA sessions. Scope in HRES, full window.",
                  foreground="#666666").grid(row=3, column=0, sticky="w")
        bf.columnconfigure(0, weight=1)

        # ---- log ------------------------------------------------------
        lf = ttk.LabelFrame(left, text="Log", padding=2)
        lf.pack(fill="both", expand=True)
        self.log_text = tk.Text(lf, width=64, height=10, font=("Consolas", 8),
                                state="disabled", wrap="none")
        ysb = ttk.Scrollbar(lf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ysb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")

        # ---- status ---------------------------------------------------
        st = ttk.Frame(left)
        st.pack(fill="x", pady=(3, 0))
        self.status = ttk.Label(st, text="ready")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(st, length=160, mode="determinate")
        self.progress.pack(side="right")

        # ---- plot notebook -------------------------------------------
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.fig_wave, (self.ax_out, self.ax_drv) = self._tab("Waveforms", 2, sharex=True)
        self.fig_err, (self.ax_err,) = self._tab("Error", 1)
        self.fig_spec, (self.ax_spec,) = self._tab("Error spectrum", 1)
        self.fig_conv, (self.ax_conv,) = self._tab("Convergence", 1)
        self.fig_frf, self.ax_frf = self._tab("FRF", 3, sharex=True)

    def _tab(self, name, nrows, sharex=False):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=name)
        fig = Figure(figsize=(8.6, 6.2), dpi=100, constrained_layout=True)
        axes = fig.subplots(nrows, 1, sharex=sharex)
        axes = np.atleast_1d(axes)
        canvas = FigureCanvasTkAgg(fig, master=frame)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(canvas, frame)          # zoom/pan, scope habits
        fig._canvas = canvas
        return fig, axes

    def _path_row(self, parent, row, label, var, browse):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew",
                                                 padx=2)
        ttk.Button(parent, text="...", width=3, command=browse).grid(
            row=row, column=2)

    def _browse(self, var, name, pat, initdir):
        p = filedialog.askopenfilename(
            title=name, initialdir=os.path.dirname(var.get()) or initdir,
            filetypes=((name, pat), ("All files", "*.*")))
        if p:
            var.set(p)

    def _browse_measured(self):
        """Pick one capture file; the run-index suffix Scope Grab appends is
        replaced with * so the whole 64-shot sequence matches. Read the file
        list the step prints -- the glob averages EVERYTHING it matches."""
        p = filedialog.askopenfilename(
            title="One file of the capture sequence",
            initialdir=os.path.dirname(self.meas_var.get())
            or os.path.join(SIBLINGS, "scope_data"),
            filetypes=(("Scope CSV", "*.csv"), ("All files", "*.*")))
        if p:
            g = re.sub(r"_\d+\.csv$", "*.csv", p)
            self.meas_var.set(g if g != p else p)

    # ------------------------------------------------------------- plumbing
    def log(self, text):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        for line in str(text).splitlines() or [""]:
            self.log_text.insert("end", f"{stamp}  {line}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def pump(self):
        try:
            while True:
                kind, *rest = self.msgs.get_nowait()
                if kind == "log":
                    self.log(rest[0])
                elif kind == "status":
                    self.status.configure(text=rest[0])
                elif kind == "busy":
                    self.set_busy(rest[0])
                elif kind == "progress":
                    i, n = rest
                    self.progress.configure(maximum=max(n, 1), value=i)
                elif kind == "call":
                    rest[0]()
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in self._actions:
            b.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")
        if not busy:
            self.progress.configure(value=0)
            self.status.configure(text="ready")

    def run_worker(self, fn, status):
        if self.busy:
            return
        self.set_busy(True)
        self.stop_evt.clear()
        self.status.configure(text=status)

        def work():
            try:
                with contextlib.redirect_stdout(_QueueWriter(self.msgs)):
                    fn()
            except SystemExit as e:      # ilc_bench helpers refuse via sys.exit
                self.msgs.put(("log", f"ABORTED: {e}"))
            except Exception:
                self.msgs.put(("log", traceback.format_exc()))
            finally:
                sys.stdout.flush()
                self.msgs.put(("busy", False))
        threading.Thread(target=work, daemon=True).start()

    def _floats(self, **pairs):
        out = {}
        for k, v in pairs.items():
            try:
                out[k] = float(v.get())
            except ValueError:
                raise RuntimeError(f"{k} is not a number: {v.get()!r}")
        return out

    # -------------------------------------------------------- session setup
    def _apply_channel_defaults(self, channel=None):
        ch = channel or self.channel_var.get()
        d = CH_DEFAULTS[ch]
        self.moncol_var.set(d["mon_col"])
        self.awgch_var.set(str(d["awg_ch"]))
        self.scopech_var.set(str(d["scope_ch"]))
        # Point at this channel's wide-probe FRF unless the user browsed to
        # something that is not just the other channel's default.
        cur = os.path.basename(self.frf_var.get())
        if not cur or cur in {c["frf"] for c in CH_DEFAULTS.values()}:
            p = os.path.join(RUN_DIR, d["frf"])
            self.frf_var.set(p if os.path.exists(p) else "")

    # -------------------------------------------------------- model ladder
    def _model_key(self):
        return LABEL2KEY.get(self.model_var.get(), "frf")

    def _update_model_fields(self):
        """Only the parameters the selected model actually has are editable --
        a greyed box says 'this model does not know about that'."""
        need = PARAMS_FOR[self._model_key()]
        for key, e in self._param_entries.items():
            e.configure(state="normal" if key in need else "disabled")

    def _set_param_entries(self, p):
        """Plant -> panel entries (blank = the term is absent from p)."""
        self.pgain_var.set(f"{p.gain:.4f}")
        self.ptau_var.set(f"{p.tau*1e6:.2f}" if p.tau > 0 else "")
        self.pfn_var.set(f"{p.fn:.0f}" if p.fn > 0 else "")
        self.pzeta_var.set(f"{p.zeta:.3f}" if p.zeta > 0 else "")

    def _entry_params(self, key, strict):
        """Panel entries for model `key` -> dict.  strict=False returns None
        on any blank entry (caller falls back to calibration); strict=True
        raises with a hint at the two fill buttons."""
        vals = {}
        for k in PARAMS_FOR[key]:
            txt = self._param_vars[k].get().strip()
            if not txt:
                if not strict:
                    return None
                raise RuntimeError(
                    f"'{k}' is blank for the {KEY2LABEL[key]} model -- type a "
                    f"value, or use From calibration / Fit from measurement")
            try:
                vals[k] = float(txt)
            except ValueError:
                raise RuntimeError(f"{k} is not a number: {txt!r}")
        if "gain" in vals and vals["gain"] <= 0:
            raise RuntimeError("gain must be positive")
        if "tau" in vals and vals["tau"] <= 0:
            raise RuntimeError("tau must be positive (it is in microseconds)")
        if "fn" in vals and vals["fn"] <= 0:
            raise RuntimeError("fn must be positive")
        if "zeta" in vals and vals["zeta"] <= 0:
            raise RuntimeError("zeta must be positive")
        return vals

    def _plant_from(self, params, dt, offset=0.0):
        return plantmod.Plant(gain=params["gain"],
                              tau=params.get("tau", 0.0) * 1e-6,
                              fn=params.get("fn", 0.0),
                              zeta=params.get("zeta", 0.0),
                              offset=offset, dt=dt)

    def do_calib(self):
        """Fill the parameter boxes from the measured calibration tables at
        the amplitude actually in use -- fn falls with drive (the EOM
        capacitance is voltage dependent), so the amplitude matters."""
        try:
            if self.session is not None:
                ch = CHANNELS[self.session.channel]
                amp = float(np.ptp(self.session.loop.target))
            else:
                tpath = self.target_var.get().strip()
                if not os.path.exists(tpath):
                    raise RuntimeError(
                        "load a session or set a target file first -- the "
                        "tables are amplitude-dependent, so the target sets "
                        "which row applies")
                _, v = run_ilc.load_target(tpath)
                ch = CHANNELS[self.channel_var.get()]
                amp = float(np.ptp(v))
        except RuntimeError as e:
            return messagebox.showerror("From calibration", str(e))
        self.pgain_var.set(f"{ch.gain(amp):.4f}")
        self.ptau_var.set(f"{ch.tau(amp)*1e6:.2f}")
        self.pfn_var.set(f"{ch.fn(amp):.0f}")
        self.pzeta_var.set(f"{ch.zeta(amp):.3f}")
        self.log(f"calibration at {amp*HV_PER_MON:.0f} V pk-pk ({ch.name}): "
                 f"gain {ch.gain(amp):.4f}, tau {ch.tau(amp)*1e6:.2f} us "
                 f"(one-pole), fn {ch.fn(amp):.0f} Hz, zeta {ch.zeta(amp):.3f} "
                 f"(tables measured 2026-08-20/21)")

    def do_fit(self):
        """Identify the selected parametric model from real data: the last
        measured iteration if there is one, else the capture glob."""
        key = self._model_key()
        if key == "frf":
            return messagebox.showinfo(
                "Fit from measurement",
                "The measured FRF is not fitted here -- build one with "
                "tools/sysid_make.py (Schroeder probe) and tools/sysid_fit.py "
                "(64-shot sequence -> run\\frf_<name>.csv), then browse to it.")
        if self.session is None:
            return messagebox.showerror("Fit", "load or init a session first")
        pattern = self.meas_var.get().strip()
        mon = self.moncol_var.get()
        self.run_worker(lambda: self._fit_work(key, pattern, mon),
                        "fitting plant from measurement...")

    def _fit_work(self, model_key, pattern, mon):
        s = self.session
        if s.snapshots:
            snap = s.snapshots[-1]
            y, src = snap["y"], f"the iteration-{snap['it']} measurement"
        elif pattern:
            y, files = read_captures(pattern, mon, s.t, s.t_off)
            src = f"{len(files)} capture(s) matching the glob"
        else:
            raise RuntimeError(
                "nothing to fit from: run an iteration, load a state with "
                "meas_*.npy beside it, or set the capture glob")
        p2, info = plantmod.identify(s.u, y, s.loop.dt, model=model_key)
        print(f"fit ({KEY2LABEL[model_key]}) from {src}:")
        print(f"  {p2}")
        print(f"  residual {info['resid_peak_pct']:.2f}% peak / "
              f"{info['resid_rms_pct']:.2f}% rms of span -- what this model "
              f"form cannot explain about the measured response")
        it = s.snapshots[-1]["it"] if s.snapshots else s.iteration
        self.msgs.put(("call", lambda: self._after_fit(p2, y, it)))

    def _after_fit(self, p, y, it):
        self._set_param_entries(p)
        self._plot_waveforms(self.session.u, y, p.forward(self.session.u), it)
        self.nb.select(0)

    def do_load(self):
        path = self.state_var.get().strip()
        if not path:
            return self._browse(self.state_var, "State files", "*.state.npz", RUN_DIR)
        try:
            s = load_session(path)
        except Exception as e:
            return messagebox.showerror("Load state", str(e))
        self.session = s
        self.channel_var.set(s.channel)
        self.stem_var.set(s.stem)
        self.gamma_var.set(f"{s.loop.gamma:g}")
        self.fcut_var.set(f"{s.loop.f_cut:g}")
        self.toff_var.set(f"{s.t_off*1e6:g}")
        self.fs_var.set(f"{s.full_scale:g}")
        self._set_param_entries(s.loop.plant)
        self._apply_channel_defaults(s.channel)
        self.log(f"loaded {path}")
        self.log(f"  {s.channel} iteration {s.iteration}, stem {s.stem}, "
                 f"gamma {s.loop.gamma:g}, f_cut {s.loop.f_cut/1e3:g} kHz, "
                 f"t-offset {s.t_off*1e6:g} us")
        self.log(f"  plant: {s.loop.plant}")

        # Pull the last bench measurements back in, so the plots do not start
        # blank on a resumed campaign. Grid mismatches (a state rebuilt on a
        # different step) are skipped rather than guessed at.
        pat = os.path.join(os.path.dirname(s.state_path),
                           f"meas_{s.stem}_i*.npy")
        for f in sorted(glob.glob(pat))[-2:]:
            y = np.load(f)
            if len(y) != len(s.t):
                continue
            it = int(re.search(r"_i(\d+)\.npy$", f).group(1))
            s.snapshots.append(dict(it=it, y=y, m=s.loop.metrics(y)))
            self.log(f"  recalled measurement {os.path.basename(f)}")
        self._refresh_summary()
        self._show_session(select_tab=True)

    def do_init(self):
        target = self.target_var.get().strip()
        if not os.path.exists(target):
            return messagebox.showerror("Init", f"target not found: {target!r}")
        chname = self.channel_var.get()
        stem = self.stem_var.get().strip() or chname
        if len(stem) + 4 > NAME_LIMIT:
            return messagebox.showerror(
                "Init", f"name stem {stem!r} is {len(stem)} chars; with '_i00' "
                        f"that is {len(stem)+4}, past the {NAME_LIMIT}-char cap "
                        f"the 4063B wedges beyond.")
        try:
            f = self._floats(gamma=self.gamma_var, f_cut=self.fcut_var,
                             t_offset_us=self.toff_var, full_scale=self.fs_var)
        except RuntimeError as e:
            return messagebox.showerror("Init", str(e))
        state_path = os.path.join(RUN_DIR, f"drive_{stem}.state.npz")
        if os.path.exists(state_path) and not messagebox.askyesno(
                "State exists",
                f"{state_path}\nalready exists. A fresh init DESTROYS that "
                f"campaign's state (it happened once, taking a four-iteration "
                f"manual state with it).\n\nOverwrite it?"):
            return

        t, v = run_ilc.load_target(target)
        dt = float(np.median(np.diff(t)))
        ch = CHANNELS[chname]
        # The first shot needs a parametric plant even when the loop will run
        # on the measured FRF -- the campaign recipe: resonant seed (its group
        # delay is right), FRF takes over from the first step.
        mode = self._model_key()
        seed_key = "resonant" if mode == "frf" else mode
        try:
            params = self._entry_params(seed_key, strict=False)
        except RuntimeError as e:
            return messagebox.showerror("Init", str(e))
        if params is not None:
            plant = self._plant_from(params, dt)
            seed_src = "from the panel entries"
        else:
            plant = ch.plant(float(np.ptp(v)), dt, model=seed_key)
            seed_src = "from the calibration tables"
        loop = ilc.Loop(plant=plant, target=v, dt=dt, channel=ch,
                        gamma=f["gamma"], f_cut=f["f_cut"])
        u = loop.first_shot()
        rep = loop.check(u)

        os.makedirs(RUN_DIR, exist_ok=True)
        out = os.path.join(RUN_DIR, f"drive_{stem}_iter0.csv")
        outputs.write_awg_csv(out, t, u,
                              comment=f"{chname} ILC iteration 0 (model-based)\n{plant}")
        wname = f"{stem}_i00"
        os.makedirs(AWG_WAVEFORMS, exist_ok=True)
        gui = outputs.write_bk_waveform(os.path.join(AWG_WAVEFORMS, wname + ".csv"),
                                        u, wname, f["full_scale"])
        s = Session(state_path=state_path, loop=loop, t=t, u=u, iteration=0,
                    stem=stem, full_scale=f["full_scale"],
                    t_off=f["t_offset_us"] * 1e-6)
        save_session(s)
        self.session = s
        self.state_var.set(state_path)
        self._apply_channel_defaults(chname)

        self._set_param_entries(plant)
        self.log(f"init {chname} ({KEY2LABEL[seed_key]} seed {seed_src}): "
                 f"{plant}")
        if mode == "frf":
            self.log("  first shot is parametric; the measured FRF takes "
                     "over at the first step")
        self.log(f"  target {np.ptp(v)*HV_PER_MON:.0f} V pk-pk over "
                 f"{t[-1]*1e3:.2f} ms, {len(v)} points at {dt*1e6:.3f} us")
        self.log(f"  uncorrected : peak error "
                 f"{np.abs(plant.forward(v/plant.gain)-v).max()*HV_PER_MON:.0f} V")
        self.log(f"  modelled    : peak error "
                 f"{np.abs(plant.forward(u)-v).max()*HV_PER_MON:.1f} V "
                 f"(first shot, if the model is right)")
        self.log(f"  limit check : {rep}")
        self.log(f"  wrote {out}")
        self.log(f"        {gui}  (GUI-ready, upload with Normalise OFF)")
        self.log(f"  state {state_path}")
        self._refresh_summary()
        self._show_session(select_tab=True)

    def _refresh_summary(self):
        s = self.session
        if s is None:
            return self.summary.configure(text="no session loaded")
        lp = s.loop
        idle = (s.u[0] * 1e3, s.u[-1] * 1e3)
        txt = (f"{s.channel}  '{s.stem}'  iteration {s.iteration}\n"
               f"target {np.ptp(lp.target)*HV_PER_MON:.0f} V pk-pk, "
               f"{len(s.t)} pts, dt {lp.dt*1e6:.2f} us\n"
               f"drive peak {np.abs(s.u).max():.3f} V, idle "
               f"{idle[0]:+.1f}/{idle[1]:+.1f} mV of "
               f"{LIMITS.idle_awg*1e3:.0f} mV cap\n"
               f"gamma {lp.gamma:g}, f_cut {lp.f_cut/1e3:g} kHz, "
               f"t-offset {s.t_off*1e6:g} us, "
               f"history {len(lp.history)} iterations")
        self.summary.configure(text=txt)

    # --------------------------------------------------- shared step pieces
    def _gather_settings(self):
        """Entry fields -> plain values, ON THE MAIN THREAD. Tk variables must
        not be read from a worker (tkinter is not thread-safe), so everything a
        worker needs is collected here and handed over as floats and strings."""
        cfg = self._floats(gamma=self.gamma_var, f_cut=self.fcut_var,
                           t_offset_us=self.toff_var)
        cfg["mode"] = self._model_key()
        cfg["frf_path"] = self.frf_var.get().strip()
        if cfg["mode"] == "frf":
            if not cfg["frf_path"]:
                raise RuntimeError("the measured-FRF model needs an FRF file "
                                   "-- browse to run\\frf_WIDE_<ch>.csv, or "
                                   "make one with tools/sysid_make.py + "
                                   "tools/sysid_fit.py")
            cfg.update(self._floats(f_use=self.fuse_var, f_max=self.fmax_var))
        else:
            cfg["params"] = self._entry_params(cfg["mode"], strict=True)
        return cfg

    def _apply_settings(self, cfg):
        """Worker side: settings -> loop, logging anything that actually
        changed. Changed values persist in the state on the next save,
        exactly like the CLI's --f-cut / --t-offset overrides."""
        s = self.session
        if abs(cfg["gamma"] - s.loop.gamma) > 1e-12:
            print(f"gamma {s.loop.gamma:g} -> {cfg['gamma']:g}")
            s.loop.gamma = cfg["gamma"]
        if abs(cfg["f_cut"] - s.loop.f_cut) > 1e-9:
            print(f"f_cut {s.loop.f_cut:g} -> {cfg['f_cut']:g} Hz")
            s.loop.f_cut = cfg["f_cut"]
        t_off = cfg["t_offset_us"] * 1e-6
        if abs(t_off - s.t_off) > 1e-9:
            print(f"t-offset {s.t_off*1e6:g} -> {t_off*1e6:g} us -- measured 0 "
                  f"on this bench; change it only if the trigger wiring changed")
            s.t_off = t_off
        if cfg["mode"] == "frf":
            s.loop.frf = ilc.FRF(cfg["frf_path"], f_use=cfg["f_use"],
                                 f_max=cfg["f_max"])
            print(f"update uses the measured inverse from "
                  f"{os.path.basename(cfg['frf_path'])} "
                  f"({s.loop.frf.f[0]:.0f}-{s.loop.frf.f[-1]:.0f} Hz, "
                  f"taper {cfg['f_use']/1e3:g}-{cfg['f_max']/1e3:g} kHz)")
            cfg["desc"] = (f"FRF {cfg['f_use']/1e3:g}-{cfg['f_max']/1e3:g}k")
        else:
            if s.loop.frf is not None:
                print(f"FRF off -- {KEY2LABEL[cfg['mode']]} lead, confined to "
                      f"f_cut {s.loop.f_cut/1e3:g} kHz")
            s.loop.frf = None
            old = s.loop.plant
            new = self._plant_from(cfg["params"], s.loop.dt, offset=old.offset)
            if repr(new) != repr(old):
                print(f"plant -> {new}")
            s.loop.plant = new
            cfg["desc"] = DESC_FOR[cfg["mode"]]

    def _write_iteration(self, wname):
        """Drive CSV in run\\, GUI-previewable copy in the AWG library --
        exactly the pair the CLIs leave behind."""
        s = self.session
        out = os.path.join(RUN_DIR, f"drive_{wname}.csv")
        outputs.write_awg_csv(out, s.t, s.u,
                              comment=f"{s.channel} ILC {wname}\n{s.loop.plant}")
        os.makedirs(AWG_WAVEFORMS, exist_ok=True)
        gui = outputs.write_bk_waveform(
            os.path.join(AWG_WAVEFORMS, wname + ".csv"), s.u, wname, s.full_scale)
        self.msgs.put(("log", f"wrote {out}"))
        self.msgs.put(("log", f"      {gui}"))

    # ---------------------------------------------------------- manual step
    def do_step(self):
        if self.session is None:
            return messagebox.showerror("Step", "load or init a session first")
        pattern = self.meas_var.get().strip()
        if not pattern:
            return messagebox.showerror("Step", "set the capture glob first")
        mon = self.moncol_var.get()
        refit, force = self.refit_var.get(), self.force_var.get()
        zerobase = self.zerobase_var.get()
        try:
            cfg = self._gather_settings()
        except RuntimeError as e:
            return messagebox.showerror("Step", str(e))
        self.run_worker(lambda: self._step_work(cfg, pattern, mon, refit,
                                                force, zerobase),
                        "stepping from captures...")

    def _step_work(self, cfg, pattern, mon, refit, force, zerobase):
        s = self.session
        self._apply_settings(cfg)
        y, files = read_captures(pattern, mon, s.t, s.t_off)
        print(f"averaging {len(files)} capture(s):")
        for f in files:
            print(f"   {os.path.basename(f)}")
        if zerobase:
            w = s.t < s.t[0] + 0.05 * (s.t[-1] - s.t[0])
            tgt_base = s.loop.target[w].mean()
            if abs(tgt_base) > 0.01 * np.ptp(s.loop.target):
                print(f"  WARNING: zero-baseline, but the target already "
                      f"averages {tgt_base*HV_PER_MON:.0f} V there -- this "
                      f"subtracts signal, not baseline")
            y = y - y[w].mean()

        it = s.iteration
        m = s.loop.metrics(y)
        m["model"] = cfg["desc"]
        print(f"iteration {it}: error peak {m['peak_err_hv']:7.1f} V   "
              f"rms {m['rms_err_hv']:6.2f} V   ({m['peak_pct']:.2f}% FS)")
        if refit:
            fit_key = cfg["mode"] if cfg["mode"] != "frf" else "resonant"
            p2, info = plantmod.identify(s.u, y, s.loop.dt, model=fit_key)
            print(f"refit plant ({KEY2LABEL[fit_key]}): {p2}  "
                  f"(residual {info['resid_peak_pct']:.2f}% peak)")
            s.loop.plant = p2

        u_prev = s.u
        u_next = s.loop.update(s.u, y)
        s.loop.history[-1]["model"] = cfg["desc"]
        rep = s.loop.check(u_next)
        print(f"limit check: {rep}")
        if not rep and not force:
            s.loop.history.pop()      # the refused update never happened
            s.snapshots.append(dict(it=it, y=y, m=m))
            self.msgs.put(("call", lambda: self._show_iteration(u_prev, y, m, it)))
            print("REFUSED to write a drive that violates a hard limit "
                  "(tick 'force' to override)")
            return
        s.u = u_next
        s.iteration = it + 1
        s.snapshots.append(dict(it=it, y=y, m=m))
        self._write_iteration(f"{s.stem}_i{s.iteration:02d}")
        save_session(s)
        print(f"state saved, now at iteration {s.iteration}")
        self.msgs.put(("call", lambda: (self._refresh_summary(),
                                        self._show_iteration(u_next, y, m, it))))

    # ------------------------------------------------------------ bench run
    def do_bench(self):
        if self.session is None:
            return messagebox.showerror("Bench", "load or init a session first")
        try:
            f = self._floats(awg_ch=self.awgch_var, scope_ch=self.scopech_var,
                             iterations=self.iters_var, repeats=self.repeats_var,
                             wait=self.wait_var)
            cfg = self._gather_settings()
        except RuntimeError as e:
            return messagebox.showerror("Bench", str(e))
        self.run_worker(lambda: self._bench_work(cfg, int(f["awg_ch"]),
                                                 int(f["scope_ch"]),
                                                 int(f["iterations"]),
                                                 int(f["repeats"]), f["wait"],
                                                 self.skip_var.get()),
                        "bench loop running...")

    def _bench_modules(self):
        if self._modules is None:
            sg = os.environ.get("SCOPE_GRAB",
                                os.path.join(SIBLINGS, "scope-grab",
                                             "scope_grab.py"))
            ag = os.environ.get("AWG_GUI",
                                os.path.join(SIBLINGS, "BK4063B-AWG-GUI",
                                             "bk4063b_awg_gui.py"))
            print(f"instrument layers: {sg}")
            print(f"                   {ag}")
            self._modules = (ilc_bench.load_module(sg, "scope_grab"),
                             ilc_bench.load_module(ag, "bk4063b_awg_gui"))
        ilc_bench._AWGMOD = self._modules[1]
        return self._modules

    def _bench_work(self, cfg, awg_ch, scope_ch, iterations, repeats, wait_s,
                    skip):
        s = self.session
        self._apply_settings(cfg)
        scopemod, awgmod = self._bench_modules()

        limit = getattr(awgmod, "MAX_ARB_NAME", NAME_LIMIT)
        if len(s.stem) + 4 > limit:
            raise RuntimeError(f"stem {s.stem!r} + '_iNN' is past the "
                               f"{limit}-char name cap")

        awg = awgmod.Awg()
        print("AWG:  ", awg.connect())
        scope = scopemod.Scope()
        print("Scope:", scope.connect())
        try:
            problems, notes = ilc_bench.check_awg_channel(
                awg, awg_ch, full_scale=s.full_scale)
            for n in notes:
                print("      ", n)
            acq = scope.get(":ACQuire:TYPE")
            print(f"       scope acquisition {acq}, {repeats} software repeats")
            if acq.upper().startswith("AVER"):
                problems.append("scope is in AVER, and :SINGle takes exactly ONE "
                                "hit of an average (measured) -- set HRES; the "
                                "repeats do the averaging")
            elif not acq.upper().startswith("HRES"):
                problems.append(f"scope is in {acq}; use HRES")
            if repeats < 16:
                problems.append(f"{repeats} repeats is too few to dither the "
                                f"scope's 2.5 mV word lattice (16 reaches the "
                                f"0.16 mV floor)")
            if problems:
                print("Setup problems:")
                for p in problems:
                    print("  !", p)
                if not skip:
                    print("REFUSING to upload. Fix the setup in the AWG GUI / "
                          "scope, or tick 'skip setup checks'.")
                    return

            k0, u = s.iteration, s.u
            for k in range(k0, k0 + iterations + 1):
                if self.stop_evt.is_set():
                    print("stopped before iteration", k)
                    break
                rep = s.loop.check(u)
                if not rep:
                    print("limit check FAILED:", rep)
                    break
                wname = f"{s.stem}_i{k:02d}"
                n, frac = ilc_bench.upload_drive(awg, awg_ch, wname, u,
                                                 s.full_scale)
                print(f"\niter {k}: uploaded {wname} ({n} pts, "
                      f"{100*frac:.1f}% of DAC range, peak {np.abs(u).max():.4f} V)")
                s.u = u
                self._write_iteration(wname)
                if k == k0:
                    time.sleep(0.5)
                    ilc_bench.verify_alignment(scope, awg_ch, u, s.t, s.t_off,
                                               wait_s)
                y = self._bench_capture(scope, scope_ch, s.t, s.t_off,
                                        repeats, wait_s)
                np.save(os.path.join(RUN_DIR, f"meas_{wname}.npy"), y)
                m = s.loop.metrics(y)
                m["model"] = cfg["desc"]
                print(f"         error: peak {m['peak_err_hv']:7.1f} V   "
                      f"rms {m['rms_err_hv']:6.2f} V   ({m['peak_pct']:.2f}% FS)")
                s.snapshots.append(dict(it=k, y=y, m=m))
                u_now = u
                self.msgs.put(("call",
                               lambda u=u_now, y=y, m=m, k=k:
                               (self._refresh_summary(),
                                self._show_iteration(u, y, m, k))))
                if k < k0 + iterations and not self.stop_evt.is_set():
                    u = s.loop.update(u, y)
                    s.loop.history[-1]["model"] = cfg["desc"]
                    s.u = u
                    s.iteration = k + 1
                    save_session(s)
        finally:
            scope.close()
            awg.close()
            print("instruments closed")
        print("\n" + s.loop.report())

    def _bench_capture(self, scope, ch, t_grid, t_off, repeats, wait_s,
                       settle=0.5):
        """ilc_bench.capture with a progress bar and a stop check between
        shots. The settle wait happens once, after the new upload."""
        time.sleep(settle)
        traces = []
        self.msgs.put(("progress", 0, repeats))
        for i in range(repeats):
            if self.stop_evt.is_set():
                raise RuntimeError("stopped mid-capture; this iteration is "
                                   "discarded (state untouched)")
            got = scope.single(wait_s=wait_s)
            if got is not True:
                raise RuntimeError(f"no trigger within {wait_s:g} s on repeat "
                                   f"{i+1} -- is the burst running?")
            ts, vs = scope.waveform(ch)
            scope.run()
            traces.append(scopeio.resample(ts, vs, t_grid, t_offset=t_off))
            self.msgs.put(("progress", i + 1, repeats))
        self.msgs.put(("progress", 0, 1))
        return ilc.averaged(traces)

    # ---------------------------------------------------------------- plots
    def _colour(self):
        return CH_DEFAULTS[self.session.channel]["colour"]

    def _show_session(self, select_tab=False):
        """Everything drawable from a freshly loaded/inited session: target,
        drive, model prediction, plus any recalled measurements."""
        s = self.session
        snap = s.snapshots[-1] if s.snapshots else None
        y = snap["y"] if snap else None
        m = snap["m"] if snap else None
        it = snap["it"] if snap else None
        pred = None if snap else s.loop.plant.forward(s.u)
        self._plot_waveforms(s.u, y, pred, it)
        if snap:
            self._plot_error(y, m, it)
            self._plot_spectrum(y, it)
        else:
            self.ax_err.clear(); self.fig_err._canvas.draw_idle()
            self.ax_spec.clear(); self.fig_spec._canvas.draw_idle()
        self._plot_convergence()
        if select_tab:
            self.nb.select(0)

    def _show_iteration(self, u, y, m, it):
        self._plot_waveforms(u, y, None, it)
        self._plot_error(y, m, it)
        self._plot_spectrum(y, it)
        self._plot_convergence()

    def _plot_waveforms(self, u, y, pred, it):
        s, c = self.session, self._colour()
        tms = s.t * 1e3
        ax = self.ax_out
        ax.clear()
        ax.plot(tms, s.loop.target * HV_PER_MON, color=TARGET_COLOUR, lw=1.0,
                label="target")
        if pred is not None:
            ax.plot(tms, pred * HV_PER_MON, color=PRED_COLOUR, lw=0.9, ls="--",
                    label="model-predicted output")
        if y is not None:
            ax.plot(tms, y * HV_PER_MON, color=c, lw=0.9,
                    label=f"measured (iter {it})")
        ax.set_ylabel("EOM voltage (V)")
        ax.legend(loc="best", fontsize=7)
        ax.set_title(f"{s.channel} '{s.stem}' -- output vs target")
        ax.grid(True, alpha=0.3)

        ax = self.ax_drv
        ax.clear()
        ax.plot(tms, u, color=c, lw=0.9,
                label=f"drive u (iteration {s.iteration})")
        cap = LIMITS.idle_awg
        ax.axhline(cap, color="#c62828", lw=0.6, ls=":")
        ax.axhline(-cap, color="#c62828", lw=0.6, ls=":")
        ax.plot([tms[0], tms[-1]], [u[0], u[-1]], "o", color=c, ms=4, mfc="none")
        ax.annotate(f"idle {u[0]*1e3:+.1f} mV", (tms[0], u[0]), fontsize=7,
                    xytext=(4, 8), textcoords="offset points")
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("AWG drive (V)")
        ax.legend(loc="best", fontsize=7)
        ax.grid(True, alpha=0.3)
        self.fig_wave._canvas.draw_idle()

    def _plot_error(self, y, m, it):
        s, c = self.session, self._colour()
        tms = s.t * 1e3
        ax = self.ax_err
        ax.clear()
        prev = next((sn for sn in reversed(s.snapshots)
                     if sn["it"] < it and len(sn["y"]) == len(s.t)), None)
        def tag(n, mm):
            d = mm.get("model") if isinstance(mm, dict) else None
            return f"iter {n} ({d})" if d else f"iter {n}"

        if prev is not None:
            ax.plot(tms, (s.loop.target - prev["y"]) * HV_PER_MON, color=c,
                    lw=0.8, alpha=GHOST_ALPHA, label=tag(prev["it"], prev["m"]))
        e_hv = (s.loop.target - y) * HV_PER_MON
        ax.plot(tms, e_hv, color=c, lw=0.9, label=tag(it, m))
        ax.axhline(0, color=TARGET_COLOUR, lw=0.5)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("error at the EOM (V)")
        ax.set_title(f"target - measured:  peak {m['peak_err_hv']:.1f} V, "
                     f"rms {m['rms_err_hv']:.2f} V  ({m['peak_pct']:.3f}% FS)")
        ax.legend(loc="best", fontsize=7)
        ax.grid(True, alpha=0.3)
        self.fig_err._canvas.draw_idle()

    def _plot_spectrum(self, y, it):
        s, c = self.session, self._colour()
        ax = self.ax_spec
        ax.clear()

        def asd(e):
            n = len(e)
            f = np.fft.rfftfreq(n, s.loop.dt)
            return f[1:], np.abs(np.fft.rfft(e * HV_PER_MON))[1:] * 2 / n

        prev = next((sn for sn in reversed(s.snapshots)
                     if sn["it"] < it and len(sn["y"]) == len(s.t)), None)
        if prev is not None:
            fp, ap = asd(s.loop.target - prev["y"])
            ax.loglog(fp, ap, color=c, lw=0.7, alpha=GHOST_ALPHA,
                      label=f"iter {prev['it']}")
        fe, ae = asd(s.loop.target - y)
        ax.loglog(fe, ae, color=c, lw=0.8, label=f"iter {it}")
        if s.loop.frf is not None:
            ax.axvspan(s.loop.frf.f_use, s.loop.frf.f_max, color="#c68000",
                       alpha=0.15, label="FRF taper band")
            ax.axvline(s.loop.frf.f_max, color="#c68000", lw=0.7, ls="--")
        else:
            ax.axvline(s.loop.f_cut, color="#c68000", lw=0.7, ls="--",
                       label=f"f_cut {s.loop.f_cut/1e3:g} kHz")
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel("error amplitude at the EOM (V)")
        ax.set_title("where the residual lives -- the update only acts left "
                     "of the band edge")
        ax.legend(loc="best", fontsize=7)
        ax.grid(True, which="both", alpha=0.3)
        self.fig_spec._canvas.draw_idle()

    def _plot_convergence(self):
        s, c = self.session, self._colour()
        hist = list(s.loop.history)
        # the newest measurement is only in history once update() ran on it;
        # show it anyway so the final bench iteration appears
        if s.snapshots and s.snapshots[-1]["it"] == len(hist):
            hist = hist + [s.snapshots[-1]["m"]]
        ax = self.ax_conv
        ax.clear()
        if hist:
            k = np.arange(len(hist))
            ax.semilogy(k, [m["peak_err_hv"] for m in hist], "o-", color=c,
                        lw=1.0, ms=4, label="peak error")
            ax.semilogy(k, [m["rms_err_hv"] for m in hist], "s--", color=c,
                        lw=0.8, ms=3, alpha=0.6, label="rms error")
            ax.set_xticks(k)
            ax.legend(loc="best", fontsize=7)
            # mark where the inverse model changed -- the point of stepping
            # through the model ladder is seeing these transitions
            prev = None
            for i, m in enumerate(hist):
                d = m.get("model") if isinstance(m, dict) else None
                if d and d != prev:
                    if prev is not None:
                        ax.axvline(i - 0.5, color="#8a8a8a", lw=0.7, ls=":")
                    ax.annotate(d, (i, 0.98),
                                xycoords=("data", "axes fraction"),
                                fontsize=6.5, color="#666666",
                                ha="left", va="top", rotation=0)
                if d:
                    prev = d
        else:
            ax.text(0.5, 0.5, "no iterations yet", ha="center", va="center",
                    transform=ax.transAxes, color="#888888")
        ax.set_xlabel("iteration")
        ax.set_ylabel("error at the EOM (V)")
        ax.set_title(f"{s.channel} '{s.stem}' -- convergence")
        ax.grid(True, which="both", alpha=0.3)
        self.fig_conv._canvas.draw_idle()

    def do_show_frf(self):
        path = self.frf_var.get().strip()
        if not os.path.exists(path):
            return messagebox.showerror("FRF", f"not found: {path!r}")
        try:
            f = self._floats(f_use=self.fuse_var, f_max=self.fmax_var)
        except RuntimeError as e:
            return messagebox.showerror("FRF", str(e))
        d = pd.read_csv(path)
        ok = d["coherence"].to_numpy() >= 0.9         # FRF's own default cut
        axm, axp, axc = self.ax_frf
        for ax in self.ax_frf:
            ax.clear()
        axm.loglog(d["f_Hz"][ok], d["H_mag"][ok], "o-", ms=3, lw=0.9,
                   color="#1f77b4", label="measured")
        axm.loglog(d["f_Hz"][~ok], d["H_mag"][~ok], "o", ms=3, mfc="none",
                   color="#c62828", label="coherence < 0.9 (dropped)")
        axm.set_ylabel("|H| (mon V / AWG V)")
        axm.set_title(os.path.basename(path))
        ph = np.degrees(np.unwrap(np.radians(d["H_phase_deg"][ok].to_numpy())))
        axp.semilogx(d["f_Hz"][ok], ph, "o-", ms=3, lw=0.9, color="#1f77b4")
        axp.set_ylabel("phase, unwrapped (deg)")

        # Overlay the current parametric model, if one is selected and filled
        # in: this is the model-vs-chain comparison that decided the campaign
        # (see docs/REPORT.md section 5) and it is what shows how much each
        # rung of the model ladder explains.
        key = self._model_key()
        overlay = None
        if key != "frf":
            try:
                overlay = self._entry_params(key, strict=False)
            except RuntimeError:
                overlay = None
        if overlay:
            fg = d["f_Hz"].to_numpy(float)
            w = 2j * np.pi * fg
            H = np.full(fg.shape, overlay["gain"], complex)
            if "tau" in overlay:
                H = H / (1 + w * overlay["tau"] * 1e-6)
            if "fn" in overlay:
                wn = 2 * np.pi * overlay["fn"]
                H = H * wn ** 2 / (w ** 2 + 2 * overlay["zeta"] * wn * w + wn ** 2)
            axm.loglog(fg, np.abs(H), "--", lw=1.1, color="#c68000",
                       label=f"model: {DESC_FOR[key]}")
            axp.semilogx(fg, np.degrees(np.unwrap(np.angle(H))), "--",
                         lw=1.1, color="#c68000")
        axc.semilogx(d["f_Hz"], d["coherence"], "o-", ms=3, lw=0.9,
                     color="#2e7d32")
        axc.axhline(0.9, color="#c62828", lw=0.7, ls=":")
        axc.set_ylabel("coherence")
        axc.set_xlabel("frequency (Hz)")
        for ax in self.ax_frf:
            ax.axvspan(f["f_use"], f["f_max"], color="#c68000", alpha=0.15)
            ax.grid(True, which="both", alpha=0.3)
        if (~ok).any() or overlay:
            axm.legend(loc="best", fontsize=7)
        self.fig_frf._canvas.draw_idle()
        self.nb.select(4)
        self.log(f"FRF {os.path.basename(path)}: "
                 f"{ok.sum()}/{len(d)} tones coherent, "
                 f"{d['f_Hz'].min():.0f}-{d['f_Hz'].max():.0f} Hz, "
                 f"taper {f['f_use']/1e3:g}-{f['f_max']/1e3:g} kHz")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
