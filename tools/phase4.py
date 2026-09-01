#!/usr/bin/env python3
"""Phase 4: the production ramps, seen in the light.

What the campaign is built toward.  For each drive it captures the ramp, inverts
the fringe to the rotation the light actually experienced, and compares that
against what the Trek monitor says -- with the measured monitor-to-crystal
correction applied, so the comparison is against the crystal rather than the
amplifier output.

    python tools/phase4.py --drives PRFRX1B_iter0 PR1PX1C_i19 PRFRX1B_i17

256 SHOTS, NOT 64.  Step 4.5 says raise the count if the floor is not 3x below
the production residual.  Measured 1 Sep: at 64 shots the shape floor is
0.130 V of equivalent drive against a 0.195 V residual, i.e. 1.50x.  At 256 it
is 0.065 V, i.e. 3.0x.  So the trigger is already met and 256 is the default.

THE FLOOR IS NOT sigma/sqrt(N).  The photodiode channel's DC level drifts
0.68 mV over a 70 s acquisition, so raw averaging stalls at 0.626 mV instead of
following 1/sqrt(N).  That does not bias the ensemble mean -- averaging is
linear -- but it does mean the honest floor must be measured on records with
each shot's own offset removed, where the SHAPE averages properly to within 9 %
of ideal out to N = 64.  See ensemble().

WHAT LIMITS THIS MEASUREMENT IS THE FORWARD MODEL, NOT THE NOISE.  Measured
1 Sep: model-vs-data sits at 4.17 mV rms = 3.28 V of equivalent drive against a
0.065 V floor and a 0.195 V residual, i.e. 17x.  It is purely dynamic -- 0.31 mV
where the ramp is flat, 3-6 mV wherever it moves.  The ramp carries content well
above 83 kHz, which is where the monitor-to-crystal correction stops being
measured because the anti-alias filter kills optical SNR there.  Read the
numbers out of this tool with that ceiling in mind.
"""
import argparse, json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ilc_bench as ib
from eomilc import polarimetry as pol
from eomilc import monitor as mon_corr
from eomilc.config import CHANNELS, HV_PER_MON

N, DT, FULL_SCALE = 5501, 2e-6, 10.0
OUT = os.path.join(ROOT, "run", "polarimetry")
TARGET = os.path.join(ROOT, "waveforms", "target_PARX1.csv")


def read_csv(path, col=-1):
    rows = [l.split(",") for l in open(path) if l[0] not in "#t" and l.strip()]
    return np.array([float(r[col]) for r in rows])


def ensemble(stack):
    """The ensemble average.

    Per-shot DC removal was tried here and is a NO-OP by construction: the mean
    of the per-shot means IS the grand mean, so subtracting each shot's offset
    and restoring the grand one returns exactly stack.mean(axis=0).  Averaging
    is linear and drift does not bias the mean.

    Where the drift DOES matter is the uncertainty on that mean, and there it
    matters a lot: raw averaging stalls at 0.626 mV instead of following
    1/sqrt(N), because the photodiode channel wanders 0.68 mV over a 70 s
    acquisition.  Remove each shot's offset before estimating scatter and the
    shape averages properly to within 9 % of ideal out to N = 64.  So: average
    plainly, but never quote sigma/sqrt(N) as the floor without removing the
    per-shot offset first.
    """
    return np.asarray(stack, float).mean(axis=0)


def calibrate(pd, mon_hv, v_pi, v_zero, theta_a, i_dark, i_edge=800):
    """A and B for this record, with theta_a and V_pi taken as known."""
    phi = (0.5 * np.pi / v_pi) * (mon_hv - v_zero)
    M = np.column_stack([np.ones(i_edge - 5),
                         np.cos(2.0 * (phi[5:i_edge] - theta_a))])
    c, *_ = np.linalg.lstsq(M, pd[5:i_edge] - i_dark, rcond=None)
    a, b = float(c[0]), float(c[1])
    if b < 0:
        b, theta_a = -b, theta_a + 0.5 * np.pi
    om = np.pi / v_pi                      # n_eom = 1, one crystal driven
    return pol.FringeCal(a=a, b=b, omega=om, psi=om * v_zero + 2 * theta_a,
                         theta_a=theta_a, n_eom=1, i_dark=i_dark)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drives", nargs="+", required=True)
    ap.add_argument("--repeats", type=int, default=256)
    ap.add_argument("--theta-a", type=float, default=0.0, help="degrees")
    ap.add_argument("--pd-ch", type=int, default=3)
    ap.add_argument("--mon-ch", type=int, default=2)
    ap.add_argument("--pd-scale", type=float, default=1.0)
    ap.add_argument("--pd-offset", type=float, default=2.0)
    ap.add_argument("--i-dark", type=float, default=-0.012)
    ap.add_argument("--tag", default="p4")
    a = ap.parse_args()

    vs = json.load(open(os.path.join(OUT, "vpi_summary.json")))["rows"]["X1b"]
    v_pi, v_zero = vs["v_pi_hv"], vs["v_zero"]
    tgt = read_csv(TARGET, 1)
    th = np.radians(a.theta_a)
    sib = os.path.dirname(ROOT)
    print(f"V_pi {v_pi:.1f} V, EO zero {v_zero:+.1f} V, analyser {a.theta_a:+.1f} deg, "
          f"{a.repeats} shots per drive")

    results = {}
    for name in a.drives:
        path = os.path.join(ROOT, "run", f"drive_{name}.csv")
        if not os.path.exists(path):
            raise SystemExit(f"no drive at {path}")
        u = read_csv(path)
        if len(u) != N:
            raise SystemExit(f"{name} is {len(u)} points, expected {N}")
        print(f"\n=== {name}: {u.min():+.4f} .. {u.max():+.4f} V AWG")

        awg = ib.make_awg(ib.load_module(
            os.path.join(sib, "BK4063B-AWG-GUI", "bk4063b.py"), "bk4063b"))
        awg.connect()
        try:
            awg.set_output(1, False)
            time.sleep(0.3)
            awg.upload_arb(1, name.replace("_", "")[:11], u / FULL_SCALE,
                           normalize=False, freq=1.0 / (N * DT))
            awg.set_output(1, True)
            time.sleep(0.5)
        finally:
            awg.close()

        sc = ib.make_scope(ib.load_module(ib.find_scope_grab(sib), "scope_grab"))
        sc.connect()
        saved = ib.scope_snapshot(sc, [1, 2, 3, 4])
        tb = {q: sc.try_get(q) for q in (":TIMebase:RANGe", ":TIMebase:POSition")}
        try:
            sc.put(":TIMebase:RANGe", 12.0e-3)
            sc.put(":TIMebase:POSition", 6.0e-3)
            chans = {1: {"coupling": "DC", "scale": 2.0, "offset": 4.7},
                     a.pd_ch: {"coupling": "DC", "scale": a.pd_scale,
                               "offset": a.pd_offset},
                     a.mon_ch: {"coupling": "DC", "scale": 1.0, "offset": 2.6},
                     4: {"coupling": "DC", "scale": 0.2, "offset": 0.0}}
            ib.scope_apply(sc, chans)
            print(f"  capturing {a.repeats} shots ...")
            cap = ib.capture_all(sc, [1, 2, 3, 4], np.arange(N) * DT, 0.0,
                                 repeats=a.repeats, wait_s=30, points=20000,
                                 settle=1.0, keep="both")
        finally:
            sc.put(":TIMebase:RANGe", float(tb[":TIMebase:RANGe"]))
            sc.put(":TIMebase:POSition", float(tb[":TIMebase:POSition"]))
            ib.scope_restore(sc, saved)
            sc.close()

        pd = ensemble(cap.grid[f"CH{a.pd_ch}"])
        mv = ensemble(cap.grid[f"CH{a.mon_ch}"]) * HV_PER_MON
        p = os.path.join(OUT, f"{a.tag}_{name}.npz")
        np.savez(p, t=np.arange(N) * DT, u=u, pd=pd, mon_hv=mv,
                 theta_a=a.theta_a, repeats=a.repeats,
                 pd_std=cap.grid[f"CH{a.pd_ch}"].std(axis=0, ddof=1),
                 mon_std=cap.grid[f"CH{a.mon_ch}"].std(axis=0, ddof=1))
        for c, w in chans.items():
            y = cap.grid[f"CH{c}"].mean(axis=0)
            lo, hi = w["offset"] - 4*w["scale"], w["offset"] + 4*w["scale"]
            if y.min() < lo or y.max() > hi:
                raise SystemExit(f"CH{c} outside its window ({y.min():.3f}.."
                                 f"{y.max():.3f} vs {lo:.3f}..{hi:.3f})")

        cal = calibrate(pd, mv, v_pi, v_zero, th, a.i_dark)
        # what the MONITOR says the crystal did, corrected for the monitor's own
        # infidelity -- this is the whole point of Phase 2 feeding Phase 4
        v_crystal = mon_corr.apply(mv, DT, outside="hold")
        dphi, eq, good = pol.invert_linear(pd, v_crystal, cal)
        mon_res = mv - tgt                           # what the ILC itself minimises
        print(f"  fringe: A {cal.a:.4f} B {cal.b:.4f} V, visibility {cal.visibility:.4f}")
        print(f"  usable samples (|sin 2d| > 0.30): {good.sum()} of {N} "
              f"({100*good.sum()/N:.0f} %)")
        print(f"  monitor residual vs target : {mon_res.std():8.4f} V rms")
        print(f"  OPTICAL residual           : {np.nanstd(eq):8.4f} V rms  ({np.degrees(np.nanstd(dphi)):.4f} deg of rotation)")
        print(f"  difference the monitor cannot see: {np.nanstd(eq[good] - mon_res[good]):8.4f} V rms")
        results[name] = dict(
            visibility=float(cal.visibility), usable=int(good.sum()),
            monitor_residual_V=float(mon_res.std()),
            optical_residual_V=float(np.nanstd(eq)),
            blind_difference_V=float(np.nanstd(eq[good] - mon_res[good])),
            file=os.path.basename(p))
        print(f"  saved {os.path.basename(p)}")

    json.dump({"when": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "theta_a_deg": a.theta_a, "repeats": a.repeats,
               "v_pi": v_pi, "v_zero": v_zero,
               "monitor_correction": "eomilc.monitor, measured 1 Sep",
               "results": results},
              open(os.path.join(OUT, f"{a.tag}_summary.json"), "w"), indent=1)
    print(f"\nsaved {a.tag}_summary.json")


if __name__ == "__main__":
    main()
