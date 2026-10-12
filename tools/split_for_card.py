#!/usr/bin/env python3
"""Cut a trained ILC drive pair into the experiment card's up/down files, at the trained rate.

    python split_for_card.py drive_S25X1_i15.csv drive_S25X2_i15.csv [--n 2500] [--settle-us 200]

Both channels are cut at the SAME times, around the stretch where both are flat (the hold
at the stop angle). For PAR ramps that is each channel's own flat top; for the series and
shared ramps (HSER, HS81, HSH6, SW*) one channel goes flat long before the other, and
cutting each at its own top would break the relative timing between X1 and X2 on the card.

The up files run from the start to --settle-us into the common flat stretch, the down files
from --settle-us before its end to the end. The hold on the atoms is then the card pause
plus 2 x settle. The default 200 us keeps the ILC's learned post-corner correction (it
settles within ~190 us) inside the files. Each part is resampled to --n points and written
as 'index,value' lines, like Ramps/20260924/drive_*_upramp.csv. The script prints the span
of each file: program exactly that ramp time on the card, for both channels, so the
drives play at the rate the ILC trained them at.
"""
import argparse, os
import numpy as np


def read(path):
    rows = []
    for line in open(path):
        f = line.strip().split(",")
        if line.startswith("#") or len(f) < 2:
            continue
        try:
            rows.append((float(f[0]), float(f[1])))
        except ValueError:          # the time_us,voltage_V header
            continue
    return np.array(rows).T


def flat_top(t, v, tol=0.002):
    idx = np.flatnonzero(v >= v.max() - tol * max(np.ptp(v), 1e-9))
    return t[idx[0]], t[idx[-1]]


def split_pair(drives, n=2500, settle_us=200.0, log=print, targets=None):
    """Cut drive files (X1, X2) at common times; returns what was written.

    `targets` (the matching target files) locate the hold. A trained drive is
    not flat across its hold to the 0.2 % the drive-only detection asks for --
    the learned correction settles there -- so pass the targets whenever you
    have them; the drives alone are the fallback."""
    data = [read(p) for p in drives]
    t = data[0][0]
    for tt, _ in data[1:]:
        if len(tt) != len(t) or np.abs(tt - t).max() > 1e-6:
            raise ValueError("the drives are not on one time grid")
    ref = [read(p) for p in targets] if targets else data
    for tt, _ in ref:
        if len(tt) != len(t) or np.abs(tt - t).max() > 1e-6:
            raise ValueError("the targets are not on the drives' time grid")
    tops = [flat_top(tt, vv) for tt, vv in ref]
    t_in, t_out = max(x[0] for x in tops), min(x[1] for x in tops)     # where every channel is flat
    if t_out - t_in < 2 * settle_us:
        raise ValueError(f"common flat stretch is only {t_out - t_in:.0f} us; lower settle_us")
    up = (t[0], t_in + settle_us)
    down = (t_out - settle_us, t[-1])
    log(f"  card: common flat stretch {t_in:.0f}..{t_out:.0f} us; hold on the atoms = card pause + {2 * settle_us:.0f} us")
    log(f"  card: program up files {(up[1] - up[0]) / 1e3:.4f} ms, down files {(down[1] - down[0]) / 1e3:.4f} ms, both channels")
    written = dict(up_ms=(up[1] - up[0]) / 1e3, down_ms=(down[1] - down[0]) / 1e3,
                   hold_extra_us=2 * settle_us, files=[])
    for path, (tt, vv) in zip(drives, data):
        stem = os.path.splitext(path)[0]
        for part, (lo, hi) in (("upramp", up), ("downramp", down)):
            ts = np.linspace(lo, hi, n)
            vs = np.interp(ts, tt, vv)
            out = f"{stem}_{part}.csv"
            with open(out, "w", newline="") as f:
                for i, x in enumerate(vs, 1):
                    f.write(f"{i},{x:.10g}\n")
            log(f"  card: {os.path.basename(out)}: {n} samples over {hi - lo:.1f} us, "
                f"first/last {vs[0]:.5f} / {vs[-1]:.5f} V")
            written["files"].append(out)
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("drives", nargs="+", help="X1 and X2 drive files (one file only for a single-channel test)")
    ap.add_argument("--n", type=int, default=2500, help="samples per card file")
    ap.add_argument("--settle-us", type=float, default=200.0, help="how far into the flat stretch each file runs")
    ap.add_argument("--targets", nargs="+", help="the matching target files, to locate the hold (recommended)")
    a = ap.parse_args()
    try:
        split_pair(a.drives, a.n, a.settle_us, targets=a.targets)
    except ValueError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
