"""The closed drives cut into the chunks Supertime's custom-ramp slots take
(Maarten, 28 Oct 2026; Desktop\\CUSTOM_RAMP_BRIEF.md).

Supertime's "Evaluate Custom Ramp" reads a two-column CSV (index, value; no
header), maps the first row onto the ramp's start edge and the last row onto
its end edge, interpolates at the step size and rescales the shape so the
first row lands on the start cell's voltage and the last on the end cell's.
So a chunk must run between two distinct voltages, and the cells carry
them. Per channel and leg of a record this writes
    up      from the drive's lead before the rise to 200 us into the flat top
    hold    the flat top, 200 us clear of both ends (the card split's hold)
    down    from 200 us before the top's end to 100 us after the fall
    after   from there to the next leg's lead (leg 1) or the record's end (leg 2)
with every boundary on a 10 us grid, so a 10 us step fits each one, and a
table with duration, step, points and the start / end voltage of every
chunk as AWG volts, as degrees at the EOM and as Supertime card volts.
The panel's Batch window (Export for Supertime...) and tools/supertime_chunks.py
both come through export().
"""
import csv
import datetime
import os
import shutil

import numpy as np

HV90 = {"X1": 5128.3, "X2": 5137.4}            # HV volts at 90 deg (the targets' V90)
GAIN = {"X1": 557.0, "X2": 601.0}              # HV volts per AWG volt, measured on the hold (14-15 Oct 2026)
SUPERTIME_V90 = {"X1": 11.412, "X2": 9.99}     # card volts at 90 deg per crystal (day-log "180 degree point" of the pair)
STEP_US = 10.0
LEAD_US, SETTLE_US, TAIL_US = 100.0, 200.0, 100.0
FLAT_TOL = 1e-3
CONST_MV = 2.0                                  # a chunk whose ends differ by less is a held value
DRIVE_RAMPS = r"D:\Supertime Computer\Documents and Settings\Admin\My Documents\2026\Ramps"
FIELDS = ("record", "channel", "leg", "chunk", "t_start_us", "t_end_us", "duration_us", "step_us", "points",
          "rows", "start_V_awg", "end_V_awg", "start_deg", "end_deg", "start_V_supertime", "end_V_supertime",
          "note", "file")


def default_out():
    """The flash drive's Ramps folder when it is plugged in, else None."""
    return DRIVE_RAMPS if os.path.isdir(DRIVE_RAMPS) else None


def grid(us, up):
    f = np.ceil if up else np.floor
    return float(f(us / STEP_US) * STEP_US)


def read_keeper(path):
    rows = [l.split(",") for l in open(path, encoding="utf-8") if l[0].isdigit()]
    a = np.array(rows, float)
    return a[:, 0], a[:, 1]


def transports(t_us, tg):
    """[(a, b, c, d)] in us per transport: rise start, flat-top start, flat-top
    end, fall end, from both channels' base targets (the common flat stretch,
    as the card split cuts it)."""
    from . import echo
    dt = float(np.median(np.diff(t_us)))
    pairs = echo._card().find_transports([tg["X1"], tg["X2"]], dt)
    out = []
    for i0, i1 in pairs:
        flat = np.ones(i1 - i0 + 1, bool)
        for k in ("X1", "X2"):
            seg = tg[k][i0:i1 + 1]
            if seg.max() > 0:
                flat &= seg >= (1 - FLAT_TOL) * seg.max()
        idx = np.flatnonzero(flat)
        b, c = (t_us[i0 + idx[0]], t_us[i0 + idx[-1]]) if idx.size else (t_us[i0], t_us[i1])
        out.append((t_us[i0], b, c, t_us[i1]))
    return out


def chunks_for(trs, t_end):
    """Per leg: [(name, t0, t1)] on the 10 us grid."""
    legs = []
    for n, (a, b, c, d) in enumerate(trs):
        t_up0 = grid(max(0.0, a - LEAD_US), False)
        t_up1 = grid(b + SETTLE_US, True)
        t_dn0 = grid(c - SETTLE_US, False)
        t_dn1 = grid(d + TAIL_US, True)
        nxt = grid(max(0.0, trs[n + 1][0] - LEAD_US), False) if n + 1 < len(trs) else grid(t_end, False)
        ch = [("up", t_up0, t_up1)]
        if t_dn0 - t_up1 >= 10 * STEP_US:
            ch.append(("hold", t_up1, t_dn0))
            ch.append(("down", t_dn0, t_dn1))
        else:                                       # no usable flat top: up meets down in the middle
            mid = grid(0.5 * (b + c), False)
            ch = [("up", t_up0, mid), ("down", mid, t_dn1)]
        if nxt - t_dn1 >= 10 * STEP_US:
            ch.append(("after", t_dn1, nxt))
        legs.append(ch)
    return legs


def deg(v_awg, k):
    return v_awg * GAIN[k] * 90.0 / HV90[k]


def export_record(run_dir, stem, short, out, log=print):
    """One record's chunks and full keepers into <out>/<stem>/; returns the
    table rows. Needs drive_<short>X1/X2.state.npz (the base targets) and
    keep/drive_<short>X1/X2_keep.csv in run_dir."""
    import run_ilc
    keep = {k: os.path.join(run_dir, "keep", f"drive_{short}{k}_keep.csv") for k in ("X1", "X2")}
    missing = [k for k, p in keep.items() if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError(f"{stem}: no keeper for {', '.join(missing)} in {os.path.join(run_dir, 'keep')}")
    tg = {}
    for k in ("X1", "X2"):
        st = run_ilc.load_state(os.path.join(run_dir, f"drive_{short}{k}.state.npz"))
        tg[k] = np.asarray(st["base_target"] if "base_target" in st else st["target"], float)
    t_us, u1 = read_keeper(keep["X1"])
    _, u2 = read_keeper(keep["X2"])
    u = {"X1": u1, "X2": u2}
    trs = transports(t_us, tg)
    legs = chunks_for(trs, t_us[-1])
    d = os.path.join(out, stem)
    os.makedirs(d, exist_ok=True)
    rows = []
    for k in ("X1", "X2"):
        shutil.copy2(keep[k], os.path.join(d, f"{stem}_{k}_full_keeper.csv"))
        for n, ch in enumerate(legs, 1):
            for name, t0, t1 in ch:
                i0, i1 = int(round(t0 / 2.0)), int(round(t1 / 2.0))
                seg = u[k][i0:i1 + 1]
                fn = f"{stem}_{k}_leg{n}_{name}.csv"
                with open(os.path.join(d, fn), "w", encoding="utf-8", newline="") as fh:
                    for j, v in enumerate(seg, 1):
                        fh.write(f"{j},{v:.9g}\n")
                v0, v1 = float(seg[0]), float(seg[-1])
                note = "held value: put it in the cells, no shape needed" if abs(v1 - v0) * 1e3 < CONST_MV else ""
                rows.append(dict(record=stem, channel=k, leg=n, chunk=name, t_start_us=f"{t0:.0f}", t_end_us=f"{t1:.0f}",
                                 duration_us=f"{t1 - t0:.0f}", step_us=f"{STEP_US:g}", points=f"{(t1 - t0) / STEP_US:.0f}",
                                 rows=len(seg), start_V_awg=f"{v0:.4f}", end_V_awg=f"{v1:.4f}",
                                 start_deg=f"{deg(v0, k):.3f}", end_deg=f"{deg(v1, k):.3f}",
                                 start_V_supertime=f"{deg(v0, k) / 90.0 * SUPERTIME_V90[k]:.4f}",
                                 end_V_supertime=f"{deg(v1, k) / 90.0 * SUPERTIME_V90[k]:.4f}",
                                 note=note, file=f"{stem}/{fn}"))
    log(f"  {stem}: {len(legs)} leg(s), {sum(len(c) for c in legs)} chunks per channel; transports "
        + "; ".join(f"{x[0]:.0f}-{x[1]:.0f}-{x[2]:.0f}-{x[3]:.0f} us" for x in trs))
    return rows


def write_table(rows, out, title="Custom-ramp chunks"):
    with open(os.path.join(out, "ramp_table.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(FIELDS), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    L = [f"# {title} ({datetime.datetime.now():%Y-%m-%d %H:%M})", "",
         "Every chunk is a two-column CSV (index, AWG volts; no header) on the 2 us grid of the closed drive. "
         "Supertime maps the first row onto the chunk's start edge and the last onto its end edge and rescales "
         "the shape onto the cell voltages, so set each chunk's start cell and end cell to the voltages below "
         "and give the ramp the duration and step listed (a different duration stretches the shape in time). "
         f"Degrees are rotation at the EOM from the measured chain gains ({GAIN['X1']:.0f} / {GAIN['X2']:.0f} V per AWG volt) and "
         f"V90 ({HV90['X1']:.1f} / {HV90['X2']:.1f} V); the Supertime column converts with the day-log 90-degree "
         f"points ({SUPERTIME_V90['X1']} V X1, {SUPERTIME_V90['X2']} V X2) and is to be confirmed against one test ramp. "
         "A chunk marked as a held value changes by under 2 mV end to end: set the cells, no shape needed.", "",
         "| record | ch | leg | chunk | t start (us) | t end (us) | duration (us) | step (us) | points | start V AWG | end V AWG | start deg | end deg | start V Supertime | end V Supertime | note |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['record']} | {r['channel']} | {r['leg']} | {r['chunk']} | {r['t_start_us']} | {r['t_end_us']} | {r['duration_us']} | "
                 f"{r['step_us']} | {r['points']} | {r['start_V_awg']} | {r['end_V_awg']} | {r['start_deg']} | {r['end_deg']} | "
                 f"{r['start_V_supertime']} | {r['end_V_supertime']} | {r['note']} |")
    with open(os.path.join(out, "ramp_table.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


README = """# Custom-ramp chunks for Supertime

One folder per record, with the two channels' full closed drives (`<stem>_X1_full_keeper.csv`, 2 us
grid, AWG volts) and the chunks `<stem>_<X1|X2>_leg<1|2>_<up|hold|down|after>.csv`. `ramp_table.md` /
`.csv` list every chunk's timing and end voltages. Written by the EOM-ILC panel (Batch window, Export
for Supertime...) or tools\\supertime_chunks.py.

## Chunk format

Two columns, comma separated, no header: `index, value`, as the September files. Values are the
closed drive in AWG volts on the 2 us grid; Supertime interpolates at its step and rescales the
shape onto the cell voltages, so only the shape carries over.

## What each chunk is, and where it goes in the sequence

| chunk | runs from | to | sequence edges (leg 1; leg 2 with the "1: " prefix) |
|---|---|---|---|
| up | 100 us before the target's rise starts (the drive leads the target) | 200 us into the flat top | EO ramp up 0 -> EO ramp up 3 |
| hold | 200 us into the flat top | 200 us before its end | EO ramp up 3 -> EO ramp down 3 (a Custom cell with time constant 1 at up 3) |
| down | 200 us before the flat top's end | 100 us after the fall | EO ramp down 3 -> EO ramp down 0 |
| after | 100 us after the fall | 100 us before the next leg's rise (leg 1) / the record's end (leg 2) | EO ramp down 0 -> 1: EO ramp up 0 (leg 1), a Custom cell at down 0 |

The hold and after chunks carry the optical close's slow corrections (the creep over the hold, the
relaxation after the fall); giving them their own Custom ramps inserts ramps and renumbers: leg 1 =
128 up, 129 hold, 130 down, 131 after, leg 2 = 132-135, and `ramp path.csv` rows 47 (X1) and 48 (X2)
must be re-pointed. A chunk marked "held value" in the table changes by under 2 mV end to end: set
its cells, no shape is needed, and the numbering drops by one for each such chunk left out.

Every boundary sits on a 10 us grid, so a 10 us step fits every chunk exactly; the table gives the
duration and the number of points. Set the edge times from the table (EO upRamp1 / Hold / dnRamp1
Time Leg 2 are the up, hold and down durations; the four chunks add up to the leg spacing). A
different duration stretches the shape in time.

## Voltages

The table gives each chunk's start and end value three ways: AWG volts as played on the bench;
degrees of rotation at the EOM from the measured chain gains and V90; and Supertime card volts
converted from degrees with the day-log 90-degree points per crystal. The last column is derived;
confirm it with one test ramp on the Trek monitor before trusting the absolute level. The resting
value in the sequence is 0.012 V; the chunks' rest ends sit within a few mV of zero at the AWG.

## Before the first run

1. One test ramp: load a chunk, set its cells to the table's voltages, and compare Supertime's
   Channel Plot or the Trek monitor on a scope with the chunk's shape.
2. Recount the enabled Trek ramps on rows 47 and 48 after adding Custom cells, and fix
   `ramp path.csv`.
3. Both channels ramp on the same edges with the same step, as always.
"""


def export(plan, stems, out, log=print, title=None, run_dir=None):
    """Every record in `stems` of a loaded plan (its run folder, its campaigns'
    AWG names) into `out`: the chunk folders, ramp_table.csv / .md and
    README.md. Returns the table rows; a record without keepers is logged
    and skipped."""
    os.makedirs(out, exist_ok=True)
    run_dir = run_dir or plan["_run_dir"]
    rows = []
    for stem in stems:
        camp = next((c for c in plan["campaigns"] if c["stem"] == stem), None)
        short = camp.get("awg", stem) if camp else stem
        try:
            rows += export_record(run_dir, stem, short, out, log)
        except FileNotFoundError as e:
            log(f"  {e}")
    if rows:
        write_table(rows, out, title or f"Custom-ramp chunks, plan {plan.get('name', '')}")
        with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(README)
    log(f"{len(rows)} chunks for {len({r['record'] for r in rows})} record(s) in {out}")
    return rows
