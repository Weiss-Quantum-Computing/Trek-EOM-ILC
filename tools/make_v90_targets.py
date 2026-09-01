#!/usr/bin/env python3
"""Rescale the 5200 V ramp targets to each channel's measured 90-degree point.

The four targets in waveforms\\ were built to 5200 V per EOM. The optical
calibration of 31 Aug - 1 Sep 2026 put the 90-degree rotation point at
5128.3 V (X1) and 5137.4 V (X2) at the Trek monitor (`Channel.v90_hv` in
eomilc/config.py), so the 5200 V peak overshoots the rotation by about 2
degrees. This writes `target_<name>_V90.csv` beside each original: the same
shape, every sample multiplied by v90_hv / (the file's own peak), on the same
2 us grid, with the provenance in the header.

Only the peak is changed. The record still starts and ends at 0 V, which is
|eo_zero_hv| (20.7 V on X1, 8.7 V on X2) away from the EO zero; moving the
start is a separate decision, so it is written into the header and not
applied. The split (SER) pair no longer sums to a single integrated stroke
sample for sample once each channel is scaled to its own V90 -- that is the
point: each channel reaches its own 90 degrees.

    python tools\\make_v90_targets.py            # all four
    python tools\\make_v90_targets.py PARX1      # one
"""
from __future__ import annotations
import os, sys, datetime
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eomilc.config import CHANNELS

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAVEFORMS = os.path.join(HERE, "waveforms")
TARGETS = {"PARX1": "EO1", "PARX2": "EO2", "SERX1": "EO1", "SERX2": "EO2"}


def rescale(name: str, chname: str) -> str:
    ch = CHANNELS[chname]
    src = os.path.join(WAVEFORMS, f"target_{name}.csv")
    out = os.path.join(WAVEFORMS, f"target_{name}_V90.csv")
    df = pd.read_csv(src, comment="#")
    t = df.iloc[:, 0].to_numpy(float)
    v = df.iloc[:, 1].to_numpy(float)
    peak = float(v.max())
    factor = ch.v90_hv / peak
    v90 = v * factor
    with open(src) as f:
        src_comments = [l.rstrip("\n") for l in f if l.startswith("#")]
    with open(out, "w", newline="") as f:
        f.write(f"# {name} rescaled to the measured 90-degree point of {ch.name}: "
                f"peak {peak:.3f} V -> {ch.v90_hv:.1f} V (x {factor:.6f}), "
                f"{datetime.date.today().isoformat()}, tools/make_v90_targets.py\n")
        f.write(f"# V90 at the monitor {ch.v90_hv:.1f} V = {ch.v90_hv/ch.cmd_hv_gain_meas/1e3:.3f} V "
                f"commanded (measured command->HV gain {ch.cmd_hv_gain_meas:.4f}); "
                f"EO zero at {ch.eo_zero_hv:+.1f} V, NOT applied -- the record still "
                f"starts/ends at 0 V; rise/fall hysteresis {ch.hysteresis_pct:.3g} %\n")
        f.write(f"# source {os.path.basename(src)}, {len(v)} pts at "
                f"{np.median(np.diff(t)):.4f} us, header of the source follows\n")
        for line in src_comments:
            f.write("#   " + line.lstrip("#").strip() + "\n")
        f.write("time_us,voltage_V\n")
        np.savetxt(f, np.column_stack([t, v90]), delimiter=",", fmt="%.6f")
    print(f"{out}: peak {peak:.3f} -> {v90.max():.3f} V, first/last "
          f"{v90[0]:.3f}/{v90[-1]:.3f} V, {len(v90)} pts")
    return out


def main(argv):
    names = argv or list(TARGETS)
    for name in names:
        if name not in TARGETS:
            sys.exit(f"unknown target {name!r}; one of {', '.join(TARGETS)}")
        rescale(name, TARGETS[name])


if __name__ == "__main__":
    main(sys.argv[1:])
