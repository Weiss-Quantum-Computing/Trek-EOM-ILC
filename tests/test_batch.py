#!/usr/bin/env python3
"""ilc_batch against the simulated bench (eomilc.simbench): the ordering rules,
resume, refusal and GUI compatibility. Needs the 2026-09-28 ramp suite's
targets (or any target_<stem>X1/X2 pair) -- pass the folder as argv[1].

    python tests/test_batch.py <suite>/targets
"""
import glob, json, os, re, shutil, sys, tempfile, types
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ilc_batch, ilc_bench                                     # noqa: E402
from eomilc import simbench, channels as chm                     # noqa: E402

TARGETS = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None
fails = []
# The role map every plan here loads without a cfg (the panel's config is
# redirected to it in main) and the simulated scope is cabled by.
SIM_MAP = {"1": {"role": "CmdX1", "name": "AWG CH1"}, "2": {"role": "CmdX2", "name": "AWG CH2"},
           "3": {"role": "MonX1", "name": ""}, "4": {"role": "MonX2", "name": ""}}


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        fails.append(what)


def bench(channels=None):
    """A simulated bench cabled by `channels` (default: SIM_MAP)."""
    frfs = {1: os.path.join(HERE, "run", "frf_WIDE_X1.csv"), 2: os.path.join(HERE, "run", "frf_WIDE_X2.csv")}
    awg, scope, mod = simbench.make_bench(frfs, channels=channels if channels is not None else SIM_MAP)
    ilc_bench._AWGMOD = mod
    return awg, scope


def parked(awg):
    """Never-float (ilc_batch's park): no output was ever switched OFF, and
    every channel the batch played on is ON with a flat PARK record."""
    never_off = not any(e[0] == "output" and not e[2] for e in awg.log)
    used = {e[1] for e in awg.log if e[0] == "upload"}
    flat = all(awg.ch[c]["on"] and awg.ch[c]["name"].startswith("PARK")
               and float(np.ptp(awg.ch[c]["wave"])) == 0.0 for c in used)
    return never_off and flat and bool(used)


def args(**kw):
    d = dict(plan=None, dry_run=False, simulate=True, allow_output_on=True, yes=True, redo=False, gui_names=False)
    d.update(kw)
    return types.SimpleNamespace(**d)


def write_plan(tmp, stems, name="t", **loop):
    p = {"name": name, "targets_dir": TARGETS, "run_dir": os.path.join(tmp, "run"),
         "loop": {"max_iterations": 6, "min_iterations": 3, "repeats": 6, "settle_s": 0.0, **loop},
         "campaigns": [{"stem": s} for s in stems], "card": {"n": 500}}
    path = os.path.join(tmp, "plan.json")
    json.dump(p, open(path, "w"))
    return path


def main():
    if not TARGETS:
        sys.exit(__doc__)
    tmp = tempfile.mkdtemp(prefix="ilcbatch_")
    chm.PANEL_CONFIG_PATH = os.path.join(tmp, "config.json")
    json.dump({"scope_channels": SIM_MAP}, open(chm.PANEL_CONFIG_PATH, "w"))
    try:
        print("[1] two campaigns, parallel and series")
        path = write_plan(tmp, ["S25", "SW50"])
        awg, scope = bench()
        man = ilc_batch.cmd_run(args(plan=path), bench=(awg, scope))
        ev = awg.log
        check(not any(e[0] == "FRQ_UNDER_LIVE_OUTPUT" for e in ev), "FRQ never moved under a live output")
        check(parked(awg), "never switched OFF; both outputs ON and parked flat at the end")
        for c in (1, 2):
            names = [e[2] for e in ev if e[0] == "upload" and e[1] == c and not e[2].startswith("PARK")]
            check(all(a != b for a, b in zip(names, names[1:])), f"CH{c} uploads alternate slots ({len(names)} uploads)")
            check(set(names) <= {f"BX{c}A", f"BX{c}B"}, f"CH{c} uses only its two slots")
        # every FRQ write under a live output is under its flat park: replay the log
        on, name = {1: False, 2: False}, {1: "", 2: ""}
        ok, live = True, 0
        for e in ev:
            if e[0] == "output":
                on[e[1]] = e[2]
            if e[0] == "upload":
                name[e[1]] = e[2]
            if e[0] == "frq" and on[e[1]]:
                live += 1
                ok = ok and name[e[1]].startswith("PARK")
        check(ok and live >= 2, f"FRQ moved under a live output only with its PARK record playing ({live} writes)")
        for stem in ("S25", "SW50"):
            c = man["campaigns"][stem]
            check(c["status"] == "done", f"{stem} done")
            for k in ("X1", "X2"):
                h = c["channels"][k]["history"]
                check(h[-1]["rms_v"] < 0.1 * h[0]["rms_v"], f"{stem}{k} learned ({h[0]['rms_v']:.0f} -> {h[-1]['rms_v']:.2f} V rms)")
            check(len(c.get("card", {}).get("files", [])) == 4, f"{stem} card files written")

        print("[2] rerun skips finished campaigns")
        awg2, scope2 = bench()
        ilc_batch.cmd_run(args(plan=path), bench=(awg2, scope2))
        check(not any(e[0] == "upload" for e in awg2.log), "nothing uploaded on the rerun")

        print("[3] the GUI loads a batch state")
        try:
            import ilc_gui
            s = ilc_gui.load_session(os.path.join(tmp, "run", "sim", "drive_SW50X1.state.npz"))
            check(s.iteration >= 3 and s.stem == "SW50X1", f"ilc_gui.load_session: {s.stem} at i{s.iteration:02d}, model {s.model_key}")
        except ImportError as e:
            print(f"  skip (ilc_gui not importable here: {e})")

        print("[4] an interrupted campaign resumes from its state")
        path = write_plan(tmp, ["N175"], max_iterations=6)
        awg3, scope3 = bench()
        orig = scope3.single
        count = {"n": 0}

        def flaky(wait_s=10.0):
            count["n"] += 1
            if count["n"] == 20:                 # mid-campaign
                raise KeyboardInterrupt
            return orig(wait_s)
        scope3.single = flaky
        ilc_batch.cmd_run(args(plan=path), bench=(awg3, scope3))
        check(parked(awg3), "outputs parked ON after the interrupt, never OFF")
        import run_ilc
        it = int(run_ilc.load_state(os.path.join(tmp, "run", "sim", "drive_N175X1.state.npz"))["iteration"])
        awg4, scope4 = bench()
        man = ilc_batch.cmd_run(args(plan=path), bench=(awg4, scope4))
        c = man["campaigns"]["N175"]["channels"]["X1"]
        h = c["history"]
        check(c["resumed_at"] == it and it > 0, f"resumed at i{it:02d}")
        check([r["it"] for r in h] == list(range(len(h))),
              f"the keeper sees the whole campaign (history i00..i{h[-1]['it']:02d}, earlier part restored from disk)")

        print("[5] a mis-set generator is refused, outputs stay off, the batch goes on")
        path = write_plan(tmp, ["C25", "N15"])
        awg5, scope5 = bench()
        awg5.ch[2]["amp"] = 10.0                 # AMP 10 Vpp instead of 20
        man = ilc_batch.cmd_run(args(plan=path), bench=(awg5, scope5))
        check(man["campaigns"]["C25"]["status"] == "refused" and "AMP" in man["campaigns"]["C25"]["error"],
              "C25 refused for AMP")
        check(not any(e[0] == "output" and e[2] for e in awg5.log), "nothing switched on")
        check(not any(e[0] == "output" and not e[2] for e in awg5.log), "nothing switched off")

        print("[6] without --allow-output-on nothing is switched on")
        path = write_plan(tmp, ["N20"])
        awg6, scope6 = bench()
        man = ilc_batch.cmd_run(args(plan=path, simulate=False, allow_output_on=False), bench=(awg6, scope6))
        check(man["campaigns"]["N20"]["status"] == "refused", "N20 refused")
        check(not any(e[0] == "output" and e[2] for e in awg6.log), "nothing switched on")

        print("[6b] outputs already ON without --allow-output-on: refused before anything changes")
        awg6b, scope6b = bench()
        awg6b.set_output(1, True)
        awg6b.set_output(2, True)
        n6b = len(awg6b.log)
        man = ilc_batch.cmd_run(args(plan=path, simulate=False, allow_output_on=False, redo=True),
                                bench=(awg6b, scope6b))
        check(man["campaigns"]["N20"]["status"] == "refused" and "--allow-output-on" in man["campaigns"]["N20"]["error"],
              "N20 refused, naming --allow-output-on")
        check(awg6b.log[n6b:] == [] and awg6b.is_on(1) and awg6b.is_on(2),
              "nothing uploaded, no FRQ written, both outputs left ON")

        print("[6c] --outputs-off-at-end: both OFF at the very end, parked before that")
        path6c = write_plan(tmp, ["N20"], name="t6c", max_iterations=3, min_iterations=1)
        awg6c, scope6c = bench()
        ilc_batch.cmd_run(args(plan=path6c, outputs_off_at_end=True), bench=(awg6c, scope6c))
        offs = [i for i, e in enumerate(awg6c.log) if e[0] == "output" and not e[2]]
        check(not awg6c.is_on(1) and not awg6c.is_on(2) and len(offs) == 2
              and offs[0] == len(awg6c.log) - 2, "both switched OFF, and only as the last two events")

        print("[7] Stop (the GUI's button) ends the batch cleanly, and the next run resumes")
        import threading
        path = write_plan(tmp, ["S47", "N15"], name="t7", max_iterations=6)
        plan = ilc_batch.load_plan(path)
        awg7, scope7 = bench()
        stop = threading.Event()
        orig7 = scope7.single

        def stopper(wait_s=10.0):
            if scope7.shots == 15:
                stop.set()                       # as if Stop were pressed mid-capture
            return orig7(wait_s)
        scope7.single = stopper
        events = []
        man = ilc_batch.run_batch(plan, args(), log=lambda s: None, stop=stop,
                                  on_event=lambda k, i: events.append(k), bench=(awg7, scope7))
        check(man["campaigns"]["S47"]["status"] == "stopped", "S47 marked stopped")
        check("N15" not in man["campaigns"], "N15 not started")
        check(parked(awg7), "outputs parked ON after Stop, never OFF")
        check(events[0] == "batch" and events[-1] == "batch_end" and "iteration" in events,
              "events: batch ... iteration ... batch_end")
        plan = ilc_batch.load_plan(path)
        man = ilc_batch.run_batch(plan, args(), log=lambda s: None, bench=bench())
        check(man["campaigns"]["S47"]["status"] == "done" and man["campaigns"]["N15"]["status"] == "done",
              "the rerun finishes both")

        print("[8] build_plan")
        out = os.path.join(tmp, "p8.json")
        plan = ilc_batch.build_plan(TARGETS, ["S25", "D2N1400"], out, "p8", card=2500)
        check(plan["campaigns"][1].get("awg") == "D2N14" and plan["card"]["n"] == 2500,
              "long stem cut to 5 characters, card recorded")
        check(ilc_batch.load_plan(out)["_targets_dir"] == os.path.normpath(TARGETS), "targets_dir relative to the plan")
        for bad, why in ((["S25", "XXX"], "missing"), (["S25", "S25"], "twice")):
            try:
                ilc_batch.build_plan(TARGETS, bad, out, "p8")
                check(False, f"refuses a stem list with a {why} stem")
            except ValueError:
                check(True, f"refuses a stem list with a {why} stem")

        print("[9] wiring from the panel's scope channel roles (eomilc.channels)")
        sim_cfg = {"scope_channels": SIM_MAP}
        path = write_plan(tmp, ["S25"], name="t9")
        plan = ilc_batch.load_plan(path, cfg=sim_cfg)
        w = plan["wiring"]
        check(w["X1"]["mon_ch"] == 3 and w["X1"]["drive_ch"] == 1 and w["X2"]["mon_ch"] == 4
              and w["X2"]["drive_ch"] == 2, "the sim's wiring from roles")
        check(chm.from_record(plan["_scope_channels"]) == chm.channels_of(sim_cfg),
              "the map is recorded on the plan")
        bench_cfg = {"scope_channels": chm.DEFAULT_CHANNELS}      # PD on CH1, CmdX1 on CH2
        w = ilc_batch.load_plan(path, cfg=bench_cfg)["wiring"]
        check(w["X1"]["drive_ch"] == 2 and w["X2"]["drive_ch"] is None and w["X2"]["mon_ch"] == 4,
              "the bench's map: X1 reads back on CH2, X2 has no readback")
        raw = json.load(open(path))
        raw["wiring"] = {"X2": {"drive_ch": 1}}
        json.dump(raw, open(path, "w"))
        w = ilc_batch.load_plan(path, cfg=bench_cfg)["wiring"]
        check(w["X2"]["drive_ch"] == 1 and w["X1"]["drive_ch"] == 2, "a plan's wiring block overrides per channel")
        no_mon = {"scope_channels": {"1": {"role": "PD"}, "3": {"role": "MonX1"}}}
        w = ilc_batch.load_plan(path, cfg=no_mon)
        check(w["wiring"]["X2"]["mon_ch"] == 4 and w["_wiring_notes"], "no MonX2 in the map: old numbers, with a note")
        # a run with no readback on either channel: the alignment check is skipped and
        # the campaign still learns; the state records the map
        off_cfg = {"scope_channels": {"1": {"role": "PD"}, "3": {"role": "MonX1"}, "4": {"role": "MonX2"}}}
        path = write_plan(tmp, ["S25"], name="t9b")
        plan = ilc_batch.load_plan(path, cfg=off_cfg)
        msgs = []
        man = ilc_batch.run_batch(plan, args(), log=msgs.append, bench=bench(off_cfg))
        check(man["campaigns"]["S25"]["status"] == "done", "no-readback campaign done")
        # the bench's real map against a scope cabled by it: X1 reads back on
        # CH2, X2 not at all, and a series shape still trains
        path = write_plan(tmp, ["SW50"], name="t9c")
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        man = ilc_batch.run_batch(plan, args(), log=lambda s: None, bench=bench(bench_cfg))
        check(man["campaigns"]["SW50"]["status"] == "done", "the bench's map (CmdX1 on CH2, no CmdX2) trains a series shape")
        check(any("NOT verified" in m for m in msgs), "the log says the alignment was not verified")
        with np.load(os.path.join(plan["_run_dir"], "drive_S25X1.state.npz"), allow_pickle=True) as st:
            check(chm.from_record(str(st["scope_channels"])) == chm.channels_of(off_cfg), "state carries the map")

        print("[10] two models, one per channel, named in the dry run")
        path = write_plan(tmp, ["S25"], name="t10")
        raw = json.load(open(path))
        raw["run_dir"] = os.path.join(tmp, "run10")       # a fresh folder: an existing state resumes with ITS model
        raw["settings"] = {"X1": {"model": "resonant", "params": {"gain": 0.557, "fn": 7823, "zeta": 1.47},
                                  "gamma": 0.6, "f_cut": 20e3, "notches": [[23700, 400]]},
                           "X2": {"model": "third_order", "params": {"gain": 0.6116, "fn": 13729, "zeta": 2.84, "tau": 2.11},
                                  "gamma": 0.6, "f_cut": 60e3, "notches": [[25400, 400]]}}
        json.dump(raw, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=sim_cfg)
        l1, l2 = ilc_batch.model_line(plan, "X1"), ilc_batch.model_line(plan, "X2")
        check("resonant" in l1 and "fn 7823" in l1 and "f_cut 20 kHz" in l1 and "23.7k/400" in l1, l1)
        check("third_order" in l2 and "tau 2.11 us" in l2 and "f_cut 60 kHz" in l2 and "25.4k/400" in l2, l2)
        msgs = []
        ilc_batch.dry_run(plan, log=msgs.append)
        check(sum(m.startswith("model X1") for m in msgs) == 1 and sum(m.startswith("model X2") for m in msgs) == 1,
              "the dry run prints both")
        man = ilc_batch.run_batch(plan, args(), log=lambda s: None, bench=bench(sim_cfg))
        c = man["campaigns"]["S25"]
        check(c["status"] == "done" and c["channels"]["X1"]["history"][-1]["rms_v"] < 0.1 * c["channels"]["X1"]["history"][0]["rms_v"]
              and c["channels"]["X2"]["history"][-1]["rms_v"] < 0.1 * c["channels"]["X2"]["history"][0]["rms_v"],
              "both channels learn with their own model")
        with np.load(os.path.join(plan["_run_dir"], "drive_S25X1.state.npz"), allow_pickle=True) as st1,                 np.load(os.path.join(plan["_run_dir"], "drive_S25X2.state.npz"), allow_pickle=True) as st2:
            check(float(st1["fn"]) == 7823 and float(st2["fn"]) == 13729 and float(st1["f_cut"]) == 20e3
                  and float(st2["f_cut"]) == 60e3, "each state records its own model")
        print("[11] the optical stage in simulation: two passes, stopped in pass 2 and resumed")
        import threading
        path = write_plan(tmp, ["S25"], name="t11")
        raw = json.load(open(path))
        raw["run_dir"] = os.path.join(tmp, "run11")
        raw["optical"] = {"scan": {"angles": "0:359:45", "shots": 2}, "gains": [0.7, 1.0],
                          "iterations": 2, "pd_delay_us": 0.0, "figs": False}
        json.dump(raw, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)            # PD on CH1: the polarimeter's map
        msgs = []
        ilc_batch.dry_run(plan, log=msgs.append)
        want = "optical: 2 passes gains 0.7/1.0, 2 iterations each, scan 0:359:45 x 2 shots, band 5 kHz"
        check(any(want in m for m in msgs), f"the dry run prints '{want}'")
        stop = threading.Event()

        def stop_at_verify2(kind, info):
            if kind == "optical" and info.get("step") == "scan" and info.get("pass") == 2 \
                    and not info["scan"].endswith("_before"):
                stop.set()                       # as if Stop were pressed as pass 2's verification starts
        awg11, scope11 = bench(bench_cfg)
        msgs = []
        man = ilc_batch.run_batch(plan, args(), log=msgs.append, stop=stop, on_event=stop_at_verify2,
                                  bench=(awg11, scope11))
        check(man["campaigns"]["S25"]["status"] == "stopped" and parked(awg11),
              "stopped in pass 2, outputs parked ON")
        odir = os.path.join(plan["_run_dir"], "optical")
        v1 = os.path.join(odir, "S25_opt1", "S25_opt1_scan.json")
        check(os.path.exists(os.path.join(odir, "S25_opt1_before", "S25_opt1_before_scan.json"))
              and os.path.exists(v1), "pass 1's scans on disk (S25_opt1_before, S25_opt1)")
        stamp = os.path.getmtime(v1)
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        awg11, scope11 = bench(bench_cfg)
        msgs = []
        man = ilc_batch.run_batch(plan, args(), log=msgs.append, bench=(awg11, scope11))
        c = man["campaigns"]["S25"]
        o = c.get("optical") or {}
        P = o.get("passes") or []
        check(c["status"] == "done" and o.get("status") == "done" and len(P) == 2
              and [p["status"] for p in P] == ["done", "done"] and [p["gain"] for p in P] == [0.7, 1.0],
              f"resumed: done, two passes ({o.get('status')}, {[p.get('status') for p in P]})")
        check(any("optical pass 1: done in an earlier run" in m for m in msgs)
              and not any(m.startswith("  scan S25_opt1") for m in msgs) and os.path.getmtime(v1) == stamp,
              "pass 1 is not repeated (no scan of it, its verification scan untouched)")
        check(any("S25_opt2: on disk" in m and "resumed" in m for m in msgs),
              "pass 2's verification scan resumes from disk")
        check(parked(awg11), "outputs parked ON at the end, never OFF")
        if len(P) == 2:
            check(P[1]["scan"] == P[0]["verify_scan"] and P[1]["verdict_before"] == P[0]["verdict_after"],
                  "pass 2's correction comes from pass 1's verification")
            files = [f for p in P for f in p["corr_files"].values()] + list(P[1]["verify_corr_files"].values())
            check(len(files) == 6 and all(os.path.exists(f) for f in files),
                  "correction files on disk (2 per pass + the last verification's)")
            check(all(set(p["applied"]) == {"X1", "X2"} and all(a["gain"] == p["gain"] and a["status"] == "applied"
                                                                 and a["kept_peak_V"] > 1.0 for a in p["applied"].values())
                      for p in P),
                  "both channels applied each pass ("
                  + ", ".join(f"{k} {a['kept_peak_V']:.1f} V" for p in P for k, a in p["applied"].items()) + ")")
            for p in P:
                b, a = p["verdict_before"]["legs"][0], p["verdict_after"]["legs"][0]
                print(f"       pass {p['pass']} x{p['gain']}: ramp top light-target {b['top_lt_mdeg']:+.0f} -> "
                      f"{a['top_lt_mdeg']:+.0f} mdeg, light-monitors {b['top_lm_mdeg']:+.0f} -> "
                      f"{a['top_lm_mdeg']:+.0f} mdeg; rms light-target {b['lt_rms_mdeg']:.0f} -> "
                      f"{a['lt_rms_mdeg']:.0f} mdeg")
            # What the closure removes in simulation is the rotator error, a
            # light - monitors term that varies along the ramp (+-0.5 deg). The
            # ramp-top extremes barely move: light - target there is mostly a
            # constant ~-0.5 deg monitors - target offset of the simulated
            # comparison (there at rest too; it is in the monitors, so no optical
            # correction is meant to take it out) and light - monitors is the
            # light's own term, which stays. The comparison's light - target
            # spread (its std about that offset) is what falls, and the monitors
            # leave the target by the correction (monitors - target spread rises).
            b0, a0, a1 = (P[0]["verdict_before"]["legs"][0], P[0]["verdict_after"]["legs"][0],
                          P[1]["verdict_after"]["legs"][0])
            check(a0["lt_std_mdeg"] < b0["lt_std_mdeg"] and a1["lt_std_mdeg"] < 0.6 * b0["lt_std_mdeg"],
                  f"light - target spread falls pass by pass: {b0['lt_std_mdeg']:.0f} -> {a0['lt_std_mdeg']:.0f} "
                  f"-> {a1['lt_std_mdeg']:.0f} mdeg")
            check(a1["mt_std_mdeg"] > b0["mt_std_mdeg"] + 50,
                  f"the monitors leave the target by the correction: monitors - target spread "
                  f"{b0['mt_std_mdeg']:.0f} -> {a1['mt_std_mdeg']:.0f} mdeg")
            check(abs(a1["top_lm_mdeg"]) > 0.5 * abs(b0["top_lm_mdeg"]),
                  f"light - monitors at the ramp top stays (the rotator error is the light's own): "
                  f"{b0['top_lm_mdeg']:+.0f} -> {a1['top_lm_mdeg']:+.0f} mdeg")
            for k in ("X1", "X2"):
                ch = c["channels"][k]
                check(ch["keeper_iteration"] == P[1]["kept"][k] and os.path.exists(ch["keeper_file"]),
                      f"{k} keeper is pass 2's last iteration (i{ch['keeper_iteration']:02d})")
                with np.load(ch["state"], allow_pickle=True) as st:
                    has = all(x in st.files for x in ("base_target", "corr_total", "corrections"))
                    lay = json.loads(str(st["corrections"])) if has else []
                    ok = has and np.allclose(st["target"], st["base_target"] + st["corr_total"])
                check(ok and len(lay) == 1 and lay[0]["gain"] == 1.0 and lay[0]["slot"] == "optical"
                      and lay[0]["path"] == os.path.abspath(P[1]["corr_files"]["EO" + k[-1]]),
                      f"{k} state: target = base_target + corr_total, one layer (pass 2's file x1.0 replaced pass 1's)")
            check(json.load(open(o["record"]))["status"] == "done", "the stage's own record is written")

        print("[11b] attribution separate: X1 alone (X2 parked), X2 alone (X1 parked), both to verify")
        path = write_plan(tmp, ["S25"], name="t11b")
        raw = json.load(open(path))
        raw["run_dir"] = os.path.join(tmp, "run11b")
        raw["optical"] = {"scan": {"angles": "0:359:45", "shots": 2, "solo_angles": "0:179:45",
                                   "verify_angles": "0:179:45"},
                          "gains": [1.0], "iterations": 2,
                          "pd_delay_us": 0.0, "figs": False, "attribution": "separate"}
        json.dump(raw, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        msgs = []
        ilc_batch.dry_run(plan, log=msgs.append)
        want = "attribution separate (X1 alone, X2 alone, then both to verify)"
        check(any(want in m for m in msgs), f"the dry run prints '{want}'")
        check(any("(solo scans 0:179:45) (verify scans 0:179:45)" in m for m in msgs),
              "the dry run names the solo and verify scans' own angles")
        s11b = ilc_batch.optical_settings(plan, plan["campaigns"][0])
        full = dict(s11b, scan=dict(s11b["scan"], solo_angles=None, verify_angles=None))
        est_h, n_h, _ = ilc_batch.optical_estimate(plan, s11b, 30.0)
        est_f, n_f, _ = ilc_batch.optical_estimate(plan, full, 30.0)
        check(n_h == n_f == 4 and est_h < est_f and ilc_batch.scan_angles(s11b["scan"], "solo") == "0:179:45"
              and ilc_batch.scan_angles(s11b["scan"], "before") == "0:359:45",
              f"the estimate counts the half-circle scans: {est_h/60:.1f} min against {est_f/60:.1f} at full circle")
        awg11b, scope11b = bench(bench_cfg)
        marks, at_scan = {}, {}

        def mark(kind, info):
            if kind == "optical" and info.get("step") in ("scan", "learn"):
                key = info["scan"] if info["step"] == "scan" else f"learn{info['pass']}"
                marks[key] = len(awg11b.log)
                if info["step"] == "scan":
                    at_scan[key] = {c: (awg11b.ch[c]["name"], float(np.ptp(awg11b.ch[c]["wave"])),
                                        float(awg11b.ch[c]["wave"][0] * awg11b.fs)) for c in (1, 2)}
        msgs = []
        man = ilc_batch.run_batch(plan, args(), log=msgs.append, on_event=mark, bench=(awg11b, scope11b))
        c = man["campaigns"]["S25"]
        o = c.get("optical") or {}
        P = o.get("passes") or [{}]
        p1 = P[0]
        odir = os.path.join(plan["_run_dir"], "optical")
        names = ["S25_opt1_before", "S25_opt1_X1", "S25_opt1_X2", "S25_opt1"]
        check(all(os.path.exists(os.path.join(odir, n, f"{n}_scan.json")) for n in names),
              "scans on disk: " + ", ".join(names))
        check(c["status"] == "done" and o.get("status") == "done" and p1.get("status") == "done"
              and p1.get("attribution") == "separate",
              f"done, one separate pass ({o.get('status')}, {p1.get('status')})")
        ss = p1.get("solo_scans") or {}
        check(set(ss) == {"X1", "X2"} and os.path.basename(ss["X1"]) == "S25_opt1_X1"
              and os.path.basename(ss["X2"]) == "S25_opt1_X2", "the pass record's solo_scans")
        angs = {}
        for nm in ("S25_opt1_before", "S25_opt1_X1", "S25_opt1"):
            mf = os.path.join(os.path.dirname(ss["X1"]), nm, nm + "_scan.json")
            angs[nm] = json.load(open(mf, encoding="utf-8"))["plan"].get("angles") if os.path.exists(mf) else None
        check(angs == {"S25_opt1_before": "0:359:45", "S25_opt1_X1": "0:179:45", "S25_opt1": "0:179:45"},
              f"the before scan runs the full circle, the solo and verify scans their own half: {angs}")
        cf = p1.get("corr_files") or {}
        check(set(cf) == {"EO1", "EO2"}
              and os.path.commonpath([cf["EO1"], ss.get("X1", "")]) == os.path.normpath(ss.get("X1", ""))
              and os.path.commonpath([cf["EO2"], ss.get("X2", "")]) == os.path.normpath(ss.get("X2", "")),
              "corr_files: EO1 from the X1 scan, EO2 from the X2 scan")

        def finite(v):
            return bool(v) and all(np.isfinite(leg[k]) for leg in v["legs"]
                                   for k in ("top_lt_mdeg", "top_lm_mdeg", "lt_rms_mdeg"))
        vs = p1.get("verdicts_solo") or {}
        check(set(vs) == {"X1", "X2"} and finite(vs["X1"]) and finite(vs["X2"]), "verdicts_solo finite")
        check(finite(p1.get("verdict_before")) and finite(p1.get("verdict_after"))
              and os.path.basename(p1.get("before_scan") or "") == "S25_opt1_before",
              "verdict_before from the joint S25_opt1_before (not applied), verdict_after from the verification")
        ap = p1.get("applied") or {}
        check(set(ap) == {"X1", "X2"} and all(a["status"] == "applied" for a in ap.values())
              and ap["X1"]["file"] == os.path.abspath(cf.get("EO1", "")) and ap["X2"]["file"] == os.path.abspath(cf.get("EO2", "")),
              "both applied, each its own crystal's file ("
              + ", ".join(f"{k} {a['kept_peak_V']:.1f} V" for k, a in ap.items()) + ")")
        # The sequence is what is tested here, not the physics: rampol.sim's
        # light-only term is an analyzer error that follows the TOTAL rotation,
        # not either crystal's own, so each solo scan reads about the whole of
        # it and the two files add to ~twice the joint one; the verification
        # need not improve in simulation.
        if finite(p1.get("verdict_before")) and finite(p1.get("verdict_after")):
            b, a = p1["verdict_before"]["legs"][0], p1["verdict_after"]["legs"][0]
            print(f"       joint before -> after: ramp top light-monitors {b['top_lm_mdeg']:+.0f} -> "
                  f"{a['top_lm_mdeg']:+.0f} mdeg; light-target spread {b['lt_std_mdeg']:.0f} -> "
                  f"{a['lt_std_mdeg']:.0f} mdeg; correction peak joint "
                  + "/".join(f"{x['peak_V']:.1f}" for x in p1["verdict_before"]["correction"]["crystals"].values())
                  + " V, solo X1 " + "/".join(f"{x['peak_V']:.1f}" for x in vs["X1"]["correction"]["crystals"].values())
                  + " V, solo X2 " + "/".join(f"{x['peak_V']:.1f}" for x in vs["X2"]["correction"]["crystals"].values())
                  + " V (EO1/EO2)")
        check(os.path.basename(p1.get("verify_scan") or "") == "S25_opt1" and ilc_batch._scan_done(p1.get("verify_scan"))
              and set(p1.get("verify_corr_files") or {}) == {"EO1", "EO2"},
              "verify_scan S25_opt1 (joint), its files recorded")
        log = awg11b.log
        i1, i2, il = marks.get("S25_opt1_X1", -1), marks.get("S25_opt1_X2", -1), marks.get("learn1", -1)
        ib = marks.get("S25_opt1_before", -1)
        check(0 <= ib < i1 and ("upload", 2, "PARK2") in log[ib:i1], "('upload', 2, 'PARK2') before the X1 scan")
        check(0 <= i1 < i2 and ("upload", 1, "PARK1") in log[i1:i2]
              and any(e[0] == "upload" and e[1] == 2 and e[2].startswith("BX2") for e in log[i1:i2]),
              "('upload', 1, 'PARK1') and X2's keeper on CH2 before the X2 scan")
        check(at_scan.get("S25_opt1_X1", {}).get(2, ("", 1))[0] == "PARK2" and at_scan["S25_opt1_X1"][2][1] == 0.0
              and at_scan["S25_opt1_X1"][1][0].startswith("BX1")
              and at_scan.get("S25_opt1_X2", {}).get(1, ("", 1))[0] == "PARK1" and at_scan["S25_opt1_X2"][1][1] == 0.0
              and at_scan["S25_opt1_X2"][2][0].startswith("BX2")
              and at_scan.get("S25_opt1", {}).get(1, ("",))[0].startswith("BX1")
              and at_scan["S25_opt1"][2][0].startswith("BX2"),
              "the generator holds: X1 scan BX1/PARK2 (flat), X2 scan PARK1 (flat)/BX2, verification both keepers")
        up = [e for e in log[i2:il] if e[0] == "upload"] if 0 <= i2 < il else []
        check({e[1] for e in up} == {1, 2} and all(e[2].startswith(f"BX{e[1]}") for e in up),
              f"both keepers uploaded again before the learn ({', '.join(e[2] for e in up)})")
        check(not any(e[0] == "output" and not e[2] for e in log[marks.get("S25_opt1_before", 0):il]),
              "no output switched off to park")
        with open(os.path.join(ss.get("X1", ""), "S25_opt1_X1_scan.json")) as fh:
            idle1 = json.load(fh)["drive"]["idle_V"]
        # the manifest holds what the simulated polarimeter played: bridged
        # (ilc_batch._sim_drives), the simbench chain's response to the flat
        # park drive over rampol.sim's CH2 gain (0.5924), so EO2 is flat at
        # what the bridge makes of the idle, not at the AWG volts themselves
        park_v = (p1.get("parked") or {}).get("X2", {}).get("idle_V", float("nan"))
        with np.load(c["channels"]["X2"]["state"], allow_pickle=True) as st:
            t2 = np.asarray(st["t"], float)
        flat = np.full(len(t2), park_v)
        want = scope11b.chains[2].respond(flat, float(np.median(np.diff(t2))))[0] / 0.5924
        check(abs(idle1["EO2"] - want) < 1e-6 and abs(park_v - at_scan["S25_opt1_X1"][2][2]) < 1e-3,
              f"the X1 scan's manifest has EO2 at the idle (PARK2 {park_v:+.3f} V on the AWG, played by the "
              f"bridged simulation as {idle1['EO2']:+.4f} V)")
        check(any("X2 parked: CH2 plays PARK2" in m for m in msgs) and any("X1 parked: CH1 plays PARK1" in m for m in msgs),
              "the log names the parked channel")
        check(parked(awg11b), "outputs parked ON at the end, never OFF")
        for k in ("X1", "X2"):
            ch = c["channels"][k]
            with np.load(ch["state"], allow_pickle=True) as st:
                lay = json.loads(str(st["corrections"])) if "corrections" in st.files else []
            check(ch["keeper_iteration"] == p1.get("kept", {}).get(k) and len(lay) == 1
                  and lay[0]["path"] == os.path.abspath(cf.get("EO" + k[-1], "")),
                  f"{k}: keeper i{ch['keeper_iteration']:02d} after the correction, one layer from its own scan")
        bad = dict(raw, optical=dict(raw["optical"], attribution="sideways"))
        try:
            ilc_batch.optical_settings(bad, bad["campaigns"][0])
            check(False, "attribution 'sideways' refused")
        except ilc_batch.BatchError as e:
            check("sideways" in str(e), f"attribution 'sideways' refused ({e})")
        # joint_before false: no _before scan, no before verdict
        raw["run_dir"] = os.path.join(tmp, "run11c")
        raw["optical"]["joint_before"] = False
        json.dump(raw, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        msgs = []
        man = ilc_batch.run_batch(plan, args(), log=msgs.append, bench=bench(bench_cfg))
        o = man["campaigns"]["S25"].get("optical") or {}
        p1 = (o.get("passes") or [{}])[0]
        odir = os.path.join(plan["_run_dir"], "optical")
        check(o.get("status") == "done" and not os.path.exists(os.path.join(odir, "S25_opt1_before"))
              and p1.get("verdict_before", "absent") is None and finite(p1.get("verdict_after")),
              "joint_before false: no S25_opt1_before, verdict_before None, the verification still runs")
        # two passes: pass 1 solo (slot optical), pass 2 the joint verification's residual ADDED
        # in slot joint2 -- light - monitors is the whole error each time, a second solo pass
        # would only repeat the first
        raw["run_dir"] = os.path.join(tmp, "run11d")
        raw["optical"]["gains"] = [1.0, 1.0]
        raw["optical"]["joint_before"] = True
        json.dump(raw, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        msgs = []
        ilc_batch.dry_run(plan, log=msgs.append)
        check(any("pass 1 solo, later passes add the joint verification's residual" in m for m in msgs),
              "the dry run says what pass 2 does")
        msgs = []
        man = ilc_batch.run_batch(plan, args(), log=msgs.append, bench=bench(bench_cfg))
        c = man["campaigns"]["S25"]
        o = c.get("optical") or {}
        P = o.get("passes") or []
        odir = os.path.join(plan["_run_dir"], "optical")
        check(c["status"] == "done" and o.get("status") == "done" and len(P) == 2
              and [p.get("status") for p in P] == ["done", "done"], f"two separate passes done ({[p.get('status') for p in P]})")
        if len(P) == 2:
            check(P[0].get("slot") == "optical" and P[0].get("source") == "solo scans"
                  and P[1].get("slot") == "joint2" and P[1].get("source") == "joint verification"
                  and all(f.endswith("_joint2.csv") for f in P[1]["corr_files"].values()) and "solo_scans" not in P[1],
                  "pass 1 from the solo scans in slot optical; pass 2 derived from pass 1's verification in slot joint2, no solo scans")
            # the joint2 file = the joint file minus this crystal's split share of what was applied on both
            from eomilc import corrections as cm2
            from eomilc.config import CHANNELS as CH2
            kv = {c: CH2[c].v90_hv * 1000 / 90 for c in ("EO1", "EO2")}
            solo = {c: cm2.read_correction(P[0]["corr_files"][c]) for c in ("EO1", "EO2")}
            with np.load(c["channels"]["X1"]["state"], allow_pickle=True) as st1, \
                    np.load(c["channels"]["X2"]["state"], allow_pickle=True) as st2:
                lay1 = json.loads(str(st1["corrections"])); lay2 = json.loads(str(st2["corrections"]))
            jo = cm2.read_correction(P[0]["verify_corr_files"]["EO1"])
            j2 = cm2.read_correction(P[1]["corr_files"]["EO1"])
            # the applied solo totals in V at the EOM are the gated solo files x gain: use the
            # gate's kept peaks to bound the check (the exact kept arrays are in the states' corr_total)
            check(j2.meta.get("slot") == "joint2" and j2.meta.get("derived_from") == os.path.abspath(P[0]["verify_corr_files"]["EO1"])
                  and len(j2.delta) == len(jo.delta) and not np.allclose(j2.delta, jo.delta),
                  "joint2's file names its source and differs from the plain joint file")
            lay_ok = [L.get("slot") for L in lay1] == ["optical", "joint2"] and [L.get("slot") for L in lay2] == ["optical", "joint2"]
            check(lay_ok, f"both states: layers optical + joint2 ({[L.get('slot') for L in lay1]})")
            check(not os.path.isdir(os.path.join(odir, "S25_opt2_X1")) and os.path.isdir(os.path.join(odir, "S25_opt2")),
                  "pass 2 scanned only its verification")
            check(any("go in slot joint2 beside the solo correction" in m for m in msgs)
                  and any("joint2 = joint" in m and "minus 50% of the applied total" in m for m in msgs), "the log says so")
            for k in ("X1", "X2"):
                with np.load(c["channels"][k]["state"], allow_pickle=True) as st:
                    lay = json.loads(str(st["corrections"])) if st["corrections"].dtype.kind in "US" else list(st["corrections"])
                    slots = sorted(L.get("slot") for L in lay)
                    check(slots == ["joint2", "optical"] and np.allclose(st["target"], st["base_target"] + st["corr_total"]),
                          f"{k} state: two layers {slots}, target = base + total")
            v1, v2 = P[0]["verdict_after"], P[1]["verdict_after"]
            check(finite(v1) and finite(v2), "both verifications finite")

        print("[12] a refused correction is recorded; the campaign keeps its electrical keeper")
        path = write_plan(tmp, ["S25"], name="t12")
        raw = json.load(open(path))
        raw["run_dir"] = os.path.join(tmp, "run12")
        raw["optical"] = {"scan": {"angles": "0:359:45", "shots": 2}, "gains": [1.0], "iterations": 2,
                          "pd_delay_us": 0.0, "figs": False, "cap_v": 300}
        raw["campaigns"] = [{"stem": "S25", "optical": {"cap_v": 1.0}}]     # per campaign wins
        json.dump(raw, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        s12 = ilc_batch.optical_settings(plan, plan["campaigns"][0])
        check(s12["cap_v"] == 1.0 and s12["scan"]["angles"] == "0:359:45" and s12["scan"]["points"] is None
              and s12["scan"]["sample_us"] == 2.0,
              "settings: the campaign's over the root's over the defaults")
        awg12, scope12 = bench(bench_cfg)
        msgs = []
        man = ilc_batch.run_batch(plan, args(), log=msgs.append, bench=(awg12, scope12))
        c = man["campaigns"]["S25"]
        o = c.get("optical") or {}
        P = o.get("passes") or [{}]
        check(c["status"] == "optical refused" and o.get("status") == "refused" and P[0].get("status") == "refused"
              and any("cap" in f for f in P[0].get("refused", [])),
              f"refused for the cap, recorded ({(P[0].get('refused') or ['?'])[0][:70]})")
        check(any("correction REFUSED" in m for m in msgs), "the log says so")
        for k in ("X1", "X2"):
            ch = c["channels"][k]
            late = [h for h in ch["history"] if h["it"] >= 3]
            best = min(late, key=lambda h: h["rms_v"])
            with np.load(ch["state"], allow_pickle=True) as st:
                clean = "base_target" not in st.files and "corrections" not in st.files
            check(ch["keeper_iteration"] == best["it"] and os.path.exists(ch["keeper_file"]) and clean,
                  f"{k}: electrical keeper i{best['it']:02d}, target untouched")
        check(parked(awg12), "outputs parked ON, never OFF")
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        msgs = []
        man = ilc_batch.run_batch(plan, args(redo=True, no_optical=True), log=msgs.append, bench=bench(bench_cfg))
        check("optical" not in man["campaigns"]["S25"] and any("skipped (--no-optical)" in m for m in msgs),
              "--no-optical skips the stage")

        print("[13] a campaign's echo key: the spin-echo record is built at plan load, into the targets folder")
        tdir = os.path.join(tmp, "targets13")
        os.makedirs(tdir)
        for k in ("X1", "X2"):
            shutil.copy(os.path.join(TARGETS, f"target_S47{k}.csv"), tdir)
        path = os.path.join(tmp, "plan13.json")
        json.dump({"name": "t13", "targets_dir": tdir, "run_dir": os.path.join(tmp, "run13"),
                   "campaigns": [{"stem": "S47", "echo": {"spacing_ms": 16.667}},
                                 {"stem": "S47", "echo": {"spacing_ms": 16.667, "legs": 3, "suffix": "ECHO3"}},
                                 {"stem": "S25", "echo": {"spacing_ms": 16.667}}]}, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        c1, c2, c3 = plan["campaigns"]
        check(c1["stem"] == "S47E2" and "awg" not in c1 and os.path.exists(os.path.join(tdir, "target_S47E2X1.csv"))
              and os.path.exists(os.path.join(tdir, "target_S47E2X2.csv")), "S47 + echo -> S47E2, both files in the targets folder")
        check(c2["stem"] == "S47ECHO3" and c2["awg"] == "S47EC" and c2["_echo"]["legs"] == 3
              and os.path.exists(os.path.join(tdir, "target_S47ECHO3X1.csv")), "three legs with a long suffix: AWG name cut to 5")
        check(c3["stem"] == "S25" and "error" in c3["_echo"] and "not longer than the transport" not in c3["_echo"]["error"],
              f"a missing source leaves the campaign as it was: {c3['_echo']['error'][:60]}")
        msgs = []
        ilc_batch.dry_run(plan, log=msgs.append)
        check(any(m.startswith("echo S47E2: spin echo of S47, 2 legs at 0.200, 16.868 ms (spacing 16.668 ms), 13741 pts; "
                               "card pause 5.856 ms; written") for m in msgs), "the dry run names the record and its card pause")
        check(any(m.startswith("echo S25: echo record NOT built") for m in msgs), "... and the one it could not build")
        rows = {r["stem"]: r for r in ilc_batch.plan_rows(plan)}
        check(rows["S47E2"]["n"] == 13741 and abs(rows["S47E2"]["period_ms"] - 27.482) < 1e-6 and rows["S47E2"]["ok"],
              "S47E2 row: 13741 pts, 27.482 ms, drives within limits")
        check(rows["S25"]["error"] and "echo record was not built" in rows["S25"]["error"],
              f"the S25 row reports the echo that was not built: {rows['S25']['error'][:50]}")
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        check("already there" in plan["_echo_notes"][0], "a second load finds the files unchanged")

        print("[16] a bad scope read drops the shot, a missed trigger is re-armed; reads that keep failing refuse")
        path = write_plan(tmp, ["S25"], name="t16")
        raw = json.load(open(path))
        raw["run_dir"] = os.path.join(tmp, "run16")
        raw["loop"]["max_iterations"] = 2
        raw["loop"]["min_iterations"] = 1
        json.dump(raw, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        awg16, scope16 = bench(bench_cfg)
        read0 = ilc_batch.read_waveform
        calls = {"n": 0}

        def flaky(scope, ch, points, tries=3):
            calls["n"] += 1
            if calls["n"] == 4:                       # the 2nd shot's first channel of the first capture
                raise ValueError("invalid literal for int() with base 10: b''")
            return read0(scope, ch, points, tries)
        single0 = scope16.single
        arms = {"n": 0}

        def flaky_single(wait_s=10.0, **k):
            arms["n"] += 1
            if arms["n"] == 3:
                return False                         # one missed trigger (arm 1 is the alignment check)
            return single0(wait_s=wait_s, **k)
        ilc_batch.read_waveform = flaky
        scope16.single = flaky_single
        msgs, events16 = [], []
        try:
            man = ilc_batch.run_batch(plan, args(), log=msgs.append, bench=(awg16, scope16),
                                      on_event=lambda k, i: events16.append((k, i)))
        finally:
            ilc_batch.read_waveform = read0
        b16 = next(i for k, i in events16 if k == "batch")
        check("S25" in b16.get("plan_est", {}) and b16["plan_est"]["S25"]["max_it"] == 2
              and b16["plan_est"]["S25"]["per_it_s"] > 0 and b16["plan_est"]["S25"]["optical_s"] == 0
              and b16.get("started"), "the batch event carries each campaign's time estimate and the start time")
        c = man["campaigns"]["S25"]
        check(c["status"] == "done" and any("shot DROPPED" in m for m in msgs)
              and any("5 of 6 shots kept (1 dropped)" in m for m in msgs),
              f"one bad read: the shot is dropped, the campaign goes on ({c['status']})")
        check(any("re-armed (2/3)" in m for m in msgs), "one missed trigger: re-armed, not refused")
        ilc_batch.read_waveform = lambda scope, ch, points, tries=3: (_ for _ in ()).throw(ValueError("b''"))
        try:
            raw["run_dir"] = os.path.join(tmp, "run16b")
            json.dump(raw, open(path, "w"))
            plan = ilc_batch.load_plan(path, cfg=bench_cfg)
            man = ilc_batch.run_batch(plan, args(), log=msgs.append, bench=bench(bench_cfg))
        finally:
            ilc_batch.read_waveform = read0
        c = man["campaigns"]["S25"]
        check(c["status"] == "refused" and "only 0 of 6 shots were read" in c.get("error", ""),
              f"reads that keep failing refuse the campaign ({c.get('error', '')[:60]})")

        print("[17] the progress tracker: fraction, elapsed and time left from synthetic events")
        from eomilc import progress as progmod
        bp = progmod.BatchProgress()
        est = {"A": dict(electrical_s=600.0, per_it_s=30.0, optical_s=900.0, scans=3, learn_it=10, max_it=19),
               "B": dict(electrical_s=600.0, per_it_s=30.0, optical_s=0.0, scans=0, learn_it=0, max_it=19)}
        t = 1000.0
        bp.event("batch", dict(todo=["A", "B"], n=2, plan_est=est, started=t), t)
        check(bp.text(t) == "" and bp.fraction() == 0.0, "nothing to say before the first campaign")
        bp.event("campaign", dict(stem="A", index=0, n=2), t)
        s0 = bp.text(t)
        check(s0.startswith("A 1/2 | starting") and "batch ~" in s0 and "this campaign ~25 min left" in s0, s0)
        for it in range(6):
            t += 36.0
            bp.event("iteration", dict(stem="A", key="X1", it=it, rms_v=10.0 / (it + 1), stop=""), t)
            bp.event("iteration", dict(stem="A", key="X2", it=it, rms_v=11.0 / (it + 1), stop=""), t)
        s1, f1 = bp.text(t), bp.fraction()
        check(s1.startswith("A 1/2 | i05 of <=19 (X1 1.67 V, X2 1.83 V)") and "elapsed 3 min" in s1
              and abs(bp._per_it() - 36.0) < 1e-9 and 0.1 < f1 < 0.3, f"{s1} [{f1:.2f}]")
        check(abs(bp.campaign_left_s() - (14 * 36.0 + 900.0)) < 1e-6,
              f"left in A: 14 iterations at the measured 36 s plus the optical estimate ({bp.campaign_left_s():.0f} s)")
        bp.event("iteration", dict(stem="A", key="X1", it=6, rms_v=0.5, stop="converged"), t + 36)
        bp.event("iteration", dict(stem="A", key="X2", it=6, rms_v=0.5, stop="converged"), t + 36)
        check(abs(bp.fraction() - 0.4) < 1e-9 and abs(bp.campaign_left_s() - 900.0) < 1e-6,
              "both stopped: the electrical share is complete, the optical estimate remains")
        t += 40
        bp.event("optical", dict(stem="A", step="scan", scan="A_opt1_before", **{"pass": 1}), t)
        s2 = bp.text(t)
        check("optical pass 1: scan A_opt1_before (1/3)" in s2, s2)
        t += 200
        bp.event("optical", dict(stem="A", step="compare", scan="A_opt1_before", **{"pass": 1}), t)
        check(bp.optical["scans_done"] == 1 and abs(bp._t_scan() - 200.0) < 1e-9 and 0.4 < bp.fraction() < 1.0,
              f"a scan's duration is measured (200 s) and counted ({bp.fraction():.2f})")
        check(abs(bp.campaign_left_s() - (2 * 200.0 + 10 * 36.0)) < 1e-6,
              f"left: two scans at the measured 200 s plus 10 learning iterations ({bp.campaign_left_s():.0f} s)")
        t += 10
        bp.event("optical", dict(stem="A", step="end", status="done"), t)
        bp.event("campaign_end", dict(stem="A", entry=dict(status="done", minutes=20.0)), t)
        bp.event("campaign", dict(stem="B", index=1, n=2), t)
        scale = 1200.0 / 1500.0
        check(abs(bp.batch_left_s() - bp.campaign_left_s()) < 1e-6 and abs(bp.campaign_left_s() - 20 * 30.0) < 1e-6
              and bp.fraction() == 1.0, "B: the plan's per-iteration estimate until measured; A counted complete")
        bp.event("batch_end", {}, t + 5)
        check(bp.fraction() == 2.0 and bp.text(t + 5).startswith("batch ended after"), bp.text(t + 5))
        check(progmod.BatchProgress.hms(3700) == "1:01 h" and progmod.BatchProgress.hms(90) == "1 min"
              and progmod.BatchProgress.hms(42) == "42 s", "hms")

        print("[15] the loader cuts long stems to an AWG name and refuses two campaigns under one")
        path = os.path.join(tmp, "plan15.json")
        json.dump({"name": "t15", "targets_dir": TARGETS, "run_dir": os.path.join(tmp, "run15"),
                   "campaigns": [{"stem": "D2N1400"}, {"stem": "S25"}]}, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)
        check(plan["campaigns"][0].get("awg") == "D2N14" and "awg" not in plan["campaigns"][1],
              "D2N1400 trains as D2N14 without a plan-side awg; S25 as itself")
        json.dump({"name": "t15b", "targets_dir": tdir, "run_dir": os.path.join(tmp, "run15"),
                   "campaigns": [{"stem": "S47", "echo": {"spacing_ms": 16.667, "suffix": "ECHO2"}},
                                 {"stem": "S47", "echo": {"spacing_ms": 16.667, "suffix": "EC"}}]}, open(path, "w"))
        try:
            ilc_batch.load_plan(path, cfg=bench_cfg)
            check(False, "S47ECHO2 and S47EC both cut to S47EC: refused")
        except SystemExit as e:
            check("would both train as S47EC" in str(e), f"S47ECHO2 and S47EC both cut to S47EC: refused ({str(e)[-60:]})")

        print("[14a] the last allowed iteration is measured at the full count")
        import types
        lp14 = dict(repeats=64, repeats_early=16, early_rms_v=2.0, max_iterations=20)

        def fake(key, it, rms):
            return types.SimpleNamespace(key=key, active=True, iteration=it, measured=[(it - 1, rms, rms * 5)],
                                         floor_hv=0.01)
        check(ilc_batch.shots_for([fake("X1", 11, 9.0), fake("X2", 11, 9.0)], lp14) == (16, True),
              "far above the noise, not at the cap: the early count")
        check(ilc_batch.shots_for([fake("X1", 20, 9.0), fake("X2", 20, 9.0)], lp14) == (64, False),
              "at the plan's cap: the full count even far above the noise")
        caps14 = {"X1": dict(lp14, max_iterations=12), "X2": dict(lp14, max_iterations=12)}
        check(ilc_batch.shots_for([fake("X1", 11, 9.0), fake("X2", 11, 9.0)], lp14, caps14) == (16, True)
              and ilc_batch.shots_for([fake("X1", 12, 9.0), fake("X2", 12, 9.0)], lp14, caps14) == (64, False),
              "a learn stage's cap: early until its last iteration, then the full count")

        print("[14] the PD every iteration, the analyzer angle, the early shot count")
        from eomilc import diagnostics as dg
        path = write_plan(tmp, ["S25"], name="t14", max_iterations=8, repeats=8, repeats_early=2, early_rms_v=2.0)
        raw = json.load(open(path))
        raw["run_dir"] = os.path.join(tmp, "run14")
        json.dump(raw, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg=bench_cfg)                # PD on CH1
        msgs = []
        ilc_batch.dry_run(plan, log=msgs.append)
        want = "8 shots per measurement (2 while rms > 2 V)"
        check(any(m.startswith("loop:") and m.endswith(want) for m in msgs), f"the dry run says '{want}'")
        awg14, scope14 = bench(bench_cfg)
        scope14.wiring[1] = ("mon", 1)     # the simulated PD: X1's monitor (the sim's PD role reads flat)
        counts = []
        orig_cap = ilc_batch.capture_monitors

        def counting(scope, *a, **k):
            s0 = scope.shots
            out = orig_cap(scope, *a, **k)
            counts.append(scope.shots - s0)
            return out
        ilc_batch.capture_monitors = counting
        try:
            msgs = []
            man = ilc_batch.run_batch(plan, args(), log=msgs.append, bench=(awg14, scope14))
        finally:
            ilc_batch.capture_monitors = orig_cap
        c = man["campaigns"]["S25"]
        rows = c.get("iterations") or []
        sim = plan["_run_dir"]
        check(c["status"] == "done" and len(rows) == len(counts) and len(rows) >= 6,
              f"done, one record per measurement ({len(rows)} records, {len(counts)} captures)")
        check([r["it"] for r in rows] == list(range(len(rows))), "records i00 .. i%02d" % (len(rows) - 1))
        files_ok = all(os.path.exists(os.path.join(sim, f"meas_S25PD_i{r['it']:02d}.npy"))
                       and os.path.exists(os.path.join(sim, f"meas_S25PD_i{r['it']:02d}_stats.npz")) for r in rows)
        check(files_ok, "meas_S25PD_iNN.npy and meas_S25PD_iNN_stats.npz for every iteration")
        pds = [np.load(os.path.join(sim, f"meas_S25PD_i{r['it']:02d}.npy")) for r in rows]
        check(all(abs(r["pd_peak_v"] - float(p.max())) < 1e-12 for r, p in zip(rows, pds))
              and all(p.shape == pds[0].shape for p in pds),
              f"pd_peak_v = the file's peak ({rows[0]['pd_peak_v']:.3f} V on i00)")
        ch_ok = np.isnan(rows[0]["pd_change_mv"]) and all(
            abs(r["pd_change_mv"] - 1e3 * float(np.sqrt(np.mean((p - q) ** 2)))) < 1e-9
            for r, p, q in zip(rows[1:], pds[1:], pds[:-1]))
        check(ch_ok, "pd_change_mv: NaN on i00, then rms(PD(i) - PD(i-1)) in mV ("
              + ", ".join(f"{r['pd_change_mv']:.1f}" for r in rows[1:4]) + ", ...)")
        check(rows[1]["pd_change_mv"] > rows[-1]["pd_change_mv"] > 0,
              "the PD changes less as the loop converges")
        st = dg.load_stats(os.path.join(sim, "meas_S25PD_i01_stats.npz"))
        check(st is not None and st["n_shots"] == rows[1]["shots"] and st["role"] == "PD" and st["scope_ch"] == 1
              and st["vdiv"] == 1.0 and np.isnan(st["analyzer_deg"]) and len(st["sem_t"]) == len(pds[1])
              and "fold_amp" not in st,
              "the PD sidecar: shots, role, CH1, the V/div it was read at, analyzer NaN, sem_t, no fold")
        check(sum(m.startswith("  PD: peak ") for m in msgs) == len(rows)
              and any(re.match(r"  PD: peak \d+\.\d\d V, change vs last \d+\.\d mV rms$", m) for m in msgs),
              "one 'PD: peak .. V, change vs last .. mV rms' line per iteration")
        check(np.isnan(c.get("analyzer_deg", 0.0)) and "  analyzer not read (simulated instruments)" in msgs,
              "analyzer_deg NaN in the manifest; the log says it was not read on simulated instruments")
        shots = [r["shots"] for r in rows]
        check(counts == shots, f"the scope's shot count per measurement is the manifest's: {counts}")
        n2 = shots.index(8) if 8 in shots else len(shots)
        check(shots[0] == 2 and 1 < n2 < len(shots) and set(shots[:n2]) == {2} and set(shots[n2:]) == {8},
              f"2 shots from i00 to i{n2 - 1:02d}, then 8")
        far = [any(v > 2.0 for v in r["rms_v"].values()) for r in rows]
        check(all(far[:n2 - 1]) and all(r["early"] for r in rows[:n2]) and not any(r["early"] for r in rows[n2:]),
              "2 shots only while an rms was over 2 V (" + ", ".join(
                  f"i{r['it']:02d} {max(r['rms_v'].values()):.1f}" for r in rows[:n2 + 1]) + " V)")
        prev = rows[n2 - 1]
        check(all(v <= 2.0 or v <= ilc_batch.EARLY_FLOOR_FACTOR * prev["floor_v"][k] for k, v in prev["rms_v"].items()),
              f"8 from the first measurement whose predecessor was at 2 V or within "
              f"{ilc_batch.EARLY_FLOOR_FACTOR:g}x its own floor (" + ", ".join(
                  f"{k} {v:.2f} V, floor {prev['floor_v'][k]:.2f} V" for k, v in prev["rms_v"].items()) + ")")
        check(any("(2 shots, early)" in m for m in msgs)
              and not any("early" in m for m in msgs if re.search(r"i%02d: rms" % n2, m)),
              "the iteration lines say '(2 shots, early)' and stop saying it")

        # the analyzer read: through the polarimeter's driver, opened, asked, closed, never moved
        import ilc_hardware

        class Mount:
            def __init__(self, deg):
                self.deg, self.calls = deg, []

            def position(self):
                self.calls.append("position")
                return self.deg

            def close(self):
                self.calls.append("close")

            def __getattr__(self, name):          # goto, move_to, move_by, home: must not be called
                raise AssertionError(f"the batch called {name} on the mount")
        mounts = [Mount(45.0)]
        real_open = ilc_hardware.open_ell14
        ilc_hardware.open_ell14 = lambda cfg=None, pcfg=None: mounts[-1]
        try:
            msgs = []
            deg = ilc_batch.read_analyzer(plan, object(), args(simulate=False), log=msgs.append)
            check(deg == 45.0 and mounts[0].calls == ["position", "close"]
                  and msgs == ["  analyzer at +45.0 deg (polarimeter frame)"],
                  f"read once and closed, never moved: {msgs}")
            mounts.append(Mount(315.0))
            check(ilc_batch.read_analyzer(plan, object(), args(simulate=False), log=lambda s: None) == -45.0,
                  "315 deg on the mount reads -45.0 deg")

            def busy(cfg=None, pcfg=None):
                raise PermissionError("could not open port 'COM3': PermissionError(13, 'Access is denied.')")
            ilc_hardware.open_ell14 = busy
            msgs = []
            deg = ilc_batch.read_analyzer(plan, object(), args(simulate=False), log=msgs.append)
            check(np.isnan(deg) and "did not answer" in msgs[0] and "in use" in msgs[0] and "NaN" in msgs[0],
                  f"held by the polarimeter: NaN, logged, no refusal ({msgs[0][:70]}...)")
        finally:
            ilc_hardware.open_ell14 = real_open

        # no PD in the map: the campaign runs as before, no PD files, no PD keys
        path = write_plan(tmp, ["S25"], name="t14b")
        raw = json.load(open(path))
        raw["run_dir"] = os.path.join(tmp, "run14b")
        json.dump(raw, open(path, "w"))
        plan = ilc_batch.load_plan(path, cfg={"scope_channels": SIM_MAP})
        msgs = []
        ilc_batch.dry_run(plan, log=msgs.append)
        check(any(m.startswith("loop:") and m.endswith("6 shots per measurement") for m in msgs)
              and any(m.startswith("PD: no PD channel") for m in msgs), "dry run: no early count, no PD")
        msgs = []
        man = ilc_batch.run_batch(plan, args(), log=msgs.append, bench=bench())
        c = man["campaigns"]["S25"]
        rows = c.get("iterations") or []
        check(c["status"] == "done" and not glob.glob(os.path.join(plan["_run_dir"], "meas_*PD*"))
              and rows and all("pd_peak_v" not in r and r["shots"] == 6 and not r["early"] for r in rows)
              and not any(m.startswith("  PD:") for m in msgs),
              f"no PD role: done, no PD files, {len(rows)} records of 6 shots, no PD lines")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failure(s)" + ("".join(f"\n  {f}" for f in fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
