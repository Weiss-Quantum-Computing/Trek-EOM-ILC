"""Learned target corrections: the outer loop around the ILC.

The ILC makes a MEASURED signal (the Trek monitor, or whatever a GEN channel
reads) follow its target. When the outcome that matters is measured by
something else -- the light's polarization by the rotating-analyzer
polarimeter, or anything another system's control signal is meant to
produce -- the error that measured signal cannot see is fed back by moving
the TARGET, not the drive. The inner loop then learns the drive that puts
the measured signal on the new target. Changing the drive directly would be
undone: the next ILC update pulls the monitor back onto the old target.

A correction is a waveform in the target's own units (output volts, the
`voltage_V` column of a target CSV: V at the EOM on EO1/EO2), on the
record's time axis, with a per-sample standard error and a header saying
where it came from. Any program can write one (`write_correction`); the
polarimeter's tools/target_compare.py is the first. The ILC panel gates it
(`gate`) and refuses a total it cannot play (`check_feasible`).

What the gate refuses to let through, and why:
  * anything above the correction band -- the outer measurement is slow and
    noisy; above a few kHz it is mostly the measurement (the polarimeter
    resolves 10 mdeg per sample, the ramps sweep 120 deg/ms);
  * anything not resolved by the measurement that produced it (|delta| under
    k x its low-passed standard error) -- that is noise, and an ILC learns
    noise into the drive;
  * anything smaller than the monitor measurement can verify (the floor):
    the inner loop cannot tell a 20 uV target change from its own scatter;
  * anything at the record's ends, where the AWG holds the idle level;
  * a total the Trek chain cannot play (check_feasible: rail, HV, slew,
    current, the AWG full scale) or that is bigger than the cap.
"""
from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass, field

import numpy as np

FORMAT = "EOM-ILC target correction v1"


# ------------------------------------------------------------------- file
@dataclass
class Correction:
    t: np.ndarray                 # s from the record start (target CSV time)
    delta: np.ndarray             # target units (output V)
    sigma: np.ndarray | None      # per-sample standard error, same units
    meta: dict = field(default_factory=dict)
    path: str = ""

    @property
    def name(self):
        return os.path.basename(self.path) if self.path else "(unsaved)"

    @property
    def slot(self):
        """What the correction is FOR (e.g. 'optical'): a new file in the same
        slot replaces the old one rather than stacking on it."""
        return str(self.meta.get("slot", "") or self.name)


def _meta_value(text):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        return text


def write_correction(path, t, delta, sigma=None, meta=None):
    """Write the CSV: a format line, '# key: value' header lines (values as
    JSON when they are not plain text), then time_us,delta_V[,sigma_V].

    sigma is the standard error of ONE sample at the file's own spacing, as
    if unfiltered (independent from sample to sample); the gate filters it
    the way it filters delta. Leave it out only if there is no estimate.

    Keys the panel reads (all optional): channel, units, quantity,
    sensitivity, band_hz, slot, source, method, line_removed, target_n,
    target_dt_us, target_peak_V, target_rms_V, target_file, created,
    producer, notes."""
    meta = dict(meta or {})
    meta.setdefault("created", datetime.datetime.now().isoformat(timespec="seconds"))
    t = np.asarray(t, float)
    delta = np.asarray(delta, float)
    if len(t) != len(delta):
        raise ValueError("t and delta differ in length")
    cols = [t * 1e6, delta]
    head = "time_us,delta_V"
    if sigma is not None:
        sigma = np.asarray(sigma, float)
        if len(sigma) != len(t):
            raise ValueError("sigma differs in length from t")
        cols.append(sigma)
        head += ",sigma_V"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# {FORMAT}\n")
        for k, v in meta.items():
            v = v if isinstance(v, str) else json.dumps(v)
            fh.write(f"# {k}: {v}\n")
        fh.write(head + "\n")
        for row in zip(*cols):
            fh.write(",".join(f"{x:.9g}" for x in row) + "\n")
    return path


def read_correction(path) -> Correction:
    meta, head, rows = {}, None, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                body = s[1:].strip()
                if body == FORMAT:
                    meta["format"] = FORMAT
                    continue
                k, sep, v = body.partition(":")
                if sep:
                    meta[k.strip()] = _meta_value(v)
                continue
            if head is None:
                head = [c.strip() for c in s.split(",")]
                continue
            rows.append([float(x) for x in s.split(",")])
    if head is None or not rows:
        raise ValueError(f"{os.path.basename(path)}: no data")
    if meta.get("format") != FORMAT:
        raise ValueError(f"{os.path.basename(path)} is not a target correction "
                         f"(its first line must be '# {FORMAT}'). A TARGET CSV "
                         f"goes in the Target box, not here.")
    if head[:2] != ["time_us", "delta_V"]:
        raise ValueError(f"{os.path.basename(path)}: columns are {head}, "
                         f"expected time_us,delta_V[,sigma_V]")
    a = np.asarray(rows, float)
    sig = a[:, 2] if a.shape[1] > 2 and "sigma_V" in head else None
    return Correction(t=a[:, 0] * 1e-6, delta=a[:, 1], sigma=sig, meta=meta,
                      path=os.path.abspath(path))


def target_fingerprint(target_out, dt):
    """What a correction records about the target it was computed against,
    in output units: enough to catch the wrong campaign, tolerant of the
    6-decimal rounding a CSV round trip does."""
    v = np.asarray(target_out, float)
    return {"target_n": int(len(v)), "target_dt_us": round(float(dt) * 1e6, 6),
            "target_peak_V": round(float(np.max(np.abs(v))), 4),
            "target_rms_V": round(float(np.sqrt(np.mean(v * v))), 4)}


def check_target(meta, target_out, dt):
    """[(level, text)] comparing the correction's recorded target with the
    session's. A correction learned against one target is wrong on another:
    the polarization error is a function of where the ramp is."""
    fp = target_fingerprint(target_out, dt)
    out = []
    if "target_n" not in meta:
        out.append(("warn", "the correction does not say which target it was "
                            "measured against -- make sure it is this one"))
        return out
    if int(meta["target_n"]) != fp["target_n"] or \
            abs(float(meta.get("target_dt_us", fp["target_dt_us"])) - fp["target_dt_us"]) > 1e-3:
        out.append(("fail", f"recorded target grid {meta['target_n']} pts at "
                            f"{meta.get('target_dt_us')} us, this session's is "
                            f"{fp['target_n']} at {fp['target_dt_us']} us"))
        return out
    for k in ("target_peak_V", "target_rms_V"):
        a, b = float(meta.get(k, fp[k])), fp[k]
        if abs(a - b) > 1e-3 * max(abs(b), 1.0):
            out.append(("fail", f"recorded {k} {a:g} V but this session's base "
                                f"target has {b:g} V -- the correction belongs "
                                f"to another target ({meta.get('target_file', '?')})"))
    return out


# ----------------------------------------------------------------- filters
def lowpass(x, dt, f_cut):
    """Zero-phase low-pass: cosine roll-off from 0.7 f_cut to f_cut, on the
    record mirrored so its ends do not wrap into each other."""
    x = np.asarray(x, float)
    n = len(x)
    ext = np.r_[x, x[::-1]]
    f = np.fft.rfftfreq(len(ext), dt)
    w = np.clip((f_cut - f) / (0.3 * f_cut), 0, 1)
    return np.fft.irfft(np.fft.rfft(ext) * (0.5 - 0.5 * np.cos(np.pi * w)),
                        len(ext))[:n]


def lowpass_sigma(sigma, dt, f_cut):
    """Per-sample standard error after `lowpass`, for independent per-sample
    errors: sqrt(sum h^2 x local sigma^2), h the filter's impulse response.
    A 2 kHz band on a 0.5 us grid averages ~400 samples, so 10 mdeg per
    sample becomes ~0.5 mdeg -- which is why the significance test must be
    made on the FILTERED correction, not sample by sample."""
    sigma = np.asarray(sigma, float)
    n = len(sigma)
    imp = np.zeros(n)
    imp[n // 2] = 1.0
    h = lowpass(imp, dt, f_cut)
    h2 = float(np.sum(h * h))
    # the local variance, smoothed over the filter's own span
    var = np.maximum(lowpass(sigma ** 2, dt, f_cut), 0.0)
    return np.sqrt(h2 * var)


def shrink(x, thr):
    """x where |x| is well above thr, smoothly to 0 below it:
    x * max(0, 1 - (thr/|x|)^2). Unbiased for large values (unlike a soft
    threshold) and continuous (unlike a hard one)."""
    x = np.asarray(x, float)
    thr = np.broadcast_to(np.asarray(thr, float), x.shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = 1.0 - (thr / np.abs(x)) ** 2
    w = np.where(np.isfinite(w), np.clip(w, 0.0, 1.0), 0.0)
    return x * w


def end_window(n, dt, fade_s):
    """1 in the middle, raised-cosine to exactly 0 at both ends over fade_s."""
    w = np.ones(n)
    k = int(round(fade_s / dt)) if fade_s > 0 else 0
    k = min(max(k, 1), n // 2)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, k + 1))[:-1]
    w[:k] = ramp
    w[n - k:] = ramp[::-1]
    w[0] = w[-1] = 0.0
    return w


def active_window(base_out, dt, frac=0.02, smooth_s=100e-6):
    """Where the base target is away from its idle level, as a smooth 0..1
    weight: a correction is only learned where the record was doing
    something, so the idle stretches (the AWG holds the first sample between
    bursts) are left exactly as they were."""
    v = np.asarray(base_out, float)
    idle = 0.5 * (v[0] + v[-1])
    span = float(np.max(np.abs(v - idle)))
    if span <= 0:
        return np.zeros_like(v)
    a = np.abs(v - idle) / span
    w = np.clip(a / frac, 0.0, 1.0)
    if smooth_s > 0:
        w = np.clip(lowpass(w, dt, 1.0 / (2 * smooth_s)), 0.0, 1.0)
    # exactly zero where the target IS at its idle level (under 0.2 % of its
    # span): the smoothing must not reach back into the idle stretch
    w[a < 0.1 * frac] = 0.0
    return w


def _onto(t_dst, t_src, y, dt_src, dt_dst):
    """y onto t_dst: box-averaged first when the source is finer (so its
    noise really is averaged, as the sigma scaling assumes -- plain
    interpolation would only pick samples), zero outside the source span."""
    y = np.asarray(y, float)
    n = int(round(dt_dst / dt_src)) if dt_src < dt_dst else 1
    if n > 1:
        y = np.convolve(y, np.ones(n) / n, mode="same")
    return np.interp(t_dst, t_src, y, left=0.0, right=0.0)


# -------------------------------------------------------------------- gate
@dataclass
class GateSettings:
    band_hz: float = 2000.0       # correction band: nothing above is applied
    k_sigma: float = 3.0          # resolved = |delta| > k x its filtered error
    floor_out: float = 0.0        # smallest change the inner loop can verify
    cap_out: float = float("inf")  # largest total change allowed, output units
    fade_s: float = 200e-6        # both record ends fade to exactly zero
    active_only: bool = True      # only where the base target is active
    active_frac: float = 0.02


def gate(corr: Correction, t_grid, base_out, settings: GateSettings,
         gain: float = 1.0):
    """The part of `corr` the panel will apply, on the session grid, in
    output units, and a report.

    Steps, in order: onto the grid (zero outside the correction's span);
    low-pass at the band (min of the file's band_hz and settings.band_hz);
    shrink to zero where it is not resolved -- |delta| under
    sqrt((k sigma_lp)^2 + floor^2); fade at the ends and outside the active
    stretch; low-pass again (the weights must not add content above the
    band); ends exactly zero; times gain.

    Returns (delta_out, info). info has raw/kept peak and rms, the band,
    sigma_lp's median, the fraction of the active record that was resolved,
    and 'notes' [(level, text)]."""
    t_grid = np.asarray(t_grid, float)
    dt = float(np.median(np.diff(t_grid)))
    n = len(t_grid)
    notes = []
    lo, hi = float(corr.t[0]), float(corr.t[-1])
    span = t_grid[-1] - t_grid[0]
    if hi < t_grid[0] or lo > t_grid[-1]:
        raise ValueError(f"{corr.name} covers {lo*1e3:.3f}..{hi*1e3:.3f} ms, "
                         f"the record {t_grid[0]*1e3:.3f}..{t_grid[-1]*1e3:.3f} ms")
    if lo > t_grid[0] + 0.01 * span or hi < t_grid[-1] - 0.01 * span:
        notes.append(("warn", f"covers {lo*1e3:.3f}..{hi*1e3:.3f} ms of the "
                              f"{t_grid[0]*1e3:.3f}..{t_grid[-1]*1e3:.3f} ms record; "
                              f"zero outside"))
    grid_dt = float(np.median(np.diff(corr.t)))
    d = _onto(t_grid, corr.t, corr.delta, grid_dt, dt)
    if abs(grid_dt - dt) > 1e-3 * dt:
        notes.append(("note", f"resampled from {grid_dt*1e6:.3g} us onto the "
                              f"{dt*1e6:.3g} us record grid"))
    band = float(settings.band_hz)
    fb = corr.meta.get("band_hz")
    if isinstance(fb, (int, float)) and fb > 0 and fb < band:
        band = float(fb)
    raw = d.copy()
    d = lowpass(d, dt, band)
    if corr.sigma is not None:
        s = np.interp(t_grid, corr.t, corr.sigma, left=0.0, right=0.0)
        # a finer file is box-averaged onto the record (_onto), so per sample
        # of the RECORD the error is the file's over sqrt(samples merged)
        n_box = int(round(dt / grid_dt)) if grid_dt < dt else 1
        if n_box > 1:
            s = s / np.sqrt(n_box)
        s_lp = lowpass_sigma(s, dt, band)
    else:
        s_lp = np.zeros(n)
        notes.append(("warn", "no sigma column: the correction cannot be "
                              "tested against its own noise -- only the floor "
                              "and the band apply"))
    thr = np.sqrt((settings.k_sigma * s_lp) ** 2 + settings.floor_out ** 2)
    kept = shrink(d, thr)
    w_end = end_window(n, dt, settings.fade_s)
    w_act = (active_window(base_out, dt, settings.active_frac)
             if settings.active_only else np.ones(n))
    w = w_end * w_act
    # the weights are smooth, but filter again so the shrink and the active
    # window add nothing above the band; the end fade goes on last so both
    # ends are exactly the idle level
    kept = lowpass(kept * w_act, dt, band) * w_end * w_act
    kept[0] = kept[-1] = 0.0
    kept *= float(gain)
    act = w > 0.5
    resolved = (np.abs(d) > thr) & act
    info = dict(band_hz=band,
                raw_peak=float(np.max(np.abs(raw))),
                raw_rms=float(np.sqrt(np.mean(raw[act] ** 2))) if act.any() else 0.0,
                lp_peak=float(np.max(np.abs(d * w))),
                kept_peak=float(np.max(np.abs(kept))),
                kept_rms=float(np.sqrt(np.mean(kept[act] ** 2))) if act.any() else 0.0,
                sigma_lp_median=float(np.median(s_lp[act])) if act.any() else 0.0,
                resolved_frac=float(resolved.sum() / max(act.sum(), 1)),
                gain=float(gain), notes=notes,
                # arrays, for plotting: the band-limited correction before the
                # shrink, the threshold it was held against, the weights
                lp=d * w_act * w_end * float(gain), thr=thr * float(gain),
                weight=w)
    if not corr.meta.get("line_removed", False):
        notes.append(("warn", "the file does not say line (mains) ripple was "
                              "removed. On a line-synchronous trigger the "
                              "ripple is phase-locked to the record and would "
                              "be learned as error"))
    if info["resolved_frac"] < 0.5:
        notes.append(("warn", f"only {100*info['resolved_frac']:.0f}% of the "
                              f"active record is resolved above "
                              f"{settings.k_sigma:g} sigma / the floor -- most "
                              f"of this correction is held at zero"))
    return kept, info


def check_feasible(total_out, base_out, u_now, dt, channel, limits,
                   plant_gain, full_scale, learn_band_hz, band_hz,
                   cap_out=float("inf")):
    """[(level, text)] for putting the target at base + total.

    The drive the loop will converge to is estimated as u_now + total/gain
    (inside a band of a few kHz the chain is its DC gain), and the new
    target is checked the way every drive is: the AWG rail, the post-
    divider rail, HV, slew and current (eomilc.ilc.check_limits), plus the
    upload's +/-full_scale mapping, the cap, and whether the inner loop can
    learn this band at all. 'fail' means the panel must not apply it."""
    from .ilc import check_limits
    out = []
    scale = channel.mon_scale
    tot_mon = np.asarray(total_out, float) / scale
    base_mon = np.asarray(base_out, float) / scale
    new = base_mon + tot_mon
    u_pred = np.asarray(u_now, float) + tot_mon / float(plant_gain)
    peak = float(np.max(np.abs(total_out)))
    if peak > cap_out:
        out.append(("fail", f"total change {peak:.1f} V peak is over the "
                            f"{cap_out:g} V cap -- lower a gain, or raise the "
                            f"cap on purpose"))
    if band_hz > learn_band_hz:
        out.append(("fail", f"correction band {band_hz:g} Hz is above what the "
                            f"loop learns ({learn_band_hz:g} Hz): the monitor "
                            f"would never follow it"))
    rep = check_limits(u_pred, new, dt, channel, limits)
    if not rep.ok:
        out.append(("fail", "the drive this target needs fails the chain's "
                            "limits: " + "; ".join(rep.messages[:-1])))
    else:
        out.append(("ok", rep.messages[-1] + " (estimated, for the corrected target)"))
    upk = float(np.max(np.abs(u_pred)))
    if upk > full_scale:
        out.append(("fail", f"estimated drive peak {upk:.3f} V is past the "
                            f"+/-{full_scale:g} V upload mapping"))
    elif upk > 0.97 * full_scale:
        out.append(("warn", f"estimated drive peak {upk:.3f} V is within 3% "
                            f"of the {full_scale:g} V full scale"))
    span_old = float(np.ptp(base_mon))
    grow = float(max(new.max() - base_mon.max(), base_mon.min() - new.min(), 0.0))
    if span_old > 0 and grow > 0.02 * span_old:
        out.append(("warn", f"the target now reaches {grow*scale:.0f} V past "
                            f"the old one: run Auto-set before the next bench "
                            f"run, or the monitor may clip"))
    return out
