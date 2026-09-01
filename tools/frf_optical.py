#!/usr/bin/env python3
"""Phase 2: does the Trek monitor tell the truth about the crystal voltage?

Drives one EOM with a ramped multitone and compares what the PHOTODIODE sees
against what the TREK MONITOR says, bin by bin. The monitor is the only signal
every ILC campaign has ever been optimised against, so H_pd / H_mon is the
campaign's central number.

    python tools/frf_optical.py --label M1a                 # upload
    python tools/frf_optical.py --label M1a --capture-only  # measure

WHY THIS BIAS POINT. With the analyser at the EO zero the slope sensitivity
|sin 2phi| peaks at phi = 45 deg, which is V_pi/2. So the most sensitive
operating point available needs no analyser move at all -- the mount stays
where the fringe sweep left it.

WHY A RATIO. Everything on the drive side -- the AWG's amplitude error, the
multitone's crest factor and taper leakage, the plant's own resonance, even
transient ring left over from the ramp edge -- is a real voltage on the
crystal, so the photodiode and the monitor both see it and it divides out.
What survives the ratio is exactly what the campaign is asking about: places
where the light and the monitor disagree.

WHAT MUST BE DIVIDED OUT BY HAND. The two-section 1k/680p anti-alias filter is
in the PHOTODIODE path only (chain: PDA10A2 -> inert BNC LPF -> 1k/680p ->
1k/680p -> scope). Its sections load each other, so it is
an unbuffered ladder, not a set of independent poles. Measured directly 1 Sep:
two sections at 1.53x the nominal RC, f_-3dB 57 kHz, group delay 3.1 us -- see
aa_response(). At 40 kHz that is 1.8 dB and 40 deg. Left in, it would fake precisely the high-frequency infidelity this
measurement exists to find; corrected with the wrong section count it fakes a
smaller version of the same thing.

SAFETY. The EOMs are never parked at a standing kV bias: the record ramps up,
holds the tone, and ramps back to the +-100 mV end clamp inside the burst.
Never uploads into a live channel.
"""
import argparse, json, os, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ilc_bench as ib
import sysid_make as sm
from eomilc.config import HV_PER_MON, LIMITS

N, DT, FULL_SCALE = 5501, 2e-6, 10.0
N_EDGE, N_SETTLE = 500, 500
N_HOLD = N - 2 * N_EDGE - 2 * N_SETTLE          # 3501 -> 142.816 Hz bins
RC_NOMINAL = 1000.0 * 680e-12                   # what the parts list implies


N_AA = 2                 # sections -- MEASURED, see aa_response
RC_AA = 1.040e-6         # measured effective RC, 1.53x the nominal 1k x 680p


def aa_response(f):
    """The PD-path RC ladder, measured electrically rather than assumed.

    Driving the filter from the AWG (same 50 ohm source the PDA10A2 presents)
    and reading its output where the photodiode normally sits, 1 Sep:

        n = 2, RC = 1.04 us = 1.53 x nominal, f_-3dB = 57 kHz

    Two runs agree to 1.5 %: 1.0477 us over 0.43-40 kHz (residual 0.025 dB,
    0.11 deg) and 1.0319 us over 0.43-136 kHz (0.067 dB, 0.43 deg). The wider
    span is what separates the orders -- in band alone n=2/3/4 fit equally well,
    across the corner n=2 wins by 2-3x. So the parts-list topology is right and
    the components simply run about half again their nominal RC; an earlier
    reading this session that called it three sections was wrong.

    Prefer the measured response itself (filt_F1_result.npz) over this model
    when correcting a fidelity run -- it is the same bins and needs no fit.
    """
    x = 1j * 2 * np.pi * np.asarray(f, float) * RC_AA
    coef = {1: [1, 1], 2: [1, 3, 1], 3: [1, 6, 5, 1]}[N_AA]
    return 1.0 / sum(c * x ** i for i, c in enumerate(coef))


def tone_bins(n_tones, k_lo, k_hi):
    """Log-spaced ODD bins.

    Odd-only is not decoration. Second-order intermodulation between two odd
    bins lands on an even bin, so the even bins are left empty as a live
    distortion monitor: if the fringe is being driven outside its linear range
    the evidence shows up there and nowhere else.
    """
    k = np.unique(np.round(np.geomspace(k_lo, k_hi, n_tones)).astype(int))
    k = np.unique(k + (k + 1) % 2)              # nudge each to the next odd
    return k[(k >= k_lo) & (k <= k_hi)]


def build(v_dc_hv, peak_hv, gain, bins, taper_s):
    v_dc = v_dc_hv / HV_PER_MON / gain
    peak = peak_hv / HV_PER_MON / gain
    u, win = sm.ramped_multitone(peak, v_dc, bins, N_HOLD, dt=DT,
                                 n_edge=N_EDGE, n_settle=N_SETTLE,
                                 taper_s=taper_s, awg_rail=LIMITS.awg_rail)
    if len(u) != N:
        raise ValueError(f"record is {len(u)} points, expected {N}")
    if (v_dc_hv + peak_hv) > LIMITS.hv_max:
        raise ValueError(f"{v_dc_hv + peak_hv:g} V exceeds hv_max {LIMITS.hv_max:g}")
    return u, win, v_dc, peak


def fringe_scale(pd, mon, i_edge, v_pi, v_zero, theta_a=None, log=print):
    """A and B, and the analyser angle, from the record's own ramp edge.

    The two-point trick (record start vs the hold) only works when those two
    points sit at different heights on the fringe. At theta_a = 45 deg with a
    top-of-ramp bias they are both near mid-fringe and it collapses -- the
    denominator went to 0.045 -- so instead fit

        I = A + B cos(2(phi - theta_a))

    over the ramp-up edge, where phi is read from the monitor and sweeps the
    better part of a quarter fringe. Linear in (A, B) at fixed theta_a, so
    theta_a is scanned. That makes the analyser angle a MEASURED output of
    every run rather than a number carried in from the mount.
    """
    phi = (0.5 * np.pi / v_pi) * (np.asarray(mon[5:i_edge]) * 1000.0 - v_zero)
    y = np.asarray(pd[5:i_edge])
    scan = (np.radians(np.linspace(-90, 90, 3601)) if theta_a is None
            else [np.radians(theta_a)])
    best = None
    for th in scan:
        M = np.column_stack([np.ones_like(phi), np.cos(2.0 * (phi - th))])
        c, *_ = np.linalg.lstsq(M, y, rcond=None)
        r = float(((y - M @ c) ** 2).sum())
        if best is None or r < best[0]:
            best = (r, th, c[0], c[1])
    r, th, a, b = best
    resid = np.sqrt(r / len(y))
    log(f"  analyser measured from the edge: theta_a = {np.degrees(th):+.2f} deg, "
        f"A {a:.4f} V, B {b:.4f} V, edge fit residual {resid*1e3:.2f} mV")
    if b < 0:                       # a negative B is the same fringe at th+90
        b, th = -b, th + 0.5 * np.pi
        log(f"    (folded to B > 0: theta_a = {np.degrees(th):+.2f} deg)")
    return a, b, th


def analyse(cap, win, bins, v_pi, v_zero, mon_ch, pd_ch, cmd_ch,
            theta_a=None, log=print):
    i0, i1 = win
    n = i1 - i0
    f = np.arange(n) / (n * DT)
    g = {k: v.mean(axis=0) for k, v in cap.grid.items()}
    seg = {k: v[i0:i1] for k, v in g.items()}
    pd, mon, cmd = seg[f"CH{pd_ch}"], seg[f"CH{mon_ch}"], seg[f"CH{cmd_ch}"]

    # Fringe scale straight out of this record: the ramp starts at the EO zero
    # (phi = 0, I = A + B) and the hold sits at phi = 45 deg (I = A). No fit and
    # no dark reading needed, so the scope's scale-dependent offset error
    # cannot get in.
    # sample 0 is skipped: it carries a ~1235 mV artefact on every record this
    # scope returns (measured on the fringe sweeps AND here, to within 4 mV).
    # Averaged into I(phi=0) it biased B by -3.02 %, which showed up as a 3 %
    # excess in the low-frequency fidelity -- i.e. as a fake result.
    i_zero = float(g[f"CH{pd_ch}"][5:25].mean())
    i_hold = float(pd.mean())
    dphi_dv = 0.5 * np.pi / v_pi
    # phi at the operating point, read from the monitor rather than assumed --
    # the record starts at the EO zero (I = A + B) and holds at phi_bias
    # (I = A + B cos 2phi), which is two equations for A and B.
    phi = dphi_dv * (float(mon.mean()) * 1000.0 - v_zero)
    _, b, th = fringe_scale(g[f"CH{pd_ch}"], g[f"CH{mon_ch}"], i0 - 200,
                            v_pi, v_zero, theta_a, log=log)
    sens = np.sin(2.0 * (phi - th))
    if abs(sens) < 0.2:
        raise ValueError(f"sensitivity is only {abs(sens):.3f} at phi = "
                         f"{np.degrees(phi):.1f} deg with the analyser at "
                         f"{np.degrees(th):.1f} deg -- this bias is blind, move one")
    didv = -2.0 * b * sens * dphi_dv
    log(f"  operating point: monitor hold {mon.mean()*1000:.1f} V -> phi "
        f"{np.degrees(phi):.2f} deg, sensitivity {abs(sens):.4f}")
    log(f"  fringe scale in-record: I(phi=0) {i_zero:.4f} V, I(hold) "
        f"{i_hold:.4f} V -> B {b:.4f} V, dI/dV {didv*1e3:+.4f} mV per HV volt")

    W = np.fft.rfft(np.asarray(pd) - pd.mean()), np.fft.rfft(np.asarray(mon) - mon.mean())
    PD, MON = W
    CMD = np.fft.rfft(np.asarray(cmd) - cmd.mean())
    k = np.asarray(bins)
    fk = f[k]

    # per-bin uncertainty from the shot-to-shot scatter, not from a coherence
    # estimate -- with a deterministic drive the repeats ARE the ensemble
    rep = {c: np.array([np.fft.rfft(cap.grid[f"CH{c}"][j, i0:i1]
                                    - cap.grid[f"CH{c}"][j, i0:i1].mean())[k]
                        for j in range(cap.grid[f"CH{c}"].shape[0])])
           for c in (pd_ch, mon_ch)}
    m = cap.grid[f"CH{pd_ch}"].shape[0]
    snr = {c: np.abs(rep[c].mean(axis=0)) /
              (rep[c].std(axis=0, ddof=1) / np.sqrt(m)) for c in rep}

    pd_hv = PD[k] / didv                         # PD volts -> equivalent HV volts
    pd_hv = pd_hv / aa_response(fk)              # undo the PD-path filter
    mon_hv = MON[k] * HV_PER_MON
    fid = pd_hv / mon_hv

    # distortion: the even bins between the tones should hold nothing
    ev = np.arange(2, k.max() + 2, 2)
    ev = ev[~np.isin(ev, k)]
    dist = np.abs(PD[ev]).max() / np.abs(PD[k]).mean()
    # Distortion scatters across the band; a settling tail is concentrated at
    # the bottom of it. Report both so they cannot be confused -- on 31 Aug the
    # -13 dBc worst case was entirely the ramp edge still ringing.
    hi = ev[ev > k.max() // 2]
    log(f"  even-bin monitor: worst {20*np.log10(dist):.1f} dBc (bin "
        f"{ev[np.argmax(np.abs(PD[ev]))]}), upper-band median "
        f"{20*np.log10(np.median(np.abs(PD[hi]))/np.abs(PD[k]).mean()):.1f} dBc")
    log(f"  -> concentrated low = ramp settling (common mode, divides out); "
        f"scattered = fringe nonlinearity (does not)")

    log("")
    log("     f (Hz)   |H_pd/H_mon|    dB     phase     SNR_pd  SNR_mon   AA corr")
    for j, kk in enumerate(k):
        log("  %9.1f    %8.5f  %+7.3f  %+8.2f deg  %6.0f  %7.0f   %+6.2f dB"
            % (fk[j], abs(fid[j]), 20*np.log10(abs(fid[j])),
               np.degrees(np.angle(fid[j])), snr[pd_ch][j], snr[mon_ch][j],
               -20*np.log10(abs(aa_response(fk[j])))))
    return dict(f=fk, fid=fid, snr_pd=snr[pd_ch], snr_mon=snr[mon_ch],
                b=b, didv=didv, i_zero=i_zero, i_hold=i_hold,
                distortion_dbc=20*np.log10(dist),
                h_mon=mon_hv / CMD[k], h_pd=pd_hv / CMD[k])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--awg-ch", type=int, default=1)
    ap.add_argument("--cmd-ch", type=int, default=1)
    ap.add_argument("--pd-ch", type=int, default=2)
    ap.add_argument("--mon-ch", type=int, default=3)
    ap.add_argument("--quiet-ch", type=int, default=None,
                    help="the UNDRIVEN Trek monitor, kept on scale as the check "
                         "that the other EOM is off. Defaults to the other of "
                         "CH3/CH4, which is wrong once the PD and monitor are "
                         "swapped onto different channels -- pass it explicitly.")
    ap.add_argument("--eo", default=None)
    ap.add_argument("--v-pi", type=float, default=None,
                    help="default: the measured value from vpi_summary.json")
    ap.add_argument("--peak-hv", type=float, default=250.0)
    ap.add_argument("--tones", type=int, default=48)
    ap.add_argument("--k-lo", type=int, default=3)      # 428 Hz
    ap.add_argument("--k-hi", type=int, default=279)    # 39.85 kHz
    ap.add_argument("--taper-us", type=float, default=100.0)
    ap.add_argument("--pd-offset", type=float, default=2.9,
                    help="screen centre for the PD; the fringe only runs from "
                         "I0 down to I0/2 here")
    ap.add_argument("--pd-scale", type=float, default=0.5)
    ap.add_argument("--theta-a", type=float, default=None,
                    help="analyser angle in degrees from the EO zero. "
                         "Default: fitted from the ramp edge, which is "
                         "better than trusting the mount.")
    ap.add_argument("--mon-scale", type=float, default=None,
                    help="default 0.5 V/div, which clips once the bias "
                         "passes ~3.4 kV")
    ap.add_argument("--v-dc", type=float, default=None,
                    help="bias in HV volts; default V_pi/2, the maximum-"
                         "sensitivity point with the analyser at the EO zero")
    ap.add_argument("--repeats", type=int, default=64)
    ap.add_argument("--label", required=True)
    ap.add_argument("--capture-only", action="store_true")
    a = ap.parse_args()

    sib = os.path.dirname(ROOT)
    out = os.path.join(ROOT, "run", "polarimetry")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    eo = a.eo or f"EO{a.awg_ch}"
    key = {"EO1": "X1b", "EO2": "X2a"}[eo]
    vs = json.load(open(os.path.join(out, "vpi_summary.json")))
    v_pi = a.v_pi or vs["rows"][key]["v_pi_hv"]
    v_zero = vs["rows"][key]["v_zero"]
    gain = vs["rows"][key]["gain_meas"]           # measured, not the config value
    v_dc_hv = a.v_dc if a.v_dc is not None else 0.5 * v_pi
    bins = tone_bins(a.tones, a.k_lo, a.k_hi)
    u, win, v_dc, peak = build(v_dc_hv, a.peak_hv, gain, bins, a.taper_us * 1e-6)
    df = 1.0 / (N_HOLD * DT)
    print(f"{eo}: V_pi {v_pi:.1f} V, measured chain gain {gain:.4f}")
    print(f"  bias {v_dc_hv:.1f} V HV = {v_dc:.4f} V AWG (phi = 45 deg, "
          f"sensitivity 1.000)")
    print(f"  tone peak {a.peak_hv:.0f} V HV = {peak:.4f} V AWG, "
          f"rail use {abs(v_dc)+peak:.3f} / {LIMITS.awg_rail:g} V")
    print(f"  {len(bins)} odd bins, {df*bins[0]:.1f} Hz to {df*bins[-1]/1e3:.2f} kHz, "
          f"resolution {df:.3f} Hz, window {win[0]}..{win[1]}")

    if not a.capture_only:
        awg = ib.make_awg(ib.load_module(
            os.path.join(sib, "BK4063B-AWG-GUI", "bk4063b.py"), "bk4063b"))
        awg.connect()
        try:
            if awg.is_on(a.awg_ch) is not False:
                raise SystemExit(f"AWG CH{a.awg_ch} output is ON -- refusing to "
                                 f"upload into a live channel")
            f_rec = 1.0 / (N * DT)
            n = awg.upload_arb(a.awg_ch, f"FRF{a.label}"[:11],
                               u / FULL_SCALE, normalize=False, freq=f_rec)
            bs = awg.get_basic_wave(a.awg_ch)
            per = float(str(bs.get("PERI", "nan")).rstrip("Ss"))
            if abs(per - N * DT) > 1e-6:
                raise SystemExit(f"period {per*1e3:.4f} ms, need {N*DT*1e3:.4f} ms")
            print(f"uploaded {n} pts to CH{a.awg_ch}, period {per*1e3:.4f} ms, "
                  f"amp {bs.get('AMP')}")
        finally:
            awg.close()
        np.savez(os.path.join(out, f"frf_{a.label}_drive.npz"),
                 u=u, bins=bins, win=win, v_pi=v_pi, gain=gain,
                 v_dc_hv=v_dc_hv, peak_hv=a.peak_hv)
        print("  drive saved; enable the output and re-run with --capture-only")
        return

    sc = ib.make_scope(ib.load_module(ib.find_scope_grab(sib), "scope_grab"))
    sc.connect()
    saved = ib.scope_snapshot(sc, [1, 2, 3, 4])
    tb = {q: sc.try_get(q) for q in (":TIMebase:RANGe", ":TIMebase:POSition")}
    try:
        sc.put(":TIMebase:RANGe", 12.0e-3)
        sc.put(":TIMebase:POSition", 6.0e-3)
        quiet = a.quiet_ch or (7 - a.mon_ch)
        if quiet in (a.pd_ch, a.mon_ch, a.cmd_ch) or quiet not in (1, 2, 3, 4):
            raise SystemExit(f"quiet channel {quiet} clashes or does not exist; "
                             f"pass --quiet-ch")
        chans = {
            # the command channel has to hold 0..v_dc+peak, which at the top
            # ramp offset is most of the 10 V rail -- a fixed 1 V/div clips it
            a.cmd_ch: {"coupling": "DC",
                       "scale": max(1.0, (abs(v_dc) + peak) / 3.6),
                       "offset": v_dc / 2},
            # the fringe only runs from I0 down to I0/2 here, so the PD fits on
            # a much finer scale than the full sweep needed
            a.pd_ch:  {"coupling": "DC", "scale": a.pd_scale,
                       "offset": a.pd_offset},
            a.mon_ch: {"coupling": "DC",
                       "scale": a.mon_scale or 0.5,
                       "offset": v_dc_hv / 2000.0},
            quiet:    {"coupling": "DC", "scale": 0.2, "offset": 0.0}}
        ib.scope_apply(sc, chans)
        t_grid = np.arange(N) * DT
        print(f"capturing {a.repeats} shots ...")
        cap = ib.capture_all(sc, [1, 2, 3, 4], t_grid, 0.0, repeats=a.repeats,
                             wait_s=30, points=20000, settle=1.0, keep="both")
    finally:
        sc.put(":TIMebase:RANGe", float(tb[":TIMebase:RANGe"]))
        sc.put(":TIMebase:POSition", float(tb[":TIMebase:POSition"]))
        ib.scope_restore(sc, saved)
        sc.close()

    g = {k: v.mean(axis=0) for k, v in cap.grid.items()}
    p = os.path.join(out, f"frf_{a.label}_{stamp}.npz")
    np.savez(p, t=np.arange(N) * DT, u=u, bins=bins, win=win, v_pi=v_pi,
             gain=gain, v_dc_hv=v_dc_hv, peak_hv=a.peak_hv,
             **{f"{k}_mean": v for k, v in g.items()},
             **{f"{k}_std": cap.grid[k].std(axis=0, ddof=1) for k in cap.grid})
    print(f"saved {p}")
    for c in (a.mon_ch, a.quiet_ch or (7 - a.mon_ch)):
        y = g[f"CH{c}"] * 1000.0
        nl = len(np.unique(cap.grid[f"CH{c}"][0]))
        tag = "DRIVEN" if c == a.mon_ch else "should be quiet"
        print(f"  Trek mon CH{c} {y.min():8.1f} .. {y.max():8.1f} V   "
              f"{nl:5d} levels   ({tag})")
        if nl < 20:
            print(f"  ** CH{c} clipped -- reading means nothing")
    print(f"  PD {g[f'CH{a.pd_ch}'].min():.4f} .. {g[f'CH{a.pd_ch}'].max():.4f} V")
    # scope_apply already verifies the scope ACCEPTED the setup. This checks the
    # other half -- that the signal actually landed inside it. On 31 Aug the PD
    # sat 2.9 divisions below its window while every command read back correct,
    # because X2's Trek had wandered to -5 kV and moved the operating point.
    bad = False
    for c, w in chans.items():
        lo, hi = w["offset"] - 4*w["scale"], w["offset"] + 4*w["scale"]
        y = g[f"CH{c}"]
        if y.min() < lo or y.max() > hi:
            bad = True
            print(f"  ** CH{c} ran {y.min():.4f}..{y.max():.4f} V but the window "
                  f"is {lo:.4f}..{hi:.4f} V at {w['scale']} V/div -- OUT OF WINDOW")
    if bad:
        raise SystemExit("a channel was outside its window; the numbers below "
                         "would be meaningless. Fix the levels and re-run.")
    r = analyse(cap, win, bins, v_pi, v_zero, a.mon_ch, a.pd_ch, a.cmd_ch,
                theta_a=a.theta_a)
    json.dump({k: (np.asarray(v).tolist() if np.ndim(v) else float(v))
               for k, v in r.items() if not np.iscomplexobj(v)},
              open(os.path.join(out, f"frf_{a.label}_summary.json"), "w"), indent=1)
    np.savez(os.path.join(out, f"frf_{a.label}_result.npz"), **r)
    print(f"  saved frf_{a.label}_result.npz")


if __name__ == "__main__":
    main()
