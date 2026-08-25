#!/usr/bin/env python3
"""Measure the chain's transfer function from a multitone capture.

Reads the 64-shot sequence taken while a sysid_make waveform played, computes
the FRF at each tone bin as monitor/drive from the SCOPE's own channels (so
the AWG's and probes' flatness cancel out of the model band), with coherence
from the shot-to-shot scatter, and writes:

  * run/frf_<name>.csv   -- f, |H|, phase, coherence, and the current model
  * run/frf_<name>.png   -- the measured transfer against the model

    python tools\\sysid_fit.py --ref run\\sysid_SYSID2.npz \\
        --measured "C:\\...\\day 4\\sysid2_0*.csv" --drive-col CH1 --mon-col CH3
"""
from __future__ import annotations
import argparse, glob, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # repo root: eomilc and run/ live there
sys.path.insert(0, ROOT)
from eomilc import scope as scopeio
from eomilc.config import CHANNELS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True, help="run/sysid_<name>.npz from sysid_make")
    ap.add_argument("--measured", required=True, help="glob of the sequence CSVs")
    ap.add_argument("--drive-col", default="CH1")
    ap.add_argument("--mon-col", default="CH3")
    ap.add_argument("--channel", default=None, choices=[None, "EO1", "EO2"],
                    help="overlay this channel's model (default: guess from mon-col)")
    ap.add_argument("--amp", type=float, default=None,
                    help="monitor amplitude (V) at which to evaluate the model "
                        "overlay -- the resonance moves with drive level. "
                        "Default: a rough guess from the probe peak.")
    ap.add_argument("--t-offset", type=float, default=0.0)
    a = ap.parse_args()

    ref = np.load(a.ref)
    bins, dt = ref["bins"], float(ref["dt"])
    N = len(ref["u"])
    T = N * dt
    t_grid = np.arange(N) * dt

    files = sorted(glob.glob(a.measured))
    if not files:
        sys.exit(f"nothing matched {a.measured!r}")
    print(f"{len(files)} captures")
    Hs = []
    for f in files:
        tr = scopeio.load(f)
        u = scopeio.resample(tr.t, tr[a.drive_col], t_grid, t_offset=a.t_offset * 1e-6)
        y = scopeio.resample(tr.t, tr[a.mon_col], t_grid, t_offset=a.t_offset * 1e-6)
        U = np.fft.rfft(u - u.mean())
        Y = np.fft.rfft(y - y.mean())
        Hs.append(Y[bins] / U[bins])
    Hs = np.asarray(Hs)
    H = Hs.mean(axis=0)
    # coherence proxy: 1 - (shot scatter / mean magnitude)^2, clipped
    coh = np.clip(1 - (np.abs(Hs - H).std(axis=0) / np.abs(H)) ** 2, 0, 1)

    fHz = bins / T
    chname = a.channel or ("EO1" if a.mon_col == "CH3" else "EO2")
    ch = CHANNELS[chname]
    amp = a.amp if a.amp is not None else float(ref["peak"]) * ch.gain(1.0)
    wn = 2 * np.pi * ch.fn(amp)
    zt = ch.zeta(amp)
    w = 2j * np.pi * fHz
    Hm = ch.gain(amp) * wn ** 2 / (w ** 2 + 2 * zt * wn * w + wn ** 2)

    stem = os.path.splitext(os.path.basename(a.ref))[0].replace("sysid_", "")
    csv = os.path.join(ROOT, "run", f"frf_{stem}.csv")
    import pandas as pd
    pd.DataFrame({
        "f_Hz": fHz,
        "H_mag": np.abs(H),
        "H_phase_deg": np.degrees(np.angle(H)),
        "coherence": coh,
        "model_mag": np.abs(Hm),
        "model_phase_deg": np.degrees(np.angle(Hm)),
        "mag_ratio_true_over_model": np.abs(H) / np.abs(Hm),
    }).to_csv(csv, index=False, float_format="%.6g")
    print("wrote", csv)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    INK, INK2, SURF, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#e4e3df"
    BLUE, ORANGE = "#2a78d6", "#eb6834"
    fig, axes = plt.subplots(2, 1, figsize=(9.5, 7), sharex=True)
    fig.patch.set_facecolor(SURF)
    for ax in axes:
        ax.set_facecolor(SURF); ax.grid(True, color=GRID, lw=0.8)
        ax.set_axisbelow(True)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        ax.tick_params(colors=INK2, labelsize=9, length=0)
    good = coh > 0.9
    axes[0].loglog(fHz, np.abs(Hm), color=ORANGE, lw=1.8, ls=(0, (5, 2)))
    axes[0].loglog(fHz[good], np.abs(H[good]), color=BLUE, lw=0, marker="o", ms=4.5,
                   mec=SURF, mew=0.8)
    axes[0].loglog(fHz[~good], np.abs(H[~good]), color=BLUE, lw=0, marker="o", ms=4.5,
                   alpha=0.3)
    axes[0].set_ylabel("|monitor / drive|", color=INK2, fontsize=9.5)
    axes[0].annotate("measured (faded = coherence < 0.9)", (0.03, 0.14),
                     xycoords="axes fraction", color=BLUE, fontsize=9, fontweight="medium")
    axes[0].annotate("second-order model", (0.03, 0.06), xycoords="axes fraction",
                     color=ORANGE, fontsize=9, fontweight="medium")
    axes[1].semilogx(fHz, np.degrees(np.angle(Hm)), color=ORANGE, lw=1.8, ls=(0, (5, 2)))
    axes[1].semilogx(fHz[good], np.degrees(np.angle(H[good])), color=BLUE, lw=0,
                     marker="o", ms=4.5, mec=SURF, mew=0.8)
    axes[1].set_ylabel("phase (deg)", color=INK2, fontsize=9.5)
    axes[1].set_xlabel("frequency (Hz)", color=INK2, fontsize=9.5)
    fig.suptitle(f"Measured chain transfer, {stem} vs the {chname} model",
                 color=INK, fontsize=12.5, fontweight="semibold", x=0.06, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    png = os.path.join(ROOT, "run", f"frf_{stem}.png")
    fig.savefig(png, dpi=160, facecolor=SURF)
    print("wrote", png)
    print()
    for flo, fhi in ((2e3, 5e3), (5e3, 10e3), (10e3, 24e3)):
        m = (fHz >= flo) & (fHz < fhi) & good
        if m.any():
            r = (np.abs(H[m]) / np.abs(Hm[m])).mean()
            dp = (np.degrees(np.angle(H[m])) - np.degrees(np.angle(Hm[m]))).mean()
            print(f"  {flo/1e3:4.0f}-{fhi/1e3:4.0f} kHz: true/model magnitude "
                  f"{r:5.2f}x, phase error {dp:+6.1f} deg")


if __name__ == "__main__":
    main()
