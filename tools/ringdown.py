#!/usr/bin/env python3
"""Look for modes that ring in the LIGHT but not the monitor.

Campaign step 2.1.  The point is coverage: the 49-tone grid used for the
fidelity measurement sits on 143 Hz centres and walks straight past a narrow
high-Q mode.

THE STEP THE PLAN ASKS FOR CANNOT DO THIS -- measured, 1 Sep.  A step's
spectrum falls as 1/f, so a 180 V step leaves almost no drive above a kHz: the
monitor signal went from 18.5 mV at 0.3-1 kHz to 0.6 mV at 10-20 kHz against a
1.4 mV noise floor, only 7 bins reached SNR 10, and the detection limit came
out at 233 V of equivalent mode amplitude -- larger than the step itself.  A
null from that is not a null, it is an empty measurement.  `--mode step` is
kept because the record is worth having, but do not draw conclusions from it.

The default `--mode dense` drives EVERY bin from 428 Hz to 60 kHz with a
flat-spectrum Schroeder multitone instead.  Same peak volts, but the energy is
spread evenly rather than piled up at DC, which buys about three orders of
magnitude of drive at 20 kHz.

    python tools/ringdown.py --label R1                 # upload
    python tools/ringdown.py --label R1 --capture-only

NO STANDING BIAS, AND NONE NEEDED.  Sensitivity is |sin 2(phi - theta_a)|, so
with the analyser at theta_a = -44.2 deg it peaks at phi = +0.8 deg -- about
46 V, i.e. essentially zero.  The most sensitive operating point on the fringe
is the one the crystals are safest at, so the record steps a couple of hundred
volts about zero and never goes near kV.

WHAT COUNTS AS A FINDING.  Not a peak in the photodiode: the plant rings, and
that ring is real voltage the monitor sees too.  A finding is a peak in the
RATIO -- present in the light, absent from the monitor -- that is also
phase-stable shot to shot, because a mode driven by the step is locked to the
trigger while ambient acoustic noise is not.  Both are reported per bin.
"""
import argparse, json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ilc_bench as ib
import frf_optical as fo
import sysid_make as sm
from eomilc.config import CHANNELS, HV_PER_MON, LIMITS

N, DT, FULL_SCALE = 5501, 2e-6, 10.0
N_PRE, N_TAIL = 500, 501          # baseline before the step, and the return


def build(step_hv, gain, mode="dense", k_hi=420):
    """The excitation, and the window to analyse.

    "step": baseline, one-sample step, long hold, step down, baseline.
    "dense": every bin from 3 to k_hi, Schroeder-phased so the crest factor
             stays near 2, on a zero bias.  No standing voltage is needed at
             all -- see the module docstring.
    """
    v = step_hv / HV_PER_MON / gain
    if v > LIMITS.awg_rail:
        raise ValueError(f"{v:.3f} V exceeds the {LIMITS.awg_rail:g} V rail")
    if mode == "step":
        n_hold = N - N_PRE - N_TAIL
        u = np.concatenate([np.zeros(N_PRE), np.full(n_hold, v),
                            np.zeros(N_TAIL)])
        win = (N_PRE, N_PRE + n_hold)
    elif mode == "dense":
        bins = np.arange(3, k_hi + 1)
        u, win = sm.ramped_multitone(v, 0.0, bins, fo.N_HOLD, dt=DT,
                                     n_edge=fo.N_EDGE, n_settle=fo.N_SETTLE,
                                     taper_s=100e-6, awg_rail=LIMITS.awg_rail)
    else:
        raise ValueError(f"mode must be 'step' or 'dense', got {mode!r}")
    if abs(u[0]) > 0.1 or abs(u[-1]) > 0.1:
        raise ValueError("record must start and end inside the +-100 mV clamp")
    return u, v, win


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--awg-ch", type=int, default=1)
    ap.add_argument("--cmd-ch", type=int, default=1)
    ap.add_argument("--pd-ch", type=int, default=3)
    ap.add_argument("--mon-ch", type=int, default=2)
    ap.add_argument("--quiet-ch", type=int, default=4)
    ap.add_argument("--eo", default="EO1")
    ap.add_argument("--step-hv", type=float, default=180.0,
                    help="0.1-0.2 V at the Trek input is 110-220 V at the EOM")
    ap.add_argument("--mode", default="dense", choices=("dense", "step"))
    ap.add_argument("--k-hi", type=int, default=420)   # 60.0 kHz
    ap.add_argument("--skip-us", type=float, default=60.0,
                    help="samples dropped after the step, so the plant's own "
                         "edge does not dominate the spectrum")
    ap.add_argument("--repeats", type=int, default=64)
    ap.add_argument("--pd-scale", type=float, default=0.1)
    ap.add_argument("--pd-offset", type=float, default=1.72)
    ap.add_argument("--label", required=True)
    ap.add_argument("--capture-only", action="store_true")
    a = ap.parse_args()

    sib = os.path.dirname(ROOT)
    out = os.path.join(ROOT, "run", "polarimetry")
    gain = CHANNELS[a.eo].gain(5.0)
    u, v, win = build(a.step_hv, gain, a.mode, a.k_hi)
    print(f"step {a.step_hv:.0f} V at the EOM = {v:.4f} V AWG = "
          f"{v*CHANNELS[a.eo].divider:.4f} V at the Trek input")
    print(f"  hold {win[1]-win[0]} samples = {(win[1]-win[0])*DT*1e3:.3f} ms, "
          f"resolution {1/((win[1]-win[0])*DT):.1f} Hz")

    if not a.capture_only:
        awg = ib.make_awg(ib.load_module(
            os.path.join(sib, "BK4063B-AWG-GUI", "bk4063b.py"), "bk4063b"))
        awg.connect()
        try:
            if awg.is_on(a.awg_ch) is not False:
                raise SystemExit(f"AWG CH{a.awg_ch} is ON -- refusing to upload")
            n = awg.upload_arb(a.awg_ch, f"RING{a.label}"[:11], u / FULL_SCALE,
                               normalize=False, freq=1.0 / (N * DT))
            print(f"uploaded {n} pts, amp {awg.get_basic_wave(a.awg_ch).get('AMP')}")
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
        # the step is unipolar 0..v, the dense drive is bipolar +-v about zero
        c_off = v / 2 if a.mode == "step" else 0.0
        m_off = a.step_hv / 2000.0 if a.mode == "step" else 0.0
        chans = {a.cmd_ch: {"coupling": "DC", "scale": max(0.05, v / 3.0),
                            "offset": c_off},
                 a.pd_ch:  {"coupling": "DC", "scale": a.pd_scale,
                            "offset": a.pd_offset},
                 # the plant rings: zeta = 0.21 gives ~50 % overshoot on a step,
                 # and the record holds both the up-step and down-step rings, so
                 # leave 1.6x the step of headroom on the monitor
                 a.mon_ch: {"coupling": "DC", "scale": max(0.05, a.step_hv / 2000.0),
                            "offset": m_off},
                 a.quiet_ch: {"coupling": "DC", "scale": 0.2, "offset": 0.0}}
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

    g = {k: val.mean(axis=0) for k, val in cap.grid.items()}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    p = os.path.join(out, f"ring_{a.label}_{stamp}.npz")
    np.savez(p, t=np.arange(N) * DT, u=u, win=win, step_hv=a.step_hv,
             **{f"{k}_mean": val for k, val in g.items()},
             **{f"{k}_std": cap.grid[k].std(axis=0, ddof=1) for k in cap.grid})
    print(f"saved {p}")
    for c, w in chans.items():
        y = g[f"CH{c}"]
        lo, hi = w["offset"] - 4*w["scale"], w["offset"] + 4*w["scale"]
        print(f"  CH{c} {y.min():+8.4f} .. {y.max():+8.4f} V "
              f"(window {lo:+.3f}..{hi:+.3f})")
        if y.min() < lo or y.max() > hi:
            raise SystemExit(f"CH{c} is outside its window -- fix the levels")

    skip = int(a.skip_us * 1e-6 / DT) if a.mode == "step" else 0
    i0, i1 = win[0] + skip, win[1]
    n = i1 - i0
    f = np.fft.rfftfreq(n, DT)
    w = np.hanning(n)
    def spec(y):
        return np.fft.rfft((y[i0:i1] - y[i0:i1].mean()) * w)
    PD, MO = spec(g[f"CH{a.pd_ch}"]), spec(g[f"CH{a.mon_ch}"])
    # shot-to-shot phase stability: a mode rung by the step is locked to the
    # trigger, ambient acoustic pickup is not
    per = np.array([spec(cap.grid[f"CH{a.pd_ch}"][j]) for j in range(a.repeats)])
    lock = np.abs((per / np.abs(per)).mean(axis=0))       # 1 locked, 0 random
    band = (f > 300) & (f < 60e3)
    ratio = np.abs(PD) / np.maximum(np.abs(MO), 1e-15) / np.abs(fo.aa_response(f))
    med = np.median(ratio[band])
    excess = 20 * np.log10(ratio / med)
    print(f"\n  analysis window {n} samples = {n*DT*1e3:.3f} ms, "
          f"resolution {1/(n*DT):.1f} Hz, {band.sum()} bins in 0.3-60 kHz")
    print(f"  median |PD|/|MON| in band {med:.4g} (this is the flat part; the "
          f"multitone already measured it)")
    cand = band & (excess > 6.0) & (lock > 0.5)
    print(f"\n  bins with >6 dB excess in the light AND shot-locked: {cand.sum()}")
    if cand.any():
        idx = np.argsort(excess * cand)[::-1][:12]
        print("     f (Hz)   excess dB   lock   |PD| rel   |MON| rel")
        for j in sorted(idx):
            if not cand[j]:
                continue
            print("  %9.1f   %+8.2f   %.3f   %9.2e  %9.2e"
                  % (f[j], excess[j], lock[j],
                     np.abs(PD[j])/np.abs(PD[band]).max(),
                     np.abs(MO[j])/np.abs(MO[band]).max()))
    else:
        print("     none -- no narrow feature appears in the light that is not "
              "also in the monitor")
    top = np.argsort(np.where(band, excess, -99))[::-1][:8]
    print("\n  largest excesses regardless of lock (sanity view):")
    for j in sorted(top):
        print("  %9.1f Hz  excess %+6.2f dB  lock %.3f" % (f[j], excess[j], lock[j]))
    json.dump({"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": p,
               "step_hv": a.step_hv, "window_ms": n*DT*1e3,
               "resolution_hz": 1/(n*DT), "median_ratio": float(med),
               "n_candidates": int(cand.sum()),
               "candidates_hz": [float(x) for x in f[cand]],
               "max_excess_db": float(excess[band].max()),
               "max_excess_hz": float(f[band][np.argmax(excess[band])])},
              open(os.path.join(out, f"ring_{a.label}_summary.json"), "w"), indent=1)
    print(f"\n  saved ring_{a.label}_summary.json")


if __name__ == "__main__":
    main()
