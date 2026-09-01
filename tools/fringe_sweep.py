#!/usr/bin/env python3
"""Phase 1 fringe sweep: drive one EOM (or both) through a fringe and fit V_pi.

A raised-cosine 0 -> peak -> 0 over the session's own 11.0020 ms record, so the
sweep rides the existing burst, EXT trigger and scope grid with nothing about
the timing setup changed. Raised cosine rather than triangle for two reasons: a
triangle's corners sit exactly where the plant's ~2.4 kHz resonance would ring
and distort the fringe at the turning points you most want to trust, and a
cosine samples phi densely near both extremes, which is where the fit gets its
leverage.

    python tools/fringe_sweep.py --label X1a --capture-only
    python tools/fringe_sweep.py --label X2a --awg-ch 2 --peak-hv 5400

WHAT THIS DOES AND DOES NOT MEASURE. V_pi comes from the fringe PERIOD and the
EO zero from its PHASE, so neither depends on the photodiode's DC offset.
Visibility comes from B/A and does, so it is deliberately not reported here --
the scope's offset error is scale-dependent (measured: -2.61 mV at 0.5 V/div,
-13.5 mV at 1 V/div) and a dark reading at the working scale would be needed
first. Take visibility from a static three-angle measurement instead.

SAFETY. Never uploads into a live channel and never switches an output on. The
timebase is widened to cover the whole 11 ms record and restored afterwards,
as are every channel's coupling, scale and offset.
"""
import argparse, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ilc_bench as ib
from eomilc import polarimetry as pol
from eomilc.config import CHANNELS, HV_PER_MON, LIMITS

N, DT, FULL_SCALE = 5501, 2e-6, 10.0
PER_STATIC = 4492.0          # extinction measured at theta_a = 90 deg, 31 Aug


def build(peak_hv, gain):
    t = np.arange(N) * DT
    vpk = peak_hv / HV_PER_MON / gain
    u = vpk * (1.0 - np.cos(2 * np.pi * t / (N * DT))) / 2.0
    if abs(u[0]) > 0.1 or abs(u[-1]) > 0.1:
        raise ValueError("record must start and end inside the +-100 mV clamp")
    if vpk > LIMITS.awg_rail:
        raise ValueError(f"peak {vpk:.3f} V exceeds the {LIMITS.awg_rail:g} V rail")
    if peak_hv > LIMITS.hv_max:
        raise ValueError(f"{peak_hv:g} V exceeds hv_max {LIMITS.hv_max:g}")
    return t, u, vpk


TRIM = 5          # samples dropped from each record end


def fit_all(hv, pd, awg, n_eom, gain, log=print):
    """V_pi and the EO zero, whole sweep and each half.

    The first sample of every record is discarded. Measured 31 Aug: sample 0
    carried a 1231 mV residual against 1.68 mV at sample 1, and on its own it
    inflated the rising half's fit residual from 3.1 to 23.6 mV rms and moved
    V_pi by 3 V. Trimming 5 samples removes it completely and trimming 110
    changes nothing further, so it is a one-sample boundary artefact and not a
    settling tail.
    """
    hv, pd, awg = hv[TRIM:-TRIM], pd[TRIM:-TRIM], awg[TRIM:-TRIM]
    o = -(pd.max() - PER_STATIC * pd.min()) / (PER_STATIC - 1)
    log(f"  implied PD offset at this scale: {o*1e3:+.2f} mV "
        f"(fit needs it; V_pi and v_zero do not depend on it)")
    half = int(np.argmax(hv))
    out = {}
    for tag, v, i, guess in (("whole", hv, pd, 5200.0),
                             ("rising", hv[:half], pd[:half], 5200.0),
                             ("falling", hv[half:], pd[half:], 5200.0)):
        c = pol.fit_fringe(v, i, n_eom=n_eom, v_pi_guess=guess,
                           theta_a=0.0, i_dark=o)
        r = i - pol.intensity(pol.phi_of_volts(v, c), c)
        log(f"  {tag:8s} v_pi = {c.v_pi:8.1f} V   v_zero = {c.v_zero:+7.1f} V"
            f"   resid {r.std()*1e3:6.3f} mV rms")
        out[tag] = c
    h = 200 * abs(out["rising"].v_pi - out["falling"].v_pi) / \
        (out["rising"].v_pi + out["falling"].v_pi)
    log(f"  hysteresis rise vs fall: {h:.3f} %  "
        f"({'PASS' if h < 1 else 'FAIL'}, gate 1 %)")
    ca = pol.fit_fringe(awg, pd, n_eom=n_eom,
                        v_pi_guess=5200.0 / (gain * HV_PER_MON),
                        theta_a=0.0, i_dark=o)
    log(f"  commanded: v_pi = {ca.v_pi:.4f} V AWG   -> chain gain "
        f"{out['whole'].v_pi/ca.v_pi/HV_PER_MON:.4f} (config {gain:.4f})")
    return out, ca, h, o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--awg-ch", type=int, default=1)
    ap.add_argument("--eo", default=None,
                    help="calibration channel for the drive gain; defaults to "
                         "EO<awg-ch>. EO1 and EO2 differ by 8.8 %%, so the wrong "
                         "one puts the HV peak that far off nominal.")
    ap.add_argument("--drive-ch", type=int, default=1,
                    help="scope channel carrying the AWG drive, or 0 to fit "
                         "the commanded volts against the programmed waveform "
                         "(exact, and free of the 8-bit quantisation)")
    ap.add_argument("--pd-ch", type=int, default=2)
    ap.add_argument("--mon-ch", type=int, default=3)
    ap.add_argument("--peak-hv", type=float, default=5400.0)
    ap.add_argument("--repeats", type=int, default=32)
    ap.add_argument("--n-eom", type=int, default=1)
    ap.add_argument("--label", required=True)
    ap.add_argument("--capture-only", action="store_true",
                    help="skip the upload; the waveform is already selected and "
                         "the output may be live")
    ap.add_argument("--pd-scale", type=float, default=1.0)
    ap.add_argument("--pd-offset", type=float, default=2.1)
    a = ap.parse_args()

    sib = os.path.dirname(ROOT)
    out = os.path.join(ROOT, "run", "polarimetry")
    os.makedirs(out, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    eo = a.eo or f"EO{a.awg_ch}"
    gain = CHANNELS[eo].gain(5.0)
    print(f"drive gain from {eo}: {gain:.6f}")
    t_grid, u, vpk = build(a.peak_hv, gain)

    if not a.capture_only:
        awg = ib.make_awg(ib.load_module(
            os.path.join(sib, "BK4063B-AWG-GUI", "bk4063b.py"), "bk4063b"))
        awg.connect()
        try:
            if awg.is_on(a.awg_ch) is not False:
                raise SystemExit(f"AWG CH{a.awg_ch} output is ON -- refusing to "
                                 f"upload into a live channel")
            # The arb's playback rate is set by the channel frequency, not by
            # the point count, so a channel left at another waveform's period
            # (CH2 sat at 10.602 ms for the Schroeder set) would stretch this
            # record and put the fringe on the wrong voltage axis.
            f_rec = 1.0 / (N * DT)
            n = awg.upload_arb(a.awg_ch, f"FRSW{a.label}"[:11],
                               u / FULL_SCALE, normalize=False, freq=f_rec)
            bs = awg.get_basic_wave(a.awg_ch)
            per = float(str(bs.get("PERI", "nan")).rstrip("Ss"))
            if abs(per - N * DT) > 1e-6:
                awg.basic_wave(a.awg_ch, FRQ=f_rec)
                bs = awg.get_basic_wave(a.awg_ch)
                per = float(str(bs.get("PERI", "nan")).rstrip("Ss"))
            if abs(per - N * DT) > 1e-6:
                raise SystemExit(f"period is {per*1e3:.4f} ms, need "
                                 f"{N*DT*1e3:.4f} ms -- fix before capturing")
            bt = awg.query_dict(f"C{a.awg_ch}:BTWV?")
            print(f"uploaded {n} pts to AWG CH{a.awg_ch} as "
                  f"{bs.get('WVTP')}, period {per*1e3:.4f} ms, "
                  f"amp {bs.get('AMP')}, peak {vpk:.4f} V -> {a.peak_hv:.0f} V nominal.")
            print(f"  burst {bt.get('STATE')} trig {bt.get('TRSR')} "
                  f"ncyc {bt.get('TIME')}")
            print("  Enable the output, then re-run with --capture-only.")
        finally:
            awg.close()
        return

    sc = ib.make_scope(ib.load_module(ib.find_scope_grab(sib), "scope_grab"))
    sc.connect()
    saved = ib.scope_snapshot(sc, [1, 2, 3, 4])
    tb = {q: sc.try_get(q) for q in (":TIMebase:RANGe", ":TIMebase:POSition")}
    try:
        sc.put(":TIMebase:RANGe", 12.0e-3)      # the record is 11.002 ms; the
        sc.put(":TIMebase:POSition", 6.0e-3)    # production 10 ms window clips it
        quiet = 7 - a.mon_ch                     # the other Trek monitor
        chans = {a.pd_ch: {"coupling": "DC", "scale": a.pd_scale,
                           "offset": a.pd_offset},
                 a.mon_ch: {"coupling": "DC", "scale": 1.0,
                            "offset": a.peak_hv / 2000.0},
                 # The un-driven monitor is the evidence that the other EOM is
                 # really off, so it must be ON SCALE. Left alone on 31 Aug it
                 # sat off-screen and clipped to two codes, reading a spurious
                 # 409-582 V. 0.1 V/div about zero = +-400 V of HV headroom.
                 quiet:    {"coupling": "DC", "scale": 0.1, "offset": 0.0}}
        if a.drive_ch:
            chans[a.drive_ch] = {"coupling": "DC", "scale": 2.0,
                                 "offset": vpk / 2}
        ib.scope_apply(sc, chans)
        print(f"capturing {a.repeats} shots on CH1-4 ...")
        cap = ib.capture_all(sc, [1, 2, 3, 4], t_grid, 0.0, repeats=a.repeats,
                             wait_s=30, points=20000, settle=1.0, keep="both")
    finally:
        sc.put(":TIMebase:RANGe", float(tb[":TIMebase:RANGe"]))
        sc.put(":TIMebase:POSition", float(tb[":TIMebase:POSition"]))
        ib.scope_restore(sc, saved)
        sc.close()

    g = {k: v.mean(axis=0) for k, v in cap.grid.items()}
    p = os.path.join(out, f"frsw_{a.label}_{stamp}.npz")
    np.savez(p, t=t_grid, peak_hv=a.peak_hv, n_eom=a.n_eom, label=a.label,
             **{f"{k}_mean": v for k, v in g.items()},
             **{f"{k}_std": cap.grid[k].std(axis=0, ddof=1) for k in cap.grid})
    print(f"saved {p}\n")
    for c in (3, 4):
        y = g[f"CH{c}"] * 1000.0
        sd = cap.grid[f"CH{c}"].std(axis=0, ddof=1).mean() * 1000.0
        n_lvl = len(np.unique(cap.grid[f"CH{c}"][0]))
        tag = "DRIVEN" if c == a.mon_ch else "should be quiet"
        print(f"  Trek mon CH{c} {y.min():8.1f} .. {y.max():8.1f} V   "
              f"shot std {sd:5.2f} V   {n_lvl:5d} levels   ({tag})")
        if n_lvl < 20:
            print(f"  ** CH{c} resolves only {n_lvl} levels -- it is off-scale "
                  f"and clipped, so its reading means nothing")
        elif c != a.mon_ch and (y.max() - y.min()) > 0.02 * a.peak_hv:
            print(f"  ** CH{c} swings {y.max()-y.min():.0f} V, "
                  f"{100*(y.max()-y.min())/a.peak_hv:.1f} % of the drive -- the "
                  f"other EOM is not off; V_pi below is not a single-EOM number")
    hv = g[f"CH{a.mon_ch}"] * 1000.0
    print(f"  HV peak {hv.max():.0f} V   PD {g[f'CH{a.pd_ch}'].min():.4f} to "
          f"{g[f'CH{a.pd_ch}'].max():.4f} V")
    cmd = g[f"CH{a.drive_ch}"] if a.drive_ch else u
    if not a.drive_ch:
        print("  commanded volts taken from the programmed waveform")
    fit_all(hv, g[f"CH{a.pd_ch}"], cmd, a.n_eom, gain)


if __name__ == "__main__":
    main()
