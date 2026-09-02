"""Reader for the Agilent MSO-X 2014A CSV + TXT pairs produced by the capture
script, plus the auto-PSD the wideband noise work needs.

pandas is imported inside `load` rather than at the top, and that is
load-bearing rather than tidiness. `eomilc/__init__.py` imports this module, so
a top-level `import pandas` made `import eomilc` fail outright on the system
interpreter, which has numpy but no pandas -- taking `polarimetry` and `rin`
down with it even though neither has ever needed pandas. Reading a scope CSV
still needs it; nothing else here does.
"""
from __future__ import annotations
import os, re
from dataclasses import dataclass
import numpy as np


@dataclass
class Trace:
    t: np.ndarray                 # seconds, trigger at t = 0
    data: dict                    # {channel label: np.ndarray of volts}
    header: dict                  # raw key/value pairs from the .txt sidecar

    @property
    def dt(self) -> float:
        return float(np.median(np.diff(self.t)))

    def __getitem__(self, key):
        if key in self.data:
            return self.data[key]
        for k in self.data:                       # allow "CH3" or a substring
            if k.upper().startswith(key.upper()) or key.upper() in k.upper():
                return self.data[k]
        raise KeyError(f"{key!r} not in {list(self.data)}")

    def lsb(self, key) -> float:
        """Scope quantisation step, inferred from the data itself."""
        u = np.unique(self[key])
        d = np.diff(u)
        d = d[d > 0]
        return float(np.median(d)) if d.size else 0.0

    def volts_per_div(self, ch: str) -> float | None:
        v = self.header.get(f"{ch} V/div")
        return float(v) if v is not None else None


def _read_header(txt_path: str) -> dict:
    h = {}
    if not os.path.exists(txt_path):
        return h
    with open(txt_path, "r", errors="replace") as f:
        for line in f:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            h[k.strip()] = v.strip()
    return h


def load(csv_path: str) -> Trace:
    """Load a capture. The .txt sidecar is picked up automatically if present."""
    import pandas as pd                  # see the module docstring
    df = pd.read_csv(csv_path)
    t = df.iloc[:, 0].to_numpy(dtype=float)
    data = {c: df[c].to_numpy(dtype=float) for c in df.columns[1:]}
    return Trace(t=t, data=data, header=_read_header(os.path.splitext(csv_path)[0] + ".txt"))


def resample(t_src: np.ndarray, y: np.ndarray, t_dst: np.ndarray,
             t_offset: float = 0.0, antialias: bool = True) -> np.ndarray:
    """Put a scope trace onto the waveform grid.

    t_offset shifts the SOURCE time base: use it to remove the fixed
    trigger-to-waveform-start delay.  Keep it constant between ILC iterations,
    otherwise the loop chases its own alignment.
    """
    dt_src = float(np.median(np.diff(t_src)))
    dt_dst = float(np.median(np.diff(t_dst)))
    if antialias and dt_dst > 2 * dt_src:
        n = int(round(dt_dst / dt_src))
        if n > 1:                                  # boxcar to the target rate
            k = np.ones(n) / n
            y = np.convolve(y, k, mode="same")
    return np.interp(t_dst, t_src - t_offset, y)


def measure_t_offset(t: np.ndarray, y: np.ndarray, u_ref: np.ndarray,
                     dt_ref: float) -> float:
    """Delay from the scope trigger to the START of the played waveform.

    This is the number `--t-offset` wants.  It cross-correlates the captured
    trace against the drive record that produced it, so it uses the whole
    waveform rather than one threshold crossing -- which matters here because
    MKJ leaves zero with almost no slope, so any threshold sits hundreds of
    microseconds late and moves with the noise.

    Feed it the AWG channel (CH1/CH2) rather than a monitor: the drive is the
    reference exactly, while the monitor has the plant's own lag in it.

    Measure ONCE and hard-code the result.  Re-fitting per iteration makes the
    loop chase its own alignment instead of converging.
    """
    y = np.asarray(y, float)
    dt = float(np.median(np.diff(t)))
    n = int(round(len(u_ref) * dt_ref / dt))
    ref = np.interp(np.linspace(0, len(u_ref) - 1, n),
                    np.arange(len(u_ref)), np.asarray(u_ref, float))
    c = np.correlate(y - y.mean(), ref - ref.mean(), mode="valid")
    return float(t[int(np.argmax(c))])


def find_trigger_offset(t: np.ndarray, y: np.ndarray, frac: float = 0.5) -> float:
    """Time at which y first crosses `frac` of its settled span.

    NOT the same thing as `--t-offset`, despite what this docstring used to
    imply: it returns a mid-ramp crossing, not the waveform start, and on a
    waveform that leaves zero slowly the two are milliseconds apart.  Use
    `measure_t_offset` for alignment; this is only a rough landmark finder.
    """
    base = y[t < t[0] + 0.2 * (0 - t[0])].mean() if t[0] < 0 else y[:100].mean()
    span = np.percentile(y, 99.5) - base
    i = int(np.argmax(y - base >= frac * span))
    return float(t[i])


# ------------------------------------------------------------------ readout
#
# The MSO-X accepts only these counts (:WAVeform:POINts); asked for anything
# else it rounds to a value it likes -- possibly DOWN, so pick the next one up
# ourselves. Asking is not optional: `Scope.waveform` writes
# :WAVeform:POINts:MODE on every call and the scope RESETS the point count when
# that mode is set, so a call that names no count gets whatever the scope falls
# back to. That is how a 3 us readout of a 15 ms window killed the first
# 200 kHz FRF run (26 Aug), and how the alignment check ended up the one
# capture in the loop whose resolution nobody chose (2 Sep).
#
# It lives here rather than in the panel because ilc_bench needs the same rule:
# one table, so a capture and the alignment check that precedes it are read at
# the same depth.
SCOPE_PTS = (2000, 5000, 10000, 20000, 50000, 62500)


def scope_points_for(need):
    """Smallest readout count the scope offers that is at least `need`."""
    return next((p for p in SCOPE_PTS if p >= need), SCOPE_PTS[-1])


# ------------------------------------------------------------------ spectra
#
# The SR760 stops at 100 kHz, so the intensity servo's bump at 150-300 kHz is
# invisible to it and has to come from the scope. For that comparison to mean
# anything the two instruments have to be in the same units, which is what the
# normalisation below is for: `psd` returns V^2/Hz and `Spectrum.asd` its square
# root in V/rtHz, the SR760's own units. Agreement in the 30-95 kHz overlap,
# where both instruments can see, is itself one of the validation tests.

# Coefficients for the windows numpy does not ship. The same set the SR760
# offers, so a scope PSD and an analyser trace can be taken through the same
# window instead of being compared across two different ones.
_COSINE_WINDOWS = {
    # Blackman-Harris, 4-term, -92 dB sidelobes. The SR760 calls it BMH.
    "bmh": (0.35875, 0.48829, 0.14128, 0.01168),
    # Flat top, 5-term. Amplitude-accurate on a tone to ~0.01 dB, which is what
    # it is for; an ENBW of 3.77 bins makes it a poor noise window.
    "flattop": (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368),
}

# numpy 2 renamed trapz to trapezoid and deprecated the old spelling; the bench
# runs both vintages, so bind whichever is there once.
_trapz = getattr(np, "trapezoid", None) or np.trapz


def window(name, n):
    """One of the SR760's windows, as an array of `n` points.

    Periodic rather than symmetric (scipy's `sym=False`): a spectrum treats the
    record as one period of a repeating signal, and the symmetric form repeats
    the endpoint, which puts a small step in every period and lifts the
    sidelobes the window was chosen for.
    """
    name = (name or "hann").lower()
    n = int(n)
    if n < 1:
        raise ValueError("a window needs at least one point")
    if name in ("uniform", "boxcar", "rect", "none"):
        return np.ones(n)
    k = np.arange(n)
    if name in _COSINE_WINDOWS:
        w = np.zeros(n)
        for i, ai in enumerate(_COSINE_WINDOWS[name]):
            w += (-1) ** i * ai * np.cos(2.0 * np.pi * i * k / n)
        return w
    if name in ("hann", "hanning"):
        return 0.5 - 0.5 * np.cos(2.0 * np.pi * k / n)
    if name == "hamming":
        return 0.54 - 0.46 * np.cos(2.0 * np.pi * k / n)
    if name == "blackman":
        return (0.42 - 0.5 * np.cos(2.0 * np.pi * k / n)
                + 0.08 * np.cos(4.0 * np.pi * k / n))
    raise ValueError(f"unknown window {name!r}; have uniform, hann, hamming, "
                     f"blackman, bmh, flattop")


def enbw_bins(w):
    """Equivalent noise bandwidth of a window, in bins.

    n sum(w^2) / (sum w)^2: 1.0 uniform, 1.5 Hann, 3.77 flat top. This is the
    factor by which a window widens every bin, and dividing by sum(w^2) in
    `psd` is exactly what takes it back out - which is why a density computed
    that way does not depend on the window, while a peak-amplitude reading very
    much does.
    """
    w = np.asarray(w, float)
    s = w.sum()
    return float(len(w) * np.sum(w ** 2) / (s * s)) if s else float("nan")


@dataclass
class Spectrum:
    """A one-sided auto-PSD and what it is worth.

    `n_avg` counts the spectra that went into the average; `n_indep` counts the
    ones that did not share samples. Quote the error bar off `n_indep` - with
    overlap the two differ, and `n_avg` is the flattering one.
    """
    f: np.ndarray                 # Hz
    psd: np.ndarray               # V^2/Hz, one-sided
    dt: float
    n_avg: int
    n_indep: float
    window: str
    enbw_bins: float
    nperseg: int

    @property
    def asd(self) -> np.ndarray:
        """V/rtHz - the SR760's own PSD units, for comparing the two."""
        return np.sqrt(self.psd)

    @property
    def df(self) -> float:
        """Bin spacing, Hz."""
        return float(self.f[1] - self.f[0]) if len(self.f) > 1 else float("nan")

    @property
    def rel_err(self) -> float:
        """1-sigma fractional error on a bin, 1/sqrt(n_indep)."""
        return 1.0 / np.sqrt(self.n_indep) if self.n_indep > 0 else float("nan")

    def band_power(self, lo, hi) -> float:
        """Integrated V^2 in a band, by the trapezium rule on the density."""
        sel = (self.f >= lo) & (self.f <= hi)
        if sel.sum() < 2:
            return float("nan")
        return float(_trapz(self.psd[sel], self.f[sel]))

    def band_rms(self, lo, hi) -> float:
        """rms volts in a band."""
        return float(np.sqrt(self.band_power(lo, hi)))


def psd(x, dt, window_name="hann", detrend=True, nperseg=None, noverlap=0):
    """One-sided auto-PSD in V^2/Hz, RMS-averaged over a shot stack.

    `x` is one record, or the (n_shots, n_samples) stack `ilc_bench.capture_all`
    already hands back. A stack is averaged in POWER across shots, which is the
    RMS averaging the SR760 does and the estimator whose variance falls as 1/N.
    Averaging the complex spectra instead would be vector averaging, which
    suppresses everything not phase-locked to the trigger - which is exactly the
    noise this exists to measure.

    Normalisation is

        S(f) = 2 |FFT(w x)|^2 / (fs sum(w^2))

    with DC and, on an even-length record, Nyquist not doubled. Dividing by
    sum(w^2) rather than (sum w)^2 is the ENBW correction: it makes sum(S) df
    equal mean(x^2), so the result is a density that does not depend on which
    window it was taken through. `polarimetry.band_rms` computes the same total
    a different way and agrees, which is worth keeping true.

    `nperseg` splits each record before averaging, trading resolution for
    variance - 5501 points at 2 MS/s is a single 363 Hz bin, and the servo bump
    is worth more averaging than that. `noverlap` overlaps the segments, which
    buys variance more cheaply but not for free: overlapped segments share
    samples and are not independent, so `n_indep` counts the record length in
    units of the segment rather than counting segments. That is the same trap
    the SR760's OVLP sets, and the reason `rel_err` is quoted off `n_indep`.
    """
    x = np.atleast_2d(np.asarray(x, float))
    if x.ndim != 2 or x.shape[1] < 2:
        raise ValueError("x must be one record or (n_shots, n_samples)")
    dt = float(dt)
    if not dt > 0:
        raise ValueError("dt must be positive")
    n = x.shape[1]
    seg = int(nperseg) if nperseg else n
    if not 2 <= seg <= n:
        raise ValueError(f"nperseg must be 2..{n}")
    step = seg - int(noverlap)
    if step < 1:
        raise ValueError("noverlap must be smaller than nperseg")

    w = window(window_name, seg)
    norm = dt / np.sum(w ** 2)              # = 1 / (fs sum(w^2))
    starts = list(range(0, n - seg + 1, step))
    acc = np.zeros(seg // 2 + 1)
    count = 0
    for row in x:
        for s in starts:
            piece = row[s:s + seg]
            if detrend:
                piece = piece - piece.mean()
            acc += np.abs(np.fft.rfft(piece * w)) ** 2
            count += 1
    p = acc / count * norm
    p[1:] *= 2.0
    if seg % 2 == 0 and len(p) > 1:
        p[-1] /= 2.0                        # Nyquist is not a conjugate pair

    # Independent count: the span each row actually covers, in segments.
    span = (len(starts) - 1) * step + seg if starts else 0
    n_indep = x.shape[0] * span / float(seg)
    return Spectrum(f=np.fft.rfftfreq(seg, dt), psd=p, dt=dt, n_avg=count,
                    n_indep=float(n_indep), window=(window_name or "hann").lower(),
                    enbw_bins=enbw_bins(w), nperseg=seg)
