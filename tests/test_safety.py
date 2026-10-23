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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failure(s)" + ("".join(f"\n  {f}" for f in fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
