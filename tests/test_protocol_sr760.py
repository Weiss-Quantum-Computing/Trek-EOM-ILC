"""The run_protocol paths that need the real sr760. No instruments.

tests/test_protocol.py is the offline half: it checks the planner and asserts
that importing run_protocol does NOT pull in sr760, because --dry-run has to
cost a session on a machine with no instrument code. This file is the other
half. It loads sr760 the way run_protocol loads it - by path, out of the
Spectrum-grab checkout beside this one - and checks the parts of the runner
that only mean anything with it there.

Those parts are the ones that decide whether a segment can be believed:
manifest_entry writes n_indep and rel_err into the manifest, load_segments
carries them into rin.Segment, and that is the error bar two segments are
compared across at a splice. A wrong number there does not look wrong.

Skips itself, rather than failing, when the sibling checkout or pyvisa is
missing - it is the one test here that needs both.

    python tests/test_protocol_sr760.py
"""
import importlib.util
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_protocol as rp                            # noqa: E402

SIBLINGS = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def find_sr760(siblings):
    """ilc_bench.find_spectrum_grab, inlined.

    Not imported from there: ilc_bench pulls in pandas, which the system
    interpreter does not have, and that would make this the one test in the
    folder that runs only under Anaconda. sr760 itself needs no more than
    pyvisa and numpy. The convention is cross-checked against the real function
    below whenever ilc_bench does import.
    """
    env = os.environ.get("SPECTRUM_GRAB")
    if env:
        return env
    for name in ("Spectrum-grab", "spectrum-grab"):
        p = os.path.join(siblings, name, "sr760.py")
        if os.path.exists(p):
            return p
    return os.path.join(siblings, "Spectrum-grab", "sr760.py")


LIB = find_sr760(SIBLINGS)
if not os.path.exists(LIB):
    print(f"SKIP: sr760.py not found at {LIB}\n"
          f"Clone Weiss-Quantum-Computing/Spectrum-Grab beside this repo.")
    raise SystemExit(0)
try:
    _spec = importlib.util.spec_from_file_location("sr760", LIB)
    sr = importlib.util.module_from_spec(_spec)
    sys.modules["sr760"] = sr
    _spec.loader.exec_module(sr)
except ImportError as exc:                  # pyvisa missing on this interpreter
    print(f"SKIP: sr760 will not import ({exc})")
    raise SystemExit(0)

checks = 0


def ok(label, condition, detail=""):
    global checks
    checks += 1
    print(f"[{checks}] {'OK ' if condition else 'FAIL'} {label}"
          + (f"   {detail}" if detail else ""))
    if not condition:
        raise AssertionError(label + " " + detail)


# The analyzer as the protocol preset leaves it: PSD, LogMag, Vrms, 100 linear
# RMS averages, no overlap, range pinned.
GOOD = {"SPAN": "11", "STRF": "0", "WNDO": "2", "MEAS0": "1", "DISP0": "0",
        "UNIT0": "1", "ISRC": "0", "ICPL": "0", "IRNG": "-30", "ARNG": "0",
        "AVGO": "1", "NAVG": "100", "AVGT": "0", "AVGM": "0", "OVLP": "0"}

SPEC = rp.TraceSpec(span=11, strf=0.0, label="seg0", filter_id="none")
MSET = rp.MeasurementSet(name="set A", title="rin", purpose="test",
                         traces=[SPEC])
NOTES = {"measure time (s)": "30.000"}


# ------------------------------------------------------- 1. the linkage

print("\n--- the shared library ---")

ok("sr760 was found beside this repo", os.path.exists(LIB), LIB)
try:
    import ilc_bench                                 # noqa: E402
    ok("the path convention matches ilc_bench.find_spectrum_grab",
       os.path.normcase(ilc_bench.find_spectrum_grab(SIBLINGS))
       == os.path.normcase(LIB))
except ImportError:
    ok("ilc_bench not importable here, so the inlined convention stands alone",
       True, "pandas missing - run under Anaconda to cross-check it")
# Raises RuntimeError if run_protocol's duplicated span table has drifted from
# sr760.SPANS, which is the whole reason it keeps its own copy.
rp.check_span_table(sr)
ok("run_protocol's span table still agrees with sr760.SPANS", True)
ok("record_stats takes the averaging state",
   "averaged" in inspect.signature(sr.record_stats).parameters)
# fmt_hms exists in both files for the same reason the SPAN table does - the
# planner has to cost a session without importing sr760 - so it needs the same
# reconciliation check the span table gets.
for seconds in (0.4, 12.3, 59.9, 60, 102.6, 3599, 7000, 86399, float("nan")):
    ok(f"fmt_hms({seconds}) agrees with sr760's copy",
       rp.fmt_hms(seconds) == sr.fmt_hms(seconds),
       f"{rp.fmt_hms(seconds)} vs {sr.fmt_hms(seconds)}")
ok("readout_fault is available to the runner", hasattr(sr, "readout_fault"))
ok("Status knows whether it read both bytes",
   hasattr(sr.Status, "complete"))


# ------------------------------------- 2. the numbers that reach the splice

print("\n--- manifest_entry ---")

on = rp.manifest_entry("seg0.csv", SPEC, GOOD, NOTES, MSET, -30.0, 1.0, sr)
ok("an averaged run counts elapsed/T_rec", round(on["n_indep"]) == 29,
   f"n_indep {on['n_indep']:.1f}, rel_err {on['rel_err']:.3g}")
ok("the units the RIN maths reads V/rtHz off are carried",
   on["units"] == "Vrms/sqrtHz", on["units"])
ok("the pinned range is carried", on["range_dbv"] == -30.0)
ok("the filter id is carried - a segment without one cannot be spliced",
   on["filter_id"] == "none")

# Was: the same elapsed/T_rec count with AVGO off, which put 29 independent
# records and a 0.19 error bar in the manifest for a trace that is one record
# and a 1.0 bar. The analyzer goes on acquiring with averaging off; it just
# throws each record away and keeps the newest.
off = rp.manifest_entry("seg0.csv", SPEC, dict(GOOD, AVGO="0"), NOTES, MSET,
                        -30.0, 1.0, sr)
ok("an unaveraged run is worth one record", off["n_indep"] == 1.0,
   f"n_indep {off['n_indep']:.1f}")
ok("... so the bar the splice compares across is 1, not 0.19",
   off["rel_err"] == 1.0, f"the old model said {on['rel_err']:.3g}")

# A manifest entry is a plain dict on its way to JSON; nothing here may be a
# numpy scalar or load_segments gets something json.dump will not take.
import json                                          # noqa: E402
ok("the entry still serialises", isinstance(json.dumps(off), str))


# ------------------------------- 3. the faults the capture loop must raise

print("\n--- the fault list in the capture loop ---")

src = inspect.getsource(rp)

# These three are checked in the source rather than by running the capture
# loop, which needs an analyzer, a scope and a folder to write into. They are
# one-line safety checks that are easy to drop in a refactor and silent when
# they are gone, which is exactly the kind worth pinning down.
ok("a half-read status byte is treated as a fault",
   "status.complete" in src)
ok("the scale the trace is labelled on is checked",
   "sr.readout_fault(snap)" in src)
ok("record_stats is told whether the analyzer averaged anything",
   "averaged=sr.code_of(snap" in src)
ok("... and so is the one in manifest_entry",
   "averaged=sr760_mod.code_of(snap"
   in inspect.getsource(rp.manifest_entry))
ok("the runner still refuses to start on a bad averaging setting",
   "refusing to run" in src)


print("\n--- and they fire ---")

dropped = {k: v for k, v in GOOD.items() if k not in ("UNIT0", "DISP0")}
ok("a UNIT0 that could not be read back is a fault",
   sr.readout_fault(dropped) != "", sr.readout_fault(dropped)[:56])
ok("a healthy snapshot is not", sr.readout_fault(GOOD) == "")
ok("a half-read status is not complete",
   not sr.Status(0, None, 0.0).complete)
ok("... and does not describe itself as clean",
   sr.Status(0, None, 0.0).describe() != "no",
   sr.Status(0, None, 0.0).describe())
ok("an AVGO that could not be read stops the run",
   "could not be read" in sr.averaging_fault(
       {k: v for k, v in GOOD.items() if k != "AVGO"}))

print("\n--- the overlap goes in whole, and is checked ---")

# Was: code_of everywhere, which truncates - it reads an enum's index. OVLP
# 98.4375 went into the manifest as 98, and that manifest is what the RIN
# splice reads.
ok("value_of keeps a fractional overlap",
   sr.value_of({"OVLP": "98.4375"}, "OVLP") == 98.4375)
ok("code_of would have truncated it",
   sr.code_of({"OVLP": "98.4375"}, "OVLP") == 98)

deep = dict(GOOD, NAVG="400", OVLP="98.4375")
entry = rp.manifest_entry("s.csv", SPEC, deep, NOTES, MSET, -30.0, 1.0, sr)
ok("the manifest carries the overlap unrounded",
   entry["overlap_pct"] == 98.4375, str(entry["overlap_pct"]))
ok("... and manifest_entry reads it with value_of",
   "value_of(snap, \"OVLP\")" in inspect.getsource(rp.manifest_entry))

ok("the capture loop reads it that way too",
   'ovlp=sr.value_of(snap, "OVLP")' in src)
ok("... and carries the overlap fault", "sr.overlap_fault(snap" in src)

# The fault the runner will now raise on a span that reinstalled its default.
f = sr.overlap_fault(deep, 11)
ok("a reinstalled overlap is named for what it costs",
   "worth 7.23 independent" in f and "overstates the statistics 55x" in f,
   f[:64])
ok("... and blamed on the span that installed it",
   "this span's default" in f)
ok("the protocol preset's own OVLP 0 stays silent",
   sr.overlap_fault(dict(GOOD, NAVG="400", OVLP="0"), 11) == "")

print(f"\nAll {checks} checks passed.")
