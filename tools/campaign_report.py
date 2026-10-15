"""Fill a campaign folder's manifest and write its report from what the batch
left on disk (campaign-20261014, START_HERE.md section 5). Rerun any time; it
only reads the run folder and rewrites the manifest's fill-in columns and
campaign_report.md. With --figures, the figure set of every finished
campaign that has none yet (tools/campaign_figures.py) goes to
<campaign folder>/figures/<stem>/.

    python tools/campaign_report.py <campaign folder> [--plan plans/00_all_in_order.json]
                                    [--log <batch log>] [--figures]
"""
import argparse
import csv
import datetime
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ilc_batch                                                  # noqa: E402
from eomilc import corrections as corrmod                         # noqa: E402
import campaign_figures as cf                                     # noqa: E402

FILL = ("status", "keeper_iteration", "final_rms_V_X1", "final_rms_V_X2",
        "leg2_minus_leg1_top_mdeg", "light_minus_target_top_mdeg", "notes")


def leg_tolerance_mdeg(stem):
    """The brief's leg-to-leg light tolerance (0.1 rad of pair phase)."""
    if stem.startswith("R3410"):
        return 20.0
    if stem.startswith("R3425"):
        return 8.0
    if stem.startswith("R3450"):
        return 4.0
    if stem.startswith("SF") or stem.startswith("G160") or (stem.startswith("SW") and stem != "SW100"):
        return 20.0
    return 10.0


def hold_lt(scan, band):
    """(worst transport's light - target inside the hold, the first scan's
    per-transport list) in mdeg, from the comparison and the verdict's
    landmarks; nan without them."""
    z = cf.npz_of(scan)
    v = scan.get("verdict") or {}
    tr = ((v.get("legs") or [{}])[0].get("transports")) or []
    if z is None or not tr:
        return float("nan")
    tl = z["t_leg1"]
    ms, dt = (tl - tl[0]) * 1e3, float(np.median(np.diff(tl)))
    lt = corrmod.lowpass(z["leg1_d_light"], dt, band) * 1e3
    vals = []
    for t in tr:
        a, b, c, d = (float(x) for x in t["landmarks_ms"])
        m = (ms > b + 0.2) & (ms < c - 0.2)
        if m.any():
            vals.append(float(np.mean(lt[m])))
    return max(vals, key=abs) if vals else float("nan")


def leg_diff_top(scan, band):
    """Light leg 2 - leg 1, mean over the ramp top (mdeg), from a scan's
    comparison and its verdict's transport landmarks; nan when not there."""
    z = cf.npz_of(scan)
    v = scan.get("verdict") or {}
    tr = ((v.get("legs") or [{}])[0].get("transports")) or []
    if z is None or len(tr) < 2:
        return float("nan")
    tl = z["t_leg1"]
    ms, dt = (tl - tl[0]) * 1e3, float(np.median(np.diff(tl)))
    a1, b1, c1 = (float(x) for x in tr[0]["landmarks_ms"][:3])
    a2 = float(tr[1]["landmarks_ms"][0])
    k = int(round((a2 - a1) / (dt * 1e3)))
    x = z["leg1_phi_light"]
    n = len(x) - k
    if n <= 0:
        return float("nan")
    d = corrmod.lowpass(x[k:k + n] - x[:n], dt, band) * 1e3
    top = (ms[:n] > b1 + 0.3) & (ms[:n] < c1 - 0.3)
    return float(np.mean(d[top])) if top.any() else float("nan")


def fill_row(row, plan, man, log_path, figures_to=None):
    estem = row.get("echo_stem") or row["stem"]
    e = (man.get("campaigns") or {}).get(estem)
    notes = []
    if not e:
        row["status"] = row.get("status") or ""
        return row, []
    row["status"] = e.get("status", "")
    ch = e.get("channels") or {}
    if ch:
        row["keeper_iteration"] = str(ch.get("X1", {}).get("keeper_iteration", ""))
    opt = e.get("optical") or {}
    passes = [p for p in (opt.get("passes") or []) if p.get("status") == "done"]
    if passes and passes[-1].get("kept_rms_v"):
        kr = passes[-1]["kept_rms_v"]
        row["final_rms_V_X1"], row["final_rms_V_X2"] = f"{kr.get('X1', float('nan')):.2f}", f"{kr.get('X2', float('nan')):.2f}"
    elif ch:
        row["final_rms_V_X1"] = f"{ch.get('X1', {}).get('keeper_rms_v', float('nan')):.2f}"
        row["final_rms_V_X2"] = f"{ch.get('X2', {}).get('keeper_rms_v', float('nan')):.2f}"
    c = cf.Campaign(plan, estem, log_path) if estem in {x["stem"] for x in plan["campaigns"]} else None
    band = float((opt.get("settings") or {}).get("band_hz") or 5000.0)
    if c and c.scans:
        joint = [s for s in c.scans if s["kind"] in ("before", "verify") and s.get("verdict")]
        if joint:
            wb, wa = cf.worst(joint[0]["verdict"]), cf.worst(joint[-1]["verdict"])
            hb, ha = hold_lt(joint[0], band), hold_lt(joint[-1], band)
            row["light_minus_target_top_mdeg"] = (f"hold {hb:+.0f} -> {ha:+.0f} (edge {wb.get('top_lt_mdeg', float('nan')):+.0f} "
                                                  f"-> {wa.get('top_lt_mdeg', float('nan')):+.0f})")
            after = [s for s in joint if s["kind"] == "verify"]
            if after:
                d = leg_diff_top(after[-1], band)
                row["leg2_minus_leg1_top_mdeg"] = f"{d:+.1f}" if np.isfinite(d) else ""
                tol = leg_tolerance_mdeg(row["stem"])
                if np.isfinite(d) and abs(d) > tol:
                    notes.append(f"FLAG leg 2 - leg 1 at the top {d:+.1f} mdeg exceeds {tol:g}")
            try:
                stop = float(row.get("stop_deg") or 180.0)
            except ValueError:
                stop = 180.0
            if stop < 180.0 and np.isfinite(ha) and abs(ha) > 250.0:
                notes.append(f"FLAG light - target at the stop {ha:+.0f} mdeg inside the hold (> 250)")
            fa = float(wa.get("after_fall_lt_mdeg", float("nan")))
            notes.append(f"after the fall {fa:+.0f} mdeg; rms {wa.get('lt_rms_mdeg', float('nan')):.0f}")
    if opt and opt.get("status") not in (None, "done"):
        notes.append(f"optical {opt.get('status')}: {opt.get('error', '')}".strip())
    # the card pause against the manifest
    card = e.get("card") or {}
    pauses = card.get("pauses_ms") or []
    try:
        want = float(row.get("card_pause_ms") or "nan")
    except ValueError:
        want = float("nan")
    if pauses and np.isfinite(want):
        got = float(pauses[0])
        if abs(got - want) > 0.002:
            notes.append(f"STOP card pause {got:.3f} ms differs from the manifest's {want:.3f}")
        else:
            notes.append(f"card pause {got:.3f} ms as planned")
    elif e.get("card_error"):
        notes.append(f"card NOT written: {e['card_error']}")
    for k in ("X1", "X2"):
        st = ch.get(k, {}).get("stop", "")
        if st and st not in ("converged", "noise floor", "plateau", "max iterations"):
            notes.append(f"{k} stopped: {st}")
    if e.get("error"):
        notes.append(f"error: {e['error']}")
    if e.get("minutes") is not None:
        notes.append(f"{e['minutes']:.0f} min")
    row["notes"] = "; ".join(notes)
    if figures_to and c and e.get("status", "").startswith("done"):
        dst = os.path.join(figures_to, estem)
        if not os.path.exists(os.path.join(dst, "README.md")):
            try:
                cf.make(plan, estem, log_path, figures_to)
            except Exception as ex:                 # a figure must never stop the report
                notes.append(f"figures failed: {type(ex).__name__}: {ex}")
    return row, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--plan", default=os.path.join("plans", "00_all_in_order.json"))
    ap.add_argument("--log")
    ap.add_argument("--figures", action="store_true")
    a = ap.parse_args(argv)
    folder = os.path.abspath(a.folder)
    plan_path = a.plan if os.path.isabs(a.plan) else os.path.join(folder, a.plan)
    plan = ilc_batch.load_plan(plan_path)
    man = ilc_batch.read_manifest(plan, False)
    mpath = os.path.join(folder, "campaign_manifest.csv")
    with open(mpath, encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        fields, rows = rd.fieldnames, list(rd)
    log_path = a.log or os.path.join(plan["_run_dir"], "batch_all.log")
    figs = os.path.join(folder, "figures") if a.figures else None
    needs = []
    for row in rows:
        row, notes = fill_row(row, plan, man, log_path, figs)
        for n in notes:
            if n.startswith(("FLAG", "STOP")) or "stopped:" in n or n.startswith("error") or n.startswith("optical "):
                needs.append(f"{row['stem']}: {n}")
    with open(mpath, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    done = [r for r in rows if r.get("status", "").startswith("done")]
    L = ["# Campaign 2026-10-14 report", "",
         f"Written {datetime.datetime.now():%Y-%m-%d %H:%M} from `{os.path.basename(plan_path)}` and the run folder "
         f"`{plan['_run_dir']}`; {len(done)} of {len(rows)} campaigns done. Optical: 3 passes at gain 1.0 with 4 "
         f"learning iterations each (set from the plans' 3 on the evening of 14 Oct: three left a 16-shot keeper near "
         f"0.5 V rms; the last allowed iteration now measures at the full count), before scan full circle, solo "
         f"and verify scans half circle.", "",
         "| # | stem | status | keeper | rms X1 / X2 (V) | light - target, hold (edge) before -> after (mdeg) | leg 2 - leg 1 top (mdeg) | card pause (ms) | notes |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r.get('order', '')} | {r['stem']} | {r.get('status', '')} | {r.get('keeper_iteration', '')} | "
                 f"{r.get('final_rms_V_X1', '')} / {r.get('final_rms_V_X2', '')} | {r.get('light_minus_target_top_mdeg', '')} | "
                 f"{r.get('leg2_minus_leg1_top_mdeg', '')} | {r.get('card_pause_ms', '')} | {r.get('notes', '')} |")
    L += ["", "The hold number is light - target averaged over the inside of the flat top (0.2 ms clear of both "
              "ends), the worst of the two transports; the edge number is the verdict's ramp-top extreme over the "
              "top with its 0.3 ms pad, which the comparison's time-map artefact at the ramp edges dominates.", ""]
    L += ["", "## Needs Maarten", ""] + ([f"- {n}" for n in needs] or ["- nothing flagged"])
    L += ["", "Figure sets: `figures/<stem>/` (eight figures and a README per campaign, tools/campaign_figures.py)."]
    with open(os.path.join(folder, "campaign_report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print(f"{len(done)} done of {len(rows)}; {len(needs)} item(s) flagged; manifest and report written in {folder}")


if __name__ == "__main__":
    main()
