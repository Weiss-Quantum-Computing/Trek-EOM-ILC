"""Offline regression suite for ilc_gui.py, against real MKJX1 campaign data.

55 numbered checks: state round-trips, the span guard, the header-less
file refusal, the FRQ-vs-record check, the seed-drive Init, the model ladder
with Fit to FRF, the per-channel limits,
the GEN from-scratch path, flat first shot, hold-run display, plot
overlays, compare-campaign overlays and their picker, status line and
tail-scrolling path entries, drive spectrum, spectrum
averaging, the native-rate spectrum and its bench-kept files, the FRF
probe + measurement maths + overlay viewer, the iteration
table, dot density, linked time axes, and the multi-channel capture the
optical campaign rides on. No instruments are touched --
bench/auto-set/upload/hold hardware paths are exercised on the bench, not
here. A Tk window flashes briefly; a PNG of every figure lands in the
scratch folder for eyeballing.

About two minutes, and nearly all of it is _redraw_iterations: it repaints
every tab, the suite calls it about fifty times, and that is 1-2 s each.
Anything here that looks slow for what it asserts is paying that, not doing
arithmetic.

Run with the Anaconda interpreter:

    C:\\ProgramData\\anaconda3\\python.exe -u tests\\smoke_test.py

Everything the GUI writes is redirected into a per-run scratch folder in
%TEMP% (reset on every start), so the real run/ state and the AWG
Waveforms library are never touched. The MKJX1 campaign data it runs
against is the committed fixture set in tests/data/ (plus the tracked
run/frf_WIDE_X1.csv), so a fresh clone can run the whole suite.
"""
import os, shutil, sys, glob, tempfile, traceback
import re
import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.join(tempfile.gettempdir(), "eom-ilc-smoke")
if os.path.isdir(SCRATCH):
    shutil.rmtree(SCRATCH)
os.makedirs(SCRATCH)
FIXTURES = os.path.join(REPO, "tests", "data")   # committed MKJX1 set --
# the suite runs from a fresh clone; run/ is live scratch and untracked
shutil.copy(os.path.join(FIXTURES, "drive_MKJX1.state.npz"), SCRATCH)
sys.path.insert(0, REPO)

import ilc_gui

# no modal dialogs in an unattended test: auto-answer and print instead
from tkinter import messagebox as _mb
_mb.showerror = lambda title, msg, **k: print(f"[dialog error] {title}: {msg}")
_mb.showinfo = lambda title, msg, **k: print(f"[dialog info] {title}: {msg}")
_mb.askyesno = lambda title, msg, **k: (print(f"[dialog yes] {title}"), True)[1]

# redirect every write target into the scratch folder
ilc_gui.RUN_DIR = os.path.join(SCRATCH, "run")
ilc_gui.LOG_PATH = os.path.join(SCRATCH, "run", "ilc_gui.log")
ilc_gui.AWG_WAVEFORMS = os.path.join(SCRATCH, "Waveforms")
ilc_gui.CONFIG_PATH = os.path.join(SCRATCH, "config.json")
os.makedirs(ilc_gui.RUN_DIR, exist_ok=True)

STATE = os.path.join(SCRATCH, "drive_MKJX1.state.npz")
FRF = os.path.join(REPO, "run", "frf_WIDE_X1.csv")
MEAS = sorted(glob.glob(os.path.join(FIXTURES, "meas_MKJX1_i*.npy")))[-1]

# ---- 1. pure-library checks -------------------------------------------------
s = ilc_gui.load_session(STATE)
print(f"[1] loaded state: {s.channel} iter {s.iteration}, stem {s.stem}, "
      f"gamma {s.loop.gamma}, f_cut {s.loop.f_cut}, t_off {s.t_off*1e6:g} us, "
      f"{len(s.t)} pts, history {len(s.loop.history)}")
assert s.channel == "EO1" and len(s.t) == len(s.u)

# fabricate a full-window scope capture from a real bench measurement
y = np.load(MEAS)
assert len(y) == len(s.t), "meas grid mismatch"
cap_csv = os.path.join(SCRATCH, "fake_ilc_i99_001.csv")
pd.DataFrame({"Time (s)": s.t + s.t_off, "CH3": y}).to_csv(cap_csv, index=False)
y2, files = ilc_gui.read_captures(os.path.join(SCRATCH, "fake_ilc_i99*.csv"),
                                  "CH3", s.t, s.t_off)
print(f"[2] read_captures: {len(files)} file(s), max dev vs source "
      f"{np.abs(y2-y).max():.2e} V")

# [2b] the noise-floor readout: where the error has dropped under the
# measurement's own scatter. Synthetic: a 10 kHz residual over white noise --
# every band above 10-15 kHz is noise-only, so the floor lands at 15 kHz;
# drown the residual and the floor is everywhere (0); put a big residual in
# the top band and there is no floor (None). Then the real path: read_captures
# hands the file stack back through its `stack` dict.
_tt = np.arange(5501) * 2e-6
_tgt = 0.5 * (1 - np.cos(np.pi * np.clip(_tt / 5e-3, 0, 1)))
_rng = np.random.default_rng(3)
def _shots(res_amp, res_f, noise, n=32):
    res = res_amp * np.sin(2 * np.pi * res_f * _tt)
    return np.array([_tgt - res + _rng.normal(0, noise, _tt.size)
                     for _ in range(n)])
_nb = ilc_gui.ilc.learnable_band(_tgt, _shots(5e-3, 10e3, 1e-3), 2e-6, 80e3)
assert _nb["f_floor"] == 15e3, _nb["f_floor"]
assert _nb["ratio_top"] < 2, _nb["ratio_top"]
assert _nb["n_shots"] == 32
assert ilc_gui.ilc.learnable_band(_tgt, _shots(5e-3, 10e3, 1.0), 2e-6,
                                  80e3)["f_floor"] == 0.0
assert ilc_gui.ilc.learnable_band(_tgt, _shots(0.5, 77e3, 1e-3), 2e-6,
                                  80e3)["f_floor"] is None
try:
    ilc_gui.ilc.learnable_band(_tgt, _shots(5e-3, 10e3, 1e-3, n=1), 2e-6, 80e3)
    raise AssertionError("one shot must be refused")
except ValueError:
    pass
_stk = {}
ilc_gui.read_captures(os.path.join(SCRATCH, "fake_ilc_i99*.csv"), "CH3", s.t, s.t_off, stack=_stk)
assert _stk["stack"].shape == (len(files), len(y2)), _stk["stack"].shape
print(f"[2b] noise floor: 10 kHz residual over noise -> floor at "
      f"{_nb['f_floor']/1e3:.0f} kHz; drowned -> everywhere; top-band residual "
      f"-> none; one shot refused; read_captures hands back the "
      f"{_stk['stack'].shape[0]}-file stack")

# [2c] the plateau detector: peak error flat over the last 5 entries while
# the update has not shrunk = re-learning noise. A converging history (both
# falling) stays quiet; a plateau fires and names the best iteration; too
# little history, or a pre-detector state without update_rms, returns None.
def _hist(peaks, upds=None):
    return [dict(peak_err_hv=p, **({"update_rms": u} if u is not None else {}))
            for p, u in zip(peaks, upds or [None] * len(peaks))]
_conv = _hist([100, 50, 25, 12, 6, 3, 1.5, 0.8],
              [.040, .020, .010, .005, .0025, .0012, .0006, .0003])
assert ilc_gui.ilc.plateau(_conv)["flat"] is False
_flat = _hist([100, 50, 25, 12, 6, 4, 3.8, 3.0, 2.8, 3.5, 2.8, 3.1, 3.0, 3.3],
              [.040, .020, .010, .006, .030, .031, .029, .032, .030, .031,
               .029, .030, .031, .030])
_p = ilc_gui.ilc.plateau(_flat)
assert _p["flat"] is True and _p["best_it"] == 8 and _p["best"] == 2.8, _p
assert ilc_gui.ilc.plateau(_flat[:5]) is None                 # too short
assert ilc_gui.ilc.plateau(_hist([100, 50, 25, 12, 6, 4, 3.8])) is None  # no update_rms
print(f"[2c] plateau: converging history quiet; flat history fires naming "
      f"iteration {_p['best_it']} ({_p['best']:.1f} V); short and pre-detector "
      f"histories return None")

# [2d] model_check: a chain that answers a gain-only model at 1x below 15 kHz
# and 4x above it -- the update in the top band then does not contract
# (|1 - 0.6 x 4| = 1.4); a monitor change unrelated to the push is
# 'unresponsive'; a band the update never touched is 'quiet'.
_tt = np.arange(5501) * 2e-6
_du = (0.02 * np.sin(2 * np.pi * 3e3 * _tt)
       + 0.01 * np.sin(2 * np.pi * 25e3 * _tt) * np.exp(-((_tt - 2e-3) / 0.3e-3) ** 2))
from scipy.signal import butter, sosfiltfilt
_lp = sosfiltfilt(butter(4, 15e3 / 250e3, output="sos"), _du)
_dy = 0.05 * (_lp + 4.0 * (_du - _lp))
_rows = ilc_gui.ilc.model_check(_du, _dy, lambda d: 0.05 * d, 2e-6, 40e3, 0.6)
_by = {(r["lo"], r["hi"]): r for r in _rows}
assert abs(_by[(0.0, 5e3)]["ratio_local"] - 1.0) < 0.1 and _by[(0.0, 5e3)]["verdict"] == "ok", _by[(0.0, 5e3)]
assert 3.5 < _by[(20e3, 40e3)]["ratio_local"] < 4.5 and _by[(20e3, 40e3)]["verdict"] == "no contraction", _by[(20e3, 40e3)]
assert _by[(20e3, 40e3)]["lam"] > 1.0 and _by[(20e3, 40e3)]["corr"] > 0.9
assert _by[(10e3, 20e3)]["verdict"] == "quiet", _by[(10e3, 20e3)]
_rnd = ilc_gui.ilc.model_check(_du, np.random.default_rng(1).normal(0, 1e-3, _tt.size),
                                lambda d: 0.05 * d, 2e-6, 40e3, 0.6)
assert all(r["verdict"] == "unresponsive" for r in _rnd if r["verdict"] != "quiet"), _rnd
print("[2d] model_check: 1x band ok, 4x band 'no contraction' (lam 1.4, corr > 0.9), untouched band quiet, noise unresponsive")
assert np.abs(y2 - y).max() < 1e-9

# span guard must refuse a zoomed capture
short = os.path.join(SCRATCH, "fake_zoom_001.csv")
n4 = len(s.t) // 4
pd.DataFrame({"Time (s)": s.t[:n4], "CH3": y[:n4]}).to_csv(short, index=False)
try:
    ilc_gui.read_captures(short, "CH3", s.t, s.t_off)
    raise AssertionError("span guard did not fire")
except RuntimeError as e:
    print(f"[3] span guard fired as it should: {str(e)[:80]}...")

# a header-less index,value file (the Waveform Editor's default save) must
# be refused by name, not read as 5500 points of seconds
ed_csv = os.path.join(SCRATCH, "editor_style.csv")
with open(ed_csv, "w") as f:
    f.write("".join(f"{i+1},{v:.10g}\n"
                    for i, v in enumerate(s.loop.target * 1000)))
try:
    ilc_gui.run_ilc.load_target(ed_csv)
    raise AssertionError("header-less editor file was not refused")
except ValueError as e:
    assert "ILC header" in str(e), str(e)
    print(f"[3b] header-less editor file refused: {str(e)[:70]}...")

# the FRQ-vs-record check in check_awg_channel, against a fake generator
import re as _re
import ilc_bench


def _fake_parse(reply):
    _, _, payload = reply.strip().partition(" ")
    fields = [x.strip() for x in payload.split(",") if x.strip()]
    out = {}
    if len(fields) % 2:
        out["STATE"] = fields.pop(0)
    for k, v in zip(fields[0::2], fields[1::2]):
        m = _re.match(r"^(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)[A-Za-z]*$", v)
        out[k] = m.group(1) if m else v
    return out


class _FakeAwg:
    def __init__(self, frq):
        self.frq = frq

    def read_channel(self, ch):
        return {"BSWV": f"C{ch}:BSWV WVTP,ARB,FRQ,{self.frq:.6f}HZ,AMP,20V,OFST,0V",
                "SRATE": f"C{ch}:SRATE MODE,DDS,VALUE,1000000",
                "OUTP": f"C{ch}:OUTP OFF,LOAD,HZ"}


_saved_mod = ilc_bench._AWGMOD
ilc_bench._AWGMOD = type("M", (), {"parse_reply": staticmethod(_fake_parse)})
_period = len(s.t) * s.loop.dt
_p_ok, _n_ok = ilc_bench.check_awg_channel(_FakeAwg(1 / _period), 1,
                                           expect_period=_period)
_p_bad, _ = ilc_bench.check_awg_channel(_FakeAwg(1.1 / _period), 1,
                                        expect_period=_period)
ilc_bench._AWGMOD = _saved_mod
assert not [p for p in _p_ok if "FRQ" in p], _p_ok
assert [p for p in _p_bad if "FRQ" in p], _p_bad
assert "FRQ" in _n_ok[0]
print(f"[3c] check_awg_channel: matching FRQ passes, a 10% stale FRQ is "
      f"flagged: {[p for p in _p_bad if 'FRQ' in p][0][:60]}...")

# ---- 2. full GUI drive-through ---------------------------------------------
import tkinter as tk
root = tk.Tk()
app = ilc_gui.App(root)
root.update()

def pump_until_idle(timeout=60):
    import time
    t0 = time.time()
    root.update()
    while app.busy and time.time() - t0 < timeout:
        root.update()
        time.sleep(0.02)
    root.update()
    assert not app.busy, "worker did not finish"

# load the scratch state through the GUI path
app.state_var.set(STATE)
app.do_load()
root.update()
print(f"[4] GUI load: summary = {app.summary.cget('text')!r}")

# Init with a seed drive: iteration 0 is the seed sample for sample, the
# state records where it came from, and a seed on the wrong grid is refused
seed_tgt = os.path.join(SCRATCH, "seed_target.csv")
seed_drv = os.path.join(SCRATCH, "seed_drive.csv")
ilc_gui.outputs.write_awg_csv(seed_tgt, s.t, s.loop.target * 1000,
                              "target copy for the seed check")
ilc_gui.outputs.write_awg_csv(seed_drv, s.t, s.u, "seed copy")
app.target_var.set(seed_tgt)
app.seed_var.set(seed_drv)
app.stem_var.set("SEEDT")
app.channel_var.set("EO1")
app.model_var.set(ilc_gui.KEY2LABEL["static"])
app.pgain_var.set("0.56")
app.do_init()
root.update()
assert app.session.stem == "SEEDT" and app.session.iteration == 0
assert np.allclose(app.session.u, s.u), "seed drive was not played as-is"
assert app.session.seed_path == os.path.abspath(seed_drv)
_st_seed = ilc_gui.run_ilc.load_state(app.session.state_path)
assert str(_st_seed["seed_path"]) == os.path.abspath(seed_drv)
_s_seed = ilc_gui.load_session(app.session.state_path)
assert _s_seed.seed_path == os.path.abspath(seed_drv), "seed_path lost on reload"
assert "seed" in app.summary.cget("text")
print(f"[4b] seed Init: iteration 0 = the seed drive, seed_path in the state "
      f"and back through load_session")
bad_seed = os.path.join(SCRATCH, "seed_short.csv")
ilc_gui.outputs.write_awg_csv(bad_seed, s.t[:-10], s.u[:-10], "wrong grid")
app.seed_var.set(bad_seed)
_errs = []
_orig_err = _mb.showerror
_mb.showerror = lambda title, msg, **k: _errs.append(msg)
app.do_init()
_mb.showerror = _orig_err
assert _errs and "points" in _errs[0], _errs
assert app.session.stem == "SEEDT" and np.allclose(app.session.u, s.u)
print(f"[4c] a seed on the wrong grid is refused: {_errs[0][:70]}...")
app.seed_var.set("")
app.model_var.set(ilc_gui.KEY2LABEL["frf"])     # back to the launch default:
app.pgain_var.set("")                           # the FRF-mode checks below
app.state_var.set(STATE)                        # rely on it
app.do_load()
root.update()
assert app.session.stem == "MKJX1" and not app.session.seed_path

# FRF viewer
app.frf_var.set(FRF)
app.fuse_var.set("50e3")
app.fmax_var.set("75e3")
app.do_show_frf()
root.update()
print("[5] FRF plotted")

# a manual step from the fabricated capture, with the measured inverse
it0 = app.session.iteration
app.meas_var.set(os.path.join(SCRATCH, "fake_ilc_i99*.csv"))
app.moncol_var.set("CH3")
app.do_step()
pump_until_idle()
s2 = app.session
print(f"[6] step: iteration {it0} -> {s2.iteration}, "
      f"snapshots {len(s2.snapshots)}, "
      f"last metrics peak {s2.snapshots[-1]['m']['peak_err_hv']:.2f} V / "
      f"rms {s2.snapshots[-1]['m']['rms_err_hv']:.2f} V")
assert s2.iteration == it0 + 1
wrote = sorted(os.listdir(ilc_gui.RUN_DIR)) + sorted(os.listdir(ilc_gui.AWG_WAVEFORMS))
print(f"[7] files written: {wrote}")
assert os.path.exists(os.path.join(SCRATCH, "meas_MKJX1_i06.npy")), \
    "manual Step did not persist its averaged measurement"
print("[7b] Step persisted meas_MKJX1_i06.npy beside the state")
assert f"drive_MKJX1_i{it0+1:02d}.csv" in wrote
assert f"MKJX1_i{it0+1:02d}.csv" in wrote

# the state must round-trip through the CLI's own loader
st = ilc_gui.run_ilc.load_state(STATE)
assert int(st["iteration"]) == it0 + 1
print(f"[8] state round-trips through run_ilc.load_state, iteration "
      f"{int(st['iteration'])}, history {len(list(st['history']))}")

# init a brand-new session from the real target into scratch dirs (the
# model is the launch-default measured FRF, whose stored plant is the gain
# alone -- typed here, as a fresh panel would have it)
app.target_var.set(os.path.join(REPO, "waveforms", "target_MKJX1.csv"))
app.channel_var.set("EO1")
app.stem_var.set("TSTX1")
if not app.pgain_var.get().strip():
    app.pgain_var.set("0.5584")
app.do_init()
root.update()
print(f"[9] init: {app.session.stem} iter {app.session.iteration}, "
      f"drive peak {np.abs(app.session.u).max():.3f} V")
assert os.path.exists(os.path.join(ilc_gui.RUN_DIR, "drive_TSTX1.state.npz"))

# go back to the stepped MKJX1 session so the error/spectrum/convergence
# tabs have content, then take a second step so the ghost overlay shows;
# real meas files beside the state exercise the recall-on-load path too
for f in sorted(glob.glob(os.path.join(FIXTURES, "meas_MKJX1_i*.npy")))[-2:]:
    shutil.copy(f, SCRATCH)
app.state_var.set(STATE)
app.do_load()
root.update()
app.frf_var.set(FRF)      # scratch RUN_DIR has no frf, so the auto-fill cleared it
app.do_step()
pump_until_idle()
root.update()
assert app.session.loop.history[-1].get("model", "").startswith("FRF"), \
    "FRF step did not tag the history"
print(f"[11] FRF-mode step tagged: {app.session.loop.history[-1]['model']}")

# iteration selection, multi-overlay plots, drive corrections, log file
avail = sorted({sn["it"] for sn in app.session.snapshots})
print(f"[21] available iterations: {avail}")
assert len(avail) >= 3, "recall-all did not pull the stored measurements"
app.itersel_var.set("all")
app._redraw_iterations(); root.update()
n_err = len(app.ax_err.get_lines())
assert n_err >= len(avail) + 1, f"'all' drew only {n_err} lines"
lo, hi = avail[-2], avail[-1]
app.itersel_var.set(f"{lo}-{hi}")
assert [sn["it"] for sn in app._selected_snaps()] == [lo, hi]
app.itersel_var.set(f"{avail[0]},{hi}")
assert [sn["it"] for sn in app._selected_snaps()] == [avail[0], hi]
app.itersel_var.set("")
assert [sn["it"] for sn in app._selected_snaps()] == avail[-2:]
app._redraw_iterations(); root.update()
assert len(app.ax_dcor.get_lines()) >= 2, "drive-corrections tab empty"
assert os.path.getsize(ilc_gui.LOG_PATH) > 0
logtxt = open(ilc_gui.LOG_PATH, encoding="utf-8").read()
assert "2026-" in logtxt, "log file lines are not timestamped"
first_panel_line = app.log_text.get("1.0", "2.0")
assert "2026-" not in first_panel_line and ":" not in first_panel_line[:9], \
    "panel log still shows timestamps"
print("[22] selection parsing, overlays, drive corrections, "
      "timestamped log file all OK")

# marker-density control: 1 = every sample, blank = auto, junk = auto+warn
app.dotstep_var.set("1")
app._redraw_iterations(); root.update()
line = app.ax_err.get_lines()[0]
assert line.get_markevery() == 1, line.get_markevery()
app.dotstep_var.set("")
app._redraw_iterations(); root.update()
auto_ev = app.ax_err.get_lines()[0].get_markevery()
assert auto_ev and auto_ev > 1, auto_ev
app.dotstep_var.set("nonsense")
app._redraw_iterations(); root.update()
assert app.ax_err.get_lines()[0].get_markevery() == auto_ev
app.dotstep_var.set("7")
app._redraw_iterations(); root.update()
wave_line = next(l for l in app.ax_drv.get_lines()
                 if l.get_label().startswith("drive"))
assert wave_line.get_markevery() == 7, \
    f"Waveforms tab ignored the dot spacing: {wave_line.get_markevery()}"
app.dotstep_var.set("")
app._redraw_iterations(); root.update()
print(f"[24] dot density: 1 -> every sample, blank -> auto (every "
      f"{auto_ev}th), junk -> auto; Waveforms tab follows Redraw")

# linked time axes: a zoom on one time plot drives them all and survives
# a redraw; unlinking stops the propagation
app.tlink_var.set(True)
app._redraw_iterations(); root.update()
app.ax_err.set_xlim(2.0, 4.0)          # what the toolbar rectangle does
root.update()
for ax in (app.ax_out, app.ax_dcor, app.ax_ddel):
    lo, hi = ax.get_xlim()
    assert abs(lo - 2.0) < 1e-9 and abs(hi - 4.0) < 1e-9, (lo, hi)
app._redraw_iterations(); root.update()
assert abs(app.ax_dcor.get_xlim()[0] - 2.0) < 1e-9, "window lost on redraw"
app.tlink_var.set(False)
app.ax_err.set_xlim(0.0, 8.0)
root.update()
assert abs(app.ax_out.get_xlim()[0] - 2.0) < 1e-9, "unlink did not stop sync"
app.tlink_var.set(True)
app._t_range = None
app._redraw_iterations(); root.update()
print("[28] linked time axes: zoom propagates, survives redraw, unlink works")

# Home pressed on a pane that was only ever synced must reset ALL panes
app.ax_err.set_xlim(3.0, 5.0)
root.update()
assert abs(app.ax_dcor.get_xlim()[0] - 3.0) < 1e-9
app.fig_dcor._toolbar.home()       # the pane the user did NOT zoom from
root.update()
panes = {"err": app.ax_err, "out": app.ax_out, "dcor": app.ax_dcor,
         "ddel": app.ax_ddel}
lims = {k: ax.get_xlim() for k, ax in panes.items()}
bad = {k: v for k, v in lims.items() if v[1] - v[0] <= 8.0}
assert not bad, f"panes not reset: {bad} (all: {lims})"
assert app._t_range is None
print("[29] Home on a synced pane resets every time plot")

# model ladder: a gain-only step from a typed gain (no calibration tables
# any more -- parameters are typed or fitted to this system's own data)
app.model_var.set("gain only (0th order)")
app._update_model_fields()
app.pgain_var.set("0.5584")
root.update()
it0 = app.session.iteration
app.do_step()
pump_until_idle()
p = app.session.loop.plant
assert app.session.iteration == it0 + 1
assert p.fn == 0 and p.tau == 0, f"gain-only step left dynamics in the plant: {p}"
assert app.session.loop.history[-1].get("model") == "gain only"
print(f"[12] gain-only step OK: {p}")

# fit a one-pole model from the last measurement
app.model_var.set("one pole (1st order)")
app._update_model_fields()
app.do_fit()
pump_until_idle()
root.update()
assert app.ptau_var.get(), "one-pole fit did not fill tau"
print(f"[13] one-pole fit OK: gain {app.pgain_var.get()}, "
      f"tau {app.ptau_var.get()} us")

# Fit to FRF: the second-order rung fitted to the wide X1 probe comes out as
# two real poles (zeta > 1) a few kHz up, not the withdrawn 2.3 kHz / 0.21
# resonance; the one-pole rung lands in the 40-90 us the FRF phase implies
# (the ramp-record fit says 27). Then the FRF tab draws the fit over the tones.
app.model_var.set("second order (resonant)")
app._update_model_fields()
app.frf_var.set(FRF)
app.do_fit_frf(FRF, "resonant", 40e3)
pump_until_idle()
root.update()
_fn, _z = float(app.pfn_var.get()), float(app.pzeta_var.get())
assert 3e3 < _fn < 20e3 and _z > 1.0, f"resonant FRF fit gave fn {_fn}, zeta {_z}"
_g = float(app.pgain_var.get())
assert 0.5 < _g < 0.7, f"FRF-fit gain {_g} is not the DC gain of frf_WIDE_X1"
app.model_var.set("one pole (1st order)")
app._update_model_fields()
app.do_fit_frf(FRF, "one_pole", 40e3)
pump_until_idle()
root.update()
_tau = float(app.ptau_var.get())
assert 40 < _tau < 90, f"one-pole FRF fit gave tau {_tau} us"
app.do_fit_frf(FRF, "one_pole", 40e3, gain=0.5)
pump_until_idle()
root.update()
assert abs(float(app.pgain_var.get()) - 0.5) < 1e-6, "fixed gain not honoured"
app.model_var.set("second order (resonant)")
app._update_model_fields()
app.do_fit_frf(FRF, "resonant", 40e3)
pump_until_idle()
root.update()
print(f"[14] Fit to FRF: resonant fn {_fn:.0f} Hz zeta {_z:.2f} (two real "
      f"poles), one-pole tau {_tau:.1f} us, gain {_g:.4f}; FRF overlay drawn")

# The third-order rung: a second-order section TIMES a real pole, so all four
# boxes are live and the fit has to beat the second-order one on the same
# tones -- both in residual and in where the update stops contracting, which
# is the number that decides how high f_cut may go.
app.model_var.set("second order (resonant)")
app._update_model_fields()
assert app._param_entries["tau"].instate(["disabled"]), \
    "second order must grey tau -- it has no real pole"
app.model_var.set("third order (resonant + pole)")
app._update_model_fields()
# ttk carries state as flags, and cget('state') hands back a Tcl_Obj that
# never equals the string 'normal' -- instate is the API that answers.
assert not any(app._param_entries[k].instate(["disabled"])
               for k in ("gain", "fn", "zeta", "tau")), \
    "third order must leave gain, fn, zeta AND tau editable"
app.do_fit_frf(FRF, "third_order", 40e3)
pump_until_idle()
root.update()
_3 = {k: float(v.get()) for k, v in
      (("gain", app.pgain_var), ("fn", app.pfn_var),
       ("zeta", app.pzeta_var), ("tau", app.ptau_var))}
assert _3["tau"] > 0 and _3["fn"] > 0 and _3["zeta"] > 0, _3
_f, _H = ilc_gui.ilc._read_frf(FRF)
_r2 = ilc_gui.plantmod.fit_frf(_f, _H, model="resonant", f_hi=40e3)[1]
_r3 = ilc_gui.plantmod.fit_frf(_f, _H, model="third_order", f_hi=40e3)[1]
assert _r3["resid_phase_deg"] < _r2["resid_phase_deg"], (_r2, _r3)
_b2 = ilc_gui.plantmod.contraction(
    _f, _H, ilc_gui.plantmod.fit_frf(_f, _H, model="resonant", f_hi=40e3)[0], 0.6)[1]
_b3 = ilc_gui.plantmod.contraction(
    _f, _H, ilc_gui.plantmod.fit_frf(_f, _H, model="third_order", f_hi=40e3)[0], 0.6)[1]
assert _b2 is not None and (_b3 is None or _b3 > _b2), \
    f"third order did not push the contraction boundary out: {_b2} -> {_b3}"
# and it round-trips through a state file like any other rung
app.model_var.set("second order (resonant)")
app._update_model_fields()
print(f"[14b] third order: fn {_3['fn']:.0f} Hz zeta {_3['zeta']:.2f} tau "
      f"{_3['tau']:.1f} us; phase residual {_r2['resid_phase_deg']:.1f} -> "
      f"{_r3['resid_phase_deg']:.1f} deg, contraction boundary "
      f"{_b2/1e3:.0f} -> " + (f"{_b3/1e3:.0f} kHz" if _b3 else "off the band"))

# [14c] the PLATEAU log line: fires on the flat history from [2c] and names
# the best iteration's drive; stays silent on the converging one. The
# session's real history is put back afterwards.
import io, contextlib
_keep = list(app.session.loop.history)
app.session.loop.history = _flat
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    app._plateau_line()
assert "PLATEAU" in _buf.getvalue() and "_i08.csv" in _buf.getvalue(), _buf.getvalue()
app.session.loop.history = _conv
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    app._plateau_line()
assert _buf.getvalue() == "", _buf.getvalue()
app.session.loop.history = _keep
print("[14c] PLATEAU line fires on the flat history (naming drive_*_i08.csv), "
      "silent on the converging one")

# ---- GEN: truly from scratch, no priors anywhere ---------------------------
t_g, v_g = ilc_gui.build_target_waveform("ramp up-hold-return", 2.0,
                                         0.5, 2.0, 3.0, 2.0, 0.5, 2.0)
gen_target = os.path.join(SCRATCH, "target_GENX.csv")
ilc_gui.outputs.write_awg_csv(gen_target, t_g, v_g, comment="test target")
app.target_var.set(gen_target)
app.channel_var.set("GEN")
app._apply_channel_defaults()
app.stem_var.set("GENX")
app.model_var.set("gain only (0th order)")
app._update_model_fields()
for var in (app.pgain_var, app.ptau_var, app.pfn_var, app.pzeta_var):
    var.set("")
app.shotgain_var.set("")
app.do_init()           # must refuse: no gain typed, and no table to fall back on
assert not os.path.exists(os.path.join(ilc_gui.RUN_DIR,
                                       "drive_GENX.state.npz")), \
    "init built a GEN state without any model information"
assert app.session is None or app.session.stem != "GENX"
print("[15] GEN refused a blind init, as it must")

app.pgain_var.set("0.5")
app.do_init()
root.update()
s = app.session
assert s.channel == "GEN" and s.loop.plant.gain == 0.5
assert s.loop.plant.fn == 0 and s.loop.plant.tau == 0
assert s.loop.channel.mon_scale == 1.0
# GEN carries the open limit set: nothing but the generator rail binds, so a
# record that idles away from zero is not refused by the Trek's 100 mV cap
assert s.loop.limits is ilc_gui.CHANNELS["GEN"].limits
assert s.loop.limits.idle_awg >= 10.0 and not np.isfinite(s.loop.limits.hv_max)
_rep = s.loop.check(np.full(len(s.t), 3.0))
assert _rep.ok, f"GEN guard refused a 3 V idle record: {_rep}"
assert not s.loop.check(np.full(len(s.t), 11.0)).ok, "GEN guard ignored the rail"
print("[15b] GEN limits: open apart from the 10 V rail")
assert abs(np.ptp(s.loop.target) - 2.0) < 1e-9, "mon_scale leaked into GEN"
assert abs(np.abs(s.u).max() - 4.0) < 0.2, \
    f"first shot should be ~target/gain = 4 V, got {np.abs(s.u).max():.2f}"
# flat conversion means EXACTLY proportional to the target (ends clamped)
assert np.allclose(s.u[1:-1], (s.loop.target / 0.5)[1:-1]), \
    "first shot is not a flat conversion -- pre-distortion leaked in"
print(f"[16] GEN init from scratch: {s.loop.plant}, "
      f"drive peak {np.abs(s.u).max():.2f} V, flat conversion confirmed")

# the 'real' chain is gain 0.4 (model guessed 0.5) -- two synthetic
# iterations must converge, with metrics in OUTPUT units (scale 1, not x1000)
cap1 = os.path.join(SCRATCH, "genx_cap1_001.csv")
pd.DataFrame({"Time (s)": s.t, "CH1": 0.4 * s.u}).to_csv(cap1, index=False)
app.meas_var.set(os.path.join(SCRATCH, "genx_cap1*.csv"))
app.do_step(); pump_until_idle()
m1 = s.snapshots[-1]["m"]
assert 0.2 < m1["peak_err_hv"] < 0.6, \
    f"expected ~0.4 V peak error in output units, got {m1['peak_err_hv']:.3f}"
cap2 = os.path.join(SCRATCH, "genx_cap2_001.csv")
pd.DataFrame({"Time (s)": s.t, "CH1": 0.4 * s.u}).to_csv(cap2, index=False)
app.meas_var.set(os.path.join(SCRATCH, "genx_cap2*.csv"))
app.do_step(); pump_until_idle()
m2 = s.snapshots[-1]["m"]
assert m2["peak_err_hv"] < 0.6 * m1["peak_err_hv"], \
    f"no convergence: {m1['peak_err_hv']:.3f} -> {m2['peak_err_hv']:.3f}"
print(f"[17] GEN gain-only loop converges: peak err "
      f"{m1['peak_err_hv']:.3f} -> {m2['peak_err_hv']:.3f} V")

# drive-updates tab: u_1 - u_0 for the two GEN steps just taken
app.itersel_var.set("all")
app._redraw_iterations(); root.update()
assert len(app.ax_ddel.get_lines()) >= 2, "drive-updates tab drew nothing"
app.itersel_var.set("")
app._redraw_iterations()
print("[25] drive-updates tab drew u_k - u_(k-1) for the GEN steps")

# hold runs: fabricate two in-session runs of iter 1 plus one on disk
import time as _t2
base = app._snaps_by_it()[1]
for r in (1, 2):
    app.session.snapshots.append(dict(
        it=1, y=base["y"] * (1 + 0.01 * r), m=app.session.loop.metrics(base["y"]),
        u=base["u"], run=r, t_wall=_t2.time() + 60 * r))
np.save(os.path.join(ilc_gui.RUN_DIR, "meas_GENX_i01_r03.npy"), base["y"])
app.itersel_var.set("all")
app.showruns_var.set(True)
app.dtlabels_var.set(True)
app._redraw_iterations(); root.update()
labels = [l.get_label() for l in app.ax_err.get_lines()
          if l.get_label().startswith("iter")]
assert any("r1" in l for l in labels) and any("r2" in l for l in labels), labels
assert any("+" in l for l in labels), f"no dt suffix in {labels}"
assert any(l.get_linestyle() == "--" for l in app.ax_err.get_lines()), \
    "runs are not dashed"
dcor_labels = [l.get_label() for l in app.ax_dcor.get_lines()
               if l.get_label().startswith("iter")]
assert not any(" r" in l for l in dcor_labels), \
    f"runs leaked into the drive-side tab: {dcor_labels}"
app.showruns_var.set(False)
app._redraw_iterations(); root.update()
assert not any("r1" in l.get_label() for l in app.ax_err.get_lines()), \
    "runs still shown while the checkbox is off"
app.showruns_var.set(True)
app.dtlabels_var.set(False)
print("[26] hold-run display: dashed traces, r-labels, dt suffixes, "
      "toggles, drive-side exclusion all OK")

# recall of the on-disk run file with an mtime timestamp
app.state_var.set(os.path.join(ilc_gui.RUN_DIR, "drive_GENX.state.npz"))
app.do_load(); root.update()
rsnaps = [sn for sn in app.session.snapshots if sn.get("run") is not None]
assert any(sn["it"] == 1 and sn["run"] == 3 and sn.get("t_wall")
           for sn in rsnaps), rsnaps
print("[27] recalled meas_GENX_i01_r03.npy as iter 1 r3 with a timestamp")

# [27b] Hold of an EARLIER iteration: the 'iter' box resolves to that
# iteration's stored drive beside the state (so a past drive can be
# re-measured without Init restarting the campaign), blank means the
# current drive, and a missing iteration or a non-number is refused.
_hs = app.session; _hd = os.path.dirname(_hs.state_path)
_stored = sorted(int(mm.group(1)) for pth in glob.glob(os.path.join(_hd, f"drive_{_hs.stem}_i[0-9]*.csv"))
                 if (mm := re.search(r"_i(\d+)\.csv$", pth)))
assert _stored, f"no stored drives for {_hs.stem} in {_hd}"
_k = _stored[0]
_it, _u, _src = app._hold_drive(str(_k))
assert _it == _k and len(_u) == len(_hs.t) and _src == f"drive_{_hs.stem}_i{_k:02d}.csv", (_it, _src)
assert np.allclose(_u, pd.read_csv(os.path.join(_hd, _src), comment="#").iloc[:, 1].to_numpy(float))
assert app._hold_drive("") == (None, None, "the current drive")
for bad in ("99", "x"):
    try:
        app._hold_drive(bad); raise AssertionError(f"{bad!r} must be refused")
    except RuntimeError:
        pass
print(f"[27b] hold iter: '{_k}' -> {_src} ({len(_u)} pts) tagged to iteration {_it}; blank -> current; 99 and 'x' refused")

# [27c] a hold of an iteration outside the selection, or with 'runs' off,
# was stored and listed but never drawn. _show_held ticks runs, adds the
# iteration to the box, logs it, and redraws -- the recalled GENX iter-1
# hold run has to be on the Error tab afterwards.
_hs = app.session
_hr = [sn for sn in _hs.snapshots if sn.get("run") is not None]
assert _hr, "this session has no hold runs to test with"
_hit = _hr[-1]["it"]; _lab = f"iter {_hit} r{_hr[-1]['run']}"
app.showruns_var.set(False); app.itersel_var.set("99")
app._redraw_iterations(); root.update()
assert not any(l.get_label() == _lab for l in app.ax_err.get_lines()), "run drawn while hidden?"
_before = app.log_text.get("1.0", "end")
app._show_held(_hit); root.update()
assert app.showruns_var.get() and str(_hit) in app.itersel_var.get(), (app.showruns_var.get(), app.itersel_var.get())
assert any(l.get_label() == _lab for l in app.ax_err.get_lines()), [l.get_label() for l in app.ax_err.get_lines()]
assert "so the held runs of iteration" in app.log_text.get("1.0", "end")[len(_before):]
app._show_held(_hit)                                            # already visible: nothing to say
assert app.log_text.get("1.0", "end").count("so the held runs of iteration") == 1
app.itersel_var.set(""); app.showruns_var.set(True); app._redraw_iterations(); root.update()
print(f"[27c] show_held: '{_lab}' hidden by selection 99 + runs off -> ticked runs, box '{app.itersel_var.get() or _hit}', drawn, logged once")

# [27d] the model-check log line, from the panel, on two snapshots built
# here so the answer is known: iteration 91's drive differs from 90's by a
# 3 kHz + 25 kHz update, and the monitor answers at exactly 2x the model in
# every band (contracts at gamma 0.6: lam 0.2, no flag) or at 4x (lam 1.4:
# 'does not contract ... overshoots'). Restored afterwards.
import io, contextlib
_ms = app.session; _g_keep = _ms.loop.gamma; _ms.loop.gamma = 0.6; _keep = list(_ms.snapshots)
_tt = _ms.t; _du = (0.02 * np.sin(2 * np.pi * 3e3 * _tt)
                    + 0.01 * np.sin(2 * np.pi * 25e3 * _tt) * np.exp(-((_tt - _tt[len(_tt)//3]) / 0.3e-3) ** 2))
_fwd = lambda d: _ms.loop.plant.forward(d) - _ms.loop.plant.offset
_u90 = np.zeros_like(_tt); _y90 = np.zeros_like(_tt)
_ms.snapshots.append(dict(it=90, y=_y90, m={}, u=_u90, t_wall=0.0))
for _k, _flag in ((2.0, False), (4.0, True)):
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        _mc = app._model_check_line(91, _u90 + _du, _y90 + _k * _fwd(_du))
    _out = _buf.getvalue()
    assert "model check: the chain answered the last update" in _out, _out
    assert abs(_mc["model_ratio_worst"] - _k) < 0.15 * _k, (_k, _mc)
    assert ("does not contract" in _out and "overshoots" in _out) == _flag, (_k, _out)
assert app._model_check_line(90, _u90, _y90) == {}                 # nothing before it
_ms.snapshots[:] = _keep; _ms.loop.gamma = _g_keep
print("[27d] model-check line: a 2x chain reported (lam 0.2, no flag), a 4x chain flagged 'does not contract ... overshoots'; nothing before the first snapshot")

# Fit must recover the true gain from the snapshot's own played (u, y) pair
app.do_fit(); pump_until_idle(); root.update()
g_fit = float(app.pgain_var.get())
assert abs(g_fit - 0.4) < 0.02, f"fit returned {g_fit}, true gain is 0.4"
print(f"[18] fit from the played pair recovers gain {g_fit:.4f} (true 0.4)")

# first-shot gain must be separate from the model gain
app.stem_var.set("SEPX")
app.shotgain_var.set("0.8")
app.pgain_var.set("0.5")
app.do_init()
root.update()
s2 = app.session
assert abs(np.abs(s2.u).max() - 2.5) < 1e-6, \
    f"first shot should be target/0.8 = 2.5 V peak, got {np.abs(s2.u).max()}"
assert s2.loop.plant.gain == 0.5, "model gain was overwritten by the shot gain"
print(f"[19] first-shot gain 0.8 vs model gain 0.5 kept separate: "
      f"drive peak {np.abs(s2.u).max():.3f} V, plant {s2.loop.plant}")

# target preview draws with no session requirement and sends nothing
app.do_preview_target()
root.update()
print("[20] target preview drawn")

# auto-reload on relaunch: a fresh App must restore the remembered session
import time as _t
root3 = tk.Tk(); root3.withdraw()
app3 = ilc_gui.App(root3)
t0 = _t.time()
while app3.session is None and _t.time() - t0 < 5:
    root3.update(); _t.sleep(0.02)
assert app3.session is not None, "auto-reload did not restore a session"
print(f"[23] auto-reload restored '{app3.session.stem}' at iteration "
      f"{app3.session.iteration} with {len(app3.session.snapshots)} "
      f"snapshot(s)")
root3.destroy()

# compare overlays: another stem's results ride the same plots. The GENX
# and TSTX1 campaigns live in RUN_DIR, so put MKJX1 beside them (the real
# layout -- every campaign shares run/) and load it as the active session.
for f in [os.path.join(SCRATCH, "drive_MKJX1.state.npz")] + \
         glob.glob(os.path.join(SCRATCH, "meas_MKJX1_i*.npy")):
    shutil.copy(f, ilc_gui.RUN_DIR)
app.state_var.set(os.path.join(ilc_gui.RUN_DIR, "drive_MKJX1.state.npz"))
app.do_load(); root.update()
app.itersel_var.set("")
app.cmpsel_var.set("GENX:all NOPE TSTX1")
app._redraw_iterations(); root.update()
labs = [l.get_label() for l in app.ax_err.get_lines()]
assert any(l == "GENX iter 0" for l in labs), labs
assert any(l == "GENX iter 1" for l in labs), labs
assert any(l == "GENX iter 1 r3" for l in labs), \
    f"compare hold run missing: {labs}"      # runs box is on
assert any(l.startswith("iter ") for l in labs), \
    f"active session's own traces vanished: {labs}"
# GENX draws on its OWN time grid, not the active session's
gline = next(l for l in app.ax_err.get_lines()
             if l.get_label() == "GENX iter 1")
gsess = app._cmp_cache[os.path.join(
    os.path.dirname(app.session.state_path), "drive_GENX.state.npz")][1]
assert len(gline.get_xdata()) == len(gsess.t), \
    "compare overlay is not on GENX's own grid"
conv_labs = [l.get_label() for l in app.ax_conv.get_lines()]
assert any(l == "GENX peak error" for l in conv_labs), conv_labs
assert any(l == "GENX rms error" for l in conv_labs), conv_labs
panel = app.log_text.get("1.0", "end")
assert "no state for 'NOPE'" in panel, "missing stem not reported"
assert "'TSTX1' has no stored measurements" in panel, \
    "measurement-less stem not reported"
app._redraw_iterations(); root.update()      # same spec again
panel2 = app.log_text.get("1.0", "end")
assert panel2.count("no state for 'NOPE'") == 1, \
    "compare warnings repeat on every redraw"
app.cmpsel_var.set("")
app._redraw_iterations(); root.update()
assert not any(l.get_label().startswith("GENX")
               for l in app.ax_err.get_lines()), \
    "clearing the Compare box did not remove the overlays"
print("[30] compare overlays: GENX rode the MKJX1 plots on its own grid "
      "(runs included), missing/empty stems reported once, box clears")

# the Compare selection takes the Iterations grammar per stem
app.cmpsel_var.set("GENX")                   # blank sel = last iter only
app._redraw_iterations(); root.update()
glabs = [l.get_label() for l in app.ax_err.get_lines()
         if l.get_label().startswith("GENX iter") and " r" not in l.get_label()]
assert glabs == ["GENX iter 1"], glabs
app.cmpsel_var.set("GENX:0")
app._redraw_iterations(); root.update()
glabs = [l.get_label() for l in app.ax_err.get_lines()
         if l.get_label().startswith("GENX")]
assert glabs == ["GENX iter 0"], glabs
app.cmpsel_var.set("GENX:all")               # left set for the screenshots
app._redraw_iterations(); root.update()
print("[31] compare grammar: blank = last iter, 'GENX:0' picks iteration 0")

# the picker: campaigns from OUTSIDE the run folder, which no stem can name.
# SCRATCH holds the original drive_MKJX1.state.npz -- same stem as the loaded
# session, a different file, which is exactly the collision the keying is for.
_picked = []
ilc_gui.filedialog.askopenfilenames = lambda **k: tuple(_picked)
app.cmpsel_var.set("")
app._cmp_paths.clear()
_picked[:] = [os.path.join(SCRATCH, "drive_MKJX1.state.npz")]
app.do_compare_add(); root.update()
key = app.cmpsel_var.get().strip()
assert key.startswith("MKJX1@"), (
    "an archived campaign of the loaded stem was not given its own key: "
    + repr(key))
assert app._cmp_paths.get(key) == os.path.abspath(_picked[0]), app._cmp_paths
elabs = [l.get_label() for l in app.ax_err.get_lines()]
assert any(l.startswith(key) for l in elabs), (key, elabs)
assert key in app.cmp_status.cget("text"), app.cmp_status.cget("text")
assert str(app.cmp_clear_btn.cget("state")) == "normal"
# picking the loaded session itself is refused, not drawn against itself
_picked[:] = [app.session.state_path]
app.do_compare_add(); root.update()
assert app.cmpsel_var.get().strip() == key, app.cmpsel_var.get()
assert "is the loaded session" in app.log_text.get("1.0", "end")
# a run-folder pick keeps its bare stem and writes no path to remember
_picked[:] = [os.path.join(ilc_gui.RUN_DIR, "drive_GENX.state.npz")]
app.do_compare_add(); root.update()
assert "GENX" in app.cmpsel_var.get().split(), app.cmpsel_var.get()
assert "GENX" not in app._cmp_paths, app._cmp_paths
assert "2 campaign(s)" in app.cmp_status.cget("text"), (
    app.cmp_status.cget("text"))
app.do_compare_clear(); root.update()
assert app.cmpsel_var.get() == "" and not app._cmp_paths and not app._cmp_cache
assert not any(l.get_label().startswith("MKJX1@")
               for l in app.ax_err.get_lines()), "Clear left the overlays up"
assert str(app.cmp_clear_btn.cget("state")) == "disabled"
assert "nothing loaded" in app.cmp_status.cget("text"), (
    app.cmp_status.cget("text"))
print("[51] compare picker: an out-of-folder campaign of the SAME stem got "
      "its own key and drew, the loaded session was refused, a run-folder "
      "pick stayed a bare stem, Clear emptied box, map, cache and plots")

# a stem typed one letter wrong says so on the status line, not only in the log
app.cmpsel_var.set("GENX NOPE")
app._redraw_iterations(); root.update()
txt = app.cmp_status.cget("text")
assert "1 campaign(s)" in txt and "not shown" in txt, txt
app.cmpsel_var.set("NOPE")
app._redraw_iterations(); root.update()
assert "none resolved" in app.cmp_status.cget("text"), (
    app.cmp_status.cget("text"))
app.cmpsel_var.set("GENX:all")               # back to the screenshot state
app._redraw_iterations(); root.update()
# a typed key is described like a picked one, with the NOTE that says the
# metrics are not commensurable -- and said once, however often the plots
# redraw (Clear forgets, so only the redraws since the last one are counted)
said = app.log_text.get("1.0", "end").count("NOTE: GENX is GEN")
assert said >= 1, "a GEN campaign under an EO1 session was not flagged"
assert "stored iteration(s)" in app.log_text.get("1.0", "end"), \
    "no campaign was described"
app._redraw_iterations(); root.update()
app._redraw_iterations(); root.update()
assert app.log_text.get("1.0", "end").count("NOTE: GENX is GEN") == said, \
    "the compare description repeats on every redraw"
print("[52] compare status line: names what resolved, counts what did not, "
      "says so when nothing did, and each campaign is spelled out once "
      "(channel/grid NOTE included)")

# a path entry shows the END of what it holds -- the file name, not the drive
app.state_var.set(os.path.join(SCRATCH, "a" * 120, "drive_MKJX1.state.npz"))
root.update_idletasks(); root.update()
first, last = app.state_entry.xview()
assert last >= 0.999 and first > 0, (
    "the state entry is showing the head of an overflowing path: "
    + repr((first, last)))
app.state_var.set(app.session.state_path)
root.update_idletasks(); root.update()
print("[53] path entries scroll to the tail: a path too long for its box "
      "shows the file name, not the drive letter")

# drive-corrections spectrum: same shape as the error spectrum, base
# measurements only, each stem against its own reference drive
app.itersel_var.set("all")            # so the drive-less 5/6 are selected too
app._redraw_iterations(); root.update()
labs = [l.get_label() for l in app.ax_dspec.get_lines()]
assert any(l.startswith("iter ") for l in labs), labs
# MKJX1 has measurements for 5-8 but drives only from 7 on, so 5 and 6 are
# the real drive-less case: selected, and still not drawn
assert "iter 7" in labs and "iter 8" in labs, labs
assert "iter 5" not in labs and "iter 6" not in labs, \
    f"iterations with no stored drive were drawn: {labs}"
assert "GENX iter 1" in labs, labs           # drive_GENX_i01.csv exists
# ... and so does drive_GENX_i00.csv. Init used to write iteration 0 as
# drive_<stem>_iter0.csv, a name recall_snapshots never looked for, so the
# init drive was invisible after a reload and this line asserted its
# absence. Init now writes the same _iNN name every later iteration uses.
assert "GENX iter 0" in labs, labs
assert not any(" r" in l for l in labs), \
    f"hold runs leaked into the drive spectrum: {labs}"
app.itersel_var.set("")                      # back to the default
app._redraw_iterations(); root.update()
print("[32] drive spectrum drew the stored corrections, runs and "
      "drive-less iterations excluded")

# compare gradient is a lightness ramp (distinct colours), not alpha
g0 = next(l for l in app.ax_err.get_lines() if l.get_label() == "GENX iter 0")
g1 = next(l for l in app.ax_err.get_lines() if l.get_label() == "GENX iter 1")
assert g0.get_color() != g1.get_color(), "compare gradient collapsed"
assert g0.get_alpha() in (None, 1.0), "alpha ramp is back"

# Table tab: one row per stored iteration and run, ignoring the
# Iterations box; CSV save mirrors it exactly
rows = [app.table.item(i, "values") for i in app.table.get_children()]
assert {r[0] for r in rows} == {"MKJX1", "GENX"}, rows
mk = [r for r in rows if r[0] == "MKJX1"]
gx = [r for r in rows if r[0] == "GENX"]
assert len(mk) == 9, [r[:3] for r in mk]     # history 0..8, no runs
assert len(gx) == 3 and gx[2][2] == "r3", gx
assert any(r[3] for r in mk), "model tags missing from the table"
# the model column names the model that BUILT the row's drive: iteration 0
# is Init's flat first shot (blank), and a hold run inherits its base
# iteration's label since it replays the same drive
assert mk[0][1] == "0" and mk[0][3] == "", \
    f"iteration 0 must stay unlabeled (flat Init shot): {mk[0]}"
assert gx[0][3] == "" and gx[1][3] == "gain only" == gx[2][3], \
    f"drive-model column misaligned on GENX rows: {[r[:4] for r in gx]}"
assert mk[-1][4] and mk[-1][9], \
    f"newest MKJX1 row lacks metrics or a timestamp: {mk[-1]}"
tcsv = os.path.join(SCRATCH, "iterations_test.csv")
app._save_table_csv(tcsv)
tdf = pd.read_csv(tcsv)
assert len(tdf) == len(rows) == 12, (len(tdf), len(rows))
assert "peak err (V)" in tdf.columns, list(tdf.columns)
print(f"[33] table: {len(mk)} MKJX1 + {len(gx)} GENX rows (r3 included), "
      f"CSV mirror saved; compare gradient is colour, not alpha")

# spectrum averaging: the coherent-gain normalisation holds a tone's
# amplitude across both modes, and the Welch mode actually smooths. The
# tolerances are loose on purpose -- 10 kHz lands 0.24 of a bin off centre
# in the k=8 segment, and Hann scalloping costs a few percent there (half a
# bin off it would cost 15%). Only tones on integer bins OF THE SEGMENT
# read exactly; see avg_spectrum's docstring.
tt = np.arange(4096) * 2e-6
tone = 0.5 * np.sin(2 * np.pi * 10e3 * tt)
noisy = tone + np.random.default_rng(0).normal(0, 0.05, len(tt))
f1, a1 = ilc_gui.avg_spectrum(tone, 2e-6, 1)
f8, a8 = ilc_gui.avg_spectrum(tone, 2e-6, 8)
assert abs(a1.max() - 0.5) < 0.02, f"raw tone amplitude {a1.max():.3f}"
assert abs(a8.max() - 0.5) < 0.03, f"averaged tone amplitude {a8.max():.3f}"
assert len(f8) < len(f1) // 4, "averaging did not coarsen the grid"
_, n1 = ilc_gui.avg_spectrum(noisy, 2e-6, 1)
_, n8 = ilc_gui.avg_spectrum(noisy, 2e-6, 8)
floor1 = np.std(n1[len(n1) // 2:]) / np.mean(n1[len(n1) // 2:])
floor8 = np.std(n8[len(n8) // 2:]) / np.mean(n8[len(n8) // 2:])
assert floor8 < 0.6 * floor1, \
    f"noise-floor scatter did not drop: {floor1:.2f} -> {floor8:.2f}"
# GUI wiring: both spectrum tabs follow the box, junk warns once and
# falls back to raw
raw_len = len(app.ax_spec.get_lines()[0].get_xdata())
app.specavg_var.set("8")
app._redraw_iterations(); root.update()
assert len(app.ax_spec.get_lines()[0].get_xdata()) < raw_len // 4
assert "8-segment average" in app.ax_spec.get_title()
assert "8-segment average" in app.ax_dspec.get_title()
app.specavg_var.set("junk")
app._redraw_iterations(); root.update()
assert len(app.ax_spec.get_lines()[0].get_xdata()) == raw_len, \
    "junk avg did not fall back to the raw FFT"
panel3 = app.log_text.get("1.0", "end")
assert panel3.count("spectra avg 'junk'") == 1, "avg warning repeats"
app._redraw_iterations(); root.update()
assert app.log_text.get("1.0", "end").count("spectra avg 'junk'") == 1
app.specavg_var.set("")
app._redraw_iterations(); root.update()
print("[34] spectra averaging: tone amplitude invariant, noise scatter "
      "drops, both tabs follow the box, junk warns once -> raw")

# native-rate spectrum: captures at the scope's own dt reveal content past
# the grid Nyquist that the boxcar-decimated path cannot represent
sN = app.session
tf = np.arange(sN.t[0] - 0.5e-3, sN.t[-1] + 0.5e-3, sN.loop.dt / 4)
tone_mon = 2e-3
resp = np.interp(tf, sN.t, sN.loop.target)      # a perfect chain response
fine = resp + tone_mon * np.sin(2 * np.pi * 400e3 * tf)   # 400 kHz > 250 kHz
for j in (1, 2):
    pd.DataFrame({"Time (s)": tf, "CH3": fine}).to_csv(
        os.path.join(SCRATCH, f"fine_cap_{j:03d}.csv"), index=False)
app.meas_var.set(os.path.join(SCRATCH, "fine_cap_*.csv"))
app.do_native_spec(); pump_until_idle(); root.update()
nat = next(l for l in app.ax_spec.get_lines()
           if l.get_label().startswith("native rate"))
fx, ay = nat.get_xdata(), nat.get_ydata()
assert fx.max() > 1.5 * 250e3, f"band did not extend: {fx.max():.0f} Hz"
sel = np.abs(fx - 400e3) < 2e3
tone_hv = tone_mon * sN.loop.channel.mon_scale
assert ay[sel].max() > 0.5 * tone_hv, \
    f"400 kHz tone not recovered: {ay[sel].max():.3f} vs {tone_hv:.3f} HV V"
assert any(l.get_label().startswith("iter ")
           for l in app.ax_spec.get_lines()), "overlay clobbered the tab"
# the guard still refuses a zoomed capture on this path
app.meas_var.set(os.path.join(SCRATCH, "fake_zoom_*.csv"))
app.do_native_spec(); pump_until_idle(); root.update()
assert "zoomed or mismatched" in app.log_text.get("1.0", "end"), \
    "native-spectrum span guard did not fire"
print(f"[35] native-rate spectrum: Nyquist {fx.max()/1e3:.0f} kHz, the "
      f"400 kHz tone recovered at {ay[sel].max():.2f} V (true {tone_hv:.1f}), "
      f"span guard held")

# bench capture can keep the native-rate average, and the spectrum button
# reads the kept npz (the instrument paths themselves stay bench-only)
tf2 = np.arange(sN.t[0] - 0.5e-3, sN.t[-1] + 0.5e-3, sN.loop.dt / 4)
fine2 = (np.interp(tf2, sN.t, sN.loop.target)
         + 1e-3 * np.sin(2 * np.pi * 350e3 * tf2))

class _FakeScope:
    def single(self, wait_s=None): return True
    def waveform(self, ch, **kw): return tf2 + sN.t_off, fine2
    def run(self): pass

natfile = os.path.join(SCRATCH, "meas_MKJX1_i99_native.npz")
app.stop_evt.clear()
y_grid = app._bench_capture(_FakeScope(), 3, sN.t, sN.t_off, repeats=4,
                            wait_s=1, settle=0, native_path=natfile)
assert len(y_grid) == len(sN.t), "grid average is off the waveform grid"
d = np.load(natfile)
assert len(d["t"]) == len(tf2) and np.allclose(d["y"], fine2), \
    "kept native average does not match what the scope returned"
assert np.allclose(d["t"], tf2), "native t is not in waveform time"
app._redraw_iterations(); root.update()      # clear the [35] overlay
app.meas_var.set(natfile)
app.do_native_spec(); pump_until_idle(); root.update()
nat2 = next(l for l in app.ax_spec.get_lines()
            if l.get_label().startswith("native rate"))
fx2, ay2 = nat2.get_xdata(), nat2.get_ydata()
sel2b = np.abs(fx2 - 350e3) < 2e3
assert ay2[sel2b].max() > 0.5 * 1e-3 * sN.loop.channel.mon_scale, \
    "350 kHz tone lost through the kept-native round trip"
print("[36] bench keep-native: npz saved in waveform time, spectrum "
      "button reads it, 350 kHz tone survives the round trip")

# FRF probe builder: adjustable band on the session's own grid, hard
# ceiling at the grid Nyquist, leak-free integer bins, tapered ends
nG, dtG = len(sN.t), sN.loop.dt
recG = nG * dtG
u_p, bins = ilc_gui.build_frf_probe(nG, dtG, 2.0, 400.0, 200e3, 96)
assert bins[-1] / recG > 150e3, f"band stopped at {bins[-1]/recG:.0f} Hz"
assert np.all(bins == bins.astype(int)) and len(set(bins)) == len(bins)
assert abs(np.abs(u_p).max() - 2.0) < 1e-9, "probe peak off"
assert abs(u_p[0]) < 1e-12 and abs(u_p[-1]) < 1e-12, "probe ends not tapered"
for bad in ((260e3, "past Nyquist"), (0.98 * 0.5 / dtG * 1.01, "at ceiling")):
    try:
        ilc_gui.build_frf_probe(nG, dtG, 2.0, 400.0, bad[0], 48)
        raise AssertionError(f"f hi {bad[1]} was not refused")
    except RuntimeError:
        pass
# the grid planner: the 2 us grid is the CARD's constraint, not the bench's
mode, np_, dtp = ilc_gui.plan_frf_grid(nG, dtG, 100e3)
assert mode == "session" and (np_, dtp) == (nG, dtG), (mode, np_, dtp)
mode, np_, dtp = ilc_gui.plan_frf_grid(nG, dtG, 300e3)
assert mode == "dense" and np_ <= ilc_gui.AWG_MAX_PTS, (mode, np_)
assert abs(np_ * dtp - nG * dtG) < 1e-12, "dense mode changed the duration"
assert 300e3 <= ilc_gui.PROBE_NYQ_MARGIN * 0.5 / dtp + 1,     f"dense dt {dtp} leaves less than the margin at 300 kHz"
mode, np_, dtp = ilc_gui.plan_frf_grid(nG, dtG, 2e6)
assert mode == "short" and np_ == ilc_gui.AWG_MAX_PTS, (mode, np_)
assert np_ * dtp < nG * dtG, "short mode did not shorten the record"
try:
    ilc_gui.plan_frf_grid(nG, dtG, 6e6)
    raise AssertionError("6 MHz was not refused")
except RuntimeError:
    pass
# past the arb memory the record shortens, and the margin holds:
# tones to 500 kHz keep >= 4 samples per period on the probe grid
mode, npD, dtD = ilc_gui.plan_frf_grid(nG, dtG, 500e3)
assert mode == "short" and npD == ilc_gui.AWG_MAX_PTS, (mode, npD)
u_d, bins_d = ilc_gui.build_frf_probe(npD, dtD, 2.0, 400.0, 500e3, 96)
recD = npD * dtD
assert recD < nG * dtG, "short mode kept the full record"
assert bins_d[-1] / recD > 400e3, f"band stopped at {bins_d[-1]/recD:.0f}"
assert bins_d[-1] / recD <= ilc_gui.PROBE_NYQ_MARGIN * 0.5 / dtD + 1,     "top tone past the Nyquist margin"
assert abs(u_d[0]) < 1e-12 and abs(u_d[-1]) < 1e-12
# the demand gate's numbers: a hot high-band probe exceeds the 610E specs
# on the flat-gain measure (-> confirmation dialog); a small probe passes
slw, ipk, hvpk = ilc_gui.probe_demand(u_d, dtD, sN.loop.plant.gain,
                                      sN.loop.channel)
assert ipk > ilc_gui.LIMITS.current and slw > ilc_gui.LIMITS.slew_hv, \
    f"2 V @ 500 kHz should exceed the demand specs: {slw/1e6:.1f} V/us"
assert hvpk < ilc_gui.LIMITS.hv_max, "2 V probe cannot near 6 kV"
u_small, b_small = ilc_gui.build_frf_probe(nG, dtG, 0.05, 400.0, 24e3, 48)
slw2, ipk2, _ = ilc_gui.probe_demand(u_small, dtG, sN.loop.plant.gain,
                                     sN.loop.channel)
assert ipk2 < ilc_gui.LIMITS.current and slw2 < ilc_gui.LIMITS.slew_hv, \
    f"a 50 mV probe should pass clean: {ipk2*1e3:.2f} mA"
print(f"[37] FRF grids: session to "
      f"{ilc_gui.PROBE_NYQ_MARGIN*0.5/dtG/1e3:.0f} kHz, dense above, short "
      f"past the arb memory ({npD} pts, {recD*1e3:.2f} ms record to "
      f"{bins_d[-1]/recD/1e3:.0f} kHz at >= 4 samples/period), 6 MHz "
      f"refused; demand gate maths: 2 V @ 500 kHz asks "
      f"{ipk*1e3:.0f} mA (confirm), 50 mV @ 24 kHz {ipk2*1e3:.2f} mA (clean)")

# FRF measurement maths: a fake scope plays the probe through a known
# one-pole plant; the fitted H must match the analytic transfer
tau_p = 2.2e-6
frG = np.fft.rfftfreq(nG, dtG)
Htrue = 1.0 / (1.0 + 2j * np.pi * frG * tau_p)
y_p = np.fft.irfft(np.fft.rfft(u_p) * Htrue, n=nG)

class _FrfScope:
    def single(self, wait_s=None): return True
    def waveform(self, ch, **kw):
        return (sN.t + sN.t_off, u_p if ch == 1 else y_p)
    def run(self): pass

app.stop_evt.clear()
_res = app._frf_capture(_FrfScope(), 1, 3, bins, sN.t, sN.t_off,
                        repeats=3, wait_s=1, settle=0)
H_m, coh_m = _res["mon"]
assert _res["aux"] is None, "no aux channel was asked for"
assert np.allclose(H_m, Htrue[bins], rtol=1e-6), \
    f"fitted H off by {np.abs(H_m - Htrue[bins]).max():.2e}"
assert np.all(coh_m > 0.999), "identical shots must cohere"
fpath = os.path.join(ilc_gui.RUN_DIR, "frf_AUTOTST.csv")
ilc_gui.write_frf_csv(fpath, bins / recG, H_m, coh_m)
app.fuse_var.set("100e3"); app.fmax_var.set("150e3")
app._adopt_frf(fpath); root.update()
assert app.frf_var.get() == fpath, "FRF field not pointed at the result"
frf_obj = ilc_gui.ilc.FRF(fpath, f_use=100e3, f_max=150e3)
# the same maths on the finer grid: tones past the session's 250 kHz
# Nyquist come back exactly, and a too-coarse scope record is refused
tD = np.arange(npD) * dtD
frD = np.fft.rfftfreq(npD, dtD)
yD = np.fft.irfft(np.fft.rfft(u_d) / (1 + 2j * np.pi * frD * tau_p), n=npD)

class _DenseScope:
    def single(self, wait_s=None): return True
    def waveform(self, ch, **kw):
        return (tD + sN.t_off, u_d if ch == 1 else yD)
    def run(self): pass

class _CoarseScope(_DenseScope):     # a 2 us record cannot carry 500 kHz
    def waveform(self, ch, **kw):
        return (sN.t + sN.t_off, np.interp(sN.t, tD, u_d if ch == 1 else yD))

HD, cohD = app._frf_capture(_DenseScope(), 1, 3, bins_d, tD, sN.t_off,
                            repeats=2, wait_s=1, settle=0,
                            f_top=bins_d[-1] / recD)["mon"]
HtrueD = 1.0 / (1.0 + 2j * np.pi * (bins_d / recD) * tau_p)
assert np.allclose(HD, HtrueD, rtol=1e-6), \
    f"dense-grid H off by {np.abs(HD - HtrueD).max():.2e}"
try:
    app._frf_capture(_CoarseScope(), 1, 3, bins_d, tD, sN.t_off,
                     repeats=2, wait_s=1, settle=0, f_top=bins_d[-1] / recD)
    raise AssertionError("coarse scope record was not refused")
except RuntimeError as e:
    assert "too coarse" in str(e), e
print(f"[38] FRF maths: one-pole plant recovered exactly on the session "
      f"grid AND at {bins_d[-1]/recD/1e3:.0f} kHz on the shortened fine "
      f"grid; coarse scope record refused; CSV loads as an ilc.FRF")

# [38b] the ramped probe: a multitone riding a held level, tones on integer
# bins of the HOLD, analysed over the hold alone. The record must be the
# session's own (so nothing on the bench is retuned), start and end at zero,
# hold what was asked, and -- the point of it -- recover a plant that is only
# correct over the hold, which a whole-record analysis would smear.
_nS, _dtS = len(sN.t), float(sN.loop.dt)
_u_r, _bins_r, _win = ilc_gui.build_ramped_probe(_nS, _dtS, 0.5, 8.0, 4.0,
                                                 2e3, 60e3, 48, awg_rail=10.0)
_i0, _i1 = _win
_recR = (_i1 - _i0) * _dtS
assert len(_u_r) == _nS, (len(_u_r), _nS)
assert abs(_u_r[0]) < 0.1 and abs(_u_r[-1]) < 0.1, "record must idle at zero"
assert abs(np.median(_u_r[_i0:_i1]) - 8.0) < 1e-3, np.median(_u_r[_i0:_i1])
assert np.abs(_u_r).max() <= 8.5 + 1e-9, np.abs(_u_r).max()
assert len(_bins_r) >= 8 and _bins_r[-1] / _recR <= 60e3 + 1
# a plant that acts ONLY during the hold: outside it the trace is the drive
# itself, so a whole-record analysis would read H ~ 1 and the windowed one
# must not
_tau_r = 20e-6
_frR = np.fft.rfftfreq(_nS, _dtS)
_y_full = np.fft.irfft(np.fft.rfft(_u_r) / (1 + 2j * np.pi * _frR * _tau_r), n=_nS)
_y_r = _u_r.copy(); _y_r[_i0:_i1] = _y_full[_i0:_i1]

class _RampScope:
    def single(self, wait_s=None): return True
    def waveform(self, ch, **kw): return (sN.t + sN.t_off, _u_r if ch == 1 else _y_r)
    def run(self): pass

_Hr = app._frf_capture(_RampScope(), 1, 3, _bins_r, sN.t, sN.t_off, repeats=2,
                       wait_s=1, settle=0, window=_win)["mon"][0]
_Htrue_r = 1.0 / (1.0 + 2j * np.pi * (_bins_r / _recR) * _tau_r)
_err = np.abs(_Hr - _Htrue_r).max()
assert _err < 0.05, f"windowed H off by {_err:.3f}"
_Hw = app._frf_capture(_RampScope(), 1, 3, _bins_r, sN.t, sN.t_off, repeats=2,
                       wait_s=1, settle=0)["mon"][0]          # no window
assert np.abs(_Hw - _Htrue_r).max() > 3 * _err, \
    "the whole-record analysis should NOT match a hold-only plant"
for _bad, _why in (((0.5, 9.9, 4.0, 2e3, 60e3, 48), "v_dc + peak past the rail"),
                   ((0.5, 5.0, 0.2, 2e3, 60e3, 48), "hold shorter than the taper"),
                   ((0.5, 5.0, 4.0, 2e3, 200e3, 48), "f hi past the session grid"),
                   ((0.5, 5.0, 4.0, 50.0, 60e3, 48), "f lo under the hold's 2nd bin")):
    try:
        ilc_gui.build_ramped_probe(_nS, _dtS, *_bad, awg_rail=10.0)
        raise AssertionError(f"not refused: {_why}")
    except (RuntimeError, ValueError):
        pass
print(f"[38b] ramped probe: {len(_bins_r)} tones on a {_recR*1e3:.2f} ms hold "
      f"at 8.0 V within the session's {_nS}-point record (ends at zero); "
      f"windowed H matches a hold-only plant to {_err:.3f}, whole-record does "
      f"not; four bad shapes refused")

# FRF overlay: ';'-separated paths (or globs) draw together for the
# amplitude-family comparison; the measured-FRF MODEL refuses a list
app.frf_var.set(FRF + ";" + fpath)
app.do_show_frf(); root.update()
mlabs = [l.get_label() for l in app.ax_frf[0].get_lines()
         if not l.get_label().startswith("_")]
assert "WIDE_X1" in mlabs and "AUTOTST" in mlabs, mlabs
old_model = app.model_var.get()
app.model_var.set("measured FRF (nonparametric)")
try:
    app._gather_settings()
    raise AssertionError("the model accepted a multi-file FRF field")
except RuntimeError as e:
    assert "ONE" in str(e), e
app.frf_var.set(fpath)
cfgF = app._gather_settings()
assert cfgF["frf_path"] == fpath, cfgF["frf_path"]
app.model_var.set(old_model)
app.frf_var.set(FRF + ";" + fpath)   # left overlaid for the screenshots
app.do_show_frf(); root.update()
print("[39] FRF overlay: two files side by side with per-file labels, "
      "model refuses the list, single path still drives")

# the band knobs follow the model: f_cut is inert in FRF mode (the FRF
# path never pre-filters at it) and the taper is inert everywhere else
cur_model = app.model_var.get()
app.model_var.set("measured FRF (nonparametric)"); app._update_model_fields()
assert str(app._fcut_entry.cget("state")) == "disabled"
assert str(app._fuse_entry.cget("state")) == "normal"
assert str(app._fmax_entry.cget("state")) == "normal"
app.model_var.set("gain only (0th order)"); app._update_model_fields()
assert str(app._fcut_entry.cget("state")) == "normal"
assert str(app._fuse_entry.cget("state")) == "disabled"
app.model_var.set(cur_model); app._update_model_fields()
print("[40] band knobs follow the model: f_cut greyed in FRF mode, "
      "the f_use/f_max taper greyed on the parametric rungs")

# a zero-width (or inverted) taper is refused at both layers
try:
    ilc_gui.ilc.FRF(fpath, f_use=150e3, f_max=150e3)
    raise AssertionError("zero-width taper accepted by ilc.FRF")
except ValueError as e:
    assert "brick wall" in str(e), e
app.model_var.set("measured FRF (nonparametric)")
app.frf_var.set(fpath)
app.fuse_var.set("150e3"); app.fmax_var.set("100e3")
try:
    app._gather_settings()
    raise AssertionError("inverted taper accepted by the panel")
except RuntimeError as e:
    assert "f_use < f_max" in str(e), e
app.fuse_var.set("100e3"); app.fmax_var.set("150e3")
app.model_var.set(cur_model); app._update_model_fields()
print("[41] zero-width/inverted tapers refused with the 0.9x guidance")

# the state records which inverse drives the campaign, the summary shows
# it, and Load restores the panel to it
app.model_var.set("measured FRF (nonparametric)"); app._update_model_fields()
app.target_var.set(os.path.join(REPO, "waveforms", "target_MKJX1.csv"))
app.channel_var.set("EO1")
app.stem_var.set("FRFREC")
app.shotgain_var.set("0.8"); app.pgain_var.set("0.56")
app.frf_var.set(fpath)
app.fuse_var.set("100e3"); app.fmax_var.set("150e3")
app.do_init(); root.update()
st_rec = ilc_gui.run_ilc.load_state(
    os.path.join(ilc_gui.RUN_DIR, "drive_FRFREC.state.npz"))
assert str(st_rec["model"]) == "frf", st_rec.get("model")
assert str(st_rec["frf_path"]).endswith("frf_AUTOTST.csv")
assert float(st_rec["frf_use"]) == 100e3 and float(st_rec["frf_max"]) == 150e3
assert "FRF frf_AUTOTST.csv 100-150k" in app.summary.cget("text"), \
    app.summary.cget("text")
# flip the panel away, reload the state, the panel comes back
app.model_var.set("gain only (0th order)"); app._update_model_fields()
app.frf_var.set("")
app.state_var.set(os.path.join(ilc_gui.RUN_DIR, "drive_FRFREC.state.npz"))
app.do_load(); root.update()
assert app.model_var.get() == "measured FRF (nonparametric)", \
    app.model_var.get()
assert app.frf_var.get() == fpath and app.fuse_var.get() == "100000"
assert app.session.loop.frf is not None, "loop resumed without its inverse"
assert str(app._fcut_entry.cget("state")) == "disabled"  # fields followed
# a parametric init records its rung too
app.model_var.set("gain only (0th order)"); app._update_model_fields()
app.stem_var.set("PARREC")
app.do_init(); root.update()
st_rec2 = ilc_gui.run_ilc.load_state(
    os.path.join(ilc_gui.RUN_DIR, "drive_PARREC.state.npz"))
assert str(st_rec2["model"]) == "static" and str(st_rec2["frf_path"]) == ""
assert "gain only" in app.summary.cget("text")
print("[42] model record: init writes model+FRF+taper into the state, the "
      "summary names it, Load restores panel, fields and loop.frf")

# the FRF edge guard: sized from the taper's ring time (PRFRX1B lesson --
# the 0.5 ms first cut blocked the loop from the chain's real settling
# transient, 52 V at the last sample vs the one-pole's 0.8 V), fades the
# FAST part only at the extreme ends, passes the idle-offset trim
frfL = ilc_gui.ilc.FRF(fpath, f_use=100e3, f_max=150e3)
assert frfL.edge_guard_s() == 0, "guard must be OFF by default (raw inverse)"
frfL.t_guard = None                              # opt in to the auto guard
assert abs(frfL.edge_guard_s() - 100e-6) < 1e-9, frfL.edge_guard_s()
g2 = ilc_gui.ilc.FRF(fpath, f_use=40e3, f_max=60e3)
g2.t_guard = None
assert abs(g2.edge_guard_s() - 150e-6) < 1e-9,     "auto guard does not scale with the taper width"
nT, dtT = 5501, 2e-6
cC = frfL.lead(np.full(nT, 1e-3), dtT)          # constant (idle-ish) error
assert abs(cC[0]) > 0.3 * abs(cC[nT // 2]), \
    f"the guard killed the idle trim: corr[0]={cC[0]:.2e} vs mid " \
    f"{cC[nT//2]:.2e}"
eS = np.zeros(nT)
eS[-5:] = 5e-3                       # error at the LAST samples: unfixable,
w = slice(nT - 25, nT)               # the ringing there must stay suppressed
ring_on = np.abs(np.diff(frfL.lead(eS, dtT)[w])).max()
frfL.t_guard = 0
ring_off = np.abs(np.diff(frfL.lead(eS, dtT)[w])).max()
frfL.t_guard = None
assert ring_on < 0.15 * ring_off, \
    f"terminal ringing not suppressed: {ring_on:.2e} vs {ring_off:.2e}"
eM = np.zeros(nT)
eM[-120:-90] = 5e-3                  # a transient 200 us from the end: real
z = slice(nT - 130, nT - 80)         # settling error the loop MUST chase
p_on = np.abs(frfL.lead(eM, dtT)[z]).max()
frfL.t_guard = 0
p_off = np.abs(frfL.lead(eM, dtT)[z]).max()
frfL.t_guard = None
assert p_on > 0.9 * p_off, \
    f"guard still blocking correctable settling error: {p_on:.2e} vs {p_off:.2e}"
print(f"[43] FRF edge guard: OFF by default; opt-in auto "
      f"{frfL.edge_guard_s()*1e6:.0f} us for the "
      f"100-150k taper; terminal ringing x{ring_off/ring_on:.0f} down, a "
      f"200 us-from-end transient passes at {p_on/p_off:.2f}, idle trim "
      f"intact (corr[0] {cC[0]*1e3:.2f} vs mid {cC[nT//2]*1e3:.2f} mV)")

# ---- multi-channel capture for the optical campaign -------------------------
# The photodiode rides along on the monitor's own acquisition. What these
# checks protect is that every channel comes from the SAME frozen shot (so the
# traces can be cross-correlated) and that the per-shot stack survives instead
# of being averaged away inside the capture -- the ensemble std IS the
# shot-to-shot error ILC cannot learn.
import ilc_bench                                             # noqa: E402
from eomilc import polarimetry as pol                        # noqa: E402

nMC = len(sN.t)
_shot = {"i": 0}

class _MultiScope:
    """CH1 drive, CH3 monitor, CH2 a photodiode carrying half the monitor
    plus a shot-dependent offset, so a collapsed stack is detectable."""
    def single(self, wait_s=None):
        _shot["i"] += 1
        return True
    def waveform(self, ch, **kw):
        base = {1: u_p, 3: y_p}.get(ch, 0.5 * y_p + 0.01 * _shot["i"])
        return (sN.t + sN.t_off, base)
    def run(self): pass
    def get(self, q): raise RuntimeError("no scope settings in the fake")

_shot["i"] = 0
stacks = ilc_bench.capture_all(_MultiScope(), [1, 3, 2], sN.t, sN.t_off,
                               repeats=5, wait_s=1, settle=0)
assert set(stacks) == {"CH1", "CH2", "CH3"}, stacks.keys()
assert stacks.raw is None and stacks.t_grid is sN.t
assert all(v.shape == (5, nMC) for v in stacks.values()), \
    {k: v.shape for k, v in stacks.items()}
ens = pol.ensemble(stacks["CH2"])
assert ens.n == 5 and ens.std.max() > 1e-3, \
    "the per-shot spread was averaged away inside capture_all"
_shot["i"] = 0
y_only = ilc_bench.capture(_MultiScope(), [1, 3, 2], "CH3", sN.t, sN.t_off,
                           repeats=5, wait_s=1, settle=0)
assert np.allclose(y_only, y_p, atol=1e-9), \
    "the ILC loop's averaged monitor trace changed"
print(f"[44] capture_all: 3 channels x 5 shots kept "
      f"({stacks['CH2'].shape}), ensemble std {ens.std.mean():.4f} V "
      f"survives; capture() still returns the averaged monitor unchanged")

_shot["i"] = 0
aux_store = {}
app.stop_evt.clear()
y_mon = app._bench_capture(_MultiScope(), 3, sN.t, sN.t_off, repeats=4,
                           wait_s=1, settle=0, aux=(2,), aux_store=aux_store)
assert np.allclose(y_mon, y_p, atol=1e-9), "aux capture perturbed the monitor"
capA = aux_store["capture"]
assert set(capA) == {"CH2", "CH3"} and capA["CH2"].shape == (4, nMC)
assert capA.raw is None, "keep='grid' must not carry a raw view"
_shot["i"] = 0
aux2 = {}
app._bench_capture(_MultiScope(), 3, sN.t, sN.t_off, repeats=3, wait_s=1,
                   settle=0, aux=(2,), aux_store=aux2, keep="both")
capB = aux2["capture"]
assert capB.grid["CH2"].shape == (3, nMC) and capB.raw["CH2"].shape[0] == 3
assert capB.t_raw is not None and capB.t_grid is not None
_shot["i"] = 0
y_plain = app._bench_capture(_MultiScope(), 3, sN.t, sN.t_off, repeats=4,
                             wait_s=1, settle=0)
assert np.allclose(y_plain, y_mon, atol=1e-12), \
    "the no-aux path must be bit-identical to before"
print(f"[45] _bench_capture aux: CH2 stack {capA['CH2'].shape} filled, "
      f"keep='both' carries grid+raw, monitor return bit-identical")

# [45b] offset dither: a scope that reports scale/offset gets its monitor
# offset stepped across exactly one ADC code (ADC_CODE_PER_VDIV x V/div) over
# the shots -- every shot at a distinct phase, centred on the original -- and
# put back afterwards, including when the capture dies mid-way. The plain
# fake (no try_get) is left alone: no dither, no complaint.
class _DitherScope(_MultiScope):
    def __init__(self):
        self.settings = {":CHANnel3:SCALe": "1.0", ":CHANnel3:OFFSet": "2.56",
                         ":CHANnel2:SCALe": "0.5", ":CHANnel2:OFFSet": "0.1"}
        self.offsets = {2: [], 3: []}
        self.boom_at = None
    def try_get(self, q, timeout_ms=2000): return self.settings.get(q)
    def put(self, k, v):
        self.settings[k] = v
        for c in (2, 3):
            if k == f":CHANnel{c}:OFFSet": self.offsets[c].append(float(v))
    def single(self, wait_s=None):
        if self.boom_at is not None and len(self.offsets[3]) > self.boom_at:
            raise RuntimeError("scope died mid-capture")
        return True
_ds = _DitherScope()
_y = app._bench_capture(_ds, 3, sN.t, sN.t_off, repeats=8, wait_s=1, settle=0, aux=(2,),
                        dither_codes=1)
_o3 = _ds.offsets[3][:-1]                          # the 8 dithered, then the restore
assert len(_o3) == 8 and len(set(_o3)) == 8, _o3
# offsets are written with :.6g -- 10 uV at 2.5 V -- so compare to that, not to 1e-9
assert abs(np.ptp(_o3) - ilc_gui.ADC_CODE_PER_VDIV * 1.0 * 7 / 8) < 2e-5, np.ptp(_o3)
assert abs(np.mean(_o3) - 2.56) < 2e-5 and _ds.offsets[3][-1] == 2.56, _ds.offsets[3]
assert abs(np.ptp(_ds.offsets[2][:-1]) - ilc_gui.ADC_CODE_PER_VDIV * 0.5 * 7 / 8) < 2e-5
assert _ds.settings[":CHANnel3:OFFSet"] == "2.56" and _ds.settings[":CHANnel2:OFFSet"] == "0.1"
_ds2 = _DitherScope(); _ds2.boom_at = 3
try:
    app._bench_capture(_ds2, 3, sN.t, sN.t_off, repeats=8, wait_s=1, settle=0)
    raise AssertionError("the fake was meant to die")
except RuntimeError:
    pass
assert _ds2.settings[":CHANnel3:OFFSet"] == "2.56", "offset not restored after a failed capture"
_d3 = _DitherScope()
app._bench_capture(_d3, 3, sN.t, sN.t_off, repeats=8, wait_s=1, settle=0)     # the default: 3 codes
assert abs(np.ptp(_d3.offsets[3][:-1]) - 3 * ilc_gui.ADC_CODE_PER_VDIV * 1.0 * 7 / 8) < 2e-5, np.ptp(_d3.offsets[3][:-1])
assert _d3.settings[":CHANnel3:OFFSet"] == "2.56"
_yoff = app._bench_capture(_ds, 3, sN.t, sN.t_off, repeats=4, wait_s=1, settle=0, dither=False)
assert len(_ds.offsets[3]) == 9, "dither=False must not touch the offset"
app._bench_capture(_MultiScope(), 3, sN.t, sN.t_off, repeats=4, wait_s=1, settle=0)   # no try_get: silent skip
print(f"[45b] dither: 8 shots at 8 distinct offsets spanning 7/8 of a {ilc_gui.ADC_CODE_PER_VDIV*1e3:.2f} mV code "
      f"(dither_codes=1), 3 codes by default, centred and restored; restored after a mid-capture death; "
      f"off when asked; plain fake untouched")

# [45c] the readout retry: the MSO-X sometimes answers :WAVeform:DATA? with an
# empty block (pyvisa: "invalid literal for int() with base 10: b''"), which
# killed a 19-iteration run. The acquisition is frozen between single() and
# run(), so re-reading returns the same record; a persistent failure still
# raises rather than inventing data.
class _FlakyScope(_MultiScope):
    def __init__(self, fails):
        self.fails, self.calls, self.cleared = fails, 0, 0
        class _I:
            def clear(_s): self.cleared += 1
        self.inst = _I()
    def waveform(self, ch, **kw):
        self.calls += 1
        if self.calls <= self.fails:
            raise ValueError("invalid literal for int() with base 10: b''")
        return _MultiScope.waveform(self, ch, **kw)
_fs = _FlakyScope(fails=2)
_t_r, _v_r = app._read_waveform(_fs, 3, 2000)
assert _fs.calls == 3 and _fs.cleared == 2, (_fs.calls, _fs.cleared)
assert len(_v_r) and len(_t_r) == len(_v_r)
_fs2 = _FlakyScope(fails=99)
try:
    app._read_waveform(_fs2, 3, 2000)
    raise AssertionError("a scope that never answers must raise")
except ValueError:
    pass
assert _fs2.calls == 3, _fs2.calls
_y_ok = app._bench_capture(_FlakyScope(fails=1), 3, sN.t, sN.t_off, repeats=2,
                           wait_s=1, settle=0, dither=False)
assert len(_y_ok) == len(sN.t)
print("[45c] readout retry: two empty blocks re-read the same frozen "
      "acquisition (link cleared each time), a dead link still raises after "
      "3 tries, and a capture survives one bad read")

# ---- the raw view must preserve the ADC word lattice ------------------------
# resample interpolates between scope samples and boxcars when decimating, and
# either invents values that were never digitised. Measured on the bench, a
# monitor whose real lattice is 2.5 mV came back from the resampled stack with
# 4975 distinct values and an apparent 0.6 uV step, so a dither verdict taken
# after the resample is meaningless. This is that failure in miniature.
LSB = 2.5e-3
tQ = np.arange(0, len(sN.t) * 4) * (float(np.median(np.diff(sN.t))) / 4)
vQ = np.round(np.sin(2 * np.pi * 300.0 * tQ) / LSB) * LSB

class _QuantScope:
    def single(self, wait_s=None): return True
    def waveform(self, ch, **kw): return (tQ, vQ)
    def run(self): pass

def step_of(a):
    d = np.diff(np.unique(a))
    return float(d[d > 0].min())

capQ = ilc_bench.capture_all(_QuantScope(), [2], sN.t, 0.0, repeats=2,
                             wait_s=1, settle=0, keep="both")
raw_n = len(np.unique(capQ.raw["CH2"][0]))
grid_n = len(np.unique(capQ.grid["CH2"][0]))
assert abs(step_of(capQ.raw["CH2"][0]) - LSB) < 1e-12, "raw view lost the lattice"
assert step_of(capQ.grid["CH2"][0]) < LSB / 10, "resample should invent values"
assert grid_n > 5 * raw_n, (raw_n, grid_n)
capR = ilc_bench.capture_all(_QuantScope(), [2], repeats=2, wait_s=1,
                             settle=0, keep="raw")
assert capR.grid is None and capR.t_grid is None and capR["CH2"].shape[0] == 2
try:
    ilc_bench.capture_all(_QuantScope(), [2], repeats=1, keep="grid")
    raise AssertionError("keep='grid' without a t_grid should be refused")
except ValueError as e:
    assert "t_grid" in str(e), e
print(f"[46] raw view keeps the {LSB*1e3:g} mV lattice ({raw_n} distinct "
      f"values, step {step_of(capQ.raw['CH2'][0])*1e3:.3f} mV); resampling "
      f"invents {grid_n} values at {step_of(capQ.grid['CH2'][0])*1e9:.3g} nV "
      f"-- keep='raw' needs neither t_grid nor pandas")

app.stop_evt.clear()
_res2 = app._frf_capture(_MultiScope(), 1, 3, bins, sN.t, sN.t_off,
                         repeats=3, wait_s=1, settle=0, aux_ch=2)
H_mon2, _ = _res2["mon"]
H_pd, _ = _res2["aux"]
ratio = np.abs(H_pd / H_mon2)
assert np.allclose(ratio, 0.5, rtol=1e-6), \
    f"H_pd/H_mon should be the fake's 0.5, got {ratio.min():.4f}..{ratio.max():.4f}"
nw = len(sN.t) // 4
_res3 = app._frf_capture(_MultiScope(), 1, 3,
                         np.arange(2, 20), sN.t, sN.t_off, repeats=2,
                         wait_s=1, settle=0, aux_ch=2, window=(0, nw))
assert _res3["aux"] is not None and len(_res3["mon"][0]) == 18
print(f"[47] _frf_capture aux: H_pd/H_mon flat at {ratio.mean():.4f} "
      f"(the fake's 0.5); hold-window analysis accepted over "
      f"{nw} of {len(sN.t)} samples")

# Figure PNGs for eyeballing, straight from the canvases. There used to be an
# ImageGrab pass over the nine tabs as well, and it went because it could not
# be believed: ImageGrab shoots the SCREEN, so anything sitting on top of the
# test window ended up in the file. It asserted nothing, caught its own
# exceptions and printed "FAILED (non-fatal)", and cost 5 s of the run in
# sleeps waiting for tabs to paint. Saving the figures is the part that was
# ever any use, and it is 2 s and cannot be photobombed.
root.update()
try:
    for name, fig in (("wave", app.fig_wave), ("dcorr", app.fig_dcor),
                      ("dspec", app.fig_dspec), ("ddelta", app.fig_ddel),
                      ("err", app.fig_err), ("spec", app.fig_spec),
                      ("conv", app.fig_conv),
                      ("frf", app.fig_frf)):
        fig.savefig(os.path.join(SCRATCH, f"fig_{name}.png"), dpi=100)
    print("[10b] figure PNGs saved from the canvases")
except Exception:
    traceback.print_exc()
    print("[10b] figure PNGs FAILED (non-fatal)")

root.destroy()
print("ALL CHECKS PASSED")
