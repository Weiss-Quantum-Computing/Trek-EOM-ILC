#!/usr/bin/env python3
"""The segmented spectrum on the polarimetry channel, pinned, with C1 built in.

One pass per span: converge the autorange, PIN it, take the trace, step the
range up 4 dB and take it again. The pair is C1 -- a real optical signal does
not move with the input range, so whatever does move was the analyser. From
the two levels the analyser floor in that band follows directly,

    F = sqrt((v2^2 - v1^2) / (1.585^2 - 1))          4 dB -> x1.585 in amplitude

and the margin against it is what C3 needs to say whether a band may be quoted
at all: >=10 dB quote it, 3-10 dB subtract in power and carry the bar, <3 dB
upper limit only.

Autorange is iterated to convergence, never called once -- see
pol_scout.autorange_converged for why a single call lands up to 40 dB coarse.
"""
import argparse, json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import ilc_bench as ib
from pol_scout import (BANDS, MAINS_HALFWIDTH, band_mean_psd,
                       autorange_converged)

STEP_DB = 4.0
STEP_AMP = 10 ** (STEP_DB / 20.0)          # 1.5849


def one_span(an, sr, span, navg, wait_s, log=print, pin=None):
    an.write_settings({"SPAN": str(span), "NAVG": str(navg)})
    an.settle(3, span)
    if pin is None:
        rng, npass = autorange_converged(an, span, log=log)
        log(f"  span {span:2d} ({sr.span_hz(span):9.4g} Hz): pinned {rng:+g} dBV "
            f"({npass} autorange passes)")
    else:
        # A converged autorange is NOT overload-safe. It finds the most
        # sensitive range that does not overload DURING the autorange, and
        # rare excursions are exactly what a brief look misses: measured
        # 31 Aug at theta_a = 0, the converged -44 dBV overloaded span 13 and
        # -40 overloaded span 11, the flagged average running 120 s instead of
        # 65. Pin coarser by hand and let the overload flag confirm it.
        rng = float(pin)
        log(f"  span {span:2d} ({sr.span_hz(span):9.4g} Hz): pinned {rng:+g} dBV "
            f"(forced)")
    out = []
    for label, dbv in (("pin", rng), ("pin+4", rng + STEP_DB)):
        an.write_settings({"ARNG": "0"})
        an.pin_range(dbv)
        an.settle(3, span)
        an.start()
        t0 = time.time()
        got = an.wait_done(timeout=wait_s)
        el = time.time() - t0
        snap = an.read_all_settings()
        f, a, _ = an.trace(snap=snap)
        f, a = np.asarray(f, float), np.asarray(a, float)
        oe, of = an.overload()
        st = sr.record_stats(span, el, navg=navg, ovlp=0.0)
        bad = bool(oe or of) or bool(sr.floor_fault(a)) or got != "done"
        log(f"      {label:6s} {dbv:+6.1f} dBV  {got:7s} {el:6.1f}s  "
            f"N_ind {st.get('n_indep', float('nan')):6.1f}/{navg}  "
            f"ovl={bool(oe or of)!s:5s} flat={bool(sr.floor_fault(a))!s:5s}"
            f"{'  SUSPECT' if bad else ''}")
        out.append({"label": label, "dbv": dbv, "f": f, "amp": a, "bad": bad,
                    "elapsed": el, "n_indep": st.get("n_indep"), "wait": got})
    return {"span": span, "pinned_dbv": rng, "traces": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spans", default="11,13,15,17,19")
    ap.add_argument("--navg", type=int, default=64)
    ap.add_argument("--v-dc", type=float, required=True)
    ap.add_argument("--wait", type=float, default=400.0)
    ap.add_argument("--label", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--pin", type=float, default=None,
                    help="force the input range in dBV instead of autoranging")
    a = ap.parse_args()

    root = os.path.dirname(HERE)
    sib = os.path.dirname(root)
    out = os.path.join(root, "run", "polarimetry")
    os.makedirs(out, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    sr = ib.load_module(ib.find_spectrum_grab(sib), "sr760")
    an = ib.make_analyzer(sr)
    an.connect()
    an.recover()
    saved = an.read_all_settings()
    res = []
    try:
        an.apply(sr.PRESETS["protocol"])
        for span in [int(x) for x in a.spans.split(",")]:
            res.append(one_span(an, sr, span, a.navg, a.wait, pin=a.pin))
    finally:
        an.write_settings({k: str(v) for k, v in saved.items()
                           if k in ("SPAN", "IRNG", "ICPL", "IGND", "ARNG",
                                    "NAVG", "AVGO", "OVLP", "WNDO")})
        an.close()

    npz = os.path.join(out, f"seg_{a.label}_{stamp}.npz")
    np.savez(npz, v_dc=a.v_dc, label=a.label, note=a.note,
             **{f"s{r['span']}_{t['label'].replace('+','p')}_{k}": t[k]
                for r in res for t in r["traces"] for k in ("f", "amp")},
             pinned=np.array([[r["span"], r["pinned_dbv"]] for r in res]))
    print(f"\nsaved {npz}")

    print(f"\n{a.label}   V_DC = {a.v_dc:.4f} V   "
          f"(mains masked +-{MAINS_HALFWIDTH:g} Hz)")
    print(f"{'band (Hz)':>15s} {'span':>5s} {'nV/rtHz':>9s} {'dBc/Hz':>8s} "
          f"{'C1 dB':>7s} {'floor':>9s} {'margin':>7s}  verdict")
    rows = []
    for lo, hi in BANDS:
        pick = None
        for r in res:
            if sr.span_hz(r["span"]) < hi:
                continue
            p1, n1 = band_mean_psd(r["traces"][0]["f"], r["traces"][0]["amp"], lo, hi)
            if n1 >= 6 and (pick is None or n1 > pick[1]):
                pick = (r, n1)
        if pick is None:
            print(f"{lo:7.0f}-{hi:7.0f} {'--':>5s}   no span resolves this band")
            continue
        r, nbin = pick
        p1, _ = band_mean_psd(r["traces"][0]["f"], r["traces"][0]["amp"], lo, hi)
        p2, _ = band_mean_psd(r["traces"][1]["f"], r["traces"][1]["amp"], lo, hi)
        v1, v2 = np.sqrt(p1), np.sqrt(p2)
        c1_db = 20 * np.log10(v2 / v1)
        if p2 > p1:
            floor = np.sqrt((p2 - p1) / (STEP_AMP ** 2 - 1))
            margin = 20 * np.log10(v1 / floor)
            fs, ms = f"{floor*1e9:9.1f}", f"{margin:7.1f}"
        else:
            floor, margin = 0.0, np.inf
            fs, ms = f"{'--':>9s}", f"{'>=25':>7s}"
        verdict = ("quote" if margin >= 10 else
                   "subtract dark, carry bar" if margin >= 3 else "UPPER LIMIT")
        dbc = 10 * np.log10(p1 / a.v_dc ** 2)
        print(f"{lo:7.0f}-{hi:7.0f} {r['span']:5d} {v1*1e9:9.1f} {dbc:8.1f} "
              f"{c1_db:+7.2f} {fs} {ms}  {verdict}")
        rows.append({"band": [lo, hi], "span": r["span"], "nv": v1 * 1e9,
                     "dbc": dbc, "c1_db": c1_db, "margin_db": float(margin),
                     "verdict": verdict, "bins": nbin})
    json.dump({"label": a.label, "v_dc": a.v_dc, "note": a.note,
               "stamp": stamp, "npz": os.path.basename(npz), "bands": rows,
               "pinned": {str(r["span"]): r["pinned_dbv"] for r in res}},
              open(os.path.join(out, f"seg_{a.label}_{stamp}.json"), "w"),
              indent=2)


if __name__ == "__main__":
    main()
