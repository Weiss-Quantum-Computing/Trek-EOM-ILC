#!/usr/bin/env python3
"""Train a list of ramp targets through the ILC, X1 and X2 together, one after another.

    python ilc_batch.py make-plan --targets <suite>/targets --stems S47,S25,C25 \
        --x1-from run/drive_P92PX1H.state.npz --x2-from run/drive_P92PX2A.state.npz \
        --name tier1 --out <suite>/batch_tier1.json
    python ilc_batch.py run <suite>/batch_tier1.json --dry-run
    python ilc_batch.py run <suite>/batch_tier1.json --simulate
    python ilc_batch.py run <suite>/batch_tier1.json --allow-output-on

The GUI's Batch window (ilc_gui.py, "Batch..." in the Bench loop box) builds,
checks, simulates and runs the same plans through run_batch() below.

A plan names target pairs (target_<stem>X1.csv, target_<stem>X2.csv, the layout
the ramp suites use) and how to train them. Each pair is ONE campaign: both
drives play at once from the two AWG channels, both monitors are read from the
same frozen acquisitions (CH3 and CH4), and each channel's loop updates from its
own monitor. That halves the bench time against running X1 and X2 separately,
and it is how the ramps play in the experiment -- the series and shared ramps
only make sense with both channels moving.

Everything a campaign leaves behind is what the GUI's bench loop leaves: a
state file per channel (run/drive_<stem>X1.state.npz, same keys as
ilc_gui.save_session, so "Load state" opens it), a drive CSV per iteration
(run/drive_<stem>X1_iNN.csv) and a measurement per iteration
(run/meas_<stem>X1_iNN.npy). A campaign interrupted for any reason resumes from
those states on the next run, earlier measurements included (the keeper and
the divergence check see the whole campaign, not just the part since the
resume). A simulated run writes into <run_dir>/sim instead, so its states can
never be resumed as if they had been measured on the bench.

SAFETY POSTURE -- the GUI's rules, applied without a person at every step
--------------------------------------------------------------------------
  * FRQ moves only with BOTH outputs OFF. Every campaign has its own record
    length, so FRQ changes between campaigns; the batch switches both outputs
    off, sets FRQ, and reads the channel back before anything plays.
  * Switching ON is the direction that puts voltage into something. The batch
    does it only with --allow-output-on, after one typed confirmation for the
    whole plan (or --yes), and only after the channel check below passed.
  * Both outputs go OFF between campaigns, at the end, and on any error or
    Ctrl-C (the finally block does it before closing the instruments).
  * AMP, OFST, load, sample clock and burst setup are never written. Set them
    once with the GUI's Auto-set; ilc_bench.check_awg_channel verifies them
    before every campaign and the campaign is refused if they are wrong.
  * Every drive passes Loop.check (the Trek limits in eomilc.config) before
    it is uploaded; a drive that fails stops that channel's learning.

GENERATOR MEMORY
----------------
The 4063B has no remote delete (bk4063b.py, read_waveform notes): stored
waveforms can only be removed from the front panel. The GUI's per-iteration
names (<stem>_iNN) would leave ~40 new waveforms per campaign in user memory,
so the batch uploads into two alternating slots per channel (BX1A/BX1B,
BX2A/BX2B) -- never overwriting the one that is playing -- and keeps the
per-iteration record on disk, where the GUI expects it. --gui-names restores
the GUI's naming if you want every iteration in the generator.

STOPPING, PER CHANNEL
---------------------
A channel stops learning (and keeps playing its last drive while the other
finishes) at the first of: `max_iterations`; rms error <= `stop_rms_v` after
`min_iterations`; ilc.plateau reporting the loop re-learning noise; the
noise-floor check finding nothing left to learn (after `min_iterations`); rms
more than `diverge_factor` x its best for 3 measurements in a row (it then
reverts to its best drive); or a drive failing the limit check. The keeper is
the measured iteration with the lowest rms error at or after `min_iterations`
(or overall, if it stopped before that).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from dataclasses import dataclass, field

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from eomilc.config import CHANNELS                              # noqa: E402

RUN_DIR = os.path.join(HERE, "run")
SLOTS = ("A", "B")

DEFAULT_WIRING = {
    "X1": dict(channel="EO1", awg_ch=1, drive_ch=1, mon_ch=3),
    "X2": dict(channel="EO2", awg_ch=2, drive_ch=2, mon_ch=4),
}
DEFAULT_LOOP = dict(max_iterations=20, min_iterations=8, stop_rms_v=0.8,
                    plateau=True, noise_floor_stop=True, diverge_factor=2.0,
                    repeats=64, wait_s=30.0, dither_codes=3, settle_s=0.5,
                    t_offset_us=0.0, full_scale=10.0, first_shot="flat")
# Rough bench timing for the plan's estimate only; nothing is derived from it.
# ilc_bench.capture_all: 64 HRES singles on two channels ~17 s at the 3.7 Hz trigger.
DEFAULT_TIMING = dict(trigger_hz=3.7, read_s_per_channel=0.02, upload_s=2.5,
                      campaign_overhead_s=25.0)
DEFAULT_SETTINGS = {
    # The production inverse of the 25-26 Aug campaign (README: measured FRF,
    # taper 50-75 kHz). The September keepers (P92/S92) used other rungs --
    # prefer "from_state" pointing at those states, so a new ramp is trained
    # the way the last good ones were.
    "X1": dict(model="frf", frf="run/frf_WIDE_X1.csv", f_use=50e3, f_max=75e3,
               gamma=0.6, f_cut=40e3),
    "X2": dict(model="frf", frf="run/frf_WIDE_X2.csv", f_use=50e3, f_max=75e3,
               gamma=0.6, f_cut=40e3),
}


class BatchError(RuntimeError):
    """A campaign cannot go on; the batch records it and moves to the next."""


class BatchStopped(Exception):
    """Stop was asked for (the GUI's Stop): the batch ends after switching the
    outputs off. States are saved every iteration, so the next run resumes."""


def _stopped(stop):
    if stop is not None and stop.is_set():
        raise BatchStopped("stopped")


# ------------------------------------------------------------------ the plan
def load_plan(path):
    with open(path) as f:
        plan = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    plan["_path"] = os.path.abspath(path)
    plan["_targets_dir"] = os.path.normpath(os.path.join(base, plan.get("targets_dir", "targets")))
    plan["_run_dir"] = os.path.normpath(os.path.join(HERE, plan.get("run_dir", "run")))
    plan["loop"] = {**DEFAULT_LOOP, **plan.get("loop", {})}
    plan["timing"] = {**DEFAULT_TIMING, **plan.get("timing", {})}
    wiring = {k: {**DEFAULT_WIRING[k], **plan.get("wiring", {}).get(k, {})} for k in DEFAULT_WIRING}
    plan["wiring"] = wiring
    plan["settings"] = {k: plan.get("settings", {}).get(k) or DEFAULT_SETTINGS[k] for k in DEFAULT_WIRING}
    if not plan.get("campaigns"):
        raise SystemExit(f"{path}: no campaigns")
    return plan


def repo_path(p):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(HERE, p))


def target_stems(folder):
    """Stems with both target_<stem>X1.csv and target_<stem>X2.csv in `folder`."""
    names = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    return sorted(n[len("target_"):-len("X1.csv")] for n in names
                  if n.startswith("target_") and n.endswith("X1.csv")
                  and n[:-len("X1.csv")] + "X2.csv" in names)


def build_plan(targets, stems, out, name, x1_from="", x2_from="", card=0, loop=None):
    """The plan dict for `stems` (run order) whose targets are in `targets`,
    written to `out`. Raises ValueError for a stem without both targets or two
    stems that would share file names."""
    have = set(target_stems(targets))
    missing = [s for s in stems if s not in have]
    if missing:
        raise ValueError(f"no target_<stem>X1/X2.csv pair in {targets} for: {', '.join(missing)}")
    if len(set(stems)) != len(stems):
        raise ValueError("a stem is listed twice")
    settings = {}
    for key, src in (("X1", x1_from), ("X2", x2_from)):
        if src:
            settings[key] = {"from_state": src}
    camps, used = [], {}
    for s in stems:
        c = {"stem": s}
        if len(s) + 2 > 7:                 # <stem>X1 must leave room for _iNN
            c["awg"] = s[:5]
        short = c.get("awg", s)
        if short in used:
            raise ValueError(f"{s} and {used[short]} would both train as {short}X1/X2 "
                             f"(stems are cut to 5 characters); rename one target pair")
        used[short] = s
        camps.append(c)
    plan = {"name": name, "targets_dir": os.path.relpath(targets, os.path.dirname(os.path.abspath(out))),
            "settings": settings, "loop": dict(loop or {}), "campaigns": camps}
    if card:
        plan["card"] = {"n": int(card), "settle_us": 200}
    with open(out, "w") as f:
        json.dump(plan, f, indent=1)
    return plan


def make_plan(a):
    stems = [s.strip() for s in a.stems.split(",") if s.strip()]
    try:
        plan = build_plan(a.targets, stems, a.out, a.name, a.x1_from, a.x2_from, a.card)
    except ValueError as e:
        sys.exit(str(e))
    print(f"wrote {a.out}: {len(plan['campaigns'])} campaigns")


# ------------------------------------------------------------- one channel
@dataclass
class ChannelRun:
    key: str                  # "X1" / "X2"
    wiring: dict
    stem: str                 # state/iteration name, e.g. "S25X1"
    loop: object
    t: np.ndarray
    u: np.ndarray
    iteration: int
    full_scale: float
    t_off: float
    state_path: str
    target_path: str
    model_key: str = ""
    frf_path: str = ""
    frf_use: float = 0.0
    frf_max: float = 0.0
    active: bool = True
    reason: str = ""
    measured: list = field(default_factory=list)   # [(it, rms_v, peak_v)]
    drives: dict = field(default_factory=dict)     # it -> u
    run_id: float = 0.0
    bad_streak: int = 0

    @property
    def channel(self):
        return self.loop.channel


def _settings_for(plan, key):
    """(plant params, loop params, model record) for one channel."""
    import run_ilc
    s = plan["settings"][key]
    if "from_state" in s:
        p = repo_path(s["from_state"])
        if not os.path.exists(p):
            raise BatchError(f"{key} is trained like {s['from_state']}, which is not on this machine")
        st = run_ilc.load_state(p)
        rec = dict(model=str(st["model"]) if "model" in st else "",
                   frf=str(st["frf_path"]) if "frf_path" in st else "",
                   f_use=float(st["frf_use"]) if "frf_use" in st else 0.0,
                   f_max=float(st["frf_max"]) if "frf_max" in st else 0.0)
        plant = dict(gain=float(st["gain"]), tau=float(st["tau"]), tau2=float(st.get("tau2", 0.0)),
                     fn=float(st.get("fn", 0.0)), zeta=float(st.get("zeta", 0.0)),
                     offset=float(st["offset"]))
        loopp = dict(gamma=float(st["gamma"]), f_cut=float(st["f_cut"]),
                     notches=run_ilc.state_notches(st))
        src = f"from {os.path.basename(p)}"
    else:
        rec = dict(model=s.get("model", ""), frf=s.get("frf", ""),
                   f_use=float(s.get("f_use", 0.0)), f_max=float(s.get("f_max", 0.0)))
        prm = s.get("params", {})
        plant = dict(gain=prm.get("gain"), tau=prm.get("tau", 0.0) * 1e-6, tau2=0.0,
                     fn=prm.get("fn", 0.0), zeta=prm.get("zeta", 0.0), offset=0.0)
        loopp = dict(gamma=float(s.get("gamma", 0.6)), f_cut=float(s.get("f_cut", 40e3)),
                     notches=tuple(tuple(n) for n in s.get("notches", ())))
        src = "from the plan"
    if rec["model"] == "frf":
        rec["frf"] = repo_path(rec["frf"])
        if not os.path.exists(rec["frf"]):
            raise BatchError(f"settings for {key} ({src}) use the measured FRF {rec['frf']}, "
                             f"which does not exist on this machine")
    return plant, loopp, rec, src


def build_run(plan, camp, key, log=print):
    """A ChannelRun for one channel of a campaign: resumed from its state if one
    exists, otherwise fresh with the flat first shot."""
    import run_ilc
    from eomilc import ilc, plant as plantmod
    w = plan["wiring"][key]
    ch = CHANNELS[w["channel"]]
    stem = f"{camp.get('awg', camp['stem'])}{key}"
    tpath = os.path.join(plan["_targets_dir"], f"target_{camp['stem']}{key}.csv")
    if not os.path.exists(tpath):
        raise BatchError(f"{tpath} does not exist")
    t, v = run_ilc.load_target(tpath, ch.mon_scale)
    dt = float(np.median(np.diff(t)))
    state_path = os.path.join(plan["_run_dir"], f"drive_{stem}.state.npz")
    plant_p, loop_p, rec, src = _settings_for(plan, key)
    lp = plan["loop"]
    if os.path.exists(state_path):
        st = run_ilc.load_state(state_path)
        if len(st["target"]) != len(v) or not np.allclose(st["target"], v, rtol=1e-6, atol=1e-9):
            raise BatchError(f"{state_path} exists but holds a different target than {tpath}; "
                             f"move it aside or rename the campaign")
        loop = run_ilc.build_loop(st)
        u, k = np.asarray(st["u"], float), int(st["iteration"])
        rec = dict(model=str(st["model"]) if "model" in st else rec["model"],
                   frf=str(st["frf_path"]) if "frf_path" in st else rec["frf"],
                   f_use=float(st["frf_use"]) if "frf_use" in st else rec["f_use"],
                   f_max=float(st["frf_max"]) if "frf_max" in st else rec["f_max"])
        log(f"  {stem}: resuming {os.path.basename(state_path)} at iteration {k}")
        resumed = True
    else:
        base = ch.plant(float(np.ptp(v)), dt, model="resonant")
        gain = plant_p["gain"] if plant_p["gain"] else base.gain
        p = plantmod.Plant(gain=gain, tau=plant_p["tau"], offset=plant_p["offset"],
                           tau2=plant_p["tau2"], fn=plant_p["fn"], zeta=plant_p["zeta"], dt=dt)
        if rec["model"] != "frf" and not (p.tau or p.fn):
            p = base                                      # a parametric rung with nothing given
        loop = ilc.Loop(plant=p, target=v, dt=dt, channel=ch, gamma=loop_p["gamma"],
                        f_cut=loop_p["f_cut"], limits=ch.limits, notches=loop_p["notches"])
        u = loop.first_shot(flat=lp["first_shot"] == "flat")
        k = 0
        log(f"  {stem}: fresh, settings {src}; {p}")
        resumed = False
    if rec["model"] == "frf":
        loop.frf = ilc.FRF(rec["frf"], f_use=rec["f_use"], f_max=rec["f_max"])
    cr = ChannelRun(key=key, wiring=w, stem=stem, loop=loop, t=t, u=u, iteration=k,
                    full_scale=float(lp["full_scale"]), t_off=float(lp["t_offset_us"]) * 1e-6,
                    state_path=state_path, target_path=os.path.abspath(tpath),
                    model_key=rec["model"], frf_path=rec["frf"], frf_use=rec["f_use"], frf_max=rec["f_max"])
    if resumed:
        restore_measured(cr, plan["_run_dir"])
    return cr


def restore_measured(cr, run_dir):
    """A resumed campaign's earlier measurements (meas_<stem>_iNN.npy and the
    drive CSV that played each), so the keeper and the divergence check see
    the whole campaign. Pairs that are missing or on another grid are skipped,
    as ilc_gui.recall_snapshots does."""
    import pandas as pd
    for it in range(cr.iteration):
        mp = os.path.join(run_dir, f"meas_{cr.stem}_i{it:02d}.npy")
        dp = os.path.join(run_dir, f"drive_{cr.stem}_i{it:02d}.csv")
        if not (os.path.exists(mp) and os.path.exists(dp)):
            continue
        y = np.load(mp)
        u = pd.read_csv(dp, comment="#").iloc[:, 1].to_numpy(float)
        if len(y) != len(cr.t) or len(u) != len(cr.t):
            continue
        m = cr.loop.metrics(y)
        cr.measured.append((it, m["rms_err_hv"], m["peak_err_hv"]))
        cr.drives[it] = u


def save_state(cr):
    """The keys ilc_gui.save_session writes, so the GUI and CLIs load it."""
    lp = cr.loop
    os.makedirs(os.path.dirname(cr.state_path), exist_ok=True)
    # Written beside and renamed over: the GUI's Batch window reloads this file
    # after every iteration to follow the campaign, and must never read half
    # of it. np.savez given an open file does not append ".npz".
    tmp = cr.state_path + ".tmp"
    with open(tmp, "wb") as f:
        _savez_state(f, cr, lp)
    for k in range(5):
        try:
            os.replace(tmp, cr.state_path)
            break
        except PermissionError:          # Windows: a reader still has it open
            if k == 4:
                raise
            time.sleep(0.2)


def _savez_state(f, cr, lp):
    np.savez(f, t=cr.t, target=lp.target, u=cr.u, dt=lp.dt,
             channel=lp.channel.name, gain=lp.plant.gain, tau=lp.plant.tau,
             offset=lp.plant.offset, tau2=lp.plant.tau2, fn=lp.plant.fn,
             zeta=lp.plant.zeta, full_scale=cr.full_scale, name=cr.stem,
             gamma=lp.gamma, f_cut=lp.f_cut, iteration=cr.iteration,
             t_offset=cr.t_off, history=np.array(lp.history, dtype=object),
             model=cr.model_key, frf_path=cr.frf_path, frf_use=cr.frf_use,
             frf_max=cr.frf_max, seed_path="", target_path=cr.target_path,
             notches=np.asarray(lp.notches, float).reshape(-1, 2))


def write_iteration(cr, run_dir):
    from eomilc import outputs
    wname = f"{cr.stem}_i{cr.iteration:02d}"
    outputs.write_awg_csv(os.path.join(run_dir, f"drive_{wname}.csv"), cr.t, cr.u,
                          comment=f"{cr.loop.channel.name} ILC {wname} (ilc_batch)\n{cr.loop.plant}")
    return wname


# ----------------------------------------------------------- instruments
def nice_setting(value):
    """Same 1-1.5-2-2.5-3-4-5-7.5-10 ladder as ilc_gui.nice_setting."""
    e = 10.0 ** np.floor(np.log10(value))
    for m in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if m * e >= value * (1 - 1e-9):
            return m * e
    return 10 * e


def outputs_off(awg, runs, log=print):
    for cr in runs:
        c = cr.wiring["awg_ch"]
        if awg.is_on(c):
            awg.set_output(c, False)
            log(f"  CH{c} output OFF")
        if awg.is_on(c):
            raise BatchError(f"CH{c} did not switch off")


_SLOT = {}     # per AWG channel, across the whole batch: the next slot to fill


def upload(awg, cr, gui_names, log=print):
    import ilc_bench
    if gui_names:
        name = f"{cr.stem}_i{cr.iteration:02d}"
    else:
        c = cr.wiring["awg_ch"]
        k = _SLOT.get((id(awg), c), 0)
        name = f"B{cr.key}{SLOTS[k % 2]}"
        _SLOT[(id(awg), c)] = k + 1
    n, frac = ilc_bench.upload_drive(awg, cr.wiring["awg_ch"], name, cr.u, cr.full_scale)
    return name, n, frac


def prepare_campaign(awg, scope, runs, plan, gui_names, allow_on, log=print):
    """Outputs off -> FRQ -> scope window and verticals -> first uploads ->
    channel checks -> outputs on -> alignment. Raises BatchError to refuse."""
    import ilc_bench
    period = len(runs[0].t) * runs[0].loop.dt
    outputs_off(awg, runs, log)
    for cr in runs:
        c = cr.wiring["awg_ch"]
        if awg.is_on(c):
            raise BatchError(f"CH{c} is still on; FRQ must not move under a live output")
        missed = awg.apply_channel(c, {"BSWV": {"FRQ": 1.0 / period}}, log=lambda m: None)
        if missed:
            raise BatchError(f"CH{c}: FRQ did not take ({missed})")
    log(f"  AWG FRQ {1.0/period:.6g} Hz on both channels (record {period*1e3:.4f} ms), outputs off")
    rng = nice_setting(1.3 * period)
    scope.put(":TIMebase:RANGe", f"{rng:.6g}")
    scope.put(":TIMebase:POSition", f"{period/2:.6g}")
    for cr in runs:
        for ch, lo, hi, what in ((cr.wiring["drive_ch"], cr.u.min(), cr.u.max(), "drive"),
                                 (cr.wiring["mon_ch"], cr.loop.target.min(), cr.loop.target.max(), "monitor")):
            scope.put(f":CHANnel{ch}:DISPlay", "1")
            scope.put(f":CHANnel{ch}:COUPling", "DC")
            span = max(float(hi - lo), 1e-3)
            scope.put(f":CHANnel{ch}:SCALe", f"{nice_setting(1.25 * span / 8):.6g}")
            scope.put(f":CHANnel{ch}:OFFSet", f"{0.5 * float(hi + lo):.6g}")
    log(f"  scope window {rng*1e3:.4g} ms, verticals set for drives and monitors (DC)")
    acq = scope.get(":ACQuire:TYPE")
    if not str(acq).upper().startswith("HRES"):
        raise BatchError(f"scope acquisition is {acq}; the loop needs HRES (see ilc_bench)")
    for cr in runs:
        rep = cr.loop.check(cr.u)
        if not rep:
            raise BatchError(f"{cr.stem}: drive i{cr.iteration:02d} fails the limit check: {rep}")
        name, n, frac = upload(awg, cr, gui_names, log)
        log(f"  {cr.stem} i{cr.iteration:02d} uploaded as {name} ({n} pts, {100*frac:.1f}% of DAC range)")
    for cr in runs:
        problems, notes = ilc_bench.check_awg_channel(
            awg, cr.wiring["awg_ch"], full_scale=cr.full_scale, expect_period=period)
        if problems:
            raise BatchError(f"CH{cr.wiring['awg_ch']} setup: " + "; ".join(problems))
    if not allow_on:
        raise BatchError("outputs are off and this run may not switch them on "
                         "(pass --allow-output-on)")
    for cr in runs:
        awg.set_output(cr.wiring["awg_ch"], True)
        if not awg.is_on(cr.wiring["awg_ch"]):
            raise BatchError(f"CH{cr.wiring['awg_ch']} did not switch on")
    log("  outputs ON: " + ", ".join(f"CH{cr.wiring['awg_ch']}" for cr in runs))
    time.sleep(plan["loop"]["settle_s"])
    check_alignment(scope, runs, plan, log)


def check_alignment(scope, runs, plan, log=print):
    """One shot of both DRIVE channels from one frozen acquisition, each
    cross-correlated against the drive it should be playing. A stale t-offset,
    a swapped cable or a channel that did not start would otherwise train
    both loops on the wrong thing."""
    from eomilc import scope as scopeio
    if scope.single(wait_s=plan["loop"]["wait_s"]) is not True:
        raise BatchError("alignment check: no trigger -- is the burst running?")
    got = {}
    for cr in runs:
        pts = scopeio.scope_points_for(2.2 * len(cr.t))
        got[cr.key] = scope.waveform(cr.wiring["drive_ch"], points=pts)
    scope.run()
    for cr in runs:
        ts, vs = got[cr.key]
        if float(np.ptp(vs)) < 0.5:
            raise BatchError(f"alignment check: scope CH{cr.wiring['drive_ch']} is flat -- "
                             f"is CH{cr.wiring['awg_ch']} really driving it?")
        meas = scopeio.measure_t_offset(np.asarray(ts), np.asarray(vs), cr.u, cr.loop.dt)
        if abs(meas - cr.t_off) > 10e-6:
            raise BatchError(f"alignment check: {cr.key} drive starts {meas*1e6:+.1f} us, "
                             f"t-offset says {cr.t_off*1e6:+.1f} us")
        log(f"  alignment {cr.key}: drive starts {meas*1e6:+.1f} us -- OK")


def read_waveform(scope, ch, points, tries=3):
    """scope.waveform with the GUI's retry (ilc_gui._read_waveform): an empty
    :WAVeform:DATA? block re-reads the same frozen acquisition."""
    for k in range(tries):
        try:
            return scope.waveform(ch, points=points)
        except Exception as e:
            if k == tries - 1:
                raise
            print(f"  CH{ch} readout failed ({type(e).__name__}: {e}); re-reading")
            try:
                scope.inst.clear()
            except Exception:
                pass


ADC_CODE_PER_VDIV = float(os.environ.get("EOMILC_ADC_CODE_PER_VDIV", 0.04025))   # as ilc_gui


def capture_monitors(scope, runs, plan, log=print, stop=None):
    """`repeats` shots, every active channel's monitor read from the same frozen
    acquisition, with the GUI's offset dither (ilc_gui._bench_capture): each
    channel's offset steps across `dither_codes` ADC codes over the shots so the
    converter's per-code error pattern averages out instead of being learned.
    Offsets are restored however the capture ends. Returns {key: (mean, stack)}."""
    from eomilc import scope as scopeio
    lp = plan["loop"]
    repeats, codes = int(lp["repeats"]), int(lp["dither_codes"])
    time.sleep(lp["settle_s"])
    plan_d = []
    for cr in runs:
        c = cr.wiring["mon_ch"]
        sc, off = scope.try_get(f":CHANnel{c}:SCALe"), scope.try_get(f":CHANnel{c}:OFFSet")
        if codes and sc is not None and off is not None:
            plan_d.append((c, float(off), ADC_CODE_PER_VDIV * float(sc) * codes))
    stacks = {cr.key: [] for cr in runs}
    try:
        for i in range(repeats):
            _stopped(stop)
            for c, off0, span in plan_d:
                scope.put(f":CHANnel{c}:OFFSet", f"{off0 + span * ((i + 0.5) / repeats - 0.5):.6g}")
            if scope.single(wait_s=lp["wait_s"]) is not True:
                raise BatchError(f"no trigger within {lp['wait_s']:g} s on repeat {i+1}")
            shot = {}
            for cr in runs:
                pts = scopeio.scope_points_for(2.2 * len(cr.t))
                shot[cr.key] = read_waveform(scope, cr.wiring["mon_ch"], pts)
            scope.run()
            for cr in runs:
                ts, vs = shot[cr.key]
                stacks[cr.key].append(scopeio.resample(np.asarray(ts), np.asarray(vs), cr.t, t_offset=cr.t_off))
    finally:
        for c, off0, _ in plan_d:
            try:
                scope.put(f":CHANnel{c}:OFFSet", f"{off0:.6g}")
            except Exception as e:
                log(f"  could not restore CH{c} offset: {e}")
    from eomilc import ilc
    return {k: (ilc.averaged(v), np.asarray(v)) for k, v in stacks.items()}


# ---------------------------------------------------------------- campaign
def decide(cr, m, stack, lp, log):
    """Record a measurement and decide whether this channel keeps learning."""
    from eomilc import ilc
    k = cr.iteration
    cr.measured.append((k, m["rms_err_hv"], m["peak_err_hv"]))
    cr.drives[k] = cr.u.copy()
    best = min(r[1] for r in cr.measured)
    if m["rms_err_hv"] > lp["diverge_factor"] * best:
        cr.bad_streak += 1
    else:
        cr.bad_streak = 0
    f_top = cr.loop.frf.f_max if cr.loop.frf is not None else cr.loop.f_cut
    nothing_left = False
    try:
        nb = ilc.learnable_band(cr.loop.target, stack, cr.loop.dt, f_top)
        nothing_left = nb["f_floor"] is not None and nb["f_floor"] <= 0
    except ValueError:
        pass
    if cr.bad_streak >= 3:
        return "diverged"
    if k >= lp["max_iterations"]:
        return "max iterations"
    if k >= lp["min_iterations"] and m["rms_err_hv"] <= lp["stop_rms_v"]:
        return "converged"
    if k >= lp["min_iterations"] and lp["noise_floor_stop"] and nothing_left:
        return "noise floor"
    if lp["plateau"]:
        p = ilc.plateau(cr.loop.history, run_id=cr.run_id)
        if p and p["flat"] and k >= lp["min_iterations"]:
            return "plateau"
    return ""


def keeper(cr, lp):
    late = [r for r in cr.measured if r[0] >= lp["min_iterations"]] or cr.measured
    return min(late, key=lambda r: r[1]) if late else None


def run_campaign(awg, scope, plan, camp, args, log=print, stop=None, emit=lambda kind, **kw: None):
    from eomilc import ilc, outputs
    lp = plan["loop"]
    keys = camp.get("channels", ["X1", "X2"])
    runs = [build_run(plan, camp, k, log) for k in keys]
    n = {len(cr.t) for cr in runs}
    if len(n) != 1 or len({round(float(cr.loop.dt), 12) for cr in runs}) != 1:
        raise BatchError("X1 and X2 targets are not on one grid -- they must play together")
    for cr in runs:
        save_state(cr)                                   # the GUI can open it from now on
        cr.run_id = time.time()
    prepare_campaign(awg, scope, runs, plan, args.gui_names, args.allow_output_on, log)
    for cr in runs:
        write_iteration(cr, plan["_run_dir"])
    resumed_at = {cr.key: cr.iteration for cr in runs}
    states = {cr.key: cr.state_path for cr in runs}
    while any(cr.active for cr in runs):
        _stopped(stop)
        caps = capture_monitors(scope, runs, plan, log, stop)
        changed = []
        for cr in runs:
            y, stack = caps[cr.key]
            if not cr.active:
                continue
            m = cr.loop.metrics(y)
            np.save(os.path.join(plan["_run_dir"], f"meas_{cr.stem}_i{cr.iteration:02d}.npy"), y)
            why = decide(cr, m, stack, lp, log)
            log(f"  {cr.stem} i{cr.iteration:02d}: rms {m['rms_err_hv']:6.2f} V  peak {m['peak_err_hv']:7.1f} V"
                + (f"  -> stop ({why})" if why else ""))
            emit("iteration", stem=camp["stem"], key=cr.key, it=cr.iteration, rms_v=m["rms_err_hv"],
                 peak_v=m["peak_err_hv"], stop=why, states=states)
            if why:
                cr.active, cr.reason = False, why
                cr.loop.history.append({**m, "run_id": cr.run_id})
                if why == "diverged":
                    kb = keeper(cr, lp)
                    cr.u = cr.drives[kb[0]].copy()
                    changed.append(cr)
                    log(f"  {cr.stem}: back to its best drive, i{kb[0]:02d}")
                save_state(cr)
                continue
            u_new = cr.loop.update(cr.u, y)
            cr.loop.history[-1]["update_rms"] = ilc.update_rms(cr.u, u_new)
            cr.loop.history[-1]["run_id"] = cr.run_id
            rep = cr.loop.check(u_new)
            if not rep:
                cr.active, cr.reason = False, f"limit check: {rep}"
                log(f"  {cr.stem}: the next drive fails the limit check ({rep}); stopping this channel")
                save_state(cr)
                continue
            cr.u, cr.iteration = u_new, cr.iteration + 1
            save_state(cr)
            write_iteration(cr, plan["_run_dir"])
            changed.append(cr)
        for cr in changed:
            upload(awg, cr, args.gui_names, log)
        # after the saves: a follower reloading the state sees this iteration
        emit("saved", stem=camp["stem"], states=states)
    outputs_off(awg, runs, log)
    result = {"channels": {}}
    kept, targets = {}, {}
    for cr in runs:
        targets[cr.key] = cr.target_path
        kb = keeper(cr, lp)
        u_keep = cr.drives[kb[0]]
        kdir = os.path.join(plan["_run_dir"], "keep")
        os.makedirs(kdir, exist_ok=True)
        kpath = os.path.join(kdir, f"drive_{cr.stem}_keep.csv")
        outputs.write_awg_csv(kpath, cr.t, u_keep,
                              comment=f"{cr.loop.channel.name} keeper: {cr.stem} i{kb[0]:02d}, rms {kb[1]:.2f} V, "
                                      f"peak {kb[2]:.1f} V (ilc_batch, stopped: {cr.reason})")
        kept[cr.key] = kpath
        result["channels"][cr.key] = dict(state=cr.state_path, stop=cr.reason, iterations=cr.iteration,
                                          resumed_at=resumed_at[cr.key],
                                          keeper_iteration=kb[0], keeper_rms_v=kb[1], keeper_peak_v=kb[2],
                                          keeper_file=kpath,
                                          history=[dict(it=a, rms_v=b, peak_v=c) for a, b, c in cr.measured])
        log(f"  {cr.stem}: keeper i{kb[0]:02d} (rms {kb[1]:.2f} V, peak {kb[2]:.1f} V), stopped: {cr.reason}")
    if plan.get("card") and len(kept) == 2:
        try:
            result["card"] = card_files(kept, targets, plan["card"], log)
        except (ValueError, OSError) as e:          # the training stands; only the export failed
            result["card_error"] = str(e)
            log(f"  card files NOT written: {e}")
    return result


def card_files(kept, targets, card, log=print):
    """The experiment card's up/down files from the keeper pair, both channels
    cut at the same times (tools/split_for_card.py)."""
    import importlib.util
    p = os.path.join(HERE, "tools", "split_for_card.py")
    spec = importlib.util.spec_from_file_location("split_for_card", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.split_pair([kept["X1"], kept["X2"]], n=int(card.get("n", 2500)),
                          settle_us=float(card.get("settle_us", 200)), log=log,
                          targets=[targets["X1"], targets["X2"]])


# --------------------------------------------------------------- estimates
def estimate(plan, n_points, ncamp_channels=2):
    tm, lp = plan["timing"], plan["loop"]
    per_it = (lp["repeats"] * (1.0 / tm["trigger_hz"] + tm["read_s_per_channel"] * ncamp_channels)
              + tm["upload_s"] * ncamp_channels)
    return tm["campaign_overhead_s"] + per_it * (lp["max_iterations"] + 1), per_it


def plan_rows(plan):
    """One row per campaign: everything checkable without instruments (targets,
    grids, settings, first drives against the limits, names) plus the time
    estimate. The GUI's table and dry_run both print from this."""
    rows = []
    for camp in plan["campaigns"]:
        keys = camp.get("channels", ["X1", "X2"])
        row = dict(stem=camp["stem"], ok=False, error="", n=0, period_ms=0.0, est_s=0.0, lines=[])
        rows.append(row)
        try:
            runs = [build_run(plan, camp, k, log=lambda s: None) for k in keys]
        except (BatchError, ValueError, OSError) as e:
            row["error"] = str(e)
            continue
        n = len(runs[0].t)
        ok = True
        for cr in runs:
            rep = cr.loop.check(cr.u)
            ok &= bool(rep)
            wn = f"{cr.stem}_i{cr.iteration:02d}"
            row["lines"].append(
                f"{cr.key} {cr.channel.name}: target {np.ptp(cr.loop.target)*cr.channel.mon_scale:5.0f} V, "
                f"drive i{cr.iteration:02d} peak {np.abs(cr.u).max():.3f} V, limits {'PASS' if rep else 'FAIL: ' + str(rep)}"
                + ("" if len(wn) <= 11 else f", GUI name {wn} is {len(wn)} chars (fine with slots)"))
            if not rep:
                row["error"] = f"{cr.key} first drive fails the limits: {rep}"
        row.update(ok=ok, n=n, period_ms=n * runs[0].loop.dt * 1e3,
                   est_s=estimate(plan, n, len(runs))[0],
                   start_it={cr.key: cr.iteration for cr in runs})
    return rows


def dry_run(plan, log=print):
    lp = plan["loop"]
    log(f"plan {plan.get('name', '')}: {len(plan['campaigns'])} campaigns, targets in {plan['_targets_dir']}")
    log(f"loop: {lp['max_iterations']} iterations max (min {lp['min_iterations']}), stop at rms <= {lp['stop_rms_v']} V, "
        f"{lp['repeats']} shots per measurement")
    rows = plan_rows(plan)
    for r in rows:
        if not r["n"]:
            log(f"  {r['stem']:8s} REFUSED: {r['error']}")
            continue
        log(f"  {r['stem']:8s} {r['n']:5d} pts, {r['period_ms']:7.3f} ms (FRQ {1e3/r['period_ms']:8.3f} Hz), "
            f"~{r['est_s']/60:4.1f} min | " + " | ".join(r["lines"]))
    bad = sum(not r["ok"] for r in rows)
    log(f"total ~{sum(r['est_s'] for r in rows)/3600:.1f} h at {lp['max_iterations']} iterations each "
        f"(early stops shorten it); {bad} campaign(s) with problems")
    return bad == 0


def manifest_path(plan, simulate=False):
    d = os.path.join(plan["_run_dir"], "sim") if simulate else plan["_run_dir"]
    return os.path.join(d, f"batch_{plan.get('name') or 'plan'}.json")


def read_manifest(plan, simulate=False):
    p = manifest_path(plan, simulate)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"campaigns": {}}


# ------------------------------------------------------------------- main
def open_bench(plan, simulate, log=print):
    import ilc_bench
    if simulate:
        from eomilc import simbench
        frfs = {plan["wiring"][k]["awg_ch"]: os.path.join(HERE, "run", f"frf_WIDE_{k}.csv") for k in ("X1", "X2")}
        awg, scope, mod = simbench.make_bench(frfs, full_scale=plan["loop"]["full_scale"])
        ilc_bench._AWGMOD = mod
        log("SIMULATED bench: " + awg.connect() + " / " + scope.connect())
        return awg, scope
    siblings = os.path.dirname(HERE)
    scopemod = ilc_bench.load_module(ilc_bench.find_scope_grab(siblings), "scope_grab")
    ilc_bench._AWGMOD = ilc_bench.load_module(
        os.environ.get("AWG_GUI", os.path.join(siblings, "BK4063B-AWG-GUI", "bk4063b.py")), "bk4063b_awg_gui")
    awg = ilc_bench.make_awg(ilc_bench._AWGMOD)
    log("AWG:  " + str(awg.connect()))
    scope = ilc_bench.make_scope(scopemod)
    try:
        log("Scope: " + str(scope.connect()))
    except Exception:
        awg.close()
        raise
    return awg, scope


def _all_off(awg, log, why):
    for c in (1, 2):
        try:
            if awg.is_on(c):
                awg.set_output(c, False)
                log(f"CH{c} output OFF ({why})")
        except Exception as e:
            log(f"could not switch CH{c} off: {e}")


def run_batch(plan, opts, log=print, stop=None, on_event=None, bench=None):
    """Run a loaded plan's campaigns in order; returns the manifest.

    opts: .simulate, .allow_output_on, .redo, .gui_names. `stop` is a
    threading.Event checked before every shot; `on_event(kind, info)` hears
    "batch", "campaign", "iteration", "saved", "campaign_end" and "batch_end"
    (it runs on the batch's thread and must not block). `bench` = (awg, scope)
    replaces the instruments: the GUI passes the pair it opened, the tests a
    simulated pair they can inspect. The instruments are closed at the end.

    Switching outputs on needs opts.allow_output_on; asking the person is the
    caller's job (the CLI's typed YES, the GUI's dialog)."""
    mpath = manifest_path(plan, opts.simulate)
    plan["_run_dir"] = os.path.dirname(mpath)
    os.makedirs(plan["_run_dir"], exist_ok=True)
    manifest = json.load(open(mpath)) if os.path.exists(mpath) else {"campaigns": {}}
    manifest.update(plan=plan["_path"], simulated=bool(opts.simulate))
    logf = open(os.path.join(plan["_run_dir"], f"batch_{plan.get('name') or 'plan'}.log"), "a")

    def say(s):
        log(s)
        logf.write(s + "\n")
        logf.flush()

    def emit(kind, **info):
        if on_event is not None:
            try:
                on_event(kind, info)
            except Exception as e:           # a display problem must not stop the bench
                say(f"  (display update failed: {e})")

    todo = [c["stem"] for c in plan["campaigns"]
            if opts.redo or manifest["campaigns"].get(c["stem"], {}).get("status") != "done"]
    say(f"\n=== batch {plan.get('name', '')} started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        + (f" (SIMULATED, files in {plan['_run_dir']})" if opts.simulate else ""))
    emit("batch", todo=todo, n=len(plan["campaigns"]), run_dir=plan["_run_dir"], simulated=bool(opts.simulate))
    awg, scope = bench if bench is not None else open_bench(plan, opts.simulate, say)
    try:
        for i, camp in enumerate(plan["campaigns"]):
            stem = camp["stem"]
            if stem not in todo:
                say(f"\n--- {stem}: done in an earlier run, skipped")
                continue
            _stopped(stop)
            say(f"\n--- {stem}")
            emit("campaign", stem=stem, index=todo.index(stem), n=len(todo))
            t0 = time.time()
            entry = {"started": f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"}
            try:
                entry.update(run_campaign(awg, scope, plan, camp, opts, say, stop, emit))
                entry["status"] = "done"
            except BatchError as e:
                entry.update(status="refused", error=str(e))
                say(f"  REFUSED: {e}")
                _all_off(awg, say, "campaign refused")
            except (BatchStopped, KeyboardInterrupt):
                entry["status"] = "stopped"
                raise
            except Exception as e:
                entry.update(status="error", error=f"{type(e).__name__}: {e}")
                raise
            finally:
                entry["minutes"] = round((time.time() - t0) / 60, 2)
                manifest["campaigns"][stem] = entry
                json.dump(manifest, open(mpath, "w"), indent=1, default=float)
                emit("campaign_end", stem=stem, entry=entry)
    except (BatchStopped, KeyboardInterrupt):
        say("\nstopped -- states are saved after every iteration; run again to resume")
    finally:
        _all_off(awg, say, "end of batch")
        awg.close()
        scope.close()
        json.dump(manifest, open(mpath, "w"), indent=1, default=float)
        say(f"manifest {mpath}")
        logf.close()
        emit("batch_end", manifest=manifest)
    return manifest


def cmd_run(a, bench=None):
    """`bench` = (awg, scope) replaces the instruments -- the tests pass a
    simulated pair they can inspect afterwards."""
    plan = load_plan(a.plan)
    if a.dry_run:
        import ilc_bench
        if ilc_bench._AWGMOD is None:
            from eomilc import simbench
            ilc_bench._AWGMOD = simbench.SimModule
        sys.exit(0 if dry_run(plan) else 1)
    if a.simulate:
        a.allow_output_on = True
    elif a.allow_output_on and not a.yes:
        print(f"This batch will switch AWG CH1 and CH2 ON for each of {len(plan['campaigns'])} campaigns "
              f"(and OFF between them), driving the Treks with the planned ramps.")
        if input("Type YES to allow it: ").strip() != "YES":
            sys.exit("not confirmed; nothing was touched")
    return run_batch(plan, a, log=print, bench=bench)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run (or dry-run, or simulate) a plan")
    r.add_argument("plan")
    r.add_argument("--dry-run", action="store_true", help="check the plan and estimate time; no instruments")
    r.add_argument("--simulate", action="store_true", help="run against eomilc.simbench instead of the bench")
    r.add_argument("--allow-output-on", action="store_true",
                   help="let the batch switch the AWG outputs on (after one typed confirmation)")
    r.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    r.add_argument("--redo", action="store_true", help="repeat campaigns the manifest lists as done")
    r.add_argument("--gui-names", action="store_true",
                   help="upload every iteration under its own <stem>_iNN name, as the GUI does, "
                        "instead of two alternating slots per channel")
    r.set_defaults(func=cmd_run)
    m = sub.add_parser("make-plan", help="write a plan for target pairs in a folder")
    m.add_argument("--targets", required=True, help="folder holding target_<stem>X1.csv / X2.csv")
    m.add_argument("--stems", required=True, help="comma-separated stems, in run order")
    m.add_argument("--x1-from", help="state file whose settings train X1 (plant, gamma, f_cut, notches, FRF)")
    m.add_argument("--x2-from", help="the same for X2")
    m.add_argument("--name", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--card", type=int, default=0, help="also write card files with this many samples")
    m.add_argument("--gui-names-ok", action="store_true", help=argparse.SUPPRESS)
    m.set_defaults(func=make_plan)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
