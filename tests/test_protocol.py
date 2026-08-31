"""Offline check of run_protocol.py: the planner, the sets, and the manifest.

No instruments, and deliberately no instrument code either - importing
run_protocol must not drag in ilc_bench, pyvisa or sr760, because the whole
point of --dry-run is to cost a bench session before committing to one. This
file runs on the system interpreter, where none of those would import anyway.

    python tests/test_protocol.py
"""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_protocol as rp                            # noqa: E402
from eomilc import rin as R                          # noqa: E402

checks = 0


def ok(label, condition, detail=""):
    global checks
    checks += 1
    print(f"[{checks}] {'OK ' if condition else 'FAIL'} {label}"
          + (f"   {detail}" if detail else ""))
    if not condition:
        raise AssertionError(label + " " + detail)


# ------------------------------------------------------- 1. import hygiene
print("\n--- import hygiene ---")

for mod in ("ilc_bench", "pyvisa", "sr760", "pandas"):
    ok(f"importing run_protocol does not pull in {mod}",
       mod not in sys.modules,
       "" if mod not in sys.modules else "but it did")


# ------------------------------------------------------------ 2. the clock
print("\n--- the timing model ---")

ok("record_time is bins/span", np.isclose(rp.record_time(11), 400 / 390.0),
   f"{rp.record_time(11):.4f} s")
# The two figures the protocol was costed against.
ok("100 averages at the 390 Hz span is ~102 s",
   abs(100 * rp.record_time(11) - 102.6) < 0.5,
   f"{100 * rp.record_time(11):.1f} s")
ok("100 averages at the 24.4 Hz span is ~27 min",
   abs(100 * rp.record_time(7) / 60.0 - 27.3) < 0.5,
   f"{100 * rp.record_time(7) / 60:.1f} min")
ok("an unknown span code gives NaN, not a guess",
   not np.isfinite(rp.record_time(99)))

ok("fmt_hms seconds", rp.fmt_hms(12.3) == "12.3s", rp.fmt_hms(12.3))
ok("fmt_hms minutes", rp.fmt_hms(102.6) == "1m43s", rp.fmt_hms(102.6))
ok("fmt_hms hours", rp.fmt_hms(3 * 3600 + 4 * 60) == "3h04m",
   rp.fmt_hms(3 * 3600 + 4 * 60))


# --------------------------------------------------------- 3. the planner
print("\n--- planning ---")

c = rp.BUILTIN_SETS["C"]
plan = rp.plan_set(c)
ok("the plan has one entry per trace", len(plan.traces) == len(c.traces))
ok("total is autorange plus the traces",
   np.isclose(plan.total_s,
              plan.autorange_s + sum(t.total_s for t in plan.traces)))
ok("a per-trace range policy needs no set autorange", plan.autorange_s == 0.0)
ok("averaging time is navg * T_rec",
   np.isclose(plan.traces[0].average_s,
              plan.traces[0].navg * rp.record_time(plan.traces[0].span)))
ok("settle is the record-length multiple",
   np.isclose(plan.traces[0].settle_s, c.settle_recs * plan.traces[0].t_rec))

a1 = rp.plan_set(rp.BUILTIN_SETS["A1"])
ok("a worst-case set budgets one autorange",
   a1.autorange_s == rp.AUTORANGE_S, f"{a1.autorange_s} s")
ok("prompts are budgeted for",
   all(t.prompt_s == rp.PROMPT_S for t in a1.traces))

half = rp.plan_set(c, navg_override=50)
ok("--navg halves the averaging time",
   np.isclose(half.traces[0].average_s, plan.traces[0].average_s / 2),
   f"{half.traces[0].average_s:.1f} vs {plan.traces[0].average_s:.1f}")
slow = rp.plan_set(c, settle_recs=20)
ok("--settle-recs scales the settle",
   np.isclose(slow.traces[0].settle_s, 4 * plan.traces[0].settle_s))

ok("report() names every trace",
   all(t.label in plan.report() for t in plan.traces))
ok("report() carries the total", rp.fmt_hms(plan.total_s) in plan.report())


# ------------------------------------------------------- 4. the set definitions
print("\n--- the built-in sets ---")

ok("all five sets are defined",
   set(rp.BUILTIN_SETS) == {"A1", "A2", "A3", "C1", "C"},
   ", ".join(rp.BUILTIN_SETS))

a1s = rp.BUILTIN_SETS["A1"]
ok("A1 is four resistors on one shared range",
   len(a1s.traces) == 4 and a1s.range_policy == "worst-case"
   and all(t.range_dbv is None for t in a1s.traces))
ok("A1 prompts for each resistor swap",
   all(t.prompt for t in a1s.traces))
ok("A1 stays below the 100k rolloff",
   all(rp.span_hz(t.span) <= 2000 for t in a1s.traces),
   f"top span {max(rp.span_hz(t.span) for t in a1s.traces):g} Hz")

a2 = rp.BUILTIN_SETS["A2"]
steps = np.diff([t.range_dbv for t in a2.traces])
ok("A2 sweeps every range on the 2 dB grid",
   bool(np.allclose(steps, rp.RANGE_STEP_DB)), f"{len(a2.traces)} ranges")
ok("A2 spans -60 to 0 dBV",
   a2.traces[0].range_dbv == -60 and a2.traces[-1].range_dbv == 0)
ok("A2 labels are unique and file-safe",
   len({t.name() for t in a2.traces}) == len(a2.traces)
   and all(" " not in t.name() for t in a2.traces))

a3 = rp.BUILTIN_SETS["A3"]
ok("A3 is one resistor across three spans",
   len({t.span for t in a3.traces}) == 3)

c1 = rp.BUILTIN_SETS["C1"]
pairs = {}
for t in c1.traces:
    pairs.setdefault(t.label.replace("_down2", ""), []).append(t)
ok("C1 pairs every band with itself", all(len(v) == 2 for v in pairs.values()),
   f"{len(pairs)} pairs")
for base, (hi, lo) in pairs.items():
    ok(f"C1 {base} steps down two notches",
       np.isclose(hi.range_dbv - lo.range_dbv, 2 * rp.RANGE_STEP_DB),
       f"{hi.range_dbv:g} -> {lo.range_dbv:g} dBV")
    ok(f"C1 {base} keeps the band fixed",
       hi.span == lo.span and hi.strf == lo.strf)

ok("C is four spans, four filters, four ranges",
   len(c.traces) == 4
   and len({t.span for t in c.traces}) == 4
   and len({t.filter_id for t in c.traces}) == 4
   and len({t.range_dbv for t in c.traces}) == 4)
bands = [t.band for t in c.traces]
ok("C segments are nested from 0 Hz, so every pair overlaps",
   all(b[0] == 0.0 for b in bands) and
   all(bands[i][1] < bands[i + 1][1] for i in range(len(bands) - 1)),
   " ".join(f"{b[0]:g}-{b[1]:g}" for b in bands))
ok("C reaches the SR760's 100 kHz ceiling",
   np.isclose(bands[-1][1], 100e3))
ok("every trace in every set names its filter",
   all(t.filter_id for s in rp.BUILTIN_SETS.values() for t in s.traces))


# ------------------------------------------------------ 5. manifest round trip
print("\n--- manifest -> rin.Segment ---")

tmp = tempfile.mkdtemp(prefix="protocol-test-")
try:
    # Two overlapping synthetic segments written the way save_trace writes
    # them: an AMPLITUDE density in V/rtHz, one header line, comma separated.
    def truth(x):
        return 1e-16 * (1.0 + (np.maximum(x, 1.0) / 100.0) ** -1.5)

    entries = []
    for label, f, rng, filt, suspect in (
            ("S1", np.linspace(1.0, 390.0, 400), -30.0, "none", False),
            ("S2", np.linspace(1.0, 3125.0, 400), -40.0, "hp30", False),
            ("S3", np.linspace(1.0, 3125.0, 400), -40.0, "hp30", True)):
        name = f"{label}.csv"
        np.savetxt(os.path.join(tmp, name),
                   np.column_stack([f, np.sqrt(truth(f))]),
                   delimiter=",", comments="",
                   header="Frequency (Hz),Vrms_sqrtHz")
        entries.append({"path": name, "label": label, "span_code": 11,
                        "range_dbv": rng, "filter_id": filt, "v_dc": 1.85,
                        "n_indep": 100.0, "rel_err": 0.1,
                        "overload": "no", "suspect": suspect,
                        "note": "synthetic"})
    man = os.path.join(tmp, "m.json")
    with open(man, "w", encoding="utf-8") as fh:
        json.dump({"set": "T", "segments": entries}, fh)

    segs, skipped, loaded = rp.load_segments(man)
    ok("suspect traces are dropped by default",
       len(segs) == 2 and skipped == ["S3"], f"skipped {skipped}")
    ok("segments come back as rin.Segment",
       all(isinstance(s, R.Segment) for s in segs))
    ok("the range survives into the segment",
       [s.range_dbv for s in segs] == [-30.0, -40.0])
    # The whole point: V/rtHz on disk becomes V^2/Hz in the Segment.
    ok("the CSV amplitude density is squared into a power density",
       bool(np.allclose(segs[0].psd, truth(segs[0].f), rtol=1e-9)),
       f"max rel {np.max(np.abs(segs[0].psd / truth(segs[0].f) - 1)):.2e}")

    sp = R.splice_segments(segs)
    ok("the manifest feeds splice_segments directly", len(sp.joins) == 1)
    ok("two consistent segments agree across the join",
       abs(sp.joins[0].median_db) < 0.05 and sp.ok,
       sp.report().splitlines()[-1].strip())

    all_segs, _, _ = rp.load_segments(man, skip_suspect=False)
    ok("suspect traces can be loaded deliberately", len(all_segs) == 3)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------ 6. the span table agreement
print("\n--- span table ---")


class FakeSR:
    SPANS = [("x", hz) for hz in rp.SPAN_HZ]
    N_BINS = rp.N_BINS


rp.check_span_table(FakeSR)
ok("a matching span table passes", True)

class DriftedSR(FakeSR):
    SPANS = [("x", hz) for hz in (1.0,) + rp.SPAN_HZ[1:]]

try:
    rp.check_span_table(DriftedSR)
    ok("a drifted span table is caught", False)
except RuntimeError as exc:
    ok("a drifted span table is caught", "disagrees" in str(exc))

class DriftedBins(FakeSR):
    N_BINS = 512

try:
    rp.check_span_table(DriftedBins)
    ok("a changed bin count is caught", False)
except RuntimeError:
    ok("a changed bin count is caught", True)

print(f"\nAll {checks} checks passed.")
