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

        print("[7] check_limits: a NaN drive FAILs first; slew/current also from the drive (G1-10)")
        from eomilc import ilc
        from eomilc.config import CHANNELS
        loop7 = run_ilc.build_loop({k: z[k] for k in z.files})
        u7 = np.asarray(z["u"], float)
        rep = loop7.check(u7)
        check(bool(rep), f"the fixture's own drive passes ({str(rep).splitlines()[-1].strip()[:90]})")
        bad = u7.copy()
        bad[len(bad) // 2] = np.nan
        rep = ilc.check_limits(bad, loop7.target, loop7.dt, CHANNELS["EO1"])
        check(not rep and "non-finite" in str(rep), "one NaN sample: FAIL, named")
        check(not loop7.check(bad), "Loop.check: FAIL too")
        try:
            ilc_bench.upload_drive(awg, 1, "NANTEST", bad, 10.0)
            check(False, "upload_drive refuses a NaN drive")
        except ValueError:
            check(True, "upload_drive refuses a NaN drive")
        # learned HF ripple the target does not have: 40 kHz at 0.15 V on the
        # drive, through a flat-gain model (the fixture's 2.3 kHz resonance
        # would hide it -- the prediction is only as good as the loop's model)
        z7 = {k: z[k] for k in z.files}
        z7.update(fn=0.0, zeta=0.0, tau=0.0, tau2=0.0)
        loop7f = run_ilc.build_loop(z7)
        rip = u7 + 0.15 * np.sin(2 * np.pi * 40e3 * np.asarray(z["t"], float))
        rip[0] = u7[0]
        rep_t = ilc.check_limits(rip, loop7f.target, loop7f.dt, CHANNELS["EO1"])
        rep_d = loop7f.check(rip)
        check(bool(rep_t) and not rep_d and "the drive, predicted" in str(rep_d),
              "40 kHz ripple: passes against the target alone, FAILs on the drive's predicted slew/current")

        print("[8] scope.resample: no half-sample shift at an even decimation (G1-7)")
        from eomilc import scope as scopeio, corrections as corrmod
        t_dst = np.arange(5501) * 2e-6
        for ratio in (2.2, 4.0, 3.0):
            dts = 2e-6 / ratio
            t_src = np.arange(-1e-3, 12e-3, dts)
            y = np.sin(2 * np.pi * 1e3 * t_src)
            r = scopeio.resample(t_src, y, t_dst)
            M = np.column_stack([np.sin(2 * np.pi * 1e3 * t_dst), np.cos(2 * np.pi * 1e3 * t_dst)])
            a, b = np.linalg.lstsq(M, r, rcond=None)[0]
            delay_us = -np.arctan2(b, a) / (2 * np.pi * 1e3) * 1e6
            n = int(round(2e-6 / dts))
            check(abs(delay_us) < 0.02, f"ratio {ratio:g} (box of {n}): 1 kHz sine delayed {delay_us:+.3f} us")
        d = corrmod._onto(t_dst, t_src, np.sin(2 * np.pi * 1e3 * t_src), dts, 2e-6)
        a, b = np.linalg.lstsq(M, d, rcond=None)[0]
        check(abs(np.arctan2(b, a) / (2 * np.pi * 1e3) * 1e6) < 0.02, "corrections._onto: centred too")

        print("[9] FRF mode smooths the drive far above f_max (G1-11)")
        loop9 = run_ilc.build_loop({k: z[k] for k in z.files})
        loop9.frf = ilc.FRF(FRFS[1], f_use=15e3, f_max=22e3)
        tt = np.asarray(z["t"], float)
        hf = 0.05 * np.sin(2 * np.pi * 150e3 * tt)
        u9 = u7 + hf
        u9n = loop9.update(u9, loop9.target)        # no error: only the smoothing acts
        f9 = np.fft.rfftfreq(len(tt), loop9.dt)
        spec = lambda x: np.abs(np.fft.rfft(x - np.mean(x)))
        at = lambda x, f0: float(spec(x)[np.argmin(abs(f9 - f0))])
        check(at(u9n, 150e3) < 0.05 * at(u9, 150e3), f"150 kHz seed content cut {at(u9, 150e3)/max(at(u9n, 150e3), 1e-12):.0f}x")
        check(abs(at(u9n, 5e3) - at(u9, 5e3)) <= 0.01 * at(u9, 5e3) + 1e-9, "content at 5 kHz untouched")

        print("[10] plateau: best_it is the iteration number after the run_id filter (G1-12)")
        hist = [dict(peak_err_hv=20.0 - i, update_rms=0.1, run_id=1.0) for i in range(10)]
        hist += [dict(peak_err_hv=9.0 + (0 if i == 3 else 1), update_rms=0.1, run_id=2.0) for i in range(7)]
        p10 = ilc.plateau(hist, run_id=2.0)
        check(p10 is not None and p10["best_it"] == 13, f"best_it {p10 and p10['best_it']} (the run's i13)")

        print("[11] a 0 Hz correction band is refused, not applied as zero (G1-13)")
        try:
            corrmod.lowpass(np.ones(100), 1e-6, 0.0)
            check(False, "lowpass(f_cut=0) refused")
        except ValueError:
            check(True, "lowpass(f_cut=0) refused")
        c11 = corrmod.Correction(t=tt, delta=np.zeros_like(tt), sigma=None, meta={})
        try:
            corrmod.gate(c11, tt, loop9.target * 1000.0, corrmod.GateSettings(band_hz=0.0))
            check(False, "gate(band_hz=0) refused")
        except ValueError:
            check(True, "gate(band_hz=0) refused")

        print("[12] write_bk_waveform refuses a NaN drive (G1-16)")
        from eomilc import outputs
        try:
            outputs.write_bk_waveform(os.path.join(tmp, "nan.csv"), bad, "NAN", 10.0)
            check(False, "refused")
        except ValueError:
            check(not os.path.exists(os.path.join(tmp, "nan.csv")), "refused, nothing written")

        print("[13] the panel's Upload runs the limit and channel checks, and asks while ON (G1-30)")
        import types
        import ilc_gui
        shutil.copy(FIXTURE, state)
        sess = ilc_gui.load_session(state)
        awg13, _sc13, mod13 = sim_bench()
        answers = []
        stub = types.SimpleNamespace(session=sess, _bench_modules=lambda: (None, mod13),
                                     ask_user=lambda title, msg: (answers.append(title), answers[-1] == "yes")[1])
        saved_make = ilc_bench.make_awg
        ilc_bench.make_awg = lambda m: awg13
        try:
            def upload():
                n0 = len(awg13.log)
                with contextlib.redirect_stdout(io.StringIO()) as o:
                    ilc_gui.App._upload_work(stub, 1, f"UP{len(awg13.log)}", sess.full_scale)
                return [e for e in awg13.log[n0:] if e[0] == "upload"], o.getvalue()
            ups, txt = upload()                       # FRQ still the simulator's 100 Hz
            check(not ups and "FRQ is" in txt, "a channel at another record's FRQ: refused, nothing uploaded")
            awg13.apply_channel(1, {"BSWV": {"FRQ": 1.0 / period}})
            u_ok = sess.u.copy()
            sess.u = u_ok.copy()
            sess.u[0] = 0.5                           # past the 100 mV idle cap
            ups, txt = upload()
            check(not ups and "limit check" in txt, "a drive failing the limit check: refused")
            sess.u = u_ok
            ups, txt = upload()
            check(len(ups) == 1, "a good drive into an OFF channel: uploaded")
            awg13.set_output(1, True)
            ups, txt = upload()
            check(not ups and answers[-1] == "Output is ON", "output ON: asked, and a no uploads nothing")
            stub.ask_user = lambda title, msg: True
            ups, txt = upload()
            check(len(ups) == 1, "output ON and a yes: uploaded")
        finally:
            ilc_bench.make_awg = saved_make

        print("[14] the CLI reads the scope at >= 2.2 points per record sample by default (G1-5)")
        shutil.copy(FIXTURE, state)
        awg, scope, mod = sim_bench()
        awg.apply_channel(1, {"BSWV": {"FRQ": 1.0 / period}})
        awg.set_output(1, True)
        asked = []
        real_wf = scope.waveform
        scope.waveform = lambda ch, points_mode="RAW", points=None: (asked.append(points),
                                                                      real_wf(ch, points_mode, points))[1]
        code, out = run_cli(["--resume", state, "--awg-ch", "1", "--scope-ch", "3", "--iterations", "0",
                             "--repeats", "16", "--outdir", tmp, "--awg-dir", os.path.join(tmp, "Waveforms")],
                            awg, scope, mod)
        want = ilc_bench.scopeio.scope_points_for(2.2 * len(z["t"]))
        check(code is None and asked and all(p == want for p in asked),
              f"alignment and capture both read {want} points ({len(asked)} reads)")

        print("[15] the CLI's --resume keeps every state key and learns with the state's FRF (G1-6)")
        z15 = {k: z[k] for k in z.files}
        z15.update(model="frf", frf_path=FRFS[1], frf_use=15e3, frf_max=22e3, corr_from=2,
                   scope_channels="{'3': 'MonX1'}", early_note="kept")
        np.savez(state, **z15)
        awg, scope, mod = sim_bench()
        awg.apply_channel(1, {"BSWV": {"FRQ": 1.0 / period}})
        awg.set_output(1, True)
        code, out = run_cli(["--resume", state, "--awg-ch", "1", "--scope-ch", "3", "--iterations", "1",
                             "--repeats", "16", "--outdir", tmp, "--awg-dir", os.path.join(tmp, "Waveforms")],
                            awg, scope, mod)
        with np.load(state, allow_pickle=True) as zz:
            r15 = {k: zz[k] for k in zz.files}
        lost = [k for k in z15 if k not in r15]
        check(code is None and int(r15["iteration"]) == int(z["iteration"]) + 1, "one iteration saved")
        check(not lost and str(r15["model"]) == "frf" and str(r15["early_note"]) == "kept"
              and int(r15["corr_from"]) == 2, f"no key lost ({lost or 'all kept'})")
        check("update uses the state's measured inverse" in out, "the update used the state's FRF")

        print("[16] verify_alignment raises Refused, not SystemExit (G1-8)")
        awg, scope, mod = sim_bench()                # output OFF: the drive reads flat
        try:
            ilc_bench.verify_alignment(scope, 1, u7, np.asarray(z["t"], float), 0.0, 1.0)
            check(False, "refused")
        except ilc_bench.Refused as e:
            check("flat" in str(e) and not isinstance(e, SystemExit), "Refused, a RuntimeError, says why")

        print("[17] Hardware tab: one bad V/div reply marks that channel only (G1-1)")
        import ilc_hardware as ihw

        class FakeScope:
            def __init__(self, replies):
                self.r = replies
            def connect(self):
                return "KEYSIGHT,MSO-X 2014A,SN,1.0"
            def try_get(self, q):
                return self.r.get(q)
            def close(self):
                pass
        rep17 = {":ACQuire:TYPE": "HRES", ":TIMebase:RANGe": "0.02"}
        for c in (1, 2, 3, 4):
            rep17[f":CHANnel{c}:SCALe"] = "1.0E+00"
            rep17[f":CHANnel{c}:DISPlay"] = "1"
        rep17[":CHANnel3:SCALe"] = "1.0E+0\n;:CHAN"            # a partial reply after a timeout
        saved_ms = ilc_bench.make_scope
        ilc_bench.make_scope = lambda m: FakeScope(rep17)
        try:
            r17 = ihw.check_scope(None)
        finally:
            ilc_bench.make_scope = saved_ms
        check(r17["ok"] and "HRES" in r17["text"] and "CH3 ?" in r17["detail"]
              and "CH4 1 V/div" in r17["detail"], f"row {r17['text']!r}, detail marks CH3 only")

        print("[18] Hardware tab: a generator that answers only *IDN? is not green (G1-2)")

        class MuteAwg:
            def connect(self):
                return "B&K,4063B,SN,1.0"
            def __getattr__(self, name):
                def fail(*a, **k):
                    raise IOError("timeout")
                return fail
            def close(self):
                pass
        saved_ma = ilc_bench.make_awg
        ilc_bench.make_awg = lambda m: MuteAwg()
        try:
            r18 = ihw.check_awg(None)
        finally:
            ilc_bench.make_awg = saved_ma
        check(not r18["ok"] and "CH1 ?" in r18["text"], f"row {r18['text']!r} is red")
        awg18, _s, mod18 = sim_bench()
        ilc_bench.make_awg = lambda m: awg18
        try:
            r18b = ihw.check_awg(mod18)
        finally:
            ilc_bench.make_awg = saved_ma
        check(r18b["ok"], "the simulated generator's row stays green")

        print("[19] Move: the analyzer angle is wrapped and range-checked first (G1-3)")

        class FakeMount:
            def __init__(self):
                self.pos, self.asked = 0.0, []
            def goto(self, deg, tol=0.05, settle=0.0):
                self.asked.append(deg)
                self.pos = deg % 360.0
            def move_by(self, d):
                self.pos = (self.pos + d) % 360.0
            def position(self):
                return self.pos
            def close(self):
                pass
        mount = FakeMount()
        saved_open = ihw.open_ell14
        ihw.open_ell14 = lambda cfg=None, pcfg=None: mount
        try:
            r19 = ihw.move_ell14(3600.0, pcfg={"ell_port": "COMX"}, log=lambda s: None)
            r19n = ihw.move_ell14(float("nan"), pcfg={"ell_port": "COMX"}, log=lambda s: None)
        finally:
            ihw.open_ell14 = saved_open
        check(r19["ok"] and abs(r19["deg"]) < 1e-9 and all(abs(a) < 10 for a in mount.asked),
              f"3600 deg goes as 0 ({r19['text']}; the mount was sent {mount.asked})")
        check(not r19n["ok"] and len(mount.asked) <= 2, "NaN refused before the mount is opened")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failure(s)" + ("".join(f"\n  {f}" for f in fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
