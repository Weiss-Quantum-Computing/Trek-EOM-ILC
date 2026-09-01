#!/usr/bin/env python3
"""Phase 3 null tests: run the production ramp and see what is left in the light.

Steps 3.1-3.4 are the same capture under different physical conditions -- the
analyser at the EO zero, the beam blocked, the Trek input disconnected -- so
this takes a --label per condition and compares them at the end.

    python tools/nulltest.py --label N1_pol0 --upload      # once
    python tools/nulltest.py --label N1_pol0
    python tools/nulltest.py --label N3_blocked
    python tools/nulltest.py --compare N1_pol0 N3_blocked

THE PLAN'S 0.6 % THRESHOLD IS WRONG.  It assumes the hold sits exactly at
phi = 180 deg, where |sin 2phi| = 0.  Measured V_pi says the production ramp
overshoots by 2.0 deg, and sensitivity climbs 0.035 per degree there, so the
hold corners leak 5.6 % driving X1 alone (9.3 % with both EOMs) rather than
0.6 %.  Even on the ASSUMED V_pi the answer was 2.4 %, because the EO zero is
not at commanded 0 V.  This tool computes the four corner sensitivities from
the measured calibration and quotes the threshold from those, rather than
carrying a constant that was never right.
"""
import argparse, json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ilc_bench as ib
from eomilc.config import CHANNELS, HV_PER_MON

N, DT, FULL_SCALE = 5501, 2e-6, 10.0
DRIVE = os.path.join(ROOT, "run", "drive_PRFRX1B_i17.csv")
TARGET = os.path.join(ROOT, "waveforms", "target_PARX1.csv")
OUT = os.path.join(ROOT, "run", "polarimetry")


def read_csv(path, col=-1):
    rows = [l.split(",") for l in open(path) if l[0] not in "#t" and l.strip()]
    return np.array([float(r[col]) for r in rows])


def corners(v_pi, v_zero, n_eom=1):
    """The four ramp corners and the sensitivity at each, from the target."""
    v = read_csv(TARGET, 1)
    t = read_csv(TARGET, 0) * 1e-6
    flat = v > v.max() - 1.0
    idx = {"start": 0, "into hold": int(np.argmax(flat)),
           "out of hold": int(len(flat) - 1 - np.argmax(flat[::-1])),
           "end": len(v) - 1}
    vp = v_pi / n_eom
    phi = (0.5 * np.pi / vp) * (v - v_zero)
    return {k: (t[j], v[j], np.degrees(phi[j]), abs(np.sin(2 * phi[j])))
            for k, j in idx.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--awg-ch", type=int, default=1)
    ap.add_argument("--pd-ch", type=int, default=3)
    ap.add_argument("--mon-ch", type=int, default=2)
    ap.add_argument("--repeats", type=int, default=64)
    ap.add_argument("--pd-scale", type=float, default=1.0)
    ap.add_argument("--pd-offset", type=float, default=2.0)
    ap.add_argument("--n-eom", type=int, default=1)
    a = ap.parse_args()

    vs = json.load(open(os.path.join(OUT, "vpi_summary.json")))["rows"]["X1b"]
    cor = corners(vs["v_pi_hv"], vs["v_zero"], a.n_eom)
    worst = max(s for _, _, _, s in cor.values())
    print("corner sensitivities at theta_a = 0, from the measured calibration:")
    for k, (t, v, ph, s) in cor.items():
        print("  %-12s t=%7.3f ms  V=%7.1f  phi=%7.2f deg  |sin 2phi| = %.4f"
              % (k, t * 1e3, v, ph, s))
    print("  -> genuine polarisation signal leaks through at up to %.1f %%." % (100 * worst))
    print("     ONLY a ramp-synchronous feature above that is RAM, etalon, "
          "steering or pickup.\n     The plan's 0.6 % is wrong; see the module docstring.")

    if a.compare:
        res = {}
        for lab in a.compare:
            p = os.path.join(OUT, f"null_{lab}.npz")
            if not os.path.exists(p):
                raise SystemExit(f"no capture called {lab} -- looked for {p}")
            res[lab] = np.load(p)
        A, B = (res[k] for k in a.compare)
        for k in a.compare:
            d = res[k]
            pd = d["CH%d_mean" % a.pd_ch]
            print("\n  %s: PD %.4f .. %.4f V, shot std %.3f mV"
                  % (k, pd.min(), pd.max(), d["CH%d_std" % a.pd_ch].mean() * 1e3))
        pa = A["CH%d_mean" % a.pd_ch]
        pb = B["CH%d_mean" % a.pd_ch]
        print("\n  ramp-synchronous amplitude, peak-to-peak over the record:")
        print("    %-14s %.4f V" % (a.compare[0], np.ptp(pa)))
        print("    %-14s %.4f V  (%.2f %% of the first)"
              % (a.compare[1], np.ptp(pb), 100 * np.ptp(pb) / np.ptp(pa)))
        return

    if not a.label:
        raise SystemExit("give --label, or --compare A B")

    u = read_csv(DRIVE)
    if len(u) != N:
        raise SystemExit(f"drive is {len(u)} points, expected {N}")
    gain = CHANNELS["EO1"].gain(5.0)
    print(f"\ndrive {os.path.basename(DRIVE)}: {u.min():+.4f} .. {u.max():+.4f} V AWG "
          f"({u.max()*gain*HV_PER_MON:.0f} V at the EOM, {100*abs(u).max()/10:.1f} % of rail)")

    sib = os.path.dirname(ROOT)
    if a.upload:
        awg = ib.make_awg(ib.load_module(
            os.path.join(sib, "BK4063B-AWG-GUI", "bk4063b.py"), "bk4063b"))
        awg.connect()
        try:
            if awg.is_on(a.awg_ch) is not False:
                raise SystemExit(f"AWG CH{a.awg_ch} is ON -- refusing to upload")
            n = awg.upload_arb(a.awg_ch, "PRFRX1Bi17", u / FULL_SCALE,
                               normalize=False, freq=1.0 / (N * DT))
            print(f"uploaded {n} pts; enable the output and re-run without --upload")
        finally:
            awg.close()
        return

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
        print(f"capturing {a.repeats} shots ...")
        cap = ib.capture_all(sc, [1, 2, 3, 4], np.arange(N) * DT, 0.0,
                             repeats=a.repeats, wait_s=30, points=20000,
                             settle=1.0, keep="both")
    finally:
        sc.put(":TIMebase:RANGe", float(tb[":TIMebase:RANGe"]))
        sc.put(":TIMebase:POSition", float(tb[":TIMebase:POSition"]))
        ib.scope_restore(sc, saved)
        sc.close()

    g = {k: v.mean(axis=0) for k, v in cap.grid.items()}
    p = os.path.join(OUT, f"null_{a.label}.npz")
    np.savez(p, t=np.arange(N) * DT, u=u, label=a.label,
             **{f"{k}_mean": v for k, v in g.items()},
             **{f"{k}_std": cap.grid[k].std(axis=0, ddof=1) for k in cap.grid})
    print(f"saved {p}")
    bad = False
    for c, w in chans.items():
        y = g[f"CH{c}"]
        lo, hi = w["offset"] - 4 * w["scale"], w["offset"] + 4 * w["scale"]
        print(f"  CH{c} {y.min():+8.4f} .. {y.max():+8.4f} V "
              f"(window {lo:+.3f}..{hi:+.3f})")
        bad |= y.min() < lo or y.max() > hi
    if bad:
        raise SystemExit("a channel is outside its window -- fix the levels and re-run")
    pd = g[f"CH{a.pd_ch}"]
    sd = cap.grid[f"CH{a.pd_ch}"].std(axis=0, ddof=1)
    print(f"\n  PD peak-to-peak over the record : {np.ptp(pd)*1e3:8.2f} mV")
    print(f"  PD shot-to-shot std, mean       : {sd.mean()*1e3:8.3f} mV")
    print(f"  ... at the four corners         : "
          + "  ".join(f"{sd[int(t/DT)]*1e3:.3f}" for t, _, _, _ in cor.values()))
    print(f"  broadband: std of the record after removing the ramp-synchronous mean")
    print(f"             {np.sqrt((sd**2).mean())*1e3:.3f} mV rms")


if __name__ == "__main__":
    main()
