#!/usr/bin/env python3
"""The bench-safety rules against the simulated bench (eomilc.simbench), no
instruments: never-float (a run ends PARKED -- a flat record at the drive's
idle, the output left as it is -- and OFF only on an explicit, confirmed
act), and the guards that keep a non-finite or mis-set drive away from the
AWG. One block per rule; each block names the review finding it covers
(_reviews/2026-10-15, G1-*).

    python tests/test_safety.py
"""
import contextlib, io, os, shutil, sys, tempfile
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ilc_bench                                              # noqa: E402
from eomilc import simbench                                   # noqa: E402

FIXTURE = os.path.join(HERE, "tests", "data", "drive_MKJX1.state.npz")
FRFS = {1: os.path.join(HERE, "run", "frf_WIDE_X1.csv"), 2: os.path.join(HERE, "run", "frf_WIDE_X2.csv")}
fails = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        fails.append(what)


def sim_bench():
    awg, scope, mod = simbench.make_bench(FRFS)
    ilc_bench._AWGMOD = mod
    return awg, scope, mod


def run_cli(argv, awg, scope, mod, stdin=""):
    """ilc_bench.main() on the simulated pair; returns (exit, printed)."""
    saved = (ilc_bench.load_module, ilc_bench.make_awg, ilc_bench.make_scope, sys.argv, sys.stdin)
    ilc_bench.load_module = lambda path, name: mod
    ilc_bench.make_awg = lambda m: awg
    ilc_bench.make_scope = lambda m: scope
    sys.argv = ["ilc_bench.py"] + argv
    sys.stdin = io.StringIO(stdin)
    out, code = io.StringIO(), None
    try:
        with contextlib.redirect_stdout(out):
            try:
                ilc_bench.main()
            except SystemExit as e:
                code = e.code
    finally:
        (ilc_bench.load_module, ilc_bench.make_awg, ilc_bench.make_scope, sys.argv, sys.stdin) = saved
    return code, out.getvalue()


def run_main(fn, argv, stdin=""):
    """fn() with sys.argv = argv; returns (exit, printed)."""
    saved = (sys.argv, sys.stdin)
    sys.argv, sys.stdin = list(argv), io.StringIO(stdin)
    out, code = io.StringIO(), None
    try:
        with contextlib.redirect_stdout(out):
            try:
                fn()
            except SystemExit as e:
                code = e.code
    finally:
        sys.argv, sys.stdin = saved
    return code, out.getvalue()


def write_capture(path, z, gain=0.98):
    """A scope CSV (time_s, CH3) of the state's target x gain, 1 us apart,
    spanning the record with 0.5 ms to spare -- what `run_ilc step` reads."""
    t = np.asarray(z["t"], float)
    tc = np.arange(-0.5e-3, t[-1] + 0.5e-3, 1e-6)
    v = np.interp(tc, t, np.asarray(z["target"], float) * gain)
    with open(path, "w") as f:
        f.write("time_s,CH3\n")
        for a, b in zip(tc, v):
            f.write(f"{a:.9e},{b:.6e}\n")
    return path


def main():
    tmp = tempfile.mkdtemp(prefix="ilcsafety_")
    try:
        z = np.load(FIXTURE, allow_pickle=True)
        period = len(z["t"]) * float(z["dt"])

        print("[1] park_output: a flat record at the idle, same length, output untouched (G1-4)")
        awg, scope, mod = sim_bench()
        u = np.asarray(z["u"], float)
        name, idle = ilc_bench.park_output(awg, 2, u, 10.0, "PARK2")
        check(name == "PARK2" and awg.ch[2]["name"] == "PARK2" and len(awg.ch[2]["wave"]) == len(u)
              and float(np.ptp(awg.ch[2]["wave"])) == 0.0 and abs(awg.ch[2]["wave"][0] * 10 - u[0]) < 1e-3,
              f"PARK2 flat at {idle:+.4f} V over {len(u)} points")
        check(not any(e[0] == "output" for e in awg.log), "no output switched either way")
        try:
            ilc_bench.park_output(awg, 1, np.r_[np.nan, u[1:]])
            check(False, "a NaN idle is refused")
        except ValueError:
            check(True, "a NaN idle is refused")

        print("[2] the CLI ends a run parked, the output left ON (G1-4)")
        state = os.path.join(tmp, "drive_MKJX1.state.npz")
        shutil.copy(FIXTURE, state)
        awg, scope, mod = sim_bench()
        awg.apply_channel(1, {"BSWV": {"FRQ": 1.0 / period}})
        awg.set_output(1, True)                # the person's ON, as the CLI expects
        code, out = run_cli(["--resume", state, "--awg-ch", "1", "--scope-ch", "3", "--iterations", "1",
                             "--repeats", "16", "--outdir", tmp, "--awg-dir", os.path.join(tmp, "Waveforms")],
                            awg, scope, mod)
        check(code is None, f"the run finished (exit {code})")
        check(awg.is_on(1) and awg.ch[1]["name"] == "PARK1" and float(np.ptp(awg.ch[1]["wave"])) == 0.0,
              "CH1 still ON, playing PARK1 (flat)")
        check(not any(e[0] == "output" and not e[2] for e in awg.log), "never switched OFF")
        check("CH1 parked (end of run): plays PARK1" in out, "the log says it parked")

        print("[3] the CLI's alignment refusal also ends parked (G1-4, G1-8)")
        shutil.copy(FIXTURE, state)
        awg, scope, mod = sim_bench()
        awg.apply_channel(1, {"BSWV": {"FRQ": 1.0 / period}})     # output OFF: the drive reads flat
        code, out = run_cli(["--resume", state, "--awg-ch", "1", "--scope-ch", "3", "--iterations", "1",
                             "--repeats", "16", "--outdir", tmp, "--awg-dir", os.path.join(tmp, "Waveforms")],
                            awg, scope, mod)
        check(code is not None and "flat" in str(code), f"refused: {str(code)[:70]}")
        check(awg.ch[1]["name"] == "PARK1" and not any(e[0] == "output" for e in awg.log),
              "PARK1 selected, the output never touched")

        print("[4] --outputs-off-at-end: OFF only after a typed OFF (G1-4)")
        shutil.copy(FIXTURE, state)
        awg, scope, mod = sim_bench()
        code, out = run_cli(["--resume", state, "--outputs-off-at-end"], awg, scope, mod, stdin="no\n")
        check(code is not None and "not confirmed" in str(code) and awg.log == [],
              "declined: nothing touched")
        awg.apply_channel(1, {"BSWV": {"FRQ": 1.0 / period}})
        awg.set_output(1, True)
        code, out = run_cli(["--resume", state, "--awg-ch", "1", "--scope-ch", "3", "--iterations", "0",
                             "--repeats", "16", "--outdir", tmp, "--awg-dir", os.path.join(tmp, "Waveforms"),
                             "--outputs-off-at-end"], awg, scope, mod, stdin="OFF\n")
        check(not awg.is_on(1) and "CH1 output OFF (end of run, --outputs-off-at-end)" in out,
              "confirmed: CH1 OFF at the end, and the log says why")

        print("[5] run_ilc step keeps every key it does not rewrite, and learns with the state's FRF (G1-19)")
        import run_ilc
        st5 = os.path.join(tmp, "drive_K5.state.npz")
        z5 = {k: z[k] for k in z.files}
        z5.update(corr_from=3, model="frf", frf_path=FRFS[1], frf_use=15e3, frf_max=22e3,
                  scope_channels="{'3': 'MonX1'}", run_id=1234.5, optical_note="kept")
        np.savez(st5, **z5)
        meas = write_capture(os.path.join(tmp, "cap_K5.csv"), z)
        code, out = run_main(run_ilc.main, ["run_ilc.py", "step", "--state", st5, "--measured", meas,
                                            "--mon-col", "CH3", "--out", os.path.join(tmp, "drive_K5_iter7.csv"),
                                            "--awg-dir", os.path.join(tmp, "Waveforms"), "--force"])
        check(code is None, f"the step ran (exit {code})")
        r5 = run_ilc.load_state(st5)
        check(int(r5["iteration"]) == int(z["iteration"]) + 1, "iteration advanced")
        lost = [k for k in z5 if k not in r5]
        check(not lost, f"no key lost ({lost or 'all ' + str(len(z5)) + ' kept'})")
        check(int(r5["corr_from"]) == 3 and str(r5["model"]) == "frf" and str(r5["frf_path"]) == FRFS[1]
              and float(r5["frf_max"]) == 22e3 and str(r5["optical_note"]) == "kept",
              "corr_from, model, frf_* and an unknown key carried as they were")
        check("update uses the measured inverse from" in out,
              "the step learned with the state's own FRF, not the gain-only plant")
        z5["frf_path"] = os.path.join(tmp, "gone.csv")
        np.savez(st5, **z5)
        code, out = run_main(run_ilc.main, ["run_ilc.py", "step", "--state", st5, "--measured", meas,
                                            "--mon-col", "CH3", "--awg-dir", os.path.join(tmp, "Waveforms")])
        check(code is not None and "--frf" in str(code), "an FRF state whose file is gone is refused, naming --frf")

        print("[6] run_ilc init refuses to replace an existing state (G1-20)")
        tgt6 = os.path.join(tmp, "target_K6.csv")
        with open(tgt6, "w") as f:
            f.write("time_us,voltage_V\n")
            for a, b in zip(np.asarray(z["t"]) * 1e6, np.asarray(z["target"]) * 1000.0):
                f.write(f"{a:.4f},{b:.4f}\n")
        out6 = os.path.join(tmp, "drive_K6_iter0.csv")
        argv6 = ["run_ilc.py", "init", "--target", tgt6, "--channel", "EO1", "--out", out6,
                 "--name", "K6", "--awg-dir", os.path.join(tmp, "Waveforms")]
        code, _o = run_main(run_ilc.main, argv6)
        st6 = os.path.join(tmp, "drive_K6.state.npz")
        check(code is None and os.path.exists(st6), "the first init writes the state")
        with np.load(st6, allow_pickle=True) as zz:
            zz6 = {k: zz[k] for k in zz.files}
        zz6["iteration"] = 4                        # a campaign under way
        np.savez(st6, **zz6)
        code, _o = run_main(run_ilc.main, argv6)
        with np.load(st6, allow_pickle=True) as zz:
            it6 = int(zz["iteration"])
        check(code is not None and "--overwrite-state" in str(code) and it6 == 4,
              "a second init is refused, naming --overwrite-state; the state is untouched")
        code, _o = run_main(run_ilc.main, argv6 + ["--overwrite-state"])
        with np.load(st6, allow_pickle=True) as zz:
            it6 = int(zz["iteration"])
        check(code is None and it6 == 0, "--overwrite-state replaces it deliberately")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failure(s)" + ("".join(f"\n  {f}" for f in fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
