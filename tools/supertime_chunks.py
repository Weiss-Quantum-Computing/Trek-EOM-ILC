"""Cut a batch campaign's closed drives into the chunks Supertime's custom-ramp
slots take, with the table of step size, start and end voltage per chunk
(eomilc.supertime; the panel's Batch window has the same as Export for
Supertime...).

    python tools/supertime_chunks.py <plan.json> <out folder> [stem ...]

Without stems, every campaign the plan's manifest lists as done. Chunks are
two-column CSVs (index, AWG volts; no header), every boundary on a 10 us
grid; ramp_table.csv / .md carry duration, step, points and the start / end
voltage of every chunk as AWG volts, degrees at the EOM and Supertime card
volts (derived from the day-log 90-degree points: confirm with a test ramp).
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ilc_batch                                                  # noqa: E402
from eomilc import supertime                                      # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    ap.add_argument("out")
    ap.add_argument("stems", nargs="*")
    a = ap.parse_args(argv)
    plan = ilc_batch.load_plan(a.plan)
    man = ilc_batch.read_manifest(plan, False)
    stems = a.stems or [c["stem"] for c in plan["campaigns"]
                        if (man["campaigns"].get(c["stem"]) or {}).get("status", "").startswith("done")]
    supertime.export(plan, stems, a.out)


if __name__ == "__main__":
    main()
