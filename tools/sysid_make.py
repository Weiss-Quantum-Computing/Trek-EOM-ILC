#!/usr/bin/env python3
"""Build a multitone system-ID waveform for the Trek/EOM chain.

Why: the ILC floor is a repeatable 3-6 kHz wiggle the loop cannot remove,
because the second-order model is wrong there (measured 24 Aug: the real chain
passes 4-8x the model above ~6 kHz, and the update stalls at |1-gLP|~1 in the
wiggle band). To correct that band the loop needs the TRUE transfer, measured.

This writes a Schroeder-phase multitone on the same 5301-point / 2 us grid the
ILC uses, playable through the exact same burst setup:

  * tones sit on integer bins of the 10.602 ms record -> leak-free FFT analysis
  * cosine tapers at both ends so the record starts and ends at zero
    (the AWG holds the first sample between bursts)
  * peak-limited to the requested amplitude; Schroeder phases keep the crest
    factor civilised so every tone gets decent energy

Run it at two or three amplitudes -- the resonance moves with drive level
(voltage-dependent EOM capacitance), so the model band of interest should be
identified near the amplitude the ramps actually use.

    python tools/sysid_make.py --peak 2.0 --name SYSID2
    python tools/sysid_make.py --peak 6.0 --name SYSID6

Then: upload via the AWG GUI (normalise OFF, AMP 20 Vpp), scope HRES full
window, take a 64-shot sequence (e.g. prefix sysid2), and run tools/sysid_fit.py.
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # repo root: eomilc and run/ live there
sys.path.insert(0, ROOT)
from eomilc import outputs

N = 5301                      # the ILC grid
DT = 2e-6
T = N * DT                    # 10.602 ms record, 94.32 Hz bin spacing


def tone_bins(f_lo=400.0, f_hi=24e3, n_tones=48, n=N, dt=DT):
    """Integer FFT bins, log-spaced, no duplicates. n/dt default to the
    ILC grid; the GUI passes its session's own record."""
    rec = n * dt
    want = np.geomspace(f_lo, f_hi, n_tones)
    k = sorted(set(int(round(f * rec)) for f in want))
    return np.array([b for b in k if 1 <= b <= n // 2 - 1])


def multitone(peak, bins, taper_s=150e-6, seed=0, n=N, dt=DT):
    rec = n * dt
    t = np.arange(n) * dt
    u = np.zeros(n)
    for i, k in enumerate(bins):
        phase = -np.pi * i * (i + 1) / len(bins)      # Schroeder
        u += np.cos(2 * np.pi * k / rec * t + phase)
    u *= peak / np.abs(u).max()
    nt = int(taper_s / dt)
    w = np.ones(n)
    ramp = 0.5 * (1 - np.cos(np.pi * np.arange(nt) / nt))
    w[:nt] = ramp
    w[-nt:] = ramp[::-1]
    return u * w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--peak", type=float, default=2.0,
                    help="peak volts at the AWG (2 V -> ~1.1 kV peak at the EOM)")
    ap.add_argument("--name", default=None,
                    help="waveform name, <= 11 chars (default SYSID<peak>)")
    ap.add_argument("--f-lo", type=float, default=400.0)
    ap.add_argument("--f-hi", type=float, default=24e3)
    ap.add_argument("--tones", type=int, default=48)
    ap.add_argument("--awg-dir",
                    default=os.environ.get(
                        "BK4063B_WAVEFORMS",
                        os.path.join(os.path.dirname(ROOT),
                                     "BK4063B-AWG-GUI", "Waveforms")))
    a = ap.parse_args()

    name = a.name or f"SYSID{a.peak:g}".replace(".", "p")[:11]
    bins = tone_bins(a.f_lo, a.f_hi, a.tones)
    u = multitone(a.peak, bins)

    out = os.path.join(a.awg_dir, f"{name}.csv")
    outputs.write_bk_waveform(out, u, name, full_scale=10.0)
    np.savez(os.path.join(ROOT, "run", f"sysid_{name}.npz"),
             u=u, bins=bins, dt=DT, peak=a.peak)
    print(f"{out}")
    print(f"  {len(bins)} tones, {bins[0]/T:.0f} Hz to {bins[-1]/T:.0f} Hz "
          f"on integer bins of the {T*1e3:.3f} ms record")
    print(f"  peak {np.abs(u).max():.3f} V at the AWG "
          f"(~{np.abs(u).max()*0.56*1000:.0f} V at the EOM), "
          f"rms {u.std():.3f} V, ends {u[0]:+.1e}/{u[-1]:+.1e}")
    print(f"  reference saved to run\\sysid_{name}.npz")
    print(f"\nupload '{name}' with normalise OFF, then capture a 64-shot HRES "
          f"sequence and run:\n  python tools\\sysid_fit.py --ref run\\sysid_{name}.npz "
          f"--measured \"<seq glob>\" --drive-col CH1 --mon-col CH3")


if __name__ == "__main__":
    main()
