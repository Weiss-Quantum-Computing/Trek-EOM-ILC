#!/usr/bin/env python3
"""Closed-loop ILC on the bench: BK4063B upload -> MSO-X capture -> update.

Reuses the instrument layers already written for Scope Grab and the AWG GUI --
`Scope` and `Awg` are plain classes with no Tk dependency, so this imports them
rather than reimplementing SCPI that has already been debugged against the
hardware.

    python ilc_bench.py --channel EO1 \
        --target waveform_tuned_10kHz_4p8ms.csv \
        --awg-ch 1 --mon-col CH3 --iterations 4

SAFETY POSTURE
--------------
This script does NOT set amplitude, offset, load, sample clock, or the output
state.  Set the channel up in the AWG GUI, turn the output on there, and confirm
on the monitor that you are where you expect.  This script only:

  * uploads a waveform into user memory and selects it,
  * arms the scope and reads a trace back.

Before the first upload it VERIFIES the channel is configured the way the drive
file assumes, and refuses to run if it is not.  A mismatch here silently
rescales the drive -- see the note on normalisation below.

THE NORMALISATION TRAP
----------------------
`Awg.upload_arb(..., normalize=True)` divides the samples by their own peak.
That is right for a one-off waveform and WRONG for ILC: each iteration has a
slightly different peak, so re-normalising every round quietly rescales the
correction the loop just computed, and the loop stops converging for reasons
that look like plant drift.

This script uploads with normalize=False against a FIXED full scale, so the
DAC mapping is identical on every iteration and the amplitude correction lands
where it was meant to.
"""
from __future__ import annotations
import argparse, importlib.util, os, sys, time
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from eomilc import scope as scopeio, plant as plantmod, ilc, outputs
from eomilc.config import CHANNELS, HV_PER_MON


def load_module(path, name):
    """Import one of the bench programs by file path."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise ImportError(f"cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------- AWG
def check_awg_channel(awg, ch, expect_rate=None, expect_clock="DDS",
                      full_scale=10.0, tol=0.02):
    """Refuse to upload into a channel that isn't set up the way we assume."""
    blocks = awg.read_channel(ch)
    bswv = awg_parse(blocks["BSWV"])
    srate = awg_parse(blocks["SRATE"])
    outp = awg_parse(blocks["OUTP"])
    problems, notes = [], []

    amp = as_float(bswv.get("AMP"))
    ofst = as_float(bswv.get("OFST"))
    want_amp, want_ofst = 2 * full_scale, 0.0
    if amp is None or abs(amp - want_amp) > tol * want_amp:
        problems.append(f"BSWV AMP is {amp} Vpp; this drive file assumes "
                        f"{want_amp:g} Vpp (full scale +/-{full_scale:g} V)")
    if ofst is None or abs(ofst - want_ofst) > 0.02:
        problems.append(f"BSWV OFST is {ofst} V; this drive file assumes 0 V")

    clock = srate.get("MODE")
    if expect_clock and clock != expect_clock:
        problems.append(f"sample clock is {clock}, expected {expect_clock}. "
                        f"DDS resamples the record into one period, so the point "
                        f"grid is not literal -- but it is the only mode that "
                        f"allows the triggered burst this bench runs on.")
    rate = as_float(srate.get("VALUE")) or as_float(srate.get("SRATE"))
    if expect_rate and rate and abs(rate - expect_rate) / expect_rate > 1e-3:
        problems.append(f"sample rate is {rate:g} Sa/s, expected {expect_rate:g}")

    notes.append(f"CH{ch}: output {outp.get('STATE')}, load {outp.get('LOAD')}, "
                 f"{bswv.get('WVTP')}, clock {clock} @ {rate} Sa/s, "
                 f"AMP {amp} Vpp, OFST {ofst} V")
    return problems, notes


def awg_parse(reply):
    """Thin wrapper so this file doesn't care which module parse_reply lives in."""
    return _AWGMOD.parse_reply(reply)


def as_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def upload_drive(awg, ch, name, u_awg, full_scale=10.0):
    """Upload a drive in volts, using a FIXED mapping to the DAC.

    u/full_scale lands in -1..+1, uploaded with normalize=False, so a change of
    one millivolt between iterations is a change of one millivolt at the output
    -- not a change that gets normalised away.
    """
    peak = float(np.abs(u_awg).max())
    if peak > full_scale:
        raise ValueError(f"drive peaks at {peak:.3f} V, past the {full_scale:g} V "
                         f"full scale this mapping assumes")
    # NOT a truncation: the generator accepts the upload, plays the right shape,
    # and then wedges its front panel until it is power cycled.  The limit is on
    # the stored `<name>.bin`, which is 15 characters, so a typed name gets 11.
    # Taken from the GUI so there is one definition of it on this bench.
    limit = getattr(_AWGMOD, "MAX_ARB_NAME", 11)
    if len(name) > limit:
        raise ValueError(f"waveform name {name!r} is {len(name)} chars; the 4063B "
                         f"stores it as {name}.bin and locks its front panel past "
                         f"{limit + 4} stored characters, so the cap is {limit}")
    n = awg.upload_arb(ch, name, u_awg / full_scale, normalize=False)
    return n, peak / full_scale


# ------------------------------------------------------------------- scope
def capture(scope, channels, mon_col, t_grid, t_offset,
            repeats=1, wait_s=30.0, points=None, settle=0.5):
    """Arm, wait for a trigger, read back, resample onto the waveform grid."""
    traces = []
    for i in range(repeats):
        time.sleep(settle)
        got = scope.single(wait_s=wait_s)
        if got is not True:
            raise RuntimeError(f"no trigger within {wait_s:g} s on repeat {i+1} "
                               f"-- is the sequence running?")
        cols = {}
        for ch in channels:
            t, v = scope.waveform(ch, points=points)
            cols[f"CH{ch}"] = (t, v)
        scope.run()
        t_src, v_src = cols[mon_col]
        traces.append(scopeio.resample(t_src, v_src, t_grid, t_offset=t_offset))
    return ilc.averaged(traces)


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", required=True, choices=list(CHANNELS))
    ap.add_argument("--name", default=None,
                    help="waveform name stem for the upload (default: the channel "
                         "name). Iteration suffix is appended, and the total must "
                         "fit MAX_ARB_NAME = 11 characters.")
    ap.add_argument("--target", required=True, help="target waveform CSV, volts at the EOM")
    ap.add_argument("--awg-ch", type=int, default=1, choices=(1, 2))
    ap.add_argument("--scope-ch", type=int, default=3, help="scope channel carrying the monitor")
    ap.add_argument("--iterations", type=int, default=4)
    ap.add_argument("--repeats", type=int, default=1,
                    help="software averages per iteration; leave at 1 and let the "
                         "scope average in hardware (ACQuire:TYPE AVER)")
    ap.add_argument("--t-offset", type=float, required=True,
                    help="fixed trigger-to-waveform offset in microseconds -- "
                         "measure once, then never change it")
    ap.add_argument("--full-scale", type=float, default=10.0)
    ap.add_argument("--sample-rate", type=float, default=None,
                    help="expected SRATE, only meaningful under TrueArb; under DDS "
                         "the record is resampled into one period and the point "
                         "grid is not literal")
    ap.add_argument("--points", type=int, default=None, help="scope transfer points")
    ap.add_argument("--wait", type=float, default=30.0)
    ap.add_argument("--model", default="resonant", choices=plantmod.MODELS)
    ap.add_argument("--gamma", type=float, default=0.6)
    # 5 kHz: the model is only trusted below ~5 kHz on this bench (see run_ilc)
    ap.add_argument("--f-cut", type=float, default=5e3)
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--scope-grab", default="scope_grab.py")
    ap.add_argument("--awg-gui", default="bk4063b_awg_gui.py")
    ap.add_argument("--skip-checks", action="store_true",
                    help="upload without verifying the channel setup. Don't.")
    a = ap.parse_args()

    global _AWGMOD
    scopemod = load_module(a.scope_grab, "scope_grab")
    _AWGMOD = load_module(a.awg_gui, "bk4063b_awg_gui")

    # ---- target
    df = pd.read_csv(a.target, comment="#")
    t = df.iloc[:, 0].to_numpy(float)
    t = t * 1e-6 if "us" in df.columns[0].lower() else t
    v = df.iloc[:, 1].to_numpy(float) / HV_PER_MON      # monitor volts
    dt = float(np.median(np.diff(t)))
    ch = CHANNELS[a.channel]
    amp = float(np.ptp(v))

    model = ch.plant(amp, dt, model=a.model)
    loop = ilc.Loop(plant=model, target=v, dt=dt, channel=ch,
                    gamma=a.gamma, f_cut=a.f_cut)
    stem = a.name or ch.name
    limit = getattr(_AWGMOD, "MAX_ARB_NAME", 11)
    if len(stem) + 4 > limit:                        # "_i00" is four more
        sys.exit(f"--name {stem!r} is {len(stem)} chars; with the '_i00' suffix "
                 f"that is {len(stem)+4}, past the {limit}-character cap.")
    print(f"channel {ch.name}: {model}")
    print(f"uploads as  : {stem}_i00 ... {stem}_i{a.iterations:02d}")
    print(f"target  {np.ptp(v)*HV_PER_MON:.0f} V over {t[-1]*1e3:.2f} ms, "
          f"{len(v)} points at {dt*1e6:.3f} us")

    # ---- instruments
    awg = _AWGMOD.Awg()
    print("AWG:  ", awg.connect())
    scope = scopemod.Scope()
    print("Scope:", scope.connect())

    problems, notes = check_awg_channel(awg, a.awg_ch, expect_rate=a.sample_rate,
                                        full_scale=a.full_scale)
    for n in notes:
        print("      ", n)
    acq = scope.get(":ACQuire:TYPE")
    count = scope.get(":ACQuire:COUNt") if acq.upper().startswith("AVER") else None
    print(f"       scope acquisition {acq}" + (f", {count} averages" if count else ""))
    if acq.upper().startswith("NORM") and a.repeats < 8:
        problems.append("scope is in NORM with few software repeats -- one 8-bit "
                        "trace has an LSB worth ~40 V at the EOM. Set ACQuire:TYPE "
                        "to AVER with COUNt 256, or raise --repeats.")
    if problems:
        print("\nSetup problems:")
        for p in problems:
            print("  !", p)
        if not a.skip_checks:
            sys.exit("\nRefusing to upload. Fix the setup in the GUI, or pass --skip-checks.")

    os.makedirs(a.outdir, exist_ok=True)
    t_off = a.t_offset * 1e-6
    u = loop.first_shot()

    try:
        for k in range(a.iterations + 1):
            rep = loop.check(u)
            if not rep:
                print("\nlimit check FAILED:", rep)
                break

            name = f"{stem}_i{k:02d}"                    # <= MAX_ARB_NAME
            n, frac = upload_drive(awg, a.awg_ch, name, u, a.full_scale)
            print(f"\niter {k}: uploaded {name} ({n} pts, {100*frac:.1f}% of DAC range, "
                  f"peak {np.abs(u).max():.4f} V)")
            outputs.write_awg_csv(os.path.join(a.outdir, f"drive_{name}.csv"), t, u)

            y = capture(scope, [a.scope_ch], f"CH{a.scope_ch}", t, t_off,
                        repeats=a.repeats, wait_s=a.wait, points=a.points)
            np.save(os.path.join(a.outdir, f"meas_{name}.npy"), y)

            m = loop.metrics(y)
            print(f"         error: peak {m['peak_err_hv']:7.1f} V   "
                  f"rms {m['rms_err_hv']:6.2f} V   ({m['peak_pct']:.2f}% FS)")

            if k < a.iterations:
                u = loop.update(u, y)
    finally:
        scope.close()
        awg.close()

    print("\n" + loop.report())
    print(f"\ndrives and measurements in {os.path.abspath(a.outdir)}")


if __name__ == "__main__":
    main()
