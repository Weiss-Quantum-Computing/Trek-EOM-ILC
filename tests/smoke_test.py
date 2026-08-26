"""Offline regression suite for ilc_gui.py, against real MKJX1 campaign data.

38 numbered checks: state round-trips, the span guard, the model ladder,
the GEN from-scratch path, flat first shot, hold-run display, plot
overlays, compare-stem overlays, drive spectrum, spectrum
averaging, the native-rate spectrum and its bench-kept files, the FRF
probe + measurement maths, the iteration
table, dot density, linked time axes. No instruments are touched --
bench/auto-set/upload/hold hardware paths are exercised on the bench, not
here. A Tk window flashes briefly; screenshots of every tab land in the
scratch folder for eyeballing.

Run with the Anaconda interpreter:

    C:\\ProgramData\\anaconda3\\python.exe -u tests\\smoke_test.py

Everything the GUI writes is redirected into a per-run scratch folder in
%TEMP% (reset on every start), so the real run/ state and the AWG
Waveforms library are never touched.
"""
import os, shutil, sys, glob, tempfile, traceback
import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.join(tempfile.gettempdir(), "eom-ilc-smoke")
if os.path.isdir(SCRATCH):
    shutil.rmtree(SCRATCH)
os.makedirs(SCRATCH)
shutil.copy(os.path.join(REPO, "run", "drive_MKJX1.state.npz"), SCRATCH)
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
MEAS = sorted(glob.glob(os.path.join(REPO, "run", "meas_MKJX1_i*.npy")))[-1]

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
print("---- GUI log ----")
print(app.log_text.get("1.0", "end"))
print("-----------------")
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

# init a brand-new session from the real target into scratch dirs
app.target_var.set(os.path.join(REPO, "waveforms", "target_MKJX1.csv"))
app.channel_var.set("EO1")
app.stem_var.set("TSTX1")
app.do_init()
root.update()
print(f"[9] init: {app.session.stem} iter {app.session.iteration}, "
      f"drive peak {np.abs(app.session.u).max():.3f} V")
assert os.path.exists(os.path.join(ilc_gui.RUN_DIR, "drive_TSTX1.state.npz"))

# go back to the stepped MKJX1 session so the error/spectrum/convergence
# tabs have content, then take a second step so the ghost overlay shows;
# real meas files beside the state exercise the recall-on-load path too
import shutil
for f in sorted(glob.glob(os.path.join(REPO, "run", "meas_MKJX1_i*.npy")))[-2:]:
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

# model ladder: a gain-only step, parameters from the calibration tables
app.model_var.set("gain only (0th order)")
app._update_model_fields()
app.do_calib()
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

# FRF tab with the second-order calibration model overlaid
app.model_var.set("second order (resonant)")
app._update_model_fields()
app.do_calib()
app.do_show_frf()
root.update()
print("[14] FRF overlay drawn")

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
app.do_calib()          # must refuse: GEN has no tables
app.do_init()           # must refuse: no gain typed, no tables to fall back on
assert not os.path.exists(os.path.join(ilc_gui.RUN_DIR,
                                       "drive_GENX.state.npz")), \
    "init built a GEN state without any model information"
print("[15] GEN refused calibration and blind init, as it must")

app.pgain_var.set("0.5")
app.do_init()
root.update()
s = app.session
assert s.channel == "GEN" and s.loop.plant.gain == 0.5
assert s.loop.plant.fn == 0 and s.loop.plant.tau == 0
assert s.loop.channel.mon_scale == 1.0
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

# drive-corrections spectrum: same shape as the error spectrum, base
# measurements only, each stem against its own reference drive
labs = [l.get_label() for l in app.ax_dspec.get_lines()]
assert any(l.startswith("iter ") for l in labs), labs
assert "GENX iter 1" in labs, labs           # drive_GENX_i01.csv exists
assert "GENX iter 0" not in labs, labs       # the init drive was never stored
assert not any(" r" in l for l in labs), \
    f"hold runs leaked into the drive spectrum: {labs}"
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

# spectrum averaging: a pure tone keeps its amplitude in both modes (the
# normalisation invariant), and the Welch mode actually smooths
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
    def waveform(self, ch): return tf2 + sN.t_off, fine2
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
print(f"[37] FRF probe: {len(bins)} tones 400 Hz - {bins[-1]/recG/1e3:.0f} "
      f"kHz on the session grid, Nyquist ceiling enforced")

# FRF measurement maths: a fake scope plays the probe through a known
# one-pole plant; the fitted H must match the analytic transfer
tau_p = 2.2e-6
frG = np.fft.rfftfreq(nG, dtG)
Htrue = 1.0 / (1.0 + 2j * np.pi * frG * tau_p)
y_p = np.fft.irfft(np.fft.rfft(u_p) * Htrue, n=nG)

class _FrfScope:
    def single(self, wait_s=None): return True
    def waveform(self, ch):
        return (sN.t + sN.t_off, u_p if ch == 1 else y_p)
    def run(self): pass

app.stop_evt.clear()
H_m, coh_m = app._frf_capture(_FrfScope(), 1, 3, bins, sN.t, sN.t_off,
                              repeats=3, wait_s=1, settle=0)
assert np.allclose(H_m, Htrue[bins], rtol=1e-6), \
    f"fitted H off by {np.abs(H_m - Htrue[bins]).max():.2e}"
assert np.all(coh_m > 0.999), "identical shots must cohere"
fpath = os.path.join(ilc_gui.RUN_DIR, "frf_AUTOTST.csv")
ilc_gui.write_frf_csv(fpath, bins / recG, H_m, coh_m)
app.fuse_var.set("100e3"); app.fmax_var.set("150e3")
app._adopt_frf(fpath); root.update()
assert app.frf_var.get() == fpath, "FRF field not pointed at the result"
frf_obj = ilc_gui.ilc.FRF(fpath, f_use=100e3, f_max=150e3)
print(f"[38] FRF maths: one-pole plant recovered exactly at {len(bins)} "
      f"tones, CSV loads as an ilc.FRF, field adopted + drawn")

# screenshot each tab for visual inspection
root.geometry("1380x880+40+40")
root.update()
try:
    from PIL import ImageGrab
    import time
    root.lift(); root.update(); time.sleep(0.4)
    for i, name in enumerate(["waveforms", "dcorr", "dspec", "ddelta",
                              "error", "spectrum", "convergence",
                              "table", "frf"]):
        app.nb.select(i)
        root.update(); time.sleep(0.25); root.update()
        x, ypos = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        ImageGrab.grab(bbox=(x, ypos, x + w, ypos + h)).save(
            os.path.join(SCRATCH, f"shot_{i}_{name}.png"))
    print("[10] screenshots saved")
except Exception:
    traceback.print_exc()
    print("[10] screenshots FAILED (non-fatal)")

root.destroy()
print("ALL CHECKS PASSED")
