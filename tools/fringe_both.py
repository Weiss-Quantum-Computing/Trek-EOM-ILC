#!/usr/bin/env python3
"""Drive both EOMs together and test that their phases add.

Each crystal's V_pi was measured on its own by fringe_sweep.py. Driving both
with the same raised cosine sweeps roughly 186 deg of rotation, so the fringe
runs max -> null -> max and the fit is well conditioned. The combined V_pi is
then a one-constraint test of the two separate numbers, and it is also the
number production actually wants, because production drives both channels with
the same waveform.

Both channels carry the SAME waveform shape here, so their two contributions
are perfectly degenerate and only the sum is observable. That is deliberate --
splitting them would need different shapes per channel and answers a question
production does not ask.

    python tools/fringe_both.py --peak-hv 5400
"""
import argparse, json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ilc_bench as ib
import fringe_sweep as fs
from eomilc import polarimetry as pol
from eomilc.config import CHANNELS, HV_PER_MON

T = fs.TRIM


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--peak-hv", type=float, default=5400.0)
    ap.add_argument("--repeats", type=int, default=32)
    ap.add_argument("--pd-scale", type=float, default=1.0)
    ap.add_argument("--pd-offset", type=float, default=2.1)
    ap.add_argument("--label", default="BOTH")
    a = ap.parse_args()

    sib = os.path.dirname(ROOT)
    out = os.path.join(ROOT, "run", "polarimetry")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prev = json.load(open(os.path.join(out, "vpi_summary.json")))
    vp1 = prev["rows"]["X1b"]["v_pi_hv"]
    vp2 = prev["rows"]["X2a"]["v_pi_hv"]
    pred = 1.0 / (1.0 / vp1 + 1.0 / vp2)
    print("single-EOM V_pi: X1 %.1f V, X2 %.1f V -> both together predicts "
          "%.1f V per monitor" % (vp1, vp2, pred))

    t_grid, u1, _ = fs.build(a.peak_hv, CHANNELS["EO1"].gain(5.0))

    awg = ib.make_awg(ib.load_module(
        os.path.join(sib, "BK4063B-AWG-GUI", "bk4063b.py"), "bk4063b"))
    awg.connect()
    try:
        for c in (1, 2):
            b = awg.get_basic_wave(c)
            per = float(str(b.get("PERI", "nan")).rstrip("Ss"))
            nm = awg.get_arb(c).get("NAME")
            print("  CH%d %s %s amp %s period %.4f ms out=%s"
                  % (c, b.get("WVTP"), nm, b.get("AMP"), per * 1e3, awg.is_on(c)))
            if abs(per - len(t_grid) * fs.DT) > 1e-6:
                raise SystemExit("CH%d period is wrong -- fix before driving" % c)
        was_on = {c: awg.is_on(c) for c in (1, 2)}
        for c in (1, 2):
            if not was_on[c]:
                awg.set_output(c, True)
        time.sleep(0.5)
        print("  outputs now:", {c: awg.is_on(c) for c in (1, 2)})
    finally:
        awg.close()

    sc = ib.make_scope(ib.load_module(ib.find_scope_grab(sib), "scope_grab"))
    sc.connect()
    saved = ib.scope_snapshot(sc, [1, 2, 3, 4])
    tb = {q: sc.try_get(q) for q in (":TIMebase:RANGe", ":TIMebase:POSition")}
    try:
        sc.put(":TIMebase:RANGe", 12.0e-3)
        sc.put(":TIMebase:POSition", 6.0e-3)
        ib.scope_apply(sc, {
            2: {"coupling": "DC", "scale": a.pd_scale, "offset": a.pd_offset},
            3: {"coupling": "DC", "scale": 1.0, "offset": a.peak_hv / 2000.0},
            4: {"coupling": "DC", "scale": 1.0, "offset": a.peak_hv / 2000.0}})
        print("capturing %d shots ..." % a.repeats)
        cap = ib.capture_all(sc, [1, 2, 3, 4], t_grid, 0.0, repeats=a.repeats,
                             wait_s=30, points=20000, settle=1.0, keep="both")
    finally:
        sc.put(":TIMebase:RANGe", float(tb[":TIMebase:RANGe"]))
        sc.put(":TIMebase:POSition", float(tb[":TIMebase:POSition"]))
        ib.scope_restore(sc, saved)
        sc.close()

    g = {k: v.mean(axis=0) for k, v in cap.grid.items()}
    p = os.path.join(out, "frsw_%s_%s.npz" % (a.label, stamp))
    np.savez(p, t=t_grid, peak_hv=a.peak_hv, label=a.label,
             **{"%s_mean" % k: v for k, v in g.items()},
             **{"%s_std" % k: cap.grid[k].std(axis=0, ddof=1) for k in cap.grid})
    print("saved %s" % p)

    h1, h2 = g["CH3"][T:-T] * 1000.0, g["CH4"][T:-T] * 1000.0
    pd = g["CH2"][T:-T]
    for c, y in ((3, h1), (4, h2)):
        n = len(np.unique(cap.grid["CH%d" % c][0]))
        print("  Trek mon CH%d %8.1f .. %8.1f V   %d levels" % (c, y.min(), y.max(), n))
        if n < 20:
            print("  ** CH%d is clipped -- reading means nothing" % c)
    print("  PD %.4f .. %.4f V" % (pd.min(), pd.max()))

    o = -(pd.max() - fs.PER_STATIC * pd.min()) / (fs.PER_STATIC - 1)
    # equivalent single-monitor axis: the mean of the two, so the fitted V_pi is
    # directly comparable with the per-monitor prediction above
    hv = 0.5 * (h1 + h2)
    c = pol.fit_fringe(hv, pd, n_eom=1, v_pi_guess=pred, theta_a=0.0, i_dark=o)
    r = pd - pol.intensity(pol.phi_of_volts(hv, c), c)
    print("\n  combined V_pi = %.1f V per monitor   (predicted %.1f, %+.2f %%)"
          % (c.v_pi, pred, 100 * (c.v_pi / pred - 1)))
    print("  v_zero %+.1f V   resid %.3f mV rms   phase swept %.1f deg"
          % (c.v_zero, r.std() * 1e3, 90.0 * hv.max() / c.v_pi))
    # additivity stated the way it will be used: does phi built from the two
    # separate calibrations reproduce the measured intensity?
    phi = 0.5 * np.pi * (h1 / vp1 + h2 / vp2)
    A = np.column_stack([np.ones_like(phi), np.cos(2 * phi), np.sin(2 * phi)])
    coef, *_ = np.linalg.lstsq(A, pd - o, rcond=None)
    rr = (pd - o) - A @ coef
    print("  phases-add model from the two separate calibrations: "
          "resid %.3f mV rms" % (rr.std() * 1e3))
    json.dump({"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": p,
               "v_pi_combined": c.v_pi, "v_pi_predicted": pred,
               "v_zero": c.v_zero, "resid_mV": r.std() * 1e3,
               "additive_model_resid_mV": rr.std() * 1e3},
              open(os.path.join(out, "vpi_both.json"), "w"), indent=1)
    print("  saved run/polarimetry/vpi_both.json")


if __name__ == "__main__":
    main()
