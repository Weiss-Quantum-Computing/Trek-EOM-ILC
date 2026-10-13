#!/usr/bin/env python3
"""ilc_batch against the simulated bench (eomilc.simbench): the ordering rules,
resume, refusal and GUI compatibility. Needs the 2026-09-28 ramp suite's
targets (or any target_<stem>X1/X2 pair) -- pass the folder as argv[1].

    python tests/test_batch.py <suite>/targets
"""
import json, os, shutil, sys, tempfile, types
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
        check(not awg.is_on(1) and not awg.is_on(2), "both outputs off at the end")
        for c in (1, 2):
            names = [e[2] for e in ev if e[0] == "upload" and e[1] == c]
            check(all(a != b for a, b in zip(names, names[1:])), f"CH{c} uploads alternate slots ({len(names)} uploads)")
            check(set(names) <= {f"BX{c}A", f"BX{c}B"}, f"CH{c} uses only its two slots")
        # every FRQ write happens with both outputs off: replay the log
        on = {1: False, 2: False}
        ok = True
        for e in ev:
            if e[0] == "output":
                on[e[1]] = e[2]
            if e[0] == "frq" and (on[1] or on[2]):
                ok = False
        check(ok, "every FRQ write had BOTH outputs off")
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
        check(not awg3.is_on(1) and not awg3.is_on(2), "outputs off after the interrupt")
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

        print("[6] without --allow-output-on nothing is switched on")
        path = write_plan(tmp, ["N20"])
        awg6, scope6 = bench()
        man = ilc_batch.cmd_run(args(plan=path, simulate=False, allow_output_on=False), bench=(awg6, scope6))
        check(man["campaigns"]["N20"]["status"] == "refused", "N20 refused")
        check(not any(e[0] == "output" and e[2] for e in awg6.log), "nothing switched on")

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
        check(not awg7.is_on(1) and not awg7.is_on(2), "outputs off after Stop")
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failure(s)" + ("".join(f"\n  {f}" for f in fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
