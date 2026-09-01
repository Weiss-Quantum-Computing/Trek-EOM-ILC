#!/usr/bin/env python3
"""Scouting / continuity traces on the polarimetry channel, SR760 + MSO-X.

Autoranged and uncorrected by design: this is A0-class. Nothing here may be
quoted as a value. What it is for is (a) reading the hum-line and worst-case
broadband amplitudes that A2/A6/A7 need, and (b) checking the chain still
agrees with the 31 Aug baseline before anything is built on that baseline.

Every trace carries the three cheap validity checks - overload, N_indep
against NAVG, and the flat-trace guard. The fourth (a range step to find the
analyser floor in band) belongs to C1 at a pinned range and is not done here.
"""
import argparse, json, os, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ilc_bench as ib

BANDS = ((90., 380.), (380., 1500.), (1500., 6e3), (6e3, 24e3), (24e3, 95e3))
MAINS_F, MAINS_HALFWIDTH, MAINS_TOP = 60.0, 6.0, 3e3


def mains_lines(lo, hi):
    """Every 60 Hz harmonic that lands inside [lo, hi).

    A fixed harmonic count is a trap: masking 60-480 Hz only, the 540 Hz
    through 1440 Hz lines stayed in the 380-1500 Hz average and inflated it
    by 12.8 dB -- measured 31 Aug, where the band read 5265 nV/rtHz against
    1208 once the mask actually covered it. Derive the list from the band.
    Above MAINS_TOP the harmonics are lost in the broadband and masking them
    only throws away bins.
    """
    k0 = max(1, int(np.floor((lo - MAINS_HALFWIDTH) / MAINS_F)))
    k1 = int(np.ceil(min(hi + MAINS_HALFWIDTH, MAINS_TOP) / MAINS_F))
    return [MAINS_F * k for k in range(k0, k1 + 1)]


def band_mean_psd(f, amp, lo, hi, mask_mains=True):
    """MEAN of power over the band. Never the median: a PSD estimate is
    chi-squared and its median sits below its mean by ~0.2 dB at N=32."""
    sel = (f >= lo) & (f < hi)
    if mask_mains:
        for m in mains_lines(lo, hi):
            sel &= ~((f > m - MAINS_HALFWIDTH) & (f < m + MAINS_HALFWIDTH))
    return (float(np.mean(np.asarray(amp)[sel] ** 2)), int(sel.sum())) if sel.any() \
        else (float("nan"), 0)


def autorange_converged(an, span, seconds=12, passes=6, log=print):
    """Autorange repeatedly until the range stops moving.

    `autorange()` is a rate-limited descent, not a solver: it steps toward the
    most sensitive range that does not overload for as long as its time budget
    allows, and one call routinely stops short. Measured 31 Aug on the
    polarimetry channel, successive calls at span 19 gave -2, -18, -42 dBV and
    at span 13 gave -18, -42, -42 -- so a single call landed up to 40 dB
    coarse, which on the A2 map is the difference between the flat 5.8 nV/rtHz
    floor and one that tracks the range. A range 38 dB too coarse is
    indistinguishable from a noisy band until you step it.

    The converged answer is span-independent, as it must be: the analog front
    end sees the whole signal whatever the display span.
    """
    last = None
    for k in range(passes):
        an.write_settings({"ARNG": "1"})
        an.autorange(seconds=seconds)
        now = an.input_range()
        if now == last:
            return now, k + 1
        last = now
    log(f"    (autorange still moving after {passes} passes, last {last:+g} dBV)")
    return last, passes


def take(an, sr, span, navg, wait_s, log=print):
    """One autoranged trace with its validity block."""
    an.write_settings({"SPAN": str(span), "NAVG": str(navg), "ARNG": "1"})
    an.settle(2, span)      # 2 records covers IRNG/ICPL; SPAN needs none
    log(f"  span {span} ({sr.span_hz(span):.4g} Hz): autoranging ...")
    rng, npass = autorange_converged(an, span, log=log)
    log(f"    converged to {rng:+g} dBV in {npass} passes")
    irng = an.input_range()        # a METHOD, not a property. And not
                                   # an.get("IRNG"): get() takes a full query,
                                   # so that sends a bare command and the read
                                   # then times out
    an.start()
    t0 = time.time()
    got = an.wait_done(timeout=wait_s)
    elapsed = time.time() - t0
    snap = an.read_all_settings()
    # trace() hands back its own frequency axis, read from the instrument
    freqs, amps, used_binary = an.trace(snap=snap)
    f, amp = np.asarray(freqs, float), np.asarray(amps, float)
    ovl_e, ovl_f = an.overload()
    stats = sr.record_stats(span, elapsed, navg=navg, ovlp=0.0)
    flat = sr.floor_fault(amp)
    return {"span": span, "irng_dbv": irng, "wait": got, "elapsed_s": elapsed,
            "f": f, "amp": amp, "overload": bool(ovl_e or ovl_f),
            "n_indep": stats.get("n_indep"), "navg": navg,
            "flat_fault": bool(flat), "snap": snap, "stats": stats,
            "binary": used_binary}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spans", default="13,19")
    ap.add_argument("--navg", type=int, default=64)
    ap.add_argument("--pd-ch", type=int, default=2)
    ap.add_argument("--wait", type=float, default=180.0)
    ap.add_argument("--label", default="scout")
    ap.add_argument("--v-dc", type=float, default=None,
                    help="supply V_DC instead of reading the scope. B4 forbids "
                         "both instruments on the detector node at once, so when "
                         "the analyser has it the scope cannot be asked.")
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sib = os.path.dirname(root)          # Python Projects/, where the siblings live
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(root, "run", "polarimetry")
    os.makedirs(out, exist_ok=True)

    if a.v_dc is not None:
        v_dc, detail = a.v_dc, "supplied on the command line"
        print(f"V_DC = {v_dc:.4f} V   ({detail})")
    else:
        scmod = ib.load_module(ib.find_scope_grab(sib), "scope_grab")
        sc = ib.make_scope(scmod)
        sc.connect()
        try:
            v_dc, detail = ib.measure_vdc(sc, a.pd_ch, coarse_scale=2.0)
        finally:
            sc.close()
        print(f"V_DC on CH{a.pd_ch}: {v_dc:.4f} V   ({detail})")

    sr = ib.load_module(ib.find_spectrum_grab(sib), "sr760")
    an = ib.make_analyzer(sr)
    an.connect()
    an.recover()          # flush anything a previous timeout left queued
    saved = an.read_all_settings()
    json.dump(saved, open(os.path.join(out, f"sr760_before_{a.label}_{stamp}.json"),
                          "w"), indent=2, default=str)
    res = []
    try:
        an.apply(sr.PRESETS["protocol"])
        for span in [int(x) for x in a.spans.split(",")]:
            r = take(an, sr, span, a.navg, a.wait)
            res.append(r)
            np.savez(os.path.join(out, f"{a.label}_span{span}_{stamp}.npz"),
                     f=r["f"], amp=r["amp"], v_dc=v_dc,
                     irng=str(r["irng_dbv"]), navg=a.navg)
            ok = (not r["overload"]) and (not r["flat_fault"]) and r["wait"] == "done"
            print(f"    IRNG {r['irng_dbv']} dBV, {r['wait']} in "
                  f"{r['elapsed_s']:.1f} s, N_indep {r['n_indep']}/{a.navg}, "
                  f"overload={r['overload']}, flat={r['flat_fault']}  "
                  f"-> {'USABLE' if ok else 'SUSPECT'}")
    finally:
        an.write_settings({k: str(v) for k, v in saved.items()
                           if k in ("SPAN", "IRNG", "ICPL", "IGND", "ARNG",
                                    "NAVG", "AVGO", "OVLP", "WNDO")})
        an.close()

    print(f"\nband levels (mains masked +-{MAINS_HALFWIDTH:g} Hz), V_DC = {v_dc:.4f} V")
    print(f"{'band':>16s} {'nV/rtHz':>10s} {'dBc/Hz':>9s} {'bins':>5s}  from")
    ref = {(90., 380.): -131.5, (380., 1500.): -138.7, (1500., 6e3): -132.8,
           (6e3, 24e3): -124.0, (24e3, 95e3): -116.8}
    for lo, hi in BANDS:
        best = None
        for r in res:
            if sr.span_hz(r["span"]) >= hi:
                p, n = band_mean_psd(r["f"], r["amp"], lo, hi)
                if n >= 4 and (best is None or n > best[2]):
                    best = (p, r["span"], n)
        if best is None:
            print(f"{lo:7.0f}-{hi:7.0f} {'--':>10s} {'--':>9s}      no span covers it")
            continue
        p, span, n = best
        dbc = 10 * np.log10(p / v_dc ** 2)
        d = dbc - ref[(lo, hi)]
        print(f"{lo:7.0f}-{hi:7.0f} {np.sqrt(p)*1e9:10.1f} {dbc:9.1f} {n:5d}  "
              f"span {span}   vs 31 Aug {ref[(lo,hi)]:+.1f} -> {d:+.1f} dB")
    print("\nSCOUTING ONLY - autoranged, unpinned, no range step. Do not quote.")


if __name__ == "__main__":
    main()
