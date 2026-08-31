#!/usr/bin/env python3
"""Phase 0 photodiode check: V_DC, word lattice, and dither on the PD channel.

The first thing to run after wiring the photodiode in, and the cheapest way to
find out whether the optical campaign can work at all. It answers three
questions and leaves the scope as it found it:

  1. Is V_DC where the chain predicts?  The RCRC's 2 kOhm of series resistance
     divides into the scope's 1 MOhm, so a 5.400 V detector output should read
     5.389 V at the scope -- 0.2 % down. Much more than that and something in
     the filter chain is loading DC that should not be.

  2. What is the word lattice?  Measured from the trace itself rather than
     assumed from V/div: the smallest positive step between distinct sample
     values IS the effective LSB, after HRES has bought whatever bits it buys.

  3. **Is the trace dithered?**  This is the one that decides everything. The
     ILC resolves a 0.195 mV residual on a 2.5 mV lattice purely because the
     Trek dithers the channel ~1.4 LSB, so 64-shot averaging linearises the
     quantiser. Sub-LSB quantisation is deterministic and does NOT average
     away: a PD trace quieter than about half an LSB gives a staircase that no
     number of shots will converge. The anti-alias filter removes out-of-band
     noise, so it removes dither along with it -- which is why this has to be
     checked with the filter in place, not before.

Every run writes BOTH a .npz of the raw per-shot stacks and a .txt sidecar
carrying the full scope state and the computed statistics, into
run/polarimetry/. That is deliberate: the SR760 campaign left A4, A6, A7, A9
and B3 with results in prose and no traces on disk, and the numbers are only as
good as the ability to recompute them.

Run it on Anaconda: capture_all needs eomilc.scope.resample, which needs
pandas, which the system Python does not have.

    "C:/ProgramData/anaconda3/python.exe" tools/pd_check.py --pd-ch 2 --expect-vdc 5.4

Close the ILC GUI first -- it holds its own VISA session.

Reads only. Never touches the AWG, never arms an output. The PD channel is
snapshotted and restored; measure_vdc does the same internally.
"""
from __future__ import annotations
import argparse, datetime, json, os, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ilc_bench  # noqa: E402

OUT_DIR = os.path.join(ROOT, "run", "polarimetry")

# Recorded in every sidecar. The 2.5 MHz module is electrically inert in this
# band but is part of the chain B1 will measure, so it gets logged anyway.
CHAIN = ("PDA10A2 -> commercial DC-2.5MHz BNC LPF -> 1k/680p -> 1k/680p "
         "-> MSO-X CH{pd}")


def lattice(v):
    """Effective LSB: the typical positive gap between distinct sample values.

    The median gap rather than the minimum, because one stray intermediate
    value from the resample would drag the minimum to nonsense.
    """
    u = np.unique(np.asarray(v, float))
    u = u[np.isfinite(u)]
    if u.size < 3:
        return float("nan"), int(u.size)
    d = np.diff(u)
    d = d[d > 0]
    return (float(np.median(d)) if d.size else float("nan")), int(u.size)


def raw_shots(scope, chans, n, wait_s):
    """N single shots read RAW, with no resampling.

    capture_all resamples onto the target's 2 us grid, and linear
    interpolation between scope samples invents intermediate values: measured
    here, a monitor channel whose real word lattice is 2.5 mV came back with
    4975 distinct values and an apparent 0.6 uV step. So the lattice -- and the
    dither in units of it -- has to be measured before the resample, which is
    what this is for. The resampled stack is still what the analysis uses.
    """
    out = {ch: [] for ch in chans}
    for i in range(n):
        if scope.single(wait_s=wait_s) is not True:
            raise RuntimeError(f"no trigger within {wait_s:g} s on raw shot "
                               f"{i+1} -- is the bench sequence running?")
        for ch in chans:
            _, v = scope.waveform(ch)
            out[ch].append(np.asarray(v, float))
        scope.run()
    lens = {ch: {len(v) for v in rows} for ch, rows in out.items()}
    for ch, ls in lens.items():
        if len(ls) != 1:
            raise RuntimeError(f"CH{ch} returned differing raw lengths {ls} "
                               f"-- the scope changed setup mid-run")
    return {ch: np.vstack(rows) for ch, rows in out.items()}


def channel_state(scope, ch):
    st = {}
    for key, root in ilc_bench.CHANNEL_STATE.items():
        st[key] = str(scope.get(root.format(ch=ch))).strip()
    for key, scpi in (("probe", ":CHANnel{ch}:PROBe"),
                      ("impedance", ":CHANnel{ch}:IMPedance"),
                      ("display", ":CHANnel{ch}:DISPlay")):
        st[key] = str(scope.get(scpi.format(ch=ch))).strip()
    return st


def save_run(label, stacks, meta):
    """Write the raw stacks and a human-readable sidecar side by side."""
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = os.path.join(OUT_DIR, f"{label}_{stamp}")
    if stacks:
        np.savez_compressed(stem + ".npz", **stacks)
    with open(stem + ".txt", "w") as f:
        for k, v in meta.items():
            if isinstance(v, dict):
                f.write(f"{k}:\n")
                for kk, vv in v.items():
                    f.write(f"  {kk:<18} {vv}\n")
            else:
                f.write(f"{k:<20} {v}\n")
    return stem


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pd-ch", type=int, default=2, help="photodiode channel")
    ap.add_argument("--mon-ch", type=int, default=3,
                    help="a monitor channel to compare against, or 0 to skip")
    ap.add_argument("--repeats", type=int, default=8,
                    help="shots; 8 is enough to see the lattice, 64 is a real run")
    ap.add_argument("--wait", type=float, default=15.0, help="trigger timeout, s")
    ap.add_argument("--scale", type=float, default=None,
                    help="V/div to set on the PD channel before capturing "
                         "(default: leave as found, after forcing DC coupling)")
    ap.add_argument("--target", default=os.path.join(ROOT, "waveforms",
                                                     "target_PARX1.csv"))
    ap.add_argument("--expect-vdc", type=float, default=None,
                    help="detector output in V; reports the expected 0.2%% divider loss")
    ap.add_argument("--coarse-scale", type=float, default=None,
                    help="V/div for measure_vdc's coarse pass. Its 1.0 default "
                         "puts +/-4 V on screen, which a 5.4 V pedestal is "
                         "outside; default here is expect-vdc/3, else 2.0")
    ap.add_argument("--label", default="P0_pd_check")
    ap.add_argument("--note", default="", help="free text into the sidecar")
    a = ap.parse_args()

    chans = [a.pd_ch] + ([a.mon_ch] if a.mon_ch else [])

    # Same grid as every later capture, so the analysis path is identical.
    # "# comment" lines, then a "time_us,voltage_V" header, then the data.
    d = np.genfromtxt(a.target, delimiter=",", comments="#", names=True)
    t_us = np.asarray(d[d.dtype.names[0]], float)
    t_us = t_us[np.isfinite(t_us)]          # a trailing blank line reads as NaN
    t_grid = t_us * 1e-6
    dt = float(np.median(np.diff(t_grid)))
    print(f"grid: {t_grid.size} points at {dt*1e6:g} us "
          f"= {np.ptp(t_grid)*1e3:.4f} ms  (from {os.path.basename(a.target)})")

    siblings = os.path.dirname(ROOT)
    scopemod = ilc_bench.load_module(ilc_bench.find_scope_grab(siblings),
                                     "scope_grab")
    scope = ilc_bench.make_scope(scopemod)
    idn = scope.connect()
    print("Scope:", idn)

    meta = {"captured": datetime.datetime.now().isoformat(timespec="seconds"),
            "instrument": idn, "chain": CHAIN.format(pd=a.pd_ch),
            "note": a.note, "repeats": a.repeats,
            "grid": f"{t_grid.size} pts at {dt*1e6:g} us "
                    f"= {np.ptp(t_grid)*1e3:.4f} ms from "
                    f"{os.path.basename(a.target)}"}

    saved = ilc_bench.scope_snapshot(scope, chans)
    try:
        scope.errors()                       # drain anything stale first
        print(f"\nCH{a.pd_ch} as found:")
        found = channel_state(scope, a.pd_ch)
        for k, v in found.items():
            print(f"  {k:10s} {v}")
        meta["pd_as_found"] = found

        # measure_vdc needs DC coupling, and the AC->DC step against a
        # volt-scale pedestal at 10 mV/div throws a transient -222 that
        # scope_apply is right to refuse. Make the transition deliberately,
        # at a scale that can hold the level, before handing over.
        setup = {"coupling": "DC"}
        if a.scale:
            setup["scale"] = a.scale
        elif float(found["scale"]) < 0.1:
            setup["scale"] = 1.0             # enough to find a few-volt level
        ilc_bench.scope_apply(scope, {a.pd_ch: setup})
        print(f"  -> forced {setup} before the DC measurement")

        coarse = (a.coarse_scale if a.coarse_scale
                  else (a.expect_vdc / 3.0 if a.expect_vdc else 2.0))
        print(f"\nV_DC on CH{a.pd_ch} (coarse pass at {coarse:g} V/div):")
        v_dc, detail = ilc_bench.measure_vdc(scope, a.pd_ch,
                                             coarse_scale=coarse)
        print(f"  measured  {v_dc:.4f} V     ({detail})")
        meta["v_dc"] = f"{v_dc:.4f} V"
        meta["v_dc_detail"] = json.dumps(detail, default=str)
        if a.expect_vdc:
            pred = a.expect_vdc * 1e6 / (1e6 + 2e3)
            off = 100 * (v_dc / pred - 1)
            print(f"  predicted {pred:.4f} V from {a.expect_vdc:.3f} V through "
                  f"2 kOhm into 1 MOhm  ({off:+.2f} % off)")
            meta["v_dc_predicted"] = f"{pred:.4f} V ({off:+.2f} % off)"

        # Put the PD on screen and zoomed, now that the level is known. Below
        # COARSE_OFFSET_SCALE the offset cannot reach a multi-volt pedestal at
        # all, so that is the floor whatever --scale asks for.
        want = a.scale or ilc_bench.COARSE_OFFSET_SCALE
        if abs(v_dc) > ilc_bench.FINE_OFFSET_LIMIT:
            want = max(want, ilc_bench.COARSE_OFFSET_SCALE)
        ilc_bench.scope_apply(scope, {a.pd_ch: {"coupling": "DC",
                                                "scale": want,
                                                "offset": v_dc}})
        print(f"  -> CH{a.pd_ch} to {want:g} V/div centred on {v_dc:.4f} V")

        for ch in chans:
            meta[f"ch{ch}_at_capture"] = channel_state(scope, ch)

        print(f"\n{a.repeats} raw shots (for the lattice) ...")
        raw = raw_shots(scope, chans, a.repeats, a.wait)
        print(f"{a.repeats} resampled shots via capture_all ...")
        stacks = ilc_bench.capture_all(scope, chans, t_grid, 0.0,
                                       repeats=a.repeats, wait_s=a.wait)
    finally:
        ilc_bench.scope_restore(scope, saved)
        scope.close()

    print("\nword lattice and dither  (raw traces, before any resampling)")
    verdict = {}
    for ch in chans:
        st = raw[ch]
        lsb, nuniq = lattice(st[0])
        within = float(np.mean(np.std(st, axis=1)))
        shot = float(np.mean(np.std(st, axis=0)))
        tag = "PD" if ch == a.pd_ch else "monitor"
        print(f"\n  CH{ch} ({tag}): lattice {lsb*1e3:.4f} mV, "
              f"{nuniq} distinct values in shot 1 of {st.shape[1]} samples")
        print(f"    within-shot rms   {within:11.4e} V = {within/lsb:6.2f} LSB")
        print(f"    shot-to-shot rms  {shot:11.4e} V = {shot/lsb:6.2f} LSB")
        meta[f"ch{ch}_stats"] = {
            "lattice_mV": f"{lsb*1e3:.4f}", "distinct_values": nuniq,
            "raw_samples": st.shape[1],
            "within_shot_rms_V": f"{within:.4e}",
            "within_shot_LSB": f"{within/lsb:.2f}",
            "shot_to_shot_rms_V": f"{shot:.4e}",
            "shot_to_shot_LSB": f"{shot/lsb:.2f}"}
        verdict[ch] = shot / lsb

    dv = verdict[a.pd_ch]
    if not np.isfinite(dv):
        v = ("PD lattice not measurable -- too few distinct values. That is "
             "itself the answer: the trace is quantisation-locked.")
    elif dv >= 1.0:
        v = (f"PD dithered at {dv:.2f} LSB. Averaging will linearise the "
             "quantiser; 64 shots buys the full 1/8.")
    elif dv >= 0.5:
        v = (f"PD dithered at {dv:.2f} LSB -- adequate but not generous. "
             "Averaging works; watch for staircase in the converged residual.")
    else:
        v = (f"PD dithered at only {dv:.2f} LSB. STOP: sub-LSB quantisation is "
             "deterministic and 64-shot averaging will NOT converge it. Drop "
             "V/div, or accept more detector noise, before taking data.")
    print(f"\nverdict\n  {v}")
    meta["verdict"] = v

    archive = dict(stacks)
    archive.update({f"raw_CH{ch}": v for ch, v in raw.items()})
    archive["t_grid"] = t_grid
    stem = save_run(a.label, archive, meta)
    print(f"\nsaved {stem}.npz and .txt")


if __name__ == "__main__":
    main()
