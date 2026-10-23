#!/usr/bin/env python3
"""Train a list of ramp targets through the ILC, X1 and X2 together, one after another.

    python ilc_batch.py make-plan --targets <suite>/targets --stems S47,S25,C25 \
        --x1-from run/drive_P92PX1H.state.npz --x2-from run/drive_P92PX2A.state.npz \
        --name tier1 --out <suite>/batch_tier1.json
    python ilc_batch.py run <suite>/batch_tier1.json --dry-run
    python ilc_batch.py run <suite>/batch_tier1.json --simulate
    python ilc_batch.py run <suite>/batch_tier1.json --allow-output-on
    python ilc_batch.py run <suite>/batch_tier1.json --allow-output-on --no-optical

The GUI's Batch window (ilc_gui.py, "Batch..." in the Bench loop box) builds,
checks, simulates and runs the same plans through run_batch() below.

A plan names target pairs (target_<stem>X1.csv, target_<stem>X2.csv, the layout
the ramp suites use) and how to train them. Each pair is ONE campaign: both
drives play at once from the two AWG channels, both monitors are read from the
same frozen acquisitions, and each channel's loop updates from its own monitor.
Which scope channel is which comes from the panel's Scope channels map
(eomilc.channels, the polarimeter's role vocabulary, in the panel's
config.json): the monitor is the MonXn channel, the drive readback for the
alignment check the CmdXn channel -- none assigned means that channel's
alignment is not verified, and the log says so. A plan's "wiring" block
overrides the map per channel; the map in force is written into every state
file. That halves the bench time against running X1 and X2 separately,
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
  * Never float (memory awg-trek-safety; either output may feed a stage that
    drives high, -4 to -5.7 kV, when its input floats): the batch never
    switches an output OFF on its own. Between campaigns, after each stage,
    on any error or Ctrl-C and at the end, every channel it played on PARKS:
    a flat record at its drive's idle (optical.park_drive, AWG names
    PARK1/PARK2), the output left ON. OFF at the end only with
    --outputs-off-at-end, named in the typed confirmation.
  * FRQ moves only under a FLAT record. Every campaign has its own record
    length, so FRQ changes between campaigns; a live channel is parked first
    (a flat record plays the same at any FRQ), then FRQ is set and read back
    before anything plays. An output that is OFF is set up while OFF.
  * Switching ON is the direction that puts voltage into something. The batch
    does it only with --allow-output-on, after one typed confirmation for the
    whole plan (or --yes), and only after the channel check below passed;
    without it a campaign whose outputs are already ON is refused before
    anything is changed.
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

THE PD, THE ANALYZER, THE SHOT COUNT
------------------------------------
The loop trains the monitors; the light is what the atoms get. With a PD
channel in the Scope channels map, every measurement also reads the analyzer
photodiode from the same frozen shots (offset dithered, on X1's grid) and keeps
it as meas_<stem>PD_iNN.npy plus a stats sidecar; the manifest's "iterations"
list carries pd_peak_v and pd_change_mv (rms change against the previous PD,
mV) per iteration, and the log one "PD:" line. The analyzer's angle is read
once at each campaign start through the polarimeter's ELL14 driver (never
moved; NaN when it does not answer) into the manifest and the PD sidecars.
"repeats_early" in the loop block takes fewer shots on iteration 0 and while
any active channel's last rms is above "early_rms_v" (default 2 V): the noise
floor 64 shots buy matters only near the end. The manifest records the shots
of every iteration.

OPTICAL STAGE
-------------
With an "optical" block in the plan (at its root, or per campaign, which wins;
"optical": false in a campaign switches it off; --no-optical skips it for the
run) the campaign does not end at its keepers. The ILC makes the MONITORS
follow the target; the light leaves them at the ramp top and after the fall,
which no monitor shows. So: both keepers play (outputs ON, the same
--allow-output-on rule), the rotating-analyzer polarimeter scans the light
(eomilc.optical: scan, compare), minus (light - monitors) goes onto each
channel's TARGET (optical.apply_correction, gain gains[pass]), `iterations`
more iterations of the normal loop learn the moved target (iterate_once, both
channels, the stop rules per pass), and a verification scan + compare reads
the after. The next pass applies the verification's correction at the next
gain; it REPLACES the previous one in its slot. The keeper is then the last
measured iteration after the last applied correction. A refused correction
ends the stage for that campaign, recorded, and the campaign keeps its keeper.
Every pass is in the manifest entry's "optical" block and in
<outdir>/<stem>_optical.json; a pass whose verification scan is on disk is
not repeated on a resumed run. --simulate plays the drives into the rampol
simulated bench (optical.open_devices(simulate=True) + sim_play), where the
only light-only term is its rotator error (sim_rot_err, 0.5 deg); what it
plays is what the simbench chains make of the drives (sim_bridge, see
_sim_drives) and it is cabled without a command channel (_optical_cfg).
"attribution": "separate" measures the correction per crystal instead of
splitting one joint residual: X1 alone (X2 parked flat at its idle, AWG name
PARK2, output ON), X2 alone (X1 parked, PARK1), each compared alone, both
files applied, and a joint scan verifies; "joint_before" (default true) adds
a joint scan before the first pass for the before/after table.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from eomilc import channels                                     # noqa: E402
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
                    t_offset_us=0.0, full_scale=10.0, first_shot="flat",
                    repeats_early=None, early_rms_v=2.0)
# repeats_early: the shots per measurement while the error is far above the
# noise -- on iteration 0 and whenever the last measured rms of any active
# channel was above early_rms_v (V at the EOM). 64 shots buy a noise floor the
# loop needs only near the end; while the error is volts, a quarter of them
# resolves it as well. None = repeats, i.e. off.
# Rough bench timing for the plan's estimate only; nothing is derived from it.
# ilc_bench.capture_all: 64 HRES singles on two channels ~17 s at the 3.7 Hz trigger.
# read_s_per_channel 0.12: measured 13 Oct 2026, 36 s per 64-shot iteration at 3.7 Hz
# with two channel reads (0.02 stood here and made every estimate low)
DEFAULT_TIMING = dict(trigger_hz=3.7, read_s_per_channel=0.12, upload_s=2.5,
                      campaign_overhead_s=25.0, scan_step_overhead_s=2.0, compare_s=10.0)
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
# The optical stage's settings (docs/OPTICAL_CLOSE_DESIGN.md section 3): band
# 5 kHz for the 0.3 ms ramp-top feature, PD delay 1.7 us (the bench's; 0 in
# simulation unless the plan says otherwise), split 0.5 for parallel shapes.
# cap_v: the gate's cap at the EOM (V), None = the panel's 300 V. sim_rot_err:
# the simulated rotator error (deg) that gives the closure something to do.
# attribution (the design's addendum, 13 Oct 2026): "joint" measures both
# crystals in one scan and splits the residual by `split`; "separate" scans X1
# alone (X2 parked) and X2 alone (X1 parked), each crystal gets its own
# measured correction, and a joint scan verifies. joint_before: in "separate",
# a joint scan before the first pass (the before of the table), not applied.
DEFAULT_OPTICAL = dict(
    # solo_angles / verify_angles: the X1-alone, X2-alone and verification scans'
    # angles when they differ from the before scan's (None = the same); a half
    # circle ("0:179:22.5") carries the same polarization information in half
    # the time, the full circle adds the 1-theta / 4-theta diagnostics (14 Oct 2026)
    scan=dict(angles="0:359:22.5", solo_angles=None, verify_angles=None,
              shots=8, points=None, sample_us=2.0, ref_every=9, hold_null=True,
              settle_s=1.0),
    band_hz=5000.0, pd_delay_us=1.7, split=0.5, gains=[0.7, 1.0], iterations=5,
    name="{stem}_opt{pass}", outdir="", verify=True, require_keeper=True, cap_v=None,
    figs=True, sim_rot_err=0.5, sim_bridge=True, attribution="joint", joint_before=True)
ATTRIBUTIONS = ("joint", "separate")


class BatchError(RuntimeError):
    """A campaign cannot go on; the batch records it and moves to the next."""


class BatchStopped(Exception):
    """Stop was asked for (the GUI's Stop): the batch ends after switching the
    outputs off. States are saved every iteration, so the next run resumes."""


def _stopped(stop):
    if stop is not None and stop.is_set():
        raise BatchStopped("stopped")


# ------------------------------------------------------------------ the plan
def load_plan(path, cfg=None):
    """`cfg`: the panel's config (its Scope channels map); None reads the
    panel's config.json from disk, so the CLI and the window agree."""
    with open(path) as f:
        plan = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    plan["_path"] = os.path.abspath(path)
    plan["_targets_dir"] = os.path.normpath(os.path.join(base, plan.get("targets_dir", "targets")))
    plan["_run_dir"] = os.path.normpath(os.path.join(HERE, plan.get("run_dir", "run")))
    plan["loop"] = {**DEFAULT_LOOP, **plan.get("loop", {})}
    plan["timing"] = {**DEFAULT_TIMING, **plan.get("timing", {})}
    if cfg is None:
        cfg = channels.load_panel_config()
    plan["_polarimeter_path"] = str((cfg or {}).get("polarimeter_path") or "")
    wiring, notes = {}, []
    for k in DEFAULT_WIRING:
        try:
            w = channels.wiring(cfg, k)
            base_w = dict(channel=w["channel"], awg_ch=w["awg_ch"], drive_ch=w["drive_ch"],
                          mon_ch=w["mon_ch"])
        except ValueError as e:
            base_w = dict(DEFAULT_WIRING[k])
            notes.append(f"{k}: {e}; using the old fixed numbers")
        wiring[k] = {**base_w, **plan.get("wiring", {}).get(k, {})}
    plan["wiring"] = wiring
    plan["_wiring_notes"] = notes
    plan["_scope_channels"] = channels.record(cfg)
    plan["settings"] = {k: plan.get("settings", {}).get(k) or DEFAULT_SETTINGS[k] for k in DEFAULT_WIRING}
    if not plan.get("campaigns"):
        raise SystemExit(f"{path}: no campaigns")
    plan["_echo_notes"] = echo_records(plan)
    # <name>X1 must leave room for _iNN under the generator's 11-character cap:
    # a stem over 5 characters trains under a cut AWG name (as build_plan does),
    # and no two campaigns may share one (their states would collide)
    used = {}
    for camp in plan["campaigns"]:
        if len(camp["stem"]) + 2 > 7 and not camp.get("awg"):
            camp["awg"] = camp["stem"][:5]
        short = camp.get("awg", camp["stem"])
        if short in used:
            raise SystemExit(f"{path}: {camp['stem']} and {used[short]} would both train as {short}X1/X2 "
                             f"(stems are cut to 5 characters); rename one target pair or set \"awg\"")
        used[short] = camp["stem"]
    return plan


def echo_records(plan, log=None):
    """A campaign with an "echo" key trains a spin-echo record built from its
    stem's one-transport pair (eomilc.echo): {"stem": "S47", "echo":
    {"spacing_ms": 16.667, "legs": 2, "tail_us": 200, "suffix": "E2"}} writes
    target_S47E2X1/X2.csv into the targets folder (left alone when already
    there and identical) and the campaign goes on under the stem S47E2, its
    AWG name cut to 5 characters as build_plan does. Returns the notes for
    the dry run; a source that cannot be used leaves the campaign's stem as it
    was, so plan_rows reports the campaign with its error."""
    from eomilc import echo
    notes = []
    for camp in plan["campaigns"]:
        e = camp.get("echo")
        if not e or camp.get("_echo"):
            continue
        src = camp["stem"]
        x1 = os.path.join(plan["_targets_dir"], f"target_{src}X1.csv")
        x2 = os.path.join(plan["_targets_dir"], f"target_{src}X2.csv")
        quiet = []
        try:
            r = echo.make([x1, x2], spacing_ms=float(e.get("spacing_ms", 16.667)), legs=int(e.get("legs", 2)),
                          tail_us=float(e.get("tail_us", 200.0)), suffix=e.get("suffix"),
                          out_dir=plan["_targets_dir"], log=quiet.append, do_check=False,
                          end_ms=e.get("end_ms"))
        except (ValueError, OSError) as exc:
            notes.append(f"{src}: echo record NOT built: {exc}")
            camp["_echo"] = dict(error=str(exc), source=src)
            continue
        camp["_echo"] = dict(source=src, name=r["name"], files=r["files"], written=r["written"],
                             spacing_ms=r["rec"]["spacing_ms"], legs=r["rec"]["legs"],
                             legs_ms=r["rec"]["legs_ms"], n=r["rec"]["n"], pause_ms=echo.pause_ms(r["rec"]))
        camp["stem"] = r["name"]
        if len(r["name"]) + 2 > 7 and not camp.get("awg"):
            camp["awg"] = r["name"][:5]
        notes.append(f"{r['name']}: spin echo of {src}, {r['rec']['legs']} legs at "
                     + ", ".join(f"{x:.3f}" for x in r["rec"]["legs_ms"])
                     + f" ms (spacing {r['rec']['spacing_ms']:.3f} ms), {r['rec']['n']} pts; card pause "
                     f"{echo.pause_ms(r['rec']):.3f} ms; "
                     + ("written" if any(r["written"].values()) else "already there")
                     + (f"; AWG name {camp['awg']}" if camp.get("awg") else ""))
        if log:
            log("  " + notes[-1])
    return notes


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
    scope_channels: str = ""     # eomilc.channels.record() of the map in force
    # Learned target corrections, as the panel keeps them (Session.base_target /
    # corr_total / corr_layers; state keys base_target, corr_total, corrections):
    # the target before them (monitor V), what was added (monitor V), the layers.
    base_target: object = None
    corr_total: object = None
    corr_layers: list = field(default_factory=list)
    since: int = 0               # decide()'s divergence best counts from measured[since:]
    corr_from: object = None     # the iteration the first optical correction was applied at
    extra: dict = field(default_factory=dict)      # other state keys carried through (line_fit)
    # the campaign's per-iteration record (shots, PD), one list shared by the
    # campaign's channels (run_campaign); never written into the state
    rows: list = field(default_factory=list)
    # the last measurement's noise floor (rms of the shot mean's standard
    # error, V at the EOM; NaN until measured in this run): shots_for
    floor_hv: float = float("nan")

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
    if (camp.get("_echo") or {}).get("error"):
        raise BatchError(f"{camp['stem']}: the echo record was not built ({camp['_echo']['error']}); "
                         f"the campaign would train the one-transport {camp['stem']} instead")
    if not os.path.exists(tpath):
        raise BatchError(f"{tpath} does not exist")
    t, v = run_ilc.load_target(tpath, ch.mon_scale)
    dt = float(np.median(np.diff(t)))
    state_path = os.path.join(plan["_run_dir"], f"drive_{stem}.state.npz")
    plant_p, loop_p, rec, src = _settings_for(plan, key)
    lp = plan["loop"]
    carried = {}
    if os.path.exists(state_path):
        st = run_ilc.load_state(state_path)
        carried = run_ilc.passthrough(st)
        # a state with learned corrections holds base + corrections as its
        # target: the target file is its BASE
        held = carried["base_target"] if "base_target" in carried else st["target"]
        if len(held) != len(v) or not np.allclose(held, v, rtol=1e-6, atol=1e-9):
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
    cr.scope_channels = plan.get("_scope_channels", "")
    restore_corrections(cr, carried)
    if resumed:
        if "corr_from" in st:
            cr.corr_from = int(st["corr_from"])
        restore_measured(cr, plan["_run_dir"])
    return cr


def restore_corrections(cr, carried):
    """The panel's correction keys from a state (run_ilc.passthrough) onto a
    ChannelRun, so a resumed campaign keeps its corrected target's record."""
    if "base_target" in carried:
        cr.base_target = np.asarray(carried["base_target"], float)
        if "corr_total" in carried:
            cr.corr_total = np.asarray(carried["corr_total"], float)
    if "corrections" in carried:
        try:
            cr.corr_layers = list(json.loads(str(carried["corrections"])))
        except ValueError:
            cr.corr_layers = []
    if "line_fit" in carried:
        cr.extra["line_fit"] = str(carried["line_fit"])


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
        # against the target that iteration was measured on: the base before
        # the first optical correction, the corrected one after
        tgt = cr.loop.target
        if cr.base_target is not None and cr.corr_from is not None and it < cr.corr_from:
            cr.loop.target = np.asarray(cr.base_target, float)
        try:
            m = cr.loop.metrics(y)
        finally:
            cr.loop.target = tgt
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
    # the panel's extra keys (run_ilc.PASS_THROUGH), written only when set,
    # as ilc_gui.save_session writes them
    extra = {}
    if getattr(cr, "base_target", None) is not None:
        extra["base_target"] = np.asarray(cr.base_target, float)
        if getattr(cr, "corr_from", None) is not None:
            extra["corr_from"] = int(cr.corr_from)
        if getattr(cr, "corr_total", None) is not None:
            extra["corr_total"] = np.asarray(cr.corr_total, float)
    if getattr(cr, "corr_layers", None):
        extra["corrections"] = json.dumps(cr.corr_layers, default=float)
    if getattr(lp, "line", None) is not None:
        extra["line_ref"] = np.asarray(lp.line, float)
    extra.update(getattr(cr, "extra", None) or {})
    np.savez(f, **extra, t=cr.t, target=lp.target, u=cr.u, dt=lp.dt,
             channel=lp.channel.name, gain=lp.plant.gain, tau=lp.plant.tau,
             offset=lp.plant.offset, tau2=lp.plant.tau2, fn=lp.plant.fn,
             zeta=lp.plant.zeta, full_scale=cr.full_scale, name=cr.stem,
             gamma=lp.gamma, f_cut=lp.f_cut, iteration=cr.iteration,
             t_offset=cr.t_off, history=np.array(lp.history, dtype=object),
             model=cr.model_key, frf_path=cr.frf_path, frf_use=cr.frf_use,
             frf_max=cr.frf_max, seed_path="", target_path=cr.target_path,
             notches=np.asarray(lp.notches, float).reshape(-1, 2),
             scope_channels=str(getattr(cr, "scope_channels", "") or ""))


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


# per AWG channel, (id(awg), ch) -> (crystal key, record in AWG volts, full
# scale, parked?): what this batch last put on the channel -- what a park at
# an error or the end takes its idle and length from
_PLAYED = {}


def _played(awg, c, key, u, full_scale, parked=False):
    _PLAYED[(id(awg), c)] = (key, np.asarray(u, float), float(full_scale), bool(parked))


def park_channel(awg, c, key, u, full_scale, log=print, why=""):
    """The never-float stop for one AWG channel (memory awg-trek-safety): a
    flat record at the idle of `u` (optical.park_drive -- its first sample,
    its length, so FRQ still fits) uploaded under the crystal's park name
    and selected. The output is not touched: ON stays ON, so the stage
    input never floats; OFF stays OFF. A channel that already plays its
    park is left as it is (a playing waveform is never overwritten).
    Raises if the upload fails."""
    import ilc_bench
    from eomilc import optical
    rec = _PLAYED.get((id(awg), c))
    if rec is not None and rec[3]:
        log(f"  CH{c} already parked{' (' + why + ')' if why else ''}")
        return
    park = optical.park_drive(u)
    name = optical.PARK_NAMES[key]
    n, _frac = ilc_bench.upload_drive(awg, c, name, park, full_scale)
    _played(awg, c, key, park, full_scale, parked=True)
    log(f"  CH{c} parked{' (' + why + ')' if why else ''}: plays {name}, flat at its idle "
        f"{float(park[0]):+.3f} V ({n} pts), output {'ON' if awg.is_on(c) else 'OFF'} left as it is")


def park_runs(awg, runs, log=print, why=""):
    """Park each run's channel at its drive's idle (park_channel). A park
    that fails is reported and the output left as it is -- switching OFF is
    the act the never-float rule forbids."""
    for cr in runs:
        c = cr.wiring["awg_ch"]
        try:
            park_channel(awg, c, cr.key, cr.u, cr.full_scale, log, why)
        except Exception as e:
            log(f"  CH{c}: PARK FAILED ({type(e).__name__}: {e}) -- output left as it is "
                f"(never-float); switch it OFF by hand only with its stage input bypassed")


def park_all(awg, log, why):
    """Every channel this batch played on parks at the idle of what it last
    played (a refusal, an error, Stop, the end of the batch). A channel the
    batch never touched is left alone."""
    for c in (1, 2):
        rec = _PLAYED.get((id(awg), c))
        if rec is None:
            continue
        try:
            park_channel(awg, c, rec[0], rec[1], rec[2], log, why)
        except Exception as e:
            log(f"CH{c}: PARK FAILED ({type(e).__name__}: {e}) -- output left as it is "
                f"(never-float); switch it OFF by hand only with its stage input bypassed")


_SLOT = {}     # per AWG channel, across the whole batch: the next slot to fill


AWG_MAX_PTS = int(os.environ.get("BK4063B_MAX_PTS", 8_000_000))   # the panel's cap (ilc_gui.AWG_MAX_PTS)


def upload(awg, cr, gui_names, log=print):
    import ilc_bench
    if len(cr.u) > AWG_MAX_PTS:
        raise BatchError(f"{cr.stem}: {len(cr.u)} points is over the generator's arb cap of {AWG_MAX_PTS} "
                         f"(BK4063B_MAX_PTS sets it; the 4063B holds 8 Mpts per arb, measured 8 Oct 2026)")
    if gui_names:
        name = f"{cr.stem}_i{cr.iteration:02d}"
    else:
        c = cr.wiring["awg_ch"]
        k = _SLOT.get((id(awg), c), 0)
        name = f"B{cr.key}{SLOTS[k % 2]}"
        _SLOT[(id(awg), c)] = k + 1
    n, frac = ilc_bench.upload_drive(awg, cr.wiring["awg_ch"], name, cr.u, cr.full_scale)
    _played(awg, cr.wiring["awg_ch"], cr.key, cr.u, cr.full_scale)
    return name, n, frac


def prepare_campaign(awg, scope, runs, plan, gui_names, allow_on, log=print):
    """Park live outputs -> FRQ -> scope window and verticals -> first
    uploads -> channel checks -> outputs on -> alignment. Raises BatchError
    to refuse.

    Never-float: an output that is ON is never switched off here. A live
    channel first plays a flat park at the idle (a flat record plays the
    same at any FRQ, so nothing time-scaled reaches the Trek while FRQ
    moves), then FRQ is set, then the drive is uploaded live, as every
    iteration's is. Without allow_on a campaign whose outputs are already ON
    is refused before anything is changed."""
    import ilc_bench
    period = len(runs[0].t) * runs[0].loop.dt
    live = [cr for cr in runs if awg.is_on(cr.wiring["awg_ch"])]
    if live and not allow_on:
        raise BatchError(", ".join(f"CH{cr.wiring['awg_ch']}" for cr in live)
                         + " ON and this run may not drive the outputs (pass --allow-output-on); "
                           "nothing was changed")
    for cr in live:
        park_channel(awg, cr.wiring["awg_ch"], cr.key, cr.u, cr.full_scale, log, "FRQ moves under it")
    for cr in runs:
        c = cr.wiring["awg_ch"]
        missed = awg.apply_channel(c, {"BSWV": {"FRQ": 1.0 / period}}, log=lambda m: None)
        if missed:
            raise BatchError(f"CH{c}: FRQ did not take ({missed})")
    log(f"  AWG FRQ {1.0/period:.6g} Hz on " + ", ".join(f"CH{cr.wiring['awg_ch']}" for cr in runs)
        + f" (record {period*1e3:.4f} ms); "
        + ("outputs off" if not live else
           "live: " + ", ".join(f"CH{cr.wiring['awg_ch']}" for cr in live) + ", parked flat while it moved"))
    rng = nice_setting(1.3 * period)
    # position is trigger -> screen CENTRE: say so, because the polarimeter's
    # scans leave the reference at LEFT and the trigger then sits half a
    # record off screen (13 Oct 2026: "X1 drive starts +6498.5 us", refused)
    scope.put(":TIMebase:REFerence", "CENTer")
    scope.put(":TIMebase:RANGe", f"{rng:.6g}")
    scope.put(":TIMebase:POSition", f"{period/2:.6g}")
    log("  scope channels: " + channels.describe(plan.get("_scope_channels") and
                                                  channels.from_record(plan["_scope_channels"])
                                                  ).replace("\n", " | "))
    for cr in runs:
        for ch, lo, hi, what in ((cr.wiring["drive_ch"], cr.u.min(), cr.u.max(), "drive"),
                                 (cr.wiring["mon_ch"], cr.loop.target.min(), cr.loop.target.max(), "monitor")):
            if ch is None:
                continue
            scope.put(f":CHANnel{ch}:DISPlay", "1")
            scope.put(f":CHANnel{ch}:COUPling", "DC")
            span = max(float(hi - lo), 1e-3)
            scope.put(f":CHANnel{ch}:SCALe", f"{nice_setting(1.25 * span / 8):.6g}")
            scope.put(f":CHANnel{ch}:OFFSet", f"{0.5 * float(hi + lo):.6g}")
    pdc = pd_channel(plan)
    if pdc is not None and pdc not in {cr.wiring["mon_ch"] for cr in runs}:
        # the PD is read every iteration (capture_monitors): on, DC and at the
        # polarimeter preset's screen for the PD role (1 V/div, offset 2.7 V),
        # not whatever the last scan or hand left there (14 Oct 2026: 1 mV/div)
        scope.put(f":CHANnel{pdc}:DISPlay", "1")
        scope.put(f":CHANnel{pdc}:COUPling", "DC")
        scope.put(f":CHANnel{pdc}:OFFSet", "0")
        scope.put(f":CHANnel{pdc}:SCALe", f"{PD_VDIV:.6g}")
        scope.put(f":CHANnel{pdc}:OFFSet", f"{PD_OFFSET:.6g}")
        log(f"  PD CH{pdc} on, DC, {PD_VDIV:g} V/div, offset {PD_OFFSET:+g} V (the polarimeter's preset)")
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
        if not awg.is_on(cr.wiring["awg_ch"]):
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
        if cr.wiring["drive_ch"] is None:
            log(f"  alignment {cr.key}: NOT verified -- no Cmd{cr.key} scope channel in the "
                f"Scope channels map (the drive is not read back)")
            continue
        pts = scopeio.scope_points_for(2.2 * len(cr.t))
        # with the capture's retry: one bad block (~1 in 30 on the bench)
        # must not abandon the campaign (review G1-22); reads that keep
        # failing refuse it, as the capture's do
        try:
            got[cr.key] = read_waveform(scope, cr.wiring["drive_ch"], pts)
        except Exception as e:
            scope.run()
            raise BatchError(f"alignment check: scope CH{cr.wiring['drive_ch']} could not be read "
                             f"({type(e).__name__}: {e})") from e
    scope.run()
    for cr in runs:
        if cr.key not in got:
            continue
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
PD_VDIV, PD_OFFSET = 1.0, 2.7      # the polarimeter preset's PD screen (rampol.config PRESETS roles PD)


def pd_channel(plan):
    """The scope channel the plan's recorded Scope channels map gives the PD
    role (the analyzer photodiode), or None: no map recorded, no PD role, or
    the role on two channels."""
    rec = plan.get("_scope_channels")
    chans = channels.from_record(rec) if rec else None
    if not chans:
        return None
    try:
        return channels.channel_for(chans, "PD")
    except ValueError:
        return None


def capture_monitors(scope, runs, plan, log=print, stop=None, repeats=None):
    """`repeats` shots (default the plan's), every active channel's monitor read
    from the same frozen acquisition, with the GUI's offset dither
    (ilc_gui._bench_capture): each channel's offset steps across `dither_codes`
    ADC codes over the shots so the converter's per-code error pattern averages
    out instead of being learned. Offsets are restored however the capture ends.

    The PD (pd_channel: the analyzer photodiode, when the map has one and it is
    not a monitor's channel) is read from the same frozen acquisition each
    shot, its offset dithered like the monitors', resampled onto X1's grid.
    Returns {key: (mean, stack)}, plus "PD": (mean, stack) when it was read."""
    from eomilc import scope as scopeio
    lp = plan["loop"]
    repeats = int(lp["repeats"] if repeats is None else repeats)
    codes = int(lp["dither_codes"])
    time.sleep(lp["settle_s"])
    pd = pd_channel(plan)
    if pd is not None and pd in {cr.wiring["mon_ch"] for cr in runs}:
        log(f"  PD not read: CH{pd} is also a monitor's channel")
        pd = None
    ref = next((cr for cr in runs if cr.key == "X1"), runs[0])
    plan_d = []
    for c in [cr.wiring["mon_ch"] for cr in runs] + ([pd] if pd is not None else []):
        sc, off = scope.try_get(f":CHANnel{c}:SCALe"), scope.try_get(f":CHANnel{c}:OFFSet")
        if codes and sc is not None and off is not None:
            plan_d.append((c, float(off), ADC_CODE_PER_VDIV * float(sc) * codes))
        elif codes:
            log(f"  CH{c}: offset dither SKIPPED -- the scope did not answer SCALe / OFFSet, so the "
                f"ADC code pattern is not averaged out on this channel")
    stacks = {cr.key: [] for cr in runs}
    if pd is not None:
        stacks["PD"] = []
    dropped = 0
    # the monitors' screens, for the clipping check: a shot whose samples
    # reach within one ADC code of +/-4 div of that shot's offset read the
    # rail, not the chain, and the loop would learn the clipped error
    # (review G1-23; the verticals come from the target span + 12.5 %)
    screen = {}
    for cr in runs:
        c = cr.wiring["mon_ch"]
        sc = scope.try_get(f":CHANnel{c}:SCALe")
        off = scope.try_get(f":CHANnel{c}:OFFSet")
        try:
            screen[cr.key] = (c, float(sc), float(off))
        except (TypeError, ValueError):
            log(f"  CH{c}: clipping check SKIPPED -- the scope did not answer SCALe / OFFSet")
    dith = {c: (off0, span) for c, off0, span in plan_d}
    try:
        for i in range(repeats):
            _stopped(stop)
            for c, off0, span in plan_d:
                scope.put(f":CHANnel{c}:OFFSet", f"{off0 + span * ((i + 0.5) / repeats - 0.5):.6g}")
            # a missed trigger (a burst hiccup) is re-armed twice before it ends
            # the campaign; a read that fails three times drops THIS shot, not
            # the campaign (14 Oct 2026: the 7-8 Oct overnight scans lost whole
            # runs to a read the scope answered badly about once in a thousand)
            for arm in range(3):
                got = scope.single(wait_s=lp["wait_s"])
                if got is True:
                    break
                if arm < 2:
                    log(f"  no trigger within {lp['wait_s']:g} s on repeat {i+1}: re-armed ({arm + 2}/3)")
            else:
                raise BatchError(f"no trigger within {lp['wait_s']:g} s on repeat {i+1} (three arms)")
            shot = {}
            try:
                for cr in runs:
                    pts = scopeio.scope_points_for(2.2 * len(cr.t))
                    shot[cr.key] = read_waveform(scope, cr.wiring["mon_ch"], pts)
                if pd is not None:
                    shot["PD"] = read_waveform(scope, pd, scopeio.scope_points_for(2.2 * len(ref.t)))
            except Exception as e:
                dropped += 1
                log(f"  repeat {i+1}: readout failed after 3 tries ({type(e).__name__}: {e}); shot DROPPED")
                scope.run()
                continue
            scope.run()
            for cr in runs:
                if cr.key not in screen:
                    continue
                c, sc, off = screen[cr.key]
                if c in dith:
                    off0, span = dith[c]
                    off = off0 + span * ((i + 0.5) / repeats - 0.5)
                edge = (4.0 - ADC_CODE_PER_VDIV) * sc
                vs = np.asarray(shot[cr.key][1], float)
                if vs.size and (vs.max() >= off + edge or vs.min() <= off - edge):
                    raise BatchError(
                        f"{cr.key} monitor CH{c} clipped on repeat {i+1}: {vs.min():+.4g}..{vs.max():+.4g} V "
                        f"against a {sc:g} V/div screen at offset {off:+.4g} V -- the shot overshoots the "
                        f"verticals set from the target; widen CH{c} (or the first shot) and run again")
            for cr in runs:
                ts, vs = shot[cr.key]
                stacks[cr.key].append(scopeio.resample(np.asarray(ts), np.asarray(vs), cr.t, t_offset=cr.t_off))
            if pd is not None:
                ts, vs = shot["PD"]
                stacks["PD"].append(scopeio.resample(np.asarray(ts), np.asarray(vs), ref.t, t_offset=ref.t_off))
    finally:
        for c, off0, _ in plan_d:
            try:
                scope.put(f":CHANnel{c}:OFFSet", f"{off0:.6g}")
            except Exception as e:
                log(f"  could not restore CH{c} offset: {e}")
    if dropped:
        kept = repeats - dropped
        log(f"  {kept} of {repeats} shots kept ({dropped} dropped)")
        if kept < max(4, int(0.9 * repeats)):
            raise BatchError(f"only {kept} of {repeats} shots were read: the scope is not answering reads "
                             f"({dropped} dropped in one measurement)")
    from eomilc import ilc
    return {k: (ilc.averaged(v), np.asarray(v)) for k, v in stacks.items()}


# ------------------------------------------------------------ the PD record
# The loop trains the MONITORS; the light is what the atoms get. During the
# electrical iterations the analyzer sits still, so the PD reads cos^2 of the
# light's rotation at one angle, and its trace iteration by iteration shows how
# the light answers each update. Every measurement keeps it beside the
# monitors': meas_<campaign stem>PD_iNN.npy (the shot mean, PD volts on X1's
# grid) and its stats sidecar. NN is the campaign's iteration: the higher of
# the two channels' counts when the capture was taken (one channel may have
# stopped while the other learns on).
def pd_stem(stem):
    """The campaign stem of a channel stem (S25X1 -> S25), or None when the
    stem does not end in X1 / X2."""
    return stem[:-2] if len(stem) > 2 and stem[-2:] in ("X1", "X2") else None


def pd_path(run_dir, stem, it):
    return os.path.join(run_dir, f"meas_{stem}PD_i{int(it):02d}.npy")


def read_vertical(scope, ch):
    """(V/div, offset) of a scope channel, None for what it would not say."""
    out = []
    for what in ("SCALe", "OFFSet"):
        try:
            g = getattr(scope, "try_get", None) or scope.get
            v = g(f":CHANnel{ch}:{what}")
            out.append(float(v) if v is not None else None)
        except Exception:
            out.append(None)
    return tuple(out)


def save_pd(run_dir, stem, it, mean, stack, dt, vdiv=None, offset=None, scope_ch=None,
            analyzer_deg=float("nan"), log=print):
    """The PD of one measurement on disk: meas_<stem>PD_iNN.npy (the mean) and
    its sidecar meas_<stem>PD_iNN_stats.npz (eomilc.diagnostics, as the
    monitors' are: sem_t, floor_rms, the noise spectrum; the code fold is left
    out, it means nothing on the PD) with the V/div it was read at, the scope
    channel, and the analyzer angle (polarimeter frame, NaN when not known).
    Returns dict(path, stats, peak_v, change_mv): the peak of the mean, and the
    rms of its change against the latest earlier PD file of the campaign in
    mV (NaN on i00 or with none on disk)."""
    from eomilc import diagnostics as dg
    mean = np.asarray(mean, float)
    path = pd_path(run_dir, stem, it)
    np.save(path, mean)
    st = None
    try:
        # vdiv withheld from the stats call, so no fold is computed; the PD is
        # its own reference (no target), the spectra are of its scatter
        st = dg.measurement_stats(mean, stack, dt, vdiv=None, offset=offset)
        st.update(vdiv=float(vdiv) if vdiv is not None else float("nan"),
                  role="PD", scope_ch=int(scope_ch) if scope_ch is not None else -1,
                  analyzer_deg=float(analyzer_deg))
        dg.save_stats(dg.stats_path(path), st)
    except (ValueError, OSError) as e:
        log(f"  PD stats not kept: {e}")
    change = float("nan")
    for k in range(int(it) - 1, -1, -1):
        p = pd_path(run_dir, stem, k)
        if os.path.exists(p):
            prev = np.load(p)
            if len(prev) == len(mean):
                change = float(np.sqrt(np.mean((mean - prev) ** 2))) * 1e3
            break
    return dict(path=path, stats=st, peak_v=float(np.max(mean)), change_mv=change)


def pd_line(r):
    """The log line of one PD record (save_pd's return)."""
    if np.isfinite(r["change_mv"]):
        return f"PD: peak {r['peak_v']:.2f} V, change vs last {r['change_mv']:.1f} mV rms"
    return f"PD: peak {r['peak_v']:.2f} V, no earlier PD to compare"


def pd_rows(run_dir, stem):
    """The per-iteration PD record rebuilt from a campaign's PD files on disk
    (a resumed campaign's earlier iterations): it, shots (from the sidecar),
    pd_peak_v, pd_change_mv."""
    import glob as _glob
    import re as _re
    from eomilc import diagnostics as dg
    rows, prev = [], None
    files = []
    for f in _glob.glob(os.path.join(run_dir, f"meas_{stem}PD_i*.npy")):
        m = _re.search(r"PD_i(\d+)\.npy$", f)
        if m:
            files.append((int(m.group(1)), f))
    for it, f in sorted(files):
        y = np.load(f)
        ch = float("nan")
        if prev is not None and len(prev) == len(y):
            ch = float(np.sqrt(np.mean((y - prev) ** 2))) * 1e3
        row = dict(it=it, pd_peak_v=float(np.max(y)), pd_change_mv=ch if it else float("nan"))
        st = dg.load_stats(dg.stats_path(f))
        if st is not None and "n_shots" in st:
            row["shots"] = int(st["n_shots"])
        rows.append(row)
        prev = y
    return rows


EARLY_FLOOR_FACTOR = 3.0


def shots_for(runs, lp, caps=None):
    """(shots, early) for the next measurement: repeats_early on iteration 0
    and while the last measured rms of any active channel is above early_rms_v,
    else repeats. repeats_early None (the default) = repeats: off.

    "Far above the noise" is also checked against the noise itself: a channel
    counts as far only while its rms is above EARLY_FLOOR_FACTOR x that
    measurement's own noise floor. Few shots have a floor of their own (2
    shots of the simulated chain: 2.5 V rms), and an rms held up by it would
    otherwise keep the early count for good and stop the channel on that
    floor -- the loss the early count must not cost.

    A channel at the last iteration its cap allows (`caps`: the learn stage's
    per-channel loop settings, else lp) is measured at the full count whatever
    its rms: that measurement is the one a stage keeps and plays into the
    verification, and a 16-shot keeper after a 3-iteration learn (S25E2,
    14 Oct 2026: 1.39 V rms) is not a keeper."""
    reps = int(lp["repeats"])
    n_early = lp.get("repeats_early")
    n_early = reps if n_early is None else max(2, int(n_early))
    if n_early == reps:
        return reps, False
    first = max(cr.iteration for cr in runs) == 0
    lim = float(lp.get("early_rms_v", 2.0))
    far = any(cr.active and cr.measured and cr.measured[-1][1] > lim
              and not cr.measured[-1][1] <= EARLY_FLOOR_FACTOR * cr.floor_hv
              for cr in runs)
    last = any(cr.active and cr.iteration >= int(((caps or {}).get(cr.key) or lp)["max_iterations"])
               for cr in runs)
    return (n_early, True) if (first or far) and not last else (reps, False)


def read_analyzer(plan, scope, args, log=print):
    """The analyzer's angle at campaign start, read once through the
    polarimeter's driver (ilc_hardware.read_analyzer: opened, asked, closed,
    never moved). NaN when it did not answer -- logged, never a refusal -- and
    on simulated instruments, where COM3 is not this run's to open. The mount
    is closed before the optical stage opens it again."""
    if getattr(args, "simulate", False) or type(scope).__module__.endswith("simbench"):
        log("  analyzer not read (simulated instruments)")
        return float("nan")
    import ilc_hardware
    try:
        r = ilc_hardware.read_analyzer({"polarimeter_path": plan.get("_polarimeter_path") or None})
    except Exception as e:                    # a mount problem never stops a campaign
        r = dict(ok=False, deg=float("nan"), text=f"{type(e).__name__}: {e}")
    if r["ok"] and np.isfinite(r["deg"]):
        log(f"  analyzer at {r['deg']:+.1f} deg (polarimeter frame)")
        return float(r["deg"])
    log(f"  analyzer did not answer ({r['text']}); analyzer_deg NaN")
    return float("nan")


# ---------------------------------------------------------------- campaign
def decide(cr, m, stack, lp, log):
    """Record a measurement and decide whether this channel keeps learning."""
    from eomilc import ilc
    k = cr.iteration
    cr.measured.append((k, m["rms_err_hv"], m["peak_err_hv"]))
    cr.drives[k] = cr.u.copy()
    # since: a moved target (the optical stage) starts a new best; the rms
    # against the old target says nothing about divergence from the new one
    best = min(r[1] for r in (cr.measured[getattr(cr, "since", 0):] or cr.measured))
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
    """The lowest-rms measured iteration at or past min_iterations, among the
    iterations since `cr.since` (an optical pass keeps a drive of its own: a
    pass that diverged early must not fall back to the electrical keeper,
    learned for the uncorrected target -- 14 Oct 2026)."""
    pool = cr.measured[getattr(cr, "since", 0):] or cr.measured
    late = [r for r in pool if r[0] >= lp["min_iterations"]] or pool
    return min(late, key=lambda r: r[1]) if late else None


def iterate_once(awg, scope, runs, plan, args, log=print, stop=None,
                 emit=lambda kind, **kw: None, stem=None, loop_for=None):
    """One iteration of the bench loop for `runs` (ChannelRuns playing
    together): every monitor from the same frozen acquisitions
    (capture_monitors), then per ACTIVE channel the metrics, the measurement
    saved (meas_<stem>_iNN.npy), decide(), and either a stop (state saved; a
    diverged channel goes back to its best drive) or the update, the limit
    check, the state and drive CSV saved; the new drives are uploaded and
    "saved" is emitted. Returns the ChannelRuns whose drive changed.

    `args` needs .gui_names. `stem`: the campaign's name for the events
    (default: the first channel's stem without its key). `loop_for`: {key:
    loop settings} in place of plan["loop"] for decide() and the keeper (the
    optical stage's per-pass iteration caps); the capture always uses the
    plan's. Raises BatchStopped when `stop` is set, BatchError when the
    scope does not trigger.

    The shot count is shots_for's (the plan's repeats_early while the error is
    far above the noise); the PD, when the map has one, is saved beside the
    monitors (save_pd) with plan["_analyzer_deg"] in its sidecar. One record
    per call goes onto runs[0].rows: it (the campaign's iteration), shots,
    early, and pd_peak_v / pd_change_mv when the PD was read."""
    from eomilc import ilc
    _stopped(stop)
    if stem is None:
        stem = runs[0].stem[:-len(runs[0].key)] if runs[0].stem.endswith(runs[0].key) else runs[0].stem
    states = {cr.key: cr.state_path for cr in runs}
    shots, early = shots_for(runs, plan["loop"], loop_for)
    camp_it = max(cr.iteration for cr in runs)
    caps = capture_monitors(scope, runs, plan, log, stop, repeats=shots)
    row = dict(it=camp_it, shots=shots, early=early)
    changed = []
    for cr in runs:
        y, stack = caps[cr.key]
        if not cr.active:
            continue
        lp = (loop_for or {}).get(cr.key) or plan["loop"]
        m = cr.loop.metrics(y)
        np.save(os.path.join(plan["_run_dir"], f"meas_{cr.stem}_i{cr.iteration:02d}.npy"), y)
        st = np.asarray(stack, float)
        cr.floor_hv = (float(np.sqrt(np.mean(st.std(axis=0, ddof=1) ** 2 / st.shape[0])))
                       * cr.loop.channel.mon_scale if st.ndim == 2 and st.shape[0] > 1 else float("nan"))
        row.setdefault("rms_v", {})[cr.key] = m["rms_err_hv"]
        row.setdefault("floor_v", {})[cr.key] = cr.floor_hv
        why = decide(cr, m, stack, lp, log)
        if early and why in ("noise floor", "plateau"):
            # judged against the early count's own floor: not a stop
            log(f"  {cr.stem} i{cr.iteration:02d}: {why} on {shots} shots is the early count's; "
                f"going on at {int(plan['loop']['repeats'])}")
            cr.floor_hv = float("inf")
            why = ""
        log(f"  {cr.stem} i{cr.iteration:02d}: rms {m['rms_err_hv']:6.2f} V  peak {m['peak_err_hv']:7.1f} V"
            + (f"  ({shots} shots, early)" if early else "")
            + (f"  -> stop ({why})" if why else ""))
        emit("iteration", stem=stem, key=cr.key, it=cr.iteration, rms_v=m["rms_err_hv"],
             peak_v=m["peak_err_hv"], stop=why, states=states)
        if why:
            cr.active, cr.reason = False, why
            cr.loop.history.append({**m, "run_id": cr.run_id})
            if why == "diverged":
                kb = keeper(cr, lp)
                if kb is None:
                    log(f"  {cr.stem}: no measured iteration to go back to; keeps its drive")
                else:
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
    if "PD" in caps:
        ref = next((cr for cr in runs if cr.key == "X1"), runs[0])
        pch = pd_channel(plan)
        vdiv, off = read_vertical(scope, pch)
        r = save_pd(plan["_run_dir"], pd_stem(runs[0].stem) or runs[0].stem, camp_it, caps["PD"][0],
                    caps["PD"][1], ref.loop.dt, vdiv=vdiv, offset=off, scope_ch=pch,
                    analyzer_deg=plan.get("_analyzer_deg", float("nan")), log=log)
        row.update(pd_peak_v=r["peak_v"], pd_change_mv=r["change_mv"])
        log("  " + pd_line(r))
    runs[0].rows.append(row)
    for cr in changed:
        upload(awg, cr, args.gui_names, log)
    # after the saves: a follower reloading the state sees this iteration
    emit("saved", stem=stem, states=states)
    return changed


def run_campaign(awg, scope, plan, camp, args, log=print, stop=None, emit=lambda kind, **kw: None):
    from eomilc import outputs
    lp = plan["loop"]
    opt = optical_settings(plan, camp)          # a plan error refuses before anything plays
    keys = camp.get("channels", ["X1", "X2"])
    runs = [build_run(plan, camp, k, log) for k in keys]
    n = {len(cr.t) for cr in runs}
    if len(n) != 1 or len({round(float(cr.loop.dt), 12) for cr in runs}) != 1:
        raise BatchError("X1 and X2 targets are not on one grid -- they must play together")
    rows = []                                            # the campaign's per-iteration record
    for cr in runs:
        save_state(cr)                                   # the GUI can open it from now on
        cr.run_id = time.time()
        cr.rows = rows
    plan["_analyzer_deg"] = float("nan")
    prepare_campaign(awg, scope, runs, plan, args.gui_names, args.allow_output_on, log)
    # read once and closed again: the optical stage opens the mount itself later
    plan["_analyzer_deg"] = read_analyzer(plan, scope, args, log)
    for cr in runs:
        write_iteration(cr, plan["_run_dir"])
    resumed_at = {cr.key: cr.iteration for cr in runs}
    while any(cr.active for cr in runs):
        iterate_once(awg, scope, runs, plan, args, log, stop, emit, stem=camp["stem"])
    park_runs(awg, runs, log, "electrical stage done")
    final, opt_rec = {}, None
    if opt is not None and getattr(args, "no_optical", False):
        log("  optical stage: skipped (--no-optical)")
    elif opt is not None:
        opt_rec, final = optical_stage(awg, scope, runs, plan, camp, args, opt, log, stop, emit)
    result = {"channels": {}}
    kept, targets = {}, {}
    for cr in runs:
        targets[cr.key] = cr.target_path
        kb = final.get(cr.key) or keeper(cr, lp)
        if kb is None:
            # a resumed campaign whose per-iteration files are gone: no keeper
            # to write, recorded as such (it raised a TypeError here and ended
            # the campaign as 'error' after training already stood -- G1-24)
            result["channels"][cr.key] = dict(state=cr.state_path, stop=cr.reason, iterations=cr.iteration,
                                              resumed_at=resumed_at[cr.key], keeper_iteration=None,
                                              keeper_file=None, history=[])
            log(f"  {cr.stem}: no keeper (no measured iteration on disk), stopped: {cr.reason}")
            continue
        u_keep = cr.drives[kb[0]]
        kdir = os.path.join(plan["_run_dir"], "keep")
        os.makedirs(kdir, exist_ok=True)
        kpath = os.path.join(kdir, f"drive_{cr.stem}_keep.csv")
        why = cr.reason + ("; the last iteration after the optical correction" if cr.key in final else "")
        outputs.write_awg_csv(kpath, cr.t, u_keep,
                              comment=f"{cr.loop.channel.name} keeper: {cr.stem} i{kb[0]:02d}, rms {kb[1]:.2f} V, "
                                      f"peak {kb[2]:.1f} V (ilc_batch, stopped: {why})")
        kept[cr.key] = kpath
        result["channels"][cr.key] = dict(state=cr.state_path, stop=cr.reason, iterations=cr.iteration,
                                          resumed_at=resumed_at[cr.key],
                                          keeper_iteration=kb[0], keeper_rms_v=kb[1], keeper_peak_v=kb[2],
                                          keeper_file=kpath,
                                          history=[dict(it=a, rms_v=b, peak_v=c) for a, b, c in cr.measured])
        log(f"  {cr.stem}: keeper i{kb[0]:02d} (rms {kb[1]:.2f} V, peak {kb[2]:.1f} V), stopped: {why}")
    if opt_rec is not None:
        result["optical"] = opt_rec
    # per iteration: this run's records, and a resumed campaign's earlier ones
    # rebuilt from its PD files (shots then from their sidecars)
    have = {r["it"] for r in rows}
    fstem = pd_stem(runs[0].stem) or runs[0].stem
    result["iterations"] = sorted(rows + [r for r in pd_rows(plan["_run_dir"], fstem) if r["it"] not in have],
                                  key=lambda r: r["it"])
    result["analyzer_deg"] = plan.get("_analyzer_deg", float("nan"))
    if plan.get("card") and len(kept) == 2:
        try:
            result["card"] = card_files(kept, targets, plan["card"], log)
        except (ValueError, OSError) as e:          # the training stands; only the export failed
            result["card_error"] = str(e)
            log(f"  card files NOT written: {e}")
    return result


# ------------------------------------------------------------ optical stage
class OpticalError(RuntimeError):
    """The polarimeter side of the optical stage failed (devices, scan,
    comparison): the stage ends for this campaign, recorded, and the
    campaign keeps the keeper it has."""


def _num(x):
    """0.7 -> "0.7", 1 -> "1.0": gains read as gains."""
    s = f"{float(x):g}"
    return s if ("." in s or "e" in s) else s + ".0"


def optical_settings(plan, camp):
    """The optical stage's settings for one campaign, or None when it has
    none: DEFAULT_OPTICAL under the plan root's "optical" under the
    campaign's (per-campaign wins, key by key; "scan" merges key by key too).
    "optical": true takes the defaults; a campaign's "optical": false
    switches a root block off for it. "_given" lists the keys the plan set.
    Raises BatchError for settings that cannot run."""
    root, own = plan.get("optical"), camp.get("optical")
    if own is False or (own is None and not root):
        return None
    s = json.loads(json.dumps(DEFAULT_OPTICAL))          # a deep copy
    given = set()
    for layer in (root, own):
        if not isinstance(layer, dict):
            continue
        for k, v in layer.items():
            if k == "scan" and isinstance(v, dict):
                s["scan"].update(v)
            else:
                s[k] = v
            given.add(k)
    g = s["gains"]
    s["gains"] = [float(x) for x in (g if isinstance(g, (list, tuple)) else [g])]
    s["iterations"] = int(s["iterations"])
    if not s["gains"] or any(not (0.0 < x <= 2.0) for x in s["gains"]):
        raise BatchError(f"{camp['stem']}: optical gains {g} -- each must be in (0, 2]")
    if s["iterations"] < 1:
        raise BatchError(f"{camp['stem']}: optical iterations {s['iterations']} -- at least 1")
    if s["attribution"] not in ATTRIBUTIONS:
        raise BatchError(f"{camp['stem']}: optical attribution {s['attribution']!r} -- "
                         f"{' or '.join(repr(a) for a in ATTRIBUTIONS)}")
    s["joint_before"] = bool(s["joint_before"])
    try:
        s["name"].format_map({"stem": camp["stem"], "pass": 1})
    except (KeyError, IndexError, ValueError) as e:
        raise BatchError(f"{camp['stem']}: optical name {s['name']!r} ({e}); "
                         f"use {{stem}} and {{pass}}")
    s["_given"] = sorted(given)
    return s


def optical_line(s):
    """The optical settings as one line (dry_run, the log)."""
    n, sc = len(s["gains"]), s["scan"]
    line = (f"optical: {n} pass{'es' if n != 1 else ''} gains {'/'.join(_num(g) for g in s['gains'])}, "
            f"{s['iterations']} iterations each, scan {sc['angles']} x {sc['shots']} shots"
            + (f" (solo scans {sc['solo_angles']})" if sc.get("solo_angles") else "")
            + (f" (verify scans {sc['verify_angles']})" if sc.get("verify_angles") else "")
            + f", band {float(s['band_hz'])/1e3:g} kHz")
    if s.get("attribution", "joint") == "separate":
        line += (", attribution separate (X1 alone, X2 alone, then both to verify"
                 + ("" if s.get("joint_before", True) else "; no joint before scan") + ")")
        # A light error that follows the TOTAL rotation (an analyzer or
        # extinction term, not a crystal's own) is read in full by BOTH
        # solo scans and applied twice: at gain g the pass leaves -(2g - 1)
        # of it, so gains over 0.5 overshoot such a term and 1.0 flips its
        # sign every pass. Crystal-own errors converge at any gain <= 1.
        if len(s["gains"]) > 1:
            line += " -- pass 1 solo, later passes add the joint verification's residual"
    if not s["verify"]:
        line += ", no verification scans"
    if s.get("cap_v") is not None:
        line += f", gate cap {float(s['cap_v']):g} V"
    return line


def _scan_done(folder):
    """A polarimeter scan folder whose every step is done."""
    if not folder:
        return False
    p = os.path.join(folder, f"{os.path.basename(os.path.normpath(folder))}_scan.json")
    try:
        with open(p, encoding="utf-8") as fh:
            steps = json.load(fh)["steps"]
    except (OSError, ValueError, KeyError):
        return False
    return bool(steps) and all(st.get("status") == "done" for st in steps)


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=float)
    os.replace(tmp, path)


def _advance(cr, u, run_dir):
    """`u` becomes the drive measured next, under an iteration number of its
    own (one past the last measured), its drive CSV written and the state
    saved: every measurement keeps its own drive/measurement pair on disk."""
    cr.u = np.asarray(u, float).copy()
    last = max((r[0] for r in cr.measured), default=-1)
    if cr.iteration <= last:
        cr.iteration = last + 1
    write_iteration(cr, run_dir)
    save_state(cr)


def _ends_deg(cr):
    """The rotation at the ramp top (deg): 90 x the BASE target's peak at the
    EOM / the chain's V90 (eomilc.config, the 1 Sep calibration); None when
    the chain has no V90."""
    ch = cr.channel
    v90 = float(getattr(ch, "v90_hv", float("nan")))
    if not np.isfinite(v90) or v90 <= 0:
        return None
    base = cr.base_target if cr.base_target is not None else cr.loop.target
    return 90.0 * float(np.max(base)) * ch.mon_scale / v90


def _optical_cfg(plan, simulate, log=print):
    """The config optical.open_devices reads: the plan's Scope channels map
    (PD, MonX1, MonX2 required) and the panel's polarimeter_path.

    Simulated, the polarimeter is a scope of its own (rampol.sim), cabled
    PD / MonX1 / MonX2 where the plan's map has them (the bench's default
    channels where it has not) and NO command channel: rampol.sim derives
    its command from its monitor (0.92 x monitor / gain), which lags the
    learned, pre-emphasized drive by the chain's response, and compare's
    CmdX1-vs-drive time map then misplaces the target by tens of us -- ~3
    deg of monitors - target on the slopes of a 180 deg shape. Without it
    compare maps the monitor against the target, which in simulation is
    exact."""
    rec = plan.get("_scope_channels")
    chans = channels.from_record(rec) if rec else None
    cfg = {"scope_channels": chans or channels.normalise(channels.DEFAULT_CHANNELS)}
    if plan.get("_polarimeter_path"):
        cfg["polarimeter_path"] = plan["_polarimeter_path"]
    if simulate:
        try:
            missing = [r for r in ("PD", "MonX1", "MonX2") if channels.channel_for(cfg, r) is None]
        except ValueError:
            missing = ["(a role on two channels)"]
        if missing:
            log(f"  the Scope channels map has no {', '.join(missing)}: the simulated polarimeter "
                f"is cabled by the bench's default map")
            cfg["scope_channels"] = channels.normalise(channels.DEFAULT_CHANNELS)
        cfg["scope_channels"] = {ch: (c if c["role"] in ("PD", "MonX1", "MonX2") else {"role": "off", "name": ""})
                                 for ch, c in channels.channels_of(cfg).items()}
        log("  simulated polarimeter cabled " + ", ".join(
            f"CH{ch} {c['role']}" for ch, c in cfg["scope_channels"].items() if c["role"] != "off")
            + " (no command channel: its time map would be the simulation's, not the bench's)")
    return cfg


def _sim_drives(byc, dev, scope, dt, bridge, drives=None):
    """What optical.sim_play is given: the drives (`drives` {"EO1": u, ...}
    overrides a channel's, e.g. the park drive of a solo scan), or with `bridge` (a
    simulated run whose batch scope is eomilc.simbench's) what the simbench
    chains make of them divided by rampol.sim's own AWG -> monitor gain. The
    two simulations model the chain differently (rampol.sim: the drive x
    0.5594 / 0.5924; simbench: the measured FRFs, ~0.58 / ~0.62 at the
    hold), and a drive the ILC learned on simbench reads ~5 % off its target
    in rampol.sim's monitors -- ~10 deg at the top of a 180 deg parallel shape,
    which the polarimeter sees in its monitors too, so no optical correction
    could take it out. Bridged, the polarimeter's monitors are the ILC's."""
    out = {}
    for c in ("EO1", "EO2"):
        cr = byc[c]
        u = np.asarray((drives or {}).get(c, cr.u), float)
        if bridge and dev.sim is not None:
            ch = int(c[-1])
            u = scope.chains[cr.wiring["awg_ch"]].respond(u, dt) / float(dev.sim.awg_gain[ch])
        out[c] = u
    return out


def _optical(what, fn, *a, stop=None, **kw):
    """An eomilc.optical call: Stop (rampol's Cancelled) becomes BatchStopped,
    anything else an OpticalError naming the step."""
    try:
        return fn(*a, **kw)
    except (BatchStopped, KeyboardInterrupt):
        raise
    except Exception as e:
        if type(e).__name__ == "Cancelled" or (stop is not None and stop.is_set()):
            raise BatchStopped("stopped") from e
        raise OpticalError(f"{what}: {type(e).__name__}: {e}") from e


def optical_stage(awg, scope, runs, plan, camp, args, s, log=print, stop=None,
                  emit=lambda kind, **kw: None):
    """The optical close for one campaign whose electrical keepers are
    chosen (outputs parked on entry and on exit, never OFF). Returns (record, final):
    record is the manifest's "optical" block (also written to
    <outdir>/<stem>_optical.json, which a resumed run reads); final is
    {key: (it, rms_v, peak_v)}, the last measured iteration after the last
    applied correction per channel, or {} when nothing was applied (the
    campaign then keeps its electrical keeper).

    Per pass p (gain gains[p-1]): the correction comes from the previous
    pass's verification comparison, or for pass 1 (and without
    verification) from a scan of its own, <name>_before; it is applied to
    both channels' targets or to neither (optical.apply_correction; a
    refusal ends the stage, recorded); `iterations` iterations of the loop
    (iterate_once, both channels, the stop rules with the caps of the pass);
    then the verification scan <name> and its comparison. A pass whose
    verification scan is complete on disk and recorded done is not
    repeated. Stop raises BatchStopped (the scan on disk resumes).

    attribution "separate" (docs/OPTICAL_CLOSE_DESIGN.md, addendum section
    5): the correction is measured per crystal in every pass. The before is
    the previous pass's verification, or on the first pass a joint scan
    <name>_before (joint_before; compared jointly, NOT applied); then X1
    alone (X2's channel plays optical.park_drive of its drive, uploaded as
    PARK2; outputs stay ON) -> scan <name>_X1, compared solo="X1" -> EO1's
    file; then X2 alone (X2's keeper back on, X1 parked as PARK1) -> scan
    <name>_X2 -> EO2's file; both keepers uploaded again, both files
    applied, the loop learns, and the joint verification scan <name> reads
    the after (its own residual recorded, never applied)."""
    from eomilc import corrections as corrmod, optical
    lp = plan["loop"]
    stem = camp["stem"]
    sim = bool(getattr(args, "simulate", False))
    run_dir = plan["_run_dir"]
    outdir = s.get("outdir") or os.path.join(run_dir, "optical")
    if not os.path.isabs(outdir):
        outdir = os.path.join(run_dir, outdir)
    os.makedirs(outdir, exist_ok=True)
    rpath = os.path.join(outdir, f"{stem}_optical.json")
    try:
        with open(rpath) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        rec = {}
    rec.update(settings={k: v for k, v in s.items() if not k.startswith("_")}, outdir=outdir,
               record=rpath, simulated=sim, status="running")
    rec.pop("error", None)
    passes = rec.setdefault("passes", [])
    gains, n_it, sc = s["gains"], s["iterations"], s["scan"]
    byc = {cr.channel.name: cr for cr in runs}
    final = {}
    olog = lambda m: log("    " + str(m))                              # noqa: E731
    log(f"  {optical_line(s)}")

    def save():
        _write_json(rpath, rec)

    def end(status, why=""):
        rec["status"] = status
        if why:
            rec["error"] = why
        log(f"  optical stage {status}" + (f": {why}" if why else ""))
        save()
        emit("optical", stem=stem, step="end", status=status)
        return rec, final

    if set(byc) != {"EO1", "EO2"}:
        return end("skipped", "the optical close needs both crystals (EO1 and EO2) in the campaign")
    if s["require_keeper"]:
        bad = [f"{cr.key} {cr.reason or 'nothing measured'}" for cr in runs
               if not cr.measured or cr.reason == "diverged" or cr.reason.startswith("limit check")]
        if bad:
            return end("skipped", "no keeper to correct (" + "; ".join(bad) + ")")
    if not args.allow_output_on:
        return end("refused", "outputs are off and this run may not switch them on "
                              "(pass --allow-output-on)")

    def done_pass(i):
        p = passes[i] if i < len(passes) else None
        return bool(p) and p.get("status") == "done" and (not s["verify"] or _scan_done(p.get("verify_scan")))

    def kept_of(p):
        out = {}
        for k, it in (p.get("kept") or {}).items():
            cr = next((c for c in runs if c.key == k), None)
            hit = [r for r in (cr.measured if cr else []) if r[0] == it]
            if hit:
                out[k] = hit[-1]
        return out

    p0 = 0
    while p0 < len(gains) and done_pass(p0):
        log(f"  optical pass {p0 + 1}: done in an earlier run "
            f"({os.path.basename(passes[p0].get('verify_scan') or passes[p0].get('scan') or '')}), not repeated")
        p0 += 1
    del passes[p0:]
    if p0:
        final = kept_of(passes[p0 - 1])
    if p0 >= len(gains):
        return end("done")

    # the keepers play
    for cr in runs:
        kb = final.get(cr.key) or keeper(cr, lp)
        if kb is None:
            raise BatchError(f"{cr.stem}: no keeper to play (no measured iteration on disk)")
        _advance(cr, cr.drives[kb[0]], run_dir)
        log(f"  {cr.stem}: {'pass ' + str(p0) + ' ' if cr.key in final else ''}keeper i{kb[0]:02d} "
            f"(rms {kb[1]:.2f} V, peak {kb[2]:.1f} V) plays as i{cr.iteration:02d}")
    dev = None
    status, why = "done", ""
    try:
        # the analyzer first: a polarimeter that cannot scan (no ELL14, the old
        # scope-grab module) refuses before anything is switched on
        cfg = _optical_cfg(plan, sim, log)
        dev = _optical("polarimeter", optical.open_devices, None if sim else scope, cfg, log=olog,
                       simulate=sim, stop=stop)
        for cr in runs:
            name, n, frac = upload(awg, cr, args.gui_names, log)
            log(f"  {cr.stem} i{cr.iteration:02d} uploaded as {name} ({n} pts, {100*frac:.1f}% of DAC range)")
        for cr in runs:
            if not awg.is_on(cr.wiring["awg_ch"]):
                awg.set_output(cr.wiring["awg_ch"], True)
            if not awg.is_on(cr.wiring["awg_ch"]):
                raise BatchError(f"CH{cr.wiring['awg_ch']} did not switch on")
        log("  outputs ON: " + ", ".join(f"CH{cr.wiring['awg_ch']}" for cr in runs))
        time.sleep(lp["settle_s"])
        check_alignment(scope, runs, plan, log)
        pd = float(s["pd_delay_us"])
        bridge = False
        if sim:
            dev.sim.rot_err = float(s["sim_rot_err"])
            log(f"  simulated light: the monitors' rotation + a {dev.sim.rot_err:g} deg rotator error "
                f"(the light-only term the closure has to remove)")
            bridge = bool(s["sim_bridge"]) and all(hasattr(getattr(scope, "chains", {}).get(cr.wiring["awg_ch"]),
                                                           "respond") for cr in runs)
            if bridge:
                log("  simulated polarimeter: it plays what the simulated ILC chains make of the drives "
                    "(their monitor / its own gain), so both simulations read the same monitors")
            else:
                log("  simulated polarimeter: its monitors are the drive x its own chain gain (rampol.sim), "
                    "not the simulated ILC chain's -- light - target carries their difference, "
                    "light - monitors is what the closure acts on")
            if "pd_delay_us" not in s["_given"]:
                pd = 0.0
                log("  simulated PD has no delay: compared at PD delay 0 us (1.7 us is the bench's)")
        e1, e2 = _ends_deg(byc["EO1"]), _ends_deg(byc["EO2"])
        ends = {"X1": e1, "X2": e2} if e1 is not None and e2 is not None else None
        if ends:
            log(f"  ramp tops: X1 {e1:.1f} deg, X2 {e2:.1f} deg (from the targets' peaks and V90)")
        t, dt = byc["EO1"].t, float(byc["EO1"].loop.dt)
        states = {"x1": byc["EO1"].state_path, "x2": byc["EO2"].state_path}
        gate = None
        if s.get("cap_v") is not None:
            gate = corrmod.GateSettings(band_hz=float(s["band_hz"]),
                                        **dict(optical.GATE_DEFAULTS, cap_out=float(s["cap_v"])))
        cancelled = (lambda: stop is not None and stop.is_set())

        def do_scan(name, p, drives=None, solo=None, kind="before"):
            # drives: a channel's override (a solo scan's park drive); solo: the crystal that drives;
            # kind: before / solo / verify -- the solo and verify scans may use their own angles
            ang = scan_angles(sc, kind)
            optical.sim_play(dev, _sim_drives(byc, dev, scope, dt, bridge, drives), dt=dt)
            emit("optical", stem=stem, step="scan", scan=name, **{"pass": p})
            log(f"  scan {name}: analyzer {ang} deg x {sc['shots']} shots, "
                + (f"{int(sc['points'])} points" if sc.get("points") else
                   f"readout at {float(sc.get('sample_us') or 2.0):g} us"))
            xb = {"plan": plan.get("_path", ""), "campaign": stem, "pass": p, "gain": gains[p - 1]}
            if solo:
                xb.update(attribution="separate", solo=solo,
                          parked={c: optical.PARK_NAMES[k] for c, k in (("EO1", "X1"), ("EO2", "X2"))
                                  if k != solo})
            # a solo scan's hold-null angles come from the driving crystal's
            # rotation alone (the parked one adds none)
            ends_k = ({solo: ends[solo], ("X2" if solo == "X1" else "X1"): 0.0}
                      if (solo and ends) else ends)
            folder = _optical(f"scan {name}", optical.scan, dev, outdir, name, t, log=olog,
                              cancelled=cancelled, angles=ang, shots=int(sc["shots"]),
                              points=(int(sc["points"]) if sc.get("points") else None),
                              sample_us=float(sc.get("sample_us") or 2.0), ref_every=int(sc["ref_every"]),
                              hold_null=bool(sc["hold_null"]), ends_deg=ends_k,
                              settle_s=float(sc["settle_s"]), states=states, stop=stop,
                              extra={"ilc_batch": xb})
            log(f"  scan {name}: {folder}")
            return folder

        def do_compare(folder, p, when, solo=None):
            emit("optical", stem=stem, step="compare", scan=os.path.basename(folder), **{"pass": p})
            log(f"  compare {os.path.basename(folder)}: band {float(s['band_hz'])/1e3:g} kHz, "
                f"PD delay {pd:g} us, "
                + (f"{solo} alone (the whole correction to {solo})" if solo else f"split {float(s['split']):g}"))
            summ = _optical(f"compare {os.path.basename(folder)}", optical.compare, folder,
                            states["x1"], states["x2"], band_hz=float(s["band_hz"]), pd_delay_us=pd,
                            split=float(s["split"]), log=olog, figs=bool(s["figs"]), solo=solo, stop=stop)
            v = optical.verdict(summ)
            for line in optical.verdict_lines(v):
                log(f"  {when}: {line}")
            return summ, v

        def play_keepers(why):
            # both keepers back on the generator, both play (FRQ and the outputs as they are)
            for cr in runs:
                name_, n_, frac_ = upload(awg, cr, args.gui_names, log)
                log(f"  {cr.stem} i{cr.iteration:02d} uploaded as {name_} ({n_} pts, "
                    f"{100*frac_:.1f}% of DAC range){why}")

        def solo_scans(name, p, prec):
            """Attribution "separate": X1 alone with X2 parked, then X2 alone
            with X1 parked, each compared alone. Returns the files {EOn: corr}
            and the played targets, each from its own crystal's scan."""
            import ilc_bench
            files, played = {}, {}
            prec.update(solo_scans={}, verdicts_solo={}, parked={})
            for k, other in (("X1", "X2"), ("X2", "X1")):
                me, pk = byc["EO" + k[-1]], byc["EO" + other[-1]]
                if k == "X2":
                    # CH of X2 holds PARK2 since X1's scan: its keeper goes back on
                    name_, n_, frac_ = upload(awg, me, args.gui_names, log)
                    log(f"  {me.stem} i{me.iteration:02d} uploaded as {name_} ({n_} pts, "
                        f"{100*frac_:.1f}% of DAC range): {k} drives")
                park = optical.park_drive(pk.u)
                pname = optical.PARK_NAMES[other]
                n_, frac_ = ilc_bench.upload_drive(awg, pk.wiring["awg_ch"], pname, park, pk.full_scale)
                _played(awg, pk.wiring["awg_ch"], other, park, pk.full_scale, parked=True)
                log(f"  {other} parked: CH{pk.wiring['awg_ch']} plays {pname}, flat at its idle "
                    f"{float(park[0]):+.3f} V ({n_} pts; the output stays ON)")
                prec["parked"][other] = dict(name=pname, idle_V=float(park[0]))
                sname = f"{name}_{k}"
                folder = do_scan(sname, p, drives={pk.channel.name: park}, solo=k, kind="solo")
                prec["solo_scans"][k] = folder
                save()
                summ_k, v_k = do_compare(folder, p, f"{k} alone", solo=k)
                c = me.channel.name
                if summ_k["files"].get(c):
                    files[c] = summ_k["files"][c]
                    played[c] = (summ_k.get("played") or {}).get(c, "")
                prec["verdicts_solo"][k] = v_k
                save()
            return files, played

        carry = None                  # this run's previous verification: (folder, summary, verdict)
        separate = s.get("attribution", "joint") == "separate"     # per crystal: X1 alone, X2 alone
        for i in range(p0, len(gains)):
            p, g = i + 1, gains[i]
            name = s["name"].format_map({"stem": stem, "pass": p})
            prec = {"pass": p, "gain": g, "name": name, "status": "running",
                    "started": f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"}
            passes.append(prec)
            save()
            log(f"  optical pass {p}/{len(gains)}: gain {_num(g)}")
            prev = passes[i - 1] if i > 0 else None
            slot = None                   # the file's own slot ("optical") unless a joint residual is added
            if separate:
                # the before (the table's): the previous pass's verification,
                # else a joint scan of its own (joint_before). Light minus
                # monitors is a fresh estimate of the WHOLE error every time
                # (moving the target moves the monitors with the light), so a
                # second solo pass would only repeat the first. Pass 1 measures
                # each crystal alone and its files REPLACE the optical slot;
                # pass 2 on applies the joint verification's residual, split,
                # ADDED beside it in its own slot (14 Oct 2026).
                bsrc, vb, joint = None, None, None
                if carry is not None:
                    bsrc, jsumm, vb = carry
                    joint = jsumm
                    log(f"  before: pass {p - 1}'s verification ({os.path.basename(bsrc)})")
                elif prev and prev.get("verdict_after") is not None:
                    bsrc, vb = prev.get("verify_scan"), prev["verdict_after"]
                    vf = prev.get("verify_corr_files") or {}
                    if vf and all(os.path.exists(f) for f in vf.values()):
                        joint = {"files": vf, "played": prev.get("verify_played", {})}
                    log(f"  before: pass {p - 1}'s verification ({os.path.basename(bsrc or '')}, on disk)")
                elif s["joint_before"]:
                    bsrc = do_scan(name + "_before", p)
                    _, vb = do_compare(bsrc, p, "before")
                    log(f"  {os.path.basename(bsrc)}: both crystals, the before only (not applied)")
                else:
                    log("  no joint before scan (joint_before false): the table has no before")
                prec.update(attribution="separate", before_scan=bsrc, verdict_before=vb)
                save()
                if i > 0 and joint is not None and joint.get("files"):
                    slot = f"joint{p}"
                    log(f"  pass {p}: the joint verification ({os.path.basename(bsrc or '')}) says what the whole "
                        f"correction should be; its files minus each crystal's share of what is already applied "
                        f"go in slot {slot} beside the solo correction")
                    totals = {c: (np.asarray(byc[c].corr_total, float) * byc[c].channel.mon_scale
                                  if byc[c].corr_total is not None else np.zeros(len(t)))
                              for c in ("EO1", "EO2")}
                    jdir = os.path.join(os.path.dirname(next(iter(joint["files"].values()))))
                    files = optical.joint_residual_files(joint["files"], totals, float(s["split"]), jdir, slot, log=log)
                    played = dict(joint.get("played") or {})
                    prec.update(corr_files=dict(files), played_targets=dict(played), slot=slot,
                                source="joint verification")
                else:
                    files, played = solo_scans(name, p, prec)
                    prec.update(corr_files=dict(files), played_targets=dict(played), slot="optical",
                                source="solo scans")
                    save()
                    play_keepers(": both play")
                save()
            else:
                if carry is not None:
                    src, summ, vb = carry
                    log(f"  the correction is pass {p - 1}'s verification ({os.path.basename(src)})")
                elif prev and s["verify"] and prev.get("verify_corr_files") and all(
                        os.path.exists(f) for f in prev["verify_corr_files"].values()):
                    src = prev["verify_scan"]
                    summ = {"files": prev["verify_corr_files"], "played": prev.get("verify_played", {})}
                    vb = prev.get("verdict_after")
                    log(f"  the correction is pass {p - 1}'s verification ({os.path.basename(src)}, on disk)")
                elif prev and s["verify"] and _scan_done(prev.get("verify_scan")):
                    src = prev["verify_scan"]
                    summ, vb = do_compare(src, p, "before")
                else:
                    src = do_scan(name + "_before", p)
                    summ, vb = do_compare(src, p, "before")
                files = summ["files"]
                prec.update(scan=src, corr_files=dict(summ["files"]),
                            played_targets=dict(summ.get("played") or {}), verdict_before=vb)
                save()
            # both targets move, or neither
            emit("optical", stem=stem, step="apply", **{"pass": p})
            snap = {c: (np.array(cr.loop.target, float),
                        dict(cr.loop.history[-1]) if cr.loop.history and isinstance(cr.loop.history[-1], dict)
                        else None) for c, cr in byc.items()}
            applied = {}
            try:
                for c in ("EO1", "EO2"):
                    cr = byc[c]
                    path = files.get(c)
                    if not path:
                        raise optical.CorrectionRefused([("fail", f"the comparison wrote no correction for {c}")])
                    try:
                        applied[c] = optical.apply_correction(cr.loop, cr.t, cr.u, cr.full_scale, path, gain=g,
                                                              settings=gate, base_target=cr.base_target,
                                                              layers=cr.corr_layers, slot=slot)
                    except optical.CorrectionRefused as e:
                        e.channel = cr.key
                        raise
            except optical.CorrectionRefused as e:
                for c, (tg, h) in snap.items():
                    byc[c].loop.target = tg
                    if h is not None:
                        byc[c].loop.history[-1] = h
                fails = [t_ for lev, t_ in e.notes if lev == "fail"]
                who = getattr(e, "channel", "")
                prec.update(status="refused", refused_channel=who, refused=fails,
                            notes=[list(n_) for n_ in e.notes])
                for f_ in fails:
                    log(f"  {who} correction REFUSED: {f_}")
                status, why = "refused", f"pass {p}: {who} correction refused: " + "; ".join(fails)
                break
            prec["applied"] = {}
            for c, a in applied.items():
                cr = byc[c]
                cr.base_target, cr.corr_total, cr.corr_layers = a.base_target, a.corr_total, a.layers
                if cr.corr_from is None:
                    cr.corr_from = cr.iteration     # earlier iterations were measured against the base
                path = os.path.abspath(files[c])
                L = next((L for L in a.layers if L.get("path") == path), a.layers[-1])
                L["from_it"] = int(cr.iteration)     # the panel tags the iterations after it
                moved = float(np.max(np.abs(a.corr_total))) * cr.channel.mon_scale
                prec["applied"][cr.key] = dict(file=path, gain=g, kept_peak_V=float(L.get("kept_peak", 0.0)),
                                               resolved=float(L.get("resolved", 0.0)), status=L.get("status", ""),
                                               target_moved_V=moved,
                                               notes=[list(n_) for n_ in a.notes if n_[0] not in ("ok", "info")])
                log(f"  {cr.stem}: {os.path.basename(path)} applied x{_num(g)}: {float(L.get('kept_peak', 0)):.1f} V "
                    f"peak kept at the EOM, {100*float(L.get('resolved', 0)):.0f}% resolved; target = base "
                    f"+ {moved:.1f} V peak" + (f" (from {os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))})"
                                               if separate else ""))
                for lev, txt in a.notes:
                    if lev not in ("ok", "info"):
                        log(f"    {lev}: {txt}")
            save()
            # the loop learns the moved targets
            caps, first = {}, {}
            for cr in runs:
                _advance(cr, cr.u, run_dir)
                cr.active, cr.reason, cr.bad_streak = True, "", 0
                cr.since, cr.run_id = len(cr.measured), time.time()
                first[cr.key] = cr.iteration
                caps[cr.key] = dict(lp, max_iterations=cr.iteration + n_it - 1,
                                    min_iterations=cr.iteration + min(int(lp["min_iterations"]), n_it - 1))
            emit("optical", stem=stem, step="learn", **{"pass": p})
            log(f"  {n_it} iteration{'s' if n_it != 1 else ''} on the corrected targets")
            for _ in range(n_it + 1):
                if not any(cr.active for cr in runs):
                    break
                iterate_once(awg, scope, runs, plan, args, log, stop, emit, stem=stem, loop_for=caps)
            kept = {}
            for cr in runs:
                kb = keeper(cr, caps[cr.key]) if cr.reason == "diverged" else cr.measured[-1]
                kept[cr.key] = kb
                log(f"  {cr.stem}: pass {p} keeps i{kb[0]:02d} (rms {kb[1]:.2f} V, peak {kb[2]:.1f} V), "
                    f"stopped: {cr.reason}")
            prec.update(iterations={k: [first[k], kept[k][0]] for k in kept},
                        kept={k: kb[0] for k, kb in kept.items()},
                        kept_rms_v={k: kb[1] for k, kb in kept.items()},
                        stops={cr.key: cr.reason for cr in runs})
            final = dict(kept)
            save()
            if s["verify"]:
                vfolder = do_scan(name, p, kind="verify")
                vs, va = do_compare(vfolder, p, "after")
                prec.update(verify_scan=vfolder, verify_corr_files=dict(vs["files"]),
                            verify_played=dict(vs.get("played") or {}), verdict_after=va)
                carry = (vfolder, vs, va)
                if separate:
                    log(f"  {os.path.basename(vfolder)}: both crystals; its own residual is reported, not applied "
                        f"(the next pass measures each crystal alone again)")
            else:
                prec["verdict_after"] = None
                carry = None
            prec.update(status="done", finished=f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
            save()
    except (OpticalError, BatchError) as e:
        status, why = "error", str(e)
        if passes and passes[-1].get("status") == "running":
            passes[-1].update(status="error", error=str(e))
        log(f"  optical stage FAILED: {e}")
    finally:
        try:
            park_runs(awg, runs, log, "optical stage over")
        finally:
            if dev is not None:
                optical.close_devices(dev)
            save()
    return end(status, why if status != "done" else "")


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


def optical_estimate(plan, s, per_it, ncamp_channels=2):
    """Seconds an optical stage with settings `s` takes, from the plan's timing:
    the scans (each step: the shots at the trigger rate, every channel read per
    shot, a mount move and the settle), the comparisons and the learning
    iterations. Returns (seconds, scans, learn iterations)."""
    if not s:
        return 0.0, 0, 0
    tm, sc = plan["timing"], s["scan"]
    shots = int(sc["shots"])
    step_s = (shots * (1.0 / tm["trigger_hz"] + tm["read_s_per_channel"] * (ncamp_channels + 2))
              + float(sc.get("settle_s", 1.0)) + tm.get("scan_step_overhead_s", 2.0))

    def scan_s(kind):
        n_ang = n_angles(scan_angles(sc, kind))
        steps = n_ang + (2 if sc.get("hold_null", True) else 0) + n_ang // max(1, int(sc.get("ref_every", 9)))
        return steps * step_s + tm.get("compare_s", 10.0)
    passes = len(s["gains"])
    kinds = []
    if s.get("attribution", "joint") == "separate":
        kinds += (["before"] if s.get("joint_before", True) else []) + ["solo", "solo"]
    else:
        kinds += ["before"]
    kinds += ["verify"] * (passes if s["verify"] else 0)
    learn = passes * int(s["iterations"])
    total = sum(scan_s(k) for k in kinds) + learn * per_it
    return total, len(kinds), learn


def scan_angles(sc, kind):
    """The analyzer angles of a scan of `kind` (before / solo / verify): the
    kind's own when the settings give them, else the before scan's."""
    key = {"solo": "solo_angles", "verify": "verify_angles"}.get(kind)
    return (sc.get(key) if key else None) or sc["angles"]


def n_angles(angles):
    try:
        a, b, step = (float(x) for x in str(angles).split(":"))
        return int((b - a) // step) + 1
    except (ValueError, TypeError):
        return 16


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


def model_line(plan, key):
    """One line saying what inverse this channel learns with, and where the
    numbers came from -- the two chains are not the same amplifier, so the
    plan is checked channel by channel."""
    plant, loopp, rec, src = _settings_for(plan, key)
    if rec["model"] == "frf":
        what = (f"measured FRF {os.path.basename(rec['frf'])} full to {rec['f_use']/1e3:g} kHz, "
                f"taper to {rec['f_max']/1e3:g} kHz")
    else:
        prm = [f"gain {plant['gain']:.4f}" if plant.get("gain") else "gain ?"]
        if plant.get("fn"):
            prm.append(f"fn {plant['fn']:.0f} Hz")
        if plant.get("zeta"):
            prm.append(f"zeta {plant['zeta']:.3f}")
        if plant.get("tau"):
            prm.append(f"tau {plant['tau']*1e6:.2f} us")
        what = f"{rec['model'] or 'parametric'}: " + ", ".join(prm)
    notches = ", ".join(f"{f0/1e3:g}k/{bw:g}" for f0, bw in loopp["notches"]) or "none"
    return (f"model {key}: {what}; gamma {loopp['gamma']:g}, f_cut {loopp['f_cut']/1e3:g} kHz, "
            f"notches {notches} ({src})")


def dry_run(plan, log=print):
    lp = plan["loop"]
    log(f"plan {plan.get('name', '')}: {len(plan['campaigns'])} campaigns, targets in {plan['_targets_dir']}")
    early = lp.get("repeats_early")
    early = int(lp["repeats"]) if early is None else max(2, int(early))
    log(f"loop: {lp['max_iterations']} iterations max (min {lp['min_iterations']}), stop at rms <= {lp['stop_rms_v']} V, "
        f"{lp['repeats']} shots per measurement"
        + (f" ({early} while rms > {float(lp.get('early_rms_v', 2.0)):g} V)" if early != int(lp["repeats"]) else ""))
    pd = pd_channel(plan)
    log(f"PD: CH{pd} recorded every iteration (meas_<stem>PD_iNN.npy); the analyzer angle is read at "
        f"each campaign start" if pd is not None else "PD: no PD channel in the Scope channels map, not recorded")
    for k, w in plan["wiring"].items():
        rb = f"readback CH{w['drive_ch']}" if w.get("drive_ch") else "no readback: alignment not verified"
        log(f"wiring {k}: AWG CH{w['awg_ch']} -> monitor CH{w['mon_ch']}, {rb}")
    for n in plan.get("_wiring_notes", []):
        log(f"  note: {n}")
    for n in plan.get("_echo_notes", []):
        log(f"echo {n}")
    for k in ("X1", "X2"):
        try:
            log(model_line(plan, k))
        except BatchError as e:
            log(f"model {k}: REFUSED: {e}")
    rows = plan_rows(plan)
    bad_opt = []
    for r in rows:
        if not r["n"]:
            log(f"  {r['stem']:8s} REFUSED: {r['error']}")
            continue
        log(f"  {r['stem']:8s} {r['n']:5d} pts, {r['period_ms']:7.3f} ms (FRQ {1e3/r['period_ms']:8.3f} Hz), "
            f"~{r['est_s']/60:4.1f} min | " + " | ".join(r["lines"]))
    for camp in plan["campaigns"]:
        try:
            s = optical_settings(plan, camp)
        except BatchError as e:
            log(f"  {camp['stem']:8s} optical: REFUSED: {e}")
            bad_opt.append(camp["stem"])
            continue
        if s is not None:
            log(f"  {camp['stem']:8s} {optical_line(s)}")
    bad = sum(not r["ok"] or r["stem"] in bad_opt for r in rows)
    log(f"total ~{sum(r['est_s'] for r in rows)/3600:.1f} h at {lp['max_iterations']} iterations each "
        f"(early stops shorten it; an optical stage adds its scans); {bad} campaign(s) with problems")
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
        awg, scope, mod = simbench.make_bench(frfs, full_scale=plan["loop"]["full_scale"],
                                              channels=plan.get("_scope_channels") or None)
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
    """Both outputs OFF: only on the explicit --outputs-off-at-end (named in
    the typed confirmation / the window's dialog). Everything else parks."""
    for c in (1, 2):
        try:
            if awg.is_on(c):
                awg.set_output(c, False)
                log(f"CH{c} output OFF ({why})")
        except Exception as e:
            log(f"could not switch CH{c} off: {e}")


def run_batch(plan, opts, log=print, stop=None, on_event=None, bench=None):
    """Run a loaded plan's campaigns in order; returns the manifest.

    opts: .simulate, .allow_output_on, .redo, .gui_names (and .no_optical,
    absent = False: the plan's optical stage runs). `stop` is a
    threading.Event checked before every shot; `on_event(kind, info)` hears
    "batch", "campaign", "iteration", "saved", "optical" (a step of the
    optical stage: stem, step, pass), "campaign_end" and "batch_end"
    (it runs on the batch's thread and must not block). `bench` = (awg, scope)
    replaces the instruments: the GUI passes the pair it opened, the tests a
    simulated pair they can inspect. The instruments are closed at the end.

    Switching outputs on needs opts.allow_output_on; asking the person is the
    caller's job (the CLI's typed YES, the GUI's dialog). Outputs are never
    switched off: a refusal, an error, Stop and the end park every channel
    the batch played on (park_all), unless opts.outputs_off_at_end (absent =
    False) asks for both OFF at the very end."""
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
    # what each campaign should take, for the window's progress and time left
    est = {}
    for camp in plan["campaigns"]:
        e_s, per_it = estimate(plan, 0, len(camp.get("channels", ["X1", "X2"])))
        try:
            o_s, scans, learn = optical_estimate(plan, optical_settings(plan, camp), per_it)
        except BatchError:
            o_s, scans, learn = 0.0, 0, 0
        est[camp["stem"]] = dict(electrical_s=e_s, per_it_s=per_it, optical_s=o_s, scans=scans,
                                learn_it=learn, max_it=int(plan["loop"]["max_iterations"]))
    emit("batch", todo=todo, n=len(plan["campaigns"]), run_dir=plan["_run_dir"], simulated=bool(opts.simulate),
         plan_est=est, started=time.time())
    awg, scope = bench if bench is not None else open_bench(plan, opts.simulate, say)
    for k in [k for k in _PLAYED if k[0] == id(awg)]:
        del _PLAYED[k]                   # a fresh instrument: nothing known to be playing
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
                o_ = entry.get("optical") or {}
                if o_ and o_.get("status") not in (None, "done", "skipped"):
                    # the electrical part is done; the stage is not: a plain
                    # rerun comes back to it (the states resume, done passes
                    # are not repeated)
                    entry["status"] = f"optical {o_.get('status')}"
                    say(f"  campaign recorded as '{entry['status']}'"
                        + (f": {o_.get('error')}" if o_.get("error") else "")
                        + " -- a plain rerun repeats the stage")
                else:
                    entry["status"] = "done"
            except BatchError as e:
                entry.update(status="refused", error=str(e))
                say(f"  REFUSED: {e}")
                park_all(awg, say, "campaign refused")
            except (BatchStopped, KeyboardInterrupt):
                entry["status"] = "stopped"
                raise
            except Exception as e:
                # an instrument or file error (a VISA timeout, a read that
                # failed three times) ends THIS campaign, parked, and the
                # batch goes on to the next: an overnight run must not end on
                # one bad read (14 Oct 2026)
                entry.update(status="error", error=f"{type(e).__name__}: {e}")
                say(f"  ERROR, campaign abandoned: {type(e).__name__}: {e}")
                for line in traceback.format_exception(type(e), e, e.__traceback__):
                    say("    " + line.rstrip())
                park_all(awg, say, "campaign error")
            finally:
                entry["minutes"] = round((time.time() - t0) / 60, 2)
                manifest["campaigns"][stem] = entry
                _write_json(mpath, manifest)
                emit("campaign_end", stem=stem, entry=entry)
    except (BatchStopped, KeyboardInterrupt):
        say("\nstopped -- states are saved after every iteration; run again to resume")
    finally:
        if getattr(opts, "outputs_off_at_end", False):
            _all_off(awg, say, "end of batch, --outputs-off-at-end")
        else:
            park_all(awg, say, "end of batch")
        awg.close()
        scope.close()
        _write_json(mpath, manifest)
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
        print(f"This batch will switch AWG CH1 and CH2 ON for {len(plan['campaigns'])} campaigns, driving "
              f"the Treks with the planned ramps. The outputs stay ON: between campaigns, on an error and "
              f"at the end each channel parks on a flat record at its idle (never-float).")
        if getattr(a, "outputs_off_at_end", False):
            print("--outputs-off-at-end: at the very end BOTH CH1 and CH2 are switched OFF -- their inputs "
                  "then float, so any stage they feed (X2's preconditioning stage) must be bypassed.")
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
    r.add_argument("--outputs-off-at-end", action="store_true",
                   help="switch BOTH outputs OFF at the very end instead of parking them (their inputs "
                        "then float: only with every stage they feed bypassed)")
    r.add_argument("--redo", action="store_true", help="repeat campaigns the manifest lists as done")
    r.add_argument("--no-optical", action="store_true",
                   help="skip the plan's optical stage (the campaigns end at their electrical keepers)")
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
