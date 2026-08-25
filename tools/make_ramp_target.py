#!/usr/bin/env python3
"""Build ILC targets from a tuned forward/reverse ramp pair.

The tuned waveforms arrive as two files on their own fine grid -- a forward leg
running 0 -> peak and a reverse leg running peak -> 0 -- and what the AWG plays
is those two spliced around a hold at the top:

    forward leg | hold at peak | reverse leg

This builds that composite, moves it onto the loop's 2 us grid, and writes it as
a target in volts AT THE EOM (`time_us,voltage_V`), which is what
`run_ilc.py init` consumes.  Two arrangements:

  --mode integrated   one shape, played on both channels (EOs in parallel).
                      Both channels see the same 0..peak stroke.

  --mode split        the same stroke at twice the amplitude, handed off between
                      the two channels (EOs in series).  Channel A takes the
                      stroke up to half the total and then holds; channel B
                      starts at the handoff and takes it the rest of the way.
                      A + B is the total at every sample, exactly.

    python tools/make_ramp_target.py --mode integrated --peak-hv 5200 \
           --forward forward.csv --reverse reverse.csv --out run/target_PLY.csv

    python tools/make_ramp_target.py --mode split --total-hv 10400 \
           --forward forward.csv --reverse reverse.csv \
           --out-a run/target_SPLX1.csv --out-b run/target_SPLX2.csv

Splice bookkeeping: the forward leg's last sample and the reverse leg's first
sample are the same instant as the two ends of the hold, so one copy of each is
dropped.  The result is `n_fwd + hold/dt + (n_rev - 1)` samples on an unbroken
grid, and the AWG plays them as `n * step` of period -- keep the burst period in
step with that number, not with the span between the first and last sample.
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for eomilc
from eomilc.config import CHANNELS, HV_PER_MON


def load_leg(path):
    """(time_us, voltage_V, dt) from a 2- or 3-column CSV; extra columns ignored."""
    a = np.genfromtxt(path, delimiter=",", names=True)
    names = [n.lower() for n in a.dtype.names]
    t = a[a.dtype.names[names.index("time_us")]].astype(float)
    v = a[a.dtype.names[names.index("voltage_v")]].astype(float)
    dt = np.diff(t)
    if np.ptp(dt) > 1e-9 * max(1.0, abs(dt[0])):
        sys.exit(f"{os.path.basename(path)}: source grid is not uniform "
                 f"({dt.min():.6f}..{dt.max():.6f} us)")
    return t, v, float(np.median(dt))


def splice(vf, vr, src_dt, hold_us):
    """forward | hold at peak | reverse, on the source grid, one splice copy each.

    Returns (t_us, v) with t starting at 0 and stepping by src_dt throughout.
    """
    n_hold = hold_us / src_dt
    if abs(n_hold - round(n_hold)) > 1e-9:
        sys.exit(f"a {hold_us} us hold is {n_hold:.4f} samples on the "
                 f"{src_dt} us source grid, not a whole number")
    n_hold = int(round(n_hold))
    peak = 0.5 * (vf[-1] + vr[0])
    if abs(vf[-1] - vr[0]) > 1e-6 * max(1.0, abs(peak)):
        print(f"note: legs meet at {vf[-1]:.6f} and {vr[0]:.6f} V; "
              f"holding at their mean {peak:.6f}")
    v = np.concatenate([vf, np.full(n_hold, peak), vr[1:]])
    return np.arange(v.size) * src_dt, v


def to_grid(t_src, v_src, step_us):
    """Plain linear interpolation onto an exact `step_us` grid over the same span.

    Safe here only because the source is band-limited by construction (the tuning
    puts a notch at 10 kHz); this is not a general-purpose resampler.
    """
    span = t_src[-1]
    n_int = span / step_us
    if abs(n_int - round(n_int)) > 1e-9:
        sys.exit(f"the {span:.4f} us composite is {n_int:.6f} steps of "
                 f"{step_us} us -- pick a hold that makes this whole")
    t = np.arange(round(n_int) + 1) * step_us
    return t, np.interp(t, t_src, v_src)


def rescale(v, peak_hv):
    """min..max -> 0..peak_hv, the same normalisation make_target.py uses."""
    return (v - v.min()) / float(np.ptp(v)) * peak_hv


def report(tag, t, v, channel, full_scale):
    step = float(np.median(np.diff(t)))
    ch = CHANNELS[channel]
    amp_mon = v.max() / HV_PER_MON
    gain = ch.gain(amp_mon)
    drive = amp_mon / gain
    ceiling = full_scale * gain * HV_PER_MON
    print(f"  {tag}: peak {v.max():.3f} V, ends {v[0]:.6f}/{v[-1]:.6f} V, "
          f"peak slew {np.abs(np.diff(v) / step).max():.3f} V/us")
    print(f"       {ch.name} gain {gain:.4f} -> drive {drive:.3f} V of "
          f"{full_scale:g} V ({100 * (1 - drive / full_scale):.1f}% headroom, "
          f"ceiling {ceiling:.0f} V)")
    if drive > full_scale:
        print(f"       *** {v.max():.0f} V is past this channel's "
              f"{ceiling:.0f} V ceiling ***")


def write(path, t, v, header_lines):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        for line in header_lines:
            f.write(f"# {line}\n")
        f.write("time_us,voltage_V\n")
        np.savetxt(f, np.column_stack([t, v]), delimiter=",", fmt="%.6f")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--forward", required=True)
    ap.add_argument("--reverse", required=True)
    ap.add_argument("--hold-us", type=float, default=1000.0,
                    help="flat top between the two legs, microseconds")
    ap.add_argument("--step", type=float, default=2.0, help="output grid, microseconds")
    ap.add_argument("--mode", required=True, choices=("integrated", "split"))
    ap.add_argument("--peak-hv", type=float, help="integrated: peak volts at each EOM")
    ap.add_argument("--total-hv", type=float,
                    help="split: peak volts summed across the two EOMs")
    ap.add_argument("--out", help="integrated: output target for channel A")
    ap.add_argument("--out-a", help="split: first channel (ramps first, holds high)")
    ap.add_argument("--out-b", help="integrated: the channel B copy, if you want the "
                                    "one-file-per-channel layout; "
                                    "split: second channel (holds low, ramps second)")
    ap.add_argument("--channel-a", default="EO1", choices=list(CHANNELS))
    ap.add_argument("--channel-b", default="EO2", choices=list(CHANNELS))
    ap.add_argument("--full-scale", type=float, default=10.0,
                    help="AWG volts at DAC full scale (AMP = 2x this, OFST 0)")
    a = ap.parse_args()

    tf, vf, dtf = load_leg(a.forward)
    tr, vr, dtr = load_leg(a.reverse)
    if abs(dtf - dtr) > 1e-9:
        sys.exit(f"legs are on different grids: {dtf} us and {dtr} us")

    t_src, v_src = splice(vf, vr, dtf, a.hold_us)
    t, v = to_grid(t_src, v_src, a.step)
    back = np.abs(np.interp(t_src, t, v) - v_src).max()
    print(f"source  : {vf.size} + {vr.size} pts at {dtf:g} us, {a.hold_us:g} us hold "
          f"-> {v_src.size} pts spliced, span {t_src[-1] / 1e3:.4f} ms")
    print(f"regrid  : -> {v.size} pts at {a.step:g} us, period "
          f"{v.size * a.step / 1e3:.4f} ms  (round trip off by {back:.4f} "
          f"source volts, {100 * back / np.ptp(v_src):.4f}% of span)")

    prov = (f"from {os.path.basename(a.forward)} + {a.hold_us:g} us hold + "
            f"{os.path.basename(a.reverse)}, {dtf:g} us -> {a.step:g} us linear")
    period = f"{v.size} pts at {a.step:g} us = {v.size * a.step / 1e3:.4f} ms period"

    if a.mode == "integrated":
        if a.peak_hv is None or not a.out:
            sys.exit("--mode integrated needs --peak-hv and --out")
        hv = rescale(v, a.peak_hv)
        print("\nintegrated (EOs in parallel) -- same shape on both channels:")
        for chan in (a.channel_a, a.channel_b):
            report(f"as {chan}", t, hv, chan, a.full_scale)
        # The samples are channel independent -- volts at the EOM -- so one file
        # serves both.  --out-b writes the second copy anyway, so the run/ layout
        # stays one target per channel the way the MKJ pair is.
        outs = [(a.out, a.channel_a)] + ([(a.out_b, a.channel_b)] if a.out_b else [])
        for path, chan in outs:
            write(path, t, hv, [
                f"target for {chan} " + prov,
                f"integrated ramp target (EOs in parallel): peak {a.peak_hv:.0f} V "
                f"at the EOM, {period}",
                f"first/last sample {hv[0]:.6f}/{hv[-1]:.6f} V; peak slew "
                f"{np.abs(np.diff(hv) / a.step).max():.3f} V/us at the EOM",
                "identical samples on both channels; the per-channel drive is what differs",
            ])
        print("\nwrote " + "\n      ".join(p for p, _ in outs))
        return

    if a.total_hv is None or not (a.out_a and a.out_b):
        sys.exit("--mode split needs --total-hv, --out-a and --out-b")
    total = rescale(v, a.total_hv)
    half = a.total_hv / 2.0
    k = int(np.argmax(total >= half))              # first crossing, on the up leg
    v1 = float(total[k])
    ch_a = np.minimum(total, v1)                   # ramps first, then holds high
    ch_b = total - ch_a                            # holds at 0, then ramps
    assert np.allclose(ch_a + ch_b, total, atol=1e-9)
    corner = abs(np.diff(total)[k] / a.step)

    print(f"\nsplit (EOs in series) -- handoff at sample {k}, t = {t[k]:g} us "
          f"({100 * t[k] / t[-1]:.2f}% of the record):")
    print(f"  A takes 0..{v1:.3f} V, B takes 0..{a.total_hv - v1:.3f} V, "
          f"sum {a.total_hv:.0f} V")
    print(f"  handoff corner: {corner:.3f} V/us of slope steps to 0 on A and up "
          f"from 0 on B, against {np.abs(np.diff(total) / a.step).max():.3f} V/us "
          f"peak in the record")
    for tag, arr, chan in (("A", ch_a, a.channel_a), ("B", ch_b, a.channel_b)):
        report(f"{tag} as {chan}", t, arr, chan, a.full_scale)
    for tag, arr, chan, path, role in (
            ("A", ch_a, a.channel_a, a.out_a,
             "ramps first, then holds at the handoff level"),
            ("B", ch_b, a.channel_b, a.out_b,
             "holds at 0, then ramps on from the handoff level")):
        write(path, t, arr, [
            prov,
            f"split ramp target {tag} ({chan}): {role}",
            f"total stroke {a.total_hv:.0f} V across both EOMs, handed off at "
            f"{v1:.3f} V (sample {k}, t = {t[k]:g} us); this channel peaks at "
            f"{arr.max():.3f} V",
            f"{period}; first/last sample {arr[0]:.6f}/{arr[-1]:.6f} V",
            "A + B reproduces the integrated stroke sample for sample",
        ])
    print(f"\nwrote {a.out_a}\n      {a.out_b}")


if __name__ == "__main__":
    main()
