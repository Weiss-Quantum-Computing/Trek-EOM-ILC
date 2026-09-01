#!/usr/bin/env python3
"""Measure the photodiode-path anti-alias filter directly, electrically.

Phase 2 inferred this filter from the same data whose flatness it then claimed,
which is circular for the roll-off half of the result: both crystals share the
detection path, so a real 40 kHz pole common to both EOMs is indistinguishable
from a filter. Driving the filter from the AWG and measuring its output where
the photodiode normally sits removes the ambiguity entirely.

    AWG CH1 -> filter input;  filter output -> scope CH2 (the PD's usual place)

    python tools/filter_check.py --label F1        # upload
    python tools/filter_check.py --label F1 --capture-only

Same 37 odd bins, same 7.002 ms window and same record as the fidelity
measurement, so the answer divides straight into it with no interpolation.
The tone rides on NO dc offset here -- there is no crystal in this path to keep
off a standing bias, and a bias-free burst puts every volt into the tones.
"""
import argparse, json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ilc_bench as ib
import sysid_make as sm
import frf_optical as fo

N, DT, FULL_SCALE = fo.N, fo.DT, 10.0
LAD = {1: [1, 1], 2: [1, 3, 1], 3: [1, 6, 5, 1], 4: [1, 10, 15, 7, 1]}


def ladder(f, rc, n):
    x = 1j * 2 * np.pi * np.asarray(f, float) * rc
    return 1.0 / sum(c * x ** i for i, c in enumerate(LAD[n]))


def f3db(rc, n):
    g = np.geomspace(1e3, 2e6, 40000)
    m = 20 * np.log10(np.abs(ladder(g, rc, n)))
    return float(np.interp(-3.0, m[::-1], g[::-1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--awg-ch", type=int, default=1)
    ap.add_argument("--in-ch", type=int, default=1, help="scope ch on the filter input")
    ap.add_argument("--out-ch", type=int, default=2, help="scope ch on the filter output")
    ap.add_argument("--peak", type=float, default=2.0, help="volts at the AWG")
    ap.add_argument("--tones", type=int, default=48)
    ap.add_argument("--k-lo", type=int, default=3)
    ap.add_argument("--k-hi", type=int, default=279,
                    help="279 = 39.85 kHz matches the fidelity run; 1050 = "
                         "150 kHz characterises the filter past its corner")
    ap.add_argument("--repeats", type=int, default=64)
    ap.add_argument("--label", required=True)
    ap.add_argument("--capture-only", action="store_true")
    a = ap.parse_args()

    sib = os.path.dirname(ROOT)
    out = os.path.join(ROOT, "run", "polarimetry")
    bins = fo.tone_bins(a.tones, a.k_lo, a.k_hi)
    u, win = sm.ramped_multitone(a.peak, 0.0, bins, fo.N_HOLD, dt=DT,
                                 n_edge=fo.N_EDGE, n_settle=fo.N_SETTLE,
                                 taper_s=100e-6, awg_rail=10.0)
    df = 1.0 / (fo.N_HOLD * DT)
    print(f"{len(bins)} odd bins, {df*bins[0]:.1f} Hz to {df*bins[-1]/1e3:.2f} kHz, "
          f"window {win[0]}..{win[1]}, peak {a.peak:g} V, no dc offset")

    if not a.capture_only:
        awg = ib.make_awg(ib.load_module(
            os.path.join(sib, "BK4063B-AWG-GUI", "bk4063b.py"), "bk4063b"))
        awg.connect()
        try:
            if awg.is_on(a.awg_ch) is not False:
                raise SystemExit(f"AWG CH{a.awg_ch} is ON -- refusing to upload")
            n = awg.upload_arb(a.awg_ch, f"FILT{a.label}"[:11], u / FULL_SCALE,
                               normalize=False, freq=1.0 / (N * DT))
            b = awg.get_basic_wave(a.awg_ch)
            print(f"uploaded {n} pts, period {b.get('PERI')}, amp {b.get('AMP')}")
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
        chans = {a.in_ch:  {"coupling": "DC", "scale": a.peak/3.0, "offset": 0.0},
                 a.out_ch: {"coupling": "DC", "scale": a.peak/3.0, "offset": 0.0},
                 3: {"coupling": "DC", "scale": 1.0, "offset": 0.0},
                 4: {"coupling": "DC", "scale": 1.0, "offset": 0.0}}
        ib.scope_apply(sc, chans)
        t = np.arange(N) * DT
        print(f"capturing {a.repeats} shots ...")
        cap = ib.capture_all(sc, [1, 2, 3, 4], t, 0.0, repeats=a.repeats,
                             wait_s=30, points=20000, settle=1.0, keep="both")
    finally:
        sc.put(":TIMebase:RANGe", float(tb[":TIMebase:RANGe"]))
        sc.put(":TIMebase:POSition", float(tb[":TIMebase:POSition"]))
        ib.scope_restore(sc, saved)
        sc.close()

    g = {k: v.mean(axis=0) for k, v in cap.grid.items()}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    p = os.path.join(out, f"filt_{a.label}_{stamp}.npz")
    np.savez(p, t=np.arange(N)*DT, u=u, bins=bins, win=win, peak=a.peak,
             **{f"{k}_mean": v for k, v in g.items()},
             **{f"{k}_std": cap.grid[k].std(axis=0, ddof=1) for k in cap.grid})
    print(f"saved {p}")
    for c in (1, 2, 3, 4):
        y = g[f"CH{c}"]
        print(f"  CH{c} {y.min():+8.4f} .. {y.max():+8.4f} V   p-p {np.ptp(y):7.4f}")
    for c, w in chans.items():
        lo, hi = w["offset"] - 4*w["scale"], w["offset"] + 4*w["scale"]
        y = g[f"CH{c}"]
        if y.min() < lo or y.max() > hi:
            raise SystemExit(f"CH{c} ran {y.min():.4f}..{y.max():.4f} V outside its "
                             f"{lo:.4f}..{hi:.4f} V window -- fix the levels")

    i0, i1 = win
    n = i1 - i0
    f = np.arange(n)/(n*DT)
    k = np.asarray(bins)
    fk = f[k]
    def spec(y):
        s = y[i0:i1] - y[i0:i1].mean()
        return np.fft.rfft(s)[k]
    IN, OUT = spec(g[f"CH{a.in_ch}"]), spec(g[f"CH{a.out_ch}"])
    prog = spec(u)
    print(f"\n  drive seen on CH{a.in_ch}: {np.abs(IN).mean()/np.abs(prog).mean():.4f} "
          f"of the programmed amplitude "
          f"({'the input is here' if np.abs(IN).mean() > 0.2*np.abs(prog).mean() else 'NOT the input -- using the programmed waveform'})")
    ref = IN if np.abs(IN).mean() > 0.2*np.abs(prog).mean() else prog
    H = OUT/ref
    g0 = np.abs(H[fk < 3000]).mean()

    print("\n  fitting the RC ladder to the MEASURED filter:")
    print("     n   RC (us)   RC/nominal   f_-3dB     resid dB   resid deg")
    res = {}
    for nn in (1, 2, 3, 4):
        best = min(np.linspace(0.05e-6, 5e-6, 20000),
                   key=lambda rc: (np.abs(H/g0/ladder(fk, rc, nn) - 1)**2).sum())
        q = H/g0/ladder(fk, best, nn)
        res[nn] = (best, (20*np.log10(np.abs(q))).std(), np.degrees(np.angle(q)).std(),
                   float((np.abs(q-1)**2).sum()))
        print("     %d   %7.4f   %8.2f    %6.1f kHz   %7.4f    %7.3f"
              % (nn, best*1e6, best/fo.RC_NOMINAL, f3db(best, nn)/1e3,
                 res[nn][1], res[nn][2]))
    bn = min(res, key=lambda nn: res[nn][3])
    print(f"\n  best order n = {bn}, RC = {res[bn][0]*1e6:.4f} us "
          f"({res[bn][0]/fo.RC_NOMINAL:.2f} x nominal), f_-3dB "
          f"{f3db(res[bn][0], bn)/1e3:.1f} kHz")
    print(f"  Phase 2 assumed n={fo.N_AA}, RC={fo.RC_AA*1e6:.2f} us "
          f"(f_-3dB {f3db(fo.RC_AA, fo.N_AA)/1e3:.1f} kHz)")
    d = 20*np.log10(np.abs(H/g0)) - 20*np.log10(np.abs(fo.aa_response(fk)))
    ph = np.degrees(np.angle(H/g0) - np.angle(fo.aa_response(fk)))
    print(f"  measured MINUS the Phase 2 model: {d.mean():+.3f} +- {d.std():.3f} dB, "
          f"{ph.mean():+.2f} +- {ph.std():.2f} deg")
    print(f"  -> the Phase 2 fidelity moves by at most {np.abs(d).max():.3f} dB / "
          f"{np.abs(ph).max():.2f} deg")

    print("\n     f (kHz)   |H| dB    phase      vs Phase 2 model")
    for j in range(0, len(k), 2):
        print("  %9.2f  %+7.3f  %+8.2f      %+6.3f dB  %+6.2f deg"
              % (fk[j]/1e3, 20*np.log10(abs(H[j])/g0),
                 np.degrees(np.angle(H[j])), d[j], ph[j]))
    np.savez(os.path.join(out, f"filt_{a.label}_result.npz"),
             f=fk, H=H/g0, order=bn, rc=res[bn][0], dc_gain=g0)
    json.dump({"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": p,
               "dc_gain": float(g0), "best_order": int(bn),
               "rc_us": float(res[bn][0]*1e6),
               "rc_over_nominal": float(res[bn][0]/fo.RC_NOMINAL),
               "f_3db_hz": f3db(res[bn][0], bn),
               "fits": {str(nn): {"rc_us": v[0]*1e6, "resid_db": v[1],
                                  "resid_deg": v[2], "chi2": v[3]}
                        for nn, v in res.items()},
               "vs_phase2_db": [float(d.mean()), float(d.std())],
               "vs_phase2_deg": [float(ph.mean()), float(ph.std())]},
              open(os.path.join(out, f"filt_{a.label}_summary.json"), "w"), indent=1)
    print(f"\n  saved filt_{a.label}_summary.json")


if __name__ == "__main__":
    main()
