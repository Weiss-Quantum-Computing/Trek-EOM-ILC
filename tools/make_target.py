#!/usr/bin/env python3
"""Build an ILC target from the MKJ waveform the AWG actually plays.

`MKJ_full.csv` is the AWG GUI's cached form: 106020 unipolar samples in 0..1,
played back over a 10.602 ms period, i.e. a 0.1 us grid.  ILC wants volts at the
EOM on the grid the loop will run on, so this decimates with a boxcar and scales
by the measured AWG->monitor gain for the channel.

    python tools/make_target.py --channel EO1 --peak-hv 5200 --step 2 --out target_X1.csv

The target is expressed in volts AT THE EOM, which is `HV_PER_MON` times the
monitor reading.  That is the quantity the loop controls, and it is what makes
two channels with different Trek gains directly comparable: ask both for 5200 V
and each ends up with whatever drive its own chain needs.
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for eomilc
from eomilc.config import CHANNELS, HV_PER_MON

DEFAULT_SRC = os.environ.get(
    "MKJ_FULL",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))),          # the folder holding the repos
        "BK4063B-AWG-GUI", "Waveforms", "MKJ_full.csv"))
SRC_PERIOD = 0.010602        # s, from C1:BSWV PERI -- the played length


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=DEFAULT_SRC, help="normalised source waveform")
    ap.add_argument("--period", type=float, default=SRC_PERIOD,
                    help="played length of the source record, seconds")
    ap.add_argument("--channel", required=True, choices=list(CHANNELS))
    ap.add_argument("--peak-hv", type=float, required=True, help="peak volts at the EOM")
    ap.add_argument("--step", type=float, default=2.0, help="output grid, microseconds")
    ap.add_argument("--full-scale", type=float, default=10.0,
                    help="AWG volts at DAC full scale (AMP = 2x this, OFST 0)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    y = np.loadtxt(a.src, comments="#").ravel()
    src_dt = a.period / y.size
    dec = a.step * 1e-6 / src_dt
    if abs(dec - round(dec)) > 1e-9:
        sys.exit(f"a {a.step} us step is {dec:.4f} source samples, not a whole "
                 f"number -- the source grid is {src_dt*1e6:.4f} us")
    dec = int(round(dec))
    n = (y.size // dec) * dec
    if n != y.size:
        print(f"note: dropping {y.size - n} trailing sample(s) so {dec}:1 divides evenly")
    y = y[:n].reshape(-1, dec).mean(axis=1)          # boxcar decimate

    dt = a.period / y.size
    t_us = np.arange(y.size) * dt * 1e6
    span = float(np.ptp(y))
    hv = (y - y.min()) / span * a.peak_hv            # 0 .. peak_hv at the EOM

    ch = CHANNELS[a.channel]
    amp_mon = a.peak_hv / HV_PER_MON
    gain = ch.gain(amp_mon)
    drive_peak = amp_mon / gain
    head = 100 * (1 - drive_peak / a.full_scale)

    with open(a.out, "w", newline="") as f:
        f.write(f"# target for {ch.name} from {os.path.basename(a.src)}, "
                f"{dec}:1 boxcar -> {y.size} pts at {dt*1e6:.4f} us\n")
        f.write(f"# peak {a.peak_hv:.0f} V at the EOM; chain gain {gain:.4f} "
                f"(AWG->monitor) implies a {drive_peak:.3f} V drive\n")
        f.write(f"# AWG full scale +/-{a.full_scale:g} V (AMP {2*a.full_scale:g} Vpp, "
                f"OFST 0) leaves {head:.1f}% headroom to iterate\n")
        f.write("time_us,voltage_V\n")
        np.savetxt(f, np.column_stack([t_us, hv]), delimiter=",", fmt="%.6f")

    print(f"{a.out}")
    print(f"  {y.size} points at {dt*1e6:.4f} us  ({a.period*1e3:.3f} ms total)")
    print(f"  peak {a.peak_hv:.0f} V at the EOM = {amp_mon:.4f} V at the monitor")
    print(f"  {ch.name} gain {gain:.4f} -> drive peak {drive_peak:.3f} V "
          f"of {a.full_scale:g} V full scale ({head:.1f}% headroom)")


if __name__ == "__main__":
    main()
