#!/usr/bin/env python3
"""Put a spin echo's transports into one ILC record, at the echo's spacing.

    python tools/make_echo_target.py target_S47X1.csv [target_S47X2.csv] --spacing-ms 16.667
           [--legs 2] [--tail-us 200] [--suffix E2] [--out DIR]

The command line over eomilc.echo (the panel's Session tab -- Echo... beside
the Target box -- and a batch campaign's "echo" key build the same files). A
lone X1 takes its X2 from beside it. Each written channel is read back with
run_ilc.load_target and its flat first shot put through the limit check, as
`run_ilc.py init` does (PASS/FAIL per channel; exit 1 on FAIL). The record's
layout, the comment lines and the two caps (stem, points) are documented in
eomilc/echo.py.
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from eomilc import echo                                          # noqa: E402

STEM_CAP, CHANNEL = echo.STEM_CAP, echo.CHANNEL                  # as before, for callers
awg_max_pts, read_target, name_parts, pieces, build, check = (
    echo.awg_max_pts, echo.read_target, echo.name_parts, echo.pieces, echo.build, echo.check)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", help="target_<stem>X1.csv [target_<stem>X2.csv]")
    ap.add_argument("--spacing-ms", type=float, default=16.667,
                    help="leg start to leg start (default 16.667 ms)")
    ap.add_argument("--legs", type=int, default=2, help="transports in the record (default 2)")
    ap.add_argument("--tail-us", type=float, default=200.0, help="idle after the last transport")
    ap.add_argument("--suffix", default=None, help="added to the stem (default E<legs>)")
    ap.add_argument("--out", default=None, help="output folder (default: the source's)")
    a = ap.parse_args(argv)
    if len(a.targets) > 2:
        raise SystemExit("give one or two targets (X1 [X2])")
    try:
        r = echo.make(a.targets, spacing_ms=a.spacing_ms, legs=a.legs, tail_us=a.tail_us,
                      suffix=a.suffix, out_dir=a.out, log=print)
    except ValueError as e:
        raise SystemExit(str(e))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
