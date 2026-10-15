"""Switch both 4063B outputs OFF and report, nothing else: the safety net for
a headless batch that had to be killed (a killed process cannot run the
batch's own outputs-off). Opens the generator the way the batch does, so
the same module and VISA resource are used.

    python tools/awg_outputs_off.py

Refuses to do anything while another program holds the generator (VISA
raises); wait ~60 s after killing the batch and run again.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ilc_bench                                                  # noqa: E402


def main():
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
