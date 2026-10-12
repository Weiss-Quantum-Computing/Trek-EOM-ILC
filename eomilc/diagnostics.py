"""Per-measurement diagnostics: what part of an error is repeatable, where
the quantizers show, and how each band moves from iteration to iteration.

Everything here is computed from the shot stack behind a measurement (the
64 HRES singles the bench averages) plus two numbers the loop knows at the
time and nothing recorded before 12 Oct 2026: the monitor channel's V/div
and offset. The results travel in a sidecar beside the averaged
measurement, `meas_<stem>_iNN[_rMM]_stats.npz`, so the plots can draw them
for every stored iteration after the fact.

Two quantizers and what each looks like:
  * the MSO-X's 8-bit converter: a fixed error pattern per display code
    (40.25 mV at 1 V/div; 3.4 mV pk-pk measured on 2 Sep 2026). A ramp
    sweeps it into 15-100 kHz "error" that averaging keeps and the loop
    learns into the drive. `code_fold` folds the residual on the monitor
    voltage modulo one code: a pattern stands out as a profile many sigma
    above its bin scatter; the dither flattens it to ~1 sigma.
  * the 4063B's 16-bit DAC: 20 Vpp / 65536 = 0.31 mV per step (0.17 V at
    the EOM). An update smaller than a step is rounded away, so once most of
    an update is under the step the loop can only limit-cycle.
    `update_vs_step` says how much of the update is below it.

The noise floor is the standard error of the mean per spectral bin,
sqrt(variance across shots / n), the same estimate ilc.learnable_band uses
for its log line. Error above a few times it is learnable; error inside it
is scatter the loop would imprint.

No scipy, no Tk: the test suite runs this on the bare interpreter.
"""
from __future__ import annotations

import os

import numpy as np

FORMAT = "EOM-ILC measurement stats v1"
ADC_CODE_PER_VDIV = 0.04025       # V per display code per V/div (ilc_gui)
AWG_LSB = 20.0 / 65536            # V per DAC step at AMP 20 Vpp
CAMPAIGN_BANDS = ((0.0, 10e3), (10e3, 20e3), (20e3, 30e3), (30e3, 60e3))


def _hann_spectra(target, stack, dt):
    """(f, |E| of the mean's error, per-bin standard error of the mean),
    Hann-windowed, as ilc.learnable_band computes them."""
    stack = np.asarray(stack, float)
    n_sh, n = stack.shape
    y = stack.mean(axis=0)
    w = np.hanning(n)
    f = np.fft.rfftfreq(n, dt)
    E = np.abs(np.fft.rfft((np.asarray(target, float) - y) * w))
    D = np.fft.rfft((stack - y) * w, axis=1)
    sem = np.sqrt(np.mean(np.abs(D) ** 2, axis=0) / max(n_sh - 1, 1))
    return f, E, sem


def band_rms(f, A, bands=CAMPAIGN_BANDS):
    """rms of an amplitude array over each band (Parseval on the bins)."""
    out = []
    for lo, hi in bands:
        m = (f >= lo) & (f < hi)
        out.append(float(np.sqrt(np.sum(A[m] ** 2))) if m.any() else float("nan"))
    return out


def measurement_stats(target, stack, dt, vdiv=None, offset=None, lsb=AWG_LSB,
                      bands=CAMPAIGN_BANDS):
    """Everything the diagnostics draw for one measurement, from its shots.

    Returns a dict of plain arrays and floats (npz-safe):
      n_shots, dt, sem_t (per-sample standard error of the mean, monitor V),
      f, err_spec, sem_spec (Hann spectra of the mean's error and of its
      standard error), bands, bands_rms, bands_floor (per campaign band),
      floor_rms (time-domain rms of sem_t), vdiv, offset, code_V (one ADC
      display code in volts, when vdiv is known), lsb, and the fold of the
      residual on the monitor voltage: fold_centres, fold_mean, fold_sem,
      fold_n, fold_amp, fold_sig (pk-pk of the profile and its sigma).
    """
    stack = np.asarray(stack, float)
    if stack.ndim != 2 or stack.shape[0] < 2:
        raise ValueError("stats need a (n_shots, n) stack with at least two shots")
    target = np.asarray(target, float)
    n_sh, n = stack.shape
    if target.shape != (n,):
        raise ValueError("target and stack disagree in length")
    y = stack.mean(axis=0)
    sem_t = stack.std(axis=0, ddof=1) / np.sqrt(n_sh)
    f, E, sem = _hann_spectra(target, stack, dt)
    out = dict(format=FORMAT, n_shots=int(n_sh), dt=float(dt), sem_t=sem_t,
               f=f, err_spec=E, sem_spec=sem,
               bands=np.asarray(bands, float), bands_rms=np.asarray(band_rms(f, E, bands)),
               bands_floor=np.asarray(band_rms(f, sem, bands)),
               floor_rms=float(np.sqrt(np.mean(sem_t ** 2))),
               vdiv=float(vdiv) if vdiv is not None else float("nan"),
               offset=float(offset) if offset is not None else float("nan"),
               lsb=float(lsb))
    code = ADC_CODE_PER_VDIV * float(vdiv) if vdiv is not None and vdiv > 0 else None
    out["code_V"] = float(code) if code else float("nan")
    if code:
        # Folded on the TARGET voltage, not the measured mean: binning on
        # the noisy y selects the noise (a flat level sitting on a code
        # boundary puts its positive-noise samples in the first bin with a
        # negative residual and the rest in the last with a positive one:
        # a 10-sigma "pattern" out of pure scatter). The target is where the
        # converter's input sits to within the residual, a few percent of a
        # code.
        # ... and with the slow part of the residual taken out first: the
        # converter's pattern is tens of kHz (slope / code), while an
        # unconverged iteration's error is hundreds of volts of slow
        # structure that would fold into meaningless bars. A 0.2 ms running
        # mean removes what is under ~5 kHz and passes the pattern.
        fold = code_fold(target, target - y, code, sem_t=sem_t,
                         detrend_n=max(int(round(200e-6 / dt)), 1))
        for k, v in fold.items():
            out["fold_" + k] = v
    return out


def code_fold(y, resid, code, nbins=24, sem_t=None, detrend_n=None):
    """Fold the residual on a voltage modulo one ADC code. `y` should be
    the noise-free voltage the converter saw (the target), see
    measurement_stats for why not the measured mean. `detrend_n` > 1
    subtracts a running mean of that many samples from the residual first
    (edges padded), so slow error does not fold.

    Returns centres (fraction of a code, 0-1), mean, sem (of each bin's
    mean, from the scatter of the samples in it, or from sem_t when given
    and the samples are fewer than 3), n per bin, amp (pk-pk of the binned
    profile) and sig (one sigma of amp: the two extreme bins' sems added in
    quadrature). A pattern is there when amp is several times sig; the
    dithered scope reads amp ~ sig.
    """
    y = np.asarray(y, float)
    resid = np.asarray(resid, float)
    if detrend_n and int(detrend_n) > 1:
        k = int(detrend_n)
        pad = np.r_[np.full(k, resid[0]), resid, np.full(k, resid[-1])]
        slow = np.convolve(pad, np.ones(k) / k, mode="same")[k:-k]
        resid = resid - slow
    phase = np.mod(y, code) / code
    idx = np.minimum((phase * nbins).astype(int), nbins - 1)
    mean = np.full(nbins, np.nan)
    sem = np.full(nbins, np.nan)
    cnt = np.zeros(nbins, int)
    for b in range(nbins):
        m = idx == b
        cnt[b] = int(m.sum())
        if cnt[b] == 0:
            continue
        mean[b] = resid[m].mean()
        if cnt[b] >= 3:
            sem[b] = resid[m].std(ddof=1) / np.sqrt(cnt[b])
        elif sem_t is not None:
            sem[b] = float(np.sqrt(np.mean(np.asarray(sem_t, float)[m] ** 2)) / np.sqrt(cnt[b]))
    ok = np.isfinite(mean)
    if ok.sum() >= 2:
        hi, lo = int(np.nanargmax(mean)), int(np.nanargmin(mean))
        amp = float(mean[hi] - mean[lo])
        sig = float(np.sqrt(np.nansum([sem[hi] ** 2, sem[lo] ** 2])))
    else:
        amp, sig = float("nan"), float("nan")
    centres = (np.arange(nbins) + 0.5) / nbins
    return dict(centres=centres, mean=mean, sem=sem, n=cnt, amp=amp, sig=sig,
                code=float(code))


def lattice_hist(resid, step, span_steps=4.0, bins_per_step=10):
    """Histogram of a raw-rate residual around zero in units of the scope
    word (`step`), to show whether the values sit on the lattice. Returns
    edges (in steps), counts, n_distinct (distinct values in the record)
    and the expected count per bin if the values were spread evenly over
    the same span (the level a dithered record approaches)."""
    r = np.asarray(resid, float) / float(step)
    lim = float(span_steps)
    edges = np.linspace(-lim, lim, int(2 * lim * bins_per_step) + 1)
    counts, _ = np.histogram(r, bins=edges)
    inside = r[(r >= -lim) & (r < lim)]
    flat = len(inside) / max(len(edges) - 1, 1)
    return dict(edges=edges, counts=counts, n_distinct=int(np.unique(np.round(r, 9)).size),
                flat=float(flat), n=int(len(r)))


def growth_factor(series, k=5):
    """Per-iteration growth of a positive series over its last `k` points:
    (factor, one sigma) from a log-linear least-squares fit; (nan, nan)
    with fewer than 3 finite points."""
    v = np.asarray(series, float)
    v = v[-k:]
    ok = np.isfinite(v) & (v > 0)
    if ok.sum() < 3:
        return float("nan"), float("nan")
    x = np.arange(len(v))[ok]
    ly = np.log(v[ok])
    A = np.column_stack([x, np.ones_like(x)])
    coef, res, *_ = np.linalg.lstsq(A, ly, rcond=None)
    slope = coef[0]
    dof = ok.sum() - 2
    if dof > 0:
        s2 = float(np.sum((A @ coef - ly) ** 2)) / dof
        var = s2 * np.linalg.inv(A.T @ A)[0, 0]
        sig = float(np.sqrt(var)) * float(np.exp(slope))
    else:
        sig = float("nan")
    return float(np.exp(slope)), sig


def update_vs_step(du, lsb=AWG_LSB, bins_per_step=4, span_steps=8.0):
    """How an update compares with the generator's DAC step: the fraction
    of samples whose |du| is under one step (rounded away), the rms in
    steps, and a histogram of |du| in steps."""
    d = np.abs(np.asarray(du, float)) / float(lsb)
    edges = np.linspace(0, span_steps, int(span_steps * bins_per_step) + 1)
    counts, _ = np.histogram(d, bins=edges)
    return dict(edges=edges, counts=counts, below=float(np.mean(d < 1.0)),
                rms_steps=float(np.sqrt(np.mean(d ** 2))), n=int(d.size),
                over=int(np.sum(d >= span_steps)))


def stats_path(meas_path):
    """meas_<stem>_iNN[_rMM].npy -> its sidecar."""
    base, _ = os.path.splitext(meas_path)
    return base + "_stats.npz"


def save_stats(path, stats):
    np.savez(path, **{k: (v if isinstance(v, np.ndarray) else np.asarray(v))
                      for k, v in stats.items()})
    return path


def load_stats(path):
    """The sidecar as a dict (arrays as arrays, scalars as floats/ints),
    or None when there is none."""
    if not os.path.exists(path):
        return None
    with np.load(path, allow_pickle=False) as z:
        out = {}
        for k in z.files:
            v = z[k]
            if v.ndim == 0:
                v = v.item()
            out[k] = v
    if out.get("format") != FORMAT:
        return None
    return out
