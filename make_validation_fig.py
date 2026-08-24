#!/usr/bin/env python3
"""Regenerate ilc_validation.png -- why the plant model form decides convergence.

    python make_validation_fig.py --target waveforms/target_MKJX1.csv

Four panels, all driven through the shipping Loop rather than a reimplementation
of it: convergence, the contraction factor that explains it, the plant response
the one-pole fit misses, and robustness to getting fn wrong.
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import bilinear, lfilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eomilc import ilc
from eomilc.config import CHANNELS, HV_PER_MON

# Reference categorical palette, fixed slot order, light mode.  Two of these
# slots sit under 3:1 on a light surface, so every series carries a visible
# direct label and identity never rests on colour alone.
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
INK, INK2, INK3 = "#0b0b0b", "#52514e", "#8a8985"
SURFACE, GRID = "#fcfcfb", "#e4e3df"

LSB = 1.0 * 8 / 256          # 1 V/div, 8 bit, 8 divisions
NAVG = 256


def styled(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    return ax


def label(ax, x, y, text, dx=7, dy=0, size=8.5):
    ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points",
                color=INK, fontsize=size, va="center", fontweight="medium")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="waveforms/target_MKJX1.csv")
    ap.add_argument("--channel", default="EO1")
    ap.add_argument("--iterations", type=int, default=6)
    ap.add_argument("--out", default="ilc_validation.png")
    a = ap.parse_args()

    df = pd.read_csv(a.target, comment="#")
    t = df.iloc[:, 0].to_numpy(float) * 1e-6
    v = df.iloc[:, 1].to_numpy(float) / HV_PER_MON
    dt = float(np.median(np.diff(t)))
    ch = CHANNELS[a.channel]
    amp = float(np.ptp(v))

    # Truth: the measured plant, perturbed by more than the sweep's own scatter.
    G_ERR, FN_ERR, Z_ERR = 0.01, 0.06, 0.10
    gain_t = ch.gain(amp) * (1 + G_ERR)
    fn_t, z_t = ch.fn(amp) * (1 + FN_ERR), ch.zeta(amp) * (1 + Z_ERR)

    # These panels plot PEAK error, so the rms noise floor is the wrong line to
    # draw on them -- the peak of N Gaussian samples runs well above sigma.
    # E[max|x|] ~ sigma*sqrt(2*ln(2N)) is the number a converged trace sits at.
    floor_rms = LSB / np.sqrt(12 * NAVG) * HV_PER_MON
    floor = floor_rms * np.sqrt(2 * np.log(2 * v.size))

    def run(model, gamma, seed=0, fn_err=FN_ERR):
        rng = np.random.default_rng(seed)
        wn = 2 * np.pi * ch.fn(amp) * (1 + fn_err)
        bb, ab = bilinear([wn ** 2], [1, 2 * z_t * wn, wn ** 2], fs=1 / dt)

        def meas(u):
            return gain_t * lfilter(bb, ab, u) + rng.normal(
                0, LSB / np.sqrt(12 * NAVG), u.size)

        loop = ilc.Loop(plant=ch.plant(amp, dt, model=model), target=v, dt=dt,
                        channel=ch, gamma=gamma, f_cut=20e3)
        u = loop.first_shot()
        y = meas(u)
        e = [np.abs(v - y).max() * HV_PER_MON]
        for _ in range(a.iterations):
            u = loop.update(u, y)
            y = meas(u)
            e.append(np.abs(v - y).max() * HV_PER_MON)
        return np.array(e)

    fig, axes = plt.subplots(2, 2, figsize=(11.5, 8.2))
    fig.patch.set_facecolor(SURFACE)
    k = np.arange(a.iterations + 1)

    # ---------------------------------------------------------------- A
    ax = styled(axes[0, 0])
    for lab, model, g, col in (("resonant  g=0.6", "resonant", 0.6, BLUE),
                               ("resonant  g=0.3", "resonant", 0.3, ORANGE),
                               ("one-pole  g=0.6", "one_pole", 0.6, AQUA),
                               ("one-pole  g=0.3", "one_pole", 0.3, YELLOW)):
        e = run(model, g)
        ax.plot(k, e, color=col, lw=2.0, marker="o", ms=4.5, zorder=3,
                mec=SURFACE, mew=1.2)
        label(ax, k[-1], e[-1], lab)
    ax.axhline(floor, color=INK3, lw=1.2, ls=(0, (4, 3)), zorder=2)
    ax.annotate(f"measurement floor: {floor:.1f} V peak "
                f"({floor_rms:.2f} V rms x {NAVG} averages)", (0, floor),
                xytext=(2, -11), textcoords="offset points", color=INK2,
                fontsize=8, va="top")
    ax.set_yscale("log")
    ax.set_xlim(-0.25, a.iterations + 2.9)
    ax.set_ylim(floor * 0.55, None)
    ax.set_xlabel("iteration", color=INK2, fontsize=9.5)
    ax.set_ylabel("peak error at the EOM  (V)", color=INK2, fontsize=9.5)
    ax.set_title("The one-pole model turns back up", color=INK, fontsize=11.5,
                 fontweight="semibold", loc="left", pad=10)

    # ---------------------------------------------------------------- B
    ax = styled(axes[0, 1])
    f = np.logspace(2, 4.3, 500)
    w = 2j * np.pi * f
    wn_t = 2 * np.pi * fn_t
    P = wn_t ** 2 / (w ** 2 + 2 * z_t * wn_t * w + wn_t ** 2)
    wn_m, z_m = 2 * np.pi * ch.fn(amp), ch.zeta(amp)
    inv_res = (w ** 2 + 2 * z_m * wn_m * w + wn_m ** 2) / wn_m ** 2
    inv_1p = 1 + ch.tau(amp) * w
    for lab, inv, col, dy in (("one-pole", inv_1p, AQUA, 8),
                              ("resonant", inv_res, BLUE, -10)):
        y = np.abs(1 - 0.6 * inv * P)
        ax.plot(f, y, color=col, lw=2.0, zorder=3)
        j = np.argmin(np.abs(f - 7000))
        label(ax, f[j], y[j], lab, dx=6, dy=dy)
    ax.axhline(1.0, color=INK3, lw=1.2, ls=(0, (4, 3)), zorder=2)
    ax.annotate("diverges above 1", (115, 1.0), xytext=(0, 6),
                textcoords="offset points", color=INK2, fontsize=8)
    ax.axvline(fn_t, color=INK3, lw=1.0, ls=":", zorder=1)
    ax.annotate(f"$f_n$ {fn_t:.0f} Hz", (fn_t, 0.055), xytext=(5, 0),
                textcoords="offset points", color=INK2, fontsize=8)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_ylim(0.04, 5)
    ax.set_xlabel("frequency (Hz)", color=INK2, fontsize=9.5)
    ax.set_ylabel(r"$|\,1-\gamma\,P^{-1}_{model}P_{true}\,|$", color=INK2, fontsize=9.5)
    ax.set_title("Why: the loop amplifies at the resonance", color=INK,
                 fontsize=11.5, fontweight="semibold", loc="left", pad=10)

    # ---------------------------------------------------------------- C
    ax = styled(axes[1, 0])
    one = 1 / (1 + ch.tau(amp) * w)
    j = np.argmin(np.abs(f - 1150))
    for lab, mag, col, dy in ((f"true plant, Q = {1/(2*z_t):.2f}", np.abs(P), BLUE, 13),
                              ("one-pole model", np.abs(one), AQUA, -14)):
        db = 20 * np.log10(mag)
        ax.plot(f, db, color=col, lw=2.0, zorder=3)
        label(ax, f[j], db[j], lab, dx=-6, dy=dy)
    ax.axvline(fn_t, color=INK3, lw=1.0, ls=":", zorder=1)
    ax.annotate("both fits agree on the low-frequency lag;\n"
                "only one knows about the peak", (120, -34),
                color=INK2, fontsize=8, va="top")
    ax.set_xscale("log")
    ax.set_ylim(-42, 14)
    ax.set_xlabel("frequency (Hz)", color=INK2, fontsize=9.5)
    ax.set_ylabel("gain (dB)", color=INK2, fontsize=9.5)
    ax.set_title("What the one-pole fit misses", color=INK, fontsize=11.5,
                 fontweight="semibold", loc="left", pad=10)

    # ---------------------------------------------------------------- D
    ax = styled(axes[1, 1])
    errs = np.array([-0.20, -0.10, 0.0, 0.10, 0.20])
    for lab, g, col, dy in (("g=0.4", 0.4, ORANGE, 0), ("g=0.6", 0.6, BLUE, 13),
                            ("g=0.8", 0.8, AQUA, -13)):
        fin = [run("resonant", g, seed=1, fn_err=e)[-1] for e in errs]
        ax.plot(errs * 100, fin, color=col, lw=2.0, marker="o", ms=5,
                mec=SURFACE, mew=1.2, zorder=3)
        label(ax, errs[-1] * 100, fin[-1], lab, dy=dy)
    ax.axhline(floor, color=INK3, lw=1.2, ls=(0, (4, 3)), zorder=2)
    ax.annotate("measurement floor", (-23, floor), xytext=(0, -11),
                textcoords="offset points", color=INK2, fontsize=8, va="top")
    ax.annotate("0.6 and 0.8 sit on top of each other:\n"
                "above 0.4 the learning gain stops mattering",
                (-23, 3.6), color=INK2, fontsize=8, va="top")
    ax.set_ylim(0, None)
    ax.set_xlim(-24, 31)
    ax.set_xlabel("error in the modelled $f_n$  (%)", color=INK2, fontsize=9.5)
    ax.set_ylabel(f"peak error after {a.iterations} iterations  (V)",
                  color=INK2, fontsize=9.5)
    ax.set_title("Getting $f_n$ wrong barely matters", color=INK, fontsize=11.5,
                 fontweight="semibold", loc="left", pad=10)

    fig.suptitle("Trek 610E + EOM  -  ILC convergence depends on the plant model form",
                 color=INK, fontsize=13, fontweight="semibold", x=0.052, ha="left",
                 y=0.983)
    fig.text(0.052, 0.936,
             f"{ch.name}, {np.ptp(v)*HV_PER_MON:.0f} V target on a {dt*1e6:.0f} us grid.  "
             f"Truth = the measured plant with +{G_ERR*100:.0f}% gain, "
             f"+{FN_ERR*100:.0f}% $f_n$, +{Z_ERR*100:.0f}% zeta, "
             f"seen through {NAVG}-average 8-bit capture.",
             color=INK2, fontsize=9)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.922))
    fig.savefig(a.out, dpi=160, facecolor=SURFACE)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
