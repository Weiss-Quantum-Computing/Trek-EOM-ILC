"""Offline regression suite for ilc_gui.py, against real MKJX1 campaign data.

57 numbered checks: state round-trips, the span guard, the header-less
file refusal, the FRQ-vs-record check, the seed-drive Init, the model ladder
with Fit to FRF, the per-channel limits,
the GEN from-scratch path, flat first shot, hold-run display, plot
overlays, compare-campaign overlays and their picker, status line and
tail-scrolling path entries, drive spectrum, spectrum
averaging, the native-rate spectrum and its bench-kept files, the FRF
probe + measurement maths + overlay viewer, the iteration
table, dot density, linked time axes, the multi-channel capture the
optical campaign rides on, the settings tabs, learned target corrections and
the line-ripple subtraction (tests/test_corrections.py has the maths). No instruments are touched --
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

# [24b] the Waveforms tab follows the Iterations box like the other tabs:
# 'all' draws every stored measurement and every stored drive, a single
# iteration draws one of each, and the current drive is always in the drive
# pane. It used to draw the last measurement it was handed, whatever the box.
_avail = sorted({sn["it"] for sn in app.session.snapshots if sn.get("run") is None})
app.itersel_var.set("all"); app._redraw_iterations(); root.update()
_meas = [l for l in app.ax_out.get_lines() if l.get_label().startswith("measured iter")]
assert len(_meas) == len(_avail), (len(_meas), _avail)
_drv = [l for l in app.ax_drv.get_lines() if "drive" in l.get_label()]
assert any(l.get_label().startswith("drive u (iteration") or "= current" in l.get_label() for l in _drv), [l.get_label() for l in _drv]
app.itersel_var.set(str(_avail[0])); app._redraw_iterations(); root.update()
_meas1 = [l for l in app.ax_out.get_lines() if l.get_label().startswith("measured iter")]
assert len(_meas1) == 1 and _meas1[0].get_label() == f"measured iter {_avail[0]}", [l.get_label() for l in _meas1]
app.itersel_var.set(""); app._redraw_iterations(); root.update()
print(f"[24b] Waveforms follows the selection: 'all' -> {len(_meas)} measured traces, "
      f"'{_avail[0]}' -> one; the current drive is always drawn")

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
app.moncol_var.set("CH1")      # these fakes carry the monitor in CH1; GEN's default is CH2 now (the drive's channel is CH<awg_ch>)
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

# [17b] learning notches: a free-running line in the monitor (23.7 kHz on
# X1, not phase-locked, so it survives averaging with a fresh phase) must
# not be chased. notch_filter takes it out of the error and nothing else;
# parse_notches reads the field; noise_lines finds it in a shot stack; the
# GUI carries it into the loop, the state, and back into the field on Load.
_dtn = s.loop.dt; _tn = s.t
_e = 1e-3 * np.sin(2 * np.pi * 23.7e3 * _tn) + 1e-3 * np.sin(2 * np.pi * 15e3 * _tn + 0.3)
_en = ilc_gui.ilc.notch_filter(_e, _dtn, ((23700.0, 400.0),))
_F = np.fft.rfftfreq(len(_tn), _dtn); _W = np.hanning(len(_tn))
def _amp(x, f0):
    A = np.abs(np.fft.rfft(x * _W)) * 2 / _W.sum(); return A[np.argmin(np.abs(_F - f0))]
assert _amp(_en, 23.7e3) < 0.05 * _amp(_e, 23.7e3), (_amp(_en, 23.7e3), _amp(_e, 23.7e3))
assert abs(_amp(_en, 15e3) - _amp(_e, 15e3)) < 0.02 * _amp(_e, 15e3)
assert np.allclose(ilc_gui.ilc.notch_filter(_e, _dtn, ()), _e)
assert ilc_gui.ilc.parse_notches("23700/400, 24900") == ((23700.0, 400.0), (24900.0, 400.0))
assert ilc_gui.ilc.parse_notches("") == ()
try:
    ilc_gui.ilc.parse_notches("abc"); raise AssertionError("bad notch text accepted")
except ValueError:
    pass
# a stack with a random-phase 23.7 kHz line of 0.5 mV per shot, 64 shots
_rng = np.random.default_rng(3)
_stk = np.array([0.4 * s.u + 5e-4 * np.sin(2 * np.pi * 23.7e3 * _tn + _rng.uniform(0, 2 * np.pi))
                 + 3e-5 * _rng.standard_normal(len(_tn)) for _ in range(64)])
_ln = ilc_gui.ilc.noise_lines(s.loop.target, _stk, _dtn, 40e3)
assert _ln and abs(_ln[0]["f0"] - 23.7e3) < 200 and _ln[0]["ratio"] > 5, _ln[:2]
# the loop: with the notch, the update carries no 23.7 kHz; without, it does
_lp = ilc_gui.ilc.Loop(plant=s.loop.plant, target=s.loop.target, dt=_dtn,
                       channel=s.loop.channel, gamma=0.6, f_cut=40e3, limits=s.loop.limits)
_y = 0.4 * s.u + 5e-4 * np.sin(2 * np.pi * 23.7e3 * _tn)
_du0 = _lp.update(s.u, _y) - s.u
_lp.notches = ((23700.0, 400.0),)
_du1 = _lp.update(s.u, _y) - s.u
assert _amp(_du1, 23.7e3) < 0.05 * _amp(_du0, 23.7e3), (_amp(_du1, 23.7e3), _amp(_du0, 23.7e3))
# through the GUI: field -> loop -> state -> field (the extra step is rolled
# back afterwards: the Compare checks below expect GENX to end at iter 1)
import glob as _glob, shutil as _shutil
_gen_before = set(_glob.glob(os.path.join(ilc_gui.RUN_DIR, "*GENX*")))
_gen_bak = os.path.join(SCRATCH, "genx_state_bak.npz")
_shutil.copy(s.state_path, _gen_bak)
app.notch_var.set("23700/400 24900/1500")
cap3 = os.path.join(SCRATCH, "genx_cap3_001.csv")
pd.DataFrame({"Time (s)": s.t, "CH1": 0.4 * s.u}).to_csv(cap3, index=False)
app.meas_var.set(os.path.join(SCRATCH, "genx_cap3*.csv"))
app.do_step(); pump_until_idle()
assert s.loop.notches == ((23700.0, 400.0), (24900.0, 1500.0)), s.loop.notches
_stn = ilc_gui.run_ilc.load_state(s.state_path)
assert ilc_gui.run_ilc.state_notches(_stn) == ((23700.0, 400.0), (24900.0, 1500.0))
app.notch_var.set("")
app.state_var.set(s.state_path); app.do_load(); root.update()
assert app.notch_var.get() == "23700/400, 24900/1500", app.notch_var.get()
assert app.session.loop.notches == ((23700.0, 400.0), (24900.0, 1500.0))
app.notch_var.set("")
_shutil.copy(_gen_bak, app.session.state_path)
for _f in set(_glob.glob(os.path.join(ilc_gui.RUN_DIR, "*GENX*"))) - _gen_before:
    os.remove(_f)
app.do_load(); root.update()
s = app.session
assert s.iteration == 2 and s.loop.notches == (), (s.iteration, s.loop.notches)
print(f"[17b] learning notches: 23.7 kHz taken out of the error (x{_amp(_e,23.7e3)/max(_amp(_en,23.7e3),1e-12):.0f}), 15 kHz untouched; "
      f"noise_lines finds the {_ln[0]['f0']/1e3:.2f} kHz line at {_ln[0]['ratio']:.0f}x; update blind there; field -> state -> field")

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

# [27e] restart awareness. A bench run stamps its measurements and history
# with a run id; the model check declines to compare across runs (the change
# then includes the chain's drift while the output was off) and the plateau
# only looks at the current run's entries. The wiring guard refuses a monitor
# on the drive's scope channel, and GEN's defaults no longer collide.
_ms = app.session; _keep = list(_ms.snapshots); _hk = list(_ms.loop.history); _g_keep = _ms.loop.gamma
_u90 = np.zeros_like(_ms.t); _y90 = np.zeros_like(_ms.t)
_ms.snapshots.append(dict(it=90, y=_y90, m={}, u=_u90, t_wall=0.0, run_id=111.0))
app._run_id = 222.0                                         # a new run since
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _mc = app._model_check_line(91, _u90 + _du, _y90 + 2 * _fwd(_du))
assert _mc == {} and "first measurement of this run" in _buf.getvalue(), _buf.getvalue()
app._run_id = 111.0                                         # same run: compared as before
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _mc = app._model_check_line(91, _u90 + _du, _y90 + 2 * _fwd(_du))
assert "model_ratio_worst" in _mc and abs(_mc["model_ratio_worst"] - 2) < 0.3, _mc
_flatA = _hist([100, 50, 25, 12, 6, 4], [.040, .020, .010, .006, .003, .002])   # run A converging
for _h in _flatA: _h["run_id"] = 111.0
_flatB = _hist([30, 3.5, 3.4, 3.6, 3.5, 3.4, 3.5], [.030, .030, .029, .031, .030, .030, .029])   # run B after a restart: flat
for _h in _flatB: _h["run_id"] = 222.0
assert ilc_gui.ilc.plateau(_flatA + _flatB, run_id=222.0)["flat"] is True
assert ilc_gui.ilc.plateau(_flatA + _flatB[:3], run_id=222.0) is None       # too few in this run
assert ilc_gui.ilc.plateau(_flatA + _flatB, run_id=111.0)["flat"] is False  # run A alone: converging
_ms.snapshots[:] = _keep; _ms.loop.history[:] = _hk; _ms.loop.gamma = _g_keep; app._run_id = None
assert ilc_gui.CH_DEFAULTS["GEN"]["scope_ch"] != ilc_gui.CH_DEFAULTS["GEN"]["awg_ch"]
assert ilc_gui.CH_DEFAULTS["GEN"]["mon_col"] == f"CH{ilc_gui.CH_DEFAULTS['GEN']['scope_ch']}"
import tkinter.messagebox as _mb
_said = []; _orig = _mb.showerror; _mb.showerror = lambda t, m, **k: _said.append(m)
assert app._wiring_ok(1, 1, "Bench") is False and "also the drive's channel" in _said[-1]
assert app._wiring_ok(1, 3, "Bench") is True and len(_said) == 1
_mb.showerror = _orig
print("[27e] restart-aware: model check skips the first measurement of a run and compares within it; "
      "plateau sees one run at a time; GEN monitor on its own channel; same-channel wiring refused")

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
# [31b] compare traces sit UNDER the session's own on every axis: they are
# drawn later, and at the default z-order they covered the running state's
# newest points
app.cmpsel_var.set("GENX:all TSTX1:all"); app._redraw_iterations(); root.update()
_axes = [a for nm in dir(app) if nm.startswith("fig_") for a in getattr(app, nm).axes]
_nc = _ns = 0
for _ax in _axes:
    _cmpz = [l.get_zorder() for l in _ax.get_lines() if l.get_label().split(" ")[0] in ("GENX", "TSTX1")]
    _ownz = [l.get_zorder() for l in _ax.get_lines() if l.get_label().split(" ")[0] not in ("GENX", "TSTX1") and not l.get_label().startswith("_")]
    if _cmpz and _ownz:
        assert max(_cmpz) < min(_ownz), (_ax.get_title(), max(_cmpz), min(_ownz))
        _nc += len(_cmpz); _ns += len(_ownz)
assert _nc and _ns, (_nc, _ns)
print(f"[31b] compare traces under the session's own: {_nc} compare lines below {_ns} session lines across the tabs")
app.cmpsel_var.set("GENX")
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
# (on the Session tab, which is on screen when a person reads it; since the
# Hardware tab went first the entry is unmapped until then and has no width)
app.lnb.select(app._tabs["session"]); root.update()
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

# [41b] the Measure FRF dialog: seven fields on their own rows, the notes and
# the buttons below them. The note used to be gridded at row 5, on top of the
# ramped probe's 'hold V' and 'hold ms' -- the operating point was there and
# could not be seen.
_before = set(root.winfo_children())
app.do_measure_frf(); root.update()
_dlg = next(w for w in root.winfo_children() if w not in _before); _fr = _dlg.winfo_children()[0]
_rows = {}
for _w in _fr.winfo_children():
    _gi = _w.grid_info(); _rows.setdefault(int(_gi["row"]), []).append(int(_gi.get("columnspan", 1)))
_labels = [w.cget("text") for w in _fr.winfo_children() if w.winfo_class() == "TLabel"]
assert "hold V (ramped only)" in _labels and "hold ms (ramped only)" in _labels
_clash = [r for r, spans in _rows.items() if 2 in spans and len(spans) > 1]
assert not _clash, f"a spanning note shares a row with a field: {_clash}"
_dlg.destroy(); root.update()
print(f"[41b] Measure FRF dialog: {len(_rows)} rows, hold V / hold ms visible, no note on top of a field")

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
# [42c] Load restores the Target box to the state's own target: the path
# Init recorded, or -- for a state from before the path was recorded --
# the CSV in waveforms\ / run\ whose content matches the state's target.
_tgt = os.path.join(REPO, "waveforms", "target_MKJX1.csv")
assert os.path.normcase(app.target_var.get()) == os.path.normcase(_tgt), app.target_var.get()
assert str(st_rec["target_path"] if "target_path" in st_rec else
           ilc_gui.run_ilc.load_state(app.state_var.get())["target_path"]).endswith("target_MKJX1.csv")
app.target_var.set("")
app.do_load(); root.update()
assert os.path.normcase(app.target_var.get()) == os.path.normcase(_tgt), "recorded path not restored"
_old = dict(np.load(app.state_var.get(), allow_pickle=True))
_old.pop("target_path")
np.savez(app.state_var.get(), **_old)
app.target_var.set("")
app.do_load(); root.update()
assert os.path.normcase(app.target_var.get()) == os.path.normcase(_tgt), \
    f"content match failed: {app.target_var.get()!r}"
assert app.session.target_path and os.path.normcase(app.session.target_path) == os.path.normcase(_tgt)
print("[42c] Load restores the Target box: from the recorded path, and by content for an older state")
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

# [42b] the panel's numbers are remembered per channel (first-shot gain,
# seed drive, FRF) and per channel+model (parameter boxes): switching away
# and back brings them back; a channel never used starts blank (the
# prior-leak guard); a model never used on this channel keeps the current
# numbers as a start. Both go through the same path the comboboxes use.
app._field_mem.clear()
app.channel_var.set("EO1"); app._on_channel_change()
app._mem_channel = "EO1"        # the panel was moved by hand above; pin it
app.model_var.set("gain only (0th order)"); app._on_model_change()
app.shotgain_var.set("0.81"); app.pgain_var.set("0.561"); app.seed_var.set("")
app.model_var.set("one pole (1st order)"); app._on_model_change()
assert app.pgain_var.get() == "0.561", "a new model on this channel lost the gain start"
app.pgain_var.set("0.60"); app.ptau_var.set("3.1")
app.channel_var.set("EO2"); app._on_channel_change()
assert app.shotgain_var.get() == "" and app.pgain_var.get() == "" and app.ptau_var.get() == "", \
    "EO1's numbers leaked into a never-used EO2"
app.shotgain_var.set("1.9"); app.pgain_var.set("0.70")
app.channel_var.set("EO1"); app._on_channel_change()
assert (app.shotgain_var.get(), app.pgain_var.get(), app.ptau_var.get()) == ("0.81", "0.60", "3.1"), \
    (app.shotgain_var.get(), app.pgain_var.get(), app.ptau_var.get())
assert app.model_var.get() == "one pole (1st order)"
app.model_var.set("gain only (0th order)"); app._on_model_change()
assert app.pgain_var.get() == "0.561" and app.ptau_var.get() == "", (app.pgain_var.get(), app.ptau_var.get())
app.channel_var.set("EO2"); app._on_channel_change()
assert app.shotgain_var.get() == "1.9" and app.pgain_var.get() == "", \
    "EO2/static was never set: gain must be blank, the shot gain is EO2's"
app.model_var.set("one pole (1st order)"); app._on_model_change()
assert app.pgain_var.get() == "0.70", app.pgain_var.get()
# the memory rides along in the config
app._save_config()
assert app._field_mem["EO1"]["models"]["one_pole"] == {"gain": "0.60", "tau": "3.1", "fn": "", "zeta": ""}
assert app._field_mem["EO2"]["shot_gain"] == "1.9"
# back to where the checks below expect the panel
app.channel_var.set("EO1"); app._on_channel_change()
app.model_var.set("gain only (0th order)"); app._on_model_change()
app.shotgain_var.set("0.8"); app.pgain_var.set("0.56")
print("[42b] field memory: first-shot gain / seed / FRF per channel, parameter boxes per channel+model; "
      "a fresh channel starts blank, a fresh model keeps the current start; saved with the config")

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

# [47b] Measure FRF dithers too: drive, monitor and aux offsets are stepped
# over three codes across the shots and put back, H is unchanged by it (the
# fake returns true volts whatever the offset), and dither=False leaves the
# scope alone. A fake with no try_get is skipped silently.
class _FrfDitherScope(_MultiScope):
    def __init__(self):
        self.settings = {":CHANnel1:SCALe": "0.5", ":CHANnel1:OFFSet": "0.0",
                         ":CHANnel3:SCALe": "0.5", ":CHANnel3:OFFSet": "0.0",
                         ":CHANnel2:SCALe": "0.2", ":CHANnel2:OFFSet": "0.1"}
        self.offsets = {1: [], 3: [], 2: []}
    def try_get(self, q, timeout_ms=2000): return self.settings.get(q)
    def put(self, k, v):
        self.settings[k] = v
        for c in (1, 2, 3):
            if k == f":CHANnel{c}:OFFSet": self.offsets[c].append(float(v))
_shot["i"] = 0
_fds = _FrfDitherScope()
_rd = app._frf_capture(_fds, 1, 3, bins, sN.t, sN.t_off, repeats=8, wait_s=1, settle=0, aux_ch=2)
for _c, _scale in ((1, 0.5), (3, 0.5), (2, 0.2)):
    _o = _fds.offsets[_c][:-1]
    assert len(_o) == 8 and len(set(_o)) == 8, (_c, _o)
    assert abs(np.ptp(_o) - 3 * ilc_gui.ADC_CODE_PER_VDIV * _scale * 7 / 8) < 2e-5, (_c, np.ptp(_o))
assert float(_fds.settings[":CHANnel3:OFFSet"]) == 0.0 and float(_fds.settings[":CHANnel2:OFFSet"]) == 0.1   # restored (written as :.6g)
_shot["i"] = 0
_H0, _c0 = app._frf_capture(_FrfScope(), 1, 3, bins, sN.t, sN.t_off, repeats=8, wait_s=1, settle=0)["mon"]
assert np.allclose(np.abs(_rd["mon"][0]), np.abs(_H0), rtol=1e-6), "dither changed H"
_fds2 = _FrfDitherScope(); _shot["i"] = 0
app._frf_capture(_fds2, 1, 3, bins, sN.t, sN.t_off, repeats=4, wait_s=1, settle=0, dither=False)
assert not any(_fds2.offsets.values()), "dither=False touched an offset"
print("[47b] Measure FRF dither: drive, monitor and aux stepped over 3 codes and restored; H unchanged; off when asked")

# [48] Settings tabs, learned target corrections, line ripple (5 Oct 2026).
# The left side is tabs selected by FRAME; a correction file moves the
# TARGET (base kept), round-trips through the state, refuses past the cap,
# and Revert puts the base back; a line-ripple fit from captures is
# subtracted from every measurement once ticked and is saved in the state.
from eomilc import corrections as _corrmod, mains as _mains
assert set(app._tabs) == {"hw", "session", "model", "scope", "run", "optical", "corr"}
for _fr in app._tabs.values():
    app.lnb.select(_fr)
root.update()
app.state_var.set(STATE)
app.do_load(); root.update()
_s = app.session
_sc = _s.loop.channel.mon_scale
_base_out = _s.loop.target * _sc
_bump = 60.0 * np.sin(2 * np.pi * 600 * _s.t) * (_base_out > 0.05 * _base_out.max())
_cmeta = dict(channel=_s.channel, slot="optical", band_hz=2000.0,
              line_removed=True, quantity="smoke test",
              **_corrmod.target_fingerprint(_base_out, _s.loop.dt))
_cpath = _corrmod.write_correction(os.path.join(SCRATCH, "corr_smoke.csv"), _s.t,
                                   _bump, np.full_like(_s.t, 2.0), _cmeta)
app._corr_pending = []
app.do_corr_add([_cpath])
app.corrcap_var.set("10")                     # far under the 60 V bump
app.do_corr_apply(); root.update()
assert app.session.base_target is None, "applied past the cap"
assert app._corr_pending[0]["status"] == "refused"
app.corrcap_var.set("300")
app.do_corr_apply(); root.update()
_s = app.session
assert _s.base_target is not None
_d = (_s.loop.target - _s.base_target) * _sc
assert 30 < np.max(np.abs(_d)) < 70, np.max(np.abs(_d))   # the hard-edged test bump overshoots a little through the band filter
assert _d[0] == 0 and _d[-1] == 0
app.do_load(); root.update()                  # the state carries it
_s = app.session
assert _s.base_target is not None and _s.corr_layers[0]["status"] == "applied"
assert np.allclose((_s.loop.target - _s.base_target) * _sc, _d)
app.do_corr_revert(); root.update()
_s = app.session
assert _s.base_target is None and np.allclose(_s.loop.target * _sc, _base_out)
print(f"[48] tabs by frame; correction refused past a 10 V cap, applied at 300 "
      f"({np.max(np.abs(_d)):.1f} V pk, ends 0), reloaded from the state, reverted")

# [48b] line ripple from captures: 12 shots of 60/120/180 Hz at a FIXED phase
# (line-synchronous) over 100 ms -> fit, coherence ~1, ticked -> Loop.line set
# and saved; free-running phases -> coherence low, switched off with a note.
_tl = np.arange(-10e-3, 90e-3, 5e-6)
_rng48 = np.random.default_rng(48)
def _cap48(i, locked):
    ph = 0.0 if locked else _rng48.uniform(0, 2 * np.pi)
    y = (0.9e-3 * np.cos(2 * np.pi * 60 * _tl + np.deg2rad(44) + ph)
         + 0.3e-3 * np.cos(2 * np.pi * 120 * _tl - 0.8 + 2 * ph)
         + _rng48.normal(0, 0.3e-3, _tl.size))
    p = os.path.join(SCRATCH, f"line48_{'L' if locked else 'F'}_{i:02d}.csv")
    pd.DataFrame({"Time (s)": _tl + _s.t_off, "CH3": y}).to_csv(p, index=False)
    return p
_locked = [_cap48(i, True) for i in range(12)]
app.moncol_var.set("CH3"); app.lineh_var.set("3"); app.lineseg_var.set("")
app.do_line_from_files(_locked); root.update()
_lf = app.session.line_fit
assert _lf is not None and _lf["meta"]["coherence"] > 0.9, _lf["meta"]
assert abs(_lf["fit"]["amp"][0] - 0.9e-3) < 0.05e-3
app.line_on_var.set(True); app._on_line_toggle(); root.update()
assert app.session.loop.line is not None
app.do_load(); root.update()
assert app.session.loop.line is not None and app.line_on_var.get()
_free = [_cap48(i, False) for i in range(12)]
app.do_line_from_files(_free); root.update()
assert app.session.line_fit["meta"]["coherence"] < 0.7
assert not app.line_on_var.get(), "a free-running fit left subtraction on"
_coh_free = app.session.line_fit["meta"]["coherence"]
app.do_line_clear(); root.update()
assert app.session.loop.line is None
print(f"[48b] line ripple: locked captures -> 60 Hz {_lf['fit']['amp'][0]*1e3:.3f} mV, "
      f"coherence {_lf['meta']['coherence']:.2f}, subtracted and reloaded; "
      f"free-running -> coherence {_coh_free:.2f}, "
      f"switched off; cleared")

# [54] Scope channels by ROLE (12 Oct 2026): the polarimeter's vocabulary on
# the Measure tab. The Trek chains' numbers follow the map, the drive readback
# is the CmdXn channel (none = alignment skipped, an FRF refused), the PD comes
# from the map, GEN keeps its old numbers, and the map rides into the state and
# through the CLI pass-through.
import json as _json
import run_ilc as _run_ilc
from eomilc import channels as _chm
assert set(app.chan_role) == {"1", "2", "3", "4"}
app._set_channels(_chm.DEFAULT_CHANNELS); root.update()
app.channel_var.set("EO1"); app._apply_channel_defaults()
assert app.scopech_var.get() == "3" and app.moncol_var.get() == "CH3", (
    app.scopech_var.get(), app.moncol_var.get())
assert app._drive_scope_ch(1) == 2 and app._drive_scope_ch(2) is None
assert ilc_gui.pd_channel(app.cfg) == 1
assert app._wiring_ok(1, 3, "smoke") is True
# monitor X1 moved to CH2 and the command to CH3: the numbers follow the map
app.chan_role["2"].set("MonX1"); app.chan_role["3"].set("CmdX1")
app._channels_changed(); root.update()
assert app.scopech_var.get() == "2" and app._drive_scope_ch(1) == 3, app.scopech_var.get()
app.channel_var.set("EO2"); app._apply_channel_defaults()
assert app.scopech_var.get() == "4"
# a monitor number that contradicts the map is refused; so is an FRF with no
# CmdX2 channel to read the drive from (the dialog is stubbed)
_errs = []
_real_err = ilc_gui.messagebox.showerror
ilc_gui.messagebox.showerror = lambda *a, **k: _errs.append(a)
try:
    assert app._wiring_ok(2, 3, "smoke") is False and len(_errs) == 1
    assert app._wiring_ok(2, 4, "smoke") is True
    assert app._wiring_ok(2, 4, "smoke", need_drive=True) is False and len(_errs) == 2
finally:
    ilc_gui.messagebox.showerror = _real_err
# GEN keeps the old convention: readback on scope CH<awg_ch>
app.channel_var.set("GEN"); assert app._drive_scope_ch(1) == 1
app.channel_var.set("EO1"); app._apply_channel_defaults()
# copy from the polarimeter's config (a stand-in file)
_pcfg = os.path.join(SCRATCH, "pol_config.json")
with open(_pcfg, "w") as _fh:
    _json.dump({"channels": {"1": {"role": "PD", "name": "Analyzer PD"},
                             "2": {"role": "CmdX1", "name": "Trek input X1"},
                             "3": {"role": "MonX1", "name": "Trek monitor X1"},
                             "4": {"role": "MonX2", "name": "Trek monitor X2"}}}, _fh)
_chm.POLARIMETER_CONFIG_PATH = _pcfg
app.do_channels_from_polarimeter(); root.update()
assert app.chan_role["2"].get() == "CmdX1" and app.chan_name["2"].get() == "Trek input X1"
assert app.scopech_var.get() == "3" and app._drive_scope_ch(1) == 2
# the map rides in the state, survives Load, and the CLI carries it through
_s = app.session
_s.scope_channels = _chm.record(app.cfg)
ilc_gui.save_session(_s)
_st = _run_ilc.load_state(_s.state_path)
assert "scope_channels" in _run_ilc.passthrough(_st)
assert _chm.from_record(_st["scope_channels"]) == _chm.channels_of(app.cfg)
app.do_load(); root.update()
assert _chm.from_record(app.session.scope_channels) == _chm.channels_of(app.cfg)
_d = _chm.differences(app.cfg, {"scope_channels": {**_chm.channels_of(app.cfg), "2": {"role": "off", "name": ""}}})
assert _d == ["CH2: CmdX1 -> off"], _d
# the config round-trips the map
app._save_config()
with open(ilc_gui.CONFIG_PATH) as _fh:
    assert _chm.channels_of(_json.load(_fh)) == _chm.channels_of(app.cfg)
print("[54] scope channel roles: numbers follow the map (EO1 mon CH3 / readback "
      "CH2 / PD CH1), map changes re-point them, a contradicting monitor number "
      "and an FRF without CmdX are refused, GEN unchanged, polarimeter copy, "
      "state + pass-through + config round trip")

# [55] Diagnostics (12 Oct 2026): a stats sidecar per measurement (standard
# errors, campaign bands and their floors, the residual folded on the ADC
# code), recalled with the measurement; the Diagnostics tab's six views, the
# wall-clock convergence view, and the noise bands on Error / Spectrum /
# Convergence all draw from it. Synthetic shots: an injected per-code pattern
# on the early iterations, none later.
from eomilc import diagnostics as _dg
_s = app.session
_rng = np.random.default_rng(5)
_code = _dg.ADC_CODE_PER_VDIV
for _sn in _s.snapshots:
    if _sn.get("run") is not None:
        continue
    _pat = (2.0e-3 if _sn["it"] < 3 else 0.0) * np.sin(2 * np.pi * np.mod(_s.loop.target, _code) / _code)
    _stack = _sn["y"] + _pat + _rng.normal(0, 0.8e-3, (12, len(_s.t)))
    _sn["stats"] = _dg.measurement_stats(_s.loop.target, _stack, _s.loop.dt, vdiv=1.0, offset=2.7)
    _sn["run_id"] = 1 if _sn["it"] < 8 else 2
_late = [sn for sn in _s.snapshots if sn.get("run") is None and sn["it"] >= 6]
# the fold's verdict, on records that are the target plus noise (a real
# iteration's own corner content folds to a few sigma on its own; the
# unit test and this check keep the pure cases apart): a 2 mV per-code
# pattern stands many sigma up, none reads under 4
_tgt = _s.loop.target
_st_pat = _dg.measurement_stats(
    _tgt, _tgt + 2.0e-3 * np.sin(2 * np.pi * np.mod(_tgt, _code) / _code)
    + _rng.normal(0, 0.8e-3, (12, len(_s.t))), _s.loop.dt, vdiv=1.0)
_st_none = _dg.measurement_stats(_tgt, _tgt + _rng.normal(0, 0.8e-3, (12, len(_s.t))),
                                 _s.loop.dt, vdiv=1.0)
assert _st_pat["fold_amp"] / _st_pat["fold_sig"] > 6 and \
    _st_none["fold_amp"] / _st_none["fold_sig"] < 4, \
    (_st_pat["fold_amp"] / _st_pat["fold_sig"], _st_none["fold_amp"] / _st_none["fold_sig"])
for _i, _m in enumerate(_s.loop.history):
    _m["floor_rms"] = 0.05e-3
    _m["update_below_lsb"] = 0.1 + 0.05 * _i
    _m["model_bands"] = [dict(lo=lo, hi=hi, verdict="ok", ratio_local=1.1, lam=0.34, corr=0.9,
                              update_rms=1e-3) for lo, hi in ((0, 5e3), (5e3, 10e3))]
# the sidecar round-trips through recall: write one beside a stored measurement
_f = sorted(glob.glob(os.path.join(os.path.dirname(_s.state_path), f"meas_{_s.stem}_i*.npy")))[-1]
_dg.save_stats(_dg.stats_path(_f), _late[-1]["stats"])
_s2 = ilc_gui.load_session(_s.state_path)
ilc_gui.recall_snapshots(_s2)
_with = [sn for sn in _s2.snapshots if sn.get("stats") is not None]
assert len(_with) == 1 and abs(_with[0]["stats"]["fold_amp"] - _late[-1]["stats"]["fold_amp"]) < 1e-12
os.remove(_dg.stats_path(_f))
# _measurement_stats writes the sidecar and reads the scope's V/div when it can
class _VdivScope:
    def get(self, q):
        return {":CHANnel3:SCALe": "1.0", ":CHANnel3:OFFSet": "2.7"}[q]
_tmpm = os.path.join(SCRATCH, "meas_SMOKE_i00.npy")
_st = app._measurement_stats(_late[-1]["y"] + _rng.normal(0, 1e-3, (6, len(_s.t))), _tmpm,
                             _VdivScope(), 3)
assert _st is not None and _st["vdiv"] == 1.0 and os.path.exists(_dg.stats_path(_tmpm))
assert set(app._stats_metrics(_st)) == {"bands_rms", "bands_floor", "floor_rms", "vdiv", "fold_amp", "fold_sig"}
assert app._measurement_stats(_late[-1]["y"] + _rng.normal(0, 1e-3, (6, len(_s.t))),
                              os.path.join(SCRATCH, "meas_SMOKE_i01.npy"))["vdiv"] != _st["vdiv"]  # nan without a scope
# every view draws
app.itersel_var.set("all"); app._redraw_iterations(); root.update()
assert [app.nb.tab(t, "text") for t in app.nb.tabs()] == \
    ["Waveforms", "Error", "Convergence", "Drive", "Diagnostics", "Table", "Inputs", "Optical"]
assert len(app.ax_fold.lines) > 10 and len(app.ax_foldamp.lines) >= 1
assert len(app.ax_bands.lines) >= 8                       # 4 bands x (rms + floor)
assert len(app.ax_lam.lines) >= 2 and any(l.get_ydata().size == 1 for l in app.ax_lam.lines)
assert len(app.ax_heat.images) == 1 and app._heat_cbar is not None
assert any(p.get_height() > 0 for p in app.ax_dac.patches) or \
    any("needs the selected" in t.get_text() for t in app.ax_dac.texts)   # no drive pair in this fixture
assert len(app.ax_dac.child_axes) == 1                    # the inset: % under one step per iteration
assert any("noise floor" in (c.get_label() or "") for c in app.ax_spec.collections) and \
    any("learnable above" in (l.get_label() or "") for l in app.ax_spec.lines)
assert len(app.ax_err.collections) == 1                   # the +/- 1 SEM band
assert any("noise floor" in (l.get_label() or "") for l in app.ax_conv.lines)
assert len(app.ax_convt.lines) >= 2
# the lattice view says what it needs when there is no native file
assert any("native-rate" in t.get_text() for t in app.ax_latt.texts)
app._show_fig(app.fig_heat); root.update()
assert app._views["Diagnostics"]["var"].get() == "heatmap"
app._redraw_iterations(); root.update()                   # a second draw replaces the colorbar
assert len(app.fig_heat.axes) == 2
for _sn in _s.snapshots:
    _sn.pop("stats", None)
for _m in _s.loop.history:
    for _k in ("floor_rms", "update_below_lsb", "model_bands"):
        _m.pop(_k, None)
app._redraw_iterations(); root.update()
print("[55] diagnostics: fold finds the injected per-code pattern on i0-i2 (>6 sigma) and not later; "
      "sidecar written, read back through recall, V/div from the scope; six Diagnostics views, the "
      "wall-clock convergence, the SEM band, the spectrum floor and the convergence floor all drew")

# [56] Hardware tab (13 Oct 2026): four rows, each check opens / asks /
# closes and lands in its row with the time, failures in red, and the log
# carries the detail. The checks themselves are stubbed here (no bench);
# ilc_hardware.check_awg is exercised on the simulated generator.
import time
from eomilc import simbench as _simb
import ilc_hardware as _ihw
assert [k for k, _n, _w in app.HW_ROWS] == ["scope", "awg", "ell14", "ds345"]
_awg, _scope, _mod = _simb.make_bench({1: os.path.join(REPO, "run", "frf_WIDE_X1.csv"),
                                       2: os.path.join(REPO, "run", "frf_WIDE_X2.csv")})
_mod_fake = type("M", (), {})()
_real_make = ilc_gui.ilc_bench.make_awg
ilc_gui.ilc_bench.make_awg = lambda m: _awg
try:
    _r = _ihw.check_awg(_mod_fake)
finally:
    ilc_gui.ilc_bench.make_awg = _real_make
assert _r["ok"] and "CH1 OFF" in _r["text"] and "CH2 OFF" in _r["text"], _r
_calls = []
_orig = (_ihw.check_scope, _ihw.check_awg, _ihw.check_ell14, _ihw.check_ds345)
_ihw.check_scope = lambda m, d="": (_calls.append("scope"), dict(ok=True, text="MSO-X: HRES", detail="x"))[1]
_ihw.check_awg = lambda m: (_calls.append("awg"), dict(ok=True, text="4063B: CH1 OFF", detail=""))[1]
_ihw.check_ell14 = lambda c, p=None: (_calls.append("ell14"), dict(ok=False, text="COM3: in use by another program", detail=""))[1]
_ihw.check_ds345 = lambda c, p=None: (_calls.append("ds345"), dict(ok=True, text="SRS DS345 on GPIB0::19", detail=""))[1]
try:
    app.do_hw_check(["scope", "awg", "ell14", "ds345"])
    for _ in range(200):
        root.update()
        if not app.busy:
            break
        time.sleep(0.02)
finally:
    _ihw.check_scope, _ihw.check_awg, _ihw.check_ell14, _ihw.check_ds345 = _orig
assert _calls == ["scope", "awg", "ell14", "ds345"], _calls
assert app.hw_status["scope"].cget("text") == "MSO-X: HRES" and app.hw_time["scope"].cget("text")
assert str(app.hw_status["ell14"].cget("foreground")) == "#a93a2c"
assert str(app.hw_status["ds345"].cget("foreground")) == "#1b6e1b"
assert "ell14: COM3: in use" in app.log_text.get("1.0", "end")
print("[56] hardware tab: four rows, checks in order, status + time per row, in-use in red, "
      "detail in the log; check_awg on the simulated generator reads both outputs OFF")

# [57] Pair mode (13 Oct 2026): X1 and X2 learned side by side, each with its
# own model, from one scope acquisition. A generated target pair in SCRATCH,
# Init on both with a different model in each column, Auto-set and two
# iterations of the pair bench loop on the simulated bench (cabled by the
# panel's channel map), the shown-channel switch, and back to single mode.
# Dialogs, the instruments and the AWG library are stubbed / redirected here
# and put back at the end, so the block stands alone (smoke_block.py 57).
import time as _time57
import run_ilc as _ri57
import ilc_bench as _ib57
from eomilc import simbench as _sb57
from eomilc import diagnostics as _dg57
_mb57 = ilc_gui.messagebox
_saved57 = (_mb57.askyesno, _mb57.showerror, _mb57.showinfo, ilc_gui.AWG_WAVEFORMS)
_asked57 = []
_mb57.askyesno = lambda title, msg, **k: (_asked57.append(title), True)[1]
_mb57.showerror = lambda title, msg, **k: _asked57.append(("error", title, msg))
_mb57.showinfo = lambda title, msg, **k: _asked57.append(("info", title, msg))
ilc_gui.AWG_WAVEFORMS = os.path.join(SCRATCH, "Waveforms57")


def _pump57(limit_s=300):
    _t0 = _time57.time()
    root.update()
    while app.busy and _time57.time() - _t0 < limit_s:
        root.update()
        _time57.sleep(0.02)
    for _ in range(10):
        root.update()
        _time57.sleep(0.01)
    assert not app.busy, "the worker did not finish"


try:
    # the pair: the ILC header, the 2 us grid, 0 -> 5 kV at the EOM (5 V on
    # the 1 V/kV monitor, like the fixtures) -> 0 over an 11 ms record
    _tus = np.arange(5501) * 2.0
    _ramp = np.clip(np.minimum((_tus - 500) / 3000, (10500 - _tus) / 3000), 0, 1)
    _v57 = 5000 * 0.5 * (1 - np.cos(np.pi * _ramp))
    for _k in ("X1", "X2"):
        np.savetxt(os.path.join(SCRATCH, f"target_PAIR{_k}.csv"), np.c_[_tus, _v57],
                   delimiter=",", header="time_us,voltage_V", comments="", fmt="%.6g")
    _bad = os.path.join(SCRATCH, "target_LONE.csv")
    shutil.copy(os.path.join(SCRATCH, "target_PAIRX1.csv"), _bad)

    app._set_channels(ilc_gui.chanmap.DEFAULT_CHANNELS)   # CH1 PD, CH2 CmdX1, CH3 MonX1, CH4 MonX2
    app.cmpsel_var.set("")
    app.line_on_var.set(False)
    app.pair_var.set(True)
    app._on_pair_toggle()
    root.update()
    assert app.channel_var.get() == "EO1" and str(app._channel_cb.cget("state")) == "disabled"
    assert app._col2.frame.winfo_manager() == "pack" and app._shown_frame.winfo_manager() == "pack"
    assert app.bench_btn.cget("text") == "Run bench loop (X1 + X2)"
    # a name with no X1 in it is refused, by name
    app.target_var.set(_bad)
    app.stem_var.set("PAIR")
    app.do_init()
    assert app.pair is None and _asked57[-1][0] == "error" and "no 'X1'" in _asked57[-1][2], _asked57[-1]

    # X1's column third order, X2's resonant -- each chain its own model
    app.target_var.set(os.path.join(SCRATCH, "target_PAIRX1.csv"))
    app.seed_var.set("")
    app.shotgain_var.set("")
    app.toff_var.set("0")
    app.fs_var.set("10.0")
    app.model_var.set(ilc_gui.KEY2LABEL["third_order"])
    app._on_model_change()
    for _var, _val in ((app.pgain_var, "0.557"), (app.ptau_var, "3.03"), (app.pfn_var, "12564"),
                       (app.pzeta_var, "2.209"), (app.gamma_var, "0.6"), (app.fcut_var, "20000"),
                       (app.notch_var, "")):
        _var.set(_val)
    app.model_var_2.set(ilc_gui.KEY2LABEL["resonant"])
    app._on_model_change_2()
    for _var, _val in ((app.pgain_var_2, "0.6116"), (app.pfn_var_2, "7823"), (app.pzeta_var_2, "1.474"),
                       (app.gamma_var_2, "0.5"), (app.fcut_var_2, "15000"), (app.notch_var_2, "")):
        _var.set(_val)
    root.update()
    assert app._col2.param_entries["tau"].winfo_manager() == "" and \
        app._col2.param_entries["fn"].winfo_manager() == "pack"          # only the rung's fields
    _n57 = len(_asked57)
    app.do_init()
    root.update()
    _p57 = app.pair
    assert _p57 is not None and len(_asked57) == _n57, _asked57[_n57:]
    assert (_p57["X1"].channel, _p57["X2"].channel) == ("EO1", "EO2")
    assert (_p57["X1"].stem, _p57["X2"].stem) == ("PAIRX1", "PAIRX2")
    assert app.session is _p57["X1"] and "(X1 of a pair)" in app.summary.cget("text")
    _st57 = {k: _ri57.load_state(_p57[k].state_path) for k in ("X1", "X2")}
    assert str(_st57["X1"]["model"]) == "third_order" and str(_st57["X2"]["model"]) == "resonant"
    assert abs(float(_st57["X1"]["fn"]) - 12564) < 1 and abs(float(_st57["X2"]["fn"]) - 7823) < 1
    assert float(_st57["X1"]["tau"]) > 0 and float(_st57["X2"]["tau"]) == 0
    assert float(_st57["X1"]["f_cut"]) == 20000 and float(_st57["X2"]["f_cut"]) == 15000
    assert float(_st57["X2"]["gamma"]) == 0.5
    assert str(_st57["X2"]["target_path"]).endswith("target_PAIRX2.csv")
    app._save_config()
    assert app._field_mem["EO2"]["models"]["resonant"]["fn"] == "7823"   # X2's memory is EO2's

    # the simulated bench: one bench object for every connect, cabled by the map
    _bench57 = _sb57.make_bench({1: os.path.join(REPO, "run", "frf_WIDE_X1.csv"),
                                 2: os.path.join(REPO, "run", "frf_WIDE_X2.csv")}, channels=app.cfg)
    _awg57, _scope57, _mod57 = _bench57

    def _fake_pair57():
        _ib57._AWGMOD = _mod57
        return _awg57, _scope57
    app._connect_pair = _fake_pair57
    app._bench_modules = lambda: (None, _mod57)
    assert "X2 (EO2): AWG CH2 -> monitor CH4, readback none" in app.run_state.cget("text")

    # Auto-set: refused while either output is ON; then both channels and all four verticals
    _awg57.set_output(2, True)
    app.do_autoset()
    _pump57()
    assert _awg57.ch[1]["frq"] == 100.0 and _awg57.ch[2]["frq"] == 100.0
    _awg57.set_output(2, False)
    app.do_autoset()
    _pump57()
    _per57 = 5501 * 2e-6
    assert all(abs(_awg57.ch[c]["frq"] * _per57 - 1) < 1e-9 for c in (1, 2))
    assert all(_scope57.settings[f":CHANnel{c}:OFFSet"] != "0.0" for c in (2, 3, 4))
    assert _scope57.settings[":CHANnel1:OFFSet"] == "0.0"                    # the PD is not a chain's

    # two iterations of the pair loop: one question, both ON, both OFF at the end
    app.iters_var.set("2")
    app.repeats_var.set("16")
    app.wait_var.set("5")
    app.skip_var.set(False)
    app.keepnative_var.set(False)
    _n57 = len(_asked57)
    _shots0 = _scope57.shots
    app.do_bench()
    _pump57()
    assert _asked57[_n57:] == ["Outputs are OFF"], _asked57[_n57:]
    assert not _awg57.is_on(1) and not _awg57.is_on(2)
    assert not any(e[0] == "FRQ_UNDER_LIVE_OUTPUT" for e in _awg57.log)
    # one frozen acquisition per shot serves both monitors: 3 measurements x
    # 16 shots, plus X1's alignment shot (X2 has no CmdX2 channel)
    assert _scope57.shots - _shots0 == 3 * 16 + 1, _scope57.shots - _shots0
    _ups = [e[1:] for e in _awg57.log if e[0] == "upload"]
    assert (1, "PAIRX1_i02") in _ups and (2, "PAIRX2_i02") in _ups
    for _k, _s in app.pair.items():
        assert _s.iteration == 2, (_k, _s.iteration)
        for _i in range(3):
            _f = os.path.join(ilc_gui.RUN_DIR, f"meas_{_s.stem}_i{_i:02d}.npy")
            assert os.path.exists(_f) and os.path.exists(_dg57.stats_path(_f)), _f
        _st = _ri57.load_state(_s.state_path)
        assert int(_st["iteration"]) == 2
        assert _st["history"][-1]["model"] == {"X1": "3rd order", "X2": "2nd order"}[_k]
    _log57 = app.log_text.get("1.0", "end")
    assert re.search(r"^X2\s+error: peak", _log57, re.M) and re.search(r"^X1\s+error: peak", _log57, re.M)
    assert "CH1 and CH2 output OFF (end of run)" in app.run_state.cget("text")

    # the shown switch: the Error plot redraws from the other session
    app.itersel_var.set("")
    app._redraw_iterations()
    root.update()

    def _err57(s):
        y = app._snaps_by_it(s)[2]["y"]
        return (s.loop.target - s.loop._seen(y)) * s.loop.channel.mon_scale
    assert np.allclose(app.ax_err.lines[1].get_ydata(), _err57(app.pair["X1"]))
    app.shown_var.set("X2")
    app._on_shown_change()
    root.update()
    assert app.session is app.pair["X2"] and "(X2 of a pair)" in app.summary.cget("text")
    assert "X2 of the pair" in app.status.cget("text")
    assert np.allclose(app.ax_err.lines[1].get_ydata(), _err57(app.pair["X2"]))
    assert not np.allclose(_err57(app.pair["X1"]), _err57(app.pair["X2"]))
    assert app.awgch_var.get() == "2" and app.scopech_var.get() == "4"   # single tools follow X2
    _gs = app._gather_settings()
    assert _gs["mode"] == "resonant" and set(_gs["pair"]) == {"X1", "X2"} and \
        _gs["pair"]["X1"]["mode"] == "third_order"

    # Hold on both: one run each from one acquisition per shot, no update
    app.holdruns_var.set("1")
    app.holdgap_var.set("0")
    app.holditer_var.set("")
    _n57 = len(_asked57)
    _shots0 = _scope57.shots
    app.do_hold()
    _pump57()
    assert _asked57[_n57:] == ["Outputs are OFF"], _asked57[_n57:]
    assert _scope57.shots - _shots0 == 16 and not _awg57.is_on(1) and not _awg57.is_on(2)
    for _k, _s in app.pair.items():
        assert os.path.exists(os.path.join(ilc_gui.RUN_DIR, f"meas_{_s.stem}_i02_r01.npy"))
        assert _s.iteration == 2 and int(_ri57.load_state(_s.state_path)["iteration"]) == 2
        assert any(sn.get("run") == 1 and sn["it"] == 2 for sn in _s.snapshots)

    # the X2 column's own fit: Fit to FRF fills X2's boxes and leaves X1's
    app.frf_var_2.set(os.path.join(REPO, "run", "frf_WIDE_X2.csv"))
    app.do_fit_frf(app._frf_paths(app._col2)[0], "resonant", 30e3, gain=0.6116, c=app._col2)
    _pump57()
    assert app.pgain_var_2.get() == "0.6116" and app.pfn_var_2.get() and \
        app.pfn_var.get() == "12564" and "WIDE_X2" in app.ax_frf[0].get_title()

    # Load state in pair mode brings both back, each column its own model
    app.model_var_2.set(ilc_gui.KEY2LABEL["static"])
    app._on_model_change_2()
    app.state_var.set(app.pair["X1"].state_path)
    app.do_load()
    root.update()
    assert app.pair["X2"].iteration == 2 and app.model_var_2.get() == ilc_gui.KEY2LABEL["resonant"]
    assert app.model_var.get() == ilc_gui.KEY2LABEL["third_order"] and app.stem_var.get() == "PAIR"
    assert len({sn["it"] for sn in app.pair["X2"].snapshots}) == 3

    # untick: single mode as before -- one session, one column, one channel
    app.pair_var.set(False)
    app._on_pair_toggle()
    root.update()
    assert app.pair is None and app._col2.frame.winfo_manager() == "" and \
        app._shown_frame.winfo_manager() == ""
    assert app.session.stem == "PAIRX1" and str(app._channel_cb.cget("state")) == "readonly"
    assert "pair" not in app._gather_settings()
    app.stem_var.set("SNGL")
    app.do_init()
    root.update()
    assert app.session.stem == "SNGL" and app.pair is None
    app.iters_var.set("1")
    _shots0 = _scope57.shots
    app.do_bench()
    _pump57()
    assert app.session.iteration == 1 and os.path.exists(os.path.join(ilc_gui.RUN_DIR, "meas_SNGL_i01.npy"))
    assert _scope57.shots - _shots0 == 2 * 16 + 1 and not _awg57.is_on(1)
    assert not any(e[1] == 2 and e[2].startswith("SNGL") for e in _awg57.log if e[0] == "upload")
finally:
    _mb57.askyesno, _mb57.showerror, _mb57.showinfo, ilc_gui.AWG_WAVEFORMS = _saved57
    for _a in ("_connect_pair", "_bench_modules"):
        app.__dict__.pop(_a, None)
print("[57] pair mode: X1/X2 Init from one target name (a name with no X1 refused), each "
      "state with its own model; Auto-set refused with an output ON, then both channels; "
      "two pair iterations from one acquisition per shot (49 shots), both meas + stats "
      "written, both at i02, outputs OFF; the shown switch redraws the Error plot from X2; "
      "pair Hold (16 shots, both r01); X2's own Fit to FRF; Load brings both back; untick -> "
      "single mode runs as before")

# [58] Optical tab (13 Oct 2026): the optical close by hand on both chains --
# 1 Play keepers, 2 Scan, 3 Compare, 4 Apply, 5 Learn, 6 Verify, 7 Outputs
# OFF. The ILC's loop runs on the simulated bench (as [57]); the light comes
# from eomilc.optical's simulated polarimeter, injected through
# app._optical_devices with rot_err 0.5 deg (else light - monitors is noise
# and the gate keeps nothing) and fed the drives that play. The simulated PD
# has no delay: PD delay 0. Stubs as [57], put back at the end; stands alone
# (smoke_block.py 58).
import math as _math58
import time as _time58
import run_ilc as _ri58
import ilc_bench as _ib58
from eomilc import simbench as _sb58
from eomilc import optical as _op58
from eomilc import corrections as _cm58
_mb58 = ilc_gui.messagebox
_saved58 = (_mb58.askyesno, _mb58.showerror, _mb58.showinfo, ilc_gui.AWG_WAVEFORMS)
_asked58 = []
_mb58.askyesno = lambda title, msg, **k: (_asked58.append(title), True)[1]
_mb58.showerror = lambda title, msg, **k: _asked58.append(("error", title, msg))
_mb58.showinfo = lambda title, msg, **k: _asked58.append(("info", title, msg))
ilc_gui.AWG_WAVEFORMS = os.path.join(SCRATCH, "Waveforms58")


def _pump58(limit_s=600):
    _t0 = _time58.time()
    root.update()
    while app.busy and _time58.time() - _t0 < limit_s:
        root.update()
        _time58.sleep(0.02)
    for _ in range(10):
        root.update()
        _time58.sleep(0.01)
    assert not app.busy, "the worker did not finish"


try:
    # without a pair every step is refused, by the reason
    app.pair_var.set(False)
    app._on_pair_toggle(quiet=True)
    root.update()
    assert all(str(app._opt_btns[i].cget("state")) == "disabled" for i in range(1, 8))
    app.do_opt_play()
    assert _asked58[-1][0] == "error" and "pair mode is off" in _asked58[-1][2], _asked58[-1]

    # the pair: the ILC header, the 2 us grid, 0 -> 5 kV at the EOM -> 0 over 11 ms
    _tus = np.arange(5501) * 2.0
    _ramp = np.clip(np.minimum((_tus - 500) / 3000, (10500 - _tus) / 3000), 0, 1)
    _v58 = 5000 * 0.5 * (1 - np.cos(np.pi * _ramp))
    for _k in ("X1", "X2"):
        np.savetxt(os.path.join(SCRATCH, f"target_OPT{_k}.csv"), np.c_[_tus, _v58],
                   delimiter=",", header="time_us,voltage_V", comments="", fmt="%.6g")
    app._set_channels(ilc_gui.chanmap.DEFAULT_CHANNELS)   # CH1 PD, CH2 CmdX1, CH3 MonX1, CH4 MonX2
    app.cmpsel_var.set("")
    app.line_on_var.set(False)
    app.pair_var.set(True)
    app._on_pair_toggle()
    root.update()
    app.target_var.set(os.path.join(SCRATCH, "target_OPTX1.csv"))
    app.stem_var.set("OPT")
    app.seed_var.set("")
    app.shotgain_var.set("")
    app.toff_var.set("0")
    app.fs_var.set("10.0")
    app.model_var.set(ilc_gui.KEY2LABEL["resonant"])
    app._on_model_change()
    for _var, _val in ((app.pgain_var, "0.557"), (app.pfn_var, "7823"), (app.pzeta_var, "1.474"),
                       (app.gamma_var, "0.6"), (app.fcut_var, "20000"), (app.notch_var, "")):
        _var.set(_val)
    app.model_var_2.set(ilc_gui.KEY2LABEL["resonant"])
    app._on_model_change_2()
    for _var, _val in ((app.pgain_var_2, "0.6116"), (app.pfn_var_2, "8509"), (app.pzeta_var_2, "1.83"),
                       (app.gamma_var_2, "0.6"), (app.fcut_var_2, "20000"), (app.notch_var_2, "")):
        _var.set(_val)
    _n58 = len(_asked58)
    app.do_init()
    root.update()
    _p58 = app.pair
    assert _p58 is not None and (_p58["X1"].stem, _p58["X2"].stem) == ("OPTX1", "OPTX2"), _asked58[_n58:]

    # the ILC's simulated bench, as [57]; Auto-set puts FRQ on the record
    _awg58, _scope58, _mod58 = _sb58.make_bench({1: os.path.join(REPO, "run", "frf_WIDE_X1.csv"),
                                                 2: os.path.join(REPO, "run", "frf_WIDE_X2.csv")},
                                                channels=app.cfg)

    def _fake_pair58():
        _ib58._AWGMOD = _mod58
        return _awg58, _scope58
    app._connect_pair = _fake_pair58
    app._bench_modules = lambda: (None, _mod58)
    app.do_autoset()
    _pump58()
    assert not _awg58.is_on(1) and not _awg58.is_on(2)

    # the polarimeter's simulated devices: a fresh bench per scan, its AWG
    # fed by the scan step with the drives that play. Its monitors are the
    # drive x a static gain (the polarimeter's calibration, 0.5594 / 0.5924):
    # given the DC gains of the chains the loop learns on here instead, the
    # light at the hold is the light of the drive the loop converges to
    _dc58 = {_c: float(np.mean(_ch.respond(np.ones(5501), 2e-6)[-500:]))
             for _c, _ch in _scope58.chains.items()}
    _devs58 = []

    def _optdev58(scope, cfg):
        _d = _op58.open_devices(None, cfg, log=print, simulate=True, pcfg={})
        _d.sim.rot_err = 0.5
        _d.sim.awg_gain = dict(_dc58)
        _devs58.append(_d)
        return _d
    app._optical_devices = _optdev58
    app.optangles_var.set("0:359:45")
    app.optshots_var.set("2")
    app.optdelay_var.set("0")
    app.optiters_var.set("2")
    app.optgain_var.set("0.7")
    app.optfigs_var.set(False)
    app.repeats_var.set("16")
    app.wait_var.set("5")
    app.skip_var.set(False)
    root.update()

    # step 2 before step 1: greyed, and refused by the reason
    assert str(app._opt_btns[1].cget("state")) == "normal"
    assert all(str(app._opt_btns[i].cget("state")) == "disabled" for i in range(2, 7))
    app.do_opt_scan()
    assert _asked58[-1][0] == "error" and "needs step 1" in _asked58[-1][2], _asked58[-1]

    # 1 Play keepers: one question, both ON, the i00 drives uploaded, kept ON
    _n58 = len(_asked58)
    app.do_opt_play()
    _pump58()
    assert _asked58[_n58:] == ["Outputs are OFF"], _asked58[_n58:]
    assert _awg58.is_on(1) and _awg58.is_on(2)
    _ups = [e[1:] for e in _awg58.log if e[0] == "upload"]
    assert (1, "OPTX1_i00") in _ups and (2, "OPTX2_i00") in _ups
    assert "kept ON until step 7" in app.run_state.cget("text")
    assert app._opt["outputs"][0] == "ON" and "outputs  ON" in app.opt_state.cget("text")

    # 2 Scan: the default name, the folder, the drives that play fed to the light
    assert app.optname_var.get() == "OPT_opt1", app.optname_var.get()
    assert str(app._opt_btns[2].cget("state")) == "normal"
    app.do_opt_scan()
    _pump58()
    _scan58 = app._opt["scan"]
    assert _scan58 and os.path.isfile(os.path.join(_scan58, "OPT_opt1_scan.json")), _scan58
    assert os.path.dirname(_scan58) == os.path.join(ilc_gui.RUN_DIR, "optical")
    assert app._opt_scan_complete(_scan58)
    assert np.array_equal(_devs58[-1].drives["EO1"], _p58["X1"].u) and \
        np.array_equal(_devs58[-1].drives["EO2"], _p58["X2"].u)
    assert _awg58.is_on(1) and _awg58.is_on(2)                    # the scan left them ON
    assert app.optname_var.get() == "OPT_opt2"                     # past the complete scan

    # 3 Compare: both correction files, the verdict, the Optical plot tab
    app.do_opt_compare()
    _pump58()
    _sum58 = app._opt["compare"]
    assert _sum58 is not None and _sum58["folder"] == _scan58
    for _ch in ("EO1", "EO2"):
        _c = _cm58.read_correction(_sum58["files"][_ch])
        assert _c.meta.get("channel") == _ch and np.max(np.abs(_c.delta)) > 1.0, _ch
    assert all(len(_ax.lines) > 0 for _ax in app.ax_opt) and len(app.ax_optcorr.lines) >= 2
    assert app.nb.select() == str(app._views["Optical"]["frame"])
    assert "correction peak" in app.log_text.get("1.0", "end")

    # 4 Apply: both targets moved by the gated amount, base kept, one layer
    _t58 = {_k: np.array(_s.loop.target) for _k, _s in _p58.items()}
    app.do_opt_apply()
    _pump58()
    for _k, _s in _p58.items():
        _sc = _s.loop.channel.mon_scale
        assert np.array_equal(_s.base_target, _t58[_k]), _k
        assert np.max(np.abs(_s.loop.target - _t58[_k])) * _sc > 1.0, _k
        assert np.allclose(_s.loop.target, _s.base_target + _s.corr_total)
        assert len(_s.corr_layers) == 1 and _s.corr_layers[0]["slot"] == "optical" and \
            _s.corr_layers[0]["status"] == "applied" and _s.corr_layers[0]["gain"] == 0.7
        assert _s.corr_layers[0]["path"] == os.path.abspath(_sum58["files"][ilc_gui.PAIR_CHANNEL[_k]])
        _st = _ri58.load_state(_s.state_path)
        assert np.array_equal(_st["base_target"], _t58[_k]) and "corrections" in _st
    # the Corrections tab lists the shown session's layer
    _rows = [app.corr_tv.item(_i, "values") for _i in app.corr_tv.get_children()]
    assert len(_rows) == 1 and _rows[0][2] == "optical" and _rows[0][-1] == "applied", _rows
    assert app.ax_optcorr.lines[-1].get_linestyle() == "--"        # what is on the target now

    # 5 Learn: two iterations of the pair loop, no question, outputs never switched
    _ev58 = len(_awg58.log)
    _it58 = {_k: _s.iteration for _k, _s in _p58.items()}
    _n58 = len(_asked58)
    app.do_opt_learn()
    _pump58()
    assert _asked58[_n58:] == [], _asked58[_n58:]
    assert [e for e in _awg58.log[_ev58:] if e[0] == "output"] == []
    assert _awg58.is_on(1) and _awg58.is_on(2)
    for _k, _s in _p58.items():
        assert _s.iteration == _it58[_k] + 2, (_k, _s.iteration)
        assert int(_ri58.load_state(_s.state_path)["iteration"]) == _it58[_k] + 2
        assert app._pair_playing[_k]["wname"] == f"{_s.stem}_i{_s.iteration:02d}"
    assert "outputs left ON for the Optical steps" in app.log_text.get("1.0", "end")

    # 6 Verify: a second scan + compare, the before / after table, the third view
    assert str(app._opt_btns[6].cget("state")) == "normal"
    app.do_opt_verify()
    _pump58()
    _ver58 = app._opt["verify"]
    assert _ver58 and os.path.isfile(os.path.join(_ver58, "OPT_opt1_verify_scan.json")), _ver58
    _tab58 = app._opt["table"]
    assert _tab58 and all(_math58.isfinite(_b) and _math58.isfinite(_a) for _l, _b, _a in _tab58)
    assert any("ramp top, light - target" in _l for _l, _b, _a in _tab58)
    assert len(app.ax_optva.lines) >= 3
    assert "ramp-top light - target" in app.status.cget("text") and \
        "halved" in app.status.cget("text"), app.status.cget("text")
    assert "before / after: before = OPT_opt1, after = OPT_opt1_verify" in app.log_text.get("1.0", "end")
    assert _awg58.is_on(1) and _awg58.is_on(2)
    # the verification is the next pass's comparison: Apply is open again
    assert str(app._opt_btns[4].cget("state")) == "normal"
    assert str(app._opt_btns[5].cget("state")) == "disabled"

    # 7 Outputs OFF: both, the Bench line and the state line say so
    _ev58 = len(_awg58.log)
    app.do_opt_off()
    _pump58()
    assert not _awg58.is_on(1) and not _awg58.is_on(2)
    assert [e for e in _awg58.log[_ev58:] if e[0] == "output"] == [("output", 1, False),
                                                                    ("output", 2, False)]
    assert "CH1 and CH2 output OFF (Optical step 7)" in app.run_state.cget("text")
    assert app._opt["outputs"][0] == "OFF" and "outputs  OFF" in app.opt_state.cget("text")
    assert str(app._opt_btns[2].cget("state")) == "disabled"
    for _l, _b, _a in _tab58:
        print(f"       {_l:40s}{_b:9.1f}{_a:9.1f}")
finally:
    _mb58.askyesno, _mb58.showerror, _mb58.showinfo, ilc_gui.AWG_WAVEFORMS = _saved58
    for _a in ("_connect_pair", "_bench_modules", "_optical_devices"):
        app.__dict__.pop(_a, None)
print("[58] optical tab: refused without a pair and out of order; 1 plays both keepers ON (one "
      "question); 2 scans OPT_opt1 into run\\optical with the playing drives as the light; 3 "
      "compares (EO1/EO2 corr files, plot tab drawn); 4 moves both targets, base kept, one optical "
      "layer, Corrections list shows it; 5 learns 2 iterations with no output event; 6 verifies "
      "(finite before/after, status verdict); 7 both OFF")

# [59] Spin-echo record from the Session tab (13 Oct 2026): Echo... beside the
# Target box builds target_<stem><suffix>X1/X2.csv from a one-transport pair
# (eomilc.echo, the same builder as tools/make_echo_target.py and a batch
# campaign's "echo" key), checks both channels, and leaves the new X1 in the
# Target box with its name in the stem box. A generated pair in SCRATCH, as
# block 57's (NOT named '[57]' at a line start: the block runner's regex would
# cut this block there). Dialogs stubbed and put back; stands alone
# (smoke_block.py 59).
_mb59 = ilc_gui.messagebox
_saved59 = (_mb59.showerror, _mb59.showwarning, _mb59.showinfo)
_said59 = []
_mb59.showerror = lambda title, msg, **k: _said59.append(("error", msg))
_mb59.showwarning = lambda title, msg, **k: _said59.append(("warning", msg))
_mb59.showinfo = lambda title, msg, **k: _said59.append(("info", msg))
try:
    _tus = np.arange(5501) * 2.0
    _ramp = np.clip(np.minimum((_tus - 500) / 3000, (10500 - _tus) / 3000), 0, 1)
    _v59 = 5000 * 0.5 * (1 - np.cos(np.pi * _ramp))
    _d59 = os.path.join(SCRATCH, "echo59")
    os.makedirs(_d59, exist_ok=True)
    for _k in ("X1", "X2"):
        np.savetxt(os.path.join(_d59, f"target_ECHO{_k}.csv"), np.c_[_tus, _v59 * (1.0 if _k == "X1" else 0.9)],
                   delimiter=",", header="time_us,voltage_V", comments="", fmt="%.6g")
    app.target_var.set(os.path.join(_d59, "target_ECHOX1.csv"))
    app.do_echo_dialog()
    root.update()
    assert app._echo_win.winfo_exists() and app._echo_vars["source"].get().endswith("target_ECHOX1.csv")
    assert app._echo_vars["spacing"].get() == "16.667" and app._echo_vars["legs"].get() == "2"
    assert app.do_echo_dialog() is None and app._echo_win.winfo_exists()      # one at a time: lifted
    # a spacing shorter than the transport is refused, nothing written
    app._echo_vars["spacing"].set("5")
    _n0 = len(app.log_text.get("1.0", "end"))
    assert app._echo_build() is None and _said59[-1][0] == "error" and "not longer than the transport" in _said59[-1][1]
    assert not glob.glob(os.path.join(_d59, "target_ECHOE2*"))
    # 12 ms spacing: the transport is the first to last sample OFF the idle,
    # 502..10498 us (500 and 10500 are exact zeros), so lead 251 pts | 4999 |
    # idle to 6251 | 4999 | tail 100: 11350 pts, legs at 0.502 and 12.502 ms
    app._echo_vars["spacing"].set("12")
    _r59 = app._echo_build()
    root.update()
    assert _r59 is not None and _r59["ok"] and sorted(_r59["files"]) == ["X1", "X2"]
    assert os.path.exists(os.path.join(_d59, "target_ECHOE2X1.csv")) and os.path.exists(os.path.join(_d59, "target_ECHOE2X2.csv"))
    assert _r59["rec"]["n"] == 251 + 6000 + 4999 + 100 and abs(_r59["rec"]["spacing_ms"] - 12.0) < 1e-9, \
        (_r59["rec"]["n"], _r59["rec"]["spacing_ms"], _r59["rec"]["i0"], _r59["rec"]["span_us"])
    assert _r59["rec"]["legs_ms"] == [0.502, 12.502]
    _t59, _y59 = ilc_gui.run_ilc.load_target(os.path.join(_d59, "target_ECHOE2X2.csv"))
    assert len(_t59) == _r59["rec"]["n"] and abs(_y59.max() - 4.5) < 1e-6      # X2's 4.5 kV, monitor volts
    assert np.allclose(_y59[251:5250], _y59[6251:11250]) and np.all(_y59[5250:6251] == 0)
    assert app.target_var.get() == os.path.join(_d59, "target_ECHOE2X1.csv") and app.stem_var.get() == "ECHOE2"
    # ECHOE2 is 6 characters: over the pair mode's 5 -> a warning, the record still written
    assert _said59[-1][0] == "warning" and "6 characters" in _said59[-1][1]
    _log59 = app.log_text.get("1.0", "end")[_n0:]
    assert "wrote" in _log59 and "check X1 (EO1): PASS" in _log59 and "check X2 (EO2): PASS" in _log59
    assert "card  : pause between legs" in _log59 and "legs  : 2 at 0.502, 12.502 ms" in _log59
    assert app.cfg["echo"]["spacing_ms"] == 12.0 and app.cfg["echo"]["legs"] == 2
    # a 5-character name: no warning; an identical file on disk is left alone
    app._echo_vars["suffix"].set("E")
    _said59.clear()
    _r59 = app._echo_build()
    root.update()
    assert _r59["name"] == "ECHOE" and not _said59 and app.stem_var.get() == "ECHOE"
    _m59 = os.path.getmtime(os.path.join(_d59, "target_ECHOEX1.csv"))
    _r59b = app._echo_build()
    assert not any(_r59b["written"].values()) and os.path.getmtime(os.path.join(_d59, "target_ECHOEX1.csv")) == _m59
    # the batch plan's "echo" key builds the same files from the campaign's stem
    import ilc_batch as _ib59
    import json as _json59
    _plan59 = os.path.join(_d59, "plan.json")
    _json59.dump({"name": "e59", "targets_dir": _d59, "campaigns": [{"stem": "ECHO", "echo": {"spacing_ms": 12, "suffix": "E"}}]},
                 open(_plan59, "w"))
    _p59 = _ib59.load_plan(_plan59, cfg=app.cfg)
    assert _p59["campaigns"][0]["stem"] == "ECHOE" and _p59["campaigns"][0]["_echo"]["source"] == "ECHO"
    assert _p59["_echo_notes"] and "already there" in _p59["_echo_notes"][0] and "card pause" in _p59["_echo_notes"][0]
    _msgs59 = []
    _ib59.dry_run(_p59, log=_msgs59.append)
    assert any(m.startswith("echo ECHOE: spin echo of ECHO, 2 legs at 0.502, 12.502 ms") for m in _msgs59)
    assert any(m.strip().startswith("ECHOE") and "11350 pts" in m for m in _msgs59), _msgs59
    app._echo_win.destroy()
finally:
    _mb59.showerror, _mb59.showwarning, _mb59.showinfo = _saved59
print("[59] spin-echo record: Echo... dialog refuses a too-short spacing, builds ECHOE2 (11350 pts, "
      "legs 0.502/12.502 ms, both channels PASS, Target box + stem set, 6-character stem warned), "
      "ECHOE without a warning, an unchanged file is left alone; the batch's echo key builds the "
      "same files and the dry run prints the record")

# [60] Optical tab, solo play (13 Oct 2026, the design addendum's part 6):
# each crystal measured alone with the other parked flat at its idle (PARK1 /
# PARK2 on its channel, output ON), each solo scan compared with solo= so the
# whole correction is the driving crystal's, the two files held side by side
# and applied together, Learn (both play again), and a JOINT verification
# that has no joint before to put in a table. The same stubs and simulated
# devices as block 58's, its own pair (stem SOL); stands alone
# (smoke_block.py 60).
import json as _json60
import time as _time60
import run_ilc as _ri60
import ilc_bench as _ib60
from eomilc import simbench as _sb60
from eomilc import optical as _op60
from eomilc import corrections as _cm60
_mb60 = ilc_gui.messagebox
_saved60 = (_mb60.askyesno, _mb60.showerror, _mb60.showinfo, ilc_gui.AWG_WAVEFORMS)
_asked60 = []
_mb60.askyesno = lambda title, msg, **k: (_asked60.append(title), True)[1]
_mb60.showerror = lambda title, msg, **k: _asked60.append(("error", title, msg))
_mb60.showinfo = lambda title, msg, **k: _asked60.append(("info", title, msg))
ilc_gui.AWG_WAVEFORMS = os.path.join(SCRATCH, "Waveforms60")


def _pump60(limit_s=600):
    _t0 = _time60.time()
    root.update()
    while app.busy and _time60.time() - _t0 < limit_s:
        root.update()
        _time60.sleep(0.02)
    for _ in range(10):
        root.update()
        _time60.sleep(0.01)
    assert not app.busy, "the worker did not finish"


def _log60():
    return app.log_text.get("1.0", "end")


try:
    # the pair, as block 58's: 0 -> 5 kV at the EOM -> 0 over 11 ms on the 2 us grid
    _tus = np.arange(5501) * 2.0
    _ramp = np.clip(np.minimum((_tus - 500) / 3000, (10500 - _tus) / 3000), 0, 1)
    _v60 = 5000 * 0.5 * (1 - np.cos(np.pi * _ramp))
    for _k in ("X1", "X2"):
        np.savetxt(os.path.join(SCRATCH, f"target_SOL{_k}.csv"), np.c_[_tus, _v60],
                   delimiter=",", header="time_us,voltage_V", comments="", fmt="%.6g")
    app._set_channels(ilc_gui.chanmap.DEFAULT_CHANNELS)
    app.cmpsel_var.set("")
    app.line_on_var.set(False)
    app.pair_var.set(True)
    app._on_pair_toggle()
    root.update()
    app.target_var.set(os.path.join(SCRATCH, "target_SOLX1.csv"))
    app.stem_var.set("SOL")
    app.seed_var.set("")
    app.shotgain_var.set("")
    app.toff_var.set("0")
    app.fs_var.set("10.0")
    app.model_var.set(ilc_gui.KEY2LABEL["resonant"])
    app._on_model_change()
    for _var, _val in ((app.pgain_var, "0.557"), (app.pfn_var, "7823"), (app.pzeta_var, "1.474"),
                       (app.gamma_var, "0.6"), (app.fcut_var, "20000"), (app.notch_var, "")):
        _var.set(_val)
    app.model_var_2.set(ilc_gui.KEY2LABEL["resonant"])
    app._on_model_change_2()
    for _var, _val in ((app.pgain_var_2, "0.6116"), (app.pfn_var_2, "8509"), (app.pzeta_var_2, "1.83"),
                       (app.gamma_var_2, "0.6"), (app.fcut_var_2, "20000"), (app.notch_var_2, "")):
        _var.set(_val)
    _n60 = len(_asked60)
    app.do_init()
    root.update()
    _p60 = app.pair
    assert _p60 is not None and (_p60["X1"].stem, _p60["X2"].stem) == ("SOLX1", "SOLX2"), _asked60[_n60:]

    _awg60, _scope60, _mod60 = _sb60.make_bench({1: os.path.join(REPO, "run", "frf_WIDE_X1.csv"),
                                                 2: os.path.join(REPO, "run", "frf_WIDE_X2.csv")},
                                                channels=app.cfg)

    def _fake_pair60():
        _ib60._AWGMOD = _mod60
        return _awg60, _scope60
    app._connect_pair = _fake_pair60
    app._bench_modules = lambda: (None, _mod60)
    app.do_autoset()
    _pump60()
    assert not _awg60.is_on(1) and not _awg60.is_on(2)

    _dc60 = {_c: float(np.mean(_ch.respond(np.ones(5501), 2e-6)[-500:]))
             for _c, _ch in _scope60.chains.items()}
    _devs60 = []

    def _optdev60(scope, cfg):
        _d = _op60.open_devices(None, cfg, log=print, simulate=True, pcfg={})
        _d.sim.rot_err = 0.5
        _d.sim.awg_gain = dict(_dc60)
        _devs60.append(_d)
        return _d
    app._optical_devices = _optdev60
    app.optangles_var.set("0:359:45")
    app.optshots_var.set("2")
    app.optdelay_var.set("0")
    app.optiters_var.set("1")
    app.optgain_var.set("0.7")
    app.optfigs_var.set(False)
    app.repeats_var.set("16")
    app.wait_var.set("5")
    app.skip_var.set(False)
    root.update()

    # 1, X1 alone: X1's keeper on CH1, PARK2 (flat at X2's idle) on CH2, both ON
    _ev60 = len(_awg60.log)
    _n60 = len(_asked60)
    app.optplay_var.set("X1")
    app.do_opt_play()
    _pump60()
    assert _asked60[_n60:] == ["Outputs are OFF"], _asked60[_n60:]
    assert _awg60.is_on(1) and _awg60.is_on(2)
    _ups = [e[1:] for e in _awg60.log[_ev60:] if e[0] == "upload"]
    assert _ups == [(1, "SOLX1_i00"), (2, "PARK2")], _ups
    _pk60 = app._pair_playing["X2"]
    assert _pk60.get("parked") and _pk60["wname"] == "PARK2"
    assert len(_pk60["u"]) == len(_p60["X2"].u) and np.all(_pk60["u"] == _p60["X2"].u[0])
    assert not app._pair_playing["X1"].get("parked")
    assert app._opt["playing"] == "X1"
    assert "X1 alone, X2 parked" in app.opt_state.cget("text"), app.opt_state.cget("text")
    assert "X1 keeper SOLX1_i00, X2 parked (PARK2)" in app._opt["done"][1], app._opt["done"][1]
    assert "X1 keeper SOLX1_i00, X2 parked (PARK2)" in _log60()

    # 2: the default name ends _X1; the light is X1's keeper with X2 parked
    assert app.optname_var.get() == "SOL_opt1_X1", app.optname_var.get()
    app.do_opt_scan()
    _pump60()
    _scanx1 = app._opt["scan"]
    assert _scanx1 and os.path.basename(_scanx1) == "SOL_opt1_X1" and app._opt_scan_complete(_scanx1)
    assert app._opt["scan_solo"] == "X1"
    assert np.array_equal(_devs60[-1].drives["EO1"], _p60["X1"].u) and \
        np.all(_devs60[-1].drives["EO2"] == _p60["X2"].u[0])
    _man = _json60.load(open(os.path.join(_scanx1, "SOL_opt1_X1_scan.json"), encoding="utf-8"))
    assert _man["solo"] == "X1" and _man["drive"]["idle_V"]["EO2"] == float(_p60["X2"].u[0])

    # 3, solo X1: EO1's file only, the state line says so, no joint before yet
    app.do_opt_compare()
    _pump60()
    _o = app._opt
    assert _o["compare"]["solo"] == "X1" and set(_o["compare"]["files"]) == {"EO1"}
    assert set(_o["files"]) == {"EO1"} and _o["files_from"] == {"EO1": "SOL_opt1_X1"}, _o["files_from"]
    _eo1 = _o["files"]["EO1"]
    assert _cm60.read_correction(_eo1).meta.get("channel") == "EO1"
    assert _o["before"] is None
    assert "corrections: EO1 from SOL_opt1_X1" in app.opt_state.cget("text"), app.opt_state.cget("text")
    assert "X1 drove alone" in _log60()

    # 6 is refused while one crystal plays alone, by that reason
    assert str(app._opt_btns[6].cget("state")) == "disabled"
    app.do_opt_verify()
    assert _asked60[-1][0] == "error" and "Verify needs both playing" in _asked60[-1][2], _asked60[-1]

    # 1, X2 alone: PARK1 on CH1, X2's keeper on CH2; the outputs are already ON
    _ev60 = len(_awg60.log)
    _n60 = len(_asked60)
    app.optplay_var.set("X2")
    app.do_opt_play()
    _pump60()
    assert _asked60[_n60:] == [], _asked60[_n60:]
    _ups = [e[1:] for e in _awg60.log[_ev60:] if e[0] == "upload"]
    assert _ups == [(1, "PARK1"), (2, "SOLX2_i00")], _ups
    assert [e for e in _awg60.log[_ev60:] if e[0] == "output"] == []
    assert app._pair_playing["X1"].get("parked") and not app._pair_playing["X2"].get("parked")
    assert "X2 alone, X1 parked" in app.opt_state.cget("text")

    # 2 and 3, solo X2: both files held now, EO1's kept as it was
    assert app.optname_var.get() == "SOL_opt1_X2", app.optname_var.get()
    app.do_opt_scan()
    _pump60()
    assert os.path.basename(app._opt["scan"]) == "SOL_opt1_X2" and app._opt["scan_solo"] == "X2"
    assert np.all(_devs60[-1].drives["EO1"] == _p60["X1"].u[0]) and \
        np.array_equal(_devs60[-1].drives["EO2"], _p60["X2"].u)
    app.do_opt_compare()
    _pump60()
    _o = app._opt
    assert _o["compare"]["solo"] == "X2" and set(_o["compare"]["files"]) == {"EO2"}
    assert set(_o["files"]) == {"EO1", "EO2"} and _o["files"]["EO1"] == _eo1
    assert _o["files_from"] == {"EO1": "SOL_opt1_X1", "EO2": "SOL_opt1_X2"}, _o["files_from"]
    assert _o["before"] is None
    assert "EO1 from SOL_opt1_X1 held" in _o["done"][3] and "(from SOL_opt1_X2)" in _o["done"][3], \
        _o["done"][3]
    assert "corrections: EO1 from SOL_opt1_X1, EO2 from SOL_opt1_X2" in app.opt_state.cget("text")

    # 4: both targets moved, one optical layer each, from its own scan
    _t60 = {_k: np.array(_s.loop.target) for _k, _s in _p60.items()}
    _n0 = len(_log60())
    app.do_opt_apply()
    _pump60()
    for _k, _s in _p60.items():
        _ch = ilc_gui.PAIR_CHANNEL[_k]
        assert np.array_equal(_s.base_target, _t60[_k]), _k
        assert np.max(np.abs(_s.loop.target - _t60[_k])) * _s.loop.channel.mon_scale > 1.0, _k
        assert len(_s.corr_layers) == 1 and _s.corr_layers[0]["slot"] == "optical" and \
            _s.corr_layers[0]["status"] == "applied", _s.corr_layers
        assert _s.corr_layers[0]["path"] == os.path.abspath(app._opt["files"][_ch])
        assert np.array_equal(_ri60.load_state(_s.state_path)["base_target"], _t60[_k])
    _lg = _log60()[_n0:]
    assert "EO1's file from SOL_opt1_X1 to X1" in _lg and "EO2's file from SOL_opt1_X2 to X2" in _lg, _lg
    assert "both targets moved" in _lg

    # 5: one iteration of the pair loop -- both keepers upload, so both play again
    _ev60 = len(_awg60.log)
    _n60 = len(_asked60)
    app.do_opt_learn()
    _pump60()
    assert _asked60[_n60:] == [], _asked60[_n60:]
    assert [e for e in _awg60.log[_ev60:] if e[0] == "output"] == []
    _ups = [e[1:] for e in _awg60.log[_ev60:] if e[0] == "upload"]
    assert (1, "SOLX1_i00") in _ups and (2, "SOLX2_i00") in _ups, _ups
    assert app._opt["playing"] == "both" and app._opt["learned"]
    assert "playing both" in app.opt_state.cget("text")

    # 6: a JOINT scan named after the pass; no joint before -> no table, the verdict logged
    assert str(app._opt_btns[6].cget("state")) == "normal"
    _n0 = len(_log60())
    app.do_opt_verify()
    _pump60()
    _ver60 = app._opt["verify"]
    assert _ver60 and os.path.basename(_ver60) == "SOL_opt1_verify", _ver60
    _man = _json60.load(open(os.path.join(_ver60, "SOL_opt1_verify_scan.json"), encoding="utf-8"))
    assert _man["solo"] is None
    assert app._opt["table"] is None and app._opt["compare"]["solo"] is None
    _lg = _log60()[_n0:]
    assert "no joint before" in _lg and "table skipped" in _lg and "before / after" not in _lg, _lg
    assert "ramp top light-monitors" in _lg                       # the verdict lines
    assert app._opt["before"][0] == "SOL_opt1_verify"
    assert "no joint before" in app.status.cget("text"), app.status.cget("text")
    assert set(app._opt["files"]) == {"EO1", "EO2"} and \
        set(app._opt["files_from"].values()) == {"SOL_opt1_verify"}
    assert _awg60.is_on(1) and _awg60.is_on(2)

    # 7: both OFF
    _ev60 = len(_awg60.log)
    app.do_opt_off()
    _pump60()
    assert not _awg60.is_on(1) and not _awg60.is_on(2)
    assert [e for e in _awg60.log[_ev60:] if e[0] == "output"] == [("output", 1, False),
                                                                    ("output", 2, False)]
    assert app._opt["outputs"][0] == "OFF"
finally:
    app.optplay_var.set("both")
    _mb60.askyesno, _mb60.showerror, _mb60.showinfo, ilc_gui.AWG_WAVEFORMS = _saved60
    for _a in ("_connect_pair", "_bench_modules", "_optical_devices"):
        app.__dict__.pop(_a, None)
print("[60] optical solo play: X1 alone (SOLX1_i00 on CH1, PARK2 on CH2, X2 parked) -> scan "
      "SOL_opt1_X1 -> compare holds EO1 only, no joint before, Verify refused while solo; X2 alone "
      "(PARK1 on CH1, no question) -> SOL_opt1_X2 -> both files held, EO1's kept; apply moves both "
      "targets, one optical layer each, log names both scans; learn 1 -> playing both; verify "
      "SOL_opt1_verify joint, no table, verdict logged; both OFF")

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
                      ("frf", app.fig_frf), ("corr", app.fig_corr),
                      ("opt", app.fig_opt), ("optcorr", app.fig_optcorr),
                      ("optva", app.fig_optva)):
        fig.savefig(os.path.join(SCRATCH, f"fig_{name}.png"), dpi=100)
    print("[10b] figure PNGs saved from the canvases")
except Exception:
    traceback.print_exc()
    print("[10b] figure PNGs FAILED (non-fatal)")

root.destroy()
print("ALL CHECKS PASSED")
