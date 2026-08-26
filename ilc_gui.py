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
from eomilc.config import CHANNELS, LIMITS, HV_PER_MON
import ilc_bench            # the debugged bench helpers; main() is guarded
import run_ilc              # load_target and the state-file conventions

# Remembered between sessions, kept out of the repo so a git pull cannot
# clobber it -- same convention as the sibling panels. Off Windows there is
# no APPDATA: macOS gets its native equivalent, anything else the home dir.
CONFIG_PATH = os.path.join(
    os.environ.get("APPDATA")
    or (os.path.expanduser("~/Library/Application Support")
        if sys.platform == "darwin" else os.path.expanduser("~")),
    "EOM-ILC-GUI", "config.json")

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
    "GEN": dict(mon_col="CH1", awg_ch=1, scope_ch=1,
                frf=None, colour="#2e7d32"),
}
TARGET_COLOUR = "#222222"
PRED_COLOUR = "#8a8a8a"
# compare-stem overlays: one colour per stem, leading with hues far from
# viridis (the active session's dark-purple-to-yellow iteration ramp) --
# purple and cyan sit last because they can pass for viridis endpoints
CMP_COLOURS = ["#ff7f0e", "#e377c2", "#8c564b", "#9467bd", "#17becf",
               "#7f7f7f"]

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


def avg_spectrum(e, dt, k=1):
    """Amplitude spectrum of e, optionally noise-averaged.

    k = 1: the raw single-record FFT (bin width 1/(N dt)). Full frequency
    resolution, but a periodogram's per-bin variance is ~100% of its value
    and does NOT average down with record length -- more points only add
    more equally-noisy bins.
    k > 1: Hann-windowed 50%-overlap segment averaging (Welch), segment
    length N//k. Noise variance drops ~sqrt(segments); resolution coarsens
    to ~k/(N dt), so tones closer than that merge and content localised in
    time (burst edges) smears across segments. Normalised so a pure tone
    KEEPS its displayed amplitude in both modes -- the broadband noise
    floor, by contrast, scales with bin width, so only compare curves
    drawn at equal k."""
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


# The MSO-X accepts only these readout counts (:WAVeform:POINts); asked for
# anything else it rounds to a value it likes -- possibly DOWN, so pick the
# next one up ourselves. Without an explicit request it hands over 5000
# points regardless of the band (measured 26 Aug: 3 us readout of a 15 ms
# window killed the first 200 kHz FRF run).
SCOPE_PTS = (2000, 5000, 10000, 20000, 50000, 62500)


def scope_points_for(need):
    return next((p for p in SCOPE_PTS if p >= need), SCOPE_PTS[-1])


AWG_MAX_PTS = int(os.environ.get("BK4063B_MAX_PTS", 16384))
# 16384 is the 4063B's datasheet arb memory; this bench has only stored
# 5301-point records so far, so the true ceiling is unprobed -- if a dense
# upload is refused on hardware, lower BK4063B_MAX_PTS and re-measure.

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
    max_pts = max_pts or AWG_MAX_PTS
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
        # pair it with the drive that played it, so Fit uses a true pair
        dcsv = os.path.join(run_dir, f"drive_{s.stem}_i{it:02d}.csv")
        if os.path.exists(dcsv):
            du = pd.read_csv(dcsv, comment="#").iloc[:, 1].to_numpy(float)
            if len(du) == len(s.t):
                snap["u"] = du
        s.snapshots.append(snap)


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
        c = dict(self.cfg)
        c.update(geometry=self.root.winfo_geometry(),
                 state=self.state_var.get(), target=self.target_var.get(),
                 measured=self.meas_var.get(), frf=self.frf_var.get(),
                 f_use=self.fuse_var.get(), f_max=self.fmax_var.get(),
                 model=self.model_var.get(), channel=self.channel_var.get(),
                 stem=self.stem_var.get(), shot_gain=self.shotgain_var.get(),
                 iter_sel=self.itersel_var.get(),
                 cmp_sel=self.cmpsel_var.get(),
                 spec_avg=self.specavg_var.get(),
                 dot_step=self.dotstep_var.get(),
                 show_runs=self.showruns_var.get(),
                 dt_labels=self.dtlabels_var.get(),
                 link_t=self.tlink_var.get(),
                 hold_runs=self.holdruns_var.get(),
                 hold_gap=self.holdgap_var.get(),
                 keep_native=self.keepnative_var.get(),
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
        sf = ttk.LabelFrame(left, text="Session", padding=3)
        sf.pack(fill="x", pady=(0, 2))
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
        b.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(2, 0))
        self._actions.append(b)

        self.summary = ttk.Label(sf, text="no session loaded", justify="left",
                                 font=MONO)
        self.summary.grid(row=6, column=0, columnspan=4, sticky="w", pady=(2, 0))
        sf.columnconfigure(1, weight=1)

        # ---- inverse model -------------------------------------------
        vf = ttk.LabelFrame(left, text="Inverse model (what the update "
                                       "divides the error by)", padding=3)
        vf.pack(fill="x", pady=(0, 2))
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
        self._fcut_entry = ttk.Entry(r2, textvariable=self.fcut_var, width=7)
        self._fcut_entry.pack(
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
        self._fuse_entry = ttk.Entry(r4, textvariable=self.fuse_var, width=7)
        self._fuse_entry.pack(side="left", padx=(2, 8))
        ttk.Label(r4, text="taper to zero at Hz").pack(side="left")
        self._fmax_entry = ttk.Entry(r4, textvariable=self.fmax_var, width=7)
        self._fmax_entry.pack(side="left", padx=2)
        b = ttk.Button(r4, text="Show FRF", command=self.do_show_frf)
        b.pack(side="right")
        self._actions.append(b)
        b = ttk.Button(r4, text="Measure FRF...", command=self.do_measure_frf)
        b.pack(side="right", padx=(0, 4))
        self._actions.append(b)
        vf.columnconfigure(1, weight=1)
        self._update_model_fields()

        # ---- capture post-processing ---------------------------------
        # Settings that shape how a MEASUREMENT is turned into an error --
        # nothing here touches the first shot, which is a pure flat
        # conversion. Applies to both Step and the bench loop.
        pf = ttk.LabelFrame(left, text="Capture post-processing "
                                       "(Step + Bench)", padding=3)
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

        # ---- manual step ---------------------------------------------
        mf = ttk.LabelFrame(left, text="Step from captured files", padding=3)
        mf.pack(fill="x", pady=(0, 2))
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
        r2 = ttk.Frame(mf)
        r2.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(2, 0))
        self.step_btn = ttk.Button(r2, text="Step  (captures -> update drive)",
                                   command=self.do_step)
        self.step_btn.pack(side="left", fill="x", expand=True)
        self._actions.append(self.step_btn)
        b = ttk.Button(r2, text="Native spectrum",
                       command=self.do_native_spec)
        b.pack(side="left", padx=(4, 0))
        self._actions.append(b)
        mf.columnconfigure(1, weight=1)

        # ---- bench loop ----------------------------------------------
        bf = ttk.LabelFrame(left, text="Bench loop (upload -> capture -> update)",
                            padding=3)
        bf.pack(fill="x", pady=(0, 2))
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
        r0b = ttk.Frame(bf); r0b.grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(r0b, text="skip setup checks (don't)",
                        variable=self.skip_var).pack(side="left")
        self.keepnative_var = tk.BooleanVar(
            value=bool(self.cfg.get("keep_native", False)))
        ttk.Checkbutton(r0b, text="keep native-rate avg",
                        variable=self.keepnative_var).pack(side="left",
                                                           padx=(10, 0))
        rr = ttk.Frame(bf); rr.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        b = ttk.Button(rr, text="Auto-set instruments", command=self.do_autoset)
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)
        self.upload_btn = ttk.Button(rr, text="Upload drive to AWG",
                                     command=self.do_upload)
        self.upload_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._actions.append(self.upload_btn)
        r2 = ttk.Frame(bf); r2.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        self.bench_btn = ttk.Button(r2, text="Run bench loop", command=self.do_bench)
        self.bench_btn.pack(side="left", fill="x", expand=True)
        self._actions.append(self.bench_btn)
        self.stop_btn = ttk.Button(r2, text="Stop", command=self.stop_evt.set,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(4, 0))
        r3 = ttk.Frame(bf); r3.grid(row=4, column=0, sticky="ew", pady=(2, 0))
        ttk.Label(r3, text="runs").pack(side="left")
        self.holdruns_var = tk.StringVar(value=str(self.cfg.get("hold_runs",
                                                                "5")))
        ttk.Entry(r3, textvariable=self.holdruns_var, width=4).pack(
            side="left", padx=(2, 6))
        ttk.Label(r3, text="gap s").pack(side="left")
        self.holdgap_var = tk.StringVar(value=str(self.cfg.get("hold_gap",
                                                               "30")))
        ttk.Entry(r3, textvariable=self.holdgap_var, width=6).pack(
            side="left", padx=(2, 6))
        b = ttk.Button(r3, text="Hold  (re-measure this drive, no update)",
                       command=self.do_hold)
        b.pack(side="left", fill="x", expand=True)
        self._actions.append(b)
        ttk.Label(bf, text="Close the AWG GUI and Scope Grab first (both hold VISA).\n"
                           "Outputs switch OFF when a run that played anything ends.",
                  foreground="#666666").grid(row=5, column=0, sticky="w")
        bf.columnconfigure(0, weight=1)

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
        ttk.Label(sel, text="Iterations").pack(side="left")
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

        # second row: other campaigns overlaid read-only on the same plots
        sel2 = ttk.Frame(right)
        sel2.pack(fill="x", pady=(0, 2))
        ttk.Label(sel2, text="Compare").pack(side="left")
        self.cmpsel_var = tk.StringVar(value=self.cfg.get("cmp_sel", ""))
        e3 = ttk.Entry(sel2, textvariable=self.cmpsel_var, width=26)
        e3.pack(side="left", padx=2)
        e3.bind("<Return>", lambda ev: self._redraw_iterations())
        ttk.Label(sel2, text="other stems, e.g. 'TSTX1 OLDX1:all OLDX2:0,3' "
                             "(blank sel = last iter)",
                  foreground="#666666").pack(side="left")
        ttk.Label(sel2, text="segs (blank = raw FFT)",
                  foreground="#666666").pack(side="right")
        self.specavg_var = tk.StringVar(value=self.cfg.get("spec_avg", ""))
        e4 = ttk.Entry(sel2, textvariable=self.specavg_var, width=4)
        e4.pack(side="right", padx=2)
        e4.bind("<Return>", lambda ev: self._redraw_iterations())
        ttk.Label(sel2, text="spectra avg").pack(side="right")
        self._specavg_warned = None
        self._cmp_cache = {}         # state path -> (state mtime, Session)
        self._cmp_logged = set()     # compare warnings already shown ...
        self._cmp_lastspec = None    # ... for this spec (reset on change)

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.fig_wave, (self.ax_out, self.ax_drv) = self._tab("Waveforms", 2, sharex=True)
        self.fig_dcor, (self.ax_dcor,) = self._tab("Drive corrections", 1)
        self.fig_dspec, (self.ax_dspec,) = self._tab("Drive spectrum", 1)
        self.fig_ddel, (self.ax_ddel,) = self._tab("Drive updates", 1)
        self.fig_err, (self.ax_err,) = self._tab("Error", 1)
        self.fig_spec, (self.ax_spec,) = self._tab("Error spectrum", 1)
        self.fig_conv, (self.ax_conv,) = self._tab("Convergence", 1)
        self._table_tab()
        self.fig_frf, self.ax_frf = self._tab("FRF", 3, sharex=True)

        # Home on any TIME-domain figure homes them all: the nav stacks of
        # panes that were only ever synced programmatically are empty, so
        # their own Home button would silently do nothing (measured).
        for fig in (self.fig_wave, self.fig_err, self.fig_dcor,
                    self.fig_ddel):
            fig._toolbar.home = self._wrap_home(fig._toolbar)

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
        tb = NavigationToolbar2Tk(canvas, frame)     # zoom/pan, scope habits
        fig._canvas = canvas
        fig._toolbar = tb
        return fig, axes

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
        a greyed box says 'this model does not know about that'. That
        includes the band knobs: f_cut belongs to the parametric rungs
        (the FRF path deliberately never pre-filters at it -- ilc.update),
        and the f_use/f_max taper belongs to the measured FRF."""
        key = self._model_key()
        need = PARAMS_FOR[key]
        for k, e in self._param_entries.items():
            e.configure(state="normal" if k in need else "disabled")
        frf = key == "frf"
        self._fcut_entry.configure(state="disabled" if frf else "normal")
        for e in (self._fuse_entry, self._fmax_entry):
            e.configure(state="normal" if frf else "disabled")

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
                "The measured FRF is not fitted here -- the measurement IS "
                "the identification. Use Measure FRF... to take one (or the "
                "CLI: tools/sysid_make.py + sysid_fit.py), then set the "
                "taper inside its coherent band and switch the Model to "
                "'measured FRF'. Fit belongs to the parametric rungs.")
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
        self._finish_time_axis(self.ax_out)
        self.fig_wave._canvas.draw_idle()
        self.nb.select(0)
        self._wave_redraw = lambda: self.do_preview_target(quiet=True)

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
        # blank on a resumed campaign.
        recall_snapshots(s)
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
        fpaths = self._frf_paths()
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
        # persist the averaged measurement beside the state, exactly as the
        # bench loop does -- so a Step-measured iteration survives a close
        # and reloads with the session
        np.save(os.path.join(os.path.dirname(s.state_path),
                             f"meas_{s.stem}_i{it:02d}.npy"), y)
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
            s.snapshots.append(dict(it=it, y=y, m=m, u=u_prev, t_wall=time.time()))
            self.msgs.put(("call", lambda: self._show_iteration(u_prev, y, m, it)))
            print("REFUSED to write a drive that violates a hard limit "
                  "(tick 'force' to override)")
            return
        s.u = u_next
        s.iteration = it + 1
        s.snapshots.append(dict(it=it, y=y, m=m, u=u_prev, t_wall=time.time()))
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
            if f.lower().endswith(".npz"):
                # a bench-kept native average: t is already waveform time
                d = np.load(f)
                tt, yy = np.asarray(d["t"], float), np.asarray(d["y"], float)
            else:                        # a raw Scope Grab CSV
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
        self.fig_spec._canvas.draw_idle()
        # front the Error spectrum tab (by its frame, not a magic index)
        self.nb.select(self.fig_spec._canvas.get_tk_widget().master)

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

    # ----------------------------------------------------------------- hold
    def do_hold(self):
        """Re-measure the CURRENT drive several times without updating --
        for thermalisation studies: how does the error of one fixed drive
        evolve over minutes? Measurements are tagged as runs (iter k r1,
        r2, ...) so they never mix with the loop's own iterations, and the
        state is untouched."""
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
        self.run_worker(lambda: self._hold_work(int(f["runs"]), f["gap_s"],
                                                int(f["awg_ch"]),
                                                int(f["scope_ch"]),
                                                int(f["repeats"]), f["wait"],
                                                self.keepnative_var.get()),
                        "hold: re-measuring...")

    def _hold_work(self, runs, gap_s, awg_ch, scope_ch, repeats,
               wait_s, keep_native=False):
        s = self.session
        scopemod, awgmod = self._bench_modules()
        awg = ilc_bench.make_awg(awgmod)
        print("AWG:  ", awg.connect())
        scope = ilc_bench.make_scope(scopemod)
        print("Scope:", scope.connect())
        played = False
        switched_on = False
        try:
            problems, notes = ilc_bench.check_awg_channel(
                awg, awg_ch, full_scale=s.full_scale)
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

            it = s.iteration
            wname = f"{s.stem}_i{it:02d}"
            n, frac = ilc_bench.upload_drive(awg, awg_ch, wname, s.u,
                                             s.full_scale)
            played = True
            print(f"hold: uploaded {wname} ({n} pts, {100*frac:.1f}% of DAC "
                  f"range) -- this drive will NOT be updated")
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
                y = self._bench_capture(scope, scope_ch, s.t, s.t_off,
                                        repeats, wait_s, native_path=nat)
                np.save(os.path.join(os.path.dirname(s.state_path),
                                     f"meas_{s.stem}_i{it:02d}_r{r:02d}.npy"),
                        y)
                m = s.loop.metrics(y)
                sn = dict(it=it, y=y, m=m, u=s.u, run=r, t_wall=time.time())
                s.snapshots.append(sn)
                print(f"  r{r} (+{fmt_span(time.time() - t_start)}): error "
                      f"peak {m['peak_err_hv']:7.1f} V   "
                      f"rms {m['rms_err_hv']:6.2f} V")
                self.msgs.put(("call", self._redraw_iterations))
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
                except Exception as e:
                    print(f"could not switch CH{awg_ch} output off: {e}")
            awg.close()
            scope.close()
            print("instruments closed")

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
                                                 self.skip_var.get(),
                                                 self.keepnative_var.get()),
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
                    skip, keep_native=False):
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
                if k == k0:
                    time.sleep(0.5)
                    ilc_bench.verify_alignment(scope, awg_ch, u, s.t, s.t_off,
                                               wait_s)
                nat = (os.path.join(RUN_DIR, f"meas_{wname}_native.npz")
                       if keep_native else None)
                y = self._bench_capture(scope, scope_ch, s.t, s.t_off,
                                        repeats, wait_s, native_path=nat)
                np.save(os.path.join(RUN_DIR, f"meas_{wname}.npy"), y)
                m = s.loop.metrics(y)
                m["model"] = cfg["desc"]
                print(f"         error: peak {m['peak_err_hv']:7.1f} V   "
                      f"rms {m['rms_err_hv']:6.2f} V   ({m['peak_pct']:.2f}% FS)")
                s.snapshots.append(dict(it=k, y=y, m=m, u=u, t_wall=time.time()))
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
                  ("name", f"AUTO{suffix}"))
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
            ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

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
                mode, n_p, dt_p = plan_frf_grid(len(s.t), s.loop.dt, f_hi)
                u, bins = build_frf_probe(n_p, dt_p, peak, f_lo, f_hi, tones)
                slew, i_pk, hv_pk = probe_demand(u, dt_p, s.loop.plant.gain,
                                                 s.loop.channel)
                if hv_pk > LIMITS.hv_max:
                    raise RuntimeError(
                        f"flat-gain peak output {hv_pk:.0f} V exceeds the "
                        f"{LIMITS.hv_max:.0f} V limit -- lower the probe peak")
                f = self._floats(awg_ch=self.awgch_var,
                                 scope_ch=self.scopech_var,
                                 repeats=self.repeats_var, wait=self.wait_var)
                fmax_now = float(self.fmax_var.get() or 0)
            except (RuntimeError, ValueError) as e:
                return messagebox.showerror("Measure FRF", str(e),
                                            parent=dlg)
            if (slew > LIMITS.slew_hv or i_pk > LIMITS.current) and \
                    not messagebox.askyesno(
                        "Probe demand", parent=dlg, message=(
                    f"Under a FLAT-GAIN worst case this probe asks the "
                    f"amplifier for {slew/1e6:.0f} V/us "
                    f"({i_pk*1e3:.1f} mA into "
                    f"{LIMITS.load_capacitance*1e12:.0f} pF) -- past the "
                    f"{LIMITS.slew_hv/1e6:.0f} V/us / "
                    f"{LIMITS.current*1e3:.0f} mA 610E specs.\n\n"
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
                                               fmax_now, mode, dt_p),
                "measuring FRF...")

        bb = ttk.Frame(fr)
        bb.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(bb, text="Measure", command=ok).pack(side="left",
                                                        expand=True, fill="x")
        ttk.Button(bb, text="Cancel", command=dlg.destroy).pack(
            side="left", expand=True, fill="x")

    def _measure_frf_work(self, u, bins, name, awg_ch, scope_ch, repeats,
                          wait_s, fmax_now, mode="session", dt_p=None):
        s = self.session
        dt_p = dt_p or s.loop.dt
        n_p = len(u)
        rec = n_p * dt_p                     # the PROBE's record
        t_sess = len(s.t) * s.loop.dt        # the session's record
        t_grid = np.arange(n_p) * dt_p
        scopemod, awgmod = self._bench_modules()
        limit = getattr(awgmod, "MAX_ARB_NAME", NAME_LIMIT)
        if len(name) > limit:
            raise RuntimeError(f"name {name!r} is past the {limit}-char cap")
        awg = ilc_bench.make_awg(awgmod)
        print("AWG:  ", awg.connect())
        scope = ilc_bench.make_scope(scopemod)
        print("Scope:", scope.connect())
        uploaded = False
        switched_on = False
        retune = False           # short mode changed FRQ + scope window
        vert_saved = None        # ramp verticals, put back at the end
        try:
            problems, notes = ilc_bench.check_awg_channel(
                awg, awg_ch, full_scale=s.full_scale)
            for nn in notes:
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
            print(f"  {len(bins)} tones {bins[0]/rec:.0f} Hz - "
                  f"{bins[-1]/rec/1e3:.1f} kHz, peak {np.abs(u).max():.3f} V "
                  f"({100*frac:.1f}% of DAC range)")
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
                scope.put(":TIMebase:RANGe", f"{rng:.6g}")
                scope.put(":TIMebase:POSition", f"{rec/2:.6g}")
                retune = True
                print(f"  AWG FRQ -> {1.0/rec:.6g} Hz, scope window -> "
                      f"{rng*1e3:.4g} ms (both restored at the end)")
            # verticals for the probe: bipolar around ZERO, not the ramp's
            # window -- the session verticals leave the probe in 1-2 of the
            # 8 divisions and burn bits the 8-bit codes cannot spare
            vert_saved = {}
            for chn in (awg_ch, scope_ch):
                vert_saved[chn] = (
                    float(scope.get(f":CHANnel{chn}:SCALe")),
                    float(scope.get(f":CHANnel{chn}:OFFSet")))
            pk = float(np.abs(u).max())
            mon_pk = 2.0 * pk * s.loop.plant.gain
            for chn, span in ((awg_ch, 2 * pk), (scope_ch, 2 * mon_pk)):
                scale = nice_setting(1.25 * span / 8)
                scope.put(f":CHANnel{chn}:SCALe", f"{scale:.6g}")
                scope.put(f":CHANnel{chn}:OFFSet", "0")
            print(f"scope verticals for the probe: drive +/-{pk:.3g} V, "
                  f"monitor +/-{mon_pk:.3g} V (2x flat-gain headroom for "
                  f"resonant peaking), centred on 0 -- restored at the end")
            ok, switched_on = self._ensure_output_on(awg, awg_ch)
            if not ok:
                return
            H, coh = self._frf_capture(scope, awg_ch, scope_ch, bins,
                                       t_grid, s.t_off, repeats, wait_s,
                                       f_top=bins[-1] / rec)
            f_hz = bins / rec
            path = os.path.join(RUN_DIR, f"frf_{name}.csv")
            write_frf_csv(path, f_hz, H, coh)
            good = int((coh >= 0.9).sum())
            print(f"wrote {path}")
            print(f"  {len(bins)} tones, {good} with coherence >= 0.9; "
                  f"|H| {np.abs(H).max():.4f} max, "
                  f"{np.abs(H).min():.4f} min")
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

    def _frf_capture(self, scope, drive_ch, mon_ch, bins, t_grid, t_off,
                     repeats, wait_s, settle=0.5, f_top=None):
        """Per shot: read BOTH the drive and the monitor from the same
        acquisition (the record is frozen between :SINGle and run), put
        them on the record grid, and take H = Y/U at the probe's tone
        bins. H is averaged over shots -- not the traces -- so the
        shot-to-shot scatter of H itself is the coherence estimate."""
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
        Hs = []
        self.msgs.put(("progress", 0, repeats))
        for i in range(repeats):
            if self.stop_evt.is_set():
                raise RuntimeError("stopped mid-capture; nothing written")
            got = scope.single(wait_s=wait_s)
            if got is not True:
                raise RuntimeError(f"no trigger within {wait_s:g} s on "
                                   f"repeat {i+1} -- is the burst running?")
            tu, vu = scope.waveform(drive_ch, points=pts)
            ty, vy = scope.waveform(mon_ch, points=pts)
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
            uu = scopeio.resample(tu, vu, t_grid, t_offset=t_off)
            yy = scopeio.resample(ty, vy, t_grid, t_offset=t_off)
            U = np.fft.rfft(uu - uu.mean())
            Y = np.fft.rfft(yy - yy.mean())
            Hs.append(Y[bins] / U[bins])
            self.msgs.put(("progress", i + 1, repeats))
        self.msgs.put(("progress", 0, 1))
        Hs = np.asarray(Hs)
        H = Hs.mean(axis=0)
        coh = np.clip(1 - (np.abs(Hs - H).std(axis=0) / np.abs(H)) ** 2,
                      0, 1)
        return H, coh

    def _adopt_frf(self, path):
        """Main thread: point the FRF field at the fresh measurement and
        show it. Switching the MODEL to 'measured FRF' stays the user's
        call -- measuring is not consenting to drive with it."""
        self.frf_var.set(path)
        self.log(f"FRF field now points at {os.path.basename(path)}")
        self.do_show_frf()

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
        if not awg.is_on(awg_ch):
            print(f"CH{awg_ch} did not switch on -- aborting")
            return False, True
        return True, True

    def _bench_capture(self, scope, ch, t_grid, t_off, repeats, wait_s,
                       settle=0.5, native_path=None):
        """ilc_bench.capture with a progress bar and a stop check between
        shots. The settle wait happens once, after the new upload.

        native_path: also save the repeat average at the SCOPE's own sample
        rate (npz, t = waveform time, y = monitor V) -- the boxcar
        decimation onto t_grid discards everything past the grid Nyquist,
        and this file is the only way to get it back after the fact."""
        time.sleep(settle)
        traces = []
        t_nat, y_nat = None, 0.0
        # denser than the scope's 5000-point default: >= 2 samples per grid
        # step keeps the anti-alias boxcar honest, and a kept native
        # average deserves the full readout depth
        pts = (SCOPE_PTS[-1] if native_path is not None
               else scope_points_for(2.2 * len(t_grid)))
        self.msgs.put(("progress", 0, repeats))
        for i in range(repeats):
            if self.stop_evt.is_set():
                raise RuntimeError("stopped mid-capture; this iteration is "
                                   "discarded (state untouched)")
            got = scope.single(wait_s=wait_s)
            if got is not True:
                raise RuntimeError(f"no trigger within {wait_s:g} s on repeat "
                                   f"{i+1} -- is the burst running?")
            ts, vs = scope.waveform(ch, points=pts)
            scope.run()
            traces.append(scopeio.resample(ts, vs, t_grid, t_offset=t_off))
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
        return ilc.averaged(traces)

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

    def _compare_groups(self):
        """The Compare box -> [(stem, session, snaps, colour)]: sibling
        campaigns from the active state's directory, loaded read-only (never
        saved, never stepped) and overlaid on the analysis plots. Grammar:
        space-separated stems, each optionally stem:ITERS with the
        Iterations grammar; a blank selection means that stem's last
        measured iteration."""
        s0 = self.session
        spec = self.cmpsel_var.get().strip() if s0 is not None else ""
        if spec != self._cmp_lastspec:
            self._cmp_lastspec = spec
            self._cmp_logged.clear()
        if not spec:
            return []
        run_dir = os.path.dirname(s0.state_path)
        groups, seen = [], set()
        for tok in spec.split():
            stem, _, isel = tok.partition(":")
            if not stem or stem in seen:
                continue
            seen.add(stem)
            if stem == s0.stem:
                self._log_once(f"compare: {stem!r} is the loaded session "
                               f"-- skipped")
                continue
            path = os.path.join(run_dir, f"drive_{stem}.state.npz")
            if not os.path.exists(path):
                have = sorted(
                    re.match(r"drive_(.+)\.state\.npz$",
                             os.path.basename(p)).group(1)
                    for p in glob.glob(os.path.join(run_dir,
                                                    "drive_*.state.npz")))
                self._log_once(f"compare: no state for {stem!r} in {run_dir} "
                               f"(available: {', '.join(have) or 'none'})")
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
                    self._log_once(f"compare: could not load {stem!r}: {e}")
                    continue
                self._cmp_cache[path] = (mt, cs)
            snaps = self._snaps_for(cs, isel, 1, log=self._log_once)
            if not snaps and not cs.snapshots:
                self._log_once(f"compare: {stem!r} has no stored measurements"
                               f" -- only its convergence history can show")
            groups.append((stem, cs, snaps,
                           CMP_COLOURS[len(groups) % len(CMP_COLOURS)]))
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
        orig = tb.home

        def home(*args, **kw):
            orig(*args, **kw)          # this pane's own y-reset still works
            self._home_all_time_axes()
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
                    other.figure._canvas.draw_idle()
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
        self._fill_table(cmp)
        if self._wave_redraw is not None:
            self._wave_redraw()

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
        # remember this draw so Redraw (dot spacing etc.) can replay it --
        # the tab otherwise only refreshes when a step or load provides data
        self._wave_redraw = (lambda u=u, y=y, pred=pred, it=it:
                             self._plot_waveforms(u, y, pred, it))
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
        self._finish_time_axis(self.ax_out)
        self.fig_wave._canvas.draw_idle()

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
            ax.plot(tms, (s.loop.target - sn["y"]) * sc,
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
                ax.plot(ctms, (cs.loop.target - sn["y"]) * csc,
                        color=self._cmp_colour(col, idx, k),
                        lw=1.1 if idx == k - 1 else 0.8,
                        ls="--" if sn.get("run") is not None else "-",
                        label=self._cmp_label(stem, sn),
                        **self._dot_kw(len(ctms), ms=2.6))
            total += k
        if n:
            m = snaps[-1]["m"]
            ax.set_title(f"target - measured:  iter {snaps[-1]['it']} peak "
                         f"{m['peak_err_hv']:.1f} V, rms {m['rms_err_hv']:.2f} V"
                         f"  ({m['peak_pct']:.3f}% FS)")
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
        self.fig_err._canvas.draw_idle()

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
            fe, ae = asd(s.loop.target - sn["y"], s.loop.dt, sc)
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
                fe, ae = asd(cs.loop.target - sn["y"], cs.loop.dt, csc)
                ax.loglog(fe, ae, color=self._cmp_colour(col, idx, k),
                          lw=1.0 if idx == k - 1 else 0.7,
                          ls="--" if sn.get("run") is not None else "-",
                          label=self._cmp_label(stem, sn),
                          **self._dot_kw(len(fe), ms=2.2))
            total += k
        if s.loop.frf is not None:
            ax.axvspan(s.loop.frf.f_use, s.loop.frf.f_max, color="#c68000",
                       alpha=0.15, label="FRF taper band")
            ax.axvline(s.loop.frf.f_max, color="#c68000", lw=0.7, ls="--")
        else:
            ax.axvline(s.loop.f_cut, color="#c68000", lw=0.7, ls="--",
                       label=f"f_cut {s.loop.f_cut/1e3:g} kHz")
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel(f"error amplitude at the {self._out_name()} (V)")
        ax.set_title("error spectrum, target - measured"
                     + (f"  ({kavg}-segment average)" if kavg > 1 else ""))
        self._plot_note(ax, "the update only acts left of the band edge",
                        loc="sw")
        if total:
            ax.legend(loc="best", fontsize=7, ncols=2 if total > 6 else 1)
        ax.grid(True, which="both", alpha=0.3)
        self.fig_spec._canvas.draw_idle()

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
                        **self._dot_kw(len(ctms), ms=2.6))
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
        self.fig_dcor._canvas.draw_idle()

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
                          **self._dot_kw(len(fe), ms=2.2))
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
        self.fig_dspec._canvas.draw_idle()

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
                        **self._dot_kw(len(ctms), ms=2.6))
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
        self.fig_ddel._canvas.draw_idle()

    def _plot_convergence(self, cmp=()):
        s, c = self.session, self._colour()
        hist = list(s.loop.history)
        # the newest measurement is only in history once update() ran on it;
        # show it anyway so the final bench iteration appears (hold runs
        # never enter the history -- nothing was updated)
        if (s.snapshots and s.snapshots[-1].get("run") is None
                and s.snapshots[-1]["it"] == len(hist)):
            hist = hist + [s.snapshots[-1]["m"]]
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
                        color=col, lw=0.9, ms=3, label=f"{stem} peak error")
            ax.semilogy(kk, [m["rms_err_hv"] for m in chist], "s--",
                        color=col, lw=0.7, ms=2.5, alpha=0.6,
                        label=f"{stem} rms error")
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
        self.fig_conv._canvas.draw_idle()

    def _frf_paths(self):
        """The FRF field, expanded: semicolon-separated entries, each a
        path or a glob (Windows paths carry spaces, so ';' separates).
        Show FRF overlays every match -- amplitude families side by side;
        the measured-FRF MODEL requires exactly one."""
        out = []
        for part in self.frf_var.get().split(";"):
            part = part.strip()
            if not part:
                continue
            if "*" in part or "?" in part:
                out += sorted(glob.glob(part))
            else:
                out.append(part)
        seen = set()
        return [p for p in out if not (p in seen or seen.add(p))]

    def do_show_frf(self):
        paths = self._frf_paths()
        if not paths:
            return messagebox.showerror("FRF", "the FRF field is empty "
                                        "(or the glob matched nothing)")
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            return messagebox.showerror("FRF", f"not found: {missing[0]!r}")
        try:
            f = self._floats(f_use=self.fuse_var, f_max=self.fmax_var)
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
        key = self._model_key()
        overlay = None
        if key != "frf":
            try:
                overlay = self._entry_params(key, strict=False)
            except RuntimeError:
                overlay = None
        if overlay:
            fg = np.geomspace(f_lo_all, f_hi_all, 300)
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
        axc.axhline(0.9, color="#c62828", lw=0.7, ls=":")
        axc.set_ylabel("coherence")
        axc.set_xlabel("frequency (Hz)")
        for ax in self.ax_frf:
            ax.axvspan(f["f_use"], f["f_max"], color="#c68000", alpha=0.15)
            ax.grid(True, which="both", alpha=0.3)
        if multi or any_dropped or overlay:
            axm.legend(loc="best", fontsize=7)
        self.fig_frf._canvas.draw_idle()
        # by frame, not index -- tab positions have moved before (measured)
        self.nb.select(self.fig_frf._canvas.get_tk_widget().master)
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
