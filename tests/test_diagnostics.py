#!/usr/bin/env python3
"""eomilc.diagnostics on synthetic shots: the code fold finds an injected
per-code pattern and reads ~1 sigma without one, the noise floor is the
standard error, the growth factor recovers a known rate, the DAC-step
fraction is right, and the sidecar round-trips. Bare interpreter, numpy only.

    python tests/test_diagnostics.py
"""
import os, sys, tempfile
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from eomilc import diagnostics as dg                              # noqa: E402

fails = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        fails.append(what)


def main():
    rng = np.random.default_rng(3)
    n, dt, n_sh = 4000, 2e-6, 32
    t = np.arange(n) * dt
    target = 2.5 * (1 - np.cos(np.pi * np.clip((t - 0.5e-3) / 5e-3, 0, 1)))   # 0 -> 5 V ramp
    code = dg.ADC_CODE_PER_VDIV * 1.0                                          # 1 V/div
    noise = 1.2e-3

    print("[1] a per-code pattern of 3 mV pk-pk is found many sigma up; without it the fold reads ~1 sigma")
    pattern = 1.5e-3 * np.sin(2 * np.pi * np.mod(target, code) / code)
    shots_p = target + pattern + rng.normal(0, noise, (n_sh, n))
    shots_c = target + rng.normal(0, noise, (n_sh, n))
    sp = dg.measurement_stats(target, shots_p, dt, vdiv=1.0)
    sc = dg.measurement_stats(target, shots_c, dt, vdiv=1.0)
    check(abs(sp["fold_amp"] - 3e-3) < 0.6e-3, f"pattern amplitude {sp['fold_amp']*1e3:.2f} mV (injected 3.0)")
    check(sp["fold_amp"] / sp["fold_sig"] > 8, f"pattern significance {sp['fold_amp']/sp['fold_sig']:.1f} sigma")
    check(sc["fold_amp"] / sc["fold_sig"] < 4, f"no pattern: {sc['fold_amp']/sc['fold_sig']:.1f} sigma")
    check(abs(sp["code_V"] - code) < 1e-12 and np.isnan(dg.measurement_stats(target, shots_c, dt)["code_V"]),
          "code known only with a V/div")

    print("[2] the noise floor is the standard error of the mean")
    expect = noise / np.sqrt(n_sh)
    check(abs(np.median(sc["sem_t"]) - expect) / expect < 0.1, f"sem_t median {np.median(sc['sem_t'])*1e3:.3f} mV vs {expect*1e3:.3f}")
    check(sc["floor_rms"] < 1.2 * expect, "floor_rms is the time-domain sem rms")
    bl = sc["bands_floor"]
    check(np.all(np.isfinite(bl)) and len(bl) == 4, "a floor per campaign band")
    check(sc["bands_rms"][3] < 3 * sc["bands_floor"][3], "a clean record's 30-60 kHz error sits near its floor")

    print("[3] growth factor from a known series")
    g, sg = dg.growth_factor([1, 1.02, 1.0404, 1.0612, 1.0824, 1.1041])
    check(abs(g - 1.02) < 1e-6, f"exact geometric 1.02 -> {g:.4f}")
    g2, sg2 = dg.growth_factor([5, 4, 3, 3.1, 2.9, 3.0, 3.1])
    check(abs(g2 - 1.0) < 0.05 and sg2 > 0, f"a plateau reads {g2:.3f} +- {sg2:.3f}")
    check(all(np.isnan(dg.growth_factor([1, 2]))), "two points: nan")

    print("[4] update against the DAC step")
    du = rng.normal(0, 0.5 * dg.AWG_LSB, 5000)
    r = dg.update_vs_step(du)
    check(abs(r["below"] - 0.954) < 0.03, f"a 0.5-step rms update is {r['below']*100:.0f}% under one step")
    check(abs(r["rms_steps"] - 0.5) < 0.05, "rms in steps")

    print("[5] lattice histogram")
    raw = np.round(rng.normal(0, 3e-3, 3000) / 2.5e-3) * 2.5e-3
    h = dg.lattice_hist(raw, 2.5e-3)
    check(h["n_distinct"] <= 12, f"undithered values on the lattice ({h['n_distinct']} distinct)")
    smooth = rng.normal(0, 3e-3, 3000)
    check(dg.lattice_hist(smooth, 2.5e-3)["n_distinct"] > 2000, "dithered values off it")

    print("[6] sidecar round trip")
    tmp = tempfile.mkdtemp(prefix="ilcdiag_")
    p = dg.stats_path(os.path.join(tmp, "meas_T_i03.npy"))
    check(p.endswith("meas_T_i03_stats.npz"), "sidecar name")
    dg.save_stats(p, sp)
    back = dg.load_stats(p)
    check(back is not None and back["n_shots"] == n_sh and abs(back["fold_amp"] - sp["fold_amp"]) < 1e-12
          and back["f"].shape == sp["f"].shape, "keys, scalars and arrays back")
    check(dg.load_stats(os.path.join(tmp, "nothing_stats.npz")) is None, "missing sidecar -> None")

    print(f"\n{len(fails)} failure(s)" + "".join(f"\n  {f}" for f in fails))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
