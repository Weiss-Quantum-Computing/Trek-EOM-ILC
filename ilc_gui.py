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
import types

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
    _hint = ("Run this with the Anaconda interpreter -- "
             "C:\\ProgramData\\anaconda3\\pythonw.exe -- which is the only "
             "one on this machine with scipy, pandas and pyvisa together."
             if sys.platform == "win32" else
             "Install the analysis stack first:\n"
             "    pip install numpy scipy pandas matplotlib\n"
             "(pyvisa is only needed for the bench buttons, not here.)")
    messagebox.showerror("EOM-ILC GUI", f"Missing package: {_e.name}.\n\n{_hint}")
    raise SystemExit(1)

import matplotlib
matplotlib.use("TkAgg")
matplotlib.rcParams["font.size"] = 8
from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                               NavigationToolbar2Tk)
from matplotlib.figure import Figure

from eomilc import scope as scopeio, plant as plantmod, ilc, outputs
from eomilc import corrections as corrmod, mains
from eomilc import optical     # the optical close (needs rampol only when used)
from eomilc.config import CHANNELS, LIMITS, HV_PER_MON
from eomilc import channels as chanmap   # scope channel ROLES, shared with the polarimeter
from eomilc import diagnostics as dg     # per-measurement stats, folds, floors
import matplotlib.dates as mdates
import ilc_bench            # the debugged bench helpers; main() is guarded
import ilc_hardware         # the Hardware tab's device checks
import ilc_batch            # batch runs: X1 + X2 together, a list of targets
import run_ilc              # load_target and the state-file conventions

# Remembered between sessions, kept out of the repo so a git pull cannot
# clobber it -- same convention as the sibling panels. Off Windows there is
# no APPDATA: macOS gets its native equivalent, anything else the home dir.
CONFIG_PATH = chanmap.PANEL_CONFIG_PATH      # ...\EOM-ILC-GUI\config.json

# Consolas is a Windows font; Tk elsewhere would silently substitute a
# PROPORTIONAL face and wreck the log/summary column alignment.
MONO = (("Consolas", 8) if sys.platform == "win32"
        else ("Menlo", 10) if sys.platform == "darwin"
        else ("Courier", 9))

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
    # Monitor on CH2, not CH1: the bench reads the DRIVE from scope channel
    # <awg_ch> (Auto-set sets its verticals from the drive, the alignment
    # check cross-correlates it against the record, Measure FRF divides the
    # monitor by it). With both on CH1 the alignment check was correlating
    # the monitor against the drive, and an FRF would have been Y/Y = 1.
    "GEN": dict(mon_col="CH2", awg_ch=1, scope_ch=2,
                frf=None, colour="#2e7d32"),
}
TARGET_COLOUR = "#222222"
PRED_COLOUR = "#8a8a8a"
# compare-stem overlays: one colour per stem, leading with hues far from
# viridis (the active session's dark-purple-to-yellow iteration ramp) --
# purple and cyan sit last because they can pass for viridis endpoints
CMP_COLOURS = ["#ff7f0e", "#e377c2", "#8c564b", "#9467bd", "#17becf",
               "#7f7f7f"]
# Compare-stem traces sit UNDER the session's own: they are drawn after the
# session's in every tab, so at the default z-order (2 for a line) they
# painted over the running state's newest points. Above the grid (1.5).
CMP_ZORDER = 1.8

# The MSO-X 2014A's 8-bit converter carries a fixed error pattern per code:
# ~3.4 mV pk-pk over the 40.25 mV code at 1 V/div, measured 2 Sep 2026 (fold
# the flat shot's residual on monitor voltage: one sharp period at 40.25 mV,
# flat at 31.25/25/15.6/62.5). The code scales with V/div; this is the ratio.
ADC_CODE_PER_VDIV = float(os.environ.get("EOMILC_ADC_CODE_PER_VDIV", 0.04025))
# Another scope has another code: set EOMILC_ADC_CODE_PER_VDIV to its
# code-per-V/div ratio (fold a flat shot's residual on monitor voltage to
# find it), or 0 to switch the dither off.
# The Compare box's grammar, shown where the status line would be once
# something is loaded -- the hint is only wanted while the box is empty.
CMP_HINT = ("nothing loaded -- stems in this run folder, e.g. "
            "'TSTX1 OLDX1:all OLDX2:0,3' (blank sel = last iter)")
# The status line has to be kept to a length: a Label asks for room to draw
# its text, and the request goes all the way up to the window, so a line of
# campaign names would resize the panel every time one was added.
CMP_STATUS_CHARS = 96
# The Model tab's frame title: single mode, and column 1 / column 2 of a pair.
MODEL_FRAME_TEXT = "Inverse model (what the update divides the error by)"
PAIR_KEYS = ("X1", "X2")
STEM_NOTE = f"(<= {NAME_LIMIT - 4} chars; '_iNN' is appended)"
STEM_NOTE_PAIR = f"(<= {NAME_LIMIT - 6} chars; X1/X2 + '_iNN' appended)"
PAIR_CHANNEL = {"X1": "EO1", "X2": "EO2"}


def _norm_path(p):
    """A path in the one spelling paths are compared in. The file dialog
    hands back forward slashes on Windows and whatever case the folder is
    stored in, so a picked path and a typed one for the same file are not
    equal as strings."""
    return os.path.normcase(os.path.abspath(p))


def _same_file(a, b):
    return _norm_path(a) == _norm_path(b)


def elide(text, width):
    """Long text shortened from the middle, so both ends survive - which for
    a campaign key is the stem at one end and the folder tag at the other."""
    text = str(text)
    if len(text) <= width:
        return text
    keep = max(width - 3, 2)
    head = (keep + 1) // 2
    return text[:head] + "..." + text[len(text) - (keep - head):]

# The model ladder: what the update divides the error by, in increasing order
# of how much of the chain it knows about.  The first three are parametric
# (eomilc.plant.Plant with the unused terms zeroed -- a gain-only Plant IS the
# zeroth-order case); the last is the measured inverse, whose reach is set by
# the taper band rather than by f_cut.
MODEL_LABELS = (
    ("gain only (0th order)", "static"),
    ("one pole (1st order)", "one_pole"),
    ("second order (resonant)", "resonant"),
    ("third order (resonant + pole)", "third_order"),
    ("measured FRF (nonparametric)", "frf"),
)
LABEL2KEY = dict(MODEL_LABELS)
KEY2LABEL = {k: l for l, k in MODEL_LABELS}
PARAMS_FOR = {"static": ("gain",), "one_pole": ("gain", "tau"),
              "resonant": ("gain", "fn", "zeta"),
              "third_order": ("gain", "fn", "zeta", "tau"), "frf": ()}
DESC_FOR = {"static": "gain only", "one_pole": "one pole",
            "resonant": "2nd order", "third_order": "3rd order"}


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
        self.model_key = ""          # which inverse drives the campaign --
        self.frf_path = ""           # recorded in the state so the runs
        self.frf_use = 0.0           # are self-documenting and a resumed
        self.frf_max = 0.0           # session restores the same model
        self.frf_error = ""          # why the recorded FRF would not load
        self.seed_path = ""          # the drive Init played as iteration 0,
                                     # when it was not the flat conversion
        self.target_path = ""        # the target CSV Init read, so Load can
                                     # put it back in the Target box
        # Learned target corrections (eomilc.corrections): loop.target is
        # base_target + corr_total once any are applied; None = never.
        # Monitor volts, like loop.target.
        self.base_target = None
        self.corr_total = None
        self.corr_layers = []        # [{path, slot, gain, on, ...}] as applied
        self.line_fit = None         # {"fit": mains record, "meta": {...}}
        self.scope_channels = ""     # chanmap.record() of the scope map in
                                     # force when this state was measured

    @property
    def channel(self):
        return self.loop.channel.name


def load_session(path) -> Session:
    st = run_ilc.load_state(path)
    loop = run_ilc.build_loop(st)
    s = Session(state_path=os.path.abspath(path), loop=loop,
                t=st["t"], u=st["u"], iteration=int(st["iteration"]),
                stem=str(st["name"]), full_scale=float(st["full_scale"]),
                t_off=float(st["t_offset"]))
    # model record (absent in pre-27-Aug states -- everything defaults off)
    s.model_key = str(st["model"]) if "model" in st else ""
    s.frf_path = str(st["frf_path"]) if "frf_path" in st else ""
    s.frf_use = float(st["frf_use"]) if "frf_use" in st else 0.0
    s.frf_max = float(st["frf_max"]) if "frf_max" in st else 0.0
    s.seed_path = str(st["seed_path"]) if "seed_path" in st else ""
    s.target_path = str(st["target_path"]) if "target_path" in st else ""
    s.scope_channels = str(st["scope_channels"]) if "scope_channels" in st else ""
    if "base_target" in st:
        s.base_target = np.asarray(st["base_target"], float)
        if "corr_total" in st:
            s.corr_total = np.asarray(st["corr_total"], float)
    if "corrections" in st:
        try:
            s.corr_layers = list(json.loads(str(st["corrections"])))
        except ValueError:
            s.corr_layers = []
    if "line_fit" in st:
        try:
            rec = json.loads(str(st["line_fit"]))
            s.line_fit = {"fit": mains.from_record(rec["fit"]),
                          "meta": rec.get("meta", {})}
        except (ValueError, KeyError, TypeError):
            s.line_fit = None
    if s.model_key == "frf" and s.frf_path and os.path.exists(s.frf_path):
        try:                       # the loop resumes with its recorded inverse
            loop.frf = ilc.FRF(s.frf_path, f_use=s.frf_use, f_max=s.frf_max)
        except (ValueError, OSError, KeyError) as e:
            # Keep the reason: a state recorded before the taper-band guard
            # can carry a taper outside its own FRF, and "the FRF quietly
            # did not load" is exactly the kind of silence this panel is
            # supposed to break. do_load prints it.
            s.frf_error = str(e)
    return s


def save_session(s: Session):
    """Same keys as run_ilc / ilc_bench write, so the CLIs can resume this.
    The panel's extra keys (run_ilc.PASS_THROUGH) are written only when
    there is something in them, and the CLIs carry them through."""
    lp = s.loop
    extra = {}
    if s.base_target is not None:
        extra["base_target"] = np.asarray(s.base_target, float)
        if s.corr_total is not None:
            extra["corr_total"] = np.asarray(s.corr_total, float)
    if s.corr_layers:
        extra["corrections"] = json.dumps(s.corr_layers)
    if s.line_fit is not None:
        extra["line_fit"] = json.dumps({"fit": mains.to_record(s.line_fit["fit"]),
                                        "meta": s.line_fit.get("meta", {})})
    if lp.line is not None:
        extra["line_ref"] = np.asarray(lp.line, float)
    if s.scope_channels:
        extra["scope_channels"] = str(s.scope_channels)
    np.savez(s.state_path, t=s.t, target=lp.target, u=s.u, dt=lp.dt,
             channel=lp.channel.name, gain=lp.plant.gain, tau=lp.plant.tau,
             offset=lp.plant.offset, tau2=lp.plant.tau2, fn=lp.plant.fn,
             zeta=lp.plant.zeta, full_scale=s.full_scale, name=s.stem,
             gamma=lp.gamma, f_cut=lp.f_cut, iteration=s.iteration,
             t_offset=s.t_off, history=np.array(lp.history, dtype=object),
             model=s.model_key, frf_path=s.frf_path,
             frf_use=s.frf_use, frf_max=s.frf_max, seed_path=s.seed_path,
             target_path=s.target_path,
             notches=np.asarray(lp.notches, float).reshape(-1, 2), **extra)


def find_target_file(target, mon_scale, dirs):
    """The target CSV whose content is `target` (a state's target vector, in
    monitor volts), searched by NAME in `dirs` -- for states from before the
    path was recorded. Same length and the same values, or nothing."""
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith(".csv"):
                continue
            p = os.path.join(d, name)
            try:
                _, v = run_ilc.load_target(p, mon_scale)
            except Exception:
                continue
            if len(v) == len(target) and np.allclose(v, target, rtol=1e-6,
                                                     atol=1e-9):
                return p
    return ""


def avg_spectrum(e, dt, k=1):
    """Amplitude spectrum of e, optionally noise-averaged.

    k = 1: the raw single-record FFT (bin width 1/(N dt)). Full frequency
    resolution, but a periodogram's per-bin variance is ~100% of its value
    and does NOT average down with record length -- more points only add
    more equally-noisy bins.
    k > 1: Hann-windowed 50%-overlap segment averaging (Welch), segment
    length N//k. Noise variance drops ~sqrt(segments); resolution coarsens
    to ~k/(N dt), so tones closer than that merge and content localised in
    time (burst edges) smears across segments.

    Normalised by the window's coherent gain, so a tone sitting on an
    integer bin OF THE SEGMENT keeps its amplitude. A tone that does not is
    scalloped by the Hann window -- worst case, half a bin off, it reads
    0.85x (measured: a 3.000 V tone reads 2.551 V at k=4 on the 5301-point
    grid, and 2.887 V at k=8). The broadband noise floor moves too: it
    scales with bin width, and the coherent-gain normalisation lifts it by
    a further sqrt(ENBW). Only compare curves drawn at equal k."""
    e = np.asarray(e, float)
    n = len(e)
    k = max(1, min(int(k), n // 16))
    if k == 1:
        f = np.fft.rfftfreq(n, dt)
        return f[1:], np.abs(np.fft.rfft(e))[1:] * 2 / n
    nseg = n // k
    w = np.hanning(nseg)
    hop = max(1, nseg // 2)
    acc, m = 0.0, 0
    for i0 in range(0, n - nseg + 1, hop):
        acc = acc + np.abs(np.fft.rfft(w * e[i0:i0 + nseg]))
        m += 1
    f = np.fft.rfftfreq(nseg, dt)
    return f[1:], (acc / m)[1:] * 2 / w.sum()


# The readout-count table moved into eomilc.scope so ilc_bench can size its
# alignment readout by the same rule the panel's captures use. Re-bound here
# because every call site in this file (and the smoke test) names it locally.
SCOPE_PTS = scopeio.SCOPE_PTS
scope_points_for = scopeio.scope_points_for


def pd_channel(cfg=None):
    """Scope channel carrying the photodiode, or None if there is no optical
    readout wired.

    The channel with the PD role on the Measure tab's Scope channels (the
    same map the polarimeter uses); EOMILC_PD_CH overrides it for one
    session, and a config written before the roles existed may still carry
    the old "pd_channel" key, honoured when no map says otherwise. When set,
    Measure FRF reads it alongside the monitor and writes frf_<name>_PD.csv
    next to the usual one.
    """
    v = os.environ.get("EOMILC_PD_CH")
    if v is None and cfg is not None:
        if cfg.get("scope_channels"):
            v = chanmap.channel_for(cfg, "PD")
        else:
            v = cfg.get("pd_channel")
    if v in (None, "", "none", "None"):
        return None
    ch = int(v)
    if ch not in (1, 2, 3, 4):
        raise ValueError(f"pd_channel must be 1-4, got {ch!r}")
    return ch


AWG_MAX_PTS = int(os.environ.get("BK4063B_MAX_PTS", 8_000_000))
# The 4060B series (this bench's 4063B) takes arbs of 8 pts to 8 Mpts (the
# polarimeter, rampol/awg.py, 8 Oct 2026). 16384 stood here until 14 Oct: the
# DS345's arb memory, carried over by mistake, and the suite's 33 ms spin-echo
# records (16667 pts at 2 us) were being cut short to fit it. Largest played so
# far: 7001 pts by the polarimeter; the first 16667-point upload is the probe.

# The FRF probe record's own cap, apart from the generator's arb memory: the
# scope reads 2.2 x the points back per shot, so a dense probe is shortened
# past this count (the planner's 'short' plan) as it was when 16384 was
# believed to be the arb cap (14 Oct 2026).
FRF_MAX_PTS = 16384
PROBE_NYQ_MARGIN = 0.5
# Top probe tone <= this fraction of the probe grid's Nyquist: >= 4 samples
# per period at the top tone. The first cut allowed 0.98 -- tones riding
# just under Nyquist keep barely 2 samples per period, and although the
# Y/U division cancels the DAC's zero-order-hold droop (both channels are
# measured), it does NOT cancel the images folding back near Nyquist or
# the resampling fidelity loss, and the coherence up there pays for both.
# Dense records cost nothing, so buy the margin instead.


def plan_frf_grid(n_sess, dt_sess, f_hi, max_pts=None):
    """Pick the probe's own record (mode, n, dt) for a requested f_hi.

    The 2 us campaign grid is a DOWNSTREAM constraint -- the analog card's
    lookup table updates every 2 us -- not a bench one: in DDS mode the
    4063B resamples the whole stored record into one period of FRQ, so the
    effective sample interval is period/points and a denser record probes
    higher with nothing else changing. Three regimes:

      'session' -- f_hi fits the session grid: probe on (n_sess, dt_sess),
                   instruments untouched beyond the upload.
      'dense'   -- same record duration, more points (fits arb memory):
                   FRQ, burst, trigger and scope window all unchanged.
      'short'   -- past what the arb memory carries at full duration: the
                   record shortens to max_pts * dt, FRQ rises to 1/record
                   and the scope window tightens (both restored at the
                   end); the frequency bins coarsen to 1/record.

    dt is chosen so f_hi <= PROBE_NYQ_MARGIN of the grid's Nyquist."""
    max_pts = max_pts or FRF_MAX_PTS
    t_sess = n_sess * dt_sess
    if f_hi > 5e6:
        raise RuntimeError(
            f"f hi {f_hi/1e6:g} MHz is past the 5 MHz sanity ceiling: the "
            f"monitor chain is long dead up there and the scope record "
            f"cannot carry it")
    if f_hi <= PROBE_NYQ_MARGIN * 0.5 / dt_sess:
        return "session", n_sess, dt_sess
    dt_need = PROBE_NYQ_MARGIN * 0.5 / f_hi
    n_dense = int(np.ceil(t_sess / dt_need))
    if n_dense <= max_pts:
        return "dense", n_dense, t_sess / n_dense
    return "short", max_pts, dt_need


def probe_demand(u, dt, plant_gain, ch, lim=LIMITS):
    """What the probe ASKS the amplifier for, under a flat-gain worst case:
    peak EOM slew (V/s), the capacitive current that slew implies, and the
    peak EOM voltage. Flat gain deliberately ignores the chain's rolloff --
    the Trek cannot actually deliver these numbers above its band (its own
    slew/current limiting caps what flows, which is precisely the point:
    the figures measure how hard the probe pushes the amp into limiting,
    not what the EOM will see)."""
    hv = np.asarray(u, float) * plant_gain * ch.mon_scale
    slew = float(np.abs(np.gradient(hv, dt)).max())
    return slew, lim.load_capacitance * slew, float(np.abs(hv).max())


def build_frf_probe(n, dt, peak, f_lo, f_hi, n_tones):
    """Schroeder multitone on the session's own record (tools/sysid_make
    maths): tones on integer FFT bins so the analysis is leak-free, cosine
    end tapers so the AWG idles at zero between bursts. Raises when the
    requested band does not fit the grid -- the tones live on this record's
    bins, so the hard ceiling is the grid Nyquist, not a knob."""
    from tools import sysid_make
    nyq = 0.5 / dt
    rec = n * dt
    if not (0 < f_lo < f_hi):
        raise RuntimeError("need 0 < f lo < f hi")
    if f_hi > 0.98 * nyq:
        raise RuntimeError(
            f"f hi {f_hi/1e3:g} kHz is past what the {dt*1e6:g} us record "
            f"can carry: probe tones live on this grid's FFT bins, ceiling "
            f"98% of Nyquist = {0.98*nyq/1e3:.0f} kHz. (plan_frf_grid picks "
            f"a denser record for bands like this -- go through it.)")
    if f_lo < 2 / rec:
        raise RuntimeError(f"f lo {f_lo:g} Hz is below the record's second "
                           f"bin (~{2/rec:.0f} Hz)")
    bins = sysid_make.tone_bins(f_lo, f_hi, int(n_tones), n=n, dt=dt)
    if len(bins) < 8:
        raise RuntimeError(f"only {len(bins)} distinct tone bins between "
                           f"{f_lo:g} and {f_hi:g} Hz -- widen the band or "
                           f"ask for fewer tones")
    return sysid_make.multitone(peak, bins, n=n, dt=dt), bins


def build_ramped_probe(n_total, dt, peak, v_dc, hold_ms, f_lo, f_hi,
                       n_tones, awg_rail=10.0):
    """A multitone riding on a held level, on the session's own record.

    The flat probe sits around zero, so it measures the chain small-signal
    about 0 V. The ramps do not live there: the EOM capacitance is voltage
    dependent, and at the corners this chain answers a drive ripple with
    ~4x what the zero-centred FRF predicts (P92PX1D, 2 Sep) -- which is
    what makes the loop over-correct there. This shape reaches an
    operating point within the burst, holds it, probes, and comes back,
    so the EOMs are never parked at a standing kV.

    Layout (tools.sysid_make.ramped_multitone): edge, settle, HOLD+tones,
    settle, edge -- sized to the session's own record, so FRQ, the burst
    and the scope window are exactly the ramp campaign's and nothing has
    to be retuned. The tones are integer bins of the HOLD, not of the
    record, which is what keeps the analysis leak-free; resolution is
    1/hold, and only the hold excites anything.

    Returns (u, bins, (i0, i1)) with the window on the record's index base.
    """
    from tools import sysid_make
    if not (0 < f_lo < f_hi):
        raise RuntimeError("need 0 < f lo < f hi")
    nyq = 0.5 / dt
    if f_hi > PROBE_NYQ_MARGIN * nyq:
        raise RuntimeError(
            f"f hi {f_hi/1e3:g} kHz is past {PROBE_NYQ_MARGIN:g} of this "
            f"grid's Nyquist ({PROBE_NYQ_MARGIN*nyq/1e3:.0f} kHz). The ramped "
            f"probe stays on the session's record so the burst and the scope "
            f"window are untouched, so the grid is the ceiling.")
    n_hold = int(round(hold_ms * 1e-3 / dt))
    r = n_total - n_hold
    if n_hold < 300 or r < 8:
        raise RuntimeError(
            f"a {hold_ms:g} ms hold is {n_hold} of the record's {n_total} "
            f"samples; it needs at least 300 (the tone taper) and has to "
            f"leave room for two edges and two settles")
    n_edge = n_settle = r // 4
    n_hold = n_total - 2 * (n_edge + n_settle)   # rounding goes to the hold
    rec = n_hold * dt
    if f_lo < 2 / rec:
        raise RuntimeError(
            f"f lo {f_lo:g} Hz is below the hold's second bin (~{2/rec:.0f} "
            f"Hz) -- lengthen the hold or raise f lo")
    bins = sysid_make.hold_tone_bins(f_lo, f_hi, int(n_tones), n_hold, dt)
    if len(bins) < 8:
        raise RuntimeError(f"only {len(bins)} distinct tone bins over a "
                           f"{rec*1e3:.2f} ms hold -- widen the band, "
                           f"lengthen the hold, or ask for fewer tones")
    u, win = sysid_make.ramped_multitone(peak, v_dc, bins, n_hold, dt=dt,
                                         n_edge=n_edge, n_settle=n_settle,
                                         awg_rail=awg_rail)
    return u, bins, win


def write_frf_csv(path, f_hz, H, coh):
    """The four columns ilc.FRF and Show FRF consume; sysid_fit.py stays
    the tool for the full diagnostic (model columns + png)."""
    pd.DataFrame({"f_Hz": f_hz, "H_mag": np.abs(H),
                  "H_phase_deg": np.degrees(np.angle(H)),
                  "coherence": coh}).to_csv(path, index=False,
                                            float_format="%.6g")


def recall_snapshots(s: Session):
    """Pull a session's on-disk measurements (meas_<stem>_i*.npy beside the
    state, paired with the drive CSVs that played them) back into
    s.snapshots. Grid mismatches (a state rebuilt on a different step) are
    skipped rather than guessed at."""
    run_dir = os.path.dirname(s.state_path)
    for f in sorted(glob.glob(os.path.join(run_dir, f"meas_{s.stem}_i*.npy"))):
        y = np.load(f)
        if len(y) != len(s.t):
            continue
        mo = re.search(r"_i(\d+)(?:_r(\d+))?\.npy$", f)
        if mo is None:
            continue
        it = int(mo.group(1))
        run = int(mo.group(2)) if mo.group(2) else None
        # the file's mtime IS the measurement time -- both save paths
        # write the array the moment the capture average completes
        snap = dict(it=it, y=y, m=s.loop.metrics(y), run=run,
                    t_wall=os.path.getmtime(f))
        st = dg.load_stats(dg.stats_path(f))      # the shots' statistics, when kept
        if st is not None:
            snap["stats"] = st
        # pair it with the drive that played it, so Fit uses a true pair
        dcsv = os.path.join(run_dir, f"drive_{s.stem}_i{it:02d}.csv")
        if it == 0 and not os.path.exists(dcsv):
            # campaigns Init'ed before 30 Aug 2026 wrote iteration 0 as
            # drive_<stem>_iter0.csv (run_ilc.py init still does)
            legacy = os.path.join(run_dir, f"drive_{s.stem}_iter0.csv")
            if os.path.exists(legacy):
                dcsv = legacy
        if os.path.exists(dcsv):
            du = pd.read_csv(dcsv, comment="#").iloc[:, 1].to_numpy(float)
            if len(du) == len(s.t):
                snap["u"] = du
        if run is None:
            recall_pd(snap, run_dir, s)
        s.snapshots.append(snap)


def recall_pd(snap, run_dir, s):
    """The PD of a base measurement, when it was recorded: the pair's (or a
    batch campaign's) meas_<stem>PD_iNN.npy, <stem> the session's stem without
    its X1 / X2, into snap["pd"] and its sidecar into snap["pd_stats"]. Both
    chains of a pair recall the same file."""
    pstem = ilc_batch.pd_stem(s.stem)
    if pstem is None:
        return
    f = ilc_batch.pd_path(run_dir, pstem, snap["it"])
    if not os.path.exists(f):
        return
    y = np.load(f)
    if len(y) != len(s.t):
        return
    snap["pd"] = y
    snap["pd_stats"] = dg.load_stats(dg.stats_path(f))


def read_captures(pattern, mon_col, t, t_off, stack=None):
    """Average every capture the glob matches, with the span guard from
    run_ilc: one zoomed file extrapolated flat once manufactured 172 V of
    fake error out of a real 26 V, so a short capture is a hard refusal.

    `stack`, a dict, gets stack["stack"] = the (n_files, len(t)) traces
    behind the average -- the noise-floor readout needs the shots, not
    their mean. A dict rather than a third return so every caller of the
    two-tuple stays as it is (the same shape as _bench_capture's aux_store)."""
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
    if stack is not None:
        stack["stack"] = np.asarray(traces, float)
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


def fmt_span(seconds):
    """A wall-clock interval, sized for a legend: 37s, 4.2m, 1.53h."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.2f}h"


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


# ------------------------------------------------------------------ batch
class _Prefixed(io.TextIOBase):
    """A stdout that starts every line with a tag (pair mode's X1 / X2).
    A line already indented by more than the tag keeps its column: the tag
    replaces the leading spaces instead of pushing the text right."""

    def __init__(self, out, tag):
        self.out, self.tag, self.buf = out, tag, ""

    def _tagged(self, line):
        if not line.strip():
            return line
        if line.startswith(" " * (len(self.tag) + 1)):
            return self.tag + line[len(self.tag):]
        return f"{self.tag} {line}"

    def write(self, text):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.out.write(self._tagged(line) + "\n")
        return len(text)

    def flush(self):
        if self.buf:
            self.out.write(self._tagged(self.buf))
            self.buf = ""
        self.out.flush()


class BatchWindow:
    """The Batch window: build, check, simulate and run an ilc_batch plan --
    a list of target pairs trained X1 and X2 together, one after another.
    Every run goes through App.run_worker, so the panel's busy state and its
    Stop apply; the window only displays and collects settings, and can be
    closed and reopened while a batch runs."""

    COLS = (("stem", "campaign", 72), ("pts", "points", 52),
            ("ms", "record ms", 72), ("est", "~min", 46),
            ("status", "status", 88), ("X1", "X1", 150), ("X2", "X2", 150),
            ("note", "note", 280))
    LOOP = (("max iterations", "max_iterations", 4, int),
            ("min", "min_iterations", 3, int),
            ("stop at rms V", "stop_rms_v", 5, float),
            ("shots", "repeats", 4, int))

    def __init__(self, app):
        self.app = app
        self.plan = None
        self.view_sim = False        # the table shows the run/sim manifest
        self.est = {}                # stem -> estimated seconds (Check plan)
        w = self.win = tk.Toplevel(app.root)
        w.title("EOM-ILC -- batch (X1 and X2 together)")
        w.geometry(app.cfg.get("batch_geometry", "960x540"))
        w.protocol("WM_DELETE_WINDOW", self.close)
        top = ttk.Frame(w, padding=4)
        top.pack(fill="both", expand=True)

        r = ttk.Frame(top)
        r.pack(fill="x")
        ttk.Label(r, text="Plan").pack(side="left")
        # buttons before the entry: pack order decides who survives (CLAUDE.md)
        ttk.Button(r, text="New plan...", command=self.new_plan).pack(side="right")
        ttk.Button(r, text="Reload", command=self.load).pack(side="right", padx=2)
        ttk.Button(r, text="...", width=3, command=self.browse).pack(side="right")
        e = ttk.Entry(r, textvariable=app.batchplan_var)
        e.pack(side="left", fill="x", expand=True, padx=2)
        app._show_tail(e, app.batchplan_var)
        e.bind("<Return>", lambda ev: self.load())

        r = ttk.Frame(top)
        r.pack(fill="x", pady=(3, 0))
        self.loop_vars = {}
        for lab, key, width, _ in self.LOOP:
            ttk.Label(r, text=lab).pack(side="left", padx=(0, 2))
            v = tk.StringVar()
            ttk.Entry(r, textvariable=v, width=width).pack(side="left", padx=(0, 8))
            self.loop_vars[key] = v
        ttk.Label(r, text="(written into the plan when you run it)",
                  foreground="#666666").pack(side="left")

        tf = ttk.Frame(top)
        tf.pack(fill="both", expand=True, pady=3)
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in self.COLS],
                                 show="headings", height=12, selectmode="browse")
        for key, head, width in self.COLS:
            self.tree.heading(key, text=head)
            self.tree.column(key, width=width, stretch=(key == "note"),
                             anchor="w")
        ysb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda ev: self.open_selected())
        # Esc clears the selection, so Preview draws the whole plan again
        self.tree.bind("<Escape>", lambda ev: self.tree.selection_set(()))

        r = ttk.Frame(top)
        r.pack(fill="x")
        ttk.Checkbutton(r, text="follow in the plots:",
                        variable=app.batchfollow_var).pack(side="left")
        ttk.Combobox(r, textvariable=app.batchfollowch_var, values=("X1", "X2"),
                     width=4, state="readonly").pack(side="left", padx=(2, 10))
        ttk.Checkbutton(r, text="also finished campaigns (continue them)",
                        variable=app.batchredo_var).pack(side="left")
        ttk.Checkbutton(r, text="every iteration under its own AWG name",
                        variable=app.batchgui_var).pack(side="left", padx=(10, 0))

        r = ttk.Frame(top)
        r.pack(fill="x", pady=(3, 0))
        self.btns = []
        for text, mode in (("Check plan", "check"), ("Simulate", "sim"),
                           ("Run on bench", "bench")):
            b = ttk.Button(r, text=text, command=lambda m=mode: app.do_batch_run(m))
            b.pack(side="left", padx=(0, 4))
            self.btns.append(b)
        # Preview: every campaign's targets on the Batch plot tab, or the
        # selected campaign alone with its first drive (App.do_batch_preview)
        b = ttk.Button(r, text="Preview", command=app.do_batch_preview)
        b.pack(side="left", padx=(0, 4))
        self.btns.append(b)
        self.stop_btn = ttk.Button(r, text="Stop", command=app.stop_evt.set)
        self.stop_btn.pack(side="left")
        self.status = ttk.Label(r, text="", font=MONO)
        self.status.pack(side="left", padx=8)
        ttk.Label(top, foreground="#666666", justify="left", text=(
            "AWG CH1/CH2 drive X1/X2; scope CH1/CH2 read the drives, CH3/CH4 the "
            "monitors. Close the AWG GUI and Scope Grab first.\n"
            "Set AMP/OFST/load/burst once (Auto-set); the batch only checks them. "
            "Outputs go ON after one confirmation, OFF between campaigns.\n"
            "Double-click a campaign to load its state (the followed channel) "
            "in the panel. Simulate writes to run/sim, never to run.\n"
            "Preview draws every campaign's targets on the Batch plot tab; with "
            "a campaign selected, that one alone with the first drive it plays "
            "(Esc in the table clears the selection).")
                  ).pack(fill="x")
        self.set_busy(app.busy)
        self.load()

    # -- window ---------------------------------------------------------
    def close(self):
        self.app.cfg["batch_geometry"] = self.win.winfo_geometry()
        self.app._batch_win = None
        self.win.destroy()

    def set_busy(self, busy):
        for b in self.btns:
            b.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def browse(self):
        p = filedialog.askopenfilename(
            parent=self.win, title="Batch plan",
            initialdir=os.path.dirname(self.app.batchplan_var.get()) or RUN_DIR,
            filetypes=(("Batch plan", "*.json"), ("All files", "*.*")))
        if p:
            self.app.batchplan_var.set(p)
            self.load()

    # -- the plan and its table -------------------------------------------
    def load(self):
        self.tree.delete(*self.tree.get_children())
        self.plan, self.est = None, {}
        path = self.app.batchplan_var.get().strip()
        if not path:
            self.status.configure(text="no plan -- pick one or make one")
            return
        try:
            self.plan = ilc_batch.load_plan(path, cfg=self.app.cfg)
        except (OSError, ValueError, KeyError, SystemExit) as e:
            self.status.configure(text=f"cannot read the plan: {e}")
            return
        for _, key, _, _ in self.LOOP:
            self.loop_vars[key].set(f"{self.plan['loop'][key]:g}")
        for c in self.plan["campaigns"]:
            self.tree.insert("", "end", iid=c["stem"], values=(c["stem"],))
        self.view_sim = False
        self.show_manifest()
        n = len(self.plan["campaigns"])
        self.status.configure(text=f"{self.plan.get('name', '')}: {n} campaign(s)"
                                   f" -- Check plan for points and times")

    def _set(self, stem, **cols):
        if not self.tree.exists(stem):
            return
        vals = dict(zip([c[0] for c in self.COLS], self.tree.item(stem, "values")))
        vals.update({k: v for k, v in cols.items()})
        self.tree.item(stem, values=[vals.get(c[0], "") for c in self.COLS])

    def show_manifest(self):
        if self.plan is None:
            return
        man = ilc_batch.read_manifest(self.plan, self.view_sim)
        tag = " (sim)" if self.view_sim else ""
        for c in self.plan["campaigns"]:
            e = man["campaigns"].get(c["stem"])
            if not e:
                self._set(c["stem"], status="to do", X1="", X2="", note="")
                continue
            self._set(c["stem"], status=e.get("status", "?") + tag,
                      **self._entry_cols(e))

    @staticmethod
    def _entry_cols(e):
        cols, stops = {}, []
        for key, ch in (e.get("channels") or {}).items():
            cols[key] = (f"keep i{ch['keeper_iteration']:02d} "
                         f"{ch['keeper_rms_v']:.2f} V")
            stops.append(f"{key} {ch['stop']}")
        note = e.get("error") or ", ".join(stops)
        if e.get("card"):
            c = e["card"]
            note += (f"; card: {len(c.get('files', []))} files, up "
                     f"{c.get('up_ms', 0):.3f} ms, down {c.get('down_ms', 0):.3f} ms")
        if e.get("card_error"):
            note += f"; card NOT written: {e['card_error']}"
        if e.get("minutes") is not None:
            note += f"  ({e['minutes']:.0f} min)"
        cols["note"] = note
        return cols

    def set_rows(self, rows):
        """Check plan's result: points, record length, estimate, problems."""
        self.est = {r["stem"]: r["est_s"] for r in rows}
        for r in rows:
            cols = dict(pts=r["n"] or "", ms=f"{r['period_ms']:.3f}" if r["n"] else "",
                        est=f"{r['est_s']/60:.0f}" if r["n"] else "")
            if r["error"]:
                cols.update(status="PROBLEM", note=r["error"])
            elif any(r.get("start_it", {}).values()):
                cols["note"] = "resumes at " + ", ".join(
                    f"{k} i{v:02d}" for k, v in r["start_it"].items())
            self._set(r["stem"], **cols)
        bad = sum(not r["ok"] for r in rows)
        self.status.configure(text=f"~{sum(self.est.values())/3600:.1f} h at the "
                                   f"iteration cap; {bad} with problems")

    def estimate_s(self, stems):
        return sum(self.est.get(s, 0.0) for s in stems)

    def loop_overrides(self):
        out = {}
        for lab, key, _, kind in self.LOOP:
            txt = self.loop_vars[key].get().strip()
            try:
                v = kind(float(txt)) if kind is int else kind(txt)
            except ValueError:
                raise ValueError(f"{lab}: {txt!r} is not a number")
            if v <= 0:
                raise ValueError(f"{lab} must be positive")
            out[key] = v
        if out["min_iterations"] > out["max_iterations"]:
            raise ValueError("min iterations is above max iterations")
        if out["repeats"] < 16:
            raise ValueError("fewer than 16 shots cannot dither the scope's "
                             "word lattice (see the bench loop's check)")
        return out

    def state_paths(self, stem):
        camp = next(c for c in self.plan["campaigns"] if c["stem"] == stem)
        d = self.plan["_run_dir"] + (os.sep + "sim" if self.view_sim else "")
        short = camp.get("awg", stem)
        return {k: os.path.join(d, f"drive_{short}{k}.state.npz")
                for k in camp.get("channels", ["X1", "X2"])}

    def open_selected(self):
        sel = self.tree.selection()
        if not sel or self.plan is None:
            return
        paths = self.state_paths(sel[0])
        p = paths.get(self.app.batchfollowch_var.get()) or next(iter(paths.values()))
        if not os.path.exists(p):
            self.status.configure(text=f"no state yet: {os.path.basename(p)}")
            return
        self.app.state_var.set(p)
        self.app.do_load()

    # -- events from a running batch (main thread) -------------------------
    def event(self, kind, info):
        if kind == "batch":
            self.view_sim = info["simulated"]
            for stem in info["todo"]:
                self._set(stem, status="queued" + (" (sim)" if self.view_sim else ""),
                          X1="", X2="")
        elif kind == "campaign":
            self._set(info["stem"], status="running")
            if self.tree.exists(info["stem"]):
                self.tree.see(info["stem"])
        elif kind == "iteration":
            txt = f"i{info['it']:02d} {info['rms_v']:.2f} V"
            if info["stop"]:
                txt += f" ({info['stop']})"
            self._set(info["stem"], **{info["key"]: txt})
            self.status.configure(text=f"{info['stem']} {info['key']} {txt}")
        elif kind == "campaign_end":
            e = info["entry"]
            self._set(info["stem"], status=e.get("status", "?")
                      + (" (sim)" if self.view_sim else ""), **self._entry_cols(e))
        elif kind == "batch_end":
            self.show_manifest()
            self.status.configure(text="batch ended -- see the log")

    # -- new plan -------------------------------------------------------------
    def new_plan(self):
        PlanDialog(self)


class PlanDialog:
    """New plan...: target pairs from a folder, in run order, trained with the
    settings of two existing states (ilc_batch.build_plan)."""

    def __init__(self, bw):
        self.bw = bw
        app = bw.app
        d = self.win = tk.Toplevel(bw.win)
        d.title("New batch plan")
        d.transient(bw.win)
        f = ttk.Frame(d, padding=6)
        f.pack(fill="both", expand=True)
        plan = bw.plan or {}
        st = plan.get("settings", {})
        self.targets = tk.StringVar(value=plan.get("_targets_dir", ""))
        self.stems = tk.StringVar()
        self.x1 = tk.StringVar(value=(st.get("X1") or {}).get("from_state", ""))
        self.x2 = tk.StringVar(value=(st.get("X2") or {}).get("from_state", ""))
        self.name = tk.StringVar(value="")
        self.card = tk.StringVar(value=str((plan.get("card") or {}).get("n", 2500)))
        rows = (("Targets folder", self.targets, self.pick_folder),
                ("X1 trained like", self.x1, lambda: self.pick_state(self.x1)),
                ("X2 trained like", self.x2, lambda: self.pick_state(self.x2)))
        for i, (lab, var, cmd) in enumerate(rows):
            ttk.Label(f, text=lab).grid(row=2 * i, column=0, sticky="w")
            e = ttk.Entry(f, textvariable=var, width=60)
            e.grid(row=2 * i, column=1, sticky="ew", padx=2)
            app._show_tail(e, var)
            ttk.Button(f, text="...", width=3, command=cmd).grid(row=2 * i, column=2)
            hint = ("folder of target_<stem>X1.csv / X2.csv pairs" if i == 0 else
                    "a state whose model, gains, notches and FRF to copy "
                    "(blank = the production FRF defaults)")
            ttk.Label(f, text=hint, foreground="#666666").grid(
                row=2 * i + 1, column=1, sticky="w")
        ttk.Label(f, text="Stems, run order").grid(row=6, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.stems, width=60).grid(
            row=6, column=1, columnspan=2, sticky="ew", padx=2)
        self.found = ttk.Label(f, text="", foreground="#666666", wraplength=520,
                               justify="left")
        self.found.grid(row=7, column=1, columnspan=2, sticky="w")
        r = ttk.Frame(f)
        r.grid(row=8, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(r, text="Name").pack(side="left")
        ttk.Entry(r, textvariable=self.name, width=12).pack(side="left", padx=(2, 12))
        ttk.Label(r, text="card samples").pack(side="left")
        ttk.Entry(r, textvariable=self.card, width=6).pack(side="left", padx=2)
        ttk.Label(r, text="(0 = no card files)", foreground="#666666").pack(side="left")
        r = ttk.Frame(f)
        r.grid(row=9, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(r, text="Cancel", command=d.destroy).pack(side="right")
        ttk.Button(r, text="Save plan as...", command=self.save).pack(
            side="right", padx=4)
        f.columnconfigure(1, weight=1)
        self.targets.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def refresh(self):
        stems = ilc_batch.target_stems(self.targets.get().strip())
        self.found.configure(text=(f"{len(stems)} pairs found: " + ", ".join(stems))
                             if stems else "no target pairs in that folder")
        if stems and not self.stems.get().strip():
            self.stems.set(", ".join(stems))

    def pick_folder(self):
        p = filedialog.askdirectory(parent=self.win, title="Targets folder",
                                    initialdir=self.targets.get() or HERE)
        if p:
            self.stems.set("")
            self.targets.set(p)

    def pick_state(self, var):
        p = filedialog.askopenfilename(
            parent=self.win, title="State to copy the settings of",
            initialdir=RUN_DIR, filetypes=(("State", "*.npz"), ("All files", "*.*")))
        if p:
            # inside the repo: stored relative, so the plan works from any clone
            rel = os.path.relpath(p, HERE)
            var.set(p if rel.startswith("..") else rel.replace(os.sep, "/"))

    def save(self):
        name = self.name.get().strip()
        stems = [x.strip() for x in re.split(r"[,\s]+", self.stems.get()) if x.strip()]
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("give the plan a name (letters, digits, - and _)")
            if not stems:
                raise ValueError("no stems")
            card = int(self.card.get().strip() or 0)
        except ValueError as e:
            return messagebox.showerror("New plan", str(e), parent=self.win)
        targets = self.targets.get().strip()
        out = filedialog.asksaveasfilename(
            parent=self.win, title="Save plan", defaultextension=".json",
            initialdir=os.path.dirname(os.path.abspath(targets)) if targets else RUN_DIR,
            initialfile=f"batch_{name}.json",
            filetypes=(("Batch plan", "*.json"),))
        if not out:
            return
        try:
            plan = ilc_batch.build_plan(targets, stems, out, name, self.x1.get().strip(),
                                        self.x2.get().strip(), card)
        except (ValueError, OSError) as e:
            return messagebox.showerror("New plan", str(e), parent=self.win)
        self.bw.app.log(f"wrote {out}: {len(plan['campaigns'])} campaigns")
        self.bw.app.batchplan_var.set(out)
        self.win.destroy()
        self.bw.load()


# -------------------------------------------------------------------- app
class App:
    def __init__(self, root):
        self.root = root
        root.title("EOM-ILC -- iterative learning control")
        self.msgs = queue.Queue()
        self.stop_evt = threading.Event()
        self._closing = False         # set by on_close; ask_user watches it
        self.busy = False
        self.session: Session | None = None
        self._run_id = None           # the bench run in progress (its start time)
        self._modules = None          # (scope_grab, awg_gui) once bench-loaded
        self._wave_redraw = None      # replays the Waveforms tab's last draw
        self._t_range = None          # linked time window across time plots
        self._tlink_busy = False
        self.cfg = self._load_config()
        root.geometry(self.cfg.get("geometry", "1380x880"))

        self._build_ui()
        self.log(f"--- panel started; timestamped log appends to {LOG_PATH}")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.pump)
        # reopen where the last session left off: the remembered state (and
        # every stored measurement sharing its stem) reloads on startup, so
        # closing the panel never costs the plots
        if self.cfg.get("state") and os.path.exists(self.cfg["state"]):
            root.after(200, self._auto_reload)

    # ---------------------------------------------------------------- config
    def _load_config(self):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_config(self):
        self._remember_fields()
        self._remember_fields_2()
        c = dict(self.cfg)
        c.update(field_memory=self._field_mem,
                 geometry=self.root.winfo_geometry(),
                 state=self.state_var.get(), target=self.target_var.get(),
                 measured=self.meas_var.get(), frf=self.frf_var.get(),
                 f_use=self.fuse_var.get(), f_max=self.fmax_var.get(),
                 model=self.model_var.get(), channel=self.channel_var.get(),
                 stem=self.stem_var.get(), shot_gain=self.shotgain_var.get(),
                 notch=self.notch_var.get(),
                 iter_sel=self.itersel_var.get(),
                 cmp_sel=self.cmpsel_var.get(),
                 cmp_paths=dict(self._cmp_paths),
                 spec_avg=self.specavg_var.get(),
                 dot_step=self.dotstep_var.get(),
                 show_runs=self.showruns_var.get(),
                 dt_labels=self.dtlabels_var.get(),
                 link_t=self.tlink_var.get(),
                 hold_runs=self.holdruns_var.get(),
                 hold_gap=self.holdgap_var.get(),
                 keep_native=self.keepnative_var.get(),
                 keep_verticals=self.keepvert_var.get(),
                 seed_drive=self.seed_var.get(),
                 line_f=self.linef_var.get(), line_h=self.lineh_var.get(),
                 line_win=self.linewin_var.get(),
                 line_seg=self.lineseg_var.get(),
                 corr_band=self.corrband_var.get(), corr_k=self.corrk_var.get(),
                 corr_floor=self.corrfloor_var.get(),
                 corr_cap=self.corrcap_var.get(),
                 corr_fade=self.corrfade_var.get(),
                 corr_active=bool(self.corract_var.get()),
                 repeats=self.repeats_var.get(), iterations=self.iters_var.get(),
                 batch_plan=self.batchplan_var.get(),
                 batch_follow=self.batchfollow_var.get(),
                 batch_follow_ch=self.batchfollowch_var.get(),
                 pair=bool(self.pair_var.get()),
                 echo=dict(self.cfg.get("echo", {})),
                 optical={k: v.get() for k, v in self._opt_vars.items()})
        if self._batch_win is not None:
            c["batch_geometry"] = self._batch_win.win.winfo_geometry()
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
        # _closing before destroy(): a worker parked in ask_user is waiting
        # on a dialog the pump will never deliver once the root is gone.
        self._closing = True
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

        # The settings live in tabs, in the order a campaign uses them:
        # Session (what is being learned), Model (what the update divides
        # by), Measure (how a capture becomes an error), Run (step / bench /
        # hold), Optical (the optical close by hand, pair mode), Corrections
        # (learned changes to the TARGET from another measurement). The
        # session summary, the log and the status line sit under the tabs and
        # are always visible. Tabs are selected by FRAME (self._tabs), never
        # by index.
        self.lnb = ttk.Notebook(left)
        self.lnb.pack(fill="x")
        self._tabs = {}
        for key, label in (("hw", "Hardware"), ("session", "Session"),
                           ("model", "Model"), ("scope", "Scope"),
                           ("run", "Run"), ("optical", "Optical"),
                           ("corr", "Corrections")):
            fr = ttk.Frame(self.lnb, padding=2)
            self.lnb.add(fr, text=label)
            self._tabs[key] = fr
        tab_session, tab_model = self._tabs["session"], self._tabs["model"]
        tab_measure, tab_run = self._tabs["scope"], self._tabs["run"]
        self._actions = []           # every button a running worker disables
        self._build_hw_tab(self._tabs["hw"])

        # ---- pair mode: X1 and X2 learned side by side ------------------
        # Ticked, the State and Target rows take the X1 file and the X2 file
        # is found by name (X1 -> X2); Load and Init act on both, each chain
        # keeps its own model (the Model tab grows an X2 column), and the
        # bench loop plays both drives and reads both monitors from one
        # acquisition. self.pair holds the two sessions; self.session is the
        # one the plots show (the 'shown' radio above the plots).
        self.pair = None
        self.pair_var = tk.BooleanVar(value=bool(self.cfg.get("pair", False)))
        self.shown_var = tk.StringVar(value="X1")
        self._col2 = None            # the Model tab's X2 column, built on first tick
        self._mem_model_2 = None     # the X2 column's model in the field memory
        pr = ttk.Frame(tab_session)
        pr.pack(fill="x", pady=(0, 2))
        b = ttk.Checkbutton(pr, text="Pair (X1 + X2)", variable=self.pair_var,
                            command=self._on_pair_toggle)
        b.pack(side="left")
        self._actions.append(b)
        ttk.Label(pr, text="(State / Target take the X1 file; X2's is the same "
                           "name with X2)", foreground="#666666").pack(
            side="left", padx=(6, 0))

        # ---- session: two paths, resume or start ----------------------
        rf = ttk.LabelFrame(tab_session, text="Resume a campaign", padding=3)
        rf.pack(fill="x", pady=(0, 2))
        self.state_var = tk.StringVar(value=self.cfg.get("state", ""))
        self.state_entry = self._path_row(
            rf, 0, "State", self.state_var,
            lambda: self._browse(self.state_var, "State files",
                                 "*.state.npz", RUN_DIR))
        b = ttk.Button(rf, text="Load state", command=self.do_load)
        b.grid(row=0, column=3, padx=2)
        self._actions.append(b)
        rf.columnconfigure(1, weight=1)

        sf = ttk.LabelFrame(tab_session, text="New campaign", padding=3)
        sf.pack(fill="x", pady=(0, 2))
        self.target_var = tk.StringVar(value=self.cfg.get("target", ""))
        self._path_row(sf, 0, "Target", self.target_var, self._browse_target)
        b = ttk.Button(sf, text="Build...", command=self.do_build_target)
        b.grid(row=0, column=3, padx=2)
        self._actions.append(b)
        b = ttk.Button(sf, text="Plot", command=self.do_preview_target)
        b.grid(row=0, column=4, padx=2)
        self._actions.append(b)
        b = ttk.Button(sf, text="Echo...", command=self.do_echo_dialog)
        b.grid(row=0, column=5, padx=2)
        self._actions.append(b)
        self._echo_win = None
        ch0 = self.cfg.get("channel", "EO1")
        self.channel_var = tk.StringVar(value=ch0 if ch0 in CHANNELS else "EO1")
        self.stem_var = tk.StringVar(value=self.cfg.get("stem", ""))
        r3 = ttk.Frame(sf); r3.grid(row=1, column=0, columnspan=4, sticky="ew", pady=1)
        ttk.Label(r3, text="Channel").pack(side="left")
        cb = ttk.Combobox(r3, textvariable=self.channel_var, width=5,
                          values=list(CHANNELS), state="readonly")
        cb.pack(side="left", padx=(2, 8))
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_channel_change())
        self._channel_cb = cb
        ttk.Label(r3, text="Name stem").pack(side="left")
        ttk.Entry(r3, textvariable=self.stem_var, width=9).pack(side="left", padx=2)
        self._stem_note = ttk.Label(r3, text=STEM_NOTE)
        self._stem_note.pack(side="left")

        r4 = ttk.Frame(sf); r4.grid(row=2, column=0, columnspan=5, sticky="ew", pady=1)
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

        # Seed drive: iteration 0 = an EXISTING drive CSV instead of the flat
        # conversion. This is how a converged drive edited in the Waveform
        # Editor (endpoint clipped, offset added, hold changed) is played and
        # measured against its edited target without re-learning from
        # scratch (the Sep 2026 edit-transfer campaign). Same N and dt as
        # the target, enforced; the limit check runs on it like on any
        # drive. Blank = the flat first shot. Cleared with the channel.
        self.seed_var = tk.StringVar(value=self.cfg.get("seed_drive", ""))
        self._path_row(sf, 3, "Seed drive", self.seed_var,
                       lambda: self._browse(self.seed_var, "Drive CSV",
                                            "*.csv", RUN_DIR))
        ttk.Label(sf, text="(optional: iteration 0 = this drive as-is)",
                  foreground="#666666").grid(row=3, column=3, columnspan=2,
                                             sticky="w")

        b = ttk.Button(sf, text="Init campaign", command=self.do_init)
        b.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(3, 0))
        self._actions.append(b)
        ttk.Label(sf, text="first shot = flat conversion target / gain, or the "
                           "seed drive as-is; then Run tab",
                  foreground="#666666").grid(row=5, column=0, columnspan=5,
                                             sticky="w")
        sf.columnconfigure(1, weight=1)

        # ---- inverse model -------------------------------------------
        # The column(s): column 1 is the channel's in single mode and EO1's
        # in pair mode; the X2 column (pair mode only) packs under it --
        # under, not beside: the tab has the height to spare, and the left
        # panel's width is taken from the plots.
        self._model_row = ttk.Frame(tab_model)
        self._model_row.pack(fill="x", pady=(0, 2))
        vf = ttk.LabelFrame(self._model_row, text=MODEL_FRAME_TEXT, padding=3)
        vf.pack(fill="x")
        self._model_frame1 = vf
        self.model_var = tk.StringVar(
            value=self.cfg.get("model", KEY2LABEL["frf"]))
        if self.model_var.get() not in LABEL2KEY:
            self.model_var.set(KEY2LABEL["frf"])
        r0 = ttk.Frame(vf); r0.grid(row=0, column=0, columnspan=4, sticky="ew")
        ttk.Label(r0, text="Model").pack(side="left")
        mc = ttk.Combobox(r0, textvariable=self.model_var, state="readonly",
                          width=30, values=[l for l, _ in MODEL_LABELS])
        mc.pack(side="left", padx=(2, 0))
        mc.bind("<<ComboboxSelected>>", lambda e: self._on_model_change())

        r1 = ttk.Frame(vf); r1.grid(row=1, column=0, columnspan=4,
                                    sticky="ew", pady=1)
        self.pgain_var = tk.StringVar()
        self.ptau_var = tk.StringVar()
        self.pfn_var = tk.StringVar()
        self.pzeta_var = tk.StringVar()
        self._param_vars = {"gain": self.pgain_var, "tau": self.ptau_var,
                            "fn": self.pfn_var, "zeta": self.pzeta_var}
        self._param_entries, self._param_labels = {}, {}
        for lab, key, w in (("gain", "gain", 7), ("tau us", "tau", 6),
                            ("fn Hz", "fn", 6), ("zeta", "zeta", 6)):
            lw = ttk.Label(r1, text=lab)
            lw.pack(side="left", padx=(0, 2))
            e = ttk.Entry(r1, textvariable=self._param_vars[key], width=w)
            e.pack(side="left", padx=(0, 8))
            self._param_entries[key] = e
            self._param_labels[key] = lw

        r2 = ttk.Frame(vf); r2.grid(row=2, column=0, columnspan=4,
                                    sticky="ew", pady=1)
        self.gamma_var = tk.StringVar(value="0.6")
        self.fcut_var = tk.StringVar(value="5000")
        ttk.Label(r2, text="gamma").pack(side="left", padx=(0, 2))
        ttk.Entry(r2, textvariable=self.gamma_var, width=5).pack(
            side="left", padx=(0, 8))
        self._fcut_label = ttk.Label(r2, text="f_cut Hz")
        self._fcut_label.pack(side="left", padx=(0, 2))
        self._fcut_entry = ttk.Entry(r2, textvariable=self.fcut_var, width=7)
        self._fcut_entry.pack(
            side="left", padx=(0, 6))
        self._band_note = ttk.Label(r2, text="(learning gain; parametric band edge)",
                                    foreground="#666666")
        self._band_note.pack(side="left")

        # Learning notches: the update is blind at these lines (both rungs).
        # The monitors carry a free-running ~24 kHz line the loop can only
        # chase once f_cut is above it; the noise-floor line names the
        # candidates. Remembered per channel: each Trek has its own line.
        # Row 6: rows 3-5 are the FRF rows below (gridded after this), and the
        # note is kept short -- a Label asks for its natural width and the
        # request goes all the way up to the window, widening the panel.
        rn = ttk.Frame(vf); rn.grid(row=6, column=0, columnspan=4,
                                    sticky="ew", pady=1)
        self.notch_var = tk.StringVar(value=self.cfg.get("notch", ""))
        ttk.Label(rn, text="notch Hz").pack(side="left", padx=(0, 2))
        ttk.Entry(rn, textvariable=self.notch_var, width=18).pack(
            side="left", padx=(0, 6))
        ttk.Label(rn, text="(f0/width, e.g. 23700/400; update blind there)",
                  foreground="#666666").pack(side="left")

        r3 = ttk.Frame(vf); r3.grid(row=3, column=0, columnspan=4,
                                    sticky="ew", pady=(2, 1))
        # One button, two fits, explained in the dialog it opens. The old
        # 'From calibration' (the Aug ramp-fit tables, Trek-only, and a
        # resonance the FRFs later contradicted) is gone: parameters are
        # typed or fitted to data from THIS system, nothing else.
        b = ttk.Button(r3, text="Replace values with a fit...",
                       command=self.do_fit_dialog)
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)

        self.frf_var = tk.StringVar(value=self.cfg.get("frf", ""))
        self._path_row(vf, 4, "FRF", self.frf_var,
                       lambda: self._browse(self.frf_var, "FRF CSV",
                                            "frf_*.csv", RUN_DIR))
        r4 = ttk.Frame(vf); r4.grid(row=5, column=0, columnspan=4, sticky="ew")
        self.fuse_var = tk.StringVar(value=self.cfg.get("f_use", "50e3"))
        self.fmax_var = tk.StringVar(value=self.cfg.get("f_max", "75e3"))
        l1 = ttk.Label(r4, text="full strength to Hz")
        l1.pack(side="left")
        self._fuse_entry = ttk.Entry(r4, textvariable=self.fuse_var, width=7)
        self._fuse_entry.pack(side="left", padx=(2, 8))
        l2 = ttk.Label(r4, text="taper to zero at Hz")
        l2.pack(side="left")
        self._fmax_entry = ttk.Entry(r4, textvariable=self.fmax_var, width=7)
        self._fmax_entry.pack(side="left", padx=2)
        # (widget, pack options) in order, so the taper row can be hidden on
        # the parametric rungs and come back in the same order
        self._taper_widgets = ((l1, dict(side="left")),
                               (self._fuse_entry, dict(side="left", padx=(2, 8))),
                               (l2, dict(side="left")),
                               (self._fmax_entry, dict(side="left", padx=2)))
        b = ttk.Button(r4, text="Show FRF", command=self.do_show_frf)
        b.pack(side="right")
        self._actions.append(b)
        b = ttk.Button(r4, text="Measure FRF...", command=self.do_measure_frf)
        b.pack(side="right", padx=(0, 4))
        self._actions.append(b)
        vf.columnconfigure(1, weight=1)
        # Column 1 as a column: the same variables and widgets, named the
        # way the X2 column's are, so the model helpers take either.
        self._col1 = types.SimpleNamespace(
            key="X1", frame=vf, model_var=self.model_var,
            param_vars=self._param_vars, param_entries=self._param_entries,
            param_labels=self._param_labels, gamma_var=self.gamma_var,
            fcut_var=self.fcut_var, fcut_label=self._fcut_label,
            fcut_entry=self._fcut_entry, band_note=self._band_note,
            notch_var=self.notch_var, frf_var=self.frf_var,
            fuse_var=self.fuse_var, fmax_var=self.fmax_var,
            fuse_entry=self._fuse_entry, fmax_entry=self._fmax_entry,
            taper_widgets=self._taper_widgets)
        self._update_model_fields()
        # What the panel shows belongs to THIS channel and model; the memory
        # is what every other (channel, model) pair last had. Read once here,
        # kept current at every switch and on save.
        self._field_mem = dict(self.cfg.get("field_memory") or {})
        self._mem_channel = self.channel_var.get()
        self._mem_model = self._model_key()

        # ---- capture post-processing ---------------------------------
        # Settings that shape how a MEASUREMENT is turned into an error --
        # nothing here touches the first shot, which is a pure flat
        # conversion. Applies to both Step and the bench loop.
        self._build_channels_frame(tab_measure)
        pf = ttk.LabelFrame(tab_measure, text="Capture (Step + Bench)", padding=3)
        pf.pack(fill="x", pady=(0, 2))
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

        # ---- run: the bench flow in order --------------------------------
        # Numbered the way a campaign goes: instruments, learn, hold. The
        # set-once numbers (trigger timeout, the skip/keep ticks, GEN's
        # channel numbers) live in Bench settings..., the hand-capture
        # path in Manual step... -- both reachable, neither in the way.
        # The vars are created here whatever dialog shows them, so every
        # worker and test reads the same ones.
        self.meas_var = tk.StringVar(value=self.cfg.get("measured", ""))
        self.moncol_var = tk.StringVar(value="CH3")
        self.refit_var = tk.BooleanVar(value=False)
        self.force_var = tk.BooleanVar(value=False)
        self.awgch_var = tk.StringVar(value="1")
        self.scopech_var = tk.StringVar(value="3")
        self.iters_var = tk.StringVar(value=str(self.cfg.get("iterations", "2")))
        self.repeats_var = tk.StringVar(value=str(self.cfg.get("repeats", "64")))
        self.wait_var = tk.StringVar(value="30")
        self.skip_var = tk.BooleanVar(value=False)
        self.keepnative_var = tk.BooleanVar(
            value=bool(self.cfg.get("keep_native", False)))
        # Auto-set sizes the scope verticals from the session's spans, so a
        # session with a smaller target (an edited endpoint) lands on a
        # finer V/div and a different 8-bit lattice than the campaign it is
        # compared against. Ticked, Auto-set leaves V/div and offset alone.
        self.keepvert_var = tk.BooleanVar(
            value=bool(self.cfg.get("keep_verticals", False)))
        self.holdruns_var = tk.StringVar(value=str(self.cfg.get("hold_runs", "5")))
        self.holdgap_var = tk.StringVar(value=str(self.cfg.get("hold_gap", "30")))
        # Which stored iteration to hold: blank = the current drive. Any
        # earlier drive_<stem>_iNN.csv of the loaded campaign can be
        # re-measured without Init (which would restart the campaign at
        # i00 under the same names) -- the runs are tagged to THAT
        # iteration, so 'iter 9 r1' sits beside iteration 9's own point.
        self.holditer_var = tk.StringVar(value="")
        # the Batch window's settings live here, so they outlast the window
        self._batch_win = None
        self.batchplan_var = tk.StringVar(value=self.cfg.get("batch_plan", ""))
        self.batchfollow_var = tk.BooleanVar(
            value=bool(self.cfg.get("batch_follow", True)))
        self.batchfollowch_var = tk.StringVar(
            value=self.cfg.get("batch_follow_ch", "X1"))
        self.batchredo_var = tk.BooleanVar(value=False)
        self.batchgui_var = tk.BooleanVar(value=False)
        self._bench_last = ""            # the last instrument fact a run reported

        rs = ttk.LabelFrame(tab_run, text="Bench", padding=3)
        rs.pack(fill="x", pady=(0, 2))
        self.run_state = ttk.Label(rs, text="", font=MONO, justify="left")
        self.run_state.pack(fill="x")

        f1 = ttk.LabelFrame(tab_run, text="1  Instruments", padding=3)
        f1.pack(fill="x", pady=(0, 2))
        b = ttk.Button(f1, text="Auto-set instruments", command=self.do_autoset)
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)
        self.upload_btn = ttk.Button(f1, text="Upload drive to AWG",
                                     command=self.do_upload)
        self.upload_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._actions.append(self.upload_btn)

        f2 = ttk.LabelFrame(tab_run, text="2  Learn (upload -> capture -> update, "
                                          "state saved each iteration)", padding=3)
        f2.pack(fill="x", pady=(0, 2))
        ttk.Label(f2, text="iterations").pack(side="left", padx=(0, 2))
        ttk.Entry(f2, textvariable=self.iters_var, width=3).pack(side="left", padx=(0, 8))
        ttk.Label(f2, text="repeats").pack(side="left", padx=(0, 2))
        ttk.Entry(f2, textvariable=self.repeats_var, width=4).pack(side="left", padx=(0, 8))
        self.bench_btn = ttk.Button(f2, text="Run bench loop", command=self.do_bench)
        self.bench_btn.pack(side="left", fill="x", expand=True)
        self._actions.append(self.bench_btn)
        self.stop_btn = ttk.Button(f2, text="Stop", command=self.stop_evt.set,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(4, 0))

        f3 = ttk.LabelFrame(tab_run, text="3  Hold (re-measure, no update)", padding=3)
        f3.pack(fill="x", pady=(0, 2))
        ttk.Label(f3, text="runs").pack(side="left")
        ttk.Entry(f3, textvariable=self.holdruns_var, width=4).pack(side="left", padx=(2, 6))
        ttk.Label(f3, text="gap s").pack(side="left")
        ttk.Entry(f3, textvariable=self.holdgap_var, width=5).pack(side="left", padx=(2, 6))
        ttk.Label(f3, text="iter").pack(side="left")
        ttk.Entry(f3, textvariable=self.holditer_var, width=4).pack(side="left", padx=(2, 6))
        b = ttk.Button(f3, text="Hold", command=self.do_hold)
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)

        more = ttk.Frame(tab_run)
        more.pack(fill="x", pady=(2, 2))
        # not in _actions: opening the window is harmless mid-run, and it is
        # how a batch in progress is watched after its window was closed
        ttk.Button(more, text="Batch...", command=self.do_batch).pack(
            side="left", fill="x", expand=True)
        ttk.Button(more, text="Manual step...", command=self.do_manual_step_dialog).pack(
            side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(more, text="Bench settings...", command=self.do_bench_settings).pack(
            side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Label(tab_run, text="Close the AWG GUI and Scope Grab first (both hold "
                                "VISA). Outputs switch OFF when a run that played "
                                "anything ends.",
                  foreground="#666666", wraplength=470, justify="left").pack(
            fill="x")

        # ---- line ripple + target corrections (their own builders) ------
        self._build_line_row(tab_measure)
        self._build_corr_tab(self._tabs["corr"])
        self._build_optical_tab(self._tabs["optical"])

        # ---- always visible: the session summary, the log, the status -
        self.summary = ttk.Label(left, text="no session loaded", justify="left",
                                 font=MONO)
        self.summary.pack(fill="x", pady=(4, 2))

        # ---- log ------------------------------------------------------
        lf = ttk.LabelFrame(left, text="Log", padding=2)
        lf.pack(fill="both", expand=True)
        self.log_text = tk.Text(lf, width=64, height=10, font=MONO,
                                state="disabled", wrap="none")
        ysb = ttk.Scrollbar(lf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ysb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")

        # ---- status ---------------------------------------------------
        st = ttk.Frame(left)
        st.pack(fill="x", pady=(2, 0))
        self.status = ttk.Label(st, text="ready")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(st, length=160, mode="determinate")
        self.progress.pack(side="right")

        # ---- plot notebook -------------------------------------------
        # deliberately terse -- the grammar of every field is in
        # WORKFLOW_GUI.md section 10, and width here is contested
        sel = ttk.Frame(right)
        sel.pack(fill="x", pady=(0, 2))
        # pair mode: which of the two sessions the plots, the summary and
        # every single-channel tool (Upload, Corrections, Manual step) are on
        self._shown_frame = ttk.Frame(sel)
        ttk.Label(self._shown_frame, text="shown").pack(side="left")
        for k in PAIR_KEYS:
            ttk.Radiobutton(self._shown_frame, text=k, value=k,
                            variable=self.shown_var,
                            command=self._on_shown_change).pack(side="left")
        self._iter_label = ttk.Label(sel, text="Iterations")
        self._iter_label.pack(side="left")
        self.itersel_var = tk.StringVar(value=self.cfg.get("iter_sel", ""))
        e = ttk.Entry(sel, textvariable=self.itersel_var, width=11)
        e.pack(side="left", padx=2)
        e.bind("<Return>", lambda ev: self._redraw_iterations())
        ttk.Label(sel, text="(all, 2-5, 0,3,6)",
                  foreground="#666666").pack(side="left")
        ttk.Label(sel, text="  dot every").pack(side="left")
        self.dotstep_var = tk.StringVar(value=self.cfg.get("dot_step", ""))
        e2 = ttk.Entry(sel, textvariable=self.dotstep_var, width=4)
        e2.pack(side="left", padx=2)
        e2.bind("<Return>", lambda ev: self._redraw_iterations())
        ttk.Label(sel, text="th (1 = all)",
                  foreground="#666666").pack(side="left")
        self.showruns_var = tk.BooleanVar(value=self.cfg.get("show_runs", True))
        ttk.Checkbutton(sel, text="runs", variable=self.showruns_var,
                        command=self._redraw_iterations).pack(side="left",
                                                              padx=(8, 0))
        self.dtlabels_var = tk.BooleanVar(value=self.cfg.get("dt_labels", False))
        ttk.Checkbutton(sel, text="Δt", variable=self.dtlabels_var,
                        command=self._redraw_iterations).pack(side="left")
        self.tlink_var = tk.BooleanVar(value=self.cfg.get("link_t", True))
        ttk.Checkbutton(sel, text="link t", variable=self.tlink_var).pack(
            side="left")
        b = ttk.Button(sel, text="Redraw", command=self._redraw_iterations)
        b.pack(side="right")
        self._actions.append(b)
        self._dot_warned = None

        # second row: other campaigns overlaid read-only on the same plots.
        # Picked with a dialog as well as typed, because the comparison that
        # matters usually crosses folders -- last week's archived campaign
        # against this one -- and those stems cannot be typed at all.
        sel2 = ttk.Frame(right)
        sel2.pack(fill="x", pady=(0, 2))
        ttk.Label(sel2, text="Compare").pack(side="left")
        self.cmpsel_var = tk.StringVar(value=self.cfg.get("cmp_sel", ""))
        e3 = ttk.Entry(sel2, textvariable=self.cmpsel_var, width=30)
        e3.pack(side="left", padx=2)
        e3.bind("<Return>", lambda ev: self._redraw_iterations())
        self._show_tail(e3, self.cmpsel_var)
        b = ttk.Button(sel2, text="Add campaigns...", width=16,
                       command=self.do_compare_add)
        b.pack(side="left", padx=2)
        self._actions.append(b)
        self.cmp_clear_btn = ttk.Button(sel2, text="Clear", width=6,
                                        command=self.do_compare_clear,
                                        state="disabled")
        self.cmp_clear_btn.pack(side="left")
        self._actions.append(self.cmp_clear_btn)
        ttk.Label(sel2, text="segs (blank = raw FFT)",
                  foreground="#666666").pack(side="right")
        self.specavg_var = tk.StringVar(value=self.cfg.get("spec_avg", ""))
        e4 = ttk.Entry(sel2, textvariable=self.specavg_var, width=4)
        e4.pack(side="right", padx=2)
        e4.bind("<Return>", lambda ev: self._redraw_iterations())
        ttk.Label(sel2, text="spectra avg").pack(side="right")
        self._specavg_warned = None

        # third row: what the Compare box actually resolved to. The box holds
        # a grammar, not a result -- a stem typed one letter wrong, or an
        # archive whose state file has moved, used to say so only in the log,
        # under whatever else the panel had printed since.
        sel3 = ttk.Frame(right)
        sel3.pack(fill="x", pady=(0, 2))
        self.cmp_status = ttk.Label(sel3, text=CMP_HINT, foreground="#666666")
        self.cmp_status.pack(side="left")
        self._cmp_cache = {}         # state path -> (state mtime, Session)
        self._cmp_logged = set()     # compare warnings already shown ...
        self._cmp_lastspec = None    # ... for this spec (reset on change)
        # keys picked with the dialog -> the state file they name, so a
        # campaign outside the loaded session's run folder can be compared
        # against at all. A typed key that is not in here still falls back to
        # the run folder, which is what every stem did before the picker.
        self._cmp_paths = {k: v for k, v in
                           (self.cfg.get("cmp_paths") or {}).items()
                           if isinstance(k, str) and isinstance(v, str)}
        self._cmp_last = []          # the groups the last redraw resolved
        self._cmp_described = set()  # campaigns already spelled out

        # Six tabs: the daily views first (waveforms, error, convergence),
        # then the drive diagnostics, the ledger, and the loop's inputs. A
        # tab with more than one figure has a view bar on top; _show_fig
        # brings any figure to the front.
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.nb.bind("<<NotebookTabChanged>>", self._draw_stale_visible)
        self._views = {}
        self.fig_wave, (self.ax_out, self.ax_drv) = self._tab("Waveforms", 2, sharex=True)
        self.fig_err, (self.ax_err,) = self._tab("Error", 1, tab="Error", view="error")
        self.fig_spec, (self.ax_spec,) = self._tab("Error spectrum", 1, tab="Error",
                                                   view="spectrum")
        self.fig_conv, (self.ax_conv,) = self._tab("Convergence", 1, tab="Convergence",
                                                   view="by iteration")
        self.fig_convt, (self.ax_convt,) = self._tab("Convergence (wall clock)", 1,
                                                     tab="Convergence", view="by wall clock")
        self.fig_dcor, (self.ax_dcor,) = self._tab("Drive corrections", 1, tab="Drive",
                                                   view="corrections")
        self.fig_dspec, (self.ax_dspec,) = self._tab("Drive spectrum", 1, tab="Drive",
                                                     view="spectrum")
        self.fig_ddel, (self.ax_ddel,) = self._tab("Drive updates", 1, tab="Drive",
                                                   view="updates")
        # Diagnostics: the repeatable part of the error against its noise,
        # the two quantizers, and each band's movement across iterations
        self.fig_fold, (self.ax_fold, self.ax_foldamp) = self._tab(
            "Code fold", 2, tab="Diagnostics", view="code fold")
        self.fig_latt, (self.ax_latt,) = self._tab("Lattice", 1, tab="Diagnostics",
                                                   view="lattice")
        self.fig_bands, (self.ax_bands,) = self._tab("Bands", 1, tab="Diagnostics",
                                                     view="bands")
        self.fig_lam, (self.ax_lam,) = self._tab("Contraction", 1, tab="Diagnostics",
                                                 view="contraction")
        self.fig_heat, (self.ax_heat,) = self._tab("Heatmap", 1, tab="Diagnostics",
                                                   view="heatmap")
        self._heat_cbar = None
        self.fig_dac, (self.ax_dac,) = self._tab("DAC step", 1, tab="Diagnostics",
                                                 view="DAC step")
        self._dac_inset = None
        # the analyzer photodiode per iteration: how the light answered the loop
        self.fig_pd, (self.ax_pd, self.ax_pdd) = self._tab("PD", 2, tab="Diagnostics",
                                                           view="pd")
        self._table_tab()
        self.fig_frf, self.ax_frf = self._tab("FRF", 3, sharex=True, tab="Inputs",
                                              view="FRF")
        self.fig_corr, self.ax_corr = self._tab("Target corrections", 3, sharex=True,
                                                tab="Inputs", view="target corrections")
        # Optical: the Optical tab's comparison (light, monitors and target;
        # light - monitors), the correction per crystal, and the before /
        # after of a verification
        self.fig_opt, self.ax_opt = self._tab("Optical rotation", 2, sharex=True,
                                              tab="Optical", view="rotation")
        self.fig_optcorr, (self.ax_optcorr,) = self._tab("Optical correction", 1,
                                                         tab="Optical", view="correction")
        self.fig_optva, (self.ax_optva,) = self._tab("Optical before / after", 1,
                                                     tab="Optical", view="before / after")
        # Batch: the Batch window's Preview -- a plan's targets before it runs
        # (one panel per campaign), or one campaign with its first drive; the
        # axes are rebuilt on every preview (_batch_preview_show)
        self.fig_batch, (_ax,) = self._tab("Batch", 1)
        _ax.set_axis_off()
        _ax.text(0.5, 0.5, "Batch window -> Preview draws a plan's targets here",
                 transform=_ax.transAxes, ha="center", va="center", color="#999999")

        # Home on any TIME-domain figure homes them all: the nav stacks of
        # panes that were only ever synced programmatically are empty, so
        # their own Home button would silently do nothing (measured).
        for fig in (self.fig_wave, self.fig_err, self.fig_dcor,
                    self.fig_ddel):
            self._wrap_home(fig._toolbar)

        # the wiring fields (monitor col, AWG/scope channels, FRF autopoint)
        # follow the remembered channel -- they were built with EO1's values
        self._apply_channel_defaults()
        # a panel closed in pair mode opens in pair mode (the startup reload
        # then loads the pair)
        if self.pair_var.get():
            self._on_pair_toggle(quiet=True)
        self._refresh_optical()

    def _tab(self, name, nrows, sharex=False, tab=None, view=None):
        """A figure in the plot notebook. `tab` names the notebook tab it
        lives in (default: its own, titled `name`); `view` names it on that
        tab's view bar when the tab holds several figures."""
        tab = tab or name
        grp = self._views.get(tab)
        if grp is None:
            frame = ttk.Frame(self.nb)
            self.nb.add(frame, text=tab)
            body = ttk.Frame(frame)
            body.pack(side="bottom", fill="both", expand=True)
            grp = self._views[tab] = {"frame": frame, "body": body, "bar": None,
                                      "var": tk.StringVar(), "views": {}}
        host = ttk.Frame(grp["body"])
        if view is None:
            host.pack(fill="both", expand=True)
        else:
            if grp["bar"] is None:
                grp["bar"] = ttk.Frame(grp["frame"])
                grp["bar"].pack(side="top", fill="x")
            ttk.Radiobutton(grp["bar"], text=view, value=view, variable=grp["var"],
                            command=lambda t=tab: self._show_view(t)).pack(
                side="left", padx=(4, 10), pady=(1, 0))
            grp["views"][view] = host
            if len(grp["views"]) == 1:
                grp["var"].set(view)
                host.pack(fill="both", expand=True)
        fig = Figure(figsize=(8.6, 6.2), dpi=100, constrained_layout=True)
        axes = fig.subplots(nrows, 1, sharex=sharex)
        axes = np.atleast_1d(axes)
        canvas = FigureCanvasTkAgg(fig, master=host)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        tb = NavigationToolbar2Tk(canvas, host)      # zoom/pan, scope habits
        fig._canvas = canvas
        fig._toolbar = tb
        fig._tab, fig._view = tab, view
        fig._stale = False
        return fig, axes

    # Drawing is lazy: a plot function fills its axes every time, but the
    # canvas is rasterised only when the figure is on screen -- the rest are
    # marked stale and drawn when their tab or view comes up. Rasterising 17
    # canvases cost 5.3 s of a 5.9 s redraw (12 Oct 2026), every bench
    # iteration and every one of the smoke suite's ~50 redraws.
    def _fig_visible(self, fig):
        grp = self._views.get(getattr(fig, "_tab", None))
        if grp is None:
            return True
        try:
            if self.nb.select() != str(grp["frame"]):
                return False
        except tk.TclError:
            return True
        return fig._view is None or grp["var"].get() == fig._view

    def _draw(self, fig):
        if self._fig_visible(fig):
            fig._stale = False
            fig._canvas.draw_idle()
        else:
            fig._stale = True

    def _draw_stale_visible(self, *_):
        for fig in self._all_figs():
            if getattr(fig, "_stale", False) and self._fig_visible(fig):
                fig._stale = False
                fig._canvas.draw_idle()

    def _all_figs(self):
        return [v for k, v in vars(self).items() if k.startswith("fig_") and hasattr(v, "_canvas")]

    def draw_all(self):
        """Rasterise every stale figure now (saving PNGs of all of them)."""
        for fig in self._all_figs():
            if getattr(fig, "_stale", False):
                fig._stale = False
                fig._canvas.draw_idle()

    def _show_view(self, tab):
        grp = self._views[tab]
        want = grp["var"].get()
        for v, host in grp["views"].items():
            if v == want:
                host.pack(fill="both", expand=True)
            else:
                host.pack_forget()
        self._draw_stale_visible()

    def _show_fig(self, fig):
        """Bring a figure to the front: its tab, and its view on that tab."""
        grp = self._views[fig._tab]
        self.nb.select(grp["frame"])
        if fig._view is not None and grp["var"].get() != fig._view:
            grp["var"].set(fig._view)
            self._show_view(fig._tab)

    def _table_tab(self):
        """The one non-figure tab: a ledger of every stored iteration,
        saveable as CSV the way the figures save as PNG."""
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Table")
        cols = ("stem", "iter", "run", "model", "peak_v", "rms_v",
                "peak_pct", "rms_pct", "u_pk", "at", "dt")
        self._table_heads = ("stem", "iter", "run", "model", "peak err (V)",
                             "rms err (V)", "peak %FS", "rms %FS",
                             "drive pk (V)", "measured at", "dt")
        widths = (54, 36, 36, 104, 82, 82, 72, 72, 82, 122, 72)
        bar = ttk.Frame(frame)
        bar.pack(side="bottom", fill="x")
        b = ttk.Button(bar, text="Save CSV...", command=self._save_table)
        b.pack(side="right", padx=2, pady=2)
        self._actions.append(b)
        ttk.Label(bar, text="every stored iteration and hold run, compare "
                            "stems included -- the Iterations box does not "
                            "filter this ledger",
                  foreground="#666666").pack(side="left", padx=4)
        tv = ttk.Treeview(frame, columns=cols, show="headings")
        for c, h, w in zip(cols, self._table_heads, widths):
            tv.heading(c, text=h)
            tv.column(c, width=w, stretch=(c == "model"),
                      anchor="w" if c in ("stem", "model", "at", "dt")
                      else "e")
        ysb = ttk.Scrollbar(frame, command=tv.yview)
        tv.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        self.table = tv

    # ------------------------------------------------------------ hardware
    HW_ROWS = (("scope", "Scope", "MSO-X: identity, acquisition mode, window, V/div"),
               ("awg", "AWG", "4063B: identity, each output ON/OFF, FRQ, waveform"),
               ("ell14", "ELL14", "analyzer mount (polarimeter's): identity, position"),
               ("ds345", "DS345", "light gate (polarimeter's): identity, address"))

    def _build_hw_tab(self, parent):
        """One look at the rack. Each check opens the device, asks its
        identity and how it stands, and closes it again; nothing is moved
        or programmed. The mount and the DS345 are the polarimeter's: their
        port and address come from its config, and while its window is open
        they report 'in use'."""
        f = ttk.LabelFrame(parent, text="Devices (opened, asked, closed)", padding=3)
        f.pack(fill="x", pady=(0, 2))
        self.hw_status, self.hw_time = {}, {}
        # the analyzer's last known angle (deg, time.time()) from a Check or a
        # Move here; the pair loop writes it into the PD sidecars
        self._analyzer = None
        self.ell_move_var = tk.StringVar(value="")
        i = 0
        for key, name, what in self.HW_ROWS:
            ttk.Label(f, text=name, width=7).grid(row=i, column=0, sticky="w", pady=1)
            self.hw_status[key] = ttk.Label(f, text="not checked", foreground="#666666",
                                            wraplength=330, justify="left")
            self.hw_status[key].grid(row=i, column=1, sticky="w", padx=(0, 6))
            self.hw_time[key] = ttk.Label(f, text="", foreground="#999999", width=8)
            self.hw_time[key].grid(row=i, column=2, sticky="e")
            b = ttk.Button(f, text="Check", width=7,
                           command=lambda k=key: self.do_hw_check([k]))
            b.grid(row=i, column=3, padx=(4, 0))
            self._actions.append(b)
            i += 1
            if key == "ell14":
                # the one control on this tab that moves anything: the
                # analyzer, to an angle in the polarimeter's frame, from below
                mv = ttk.Frame(f)
                mv.grid(row=i, column=1, columnspan=2, sticky="w")
                ttk.Label(mv, text="move to deg").pack(side="left")
                ttk.Entry(mv, textvariable=self.ell_move_var, width=8).pack(
                    side="left", padx=(4, 0))
                b = ttk.Button(f, text="Move", width=7, command=self.do_hw_move)
                b.grid(row=i, column=3, padx=(4, 0), pady=(0, 2))
                self._actions.append(b)
                self.hw_move_btn = b
                i += 1
        f.columnconfigure(1, weight=1)
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=(0, 2))
        b = ttk.Button(r, text="Check all four", command=lambda: self.do_hw_check(
            [k for k, _n, _w in self.HW_ROWS]))
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)
        ttk.Label(parent, foreground="#666666", justify="left", wraplength=470, text=(
            "\n".join(f"{name}: {what}" for _k, name, what in self.HW_ROWS)
            + "\nThe ILC drives the scope and the AWG only. A device held by another "
              "program (the polarimeter on COM3, Scope Grab on the scope) reports "
              "'in use'. The Run tab's Bench line keeps the last thing a run reported."
              "\nMove turns the analyzer to the angle typed (polarimeter frame), from "
              "below as the polarimeter does, reads it back and closes the mount; "
              "refused while the polarimeter window holds COM3. The pair loop records "
              "the last angle read here with the PD.")
        ).pack(fill="x")

    def do_hw_check(self, keys):
        self.run_worker(lambda: self._hw_check_work(list(keys)),
                        "checking " + ", ".join(keys) + "...")

    def _hw_check_work(self, keys):
        pcfg = ilc_hardware.polarimeter_config()
        mods = None
        for key in keys:
            if self.stop_evt.is_set():
                break
            try:
                if key in ("scope", "awg"):
                    if mods is None:
                        mods = self._bench_modules()
                    res = (ilc_hardware.check_scope(mods[0], chanmap.describe(self.cfg).split("\n")[0])
                           if key == "scope" else ilc_hardware.check_awg(mods[1]))
                elif key == "ell14":
                    res = ilc_hardware.check_ell14(self.cfg, pcfg)
                else:
                    res = ilc_hardware.check_ds345(self.cfg, pcfg)
            except Exception as exc:
                res = dict(ok=False, text=f"check failed: {exc}", detail="")
            print(f"{key}: {res['text']}" + (("\n   " + res["detail"].replace("\n", "\n   "))
                                             if res.get("detail") else ""))
            self.msgs.put(("call", lambda k=key, r=res: self._hw_show(k, r)))

    def _hw_show(self, key, res):
        self.hw_status[key].configure(text=res["text"],
                                      foreground="#1b6e1b" if res["ok"] else "#a93a2c")
        self.hw_time[key].configure(text=ilc_hardware.stamp())
        if key == "ell14" and res.get("ok") and np.isfinite(res.get("deg", float("nan"))):
            self._analyzer = (float(res["deg"]), time.time())

    def do_hw_move(self):
        """Main thread: the angle from the entry, then a worker opens the
        mount (as the Check does), approaches the angle from below, reads it
        back and closes it. The row says where it landed, with the time."""
        txt = self.ell_move_var.get().strip()
        try:
            deg = float(txt)
            if not np.isfinite(deg):
                raise ValueError
        except ValueError:
            return messagebox.showerror("ELL14", f"move to deg: {txt!r} is not an angle")
        cfg = {"polarimeter_path": self.cfg.get("polarimeter_path")}
        pcfg = ilc_hardware.polarimeter_config()
        self.run_worker(lambda: self._hw_move_work(deg, cfg, pcfg),
                        f"moving the analyzer to {deg:+.2f} deg...")

    def _hw_move_work(self, deg, cfg, pcfg):
        res = ilc_hardware.move_ell14(deg, cfg, pcfg)
        print(f"ell14: move to {deg:+.2f} deg: {res['text']}"
              + (("\n   " + res["detail"]) if res.get("detail") else ""))
        self.msgs.put(("call", lambda r=res: self._hw_show("ell14", r)))
        if not res["ok"]:
            why = ("the polarimeter window holds the mount's port -- close it (or "
                   "move the analyzer from there) and Move again"
                   if res.get("in_use") else "the mount did not move")
            self.msgs.put(("call", lambda r=res, w=why: messagebox.showerror(
                "ELL14", f"Move refused: {r['text']}\n\n{w}.")))

    # ------------------------------------------------- scope channel roles
    def _build_channels_frame(self, parent):
        """What each scope channel carries, by ROLE -- the polarimeter's
        vocabulary (eomilc.channels). The bench loop, Auto-set, Measure FRF
        and the batch derive their channel numbers from this map: the
        monitor from MonXn, the drive readback from CmdXn (none assigned =
        no alignment check, said in the log), the PD from PD. The map is
        written into every state file the bench measures."""
        f = ttk.LabelFrame(parent, text="Scope channels (roles, as the polarimeter)",
                           padding=3)
        f.pack(fill="x", pady=(0, 2))
        self.chan_role, self.chan_name = {}, {}
        chans = chanmap.channels_of(self.cfg)
        g = ttk.Frame(f)
        g.pack(fill="x")
        for ch in chanmap.CHANNEL_IDS:
            i = int(ch)
            row, col = (i - 1) // 2, 3 * ((i - 1) % 2)
            ttk.Label(g, text=f"CH{ch}").grid(row=row, column=col, sticky="w",
                                              padx=(0 if col == 0 else 10, 0), pady=1)
            self.chan_role[ch] = tk.StringVar(value=chans[ch]["role"])
            cb = ttk.Combobox(g, textvariable=self.chan_role[ch], values=chanmap.ROLES,
                              width=7, state="readonly")
            cb.grid(row=row, column=col + 1, padx=4, pady=1)
            cb.bind("<<ComboboxSelected>>", lambda ev: self._channels_changed())
            self.chan_name[ch] = tk.StringVar(value=chans[ch]["name"])
            e = ttk.Entry(g, textvariable=self.chan_name[ch], width=16)
            e.grid(row=row, column=col + 2, pady=1)
            e.bind("<FocusOut>", lambda ev: self._channels_changed())
        r = ttk.Frame(f)
        r.pack(fill="x", pady=(3, 0))
        ttk.Button(r, text="Copy from polarimeter",
                   command=self.do_channels_from_polarimeter).pack(side="left")
        ttk.Button(r, text="Defaults",
                   command=lambda: self._set_channels(chanmap.DEFAULT_CHANNELS)).pack(
            side="left", padx=(4, 0))
        self.chan_note = ttk.Label(f, text="", foreground="#555555", justify="left",
                                   wraplength=440, font=("TkFixedFont", 8))
        self.chan_note.pack(fill="x", pady=(3, 0))
        self._channels_changed(quiet=True)

    def _channels_from_panel(self):
        return {ch: {"role": self.chan_role[ch].get(),
                     "name": self.chan_name[ch].get().strip()}
                for ch in chanmap.CHANNEL_IDS}

    def _set_channels(self, chans):
        chans = chanmap.normalise(chans)
        for ch in chanmap.CHANNEL_IDS:
            self.chan_role[ch].set(chans[ch]["role"])
            self.chan_name[ch].set(chans[ch]["name"])
        self._channels_changed()

    def _channels_changed(self, quiet=False):
        """The panel's map into the config, the note, and the channel
        numbers the selected chain uses."""
        try:
            chans = chanmap.normalise(self._channels_from_panel())
            self.cfg["scope_channels"] = chans
            note = chanmap.describe(self.cfg)
            # the panel's two short lines: roles by channel, then what each
            # chain reads (the full names are in the boxes above)
            short = "  ".join(f"CH{ch} {chans[ch]['role']}" for ch in chanmap.CHANNEL_IDS)
            rest = " | ".join(L.replace("AWG CH", "AWG ").replace(" -> monitor ", ": mon ")
                              .replace("no readback (CmdX unassigned)", "no readback")
                              for L in note.split("\n")[1:])
            shown = short + "\n" + rest
        except ValueError as e:
            note = shown = str(e)
        self.chan_note.configure(text=shown)
        if not quiet:
            self._apply_channel_defaults()
            self.log("scope channels: " + note.replace("\n", " | "))

    def do_channels_from_polarimeter(self):
        chans = chanmap.read_polarimeter()
        if chans is None:
            return messagebox.showerror(
                "Scope channels", f"no polarimeter config at "
                f"{chanmap.POLARIMETER_CONFIG_PATH}")
        diff = chanmap.differences(self.cfg, chans)
        self._set_channels(chans)
        self.log("scope channels copied from the polarimeter's config"
                 + (": " + ", ".join(diff) if diff else " (no change)"))

    def _drive_scope_ch(self, awg_ch):
        """The scope channel the AWG output is read back on, or None. For
        the Trek chains it is the CmdXn role; GEN keeps the old convention
        (scope channel <awg_ch>) because nothing in the role map is about
        it."""
        if self.channel_var.get() == "GEN":
            return int(awg_ch)
        try:
            return chanmap.channel_for(self.cfg, f"CmdX{int(awg_ch)}")
        except ValueError:
            return None

    # ------------------------------------------------- line (mains) ripple
    def _build_line_row(self, parent):
        """The Scope tab's line-ripple row: the on/off switch, what is
        measured, and a button for the rest. On a line-synchronous trigger
        the mains ripple on the monitor is the same in every shot, so the
        shot average keeps it and the loop learns a drive that cancels it
        AT THAT PHASE -- wrong as soon as the experiment moves its ramps
        against the line resync. Measured once with the drive off and
        subtracted from every measurement (Loop.line), the loop never sees
        it. See eomilc.mains for the measured numbers."""
        lf = ttk.LabelFrame(parent, text="Line (mains) ripple", padding=3)
        lf.pack(fill="x", pady=(0, 2))
        self._line_fit = None            # the panel's last fit, carried to Init
        self.line_on_var = tk.BooleanVar(value=False)
        self.linef_var = tk.StringVar(value=self.cfg.get("line_f", "60"))
        self.lineh_var = tk.StringVar(value=self.cfg.get("line_h", "5"))
        self.linewin_var = tk.StringVar(value=self.cfg.get("line_win", "100"))
        self.lineseg_var = tk.StringVar(value=self.cfg.get("line_seg", ""))
        r0 = ttk.Frame(lf); r0.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(r0, text="subtract the measured ripple from every "
                                 "measurement",
                        variable=self.line_on_var,
                        command=self._on_line_toggle).pack(side="left")
        ttk.Button(r0, text="Line ripple...", command=self.do_line_dialog).pack(
            side="right")
        self.line_status = ttk.Label(lf, text="nothing measured",
                                     foreground="#444444", wraplength=430,
                                     justify="left")
        self.line_status.grid(row=1, column=0, sticky="w", pady=(2, 0))
        lf.columnconfigure(0, weight=1)

    def do_line_dialog(self):
        """Measure / fit / clear the line ripple, and its numbers."""
        fr = self._dialog("_line_win", "Line (mains) ripple")
        if fr is None:
            return
        r1 = ttk.Frame(fr); r1.pack(fill="x", pady=1)
        for lab, var, w in (("line Hz", self.linef_var, 5),
                            ("harmonics", self.lineh_var, 3),
                            ("window ms", self.linewin_var, 5)):
            ttk.Label(r1, text=lab).pack(side="left", padx=(0, 2))
            ttk.Entry(r1, textvariable=var, width=w).pack(side="left",
                                                          padx=(0, 8))
        r2 = ttk.Frame(fr); r2.pack(fill="x", pady=1)
        for text, cmd, w in (("Measure (drive off)", self.do_line_measure, None),
                             ("Fit from captures...", self.do_line_from_files, None),
                             ("Clear", self.do_line_clear, 6)):
            b = ttk.Button(r2, text=text, command=cmd, width=w)
            b.pack(side="left", fill="x", expand=w is None, padx=(0, 4))
            b.configure(state="disabled" if self.busy else "normal")
            self._actions.append(b)
        r3 = ttk.Frame(fr); r3.pack(fill="x", pady=1)
        ttk.Label(r3, text="captures: fit only in ms").pack(side="left")
        ttk.Entry(r3, textvariable=self.lineseg_var, width=18).pack(
            side="left", padx=(2, 4))
        ttk.Label(r3, text="(e.g. -12:0, 10:16.5; blank = all)",
                  foreground="#666666").pack(side="left")
        ttk.Label(fr, text="Only with a line-synchronous trigger: a free-"
                           "running one already averages the ripple away, "
                           "and subtracting a fixed phase would add it back. "
                           "Captures must share the ILC runs' trigger. The "
                           "measured ripple is shown on the Scope tab.",
                  foreground="#666666", wraplength=430,
                  justify="left").pack(fill="x", pady=(4, 0))

    def _dialog(self, attr, title):
        """A settings window (one at a time per `attr`); returns its padded
        frame, or None when it was already open (then it is raised). Buttons
        built into it may join _actions: set_busy drops the dead ones."""
        w = getattr(self, attr, None)
        if w is not None and w.winfo_exists():
            w.lift()
            return None
        w = tk.Toplevel(self.root)
        w.title(title)
        w.transient(self.root)
        w.resizable(False, False)
        setattr(self, attr, w)
        fr = ttk.Frame(w, padding=6)
        fr.pack(fill="both", expand=True)
        return fr

    def do_manual_step_dialog(self):
        """One iteration from captures taken by hand (the workflow before
        the bench loop; Native spectrum reads the same files)."""
        fr = self._dialog("_step_win", "Manual step from captured files")
        if fr is None:
            return
        g = ttk.Frame(fr); g.pack(fill="x")
        self._path_row(g, 0, "Captures", self.meas_var, self._browse_measured)
        g.columnconfigure(1, weight=1)
        r1 = ttk.Frame(fr); r1.pack(fill="x", pady=1)
        ttk.Label(r1, text="monitor col").pack(side="left")
        ttk.Combobox(r1, textvariable=self.moncol_var, width=5,
                     values=("CH1", "CH2", "CH3", "CH4")).pack(side="left", padx=(2, 8))
        ttk.Checkbutton(r1, text="refit plant", variable=self.refit_var).pack(side="left")
        ttk.Checkbutton(r1, text="force", variable=self.force_var).pack(side="left")
        r2 = ttk.Frame(fr); r2.pack(fill="x", pady=(3, 0))
        self.step_btn = ttk.Button(r2, text="Step  (captures -> update drive)",
                                   command=self.do_step)
        self.step_btn.pack(side="left", fill="x", expand=True)
        b = ttk.Button(r2, text="Native spectrum", command=self.do_native_spec)
        b.pack(side="left", padx=(4, 0))
        for w in (self.step_btn, b):
            w.configure(state="disabled" if self.busy else "normal")
            self._actions.append(w)
        ttk.Label(fr, text="The captures glob is averaged as one measurement "
                           "(unique prefixes: the step averages everything "
                           "the glob matches). Nothing is uploaded; play the "
                           "written drive yourself.",
                  foreground="#666666", wraplength=430, justify="left").pack(
            fill="x", pady=(4, 0))

    def do_bench_settings(self):
        """The set-once numbers of the bench loop."""
        fr = self._dialog("_bench_win", "Bench settings")
        if fr is None:
            return
        r0 = ttk.Frame(fr); r0.pack(fill="x", pady=1)
        ttk.Label(r0, text="trigger timeout s").pack(side="left", padx=(0, 2))
        ttk.Entry(r0, textvariable=self.wait_var, width=4).pack(side="left", padx=(0, 6))
        ttk.Label(r0, text="(per shot; raise it for slow burst rates)",
                  foreground="#666666").pack(side="left")
        for text, var, note in (
                ("skip setup checks (don't)", self.skip_var,
                 "a mismatched AMP/OFST/clock silently rescales the drive"),
                ("keep native-rate avg", self.keepnative_var,
                 "meas_<stem>_iNN_native.npz beside every measurement"),
                ("keep verticals", self.keepvert_var,
                 "Auto-set leaves V/div and offset alone (same 8-bit lattice)")):
            r = ttk.Frame(fr); r.pack(fill="x", pady=1)
            ttk.Checkbutton(r, text=text, variable=var).pack(side="left")
            ttk.Label(r, text=note, foreground="#666666").pack(side="left", padx=(6, 0))
        r4 = ttk.Frame(fr); r4.pack(fill="x", pady=(4, 1))
        ttk.Label(r4, text="AWG ch").pack(side="left", padx=(0, 2))
        ttk.Entry(r4, textvariable=self.awgch_var, width=3).pack(side="left", padx=(0, 6))
        ttk.Label(r4, text="scope ch").pack(side="left", padx=(0, 2))
        ttk.Entry(r4, textvariable=self.scopech_var, width=3).pack(side="left", padx=(0, 6))
        ttk.Label(fr, text="Channel numbers: EO1 and EO2 take them from the Scope "
                           "tab's map (a number that contradicts it is refused); "
                           "GEN uses these as typed.",
                  foreground="#666666", wraplength=430, justify="left").pack(
            fill="x", pady=(0, 2))

    def _refresh_run_state(self):
        """The Bench line on the Run tab: the wiring in force and the last
        thing a run reported about the instruments."""
        if not hasattr(self, "run_state"):
            return
        self._refresh_optical()
        if self.pair_var.get():
            lines = []
            for k in PAIR_KEYS:
                try:
                    w = chanmap.wiring(self.cfg, k)
                    lines.append(f"{k} ({w['channel']}): AWG CH{w['awg_ch']} -> monitor "
                                 f"CH{w['mon_ch']}, readback "
                                 + (f"CH{w['drive_ch']}" if w["drive_ch"]
                                    else "none (alignment not verified)"))
                except ValueError as e:
                    lines.append(f"{k}: {e}")
            return self.run_state.configure(
                text="\n".join(lines) + "\n" + (self._bench_last or "instruments: "
                                                "nothing played this session"))
        ch = self.channel_var.get()
        try:
            awg, mon = int(self.awgch_var.get()), int(self.scopech_var.get())
            rb = self._drive_scope_ch(awg)
            line1 = (f"{ch}: AWG CH{awg} -> monitor CH{mon}, readback "
                     + (f"CH{rb}" if rb else "none (alignment not verified)"))
        except (ValueError, TypeError):
            line1 = f"{ch}: channel numbers not set"
        self.run_state.configure(
            text=line1 + "\n" + (self._bench_last or "instruments: nothing played this session"))

    def _bench_note(self, text):
        """A worker's one-line instrument fact for the Bench line (any
        thread: it goes through the message pump)."""
        stamp = datetime.datetime.now().strftime("%H:%M")

        def apply():
            self._bench_last = f"{stamp} {text}"
            self._refresh_run_state()
        if threading.current_thread() is threading.main_thread():
            apply()
        else:
            self.msgs.put(("call", apply))

    def _hint(self):
        """What to do next, for the status line when nothing is running."""
        s = self.session
        if s is None:
            return "ready -- Load state, or Init a campaign (Session tab)"
        hist = [m for m in s.loop.history if isinstance(m, dict) and "rms_err_hv" in m]
        if not hist:
            return (f"ready -- {self._hint_who(s)}: Auto-set, then Run bench "
                    f"loop (Run tab)")
        late = [(i, m["rms_err_hv"]) for i, m in enumerate(hist) if i >= 8] or \
            list((i, m["rms_err_hv"]) for i, m in enumerate(hist))
        i, r = min(late, key=lambda t: t[1])
        return (f"ready -- {self._hint_who(s)}: lowest rms {'since i08 ' if len(hist) > 8 else ''}"
                f"is i{i:02d} ({r:.2f} V at the EOM)")

    def _hint_who(self, s):
        """The status hint's name for the shown session: its stem, and in
        pair mode which chain of the pair it is."""
        k = self._pair_key_of(s)
        return f"{s.stem} (shown: {k} of the pair)" if k else s.stem

    def _line_segments(self):
        """'-12:0, 10:16.5' (ms) -> [(t0, t1)] in seconds; blank -> None."""
        text = self.lineseg_var.get().strip()
        if not text:
            return None
        out = []
        for tok in re.split(r"[,;]+", text):
            tok = tok.strip()
            if not tok:
                continue
            a, sep, b = tok.partition(":")
            try:
                out.append((float(a) * 1e-3, float(b) * 1e-3))
            except ValueError:
                raise RuntimeError(f"fit window {tok!r}: use from:to in ms, "
                                   f"e.g. -12:0, 10:16.5")
            if not sep or out[-1][1] <= out[-1][0]:
                raise RuntimeError(f"fit window {tok!r}: needs from:to with "
                                   f"to > from")
        return out or None

    def _fit_line_shots(self, t, stack, f_line, harmonics, segments):
        """Fit the ripple to the shot mean; the per-shot phase of the
        fundamental says whether the trigger is line-synchronous (coherence
        near 1) or free-running (near 1/sqrt(shots))."""
        t = np.asarray(t, float)
        stack = np.atleast_2d(np.asarray(stack, float))
        segs = (None if segments is None else
                [(t >= a) & (t <= b) for a, b in segments])
        if segs is not None and not any(m.any() for m in segs):
            raise RuntimeError("no samples inside the fit windows")
        fit = mains.fit_line(t, stack.mean(axis=0), f_line, harmonics, segs)
        coh = None
        if len(stack) > 1:
            ph = [np.deg2rad(mains.fit_line(t, row, f_line, 1, segs)
                             ["phase_deg"][0]) for row in stack]
            coh = float(abs(np.mean(np.exp(1j * np.asarray(ph)))))
        span = float(sum((min(b, t[-1]) - max(a, t[0])) for a, b in segments)
                     if segments else t[-1] - t[0])
        meta = dict(measured=datetime.datetime.now().isoformat(
                        timespec="seconds"),
                    n_shots=int(len(stack)), coherence=coh,
                    span_ms=round(span * 1e3, 3),
                    segments_ms=([[a * 1e3, b * 1e3] for a, b in segments]
                                 if segments else None))
        return fit, meta

    def _apply_line(self, s, on):
        s.loop.line = (mains.line_wave(s.t, s.line_fit["fit"])
                       if on and s.line_fit is not None else None)

    def _set_line_fit(self, fit, meta):
        """Main thread: adopt a fit for the panel and the loaded session."""
        self._line_fit = {"fit": fit, "meta": meta}
        s = self.session
        sc = s.loop.channel.mon_scale if s else 1.0
        unit = f"V at the {s.loop.channel.out_name}" if s else "V"
        self.log(f"line ripple ({meta.get('source', '?')}): "
                 + mains.describe(fit, scale=sc, unit=unit))
        coh = meta.get("coherence")
        if coh is not None:
            synced = coh >= 0.7
            self.log(f"  phase coherence over {meta['n_shots']} shots "
                     f"{coh:.2f} -- "
                     + ("line-synchronous trigger: the ripple repeats in "
                        "every shot and the average keeps it"
                        if synced else
                        "the trigger is NOT line-synchronous: the ripple "
                        "averages away by itself; leave subtraction OFF"))
            if not synced and self.line_on_var.get():
                self.line_on_var.set(False)
                self.log("  subtraction switched OFF")
        if fit.get("cond", 0) > 1e3:
            self.log(f"  NOTE: the fit window is short for {fit['f_line']:g} "
                     f"Hz (condition {fit['cond']:.0f}) -- the ripple and a "
                     f"drift trade off; use a window of several periods")
        if s is not None:
            s.line_fit = self._line_fit
            self._apply_line(s, self.line_on_var.get())
            save_session(s)
        if not self.line_on_var.get():
            self.log("  tick 'subtract the measured ripple' to use it")
        self._refresh_line_status()
        self._refresh_summary()

    def _on_line_toggle(self):
        s = self.session
        want = bool(self.line_on_var.get())
        lf = s.line_fit if (s is not None and s.line_fit) else self._line_fit
        if want and lf is None:
            self.line_on_var.set(False)
            return messagebox.showinfo(
                "Line ripple", "Nothing measured yet: Measure (drive off), "
                               "or Fit from captures taken on the same "
                               "trigger with the drive off.")
        if want:
            coh = lf["meta"].get("coherence")
            if coh is not None and coh < 0.7 and not messagebox.askyesno(
                    "Line ripple",
                    f"The ripple's phase moved from shot to shot (coherence "
                    f"{coh:.2f}): the trigger was not line-synchronous when "
                    f"it was measured, so the shot average removes the "
                    f"ripple by itself, and subtracting a fixed phase ADDS "
                    f"ripple-shaped error.\n\nSubtract it anyway?"):
                self.line_on_var.set(False)
                return
        if s is not None:
            if s.line_fit is None:
                s.line_fit = lf
            self._apply_line(s, want)
            save_session(s)
            self.log("line ripple " + ("subtracted from every measurement "
                                       "before the update" if want else
                                       "no longer subtracted"))
        self._refresh_line_status()
        self._refresh_summary()

    def _refresh_line_status(self):
        if not self.line_status.winfo_exists():
            return
        s = self.session
        lf = s.line_fit if (s is not None and s.line_fit) else self._line_fit
        if lf is None:
            return self.line_status.configure(text="nothing measured")
        sc = s.loop.channel.mon_scale if s else 1.0
        m = lf.get("meta", {})
        coh = m.get("coherence")
        on = s is not None and s.loop.line is not None
        txt = (("SUBTRACTED: " if on else "measured, not used: ")
               + mains.describe(lf["fit"], scale=sc, unit="V")
               + f"\n{m.get('source', '')}, {m.get('n_shots', '?')} shots, "
                 f"{m.get('span_ms', '?')} ms"
               + (f", coherence {coh:.2f}" if coh is not None else "")
               + f", {m.get('measured', '')}")
        self.line_status.configure(text=txt)

    def do_line_clear(self):
        self._line_fit = None
        s = self.session
        if s is not None:
            s.line_fit = None
            s.loop.line = None
            save_session(s)
        self.line_on_var.set(False)
        self.log("line ripple cleared")
        self._refresh_line_status()
        self._refresh_summary()

    def do_line_measure(self):
        """Bench: the monitor with the drive OFF, on the run's own trigger,
        over a window of several line periods, ripple fitted."""
        if self.session is None:
            return messagebox.showerror("Line ripple",
                                        "load or init a session first -- the "
                                        "ripple is evaluated on its record")
        try:
            f = self._floats(awg_ch=self.awgch_var, scope_ch=self.scopech_var,
                             repeats=self.repeats_var, wait=self.wait_var,
                             f_line=self.linef_var, harm=self.lineh_var,
                             win=self.linewin_var)
        except RuntimeError as e:
            return messagebox.showerror("Line ripple", str(e))
        if not self._wiring_ok(f["awg_ch"], f["scope_ch"], "Line ripple"):
            return
        if f["win"] * 1e-3 * f["f_line"] < 2:
            return messagebox.showerror(
                "Line ripple", f"a {f['win']:g} ms window holds "
                               f"{f['win']*1e-3*f['f_line']:.1f} periods of "
                               f"{f['f_line']:g} Hz; use at least 2 (100 ms "
                               f"is 6 at 60 Hz)")
        reps = max(8, min(int(f["repeats"]), 32))
        t_off = self.session.t_off
        self.run_worker(lambda: self._line_measure_work(
            int(f["awg_ch"]), int(f["scope_ch"]), reps, f["wait"],
            f["f_line"], int(f["harm"]), f["win"] * 1e-3, t_off),
            "measuring line ripple, drive off...")

    def _line_measure_work(self, awg_ch, scope_ch, repeats, wait_s, f_line,
                           harmonics, win_s, t_off):
        awg, scope = self._connect_pair()
        tb, shots, t0 = {}, [], None
        try:
            if awg.is_on(awg_ch):
                if not self.ask_user(
                        "Line ripple",
                        f"AWG CH{awg_ch} output is ON. The reference is the "
                        f"monitor with the drive OFF -- switch the output off "
                        f"now? (It stays off afterwards, as after any run.)"):
                    print("line ripple NOT measured: the output was left on")
                    return
                awg.set_output(awg_ch, False)
                print(f"CH{awg_ch} output OFF")
            for k in (":TIMebase:REFerence", ":TIMebase:RANGe", ":TIMebase:POSition"):
                tb[k] = scope.get(k)
            scope.put(":TIMebase:REFerence", "CENTer")
            scope.put(":TIMebase:RANGe", f"{win_s:.6g}")
            scope.put(":TIMebase:POSition", f"{0.4 * win_s:.6g}")
            print(f"scope: {win_s*1e3:g} ms window from {-0.1*win_s*1e3:g} ms "
                  f"(restored afterwards), CH{scope_ch}, {repeats} shots")
            pts = scope_points_for(20000)
            plan, restore = self._dither_plan(scope, (scope_ch,), repeats, 3)
            try:
                self.msgs.put(("progress", 0, repeats))
                for i in range(repeats):
                    if self.stop_evt.is_set():
                        raise RuntimeError("stopped; nothing kept")
                    self._dither_step(scope, plan, i, repeats, 3)
                    got = scope.single(wait_s=wait_s)
                    if got is not True:
                        raise RuntimeError(
                            f"no trigger within {wait_s:g} s with the drive "
                            f"off. If the scope triggers on the drive itself "
                            f"there is nothing to trigger on: capture the "
                            f"monitor on the experiment's trigger (Scope Grab) "
                            f"and use 'Fit from captures'.")
                    ts, vs = self._read_waveform(scope, scope_ch, pts)
                    scope.run()
                    ts, vs = np.asarray(ts, float), np.asarray(vs, float)
                    if t0 is None:
                        t0 = ts
                    shots.append(np.interp(t0, ts, vs))
                    self.msgs.put(("progress", i + 1, repeats))
            finally:
                self._dither_restore(scope, restore)
        finally:
            for k, v in tb.items():
                try:
                    scope.put(k, v)
                except Exception as e:
                    print(f"could not restore {k} ({e})")
            awg.close()
            scope.close()
            print("instruments closed")
        fit, meta = self._fit_line_shots(t0 - t_off, np.asarray(shots),
                                         f_line, harmonics, None)
        meta["source"] = f"bench, drive off, CH{scope_ch}"
        self.msgs.put(("call", lambda: self._set_line_fit(fit, meta)))

    def do_line_from_files(self, files=None):
        """Offline: Scope Grab captures (CSV or NPZ) of the monitor with the
        drive off -- or with undriven stretches named in 'fit only in ms' --
        taken on the same trigger as the ILC runs."""
        s = self.session
        if s is None:
            return messagebox.showerror("Line ripple",
                                        "load or init a session first")
        try:
            f = self._floats(f_line=self.linef_var, harm=self.lineh_var)
            segs = self._line_segments()
        except RuntimeError as e:
            return messagebox.showerror("Line ripple", str(e))
        if files is None:
            files = filedialog.askopenfilenames(
                title="Captures of the monitor, drive off (same trigger)",
                filetypes=[("Scope Grab captures", "*.csv *.npz"),
                           ("All files", "*.*")])
        if not files:
            return
        mon = self.moncol_var.get()
        try:
            t0, stack = None, []
            for p in files:
                tr = scopeio.load(p)
                tt = np.asarray(tr.t, float) - s.t_off
                y = np.asarray(tr[mon], float)
                if t0 is None:
                    t0 = tt
                stack.append(np.interp(t0, tt, y))
            fit, meta = self._fit_line_shots(t0, np.asarray(stack), f["f_line"],
                                             int(f["harm"]), segs)
        except (OSError, KeyError, ValueError, RuntimeError) as e:
            return messagebox.showerror("Line ripple", str(e))
        meta["source"] = (f"{len(files)} capture(s), {mon}, "
                          f"{os.path.basename(files[0])}"
                          + (" ..." if len(files) > 1 else ""))
        self._set_line_fit(fit, meta)

    # ---------------------------------------------- learned target corrections
    def _build_corr_tab(self, parent):
        """Corrections: the outer loop. A correction file (eomilc.corrections)
        says how far the TARGET should move so that something the monitor
        cannot see -- the light's polarization, for the EOMs -- comes out
        right. The loop then learns the drive. The base target is kept."""
        ttk.Label(parent, text=(
            "Moves the TARGET by an error measured elsewhere (the polarimeter's "
            "light minus monitors); the loop then learns the drive. Revert "
            "restores the base target."),
            foreground="#555555", wraplength=440, justify="left").pack(
            fill="x", pady=(0, 3))
        lf = ttk.LabelFrame(parent, text="Correction files", padding=3)
        lf.pack(fill="x", pady=(0, 2))
        cols = ("on", "file", "slot", "gain", "kept", "res", "status")
        heads = ("on", "file", "slot", "gain", "kept pk (V)", "resolved",
                 "status")
        widths = (28, 150, 62, 40, 70, 58, 70)
        tv = ttk.Treeview(lf, columns=cols, show="headings", height=4,
                          selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            tv.heading(c, text=h)
            tv.column(c, width=w, stretch=(c == "file"),
                      anchor="w" if c in ("file", "slot", "status") else "e")
        tv.grid(row=0, column=0, sticky="ew")
        self.corr_tv = tv
        r = ttk.Frame(lf); r.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        for text, cmd in (("Add file...", self.do_corr_add),
                          ("Remove", self.do_corr_remove),
                          ("On / off", self.do_corr_toggle)):
            b = ttk.Button(r, text=text, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self._actions.append(b)
        ttk.Label(r, text="gain").pack(side="left", padx=(6, 2))
        self.corrgain_var = tk.StringVar(value="1")
        e = ttk.Entry(r, textvariable=self.corrgain_var, width=5)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.do_corr_gain())
        b = ttk.Button(r, text="Set", width=4, command=self.do_corr_gain)
        b.pack(side="left", padx=(2, 0))
        self._actions.append(b)
        lf.columnconfigure(0, weight=1)

        gf = ttk.LabelFrame(parent, text="Limits (nothing past these is "
                                         "applied)", padding=3)
        gf.pack(fill="x", pady=(0, 2))
        cfg = self.cfg
        self.corrband_var = tk.StringVar(value=cfg.get("corr_band", "2000"))
        self.corrk_var = tk.StringVar(value=cfg.get("corr_k", "3"))
        self.corrfloor_var = tk.StringVar(value=cfg.get("corr_floor", "0.1"))
        self.corrcap_var = tk.StringVar(value=cfg.get("corr_cap", "300"))
        self.corrfade_var = tk.StringVar(value=cfg.get("corr_fade", "200"))
        self.corract_var = tk.BooleanVar(value=bool(cfg.get("corr_active",
                                                            True)))
        r1 = ttk.Frame(gf); r1.grid(row=0, column=0, sticky="ew")
        for lab, var, w in (("band Hz", self.corrband_var, 6),
                            ("resolved at k sigma", self.corrk_var, 4),
                            ("floor V", self.corrfloor_var, 5)):
            ttk.Label(r1, text=lab).pack(side="left", padx=(0, 2))
            ttk.Entry(r1, textvariable=var, width=w).pack(side="left",
                                                          padx=(0, 8))
        r2 = ttk.Frame(gf); r2.grid(row=1, column=0, sticky="ew", pady=1)
        for lab, var, w in (("cap V", self.corrcap_var, 6),
                            ("end fade us", self.corrfade_var, 5)):
            ttk.Label(r2, text=lab).pack(side="left", padx=(0, 2))
            ttk.Entry(r2, textvariable=var, width=w).pack(side="left",
                                                          padx=(0, 8))
        ttk.Checkbutton(r2, text="only where the target is active",
                        variable=self.corract_var).pack(side="left")
        ttk.Label(gf, text=(
            "Volts at the output, as in a target CSV; the Trek limits, the "
            "AWG rail and full scale are always checked (WORKFLOW_GUI.md 10)."),
            foreground="#666666", wraplength=440, justify="left").grid(
            row=2, column=0, sticky="w")
        gf.columnconfigure(0, weight=1)

        r3 = ttk.Frame(parent); r3.pack(fill="x", pady=(2, 2))
        for text, cmd in (("Preview", self.do_corr_preview),
                          ("Apply to target", self.do_corr_apply),
                          ("Revert to base", self.do_corr_revert)):
            b = ttk.Button(r3, text=text, command=cmd)
            b.pack(side="left", fill="x", expand=True, padx=(0, 4))
            self._actions.append(b)
        self.corr_status = ttk.Label(parent, text="no session loaded",
                                     foreground="#444444", wraplength=440,
                                     justify="left")
        self.corr_status.pack(fill="x")
        self._corr_pending = []          # the box: applied layers + new files
        self._corr_cache = {}            # path -> (mtime, Correction)

    def _load_corr(self, path):
        mt = os.path.getmtime(path)
        hit = self._corr_cache.get(path)
        if hit and hit[0] == mt:
            return hit[1]
        c = corrmod.read_correction(path)
        self._corr_cache[path] = (mt, c)
        return c

    def _refresh_corr_list(self):
        tv = self.corr_tv
        sel = tv.selection()
        tv.delete(*tv.get_children())
        for i, L in enumerate(self._corr_pending):
            tv.insert("", "end", iid=str(i), values=(
                "yes" if L.get("on", True) else "no",
                os.path.basename(L["path"]), L.get("slot", ""),
                f"{L.get('gain', 1.0):g}",
                f"{L['kept_peak']:.1f}" if "kept_peak" in L else "",
                f"{100*L['resolved']:.0f}%" if "resolved" in L else "",
                L.get("status", "")))
        if sel and sel[0] in tv.get_children():
            tv.selection_set(sel)
        self._refresh_corr_status()

    def _refresh_corr_status(self):
        s = self.session
        if s is None:
            return self.corr_status.configure(text="no session loaded")
        sc = s.loop.channel.mon_scale
        if s.base_target is None or s.corr_total is None:
            txt = "target = base target (no corrections applied)"
        else:
            on = [L for L in s.corr_layers if L.get("on", True)]
            d = s.corr_total * sc
            txt = (f"target = base + {len(on)} correction(s): "
                   f"{np.max(np.abs(d)):.1f} V peak, "
                   f"{np.sqrt(np.mean(d * d)):.1f} V rms change "
                   f"(applied {on[0].get('applied', '?') if on else '?'})")
        if any(L.get("status") in ("added", "changed")
               for L in self._corr_pending):
            txt += "\nchanged since the last Apply: Preview, then Apply"
        self.corr_status.configure(text=txt)

    def _corr_selected(self):
        sel = self.corr_tv.selection()
        return int(sel[0]) if sel else None

    def do_corr_add(self, paths=None):
        if self.session is None:
            return messagebox.showerror("Corrections",
                                        "load or init a session first")
        if paths is None:
            paths = filedialog.askopenfilenames(
                title="Target correction files",
                filetypes=[("Target corrections", "*.csv"),
                           ("All files", "*.*")])
        for p in paths:
            try:
                c = self._load_corr(p)
            except (OSError, ValueError) as e:
                messagebox.showerror("Corrections", str(e))
                continue
            p = os.path.abspath(p)
            same = [i for i, L in enumerate(self._corr_pending)
                    if L.get("slot") and L["slot"] == c.slot]
            layer = dict(path=p, slot=c.slot, gain=1.0, on=True,
                         status="added",
                         quantity=str(c.meta.get("quantity", "")))
            if same:
                old = self._corr_pending[same[0]]
                if not messagebox.askyesno(
                        "Corrections",
                        f"{os.path.basename(p)} is for the same slot "
                        f"('{c.slot}') as {os.path.basename(old['path'])}.\n\n"
                        f"Replace it? (A new measurement of the same thing "
                        f"supersedes the old one; stacking both would apply "
                        f"it twice.)"):
                    continue
                layer["gain"] = old.get("gain", 1.0)
                self._corr_pending[same[0]] = layer
            else:
                self._corr_pending.append(layer)
            self.log(f"correction added: {os.path.basename(p)} -- "
                     f"{c.meta.get('quantity', 'quantity not stated')}; "
                     f"source {c.meta.get('source', '?')}")
            for k in ("method", "line", "notes"):
                if c.meta.get(k):
                    self.log(f"  {k}: {c.meta[k]}")
        self._refresh_corr_list()

    def do_corr_remove(self):
        i = self._corr_selected()
        if i is None:
            return
        L = self._corr_pending.pop(i)
        self.log(f"correction removed from the box: "
                 f"{os.path.basename(L['path'])} (Apply to take it out of "
                 f"the target)")
        self._refresh_corr_list()

    def do_corr_toggle(self):
        i = self._corr_selected()
        if i is None:
            return
        L = self._corr_pending[i]
        L["on"] = not L.get("on", True)
        L["status"] = "changed"
        self._refresh_corr_list()

    def do_corr_gain(self):
        i = self._corr_selected()
        if i is None:
            return messagebox.showinfo("Corrections", "select a file first")
        try:
            g = float(self.corrgain_var.get())
        except ValueError:
            return messagebox.showerror("Corrections", "gain must be a number")
        if not 0 <= g <= 1.5:
            return messagebox.showerror(
                "Corrections", "gain 0..1.5: 1 applies the measured "
                               "correction, less learns part of it")
        self._corr_pending[i]["gain"] = g
        self._corr_pending[i]["status"] = "changed"
        self._refresh_corr_list()

    def _corr_settings(self):
        f = self._floats(band=self.corrband_var, k=self.corrk_var,
                         floor=self.corrfloor_var, cap=self.corrcap_var,
                         fade=self.corrfade_var)
        if f["band"] <= 0 or f["k"] < 0 or f["floor"] < 0 or f["cap"] <= 0:
            raise RuntimeError("band and cap must be > 0; k and floor >= 0")
        return corrmod.GateSettings(band_hz=f["band"], k_sigma=f["k"],
                                    floor_out=f["floor"], cap_out=f["cap"],
                                    fade_s=f["fade"] * 1e-6,
                                    active_only=bool(self.corract_var.get()))

    def _corr_compute(self):
        """Gate every 'on' layer against the BASE target and check the total.
        Returns (total_out, results, notes, base_out); results is
        [(layer, kept_out, info)], notes [(level, text)]."""
        s = self.session
        ch = s.loop.channel
        sc = ch.mon_scale
        st = self._corr_settings()
        base = s.base_target if s.base_target is not None else s.loop.target
        base_out = np.asarray(base, float) * sc
        total = np.zeros_like(base_out)
        results, notes, bands = [], [], []
        for L in self._corr_pending:
            if not L.get("on", True):
                continue
            c = self._load_corr(L["path"])
            tag = os.path.basename(L["path"])
            mch = c.meta.get("channel")
            if mch and str(mch) != s.channel:
                notes.append(("fail", f"{tag}: written for {mch}, this session "
                                      f"is {s.channel}"))
                continue
            for lev, txt in corrmod.check_target(c.meta, base_out, s.loop.dt):
                notes.append((lev, f"{tag}: {txt}"))
            kept, info = corrmod.gate(c, s.t, base_out, st, L.get("gain", 1.0))
            for lev, txt in info["notes"]:
                notes.append((lev, f"{tag}: {txt}"))
            L.update(kept_peak=info["kept_peak"], kept_rms=info["kept_rms"],
                     resolved=info["resolved_frac"], band=info["band_hz"])
            bands.append(info["band_hz"])
            results.append((L, kept, info))
            total = total + kept
        if results:
            learn = (float(s.loop.frf.f_use) if s.loop.frf is not None
                     else float(s.loop.f_cut))
            notes += corrmod.check_feasible(
                total, base_out, s.u, s.loop.dt, ch, s.loop.limits,
                s.loop.plant.gain, s.full_scale, learn, max(bands), st.cap_out)
        return total, results, notes, base_out

    def do_corr_preview(self):
        if self.session is None:
            return messagebox.showerror("Corrections",
                                        "load or init a session first")
        try:
            total, results, notes, base_out = self._corr_compute()
        except (RuntimeError, OSError, ValueError) as e:
            return messagebox.showerror("Corrections", str(e))
        self._report_corr(total, results, notes, "preview")
        self._plot_corrections(total, results, base_out)
        self._show_fig(self.fig_corr)
        self._refresh_corr_list()
        return notes

    def _report_corr(self, total, results, notes, what):
        if not results:
            self.log(f"corrections {what}: nothing switched on")
            return
        for L, kept, info in results:
            self.log(f"  {os.path.basename(L['path'])}: x{info['gain']:g}, band "
                     f"{info['band_hz']:g} Hz, raw {info['raw_peak']:.1f} V pk -> "
                     f"applied {info['kept_peak']:.1f} V pk / {info['kept_rms']:.1f} "
                     f"V rms; resolved over {100*info['resolved_frac']:.0f}% "
                     f"of the active record (median filtered sigma "
                     f"{info['sigma_lp_median']:.2g} V)")
        self.log(f"corrections {what}: total {np.max(np.abs(total)):.1f} V "
                 f"peak at the {self.session.loop.channel.out_name}")
        for lev, txt in notes:
            self.log(f"  {lev.upper()}: {txt}")

    def do_corr_apply(self):
        s = self.session
        if s is None:
            return messagebox.showerror("Corrections",
                                        "load or init a session first")
        try:
            total, results, notes, base_out = self._corr_compute()
        except (RuntimeError, OSError, ValueError) as e:
            return messagebox.showerror("Corrections", str(e))
        self._report_corr(total, results, notes, "apply")
        self._plot_corrections(total, results, base_out)
        if not results:
            return messagebox.showinfo(
                "Corrections", "Nothing is switched on. To take every "
                               "correction out, use Revert to base.")
        fails = [t for lev, t in notes if lev == "fail"]
        if fails:
            for L, _, _ in results:
                L["status"] = "refused"
            self._refresh_corr_list()
            return messagebox.showerror(
                "Corrections -- refused",
                "Not applied:\n\n" + "\n\n".join(fails))
        warns = [t for lev, t in notes if lev == "warn"]
        sc = s.loop.channel.mon_scale
        u_pk = float(np.max(np.abs(s.u + total / sc / s.loop.plant.gain)))
        msg = (f"Move the target by {np.max(np.abs(total)):.1f} V peak "
               f"({np.sqrt(np.mean(total**2)):.1f} V rms over the record) at "
               f"the {s.loop.channel.out_name}?\n\nEstimated drive peak "
               f"{u_pk:.3f} V of the {s.full_scale:g} V full scale. The next "
               f"iteration measures against the new target, so the error "
               f"jumps once and the loop then learns it.")
        if warns:
            msg += "\n\nWarnings:\n- " + "\n- ".join(warns)
        if not messagebox.askyesno("Apply corrections", msg):
            return
        if s.base_target is None:
            s.base_target = np.asarray(s.loop.target, float).copy()
        s.corr_total = total / sc
        s.loop.target = s.base_target + s.corr_total
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        for L, _, _ in results:
            L["status"], L["applied"] = "applied", stamp
        for L in self._corr_pending:
            if not L.get("on", True):
                L["status"] = "off"
        s.corr_layers = [dict(L) for L in self._corr_pending]
        if s.loop.history and isinstance(s.loop.history[-1], dict):
            s.loop.history[-1]["target_changed"] = stamp
        save_session(s)
        self.log(f"target corrected at iteration {s.iteration} ({stamp}); base "
                 f"target kept in the state. The next measurement is against "
                 f"the new target.")
        self._refresh_corr_list()
        self._refresh_summary()
        self._redraw_iterations()
        self._plot_corrections(total, results, base_out)

    def do_corr_revert(self):
        s = self.session
        if s is None or s.base_target is None:
            return messagebox.showinfo("Corrections",
                                       "the target is already the base target")
        if not messagebox.askyesno(
                "Revert", "Put the target back to the base target? The files "
                          "stay in the box (switched off)."):
            return
        s.loop.target = np.asarray(s.base_target, float).copy()
        s.base_target, s.corr_total = None, None
        for L in self._corr_pending:
            L["on"], L["status"] = False, "off"
        s.corr_layers = [dict(L) for L in self._corr_pending]
        save_session(s)
        self.log("target reverted to the base target")
        self._refresh_corr_list()
        self._refresh_summary()
        self._redraw_iterations()

    def _replot_corr_quiet(self):
        """The Corrections plot for a freshly loaded session: the gate re-run
        on the box's files (nothing logged, nothing applied), the stored
        total underneath. A file that has gone missing just leaves the
        middle panel empty."""
        s = self.session
        results, base_out = (), None
        if s is not None and self._corr_pending:
            try:
                _, results, _, base_out = self._corr_compute()
            except Exception:
                results = ()
        total = (s.corr_total * s.loop.channel.mon_scale
                 if s is not None and s.corr_total is not None else None)
        self._plot_corrections(total, results, base_out)

    def _plot_corrections(self, total=None, results=(), base_out=None):
        s = self.session
        ax0, ax1, ax2 = self.ax_corr
        for ax in self.fig_corr.axes[3:]:       # the last draw's twin axis
            ax.remove()
        for ax in self.ax_corr:
            ax.clear()
        if s is None:
            self._draw(self.fig_corr)
            return
        sc = s.loop.channel.mon_scale
        tms = s.t * 1e3
        out = s.loop.channel.out_name
        if base_out is None:
            base = s.base_target if s.base_target is not None else s.loop.target
            base_out = np.asarray(base, float) * sc
        if total is None and s.corr_total is not None:
            total = s.corr_total * sc
        ax0.plot(tms, base_out, color=TARGET_COLOUR, lw=1.0, label="base target")
        if total is not None:
            ax0.plot(tms, base_out + total, color="#d62728", lw=0.9, ls="--",
                     label="base + corrections")
        ax0.set_ylabel(f"{out} (V)")
        ax0.set_title(f"{s.channel} '{s.stem}' target and learned corrections")
        ax0.legend(fontsize=7, loc="best")
        for i, (L, kept, info) in enumerate(results):
            col = CMP_COLOURS[i % len(CMP_COLOURS)]
            name = os.path.basename(L["path"])
            ax1.fill_between(tms, -info["thr"], info["thr"], color=col,
                             alpha=0.15, lw=0)
            ax1.plot(tms, info["lp"], color=col, lw=0.6, alpha=0.6,
                     label=f"{name}: in band, before the gate")
            ax1.plot(tms, kept, color=col, lw=1.1, label=f"{name}: applied")
        ax1.set_ylabel(f"change ({out} V)")
        if results:
            ax1.legend(fontsize=7, loc="best")
            self._plot_note(ax1, "shaded: k sigma (filtered) and floor -- held "
                                 "at zero inside")
        if total is not None:
            ax2.plot(tms, total, color="k", lw=1.0, label="total target change")
            ax2b = ax2.twinx()
            ax2b.plot(tms, total / sc / s.loop.plant.gain * 1e3, color="#1f77b4",
                      lw=0.6, alpha=0.6)
            ax2b.set_ylabel("estimated drive change (mV)", color="#1f77b4")
            ax2.legend(fontsize=7, loc="upper left")
        ax2.set_ylabel(f"{out} V")
        ax2.set_xlabel("time (ms)")
        for ax in self.ax_corr:
            ax.grid(True, alpha=0.3)
        self._draw(self.fig_corr)

    def _path_row(self, parent, row, label, var, browse):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        e = ttk.Entry(parent, textvariable=var)
        e.grid(row=row, column=1, sticky="ew", padx=2)
        self._show_tail(e, var)
        ttk.Button(parent, text="...", width=3, command=browse).grid(
            row=row, column=2)
        return e

    def _show_tail(self, entry, var):
        """An entry that scrolls to the END of what it holds.

        Tk only scrolls an Entry to keep the INSERTION CURSOR visible, and a
        programmatic set() leaves that at character 0 -- so every path the
        panel fills in for you (Browse, Load state, the restored config, the
        channel's FRF) showed 'C:\\Users\\mzd416\\Desktop\\Pyth' and hid the
        file name, which is the half that says which one it is.

        Only moved while the box is not being typed in: an entry with focus
        keeps Tk's own cursor-following, so editing the middle of a path
        still behaves."""
        def tail(*_):
            try:
                if entry.winfo_exists() and entry.focus_get() is not entry:
                    entry.xview_moveto(1.0)
            except tk.TclError:
                pass
        var.trace_add("write", lambda *_: entry.after_idle(tail))
        # leaving the box is the other moment the tail matters: the cursor
        # was left wherever the edit ended, which is rarely the end
        entry.bind("<FocusOut>", lambda ev: entry.after_idle(tail), add="+")
        # and on every resize -- which is also how the FIRST value gets its
        # tail. A value set before the panel is laid out (the restored config,
        # a Load fired from the startup reload) reaches an entry that is still
        # zero-width, and scrolling a box with no width does nothing.
        entry.bind("<Configure>", lambda ev: tail(), add="+")
        return entry

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
            filetypes=(("Scope captures", "*.csv *.npz"),
                       ("All files", "*.*")))
        if p:
            g = re.sub(r"_\d+\.(csv|npz)$", r"*.\1", p, flags=re.I)
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
        self._actions = [b for b in self._actions if b.winfo_exists()]
        for b in self._actions:
            b.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")
        self.opt_stop_btn.configure(state="normal" if busy else "disabled")
        if self._batch_win is not None:
            self._batch_win.set_busy(busy)
        # Clear has a second condition -- there has to be something loaded --
        # so it re-derives its own state rather than taking the blanket one
        self._refresh_compare_status()
        if not busy:
            self.progress.configure(value=0)
            self.status.configure(text=self._hint())
            # the Optical steps grey again by their own prerequisites, and a
            # verification's verdict takes the status line once
            self._refresh_optical()
            if self._opt.get("announce"):
                self.status.configure(text=self._opt.pop("announce"))

    def run_worker(self, fn, status):
        if self.busy:
            return
        self.set_busy(True)
        self.stop_evt.clear()
        self.status.configure(text=status)

        def work():
            out = _QueueWriter(self.msgs)
            try:
                with contextlib.redirect_stdout(out):
                    try:
                        fn()
                    finally:
                        # INSIDE the redirect: flushing after the context
                        # manager exits flushes the restored real stdout, so
                        # a helper's last line without a trailing newline was
                        # sitting in the writer's buffer and thrown away --
                        # including on the exception path.
                        out.flush()
            except SystemExit as e:      # ilc_bench helpers refuse via sys.exit
                self.msgs.put(("log", f"ABORTED: {e}"))
            except Exception:
                self.msgs.put(("log", traceback.format_exc()))
            finally:
                self.msgs.put(("busy", False))
        threading.Thread(target=work, daemon=True).start()

    def ask_user(self, title, msg):
        """A yes/no question from a WORKER thread: marshalled to the main
        thread (Tk dialogs, like Tk variables, are main-thread-only) while
        the worker blocks on the answer.

        Answers NO rather than blocking forever if the dialog itself raises
        or the panel is closing. An unbounded wait here parks the worker
        holding both VISA sessions, and its cleanup -- output off, close --
        then never runs.
        """
        evt = threading.Event()
        result = {}

        def ask():
            try:
                result["yes"] = messagebox.askyesno(title, msg)
            finally:
                evt.set()       # a raising dialog must not park the worker
        self.msgs.put(("call", ask))
        while not evt.wait(0.2):
            if self._closing:   # pump has stopped; no answer is coming
                print(f"panel closing -- taking 'no' for: {title}")
                return False
        return bool(result.get("yes"))

    def _notches(self, c=None):
        try:
            return ilc.parse_notches((c or self._col1).notch_var.get())
        except ValueError as e:
            raise RuntimeError(str(e))

    def _floats(self, **pairs):
        out = {}
        for k, v in pairs.items():
            try:
                out[k] = float(v.get())
            except ValueError:
                raise RuntimeError(f"{k} is not a number: {v.get()!r}")
        return out

    # -------------------------------------------------------- session setup
    # -- per-channel, per-model memory of the panel's numbers --------------
    #
    # Switching channel means switching system: the previous channel's
    # numbers do not follow -- they are the previous chain's, and inheriting
    # them silently is exactly the prior-knowledge leak the GEN channel
    # exists to prevent. But they are not thrown away either: each channel
    # keeps its own first-shot gain, seed drive and browsed FRF, and each
    # (channel, model) pair keeps its own parameter boxes, so flipping
    # EO1 -> EO2 -> EO1, or one pole -> resonant -> one pole, brings back
    # what was there. Remembered across launches with the rest of the config.

    _MEM_KEYS = ("gain", "tau", "fn", "zeta")

    def _remember_fields(self):
        """Store what the panel shows under the channel and model it belongs
        to (the ones last recalled -- not the boxes' current selection,
        which may already have moved on)."""
        ch = getattr(self, "_mem_channel", None)
        if ch is None:
            return
        m = self._field_mem.setdefault(ch, {})
        m["shot_gain"] = self.shotgain_var.get().strip()
        m["seed_drive"] = self.seed_var.get().strip()
        m["frf"] = self.frf_var.get().strip()
        m["notch"] = self.notch_var.get().strip()
        m.setdefault("models", {})[self._mem_model] = {
            k: self._param_vars[k].get().strip() for k in self._MEM_KEYS}

    def _recall_params(self, ch, key, blank_if_new, c=None):
        """The parameter boxes for (ch, key). A pair never seen before keeps
        the current numbers on a MODEL switch (a starting point, as before)
        and blanks them on a CHANNEL switch (the prior-leak guard). `c`: the
        Model column whose boxes are filled (default column 1)."""
        pv = (c or self._col1).param_vars
        got = (self._field_mem.get(ch) or {}).get("models", {}).get(key)
        if got is None:
            if blank_if_new:
                for v in pv.values():
                    v.set("")
            return False
        for k in self._MEM_KEYS:
            pv[k].set(got.get(k, ""))
        return True

    def _on_model_change(self):
        key = self._model_key()
        if key != self._mem_model:
            self._remember_fields()
            had = self._recall_params(self._mem_channel, key, blank_if_new=False)
            self._mem_model = key
            if had:
                shown = ", ".join(f"{k} {self._param_vars[k].get()}"
                                  for k in PARAMS_FOR[key]
                                  if self._param_vars[k].get().strip())
                self.log(f"{KEY2LABEL[key]}: recalled {shown or 'blank boxes'}")
        self._update_model_fields()

    def _on_channel_change(self):
        ch = self.channel_var.get()
        if ch == self._mem_channel:
            return
        self._remember_fields()
        old = self._mem_channel
        for v in self._param_vars.values():
            v.set("")
        self.shotgain_var.set("")
        self.seed_var.set("")        # a drive is in ONE chain's volts
        self.notch_var.set("")       # and each Trek has its own line
        # The FRF file goes too. On the measured-FRF rung the FRF IS the
        # model, so a browsed one surviving a channel switch was the same
        # leak the parameter boxes above are cleared to prevent -- and the
        # worst case is GEN, whose whole point is that nothing measured on
        # the Trek chains applies until it is loaded on purpose. Blanking it
        # first lets _apply_channel_defaults re-point at the NEW channel's
        # default (nothing, for GEN), and the memory then brings back the
        # one this channel had browsed to, if it still exists.
        old_frf = self.frf_var.get().strip()
        self.frf_var.set("")
        self._apply_channel_defaults()
        m = self._field_mem.get(ch) or {}
        self.shotgain_var.set(m.get("shot_gain", ""))
        self.seed_var.set(m.get("seed_drive", ""))
        self.notch_var.set(m.get("notch", ""))
        if m.get("frf") and os.path.exists(m["frf"]):
            self.frf_var.set(m["frf"])
        self._recall_params(ch, self._mem_model, blank_if_new=True)
        self._mem_channel = ch
        back = [f"first-shot gain {self.shotgain_var.get()}"
                if self.shotgain_var.get().strip() else ""]
        back += [f"{k} {self._param_vars[k].get()}"
                 for k in PARAMS_FOR[self._mem_model]
                 if self._param_vars[k].get().strip()]
        if self.seed_var.get().strip():
            back.append(f"seed {os.path.basename(self.seed_var.get())}")
        back = [b for b in back if b]
        if m:
            self.log(f"channel {old} -> {ch}: recalled "
                     + (", ".join(back) if back else "blank boxes")
                     + f" ({KEY2LABEL[self._mem_model]})")
        else:
            self.log(f"channel {old} -> {ch}: nothing remembered for {ch} "
                     f"yet -- boxes blank; {old}'s numbers are kept for "
                     f"when you switch back")
        if old_frf and os.path.basename(old_frf) != os.path.basename(
                self.frf_var.get()):
            self.log(f"  dropped the FRF {os.path.basename(old_frf)}; it was "
                     f"measured on the other chain. Browse to this "
                     f"channel's own, or measure one.")
        if os.path.exists(self.target_var.get().strip()):
            self.do_preview_target(quiet=True)

    def _apply_channel_defaults(self, channel=None, wiring_only=False):
        """The wiring fields for `channel` (default: the Channel box), and
        the FRF autopoint -- unless wiring_only, which pair mode uses when the
        shown chain changes: the FRF field is column 1's, EO1's."""
        ch = channel or self.channel_var.get()
        d = CH_DEFAULTS[ch]
        mon_col, awg_ch, scope_ch = d["mon_col"], d["awg_ch"], d["scope_ch"]
        if ch in ("EO1", "EO2"):
            # the Trek chains follow the role map (Scope channels, Measure
            # tab); a map without this chain's monitor falls back to the
            # numbers above and the note on the Measure tab says why
            try:
                w = chanmap.wiring(self.cfg, "X" + ch[-1])
                awg_ch, scope_ch = w["awg_ch"], w["mon_ch"]
                mon_col = f"CH{scope_ch}"
            except ValueError:
                pass
        self.moncol_var.set(mon_col)
        self.awgch_var.set(str(awg_ch))
        self.scopech_var.set(str(scope_ch))
        self._refresh_run_state()
        if wiring_only:
            return
        # Point at this channel's wide-probe FRF unless the user browsed to
        # something that is not just the other channel's default.
        cur = os.path.basename(self.frf_var.get())
        if not cur or cur in {c["frf"] for c in CH_DEFAULTS.values() if c["frf"]}:
            p = os.path.join(RUN_DIR, d["frf"]) if d["frf"] else ""
            self.frf_var.set(p if p and os.path.exists(p) else "")

    # -------------------------------------------------------- model ladder
    def _model_key(self, c=None):
        return LABEL2KEY.get((c or self._col1).model_var.get(), "frf")

    def _update_model_fields(self, c=None):
        """Only the parameters the selected model actually has are shown
        (and the hidden ones are disabled as well, so nothing reads them).
        That includes the band knobs: f_cut belongs to the parametric rungs
        (the FRF path deliberately never pre-filters at it -- ilc.update),
        and the f_use/f_max taper belongs to the measured FRF. The FRF path
        row stays on every rung: the fit dialog reads it. `c`: the Model
        column (default column 1; pair mode's X2 column has its own)."""
        c = c or self._col1
        key = self._model_key(c)
        need = PARAMS_FOR[key]
        for k in ("gain", "tau", "fn", "zeta"):
            c.param_labels[k].pack_forget()
            c.param_entries[k].pack_forget()
        for k in ("gain", "tau", "fn", "zeta"):
            c.param_entries[k].configure(state="normal" if k in need else "disabled")
            if k in need:
                c.param_labels[k].pack(side="left", padx=(0, 2))
                c.param_entries[k].pack(side="left", padx=(0, 8))
        frf = key == "frf"
        c.fcut_entry.configure(state="disabled" if frf else "normal")
        for w in (c.fcut_label, c.fcut_entry, c.band_note):
            if w is not None:
                w.pack_forget()
        if not frf:
            c.fcut_label.pack(side="left", padx=(0, 2))
            c.fcut_entry.pack(side="left", padx=(0, 6))
            if c.band_note is not None:
                c.band_note.pack(side="left")
        for e in (c.fuse_entry, c.fmax_entry):
            e.configure(state="normal" if frf else "disabled")
        for w, _opts in c.taper_widgets:
            w.pack_forget()
        if frf:
            for w, opts in c.taper_widgets:
                w.pack(**opts)

    def _set_param_entries(self, p, c=None):
        """Plant -> panel entries (blank = the term is absent from p)."""
        pv = (c or self._col1).param_vars
        pv["gain"].set(f"{p.gain:.4f}")
        pv["tau"].set(f"{p.tau*1e6:.2f}" if p.tau > 0 else "")
        pv["fn"].set(f"{p.fn:.0f}" if p.fn > 0 else "")
        pv["zeta"].set(f"{p.zeta:.3f}" if p.zeta > 0 else "")

    def _entry_params(self, key, strict, c=None):
        """Panel entries for model `key` -> dict.  strict=False returns None
        on any blank entry (the caller says which and how to fill it);
        strict=True raises with a hint at the fit button."""
        pv = (c or self._col1).param_vars
        vals = {}
        for k in PARAMS_FOR[key]:
            txt = pv[k].get().strip()
            if not txt:
                if not strict:
                    return None
                raise RuntimeError(
                    f"'{k}' is blank for the {KEY2LABEL[key]} model -- type a "
                    f"value, or use 'Replace values with a fit...'")
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

    # ------------------------------------------------------------ fitting
    def do_fit_dialog(self, c=None):
        """'Replace values with a fit?' -- the two ways the selected rung's
        parameters can be filled from data, each explained, and Cancel.
        Both overwrite the parameter boxes; the loop picks the new values up
        at the next Init/Step/Bench (those read the boxes). `c`: the Model
        column (pair mode's X2 column has its own button)."""
        c = c or self._col1
        key = self._model_key(c)
        sess = self._col_session(c)
        if key == "frf":
            return messagebox.showinfo(
                "Replace values with a fit",
                "The measured FRF has no parameters to fit -- the measurement "
                "IS the identification. Use Measure FRF... to take one (or "
                "the CLI: tools/sysid_make.py + sysid_fit.py), set the taper "
                "inside its coherent band, and the loop divides by it "
                "directly. Fitting belongs to the parametric rungs.")
        names = ", ".join(PARAMS_FOR[key])
        fps = self._frf_paths(c)
        frf_ok = len(fps) == 1 and os.path.exists(fps[0])
        meas_ok = sess is not None and (
            bool(sess.snapshots) or bool(self.meas_var.get().strip()))

        dlg = tk.Toplevel(self.root)
        dlg.title("Replace values with a fit?"
                  + (f" ({c.key})" if self.pair_var.get() else ""))
        dlg.transient(self.root)
        dlg.grab_set()
        fr = ttk.Frame(dlg, padding=10)
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text=f"Model: {KEY2LABEL[key]}  --  parameters: {names}",
                  font=MONO).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(fr, foreground="#666666", wraplength=560, justify="left", text=(
            "Either option overwrites the parameter boxes. The loop uses the "
            "new values from the next Init, Step or bench iteration; nothing "
            "already played changes.")).grid(row=1, column=0, columnspan=3,
                                             sticky="w", pady=(2, 8))

        # ---- option A: the FRF
        a = ttk.LabelFrame(fr, text="Fit to FRF", padding=6)
        a.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Label(a, wraplength=540, justify="left", text=(
            "Least squares of this model form to the measured transfer "
            "function in the FRF field: log-magnitude and phase with equal "
            "weight, coherent tones only, from the lowest tone up to the "
            "frequency below. Every frequency counts the same, so this is "
            "the fit that matters for loop stability -- the log reports the "
            "magnitude and phase residual in band and the frequency where "
            "|1 - gamma*H/H_model| first reaches 1, i.e. where the update "
            "stops contracting with these values. Only the dynamic terms "
            "are fitted; the gain is never free (a free gain absorbs model "
            "mismatch: 0.70 against a measured 0.56 on X1). It is the gain "
            "box when that holds a value -- a gain known from the loop's own "
            "DC contraction beats the FRF's lowest tone, which sits at a few "
            "hundred Hz and reads 0.59 on X1 -- else the median |H| of the "
            "three lowest tones. Second order accepts zeta > 1 (two real "
            "poles).")
                  ).grid(row=0, column=0, columnspan=4, sticky="w")
        try:
            fhi0 = max(float(c.fcut_var.get()), 20e3)
        except ValueError:
            fhi0 = 40e3
        fhi_var = tk.StringVar(value=f"{fhi0:g}")
        have_gain = bool(c.param_vars["gain"].get().strip())
        fixg_var = tk.BooleanVar(value=not have_gain)
        ttk.Label(a, text="fit up to Hz").grid(row=1, column=0, sticky="w",
                                                pady=(4, 0))
        ttk.Entry(a, textvariable=fhi_var, width=9).grid(row=1, column=1,
                                                         sticky="w", pady=(4, 0))
        ttk.Checkbutton(a, text="gain from the lowest tones (untick: keep the gain box)",
                        variable=fixg_var).grid(row=1, column=2, sticky="w",
                                                padx=(12, 0), pady=(4, 0))
        ttk.Label(a, foreground="#666666", text=(
            "(prefilled: f_cut, at least 20 kHz -- the band the model has to "
            "be right in)")).grid(row=1, column=3, sticky="w", padx=(8, 0),
                                  pady=(4, 0))
        src_a = (os.path.basename(fps[0]) if frf_ok else
                 ("no FRF file in the FRF field" if not fps else
                  f"the FRF field lists {len(fps)} files (Show FRF's overlay "
                  f"view); a fit needs exactly one" if len(fps) > 1 else
                  f"FRF not found: {fps[0]}"))
        ttk.Label(a, text=f"source: {src_a}", foreground="#666666").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        def go_frf():
            try:
                f_hi = float(fhi_var.get())
                if f_hi <= 0:
                    raise ValueError
            except ValueError:
                return messagebox.showerror("Fit to FRF", "'fit up to' must be "
                                            "a positive frequency in Hz",
                                            parent=dlg)
            gain = None
            if not fixg_var.get():
                txt = c.param_vars["gain"].get().strip()
                try:
                    gain = float(txt)
                    if gain <= 0:
                        raise ValueError
                except ValueError:
                    return messagebox.showerror(
                        "Fit to FRF", "the gain box is not a positive number; "
                        "type one or tick 'gain from the lowest tones'",
                        parent=dlg)
            dlg.destroy()
            self.do_fit_frf(fps[0], key, f_hi, gain, c=c)

        bb = ttk.Button(a, text="Fit to FRF", command=go_frf,
                        state="normal" if frf_ok else "disabled")
        bb.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        # ---- option B: the played pair
        b = ttk.LabelFrame(fr, text="Fit to measurement", padding=6)
        b.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(b, wraplength=540, justify="left", text=(
            "plant.identify on the last played drive/response pair in the "
            "time domain: the latest measurement in this session (against "
            "the drive that produced it), else the Captures glob. Weighted by "
            "the record's own spectrum, so on a slow ramp -- almost all of it "
            "below a few kHz -- the gain comes out right and the lag terms "
            "come out short (27 us against 70 us from the FRF on X1). Use it "
            "for the gain, or when no FRF has been measured yet.")
                  ).grid(row=0, column=0, sticky="w")
        src_b = ("the latest measurement in this session"
                 if sess is not None and sess.snapshots else
                 f"the Captures glob {self.meas_var.get().strip()}"
                 if meas_ok else
                 "nothing to fit from: load or init a session and run an "
                 "iteration, or set the Captures glob")
        ttk.Label(b, text=f"source: {src_b}", foreground="#666666").grid(
            row=1, column=0, sticky="w", pady=(4, 0))

        def go_meas():
            dlg.destroy()
            self.do_fit(c=c)

        ttk.Button(b, text="Fit to measurement", command=go_meas,
                   state="normal" if meas_ok else "disabled").grid(
            row=2, column=0, sticky="ew", pady=(6, 0))

        ttk.Button(fr, text="Cancel", command=dlg.destroy).grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))

    def do_fit_frf(self, path, model_key, f_hi, gain=None, c=None):
        """Fit the selected parametric rung to the FRF file `path` up to
        f_hi Hz; gain None = the median |H| of the lowest tones. Works with
        or without a session (the fit lives in frequency, not on a grid).
        `c`: the Model column the result goes into."""
        c = c or self._col1
        try:
            gamma = float(c.gamma_var.get())
        except ValueError:
            gamma = 0.6
        sess = self._col_session(c)
        dt = sess.loop.dt if sess is not None else 2e-6
        self.run_worker(lambda: self._fit_frf_work(path, model_key, f_hi,
                                                   gain, gamma, dt, c),
                        "fitting model to the FRF...")

    def _fit_frf_work(self, path, model_key, f_hi, gain, gamma, dt, c=None):
        f, H = ilc._read_frf(path)
        p, info = plantmod.fit_frf(f, H, model=model_key, f_hi=f_hi,
                                   gain=gain, dt=dt)
        lam, f_b = plantmod.contraction(f, H, p, gamma)
        band = (f >= info["f_lo"]) & (f <= info["f_hi"])
        print(f"fit ({KEY2LABEL[model_key]}) to {os.path.basename(path)}, "
              f"{info['n_tones']} tones {info['f_lo']:.0f} Hz - "
              f"{info['f_hi']/1e3:.4g} kHz:")
        print(f"  {p}")
        print(f"  gain: {info['gain_source']}")
        print(f"  residual in band: {info['resid_mag_pct']:.1f}% rms magnitude, "
              f"{info['resid_phase_deg']:.1f} deg rms phase -- what this form "
              f"cannot follow in the measured response")
        worst = int(np.argmax(lam[band]))
        print(f"  contraction |1 - {gamma:g} H/H_model|: worst in band "
              f"{lam[band][worst]:.2f} at {f[band][worst]/1e3:.1f} kHz; "
              + (f"first reaches 1 at {f_b/1e3:.1f} kHz -- the update stops "
                 f"contracting there; keep f_cut below it (the Q filter "
                 f"shields a little past it)" if f_b is not None else
                 f"below 1 at every measured tone to {f[-1]/1e3:.0f} kHz"))
        self.msgs.put(("call", lambda: self._after_frf_fit(p, c)))

    def _after_frf_fit(self, p, c=None):
        self._set_param_entries(p, c)
        try:
            self.do_show_frf(c)         # the fitted model over the tones
        except Exception as e:          # the overlay is a courtesy
            self.log(f"(FRF overlay not drawn: {e})")

    def do_fit(self, c=None):
        """Identify the selected parametric model from real data: the last
        measured iteration if there is one, else the capture glob. `c`: the
        Model column (its session: X2's for pair mode's X2 column)."""
        c = c or self._col1
        key = self._model_key(c)
        if key == "frf":
            return messagebox.showinfo(
                "Fit to measurement",
                "The measured FRF is not fitted here -- the measurement IS "
                "the identification. Use Measure FRF... to take one (or the "
                "CLI: tools/sysid_make.py + sysid_fit.py), then set the "
                "taper inside its coherent band and switch the Model to "
                "'measured FRF'. Fit belongs to the parametric rungs.")
        sess = self._col_session(c)
        if sess is None:
            return messagebox.showerror("Fit to measurement",
                                        "load or init a session first")
        pattern = self.meas_var.get().strip()
        mon = self.moncol_var.get()
        self.run_worker(lambda: self._fit_work(key, pattern, mon, c, sess),
                        "fitting plant from measurement...")

    def _fit_work(self, model_key, pattern, mon, c=None, s=None):
        s = s or self.session
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
        self.msgs.put(("call", lambda: self._after_fit(p2, u_fit, y, it, c, s)))

    def _after_fit(self, p, u_fit, y, it, c=None, s=None):
        self._set_param_entries(p, c)
        if s is not None and s is not self.session:
            return                  # the plots show the other chain of the pair
        self._plot_waveforms_pinned(u_fit, y, p.forward(u_fit), it)
        self._show_fig(self.fig_wave)

    def _browse_target(self):
        self._browse(self.target_var, "Target CSV", "*.csv",
                     os.path.join(HERE, "waveforms"))
        if os.path.exists(self.target_var.get().strip()):
            self.do_preview_target()

    def _first_shot_gain(self, shot=None, c=None):
        """The conversion gain for the flat first shot: the dedicated entry,
        else the model gain as fallback. None when neither is set. `shot`
        replaces the entry's text and `c` names the Model column whose gain
        is the fallback (pair mode's X2: no first-shot gain, its own gain)."""
        shot_txt = self.shotgain_var.get() if shot is None else shot
        model_txt = (c or self._col1).param_vars["gain"].get()
        for name, txt in (("first-shot gain", shot_txt),
                          ("model gain", model_txt)):
            txt = txt.strip()
            if txt:
                try:
                    g = float(txt)
                except ValueError:
                    raise RuntimeError(f"{name} is not a number: {txt!r}")
                if g <= 0:
                    raise RuntimeError(f"{name} must be positive")
                return g
        return None

    def do_preview_target(self, quiet=False, replay=False):
        """Plot the target file, and the AWG output the flat first shot
        would produce at AMP = 2x full scale / OFST 0 -- nothing is sent.
        This is the look-before-you-init view.

        `replay` marks the call that _redraw_iterations makes to re-render
        the Waveforms tab. That one must not front the notebook or write to
        the log: it fires on every Redraw and after every Hold run, and it
        was yanking the tab away from whatever was being watched and
        repeating the same preview line each time."""
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
                label="target (file contents)", **self._dot_kw(len(tms)))
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
                     **self._dot_kw(len(tms)))
            ax2.axhline(fs, color="#c62828", lw=0.8, ls="--")
            ax2.axhline(-fs, color="#c62828", lw=0.8, ls="--",
                        label=f"+/-{fs:g} V full scale "
                              f"(AMP {2*fs:g} Vpp, OFST 0)")
            note = f"peak {pk:.3f} V = {100*pk/fs:.1f}% of DAC range"
            if pk > fs:
                note += "  --  CLIPS: raise the gain or lower the target"
                if not replay:
                    self.log(f"preview: the first shot would CLIP "
                             f"({pk:.3f} V > {fs:g} V full scale)")
            ax2.set_title(note)
            ax2.legend(loc="best", fontsize=7)
            if not replay:
                self.log(f"preview {os.path.basename(path)}: "
                         f"{np.ptp(v)*ch.mon_scale:.0f} V pk-pk over "
                         f"{t[-1]*1e3:.2f} ms, {len(v)} pts; AWG peak "
                         f"{pk:.3f} V ({100*pk/fs:.1f}% of range) at gain "
                         f"{g:g}")
        else:
            ax2.text(0.5, 0.5, "type a first-shot gain (or a model gain)\n"
                               "to preview the AWG output",
                     ha="center", va="center", transform=ax2.transAxes,
                     color="#888888")
            if not replay:
                self.log(f"preview {os.path.basename(path)}: "
                         f"{np.ptp(v)*ch.mon_scale:.0f} V pk-pk over "
                         f"{t[-1]*1e3:.2f} ms, {len(v)} pts (no gain set, "
                         f"AWG preview skipped)")
        ax2.set_xlabel("time (ms)")
        ax2.set_ylabel("AWG drive (V)")
        ax2.grid(True, alpha=0.3)
        self._finish_time_axis(self.ax_out)
        self._draw(self.fig_wave)
        if not replay:
            self._show_fig(self.fig_wave)
        self._wave_redraw = (lambda:
                             self.do_preview_target(quiet=True, replay=True))

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

    # ---- spin echo: both transports of a suite target in one record ------
    def do_echo_dialog(self):
        """Build target_<stem>E2X1/X2.csv from a one-transport pair
        (eomilc.echo): the source's lead, the transport, idle until the next
        leg starts one spacing after the first, the transport again, a tail.
        The Target box then holds the new X1 file and the stem its name, so
        Init (pair mode) trains both legs in one campaign. The same builder
        serves tools/make_echo_target.py and a batch campaign's "echo" key."""
        fr = self._dialog("_echo_win", "Spin-echo record from a target pair")
        if fr is None:
            return
        e = self.cfg.get("echo", {}) if isinstance(self.cfg.get("echo"), dict) else {}
        src = self.target_var.get().strip()
        v = dict(source=tk.StringVar(value=src),
                 spacing=tk.StringVar(value=str(e.get("spacing_ms", "16.667"))),
                 legs=tk.StringVar(value=str(e.get("legs", "2"))),
                 tail=tk.StringVar(value=str(e.get("tail_us", "200"))),
                 end=tk.StringVar(value=str(e.get("end_ms", "33.334"))),
                 suffix=tk.StringVar(value=str(e.get("suffix", ""))),
                 out=tk.StringVar(value=str(e.get("out", ""))))
        self._echo_vars = v
        g = ttk.Frame(fr); g.pack(fill="x")
        self._path_row(g, 0, "Source X1", v["source"],
                       lambda: self._browse(v["source"], "Target CSV", "target_*X1.csv",
                                            os.path.dirname(src) if src else os.path.join(HERE, "waveforms")))
        self._path_row(g, 1, "Write to", v["out"],
                       lambda: v["out"].set(filedialog.askdirectory(
                           parent=self._echo_win, title="Folder for the echo targets",
                           initialdir=v["out"].get() or os.path.dirname(v["source"].get()) or HERE)
                           or v["out"].get()))
        g.columnconfigure(1, weight=1)
        r = ttk.Frame(fr); r.pack(fill="x", pady=(3, 0))
        for lab, key, w in (("leg spacing ms", "spacing", 8), ("legs", "legs", 3),
                            ("tail us", "tail", 5), ("end ms", "end", 7), ("suffix", "suffix", 5)):
            ttk.Label(r, text=lab).pack(side="left", padx=(0, 2))
            ttk.Entry(r, textvariable=v[key], width=w).pack(side="left", padx=(0, 8))
        ttk.Label(fr, foreground="#666666", wraplength=470, justify="left", text=(
            "X2's source is the same name with X2, beside X1's. Leg spacing is "
            "leg start to leg start (the experiment's echo time; it is rounded to "
            "the record's grid). Suffix blank = E<legs>; the files are "
            "target_<stem><suffix>X1/X2.csv, in the source's folder unless "
            "Write to is set. The stem must stay within the pair mode's "
            f"{NAME_LIMIT - 6} characters. The log gets the record's numbers, "
            "the card pause between legs and the limit check per channel.")).pack(
            fill="x", pady=(4, 0))
        bf = ttk.Frame(fr); bf.pack(fill="x", pady=(6, 0))
        b = ttk.Button(bf, text="Build + check", command=self._echo_build)
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)
        ttk.Button(bf, text="Close", command=self._echo_win.destroy).pack(side="left", padx=(4, 0))

    def _echo_build(self):
        """The dialog's button: build, check, put the new X1 in the Target
        box and its name in the stem box, plot it."""
        from eomilc import echo
        v = self._echo_vars
        src = v["source"].get().strip()
        if not os.path.exists(src):
            return messagebox.showerror("Spin-echo record", f"source not found: {src!r}",
                                        parent=self._echo_win)
        try:
            spacing, legs, tail = float(v["spacing"].get()), int(float(v["legs"].get())), float(v["tail"].get())
            end = float(v["end"].get()) if v["end"].get().strip() else None
        except ValueError:
            return messagebox.showerror("Spin-echo record", "spacing, legs, tail and end must be numbers",
                                        parent=self._echo_win)
        suffix = v["suffix"].get().strip() or None
        out = v["out"].get().strip() or None
        self.log(f"spin-echo record from {os.path.basename(src)}: {legs} legs, {spacing:g} ms spacing")
        try:
            r = echo.make([src], spacing_ms=spacing, legs=legs, tail_us=tail, suffix=suffix,
                          out_dir=out, log=lambda s: self.log("  " + s), end_ms=end)
        except (ValueError, OSError) as e:
            self.log(f"  REFUSED: {e}")
            return messagebox.showerror("Spin-echo record", str(e), parent=self._echo_win)
        self.cfg["echo"] = dict(spacing_ms=spacing, legs=legs, tail_us=tail, end_ms=v["end"].get().strip(),
                                suffix=v["suffix"].get().strip(), out=v["out"].get().strip())
        if "X2" not in r["files"]:
            self.log("  NOTE: no X2 source beside the X1 file; only X1's record was written "
                     "(pair mode needs both)")
        self.target_var.set(r["files"].get("X1") or next(iter(r["files"].values())))
        self.stem_var.set(r["name"])
        if not r["ok"]:
            messagebox.showerror("Spin-echo record",
                                 "written, but a check FAILED -- see the log", parent=self._echo_win)
        elif r["notes"]:
            messagebox.showwarning("Spin-echo record", "written; " + "; ".join(r["notes"]),
                                   parent=self._echo_win)
        self.do_preview_target(quiet=True)
        return r

    def _auto_reload(self):
        try:
            self.log("restoring the last session...")
            self.do_load()
        except Exception as e:
            self.log(f"could not restore the last session: {e}")

    def do_load(self):
        path = self.state_var.get().strip()
        if not path:
            return self._browse(self.state_var, "State files", "*.state.npz", RUN_DIR)
        if self.pair_var.get():
            return self._load_pair(path)
        try:
            s = load_session(path)
        except Exception as e:
            return messagebox.showerror("Load state", str(e))
        self._take_session(s, path)

    def _take_session(self, s, path):
        """A loaded session into the panel: column 1, the Session tab's
        boxes, the Target box, the recalled measurements, the plots."""
        self.session = s
        self._remember_fields()     # the panel's numbers, before the state's
        self.channel_var.set(s.channel)
        self.stem_var.set(s.stem)
        self.gamma_var.set(f"{s.loop.gamma:g}")
        self.fcut_var.set(f"{s.loop.f_cut:g}")
        self.notch_var.set(", ".join(f"{f0:g}/{bw:g}" for f0, bw in s.loop.notches))
        self.toff_var.set(f"{s.t_off*1e6:g}")
        self.fs_var.set(f"{s.full_scale:g}")
        self._set_param_entries(s.loop.plant)
        self._apply_channel_defaults(s.channel)
        if s.model_key:
            self.model_var.set(KEY2LABEL.get(s.model_key,
                                             self.model_var.get()))
            self._update_model_fields()
            if s.model_key == "frf" and s.frf_path:
                self.frf_var.set(s.frf_path)
                self.fuse_var.set(f"{s.frf_use:g}")
                self.fmax_var.set(f"{s.frf_max:g}")
                if not os.path.exists(s.frf_path):
                    self.log(f"NOTE: the state's recorded FRF file is "
                             f"missing: {s.frf_path}")
                elif s.frf_error:
                    self.log(f"NOTE: the state's recorded FRF did NOT load, "
                             f"so the loop is resuming without it:\n"
                             f"      {s.frf_error}")
        self._mem_channel, self._mem_model = s.channel, self._model_key()
        # The Target box follows the state: the file its iterations were
        # measured against, not whatever the box happened to hold. A state
        # from before the path was recorded is matched by content against
        # the target folders; a target that is nowhere leaves the box alone
        # and says so -- the box is what Init and the preview read.
        tp = s.target_path
        how = "recorded in the state"
        if not (tp and os.path.exists(tp)):
            found = find_target_file(
                s.loop.target, s.loop.channel.mon_scale,
                (os.path.join(HERE, "waveforms"), RUN_DIR,
                 os.path.dirname(os.path.abspath(path))))
            how = ("matched by content -- the state predates the recorded "
                   "path" if not tp else
                   f"matched by content -- the recorded {tp} is missing")
            tp = found
        if tp:
            if os.path.normcase(os.path.abspath(tp)) != os.path.normcase(
                    os.path.abspath(self.target_var.get().strip() or "?")):
                self.log(f"  target box -> {tp} ({how})")
            self.target_var.set(tp)
            if not s.target_path or s.target_path != tp:
                s.target_path = tp          # written back on the next save
        else:
            self.log(f"  NOTE: the state's target file is not known "
                     f"({'recorded ' + s.target_path + ' is missing' if s.target_path else 'no path recorded'}) "
                     f"and no CSV in waveforms\\ or run\\ matches its "
                     f"content -- the Target box is left as it was")
        self.log(f"loaded {path}")
        self.log(f"  {s.channel} iteration {s.iteration}, stem {s.stem}, "
                 f"gamma {s.loop.gamma:g}, f_cut {s.loop.f_cut/1e3:g} kHz, "
                 f"t-offset {s.t_off*1e6:g} us")
        self.log(f"  plant: {s.loop.plant}")

        # Pull the last bench measurements back in, so the plots do not start
        # blank on a resumed campaign.
        recall_snapshots(s)
        if s.snapshots:
            its = sorted({sn["it"] for sn in s.snapshots})
            self.log(f"  recalled {len(its)} stored measurement(s): "
                     f"iterations {', '.join(str(i) for i in its)}" + self._pd_recalled(s))
        rec = chanmap.from_record(s.scope_channels) if s.scope_channels else None
        if rec is not None:
            diff = chanmap.differences(rec, self.cfg)
            self.log("  scope channels when measured: "
                     + chanmap.describe(rec).split("\n")[0]
                     + ("" if not diff else " -- DIFFERS from the panel's now: "
                        + ", ".join(diff)))
        # the Corrections box and the line-ripple switch follow the state
        self._corr_pending = [dict(L) for L in s.corr_layers]
        if s.line_fit is not None:
            self._line_fit = s.line_fit
        self.line_on_var.set(s.loop.line is not None)
        if s.base_target is not None:
            d = (s.corr_total if s.corr_total is not None else
                 s.loop.target - s.base_target) * s.loop.channel.mon_scale
            self.log(f"  target = base + learned corrections "
                     f"({np.max(np.abs(d)):.1f} V peak) -- Corrections tab")
        if s.loop.line is not None:
            self.log("  line ripple subtracted before each update -- Measure "
                     "tab")
        self._refresh_corr_list()
        self._refresh_line_status()
        self._replot_corr_quiet()
        self._refresh_summary()
        self._show_session(select_tab=True)

    def do_init(self):
        if self.pair_var.get():
            return self._init_pair()
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
                "A campaign already uses this stem",
                f"{state_path}\nalready exists.\n\n"
                f"Init OVERWRITES the loop's memory: the current drive, the "
                f"iteration counter, and the error history behind the "
                f"Convergence tab -- that history exists nowhere else. The "
                f"per-iteration files (drive_/meas_{stem}_iNN) survive this "
                f"press, but the new campaign restarts at i00 under the SAME "
                f"names and overwrites them one iteration at a time.\n\n"
                f"To keep iterating on the existing campaign use Load state "
                f"instead; to start fresh AND keep the old campaign, change "
                f"the name stem.\n\nDiscard the existing campaign?"):
            return

        ch = CHANNELS[chname]
        t, v = run_ilc.load_target(target, ch.mon_scale)
        dt = float(np.median(np.diff(t)))
        # The first shot is a FLAT conversion (target / gain) -- only the
        # seed's gain shapes it. The rest of the seed plant still matters:
        # it is the parametric model stored in the state, drives the
        # model-predicted-output trace, and is what parametric updates use.
        mode = self._model_key()
        frf_rec = ("", 0.0, 0.0)
        if mode == "frf":
            fps = self._frf_paths()
            if len(fps) != 1:
                return messagebox.showerror(
                    "Init", f"the measured-FRF model needs exactly ONE file "
                            f"in the FRF field (it holds {len(fps)})")
            if not os.path.exists(fps[0]):
                return messagebox.showerror("Init",
                                            f"FRF not found: {fps[0]!r}")
            try:
                fb = self._floats(f_use=self.fuse_var, f_max=self.fmax_var)
            except RuntimeError as e:
                return messagebox.showerror("Init", str(e))
            if not 0 < fb["f_use"] < fb["f_max"]:
                return messagebox.showerror(
                    "Init", f"the taper needs 0 < f_use < f_max "
                            f"(got {fb['f_use']:g}/{fb['f_max']:g})")
            try:
                self._check_taper_band(fps[0], fb["f_use"], fb["f_max"])
            except RuntimeError as e:
                return messagebox.showerror("Init", str(e))
            frf_rec = (fps[0], fb["f_use"], fb["f_max"])
        # On the measured-FRF rung the stored parametric plant is only the
        # gain (the model-predicted trace and the DC scale): the FRF drives
        # the updates. It used to be seeded with the calibration tables'
        # resonance, which put the withdrawn 2.3 kHz / 0.21 into every FRF
        # state's record.
        seed_key = "static" if mode == "frf" else mode
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
        if params is None:
            # No silent fallback to any table: the numbers come from this
            # system -- typed, or fitted to its FRF or its played response.
            blank = [k for k in PARAMS_FOR[seed_key]
                     if not self._param_vars[k].get().strip()]
            return messagebox.showerror(
                "Init", f"The {KEY2LABEL[seed_key]} model has no value for "
                        f"{', '.join(blank)}.\n\nType it, or fill it with "
                        f"'Replace values with a fit...' (Fit to FRF needs "
                        f"only an FRF file in the FRF field; Fit to "
                        f"measurement needs a played iteration).\n\nStarting "
                        f"from scratch: choose 'gain only', type a "
                        f"conservative gain -- high is the safe direction, "
                        f"the loop converges for gamma*g_true/g_model < 2 -- "
                        f"Init, run one iteration, then fit.")
        plant = self._plant_from(params, dt)
        seed_src = "from the panel entries"
        try:
            notches = self._notches()
        except RuntimeError as e:
            return messagebox.showerror("Init", str(e))
        loop = ilc.Loop(plant=plant, target=v, dt=dt, channel=ch,
                        gamma=f["gamma"], f_cut=f["f_cut"], limits=ch.limits,
                        notches=notches)
        u = loop.first_shot(gain=g_shot)
        g_used = g_shot if g_shot is not None else plant.gain
        seed = self.seed_var.get().strip()
        if seed:
            try:
                u = run_ilc.load_seed_drive(seed, t)
            except (ValueError, OSError, KeyError) as e:
                return messagebox.showerror("Init", f"seed drive: {e}")
            seed = os.path.abspath(seed)
        rep = loop.check(u)
        if seed and not rep and not messagebox.askyesno(
                "Seed drive fails the limit check",
                f"{rep}\n\nThe bench loop and Hold refuse to play a drive "
                f"that fails this check, so this session could not be "
                f"measured as it stands. Keep it anyway (fix the file, then "
                f"Init again)?"):
            return

        os.makedirs(RUN_DIR, exist_ok=True)
        # _i00, NOT _iter0: this is the same file _write_iteration produces
        # for every later iteration and the one recall_snapshots reads back.
        # Under the old name it was written and never read, so a campaign
        # that was Init'ed here and then stepped from files came back after a
        # reload with no iteration-0 drive -- and Drive corrections silently
        # swapped its reference from "the iteration-0 drive" to "the flat
        # conversion", which differ by whatever the gain box has done since.
        out = os.path.join(RUN_DIR, f"drive_{stem}_i00.csv")
        how = (f"seed drive {os.path.basename(seed)}, played as-is" if seed
               else "flat conversion, target / gain")
        outputs.write_awg_csv(out, t, u,
                              comment=f"{chname} ILC iteration 0 "
                                      f"({how})\n{plant}")
        wname = f"{stem}_i00"
        os.makedirs(AWG_WAVEFORMS, exist_ok=True)
        gui = outputs.write_bk_waveform(os.path.join(AWG_WAVEFORMS, wname + ".csv"),
                                        u, wname, f["full_scale"])
        s = Session(state_path=state_path, loop=loop, t=t, u=u, iteration=0,
                    stem=stem, full_scale=f["full_scale"],
                    t_off=f["t_offset_us"] * 1e-6)
        s.model_key = mode
        s.frf_path, s.frf_use, s.frf_max = frf_rec
        s.seed_path = seed
        s.target_path = os.path.abspath(target)
        # The line ripple belongs to the bench and its trigger, not to the
        # campaign: a fit the panel holds carries into the new session (on
        # the new record) if the switch is on. Corrections do not carry --
        # they belong to the target they were measured against.
        if self._line_fit is not None:
            s.line_fit = self._line_fit
            self._apply_line(s, self.line_on_var.get())
        self._corr_pending = []
        s.scope_channels = chanmap.record(self.cfg)
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
        if seed:
            self.log(f"  first shot  : SEED drive {seed} played as-is "
                     f"(no conversion, no pre-distortion), drive peak "
                     f"{np.abs(u).max():.4f} V, idle {u[0]*1e3:+.1f}/"
                     f"{u[-1]*1e3:+.1f} mV -- recorded in the state as "
                     f"seed_path")
        else:
            self.log(f"  first shot  : flat conversion (target / {g_used:g}"
                     f"{sep}), drive peak {np.abs(u).max():.4f} V -- no "
                     f"pre-distortion, the first measurement shows the "
                     f"chain's raw response")
        self.log(f"  predicted   : peak error "
                 f"{np.abs(plant.forward(u)-v).max()*ch.mon_scale:.1f} V "
                 f"(the model's guess at what that measurement shows)")
        if mode == "frf":
            self.log(f"  model       : measured FRF "
                     f"{os.path.basename(frf_rec[0])}, taper "
                     f"{frf_rec[1]/1e3:g}-{frf_rec[2]/1e3:g} kHz -- takes "
                     f"over at the first step (recorded in the state)")
        self.log(f"  limit check : {rep}")
        self.log(f"  wrote {out}")
        self.log(f"        {gui}  (GUI-ready, upload with Normalise OFF)")
        self.log(f"  state {state_path}")
        self._refresh_summary()
        self._show_session(select_tab=True)
        self._save_config()          # stem/gain/channel survive even a kill

    def _refresh_summary(self):
        s = self.session
        if not self.busy:
            self.status.configure(text=self._hint())
        if s is None:
            self.upload_btn.configure(text="Upload drive to AWG")
            return self.summary.configure(text="no session loaded")
        self.upload_btn.configure(
            text=f"Upload {s.stem}_i{s.iteration:02d} to AWG")
        lp = s.loop
        idle = (s.u[0] * 1e3, s.u[-1] * 1e3)
        if s.model_key == "frf":
            tag = (f"  --  FRF {os.path.basename(s.frf_path) or '?'} "
                   f"{s.frf_use/1e3:g}-{s.frf_max/1e3:g}k")
            band = ""
        elif s.model_key:
            tag = f"  --  {DESC_FOR.get(s.model_key, s.model_key)}"
            band = f"f_cut {lp.f_cut/1e3:g} kHz, "
        else:                        # a pre-record state: show what we know
            tag = ""
            band = f"f_cut {lp.f_cut/1e3:g} kHz, "
        of_pair = (f" ({self._pair_key_of(s)} of a pair)"
                   if self._pair_key_of(s) else "")
        txt = (f"{s.channel}  '{s.stem}'  iteration {s.iteration}{tag}{of_pair}\n"
               f"target {np.ptp(lp.target)*lp.channel.mon_scale:.0f} V pk-pk, "
               f"{len(s.t)} pts, dt {lp.dt*1e6:.2f} us\n"
               f"drive peak {np.abs(s.u).max():.3f} V, idle "
               f"{idle[0]:+.1f}/{idle[1]:+.1f} mV of "
               f"{lp.limits.idle_awg*1e3:.0f} mV cap\n"
               f"gamma {lp.gamma:g}, {band}"
               f"t-offset {s.t_off*1e6:g} us, "
               f"history {len(lp.history)} iterations")
        if s.seed_path:
            txt += f"\niteration 0 was seed {os.path.basename(s.seed_path)}"
        if s.base_target is not None:
            d = (s.loop.target - s.base_target) * lp.channel.mon_scale
            txt += (f"\ntarget = base + corrections "
                    f"({np.max(np.abs(d)):.1f} V pk)")
        if lp.line is not None:
            txt += (f"\nline ripple subtracted "
                    f"({np.max(np.abs(lp.line))*lp.channel.mon_scale:.2f} V pk)")
        self.summary.configure(text=txt)
        self._refresh_corr_status()
        self._refresh_line_status()
        self._refresh_optical()

    # --------------------------------------------------- shared step pieces
    def _check_taper_band(self, path, f_use, f_max):
        """Refuse a taper whose FULL-STRENGTH edge sits outside the tones the
        file actually carries, and hand back the top tone so the caller can
        warn about an f_max that overshoots it.

        The same rule `ilc.FRF` enforces, checked here so it arrives as a
        dialog before anything is dispatched rather than as a worker
        traceback after the instruments are open."""
        try:
            _, f_top = ilc.frf_band(path)
        except (OSError, ValueError, KeyError) as e:
            raise RuntimeError(f"{os.path.basename(path)}: {e}")
        if f_use >= f_top:
            raise RuntimeError(
                f"'full strength to' is {f_use/1e3:g} kHz, but "
                f"{os.path.basename(path)} only measures up to "
                f"{f_top/1e3:.1f} kHz. Past its top coherent tone the inverse "
                f"holds |H| flat, so the whole full-strength band would be "
                f"divided by an extrapolation: 1/|H| stays large where the "
                f"chain has really rolled off, and the update builds drive at "
                f"frequencies nothing was ever measured at. Pull the taper "
                f"inside the measured band, or measure a wider FRF.")
        return f_top

    def _gather_settings(self):
        """Entry fields -> plain values, ON THE MAIN THREAD. Tk variables must
        not be read from a worker (tkinter is not thread-safe), so everything a
        worker needs is collected here and handed over as floats and strings.

        Pair mode: the SHOWN channel's column, as in single mode (Manual step
        acts on the shown session), plus both under cfg["pair"] =
        {"X1": cfg, "X2": cfg} for the pair loop and Init."""
        if not (self.pair_var.get() and self._col2 is not None):
            return self._gather_column(self._col1)
        both = {k: self._gather_column(self._pair_col(k)) for k in PAIR_KEYS}
        out = dict(both[self.shown_var.get()])
        out["pair"] = both
        return out

    def _gather_column(self, c):
        """One Model column's settings (plus the shared t-offset)."""
        cfg = self._floats(gamma=c.gamma_var, f_cut=c.fcut_var,
                           t_offset_us=self.toff_var)
        cfg["mode"] = self._model_key(c)
        cfg["notches"] = self._notches(c)
        fpaths = self._frf_paths(c)
        cfg["frf_path"] = fpaths[0] if fpaths else ""
        if cfg["mode"] == "frf":
            if not fpaths:
                raise RuntimeError("the measured-FRF model needs an FRF file "
                                   "-- browse to run\\frf_<name>.csv, or "
                                   "measure one (Measure FRF...)")
            if len(fpaths) > 1:
                raise RuntimeError(
                    f"the FRF field lists {len(fpaths)} files (the overlay "
                    f"view) -- the measured-FRF model divides by exactly "
                    f"ONE. Keep the one to drive with.")
            cfg.update(self._floats(f_use=c.fuse_var, f_max=c.fmax_var))
            if not 0 < cfg["f_use"] < cfg["f_max"]:
                raise RuntimeError(
                    f"the taper needs 0 < f_use < f_max "
                    f"(got {cfg['f_use']:g}/{cfg['f_max']:g}). A zero-width "
                    f"taper is a brick wall that rings -- to nearly disable "
                    f"it, set f_use ~ 0.9 x f_max.")
            cfg["frf_top"] = self._check_taper_band(
                cfg["frf_path"], cfg["f_use"], cfg["f_max"])
        else:
            cfg["params"] = self._entry_params(cfg["mode"], strict=True, c=c)
        return cfg

    def _apply_settings(self, cfg, s=None):
        """Worker side: settings -> loop, logging anything that actually
        changed. Changed values persist in the state on the next save,
        exactly like the CLI's --f-cut / --t-offset overrides. `s`: the
        session (default the shown one; the pair loop names each)."""
        s = s or self.session
        if abs(cfg["gamma"] - s.loop.gamma) > 1e-12:
            print(f"gamma {s.loop.gamma:g} -> {cfg['gamma']:g}")
            s.loop.gamma = cfg["gamma"]
        if abs(cfg["f_cut"] - s.loop.f_cut) > 1e-9:
            print(f"f_cut {s.loop.f_cut:g} -> {cfg['f_cut']:g} Hz")
            s.loop.f_cut = cfg["f_cut"]
        if tuple(cfg["notches"]) != tuple(s.loop.notches):
            print("learning notches: " + (", ".join(
                f"{f0/1e3:g} kHz +/- {bw/2e3:g}" for f0, bw in cfg["notches"])
                or "none") + " -- the update is blind there")
            s.loop.notches = tuple(cfg["notches"])
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
            if s.loop.frf.f_extrap > 0:
                print(f"       WARNING: the taper reaches "
                      f"{cfg['f_max']/1e3:g} kHz but the top coherent tone is "
                      f"{s.loop.frf.f_top/1e3:.1f} kHz -- the last "
                      f"{s.loop.frf.f_extrap/1e3:.1f} kHz of the correction "
                      f"band divides by an extrapolated |H|. It is fading "
                      f"there, but it is not measured.")
            cfg["desc"] = (f"FRF {cfg['f_use']/1e3:g}-{cfg['f_max']/1e3:g}k")
            s.model_key, s.frf_path = "frf", cfg["frf_path"]
            s.frf_use, s.frf_max = cfg["f_use"], cfg["f_max"]
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
            s.model_key = cfg["mode"]
            s.frf_path, s.frf_use, s.frf_max = "", 0.0, 0.0

    def _noise_floor(self, stack, s=None):
        """One log line per measurement: where the error has fallen under
        the measurement's own scatter, against the band the update acts on
        (f_cut, or the taper's end on the FRF rung). Returns the numbers for
        the metrics dict -- empty when there is nothing to estimate from.
        `s`: the session (default the shown one; the pair loop names it)."""
        s = s or self.session
        f_top = s.loop.frf.f_max if s.loop.frf is not None else s.loop.f_cut
        try:
            # the loop's error is target - (y - line): the same thing as
            # (target + line) - y, which is what these helpers take
            tgt = s.loop.target + (s.loop.line if s.loop.line is not None
                                   and len(s.loop.line) == len(s.loop.target)
                                   else 0.0)
            nb = ilc.learnable_band(tgt, stack, s.loop.dt, f_top)
        except ValueError as e:
            print(f"         noise floor: not estimated -- {e}")
            return {}
        n, k, r, ff = nb["n_shots"], nb["factor"], nb["ratio_top"], nb["f_floor"]
        edge = f"{f_top/1e3:g} kHz"
        if not np.isfinite(r):
            print(f"         noise floor: the {n} shots are identical -- no "
                  f"repeatability to compare against")
        elif ff is None:
            print(f"         noise floor: error is {r:.1f}x the {n}-shot "
                  f"repeatability at the band edge ({edge}) -- still learning "
                  f"across the band")
        elif ff <= 0:
            print(f"         noise floor: error is under {k:g}x the {n}-shot "
                  f"repeatability in EVERY band to {edge} -- nothing left to "
                  f"learn; further iterations only add noise")
        else:
            print(f"         noise floor: nothing left to learn above "
                  f"{ff/1e3:.0f} kHz (error under {k:g}x the {n}-shot "
                  f"repeatability there); the band edge is {edge} -- lower it "
                  f"to ~{ff/1e3:.0f} kHz, or stop")
        out = dict(f_floor=ff, noise_ratio_top=float(r))
        try:
            lines = ilc.noise_lines(tgt, stack, s.loop.dt, f_top)
        except ValueError:
            lines = []
        if lines:
            cur = tuple(s.loop.notches)
            parts, want = [], []
            for d in lines[:4]:
                covered = any(abs(d["f0"] - f0) <= bw / 2 for f0, bw in cur)
                parts.append(f"{d['f0']/1e3:.2f} kHz ({d['ratio']:.0f}x the "
                             f"scatter around it, {d['sem']*1e3:.3f} mV left "
                             f"in the {n}-shot mean"
                             + (", notched)" if covered else ")"))
                if not covered:
                    want.append(f"{round(d['f0']/10)*10:.0f}/"
                                f"{max(400, round(2*d['width']/100)*100):.0f}")
            print(f"         noise lines: " + "; ".join(parts))
            if want:
                print(f"         -> not repeatable shot to shot, so the "
                      f"update can only chase them: put "
                      f"'{' '.join(want)}' in the notch field")
            out["noise_lines"] = [(d["f0"], d["width"], d["sem"], d["ratio"])
                                  for d in lines]
        return out

    def _measurement_stats(self, stack, path, scope=None, scope_ch=None, s=None):
        """The shots' statistics for one measurement (eomilc.diagnostics):
        the standard error per sample and per bin, the campaign bands and
        their floors, and the residual folded on the ADC code when the
        monitor's V/div is known. Written beside the measurement as
        meas_..._stats.npz so every stored iteration can be drawn later.
        Returns the dict, or None when there was nothing to compute from."""
        s = s or self.session
        vdiv = offset = None
        if scope is not None and scope_ch is not None:
            try:
                g = getattr(scope, "try_get", None) or scope.get
                vdiv = float(g(f":CHANnel{scope_ch}:SCALe"))
                offset = float(g(f":CHANnel{scope_ch}:OFFSet"))
            except Exception:
                vdiv = offset = None
        tgt = s.loop.target + (s.loop.line if s.loop.line is not None
                               and len(s.loop.line) == len(s.loop.target) else 0.0)
        try:
            st = dg.measurement_stats(tgt, stack, s.loop.dt, vdiv=vdiv, offset=offset)
        except ValueError as e:
            print(f"         stats: not kept -- {e}")
            return None
        try:
            dg.save_stats(dg.stats_path(path), st)
        except OSError as e:
            print(f"         stats: could not write the sidecar ({e})")
        if np.isfinite(st.get("fold_amp", float("nan"))):
            amp, sig = st["fold_amp"], st["fold_sig"]
            print(f"         code fold: {amp*1e3:.2f} mV pk-pk per {st['code_V']*1e3:.2f} mV code "
                  f"({amp/max(sig, 1e-12):.1f} sigma; the dithered scope reads ~1-3)")
        return st

    @staticmethod
    def _stats_metrics(st):
        """The few numbers of a stats dict that go into the history."""
        if not st:
            return {}
        return dict(bands_rms=[float(v) for v in st["bands_rms"]],
                    bands_floor=[float(v) for v in st["bands_floor"]],
                    floor_rms=float(st["floor_rms"]), vdiv=float(st["vdiv"]),
                    fold_amp=float(st.get("fold_amp", float("nan"))),
                    fold_sig=float(st.get("fold_sig", float("nan"))))

    def _model_check_line(self, it, u_now, y_now, s=None):
        """After a measurement of iteration `it`: did the chain answer the
        update that made this drive the way the model predicted? Needs the
        previous base measurement and its drive; silent without them.
        Returns the numbers for the metrics dict."""
        s = s or self.session
        prev = next((sn for sn in reversed(s.snapshots)
                     if sn.get("run") is None and sn["it"] == it - 1
                     and sn.get("u") is not None), None)
        if prev is None or len(prev["u"]) != len(u_now):
            return {}
        if prev.get("run_id") != self._run_id:
            print(f"         model check: first measurement of this run -- the "
                  f"change since iteration {it - 1} includes whatever the chain "
                  f"did while the output was off, so it is not the chain's "
                  f"answer to the update; skipped (compare from the next "
                  f"iteration)")
            return {}
        f_top = s.loop.frf.f_max if s.loop.frf is not None else s.loop.f_cut
        if s.loop.frf is not None:
            fwd = lambda du: s.loop.frf.forward(du, s.loop.dt)
        else:
            fwd = lambda du: s.loop.plant.forward(du) - s.loop.plant.offset
        try:
            rows = ilc.model_check(np.asarray(u_now) - prev["u"],
                                   np.asarray(y_now) - prev["y"], fwd,
                                   s.loop.dt, f_top, s.loop.gamma)
        except ValueError as e:
            print(f"         model check: not done -- {e}")
            return {}
        live = [r for r in rows if r["verdict"] != "quiet"]
        if not live:
            return {}
        print("         model check: the chain answered the last update at "
              + ", ".join(f"{r['ratio_local']:.1f}x ({r['lo']/1e3:.0f}-{r['hi']/1e3:.0f} kHz, "
                          f"corr {r['corr']:+.2f})" for r in live)
              + " where it acted")
        bad = [r for r in live if r["verdict"] != "ok"]
        worst = max(live, key=lambda r: abs(np.log(max(r["ratio_local"], 1e-9))))
        if bad:
            lo = min(r["lo"] for r in bad)
            unresp = [r for r in bad if r["verdict"] == "unresponsive"]
            nocon = [r for r in bad if r["verdict"] == "no contraction"]
            if nocon:
                print(f"         -> from {lo/1e3:.0f} kHz the update does not contract "
                      f"(|1 - gamma x response| = "
                      f"{max(r['lam'] for r in nocon):.1f}): the model is "
                      f"{'low' if worst['ratio_local'] > 1 else 'high'} there and the "
                      f"correction {'overshoots' if worst['ratio_local'] > 1 else 'falls short'}. "
                      f"Refit the model in that band, or bring the band edge under "
                      f"{lo/1e3:.0f} kHz")
            if unresp:
                print(f"         -> {', '.join(f'{r[chr(108)+chr(111)]/1e3:.0f}-{r[chr(104)+chr(105)]/1e3:.0f}' for r in unresp)} kHz: "
                      f"the monitor's change has nothing to do with what was pushed "
                      f"(corr < 0.3) -- noise, or something the drive cannot reach")
        return dict(model_ratio_worst=float(worst["ratio_local"]),
                    model_band_worst=float(worst["lo"]),
                    model_bands=[dict(lo=r["lo"], hi=r["hi"], verdict=r["verdict"],
                                      ratio_local=r["ratio_local"], lam=r["lam"],
                                      corr=r["corr"], update_rms=r["update_rms"])
                                 for r in rows])

    def _plateau_line(self, s=None):
        """When the history says the loop is re-learning noise, say so --
        and name the best iteration, whose drive is on disk. Silent
        otherwise: the noise-floor line is already there every time, and a
        second every-iteration line would bury both."""
        s = s or self.session
        p = ilc.plateau(s.loop.history, run_id=self._run_id)
        if not p or not p["flat"]:
            return
        print(f"         PLATEAU: peak error flat for {p['n']} iterations "
              f"(best {p['best_recent']:.1f} V, before that "
              f"{p['best_before']:.1f} V) while the update is not shrinking "
              f"({p['upd_recent']*1e3:.3g} mV rms, was "
              f"{p['upd_before']*1e3:.3g}) -- the loop is re-learning noise. "
              f"Stop, or lower the band edge. Best so far: iteration "
              f"{p['best_it']} at {p['best']:.1f} V, "
              f"drive_{s.stem}_i{p['best_it']:02d}.csv")

    def _write_iteration(self, wname, s=None):
        """Drive CSV in run\\, GUI-previewable copy in the AWG library --
        exactly the pair the CLIs leave behind."""
        s = s or self.session
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
        stk = {}                             # the files behind the average
        y, files = read_captures(pattern, mon, s.t, s.t_off, stack=stk)
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
        # persist the averaged measurement beside the state, exactly as the
        # bench loop does -- so a Step-measured iteration survives a close
        # and reloads with the session
        np.save(os.path.join(os.path.dirname(s.state_path),
                             f"meas_{s.stem}_i{it:02d}.npy"), y)
        print(f"iteration {it}: error peak {m['peak_err_hv']:7.1f} V   "
              f"rms {m['rms_err_hv']:6.2f} V   ({m['peak_pct']:.2f}% FS)")
        m.update(self._noise_floor(stk["stack"]))
        st = self._measurement_stats(stk["stack"], os.path.join(
            os.path.dirname(s.state_path), f"meas_{s.stem}_i{it:02d}.npy"))
        m.update(self._stats_metrics(st))
        self._run_id = None                  # a Step from files is not a bench run
        m.update(self._model_check_line(it, s.u, y))
        if refit:
            fit_key = cfg["mode"] if cfg["mode"] != "frf" else "resonant"
            p2, info = plantmod.identify(s.u, y, s.loop.dt, model=fit_key)
            print(f"refit plant ({KEY2LABEL[fit_key]}): {p2}  "
                  f"(residual {info['resid_peak_pct']:.2f}% peak)")
            s.loop.plant = p2

        u_prev = s.u
        u_next = s.loop.update(s.u, y)
        s.loop.history[-1]["model"] = cfg["desc"]
        s.loop.history[-1]["update_rms"] = ilc.update_rms(u_prev, u_next)
        s.loop.history[-1]["run_id"] = None
        rep = s.loop.check(u_next)
        print(f"limit check: {rep}")
        if not rep and not force:
            s.loop.history.pop()      # the refused update never happened
            s.snapshots.append(dict(it=it, y=y, m=m, u=u_prev, t_wall=time.time(),
                                    stats=st))
            self.msgs.put(("call", lambda: self._show_iteration(u_prev, y, m, it)))
            print("REFUSED to write a drive that violates a hard limit "
                  "(tick 'force' to override)")
            return
        s.loop.history[-1]["update_below_lsb"] = dg.update_vs_step(u_next - u_prev)["below"]
        s.u = u_next
        s.iteration = it + 1
        s.snapshots.append(dict(it=it, y=y, m=m, u=u_prev, t_wall=time.time(),
                                stats=st))
        self._plateau_line()
        self._write_iteration(f"{s.stem}_i{s.iteration:02d}")
        save_session(s)
        print(f"state saved, now at iteration {s.iteration}")
        self.msgs.put(("call", lambda: (self._refresh_summary(),
                                        self._show_iteration(u_next, y, m, it))))

    # -------------------------------------------------------------- autoset
    def do_native_spec(self):
        """Error spectrum straight from the capture files at the SCOPE's own
        sample rate: the target is interpolated onto the scope's time base
        instead of the capture being boxcar-decimated onto the waveform
        grid. The record length is unchanged, so the low-frequency bins do
        not move -- what this buys is the band past the grid Nyquist and
        the top octave without the anti-alias boxcar's droop. Draws as an
        overlay on the Error spectrum tab; the next Redraw clears it."""
        if self.session is None:
            return messagebox.showerror("Native spectrum",
                                        "load a state first")
        pattern = self.meas_var.get().strip()
        if not pattern:
            return messagebox.showerror("Native spectrum",
                                        "set the capture glob first")
        mon = self.moncol_var.get()
        kavg = self._spec_avg()
        self.run_worker(lambda: self._native_spec_work(pattern, mon, kavg),
                        "native-rate spectrum from captures...")

    def _native_spec_work(self, pattern, mon, kavg):
        s = self.session
        files = sorted(glob.glob(pattern))
        if not files:
            raise RuntimeError(f"no scope files matched {pattern!r}")
        t0, acc = None, 0.0
        for f in files:
            if f.lower().endswith(".npz") and not scopeio.is_capture_npz(f):
                # a bench-kept native average: t is already waveform time
                d = np.load(f)
                tt, yy = np.asarray(d["t"], float), np.asarray(d["y"], float)
            else:                        # a raw Scope Grab CSV or NPZ
                tr = scopeio.load(f)
                tt, yy = tr.t - s.t_off, tr[mon]
            if tt[0] > s.t[0] + 1e-4 or tt[-1] < s.t[-1] - 1e-4:
                raise RuntimeError(
                    f"{os.path.basename(f)} spans {tt[0]*1e3:.2f}.."
                    f"{tt[-1]*1e3:.2f} ms but the waveform runs "
                    f"{s.t[0]*1e3:.2f}..{s.t[-1]*1e3:.2f} ms. A zoomed or "
                    f"mismatched capture matched the glob -- tighten the "
                    f"pattern.")
            if t0 is None:
                t0, y = tt, yy
            else:                        # sequences share a time base; a
                y = np.interp(t0, tt, yy)          # stray one is aligned
            acc = acc + y
        y = acc / len(files)
        ts = t0
        m = (ts >= s.t[0]) & (ts <= s.t[-1])   # same span as the grid error
        ts, y = ts[m], y[m]
        dtn = float(np.median(np.diff(ts)))
        e = (np.interp(ts, s.t, s.loop.target) - y) * s.loop.channel.mon_scale
        f_n, a_n = avg_spectrum(e, dtn, kavg)
        print(f"native-rate spectrum: {len(files)} capture(s), "
              f"dt {dtn*1e9:.0f} ns (grid {s.loop.dt*1e6:g} us), Nyquist "
              f"{0.5/dtn/1e3:.0f} kHz vs {0.5/s.loop.dt/1e3:.0f} kHz -- the "
              f"record length is unchanged, so the low-frequency bins "
              f"do not move")
        nfiles = len(files)
        self.msgs.put(("call",
                       lambda: self._draw_native_spec(f_n, a_n, dtn, nfiles)))

    def _draw_native_spec(self, f, a, dtn, nfiles):
        ax = self.ax_spec
        ax.loglog(f, a, color="#000000", lw=0.8, alpha=0.75,
                  label=f"native rate ({nfiles} captures, dt {dtn*1e9:.0f} ns)")
        ax.legend(loc="best", fontsize=7)
        self._draw(self.fig_spec)
        # front the Error spectrum tab (by its frame, not a magic index)
        self._show_fig(self.fig_spec)

    def do_autoset(self):
        """Configure the AWG and scope from what the session already knows:
        the record's period sets the arb frequency and the scope window, the
        drive and target spans set the verticals. Never touches an output
        switch, and refuses to reconfigure a channel that is live."""
        if self.pair_var.get():
            return self._do_autoset_pair()
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
        if not self._wiring_ok(f["awg_ch"], f["scope_ch"], "Auto-set"):
            return
        period = float(s.t[-1]) + float(s.loop.dt)
        u, v = s.u, s.loop.target
        wname = f"{s.stem}_i{s.iteration:02d}"
        keep_vert = bool(self.keepvert_var.get())
        drive_ch = self._drive_scope_ch(f["awg_ch"])
        self.run_worker(lambda: self._autoset_work(
            period, float(u.min()), float(u.max()),
            float(v.min()), float(v.max()), s.full_scale,
            int(f["awg_ch"]), int(f["scope_ch"]), wname, keep_vert, drive_ch),
            "auto-setting instruments...")

    def _autoset_work(self, period, u_lo, u_hi, v_lo, v_hi, fs,
                      awg_ch, scope_ch, wname, keep_verticals=False,
                      drive_ch=None):
        # Connect BOTH instruments before writing to either: a scope that
        # does not answer (first live test: Scope Grab held its VISA session)
        # must not leave a half-configured bench.
        awg, scope = self._connect_pair()

        # ONE try/finally over both blocks. It used to be two, and the
        # `return` in the AWG block below then unwound through the AWG's
        # finally only -- leaving the scope's session open for the rest of
        # the panel's life, on the single most likely way to leave this
        # function (the output was already on).
        try:
            self._autoset_on(awg, scope, period, u_lo, u_hi, v_lo, v_hi, fs,
                             awg_ch, scope_ch, wname, keep_verticals, drive_ch)
        finally:
            # both closes leave the shared ResourceManager standing
            # (ilc_bench.make_awg / make_scope), so the order does not matter
            awg.close()
            scope.close()
            print("instruments closed")

    def _autoset_on(self, awg, scope, period, u_lo, u_hi, v_lo, v_hi, fs,
                    awg_ch, scope_ch, wname, keep_verticals=False,
                    drive_ch=None):
        """Auto-set's work on instruments already open (one chain): the AWG
        channel, the scope window, the chain's drive and monitor verticals.
        _autoset_work opens and closes around it; pair mode calls it once
        per chain inside one session."""
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
        self._bench_note(f"Auto-set: CH{awg_ch} FRQ {1.0/period:.6g} Hz, AMP "
                         f"{2*fs:g} Vpp, output {'ON' if awg.is_on(awg_ch) else 'OFF'}; "
                         f"scope HRES")
        print(f"       waveform readback: {arwv}")
        if not pick:
            print(f"       NOTE: '{wname}' is not in the generator's user "
                  f"memory (it holds: {', '.join(stored) or 'nothing'}). "
                  f"The selection was left alone -- upload it from the "
                  f"AWG GUI's Waveforms library ({wname}.csv, Normalise "
                  f"OFF), or let the bench loop upload it.")
        print(f"       burst readback: {btwv}")

        # full window with settle room, waveform start at the left edge:
        # position (trigger -> screen centre) = half the period
        rng = nice_setting(1.3 * period)
        # the polarimeter's scans leave the reference at LEFT, under which
        # this position puts the trigger half a record off screen (13 Oct
        # 2026: the batch refused B2S25 with "drive starts +6498.5 us")
        scope.put(":TIMebase:REFerence", "CENTer")
        scope.put(":TIMebase:RANGe", f"{rng:.6g}")
        scope.put(":TIMebase:POSition", f"{period/2:.6g}")
        scope.put(":ACQuire:TYPE", "HRES")
        print("scope channels: " + chanmap.describe(self.cfg).replace("\n", " | "))
        if drive_ch is None:
            print(f"  no CmdX{awg_ch} readback channel: only the monitor's "
                  f"verticals are set")
        for ch, lo, hi, what in ((drive_ch, u_lo, u_hi, "drive"),
                                 (scope_ch, v_lo, v_hi, "monitor")):
            if ch is None:
                continue
            scope.put(f":CHANnel{ch}:DISPlay", "1")
            # DC, always -- and before the verticals, and regardless of
            # 'keep verticals'. The ILC error is an absolute level, so AC
            # coupling does not rescale the measurement, it deletes the
            # part being measured: the monitor's DC is the EOM voltage,
            # and the drive's is the idle the end clamp exists to bound.
            # A noise capture leaves these channels AC on purpose
            # (eomilc.rin, ilc_bench.noise_capture set and restore it),
            # so a restore that did not run would otherwise sit here
            # silently until the next campaign learned nonsense. Coupling
            # first for the reason ilc_bench.scope_apply gives: the offset
            # written after it is interpreted against the coupling then in
            # force. 'keep verticals' is about V/div and offset -- the
            # ones you may want hand-tuned -- not about which half of the
            # signal reaches the digitiser.
            was = scope.try_get(f":CHANnel{ch}:COUPling")
            scope.put(f":CHANnel{ch}:COUPling", "DC")
            if was is not None and was.upper() != "DC":
                print(f"scope CH{ch} ({what}): coupling was {was} -- set "
                      f"to DC (AC deletes the level the loop measures)")
            if keep_verticals:
                print(f"scope CH{ch} ({what}): {scope.get(f':CHANnel{ch}:SCALe')} "
                      f"V/div, offset {scope.get(f':CHANnel{ch}:OFFSet')} V, "
                      f"DC -- verticals left as found (keep verticals "
                      f"ticked); coupling is not a vertical")
                continue
            span = max(hi - lo, 1e-3)
            scale = nice_setting(1.25 * span / 8)      # 8 vertical divs
            mid = 0.5 * (hi + lo)
            scope.put(f":CHANnel{ch}:SCALe", f"{scale:.6g}")
            scope.put(f":CHANnel{ch}:OFFSet", f"{mid:.6g}")
            print(f"scope CH{ch} ({what}): {scale:.3g} V/div, offset "
                  f"{mid:+.3g} V, DC (signal {lo:+.3g}..{hi:+.3g} V)")
        print(f"scope: {rng*1e3:.4g} ms window ({rng/10*1e3:.4g} ms/div), "
              f"position +{period/2*1e3:.4g} ms, acquisition HRES")
        print("trigger source and level NOT touched -- they belong to "
              "the burst-pulse wiring; confirm the shot still fires")
        errs = scope.errors()
        if errs:
            print("scope reported:", errs)

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

    # ----------------------------------------------------------------- hold
    def do_hold(self):
        """Re-measure the CURRENT drive several times without updating --
        for thermalisation studies: how does the error of one fixed drive
        evolve over minutes? Measurements are tagged as runs (iter k r1,
        r2, ...) so they never mix with the loop's own iterations, and the
        state is untouched."""
        if self.pair_var.get():
            return self._do_hold_pair()
        if self.session is None:
            return messagebox.showerror("Hold", "load or init a session first")
        try:
            f = self._floats(runs=self.holdruns_var, gap_s=self.holdgap_var,
                             awg_ch=self.awgch_var, scope_ch=self.scopech_var,
                             repeats=self.repeats_var, wait=self.wait_var)
        except RuntimeError as e:
            return messagebox.showerror("Hold", str(e))
        if f["runs"] < 1:
            return messagebox.showerror("Hold", "runs must be at least 1")
        if not self._wiring_ok(f["awg_ch"], f["scope_ch"], "Hold"):
            return
        try:
            it_hold, u_hold, src = self._hold_drive(self.holditer_var.get())
        except RuntimeError as e:
            return messagebox.showerror("Hold", str(e))
        native = bool(self.keepnative_var.get())     # main thread, as do_bench
        self.run_worker(lambda: self._hold_work(int(f["runs"]), f["gap_s"],
                                                int(f["awg_ch"]),
                                                int(f["scope_ch"]),
                                                int(f["repeats"]), f["wait"],
                                                native, it_hold, u_hold, src),
                        "hold: re-measuring...")

    def _hold_drive(self, spec, s=None):
        """The 'iter' box -> (iteration, drive, source). Blank means the
        session's current drive (None, None, ...). A number means that
        iteration's stored drive_<stem>_iNN.csv beside the state -- the
        file the loop actually wrote and played -- so an earlier drive can
        be re-measured without Init, which would restart the campaign at
        i00 under the same names and discard the loop's memory."""
        spec = spec.strip()
        s = s or self.session
        if not spec:
            return None, None, "the current drive"
        try:
            it = int(spec)
        except ValueError:
            raise RuntimeError(f"'iter' must be an iteration number, not "
                               f"{spec!r} (blank = the current drive)")
        run_dir = os.path.dirname(s.state_path)
        path = os.path.join(run_dir, f"drive_{s.stem}_i{it:02d}.csv")
        if it == 0 and not os.path.exists(path):
            legacy = os.path.join(run_dir, f"drive_{s.stem}_iter0.csv")
            if os.path.exists(legacy):
                path = legacy
        if not os.path.exists(path):
            have = sorted(int(m.group(1)) for p in glob.glob(
                os.path.join(run_dir, f"drive_{s.stem}_i[0-9]*.csv"))
                if (m := re.search(r"_i(\d+)\.csv$", p)))
            raise RuntimeError(
                f"no stored drive for iteration {it} of {s.stem}: "
                f"{os.path.basename(path)} is not beside the state. "
                f"Stored: {', '.join(map(str, have)) or 'none'}.")
        u = pd.read_csv(path, comment="#").iloc[:, 1].to_numpy(float)
        if len(u) != len(s.t):
            raise RuntimeError(f"{os.path.basename(path)} has {len(u)} "
                               f"points; this session's grid has {len(s.t)}")
        return it, u, os.path.basename(path)

    def _show_held(self, it):
        """Main thread, after a hold run: make the held iteration visible.

        The plots draw hold runs only for iterations the Iterations box
        selects, and only with 'runs' ticked. Holding the CURRENT iteration
        always fell inside the default selection (the last two); holding an
        earlier one does not, and its runs were stored, listed in the Table,
        and never drawn. So: tick 'runs' if it is off, add the iteration to
        the box if it is not selected, say what changed, then redraw."""
        s = self.session
        changed = []
        if not self.showruns_var.get():
            self.showruns_var.set(True)
            changed.append("ticked 'runs'")
        avail = sorted({sn["it"] for sn in s.snapshots})
        spec = self.itersel_var.get().strip()
        picked = self._pick_iters(spec, avail, 2, lambda m: None)
        if it not in picked:
            if spec.lower() in ("all", "*"):
                pass                      # cannot happen: 'all' picks it
            elif spec:
                spec = f"{spec},{it}"
            else:                         # blank = the last two; keep those
                spec = ",".join(str(k) for k in avail[-2:] if k != it) + f",{it}"
            self.itersel_var.set(spec.strip(","))
            changed.append(f"Iterations box -> '{self.itersel_var.get()}'")
        if changed:
            self.log(f"hold: {' and '.join(changed)} so the held runs of "
                     f"iteration {it} draw alongside the rest -- change it "
                     f"back when done")
        self._redraw_iterations()

    def _hold_work(self, runs, gap_s, awg_ch, scope_ch, repeats,
               wait_s, keep_native=False, it_hold=None, u_hold=None,
               src="the current drive"):
        s = self.session
        s.scope_channels = chanmap.record(self.cfg)
        print("scope channels: " + chanmap.describe(self.cfg).replace("\n", " | "))
        # the drive to play and the iteration its runs are tagged to: the
        # session's current one, or an earlier stored one (see _hold_drive)
        it = s.iteration if it_hold is None else int(it_hold)
        u = s.u if u_hold is None else np.asarray(u_hold, float)
        awg, scope = self._connect_pair()
        played = False
        switched_on = False
        try:
            problems, notes = ilc_bench.check_awg_channel(
                awg, awg_ch, full_scale=s.full_scale,
                expect_period=len(s.t) * s.loop.dt)
            for note in notes:
                print("      ", note)
            acq = scope.get(":ACQuire:TYPE")
            if not acq.upper().startswith("HRES"):
                problems.append(f"scope is in {acq}; hold uses the same "
                                f"averaged-HRES scheme as the loop")
            if problems:
                print("Setup problems:")
                for p in problems:
                    print("  !", p)
                print("REFUSING -- fix the setup first.")
                return
            # The same guard the bench loop runs before every upload. Hold
            # used to skip it, and with seed drives Hold is the path that
            # plays hand-edited samples (1 Sep 2026).
            rep = s.loop.check(u)
            print("       limit check:", rep)
            if not rep:
                print("REFUSING to play this drive: it fails the limit check "
                      "(an edited seed past the idle cap or the rails?). Fix "
                      "the file and Init again.")
                return

            wname = f"{s.stem}_i{it:02d}"
            n, frac = ilc_bench.upload_drive(awg, awg_ch, wname, u,
                                             s.full_scale)
            played = True
            print(f"hold: uploaded {wname} ({n} pts, {100*frac:.1f}% of DAC "
                  f"range) from {src} -- this drive will NOT be updated"
                  + (f"; the session stays at iteration {s.iteration}"
                     if it != s.iteration else ""))
            ok, switched_on = self._ensure_output_on(awg, awg_ch)
            if not ok:
                return

            prior = [sn["run"] for sn in s.snapshots
                     if sn["it"] == it and sn.get("run") is not None]
            r0 = max(prior, default=0) + 1
            print(f"hold: {runs} run(s) of iteration {it}, {gap_s:g} s gap, "
                  f"starting at r{r0}")
            t_start = time.time()
            for j in range(runs):
                if self.stop_evt.is_set():
                    print("hold stopped by user")
                    break
                r = r0 + j
                nat = (os.path.join(os.path.dirname(s.state_path),
                                    f"meas_{s.stem}_i{it:02d}_r{r:02d}"
                                    f"_native.npz")
                       if keep_native else None)
                stk = {}                     # the shots behind the average
                y = self._bench_capture(scope, scope_ch, s.t, s.t_off,
                                        repeats, wait_s, native_path=nat,
                                        aux_store=stk)
                np.save(os.path.join(os.path.dirname(s.state_path),
                                     f"meas_{s.stem}_i{it:02d}_r{r:02d}.npy"),
                        y)
                m = s.loop.metrics(y)
                sn = dict(it=it, y=y, m=m, u=u, run=r, t_wall=time.time())
                s.snapshots.append(sn)
                print(f"  r{r} (+{fmt_span(time.time() - t_start)}): error "
                      f"peak {m['peak_err_hv']:7.1f} V   "
                      f"rms {m['rms_err_hv']:6.2f} V")
                m.update(self._noise_floor(
                    stk["capture"].grid[f"CH{scope_ch}"]))
                st = self._measurement_stats(
                    stk["capture"].grid[f"CH{scope_ch}"],
                    os.path.join(os.path.dirname(s.state_path),
                                 f"meas_{s.stem}_i{it:02d}_r{r:02d}.npy"),
                    scope, scope_ch)
                m.update(self._stats_metrics(st))
                sn["stats"] = st
                self.msgs.put(("call", lambda it=it: self._show_held(it)))
                if j < runs - 1 and gap_s > 0:
                    # sleep in slices so Stop stays responsive
                    t_end = time.time() + gap_s
                    while time.time() < t_end:
                        if self.stop_evt.is_set():
                            break
                        time.sleep(min(0.2, max(t_end - time.time(), 0.01)))
            print(f"hold finished: state untouched, iteration still {it}")
        finally:
            if played or switched_on:
                try:
                    awg.set_output(awg_ch, False)
                    print(f"CH{awg_ch} output OFF (end of hold)")
                    self._bench_note(f"CH{awg_ch} output OFF (end of hold)")
                except Exception as e:
                    print(f"could not switch CH{awg_ch} output off: {e}")
            awg.close()
            scope.close()
            print("instruments closed")

    # ------------------------------------------------------------ bench run
    def do_bench(self):
        if self.pair_var.get():
            return self._do_bench_pair()
        if self.session is None:
            return messagebox.showerror("Bench", "load or init a session first")
        try:
            f = self._floats(awg_ch=self.awgch_var, scope_ch=self.scopech_var,
                             iterations=self.iters_var, repeats=self.repeats_var,
                             wait=self.wait_var)
            cfg = self._gather_settings()
        except RuntimeError as e:
            return messagebox.showerror("Bench", str(e))
        if not self._wiring_ok(f["awg_ch"], f["scope_ch"], "Bench"):
            return
        drive_ch = self._drive_scope_ch(f["awg_ch"])
        # the ticks read HERE, on the main thread: the lambda runs on the
        # worker, where a Tk variable must not be touched (it raised "main
        # thread is not in main loop" whenever Tk was pumped by update())
        skip, native = bool(self.skip_var.get()), bool(self.keepnative_var.get())
        self.run_worker(lambda: self._bench_work(cfg, int(f["awg_ch"]),
                                                 int(f["scope_ch"]),
                                                 int(f["iterations"]),
                                                 int(f["repeats"]), f["wait"],
                                                 skip, native, drive_ch),
                        "bench loop running...")

    def _bench_modules(self):
        if self._modules is None:
            # renamed keysight-scope-grab on GitHub; old checkouts keep the
            # old folder name -- ilc_bench tries both (SCOPE_GRAB overrides)
            sg = ilc_bench.find_scope_grab(SIBLINGS)
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

    def _wiring_ok(self, awg_ch, scope_ch, what, need_drive=False):
        """The bench reads the DRIVE back from the CmdXn channel of the role
        map (GEN: scope channel <awg_ch>) and the monitor from <scope_ch>:
        Auto-set sets both verticals, the alignment check correlates the
        drive channel against the record, Measure FRF divides monitor by
        drive. The same channel for both is not a configuration, so refuse
        it with the reason rather than let the alignment check pass on a
        monitor or an FRF come out as Y/Y. No readback channel at all is
        allowed for the loop (the alignment check is then skipped, said in
        the log) but not for an FRF (need_drive)."""
        drive_ch = self._drive_scope_ch(awg_ch)
        if drive_ch is not None and int(drive_ch) == int(scope_ch):
            messagebox.showerror(what, (
                f"scope ch {scope_ch} is also the drive's channel: the bench "
                f"reads the AWG's output back from scope CH{drive_ch} "
                f"(CmdX{int(awg_ch)} in the Scope channels map; GEN: the "
                f"AWG channel's number). Put the monitor on another channel, "
                f"or change the map."))
            return False
        mon_role = {r: c for c, (r, _n) in chanmap.roles(self.cfg).items()}
        want = f"MonX{int(awg_ch)}"
        if self.channel_var.get() != "GEN" and mon_role.get(want) not in (None, int(scope_ch)):
            messagebox.showerror(what, (
                f"scope ch {scope_ch} is not the channel the Scope channels "
                f"map gives {want} (CH{mon_role[want]}). Fix one of them."))
            return False
        if drive_ch is None and need_drive:
            messagebox.showerror(what, (
                f"no scope channel carries CmdX{int(awg_ch)}: an FRF is "
                f"monitor / drive and needs the drive read back. Assign the "
                f"role on the Measure tab (tee the AWG output to that "
                f"channel)."))
            return False
        return True

    def _connect_pair(self):
        """Open both instruments, or neither.

        A scope that does not answer -- Scope Grab still holding its VISA
        session is the usual reason -- must not leave the generator's session
        orphaned in this process. `make_awg` would then hand the next attempt
        a SECOND session to the same USB resource, and that failure reads as
        'the AWG is not there' rather than 'close Scope Grab', which is the
        wrong thing to go and check. Every bench worker connects through
        here so the two sessions are always opened as a pair.
        """
        scopemod, awgmod = self._bench_modules()
        awg = ilc_bench.make_awg(awgmod)
        print("AWG:  ", awg.connect())
        scope = ilc_bench.make_scope(scopemod)
        try:
            print("Scope:", scope.connect())
        except Exception as e:
            awg.close()
            raise RuntimeError(
                f"{e}\nNothing was changed, and the AWG session was closed "
                f"again -- nothing is left open. If Scope Grab (or any other "
                f"program) is running it holds the scope's VISA session; "
                f"close it and try again. Otherwise check the scope's power "
                f"and rear-panel USB.")
        return awg, scope

    def _bench_work(self, cfg, awg_ch, scope_ch, iterations, repeats, wait_s,
                    skip, keep_native=False, drive_ch=None):
        s = self.session
        self._apply_settings(cfg)
        # the map in force travels with the data: into the state, and the log
        s.scope_channels = chanmap.record(self.cfg)
        print("scope channels: " + chanmap.describe(self.cfg).replace("\n", " | "))
        if drive_ch is None:
            print(f"  NOTE: no CmdX{awg_ch} readback channel -- the trigger "
                  f"alignment is not verified on this run")
        _, awgmod = self._bench_modules()
        # One id per bench run. The first measurement of a run differs from
        # the last one of the previous run by whatever the chain did while
        # the output was off -- the model check must not read that as the
        # chain's answer to an update, nor the plateau as the loop's doing.
        self._run_id = time.time()

        limit = getattr(awgmod, "MAX_ARB_NAME", NAME_LIMIT)
        if len(s.stem) + 4 > limit:
            raise RuntimeError(f"stem {s.stem!r} + '_iNN' is past the "
                               f"{limit}-char name cap")

        awg, scope = self._connect_pair()
        uploaded_any = False
        switched_on = False
        try:
            problems, notes = ilc_bench.check_awg_channel(
                awg, awg_ch, full_scale=s.full_scale,
                expect_period=len(s.t) * s.loop.dt)
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

            ok, switched_on = self._ensure_output_on(awg, awg_ch)
            if not ok:
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
                if k == k0 and drive_ch is not None:
                    time.sleep(0.5)
                    ilc_bench.verify_alignment(scope, drive_ch, u, s.t, s.t_off,
                                               wait_s)
                nat = (os.path.join(RUN_DIR, f"meas_{wname}_native.npz")
                       if keep_native else None)
                stk = {}                     # the shots behind the average
                y = self._bench_capture(scope, scope_ch, s.t, s.t_off,
                                        repeats, wait_s, native_path=nat,
                                        aux_store=stk)
                np.save(os.path.join(RUN_DIR, f"meas_{wname}.npy"), y)
                m = s.loop.metrics(y)
                m["model"] = cfg["desc"]
                print(f"         error: peak {m['peak_err_hv']:7.1f} V   "
                      f"rms {m['rms_err_hv']:6.2f} V   ({m['peak_pct']:.2f}% FS)")
                m.update(self._noise_floor(
                    stk["capture"].grid[f"CH{scope_ch}"]))
                st = self._measurement_stats(stk["capture"].grid[f"CH{scope_ch}"],
                                             os.path.join(RUN_DIR, f"meas_{wname}.npy"),
                                             scope, scope_ch)
                m.update(self._stats_metrics(st))
                m.update(self._model_check_line(k, u, y))
                s.snapshots.append(dict(it=k, y=y, m=m, u=u, t_wall=time.time(),
                                        run_id=self._run_id, stats=st))
                u_now = u
                self.msgs.put(("call",
                               lambda u=u_now, y=y, m=m, k=k:
                               (self._refresh_summary(),
                                self._show_iteration(u, y, m, k))))
                if k < k0 + iterations and not self.stop_evt.is_set():
                    u_new = s.loop.update(u, y)
                    s.loop.history[-1]["model"] = cfg["desc"]
                    s.loop.history[-1]["update_rms"] = ilc.update_rms(u, u_new)
                    s.loop.history[-1]["run_id"] = self._run_id
                    s.loop.history[-1]["update_below_lsb"] = dg.update_vs_step(u_new - u)["below"]
                    self._plateau_line()
                    u = u_new
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
                    self._bench_note(f"CH{awg_ch} output OFF (end of run)")
                except Exception as e:
                    print(f"could not switch CH{awg_ch} output off: {e}")
            # both closes leave the shared ResourceManager standing
            awg.close()
            scope.close()
            print("instruments closed")
        print("\n" + s.loop.report())

    # ------------------------------------------------------------ pair mode
    # X1 and X2 learned side by side, the way the batch trains them and the
    # way the ramps play in the experiment: two sessions, each with its own
    # Model column (the two Trek chains are different amplifiers), one bench
    # loop that plays both drives and reads both monitors from the same
    # frozen acquisitions. Everything single-channel (Upload, Manual step,
    # Corrections, Measure FRF, the line ripple) acts on the SHOWN session.

    def _pair_col(self, k):
        """The Model column of chain k: column 1 is X1's, column 2 X2's."""
        return self._col1 if k == "X1" else self._col2

    def _col_session(self, c):
        """The session a Model column belongs to: X1's or X2's in pair mode,
        the loaded one for column 1 otherwise."""
        if self.pair is not None:
            return self.pair.get(c.key)
        return self.session if c is self._col1 else None

    def _pair_key_of(self, s):
        """"X1" / "X2" when s is one of the loaded pair, else ""."""
        if self.pair is None or s is None:
            return ""
        return next((k for k, ps in self.pair.items() if ps is s), "")

    @staticmethod
    def _pair_path(path, what="file"):
        """X2's file from X1's, by name: the LAST 'X1' in the file name
        becomes 'X2' (target_S47X1.csv -> target_S47X2.csv,
        drive_S47X1.state.npz -> drive_S47X2.state.npz). Refuses a name with
        no X1 in it and an X2 file that is not there."""
        d, base = os.path.split(path)
        i = base.rfind("X1")
        if i < 0:
            raise RuntimeError(
                f"pair mode: the {what} {base!r} has no 'X1' in its name, so "
                f"X2's cannot be found by name. Pick the X1 file of the pair "
                f"(e.g. target_S47X1.csv, drive_S47X1.state.npz), or untick "
                f"Pair.")
        p2 = os.path.join(d, base[:i] + "X2" + base[i + 2:])
        if not os.path.exists(p2):
            raise RuntimeError(
                f"pair mode: X2's {what} is not there:\n{p2}\n(derived from "
                f"{base} by X1 -> X2)")
        return p2

    @staticmethod
    def _check_pair(s1, s2):
        """Two sessions that can play as a pair: EO1 and EO2, one grid."""
        if s1.channel != "EO1" or s2.channel != "EO2":
            raise RuntimeError(
                f"pair mode: X1's state is on {s1.channel} and X2's on "
                f"{s2.channel}; a pair is EO1 + EO2")
        if len(s1.t) != len(s2.t) or abs(float(s1.loop.dt) - float(s2.loop.dt)) > 1e-12:
            raise RuntimeError(
                f"pair mode: X1 ({len(s1.t)} pts, dt {s1.loop.dt*1e6:g} us) and "
                f"X2 ({len(s2.t)} pts, dt {s2.loop.dt*1e6:g} us) are not on one "
                f"grid -- they play together, from one trigger")

    # -- the X2 column of the Model tab ----------------------------------
    def _build_model_column_2(self):
        """The X2 (EO2) column: a parallel set of variables (model_var_2,
        pgain_var_2, ptau_var_2, pfn_var_2, pzeta_var_2, gamma_var_2,
        fcut_var_2, notch_var_2, frf_var_2, fuse_var_2, fmax_var_2) with the
        same rungs, the same show-only-the-rung's-fields behaviour, its own
        'Replace values with a fit...' and Show FRF. The grey notes are
        column 1's; the X2 column packs under it."""
        vf = ttk.LabelFrame(self._model_row, text="X2 (EO2) inverse model",
                            padding=3)
        self.model_var_2 = tk.StringVar(value=self.model_var.get())
        r0 = ttk.Frame(vf); r0.grid(row=0, column=0, columnspan=3, sticky="ew")
        ttk.Label(r0, text="Model").pack(side="left")
        mc = ttk.Combobox(r0, textvariable=self.model_var_2, state="readonly",
                          width=30, values=[l for l, _ in MODEL_LABELS])
        mc.pack(side="left", padx=(2, 0))
        mc.bind("<<ComboboxSelected>>", lambda e: self._on_model_change_2())

        r1 = ttk.Frame(vf); r1.grid(row=1, column=0, columnspan=3,
                                    sticky="ew", pady=1)
        self.pgain_var_2 = tk.StringVar()
        self.ptau_var_2 = tk.StringVar()
        self.pfn_var_2 = tk.StringVar()
        self.pzeta_var_2 = tk.StringVar()
        pv = {"gain": self.pgain_var_2, "tau": self.ptau_var_2,
              "fn": self.pfn_var_2, "zeta": self.pzeta_var_2}
        entries, labels = {}, {}
        for lab, key, w in (("gain", "gain", 7), ("tau us", "tau", 6),
                            ("fn Hz", "fn", 6), ("zeta", "zeta", 6)):
            lw = ttk.Label(r1, text=lab)
            lw.pack(side="left", padx=(0, 2))
            e = ttk.Entry(r1, textvariable=pv[key], width=w)
            e.pack(side="left", padx=(0, 8))
            entries[key], labels[key] = e, lw

        r2 = ttk.Frame(vf); r2.grid(row=2, column=0, columnspan=3,
                                    sticky="ew", pady=1)
        self.gamma_var_2 = tk.StringVar(value="0.6")
        self.fcut_var_2 = tk.StringVar(value="5000")
        ttk.Label(r2, text="gamma").pack(side="left", padx=(0, 2))
        ttk.Entry(r2, textvariable=self.gamma_var_2, width=5).pack(
            side="left", padx=(0, 8))
        fl = ttk.Label(r2, text="f_cut Hz")
        fl.pack(side="left", padx=(0, 2))
        fe = ttk.Entry(r2, textvariable=self.fcut_var_2, width=7)
        fe.pack(side="left", padx=(0, 6))

        r3 = ttk.Frame(vf); r3.grid(row=3, column=0, columnspan=3,
                                    sticky="ew", pady=(2, 1))
        b = ttk.Button(r3, text="Replace values with a fit...",
                       command=lambda: self.do_fit_dialog(self._col2))
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)

        self.frf_var_2 = tk.StringVar()
        self._path_row(vf, 4, "FRF", self.frf_var_2,
                       lambda: self._browse(self.frf_var_2, "FRF CSV",
                                            "frf_*.csv", RUN_DIR))
        r4 = ttk.Frame(vf); r4.grid(row=5, column=0, columnspan=3, sticky="ew")
        self.fuse_var_2 = tk.StringVar(value="50e3")
        self.fmax_var_2 = tk.StringVar(value="75e3")
        l1 = ttk.Label(r4, text="full strength to Hz")
        fu = ttk.Entry(r4, textvariable=self.fuse_var_2, width=7)
        l2 = ttk.Label(r4, text="taper to zero at Hz")
        fm = ttk.Entry(r4, textvariable=self.fmax_var_2, width=7)
        taper = ((l1, dict(side="left")), (fu, dict(side="left", padx=(2, 8))),
                 (l2, dict(side="left")), (fm, dict(side="left", padx=2)))
        for w, opts in taper:
            w.pack(**opts)
        b = ttk.Button(r4, text="Show FRF",
                       command=lambda: self.do_show_frf(self._col2))
        b.pack(side="right")
        self._actions.append(b)

        rn = ttk.Frame(vf); rn.grid(row=6, column=0, columnspan=3,
                                    sticky="ew", pady=1)
        self.notch_var_2 = tk.StringVar()
        ttk.Label(rn, text="notch Hz").pack(side="left", padx=(0, 2))
        ttk.Entry(rn, textvariable=self.notch_var_2, width=18).pack(
            side="left", padx=(0, 6))
        vf.columnconfigure(1, weight=1)
        self._col2 = types.SimpleNamespace(
            key="X2", frame=vf, model_var=self.model_var_2, param_vars=pv,
            param_entries=entries, param_labels=labels,
            gamma_var=self.gamma_var_2, fcut_var=self.fcut_var_2,
            fcut_label=fl, fcut_entry=fe, band_note=None,
            notch_var=self.notch_var_2, frf_var=self.frf_var_2,
            fuse_var=self.fuse_var_2, fmax_var=self.fmax_var_2,
            fuse_entry=fu, fmax_entry=fm, taper_widgets=taper)
        self._update_model_fields(self._col2)

    def _remember_fields_2(self):
        """The X2 column into the field memory under EO2 -- the same store
        and the same keys column 1 writes for its channel, plus the rung
        and band numbers column 1 keeps in the config instead."""
        c = self._col2
        if c is None or self._mem_model_2 is None:
            return
        m = self._field_mem.setdefault("EO2", {})
        m["frf"] = c.frf_var.get().strip()
        m["notch"] = c.notch_var.get().strip()
        m.setdefault("models", {})[self._mem_model_2] = {
            k: c.param_vars[k].get().strip() for k in self._MEM_KEYS}
        m.update(model=c.model_var.get(), gamma=c.gamma_var.get().strip(),
                 f_cut=c.fcut_var.get().strip(), f_use=c.fuse_var.get().strip(),
                 f_max=c.fmax_var.get().strip())

    def _recall_column_2(self):
        """The X2 column from EO2's field memory: what it last held, or a
        blank column (the prior-leak guard) and X2's own wide-probe FRF."""
        c = self._col2
        m = self._field_mem.get("EO2") or {}
        lab = m.get("model")
        c.model_var.set(lab if lab in LABEL2KEY else self.model_var.get())
        key = self._model_key(c)
        self._recall_params("EO2", key, blank_if_new=True, c=c)
        c.notch_var.set(m.get("notch", ""))
        for var, name, dflt in ((c.gamma_var, "gamma", "0.6"),
                                (c.fcut_var, "f_cut", "5000"),
                                (c.fuse_var, "f_use", "50e3"),
                                (c.fmax_var, "f_max", "75e3")):
            var.set(m.get(name) or dflt)
        frf = m.get("frf", "")
        parts = [p.strip() for p in frf.split(";") if p.strip()]
        if not (parts and all(glob.glob(p) if ("*" in p or "?" in p)
                              else os.path.exists(p) for p in parts)):
            d = os.path.join(RUN_DIR, CH_DEFAULTS["EO2"]["frf"])
            frf = d if os.path.exists(d) else ""
        c.frf_var.set(frf)
        self._mem_model_2 = key
        self._update_model_fields(c)

    def _on_model_change_2(self):
        c = self._col2
        key = self._model_key(c)
        if key != self._mem_model_2:
            self._remember_fields_2()
            had = self._recall_params("EO2", key, blank_if_new=False, c=c)
            self._mem_model_2 = key
            if had:
                shown = ", ".join(f"{k} {c.param_vars[k].get()}"
                                  for k in PARAMS_FOR[key]
                                  if c.param_vars[k].get().strip())
                self.log(f"X2 {KEY2LABEL[key]}: recalled {shown or 'blank boxes'}")
        self._update_model_fields(c)

    def _fill_column(self, c, s):
        """A loaded state's model into a column (do_load does column 1)."""
        c.gamma_var.set(f"{s.loop.gamma:g}")
        c.fcut_var.set(f"{s.loop.f_cut:g}")
        c.notch_var.set(", ".join(f"{f0:g}/{bw:g}" for f0, bw in s.loop.notches))
        self._set_param_entries(s.loop.plant, c)
        if s.model_key:
            c.model_var.set(KEY2LABEL.get(s.model_key, c.model_var.get()))
            if s.model_key == "frf" and s.frf_path:
                c.frf_var.set(s.frf_path)
                c.fuse_var.set(f"{s.frf_use:g}")
                c.fmax_var.set(f"{s.frf_max:g}")
                if not os.path.exists(s.frf_path):
                    self.log(f"NOTE: {c.key}'s recorded FRF file is missing: "
                             f"{s.frf_path}")
                elif s.frf_error:
                    self.log(f"NOTE: {c.key}'s recorded FRF did NOT load, so "
                             f"the loop is resuming without it:\n      "
                             f"{s.frf_error}")
        self._update_model_fields(c)

    # -- the tick and the shown radio --------------------------------------
    def _on_pair_toggle(self, quiet=False):
        """Pair tick: on, the panel is EO1's in column 1 and EO2's in the X2
        column, the shown radio appears and Load/Init act on both. Off: the
        X2 column and the radio go, the X1 session stays loaded, and the
        panel is the single-channel one it was."""
        if self.pair_var.get():
            if self.channel_var.get() != "EO1":
                self.channel_var.set("EO1")
                self._on_channel_change()
            self._channel_cb.configure(state="disabled")
            if self._col2 is None:
                self._build_model_column_2()
            self._recall_column_2()
            self._col2.frame.pack(fill="x", pady=(4, 0))
            self._model_frame1.configure(text="X1 (EO1) inverse model")
            self._shown_frame.pack(side="left", padx=(0, 8),
                                   before=self._iter_label)
            self.shown_var.set("X1")
            self.bench_btn.configure(text="Run bench loop (X1 + X2)")
            self._stem_note.configure(text=STEM_NOTE_PAIR)
            if not quiet:
                self.log("pair mode: State and Target take the X1 file (X2's "
                         "is the same name with X2); Load state or Init "
                         "campaign acts on both. Model: X1 left, X2 right.")
        else:
            self._remember_fields_2()
            if self._col2 is not None:
                self._col2.frame.pack_forget()
            self._model_frame1.configure(text=MODEL_FRAME_TEXT)
            self._shown_frame.pack_forget()
            self._channel_cb.configure(state="readonly")
            self.bench_btn.configure(text="Run bench loop")
            self._stem_note.configure(text=STEM_NOTE)
            had = self.pair
            self.pair = None
            self.shown_var.set("X1")
            if had is not None:
                if self.session is not had["X1"]:
                    self.session = had["X1"]
                    self._sync_shown()
                else:
                    self._apply_channel_defaults()
                    self._refresh_summary()
            if not quiet:
                self.log("pair mode off: single channel"
                         + (f"; {had['X1'].stem} stays loaded" if had else ""))
        self._refresh_run_state()
        if not self.busy:
            self.status.configure(text=self._hint())

    def _on_shown_change(self):
        if self.pair is None:
            return
        s = self.pair.get(self.shown_var.get())
        if s is None or s is self.session:
            return
        self.session = s
        self._sync_shown()

    def _sync_shown(self):
        """Everything that follows the shown session: the wiring fields
        (Upload, Measure FRF and the line ripple act on the shown chain),
        the Corrections box, the line switch, the summary, the plots."""
        s = self.session
        self._apply_channel_defaults(s.channel, wiring_only=True)
        self._corr_pending = [dict(L) for L in s.corr_layers]
        if s.line_fit is not None:
            self._line_fit = s.line_fit
        self.line_on_var.set(s.loop.line is not None)
        self._refresh_corr_list()
        self._refresh_line_status()
        self._replot_corr_quiet()
        self._refresh_summary()
        self._show_session()

    # -- Load and Init on both -------------------------------------------
    def _ensure_pair_ui(self):
        """The tick set without its command (a script, a test): build what
        the tick builds before anything reads the X2 column."""
        if self._col2 is None or not self._col2.frame.winfo_manager():
            self._on_pair_toggle(quiet=True)

    def _load_pair(self, path):
        self._ensure_pair_ui()
        try:
            p2 = self._pair_path(path, "state file")
            s1, s2 = load_session(path), load_session(p2)
            self._check_pair(s1, s2)
        except Exception as e:
            return messagebox.showerror("Load state", str(e))
        self.pair = {"X1": s1, "X2": s2}
        self.shown_var.set("X1")
        self._take_session(s1, path)          # column 1, Target box, X1's plots
        self.stem_var.set(s1.stem[:-2] if s1.stem.endswith("X1") else s1.stem)
        self._remember_fields_2()
        self._fill_column(self._col2, s2)
        self._mem_model_2 = self._model_key(self._col2)
        self.log(f"loaded {p2}")
        self.log(f"  {s2.channel} iteration {s2.iteration}, stem {s2.stem}, "
                 f"gamma {s2.loop.gamma:g}, f_cut {s2.loop.f_cut/1e3:g} kHz "
                 f"-- the X2 of the pair (shown: X1)")
        self.log(f"  plant: {s2.loop.plant}")
        recall_snapshots(s2)
        if s2.snapshots:
            its = sorted({sn["it"] for sn in s2.snapshots})
            self.log(f"  recalled {len(its)} stored X2 measurement(s): "
                     f"iterations {', '.join(str(i) for i in its)}" + self._pd_recalled(s2))
        self._refresh_run_state()
        self._refresh_summary()

    def _init_pair(self):
        """Init campaign on both chains: X1 from the Target box and column 1,
        X2 from the target of the same name with X2 and the X2 column. The
        stem gets X1 / X2 appended. Nothing is written until both are built
        and checked."""
        self._ensure_pair_ui()
        t1 = self.target_var.get().strip()
        if not os.path.exists(t1):
            return messagebox.showerror("Init", f"target not found: {t1!r}")
        base = self.stem_var.get().strip()
        if len(base) + 2 + 4 > NAME_LIMIT:
            return messagebox.showerror(
                "Init", f"name stem {base!r} is {len(base)} chars; with X1/X2 "
                        f"and '_i00' that is {len(base) + 6}, past the "
                        f"{NAME_LIMIT}-char cap the 4063B wedges beyond. In "
                        f"pair mode the stem is at most {NAME_LIMIT - 6} "
                        f"characters.")
        try:
            targets = {"X1": t1, "X2": self._pair_path(t1, "target")}
            seeds = {"X1": self.seed_var.get().strip(), "X2": ""}
            if seeds["X1"]:
                seeds["X2"] = self._pair_path(seeds["X1"], "seed drive")
            fc = self._floats(t_offset_us=self.toff_var, full_scale=self.fs_var)
        except RuntimeError as e:
            return messagebox.showerror("Init", str(e))
        stems = {k: base + k for k in PAIR_KEYS}
        paths = {k: os.path.join(RUN_DIR, f"drive_{stems[k]}.state.npz")
                 for k in PAIR_KEYS}
        there = [p for p in paths.values() if os.path.exists(p)]
        if there and not messagebox.askyesno(
                "A campaign already uses this stem",
                "\n".join(there) + "\nalready exist" + ("s" if len(there) == 1
                                                        else "") + ".\n\n"
                "Init OVERWRITES the loops' memory: the current drives, the "
                "iteration counters, and the error histories behind the "
                "Convergence tab. The per-iteration files survive this press, "
                "but the new pair restarts at i00 under the SAME names.\n\n"
                "To keep iterating use Load state instead; to start fresh AND "
                "keep the old pair, change the name stem.\n\nDiscard the "
                "existing campaign(s)?"):
            return
        built = {}
        for k in PAIR_KEYS:
            r = self._init_compute(k, targets[k], self._pair_col(k),
                                   self.shotgain_var.get() if k == "X1" else "",
                                   seeds[k])
            if r is None:
                return
            built[k] = r
        if len(built["X1"]["t"]) != len(built["X2"]["t"]) or \
                abs(built["X1"]["dt"] - built["X2"]["dt"]) > 1e-12:
            return messagebox.showerror(
                "Init", f"the X1 and X2 targets are not on one grid "
                        f"({len(built['X1']['t'])} and {len(built['X2']['t'])} "
                        f"points) -- they play together, from one trigger")
        os.makedirs(RUN_DIR, exist_ok=True)
        pair = {}
        for k in PAIR_KEYS:
            r = built[k]
            s, out, gui = self._init_commit(r, stems[k], paths[k], fc)
            if k == "X1" and self._line_fit is not None:
                s.line_fit = self._line_fit
                self._apply_line(s, self.line_on_var.get())
            save_session(s)
            pair[k] = s
            self._set_param_entries(r["plant"], self._pair_col(k))
            self._init_log(r, s, out, gui)
        if self._line_fit is not None:
            self.log("  the panel's line-ripple fit went into X1 only (it was "
                     "measured on one monitor); show X2 and measure its own")
        self._corr_pending = []
        self.pair = pair
        self.shown_var.set("X1")
        self.session = pair["X1"]
        self.state_var.set(paths["X1"])
        self._apply_channel_defaults("EO1")
        self._mem_model_2 = self._model_key(self._col2)
        self._refresh_corr_list()
        self._refresh_line_status()
        self._refresh_summary()
        self._show_session(select_tab=True)
        self._save_config()

    def _init_compute(self, k, target, c, shot_txt, seed):
        """One chain of a pair Init, built but not written: the same steps
        and refusals as do_init's, from column c. None after a refusal (the
        dialog has said why)."""
        title = f"Init {k}"
        chname = PAIR_CHANNEL[k]
        try:
            f = self._floats(gamma=c.gamma_var, f_cut=c.fcut_var)
        except RuntimeError as e:
            return messagebox.showerror(title, str(e)) and None
        ch = CHANNELS[chname]
        try:
            t, v = run_ilc.load_target(target, ch.mon_scale)
        except (ValueError, OSError, KeyError) as e:
            return messagebox.showerror(title, str(e)) and None
        dt = float(np.median(np.diff(t)))
        mode = self._model_key(c)
        frf_rec = ("", 0.0, 0.0)
        if mode == "frf":
            fps = self._frf_paths(c)
            if len(fps) != 1:
                return messagebox.showerror(
                    title, f"the measured-FRF model needs exactly ONE file in "
                           f"{k}'s FRF field (it holds {len(fps)})") and None
            if not os.path.exists(fps[0]):
                return messagebox.showerror(title, f"FRF not found: {fps[0]!r}") and None
            try:
                fb = self._floats(f_use=c.fuse_var, f_max=c.fmax_var)
                if not 0 < fb["f_use"] < fb["f_max"]:
                    raise RuntimeError(f"the taper needs 0 < f_use < f_max "
                                       f"(got {fb['f_use']:g}/{fb['f_max']:g})")
                self._check_taper_band(fps[0], fb["f_use"], fb["f_max"])
            except RuntimeError as e:
                return messagebox.showerror(title, str(e)) and None
            frf_rec = (fps[0], fb["f_use"], fb["f_max"])
        seed_key = "static" if mode == "frf" else mode
        gain_box = c.param_vars["gain"]
        if not gain_box.get().strip() and shot_txt.strip():
            gain_box.set(shot_txt.strip())
            self.log(f"{k}: model gain was blank -- seeded from the first-shot gain")
        try:
            g_shot = self._first_shot_gain(shot_txt, c)
            params = self._entry_params(seed_key, strict=False, c=c)
            notches = self._notches(c)
        except RuntimeError as e:
            return messagebox.showerror(title, str(e)) and None
        if params is None:
            blank = [n for n in PARAMS_FOR[seed_key]
                     if not c.param_vars[n].get().strip()]
            return messagebox.showerror(
                title, f"{k}'s {KEY2LABEL[seed_key]} model has no value for "
                       f"{', '.join(blank)}.\n\nType it in the {k} column of "
                       f"the Model tab, or fill it with its 'Replace values "
                       f"with a fit...'.") and None
        plant = self._plant_from(params, dt)
        loop = ilc.Loop(plant=plant, target=v, dt=dt, channel=ch,
                        gamma=f["gamma"], f_cut=f["f_cut"], limits=ch.limits,
                        notches=notches)
        u = loop.first_shot(gain=g_shot)
        g_used = g_shot if g_shot is not None else plant.gain
        if seed:
            try:
                u = run_ilc.load_seed_drive(seed, t)
            except (ValueError, OSError, KeyError) as e:
                return messagebox.showerror(title, f"seed drive: {e}") and None
            seed = os.path.abspath(seed)
        rep = loop.check(u)
        if seed and not rep and not messagebox.askyesno(
                f"{k} seed drive fails the limit check",
                f"{rep}\n\nThe bench loop and Hold refuse to play a drive that "
                f"fails this check. Keep it anyway (fix the file, then Init "
                f"again)?"):
            return None
        return dict(k=k, chname=chname, target=target, t=t, v=v, dt=dt, ch=ch,
                    plant=plant, loop=loop, u=u, g_used=g_used, seed=seed,
                    rep=rep, mode=mode, frf_rec=frf_rec, seed_key=seed_key)

    def _init_commit(self, r, stem, state_path, fc):
        """Write one built chain: the i00 drive, its AWG-library copy, the
        Session (saved by the caller)."""
        out = os.path.join(RUN_DIR, f"drive_{stem}_i00.csv")
        how = (f"seed drive {os.path.basename(r['seed'])}, played as-is"
               if r["seed"] else "flat conversion, target / gain")
        outputs.write_awg_csv(out, r["t"], r["u"],
                              comment=f"{r['chname']} ILC iteration 0 "
                                      f"({how})\n{r['plant']}")
        wname = f"{stem}_i00"
        os.makedirs(AWG_WAVEFORMS, exist_ok=True)
        gui = outputs.write_bk_waveform(os.path.join(AWG_WAVEFORMS, wname + ".csv"),
                                        r["u"], wname, fc["full_scale"])
        s = Session(state_path=state_path, loop=r["loop"], t=r["t"], u=r["u"],
                    iteration=0, stem=stem, full_scale=fc["full_scale"],
                    t_off=fc["t_offset_us"] * 1e-6)
        s.model_key = r["mode"]
        s.frf_path, s.frf_use, s.frf_max = r["frf_rec"]
        s.seed_path = r["seed"]
        s.target_path = os.path.abspath(r["target"])
        s.scope_channels = chanmap.record(self.cfg)
        return s, out, gui

    def _init_log(self, r, s, out, gui):
        k, ch, plant, v, u = r["k"], r["ch"], r["plant"], r["v"], r["u"]
        self.log(f"init {k} ({r['chname']}, {KEY2LABEL[r['seed_key']]} seed "
                 f"from the {k} column): {plant}")
        self.log(f"{k}   target {np.ptp(v)*ch.mon_scale:.0f} V pk-pk over "
                 f"{r['t'][-1]*1e3:.2f} ms, {len(v)} points at {r['dt']*1e6:.3f} us"
                 f" ({os.path.basename(r['target'])})")
        if r["seed"]:
            self.log(f"{k}   first shot  : SEED drive {r['seed']} played as-is")
        else:
            self.log(f"{k}   first shot  : flat conversion (target / "
                     f"{r['g_used']:g}), drive peak {np.abs(u).max():.4f} V")
        if r["mode"] == "frf":
            self.log(f"{k}   model       : measured FRF "
                     f"{os.path.basename(r['frf_rec'][0])}, taper "
                     f"{r['frf_rec'][1]/1e3:g}-{r['frf_rec'][2]/1e3:g} kHz")
        self.log(f"{k}   limit check : {r['rep']}")
        self.log(f"{k}   wrote {out}")
        self.log(f"{k}         {gui}")
        self.log(f"{k}   state {s.state_path}")

    # -- the bench, on both chains -----------------------------------------
    def _pair_ready(self, what):
        """Main thread: the pair's wiring from the Scope channels map
        ({key: eomilc.channels.wiring}), or None after saying why not."""
        self._ensure_pair_ui()
        if self.pair is None:
            messagebox.showerror(what, "pair mode: Load state or Init campaign "
                                       "first -- either one acts on X1 and X2")
            return None
        w = {}
        for k in PAIR_KEYS:
            try:
                w[k] = chanmap.wiring(self.cfg, k)
            except ValueError as e:
                messagebox.showerror(what, f"{k}: {e}")
                return None
            if w[k]["drive_ch"] is not None and w[k]["drive_ch"] == w[k]["mon_ch"]:
                messagebox.showerror(what, f"{k}: scope CH{w[k]['mon_ch']} is both "
                                           f"the monitor and the drive readback")
                return None
        try:
            self._check_pair(self.pair["X1"], self.pair["X2"])
        except RuntimeError as e:
            messagebox.showerror(what, str(e))
            return None
        return w

    @contextlib.contextmanager
    def _prefixed(self, tag):
        """Worker side: every line printed inside starts with `tag` (X1 /
        X2), so the two chains' reports stay apart in one log."""
        w = _Prefixed(sys.stdout, tag)
        try:
            with contextlib.redirect_stdout(w):
                yield
        finally:
            w.flush()

    def _ensure_outputs_on(self, awg, chans, until="Switched OFF again when the run ends."):
        """_ensure_output_on for several channels with ONE question.
        Returns (proceed, the channels this call switched on); the caller's
        cleanup switches every played channel off regardless. `until` is
        the question's last line: when the outputs go OFF again."""
        off = [c for c in chans if not awg.is_on(c)]
        if not off:
            return True, []
        names = " and ".join(f"CH{c}" for c in off)
        if not self.ask_user(
                "Outputs are OFF",
                f"{names} {'outputs are' if len(off) > 1 else 'output is'} OFF, "
                f"and the measurement needs both chains driving.\n\nTurn "
                f"{names} ON and run?\n\n({until})"):
            print(f"run cancelled: {names} left OFF")
            return False, []
        done = []
        for c in off:
            awg.set_output(c, True)
            done.append(c)
            print(f"CH{c} output ON (confirmed in dialog)")
            if not awg.is_on(c):
                print(f"CH{c} did not switch on -- aborting")
                return False, done
        self._bench_note(f"{names} output ON")
        return True, done

    def _outputs_off(self, awg, chans, why):
        """Worker side: the played channels OFF, each on its own."""
        done = []
        for c in sorted(set(chans)):
            try:
                awg.set_output(c, False)
                done.append(c)
                print(f"CH{c} output OFF ({why})")
            except Exception as e:
                print(f"could not switch CH{c} output off: {e}")
        if done:
            self._bench_note(" and ".join(f"CH{c}" for c in done)
                             + f" output OFF ({why})")
            self._opt_mark_outputs("OFF", why)

    def _pair_capture(self, scope, ss, mon, repeats, wait_s, pd=None):
        """Both monitors from the same frozen acquisitions, shot by shot,
        with the offset dither on both: _bench_capture with X2's monitor as
        its aux channel (read between :SINGle and run, like X1's), which is
        ilc_batch.capture_monitors' recipe with this panel's progress bar
        and Stop. Returns {key: (mean, stack)} on X1's grid (the pair shares
        one -- _check_pair). `pd`: the PD's scope channel, read as one more
        aux channel (dithered the same way) and returned as "PD"."""
        s1 = ss["X1"]
        stk = {}
        self._bench_capture(scope, mon["X1"], s1.t, s1.t_off, repeats, wait_s,
                            aux=(mon["X2"],) + ((pd,) if pd is not None else ()),
                            aux_store=stk)
        grid = stk["capture"].grid
        out = {}
        for k in PAIR_KEYS:
            stack = np.asarray(grid[f"CH{mon[k]}"], float)
            out[k] = (ilc.averaged(stack), stack)
        if pd is not None:
            stack = np.asarray(grid[f"CH{pd}"], float)
            out["PD"] = (ilc.averaged(stack), stack)
        return out

    @staticmethod
    def _pd_recalled(s):
        """The load line's note on the PD files recalled with a session."""
        n = len({sn["it"] for sn in s.snapshots if sn.get("pd") is not None})
        return (f"; the PD with {n} (meas_{ilc_batch.pd_stem(s.stem)}PD_iNN.npy)" if n else "")

    def _pd_channel(self, mon):
        """Worker side: the PD's scope channel from the Scope channels map,
        or None (no PD role, the role on two channels, or a monitor's
        channel -- said in the log)."""
        try:
            pd = chanmap.channel_for(self.cfg, "PD")
        except ValueError as e:
            print(f"  PD not read: {e}")
            return None
        if pd is not None and pd in mon.values():
            print(f"  PD not read: CH{pd} is also a monitor's channel")
            return None
        return pd

    def _pair_pd(self, ss, caps, scope, pd):
        """Worker side, after a pair measurement: the PD saved beside the
        monitors as meas_<pair stem>PD_iNN.npy + its sidecar (ilc_batch.save_pd,
        the batch's recipe; NN the higher of the two iteration counts, as the
        batch numbers it), its line logged, and the mean and stats onto both
        sessions' newest snapshots."""
        s1 = ss["X1"]
        it = max(s.iteration for s in ss.values())
        vdiv, off = ilc_batch.read_vertical(scope, pd)
        deg = self._analyzer[0] if self._analyzer is not None else float("nan")
        r = ilc_batch.save_pd(RUN_DIR, ilc_batch.pd_stem(s1.stem) or s1.stem, it,
                              caps["PD"][0], caps["PD"][1], s1.loop.dt, vdiv=vdiv,
                              offset=off, scope_ch=pd, analyzer_deg=deg)
        print("         " + ilc_batch.pd_line(r)
              + (f" (analyzer {deg:+.1f} deg, as last read on the Hardware tab)"
                 if np.isfinite(deg) else ""))
        for s in ss.values():
            if s.snapshots and s.snapshots[-1].get("run") is None:
                s.snapshots[-1]["pd"] = np.asarray(caps["PD"][0], float)
                s.snapshots[-1]["pd_stats"] = r["stats"]

    def _do_bench_pair(self):
        w = self._pair_ready("Bench")
        if w is None:
            return
        try:
            f = self._floats(iterations=self.iters_var, repeats=self.repeats_var,
                             wait=self.wait_var)
            cfg = self._gather_settings()
        except RuntimeError as e:
            return messagebox.showerror("Bench", str(e))
        ss = dict(self.pair)
        skip, native = bool(self.skip_var.get()), bool(self.keepnative_var.get())
        self.run_worker(lambda: self._pair_bench_work(
            ss, cfg["pair"], w, int(f["iterations"]), int(f["repeats"]),
            f["wait"], skip, native), "pair bench loop running...")

    def _pair_bench_work(self, ss, cfgs, wires, iterations, repeats, wait_s,
                         skip, keep_native=False, keep_on=False):
        """The bench loop on both chains: per iteration both drives are
        uploaded to their AWG channels, both monitors are read from one
        acquisition per shot, and each session is updated by its own loop
        with its own model. Same checks, files, stats and stops as the
        single-channel loop (_bench_work); lines are prefixed X1 / X2.

        keep_on: the Optical tab's Learn (step 5). The outputs are already
        ON (its step 1) and the light is still wanted afterwards: nothing is
        asked, an output found OFF refuses the run before anything is
        uploaded, and the outputs stay ON when the run ends -- finished,
        stopped, or at a failed limit check. Only a run that DIES switches
        them OFF. The Run tab never passes it."""
        for k in PAIR_KEYS:
            with self._prefixed(k):
                self._apply_settings(cfgs[k], s=ss[k])
        rec = chanmap.record(self.cfg)
        for s in ss.values():
            s.scope_channels = rec
        print("scope channels: " + chanmap.describe(self.cfg).replace("\n", " | "))
        mon = {k: wires[k]["mon_ch"] for k in PAIR_KEYS}
        for k in PAIR_KEYS:
            if wires[k]["drive_ch"] is None:
                print(f"  NOTE: no Cmd{k} readback channel -- {k}'s trigger "
                      f"alignment is not verified on this run")
        pd_ch = self._pd_channel(mon)
        if pd_ch is not None:
            print(f"  the PD (CH{pd_ch}) is read with the monitors every iteration: "
                  f"meas_{ilc_batch.pd_stem(ss['X1'].stem) or ss['X1'].stem}PD_iNN.npy")
        if keep_native:
            print("  note: 'keep native-rate avg' is a single-channel option; "
                  "pair runs keep the decimated measurements only")
        _, awgmod = self._bench_modules()
        self._run_id = time.time()
        limit = getattr(awgmod, "MAX_ARB_NAME", NAME_LIMIT)
        for s in ss.values():
            if len(s.stem) + 4 > limit:
                raise RuntimeError(f"stem {s.stem!r} + '_iNN' is past the "
                                   f"{limit}-char name cap")
        awg, scope = self._connect_pair()
        played, switched = set(), []
        finished = False
        try:
            problems = []
            for k in PAIR_KEYS:
                s = ss[k]
                p, notes = ilc_bench.check_awg_channel(
                    awg, wires[k]["awg_ch"], full_scale=s.full_scale,
                    expect_period=len(s.t) * s.loop.dt)
                for n in notes:
                    print(f"{k}     ", n)
                problems += [f"{k}: {x}" for x in p]
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
                    print("REFUSING to upload. Fix the setup (Auto-set with both "
                          "outputs off), or tick 'skip setup checks'.")
                    return
            chans = [wires[k]["awg_ch"] for k in PAIR_KEYS]
            if keep_on:
                off = [c for c in chans if not awg.is_on(c)]
                if off:
                    print("REFUSING: " + " and ".join(f"CH{c}" for c in off)
                          + " OFF -- the Optical steps play from step 1 (Play "
                            "keepers); nothing was uploaded")
                    self._opt_mark_outputs("OFF", "read at Learn")
                    return
            else:
                ok, switched = self._ensure_outputs_on(awg, chans)
                if not ok:
                    return
            u = {k: ss[k].u for k in PAIR_KEYS}
            for n in range(iterations + 1):
                if self.stop_evt.is_set():
                    print("stopped before the next iteration")
                    break
                bad = {k: ss[k].loop.check(u[k]) for k in PAIR_KEYS}
                bad = {k: r for k, r in bad.items() if not r}
                if bad:
                    for k, r in bad.items():
                        print(f"{k} limit check FAILED:", r)
                    break
                print()
                for k in PAIR_KEYS:
                    s = ss[k]
                    wname = f"{s.stem}_i{s.iteration:02d}"
                    nn, frac = ilc_bench.upload_drive(awg, wires[k]["awg_ch"],
                                                      wname, u[k], s.full_scale)
                    played.add(wires[k]["awg_ch"])
                    self._pair_playing[k] = dict(wname=wname, u=np.array(u[k], float))
                    print(f"{k} iter {s.iteration}: uploaded {wname} to "
                          f"CH{wires[k]['awg_ch']} ({nn} pts, {100*frac:.1f}% of "
                          f"DAC range, peak {np.abs(u[k]).max():.4f} V)")
                    s.u = u[k]
                    self._write_iteration(wname, s)
                if n == 0:
                    time.sleep(0.5)
                    for k in PAIR_KEYS:
                        if wires[k]["drive_ch"] is not None:
                            with self._prefixed(k):
                                ilc_bench.verify_alignment(
                                    scope, wires[k]["drive_ch"], u[k], ss[k].t,
                                    ss[k].t_off, wait_s)
                caps = self._pair_capture(scope, ss, mon, repeats, wait_s, pd=pd_ch)
                for k in PAIR_KEYS:
                    s = ss[k]
                    y, stack = caps[k]
                    it = s.iteration
                    path = os.path.join(RUN_DIR, f"meas_{s.stem}_i{it:02d}.npy")
                    np.save(path, y)
                    m = s.loop.metrics(y)
                    m["model"] = cfgs[k]["desc"]
                    with self._prefixed(k):
                        print(f"         error: peak {m['peak_err_hv']:7.1f} V   "
                              f"rms {m['rms_err_hv']:6.2f} V   "
                              f"({m['peak_pct']:.2f}% FS)")
                        m.update(self._noise_floor(stack, s=s))
                        st = self._measurement_stats(stack, path, scope, mon[k], s=s)
                        m.update(self._stats_metrics(st))
                        m.update(self._model_check_line(it, u[k], y, s=s))
                    s.snapshots.append(dict(it=it, y=y, m=m, u=u[k],
                                            t_wall=time.time(),
                                            run_id=self._run_id, stats=st))
                if "PD" in caps:
                    self._pair_pd(ss, caps, scope, pd_ch)
                self.msgs.put(("call", self._after_pair_measurement))
                if n < iterations and not self.stop_evt.is_set():
                    for k in PAIR_KEYS:
                        s = ss[k]
                        u_new = s.loop.update(u[k], caps[k][0])
                        h = s.loop.history[-1]
                        h["model"] = cfgs[k]["desc"]
                        h["update_rms"] = ilc.update_rms(u[k], u_new)
                        h["run_id"] = self._run_id
                        h["update_below_lsb"] = dg.update_vs_step(u_new - u[k])["below"]
                        with self._prefixed(k):
                            self._plateau_line(s)
                        u[k] = u_new
                        s.u = u_new
                        s.iteration += 1
                        save_session(s)
            finished = True
        finally:
            # both chains, as the single loop does for its one: a run that
            # played anything (or switched an output on) ends with it OFF --
            # except the Optical tab's Learn, which ends with the light still
            # on unless it died
            if (played or switched) and not (keep_on and finished):
                self._outputs_off(awg, set(played) | set(switched),
                                  "Learn died" if keep_on else "end of run")
            elif keep_on and played:
                print("outputs left ON for the Optical steps (OFF at step 7)")
            awg.close()
            scope.close()
            print("instruments closed")
        for k in PAIR_KEYS:
            print(f"\n{k}:\n" + ss[k].loop.report())

    def _after_pair_measurement(self):
        """Main thread, after each pair measurement: the shown session's
        summary and plots."""
        self._refresh_summary()
        self._wave_redraw = None
        self._redraw_iterations()

    def _do_hold_pair(self):
        w = self._pair_ready("Hold")
        if w is None:
            return
        try:
            f = self._floats(runs=self.holdruns_var, gap_s=self.holdgap_var,
                             repeats=self.repeats_var, wait=self.wait_var)
        except RuntimeError as e:
            return messagebox.showerror("Hold", str(e))
        if f["runs"] < 1:
            return messagebox.showerror("Hold", "runs must be at least 1")
        drives = {}
        for k in PAIR_KEYS:
            try:
                drives[k] = self._hold_drive(self.holditer_var.get(),
                                             s=self.pair[k])
            except RuntimeError as e:
                return messagebox.showerror(f"Hold {k}", str(e))
        ss = dict(self.pair)
        self.run_worker(lambda: self._pair_hold_work(
            ss, w, int(f["runs"]), f["gap_s"], int(f["repeats"]), f["wait"],
            drives), "hold: re-measuring both chains...")

    def _pair_hold_work(self, ss, wires, runs, gap_s, repeats, wait_s, drives):
        """Hold on both chains: the same drive(s) re-measured `runs` times,
        both monitors from one acquisition per shot, no update, states
        untouched. Runs are numbered per session (iter k rN)."""
        rec = chanmap.record(self.cfg)
        for s in ss.values():
            s.scope_channels = rec
        print("scope channels: " + chanmap.describe(self.cfg).replace("\n", " | "))
        its = {k: ss[k].iteration if drives[k][0] is None else int(drives[k][0])
               for k in PAIR_KEYS}
        us = {k: ss[k].u if drives[k][1] is None else np.asarray(drives[k][1], float)
              for k in PAIR_KEYS}
        mon = {k: wires[k]["mon_ch"] for k in PAIR_KEYS}
        awg, scope = self._connect_pair()
        played, switched = set(), []
        try:
            problems = []
            for k in PAIR_KEYS:
                s = ss[k]
                p, notes = ilc_bench.check_awg_channel(
                    awg, wires[k]["awg_ch"], full_scale=s.full_scale,
                    expect_period=len(s.t) * s.loop.dt)
                for n in notes:
                    print(f"{k}     ", n)
                problems += [f"{k}: {x}" for x in p]
                rep = s.loop.check(us[k])
                print(f"{k}      limit check:", rep)
                if not rep:
                    problems.append(f"{k}: the drive fails the limit check")
            acq = scope.get(":ACQuire:TYPE")
            if not acq.upper().startswith("HRES"):
                problems.append(f"scope is in {acq}; hold uses the same "
                                f"averaged-HRES scheme as the loop")
            if problems:
                print("Setup problems:")
                for p in problems:
                    print("  !", p)
                print("REFUSING -- fix the setup first.")
                return
            for k in PAIR_KEYS:
                s = ss[k]
                wname = f"{s.stem}_i{its[k]:02d}"
                nn, frac = ilc_bench.upload_drive(awg, wires[k]["awg_ch"], wname,
                                                  us[k], s.full_scale)
                played.add(wires[k]["awg_ch"])
                self._pair_playing[k] = dict(wname=wname, u=np.array(us[k], float))
                print(f"{k} hold: uploaded {wname} ({nn} pts, {100*frac:.1f}% of "
                      f"DAC range) -- this drive will NOT be updated")
            ok, switched = self._ensure_outputs_on(
                awg, [wires[k]["awg_ch"] for k in PAIR_KEYS])
            if not ok:
                return
            r0 = {k: max([sn["run"] for sn in ss[k].snapshots
                          if sn["it"] == its[k] and sn.get("run") is not None],
                         default=0) + 1 for k in PAIR_KEYS}
            print(f"hold: {runs} run(s), {gap_s:g} s gap, X1 i{its['X1']:02d} "
                  f"from r{r0['X1']}, X2 i{its['X2']:02d} from r{r0['X2']}")
            t_start = time.time()
            for j in range(runs):
                if self.stop_evt.is_set():
                    print("hold stopped by user")
                    break
                caps = self._pair_capture(scope, ss, mon, repeats, wait_s)
                for k in PAIR_KEYS:
                    s = ss[k]
                    r, it = r0[k] + j, its[k]
                    y, stack = caps[k]
                    path = os.path.join(os.path.dirname(s.state_path),
                                        f"meas_{s.stem}_i{it:02d}_r{r:02d}.npy")
                    np.save(path, y)
                    m = s.loop.metrics(y)
                    sn = dict(it=it, y=y, m=m, u=us[k], run=r, t_wall=time.time())
                    s.snapshots.append(sn)
                    with self._prefixed(k):
                        print(f"  r{r} (+{fmt_span(time.time() - t_start)}): error "
                              f"peak {m['peak_err_hv']:7.1f} V   "
                              f"rms {m['rms_err_hv']:6.2f} V")
                        m.update(self._noise_floor(stack, s=s))
                        st = self._measurement_stats(stack, path, scope, mon[k], s=s)
                    m.update(self._stats_metrics(st))
                    sn["stats"] = st
                self.msgs.put(("call", lambda: self._show_held(
                    its.get(self._pair_key_of(self.session) or "X1", its["X1"]))))
                if j < runs - 1 and gap_s > 0:
                    t_end = time.time() + gap_s
                    while time.time() < t_end:
                        if self.stop_evt.is_set():
                            break
                        time.sleep(min(0.2, max(t_end - time.time(), 0.01)))
            print("hold finished: states untouched")
        finally:
            if played or switched:
                self._outputs_off(awg, set(played) | set(switched), "end of hold")
            awg.close()
            scope.close()
            print("instruments closed")

    def _do_autoset_pair(self):
        w = self._pair_ready("Auto-set")
        if w is None:
            return
        items = []
        for k in PAIR_KEYS:
            s = self.pair[k]
            items.append(dict(
                key=k, period=float(s.t[-1]) + float(s.loop.dt),
                u_lo=float(s.u.min()), u_hi=float(s.u.max()),
                v_lo=float(s.loop.target.min()), v_hi=float(s.loop.target.max()),
                fs=s.full_scale, awg_ch=int(w[k]["awg_ch"]),
                scope_ch=int(w[k]["mon_ch"]),
                wname=f"{s.stem}_i{s.iteration:02d}", drive_ch=w[k]["drive_ch"]))
        keep_vert = bool(self.keepvert_var.get())
        self.run_worker(lambda: self._pair_autoset_work(items, keep_vert),
                        "auto-setting both chains...")

    def _pair_autoset_work(self, items, keep_vert):
        """Auto-set for both chains in one session of the instruments: both
        AWG channels, the scope window, and the drive and monitor verticals
        of both. FRQ moves only with BOTH outputs off."""
        awg, scope = self._connect_pair()
        try:
            live = [it["awg_ch"] for it in items if awg.is_on(it["awg_ch"])]
            if live:
                print("REFUSING to auto-set: "
                      + " and ".join(f"CH{c}" for c in live)
                      + (" output is" if len(live) == 1 else " outputs are")
                      + " ON. In pair mode FRQ/AMP are changed only with BOTH "
                        "outputs off -- a live output carries real voltage to "
                        "its chain. Switch it off first.")
                return
            for it in items:
                with self._prefixed(it["key"]):
                    self._autoset_on(awg, scope, it["period"], it["u_lo"],
                                     it["u_hi"], it["v_lo"], it["v_hi"], it["fs"],
                                     it["awg_ch"], it["scope_ch"], it["wname"],
                                     keep_vert, it["drive_ch"])
        finally:
            awg.close()
            scope.close()
            print("instruments closed")

    # -------------------------------------------------------------- optical
    # The optical close by hand (docs/OPTICAL_CLOSE_DESIGN.md, part 4). The
    # loop makes the MONITORS follow the target; the experiment needs the
    # LIGHT, which leaves the monitors at the ramp top and after the fall.
    # Seven steps on the Optical tab, both chains at once: play the keepers
    # (outputs ON, and left ON), scan the light with the polarimeter's
    # analyzer, compare it with the monitors and the target, move both
    # TARGETS by minus (light - monitors), let the pair loop learn them with
    # the light still on, verify with a second scan, outputs OFF.
    # eomilc.optical does the measuring and the arithmetic -- the batch's
    # optical stage calls the same functions; this is the panel around them.
    # Every step is a worker that opens the scope and the AWG for itself
    # (_connect_pair) and closes them again WITHOUT touching the outputs:
    # only step 7 switches them OFF (and a step that dies on the way).
    #
    # Solo play (13 Oct 2026, the design's addendum): step 1 can play ONE
    # crystal's keeper with the other channel parked flat at its idle
    # (optical.park_drive, uploaded as PARK1 / PARK2; the output stays ON).
    # A scan of that is compared with solo= so the whole light - monitors
    # goes to the crystal that drove; the correction files ACCUMULATE in
    # self._opt["files"] (X1 alone, X2 alone, then Apply both), and the
    # verification is always a joint scan (both playing: the light the
    # atoms get).

    OPT_HINTS = {
        1: "uploads the sessions' current drives (both, or one with the other "
           "parked flat at its idle), outputs ON (one question); they stay ON "
           "until 7",
        2: "the analyzer steps through the angles while the keepers play; "
           "the scan lands in run\\optical",
        3: "light - monitors and light - target per leg, and a correction "
           "file per crystal (a solo scan: the driving crystal's only)",
        4: "moves the TARGETS by the gated corrections held, one file per "
           "crystal (base targets kept); a refusal changes nothing",
        5: "the pair loop learns the moved targets; the outputs stay ON",
        6: "a second scan and comparison; before / after in the log and on "
           "the Optical plot tab",
        7: "both outputs OFF -- where every session ends",
    }
    # the before / after rows, per leg (eomilc.optical.verdict's keys)
    OPT_ROWS = (("top_lt_mdeg", "ramp top, light - target"),
                ("after_fall_lt_mdeg", "first ms after fall, light - target"),
                ("lt_rms_mdeg", "light - target rms"),
                ("top_lm_mdeg", "ramp top, light - monitors"))
    OPT_LEG_COLOURS = ("#1f77b4", "#d62728")

    def _build_optical_tab(self, parent):
        """The Optical tab: a state line and the seven numbered steps, each
        greyed until the step before it has a result. The rarely changed
        numbers are in Settings... (WORKFLOW_GUI.md 10, 11)."""
        oc = self.cfg.get("optical") or {}
        self._opt_vars = {}
        # {X1/X2: {wname, u[, parked]}}: what the AWG was last given
        self._pair_playing = {}
        self._opt = {"outputs": ("not reported this session", "", "")}
        self._opt_reset(None)
        self._opt_auto = None        # the name the Scan box was last filled with

        def sv(key, default):
            v = tk.StringVar(value=str(oc.get(key, default)))
            self._opt_vars[key] = v
            return v

        def bv(key, default):
            v = tk.BooleanVar(value=bool(oc.get(key, default)))
            self._opt_vars[key] = v
            return v
        self.optname_var = tk.StringVar(value="")
        self.optplay_var = tk.StringVar(value="both")      # "both" / "X1" / "X2"
        self.optangles_var = sv("angles", "0:359:22.5")
        self.optshots_var = sv("shots", "8")
        self.optband_var = sv("band_hz", "5000")
        self.optdelay_var = sv("pd_delay_us", "1.7")
        self.optsplit_var = sv("split", "0.5")
        self.optgain_var = sv("gain", "0.7")
        self.optiters_var = sv("iterations", "5")
        self.optpoints_var = sv("points", "")
        if self.optpoints_var.get().strip() == "20000":
            # the default before 14 Oct 2026, remembered in older configs: blank
            # means the readout follows the sample spacing (2 us), which is
            # what the scans want whatever the record length
            self.optpoints_var.set("")
        self.optsample_var = sv("sample_us", "2.0")     # used when points is blank
        self.optref_var = sv("ref_every", "9")
        self.optsettle_var = sv("settle_s", "1.0")
        self.optpreset_var = sv("preset", "")
        self.optnull_var = bv("hold_null", True)
        self.optfigs_var = bv("figures", True)

        ttk.Label(parent, text=(
            "The optical close by hand, both chains (pair mode): the light "
            "measured against the monitors moves both TARGETS, the loop learns "
            "them, a second scan checks. The outputs stay ON from 1 to 7."),
            foreground="#555555", wraplength=440, justify="left").pack(
            fill="x", pady=(0, 3))
        sf = ttk.LabelFrame(parent, text="Optical close", padding=3)
        sf.pack(fill="x", pady=(0, 2))
        self.opt_state = ttk.Label(sf, text="", font=MONO, justify="left")
        self.opt_state.pack(fill="x")

        g = ttk.Frame(parent)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        self._opt_btns, self._opt_notes = {}, {}
        rows = {}
        for i in range(1, 8):
            ttk.Label(g, text=str(i), width=2).grid(row=2 * i, column=0, sticky="nw",
                                                    pady=(3, 0))
            rows[i] = ttk.Frame(g)
            rows[i].grid(row=2 * i, column=1, sticky="ew", pady=(2, 0))
            self._opt_notes[i] = ttk.Label(g, text="", foreground="#666666",
                                           wraplength=430, justify="left")
            self._opt_notes[i].grid(row=2 * i + 1, column=1, sticky="w")

        def field(row, lab, var, w):
            ttk.Label(row, text=lab).pack(side="left", padx=(0, 2))
            ttk.Entry(row, textvariable=var, width=w).pack(side="left", padx=(0, 5))

        def button(i, text, cmd):
            b = ttk.Button(rows[i], text=text, command=cmd)
            b.pack(side="left", fill="x", expand=True)
            self._actions.append(b)
            self._opt_btns[i] = b

        button(1, "Play (outputs ON)", self.do_opt_play)
        ttk.Label(rows[1], text="play:").pack(side="left", padx=(6, 2))
        for val, lab in (("both", "both"), ("X1", "X1 alone"), ("X2", "X2 alone")):
            ttk.Radiobutton(rows[1], text=lab, value=val,
                            variable=self.optplay_var).pack(side="left", padx=(0, 3))
        field(rows[2], "name", self.optname_var, 12)
        field(rows[2], "angles", self.optangles_var, 10)
        field(rows[2], "shots", self.optshots_var, 3)
        button(2, "Scan", self.do_opt_scan)
        # not in _actions: it is the one button that works WHILE busy
        self.opt_stop_btn = ttk.Button(rows[2], text="Stop", width=5,
                                       command=self.stop_evt.set, state="disabled")
        self.opt_stop_btn.pack(side="left", padx=(4, 0))
        field(rows[3], "band Hz", self.optband_var, 6)
        field(rows[3], "PD delay us", self.optdelay_var, 4)
        field(rows[3], "split X1", self.optsplit_var, 4)
        button(3, "Compare", self.do_opt_compare)
        field(rows[4], "gain", self.optgain_var, 4)
        button(4, "Apply to the targets", self.do_opt_apply)
        field(rows[5], "iterations", self.optiters_var, 3)
        button(5, "Learn (pair loop, outputs stay ON)", self.do_opt_learn)
        button(6, "Verify (scan + compare)", self.do_opt_verify)
        button(7, "Outputs OFF (both)", self.do_opt_off)
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=(4, 0))
        ttk.Button(r, text="Settings...", command=self.do_opt_settings).pack(side="left")
        ttk.Label(r, text="points / sample us, reference returns, hold-null angles, settle, "
                          "preset, figures", foreground="#666666").pack(
            side="left", padx=(6, 0))

    def do_opt_settings(self):
        """The Optical steps' rarely changed numbers."""
        fr = self._dialog("_opt_win", "Optical settings")
        if fr is None:
            return
        # points per shot and the sample spacing share a row: a blank points
        # field reads the screen out at `sample us` (optical.scan computes it)
        r = ttk.Frame(fr)
        r.pack(fill="x", pady=1)
        ttk.Label(r, text="points per shot", width=15).pack(side="left")
        ttk.Entry(r, textvariable=self.optpoints_var, width=7).pack(side="left", padx=(2, 6))
        ttk.Label(r, text="sample us").pack(side="left")
        ttk.Entry(r, textvariable=self.optsample_var, width=5).pack(side="left", padx=(2, 6))
        ttk.Label(fr, text="scope readout per shot (20000, the polarimeter's); points "
                           "blank = the screen span / sample spacing (2 us matches the "
                           "ILC grid)", foreground="#666666", wraplength=430,
                  justify="left").pack(fill="x", padx=(4, 0))
        for lab, var, w, note in (
                ("reference every", self.optref_var, 4,
                 "a return to the reference angle every N steps"),
                ("settle s", self.optsettle_var, 5,
                 "wait before the first shot (bench only)"),
                ("preset", self.optpreset_var, 12,
                 "a polarimeter preset written first; blank = none")):
            r = ttk.Frame(fr)
            r.pack(fill="x", pady=1)
            ttk.Label(r, text=lab, width=15).pack(side="left")
            ttk.Entry(r, textvariable=var, width=w).pack(side="left", padx=(2, 6))
            ttk.Label(r, text=note, foreground="#666666").pack(side="left")
        for text, var, note in (
                ("hold-null angles", self.optnull_var,
                 "extra angles around the hold's null, from each target's peak"),
                ("comparison figures", self.optfigs_var,
                 "the polarimeter's fig1-5 PNGs beside the comparison")):
            r = ttk.Frame(fr)
            r.pack(fill="x", pady=1)
            ttk.Checkbutton(r, text=text, variable=var).pack(side="left")
            ttk.Label(r, text=note, foreground="#666666").pack(side="left", padx=(6, 0))
        ttk.Label(fr, text="A preset writes the polarimeter's V/div and trigger over "
                           "the ILC's: leave it blank unless the PD is off screen. "
                           "The analyzer's port and zero come from the polarimeter's "
                           "config (Hardware tab: ELL14).",
                  foreground="#666666", wraplength=430, justify="left").pack(
            fill="x", pady=(4, 0))

    # -- state, prerequisites ----------------------------------------------
    def _opt_reset(self, key):
        """Forget the step results: a different pair (Load, Init) starts the
        close over. What the outputs were last seen doing is kept.

        files / files_from / files_meta: the correction files held for Apply,
        {EO1/EO2: path}, {EOn: the scan it came from}, {EOn: {band, peak_V}}.
        They ACCUMULATE over compares (a solo compare sets its crystal's and
        keeps the other's); scan_solo: what played during the last scan
        ("X1" / "X2" alone, None both); playing: mirrors _opt_playing()."""
        self._opt = dict(pair_key=key, outputs=self._opt["outputs"], scan=None,
                         scan_name=None, scan_solo=None, compare=None, verdict=None,
                         before=None, applied=None, learned=None, verify=None, table=None,
                         verdict_line=None, done={}, files={}, files_from={},
                         files_meta={}, playing=self._opt_playing())

    def _opt_mark_outputs(self, state, why):
        """Any thread: what the outputs were last seen or set to."""
        stamp = datetime.datetime.now().strftime("%H:%M")

        def apply():
            self._opt["outputs"] = (state, stamp, why)
            self._refresh_optical()
        if threading.current_thread() is threading.main_thread():
            apply()
        else:
            self.msgs.put(("call", apply))

    def _opt_base(self):
        s1 = self.pair["X1"]
        return s1.stem[:-2] if s1.stem.endswith("X1") else s1.stem

    @staticmethod
    def _opt_scan_complete(folder):
        """A scan folder whose manifest has every step done."""
        try:
            js = [f for f in os.listdir(folder) if f.endswith("_scan.json")]
            with open(os.path.join(folder, js[0]), encoding="utf-8") as fh:
                steps = json.load(fh).get("steps") or []
            return bool(steps) and all(s.get("status") == "done" for s in steps)
        except (OSError, ValueError, IndexError):
            return False

    def _opt_free_name(self, stem, first=""):
        """<stem><first>, else <stem>2, 3, ...: the first name in run\\optical
        that is not a COMPLETE scan (an unfinished one is resumed)."""
        out = os.path.join(RUN_DIR, "optical")
        n, name = 1, stem + first
        while self._opt_scan_complete(os.path.join(out, name)):
            n += 1
            name = f"{stem}{n}"
        return name

    def _opt_default_name(self):
        """<base>_optN, or <base>_optN_X1 / _X2 while one crystal plays
        alone: N the first number without a complete scan of that name."""
        out = os.path.join(RUN_DIR, "optical")
        pl = self._opt_playing()
        suffix = f"_{pl}" if pl in PAIR_KEYS else ""
        base, n = self._opt_base(), 1
        while self._opt_scan_complete(os.path.join(out, f"{base}_opt{n}{suffix}")):
            n += 1
        return f"{base}_opt{n}{suffix}"

    def _opt_playing(self):
        """What the AWG was last given, as a play mode: "both", "X1" / "X2"
        (that crystal's keeper, the other parked), or None (nothing of a
        pair). Read from _pair_playing, which every pair upload writes (the
        Run tab's loop and Hold included, so a Learn makes it "both")."""
        pp = getattr(self, "_pair_playing", None) or {}
        if not all(k in pp for k in PAIR_KEYS):
            return None
        parked = [k for k in PAIR_KEYS if pp[k].get("parked")]
        if not parked:
            return "both"
        driving = [k for k in PAIR_KEYS if k not in parked]
        return driving[0] if len(driving) == 1 else None

    @staticmethod
    def _opt_play_text(pl):
        return {"both": "playing both", "X1": "X1 alone, X2 parked",
                "X2": "X2 alone, X1 parked"}.get(pl, "nothing of a pair")

    def _opt_prereq(self, i):
        """(ok, why not) for step i, from what the panel holds."""
        p = self.pair
        if p is None:
            return False, ("pair mode is off: tick Pair (X1 + X2) on the Session tab, "
                           "then Load state or Init campaign" if not self.pair_var.get()
                           else "no pair loaded: Load state or Init campaign (Session tab)")
        if i in (1, 7):
            return True, ""
        o = self._opt
        if i == 3:
            if not (o["scan"] and os.path.isdir(o["scan"])):
                return False, "needs step 2: no scan of this pair yet"
            return True, ""
        if i == 4:
            c = o["compare"]
            if not c or c.get("folder") != o["scan"]:
                return False, "needs step 3: the last scan is not compared yet"
            if not o["files"]:
                return False, "needs step 3: no correction file held"
            return True, ""
        on = o["outputs"][0] == "ON"
        if i == 5:
            if not o["applied"]:
                return False, "needs step 4: no correction applied to this pair yet"
            if not on:
                return False, f"needs step 1: outputs {o['outputs'][0]} -- Play keepers"
            return True, ""
        if i == 6:
            pl = self._opt_playing()
            if pl in PAIR_KEYS:
                return False, ("Verify needs both playing: step 1 with 'both', or Learn "
                               f"(now {self._opt_play_text(pl)})")
            if not o["learned"]:
                return False, "needs step 5: the loop has not learned the corrected targets"
        # 2 and 6: the light must come from the drives the sessions hold --
        # each chain's current drive, or its park drive when it is parked
        if not on:
            return False, f"needs step 1: outputs {o['outputs'][0]} -- Play keepers"
        for k in PAIR_KEYS:
            pl = self._pair_playing.get(k)
            want = optical.park_drive(p[k].u) if pl and pl.get("parked") else p[k].u
            if pl is None or len(pl["u"]) != len(want) or not np.array_equal(pl["u"], want):
                return False, (f"needs step 1: the AWG holds "
                               f"{pl['wname'] if pl else 'nothing of this pair'}, {k} "
                               f"is at i{p[k].iteration:02d} -- Play keepers again")
        return True, ""

    def _opt_check(self, i):
        """Main thread: True, or the reason in a dialog."""
        ok, why = self._opt_prereq(i)
        if not ok:
            messagebox.showerror(f"Optical {i}", why)
        return ok

    def _refresh_optical(self):
        """The Optical tab's state line, notes and greying (main thread)."""
        if not hasattr(self, "opt_state"):
            return
        p = self.pair
        key = None if p is None else (id(p["X1"]), id(p["X2"]))
        if key != self._opt["pair_key"]:
            self._opt_reset(key)
        o = self._opt
        lines = []
        if p is None:
            lines.append("pair mode " + ("on, no pair loaded" if self.pair_var.get()
                                         else "off") + ": the steps need a pair")
        else:
            lines.append("pair     " + "   ".join(f"{k} {p[k].stem} i{p[k].iteration:02d}"
                                                  for k in PAIR_KEYS))
            auto = self._opt_default_name()
            cur = self.optname_var.get().strip()
            if not cur or cur == self._opt_auto:
                if cur != auto:
                    self.optname_var.set(auto)
                self._opt_auto = auto
        st, when, why = o["outputs"]
        lines.append(f"outputs  {st}" + (f" at {when} ({why})" if when else ""))
        o["playing"] = self._opt_playing()
        pl = [self._pair_playing[k]["wname"] for k in PAIR_KEYS if k in self._pair_playing]
        if pl:
            lines.append("AWG got  " + ", ".join(pl)
                         + (f" ({self._opt_play_text(o['playing'])})" if o["playing"] else ""))
        lines.append("scan     " + (o["scan_name"] or "none yet")
                     + (f" -> verify {os.path.basename(o['verify'])}" if o["verify"] else ""))
        if o["files"]:
            lines.append("corrections: " + ", ".join(
                f"{ch} from {o['files_from'].get(ch, '?')}" for ch in sorted(o["files"])))
            txt = "corr pk  " + ", ".join(f"{ch} {o['files_meta'][ch]['peak_V']:.1f} V"
                                          for ch in sorted(o["files"]))
            if o["applied"]:
                txt += f"; applied x{o['applied']['gain']:g}: " + ", ".join(
                    f"{x:.1f}" for x in o["applied"]["peak"].values()) + " V"
            lines.append(txt)
        if o["verdict_line"]:
            lines.append(o["verdict_line"])
        self.opt_state.configure(text="\n".join(elide(L, 66) for L in lines))
        for i in range(1, 8):
            ok, why = self._opt_prereq(i)
            if not self.busy:
                self._opt_btns[i].configure(state="normal" if ok else "disabled")
            self._opt_notes[i].configure(
                text=(o["done"].get(i) or self.OPT_HINTS[i]) if ok else why,
                foreground="#666666" if ok else "#a06000")

    # -- the readers of the step fields (main thread) ----------------------
    def _opt_scan_kw(self):
        angles = self.optangles_var.get().strip()
        try:
            parts = [float(x) for x in re.split(r"[:,\s]+", angles) if x]
            if not parts or (":" in angles and (len(parts) != 3 or parts[2] <= 0)):
                raise ValueError
        except ValueError:
            raise RuntimeError(f"angles {angles!r}: start:stop:step in degrees "
                               f"(0:359:22.5), or a list (0, 45, 90)")
        f = self._floats(shots=self.optshots_var, sample_us=self.optsample_var,
                         ref_every=self.optref_var, settle_s=self.optsettle_var)
        # points blank: optical.scan computes them from the screen span and
        # the sample spacing
        pts = self.optpoints_var.get().strip()
        if pts:
            f.update(self._floats(points=self.optpoints_var))
        if (f["shots"] < 1 or (pts and f["points"] < 1000) or f["ref_every"] < 1
                or f["settle_s"] < 0 or f["sample_us"] <= 0):
            raise RuntimeError("shots >= 1, points >= 1000 (or blank), sample us > 0, "
                               "reference every >= 1, settle >= 0")
        return dict(angles=angles, shots=int(f["shots"]),
                    points=int(f["points"]) if pts else None, sample_us=f["sample_us"],
                    ref_every=int(f["ref_every"]), settle_s=f["settle_s"],
                    hold_null=bool(self.optnull_var.get()),
                    preset=self.optpreset_var.get().strip())

    def _opt_compare_kw(self):
        f = self._floats(band=self.optband_var, delay=self.optdelay_var,
                         split=self.optsplit_var)
        if f["band"] <= 0 or f["delay"] < 0 or not 0 <= f["split"] <= 1:
            raise RuntimeError("band > 0 Hz, PD delay >= 0 us, split X1 0..1")
        return dict(band=f["band"], delay=f["delay"], split=f["split"],
                    figs=bool(self.optfigs_var.get()))

    def _opt_scan_name(self, name):
        name = name.strip()
        if not re.match(r"^[\w.-]+$", name or "?"):
            raise RuntimeError(f"scan name {name!r}: letters, digits, _ . - only "
                               f"(it is a folder name)")
        return name

    # -- 1 Play keepers ----------------------------------------------------
    def do_opt_play(self):
        if not self._opt_check(1):
            return
        w = self._pair_ready("Optical 1")
        if w is None:
            return
        ss = dict(self.pair)
        which = self.optplay_var.get()
        if which not in ("both",) + PAIR_KEYS:
            which = "both"
        self.run_worker(lambda: self._opt_play_work(ss, w, which),
                        "optical 1: " + ("playing both keepers..." if which == "both"
                                         else f"playing {which} alone..."))

    def _opt_play_work(self, ss, wires, which="both"):
        """The driving sessions' current drives uploaded and selected, both
        outputs ON (one question), and LEFT ON: the light is what steps 2-6
        measure. which "X1" / "X2": that crystal's keeper alone, the other
        channel PARKED -- optical.park_drive of its drive (flat at the
        record's idle, the same length, so FRQ and the trigger hold) uploaded
        as optical.PARK_NAMES[it], its output ON like the other's (an output
        is never switched off to park). A setup or limit problem refuses
        before anything is uploaded; a failure after an output went on
        switches it off again."""
        _, awgmod = self._bench_modules()
        limit = getattr(awgmod, "MAX_ARB_NAME", NAME_LIMIT)
        parked = {k for k in PAIR_KEYS if which in PAIR_KEYS and k != which}
        drives = {k: (optical.park_drive(ss[k].u) if k in parked else np.array(ss[k].u, float))
                  for k in PAIR_KEYS}
        wnames = {k: (optical.PARK_NAMES[k] if k in parked
                      else f"{ss[k].stem}_i{ss[k].iteration:02d}") for k in PAIR_KEYS}
        awg, scope = self._connect_pair()
        switched, ok_end = [], False
        try:
            problems = []
            for k in PAIR_KEYS:
                s = ss[k]
                if len(wnames[k]) > limit:
                    problems.append(f"{k}: {wnames[k]!r} is past the {limit}-char name cap")
                p, notes = ilc_bench.check_awg_channel(
                    awg, wires[k]["awg_ch"], full_scale=s.full_scale,
                    expect_period=len(s.t) * s.loop.dt)
                for n in notes:
                    print(f"{k}     ", n)
                problems += [f"{k}: {x}" for x in p]
                rep = s.loop.check(drives[k])
                print(f"{k}      limit check" + (" (park drive)" if k in parked else "")
                      + ":", rep)
                if not rep:
                    problems.append(f"{k}: the {'park ' if k in parked else ''}drive fails "
                                    f"the limit check")
            if problems:
                print("Setup problems:")
                for p in problems:
                    print("  !", p)
                print("REFUSING to play -- fix the setup first (Auto-set with both "
                      "outputs off).")
                return
            for k in PAIR_KEYS:
                s = ss[k]
                nn, frac = ilc_bench.upload_drive(awg, wires[k]["awg_ch"], wnames[k],
                                                  drives[k], s.full_scale)
                self._pair_playing[k] = dict(wname=wnames[k], u=drives[k])
                if k in parked:
                    self._pair_playing[k]["parked"] = True
                    print(f"{k} parked: uploaded {wnames[k]} to CH{wires[k]['awg_ch']} "
                          f"({nn} pts, flat at the idle {drives[k][0]:+.4f} V)")
                else:
                    print(f"{k} keeper: uploaded {wnames[k]} to CH{wires[k]['awg_ch']} "
                          f"({nn} pts, {100*frac:.1f}% of DAC range)")
            chans = [wires[k]["awg_ch"] for k in PAIR_KEYS]
            ok, switched = self._ensure_outputs_on(
                awg, chans, until="They stay ON through the Optical steps, until "
                                  "step 7, Outputs OFF.")
            if not ok:
                return
            ok_end = True
            names = " and ".join(f"CH{c}" for c in chans)
            if parked:
                (other,) = parked
                what = f"{which} keeper {wnames[which]}, {other} parked ({wnames[other]})"
            else:
                what = "playing " + ", ".join(wnames[k] for k in PAIR_KEYS)
            print(f"{names} ON, {what} -- left ON for the Optical steps")
            self._bench_note(f"{names} output ON (Optical: kept ON until step 7)")
            self._opt_mark_outputs("ON", "step 1")
            stamp = datetime.datetime.now().strftime("%H:%M")
            done = f"{stamp}: {what}"
            self.msgs.put(("call", lambda: (self._opt["done"].__setitem__(1, done),
                                            self._opt.__setitem__("playing", which),
                                            self._refresh_optical())))
        finally:
            if switched and not ok_end:
                self._outputs_off(awg, switched, "Play keepers failed")
            awg.close()
            scope.close()
            print("instruments closed")

    # -- 2 Scan ------------------------------------------------------------
    def do_opt_scan(self):
        if not self._opt_check(2):
            return
        w = self._pair_ready("Optical 2")
        if w is None:
            return
        try:
            kw = self._opt_scan_kw()
            name = self._opt_scan_name(self.optname_var.get() or self._opt_default_name())
        except RuntimeError as e:
            return messagebox.showerror("Optical 2", str(e))
        ss = dict(self.pair)
        pl = self._opt_playing()
        solo = pl if pl in PAIR_KEYS else None

        def work():
            folder = self._opt_scan_work(ss, w, name, kw, solo=solo)
            if folder:
                self.msgs.put(("call", lambda: self._opt_scan_done(folder, name, solo)))
        self.run_worker(work, f"optical 2: scanning {name}"
                        + (f" ({solo} alone)..." if solo else "..."))

    def _optical_devices(self, scope, cfg):
        """Worker side: the polarimeter's devices beside the ILC's open
        scope (the ELL14 from the polarimeter's config; the scope wrapped,
        never closed by eomilc.optical). The one seam the smoke test
        replaces, with eomilc.optical's simulated devices: the panel has no
        simulate switch."""
        return optical.open_devices(scope, cfg, log=print)

    @staticmethod
    def _opt_ends(ss, solo=None):
        """{X1: deg, X2: deg}: each chain's hold rotation, 90 deg x the
        target's peak at the EOM / that chain's V90 (eomilc.config v90_hv).
        The scan's hold-null angles come from their sum. solo "X1" / "X2":
        the other crystal is parked at its idle and adds nothing (0 deg).
        None when a driving channel has no V90."""
        out = {}
        for k in PAIR_KEYS:
            if solo is not None and k != solo:
                out[k] = 0.0
                continue
            s = ss[k]
            ch = s.loop.channel
            tgt = s.base_target if s.base_target is not None else s.loop.target
            v90 = float(getattr(ch, "v90_hv", float("nan")))
            if not np.isfinite(v90) or v90 <= 0:
                return None
            out[k] = 90.0 * float(np.max(tgt)) * ch.mon_scale / v90
        return out

    def _opt_scan_work(self, ss, wires, name, kw, solo=None):
        """Worker side: one scan while the keepers play. Opens the scope and
        the AWG, refuses when either output is OFF, opens the analyzer beside
        the scope, scans into run\\optical, and closes everything WITHOUT
        touching the outputs. Returns the scan folder, or None (refused, or
        stopped -- a stopped scan is saved and resumes under its name).
        solo "X1" / "X2": that crystal drove alone, the other parked (the
        park drive is in _pair_playing); recorded in the scan's manifest as
        "solo" so the scan says how it must be compared."""
        outdir = os.path.join(RUN_DIR, "optical")
        os.makedirs(outdir, exist_ok=True)
        awg, scope = self._connect_pair()
        dev = None
        try:
            off = [wires[k]["awg_ch"] for k in PAIR_KEYS
                   if not awg.is_on(wires[k]["awg_ch"])]
            if off:
                print("REFUSING to scan: " + " and ".join(f"CH{c}" for c in off)
                      + " OFF -- the light comes from the keepers playing (step 1)")
                self._opt_mark_outputs("OFF", "read at the scan")
                return None
            dev = self._optical_devices(scope, self.cfg)
            # the drives that play (on the bench only remembered, for the
            # manifest and the sampling estimate; simulated, they ARE the light)
            drives = {PAIR_CHANNEL[k]: (self._pair_playing[k]["u"] if k in self._pair_playing
                                        else ss[k].u) for k in PAIR_KEYS}
            optical.sim_play(dev, drives, dt=ss["X1"].loop.dt)
            if solo:
                other = "X2" if solo == "X1" else "X1"
                print(f"{solo} alone: {self._pair_playing[solo]['wname']} plays, {other} parked "
                      f"({self._pair_playing[other]['wname']}, flat at "
                      f"{self._pair_playing[other]['u'][0]:+.4f} V)")
            ends = self._opt_ends(ss, solo) if kw["hold_null"] else None
            if ends:
                print("hold rotation " + ", ".join(f"{k} {v:.1f} deg" for k, v in ends.items())
                      + " (90 deg x target peak / V90): hold-null angles from their sum")

            def progress(done, total, text):
                self.msgs.put(("progress", done, total))
                self.msgs.put(("status", f"optical scan {name}: {text}"))
            try:
                return optical.scan(
                    dev, outdir, name, ss["X1"].t, log=print, progress=progress,
                    cancelled=self.stop_evt.is_set, angles=kw["angles"], shots=kw["shots"],
                    points=kw["points"], sample_us=kw["sample_us"], ref_every=kw["ref_every"],
                    hold_null=kw["hold_null"], ends_deg=ends, settle_s=kw["settle_s"],
                    preset=kw["preset"] or None, extra={"solo": solo},
                    states={"x1": ss["X1"].state_path, "x2": ss["X2"].state_path})
            except Exception as e:
                if type(e).__name__ != "Cancelled":       # rampol.hw.Cancelled
                    raise
                print(f"scan {name} stopped: what was taken is saved; Scan again "
                      f"under the same name resumes it")
                return None
        finally:
            if dev is not None:
                optical.close_devices(dev)
            awg.close()
            scope.close()
            print("instruments closed (outputs left as they were)")

    def _opt_scan_done(self, folder, name, solo=None):
        """A new scan: the step results after it start over, except the
        correction files held (a solo pass collects one per crystal)."""
        o = self._opt
        o.update(scan=folder, scan_name=name, scan_solo=solo, compare=None, verdict=None,
                 applied=None, learned=None, verify=None, table=None, verdict_line=None)
        o["done"] = {1: o["done"].get(1)} if o["done"].get(1) else {}
        o["done"][2] = (f"{datetime.datetime.now():%H:%M}: {name}"
                        + (f" ({solo} alone)" if solo else "") + f" -> {elide(folder, 40)}")
        self._refresh_optical()

    # -- 3 Compare ---------------------------------------------------------
    def do_opt_compare(self):
        if not self._opt_check(3):
            return
        try:
            ck = self._opt_compare_kw()
        except RuntimeError as e:
            return messagebox.showerror("Optical 3", str(e))
        o = self._opt
        ss, folder, solo, name = dict(self.pair), o["scan"], o["scan_solo"], o["scan_name"]

        def work():
            summary, v = self._opt_compare_work(ss, folder, ck, solo=solo)
            self.msgs.put(("call", lambda: self._opt_compare_done(summary, v, name)))
        self.run_worker(work, f"optical 3: comparing {os.path.basename(folder)}"
                        + (f" ({solo} alone)..." if solo else "..."))

    def _opt_compare_work(self, ss, folder, ck, solo=None):
        """Worker side: eomilc.optical.compare on a scan with both states
        (their BASE targets when corrected), and its verdict, logged. solo
        "X1" / "X2": the scan was taken with that crystal alone (what played
        at the scan, not now), so the whole correction is its own and only
        its file is expected."""
        print(f"compare {os.path.basename(folder)} with {ss['X1'].stem} / {ss['X2'].stem}: "
              f"band {ck['band']:g} Hz, PD delay {ck['delay']:g} us, "
              + (f"{solo} alone (split forced)" if solo else f"split X1 {ck['split']:g}"))
        summary = optical.compare(folder, ss["X1"].state_path, ss["X2"].state_path,
                                  band_hz=ck["band"], pd_delay_us=ck["delay"],
                                  split=ck["split"], log=print, figs=ck["figs"], solo=solo)
        want = [PAIR_CHANNEL[solo]] if solo else ["EO1", "EO2"]
        missing = [ch for ch in want if ch not in summary["files"]]
        if missing:
            raise RuntimeError(f"the comparison wrote no correction for {', '.join(missing)}")
        summary["folder"] = folder
        v = optical.verdict(summary)
        for line in optical.verdict_lines(v):
            print("  " + line)
        for ch in want:
            print(f"  {ch}: {summary['files'][ch]}")
        return summary, v

    def _opt_hold_files(self, summary, v, name):
        """Main thread: the comparison's correction files join the ones held
        for Apply -- both from a joint compare, the driving crystal's from a
        solo one (the other crystal's file, if any, is kept)."""
        o = self._opt
        for ch, path in summary["files"].items():
            o["files"][ch] = path
            o["files_from"][ch] = name
            o["files_meta"][ch] = dict(
                band=float(summary.get("f_cut_Hz", 5000.0)),
                peak_V=float(v["correction"]["crystals"].get(ch, {}).get("peak_V", float("nan"))))

    def _opt_compare_done(self, summary, v, name=None):
        o = self._opt
        name = name or o["scan_name"]
        o.update(compare=summary, verdict=v)
        if not summary.get("solo"):
            o["before"] = (name, v)       # only a JOINT comparison is a verify's before
        self._opt_hold_files(summary, v, name)
        parts = []
        for ch in sorted(o["files"]):
            if ch in summary["files"]:
                parts.append(f"{ch} {o['files_meta'][ch]['peak_V']:.1f} V (from {name})")
            else:
                parts.append(f"{ch} from {o['files_from'][ch]} held")
        o["done"][3] = f"{datetime.datetime.now():%H:%M}: " + "; ".join(parts)
        self._plot_optical(summary)
        self._show_fig(self.fig_opt)
        self._refresh_optical()

    # -- 4 Apply -----------------------------------------------------------
    def do_opt_apply(self):
        if not self._opt_check(4):
            return
        try:
            gain = float(self.optgain_var.get())
            if not 0 < gain <= 1.5:
                raise RuntimeError("gain 0..1.5: 1 applies the measured correction, "
                                   "less learns part of it")
            lim = self._corr_settings()
        except ValueError:
            return messagebox.showerror("Optical 4", "gain must be a number")
        except RuntimeError as e:
            return messagebox.showerror("Optical 4", str(e))
        o = self._opt
        # each file at the band of the comparison it came from (the 0.3 ms
        # ramp-top feature needs ~5 kHz), the Corrections tab's limits for
        # everything else; the chains without a file held are left alone
        files, sts, froms = {}, {}, {}
        for k in PAIR_KEYS:
            ch = PAIR_CHANNEL[k]
            if ch not in o["files"]:
                continue
            files[k], froms[k] = o["files"][ch], o["files_from"].get(ch, "?")
            sts[k] = corrmod.GateSettings(
                band_hz=float(o["files_meta"].get(ch, {}).get("band", 5000.0)),
                k_sigma=lim.k_sigma, floor_out=lim.floor_out, cap_out=lim.cap_out,
                fade_s=lim.fade_s, active_only=False)   # the light after the ramp is corrected too (14 Oct 2026)
        # Light minus monitors is a fresh estimate of the WHOLE error every
        # time (moving the target moves the monitors with the light), so a
        # file in the same slot REPLACES the last. A joint verification's file
        # applied where a solo correction already sits goes beside it in its
        # own slot (joint2, joint3, ...) and ADDS to it: the residual the
        # joint run still shows after each crystal's own error was taken out
        # one at a time (14 Oct 2026).
        slots = {}
        for k in files:
            joint = not str(froms[k]).endswith(("_X1", "_X2"))
            lay = self.pair[k].corr_layers or []
            has_solo = any(L.get("slot") == "optical" and ("_X1" in str(L.get("path", ""))
                                                             or "_X2" in str(L.get("path", "")))
                           for L in lay)
            if joint and has_solo:
                n = 2 + sum(str(L.get("slot", "")).startswith("joint") for L in lay)
                slots[k] = f"joint{n}"
        if slots:
            # the joint files say what the WHOLE correction should be: what goes
            # beside the solo layer is each crystal's share of the difference
            # (eomilc.optical.joint_residual_files)
            totals = {PAIR_CHANNEL[kk]: (np.asarray(self.pair[kk].corr_total, float) * self.pair[kk].loop.channel.mon_scale
                                         if self.pair[kk].corr_total is not None else np.zeros(len(self.pair[kk].t)))
                      for kk in PAIR_KEYS}
            jf = {PAIR_CHANNEL[kk]: files[kk] for kk in slots}
            jdir = os.path.dirname(next(iter(jf.values())))
            slot = next(iter(slots.values()))
            try:
                derived = optical.joint_residual_files(jf, totals, float(self.optsplit_var.get() or 0.5), jdir, slot,
                                                       log=self.log)
            except (ValueError, OSError) as e:
                return messagebox.showerror("Optical 4", f"the joint files could not be combined with the solo "
                                                         f"correction: {e}")
            for kk in slots:
                files[kk] = derived[PAIR_CHANNEL[kk]]
                froms[kk] = f"{froms[kk]} ({slot})"
            self.log("optical 4: " + ", ".join(f"{k}: {os.path.basename(files[k])} in slot {s_} beside the solo "
                                               f"correction" for k, s_ in slots.items()))
        ss = dict(self.pair)
        self.run_worker(lambda: self._opt_apply_work(ss, files, gain, sts, froms, slots),
                        "optical 4: applying the corrections...")

    def _opt_apply_work(self, ss, files, gain, sts, froms, slots=None):
        """Worker side: eomilc.optical.apply_correction on each chain's loop
        with the file held for its crystal (files {X1/X2: path}: EO1's to
        X1, EO2's to X2; froms: the scan each came from; sts: the gate
        settings per chain), on top of the session's base target and layers.
        All or nothing: a refusal on X2 puts X1's target back, says why in
        the log and a dialog, and nothing is saved."""
        keys = [k for k in PAIR_KEYS if k in files]
        saved = {}
        for k in keys:
            lp = ss[k].loop
            h = lp.history[-1] if lp.history and isinstance(lp.history[-1], dict) else None
            saved[k] = (np.array(lp.target, float), dict(h) if h is not None else None)
        done, k = {}, None

        def put_back():
            for kk in done:
                lp = ss[kk].loop
                lp.target = saved[kk][0]
                if saved[kk][1] is not None:
                    lp.history[-1] = saved[kk][1]
        print("applying " + ", ".join(f"{PAIR_CHANNEL[k]}'s file from {froms[k]} to {k}"
                                      for k in keys)
              + "".join(f"; {k} has no file held, left as it is" for k in PAIR_KEYS
                        if k not in files))
        try:
            for k in keys:
                s = ss[k]
                done[k] = optical.apply_correction(
                    s.loop, s.t, s.u, s.full_scale, files[k], gain=gain,
                    settings=sts[k], base_target=s.base_target, layers=s.corr_layers,
                    slot=(slots or {}).get(k))
        except optical.CorrectionRefused as exc:
            put_back()
            print(f"{k}: correction REFUSED -- nothing changed on either chain")
            for lev, txt in exc.notes:
                print(f"  {lev.upper()}: {txt}")
            fails = [t for lev, t in exc.notes if lev == "fail"] or [str(exc)]
            msg = (f"{k} ({PAIR_CHANNEL[k]}, from {froms[k]}): not applied, and nothing "
                   f"changed on either chain.\n\n" + "\n\n".join(fails))
            self.msgs.put(("call", lambda: messagebox.showerror("Optical 4 -- refused", msg)))
            return
        except BaseException:
            put_back()
            raise
        for k in keys:
            s, a = ss[k], done[k]
            s.base_target, s.corr_total, s.corr_layers = a.base_target, a.corr_total, a.layers
            save_session(s)
            with self._prefixed(k):
                L = next((x for x in a.layers if x.get("slot") == "optical"), {})
                print(f"target corrected ({os.path.basename(files[k])} from {froms[k]}, "
                      f"x{gain:g}, band {sts[k].band_hz:g} Hz): {a.info['peak_out']:.1f} V "
                      f"peak, {a.info['rms_out']:.1f} V rms at the EOM; resolved over "
                      f"{100*L.get('resolved', 0):.0f}% of the active record; drive "
                      f"peak est. {a.info['drive_peak_est']:.3f} V")
                for lev, txt in a.notes:
                    print(f"  {lev.upper()}: {txt}")
        print(("both targets" if len(keys) == 2 else f"{keys[0]}'s target")
              + " moved, base targets kept in the states; the next measurement is "
                "against the new targets")
        peaks = {k: done[k].info["peak_out"] for k in keys}
        self.msgs.put(("call", lambda: self._opt_apply_done(gain, peaks)))

    def _opt_apply_done(self, gain, peaks):
        o = self._opt
        o["applied"] = dict(gain=gain, peak=peaks)
        o["learned"] = None
        o["done"][4] = (f"{datetime.datetime.now():%H:%M}: x{gain:g}, "
                        + ", ".join(f"{k} {v:.1f} V" for k, v in peaks.items())
                        + " peak on the targets")
        # the Corrections tab shows the shown session's layers, as _sync_shown
        if self.session is not None and self.session in self.pair.values():
            self._corr_pending = [dict(L) for L in self.session.corr_layers]
            self._refresh_corr_list()
            self._replot_corr_quiet()
        self._refresh_summary()
        self._redraw_iterations()
        if o["compare"]:
            self._plot_optical_corr(o["compare"])
        self._refresh_optical()

    # -- 5 Learn -----------------------------------------------------------
    def do_opt_learn(self):
        if not self._opt_check(5):
            return
        w = self._pair_ready("Optical 5")
        if w is None:
            return
        try:
            f = self._floats(iterations=self.optiters_var, repeats=self.repeats_var,
                             wait=self.wait_var)
            if f["iterations"] < 1:
                raise RuntimeError("iterations must be at least 1")
            cfg = self._gather_settings()
        except RuntimeError as e:
            return messagebox.showerror("Optical 5", str(e))
        ss = dict(self.pair)
        it0 = {k: ss[k].iteration for k in PAIR_KEYS}
        skip = bool(self.skip_var.get())

        def work():
            self._pair_bench_work(ss, cfg["pair"], w, int(f["iterations"]),
                                  int(f["repeats"]), f["wait"], skip, keep_on=True)
            self.msgs.put(("call", lambda: self._opt_learn_done(ss, it0)))
        self.run_worker(work, "optical 5: the pair loop on the corrected targets...")

    def _opt_learn_done(self, ss, it0):
        o = self._opt
        it1 = {k: ss[k].iteration for k in PAIR_KEYS}
        # the pair loop uploaded both keepers: a solo play is over
        o["playing"] = self._opt_playing()
        if all(it1[k] > it0[k] for k in PAIR_KEYS):
            o["learned"] = dict(it0=it0, it1=it1)
            o["done"][5] = (f"{datetime.datetime.now():%H:%M}: " + ", ".join(
                f"{k} i{it0[k]:02d} -> i{it1[k]:02d}" for k in PAIR_KEYS))
        else:
            self.log("optical 5: the loop did not advance (see above); Learn again")
        self._refresh_optical()

    # -- 6 Verify ----------------------------------------------------------
    def do_opt_verify(self):
        if not self._opt_check(6):
            return
        w = self._pair_ready("Optical 6")
        if w is None:
            return
        try:
            kw = self._opt_scan_kw()
            ck = self._opt_compare_kw()
        except RuntimeError as e:
            return messagebox.showerror("Optical 6", str(e))
        o = self._opt
        if not o["scan_name"]:
            return messagebox.showerror("Optical 6", "no comparison to verify against: "
                                                     "Scan and Compare (2, 3) first")
        # always a JOINT scan, named after the pass (a solo scan's _X1 / _X2 off)
        name = self._opt_free_name(re.sub(r"_X[12]$", "", o["scan_name"]) + "_verify")
        ss, before = dict(self.pair), o["before"]

        def work():
            folder = self._opt_scan_work(ss, w, name, kw)
            if not folder:
                return
            summary, v = self._opt_compare_work(ss, folder, ck)
            if before is None:
                # a solo-only pass: no joint comparison to put beside it
                print(f"no joint before to compare {name} with (the pass was measured "
                      f"one crystal at a time): table skipped, the verdict above stands "
                      f"alone and is the next pass's before")
                rows, halved = None, False
            else:
                rows, halved = self._opt_table(before[1], v)
                print(f"before / after: before = {before[0]}, after = {name} (mdeg; "
                      f"correction peaks in V)")
                print(f"{'':40s}{'before':>9s}{'after':>9s}")
                for lab, b, a in rows:
                    print(f"{lab:40s}{b:9.1f}{a:9.1f}")
            self.msgs.put(("call", lambda: self._opt_verify_done(
                folder, name, summary, v, rows, halved, before[0] if before else None)))
        self.run_worker(work, f"optical 6: verification scan {name}...")

    @classmethod
    def _opt_table(cls, vb, va):
        """Before / after rows [(label, before, after)] from two verdicts,
        and whether every leg's ramp-top light - target extreme halved."""
        rows, halved = [], []
        after = {L["leg"]: L for L in va["legs"]}
        for Lb in vb["legs"]:
            La = after.get(Lb["leg"])
            if La is None:
                continue
            for key, lab in cls.OPT_ROWS:
                rows.append((f"leg {Lb['leg']} {lab}", float(Lb[key]), float(La[key])))
            halved.append(abs(La["top_lt_mdeg"]) <= 0.5 * abs(Lb["top_lt_mdeg"]))
        for ch, cb in vb["correction"]["crystals"].items():
            ca = va["correction"]["crystals"].get(ch)
            if ca:
                rows.append((f"correction peak {ch} (V at the EOM)", float(cb["peak_V"]),
                             float(ca["peak_V"])))
        return rows, bool(halved) and all(halved)

    def _opt_verify_done(self, folder, name, summary, v, rows, halved, before_name):
        """rows None: no joint before (a solo-only pass) -- no table, no
        before / after view; the verdict is reported and becomes the before."""
        o = self._opt
        stamp = f"{datetime.datetime.now():%H:%M}"
        if rows is None:
            tops = [L["top_lt_mdeg"] for L in v["legs"]]
            o["verdict_line"] = ("verify   ramp top light-target "
                                 + ", ".join(f"{abs(a):.0f}" for a in tops)
                                 + " mdeg (no joint before)")
            o["announce"] = (f"optical verify {name}: ramp-top light - target "
                             + ", ".join(f"{abs(a):.0f}" for a in tops)
                             + " mdeg -- no joint before, no table")
            o["done"][6] = f"{stamp}: {name}: no joint before, the verdict is in the log"
        else:
            tops = [(b, a) for lab, b, a in rows if "ramp top, light - target" in lab]
            word = "halved" if halved else "NOT halved"
            o["verdict_line"] = ("verify   ramp top light-target "
                                 + ", ".join(f"{abs(b):.0f}->{abs(a):.0f}" for b, a in tops)
                                 + f" mdeg: {word}")
            o["announce"] = (f"optical verify {name}: ramp-top light - target "
                             + ", ".join(f"{abs(b):.0f} -> {abs(a):.0f}" for b, a in tops)
                             + f" mdeg -- {word}")
            o["done"][6] = f"{stamp}: {name}: ramp-top extreme {word} (table in the log)"
        o.update(verify=folder, table=rows)
        self._plot_optical(summary)
        if rows is None:
            self._show_fig(self.fig_opt)
        else:
            self._plot_optical_verdict(rows, (before_name, name))
            self._show_fig(self.fig_optva)
        # the verification is the next pass's comparison: Apply takes its
        # correction (it REPLACES the optical layer -- a fresh estimate of the
        # whole correction, not a remainder) and the next Verify compares
        # against it. A joint comparison: both files held come from it.
        o.update(scan=folder, scan_solo=None, compare=dict(summary, folder=folder), verdict=v,
                 before=(name, v), applied=None, learned=None)
        self._opt_hold_files(summary, v, name)
        self._refresh_optical()

    # -- 7 Outputs OFF -----------------------------------------------------
    def do_opt_off(self):
        if not self._opt_check(7):
            return
        w = self._pair_ready("Optical 7")
        if w is None:
            return
        chans = [w[k]["awg_ch"] for k in PAIR_KEYS]

        def work():
            awg, scope = self._connect_pair()
            try:
                self._outputs_off(awg, chans, "Optical step 7")
            finally:
                awg.close()
                scope.close()
                print("instruments closed")
            stamp = datetime.datetime.now().strftime("%H:%M")
            self.msgs.put(("call", lambda: (self._opt["done"].__setitem__(
                7, f"{stamp}: both outputs OFF"), self._refresh_optical())))
        self.run_worker(work, "optical 7: both outputs OFF...")

    # -- the Optical plot tab ----------------------------------------------
    def _plot_optical(self, summary):
        """The rotation view from one comparison (target_compare.npz in its
        folder): per leg the light, the monitors and the target in degrees,
        and light - monitors; then the correction view."""
        ax0, ax1 = self.ax_opt
        for ax in self.ax_opt:
            ax.clear()
        name = summary.get("scan", "?")
        try:
            z = np.load(os.path.join(summary["out"], "target_compare.npz"))
        except (OSError, KeyError, ValueError) as e:
            self.log(f"optical plot: no target_compare.npz ({e})")
            z = None
        if z is not None:
            tl = z["t_leg1"]
            ms = (tl - tl[0]) * 1e3
            dt = float(np.median(np.diff(tl)))
            band = float(summary.get("f_cut_Hz", 5000.0))
            ax0.plot(ms, z["phi_target"], color=TARGET_COLOUR, lw=1.2,
                     label="target (base), time-mapped")
            legs = sorted(int(m.group(1)) for m in
                          (re.match(r"leg(\d+)_phi_light$", f) for f in z.files) if m)
            for k in legs:
                col = self.OPT_LEG_COLOURS[(k - 1) % 2]
                ax0.plot(ms, z[f"leg{k}_phi_light"], color=col, lw=0.9,
                         label=f"light, leg {k}")
                ax0.plot(ms, z[f"leg{k}_phi_mon"], color=col, lw=0.9, ls="--",
                         alpha=0.6, label=f"monitors, leg {k}")
                d = z[f"leg{k}_d_lm"]
                ax1.plot(ms, d * 1e3, color="#bbbbbb", lw=0.5,
                         label=f"light - monitors, leg {k}")
                ax1.plot(ms, corrmod.lowpass(d, dt, band) * 1e3, color=col, lw=1.0,
                         label=f"leg {k} low-passed at {band:g} Hz")
            ax1.plot(ms, -z["correction_deg"] * 1e3, color="k", lw=0.8, ls=":",
                     label="minus the correction")
            ax0.legend(fontsize=7, loc="best")
            ax1.legend(fontsize=7, loc="best")
        ax0.set_ylabel("rotation (deg)")
        ax0.set_title(f"Light, monitors and target: scan {name}")
        ax1.set_ylabel("light - monitors (mdeg)")
        ax1.set_xlabel("time from the leg start (ms)")
        for ax in self.ax_opt:
            ax.grid(True, alpha=0.3)
        self._draw(self.fig_opt)
        self._plot_optical_corr(summary)

    def _plot_optical_corr(self, summary):
        """The correction view: each crystal's file (V at the EOM, +-1
        sigma), and what is on the target now when a correction is applied."""
        ax = self.ax_optcorr
        ax.clear()
        files = summary.get("files") or {}
        for k in PAIR_KEYS:
            ch = PAIR_CHANNEL[k]
            col = CH_DEFAULTS[ch]["colour"]
            p = files.get(ch)
            if p and os.path.exists(p):
                c = corrmod.read_correction(p)
                tm = c.t * 1e3
                if c.sigma is not None:
                    ax.fill_between(tm, c.delta - c.sigma, c.delta + c.sigma, color=col,
                                    alpha=0.2, lw=0)
                ax.plot(tm, c.delta, color=col, lw=1.0, label=f"{ch}: {os.path.basename(p)}")
            s = (self.pair or {}).get(k)
            if s is not None and s.corr_total is not None:
                g = [L.get("gain", 1.0) for L in s.corr_layers if L.get("slot") == "optical"
                     and L.get("status") == "applied"]
                ax.plot(s.t * 1e3, s.corr_total * s.loop.channel.mon_scale, color=col,
                        lw=0.9, ls="--", label=f"{ch}: target now - base target"
                        + (f" (optical x{g[0]:g})" if g else ""))
        ax.set_ylabel("change at the EOM (V)")
        ax.set_xlabel("time in the record (ms)")
        ax.set_title(f"Target correction per crystal, -(light - monitors): scan "
                     f"{summary.get('scan', '?')}")
        if ax.lines:
            ax.legend(fontsize=7, loc="best")
            self._plot_note(ax, "shaded: +-1 sigma of the file; dashed: after the gate "
                                "and the gain")
        ax.grid(True, alpha=0.3)
        self._draw(self.fig_optcorr)

    def _plot_optical_verdict(self, rows, names):
        """The before / after view: per leg the magnitudes the verdict reads
        (light - target at the ramp top, in the first ms after the fall, its
        rms; light - monitors at the ramp top), before and after."""
        ax = self.ax_optva
        ax.clear()
        lr = [r for r in rows if r[0].startswith("leg ")]
        y = np.arange(len(lr))[::-1]
        for yi, (_lab, b, a) in zip(y, lr):
            ax.plot([abs(b), abs(a)], [yi, yi], color="#cccccc", lw=2, zorder=1)
        if lr:
            ax.plot([abs(r[1]) for r in lr], y, "o", color="#7f7f7f", zorder=2,
                    label=f"before: {names[0]}")
            ax.plot([abs(r[2]) for r in lr], y, "o", color="#1f77b4", zorder=2,
                    label=f"after: {names[1]}")
            ax.set_yticks(y)
            ax.set_yticklabels([r[0] for r in lr], fontsize=7)
            ax.legend(fontsize=7, loc="lower right")
        ax.set_xlim(left=0)
        ax.set_xlabel("magnitude (mdeg), low-passed at the correction band")
        ax.set_title(f"Light against the base target and the monitors: {names[0]} "
                     f"and {names[1]}")
        ax.grid(True, axis="x", alpha=0.3)
        self._draw(self.fig_optva)

    # ---------------------------------------------------------------- batch
    def do_batch(self):
        if self._batch_win is None:
            self._batch_win = BatchWindow(self)
        else:
            self._batch_win.win.deiconify()
            self._batch_win.win.lift()

    def do_batch_run(self, mode):
        """mode: 'check' (no instruments), 'sim' (eomilc.simbench, files in
        run/sim), 'bench' (both Treks, after one confirmation)."""
        if self.busy:
            return
        w = self._batch_win
        path = self.batchplan_var.get().strip()
        if not path or not os.path.exists(path):
            return messagebox.showerror("Batch", "pick a plan file first "
                                        "(or make one with New plan...)",
                                        parent=w.win)
        try:
            over = w.loop_overrides()
            plan = ilc_batch.load_plan(path, cfg=self.cfg)
        except (ValueError, OSError, SystemExit) as e:
            return messagebox.showerror("Batch", str(e), parent=w.win)
        # The plan file is the record of how a batch was trained: numbers
        # typed in the window are written into it before anything runs.
        changed = {k: v for k, v in over.items() if plan["loop"].get(k) != v}
        if changed and mode != "check":
            with open(path) as f:
                raw = json.load(f)
            raw.setdefault("loop", {}).update(changed)
            with open(path, "w") as f:
                json.dump(raw, f, indent=1)
            self.log("plan updated: " + ", ".join(
                f"{k} {plan['loop'].get(k)} -> {v}" for k, v in changed.items()))
            plan = ilc_batch.load_plan(path, cfg=self.cfg)
        redo, gui = self.batchredo_var.get(), self.batchgui_var.get()
        if mode == "bench":
            man = ilc_batch.read_manifest(plan)
            todo = [c["stem"] for c in plan["campaigns"] if redo or
                    man["campaigns"].get(c["stem"], {}).get("status") != "done"]
            if not todo:
                return messagebox.showinfo(
                    "Batch", "every campaign in this plan is done (tick 'also "
                    "finished campaigns' to continue them)", parent=w.win)
            est = w.estimate_s(todo)
            lp = plan["loop"]
            if not messagebox.askyesno("Run the batch on the bench", (
                    f"{len(todo)} campaign(s): {', '.join(todo)}\n\n"
                    f"For each one: both AWG outputs OFF, FRQ set, both drives "
                    f"uploaded and the channel setup checked (AMP, OFST, load, "
                    f"burst), then CH1 and CH2 switched ON to drive both Treks, "
                    f"trained for up to {lp['max_iterations']} iterations, and "
                    f"switched OFF again before the next.\n\n"
                    + (f"At most ~{est/3600:.1f} h (from Check plan). " if est else "")
                    + "Stop ends it at the next shot: outputs off, states "
                    "kept, and running again resumes.\n\n"
                    "Switch the outputs ON for this batch?"), parent=w.win):
                return
        self.run_worker(lambda: self._batch_work(path, mode, redo, gui, over),
                        {"check": "checking the plan...",
                         "sim": "batch running (SIMULATED)...",
                         "bench": "batch running on the bench..."}[mode])

    def _batch_work(self, path, mode, redo, gui_names, over):
        import types
        plan = ilc_batch.load_plan(path, cfg=self.cfg)
        plan["loop"].update(over)       # Check plan: the numbers in the window
        if mode == "check":
            rows = ilc_batch.plan_rows(plan)
            lp = plan["loop"]
            print(f"plan {plan.get('name', '')}: {len(rows)} campaigns; "
                  f"{lp['max_iterations']} iterations max (min "
                  f"{lp['min_iterations']}), stop at {lp['stop_rms_v']} V rms, "
                  f"{lp['repeats']} shots")
            for r in rows:
                if r["error"]:
                    print(f"  {r['stem']}: {r['error']}")
            bad = sum(not r["ok"] for r in rows)
            print(f"  ~{sum(r['est_s'] for r in rows)/3600:.1f} h at the "
                  f"iteration cap; {bad} campaign(s) with problems")
            self.msgs.put(("call", lambda: self._batch_win and
                           self._batch_win.set_rows(rows)))
            return
        sim = mode == "sim"
        bench = None if sim else self._connect_pair()
        opts = types.SimpleNamespace(simulate=sim, allow_output_on=True,
                                     redo=redo, gui_names=gui_names)
        ilc_batch.run_batch(
            plan, opts, log=print, stop=self.stop_evt, bench=bench,
            on_event=lambda k, i: self.msgs.put(
                ("call", lambda: self._batch_event(k, i))))

    # -- batch: Preview (what the plan will fit to, before it runs) ---------
    def do_batch_preview(self):
        """The Batch window's Preview: the plan loaded as Check plan loads it
        (echo records built), then every campaign's targets on the Batch
        tab -- or, with a campaign selected in the table, that campaign
        alone with the first drive it will play and its limit check."""
        if self.busy:
            return
        w = self._batch_win
        path = self.batchplan_var.get().strip()
        if not path or not os.path.exists(path):
            return messagebox.showerror("Batch", "pick a plan file first "
                                        "(or make one with New plan...)",
                                        parent=w.win)
        try:
            over = w.loop_overrides()
        except ValueError as e:
            return messagebox.showerror("Batch", str(e), parent=w.win)
        sel = w.tree.selection()
        stem = sel[0] if sel else None

        def work():
            plan = ilc_batch.load_plan(path, cfg=self.cfg)
            plan["loop"].update(over)
            camps = {c["stem"]: c for c in plan["campaigns"]}
            if stem is not None and stem not in camps:
                print(f"preview: {stem} is not in the plan any more (reloaded "
                      f"from disk); previewing the whole plan")
            if stem in camps:
                d = self._batch_preview_one(plan, camps[stem])
            else:
                d = self._batch_preview_all(plan)
            self.msgs.put(("call", lambda: self._batch_preview_show(d)))
        self.run_worker(work, "previewing the plan..." if stem is None
                        else f"previewing {stem}...")

    @staticmethod
    def _batch_flags(plan, camp):
        """' optical' / ' echo of <source>' for a preview panel's title."""
        s = ""
        try:
            if ilc_batch.optical_settings(plan, camp) is not None:
                s += " optical"
        except ilc_batch.BatchError:
            s += " optical (refused)"
        e = camp.get("_echo") or {}
        if e.get("source") and not e.get("error"):
            s += f" echo of {e['source']}"
        return s

    @staticmethod
    def _batch_record(stem, n, period_ms):
        if not n:
            return stem
        return (f"{stem}  {n} pts  {period_ms:.3f} ms  "
                f"FRQ {1e3 / period_ms:.3f} Hz")

    def _batch_preview_all(self, plan):
        """Worker side: one panel's worth per campaign (targets in kV at the
        EOM against ms, or the row's error), the models and the total."""
        rows = ilc_batch.plan_rows(plan)
        lp = plan["loop"]
        panels, bad = [], 0
        for camp, r in zip(plan["campaigns"], rows):
            flags = self._batch_flags(plan, camp)
            bad += (not r["ok"]) or "refused" in flags
            p = dict(stem=r["stem"], error=r["error"], traces={},
                     title=self._batch_record(r["stem"], r["n"], r["period_ms"]) + flags)
            if r["n"]:
                for key in camp.get("channels", list(PAIR_KEYS)):
                    tp = os.path.join(plan["_targets_dir"], f"target_{camp['stem']}{key}.csv")
                    t, v = run_ilc.load_target(tp, 1.0)       # voltage_V: V at the EOM
                    p["traces"][key] = (t * 1e3, v / 1e3)
            if r["error"]:
                print(f"  {r['stem']}: {r['error']}")
            panels.append(p)
        models = []
        for k in PAIR_KEYS:
            try:
                models.append(ilc_batch.model_line(plan, k))
            except ilc_batch.BatchError as e:
                models.append(f"model {k}: REFUSED: {e}")
        total = (f"total ~{sum(r['est_s'] for r in rows) / 3600:.1f} h at "
                 f"{lp['max_iterations']} iterations each (early stops shorten it; "
                 f"an optical stage adds its scans); {bad} campaign(s) with problems")
        print(f"preview {plan.get('name', '')}: {len(panels)} campaigns")
        for line in models + [total]:
            print("  " + line)
        return dict(kind="all", name=plan.get("name", ""), panels=panels,
                    models=models, total=total)

    def _batch_preview_one(self, plan, camp):
        """Worker side: one campaign -- both targets, the ramp marks (the
        card split's transports and common flat top), the first drive each
        channel plays and its limit check."""
        from eomilc import echo
        keys = camp.get("channels", list(PAIR_KEYS))
        d = dict(kind="one", stem=camp["stem"], runs={}, marks=[], error="",
                 flags=self._batch_flags(plan, camp), title=camp["stem"])
        try:
            runs = [ilc_batch.build_run(plan, camp, k, log=lambda s: None) for k in keys]
        except (ilc_batch.BatchError, ValueError, OSError) as e:
            d["error"] = str(e)
            d["title"] += d["flags"]
            print(f"preview {camp['stem']}: {e}")
            return d
        n = len(runs[0].t)
        d["title"] = self._batch_record(camp["stem"], n, n * runs[0].loop.dt * 1e3) + d["flags"]
        for cr in runs:
            rep = cr.loop.check(cr.u)
            lim, sc = cr.loop.limits, cr.channel.mon_scale
            slew = float(np.abs(np.gradient(cr.loop.target * sc, cr.loop.dt)).max())
            d["runs"][cr.key] = dict(
                tms=cr.t * 1e3, target_kv=cr.loop.target * sc / 1e3, u=np.asarray(cr.u, float),
                it=cr.iteration, ok=bool(rep), slew_v_us=slew / 1e6,
                i_ma=lim.load_capacitance * slew * 1e3,
                v90_kv=getattr(cr.channel, "v90_hv", 0.0) / 1e3,
                corrected=cr.corr_total is not None)
            print(f"preview {cr.stem} drive i{cr.iteration:02d}: {rep}")
        try:
            sfc = echo._card()
            t_us = runs[0].t * 1e6
            vs = [cr.loop.target * cr.channel.mon_scale for cr in runs]
            if all(len(v) == len(t_us) for v in vs):
                for i0, i1 in sfc.find_transports(vs, float(np.median(np.diff(t_us)))):
                    tops = [sfc.flat_top(t_us[i0:i1 + 1], v[i0:i1 + 1]) for v in vs]
                    a, b = max(x[0] for x in tops), min(x[1] for x in tops)
                    d["marks"].append(dict(t0=t_us[i0] / 1e3, t1=t_us[i1] / 1e3,
                                           a=a / 1e3, b=b / 1e3, common=a <= b))
        except Exception as e:              # the marks are a guide, not a check
            print(f"preview {camp['stem']}: no ramp marks ({e})")
        return d

    def _batch_preview_show(self, d):
        """Main thread: draw a preview on the Batch tab and bring it up."""
        fig = self.fig_batch
        fig.clear()
        if d["kind"] == "all":
            self._batch_preview_grid(fig, d)
            n = len(d["panels"])
            txt = f"previewed {n} campaign{'' if n == 1 else 's'}"
        else:
            self._batch_preview_single(fig, d)
            txt = f"previewed {d['stem']}"
        fig._batch_preview = d
        self._show_fig(fig)
        self._draw(fig)
        w = self._batch_win
        if w is not None and w.win.winfo_exists():
            w.status.configure(text=txt)

    BATCH_ERR = "#c62828"

    def _batch_preview_grid(self, fig, d):
        import textwrap
        panels = d["panels"]
        n = max(len(panels), 1)
        ncol = min(3, n)
        nrow = -(-n // ncol)
        axs = fig.subplots(nrow, ncol, squeeze=False).ravel()
        for ax in axs[len(panels):]:
            fig.delaxes(ax)
        small = nrow > 3
        fs_t, fs_k = (6.0, 5.5) if small else (8.0, 7.0)
        legend_done = False
        for ax, p in zip(axs, panels):
            ax.set_title(p["title"], fontsize=fs_t, loc="left")
            ax.tick_params(labelsize=fs_k, pad=1)
            for key, (tms, kv) in p["traces"].items():
                ax.plot(tms, kv, color=CH_DEFAULTS[PAIR_CHANNEL[key]]["colour"],
                        lw=1.3 if key == "X1" else 0.8, label=key)
            if p["traces"]:
                tms = next(iter(p["traces"].values()))[0]
                ax.set_xlim(tms[0], tms[-1])
                ax.axhline(0, color=TARGET_COLOUR, lw=0.4)
                ax.grid(True, alpha=0.3)
                if not legend_done:
                    ax.legend(fontsize=fs_k, loc="upper right", handlelength=1.2)
                    legend_done = True
            else:
                ax.set_xticks([])
                ax.set_yticks([])
            if p["error"]:
                ax.text(0.5, 0.06 if p["traces"] else 0.5,
                        textwrap.fill(p["error"], 55 if ncol == 3 else 90),
                        transform=ax.transAxes, ha="center",
                        va="bottom" if p["traces"] else "center",
                        color=self.BATCH_ERR, fontsize=fs_k)
        fig.supylabel("target (kV at the EOM)", fontsize=8)
        fig.supxlabel("time (ms)\n" + "\n".join(d["models"] + [d["total"]]),
                      x=0.01, ha="left", fontsize=6.5, color="#444444")

    def _batch_preview_single(self, fig, d):
        import textwrap
        ax_t, ax_u = fig.subplots(2, 1, sharex=True)
        ax_t.set_title(d["title"], fontsize=9, loc="left")
        if d["error"]:
            fig.delaxes(ax_u)
            ax_t.set_xticks([])
            ax_t.set_yticks([])
            ax_t.text(0.5, 0.5, textwrap.fill(d["error"], 100), transform=ax_t.transAxes,
                      ha="center", va="center", color=self.BATCH_ERR, fontsize=8)
            return
        for key, r in d["runs"].items():
            col = CH_DEFAULTS[PAIR_CHANNEL[key]]["colour"]
            ax_t.plot(r["tms"], r["target_kv"], color=col, lw=1.6 if key == "X1" else 1.0,
                      label=f"{key} target" + (" (with learned corrections)"
                                               if r["corrected"] else ""))
            if r["v90_kv"]:
                ax_t.axhline(r["v90_kv"], color=col, lw=0.6, ls=":",
                             label=f"{key} V90 {r['v90_kv'] * 1e3:.1f} V")
            ax_u.plot(r["tms"], r["u"], color=col, lw=1.4 if key == "X1" else 1.0,
                      label=f"{key} drive i{r['it']:02d}: {'PASS' if r['ok'] else 'FAIL'}, "
                            f"peak slew {r['slew_v_us']:.2f} V/us, "
                            f"peak current {r['i_ma']:.2f} mA")
        tr = ax_t.get_xaxis_transform()
        for m in d["marks"]:
            xs = (m["t0"], m["a"], m["b"], m["t1"]) if m["common"] else (m["t0"], m["t1"])
            for ax in (ax_t, ax_u):
                for x in xs:
                    ax.axvline(x, color="#777777", lw=0.5, zorder=1)
            if m["common"]:
                for lab, x0, x1 in (("rise", m["t0"], m["a"]), ("hold", m["a"], m["b"]),
                                    ("fall", m["b"], m["t1"])):
                    ax_t.annotate(f"{lab} {x1 - x0:.3f} ms", ((x0 + x1) / 2, 0.07),
                                  xycoords=tr, ha="center", va="bottom", fontsize=6.5,
                                  color="#555555")
            else:
                ax_t.annotate("no common flat top", ((m["t0"] + m["t1"]) / 2, 0.07),
                              xycoords=tr, ha="center", va="bottom", fontsize=6.5,
                              color="#555555")
        for ax in (ax_t, ax_u):
            ax.axhline(0, color=TARGET_COLOUR, lw=0.5)
            ax.grid(True, alpha=0.3)
            ax.legend(fontsize=7, loc="best")
            ax.set_xlabel("time (ms)")
            ax.tick_params(labelbottom=True)
        tms = next(iter(d["runs"].values()))["tms"]
        ax_t.set_xlim(tms[0], tms[-1])
        ax_t.set_ylim(top=ax_t.get_ylim()[1] * 1.12)          # room for the note
        ax_t.set_ylabel("target (kV at the EOM)")
        ax_u.set_ylabel("first drive (AWG V)")
        v90 = " / ".join(f"{r['v90_kv'] * 1e3:.0f}" for r in d["runs"].values() if r["v90_kv"])
        self._plot_note(ax_t, "look for: both channels on one grid, the idle at 0 at both "
                              "ends, the peaks at V90" + (f" ({v90} V)" if v90 else "")
                        + ", no edge faster than 0.3 ms")

    def _batch_event(self, kind, info):
        """Main thread: a batch event, for the window (if open), the progress
        bar and the plots."""
        if kind == "campaign":
            self.progress.configure(maximum=max(info["n"], 1),
                                    value=info["index"])
            self.status.configure(text=f"batch: {info['stem']} "
                                       f"({info['index'] + 1}/{info['n']})")
        elif kind == "saved" and self.batchfollow_var.get():
            self._batch_follow(info["states"])
        if self._batch_win is not None:
            self._batch_win.event(kind, info)

    def _batch_follow(self, states):
        if self.pair_var.get() and all(os.path.exists(states.get(k, ""))
                                       for k in PAIR_KEYS):
            return self._batch_follow_pair(states)
        path = states.get(self.batchfollowch_var.get()) or next(iter(states.values()))
        if not os.path.exists(path):
            return
        if self.session is not None and _same_file(self.session.state_path, path):
            # same campaign: only the measurements moved on -- reload quietly
            s = load_session(path)
            recall_snapshots(s)
            self.session = s
            self._refresh_summary()
            self._show_session()
        else:
            self.state_var.set(path)
            self.do_load()

    def _batch_follow_pair(self, states):
        """Pair mode follows a batch campaign as a pair: both states, shown
        on the Batch window's follow channel."""
        k = self.batchfollowch_var.get() if self.batchfollowch_var.get() in PAIR_KEYS else "X1"
        if self.pair is not None and all(_same_file(self.pair[c].state_path, states[c])
                                         for c in PAIR_KEYS):
            for c in PAIR_KEYS:            # same campaign: reload both quietly
                s = load_session(states[c])
                recall_snapshots(s)
                self.pair[c] = s
            self.shown_var.set(k)
            self.session = self.pair[k]
            self._refresh_summary()
            self._show_session()
            return
        self.state_var.set(states["X1"])
        self.do_load()
        if self.pair is not None and k != self.shown_var.get():
            self.shown_var.set(k)
            self._on_shown_change()

    def do_measure_frf(self):
        """Automated system ID: build a Schroeder multitone on the session's
        grid, play it through the bench, and fit the FRF from the scope's
        own drive+monitor channels -- the panel version of sysid_make +
        sysid_fit, with the band adjustable. Uses the bench panel's
        AWG/scope channels, repeats, and wait."""
        if self.session is None:
            return messagebox.showerror("Measure FRF",
                                        "load or init a session first -- the "
                                        "probe is built on its time grid")
        s = self.session
        suffix = {"EO1": "X1", "EO2": "X2"}.get(s.channel, "GN")
        dlg = tk.Toplevel(self.root)
        dlg.title("Measure FRF")
        dlg.transient(self.root)
        dlg.grab_set()
        fr = ttk.Frame(dlg, padding=8)
        fr.pack(fill="both", expand=True)
        nyq = 0.5 / s.loop.dt
        fields = (("probe peak V (at the AWG)", "2.0"),
                  ("f lo Hz", "400"),
                  ("f hi Hz", "100e3"),
                  ("tones", "72"),
                  ("name", f"AUTO{suffix}"),
                  ("hold V (ramped only)", "0.0"),
                  ("hold ms (ramped only)", "4.0"))
        fvars = {}
        for i, (lab, dv) in enumerate(fields):
            ttk.Label(fr, text=lab).grid(row=i, column=0, sticky="w")
            fvars[lab] = tk.StringVar(value=dv)
            ttk.Entry(fr, textvariable=fvars[lab], width=12).grid(
                row=i, column=1, sticky="w")
        ttk.Label(fr, foreground="#666666", text=(
            f"Tones sit on integer bins of the probe's record. Up to "
            f"{PROBE_NYQ_MARGIN*nyq/1e3:.0f} kHz the probe reuses this\nsession's "
            f"{len(s.t)*s.loop.dt*1e3:.3f} ms record; higher bands get a "
            f"denser record (DDS plays the same\nperiod), and past the arb "
            f"memory a shorter one (FRQ + scope window changed for\nthe "
            f"run, restored after; low-f bins coarsen). Sanity ceiling "
            f"5 MHz. Uses the bench\npanel's channels/repeats/wait; "
            f"output-on asks first, off at the end.\n"
            f"Writes run\\frf_<name>.csv and points the FRF field at it.")
            # BELOW the fields, not at a fixed row: this note sat at row 5
            # from the days of five fields, and when the two ramped ones were
            # added at rows 5 and 6 it landed on top of them -- the operating
            # point and its hold time were there, and invisible.
            ).grid(row=len(fields), column=0, columnspan=2, sticky="w",
                   pady=(4, 0))

        ramp_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(fr, variable=ramp_var, text=(
            "ramped: hold at an operating point and probe THERE, not around "
            "zero")).grid(row=len(fields) + 1, column=0, columnspan=2,
                          sticky="w", pady=(6, 0))
        ttk.Label(fr, foreground="#666666", justify="left", text=(
            "The chain is not the same at 5 kV as at 0 V -- the EOM "
            "capacitance is voltage dependent, and at the ramp corners it\n"
            "answers a drive ripple with several times what the zero-centred "
            "FRF predicts, which is what makes the loop over-correct\n"
            "there. The record ramps to 'hold V', holds it for 'hold ms' "
            "with the tones on top, and comes back, all within the\n"
            "burst -- the EOMs are never parked at a standing kV, and the "
            "session's own record, FRQ and scope window are kept.\n"
            "Tones sit on integer bins of the HOLD, so resolution is 1/hold "
            "and f hi is capped by the session grid.")
            ).grid(row=len(fields) + 2, column=0, columnspan=2, sticky="w")

        def ok():
            try:
                peak = float(fvars["probe peak V (at the AWG)"].get())
                f_lo = float(fvars["f lo Hz"].get())
                f_hi = float(fvars["f hi Hz"].get())
                tones = int(float(fvars["tones"].get()))
                name = fvars["name"].get().strip()
                if not name:
                    raise RuntimeError("name the probe")
                if peak <= 0 or peak > s.full_scale:
                    raise RuntimeError(f"probe peak must be 0 < peak <= "
                                       f"full scale ({s.full_scale:g} V)")
                lim = s.loop.limits          # this channel's, not the Trek's
                if ramp_var.get():
                    mode, n_p, dt_p = "session", len(s.t), s.loop.dt
                    u, bins, window = build_ramped_probe(
                        n_p, dt_p, peak, float(fvars["hold V (ramped only)"].get()),
                        float(fvars["hold ms (ramped only)"].get()),
                        f_lo, f_hi, tones, awg_rail=lim.awg_rail)
                else:
                    mode, n_p, dt_p = plan_frf_grid(len(s.t), s.loop.dt, f_hi)
                    u, bins = build_frf_probe(n_p, dt_p, peak, f_lo, f_hi, tones)
                    window = None
                slew, i_pk, hv_pk = probe_demand(u, dt_p, s.loop.plant.gain,
                                                 s.loop.channel, lim)
                if hv_pk > lim.hv_max:
                    raise RuntimeError(
                        f"flat-gain peak output {hv_pk:.0f} V exceeds the "
                        f"{lim.hv_max:.0f} V limit -- lower the probe peak")
                f = self._floats(awg_ch=self.awgch_var,
                                 scope_ch=self.scopech_var,
                                 repeats=self.repeats_var, wait=self.wait_var)
                fmax_now = float(self.fmax_var.get() or 0)
            except (RuntimeError, ValueError) as e:
                return messagebox.showerror("Measure FRF", str(e),
                                            parent=dlg)
            if not self._wiring_ok(f["awg_ch"], f["scope_ch"], "Measure FRF",
                                   need_drive=True):
                return
            drive_ch = self._drive_scope_ch(f["awg_ch"])
            if (slew > lim.slew_hv or i_pk > lim.current) and \
                    not messagebox.askyesno(
                        "Probe demand", parent=dlg, message=(
                    f"Under a FLAT-GAIN worst case this probe asks the "
                    f"amplifier for {slew/1e6:.0f} V/us "
                    f"({i_pk*1e3:.1f} mA into "
                    f"{lim.load_capacitance*1e12:.0f} pF) -- past the "
                    f"{lim.slew_hv/1e6:.0f} V/us / "
                    f"{lim.current*1e3:.0f} mA limits configured for "
                    f"{s.channel} (the 610E specs on the Trek chains).\n\n"
                    f"The Trek cannot actually exceed its own limits: above "
                    f"its band the amp's slew/current limiting caps what "
                    f"flows, so the real cost is time spent limiting during "
                    f"the burst -- distortion and dead coherence up there, "
                    f"before any hazard. The validated 24 kHz probes "
                    f"(2 V and 6 V, played 24 Aug) carried 10-30 mA on this "
                    f"same measure.\n\nHalving the peak halves the demand. "
                    f"Run this probe?")):
                return
            dlg.destroy()
            self.run_worker(
                lambda: self._measure_frf_work(u, bins, name,
                                               int(f["awg_ch"]),
                                               int(f["scope_ch"]),
                                               int(f["repeats"]), f["wait"],
                                               fmax_now, mode, dt_p, window,
                                               drive_ch=int(drive_ch)),
                "measuring FRF...")

        bb = ttk.Frame(fr)
        bb.grid(row=len(fields) + 3, column=0, columnspan=2, sticky="ew",
                pady=(6, 0))
        ttk.Button(bb, text="Measure", command=ok).pack(side="left",
                                                        expand=True, fill="x")
        ttk.Button(bb, text="Cancel", command=dlg.destroy).pack(
            side="left", expand=True, fill="x")

    def _measure_frf_work(self, u, bins, name, awg_ch, scope_ch, repeats,
                          wait_s, fmax_now, mode="session", dt_p=None,
                          window=None, drive_ch=None):
        s = self.session
        dt_p = dt_p or s.loop.dt
        # the drive is read back on the CmdXn channel of the role map
        # (callers before the map passed none: then scope CH<awg_ch>)
        drive_ch = int(awg_ch) if drive_ch is None else int(drive_ch)
        print("scope channels: " + chanmap.describe(self.cfg).replace("\n", " | "))
        n_p = len(u)
        rec = n_p * dt_p                     # the PROBE's record
        # The tones are integer bins of whatever is ANALYSED: the whole
        # record for a flat probe, the hold for a ramped one. Every
        # frequency below comes from that, not from the record.
        rec_a = (window[1] - window[0]) * dt_p if window else rec
        t_sess = len(s.t) * s.loop.dt        # the session's record
        t_grid = np.arange(n_p) * dt_p
        _, awgmod = self._bench_modules()
        limit = getattr(awgmod, "MAX_ARB_NAME", NAME_LIMIT)
        if len(name) > limit:
            raise RuntimeError(f"name {name!r} is past the {limit}-char cap")
        awg, scope = self._connect_pair()
        uploaded = False
        switched_on = False
        retune = False           # short mode changed FRQ + scope window
        vert_saved = None        # ramp verticals, put back at the end
        try:
            problems, notes = ilc_bench.check_awg_channel(
                awg, awg_ch, full_scale=s.full_scale,
                expect_period=t_sess)   # the channel must still hold the
            for nn in notes:            # session's period before a probe
                print("      ", nn)
            acq = scope.get(":ACQuire:TYPE")
            print(f"       scope acquisition {acq}, {repeats} repeats")
            if not acq.upper().startswith("HRES"):
                problems.append(f"scope is in {acq}; use HRES")
            if problems:
                print("Setup problems:")
                for p in problems:
                    print("  !", p)
                print("REFUSING to play the probe -- fix the setup "
                      "(Auto-set instruments does it). The FRF has no "
                      "'skip': a wrong AMP rescales H silently.")
                return
            if mode == "short" and awg.is_on(awg_ch):
                print(f"REFUSING: this band needs a shorter record, which "
                      f"changes FRQ -- and FRQ must not move under a live "
                      f"output. Switch CH{awg_ch} OFF; the run asks before "
                      f"switching it back on.")
                return
            stored = awg.list_waveforms(user_only=True)
            clash = next((n for n in stored
                          if n.lower() == name.lower()), None)
            if clash and not self.ask_user(
                    "Overwrite stored waveform?",
                    f"'{clash}' already exists in the generator's user "
                    f"memory; uploading the probe replaces it (the generator "
                    f"cannot read a waveform back out).\n\nOverwrite?"):
                print(f"FRF cancelled: {clash} left as stored")
                return
            n_pts, frac = ilc_bench.upload_drive(awg, awg_ch, name, u,
                                                 s.full_scale)
            uploaded = True
            print(f"probe uploaded: {name}, {n_pts} pts on a "
                  f"{rec*1e3:.3f} ms record (effective dt {dt_p*1e6:.3g} us"
                  + (", the session's own grid" if mode == "session" else
                     f" vs the session's {s.loop.dt*1e6:g} us -- DDS plays "
                     f"the denser record over the same period" if
                     mode == "dense" else
                     f"; record shortened from {t_sess*1e3:.3f} ms, "
                     f"frequency bins coarsen to {1/rec:.0f} Hz") + ")")
            print(f"  {len(bins)} tones {bins[0]/rec_a:.0f} Hz - "
                  f"{bins[-1]/rec_a/1e3:.1f} kHz, peak {np.abs(u).max():.3f} V "
                  f"({100*frac:.1f}% of DAC range)")
            if window:
                print(f"  ramped probe: tones ride a hold of "
                      f"{rec_a*1e3:.3f} ms ({window[0]*dt_p*1e3:.3f}-"
                      f"{window[1]*dt_p*1e3:.3f} ms into the record), analysed "
                      f"over the hold alone -- bins are {1/rec_a:.0f} Hz apart "
                      f"and H is the chain AT that operating point, not "
                      f"around zero")
            if mode == "short":
                # FRQ while the output is still off (checked above), in the
                # ordering apply_channel honours; scope window to match
                missed = awg.apply_channel(
                    awg_ch, {"BSWV": {"FRQ": 1.0 / rec}},
                    log=lambda m: print("      ", m))
                if missed:
                    raise RuntimeError(f"FRQ for the probe record did not "
                                       f"take: {missed}")
                rng = nice_setting(1.3 * rec)
                scope.put(":TIMebase:REFerence", "CENTer")
                scope.put(":TIMebase:RANGe", f"{rng:.6g}")
                scope.put(":TIMebase:POSition", f"{rec/2:.6g}")
                retune = True
                print(f"  AWG FRQ -> {1.0/rec:.6g} Hz, scope window -> "
                      f"{rng*1e3:.4g} ms (both restored at the end)")
            # verticals for the probe: bipolar around ZERO, not the ramp's
            # window -- the session verticals leave the probe in 1-2 of the
            # 8 divisions and burn bits the 8-bit codes cannot spare
            vert_saved = {}
            for chn in (drive_ch, scope_ch):
                vert_saved[chn] = (
                    float(scope.get(f":CHANnel{chn}:SCALe")),
                    float(scope.get(f":CHANnel{chn}:OFFSet")))
            pk = float(np.abs(u).max())
            mon_pk = 2.0 * pk * s.loop.plant.gain
            for chn, span in ((drive_ch, 2 * pk), (scope_ch, 2 * mon_pk)):
                scale = nice_setting(1.25 * span / 8)
                scope.put(f":CHANnel{chn}:SCALe", f"{scale:.6g}")
                scope.put(f":CHANnel{chn}:OFFSet", "0")
            print(f"scope verticals for the probe: drive +/-{pk:.3g} V, "
                  f"monitor +/-{mon_pk:.3g} V (2x flat-gain headroom for "
                  f"resonant peaking), centred on 0 -- restored at the end")
            ok, switched_on = self._ensure_output_on(awg, awg_ch)
            if not ok:
                return
            pd_ch = pd_channel(self.cfg)
            res = self._frf_capture(scope, drive_ch, scope_ch, bins,
                                    t_grid, s.t_off, repeats, wait_s,
                                    f_top=bins[-1] / rec_a, aux_ch=pd_ch,
                                    window=window)
            H, coh = res["mon"]
            f_hz = bins / rec_a
            path = os.path.join(RUN_DIR, f"frf_{name}.csv")
            write_frf_csv(path, f_hz, H, coh)
            good = int((coh >= 0.9).sum())
            print(f"wrote {path}")
            print(f"  {len(bins)} tones, {good} with coherence >= 0.9; "
                  f"|H| {np.abs(H).max():.4f} max, "
                  f"{np.abs(H).min():.4f} min")
            if res["aux"] is not None:
                Hp, cohp = res["aux"]
                ppath = os.path.join(RUN_DIR, f"frf_{name}_PD.csv")
                write_frf_csv(ppath, f_hz, Hp, cohp)
                ratio = np.abs(Hp / H)
                band = f_hz <= 40e3
                flat = (float(np.ptp(ratio[band]) / np.mean(ratio[band]))
                        if band.any() else float("nan"))
                lag = np.rad2deg(np.angle(Hp / H))
                print(f"wrote {ppath}   (photodiode on CH{pd_ch})")
                print(f"  MONITOR FIDELITY  |H_pd/H_mon| varies "
                      f"{100*flat:.2f}% to 40 kHz, phase "
                      f"{np.min(lag[band]):+.1f} to {np.max(lag[band]):+.1f} deg")
                print(f"  gate: flat within 2% and 3 deg. Structure here means "
                      f"the monitor is NOT the electrode voltage.")
            if fmax_now and fmax_now > f_hz[-1] + 1:
                print(f"note: 'taper to zero at' is {fmax_now/1e3:g} kHz but "
                      f"the measurement stops at {f_hz[-1]/1e3:.1f} kHz -- "
                      f"pull the taper inside the measured band")
            self.msgs.put(("call", lambda: self._adopt_frf(path)))
        finally:
            if uploaded or switched_on:
                try:
                    awg.set_output(awg_ch, False)
                    print(f"CH{awg_ch} output OFF (end of run)")
                    self._bench_note(f"CH{awg_ch} output OFF (end of run)")
                except Exception as e:
                    print(f"could not switch CH{awg_ch} output off: {e}")
            if retune:
                # output is off again -- put the bench back the way the
                # session's drives expect it
                try:
                    missed = awg.apply_channel(
                        awg_ch, {"BSWV": {"FRQ": 1.0 / t_sess}},
                        log=lambda m: print("      ", m))
                    rng = nice_setting(1.3 * t_sess)
                    scope.put(":TIMebase:REFerence", "CENTer")
                    scope.put(":TIMebase:RANGe", f"{rng:.6g}")
                    scope.put(":TIMebase:POSition", f"{t_sess/2:.6g}")
                    print(f"restored: AWG FRQ {1.0/t_sess:.6g} Hz, scope "
                          f"window {rng*1e3:.4g} ms. The probe is still the "
                          f"SELECTED waveform -- the bench loop re-selects "
                          f"its own drive on upload, or press Auto-set."
                          + (f" FRQ restore did NOT take: {missed}"
                             if missed else ""))
                except Exception as e:
                    print(f"could not restore FRQ/scope window: {e} -- "
                          f"press Auto-set instruments before the next run")
            if vert_saved:
                try:
                    for chn, (sc, off) in vert_saved.items():
                        scope.put(f":CHANnel{chn}:SCALe", f"{sc:.6g}")
                        scope.put(f":CHANnel{chn}:OFFSet", f"{off:.6g}")
                    print("scope verticals restored to the session's")
                except Exception as e:
                    print(f"could not restore scope verticals: {e} -- "
                          f"press Auto-set instruments before the next run")
            awg.close()
            scope.close()
            print("instruments closed")

    def _read_waveform(self, scope, ch, points, tries=3):
        """scope.waveform with a retry.

        The MSO-X occasionally answers :WAVeform:DATA? with an empty block;
        pyvisa reports it as "invalid literal for int() with base 10: b''"
        and it killed a 19-iteration run at the readout (2 Sep 2026). The
        acquisition is stopped between single() and run(), so re-reading
        returns the SAME frozen record -- a retry is not a second
        measurement, and nothing is averaged twice. Clear the link first to
        drop whatever partial reply is still in flight.
        """
        for k in range(tries):
            try:
                return scope.waveform(ch, points=points)
            except Exception as e:
                if k == tries - 1:
                    raise
                print(f"  CH{ch} readout failed ({type(e).__name__}: {e}) -- "
                      f"clearing the link and re-reading the same frozen "
                      f"acquisition, attempt {k + 2} of {tries}")
                try:
                    scope.inst.clear()
                except Exception:
                    pass
                time.sleep(0.2)

    def _frf_capture(self, scope, drive_ch, mon_ch, bins, t_grid, t_off,
                     repeats, wait_s, settle=0.5, f_top=None,
                     aux_ch=None, window=None, dither=True, dither_codes=3):
        """Per shot: read the drive, the monitor and optionally a third
        channel from the same acquisition (the record is frozen between
        :SINGle and run), put them on the record grid, and take H = Y/U at
        the probe's tone bins. H is averaged over shots -- not the traces --
        so the shot-to-shot scatter of H itself is the coherence estimate.

        aux_ch: the photodiode. Its H is computed against the same drive, so
        H_aux / H_mon is the monitor-fidelity ratio the optical campaign is
        built on -- and because it is a ratio, anything common to both (the
        drive's own shape, the analyser being a few degrees off quadrature,
        the taper's leakage) divides straight out.

        window: (i0, i1) analysis window on the record grid, for a ramped probe
        where only the hold carries the multitone. The tone bins MUST then be
        integer over i1-i0 rather than over the whole record -- that is what
        keeps the analysis leak-free, and tone_bins/ramped_multitone in
        tools.sysid_make size them that way. Returns a dict:
        {"mon": (H, coh), "aux": (H, coh) or None}.
        """
        time.sleep(settle)
        # ask the scope for a readout dense enough for the band -- its
        # unprompted default is 5000 points whatever the window
        pts = None
        if f_top is not None:
            try:
                win = float(scope.get(":TIMebase:RANGe"))
            except Exception:
                win = 1.3 * (t_grid[-1] - t_grid[0])
            pts = scope_points_for(6.0 * f_top * win)
            print(f"scope readout: {pts} points over the {win*1e3:.3g} ms "
                  f"window for the {f_top/1e3:.0f} kHz band")
        Hs, Ha = [], []
        sl = (slice(None) if window is None
              else slice(int(window[0]), int(window[1])))
        # Dither here as well: the probe is the same waveform every shot, so
        # the converter's per-code pattern is identical in every shot,
        # survives the average of H, and -- because it repeats -- reads as
        # COHERENT. It is what limits H where |H| is small: at the top of
        # the band the monitor tone is ~0.1 mV against a ~1 mV-per-code
        # pattern. Every channel read is stepped, drive included.
        chans = (drive_ch, mon_ch) + ((aux_ch,) if aux_ch is not None else ())
        plan, restore = self._dither_plan(scope, chans, repeats,
                                          dither_codes if dither else 0)
        self.msgs.put(("progress", 0, repeats))
        try:
          for i in range(repeats):
            if self.stop_evt.is_set():
                raise RuntimeError("stopped mid-capture; nothing written")
            self._dither_step(scope, plan, i, repeats, dither_codes)
            got = scope.single(wait_s=wait_s)
            if got is not True:
                raise RuntimeError(f"no trigger within {wait_s:g} s on "
                                   f"repeat {i+1} -- is the burst running?")
            tu, vu = self._read_waveform(scope, drive_ch, pts)
            ty, vy = self._read_waveform(scope, mon_ch, pts)
            ta, va = (self._read_waveform(scope, aux_ch, pts)
                      if aux_ch is not None else (None, None))
            scope.run()
            if i == 0 and f_top is not None:
                # measured, not assumed: the scope's record must actually
                # carry the band before H at the top tones means anything
                dt_sc = float(np.median(np.diff(tu)))
                if 0.5 / dt_sc < f_top:
                    raise RuntimeError(
                        f"the scope record is too coarse for this band: "
                        f"{len(tu)} pts at {dt_sc*1e9:.0f} ns puts its "
                        f"Nyquist at {0.5/dt_sc/1e3:.0f} kHz, below the "
                        f"top tone ({f_top/1e3:.0f} kHz). Tighten the "
                        f"window or lower f hi.")
            uu = scopeio.resample(tu, vu, t_grid, t_offset=t_off)[sl]
            yy = scopeio.resample(ty, vy, t_grid, t_offset=t_off)[sl]
            U = np.fft.rfft(uu - uu.mean())
            Y = np.fft.rfft(yy - yy.mean())
            Hs.append(Y[bins] / U[bins])
            if aux_ch is not None:
                aa = scopeio.resample(ta, va, t_grid, t_offset=t_off)[sl]
                Ha.append(np.fft.rfft(aa - aa.mean())[bins] / U[bins])
            self.msgs.put(("progress", i + 1, repeats))
        finally:
            self._dither_restore(scope, restore)
        self.msgs.put(("progress", 0, 1))

        def reduce(stack):
            stack = np.asarray(stack)
            h = stack.mean(axis=0)
            # NOT a cross-spectral coherence: this is the shot-to-shot scatter
            # of H itself, which reads ~1 for any repeatable systematic. For
            # "does the light follow the monitor" use
            # eomilc.polarimetry.coherence_ensemble on inverted volts instead.
            c = np.clip(1 - (np.abs(stack - h).std(axis=0) / np.abs(h)) ** 2,
                        0, 1)
            return h, c

        return {"mon": reduce(Hs),
                "aux": reduce(Ha) if aux_ch is not None else None}

    def _adopt_frf(self, path):
        """Main thread: point the FRF field at the fresh measurement and
        show it. Switching the MODEL to 'measured FRF' stays the user's
        call -- measuring is not consenting to drive with it. In pair mode
        the FRF belongs to the shown chain, so it lands in that column."""
        c = (self._pair_col(self.shown_var.get())
             if self.pair is not None and self._col2 is not None else self._col1)
        c.frf_var.set(path)
        self.log(f"{c.key + ' ' if self.pair is not None else ''}FRF field now "
                 f"points at {os.path.basename(path)}")
        self.do_show_frf(c)

    def _ensure_output_on(self, awg, awg_ch):
        """Worker-side: switching ON is the direction that puts voltage into
        something, so it asks first. Returns (proceed, we_switched_it_on);
        the caller's cleanup switches it back off regardless of who did."""
        if awg.is_on(awg_ch):
            return True, False
        if not self.ask_user(
                "Output is OFF",
                f"CH{awg_ch} output is OFF, and the measurement needs it "
                f"driving.\n\nTurn CH{awg_ch} ON and run?\n\n"
                f"(It is switched OFF again when the run ends.)"):
            print(f"run cancelled: CH{awg_ch} output left OFF")
            return False, False
        awg.set_output(awg_ch, True)
        print(f"CH{awg_ch} output ON (confirmed in dialog)")
        self._bench_note(f"CH{awg_ch} output ON")
        if not awg.is_on(awg_ch):
            print(f"CH{awg_ch} did not switch on -- aborting")
            return False, True
        return True, True

    # -- offset dither: ilc_bench.dither_plan has the why. Thin wrappers so
    # the bench capture, Measure FRF and the polarization fine-tune share
    # one implementation (the fine-tune reaches it through capture_all).

    def _dither_plan(self, scope, channels, repeats, codes):
        return ilc_bench.dither_plan(scope, channels, repeats, codes)

    def _dither_step(self, scope, plan, i, repeats, codes):
        ilc_bench.dither_step(scope, plan, i, repeats, codes)

    def _dither_restore(self, scope, restore):
        ilc_bench.dither_restore(scope, restore)

    def _bench_capture(self, scope, ch, t_grid, t_off, repeats, wait_s,
                       settle=0.5, native_path=None, aux=(), aux_store=None,
                       keep="grid", dither=True, dither_codes=3):
        """ilc_bench.capture with a progress bar and a stop check between
        shots. The settle wait happens once, after the new upload.

        dither: step every read channel's offset across `dither_codes` ADC
        codes over the shots (see ADC_CODE_PER_VDIV and the note in the
        body), so the converter's per-code error pattern averages out of the
        mean instead of being learned into the drive. On by default; the
        offsets go back on exit. It also makes the pattern non-repeatable
        across shots, so the noise-floor readout sees it as the noise it is.
        Three codes rather than one, so code-to-code variation (the part a
        one-code sweep cannot touch) is averaged as well: 64 shots over 3
        codes is still 21 phases per code, and +/-60 mV at 1 V/div is 0.06
        of a division.

        native_path: also save the repeat average at the SCOPE's own sample
        rate (npz, t = waveform time, y = monitor V) -- the boxcar
        decimation onto t_grid discards everything past the grid Nyquist,
        and this file is the only way to get it back after the fact.

        aux: extra scope channels to read alongside the monitor -- the
        photodiode for the optical campaign. They are read from the SAME
        frozen acquisition as the monitor, between :SINGle and run(), so the
        traces are simultaneous and can be cross-correlated; separate
        triggers would quietly destroy exactly that. Every shot is kept
        rather than averaged, because the ensemble std is the shot-to-shot
        error ILC cannot learn and averaging inside the capture throws it
        away (see eomilc.polarimetry).

        aux_store: a dict the caller passes in; gets aux_store["capture"] =
        an ilc_bench.Capture covering the monitor AND every aux channel.
        Filling a caller's dict rather than changing the return type keeps
        the ILC loop's `y = self._bench_capture(...)` untouched.

        keep: "grid" (resampled, the default), "raw" (the scope's own
        samples, untouched) or "both". The monitor is ALWAYS resampled
        regardless, because the loop iterates on the grid -- keep only
        governs what lands in aux_store. Reach for "raw" or "both" for
        anything that reasons about ADC codes: resample interpolates
        between samples and boxcars when decimating, and either one
        destroys the word lattice a dither check depends on.
        """
        time.sleep(settle)
        traces = []
        aux = tuple(aux)
        want = aux or aux_store is not None
        cols = [f"CH{c}" for c in (ch,) + aux]
        g = {c: [] for c in cols} if (want and keep in ("grid", "both")) else None
        r = {c: [] for c in cols} if (want and keep in ("raw", "both")) else None
        t_raw = None
        t_nat, y_nat = None, 0.0
        # denser than the scope's 5000-point default: >= 2 samples per grid
        # step keeps the anti-alias boxcar honest, and a kept native
        # average deserves the full readout depth
        pts = (SCOPE_PTS[-1] if native_path is not None
               else scope_points_for(2.2 * len(t_grid)))
        # Offset dither. The converter's per-code error pattern (see
        # ADC_CODE_PER_VDIV) is a function of VOLTAGE, so a ramp sweeps it
        # into 15-100 kHz "error" -- slope / code -- that the shot average
        # keeps exactly, the SEM test calls repeatable, and the loop then
        # corrects into real ripple at the EOM (P92PX1B/C, 2 Sep 2026: 1 V
        # rms at the corners that the flat shot never had). Stepping the
        # channel offset across one code over the shots puts every shot at a
        # different code phase, so the average takes the pattern's mean; the
        # preamble's yorigin already returns true volts, so nothing
        # downstream changes. Restored on the way out, however the capture
        # ends. Skipped, and said so, on a scope that cannot report its
        # scale and offset.
        plan, restore = self._dither_plan(
            scope, (ch,) + aux, repeats, dither_codes if dither else 0)
        try:
            self.msgs.put(("progress", 0, repeats))
            for i in range(repeats):
                if self.stop_evt.is_set():
                    raise RuntimeError("stopped mid-capture; this iteration is "
                                       "discarded (state untouched)")
                self._dither_step(scope, plan, i, repeats, dither_codes)
                got = scope.single(wait_s=wait_s)
                if got is not True:
                    raise RuntimeError(f"no trigger within {wait_s:g} s on repeat "
                                       f"{i+1} -- is the burst running?")
                ts, vs = self._read_waveform(scope, ch, pts)
                extra = [(c, *self._read_waveform(scope, c, pts)) for c in aux]
                scope.run()
                y_mon = scopeio.resample(ts, vs, t_grid, t_offset=t_off)
                traces.append(y_mon)
                if want:
                    shot = [(f"CH{ch}", ts, vs)] + [(f"CH{c}", ta, va)
                                                    for c, ta, va in extra]
                    for col, tc, vc in shot:
                        if g is not None:
                            g[col].append(y_mon if col == f"CH{ch}" else
                                          scopeio.resample(tc, vc, t_grid,
                                                           t_offset=t_off))
                        if r is not None:
                            v = np.asarray(vc, float)
                            if t_raw is None:
                                t_raw = np.asarray(tc, float) - t_off
                            elif v.size != t_raw.size:
                                raise RuntimeError(
                                    f"{col} returned {v.size} raw samples on "
                                    f"repeat {i+1} against {t_raw.size} on the "
                                    f"first -- the scope changed setup mid-run.")
                            r[col].append(v)
                if native_path is not None:
                    if t_nat is None:            # repeats share the scope
                        t_nat = np.asarray(ts, float)   # config; align a stray
                        y_nat = np.asarray(vs, float)   # one instead of dying
                    else:
                        y_nat = y_nat + np.interp(t_nat, ts, vs)
                self.msgs.put(("progress", i + 1, repeats))
            self.msgs.put(("progress", 0, 1))
            if native_path is not None and t_nat is not None:
                np.savez(native_path, t=t_nat - t_off, y=y_nat / repeats)
                print(f"  native-rate average kept: {os.path.basename(native_path)}"
                      f" ({len(t_nat)} pts, dt "
                      f"{np.median(np.diff(t_nat))*1e9:.0f} ns)")
            if want and aux_store is not None:
                aux_store["capture"] = ilc_bench.Capture(
                    channels=tuple(cols), n_shots=int(repeats),
                    grid={c: np.asarray(v, float) for c, v in g.items()}
                    if g is not None else None,
                    raw={c: np.asarray(v, float) for c, v in r.items()}
                    if r is not None else None,
                    t_grid=t_grid if g is not None else None, t_raw=t_raw)
                if aux:
                    print(f"  kept {repeats} shots on {', '.join(cols)} "
                          f"({keep}) for the ensemble split")
            return ilc.averaged(traces)
        finally:
            self._dither_restore(scope, restore)

    # ---------------------------------------------------------------- plots
    def _colour(self):
        return CH_DEFAULTS[self.session.channel]["colour"]

    def _out_scale(self):
        return self.session.loop.channel.mon_scale

    def _out_name(self):
        return self.session.loop.channel.out_name

    def _snaps_by_it(self, s=None):
        """BASE measurements (run None) keyed by iteration; a later base
        measurement of the same iteration replaces the earlier one. Hold
        runs are kept separately -- see _snaps_for."""
        m = {}
        for sn in (s or self.session).snapshots:
            if sn.get("run") is None:
                m[sn["it"]] = sn
        return m

    def _pick_iters(self, spec, avail, last_n, log):
        """The iteration grammar shared by the Iterations and Compare boxes:
        blank = the last last_n, 'all', a range '2-5', or a list '0,3,6'."""
        spec = spec.strip().lower()
        if not spec:
            return avail[-last_n:]
        if spec in ("all", "*"):
            return list(avail)
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
            log(f"iteration selection {spec!r} not understood -- "
                f"use 'all', '2-5', or '0,3,6'; showing the last {last_n}")
            return avail[-last_n:]
        pick = [i for i in avail if i in its]
        if avail and not pick:
            log(f"no stored measurements match {spec!r} "
                f"(available: {avail})")
        return pick

    def _snaps_for(self, s, spec, last_n, log=None):
        """Snapshots of session s that spec picks: each picked iteration
        contributes its base measurement plus, when the 'runs' box is
        ticked, every hold re-measurement in run order."""
        by_it = self._snaps_by_it(s)
        avail = sorted({sn["it"] for sn in s.snapshots})
        pick = self._pick_iters(spec, avail, last_n, log or self.log)
        runs_by_it = {}
        if self.showruns_var.get():
            for sn in s.snapshots:
                if sn.get("run") is not None:
                    runs_by_it.setdefault(sn["it"], {})[sn["run"]] = sn
        out = []
        for i in pick:
            if i in by_it:
                out.append(by_it[i])
            for r in sorted(runs_by_it.get(i, {})):
                out.append(runs_by_it[i][r])
        return out

    def _selected_snaps(self):
        """The iterations the plots show, per the 'Iterations shown' box:
        blank = the last two, 'all', a range '2-5', or a list '0,3,6'."""
        if self.session is None:
            return []
        return self._snaps_for(self.session, self.itersel_var.get(), 2)

    def _log_once(self, msg):
        """Compare warnings fire on every redraw -- show each once per spec."""
        if msg not in self._cmp_logged:
            self._cmp_logged.add(msg)
            self.log(msg)

    def _cmp_resolve(self, key):
        """A Compare key -> the state file it names, or None.

        Picked campaigns win: the dialog wrote key -> path, and that path is
        the whole point of having picked it. Anything else is a stem in the
        loaded session's run folder, which is what every key meant before
        the picker existed -- so a typed spec, a remembered one, and the
        suite's fixtures all keep working."""
        picked = self._cmp_paths.get(key)
        if picked:
            return picked if os.path.exists(picked) else None
        run_dir = os.path.dirname(self.session.state_path)
        path = os.path.join(run_dir, f"drive_{key}.state.npz")
        return path if os.path.exists(path) else None

    @staticmethod
    def _cmp_key(path, taken):
        """The Compare key for a picked state file: its stem, or stem@folder
        when that stem is already spoken for by a DIFFERENT file. `taken` is
        {key: path} of everything already named.

        Two campaigns of the same name in two folders -- an archived run and
        the one it was repeated as -- is exactly the comparison the picker
        was added for, and a bare stem cannot name both."""
        def free(k):
            other = taken.get(k)
            return other is None or _same_file(other, path)

        stem = re.sub(r"^drive_|\.state\.npz$", "", os.path.basename(path))
        if free(stem):
            return stem
        tag = os.path.basename(os.path.dirname(os.path.abspath(path))) or "?"
        key, n = f"{stem}@{tag}", 2
        while not free(key):                     # the same folder NAME twice
            key, n = f"{stem}@{tag}~{n}", n + 1  # up the tree: number rather
        return key                               # than quietly collide

    def do_compare_add(self):
        """Pick campaigns to overlay, from anywhere on disk.

        The typed grammar stays the record of what is shown -- picking a file
        appends its key to the box (keeping any :ITERS already typed for that
        key), so the two are one thing seen two ways rather than two lists
        that can disagree."""
        if self.session is None:
            return messagebox.showerror(
                "Compare", "Load a session first -- compare overlays ride "
                           "the active campaign's plots.")
        paths = filedialog.askopenfilenames(
            title="Pick the state files of the campaigns to compare",
            initialdir=os.path.dirname(self.session.state_path) or RUN_DIR,
            filetypes=(("ILC state files", "*.state.npz"),
                       ("All files", "*.*")))
        if not paths:
            return
        # what each key already points at: the picked map, the run folder's
        # own stems (so picking run\drive_GENX keeps calling it GENX), and
        # the active campaign's stem -- an ARCHIVED campaign of the same name
        # is a different measurement and has to get a key of its own
        run_dir = os.path.dirname(self.session.state_path)
        taken = dict(self._cmp_paths)
        taken.setdefault(self.session.stem, self.session.state_path)
        for tok in self.cmpsel_var.get().split():
            key = tok.partition(":")[0]
            if key and key not in taken:
                taken[key] = os.path.join(run_dir,
                                          f"drive_{key}.state.npz")
        added = []
        for p in paths:
            p = os.path.abspath(p)
            if not os.path.exists(p):        # a stale shortcut, a dead drive
                self.log(f"compare: {p} is not there -- skipped")
                continue
            if _same_file(p, self.session.state_path):
                self.log(f"compare: {os.path.basename(p)} is the loaded "
                         f"session -- skipped")
                continue
            key = self._cmp_key(p, taken)
            taken[key] = p
            # a campaign the run folder already answers for under this key
            # needs no map entry, and not writing one keeps the remembered
            # config free of paths that go stale when a folder is renamed
            plain = os.path.join(run_dir, f"drive_{key}.state.npz")
            if os.path.exists(plain) and _same_file(plain, p):
                self._cmp_paths.pop(key, None)
            else:
                self._cmp_paths[key] = p
            if key not in [t.partition(":")[0]
                           for t in self.cmpsel_var.get().split()]:
                added.append(key)
        if added:
            spec = " ".join(self.cmpsel_var.get().split() + added)
            self.cmpsel_var.set(spec)
        self._cmp_logged.clear()
        self._cmp_lastspec = None       # force a fresh round of warnings
        self._redraw_iterations()       # which describes what resolved

    def do_compare_clear(self):
        """Unload every comparison: the box, the picked paths and the cache.

        The cache goes too -- keeping a loaded Session for a campaign nobody
        is drawing any more is memory held for no reason, and it would serve
        a stale copy if the campaign is stepped before it is compared
        against again."""
        self.cmpsel_var.set("")
        self._cmp_paths.clear()
        self._cmp_cache.clear()
        self._cmp_logged.clear()
        self._cmp_lastspec = None
        self._cmp_described.clear()
        self.log("compare: cleared. Nothing is drawn alongside any more.")
        self._redraw_iterations()

    def _describe_compare(self):
        """What each resolved campaign is, said once per campaign.

        The status line has room for the names; this is the line that says
        whether the thing now riding the plots is even commensurable with the
        session underneath it. Said for a typed key as much as a picked one --
        the trap does not care how the campaign got into the box."""
        s0 = self.session
        for key, cs, snaps, _col in self._cmp_last:
            token = (key, cs.state_path)
            if token in self._cmp_described:
                continue
            self._cmp_described.add(token)
            its = sorted({sn["it"] for sn in cs.snapshots})
            self.log(f"  {key}   {cs.channel}   {len(cs.t)} pts @ "
                     f"{cs.loop.dt*1e6:g} us   "
                     f"{len(its)} stored iteration(s)"
                     + (f" ({its[0]}-{its[-1]})" if its else "")
                     + f"   {cs.model_key or 'no model recorded'}")
            # A different channel or a different grid does not stop the
            # overlay -- both are legitimate comparisons -- but the metrics
            # underneath them are then not the same measurement, and that is
            # invisible in a plot of two curves.
            if cs.channel != s0.channel:
                self.log(f"    NOTE: {key} is {cs.channel}, this session is "
                         f"{s0.channel} -- different monitor scaling, so the "
                         f"error metrics are not directly comparable")
            if len(cs.t) != len(s0.t) or abs(cs.loop.dt - s0.loop.dt) > 1e-15:
                self.log(f"    NOTE: {key} runs {len(cs.t)} pts @ "
                         f"{cs.loop.dt*1e6:g} us against this session's "
                         f"{len(s0.t)} @ {s0.loop.dt*1e6:g} us -- it is drawn "
                         f"on its own grid, and its spectra have their own "
                         f"bin width")

    def _refresh_compare_status(self):
        """Main thread. Keep the compare line telling the truth: what
        resolved, and how much of the box did not."""
        asked = len({t.partition(":")[0]
                     for t in self.cmpsel_var.get().split()
                     if t.partition(":")[0]})
        got = self._cmp_last
        if got:
            names = "; ".join(
                f"{k} ({len(sn)} iter)" if sn else f"{k} (history only)"
                for k, _cs, sn, _c in got)
            text = f"{len(got)} campaign(s): {names}"
            colour = "#006600"
            if asked > len(got):
                text += f"   [{asked - len(got)} not shown -- see the log]"
                colour = "#cc6600"
        elif asked:
            text = (f"{asked} stem(s) in the box, none resolved -- "
                    f"see the log")
            colour = "#cc6600"
        else:
            text, colour = CMP_HINT, "#666666"
        self.cmp_status.configure(text=elide(text, CMP_STATUS_CHARS),
                                  foreground=colour)
        self.cmp_clear_btn.configure(
            state="normal" if (asked or self._cmp_paths) and not self.busy
            else "disabled")

    def _compare_groups(self):
        """The Compare box -> [(key, session, snaps, colour)]: other
        campaigns loaded read-only (never saved, never stepped) and overlaid
        on the analysis plots. Grammar: space-separated keys, each optionally
        key:ITERS with the Iterations grammar; a blank selection means that
        campaign's last measured iteration.

        A key is a stem in the loaded session's run folder, or whatever the
        Add campaigns... dialog mapped to a state file elsewhere."""
        s0 = self.session
        spec = self.cmpsel_var.get().strip() if s0 is not None else ""
        if spec != self._cmp_lastspec:
            self._cmp_lastspec = spec
            self._cmp_logged.clear()
        if not spec:
            self._cmp_last = []
            return []
        run_dir = os.path.dirname(s0.state_path)
        groups, seen = [], set()
        for tok in spec.split():
            key, _, isel = tok.partition(":")
            if not key or key in seen:
                continue
            seen.add(key)
            path = self._cmp_resolve(key)
            # compared by FILE, not by stem: by the naming convention the run
            # folder's file for the active stem is the loaded state, but an
            # archived campaign of the SAME NAME in another folder is a
            # different measurement and exactly what the picker is for
            if path is not None and _same_file(path, s0.state_path):
                self._log_once(f"compare: {key!r} is the loaded session "
                               f"-- skipped")
                continue
            if path is None:
                if self._cmp_paths.get(key):
                    self._log_once(
                        f"compare: the file picked for {key!r} is gone: "
                        f"{self._cmp_paths[key]}")
                    continue
                have = sorted(
                    re.match(r"drive_(.+)\.state\.npz$",
                             os.path.basename(p)).group(1)
                    for p in glob.glob(os.path.join(run_dir,
                                                    "drive_*.state.npz")))
                self._log_once(f"compare: no state for {key!r} in {run_dir} "
                               f"(available: {', '.join(have) or 'none'}) "
                               f"-- Add campaigns... reaches other folders")
                continue
            mt = os.path.getmtime(path)
            cached = self._cmp_cache.get(path)
            if cached and cached[0] == mt:
                cs = cached[1]
            else:
                try:
                    cs = load_session(path)
                    recall_snapshots(cs)
                except Exception as e:
                    self._log_once(f"compare: could not load {key!r}: {e}")
                    continue
                self._cmp_cache[path] = (mt, cs)
            snaps = self._snaps_for(cs, isel, 1, log=self._log_once)
            if not snaps and not cs.snapshots:
                self._log_once(f"compare: {key!r} has no stored measurements"
                               f" -- only its convergence history can show")
            groups.append((key, cs, snaps,
                           CMP_COLOURS[len(groups) % len(CMP_COLOURS)]))
        self._cmp_last = groups
        return groups

    def _cmp_colour(self, base, idx, n):
        """Within one compare stem, older selected iterations blend toward
        white -- a lightness ramp stays readable where an alpha ramp
        washed into the background and into the active session's traces."""
        if n <= 1 or idx == n - 1:
            return base
        w = 0.65 * (1 - idx / (n - 1))       # oldest = 65% toward white
        r, g, b = (int(base[i:i + 2], 16) / 255 for i in (1, 3, 5))
        return (r + (1 - r) * w, g + (1 - g) * w, b + (1 - b) * w)

    def _cmp_label(self, stem, sn):
        lab = f"{stem} iter {sn['it']}"
        if sn.get("run") is not None:
            lab += f" r{sn['run']}"
        return lab

    def _snap_label(self, sn):
        d = sn["m"].get("model") if isinstance(sn.get("m"), dict) else None
        lab = f"iter {sn['it']}"
        if sn.get("run") is not None:
            lab += f" r{sn['run']}"
        elif d:
            lab += f" ({d})"
        if self.dtlabels_var.get():
            suf = self._dt_suffix(sn)
            if suf:
                lab += f"  {suf}"
        return lab

    def _dt_suffix(self, sn, s=None):
        """Time offset for the Δt legend labels: a hold run against its
        iteration's base measurement (or the first run when no base was
        measured); a base iteration against the latest earlier iteration."""
        s = s or self.session
        t = sn.get("t_wall")
        if t is None:
            return ""
        ref = None
        if sn.get("run") is not None:
            base = self._snaps_by_it(s).get(sn["it"])
            if base is not None and base.get("t_wall"):
                ref = base["t_wall"]
            else:
                rr = [x["t_wall"] for x in s.snapshots
                      if x["it"] == sn["it"] and x.get("run") is not None
                      and x.get("t_wall")]
                ref = min(rr) if rr else None
        else:
            earlier = [x["t_wall"] for x in self._snaps_by_it(s).values()
                       if x["it"] < sn["it"] and x.get("t_wall")]
            ref = max(earlier) if earlier else None
        if ref is None or t <= ref:
            return ""
        return "+" + fmt_span(t - ref)

    def _iter_colour(self, idx, n):
        return matplotlib.colormaps["viridis"](0.1 + 0.75 * idx / max(n - 1, 1))

    def _time_axes(self):
        """Every axis whose x is time in ms (ax_drv shares x with ax_out)."""
        return (self.ax_out, self.ax_err, self.ax_dcor, self.ax_ddel)

    def _finish_time_axis(self, ax):
        """Apply the linked time window and re-arm the link callback.
        Called at the end of every time-domain plot function: ax.clear()
        wipes the callback registry, so each redraw re-registers -- which
        also means autoscaling during the draw itself never fires the link,
        only the user's toolbar zoom/pan/home afterwards does."""
        if self.tlink_var.get() and self._t_range is not None:
            ax.set_xlim(self._t_range)
        ax.callbacks.connect("xlim_changed", self._on_xlim_changed)

    def _wrap_home(self, tb):
        """Home on this pane homes every linked time axis as well.

        Rebinds the BUTTON, not just the attribute. NavigationToolbar2Tk wires
        each button with command=getattr(self, callback) at construction, so it
        holds the original bound method and replacing tb.home afterwards leaves
        the button calling that - the wrap never fired. Measured on matplotlib
        3.10 (this panel's Anaconda build) and 3.11: with the attribute alone
        replaced, clicking Home ran the original and nothing else.
        """
        orig = tb.home

        def home(*args, **kw):
            orig(*args, **kw)          # this pane's own y-reset still works
            self._home_all_time_axes()

        tb.home = home
        button = getattr(tb, "_buttons", {}).get("Home")
        if button is not None:
            button.configure(command=home)
        return home

    def _home_all_time_axes(self):
        """Release the linked window and x-autoscale every time plot.

        The draws happen synchronously INSIDE the busy window: autoscale is
        applied at draw time, and a deferred draw_idle would fire
        xlim_changed after the guard lifted, re-capturing the full range as
        if it were a zoom."""
        if not self.tlink_var.get():
            return
        self._tlink_busy = True
        try:
            # ax_drv shares x with ax_out, and a shared group only
            # autoscales when EVERY sibling has autoscale enabled -- so the
            # whole group is re-enabled before anything draws
            for ax in self._time_axes() + (self.ax_drv,):
                ax.autoscale(enable=True, axis="x")
            for fig in (self.fig_wave, self.fig_err, self.fig_dcor,
                        self.fig_ddel):
                fig._canvas.draw()
            # a pane with no data (an empty corrections/updates tab) cannot
            # autoscale -- conform everyone to the Waveforms axis, which
            # always carries the target while a session is loaded
            ref = self.ax_out.get_xlim()
            for ax in self._time_axes():
                if ax.get_xlim() != ref:
                    ax.set_xlim(ref)
                    ax.figure._canvas.draw()
        finally:
            self._tlink_busy = False
        self._t_range = None

    def _on_xlim_changed(self, ax):
        """Toolbar zoom/pan/home on one time plot drives them all -- the
        rectangle zoom stays exactly as it is; it just acts everywhere."""
        if not self.tlink_var.get() or self._tlink_busy:
            return
        self._tlink_busy = True
        try:
            lims = ax.get_xlim()
            self._t_range = lims
            for other in self._time_axes():
                if other is ax:
                    continue
                if other.get_xlim() != lims:
                    other.set_xlim(lims)
                    self._draw(other.figure)
        finally:
            self._tlink_busy = False

    def _dot_kw(self, n, ms=3.0):
        """dot_kw honouring the 'dot every Nth sample' box: blank = auto
        (~180 dots per trace), a number = that literal subsampling step,
        1 = every real sample drawn."""
        txt = self.dotstep_var.get().strip()
        if txt:
            try:
                return dict(marker=".", markersize=ms,
                            markevery=max(1, int(float(txt))))
            except ValueError:
                if txt != self._dot_warned:
                    self._dot_warned = txt
                    self.log(f"dot spacing {txt!r} is not a number -- "
                             f"using auto")
        return dot_kw(n, ms=ms)

    def _redraw_iterations(self):
        """Re-render every per-iteration plot from the current selection."""
        if self.session is None:
            return
        snaps = self._selected_snaps()
        cmp = self._compare_groups()
        self._plot_error(snaps, cmp)
        self._plot_spectrum(snaps, cmp)
        self._plot_dcorr(snaps, cmp)
        self._plot_dspec(snaps, cmp)
        self._plot_ddelta(snaps, cmp)
        self._plot_convergence(cmp)
        self._plot_convergence_time()
        self._plot_diagnostics(snaps)
        self._fill_table(cmp)
        if self._wave_redraw is not None:     # a pinned view (fit, target preview)
            self._wave_redraw()
        else:
            self._plot_waveforms(snaps, cmp)
        self._refresh_compare_status()
        self._describe_compare()

    def _show_session(self, select_tab=False):
        """Everything drawable from a freshly loaded/inited session: target,
        drive, model prediction, plus any recalled measurements."""
        self._wave_redraw = None            # back onto the selection
        self._redraw_iterations()
        if select_tab:
            self._show_fig(self.fig_wave)

    def _show_iteration(self, u, y, m, it):
        self._wave_redraw = None            # back onto the selection
        self._redraw_iterations()

    def _plot_waveforms(self, snaps, cmp=()):
        """The Waveforms tab from the SELECTION, like every other tab: the
        target, every selected measurement (hold runs dashed) and every
        selected iteration's drive, with the compare stems' selected ones
        alongside. It used to draw one measurement -- whatever the last
        step or load handed it -- and replay that on Redraw, so the
        Iterations box did nothing here and Compare showed one trace per
        stem. The session's CURRENT drive is always in the drive pane, bold,
        because it is what plays next; a fresh session with no measurement
        shows the model's predicted output instead."""
        s, c = self.session, self._colour()
        sc = self._out_scale()
        tms = s.t * 1e3
        n = len(snaps)

        ax = self.ax_out
        ax.clear()
        ax.plot(tms, s.loop.target * sc, color=TARGET_COLOUR, lw=1.0,
                label="target" if s.base_target is None
                else "target (base + learned corrections)",
                **self._dot_kw(len(tms)))
        if s.base_target is not None:
            ax.plot(tms, s.base_target * sc, color=TARGET_COLOUR, lw=0.7,
                    ls=":", label="base target")
        if not s.snapshots:
            # model output, not data -- dashed and dotless on purpose
            ax.plot(tms, s.loop.plant.forward(s.u) * sc, color=PRED_COLOUR,
                    lw=0.9, ls="--", label="model-predicted output")
        for idx, sn in enumerate(snaps):
            ax.plot(tms, sn["y"] * sc, color=self._iter_colour(idx, n),
                    lw=1.1 if idx == n - 1 else 0.8,
                    ls="--" if sn.get("run") is not None else "-",
                    label="measured " + self._snap_label(sn),
                    **self._dot_kw(len(tms)))
        total = n
        for stem, cs, csnaps, col in cmp:
            csc = cs.loop.channel.mon_scale
            ctms = cs.t * 1e3
            if csnaps and not (len(cs.t) == len(s.t) and
                               np.array_equal(cs.loop.target * csc,
                                              s.loop.target * sc)):
                ax.plot(ctms, cs.loop.target * csc, color=col, lw=0.7,
                        ls=":", alpha=0.8, label=f"{stem} target", zorder=CMP_ZORDER)
            k = len(csnaps)
            for idx, sn in enumerate(csnaps):
                ax.plot(ctms, sn["y"] * csc, color=self._cmp_colour(col, idx, k),
                        lw=1.1 if idx == k - 1 else 0.8,
                        ls="--" if sn.get("run") is not None else "-",
                        label=f"{stem} measured iter {sn['it']}"
                              + (f" r{sn['run']}" if sn.get("run") is not None else ""),
                        **self._dot_kw(len(ctms)), zorder=CMP_ZORDER)
            total += k
        ax.set_ylabel(f"{self._out_name()} voltage (V)")
        ax.legend(loc="best", fontsize=7, ncols=2 if total > 6 else 1)
        ax.set_title(f"{s.channel} '{s.stem}' -- output vs target")
        ax.grid(True, alpha=0.3)

        ax = self.ax_drv
        ax.clear()
        u = s.u
        shown_current = False
        for idx, sn in enumerate(snaps):
            if sn.get("run") is not None or sn.get("u") is None \
                    or len(sn["u"]) != len(s.t):
                continue
            same = len(sn["u"]) == len(u) and np.array_equal(sn["u"], u)
            shown_current = shown_current or same
            ax.plot(tms, sn["u"], color=self._iter_colour(idx, n),
                    lw=1.1 if same else 0.8,
                    label=f"iter {sn['it']} drive"
                          + (f" (= current, iteration {s.iteration})" if same else ""),
                    **self._dot_kw(len(tms)))
        if not shown_current:
            ax.plot(tms, u, color=c, lw=1.2,
                    label=f"drive u (iteration {s.iteration}, next to play)",
                    **self._dot_kw(len(tms)))
        for stem, cs, csnaps, col in cmp:
            k = len(csnaps)
            for idx, sn in enumerate(csnaps):
                if sn.get("run") is not None or sn.get("u") is None \
                        or len(sn["u"]) != len(cs.t):
                    continue
                ax.plot(cs.t * 1e3, sn["u"], color=self._cmp_colour(col, idx, k),
                        lw=0.8, label=f"{stem} iter {sn['it']} drive",
                        **self._dot_kw(len(cs.t)), zorder=CMP_ZORDER)
        fs = s.full_scale
        ax.axhline(fs, color="#c62828", lw=0.8, ls="--")
        ax.axhline(-fs, color="#c62828", lw=0.8, ls="--",
                   label=f"+/-{fs:g} V full scale (AMP {2*fs:g} Vpp, OFST 0)")
        cap = s.loop.limits.idle_awg
        ax.axhline(cap, color="#c62828", lw=0.6, ls=":")
        ax.axhline(-cap, color="#c62828", lw=0.6, ls=":")
        ax.plot([tms[0], tms[-1]], [u[0], u[-1]], "o", color=c, ms=4, mfc="none")
        pk = float(np.abs(u).max())
        ax.set_title(f"current drive peak {pk:.3f} V = {100*pk/fs:.1f}% of DAC "
                     f"range,  idle {u[0]*1e3:+.1f}/{u[-1]*1e3:+.1f} mV of "
                     f"{cap*1e3:.0f} mV cap", fontsize=8)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("AWG drive (V)")
        ax.legend(loc="best", fontsize=7, ncols=2 if n + len(cmp) > 6 else 1)
        ax.grid(True, alpha=0.3)
        self._finish_time_axis(self.ax_out)
        self._draw(self.fig_wave)

    def _plot_waveforms_pinned(self, u, y, pred, it):
        """One drive/measurement pair with a model prediction over it -- the
        view a fit wants, pinned to the tab (Redraw replays it) until the
        next iteration or load puts the tab back onto the selection."""
        self._wave_redraw = (lambda u=u, y=y, pred=pred, it=it:
                             self._plot_waveforms_pinned(u, y, pred, it))
        s, c = self.session, self._colour()
        sc = self._out_scale()
        tms = s.t * 1e3
        ax = self.ax_out
        ax.clear()
        ax.plot(tms, s.loop.target * sc, color=TARGET_COLOUR, lw=1.0,
                label="target", **self._dot_kw(len(tms)))
        if pred is not None:
            # model output, not data -- dashed and dotless on purpose
            ax.plot(tms, pred * sc, color=PRED_COLOUR, lw=0.9, ls="--",
                    label="model-predicted output")
        if y is not None:
            ax.plot(tms, y * sc, color=c, lw=0.9,
                    label=f"measured (iter {it})", **self._dot_kw(len(tms)))
        # compare stems ride along with their LAST selected measurement --
        # the Error tab is the multi-iteration surface, this pane stays legible
        for stem, cs, csnaps, col in self._compare_groups():
            if not csnaps:
                continue
            sn = csnaps[-1]
            csc = cs.loop.channel.mon_scale
            ctms = cs.t * 1e3
            if not np.array_equal(cs.loop.target * csc, s.loop.target * sc):
                ax.plot(ctms, cs.loop.target * csc, color=col, lw=0.7,
                        ls=":", alpha=0.8, label=f"{stem} target")
            run = f" r{sn['run']}" if sn.get("run") is not None else ""
            ax.plot(ctms, sn["y"] * csc, color=col, lw=0.9,
                    label=f"{stem} measured (iter {sn['it']}{run})",
                    **self._dot_kw(len(ctms)))
        ax.set_ylabel(f"{self._out_name()} voltage (V)")
        ax.legend(loc="best", fontsize=7)
        ax.set_title(f"{s.channel} '{s.stem}' -- output vs target")
        ax.grid(True, alpha=0.3)

        ax = self.ax_drv
        ax.clear()
        ax.plot(tms, u, color=c, lw=0.9,
                label=f"drive u (iteration {s.iteration})", **self._dot_kw(len(tms)))
        fs = s.full_scale
        ax.axhline(fs, color="#c62828", lw=0.8, ls="--")
        ax.axhline(-fs, color="#c62828", lw=0.8, ls="--",
                   label=f"+/-{fs:g} V full scale (AMP {2*fs:g} Vpp, OFST 0)")
        cap = s.loop.limits.idle_awg
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
        self._finish_time_axis(self.ax_out)
        self._draw(self.fig_wave)

    def _plot_note(self, ax, text, loc="nw"):
        """The interpretive sentence that used to crowd the title: small
        and grey, in a corner the data leaves empty -- the time traces
        start at idle on the left edge (nw), and a falling spectrum leaves
        the lower left clear (sw)."""
        xy = (0.01, 0.99) if loc == "nw" else (0.01, 0.01)
        ax.annotate(text, xy, xycoords="axes fraction", fontsize=6.5,
                    color="#999999", ha="left",
                    va="top" if loc == "nw" else "bottom")

    def _plot_error(self, snaps, cmp=()):
        s = self.session
        tms = s.t * 1e3
        sc = self._out_scale()
        ax = self.ax_err
        ax.clear()
        n = len(snaps)
        for idx, sn in enumerate(snaps):
            ax.plot(tms, (s.loop.target - s.loop._seen(sn["y"])) * sc,
                    color=self._iter_colour(idx, n),
                    lw=1.1 if idx == n - 1 else 0.8,
                    ls="--" if sn.get("run") is not None else "-",
                    label=self._snap_label(sn),
                    **self._dot_kw(len(tms), ms=2.6))
        total = n
        for stem, cs, csnaps, col in cmp:
            ctms = cs.t * 1e3            # each stem on its OWN grid & scale
            csc = cs.loop.channel.mon_scale
            k = len(csnaps)
            for idx, sn in enumerate(csnaps):
                ax.plot(ctms, (cs.loop.target - cs.loop._seen(sn["y"])) * csc,
                        color=self._cmp_colour(col, idx, k),
                        lw=1.1 if idx == k - 1 else 0.8,
                        ls="--" if sn.get("run") is not None else "-",
                        label=self._cmp_label(stem, sn),
                        **self._dot_kw(len(ctms), ms=2.6), zorder=CMP_ZORDER)
            total += k
        if n:
            m = snaps[-1]["m"]
            ax.set_title(f"target - measured:  iter {snaps[-1]['it']} peak "
                         f"{m['peak_err_hv']:.1f} V, rms {m['rms_err_hv']:.2f} V"
                         f"  ({m['peak_pct']:.3f}% FS)")
            st = snaps[-1].get("stats")
            if st is not None and len(st["sem_t"]) == len(tms):
                e = (s.loop.target - s.loop._seen(snaps[-1]["y"])) * sc
                band = st["sem_t"] * sc
                ax.fill_between(tms, e - band, e + band, color=self._iter_colour(n - 1, n),
                                alpha=0.22, lw=0,
                                label=f"+/-1 standard error of the {st['n_shots']}-shot mean")
        if total:
            ax.legend(loc="best", fontsize=7, ncols=2 if total > 6 else 1)
        else:
            ax.text(0.5, 0.5, "no measurements stored yet",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#888888")
        ax.axhline(0, color=TARGET_COLOUR, lw=0.5)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel(f"error at the {self._out_name()} (V)")
        ax.grid(True, alpha=0.3)
        self._finish_time_axis(ax)
        self._draw(self.fig_err)

    def _spec_avg(self):
        """The 'spectra avg' box: blank/1 = the raw FFT, N = Welch with N
        segments on both spectrum tabs. See avg_spectrum for the trade."""
        raw = self.specavg_var.get().strip()
        if not raw:
            return 1
        try:
            return max(1, int(float(raw)))
        except ValueError:
            if self._specavg_warned != raw:
                self._specavg_warned = raw
                self.log(f"spectra avg {raw!r} is not a number -- "
                         f"using the raw FFT")
            return 1

    def _plot_spectrum(self, snaps, cmp=()):
        s = self.session
        sc = self._out_scale()
        ax = self.ax_spec
        ax.clear()
        kavg = self._spec_avg()

        def asd(e, dt, scale):
            return avg_spectrum(e * scale, dt, kavg)

        n = len(snaps)
        for idx, sn in enumerate(snaps):
            fe, ae = asd(s.loop.target - s.loop._seen(sn["y"]), s.loop.dt, sc)
            ax.loglog(fe, ae, color=self._iter_colour(idx, n),
                      lw=1.0 if idx == n - 1 else 0.7,
                      ls="--" if sn.get("run") is not None else "-",
                      label=self._snap_label(sn),
                      **self._dot_kw(len(fe), ms=2.2))
        total = n
        for stem, cs, csnaps, col in cmp:
            csc = cs.loop.channel.mon_scale
            k = len(csnaps)
            for idx, sn in enumerate(csnaps):
                fe, ae = asd(cs.loop.target - cs.loop._seen(sn["y"]),
                             cs.loop.dt, csc)
                ax.loglog(fe, ae, color=self._cmp_colour(col, idx, k),
                          lw=1.0 if idx == k - 1 else 0.7,
                          ls="--" if sn.get("run") is not None else "-",
                          label=self._cmp_label(stem, sn),
                          **self._dot_kw(len(fe), ms=2.2), zorder=CMP_ZORDER)
            total += k
        if s.loop.frf is not None:
            ax.axvspan(s.loop.frf.f_use, s.loop.frf.f_max, color="#c68000",
                       alpha=0.15, label="FRF taper band")
            ax.axvline(s.loop.frf.f_max, color="#c68000", lw=0.7, ls="--")
        else:
            ax.axvline(s.loop.f_cut, color="#c68000", lw=0.7, ls="--",
                       label=f"f_cut {s.loop.f_cut/1e3:g} kHz")
        if n and snaps[-1].get("stats") is not None:
            # the newest measurement's noise floor, in the plot's own units:
            # the drawn spectrum scaled by the stats' sem/error ratio per bin
            # (both Hann spectra of the same record, so the ratio holds
            # whatever the normalisation above; Welch smoothing makes it a
            # proxy, still fair). Error inside the band is the shots'
            # scatter; the loop can only learn what sits well above it.
            st = snaps[-1]["stats"]
            fe, ae = asd(s.loop.target - s.loop._seen(snaps[-1]["y"]), s.loop.dt, sc)
            ratio = st["sem_spec"] / np.maximum(st["err_spec"], 1e-30)
            floor = ae * np.interp(fe, st["f"], ratio)
            # the per-bin standard error is itself a noisy estimate (a few
            # dozen shots): smoothed over 15 bins in the log domain for the
            # eye; the bands view carries the exact numbers
            k = 15
            lf = np.log(np.maximum(floor, 1e-30))
            pad = np.r_[np.full(k, lf[0]), lf, np.full(k, lf[-1])]
            floor_s = np.exp(np.convolve(pad, np.ones(k) / k, mode="same")[k:-k])
            ax.fill_between(fe, floor_s * 1e-3, floor_s, color="#999999", alpha=0.12, lw=0,
                            label=f"noise floor: standard error of the {st['n_shots']}-shot "
                                  f"mean (smoothed)")
            ax.loglog(fe, 2 * floor_s, color="#777777", lw=0.8, ls=":",
                      label="2x floor: learnable above")
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel(f"error amplitude at the {self._out_name()} (V)")
        ax.set_title("error spectrum, target - measured"
                     + (f"  ({kavg}-segment average)" if kavg > 1 else ""))
        self._plot_note(ax, "the update only acts left of the band edge",
                        loc="sw")
        if total:
            ax.legend(loc="best", fontsize=7, ncols=2 if total > 6 else 1)
        ax.grid(True, which="both", alpha=0.3)
        self._draw(self.fig_spec)

    def _dcorr_ref(self, s, gui_gain=False):
        """Reference drive the corrections are measured against: the stored
        iteration-0 drive when there is one, else the target's flat
        conversion. Only the active session (gui_gain) may use the panel's
        first-shot gain entry -- compare stems fall back to their own
        plant gain."""
        by_it = self._snaps_by_it(s)
        if 0 in by_it and by_it[0].get("u") is not None:
            return by_it[0]["u"], "the iteration-0 drive"
        g = None
        if gui_gain:
            try:
                g = self._first_shot_gain()
            except RuntimeError:
                g = None
        g = g or s.loop.plant.gain
        return s.loop.target / g, f"the flat conversion target/{g:g}"

    def _plot_dcorr(self, snaps, cmp=()):
        """The drive side of the error plot: each iteration's AWG waveform
        minus the target's flat conversion -- the correction the loop has
        accumulated at the input, in millivolts at the AWG."""
        s = self.session
        tms = s.t * 1e3
        ax = self.ax_dcor
        ax.clear()
        # hold runs replay the SAME drive -- their correction is identical
        # to the base iteration's, so only base measurements draw here
        snaps = [sn for sn in snaps if sn.get("run") is None]
        u_ref, ref_lab = self._dcorr_ref(s, gui_gain=True)
        n = len(snaps)
        shown = 0
        skipped = []
        for idx, sn in enumerate(snaps):
            u = sn.get("u")
            if u is None or len(u) != len(s.t):
                skipped.append(f"{sn['it']}")
                continue
            ax.plot(tms, (u - u_ref) * 1e3, color=self._iter_colour(idx, n),
                    lw=1.1 if idx == n - 1 else 0.8,
                    label=self._snap_label(sn),
                    **self._dot_kw(len(tms), ms=2.6))
            shown += 1
        cshown = 0
        for stem, cs, csnaps, col in cmp:
            csnaps = [sn for sn in csnaps if sn.get("run") is None]
            c_ref, _ = self._dcorr_ref(cs)   # each stem vs its OWN reference
            ctms = cs.t * 1e3
            k = len(csnaps)
            for idx, sn in enumerate(csnaps):
                u = sn.get("u")
                if u is None or len(u) != len(cs.t):
                    skipped.append(f"{stem} {sn['it']}")
                    continue
                ax.plot(ctms, (u - c_ref) * 1e3,
                        color=self._cmp_colour(col, idx, k),
                        lw=1.1 if idx == k - 1 else 0.8,
                        label=self._cmp_label(stem, sn),
                        **self._dot_kw(len(ctms), ms=2.6), zorder=CMP_ZORDER)
                cshown += 1
        if shown + cshown:
            ax.legend(loc="best", fontsize=7,
                      ncols=2 if shown + cshown > 6 else 1)
        else:
            ax.text(0.5, 0.5, "no drives stored for the selected iterations",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#888888")
        if skipped:
            ax.annotate(f"no stored drive for iter {', '.join(skipped)}",
                        (0.02, 0.02),
                        xycoords="axes fraction", fontsize=7, color="#888888")
        ax.axhline(0, color=TARGET_COLOUR, lw=0.5)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("drive correction at the AWG (mV)")
        ax.set_title(f"drive minus {ref_lab}"
                     + (" (compare stems: their own reference)"
                        if cshown else ""))
        self._plot_note(ax, "what the loop has learned to add at the input")
        ax.grid(True, alpha=0.3)
        self._finish_time_axis(ax)
        self._draw(self.fig_dcor)

    def _plot_dspec(self, snaps, cmp=()):
        """Spectrum of the drive corrections, mirroring the error spectrum:
        where the LEARNED correction lives. In-band structure is the loop
        doing its job; content right of the band edge is correction the
        update could not have put there and did not remove -- a hand-edited
        drive, a stale state, or a band that was wider earlier."""
        s = self.session
        ax = self.ax_dspec
        ax.clear()
        kavg = self._spec_avg()

        def asd(e, dt):
            return avg_spectrum(e, dt, kavg)

        # same drive -> same correction: base measurements only, like the
        # Drive corrections tab
        snaps = [sn for sn in snaps if sn.get("run") is None]
        u_ref, _ = self._dcorr_ref(s, gui_gain=True)
        n = len(snaps)
        shown = 0
        for idx, sn in enumerate(snaps):
            u = sn.get("u")
            if u is None or len(u) != len(s.t):
                continue
            fe, ae = asd((u - u_ref) * 1e3, s.loop.dt)
            ax.loglog(fe, ae, color=self._iter_colour(idx, n),
                      lw=1.0 if idx == n - 1 else 0.7,
                      label=self._snap_label(sn),
                      **self._dot_kw(len(fe), ms=2.2))
            shown += 1
        for stem, cs, csnaps, col in cmp:
            csnaps = [sn for sn in csnaps if sn.get("run") is None]
            c_ref, _ = self._dcorr_ref(cs)   # each stem vs its OWN reference
            k = len(csnaps)
            for idx, sn in enumerate(csnaps):
                u = sn.get("u")
                if u is None or len(u) != len(cs.t):
                    continue
                fe, ae = asd((u - c_ref) * 1e3, cs.loop.dt)
                ax.loglog(fe, ae, color=self._cmp_colour(col, idx, k),
                          lw=1.0 if idx == k - 1 else 0.7,
                          label=self._cmp_label(stem, sn),
                          **self._dot_kw(len(fe), ms=2.2), zorder=CMP_ZORDER)
                shown += 1
        if s.loop.frf is not None:
            ax.axvspan(s.loop.frf.f_use, s.loop.frf.f_max, color="#c68000",
                       alpha=0.15, label="FRF taper band")
            ax.axvline(s.loop.frf.f_max, color="#c68000", lw=0.7, ls="--")
        else:
            ax.axvline(s.loop.f_cut, color="#c68000", lw=0.7, ls="--",
                       label=f"f_cut {s.loop.f_cut/1e3:g} kHz")
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel("correction amplitude at the AWG (mV)")
        ax.set_title("drive-correction spectrum"
                     + (f"  ({kavg}-segment average)" if kavg > 1 else ""))
        self._plot_note(ax, "the update cannot put power right of the band "
                            "edge -- content there is inherited", loc="sw")
        if shown:
            ax.legend(loc="best", fontsize=7, ncols=2 if shown > 6 else 1)
        else:
            ax.text(0.5, 0.5, "no drives stored for the selected iterations",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#888888")
        ax.grid(True, which="both", alpha=0.3)
        self._draw(self.fig_dspec)

    def _table_rows(self, cmp=()):
        """One row per stored base iteration and hold run, compare stems
        after the active session. History supplies the metrics (it carries
        the model tag); snapshots add what only they know -- the played
        drive's peak, the wall-clock time, the dt against the reference."""
        def row(stem, s, it, run, model, m, sn):
            def num(key):
                v = m.get(key) if isinstance(m, dict) else None
                return f"{v:.3f}" if v is not None else ""
            u = sn.get("u") if sn else None
            tw = sn.get("t_wall") if sn else None
            return (stem, it, f"r{run}" if run is not None else "", model,
                    num("peak_err_hv"), num("rms_err_hv"),
                    num("peak_pct"), num("rms_pct"),
                    f"{np.abs(u).max():.3f}" if u is not None else "",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tw))
                    if tw else "",
                    self._dt_suffix(sn, s) if sn else "")

        rows = []
        for stem, s in [(self.session.stem, self.session)] + \
                       [(g[0], g[1]) for g in cmp]:
            by_it = self._snaps_by_it(s)
            hist = list(s.loop.history)

            # the model column names the model that BUILT the row's drive.
            # history[i] is tagged with the model that consumed measurement
            # i (making drive i+1), so the row reads the tag one back --
            # and iteration 0 stays blank: its drive is Init's first shot,
            # no model involved. A hold run replays its base iteration's
            # drive, so it inherits the same label.
            def drive_model(i):
                if 1 <= i <= len(hist) and isinstance(hist[i - 1], dict):
                    return hist[i - 1].get("model") or ""
                return ""

            for i in sorted(set(range(len(hist))) | set(by_it)):
                sn = by_it.get(i)
                m = hist[i] if i < len(hist) else sn["m"]
                rows.append(row(stem, s, i, None, drive_model(i), m, sn))
                for r in sorted((x for x in s.snapshots
                                 if x["it"] == i and x.get("run") is not None),
                                key=lambda x: x["run"]):
                    rows.append(row(stem, s, i, r["run"], drive_model(i),
                                    r["m"], r))
        return rows

    def _fill_table(self, cmp=()):
        tv = self.table
        tv.delete(*tv.get_children())
        for r in self._table_rows(cmp):
            tv.insert("", "end", values=r)

    def _save_table(self):
        if self.session is None or not self.table.get_children():
            return messagebox.showerror("Table", "nothing to save yet")
        path = filedialog.asksaveasfilename(
            title="Save iteration table", defaultextension=".csv",
            initialdir=RUN_DIR,
            initialfile=f"iterations_{self.session.stem}.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        self._save_table_csv(path)
        self.log(f"table saved to {path}")

    def _save_table_csv(self, path):
        """Exactly what the Table tab shows, as CSV."""
        rows = [self.table.item(i, "values")
                for i in self.table.get_children()]
        pd.DataFrame(rows, columns=self._table_heads).to_csv(path,
                                                             index=False)

    def _plot_ddelta(self, snaps, cmp=()):
        """Iteration-to-iteration drive change: u_k minus u_(k-1) -- the
        update the loop actually applied going into iteration k, in mV at
        the AWG. Shrinking updates are convergence seen from the input;
        an update that stops shrinking while the error flat-lines says the
        loop is re-learning the same correction against noise or drift."""
        s = self.session
        tms = s.t * 1e3
        ax = self.ax_ddel
        ax.clear()
        # same-drive hold runs have a zero delta by construction -- base only
        snaps = [sn for sn in snaps if sn.get("run") is None]
        by_it = self._snaps_by_it()      # the prior drive can come from any
        n = len(snaps)                   # stored iteration, selected or not
        shown = 0
        skipped = []
        for idx, sn in enumerate(snaps):
            u = sn.get("u")
            prev = by_it.get(sn["it"] - 1)
            up = prev.get("u") if prev else None
            if (u is None or up is None
                    or len(u) != len(s.t) or len(up) != len(s.t)):
                skipped.append(f"{sn['it']}")
                continue
            ax.plot(tms, (u - up) * 1e3, color=self._iter_colour(idx, n),
                    lw=1.1 if idx == n - 1 else 0.8,
                    label=f"{self._snap_label(sn)} - iter {sn['it'] - 1}",
                    **self._dot_kw(len(tms), ms=2.6))
            shown += 1
        for stem, cs, csnaps, col in cmp:
            csnaps = [sn for sn in csnaps if sn.get("run") is None]
            c_by_it = self._snaps_by_it(cs)
            ctms = cs.t * 1e3
            k = len(csnaps)
            for idx, sn in enumerate(csnaps):
                u = sn.get("u")
                prev = c_by_it.get(sn["it"] - 1)
                up = prev.get("u") if prev else None
                if (u is None or up is None
                        or len(u) != len(cs.t) or len(up) != len(cs.t)):
                    skipped.append(f"{stem} {sn['it']}")
                    continue
                ax.plot(ctms, (u - up) * 1e3,
                        color=self._cmp_colour(col, idx, k),
                        lw=1.1 if idx == k - 1 else 0.8,
                        label=f"{self._cmp_label(stem, sn)} "
                              f"- iter {sn['it'] - 1}",
                        **self._dot_kw(len(ctms), ms=2.6), zorder=CMP_ZORDER)
                shown += 1
        if shown:
            ax.legend(loc="best", fontsize=7, ncols=2 if shown > 6 else 1)
        else:
            ax.text(0.5, 0.5, "needs stored drives for an iteration AND the "
                              "one immediately before it",
                    ha="center", va="center", transform=ax.transAxes,
                    color="#888888")
        if skipped:
            ax.annotate("no prior-iteration drive for iter "
                        f"{', '.join(skipped)}",
                        (0.02, 0.02), xycoords="axes fraction", fontsize=7,
                        color="#888888")
        ax.axhline(0, color=TARGET_COLOUR, lw=0.5)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("drive change at the AWG (mV)")
        ax.set_title("u_k minus u_(k-1)")
        self._plot_note(ax, "the update each shown iteration applied -- "
                            "shrinking updates are convergence at the input")
        ax.grid(True, alpha=0.3)
        self._finish_time_axis(ax)
        self._draw(self.fig_ddel)

    def _plot_convergence(self, cmp=()):
        s, c = self.session, self._colour()
        hist = list(s.loop.history)
        # The newest measurement is only in history once update() ran on it;
        # show it anyway so the final bench iteration appears (hold runs
        # never enter the history -- nothing was updated). Pick the last
        # BASE measurement rather than the last snapshot: a Hold appends run
        # snapshots on top of it, and testing snapshots[-1] then dropped the
        # very iteration the hold runs are being compared against -- the
        # curve stopped one point short of its own hold markers. The compare
        # branch below has always filtered; this is it doing the same.
        base = [sn for sn in s.snapshots if sn.get("run") is None]
        if base and base[-1]["it"] == len(hist):
            hist = hist + [base[-1]["m"]]
        ax = self.ax_conv
        ax.clear()
        n_it = len(hist)
        if hist:
            k = np.arange(len(hist))
            ax.semilogy(k, [m["peak_err_hv"] for m in hist], "o-", color=c,
                        lw=1.0, ms=4, label="peak error")
            ax.semilogy(k, [m["rms_err_hv"] for m in hist], "s--", color=c,
                        lw=0.8, ms=3, alpha=0.6, label="rms error")
            run_pts = [(sn["it"], sn["m"]["peak_err_hv"])
                       for sn in s.snapshots if sn.get("run") is not None]
            if run_pts and self.showruns_var.get():
                xs, ys = zip(*run_pts)
                ax.semilogy(xs, ys, "o", ms=4, mfc="none", color="#c68000",
                            label="hold runs (same drive)")
            # the measurement's own noise floor, rms: nothing under it is
            # learnable, so an rms curve that reaches it has converged
            fl = [(i, m["floor_rms"] * self._out_scale()) for i, m in enumerate(hist)
                  if isinstance(m, dict) and np.isfinite(m.get("floor_rms", np.nan))]
            if fl:
                xs, ys = zip(*fl)
                ax.semilogy(xs, ys, ":", color="#999999", lw=1.0,
                            label="noise floor rms (standard error of the mean)")
        # compare stems: their WHOLE campaign's peak-error curve (the
        # Compare box's iteration selection only affects the time plots)
        for stem, cs, csnaps, col in cmp:
            chist = list(cs.loop.history)
            base = [sn for sn in cs.snapshots if sn.get("run") is None]
            if base and base[-1]["it"] == len(chist):
                chist = chist + [base[-1]["m"]]
            if not chist:
                continue
            kk = np.arange(len(chist))
            ax.semilogy(kk, [m["peak_err_hv"] for m in chist], "o-",
                        color=col, lw=0.9, ms=3, label=f"{stem} peak error", zorder=CMP_ZORDER)
            ax.semilogy(kk, [m["rms_err_hv"] for m in chist], "s--",
                        color=col, lw=0.7, ms=2.5, alpha=0.6,
                        label=f"{stem} rms error", zorder=CMP_ZORDER)
            n_it = max(n_it, len(chist))
        if n_it:
            ax.set_xticks(np.arange(n_it))
            ax.legend(loc="best", fontsize=7)
        if hist:
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
        elif not n_it:
            ax.text(0.5, 0.5, "no iterations yet", ha="center", va="center",
                    transform=ax.transAxes, color="#888888")
        ax.set_xlabel("iteration")
        ax.set_ylabel(f"error at the {self._out_name()} (V)")
        ax.set_title(f"{s.channel} '{s.stem}' -- convergence")
        ax.grid(True, which="both", alpha=0.3)
        self._draw(self.fig_conv)

    def _plot_convergence_time(self):
        """The same errors against the clock: thermal transients after an
        output was off, the slow floor changes of September, and how long a
        hold series ran all read from the time axis, not the iteration
        count. Restarts (a new run id) are marked; hold runs hollow."""
        s, c = self.session, self._colour()
        ax = self.ax_convt
        ax.clear()
        sc = self._out_scale()
        base = sorted([sn for sn in s.snapshots if sn.get("run") is None and sn.get("t_wall")],
                      key=lambda sn: sn["t_wall"])
        runs = [sn for sn in s.snapshots if sn.get("run") is not None and sn.get("t_wall")]
        if not base and not runs:
            ax.text(0.5, 0.5, "no timestamped measurements", ha="center", va="center",
                    transform=ax.transAxes, color="#888888")
            self._draw(self.fig_convt)
            return

        def when(sn):
            return mdates.date2num(datetime.datetime.fromtimestamp(sn["t_wall"]))
        if base:
            xs = [when(sn) for sn in base]
            ax.semilogy(xs, [sn["m"]["peak_err_hv"] for sn in base], "o-", color=c, lw=1.0,
                        ms=4, label="peak error")
            ax.semilogy(xs, [sn["m"]["rms_err_hv"] for sn in base], "s--", color=c, lw=0.8,
                        ms=3, alpha=0.6, label="rms error")
            for x, sn in zip(xs, base):
                ax.annotate(str(sn["it"]), (x, sn["m"]["peak_err_hv"]), fontsize=6,
                            color="#666666", xytext=(0, 4), textcoords="offset points",
                            ha="center")
            prev = None
            for x, sn in zip(xs, base):
                rid = sn.get("run_id")
                if prev is not None and rid != prev:
                    ax.axvline(x, color="#8a8a8a", lw=0.7, ls=":")
                prev = rid
            fl = [(x, sn["stats"]["floor_rms"] * sc) for x, sn in zip(xs, base)
                  if sn.get("stats") is not None]
            if fl:
                fx, fy = zip(*fl)
                ax.semilogy(fx, fy, ":", color="#999999", lw=1.0, label="noise floor rms")
        if runs and self.showruns_var.get():
            ax.semilogy([when(sn) for sn in runs], [sn["m"]["peak_err_hv"] for sn in runs],
                        "o", ms=4, mfc="none", color="#c68000", label="hold runs (same drive)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.set_xlabel("wall clock")
        ax.set_ylabel(f"error at the {self._out_name()} (V)")
        ax.set_title(f"{s.channel} '{s.stem}' -- convergence against the clock")
        self._plot_note(ax, "dotted verticals: a new run (the output was off in between)")
        ax.legend(loc="best", fontsize=7)
        ax.grid(True, which="both", alpha=0.3)
        self._draw(self.fig_convt)

    # ------------------------------------------------------------ diagnostics
    def _plot_diagnostics(self, snaps):
        self._plot_fold(snaps)
        self._plot_lattice(snaps)
        self._plot_bands()
        self._plot_contraction()
        self._plot_heatmap()
        self._plot_dacstep(snaps)
        self._plot_pd()

    def _with_stats(self, snaps=None, base_only=True):
        s = self.session
        pool = s.snapshots if snaps is None else snaps
        out = [sn for sn in pool if sn.get("stats") is not None
               and not (base_only and sn.get("run") is not None)]
        return sorted(out, key=lambda sn: (sn["it"], sn.get("run") or 0))

    def _plot_pd(self):
        """The analyzer photodiode per stored iteration of the shown session
        (top: volts against ms, coloured as the Waveforms tab) and each one's
        change against the last stored (bottom: mV), with each iteration's
        monitor error rms in the legend. From meas_<stem>PD_iNN.npy, which the
        pair bench loop and the batch keep when the Scope channels map has a
        PD channel; nothing but a note when the session has none."""
        s = self.session
        ax, axd = self.ax_pd, self.ax_pdd
        ax.clear()
        axd.clear()
        pstem = ilc_batch.pd_stem(s.stem) or s.stem
        by = {}
        for sn in s.snapshots:
            if sn.get("run") is None and sn.get("pd") is not None and len(sn["pd"]) == len(s.t):
                by[sn["it"]] = sn            # a later base measurement replaces an earlier one
        sel = [by[k] for k in sorted(by)]
        if not sel:
            ax.set_axis_off()
            axd.set_visible(False)
            ax.text(0.5, 0.5, f"no PD recorded for {pstem}\n(the pair bench loop and the batch keep "
                              f"meas_{pstem}PD_iNN.npy when the Scope channels map has a PD channel)",
                    ha="center", va="center", transform=ax.transAxes, color="#888888")
            self._draw(self.fig_pd)
            return
        ax.set_axis_on()
        axd.set_visible(True)
        tms = s.t * 1e3
        n = len(sel)
        ref = sel[-1]
        key = self._pair_key_of(s) or None
        other = None
        if key is not None:
            ok = "X2" if key == "X1" else "X1"
            other = (ok, self._snaps_by_it(self.pair[ok]))
        st = ref.get("pd_stats") or {}
        ch = int(st.get("scope_ch", -1)) if st else -1
        if ch < 1:
            try:
                ch = chanmap.channel_for(self.cfg, "PD") or -1
            except ValueError:
                ch = -1
        deg = float(st.get("analyzer_deg", float("nan"))) if st else float("nan")
        for idx, sn in enumerate(sel):
            col = self._iter_colour(idx, n)
            lw = 1.1 if idx == n - 1 else 0.8
            ax.plot(tms, sn["pd"], color=col, lw=lw,
                    label=f"iter {sn['it']}: peak {float(np.max(sn['pd'])):.3f} V",
                    **self._dot_kw(len(tms)))
            rms = f"{key or s.channel} monitor rms {sn['m']['rms_err_hv']:.2f} V"
            osn = other[1].get(sn["it"]) if other is not None else None
            if osn is not None:
                rms += f", {other[0]} {osn['m']['rms_err_hv']:.2f} V"
            axd.plot(tms, (sn["pd"] - ref["pd"]) * 1e3, color=col, lw=lw,
                     label=f"iter {sn['it']}: {rms}", **self._dot_kw(len(tms)))
        ax.set_title(f"PD on CH{ch if ch > 0 else '?'} by iteration, {pstem}"
                     + (f", analyzer {deg:+.1f} deg" if np.isfinite(deg) else ""))
        ax.set_ylabel("PD (V)")
        ax.set_xlabel("time (ms)")
        lo, hi = ax.get_ylim()
        ax.set_ylim(lo, hi + 0.3 * (hi - lo))        # headroom: the note sits above the traces
        ax.legend(loc="upper right", fontsize=6, ncols=2 if n > 8 else 1)
        ax.grid(True, alpha=0.3)
        self._plot_note(ax, "at a fixed analyzer angle the PD reads cos^2 of the light's rotation, so "
                            "its change tracks the light's response to the loop;\nmost sensitive with "
                            "the analyzer ~45 deg from the polarization, blind at extinction or full "
                            "transmission")
        axd.axhline(0, color=TARGET_COLOUR, lw=0.5)
        axd.set_title(f"PD minus iter {ref['it']} (the last stored)")
        axd.set_ylabel("PD change (mV)")
        axd.set_xlabel("time (ms)")
        axd.legend(loc="best", fontsize=6, ncols=2 if n > 8 else 1)
        axd.grid(True, alpha=0.3)
        self._draw(self.fig_pd)

    def _plot_fold(self, snaps):
        """The residual folded on one ADC display code, for the selected
        iterations (top, with each bin's standard error), and the fold's
        peak-to-peak against iteration (bottom, one sigma bars). A pattern
        reads many sigma above its bars; the dithered scope reads one to
        three. Hollow markers: amplitude under 3 sigma (nothing resolved)."""
        s = self.session
        sc = self._out_scale()
        ax, axa = self.ax_fold, self.ax_foldamp
        ax.clear(); axa.clear()
        sel = [sn for sn in self._with_stats(snaps, base_only=False)
               if np.isfinite(sn["stats"].get("fold_amp", np.nan))]
        n = len(sel)
        for idx, sn in enumerate(sel):
            st = sn["stats"]
            ax.errorbar(st["fold_centres"], st["fold_mean"] * sc * 1e3,
                        yerr=st["fold_sem"] * sc * 1e3,
                        fmt="o--" if sn.get("run") is not None else "o-", ms=3,
                        lw=0.9, capsize=2, color=self._iter_colour(idx, n),
                        label=f"{self._snap_label(sn)}: {st['fold_amp']*sc:.2f} V pk-pk, "
                              f"{st['fold_amp']/max(st['fold_sig'], 1e-12):.1f} sigma")
        if sel:
            code = sel[-1]["stats"]["code_V"]
            ax.set_xlabel(f"position within one ADC code (fraction of {code*1e3:.2f} mV at "
                          f"{sel[-1]['stats']['vdiv']:g} V/div)")
            ax.legend(loc="best", fontsize=6, ncols=2 if n > 6 else 1)
        else:
            ax.text(0.5, 0.5, "no fold: measurements before 12 Oct 2026 have no stats "
                              "sidecar, or the scope's V/div was not read",
                    ha="center", va="center", transform=ax.transAxes, color="#888888")
            ax.set_xlabel("position within one ADC code")
        ax.set_ylabel(f"mean residual (mV at the {self._out_name()})")
        ax.set_title("residual folded on the scope's display code (the 8-bit converter's pattern)")
        ax.axhline(0, color=TARGET_COLOUR, lw=0.5)
        ax.grid(True, alpha=0.3)
        self._plot_note(ax, "flat within the bars = the dither is doing its job")
        allst = [sn for sn in self._with_stats() if np.isfinite(sn["stats"].get("fold_amp", np.nan))]
        if allst:
            its = [sn["it"] for sn in allst]
            amp = np.array([sn["stats"]["fold_amp"] for sn in allst]) * sc * 1e3
            sig = np.array([sn["stats"]["fold_sig"] for sn in allst]) * sc * 1e3
            axa.errorbar(its, amp, yerr=np.minimum(sig, 0.999 * amp), fmt="o", ms=4,
                         capsize=2, color=self._colour(), lw=0.9,
                         label="fold pk-pk, 1 sigma (slow error removed before folding)")
            axa.set_yscale("log")
            weak = amp < 3 * sig
            if weak.any():
                axa.plot(np.array(its)[weak], amp[weak], "o", ms=4, mfc="white",
                         color=self._colour(), label="under 3 sigma: not resolved")
            axa.plot(its, 3 * sig, ":", color="#999999", lw=0.9, label="3 sigma of the bins")
            axa.set_xticks(its)
            axa.legend(loc="best", fontsize=7)
        axa.set_xlabel("iteration")
        axa.set_ylabel(f"fold pk-pk (mV at the {self._out_name()})")
        axa.grid(True, alpha=0.3)
        self._draw(self.fig_fold)

    def _plot_lattice(self, snaps):
        """Histogram of the newest selected measurement's RAW-rate residual
        in units of the scope's word (HRES: 10.24 V / 4096 per V/div). Values
        piling up on integer words say the converter's lattice is in the
        data undithered; a dithered record spreads out. Needs the
        native-rate file the 'keep native-rate avg' tick writes."""
        s = self.session
        ax = self.ax_latt
        ax.clear()
        sel = [sn for sn in snaps if sn.get("run") is None] or list(snaps)
        done = False
        if sel:
            sn = sel[-1]
            suffix = f"_r{sn['run']:02d}" if sn.get("run") is not None else ""
            path = os.path.join(os.path.dirname(s.state_path),
                                f"meas_{s.stem}_i{sn['it']:02d}{suffix}_native.npz")
            st = sn.get("stats")
            vdiv = st["vdiv"] if st is not None and np.isfinite(st.get("vdiv", np.nan)) else None
            if os.path.exists(path):
                z = np.load(path)
                tn, yn = np.asarray(z["t"], float), np.asarray(z["y"], float)
                tgt = np.interp(tn, s.t, s.loop.target, left=np.nan, right=np.nan)
                ok = np.isfinite(tgt)
                word = 10.24 / 4096 * (vdiv if vdiv else 1.0)
                h = dg.lattice_hist(tgt[ok] - yn[ok], word)
                centres = 0.5 * (h["edges"][1:] + h["edges"][:-1])
                ax.bar(centres, h["counts"], width=np.diff(h["edges"]), color=self._colour(),
                       alpha=0.8, label=f"{self._snap_label(sn)}: {h['n_distinct']} distinct "
                                        f"values in {h['n']} samples")
                ax.axhline(h["flat"], color="#999999", lw=0.8, ls=":",
                           label="even spread over this span")
                for k in range(-4, 5):
                    ax.axvline(k, color="#cccccc", lw=0.5)
                ax.set_xlabel(f"raw residual (scope words of {word*1e3:.2f} mV"
                              + (f" at {vdiv:g} V/div)" if vdiv else ", 1 V/div assumed)"))
                ax.legend(loc="best", fontsize=7)
                done = True
        if not done:
            ax.text(0.5, 0.5, "no native-rate capture for the newest selected iteration\n"
                              "(tick 'keep native-rate avg' in Bench settings before a run)",
                    ha="center", va="center", transform=ax.transAxes, color="#888888")
            ax.set_xlabel("raw residual (scope words)")
        ax.set_ylabel("samples")
        ax.set_title("raw-rate residual on the converter's word lattice")
        self._plot_note(ax, "spikes on the integer words = undithered quantisation")
        ax.grid(True, alpha=0.3)
        self._draw(self.fig_latt)

    def _plot_bands(self):
        """Rms error per campaign band against iteration, with each band's
        noise floor dotted in its own colour (the bound nothing under it is
        learnable) and the growth factor per iteration over the last five
        points in the legend, with its one sigma: above 1.02 the band is
        pumping, the gate the September plans used."""
        s = self.session
        ax = self.ax_bands
        ax.clear()
        sc = self._out_scale()
        sel = self._with_stats()
        if not sel:
            ax.text(0.5, 0.5, "no band statistics stored yet", ha="center", va="center",
                    transform=ax.transAxes, color="#888888")
        else:
            bands = sel[-1]["stats"]["bands"]
            its = np.array([sn["it"] for sn in sel])
            cols = matplotlib.colormaps["tab10"]
            for b, (lo, hi) in enumerate(bands):
                rms = np.array([sn["stats"]["bands_rms"][b] for sn in sel]) * sc
                fl = np.array([sn["stats"]["bands_floor"][b] for sn in sel]) * sc
                g, sg = dg.growth_factor(rms)
                lab = f"{lo/1e3:g}-{hi/1e3:g} kHz"
                if np.isfinite(g):
                    lab += f": x{g:.3f} +/- {sg:.3f} per iteration"
                ax.semilogy(its, rms, "o-", ms=3.5, lw=1.0, color=cols(b), label=lab)
                ax.semilogy(its, fl, ":", lw=1.0, color=cols(b))
            ax.semilogy([], [], ":", color="#999999", label="dotted: the band's noise floor")
            ax.set_xticks(its)
            ax.legend(loc="best", fontsize=7)
        ax.set_xlabel("iteration")
        ax.set_ylabel(f"rms error in band (V at the {self._out_name()})")
        ax.set_title("error per campaign band, with each band's noise floor")
        self._plot_note(ax, "a band at its floor is done; a factor above 1.02 is pumping")
        ax.grid(True, which="both", alpha=0.3)
        self._draw(self.fig_bands)

    def _plot_contraction(self):
        """The contraction the loop actually had per band, |1 - gamma x
        (achieved / predicted)|, from the model check that follows every
        bench iteration. Under 1 the band converges; at or above it the
        next update overshoots. Hollow markers: the band's update did not
        correlate with what the monitor did (corr < 0.3), so the ratio is
        noise. Nothing drawn for iterations measured before the record
        existed, or for the first iteration after a restart."""
        s = self.session
        ax = self.ax_lam
        ax.clear()
        rows = [(i, m["model_bands"]) for i, m in enumerate(s.loop.history)
                if isinstance(m, dict) and m.get("model_bands")]
        if not rows:
            ax.text(0.5, 0.5, "no model-check bands in the history yet\n"
                              "(recorded from the second bench iteration of a run)",
                    ha="center", va="center", transform=ax.transAxes, color="#888888")
        else:
            cols = matplotlib.colormaps["tab10"]
            labels = {}
            for i, mb in rows:
                for b, r in enumerate(mb):
                    if r.get("lam") is None:
                        continue
                    key = (r["lo"], r["hi"])
                    if key not in labels:
                        labels[key] = len(labels)
                    k = labels[key]
                    weak = r.get("verdict") == "unresponsive"
                    ax.plot([i], [r["lam"]], "o", ms=5, color=cols(k),
                            mfc="white" if weak else cols(k),
                            label=(f"{r['lo']/1e3:g}-{r['hi']/1e3:g} kHz" if i == rows[-1][0]
                                   or not any(rr.get("lam") is not None and (rr["lo"], rr["hi"]) == key
                                              for _j, mbb in rows[rows.index((i, mb)) + 1:] for rr in mbb)
                                   else None))
            for key, k in labels.items():
                pts = [(i, r["lam"]) for i, mb in rows for r in mb
                       if (r["lo"], r["hi"]) == key and r.get("lam") is not None]
                if len(pts) > 1:
                    xs, ys = zip(*pts)
                    ax.plot(xs, ys, "-", lw=0.7, color=cols(k), alpha=0.6)
            ax.axhline(1.0, color="#a93a2c", lw=0.9)
            ax.axhspan(1.0, max(1.5, ax.get_ylim()[1]), color="#a93a2c", alpha=0.08)
            ax.set_ylim(bottom=0)
            ax.set_xticks([i for i, _ in rows])
            h, l = ax.get_legend_handles_labels()
            seen, hh, ll = set(), [], []
            for a, b in zip(h, l):
                if b and b not in seen:
                    seen.add(b); hh.append(a); ll.append(b)
            ax.legend(hh, ll, loc="best", fontsize=7)
        ax.set_xlabel("iteration (the update that made it)")
        ax.set_ylabel("|1 - gamma x achieved/predicted|")
        ax.set_title("contraction per band from the model check")
        self._plot_note(ax, "shaded: no contraction, the correction overshoots; hollow: corr < 0.3")
        ax.grid(True, alpha=0.3)
        self._draw(self.fig_lam)

    def _plot_heatmap(self):
        """Every stored base iteration's error as one image: time across,
        iteration down, colour the error at the output. Corner ripple, hold
        creep and end effects separate at a glance, and a band growing at
        the corners shows as a stripe getting brighter down the image."""
        s = self.session
        ax = self.ax_heat
        ax.clear()
        if self._heat_cbar is not None:
            try:
                self._heat_cbar.remove()
            except Exception:
                pass
            self._heat_cbar = None
        sc = self._out_scale()
        base = sorted([sn for sn in s.snapshots if sn.get("run") is None and len(sn["y"]) == len(s.t)],
                      key=lambda sn: sn["it"])
        if len(base) < 2:
            ax.text(0.5, 0.5, "needs at least two stored iterations", ha="center",
                    va="center", transform=ax.transAxes, color="#888888")
        else:
            its = [sn["it"] for sn in base]
            E = np.array([(s.loop.target - s.loop._seen(sn["y"])) * sc for sn in base])
            vmax = float(np.nanpercentile(np.abs(E), 99)) or 1.0
            tms = s.t * 1e3
            im = ax.imshow(E, aspect="auto", cmap="RdBu_r", vmin=-vmax, vmax=vmax,
                           extent=[tms[0], tms[-1], its[-1] + 0.5, its[0] - 0.5],
                           interpolation="nearest")
            ax.set_yticks(its)
            self._heat_cbar = self.fig_heat.colorbar(im, ax=ax, pad=0.01)
            self._heat_cbar.set_label(f"error at the {self._out_name()} (V), +/-99th percentile")
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("iteration")
        ax.set_title("error across the record, every stored iteration")
        self._draw(self.fig_heat)

    def _plot_dacstep(self, snaps):
        """The newest selected iteration's update against the generator's
        DAC step (20 Vpp / 65536 = 0.31 mV): the histogram of |du| in steps,
        the fraction under one step (rounded away by the generator), and
        that fraction's history. Once most of an update is under a step the
        loop can only limit-cycle on the quantizer."""
        s = self.session
        ax = self.ax_dac
        if self._dac_inset is not None:
            try:
                self._dac_inset.remove()
            except Exception:
                pass
            self._dac_inset = None
        ax.clear()
        by_it = self._snaps_by_it()
        sel = [sn for sn in snaps if sn.get("run") is None and sn.get("u") is not None]
        done = False
        if sel:
            sn = sel[-1]
            prev = by_it.get(sn["it"] - 1)
            if prev is not None and prev.get("u") is not None and len(prev["u"]) == len(sn["u"]):
                r = dg.update_vs_step(np.asarray(sn["u"]) - np.asarray(prev["u"]))
                centres = 0.5 * (r["edges"][1:] + r["edges"][:-1])
                ax.bar(centres, r["counts"], width=np.diff(r["edges"]), color=self._colour(),
                       alpha=0.8, label=f"update into iter {sn['it']}: {r['below']*100:.0f}% of "
                                        f"samples under one step, rms {r['rms_steps']:.2f} steps"
                                        + (f", {r['over']} off scale" if r["over"] else ""))
                ax.axvline(1.0, color="#a93a2c", lw=0.9, label="one DAC step (rounded away below)")
                done = True
        hist_pts = [(i, m["update_below_lsb"]) for i, m in enumerate(s.loop.history)
                    if isinstance(m, dict) and m.get("update_below_lsb") is not None]
        if hist_pts:
            ax2 = self._dac_inset = ax.inset_axes([0.55, 0.55, 0.42, 0.4])
            xs, ys = zip(*hist_pts)
            ax2.plot(xs, np.array(ys) * 100, "o-", ms=3, lw=0.8, color=self._colour())
            ax2.set_ylim(0, 100)
            ax2.set_xlabel("iteration", fontsize=7)
            ax2.set_ylabel("% under one step", fontsize=7)
            ax2.tick_params(labelsize=6)
            ax2.grid(True, alpha=0.3)
        if not done:
            ax.text(0.5, 0.3, "needs the selected iteration's drive and the one before it",
                    ha="center", va="center", transform=ax.transAxes, color="#888888")
        else:
            ax.legend(loc="upper left", fontsize=7)
        ax.set_xlabel(f"|update| in DAC steps of {dg.AWG_LSB*1e3:.2f} mV")
        ax.set_ylabel("samples")
        ax.set_title("the drive update against the generator's quantizer")
        ax.grid(True, alpha=0.3)
        self._draw(self.fig_dac)

    def _frf_paths(self, c=None):
        """The FRF field, expanded: semicolon-separated entries, each a
        path or a glob (Windows paths carry spaces, so ';' separates).
        Show FRF overlays every match -- amplitude families side by side;
        the measured-FRF MODEL requires exactly one."""
        out = []
        for part in (c or self._col1).frf_var.get().split(";"):
            part = part.strip()
            if not part:
                continue
            if "*" in part or "?" in part:
                out += sorted(glob.glob(part))
            else:
                out.append(part)
        seen = set()
        return [p for p in out if not (p in seen or seen.add(p))]

    def do_show_frf(self, c=None):
        c = c or self._col1
        paths = self._frf_paths(c)
        if not paths:
            return messagebox.showerror("FRF", "the FRF field is empty "
                                        "(or the glob matched nothing)")
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            return messagebox.showerror("FRF", f"not found: {missing[0]!r}")
        try:
            f = self._floats(f_use=c.fuse_var, f_max=c.fmax_var)
        except RuntimeError as e:
            return messagebox.showerror("FRF", str(e))
        axm, axp, axc = self.ax_frf
        for ax in self.ax_frf:
            ax.clear()
        multi = len(paths) > 1
        names, any_dropped = [], False
        f_lo_all, f_hi_all = np.inf, 0.0
        for i, path in enumerate(paths):
            d = pd.read_csv(path)
            ok = d["coherence"].to_numpy() >= 0.9     # FRF's own default cut
            any_dropped = any_dropped or bool((~ok).any())
            col = ("#1f77b4" if i == 0
                   else CMP_COLOURS[(i - 1) % len(CMP_COLOURS)])
            name = re.sub(r"^frf_|\.csv$", "", os.path.basename(path))
            names.append(name)
            f_lo_all = min(f_lo_all, float(d["f_Hz"].min()))
            f_hi_all = max(f_hi_all, float(d["f_Hz"].max()))
            axm.loglog(d["f_Hz"][ok], d["H_mag"][ok], "o-", ms=3, lw=0.9,
                       color=col, label=name if multi else "measured")
            axm.loglog(d["f_Hz"][~ok], d["H_mag"][~ok], "o", ms=3,
                       mfc="none", color=col if multi else "#c62828",
                       label=None if multi
                       else "coherence < 0.9 (dropped)")
            ph = np.degrees(np.unwrap(
                np.radians(d["H_phase_deg"][ok].to_numpy())))
            axp.semilogx(d["f_Hz"][ok], ph, "o-", ms=3, lw=0.9, color=col)
            axc.semilogx(d["f_Hz"], d["coherence"], "o-", ms=3, lw=0.9,
                         color=col if multi else "#2e7d32")
            self.log(f"FRF {name}: {int(ok.sum())}/{len(d)} tones coherent, "
                     f"{d['f_Hz'].min():.0f}-{d['f_Hz'].max():.0f} Hz")
        axm.set_ylabel("|H| (mon V / AWG V)")
        axm.set_title(", ".join(names))
        if multi and any_dropped:
            self._plot_note(axm, "open markers: coherence < 0.9 "
                                 "(dropped on load)", loc="sw")
        axp.set_ylabel("phase, unwrapped (deg)")

        # Overlay the current parametric model, if one is selected and filled
        # in: this is the model-vs-chain comparison that decided the campaign
        # (see docs/REPORT.md section 5) and it is what shows how much each
        # rung of the model ladder explains.
        key = self._model_key(c)
        overlay = None
        if key != "frf":
            try:
                overlay = self._entry_params(key, strict=False, c=c)
            except RuntimeError:
                overlay = None
        if overlay:
            fg = np.geomspace(f_lo_all, f_hi_all, 300)
            # Plant.response rather than a second hand-rolled copy of it:
            # this is the same H that fit_frf minimises against and that the
            # contraction number divides by, so the curve drawn here cannot
            # drift from the one the loop is judged on -- and a new rung
            # (third order) overlays correctly without touching this block.
            # response() is continuous, so dt does not enter.
            sess = self._col_session(c)
            dt_ov = sess.loop.dt if sess is not None else 2e-6
            H = self._plant_from(overlay, dt_ov).response(fg)
            axm.loglog(fg, np.abs(H), "--", lw=1.1, color="#c68000",
                       label=f"model: {DESC_FOR[key]}")
            axp.semilogx(fg, np.degrees(np.unwrap(np.angle(H))), "--",
                         lw=1.1, color="#c68000")
        axc.axhline(0.9, color="#c62828", lw=0.7, ls=":")
        axc.set_ylabel("coherence")
        axc.set_xlabel("frequency (Hz)")
        for ax in self.ax_frf:
            ax.axvspan(f["f_use"], f["f_max"], color="#c68000", alpha=0.15)
            ax.grid(True, which="both", alpha=0.3)
        if multi or any_dropped or overlay:
            axm.legend(loc="best", fontsize=7)
        self._draw(self.fig_frf)
        # by frame, not index -- tab positions have moved before (measured)
        self._show_fig(self.fig_frf)
        self.log(f"taper {f['f_use']/1e3:g}-{f['f_max']/1e3:g} kHz shaded")


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
