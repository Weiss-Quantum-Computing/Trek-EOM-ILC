"""Line (mains) ripple: fitted, so the loop can be told not to learn it.

MEASURED 5 Oct 2026 (polarimeter scan 16-ms-spin-echo-test-4, the
experiment's own line-synchronous trigger, fitted on the undriven stretches):
the Trek monitors carry 0.88 mV (X1) and 1.35 mV (X2) at 60 Hz, ~0.3-0.4 mV
at 120 and 180 Hz -- about 1 V at each EOM -- and the light 23 mdeg at 60 Hz.
Its phase at the trigger was the same in all 19 analyzer steps (+-15 deg over
25 minutes), so it does not average away: on a line-synchronous trigger the
ripple is part of the "repeatable error" every measurement reports, and an
ILC learns a drive that cancels it AT THAT PHASE. The experiment can move the
ramps relative to its line resync, and the learned anti-ripple then adds to
the ripple instead of cancelling it. The bench used to trigger off the line,
so the ripple took a fresh phase every shot and averaged to 1/sqrt(n).

The fix here: measure the ripple with the drive off (`fit_line` on a window
of several line periods, same trigger), and subtract the fitted harmonics
from every measurement before the update (`Loop.line`). The loop is then
blind to the ripple and learns only what the drive does. A record shorter
than a line period cannot separate ripple from slow error on its own -- 60 Hz
over the 11 ms ramp record is barely more than a half cycle and looks like a
gain or an offset -- which is why it is measured separately, not projected
out of each record.
"""
from __future__ import annotations

import numpy as np


def _design(t, f_line, harmonics, segments):
    cols, names = [], []
    t = np.asarray(t, float)
    for i, seg in enumerate(segments):
        seg = np.asarray(seg, bool)
        tm = t[seg].mean() if seg.any() else 0.0
        cols += [seg.astype(float), seg * (t - tm)]
        names += [f"offset{i}", f"slope{i}"]
    for h in range(1, harmonics + 1):
        w = 2 * np.pi * f_line * h
        cols += [np.cos(w * t), np.sin(w * t)]
        names += [f"cos{h}", f"sin{h}"]
    return np.column_stack(cols), names


def fit_line(t, y, f_line=60.0, harmonics=3, segments=None):
    """Least-squares line ripple in y(t): sum over h of
    a_h cos(2 pi h f t) + b_h sin(2 pi h f t), with an offset and a slope of
    its own in each segment (bool masks; default: every sample, one segment),
    so slow drift and per-stretch levels do not leak into the harmonics.

    t must be the time from the TRIGGER (the record's own time base), so the
    phases mean the same thing on any capture with that trigger.

    Returns dict(f_line, harmonics, a, b, sig, amp, phase_deg, resid_rms, n,
    cond). Fit over at least ~2 periods of f_line for a stable answer; `cond`
    is the design matrix's condition number (large = the window cannot tell
    the ripple from a drift)."""
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    if segments is None:
        segments = [np.isfinite(y)]
    A, _ = _design(t, f_line, harmonics, segments)
    m = np.zeros(len(t), bool)
    for s in segments:
        m |= np.asarray(s, bool)
    m &= np.isfinite(y)
    if m.sum() < A.shape[1] + 4:
        raise ValueError("too few samples for a line fit")
    Am = A[m]
    coef, *_ = np.linalg.lstsq(Am, y[m], rcond=None)
    res = y[m] - Am @ coef
    dof = max(int(m.sum()) - A.shape[1], 1)
    s2 = float(res @ res) / dof
    cov = np.linalg.pinv(Am.T @ Am) * s2
    k0 = 2 * len(segments)
    a = coef[k0::2][:harmonics]
    b = coef[k0 + 1::2][:harmonics]
    sa = np.sqrt(np.diag(cov)[k0::2][:harmonics])
    sb = np.sqrt(np.diag(cov)[k0 + 1::2][:harmonics])
    return dict(f_line=float(f_line), harmonics=int(harmonics),
                a=a.astype(float), b=b.astype(float),
                sig=np.sqrt(0.5 * (sa ** 2 + sb ** 2)),
                amp=np.hypot(a, b), phase_deg=np.degrees(np.arctan2(-b, a)),
                resid_rms=float(np.std(res)), n=int(m.sum()),
                cond=float(np.linalg.cond(Am)))


def line_wave(t, fit):
    """The fitted harmonics on time axis t (no offsets or slopes)."""
    t = np.asarray(t, float)
    out = np.zeros_like(t)
    for h in range(1, int(fit["harmonics"]) + 1):
        w = 2 * np.pi * fit["f_line"] * h
        out += fit["a"][h - 1] * np.cos(w * t) + fit["b"][h - 1] * np.sin(w * t)
    return out


def describe(fit, scale=1.0, unit="V"):
    """'60 Hz 0.88 @+44 deg, 120 Hz ...' with amplitudes x scale."""
    return ", ".join(
        f"{fit['f_line']*h:g} Hz {fit['amp'][h-1]*scale:.3g}"
        f"+-{fit['sig'][h-1]*scale:.1g} {unit} @{fit['phase_deg'][h-1]:+.0f} deg"
        for h in range(1, int(fit["harmonics"]) + 1))


def to_record(fit):
    """A plain dict (JSON-able) for a state file."""
    return {k: (np.asarray(v).tolist() if isinstance(v, np.ndarray) else v)
            for k, v in fit.items()}


def from_record(rec):
    out = dict(rec)
    for k in ("a", "b", "sig", "amp", "phase_deg"):
        if k in out:
            out[k] = np.asarray(out[k], float)
    return out
