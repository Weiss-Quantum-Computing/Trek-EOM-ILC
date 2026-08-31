#!/usr/bin/env python3
"""Drive a declarative measurement set on the SR760, for the RIN validation.

    python run_protocol.py --list
    python run_protocol.py --set A1 --dry-run
    python run_protocol.py --set C --outdir data/rin --v-dc 1.85

Each set is a name, a range policy and an ordered list of traces. The runner
ranges once, pins, and then holds that range for every trace in the set,
verifying it before each save - because the whole content of these sets is the
difference between their traces, and a range that moved is a step in the noise
floor that no file records.

Files are written through the sr760 library's own `write_csv` and
`metadata_text`, so a trace taken here is indistinguishable from one taken with
the Spectrum Grab panel. Alongside them each set writes one manifest.json whose
entries load straight into `eomilc.rin.splice_segments` via `load_segments`.

Why this lives in EOM-ILC and not in Spectrum-grab: it is campaign-specific, its
output feeds eomilc/rin.py, and it needs ilc_bench._shared_rm. The C-phase sets
want the MSO-X as well - for V_DC and for the servo bump above the SR760's
100 kHz ceiling - and a second ResourceManager half-loads on this machine and
then fails every open_resource with VI_ERROR_ALLOC.

--dry-run prints the whole plan and a wall-clock estimate without touching
hardware, and imports no instrument code at all, so the planner is exercised by
tests on the bare system interpreter.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from dataclasses import dataclass

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from eomilc import rin                                          # noqa: E402

# The SR760's span table, code -> Hz. Duplicated from sr760.SPANS deliberately:
# the planner has to work with no instrument code importable at all, and this is
# a constant of the instrument rather than a shared implementation.
# `check_span_table` reconciles the two whenever sr760 IS importable.
#
# Every span is the widest halved, exactly. These used to be the manual's
# rounded display figures - 390 for 390.625, 1560 for 1562.5 - which put a
# 0.16 % error into every record length and everything costed from one. The
# default overlaps measured on 31 Aug 2026 are exactly 93.75 % at code 13 and
# 98.4375 % at code 11, which only comes out of the exact spans.
SPAN_HZ = tuple(100000.0 / 2.0 ** (19 - i) for i in range(20))
N_BINS = 400

# Input range step. The SR760 sets IRNG in dBV on a 2 dB grid, so "two notches
# down" is 4 dB less sensitive. Set A2 measures this directly - it maps the
# floor against every range - so if the grid turns out to be something else,
# A2's output is what says so.
RANGE_STEP_DB = 2.0

# Rough per-trace overheads, seconds. Only the plan uses these; nothing is
# derived from them at run time.
TRANSFER_S = 1.5              # SPEB? binary dump plus the settings read-back
AUTORANGE_S = 15.0            # once per set
PROMPT_S = 20.0               # a pause for a human to change something


def span_hz(code):
    return SPAN_HZ[code] if 0 <= code < len(SPAN_HZ) else float("nan")


def record_time(code, n_bins=N_BINS):
    """The analyser's record length, bins / span."""
    hz = span_hz(code)
    return n_bins / hz if np.isfinite(hz) and hz > 0 else float("nan")


# ------------------------------------------------------------------ the sets

@dataclass
class TraceSpec:
    """One trace in a set.

    `filter_id` names the analog pre-filter in front of the analyser for this
    trace, and is carried all the way into the manifest: it is the thing
    `rin.filter_response` measures and `rin.splice_segments` corrects for, so a
    trace whose filter is not recorded cannot be spliced.

    `range_dbv` overrides the set's pinned range for this trace alone - the
    range-map and range-step sets are entirely about moving it on purpose.
    """
    span: int
    strf: float = 0.0
    label: str = ""
    filter_id: str = "none"
    navg: int | None = None            # None: whatever the preset says
    range_dbv: float | None = None     # None: the set's pinned range
    prompt: str = ""                   # pause and say this before the trace
    note: str = ""

    @property
    def band(self):
        return self.strf, self.strf + span_hz(self.span)

    def name(self):
        return self.label or f"span{self.span}_strf{self.strf:g}"


@dataclass
class MeasurementSet:
    """A named set of traces that share one pinned input range."""
    name: str
    title: str                          # the file-name stem, like the GUI's
    purpose: str
    traces: list
    range_policy: str = "worst-case"    # worst-case | per-trace | fixed
    range_trace: int = 0                # which trace to autorange on
    range_dbv: float | None = None      # for range_policy == "fixed"
    navg: int = 100
    settle_recs: float = 5.0
    v_dc: float | None = None           # supplied, or read from the scope

    def navg_for(self, t: TraceSpec) -> int:
        return int(t.navg if t.navg is not None else self.navg)


def _c_segments():
    """The C-phase segment set: four spans, four filters, four pinned ranges.

    Every segment starts at 0 Hz, so the bands are nested rather than abutting
    and each neighbouring pair overlaps over the whole of the narrower one.
    That is deliberate: `splice_segments` compares neighbours across their
    overlap and the disagreement there is the error signal for the filter
    corrections, so a generous overlap is a better measurement, not waste.

    The pre-filters are high-pass because the suppressed spectrum falls about
    50 dB from DC to 100 kHz: on the wide spans the low-frequency content is
    what eats the input range, and taking it out is what lets the range be
    pinned somewhere sensitive enough to see the high-frequency floor. The
    wide spans get more averages because their records are short - 500 averages
    at the 12.5 kHz span costs 16 s.
    """
    return [
        TraceSpec(span=11, strf=0.0, label="S1_dc_390", filter_id="none",
                  navg=100, range_dbv=-30.0,
                  note="0-390 Hz, unfiltered; the low-frequency end"),
        TraceSpec(span=14, strf=0.0, label="S2_dc_3k", filter_id="hp30",
                  navg=200, range_dbv=-40.0,
                  note="0-3.125 kHz behind a 30 Hz high-pass"),
        TraceSpec(span=16, strf=0.0, label="S3_dc_12k", filter_id="hp300",
                  navg=500, range_dbv=-50.0,
                  note="0-12.5 kHz behind a 300 Hz high-pass"),
        TraceSpec(span=19, strf=0.0, label="S4_dc_100k", filter_id="hp3k",
                  navg=500, range_dbv=-56.0,
                  note="0-100 kHz behind a 3 kHz high-pass; the servo bump "
                       "above this needs the scope"),
    ]


def _a2_ranges(lo=-60.0, hi=0.0, step=RANGE_STEP_DB):
    n = int(round((hi - lo) / step)) + 1
    return [lo + i * step for i in range(n)]


BUILTIN_SETS = {
    "A1": MeasurementSet(
        name="A1", title="A1_johnson", range_policy="worst-case",
        purpose="Johnson noise against four resistors, one shared range. The "
                "fitted 4kT slope calibrates the whole voltage-noise scale; "
                "feed the band-averaged results to rin.johnson_check.",
        navg=100,
        traces=[
            TraceSpec(span=11, label="R50", filter_id="none",
                      prompt="Fit the 50 ohm termination",
                      note="50 ohm - pins the amplifier floor"),
            TraceSpec(span=11, label="R2k", filter_id="none",
                      prompt="Fit the 2 kohm resistor", note="2 kohm"),
            TraceSpec(span=11, label="R10k", filter_id="none",
                      prompt="Fit the 10 kohm resistor", note="10 kohm"),
            TraceSpec(span=11, label="R100k", filter_id="none",
                      prompt="Fit the 100 kohm resistor",
                      note="100 kohm - rolls off above ~2 kHz against the "
                           "input and cable capacitance, so band-average it "
                           "below that before fitting"),
        ]),
    "A2": MeasurementSet(
        name="A2", title="A2_rangemap", range_policy="per-trace",
        purpose="Floor against every input range on a 50 ohm termination. "
                "Maps where the analyser's own floor sits per range, which is "
                "what says whether a segment's pinned range was sensitive "
                "enough - and confirms the 2 dB range grid.",
        navg=20,
        traces=[TraceSpec(span=11, label=f"rng{r:+.0f}".replace("+", "p")
                                         .replace("-", "m"),
                          filter_id="none", range_dbv=r,
                          note=f"input range {r:g} dBV")
                for r in _a2_ranges()]),
    "A3": MeasurementSet(
        name="A3", title="A3_spanindep", range_policy="worst-case",
        purpose="One resistor across three spans. A power spectral density is "
                "span-independent by construction, so any span dependence here "
                "is an artefact of the analyser or of the record length - and "
                "would forge a false slope in a spliced trace.",
        navg=100,
        traces=[
            TraceSpec(span=9, label="span9_97Hz", filter_id="none",
                      note="97.5 Hz span"),
            TraceSpec(span=11, label="span11_390Hz", filter_id="none",
                      note="390 Hz span"),
            TraceSpec(span=13, label="span13_1k56", filter_id="none",
                      note="1.56 kHz span"),
        ]),
    "C1": MeasurementSet(
        name="C1", title="C1_rangestep", range_policy="per-trace",
        purpose="Range-step pairs: each band taken at its working range and "
                "again two notches down. The two must agree; where they do "
                "not, the range calibration is what a spliced segment "
                "disagreement is really measuring.",
        navg=100,
        traces=[t for base in (
                    TraceSpec(span=11, label="S1", filter_id="none",
                              range_dbv=-30.0),
                    TraceSpec(span=14, label="S2", filter_id="hp30",
                              range_dbv=-40.0))
                for t in (base,
                          TraceSpec(span=base.span, strf=base.strf,
                                    label=base.label + "_down2",
                                    filter_id=base.filter_id,
                                    range_dbv=base.range_dbv
                                              - 2 * RANGE_STEP_DB,
                                    note="same band, two range notches down"))]),
    "C": MeasurementSet(
        name="C", title="C_segments", range_policy="per-trace",
        purpose="The RIN segment set: four spans, four pre-filters, four "
                "pinned ranges, nested from 0 Hz so every neighbouring pair "
                "overlaps. Splice with rin.splice_segments and read the join "
                "disagreements as the error budget.",
        navg=100, traces=_c_segments()),
}


# ------------------------------------------------------------------ planning

@dataclass
class PlannedTrace:
    label: str
    span: int
    strf: float
    band: tuple
    filter_id: str
    navg: int
    range_dbv: float | None
    t_rec: float
    settle_s: float
    average_s: float
    transfer_s: float
    prompt_s: float

    @property
    def total_s(self) -> float:
        return (self.settle_s + self.average_s + self.transfer_s
                + self.prompt_s)


@dataclass
class Plan:
    set_name: str
    purpose: str
    traces: list
    autorange_s: float

    @property
    def total_s(self) -> float:
        return self.autorange_s + sum(t.total_s for t in self.traces)

    def report(self) -> str:
        out = [f"{self.set_name}: {len(self.traces)} traces, "
               f"{fmt_hms(self.total_s)} estimated",
               f"  {self.purpose}", ""]
        head = (f"  {'trace':<16} {'span':>5} {'band (Hz)':>18} "
                f"{'filt':>6} {'navg':>5} {'range':>7} {'T_rec':>8} "
                f"{'settle':>8} {'average':>9} {'total':>9}")
        out.append(head)
        out.append("  " + "-" * (len(head) - 2))
        for t in self.traces:
            rng = "auto" if t.range_dbv is None else f"{t.range_dbv:g}"
            out.append(
                f"  {t.label:<16} {t.span:>5} "
                f"{t.band[0]:>8.4g}-{t.band[1]:<9.4g} {t.filter_id:>6} "
                f"{t.navg:>5} {rng:>7} {t.t_rec:>8.4g} "
                f"{fmt_hms(t.settle_s):>8} {fmt_hms(t.average_s):>9} "
                f"{fmt_hms(t.total_s):>9}")
        out.append("")
        out.append(f"  autorange {fmt_hms(self.autorange_s)}  +  traces "
                   f"{fmt_hms(self.total_s - self.autorange_s)}  =  "
                   f"{fmt_hms(self.total_s)}")
        return "\n".join(out)


def fmt_hms(seconds):
    """Seconds as the shortest honest thing to read."""
    if not np.isfinite(seconds):
        return "?"
    s = int(round(seconds))
    if s < 60:
        return f"{seconds:.1f}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def plan_set(mset: MeasurementSet, navg_override=None, settle_recs=None):
    """Everything the set will do and how long it will take. No hardware.

    The averaging estimate is NAVG * T_rec exactly, which is only true because
    the protocol preset sets OVLP 0. With overlap it would be an underestimate
    and the statistics would be an overestimate - the same trade in both
    directions. That is the point of running with no overlap: the plan, the
    clock and record_stats all agree.
    """
    recs = mset.settle_recs if settle_recs is None else float(settle_recs)
    planned = []
    for t in mset.traces:
        navg = int(navg_override) if navg_override else mset.navg_for(t)
        t_rec = record_time(t.span)
        planned.append(PlannedTrace(
            label=t.name(), span=t.span, strf=t.strf, band=t.band,
            filter_id=t.filter_id, navg=navg,
            range_dbv=pinned_range_for(mset, t), t_rec=t_rec,
            settle_s=recs * t_rec, average_s=navg * t_rec,
            transfer_s=TRANSFER_S, prompt_s=PROMPT_S if t.prompt else 0.0))
    autorange = AUTORANGE_S if mset.range_policy == "worst-case" else 0.0
    return Plan(set_name=mset.name, purpose=mset.purpose, traces=planned,
                autorange_s=autorange)


def pinned_range_for(mset: MeasurementSet, t: TraceSpec):
    """The range this trace will be pinned to, or None for 'autorange first'."""
    if t.range_dbv is not None:
        return t.range_dbv
    if mset.range_policy == "fixed":
        return mset.range_dbv
    return None                      # worst-case: decided by the autorange


def check_span_table(sr760_mod):
    """Reconcile this module's printed span table with the library's.

    The planner cannot import sr760 - it has to run with no instrument code
    present - so the table is written out here as well. This is what stops the
    two drifting: it is called on every real run, where sr760 IS imported.
    """
    theirs = tuple(hz for _label, hz in sr760_mod.SPANS)
    if theirs != SPAN_HZ:
        raise RuntimeError(
            "the span table in run_protocol.py disagrees with sr760.SPANS:\n"
            f"  here : {SPAN_HZ}\n  sr760: {theirs}")
    if sr760_mod.N_BINS != N_BINS:
        raise RuntimeError(f"N_BINS {sr760_mod.N_BINS} != {N_BINS}")


# ------------------------------------------------------------------ manifest

def manifest_entry(path, spec: TraceSpec, snap, notes, mset: MeasurementSet,
                   pinned, v_dc, sr760_mod):
    """One segment, in the shape `load_segments` turns into a rin.Segment.

    Deliberately the fields of `rin.Segment` plus the provenance a splice needs
    to be argued about - the pinned range and the filter id are the first two
    things anyone asks when two segments disagree across their overlap.
    """
    # `averaged` matters here more than anywhere: n_indep and rel_err go into
    # the manifest, and load_segments carries them into the RIN splice as the
    # error bar two segments are compared across. The elapsed/T_rec count
    # assumes the analyzer averaged what it acquired; with AVGO off it keeps
    # only the newest record, so the run buys one record however long it ran.
    stats = sr760_mod.record_stats(
        sr760_mod.code_of(snap, "SPAN"), float(notes.get("measure time (s)", 0)),
        navg=sr760_mod.code_of(snap, "NAVG"),
        ovlp=sr760_mod.code_of(snap, "OVLP"),
        averaged=sr760_mod.code_of(snap, "AVGO", 0) == 1)
    return {
        "path": os.path.basename(path),
        "label": spec.name(),
        "set": mset.name,
        "span_code": spec.span,
        "span_hz": span_hz(spec.span),
        "strf_hz": spec.strf,
        "range_dbv": pinned,
        "filter_id": spec.filter_id,
        "v_dc": v_dc,
        "units": sr760_mod.trace_units(snap),
        "navg": sr760_mod.code_of(snap, "NAVG"),
        "overlap_pct": sr760_mod.code_of(snap, "OVLP"),
        "t_rec_s": stats["t_rec_s"],
        "n_indep": stats["n_indep"],
        "rel_err": stats["rel_err"],
        "elapsed_s": stats["elapsed_s"],
        "overload": notes.get("overload", "unread"),
        "trace_quality": notes.get("trace quality", ""),
        "suspect": str(notes.get("trace quality", "")).startswith("SUSPECT"),
        "note": spec.note,
    }


def load_segments(manifest_path, skip_suspect=True):
    """Read a manifest back as `rin.Segment`s, ready for `splice_segments`.

    **The CSV holds an amplitude density in V/rtHz and rin.Segment wants a power
    density in V^2/Hz**, so the column is squared here. That conversion has to
    happen exactly once, and this is where: doing it again downstream is the
    factor-of-two-in-dB error that rin.py's docstrings keep warning about.

    Suspect traces are dropped by default. A trace whose range slipped or whose
    front end overloaded is not a worse measurement, it is a different one, and
    splicing it against its neighbours measures the fault rather than the noise.
    """
    with open(manifest_path, encoding="utf-8") as fh:
        man = json.load(fh)
    root = os.path.dirname(os.path.abspath(manifest_path))
    segments, skipped = [], []
    for entry in man["segments"]:
        if skip_suspect and entry.get("suspect"):
            skipped.append(entry["label"])
            continue
        data = np.loadtxt(os.path.join(root, entry["path"]), delimiter=",",
                          skiprows=1)
        f, asd = data[:, 0], data[:, 1]
        segments.append(rin.Segment(
            f=f, psd=asd ** 2, label=entry["label"],
            range_dbv=entry.get("range_dbv"),
            note=f"{entry.get('filter_id', '')} | {entry.get('note', '')}"))
    return segments, skipped, man


# ------------------------------------------------------------------ the run

def open_scope(ilc_bench, siblings, addr=None, log=print):
    """The MSO-X, on the SAME ResourceManager as the analyser.

    This is the pairing the shared RM exists for: a second ResourceManager
    half-loads on this machine and then fails every open_resource with
    VI_ERROR_ALLOC, and the failure looks like 'no instrument found' rather
    than like a VISA problem.
    """
    path = ilc_bench.find_scope_grab(siblings)
    if not os.path.exists(path):
        raise SystemExit(f"scope_grab.py not found at {path}\nClone "
                         f"Weiss-Quantum-Computing/keysight-scope-grab beside "
                         f"this repo, or set SCOPE_GRAB to the file.")
    mod = ilc_bench.load_module(path, "scope_grab")
    scope = ilc_bench.make_scope(mod)
    log("Connecting to the MSO-X for V_DC...")
    scope.connect(addr)
    log(f"  {scope.idn}")
    return scope


def read_vdc(ilc_bench, scope, channel, coarse_scale, log=print):
    """V_DC with the detail, or (None, {}) if the scope refuses it.

    A failure here must not stop a measurement set: the traces are still worth
    having, and V_DC can be supplied afterwards with --v-dc. What it must not
    do is guess.
    """
    try:
        return ilc_bench.measure_vdc(scope, channel,
                                     coarse_scale=coarse_scale, log=log)
    except Exception as exc:
        log(f"    V_DC could not be measured: {exc}")
        return None, {"error": str(exc)}


def run_set(mset: MeasurementSet, outdir, addr=None, navg_override=None,
            settle_recs=None, v_dc=None, scope_ch=None, scope_addr=None,
            vdc_scale=1.0, confirm=input, log=print):
    """Take the whole set. Needs the instruments."""
    import ilc_bench                                        # noqa: E402

    siblings = os.path.dirname(HERE)        # the folder the repos sit in
    lib = ilc_bench.find_spectrum_grab(siblings)
    if not os.path.exists(lib):
        raise SystemExit(f"sr760.py not found at {lib}\nClone "
                         f"Weiss-Quantum-Computing/Spectrum-Grab beside this "
                         f"repo, or set SPECTRUM_GRAB to the file.")
    sr = ilc_bench.load_module(lib, "sr760")
    check_span_table(sr)

    an = ilc_bench.make_analyzer(sr, addr=addr)
    log("Connecting to the SR760...")
    log(f"  {an.connect(addr)}")
    log(f"  {an.addr}")

    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    recs = mset.settle_recs if settle_recs is None else float(settle_recs)

    log(f"Staging the protocol preset ({len(sr.PRESETS['protocol'])} settings)")
    an.apply(sr.PRESETS["protocol"], log=lambda m: log(m))
    snap = an.read_all_settings(retry_all=True, log=lambda m: log(m))
    bad = sr.averaging_fault(snap)
    if bad:
        raise SystemExit(f"refusing to run: {bad}. Fix the averaging before "
                         f"taking noise data.")

    # V_DC, if a scope channel was named. Measured before the traces and again
    # after: RIN is S_V / V_DC^2, so the level squares straight into every
    # answer, and a level that moved during the set moves all of them.
    scope = None
    vdc_start = vdc_end = None
    vdc_detail = {}
    if v_dc is None and scope_ch is not None:
        scope = open_scope(ilc_bench, siblings, addr=scope_addr, log=log)
        vdc_start, vdc_detail = read_vdc(ilc_bench, scope, scope_ch,
                                         vdc_scale, log=log)
        v_dc = vdc_start
    elif v_dc is not None and scope_ch is not None:
        log(f"--v-dc {v_dc:g} given, so the scope is not consulted.")

    entries = []
    pinned = None
    try:
        for i, spec in enumerate(mset.traces):
            if spec.prompt:
                confirm(f"\n>>> {spec.prompt}, then press Enter "
                        f"(Ctrl-C to stop): ")
            an.put(f"SPAN {spec.span}")
            an.put(f"STRF {spec.strf:.10g}")

            want = pinned_range_for(mset, spec)
            if want is not None:
                an.pin_range(want)
                pinned = want
            elif pinned is None:
                log(f"  auto-ranging on {spec.name()} for the whole set...")
                rng, over, polls = an.autorange(AUTORANGE_S)
                log(f"    settled at {rng} dBV (overload on {over}/{polls})")
                found = an.input_range()
                an.pin_range(found)
                pinned = an.input_range()
            else:
                an.pin_range(pinned)

            navg = int(navg_override) if navg_override else mset.navg_for(spec)
            an.put(an.command("NAVG", str(navg)))
            an.settle(recs, spec.span, log=lambda m: log(m))

            log(f"[{i + 1}/{len(mset.traces)}] {spec.name()}: span {spec.span}, "
                f"{navg} averages, range {pinned:g} dBV, filter "
                f"{spec.filter_id}")
            notes = {"measurement set": mset.name,
                     "filter id": spec.filter_id,
                     "segment note": spec.note}
            # The timeout is generous against the plan rather than a fixed
            # number: 100 averages at the 24.4 Hz span is 27 minutes, and a
            # timeout shorter than the measurement reads the trace half built.
            t0 = time.perf_counter()
            an.start()
            state = an.wait_done(max(60.0, 4 * navg * record_time(spec.span)))
            elapsed = time.perf_counter() - t0
            status = an.refresh_status()
            notes["measure time (s)"] = f"{elapsed:.3f}"
            notes["measurement"] = state
            notes["overload"] = status.describe()

            an.autoscale()
            snap = an.read_all_settings()
            hold, ok = sr.hold_notes(pinned, snap, set_name=mset.name)
            notes.update(hold)
            faults = [] if ok else [hold["trace quality"]
                                    .removeprefix("SUSPECT: ")]
            if status.overloaded:
                faults.append("overload flagged during the run")
            elif not status.complete:
                # Half a status byte is not a clean one: an ERRS that came back
                # clear says nothing about the FFT overload in FFTS. Same
                # standard hold_notes holds the range to - unverified and
                # verified are different claims, and only one of them belongs
                # on a segment a splice rests on.
                silent = " and ".join(name for name, value
                                      in (("ERRS", status.errs),
                                          ("FFTS", status.ffts))
                                      if value is None)
                faults.append(f"overload unverified: {silent} did not answer")
            # Which scale the trace is labelled on, and whether that was read or
            # assumed. An assumed UNIT0 is a 160 dB assumption, and the units go
            # into the manifest that the RIN maths reads V/rtHz off.
            bad_read = sr.readout_fault(snap)
            if bad_read:
                faults.append(bad_read)
            avg_bad = sr.averaging_fault(snap)
            if avg_bad:
                faults.append(avg_bad)
            notes["trace quality"] = ("SUSPECT: " + "; ".join(faults) if faults
                                      else hold.get("trace quality", "clean"))
            if faults:
                log(f"    *** {notes['trace quality']} ***")
            notes.update(sr.stats_notes(sr.record_stats(
                sr.code_of(snap, "SPAN"), elapsed,
                navg=sr.code_of(snap, "NAVG"),
                ovlp=sr.code_of(snap, "OVLP"),
                averaged=sr.code_of(snap, "AVGO", 0) == 1)))

            freqs, amps, used_binary = an.trace(snap=snap, log=lambda m: log(m))
            notes["transfer"] = ("binary dump (SPEB?)" if used_binary
                                 else "bin by bin (BVAL?/SPEC?)")
            path = save_trace(sr, an, outdir, mset, spec, stamp, freqs, amps,
                              snap, notes, log=log)
            entries.append(manifest_entry(path, spec, snap, notes, mset,
                                          pinned, v_dc or mset.v_dc, sr))
    except KeyboardInterrupt:
        log("\nStopped. Writing the manifest for the traces that completed.")
    finally:
        an.close()

    drift_pct = None
    if scope is not None:
        try:
            vdc_end, end_detail = read_vdc(ilc_bench, scope, scope_ch,
                                           vdc_scale, log=log)
            vdc_detail = {"start": vdc_detail, "end": end_detail}
            if vdc_start and vdc_end:
                drift_pct = 100.0 * (vdc_end / vdc_start - 1.0)
                # RIN goes as 1/V_DC^2, so the level's drift doubles into it.
                log(f"  V_DC drifted {drift_pct:+.2f}% across the set "
                    f"({vdc_start:.6g} -> {vdc_end:.6g} V), which is "
                    f"{2 * drift_pct:+.2f}% on every RIN in it.")
                if abs(drift_pct) > 2.0:
                    log("  *** That is more than 2%: the light level was not "
                        "stable enough to treat one V_DC as covering the whole "
                        "set. ***")
        finally:
            scope.close()

    for entry in entries:
        entry["v_dc_start"] = vdc_start
        entry["v_dc_end"] = vdc_end
        entry["v_dc_drift_pct"] = drift_pct
        if entry.get("v_dc") is None:
            entry["v_dc"] = vdc_start

    man_path = write_manifest(outdir, mset, entries, stamp, pinned, recs,
                              vdc_detail=vdc_detail)
    log(f"\n{len(entries)} trace(s); manifest {man_path}")
    return man_path, entries


def save_trace(sr, an, outdir, mset, spec, stamp, freqs, amps, snap, notes,
               log=print):
    """CSV + metadata, through the library's own writers.

    Named the way the panel names its captures, so the two are
    indistinguishable in a folder and the same analysis reads both.
    """
    code = sr.code_of(snap, "SPAN")
    start = float(freqs[0])
    ylabel = sr.trace_units(snap)
    parts = [sr.safe_name(mset.title), sr.safe_name(spec.name())]
    if code is not None:
        parts.append(f"span{code}")
    if start:
        parts.append(f"strf{start:g}Hz")
    parts += [f"{round(float(np.max(freqs)))}Hz", stamp]
    base = sr.unique_base(outdir, "_".join(parts), [".csv", ".txt"])
    sr.write_csv(base + ".csv", freqs, amps, ylabel)
    extra = {
        "span": (f"{code} - {sr.SPANS[code][0]}"
                 if code is not None and 0 <= code < len(sr.SPANS) else "?"),
        "start frequency (Hz)": f"{start:g}",
        "stop frequency (Hz)": f"{float(freqs[-1]):g}",
        "bins": str(len(freqs)),
        "trace units": ylabel,
    }
    extra.update(notes)
    with open(base + ".txt", "w", encoding="utf-8") as fh:
        fh.write(sr.metadata_text(an, snap, extra, an.command))
    log(f"    {os.path.basename(base)}.csv / .txt")
    return base + ".csv"


def write_manifest(outdir, mset, entries, stamp, pinned, recs, vdc_detail=None):
    path = os.path.join(outdir, f"{mset.title}_{stamp}_manifest.json")
    n = 1
    while os.path.exists(path):
        path = os.path.join(outdir, f"{mset.title}_{stamp}_manifest_{n}.json")
        n += 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "set": mset.name,
            "title": mset.title,
            "purpose": mset.purpose,
            "written": datetime.datetime.now().isoformat(timespec="seconds"),
            "range_policy": mset.range_policy,
            "pinned_range_dbv": pinned,
            "settle_record_lengths": recs,
            # How V_DC was arrived at, kept because RIN divides by its square
            # and "where did this number come from" is the first question of
            # any RIN that looks wrong.
            "v_dc_measurement": vdc_detail or {},
            "segments": entries,
        }, fh, indent=2)
    return path


# ------------------------------------------------------------------ cli

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", dest="set_name", help="which measurement set")
    ap.add_argument("--list", action="store_true", help="list the sets")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and the wall-clock estimate; touches "
                         "no hardware and imports no instrument code")
    ap.add_argument("--outdir", default=os.path.join(HERE, "run", "protocol"))
    ap.add_argument("--addr", default=None, help="VISA address of the SR760")
    ap.add_argument("--navg", type=int, default=None,
                    help="override every trace's average count")
    ap.add_argument("--settle-recs", type=float, default=None,
                    help="settle in record lengths (default per set)")
    ap.add_argument("--v-dc", type=float, default=None,
                    help="photodiode DC level in volts, written into the "
                         "manifest so rin.rin() has something to divide by. "
                         "Overrides --scope-ch: given this, the scope is not "
                         "consulted.")
    ap.add_argument("--scope-ch", type=int, default=None,
                    help="MSO-X channel carrying the photodiode. Given this, "
                         "V_DC is measured on the scope before and after the "
                         "set, DC-coupled, and the drift between the two is "
                         "recorded - RIN goes as 1/V_DC^2, so a level that "
                         "moved 2%% moves every RIN in the set by 4%%.")
    ap.add_argument("--scope-addr", default=None,
                    help="VISA address of the MSO-X (default: scan USB)")
    ap.add_argument("--vdc-scale", type=float, default=1.0,
                    help="V/div for the coarse V_DC pass; raise it if the "
                         "level is off screen at 1 V/div (default 1.0)")
    args = ap.parse_args(argv)

    if args.list or not args.set_name:
        print("Measurement sets:\n")
        for name, s in BUILTIN_SETS.items():
            p = plan_set(s)
            print(f"  {name:<4} {len(s.traces):>3} traces  "
                  f"{fmt_hms(p.total_s):>8}   {s.purpose.splitlines()[0][:60]}")
        print("\n--set <name> --dry-run for the full plan.")
        return 0

    mset = BUILTIN_SETS.get(args.set_name)
    if mset is None:
        raise SystemExit(f"no set called {args.set_name!r}; "
                         f"have {', '.join(BUILTIN_SETS)}")
    plan = plan_set(mset, navg_override=args.navg,
                    settle_recs=args.settle_recs)
    print(plan.report())
    if args.dry_run:
        print("\n(dry run - nothing was sent and no instrument was opened)")
        return 0

    run_set(mset, args.outdir, addr=args.addr, navg_override=args.navg,
            settle_recs=args.settle_recs, v_dc=args.v_dc,
            scope_ch=args.scope_ch, scope_addr=args.scope_addr,
            vdc_scale=args.vdc_scale)
    return 0


if __name__ == "__main__":
    sys.exit(main())
