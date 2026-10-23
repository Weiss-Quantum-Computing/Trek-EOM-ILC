"""Switch both 4063B outputs OFF and report, nothing else: the safety net for
a headless batch that had to be killed (a killed process cannot run the
batch's own outputs-off). Opens the generator the way the batch does, so
the same module and VISA resource are used.

    python tools/awg_outputs_off.py

The never-float stop is tools/awg_park.py (flat idle records, outputs left
ON). OFF floats every input the outputs feed -- a stage that drives high
when its input floats (X2's preconditioning stage) must be bypassed -- so
this asks for a typed OFF that names both channels first (--yes skips it).

Refuses to do anything while another program holds the generator (VISA
raises); wait ~60 s after killing the batch and run again.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ilc_bench                                                  # noqa: E402


def main(argv=None, ask=input):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    a = ap.parse_args(argv)
    if not a.yes:
        print("This switches AWG CH1 and CH2 OFF: their inputs then float. Only with every stage "
              "they feed bypassed -- tools/awg_park.py parks them ON instead.")
        if ask("Type OFF to switch CH1 and CH2 off: ").strip() != "OFF":
            sys.exit("not confirmed; nothing was touched")
    siblings = os.path.dirname(ROOT)
    ilc_bench._AWGMOD = ilc_bench.load_module(
        os.environ.get("AWG_GUI", os.path.join(siblings, "BK4063B-AWG-GUI", "bk4063b.py")), "bk4063b_awg_gui")
    awg = ilc_bench.make_awg(ilc_bench._AWGMOD)
    print("AWG:", awg.connect())
    try:
        for c in (1, 2):
            was = awg.is_on(c)
            if was:
                awg.set_output(c, False)
            print(f"CH{c}: was {'ON' if was else 'OFF'}, now {'ON' if awg.is_on(c) else 'OFF'}")
    finally:
        awg.close()


if __name__ == "__main__":
    main()
