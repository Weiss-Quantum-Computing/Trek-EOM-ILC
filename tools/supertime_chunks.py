"""Cut a batch campaign's closed drives into the chunks Supertime's custom-ramp
slots take, with the table of step size, start and end voltage per chunk
(Maarten, 28 Oct 2026; see CUSTOM_RAMP_BRIEF.md on the Desktop).

    python tools/supertime_chunks.py <plan.json> <out folder> [stem ...]

Supertime's "Evaluate Custom Ramp" takes a two-column CSV (index, value; no
header), maps the first row onto the ramp's start edge and the last row onto
its end edge, interpolates at the step size, and rescales the shape so the
first row lands on the start cell's voltage and the last row on the end
cell's voltage. So a chunk must run between two distinct voltages, and the
cells carry those voltages: this tool writes, per channel and leg, the four
chunks
    up      from the drive's lead before the rise to 200 us into the flat top
    hold    the flat top, 200 us clear of both ends (the card split's hold)
    down    from 200 us before the top's end to 100 us after the fall
    after   from there to the next leg's lead (leg 1) or the record's end (leg 2)
with every boundary on a 10 us grid, so a 10 us step fits each one, and the
table ramp_table.csv / ramp_table.md with t_start, t_end, duration, step,
points, and the start / end voltage of every chunk: as played at the AWG
(volts), as rotation at the EOM (degrees, from the measured chain gains and
V90), and converted to Supertime's card volts with the day-log 180-degree
points (11.412 V X1, 9.99 V X2 at 90 degrees per crystal) -- that last
column is derived, confirm it against one test ramp. Values in the chunk
files are AWG volts; the rescale in Supertime removes the scale anyway. The
full keeper of each channel goes beside the chunks for reference.
"""
import argparse
import csv
import datetime

import os
import shutil
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ilc_batch                                                  # noqa: E402
import run_ilc                                                    # noqa: E402
from eomilc import echo                                           # noqa: E402

HV90 = {"X1": 5128.3, "X2": 5137.4}            # HV volts at 90 deg (the targets' V90)
GAIN = {"X1": 557.0, "X2": 601.0}              # HV volts per AWG volt, measured on the hold (14-15 Oct 2026)
SUPERTIME_V90 = {"X1": 11.412, "X2": 9.99}     # card volts at 90 deg per crystal (day-log "180 degree point" of the pair)
STEP_US = 10.0
LEAD_US, SETTLE_US, TAIL_US = 100.0, 200.0, 100.0
FLAT_TOL = 1e-3
CONST_MV = 2.0                                  # a chunk whose ends differ by less is a held value


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    ap.add_argument("out")
    ap.add_argument("stems", nargs="*")
    a = ap.parse_args(argv)
    plan = ilc_batch.load_plan(a.plan)
    man = ilc_batch.read_manifest(plan, False)
    run_dir = plan["_run_dir"]
    stems = a.stems or [c["stem"] for c in plan["campaigns"]
                        if (man["campaigns"].get(c["stem"]) or {}).get("status", "").startswith("done")]
    os.makedirs(a.out, exist_ok=True)
    rows = []
    for stem in stems:
        camp = next(c for c in plan["campaigns"] if c["stem"] == stem)
        short = camp.get("awg", stem)
        keep = {k: os.path.join(run_dir, "keep", f"drive_{short}{k}_keep.csv") for k in ("X1", "X2")}
        if not all(os.path.exists(p) for p in keep.values()):
            print(f"{stem}: no keepers, skipped")
            continue
        tg = {}
        for k in ("X1", "X2"):
            st = run_ilc.load_state(os.path.join(run_dir, f"drive_{short}{k}.state.npz"))
            tg[k] = np.asarray(st["base_target"] if "base_target" in st else st["target"], float)
        t_us, u1 = read_keeper(keep["X1"])
        _, u2 = read_keeper(keep["X2"])
        u = {"X1": u1, "X2": u2}
        trs = transports(t_us, tg)
        legs = chunks_for(trs, t_us[-1])
        d = os.path.join(a.out, stem)
        os.makedirs(d, exist_ok=True)
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
        print(f"{stem}: {len(legs)} legs, {sum(len(c) for c in legs)} chunks per channel; transports "
              + "; ".join(f"{x[0]:.0f}-{x[1]:.0f}-{x[2]:.0f}-{x[3]:.0f} us" for x in trs))
    fields = list(rows[0].keys()) if rows else []
    with open(os.path.join(a.out, "ramp_table.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    L = [f"# Custom-ramp chunks, campaign 2026-10-14 ({datetime.datetime.now():%Y-%m-%d %H:%M})", "",
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
    with open(os.path.join(a.out, "ramp_table.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print(f"{len(rows)} chunks for {len(stems)} records in {a.out}")


if __name__ == "__main__":
    main()
