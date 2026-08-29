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


def cosine_edge(n):
    """Raised-cosine 0 -> 1 over n samples, flat-tangent at both ends.

    A stand-in for the production arccos edge, which measures 0.08-0.22%
    overshoot; pass your own profile to `ramped_multitone` when you want the
    real one. What matters either way is that the edge is gentle enough that
    the chain has settled before the analysis window opens.
    """
    return 0.5 * (1.0 - np.cos(np.pi * np.linspace(0.0, 1.0, int(n))))


def hold_tone_bins(f_lo, f_hi, n_tones, n_hold, dt):
    """Integer FFT bins of the HOLD, not of the whole record.

    This is the invariant that makes the analysis leak-free, and confining the
    multitone to a hold is exactly what breaks it if you reuse `tone_bins`:
    those bins are integer over n*dt for the full record, and a tone that is
    not periodic over the analysis window leaks into every neighbour. The
    resolution you get is 1/(n_hold*dt) and no subtraction trick recovers
    more -- the hold is the only interval where the probe excites anything.
    """
    return tone_bins(f_lo, f_hi, n_tones, n=int(n_hold), dt=dt)


def ramped_multitone(peak, v_dc, bins, n_hold, dt=DT, n_edge=500,
                     n_settle=500, taper_s=150e-6, edge_profile=None,
                     awg_rail=10.0):
    """Ramp up to `v_dc`, hold a multitone there, ramp back down.

    The EOMs must not be parked at a standing kV bias, so the operating point
    is reached and left within the burst. That constraint is not a compromise:
    a bare offset multitone would leave the AWG holding `v_dc` on the crystals
    between bursts, which is precisely what the loop's +/-100 mV end clamp
    exists to prevent, so this is the only record shape that is compatible
    with the existing safety anyway.

    Layout, all in AWG volts:

        [n_edge]  0 -> v_dc          gentle edge, nothing to settle from
        [n_settle] flat at v_dc      >= 1 ms recommended before the window
        [n_hold]   v_dc + multitone  <- the analysis window
        [n_settle] flat at v_dc
        [n_edge]  v_dc -> 0

    Returns (u, (i0, i1)) with the record in AWG volts and the analysis window
    on the same index base. The multitone is tapered into and out of the hold
    so it joins the flat sections continuously; the taper leaks a little, but
    the fidelity test is the RATIO H_pd/H_mon measured at the same bins with
    the same window, and anything common to both divides straight out.
    """
    n_edge, n_settle, n_hold = int(n_edge), int(n_settle), int(n_hold)
    if peak <= 0 or n_hold < 16:
        raise ValueError("need a positive peak and a hold of at least 16 samples")
    if abs(v_dc) + peak > awg_rail:
        raise ValueError(f"v_dc {v_dc:g} V + peak {peak:g} V exceeds the "
                         f"{awg_rail:g} V AWG rail")

    edge = cosine_edge(n_edge) if edge_profile is None else \
        np.asarray(edge_profile, float) / np.max(np.abs(edge_profile))
    tone = multitone(peak, bins, taper_s=taper_s, n=n_hold, dt=dt)

    u = np.concatenate([v_dc * edge,
                        np.full(n_settle, v_dc),
                        v_dc + tone,
                        np.full(n_settle, v_dc),
                        v_dc * edge[::-1]])
    i0 = len(edge) + n_settle
    if abs(u[0]) > 0.1 or abs(u[-1]) > 0.1:
        raise ValueError("record must start and end within the +/-100 mV end "
                         "clamp -- check the edge profile")
    return u, (i0, i0 + n_hold)


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
