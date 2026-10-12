#!/usr/bin/env python3
"""The GUI's Batch window against the simulated bench: plan builder, Check
plan, Simulate, Stop, the bench path (with the confirmation) and following a
campaign in the plots. Needs a target folder, as tests/test_batch.py does:

    python tests/test_batch_gui.py <suite>/targets

One Tk root for the whole process (CLAUDE.md: a second root wedges aqua). The
config file is redirected to a temporary folder; the dialogs are stubbed.
"""
import json, os, shutil, sys, tempfile, time
import tkinter as tk

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ilc_gui, ilc_bench                                        # noqa: E402
from eomilc import simbench                                      # noqa: E402

TARGETS = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None
fails = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        fails.append(what)


def pump(root, app, limit_s=300):
    """Until the worker is done. update() is called with short sleeps; the
    bound only protects against a worker that never finishes."""
    t0 = time.time()
    root.update()
    while app.busy and time.time() - t0 < limit_s:
        root.update()
        time.sleep(0.02)
    for _ in range(10):                  # drain the last queued calls
        root.update()
        time.sleep(0.02)
    return not app.busy


def main():
    if not TARGETS:
        sys.exit(__doc__)
    tmp = tempfile.mkdtemp(prefix="ilcbatchgui_")
    ilc_gui.CONFIG_PATH = os.path.join(tmp, "config.json")
    asked = []
    ilc_gui.messagebox.askyesno = lambda title, msg, **kw: asked.append(msg) or answer["yes"]
    ilc_gui.messagebox.showerror = lambda *a, **kw: asked.append(("error",) + a)
    ilc_gui.messagebox.showinfo = lambda *a, **kw: asked.append(("info",) + a)
    answer = {"yes": True}
    root = tk.Tk()
    try:
        app = ilc_gui.App(root)
        root.withdraw()

        print("[1] New plan... writes a plan and loads it")
        app.do_batch()
        bw = app._batch_win
        bw.win.withdraw()
        pd_ = ilc_gui.PlanDialog(bw)
        pd_.win.withdraw()
        pd_.targets.set(TARGETS)
        root.update()
        check("S25" in pd_.stems.get(), "stems filled from the folder")
        pd_.stems.set("S25, SW50, N15")
        pd_.name.set("gui")
        plan_path = os.path.join(tmp, "batch_gui.json")
        ilc_gui.filedialog.asksaveasfilename = lambda **kw: plan_path
        pd_.save()
        root.update()
        raw = json.load(open(plan_path))
        check([c["stem"] for c in raw["campaigns"]] == ["S25", "SW50", "N15"], "plan written in run order")
        raw["run_dir"] = os.path.join(tmp, "run")              # keep the test out of the repo's run/
        json.dump(raw, open(plan_path, "w"))
        bw.load()
        check(bw.tree.get_children() == ("S25", "SW50", "N15"), "table lists the campaigns")
        check(app.batchplan_var.get() == plan_path, "plan box points at it")

        print("[2] Check plan fills points and estimates, no instruments")
        bw.loop_vars["max_iterations"].set("5")
        bw.loop_vars["min_iterations"].set("3")
        bw.loop_vars["repeats"].set("16")
        app.do_batch_run("check")
        check(pump(root, app), "check finished")
        vals = dict(zip([c[0] for c in bw.COLS], bw.tree.item("S25", "values")))
        check(str(vals["pts"]).isdigit() and float(vals["est"]) > 0, f"S25: {vals['pts']} points, ~{vals['est']} min")
        check(json.load(open(plan_path)).get("loop", {}) == {}, "Check plan does not write the plan")

        print("[3] Simulate runs all three, follows X2 in the plots")
        app.batchfollowch_var.set("X2")
        app.do_batch_run("sim")
        check(pump(root, app), "simulated batch finished")
        loop = json.load(open(plan_path))["loop"]
        check(loop.get("max_iterations") == 5 and loop.get("repeats") == 16, "window numbers written into the plan")
        for stem in ("S25", "SW50", "N15"):
            v = dict(zip([c[0] for c in bw.COLS], bw.tree.item(stem, "values")))
            check(v["status"] == "done (sim)" and v["X1"].startswith("keep"), f"{stem}: {v['status']}, X1 {v['X1']}, X2 {v['X2']}")
        s = app.session
        check(s is not None and s.stem == "N15X2" and os.sep + "sim" + os.sep in s.state_path,
              f"panel follows the last campaign's X2 ({s.stem if s else None}, {len(s.snapshots) if s else 0} measurements)")
        check(not os.path.exists(os.path.join(tmp, "run", "drive_S25X1.state.npz")), "nothing written outside run/sim")

        print("[4] Stop mid-campaign")
        man_before = json.load(open(os.path.join(tmp, "run", "sim", "batch_gui.json")))
        bw.loop_vars["max_iterations"].set("6")
        app.batchredo_var.set(True)
        orig = bw.event

        def stop_on_iteration(kind, info):
            orig(kind, info)
            if kind == "iteration" and info["stem"] == "SW50":
                app.stop_evt.set()           # as if Stop were pressed during SW50
        bw.event = stop_on_iteration
        app.do_batch_run("sim")
        check(pump(root, app), "stopped batch finished")
        bw.event = orig
        app.batchredo_var.set(False)
        v = dict(zip([c[0] for c in bw.COLS], bw.tree.item("SW50", "values")))
        man = json.load(open(os.path.join(tmp, "run", "sim", "batch_gui.json")))
        c = man["campaigns"]
        check(c["S25"]["status"] == "done" and c["S25"]["channels"]["X1"]["resumed_at"] == 5,
              "S25 continued from i05 to the new cap")
        check(c["SW50"]["status"] == "stopped" and v["status"] == "stopped (sim)", f"SW50 {v['status']}")
        check(c["N15"]["started"] == man_before["campaigns"]["N15"]["started"], "N15 not started again")

        print("[5] Run on bench: refused without the confirmation, runs with it")
        opened = []

        def fake_pair():
            frfs = {1: os.path.join(HERE, "run", "frf_WIDE_X1.csv"), 2: os.path.join(HERE, "run", "frf_WIDE_X2.csv")}
            awg, scope, mod = simbench.make_bench(frfs)
            ilc_bench._AWGMOD = mod
            opened.append(awg)
            return awg, scope
        app._connect_pair = fake_pair
        answer["yes"] = False
        n0 = len(asked)
        app.do_batch_run("bench")
        root.update()
        check(len(asked) == n0 + 1 and not app.busy and not opened, "declined: nothing opened, nothing run")
        check("Switch the outputs ON" in asked[-1] and "S25, SW50, N15" in asked[-1], "the question names the campaigns")
        answer["yes"] = True
        app.do_batch_run("bench")
        check(pump(root, app), "bench batch finished")
        awg = opened[0]
        check(not awg.is_on(1) and not awg.is_on(2), "outputs off at the end")
        check(not any(e[0] == "FRQ_UNDER_LIVE_OUTPUT" for e in awg.log), "FRQ never under a live output")
        man = json.load(open(os.path.join(tmp, "run", "batch_gui.json")))
        check(all(man["campaigns"][s]["status"] == "done" for s in ("S25", "SW50", "N15")),
              "all three done, manifest in run/ (not run/sim)")
        v = dict(zip([c[0] for c in bw.COLS], bw.tree.item("SW50", "values")))
        check(v["status"] == "done", f"table shows the bench manifest: SW50 {v['status']}, {v['note']}")

        print("[6] double-click loads a campaign's state")
        bw.tree.selection_set("SW50")
        app.batchfollowch_var.set("X1")
        bw.open_selected()
        check(app.session.stem == "SW50X1" and app.state_var.get().endswith("drive_SW50X1.state.npz"),
              "SW50X1 loaded in the panel")

        print("[7] the window closes and reopens with its settings")
        bw.close()
        app.do_batch()
        check(app._batch_win.plan is not None and app._batch_win.loop_vars["max_iterations"].get() == "6",
              "plan and loop numbers back")
        app._batch_win.win.withdraw()
        app._save_config()
        cfg = json.load(open(ilc_gui.CONFIG_PATH))
        check(cfg.get("batch_plan") == plan_path and cfg.get("batch_follow_ch") == "X1", "remembered in the config")
    finally:
        root.destroy()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failure(s)" + ("".join(f"\n  {f}" for f in fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
