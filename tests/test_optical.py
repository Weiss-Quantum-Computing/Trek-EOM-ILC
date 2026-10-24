#!/usr/bin/env python3
"""eomilc.optical on the simulated polarimeter: two loops built from a suite
target pair as ilc_batch builds them, their flat first shots played into the
rampol.sim bench, a scan, the comparison, the corrections applied, refused
and replaced, and the verdict. Needs the ramp-polarimeter checkout beside
this one, scipy (Anaconda python), and a target_<stem>X1/X2.csv pair - the
2026-09-28 suite's targets folder as argv[1] (that folder by default when it
is on this PC):

    C:\\ProgramData\\anaconda3\\python.exe tests\\test_optical.py <suite>/targets
"""
import math
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ilc_batch, run_ilc                                        # noqa: E402
from eomilc import channels as chm, corrections as corrmod       # noqa: E402
from eomilc import ilc, optical, plant as plantmod               # noqa: E402
from eomilc.config import CHANNELS                               # noqa: E402
RAMPOL_AT_IMPORT = "rampol" in sys.modules

DEFAULT_TARGETS = os.path.join(os.path.expanduser("~"), "OneDrive - The Pennsylvania State University",
                               "Share Data", "Ramps", "suite-20260928", "targets")
TARGETS = (os.path.abspath(sys.argv[1]) if len(sys.argv) > 1
           else (DEFAULT_TARGETS if os.path.isdir(DEFAULT_TARGETS) else None))
STEM = "S25"
# the two chains' models as the batch's two-model test gives them
MODELS = {"X1": ("EO1", 0.557, 7823.0, 1.47), "X2": ("EO2", 0.6116, 8509.0, 1.83)}
fails = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        fails.append(what)


def finite(x):
    """Every number in a nested dict / list is finite."""
    if isinstance(x, dict):
        return all(finite(v) for v in x.values())
    if isinstance(x, (list, tuple)):
        return all(finite(v) for v in x)
    if isinstance(x, (int, float, np.floating)) and not isinstance(x, bool):
        return math.isfinite(float(x))
    return True


def build(key, run_dir):
    """A ChannelRun as ilc_batch.build_run makes a fresh one, one measured
    iteration in its history, saved as the batch saves it."""
    ch_name, gain, fn, zeta = MODELS[key]
    ch = CHANNELS[ch_name]
    tpath = os.path.join(TARGETS, f"target_{STEM}{key}.csv")
    t, v = run_ilc.load_target(tpath, ch.mon_scale)
    dt = float(np.median(np.diff(t)))
    p = plantmod.Plant(gain=gain, fn=fn, zeta=zeta, dt=dt)
    loop = ilc.Loop(plant=p, target=v, dt=dt, channel=ch, gamma=0.6, f_cut=20e3,
                    limits=ch.limits)
    u = loop.first_shot(flat=True)
    loop.history.append(loop.metrics(p.forward(u)))
    cr = ilc_batch.ChannelRun(key=key, wiring={}, stem=f"{STEM}{key}", loop=loop, t=t, u=u,
                              iteration=1, full_scale=10.0, t_off=0.0,
                              state_path=os.path.join(run_dir, f"drive_{STEM}{key}.state.npz"),
                              target_path=os.path.abspath(tpath))
    ilc_batch.save_state(cr)
    return cr


def main():
    if not TARGETS or not os.path.isfile(os.path.join(TARGETS, f"target_{STEM}X1.csv")):
        sys.exit(__doc__ + f"\nno target_{STEM}X1.csv / X2.csv pair found"
                 + (f" in {TARGETS}" if TARGETS else " (give the folder as argv[1])"))
    tmp = tempfile.mkdtemp(prefix="optical_")
    msgs = []
    log = msgs.append
    try:
        run_dir, scans = os.path.join(tmp, "run"), os.path.join(tmp, "scans")
        os.makedirs(run_dir)
        os.makedirs(scans)
        runs = {k: build(k, run_dir) for k in ("X1", "X2")}
        t = runs["X1"].t
        dt = runs["X1"].loop.dt

        print("[1] import and simulated devices")
        check(not RAMPOL_AT_IMPORT, "eomilc.optical imports without rampol")
        cfg = {"scope_channels": chm.DEFAULT_CHANNELS}
        dev = optical.open_devices(None, cfg, log=log, simulate=True, pcfg={})
        check(dev.sim is not None and dev.roles.get(1) == "PD" and dev.channels[3][0] == "MonX1",
              "simulated bench with the ILC's role map (PD CH1, MonX1 CH3)")
        # the simulated light is the monitors' rotation unless the rotator
        # carries an error: give the closure something to correct
        dev.sim.rot_err = 0.5
        drives = {"EO1": runs["X1"].u, "EO2": runs["X2"].u}
        optical.sim_play(dev, drives, dt=dt)
        per, x = dev.sim.awg_drive[1]
        check(abs(per - len(t) * dt) < 1e-12 and np.array_equal(x, runs["X1"].u)
              and all(dev.sim.awg_on.values()), "sim_play puts both drives into the bench (period N dt)")
        m1, m2 = dev.sim.monitors(np.array([t[len(t) // 2]]))
        check(abs(m1[0] - runs["X1"].u[len(t) // 2] * dev.sim.awg_gain[1]) < 1e-9,
              f"the simulated monitor follows the drive ({m1[0]:.3f} V mid-record)")
        before = dict(dev.scope.inst.state)

        print("[2] scan")
        folder = optical.scan(dev, scans, f"{STEM}_opt0", t, log=log, angles="0:359:45", shots=2,
                              states={"x1": runs["X1"].state_path, "x2": runs["X2"].state_path})
        mf = os.path.join(folder, f"{STEM}_opt0_scan.json")
        import json
        man = json.load(open(mf, encoding="utf-8"))
        steps = man["steps"]
        check(man["format"] == "rampol-scan/1" and all(s["status"] == "done" for s in steps),
              f"polarimeter scan format, {len(steps)} steps done")
        check(sum(s["kind"] == "scan" for s in steps) == 8 and len(steps[0]["files"]) == 2,
              "8 analyzer angles, 2 shots each")
        npz = [f for f in os.listdir(folder) if f.endswith(".npz")]
        check(len(npz) == 2 * len(steps), f"{len(npz)} Scope Grab captures on disk")
        check(abs(man["drive"]["record_ms"] - len(t) * dt * 1e3) < 1e-9
              and man["plan"]["mode"] == "single" and abs(man["plan"]["points"] - 8000) <= 1
              and abs(man["plan"]["sampling_us"] - 2.0) < 1e-3,
              "manifest records the record length and the sampling")
        st = dev.scope.inst.state
        check(all(st.get(k) == before.get(k) for k in (":TIMebase:REFerence", ":TIMebase:SCALe",
                                                       ":TIMebase:POSition")),
              "the scope's timebase is put back after the scan")
        w = [m for m in msgs if "scope timebase" in m]
        # 0.5 ms + 6.402 ms x 1.02 + 8 ms over 10 div = 1.50 ms/div, rounded UP: 1.6
        check(bool(w) and "1.6 ms/div from -0.50 ms" in w[-1], (w or ["no timebase line"])[-1].strip())
        again = optical.scan(dev, scans, f"{STEM}_opt0", t, log=log, angles="0:359:45", shots=2)
        check(again == folder and any("resumed" in m for m in msgs), "a scan on disk is resumed")
        check(any(m.startswith("  check done: light on the PD") for m in msgs)
              and any("PD CH1 at 1 V/div, offset +2.7 V" in m for m in msgs),
              "the pre-scan shot found light on the PD, the PD's screen from the preset")
        st = dev.scope.inst.state
        check(st.get(":TRIGger:SWEep") == before.get(":TRIGger:SWEep")
              and st.get(":CHANnel1:SCALe") == before.get(":CHANnel1:SCALe"),
              "sweep and the PD's V/div put back after the scan")
        imax = dev.sim.imax
        dev.sim.imax = 0.0                                      # the light off
        try:
            optical.scan(dev, scans, f"{STEM}_dark", t, log=log, angles="0:359:45", shots=2)
            check(False, "a dark PD refuses the scan")
        except optical.ScanRefused as e:
            check("never rises above" in str(e) and "light is off" in str(e) and e.findings,
                  f"a dark PD refuses the scan: {str(e)[:70]}")
        check(not os.path.isdir(os.path.join(scans, f"{STEM}_dark")), "nothing scanned when refused")
        dev.sim.imax = imax
        n0 = len(msgs)
        optical.scan(dev, scans, f"{STEM}_opt0", t, log=log, angles="0:359:45", shots=2, check=False)
        check(not any("check " in m for m in msgs[n0:]), "check=False skips the shot")
        # a scan taken dark (the check off) must not be resumed into a lit one
        dev.sim.imax = 0.0
        optical.scan(dev, scans, f"{STEM}_darkres", t, log=log, angles="0:359:90", shots=1, check=False)
        dev.sim.imax = imax
        try:
            optical.scan(dev, scans, f"{STEM}_darkres", t, log=log, angles="0:359:90", shots=1)
            check(False, "a dark scan on disk refuses to resume")
        except optical.ScanRefused as e:
            check("scanned with no light" in str(e) and "delete" in str(e),
                  f"a dark scan on disk refuses to resume: {str(e)[:60]}")

        print("[3] compare")
        # the simulated PD has no delay: 1.7 us (the bench's) would read as up
        # to ~0.5 deg of light - monitors on these 8 V/us edges
        s = optical.compare(folder, runs["X1"].state_path, runs["X2"].state_path, band_hz=5000.0,
                            pd_delay_us=0.0, split=0.5, log=log, figs=False)
        check(len(s["timing"]) == 1, "one leg: no phantom second leg in the 30 ms tail")
        for ch in ("EO1", "EO2"):
            p = s["files"].get(ch)
            c = corrmod.read_correction(p) if p and os.path.exists(p) else None
            check(c is not None and c.meta.get("channel") == ch
                  and os.path.basename(p) == f"corr_{STEM}X{ch[-1]}_optical.csv",
                  f"{ch}: {os.path.basename(p or '(none)')} carries channel {ch}")
            q = s["played"].get(ch)
            check(bool(q) and os.path.exists(q)
                  and os.path.basename(q) == f"target_{STEM}X{ch[-1]}_played.csv",
                  f"{ch}: played target {os.path.basename(q or '(none)')}")
        v = optical.verdict(s)
        check(finite(v) and len(v["legs"]) == 1 and set(v["correction"]["crystals"]) == {"EO1", "EO2"},
              "verdict: finite numbers, one leg, both crystals")
        L = v["legs"][0]
        check(abs(L["top_lm_mdeg"]) > 50 and v["correction"]["crystals"]["EO1"]["peak_V"] > 1.0,
              f"the simulated light error is seen: top {L['top_lm_mdeg']:+.0f} mdeg, after the fall "
              f"{L['after_fall_lm_mdeg']:+.0f} mdeg, correction "
              f"{v['correction']['crystals']['EO1']['peak_V']:.1f} V on EO1")
        for line in optical.verdict_lines(v):
            print("       " + line)
        check(math.isfinite(v["psi_rest_deg"]) and math.isfinite(s["psi_rest_deg"]) and v["zero_ok"]
              and any(l.startswith("analyzer zero: the rest polarization sits at") and "-- OK" in l
                      for l in optical.verdict_lines(v)),
              f"the comparison localises the analyzer zero: rest at {v['psi_rest_deg']:+.2f} deg, OK")

        print("[3b] the review helpers (the Batch window's optical table, the live view)")
        rec = {"passes": [{"pass": 1, "gain": 1.0, "status": "done", "before_scan": folder,
                           "verdict_before": v, "solo_scans": {"X1": folder + "_X1"},
                           "verdicts_solo": {"X1": v}, "verify_scan": folder + "_v",
                           "verdict_after": v},
                          {"pass": 2, "gain": 1.0, "status": "running",
                           "before_scan": folder + "_v", "verdict_before": v}]}
        sc = optical.record_scans(rec)
        check([x["kind"] for x in sc] == ["before", "X1", "verify"] and sc[2]["pass"] == 1
              and optical.scan_before(sc, 2) is sc[0] and sc[0]["label"] == "before (both)"
              and sc[1]["scan"] == f"{STEM}_opt0_X1",
              "record_scans lists each scan once, in order; a verification's before is the joint scan before it")
        old = {k: x for k, x in v.items() if k not in ("rest_expected_deg", "zero_off_deg")}
        old["zero_ok"] = False
        sc2 = optical.record_scans({"passes": [{"pass": 1, "before_scan": folder, "verdict_before": old}]})
        check(sc2[0]["verdict"]["zero_ok"] and abs(sc2[0]["verdict"]["zero_off_deg"]) <= optical.ZERO_TOL_DEG,
              "a record from before the crossed-zero convention gets its zero judged again")
        ss = optical.scan_summary(folder)
        check(ss is not None and ss["out"].endswith("target_compare") and set(ss["files"]) == {"EO1", "EO2"}
              and ss["scan"] == f"{STEM}_opt0", "scan_summary reads the comparison back with out and files")
        check(optical.scan_summary(os.path.join(scans, "nothing")) is None, "no comparison: None")
        wins = optical.verdict_windows(v)
        check(len(wins) >= 2 and all(w[0] < w[1] for w, _c in wins), f"verdict_windows: {len(wins)} windows")
        cache = {}
        live = optical.live_scan(folder, cache=cache)
        check(live["steps_done"] == live["steps_total"] and not live["running"] and len(live["theta"]) == 8
              and live["rotation_deg"] is not None and live["mon_deg"] is not None,
              f"live_scan: {live['steps_done']} steps, {len(live['theta'])} angles, rest {live['rest_deg']:+.2f} deg")
        rot, mon = live["rotation_deg"], live["mon_deg"]
        check(abs(float(rot.max()) - float(mon.max())) < 2.0 and float(np.max(np.abs(rot - mon))) < 5.0,
              f"the fitted light follows the monitors: tops {rot.max():.1f} / {mon.max():.1f} deg")
        live2 = optical.live_scan(folder, cache=cache)
        check(live2["steps_done"] == live["steps_done"] and len(cache) > 0, "the cache serves a reload")

        print("[4] apply")
        applied = {}
        for key, ch in (("X1", "EO1"), ("X2", "EO2")):
            cr = runs[key]
            lp = cr.loop
            base0 = np.array(lp.target, float)
            sc = lp.channel.mon_scale
            a = optical.apply_correction(lp, cr.t, cr.u, cr.full_scale, s["files"][ch])
            applied[key] = a
            moved = np.max(np.abs(lp.target - base0)) * sc
            check(np.allclose(lp.target, base0 + a.info["total_out"] / sc) and moved > 1.0
                  and np.array_equal(a.base_target, base0) and np.array_equal(a.target, lp.target),
                  f"{ch}: target moved by the gated total ({moved:.1f} V peak at the EOM), base kept")
            check(len(a.layers) == 1 and a.layers[0]["status"] == "applied"
                  and a.layers[0]["slot"] == "optical" and "applied" in a.layers[0],
                  f"{ch}: one layer, slot optical, applied")
            check(bool(lp.history[-1].get("target_changed")), f"{ch}: history[-1] target_changed")
            cap = corrmod.GateSettings(band_hz=5000.0, cap_out=1.0, **{k: v_ for k, v_ in
                                       optical.GATE_DEFAULTS.items() if k != "cap_out"})
            now = np.array(lp.target, float)
            try:
                optical.apply_correction(lp, cr.t, cr.u, cr.full_scale, s["files"][ch], settings=cap,
                                         base_target=a.base_target, layers=a.layers)
                check(False, f"{ch}: cap 1 V refused")
            except optical.CorrectionRefused as exc:
                check(any("cap" in t_ for lev, t_ in exc.notes if lev == "fail")
                      and np.array_equal(lp.target, now), f"{ch}: cap 1 V refused, target untouched")
            a2 = optical.apply_correction(lp, cr.t, cr.u, cr.full_scale, s["files"][ch], gain=0.5,
                                          base_target=a.base_target, layers=a.layers)
            check(len(a2.layers) == 1 and a2.layers[0]["gain"] == 0.5
                  and np.allclose(a2.corr_total, 0.5 * a.corr_total, atol=1e-12)
                  and np.allclose(lp.target, a.base_target + a2.corr_total),
                  f"{ch}: a second file in the slot replaces the first (x0.5, one layer)")
        other = optical.apply_correction
        try:
            other(runs["X1"].loop, t, runs["X1"].u, 10.0, s["files"]["EO2"],
                  base_target=applied["X1"].base_target)
            check(False, "an EO2 file on the EO1 loop is refused")
        except optical.CorrectionRefused as exc:
            check(any("written for EO2" in t_ for _l, t_ in exc.notes),
                  "an EO2 file on the EO1 loop is refused")

        print("[5] a corrected state compares on its base target")
        cr = runs["X1"]
        z = dict(np.load(cr.state_path, allow_pickle=True))
        z["base_target"] = applied["X1"].base_target
        z["target"] = cr.loop.target
        corrected = os.path.join(run_dir, "drive_corrected.state.npz")
        np.savez(corrected, **z)
        used = optical._base_state(corrected, os.path.join(tmp, "cmp"))
        zz = np.load(used, allow_pickle=True)
        check(used != corrected and np.array_equal(zz["target"], applied["X1"].base_target)
              and str(zz["name"]) == f"{STEM}X1", "the copy holds the base target under the same name")
        check(optical._base_state(cr.state_path, tmp) == cr.state_path,
              "an uncorrected state is used as it is")
        optical.close_devices(dev)

        print("[6] a record with two transports (a spin echo's legs) is read per transport")
        import subprocess
        etmp = os.path.join(tmp, "echo")
        os.makedirs(etmp)
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "make_echo_target.py"),
                            os.path.join(TARGETS, "target_S47X1.csv"), "--spacing-ms", "16.667",
                            "--out", etmp], capture_output=True, text=True)
        e1, e2 = (os.path.join(etmp, f"target_S47E2X{k}.csv") for k in (1, 2))
        check(r.returncode == 0 and os.path.exists(e1) and os.path.exists(e2),
              "make_echo_target wrote the S47E2 pair" + ("" if r.returncode == 0 else f": {r.stderr[-200:]}"))
        eruns = {}
        for key in ("X1", "X2"):
            ch_name, gain, fn, zeta = MODELS[key]
            ch = CHANNELS[ch_name]
            tp = e1 if key == "X1" else e2
            tt, vv = run_ilc.load_target(tp, ch.mon_scale)
            ddt = float(np.median(np.diff(tt)))
            p = plantmod.Plant(gain=gain, fn=fn, zeta=zeta, dt=ddt)
            lp = ilc.Loop(plant=p, target=vv, dt=ddt, channel=ch, gamma=0.6, f_cut=20e3, limits=ch.limits)
            uu = lp.first_shot(flat=True)
            lp.history.append(lp.metrics(p.forward(uu)))
            cr = ilc_batch.ChannelRun(key=key, wiring={}, stem=f"S47E2{key}", loop=lp, t=tt, u=uu,
                                      iteration=1, full_scale=10.0, t_off=0.0,
                                      state_path=os.path.join(run_dir, f"drive_S47E2{key}.state.npz"),
                                      target_path=os.path.abspath(tp))
            ilc_batch.save_state(cr)
            eruns[key] = cr
        et, edt = eruns["X1"].t, eruns["X1"].loop.dt
        edev = optical.open_devices(None, cfg, log=log, simulate=True, pcfg={})
        edev.sim.rot_err = 0.5
        optical.sim_play(edev, {"EO1": eruns["X1"].u, "EO2": eruns["X2"].u}, dt=edt)
        efolder = optical.scan(edev, scans, "S47E2_opt0", et, log=log, angles="0:359:45", shots=2)
        es = optical.compare(efolder, eruns["X1"].state_path, eruns["X2"].state_path, band_hz=5000.0,
                             pd_delay_us=0.0, split=0.5, log=log, figs=False)
        tr = es.get("transports") or []
        check(len(tr) == 2, f"the comparison reports {len(tr)} transports")
        lm = [t.get("landmarks_ms", [None] * 4) for t in tr]
        check(len(tr) == 2 and abs(lm[1][0] - lm[0][0] - 16.668) < 0.1,
              f"transport 2 starts {lm[1][0] - lm[0][0] if len(tr) == 2 else float('nan'):.3f} ms after transport 1")
        ev = optical.verdict(es)
        L = ev["legs"][0]
        check(len(L.get("transports", [])) == 2 and L.get("from") == "worst transport",
              "verdict carries both transports and takes the leg's extremes from the worst one")
        check(finite(L["transports"]), "per-transport numbers finite")
        lines = optical.verdict_lines(ev)
        check(sum("transport 2" in x for x in lines) == 1, "one log line per transport")
        for ch in ("EO1", "EO2"):
            c = corrmod.read_correction(es["files"][ch])
            check(len(c.delta) == len(et), f"{ch}: one correction over the whole {len(et)}-point record")
        optical.close_devices(edev)

        print("[7] one crystal alone, the other parked: the correction is attributed to it")
        pk = optical.park_drive(runs["X2"].u)
        check(len(pk) == len(runs["X2"].u) and np.all(pk == runs["X2"].u[0]),
              "park_drive: flat at the drive's first sample, same length")
        sdev = optical.open_devices(None, cfg, log=log, simulate=True, pcfg={})
        sdev.sim.rot_err = 0.5
        optical.sim_play(sdev, {"EO1": runs["X1"].u, "EO2": pk}, dt=dt)
        sfolder = optical.scan(sdev, scans, f"{STEM}_solo1_X1", t, log=log, angles="0:359:45", shots=2)
        try:
            optical.compare(sfolder, runs["X1"].state_path, runs["X2"].state_path, solo="both", log=log)
            check(False, "a bad solo value is refused")
        except ValueError as e:
            check("solo must be" in str(e), "a bad solo value is refused")
        ss = optical.compare(sfolder, runs["X1"].state_path, runs["X2"].state_path, band_hz=5000.0,
                             pd_delay_us=0.0, split=0.5, log=log, figs=False, solo="X1")
        check(ss["solo"] == "X1" and ss["driven"] == ["X1"] and ss["correction"]["split_X1"] == 1.0,
              "solo X1: the comparison knows, split forced to 1")
        check(set(ss["files"]) == {"EO1"} and os.path.exists(ss["files"]["EO1"]),
              "files: EO1's correction only")
        pk_state = os.path.join(sfolder, "analysis", "target_compare",
                                "parked_" + os.path.basename(runs["X2"].state_path))
        with np.load(pk_state, allow_pickle=True) as z:
            check(bool(z["parked"]) and np.ptp(z["target"]) == 0 and np.ptp(z["u"]) == 0,
                  "the parked state handed to the polarimeter is flat")
        check(any("X1 drove alone" in m for m in msgs) and any("X1 alone" in m and "parked" in m for m in msgs),
              "both layers log the solo attribution")
        sv = optical.verdict(ss)
        check(finite(sv) and sv["correction"]["crystals"]["EO2"]["peak_V"] == 0.0
              and sv["correction"]["crystals"]["EO1"]["peak_V"] > 0.5,
              f"verdict: EO1 {sv['correction']['crystals']['EO1']['peak_V']:.1f} V, EO2 0 V")
        lp = runs["X1"].loop
        before = np.array(lp.target, float)
        a = optical.apply_correction(lp, runs["X1"].t, runs["X1"].u, runs["X1"].full_scale, ss["files"]["EO1"],
                                     base_target=applied["X1"].base_target, layers=applied["X1"].layers)
        check(np.max(np.abs(a.target - before)) > 0, "EO1's solo correction applies to X1's loop")
        # the other way round: X2 alone
        optical.sim_play(sdev, {"EO1": optical.park_drive(runs["X1"].u), "EO2": runs["X2"].u}, dt=dt)
        sfolder2 = optical.scan(sdev, scans, f"{STEM}_solo1_X2", t, log=log, angles="0:359:45", shots=2)
        ss2 = optical.compare(sfolder2, runs["X1"].state_path, runs["X2"].state_path, band_hz=5000.0,
                              pd_delay_us=0.0, log=log, figs=False, solo="X2")
        check(ss2["solo"] == "X2" and set(ss2["files"]) == {"EO2"} and ss2["correction"]["split_X1"] == 0.0
              and finite(optical.verdict(ss2)), "solo X2: EO2's file only, finite verdict")
        optical.close_devices(sdev)
    except Exception:
        import traceback
        traceback.print_exc()
        fails.append("exception")
        print("\n".join("    | " + m for m in msgs[-15:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failure(s)" + ("".join(f"\n  {f}" for f in fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
