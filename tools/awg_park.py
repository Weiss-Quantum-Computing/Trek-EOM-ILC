"""Park both 4063B outputs and report, nothing else: the never-float stop for
a headless batch that had to be killed (a killed process cannot run the
batch's own park). Each channel gets a flat record at its idle -- uploaded
as PARK1 / PARK2 and selected, the names the batch and the panel park
under -- and its output is left as it is: ON stays ON, so a stage the
output feeds never sees a floating input (memory awg-trek-safety). A flat
record plays the same at any FRQ, so the channel's FRQ is not touched.
Opens the generator the way the batch does (same module, VISA resource).

    python tools/awg_park.py
    python tools/awg_park.py --state1 run/drive_S25X1.state.npz --state2 run/drive_S25X2.state.npz
    python tools/awg_park.py --idle1 0.024 --idle2 0.080

The idle is the drive's first sample: from --stateN (an ILC state's drive),
else --idleN volts at the AWG, else 0 V (within ~100 mV of every learned
idle trim: X1 +20..+26 mV, X2 +78..+81 mV). An output that is OFF is left
OFF unless --on, which switches it ON after a typed ON that names it.
tools/awg_outputs_off.py is the OFF counterpart, for a bypassed stage only.

Refuses to do anything while another program holds the generator (VISA
raises); wait ~60 s after killing the batch and run again.
"""
import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ilc_bench                                                  # noqa: E402

N_PARK = 5501            # any length plays flat; the ILC's 11 ms record, 2 us grid
FULL_SCALE = 10.0


def idle_of(state, volts):
    if state:
        with np.load(state, allow_pickle=True) as z:
            return float(np.asarray(z["u"], float)[0]), os.path.basename(state)
    if volts is not None:
        return float(volts), "--idle"
    return 0.0, "default 0 V"


def main(argv=None, awg=None, ask=input):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for c in (1, 2):
        ap.add_argument(f"--state{c}", default=None, help=f"ILC state whose drive's first sample is CH{c}'s idle")
        ap.add_argument(f"--idle{c}", type=float, default=None, help=f"CH{c}'s idle, volts at the AWG")
    ap.add_argument("--on", action="store_true",
                    help="switch an output that is OFF ON (after a typed ON) once it holds its park")
    a = ap.parse_args(argv)
    idles = {c: idle_of(getattr(a, f"state{c}"), getattr(a, f"idle{c}")) for c in (1, 2)}
    for c, (v, src) in idles.items():
        if not np.isfinite(v) or abs(v) > 0.1:
            sys.exit(f"CH{c} idle {v:+.4f} V ({src}) is not within the 100 mV idle cap; nothing touched")
    if awg is None:
        siblings = os.path.dirname(ROOT)
        ilc_bench._AWGMOD = ilc_bench.load_module(
            os.environ.get("AWG_GUI", os.path.join(siblings, "BK4063B-AWG-GUI", "bk4063b.py")),
            "bk4063b_awg_gui")
        awg = ilc_bench.make_awg(ilc_bench._AWGMOD)
        print("AWG:", awg.connect())
    try:
        for c in (1, 2):
            problems, _notes = ilc_bench.check_awg_channel(awg, c, full_scale=FULL_SCALE)
            if problems:
                print(f"CH{c}: note -- " + "; ".join(problems) + " (the park still plays flat)")
            name, idle = ilc_bench.park_output(awg, c, np.full(N_PARK, idles[c][0]), FULL_SCALE,
                                               f"PARK{c}")
            print(f"CH{c}: plays {name}, flat at {idle:+.4f} V ({idles[c][1]}); output "
                  f"{'ON' if awg.is_on(c) else 'OFF'}")
        off = [c for c in (1, 2) if not awg.is_on(c)]
        if off and a.on:
            names = " and ".join(f"CH{c}" for c in off)
            if ask(f"Type ON to switch {names} on (each holds its flat park): ").strip() == "ON":
                for c in off:
                    awg.set_output(c, True)
                    print(f"CH{c}: output {'ON' if awg.is_on(c) else 'still OFF'}")
            else:
                print(f"not confirmed: {names} left OFF")
        elif off:
            print("left OFF: " + ", ".join(f"CH{c}" for c in off) + " (--on switches them on, parked)")
    finally:
        awg.close()


if __name__ == "__main__":
    main()
