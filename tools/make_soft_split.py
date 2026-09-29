#!/usr/bin/env python3
"""Split a total polarization-rotation ramp between the two EOMs with a soft handover.

The SER targets hand the stroke from X1 to X2 at a hard corner at 90 degrees:
X1 stops dead and X2 starts at full speed at the one angle where the atom
moves fastest per degree (dx/dtheta peaks at 90 degrees, 91 nm/deg for F=3).
This builds the pair from the same total rotation theta(t), but shares each
increment of rotation between the channels with a weight w(theta):

    theta_X1(theta) = integral_0^theta w(t') dt'     theta_X2 = theta - theta_X1

w is `--w-low` below a blend window and 0 above it, joined by a symmetric C2
smoothstep. For a symmetric step the integral of w is w_low x (the window
centre), so the window is centred on --x1-deg / --w-low and X1 ends at exactly
--x1-deg:

    --x1-deg 90 --blend-deg 0                 the present hard SER split
    --x1-deg 99 --blend-deg 20                X1 to 99 deg (110 % of 90), X2
                                              the remaining 81; X2 starts at
                                              89 deg, X1 finishes at 109 deg
    --x1-deg 81 --blend-deg 20                the mirror: same sensitivity at
                                              the handover, fits today's X1 rail
    --x1-deg 90 --w-low 0.5 --blend-deg 0     PAR (both channels to 90 deg)

w depends on theta, not on time, so the down leg is automatically the mirror
of the up leg (X2 returns first) whatever the time profile, and the two
channels' angles always sum to the source's total, sample for sample.

The source is the total rotation of a target pair that already sums to 180
degrees -- by default the PAR V90 pair, whose stroke is also the one the SER
pair splits -- or one CSV (time_us, voltage_V) scaled by --v-pi, e.g. a
single-EOM Transport-Optimizer ramp:

    python tools/make_soft_split.py --x1-deg 81 --blend-deg 20 --stem SFT81X
    python tools/make_soft_split.py --total K06_5ms_capon_polyband.csv --v-pi 5400 ...

Each channel is scaled by its own measured 90-degree point (Channel.v90_hv),
like tools/make_v90_targets.py, and checked against the rail (10 V at the AWG
x cmd_hv_gain_meas). The EO zero is not applied: the records start and end at
0 V, as the V90 targets do.
"""
from __future__ import annotations
import argparse, datetime, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eomilc.config import CHANNELS

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAVEFORMS = os.path.join(HERE, "waveforms")


def read_target(path):
    df = pd.read_csv(path, comment="#")
    return df.iloc[:, 0].to_numpy(float), df.iloc[:, 1].to_numpy(float)


def smoothstep(u):
    """0 -> 1 with zero first and second derivative at both ends"""
    u = np.clip(u, 0.0, 1.0)
    return u ** 3 * (10 - 15 * u + 6 * u ** 2)


def x1_share(theta, x1_deg, blend_deg, w_low):
    """w(theta): the fraction of each increment of rotation that X1 takes"""
    centre = x1_deg / w_low
    if blend_deg <= 0:
        return np.where(theta < centre, w_low, 0.0)
    return w_low * (1 - smoothstep((theta - (centre - blend_deg / 2)) / blend_deg))


def split(theta, x1_deg, blend_deg, w_low, grid=1e-3):
    g = np.arange(0.0, 180.0 + grid, grid)
    w = x1_share(g, x1_deg, blend_deg, w_low)
    th1_g = np.concatenate([[0.0], np.cumsum(0.5 * (w[1:] + w[:-1]) * grid)])
    th1 = np.interp(theta, g, th1_g)
    return th1, theta - th1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--x1-deg", type=float, required=True,
                    help="rotation X1 ends at (X2 takes 180 minus this)")
    ap.add_argument("--blend-deg", type=float, default=20.0,
                    help="width of the handover window in total rotation (0 = hard corner)")
    ap.add_argument("--w-low", type=float, default=1.0,
                    help="X1's share of the rotation below the window (1 = X1 alone, 0.5 = shared)")
    ap.add_argument("--total", help="single source CSV (time_us, voltage_V); default: the PAR V90 pair")
    ap.add_argument("--v-pi", type=float, help="--total: volts of the source that mean 180 degrees")
    ap.add_argument("--stem", default=None, help="output names target_<stem>1.csv / target_<stem>2.csv")
    ap.add_argument("--out-dir", default=WAVEFORMS)
    ap.add_argument("--full-scale", type=float, default=10.0, help="AWG volts at full scale")
    a = ap.parse_args()

    if not 0 < a.w_low <= 1:
        sys.exit("--w-low must be in (0, 1]")
    centre = a.x1_deg / a.w_low
    if centre - a.blend_deg / 2 < 0 or centre + a.blend_deg / 2 > 180:
        sys.exit(f"the window {centre - a.blend_deg / 2:.1f}..{centre + a.blend_deg / 2:.1f} deg "
                 f"does not fit inside 0..180 deg")

    ch1, ch2 = CHANNELS["EO1"], CHANNELS["EO2"]
    if a.total:
        if not a.v_pi:
            sys.exit("--total needs --v-pi")
        t, v = read_target(a.total)
        theta = 180.0 * v / a.v_pi
        source = f"{os.path.basename(a.total)} scaled by 180 deg / {a.v_pi:g} V"
    else:
        t, v1 = read_target(os.path.join(WAVEFORMS, "target_PARX1_V90.csv"))
        t2, v2 = read_target(os.path.join(WAVEFORMS, "target_PARX2_V90.csv"))
        if len(t2) != len(t) or np.abs(t2 - t).max() > 1e-6:
            sys.exit("the PAR pair is not on one grid")
        theta = 90.0 * v1 / ch1.v90_hv + 90.0 * v2 / ch2.v90_hv
        source = "total rotation of target_PARX1_V90 + target_PARX2_V90"
    step = float(np.median(np.diff(t)))

    th1, th2 = split(theta, a.x1_deg, a.blend_deg, a.w_low)
    out_v = {ch1.name: ch1.v90_hv * th1 / 90.0, ch2.name: ch2.v90_hv * th2 / 90.0}
    stem = a.stem or f"SFT{a.x1_deg:g}B{a.blend_deg:g}X".replace(".", "p")

    lo, hi = centre - a.blend_deg / 2, centre + a.blend_deg / 2
    up = np.arange(len(t)) <= int(np.argmax(theta))
    t_lo = t[up][np.argmax(theta[up] >= lo)]
    t_hi = t[up][np.argmax(theta[up] >= hi)]
    print(f"source : {source}, {len(t)} pts at {step:g} us, peak {theta.max():.3f} deg")
    print(f"split  : X1 takes {100 * a.w_low:g} % of the rotation up to {lo:.1f} deg, "
          f"blends to 0 by {hi:.1f} deg (up leg {t_lo:.0f}..{t_hi:.0f} us); "
          f"X1 ends at {th1.max():.3f} deg, X2 at {th2.max():.3f} deg")

    stamp = datetime.date.today().isoformat()
    for k, ch in ((1, ch1), (2, ch2)):
        v = out_v[ch.name]
        ceiling = a.full_scale * ch.cmd_hv_gain_meas * 1e3
        slew = np.abs(np.diff(v)).max() / step
        head = 100 * (1 - v.max() / ceiling)
        print(f"  {ch.name}: peak {v.max():.1f} V ({90 * v.max() / ch.v90_hv:.2f} deg), ceiling "
              f"{ceiling:.0f} V ({head:+.1f} % headroom), peak slew {slew:.2f} V/us")
        if v.max() > ceiling:
            print(f"  *** {ch.name} needs {v.max():.0f} V, past its {ceiling:.0f} V ceiling: "
                  f"raise its divider or move the handover ***")
        path = os.path.join(a.out_dir, f"target_{stem}{k}.csv")
        with open(path, "w", newline="") as f:
            f.write(f"# soft-handover split target for {ch.name}, {stamp}, tools/make_soft_split.py\n")
            f.write(f"# {source}\n")
            f.write(f"# X1 ends at {a.x1_deg:g} deg, blend window {lo:.2f}..{hi:.2f} deg of total "
                    f"rotation, X1 share below it {a.w_low:g} (C2 smoothstep)\n")
            f.write(f"# scaled to this channel's V90 {ch.v90_hv:.1f} V; EO zero {ch.eo_zero_hv:+.1f} V "
                    f"NOT applied; X1 + X2 angles sum to the source sample for sample\n")
            f.write(f"# peak {v.max():.3f} V, peak slew {slew:.3f} V/us, "
                    f"{len(t)} pts at {step:g} us = {len(t) * step / 1e3:.4f} ms period\n")
            f.write("time_us,voltage_V\n")
            np.savetxt(f, np.column_stack([t, v]), delimiter=",", fmt="%.6f")
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()
