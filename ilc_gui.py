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
LOG_PATH = os.path.join(RUN_DIR, "ilc_gui.log")   # timestamped copy of the log
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
    # GEN is the blank channel for any other system: unity scale, no
    # calibration tables, and deliberately NO auto-pointed FRF -- nothing
    # measured on the Trek chains applies until it is loaded on purpose.
    "GEN": dict(mon_col="CH1", awg_ch=1, scope_ch=1,
                frf=None, colour="#2e7d32"),
}
TARGET_COLOUR = "#222222"
PRED_COLOUR = "#8a8a8a"

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


TARGET_SHAPES = ("ramp up-hold-return", "half-sine pulse")


def build_target_waveform(shape, peak, lead_ms, rise_ms, hold_ms, fall_ms,
                          tail_ms, dt_us):
    """A target from scratch: cosine-edged shapes on a uniform grid, for a
    system that has no target waveform yet.

    Cosine edges start and end with zero slope, so the demand carries no
    corner the chain must chase, and the record begins and ends at zero --
    the level the AWG idles on between bursts.  Times in ms, dt in us,
    peak in OUTPUT units (what the target CSV column carries)."""
    dt = dt_us * 1e-6
    if dt <= 0:
        raise RuntimeError("dt must be positive")

    def n(ms):
        return max(int(round(ms * 1e-3 / dt)), 0)

    lead, rise, hold, fall, tail = (n(x) for x in (lead_ms, rise_ms,
                                                   hold_ms, fall_ms, tail_ms))
    if shape == "half-sine pulse":
        body = peak * np.sin(np.pi * np.linspace(0.0, 1.0,
                                                 max(rise + hold + fall, 2)))
    else:                                    # cosine-edged ramp up-hold-return
        up = peak * 0.5 * (1 - np.cos(np.pi * np.linspace(0.0, 1.0,
                                                          max(rise, 2))))
        down = (peak * 0.5 * (1 - np.cos(np.pi * np.linspace(0.0, 1.0,
                                                             max(fall, 2)))))[::-1]
        body = np.concatenate([up, np.full(hold, float(peak)), down])
    v = np.concatenate([np.zeros(lead), body, np.zeros(tail)])
    if len(v) < 8:
        raise RuntimeError("target has fewer than 8 samples -- lengthen the "
                           "segments or shorten dt")
    return np.arange(len(v)) * dt, v


def dot_kw(n, dots=180, ms=3.0):
    """Marker kwargs that put ~`dots` small dots of REAL samples on a trace.

    Every dot drawn is an actual sample (markevery subsamples the markers,
    never the line), sized and thinned so a 5301-point record reads as a
    line with visible data points rather than a saturated ribbon."""
    return dict(marker=".", markersize=ms,
                markevery=max(1, int(round(n / dots))))


def nice_setting(value):
    """Smallest 'nice' instrument setting >= value: 1-1.5-2-2.5-3-4-5-7.5-10
    per decade, the values front panels actually offer."""
    e = 10.0 ** np.floor(np.log10(value))
    for m in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if m * e >= value * (1 - 1e-9):
            return m * e
    return 10 * e


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
        self.log(f"--- panel started; timestamped log appends to {LOG_PATH}")
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
                 model=self.model_var.get(), channel=self.channel_var.get(),
                 stem=self.stem_var.get(), shot_gain=self.shotgain_var.get(),
                 iter_sel=self.itersel_var.get(),
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
        self._path_row(sf, 2, "Target", self.target_var, self._browse_target)
        b = ttk.Button(sf, text="Build...", command=self.do_build_target)
        b.grid(row=2, column=3, padx=2)
        self._actions.append(b)
        b = ttk.Button(sf, text="Plot", command=self.do_preview_target)
        b.grid(row=2, column=4, padx=2)
        self._actions.append(b)
        ch0 = self.cfg.get("channel", "EO1")
        self.channel_var = tk.StringVar(value=ch0 if ch0 in CHANNELS else "EO1")
        self.stem_var = tk.StringVar(value=self.cfg.get("stem", ""))
        r3 = ttk.Frame(sf); r3.grid(row=3, column=0, columnspan=4, sticky="ew", pady=1)
        ttk.Label(r3, text="Channel").pack(side="left")
        cb = ttk.Combobox(r3, textvariable=self.channel_var, width=5,
                          values=list(CHANNELS), state="readonly")
        cb.pack(side="left", padx=(2, 8))
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_channel_change())
        ttk.Label(r3, text="Name stem").pack(side="left")
        ttk.Entry(r3, textvariable=self.stem_var, width=9).pack(side="left", padx=2)
        ttk.Label(r3, text=f"(<= {NAME_LIMIT - 4} chars; '_iNN' is appended)"
                  ).pack(side="left")

        r4 = ttk.Frame(sf); r4.grid(row=4, column=0, columnspan=5, sticky="ew", pady=1)
        # The first-shot gain is deliberately NOT the model gain: it fixes
        # what iteration 0 plays, and tuning the correction model afterwards
        # must not silently rescale it. Blank = fall back to the model gain.
        # Remembered between launches (it belongs to the remembered channel);
        # still cleared when the channel is SWITCHED -- the prior-leak guard.
        self.shotgain_var = tk.StringVar(value=self.cfg.get("shot_gain", ""))
        self.fs_var = tk.StringVar(value="10.0")
        ttk.Label(r4, text="first-shot gain").pack(side="left", padx=(0, 2))
        ttk.Entry(r4, textvariable=self.shotgain_var, width=7).pack(
            side="left", padx=(0, 8))
        ttk.Label(r4, text="full scale V").pack(side="left", padx=(0, 2))
        ttk.Entry(r4, textvariable=self.fs_var, width=5).pack(
            side="left", padx=(0, 6))
        ttk.Label(r4, text="(AWG: AMP = 2x full scale, OFST 0)",
                  foreground="#666666").pack(side="left")

        b = ttk.Button(sf, text="Init  (first shot = flat conversion, "
                                "target / gain)", command=self.do_init)
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
                                    sticky="ew", pady=1)
        self.gamma_var = tk.StringVar(value="0.6")
        self.fcut_var = tk.StringVar(value="5000")
        ttk.Label(r2, text="gamma").pack(side="left", padx=(0, 2))
        ttk.Entry(r2, textvariable=self.gamma_var, width=5).pack(
            side="left", padx=(0, 8))
        ttk.Label(r2, text="f_cut Hz").pack(side="left", padx=(0, 2))
        ttk.Entry(r2, textvariable=self.fcut_var, width=7).pack(
            side="left", padx=(0, 6))
        ttk.Label(r2, text="(learning gain; parametric band edge)",
                  foreground="#666666").pack(side="left")

        r3 = ttk.Frame(vf); r3.grid(row=3, column=0, columnspan=4,
                                    sticky="ew", pady=(2, 1))
        b = ttk.Button(r3, text="From calibration", command=self.do_calib)
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)
        b = ttk.Button(r3, text="Fit from measurement", command=self.do_fit)
        b.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._actions.append(b)

        self.frf_var = tk.StringVar(value=self.cfg.get("frf", ""))
        self._path_row(vf, 4, "FRF", self.frf_var,
                       lambda: self._browse(self.frf_var, "FRF CSV",
                                            "frf_*.csv", RUN_DIR))
        r4 = ttk.Frame(vf); r4.grid(row=5, column=0, columnspan=4, sticky="ew")
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

        # ---- capture post-processing ---------------------------------
        # Settings that shape how a MEASUREMENT is turned into an error --
        # nothing here touches the first shot, which is a pure flat
        # conversion. Applies to both Step and the bench loop.
        pf = ttk.LabelFrame(left, text="Capture post-processing "
                                       "(Step + Bench)", padding=4)
        pf.pack(fill="x", pady=(0, 4))
        r0 = ttk.Frame(pf); r0.grid(row=0, column=0, sticky="ew")
        self.toff_var = tk.StringVar(value="0.0")
        self.zerobase_var = tk.BooleanVar(value=False)
        ttk.Label(r0, text="t-offset us").pack(side="left", padx=(0, 2))
        ttk.Entry(r0, textvariable=self.toff_var, width=6).pack(
            side="left", padx=(0, 8))
        ttk.Checkbutton(r0, text="zero baseline (not for MKJ)",
                        variable=self.zerobase_var).pack(side="left")
        ttk.Label(pf, text="t-offset: measured 0 on this bench -- change only "
                           "if the trigger wiring changed",
                  foreground="#666666").grid(row=1, column=0, sticky="w")
        pf.columnconfigure(0, weight=1)

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
        ttk.Checkbutton(r1, text="refit plant", variable=self.refit_var).pack(side="left")
        ttk.Checkbutton(r1, text="force", variable=self.force_var).pack(side="left")
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
        rr = ttk.Frame(bf); rr.grid(row=2, column=0, sticky="ew", pady=(3, 0))
        b = ttk.Button(rr, text="Auto-set instruments", command=self.do_autoset)
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)
        self.upload_btn = ttk.Button(rr, text="Upload drive to AWG",
                                     command=self.do_upload)
        self.upload_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._actions.append(self.upload_btn)
        r2 = ttk.Frame(bf); r2.grid(row=3, column=0, sticky="ew", pady=(3, 0))
        self.bench_btn = ttk.Button(r2, text="Run bench loop", command=self.do_bench)
        self.bench_btn.pack(side="left", fill="x", expand=True)
        self._actions.append(self.bench_btn)
        self.stop_btn = ttk.Button(r2, text="Stop", command=self.stop_evt.set,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(4, 0))
        ttk.Label(bf, text="Close the AWG GUI and Scope Grab first -- both hold\n"
                           "their VISA sessions. Outputs switch OFF when a run\n"
                           "that played anything ends.",
                  foreground="#666666").grid(row=4, column=0, sticky="w")
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
        sel = ttk.Frame(right)
        sel.pack(fill="x", pady=(0, 2))
        ttk.Label(sel, text="Iterations shown").pack(side="left")
        self.itersel_var = tk.StringVar(value=self.cfg.get("iter_sel", ""))
        e = ttk.Entry(sel, textvariable=self.itersel_var, width=14)
        e.pack(side="left", padx=2)
        e.bind("<Return>", lambda ev: self._redraw_iterations())
        ttk.Label(sel, text="(blank = last two;  'all',  '2-5',  or '0,3,6')",
                  foreground="#666666").pack(side="left")
        b = ttk.Button(sel, text="Redraw", command=self._redraw_iterations)
        b.pack(side="right")
        self._actions.append(b)

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.fig_wave, (self.ax_out, self.ax_drv) = self._tab("Waveforms", 2, sharex=True)
        self.fig_err, (self.ax_err,) = self._tab("Error", 1)
        self.fig_spec, (self.ax_spec,) = self._tab("Error spectrum", 1)
        self.fig_dcor, (self.ax_dcor,) = self._tab("Drive corrections", 1)
        self.fig_conv, (self.ax_conv,) = self._tab("Convergence", 1)
        self.fig_frf, self.ax_frf = self._tab("FRF", 3, sharex=True)

        # the wiring fields (monitor col, AWG/scope channels, FRF autopoint)
        # follow the remembered channel -- they were built with EO1's values
        self._apply_channel_defaults()

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
        """Panel log without timestamps (they were noise at reading width);
        the file copy in run\\ilc_gui.log keeps them."""
        lines = str(text).splitlines() or [""]
        self.log_text.configure(state="normal")
        for line in lines:
            self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            os.makedirs(RUN_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(f"{stamp}  {line}\n")
        except OSError:
            pass                        # a full disk must not kill the loop

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

    def ask_user(self, title, msg):
        """A yes/no question from a WORKER thread: marshalled to the main
        thread (Tk dialogs, like Tk variables, are main-thread-only) while
        the worker blocks on the answer."""
        evt = threading.Event()
        result = {}

        def ask():
            result["yes"] = messagebox.askyesno(title, msg)
            evt.set()
        self.msgs.put(("call", ask))
        evt.wait()
        return result["yes"]

    def _floats(self, **pairs):
        out = {}
        for k, v in pairs.items():
            try:
                out[k] = float(v.get())
            except ValueError:
                raise RuntimeError(f"{k} is not a number: {v.get()!r}")
        return out

    # -------------------------------------------------------- session setup
    def _on_channel_change(self):
        """Switching channel means switching system: model parameters from
        the previous channel do not follow -- they are the previous chain's
        numbers, and inheriting them silently is exactly the prior-knowledge
        leak the GEN channel exists to prevent."""
        for v in self._param_vars.values():
            v.set("")
        self.shotgain_var.set("")
        self._apply_channel_defaults()
        if os.path.exists(self.target_var.get().strip()):
            self.do_preview_target(quiet=True)

    def _apply_channel_defaults(self, channel=None):
        ch = channel or self.channel_var.get()
        d = CH_DEFAULTS[ch]
        self.moncol_var.set(d["mon_col"])
        self.awgch_var.set(str(d["awg_ch"]))
        self.scopech_var.set(str(d["scope_ch"]))
        # Point at this channel's wide-probe FRF unless the user browsed to
        # something that is not just the other channel's default.
        cur = os.path.basename(self.frf_var.get())
        if not cur or cur in {c["frf"] for c in CH_DEFAULTS.values() if c["frf"]}:
            p = os.path.join(RUN_DIR, d["frf"]) if d["frf"] else ""
            self.frf_var.set(p if p and os.path.exists(p) else "")

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
            # The CHOSEN channel, not the loaded session's: after switching
            # the combobox to another system, filling from the old session's
            # calibration would smuggle that chain's numbers across.
            ch = CHANNELS[self.channel_var.get()]
            if self.session is not None and self.session.channel == ch.name:
                amp = float(np.ptp(self.session.loop.target))
            else:
                tpath = self.target_var.get().strip()
                if not os.path.exists(tpath):
                    raise RuntimeError(
                        "load a session or set a target file first -- the "
                        "tables are amplitude-dependent, so the target sets "
                        "which row applies")
                _, v = run_ilc.load_target(tpath, ch.mon_scale)
                amp = float(np.ptp(v))
            # a channel with no tables (GEN) raises here rather than
            # borrowing another system's numbers
            self.pgain_var.set(f"{ch.gain(amp):.4f}")
            self.ptau_var.set(f"{ch.tau(amp)*1e6:.2f}")
            self.pfn_var.set(f"{ch.fn(amp):.0f}")
            self.pzeta_var.set(f"{ch.zeta(amp):.3f}")
        except (RuntimeError, ValueError) as e:
            return messagebox.showerror("From calibration", str(e))
        self.log(f"calibration at {amp*ch.mon_scale:.0f} V pk-pk ({ch.name}): "
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
            # Fit the measurement against the drive that PLAYED it. After a
            # step the current drive has already moved on, and identifying a
            # mismatched pair returns a plant of the update, not the chain.
            snap = s.snapshots[-1]
            y, src = snap["y"], f"the iteration-{snap['it']} measurement"
            u_fit = snap.get("u")
            if u_fit is None:
                u_fit = s.u
                if snap["it"] != s.iteration:
                    print(f"  note: no drive stored with this measurement -- "
                          f"fitting iteration-{snap['it']} data against the "
                          f"iteration-{s.iteration} drive. Prefer a fresh "
                          f"measurement.")
        elif pattern:
            y, files = read_captures(pattern, mon, s.t, s.t_off)
            src = f"{len(files)} capture(s) matching the glob"
            u_fit = s.u
        else:
            raise RuntimeError(
                "nothing to fit from: run an iteration, load a state with "
                "meas_*.npy beside it, or set the capture glob")
        p2, info = plantmod.identify(u_fit, y, s.loop.dt, model=model_key)
        print(f"fit ({KEY2LABEL[model_key]}) from {src}:")
        print(f"  {p2}")
        print(f"  residual {info['resid_peak_pct']:.2f}% peak / "
              f"{info['resid_rms_pct']:.2f}% rms of span -- what this model "
              f"form cannot explain about the measured response")
        it = s.snapshots[-1]["it"] if s.snapshots else s.iteration
        self.msgs.put(("call", lambda: self._after_fit(p2, u_fit, y, it)))

    def _after_fit(self, p, u_fit, y, it):
        self._set_param_entries(p)
        self._plot_waveforms(u_fit, y, p.forward(u_fit), it)
        self.nb.select(0)

    def _browse_target(self):
        self._browse(self.target_var, "Target CSV", "*.csv",
                     os.path.join(HERE, "waveforms"))
        if os.path.exists(self.target_var.get().strip()):
            self.do_preview_target()

    def _first_shot_gain(self):
        """The conversion gain for the flat first shot: the dedicated entry,
        else the model gain as fallback. None when neither is set."""
        for name, var in (("first-shot gain", self.shotgain_var),
                          ("model gain", self.pgain_var)):
            txt = var.get().strip()
            if txt:
                try:
                    g = float(txt)
                except ValueError:
                    raise RuntimeError(f"{name} is not a number: {txt!r}")
                if g <= 0:
                    raise RuntimeError(f"{name} must be positive")
                return g
        return None

    def do_preview_target(self, quiet=False):
        """Plot the target file, and the AWG output the flat first shot
        would produce at AMP = 2x full scale / OFST 0 -- nothing is sent.
        This is the look-before-you-init view."""
        path = self.target_var.get().strip()
        if not os.path.exists(path):
            if not quiet:
                messagebox.showerror("Plot target",
                                     f"target not found: {path!r}")
            return
        chname = self.channel_var.get()
        ch = CHANNELS[chname]
        try:
            t, v = run_ilc.load_target(path, ch.mon_scale)
            fs = float(self.fs_var.get())
            g = self._first_shot_gain()
        except (RuntimeError, ValueError) as e:
            if not quiet:
                messagebox.showerror("Plot target", str(e))
            return
        tms = t * 1e3
        c = CH_DEFAULTS[chname]["colour"]
        ax = self.ax_out
        ax.clear()
        ax.plot(tms, v * ch.mon_scale, color=TARGET_COLOUR, lw=1.0,
                label="target (file contents)", **dot_kw(len(tms)))
        ax.set_ylabel(f"{ch.out_name} voltage (V)")
        ax.set_title(f"{os.path.basename(path)} -- preview, nothing sent")
        ax.legend(loc="best", fontsize=7)
        ax.grid(True, alpha=0.3)

        ax2 = self.ax_drv
        ax2.clear()
        if g is not None:
            u = v / g
            pk = float(np.abs(u).max())
            ax2.plot(tms, u, color=c, lw=0.9,
                     label=f"predicted AWG output (target / {g:g})",
                     **dot_kw(len(tms)))
            ax2.axhline(fs, color="#c62828", lw=0.8, ls="--")
            ax2.axhline(-fs, color="#c62828", lw=0.8, ls="--",
                        label=f"+/-{fs:g} V full scale "
                              f"(AMP {2*fs:g} Vpp, OFST 0)")
            note = f"peak {pk:.3f} V = {100*pk/fs:.1f}% of DAC range"
            if pk > fs:
                note += "  --  CLIPS: raise the gain or lower the target"
                self.log(f"preview: the first shot would CLIP "
                         f"({pk:.3f} V > {fs:g} V full scale)")
            ax2.set_title(note)
            ax2.legend(loc="best", fontsize=7)
            self.log(f"preview {os.path.basename(path)}: "
                     f"{np.ptp(v)*ch.mon_scale:.0f} V pk-pk over "
                     f"{t[-1]*1e3:.2f} ms, {len(v)} pts; AWG peak {pk:.3f} V "
                     f"({100*pk/fs:.1f}% of range) at gain {g:g}")
        else:
            ax2.text(0.5, 0.5, "type a first-shot gain (or a model gain)\n"
                               "to preview the AWG output",
                     ha="center", va="center", transform=ax2.transAxes,
                     color="#888888")
            self.log(f"preview {os.path.basename(path)}: "
                     f"{np.ptp(v)*ch.mon_scale:.0f} V pk-pk over "
                     f"{t[-1]*1e3:.2f} ms, {len(v)} pts (no gain set, AWG "
                     f"preview skipped)")
        ax2.set_xlabel("time (ms)")
        ax2.set_ylabel("AWG drive (V)")
        ax2.grid(True, alpha=0.3)
        self.fig_wave._canvas.draw_idle()
        self.nb.select(0)

    def do_build_target(self):
        """Build a target CSV from scratch, for a system with no target yet.
        Values are in OUTPUT units for the selected channel: EOM volts on
        EO1/EO2 (divided by mon_scale on load), measured volts on GEN."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Build a target")
        dlg.transient(self.root)
        dlg.grab_set()
        f = ttk.Frame(dlg, padding=8)
        f.pack(fill="both", expand=True)
        shape_var = tk.StringVar(value=TARGET_SHAPES[0])
        ttk.Label(f, text="Shape").grid(row=0, column=0, sticky="w")
        ttk.Combobox(f, textvariable=shape_var, values=TARGET_SHAPES,
                     state="readonly", width=22).grid(row=0, column=1,
                                                      sticky="w")
        fields = (("peak (output units)", "1.0"), ("lead ms", "0.5"),
                  ("rise ms", "2.0"), ("hold ms", "3.0"), ("fall ms", "2.0"),
                  ("tail ms", "0.5"), ("dt us", "2.0"))
        fvars = {}
        for i, (lab, dv) in enumerate(fields, start=1):
            ttk.Label(f, text=lab).grid(row=i, column=0, sticky="w")
            fvars[lab] = tk.StringVar(value=dv)
            ttk.Entry(f, textvariable=fvars[lab], width=10).grid(
                row=i, column=1, sticky="w")
        ttk.Label(f, foreground="#666666", text=(
            "Cosine edges: zero slope at both ends, and the record\n"
            "starts and ends at zero -- the level the AWG idles on."),
            ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def ok():
            try:
                p = {lab: float(v.get()) for lab, v in fvars.items()}
            except ValueError:
                return messagebox.showerror("Build target",
                                            "every field must be a number",
                                            parent=dlg)
            path = filedialog.asksaveasfilename(
                parent=dlg, title="Save target CSV",
                initialdir=os.path.join(HERE, "waveforms"),
                initialfile="target_NEW.csv",
                defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
            if not path:
                return
            try:
                t, v = build_target_waveform(
                    shape_var.get(), p["peak (output units)"], p["lead ms"],
                    p["rise ms"], p["hold ms"], p["fall ms"], p["tail ms"],
                    p["dt us"])
            except RuntimeError as e:
                return messagebox.showerror("Build target", str(e), parent=dlg)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            outputs.write_awg_csv(
                path, t, v,
                comment=f"built target: {shape_var.get()}, "
                        f"peak {p['peak (output units)']:g} output units, "
                        f"dt {p['dt us']:g} us")
            self.target_var.set(path)
            self.log(f"built target {path}")
            self.log(f"  {shape_var.get()}, peak {p['peak (output units)']:g} "
                     f"over {t[-1]*1e3:.2f} ms, {len(v)} points at "
                     f"{p['dt us']:g} us")
            dlg.destroy()
            self.do_preview_target(quiet=True)

        bf = ttk.Frame(f)
        bf.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(bf, text="Build + save...", command=ok).pack(
            side="left", fill="x", expand=True)
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(
            side="left", padx=(4, 0))

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
        for f in sorted(glob.glob(pat)):
            y = np.load(f)
            if len(y) != len(s.t):
                continue
            it = int(re.search(r"_i(\d+)\.npy$", f).group(1))
            snap = dict(it=it, y=y, m=s.loop.metrics(y))
            # pair it with the drive that played it, so Fit uses a true pair
            dcsv = os.path.join(os.path.dirname(s.state_path),
                                f"drive_{s.stem}_i{it:02d}.csv")
            if os.path.exists(dcsv):
                du = pd.read_csv(dcsv, comment="#").iloc[:, 1].to_numpy(float)
                if len(du) == len(s.t):
                    snap["u"] = du
            s.snapshots.append(snap)
        if s.snapshots:
            its = sorted({sn["it"] for sn in s.snapshots})
            self.log(f"  recalled {len(its)} stored measurement(s): "
                     f"iterations {', '.join(str(i) for i in its)}")
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

        ch = CHANNELS[chname]
        t, v = run_ilc.load_target(target, ch.mon_scale)
        dt = float(np.median(np.diff(t)))
        # The first shot is a FLAT conversion (target / gain) -- only the
        # seed's gain shapes it. The rest of the seed plant still matters:
        # it is the parametric model stored in the state, drives the
        # model-predicted-output trace, and is what parametric updates use.
        mode = self._model_key()
        seed_key = "resonant" if mode == "frf" else mode
        # one-number bootstrap: a typed first-shot gain can seed a blank
        # model gain, so either box alone is enough to start from scratch
        if not self.pgain_var.get().strip() and self.shotgain_var.get().strip():
            self.pgain_var.set(self.shotgain_var.get().strip())
            self.log("model gain was blank -- seeded from the first-shot gain")
        try:
            g_shot = self._first_shot_gain()
            params = self._entry_params(seed_key, strict=False)
        except RuntimeError as e:
            return messagebox.showerror("Init", str(e))
        if params is not None:
            plant = self._plant_from(params, dt)
            seed_src = "from the panel entries"
        else:
            try:
                plant = ch.plant(float(np.ptp(v)), dt, model=seed_key)
            except ValueError as e:
                return messagebox.showerror(
                    "Init", f"{e}\n\nStarting from scratch, a typed gain with "
                            f"the gain-only model is enough: guess it "
                            f"conservatively (drive comes out larger if gain "
                            f"is guessed high), run one iteration, then 'Fit "
                            f"from measurement' replaces the guess with the "
                            f"measured value.")
            seed_src = "from the calibration tables"
        loop = ilc.Loop(plant=plant, target=v, dt=dt, channel=ch,
                        gamma=f["gamma"], f_cut=f["f_cut"])
        u = loop.first_shot(gain=g_shot)
        g_used = g_shot if g_shot is not None else plant.gain
        rep = loop.check(u)

        os.makedirs(RUN_DIR, exist_ok=True)
        out = os.path.join(RUN_DIR, f"drive_{stem}_iter0.csv")
        outputs.write_awg_csv(out, t, u,
                              comment=f"{chname} ILC iteration 0 "
                                      f"(flat conversion, target / gain)\n{plant}")
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
        self.log(f"  target {np.ptp(v)*ch.mon_scale:.0f} V pk-pk over "
                 f"{t[-1]*1e3:.2f} ms, {len(v)} points at {dt*1e6:.3f} us")
        sep = ("" if abs(g_used - plant.gain) < 1e-12 else
               f"; the model gain is {plant.gain:g}, a separate knob")
        self.log(f"  first shot  : flat conversion (target / {g_used:g}{sep}), "
                 f"drive peak {np.abs(u).max():.4f} V -- no pre-distortion, "
                 f"the first measurement shows the chain's raw response")
        self.log(f"  predicted   : peak error "
                 f"{np.abs(plant.forward(u)-v).max()*ch.mon_scale:.1f} V "
                 f"(the model's guess at what that measurement shows)")
        if mode == "frf":
            self.log("  the measured FRF takes over at the first step")
        self.log(f"  limit check : {rep}")
        self.log(f"  wrote {out}")
        self.log(f"        {gui}  (GUI-ready, upload with Normalise OFF)")
        self.log(f"  state {state_path}")
        self._refresh_summary()
        self._show_session(select_tab=True)
        self._save_config()          # stem/gain/channel survive even a kill

    def _refresh_summary(self):
        s = self.session
        if s is None:
            self.upload_btn.configure(text="Upload drive to AWG")
            return self.summary.configure(text="no session loaded")
        self.upload_btn.configure(
            text=f"Upload {s.stem}_i{s.iteration:02d} to AWG")
        lp = s.loop
        idle = (s.u[0] * 1e3, s.u[-1] * 1e3)
        txt = (f"{s.channel}  '{s.stem}'  iteration {s.iteration}\n"
               f"target {np.ptp(lp.target)*lp.channel.mon_scale:.0f} V pk-pk, "
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
                      f"averages {tgt_base*self._out_scale():.0f} V there -- "
                      f"this subtracts signal, not baseline")
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
            s.snapshots.append(dict(it=it, y=y, m=m, u=u_prev))
            self.msgs.put(("call", lambda: self._show_iteration(u_prev, y, m, it)))
            print("REFUSED to write a drive that violates a hard limit "
                  "(tick 'force' to override)")
            return
        s.u = u_next
        s.iteration = it + 1
        s.snapshots.append(dict(it=it, y=y, m=m, u=u_prev))
        self._write_iteration(f"{s.stem}_i{s.iteration:02d}")
        save_session(s)
        print(f"state saved, now at iteration {s.iteration}")
        self.msgs.put(("call", lambda: (self._refresh_summary(),
                                        self._show_iteration(u_next, y, m, it))))

    # -------------------------------------------------------------- autoset
    def do_autoset(self):
        """Configure the AWG and scope from what the session already knows:
        the record's period sets the arb frequency and the scope window, the
        drive and target spans set the verticals. Never touches an output
        switch, and refuses to reconfigure a channel that is live."""
        if self.session is None:
            return messagebox.showerror("Auto-set",
                                        "load or init a session first -- the "
                                        "settings come from its timing and "
                                        "amplitudes")
        s = self.session
        try:
            f = self._floats(awg_ch=self.awgch_var, scope_ch=self.scopech_var)
        except RuntimeError as e:
            return messagebox.showerror("Auto-set", str(e))
        period = float(s.t[-1]) + float(s.loop.dt)
        u, v = s.u, s.loop.target
        wname = f"{s.stem}_i{s.iteration:02d}"
        self.run_worker(lambda: self._autoset_work(
            period, float(u.min()), float(u.max()),
            float(v.min()), float(v.max()), s.full_scale,
            int(f["awg_ch"]), int(f["scope_ch"]), wname),
            "auto-setting instruments...")

    def _autoset_work(self, period, u_lo, u_hi, v_lo, v_hi, fs,
                      awg_ch, scope_ch, wname):
        scopemod, awgmod = self._bench_modules()

        # Connect BOTH instruments before writing to either: a scope that
        # does not answer (first live test: Scope Grab held its VISA session)
        # must not leave a half-configured bench.
        awg = ilc_bench.make_awg(awgmod)
        print("AWG:  ", awg.connect())
        scope = ilc_bench.make_scope(scopemod)
        try:
            print("Scope:", scope.connect())
        except Exception as e:
            awg.close()
            raise RuntimeError(
                f"{e}\nNothing was changed. If Scope Grab (or any other "
                f"program) is open it holds the scope's VISA session -- "
                f"close it and try again; otherwise check the scope's power "
                f"and rear-panel USB.")

        try:
            if awg.is_on(awg_ch):
                print(f"REFUSING to auto-set CH{awg_ch}: its output is ON, "
                      f"and changing FRQ/AMP under a live output moves real "
                      f"voltage at the chain. Switch it off first.")
                return
            # Select the session's CURRENT drive if the generator already
            # holds it -- selecting is all autoset may do; uploading is the
            # bench loop's job (or the AWG GUI's, from its Waveforms library).
            stored = awg.list_waveforms(user_only=True)
            pick = next((n for n in stored if n == wname), None) or \
                next((n for n in stored if n.lower() == wname.lower()), None)
            blocks = {
                "OUTP": {"LOAD": "HZ"},
                "SRATE": {"MODE": "DDS"},
                "BSWV": {"WVTP": "ARB", "FRQ": 1.0 / period,
                         "AMP": 2 * fs, "OFST": 0},
                "MODE": ("Burst", {"GATE_NCYC": "NCYC", "TIME": 1,
                                   "TRSR": "EXT"}),
            }
            if pick:
                blocks["ARWV"] = {"NAME": pick}
            # apply_channel writes in the only order the 4063B honours
            # (load first, ARWV and SRATE before BSWV, mode block last, and
            # burst STATE,ON sent separately before its parameters -- the
            # manual requires it, and a combined write drops the type switch)
            missed = awg.apply_channel(awg_ch, blocks,
                                       log=lambda m: print("      ", m))
            if missed:
                print("       did NOT take (apply_channel readback):", missed)
            # trust nothing: read the selection and burst back and show them
            arwv = awg.query(f"C{awg_ch}:ARWV?").strip()
            btwv = awg.query(f"C{awg_ch}:BTWV?").strip()
            print(f"AWG CH{awg_ch}: ARB, DDS, period {period*1e3:.4f} ms "
                  f"(FRQ {1.0/period:.6g} Hz), AMP {2*fs:g} Vpp, OFST 0, "
                  f"load HZ -- output left "
                  f"{'ON' if awg.is_on(awg_ch) else 'OFF'}")
            print(f"       waveform readback: {arwv}")
            if not pick:
                print(f"       NOTE: '{wname}' is not in the generator's user "
                      f"memory (it holds: {', '.join(stored) or 'nothing'}). "
                      f"The selection was left alone -- upload it from the "
                      f"AWG GUI's Waveforms library ({wname}.csv, Normalise "
                      f"OFF), or let the bench loop upload it.")
            print(f"       burst readback: {btwv}")
        finally:
            awg.close()

        try:
            # full window with settle room, waveform start at the left edge:
            # position (trigger -> screen centre) = half the period
            rng = nice_setting(1.3 * period)
            scope.put(":TIMebase:RANGe", f"{rng:.6g}")
            scope.put(":TIMebase:POSition", f"{period/2:.6g}")
            scope.put(":ACQuire:TYPE", "HRES")
            for ch, lo, hi, what in ((awg_ch, u_lo, u_hi, "drive"),
                                     (scope_ch, v_lo, v_hi, "monitor")):
                span = max(hi - lo, 1e-3)
                scale = nice_setting(1.25 * span / 8)      # 8 vertical divs
                mid = 0.5 * (hi + lo)
                scope.put(f":CHANnel{ch}:DISPlay", "1")
                scope.put(f":CHANnel{ch}:SCALe", f"{scale:.6g}")
                scope.put(f":CHANnel{ch}:OFFSet", f"{mid:.6g}")
                print(f"scope CH{ch} ({what}): {scale:.3g} V/div, offset "
                      f"{mid:+.3g} V (signal {lo:+.3g}..{hi:+.3g} V)")
            print(f"scope: {rng*1e3:.4g} ms window ({rng/10*1e3:.4g} ms/div), "
                  f"position +{period/2*1e3:.4g} ms, acquisition HRES")
            print("trigger source and level NOT touched -- they belong to "
                  "the burst-pulse wiring; confirm the shot still fires")
            errs = scope.errors()
            if errs:
                print("scope reported:", errs)
        finally:
            scope.close()

    # --------------------------------------------------------------- upload
    def do_upload(self):
        """Upload the session's current drive into the generator's user
        memory and select it -- the manual-workflow counterpart of what the
        bench loop does every iteration. Same fixed +/-full-scale mapping,
        never normalised."""
        if self.session is None:
            return messagebox.showerror("Upload",
                                        "load or init a session first")
        s = self.session
        try:
            f = self._floats(awg_ch=self.awgch_var)
        except RuntimeError as e:
            return messagebox.showerror("Upload", str(e))
        wname = f"{s.stem}_i{s.iteration:02d}"
        self.run_worker(lambda: self._upload_work(int(f["awg_ch"]), wname,
                                                  s.full_scale),
                        f"uploading {wname}...")

    def _upload_work(self, awg_ch, wname, fs):
        s = self.session
        scopemod, awgmod = self._bench_modules()
        awg = ilc_bench.make_awg(awgmod)
        print("AWG:  ", awg.connect())
        try:
            # warn about an overwrite only when there is something to
            # overwrite -- and remember the generator cannot read a stored
            # waveform back out, so the old samples really are gone
            stored = awg.list_waveforms(user_only=True)
            clash = next((n for n in stored
                          if n.lower() == wname.lower()), None)
            if clash:
                if not self.ask_user(
                        "Overwrite stored waveform?",
                        f"'{clash}' already exists in the generator's user "
                        f"memory, and uploading replaces it.\n\nThe generator "
                        f"cannot read a waveform back out -- the local copies "
                        f"in the AWG GUI's Waveforms library are the only "
                        f"record of the old samples.\n\nOverwrite?"):
                    print(f"upload cancelled: {clash} left as stored")
                    return
            if awg.is_on(awg_ch):
                print(f"note: CH{awg_ch} output is ON -- the new waveform "
                      f"starts playing the moment it is selected")
            n, frac = ilc_bench.upload_drive(awg, awg_ch, wname, s.u, fs)
            print(f"uploaded {wname} to CH{awg_ch}: {n} pts, "
                  f"{100*frac:.1f}% of DAC range, peak "
                  f"{np.abs(s.u).max():.4f} V (fixed mapping 1.0 = {fs:g} V, "
                  f"never normalised)")
            arwv = awg.query(f"C{awg_ch}:ARWV?").strip()
            print(f"       selection readback: {arwv}")
        finally:
            awg.close()

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
            # bk4063b.py, not the GUI file: that repo moved the instrument
            # class out of the panel (its commit 18142f9)
            ag = os.environ.get("AWG_GUI",
                                os.path.join(SIBLINGS, "BK4063B-AWG-GUI",
                                             "bk4063b.py"))
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

        awg = ilc_bench.make_awg(awgmod)
        print("AWG:  ", awg.connect())
        scope = ilc_bench.make_scope(scopemod)
        print("Scope:", scope.connect())
        uploaded_any = False
        switched_on = False
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

            # Switching ON is the direction that puts voltage into something,
            # so it asks first -- and the end-of-run cleanup switches it back
            # off regardless of who turned it on.
            if not awg.is_on(awg_ch):
                if not self.ask_user(
                        "Output is OFF",
                        f"CH{awg_ch} output is OFF, and the loop needs it "
                        f"driving.\n\nTurn CH{awg_ch} ON and run?\n\n"
                        f"(It is switched OFF again when the run ends.)"):
                    print(f"run cancelled: CH{awg_ch} output left OFF")
                    return
                awg.set_output(awg_ch, True)
                switched_on = True
                print(f"CH{awg_ch} output ON (confirmed in dialog)")
                if not awg.is_on(awg_ch):
                    print(f"CH{awg_ch} did not switch on -- aborting")
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
                uploaded_any = True
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
                s.snapshots.append(dict(it=k, y=y, m=m, u=u))
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
            # a finished (or died) run leaves nothing driving the chain --
            # but a run refused at the setup checks leaves the bench as
            # found. "Played anything OR we switched it on" covers the gap
            # where the on-switch succeeded and the first upload did not.
            if uploaded_any or switched_on:
                try:
                    awg.set_output(awg_ch, False)
                    print(f"CH{awg_ch} output OFF (end of run)")
                except Exception as e:
                    print(f"could not switch CH{awg_ch} output off: {e}")
            # both closes leave the shared ResourceManager standing
            awg.close()
            scope.close()
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

    def _out_scale(self):
        return self.session.loop.channel.mon_scale

    def _out_name(self):
        return self.session.loop.channel.out_name

    def _snaps_by_it(self):
        """Stored measurements keyed by iteration; a re-measurement of the
        same iteration replaces the earlier one."""
        m = {}
        for sn in self.session.snapshots:
            m[sn["it"]] = sn
        return m

    def _selected_snaps(self):
        """The iterations the plots show, per the 'Iterations shown' box:
        blank = the last two, 'all', a range '2-5', or a list '0,3,6'."""
        s = self.session
        if s is None:
            return []
        by_it = self._snaps_by_it()
        avail = sorted(by_it)
        spec = self.itersel_var.get().strip().lower()
        if not spec:
            pick = avail[-2:]
        elif spec in ("all", "*"):
            pick = avail
        else:
            its = set()
            try:
                for part in spec.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if "-" in part:
                        a, b = part.split("-", 1)
                        its.update(range(int(a), int(b) + 1))
                    else:
                        its.add(int(part))
            except ValueError:
                self.log(f"iteration selection {spec!r} not understood -- "
                         f"use 'all', '2-5', or '0,3,6'; showing last two")
                its = set(avail[-2:])
            pick = [i for i in avail if i in its]
            if avail and not pick:
                self.log(f"no stored measurements match {spec!r} "
                         f"(available: {avail})")
        return [by_it[i] for i in pick]

    def _snap_label(self, sn):
        d = sn["m"].get("model") if isinstance(sn.get("m"), dict) else None
        return f"iter {sn['it']} ({d})" if d else f"iter {sn['it']}"

    def _iter_colour(self, idx, n):
        return matplotlib.colormaps["viridis"](0.1 + 0.75 * idx / max(n - 1, 1))

    def _redraw_iterations(self):
        """Re-render every per-iteration plot from the current selection."""
        if self.session is None:
            return
        snaps = self._selected_snaps()
        self._plot_error(snaps)
        self._plot_spectrum(snaps)
        self._plot_dcorr(snaps)
        self._plot_convergence()

    def _show_session(self, select_tab=False):
        """Everything drawable from a freshly loaded/inited session: target,
        drive, model prediction, plus any recalled measurements."""
        s = self.session
        snap = s.snapshots[-1] if s.snapshots else None
        pred = None if snap else s.loop.plant.forward(s.u)
        self._plot_waveforms(s.u, snap["y"] if snap else None, pred,
                             snap["it"] if snap else None)
        self._redraw_iterations()
        if select_tab:
            self.nb.select(0)

    def _show_iteration(self, u, y, m, it):
        self._plot_waveforms(u, y, None, it)
        self._redraw_iterations()

    def _plot_waveforms(self, u, y, pred, it):
        s, c = self.session, self._colour()
        sc = self._out_scale()
        tms = s.t * 1e3
        ax = self.ax_out
        ax.clear()
        ax.plot(tms, s.loop.target * sc, color=TARGET_COLOUR, lw=1.0,
                label="target", **dot_kw(len(tms)))
        if pred is not None:
            # model output, not data -- dashed and dotless on purpose
            ax.plot(tms, pred * sc, color=PRED_COLOUR, lw=0.9, ls="--",
                    label="model-predicted output")
        if y is not None:
            ax.plot(tms, y * sc, color=c, lw=0.9,
                    label=f"measured (iter {it})", **dot_kw(len(tms)))
        ax.set_ylabel(f"{self._out_name()} voltage (V)")
        ax.legend(loc="best", fontsize=7)
        ax.set_title(f"{s.channel} '{s.stem}' -- output vs target")
        ax.grid(True, alpha=0.3)

        ax = self.ax_drv
        ax.clear()
        ax.plot(tms, u, color=c, lw=0.9,
                label=f"drive u (iteration {s.iteration})", **dot_kw(len(tms)))
        fs = s.full_scale
        ax.axhline(fs, color="#c62828", lw=0.8, ls="--")
        ax.axhline(-fs, color="#c62828", lw=0.8, ls="--",
                   label=f"+/-{fs:g} V full scale (AMP {2*fs:g} Vpp, OFST 0)")
        cap = LIMITS.idle_awg
        ax.axhline(cap, color="#c62828", lw=0.6, ls=":")
        ax.axhline(-cap, color="#c62828", lw=0.6, ls=":")
        ax.plot([tms[0], tms[-1]], [u[0], u[-1]], "o", color=c, ms=4, mfc="none")
        pk = float(np.abs(u).max())
        ax.set_title(f"drive peak {pk:.3f} V = {100*pk/fs:.1f}% of DAC range,  "
                     f"idle {u[0]*1e3:+.1f}/{u[-1]*1e3:+.1f} mV of "
                     f"{cap*1e3:.0f} mV cap", fontsize=8)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("AWG drive (V)")
        ax.legend(loc="best", fontsize=7)
        ax.grid(True, alpha=0.3)
        self.fig_wave._canvas.draw_idle()

    def _plot_error(self, snaps):
        s = self.session
        tms = s.t * 1e3
        sc = self._out_scale()
        ax = self.ax_err
        ax.clear()
        n = len(snaps)
        for idx, sn in enumerate(snaps):
            ax.plot(tms, (s.loop.target - sn["y"]) * sc,
                    color=self._iter_colour(idx, n),
                    lw=1.1 if idx == n - 1 else 0.8,
                    label=self._snap_label(sn),
                    **dot_kw(len(tms), ms=2.6))
        if n:
            m = snaps[-1]["m"]
            ax.set_title(f"target - measured:  iter {snaps[-1]['it']} peak "
                         f"{m['peak_err_hv']:.1f} V, rms {m['rms_err_hv']:.2f} V"
                         f"  ({m['peak_pct']:.3f}% FS)")
            ax.legend(loc="best", fontsize=7, ncols=2 if n > 6 else 1)
        else:
            ax.text(0.5, 0.5, "no measurements stored yet",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#888888")
        ax.axhline(0, color=TARGET_COLOUR, lw=0.5)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel(f"error at the {self._out_name()} (V)")
        ax.grid(True, alpha=0.3)
        self.fig_err._canvas.draw_idle()

    def _plot_spectrum(self, snaps):
        s = self.session
        sc = self._out_scale()
        ax = self.ax_spec
        ax.clear()

        def asd(e):
            npts = len(e)
            f = np.fft.rfftfreq(npts, s.loop.dt)
            return f[1:], np.abs(np.fft.rfft(e * sc))[1:] * 2 / npts

        n = len(snaps)
        for idx, sn in enumerate(snaps):
            fe, ae = asd(s.loop.target - sn["y"])
            ax.loglog(fe, ae, color=self._iter_colour(idx, n),
                      lw=1.0 if idx == n - 1 else 0.7,
                      label=self._snap_label(sn),
                      **dot_kw(len(fe), ms=2.2))
        if s.loop.frf is not None:
            ax.axvspan(s.loop.frf.f_use, s.loop.frf.f_max, color="#c68000",
                       alpha=0.15, label="FRF taper band")
            ax.axvline(s.loop.frf.f_max, color="#c68000", lw=0.7, ls="--")
        else:
            ax.axvline(s.loop.f_cut, color="#c68000", lw=0.7, ls="--",
                       label=f"f_cut {s.loop.f_cut/1e3:g} kHz")
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel(f"error amplitude at the {self._out_name()} (V)")
        ax.set_title("where the residual lives -- the update only acts left "
                     "of the band edge")
        if n:
            ax.legend(loc="best", fontsize=7, ncols=2 if n > 6 else 1)
        ax.grid(True, which="both", alpha=0.3)
        self.fig_spec._canvas.draw_idle()

    def _plot_dcorr(self, snaps):
        """The drive side of the error plot: each iteration's AWG waveform
        minus the target's flat conversion -- the correction the loop has
        accumulated at the input, in millivolts at the AWG."""
        s = self.session
        tms = s.t * 1e3
        ax = self.ax_dcor
        ax.clear()
        by_it = self._snaps_by_it()
        if 0 in by_it and by_it[0].get("u") is not None:
            u_ref, ref_lab = by_it[0]["u"], "the iteration-0 drive"
        else:
            try:
                g = self._first_shot_gain()
            except RuntimeError:
                g = None
            g = g or s.loop.plant.gain
            u_ref = s.loop.target / g
            ref_lab = f"the flat conversion target/{g:g}"
        n = len(snaps)
        shown = 0
        skipped = []
        for idx, sn in enumerate(snaps):
            u = sn.get("u")
            if u is None or len(u) != len(s.t):
                skipped.append(sn["it"])
                continue
            ax.plot(tms, (u - u_ref) * 1e3, color=self._iter_colour(idx, n),
                    lw=1.1 if idx == n - 1 else 0.8,
                    label=self._snap_label(sn),
                    **dot_kw(len(tms), ms=2.6))
            shown += 1
        if shown:
            ax.legend(loc="best", fontsize=7, ncols=2 if shown > 6 else 1)
        else:
            ax.text(0.5, 0.5, "no drives stored for the selected iterations",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#888888")
        if skipped:
            ax.annotate(f"no stored drive for iter {skipped}", (0.02, 0.02),
                        xycoords="axes fraction", fontsize=7, color="#888888")
        ax.axhline(0, color=TARGET_COLOUR, lw=0.5)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("drive correction at the AWG (mV)")
        ax.set_title(f"drive minus {ref_lab} -- what the loop has learned "
                     f"to add at the input")
        ax.grid(True, alpha=0.3)
        self.fig_dcor._canvas.draw_idle()

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
        ax.set_ylabel(f"error at the {self._out_name()} (V)")
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
        self.nb.select(5)
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
