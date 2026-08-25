#!/usr/bin/env python3
"""Validate the ILC loop against a plant the model does NOT perfectly describe.

The truth here is the SECOND-ORDER plant measured on 2026-08-21 -- perturbed in
gain, fn and zeta by more than the sweep's own scatter -- driven through the
measured scope quantisation and noise.  If the loop converges under these
conditions it will converge on the bench.

    python simulation/simulate.py --target target_MKJ_EO1.csv

Note what the model comparison shows: the one-pole model, which is what this
package used before 2026-08-24, does not converge.  It bottoms out around
iteration 3 and then climbs, because at fn the true plant has a Q = 2.4 peak the
one-pole lead knows nothing about and the ILC contraction factor goes past 1.
"""
from __future__ import annotations
import argparse, sys, os
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for eomilc
from eomilc.plant import Plant
from eomilc.ilc import Loop
from eomilc.config import CHANNELS, HV_PER_MON


def load_target(path):
    df = pd.read_csv(path, comment="#")
    t = df.iloc[:, 0].to_numpy(float)
    t = t * 1e-6 if "us" in df.columns[0].lower() else t
    return t, df.iloc[:, 1].to_numpy(float) / HV_PER_MON


def truth_for(ch, amp, dt, gain_err, fn_err, zeta_err):
    return Plant(gain=ch.gain(amp) * (1 + gain_err), dt=dt,
                 fn=ch.fn(amp) * (1 + fn_err), zeta=ch.zeta(amp) * (1 + zeta_err))


def run(target, ch_name="EO1", model="resonant", n_iter=6, n_avg=256,
        gain_err=0.01, fn_err=0.06, zeta_err=0.10,
        noise_rms=14.66e-3, lsb=31.25e-3, gamma=0.6, seed=0, quiet=False):
    rng = np.random.default_rng(seed)
    t, v = load_target(target)
    dt = float(np.median(np.diff(t)))
    ch = CHANNELS[ch_name]
    amp = float(np.ptp(v))

    true = truth_for(ch, amp, dt, gain_err, fn_err, zeta_err)
    loop = Loop(plant=ch.plant(amp, dt, model=model), target=v, dt=dt,
                channel=ch, gamma=gamma, f_cut=20e3)

    def measure(u):
        y = true.forward(u)
        n = rng.normal(0, noise_rms, (n_avg, len(y))).mean(axis=0)
        q = rng.uniform(-lsb / 2, lsb / 2, (n_avg, len(y))).mean(axis=0)
        return y + n + q

    u = loop.first_shot()
    rows = []
    for k in range(n_iter + 1):
        y = measure(u)
        m = loop.metrics(y)
        rows.append((k, m["peak_err_hv"], m["rms_err_hv"], m["peak_pct"], float(np.abs(u).max())))
        if k < n_iter:
            u = loop.update(u, y)

    if not quiet:
        print(f"channel {ch_name}, {model} model")
        print(f"  model : {loop.plant}")
        print(f"  TRUTH : {true}")
        print(f"  errors: gain {gain_err*100:+.0f}%, fn {fn_err*100:+.0f}%, zeta {zeta_err*100:+.0f}%")
        print(f"  {n_avg} averages of {noise_rms*1e3:.1f} mV rms + a {lsb*1e3:.0f} mV LSB "
              f"-> floor {lsb/np.sqrt(12*n_avg)*HV_PER_MON:.1f} V rms\n")
        print(f"{'iter':>4} {'peak err':>11} {'rms err':>10} {'peak %':>8} {'AWG peak':>10}")
        y0 = true.forward(v / loop.plant.gain)
        print(f"{'raw':>4} {np.abs(y0-v).max()*HV_PER_MON:8.1f} V "
              f"{np.abs(y0-v).std()*HV_PER_MON:7.2f} V "
              f"{100*np.abs(y0-v).max()/np.ptp(v):7.2f}% {np.abs(v/loop.plant.gain).max():9.3f} V")
        for k, pk, rms, pct, up in rows:
            print(f"{k:>4} {pk:8.1f} V {rms:7.2f} V {pct:7.2f}% {up:9.3f} V")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, help="target waveform CSV, volts at the EOM")
    ap.add_argument("--iterations", type=int, default=6)
    a = ap.parse_args()

    for ch in ("EO1", "EO2"):
        run(a.target, ch, n_iter=a.iterations); print()

    print("=== why the model form matters: final peak error (V at the EOM) ===")
    print(f"{'':22s} " + " ".join(f"{'i'+str(k):>7s}" for k in range(a.iterations + 1)))
    for model in ("resonant", "one_pole"):
        for gam in (0.3, 0.6):
            rows = run(a.target, "EO1", model=model, n_iter=a.iterations,
                       gamma=gam, quiet=True)
            errs = [r[1] for r in rows]
            note = "" if errs[-1] <= min(errs) * 1.1 else "  <-- diverging"
            print(f"{model+f'  gamma={gam}':22s} "
                  + " ".join(f"{e:7.1f}" for e in errs) + note)

    print("\n=== robustness of the resonant model to fn error ===")
    print(f"{'fn error':>10} " + " ".join(f"{'g='+str(g):>9}" for g in (0.4, 0.6, 0.8)))
    for fe in (-0.20, -0.10, 0.0, 0.10, 0.20):
        out = []
        for gam in (0.4, 0.6, 0.8):
            rows = run(a.target, "EO1", n_iter=a.iterations, fn_err=fe,
                       gamma=gam, seed=1, quiet=True)
            out.append(f"{rows[-1][1]:9.1f}")
        print(f"{fe*100:+9.0f}% " + " ".join(out))


if __name__ == "__main__":
    main()
