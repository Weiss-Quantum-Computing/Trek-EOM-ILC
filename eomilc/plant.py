"""Plant identification and forward/inverse models for one AWG -> monitor chain.

The Trek 610E + EOM is a lightly damped SECOND ORDER system, not a single pole:
zeta ~ 0.21, fn 2.2-3.0 kHz, measured 2026-08-21 (see analysis/results2.json).
A one-pole fit of the same data returns tau ~ 2*zeta/wn, which is the
resonance's group delay -- it gets the lag right and misses the ring entirely.

That distinction is not cosmetic.  ILC contracts only where
|1 - gamma * Pmodel^-1 * Ptrue| < 1, and at fn the true plant has a Q = 2.25 peak
the one-pole lead knows nothing about.  With gamma = 0.6 that factor is 1.51 at
2326 Hz: the loop bottoms out around iteration 2 and then climbs, which on the
bench reads exactly like plant drift.  Set `fn`/`zeta` and it converges.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from scipy.signal import lfilter, filtfilt, butter, bilinear
from scipy.optimize import least_squares


def _pole(x: np.ndarray, tau: float, dt: float) -> np.ndarray:
    """Discrete one-pole low pass with time constant tau, matched-exponential."""
    a = np.exp(-dt / tau)
    return lfilter([1 - a], [1.0, -a], x)


def _resonant(x: np.ndarray, fn: float, zeta: float, dt: float) -> np.ndarray:
    """Discrete second-order section, unity DC gain, via the bilinear transform."""
    wn = 2 * np.pi * fn
    b, a = bilinear([wn ** 2], [1.0, 2 * zeta * wn, wn ** 2], fs=1.0 / dt)
    return lfilter(b, a, x)


@dataclass
class Plant:
    """y = offset + gain * [ SOS(fn, zeta) o LP(tau) o LP(tau2) ] (u)

    Every dynamic term is optional and disabled by zero.  `gain` maps AWG volts
    to monitor volts.

    Do NOT set both `fn` and `tau` from the calibration tables: they describe the
    SAME lag (tau == 2*zeta/wn to within 1.5% across the whole sweep), so setting
    both applies it twice.  `Channel.plant()` in config.py picks one and is the
    intended way to build these.
    """
    gain: float
    tau: float = 0.0             # dominant real pole, s
    offset: float = 0.0
    tau2: float = 0.0            # optional second real pole, s
    fn: float = 0.0              # resonance, Hz.  0 disables the SOS
    zeta: float = 0.0            # damping ratio
    dt: float = 1e-6

    def forward(self, u: np.ndarray) -> np.ndarray:
        y = np.asarray(u, float)
        if self.fn > 0:
            y = _resonant(y, self.fn, self.zeta, self.dt)
        if self.tau > 0:
            y = _pole(y, self.tau, self.dt)
        if self.tau2 > 0:
            y = _pole(y, self.tau2, self.dt)
        return self.gain * y + self.offset

    def lead(self, x: np.ndarray) -> np.ndarray:
        """Inverse dynamics on a DIFFERENCE signal in monitor volts.

        Same operator as `inverse` but without the offset term, because the
        offset cancels in a difference.  This is what ILC needs: it corrects an
        error, not an absolute level.

        The resonant inverse differentiates TWICE.  Low-pass the error before
        calling this on measured data -- raw 8-bit scope grass through d2/dt2
        will bury the correction.  `Loop.update` does exactly that.
        """
        u = np.asarray(x, float) / self.gain
        if self.tau > 0:
            u = u + self.tau * np.gradient(u, self.dt)
        if self.tau2 > 0:
            u = u + self.tau2 * np.gradient(u, self.dt)
        if self.fn > 0:
            wn = 2 * np.pi * self.fn
            d1 = np.gradient(u, self.dt)
            u = u + (2 * self.zeta / wn) * d1 + np.gradient(d1, self.dt) / wn ** 2
        return u

    def inverse(self, v: np.ndarray) -> np.ndarray:
        """Drive required to make the monitor follow `v` (monitor volts).

        Exact inverse of the model.  Derivatives are centred differences, which
        is what makes the result usable -- a raw forward difference amplifies the
        last LSB of `v` into the drive.
        """
        return self.lead(np.asarray(v, float) - self.offset)

    def response(self, f) -> np.ndarray:
        """Continuous-time H(f) of this model: gain times the dynamic terms.

        This is the transfer function `lead` inverts (the derivative form),
        not the discrete filters `forward` runs -- the two agree well below
        Nyquist, and it is the continuous one a measured FRF is compared and
        fitted against.  Any zeta > 0 is allowed: zeta > 1 is two real poles
        at wn*(zeta -/+ sqrt(zeta^2 - 1)), which is what the Trek chains'
        FRFs turned out to want (X1 at 0.5 V: fn 7.3 kHz, zeta 1.1).
        """
        w = 2 * np.pi * np.asarray(f, float)
        H = np.full(w.shape, self.gain, complex)
        if self.tau > 0:
            H = H / (1 + 1j * w * self.tau)
        if self.tau2 > 0:
            H = H / (1 + 1j * w * self.tau2)
        if self.fn > 0:
            wn = 2 * np.pi * self.fn
            H = H / (1 + 2j * self.zeta * w / wn - (w / wn) ** 2)
        return H

    def __repr__(self):
        s = f"Plant(gain={self.gain:.4f}"
        if self.fn > 0:
            s += (f", fn={self.fn:.0f} Hz, zeta={self.zeta:.3f}"
                  f", Q={1/(2*self.zeta):.2f}, delay={2*self.zeta/(2*np.pi*self.fn)*1e6:.1f} us")
        if self.tau > 0:
            s += f", tau={self.tau*1e6:.2f} us, f3dB={1/(2*np.pi*self.tau):.0f} Hz"
        if self.tau2 > 0:
            s += f", tau2={self.tau2*1e6:.2f} us"
        return s + f", offset={self.offset*1e3:.2f} mV)"


MODELS = ("resonant", "one_pole", "two_pole", "static")


def identify(u: np.ndarray, y: np.ndarray, dt: float,
             model: str = "resonant", mask: np.ndarray | None = None) -> tuple[Plant, dict]:
    """Least-squares fit of a plant from a drive/response pair.

    `u` is the AWG trace and `y` the monitor trace, both already on the same grid
    with the trigger offset removed.  `model` is one of MODELS; "resonant" is the
    right one for this chain and the default.
    """
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}, not {model!r}")
    u = np.asarray(u, float); y = np.asarray(y, float)
    m = np.ones_like(u, bool) if mask is None else np.asarray(mask, bool)

    # seed from settled levels
    n0 = max(int(0.02 * len(u)), 20)
    g0 = ((np.percentile(y, 99) - y[:n0].mean())
          / max(np.percentile(u, 99) - u[:n0].mean(), 1e-12))

    def build(p):
        if model == "resonant":
            g, fn, zeta, off = p
            return Plant(gain=g, offset=off, fn=max(fn, 1.0),
                         zeta=float(np.clip(zeta, 1e-3, 10.0)), dt=dt)
        if model == "two_pole":
            g, tau, tau2, off = p
            return Plant(gain=g, tau=max(tau, dt), offset=off, tau2=max(tau2, 0.0), dt=dt)
        if model == "static":
            g, off = p
            return Plant(gain=g, offset=off, dt=dt)
        g, tau, off = p
        return Plant(gain=g, tau=max(tau, dt), offset=off, dt=dt)

    def resid(p):
        return (build(p).forward(u) - y)[m]

    if model == "resonant":
        p0, xs = [g0, 2500.0, 0.22, 0.0], [0.1, 500.0, 0.05, 1e-3]
    elif model == "two_pole":
        p0, xs = [g0, 25e-6, 5e-6, 0.0], [0.1, 5e-6, 5e-6, 1e-3]
    elif model == "static":
        p0, xs = [g0, 0.0], [0.1, 1e-3]
    else:
        p0, xs = [g0, 25e-6, 0.0], [0.1, 5e-6, 1e-3]

    sol = least_squares(resid, p0, x_scale=xs)
    plant = build(sol.x)

    r = sol.fun
    span = np.ptp(y[m])
    info = dict(model=model, resid_rms=float(r.std()), resid_peak=float(np.abs(r).max()),
                resid_rms_pct=100 * float(r.std()) / span,
                resid_peak_pct=100 * float(np.abs(r).max()) / span, span=float(span))
    return plant, info


def _phase_crossing(f: np.ndarray, H: np.ndarray, deg: float, fallback: float) -> float:
    """Lowest frequency where the unwrapped phase of H passes `deg` (< 0)."""
    ph = np.degrees(np.unwrap(np.angle(H)))
    below = np.flatnonzero(ph <= deg)
    if below.size == 0 or below[0] == 0:
        return fallback
    i = below[0]
    return float(np.interp(deg, [ph[i], ph[i - 1]], [f[i], f[i - 1]]))


def fit_frf(f, H, model: str = "one_pole", f_hi: float | None = None,
            f_lo: float | None = None, gain: float | None = None,
            n_dc: int = 3, dt: float = 1e-6) -> tuple[Plant, dict]:
    """Least-squares fit of a parametric Plant to a measured response H(f).

    `f` in Hz and `H` complex (monitor per drive), as `ilc._read_frf` hands
    them over -- coherent tones only.  The residual is log(H / H_model):
    real part the log-magnitude error, imaginary part the phase error in
    radians, weighted equally, over the tones between `f_lo` (default the
    lowest tone) and `f_hi` (default the highest).  Every tone counts the
    same, which is the point: a time-domain fit to a ramp record weights
    the band the ramp lives in and returns lag terms 2-3x short (27 us
    against 70 us on X1 -- see `identify`).

    The gain is NOT free by default.  Over a band where the form is
    imperfect a free gain absorbs model mismatch (0.70 against a measured DC
    gain of 0.56 on X1); so it is the median |H| of the `n_dc` lowest tones
    unless `gain` is given.  Only the dynamic terms are fitted, in log space
    so they stay positive; the resonant form's zeta is unbounded above
    (zeta > 1 = two real poles).

    Returns (Plant, info) like `identify`: info carries the band, the gain
    source, and the rms magnitude (%) and phase (deg) residuals in band.
    """
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}, not {model!r}")
    f = np.asarray(f, float)
    H = np.asarray(H, complex)
    order = np.argsort(f)
    f, H = f[order], H[order]
    if f.size < 3:
        raise ValueError("an FRF fit needs at least three coherent tones")
    lo = float(f[0]) if f_lo is None else float(f_lo)
    hi = float(f[-1]) if f_hi is None else float(f_hi)
    m = (f >= lo) & (f <= hi)
    if m.sum() < 3:
        raise ValueError(f"only {int(m.sum())} tone(s) between {lo:g} and "
                         f"{hi:g} Hz -- widen the fit band")
    if gain is None:
        k = max(1, min(int(n_dc), f.size))
        gain = float(np.median(np.abs(H[:k])))
        gain_src = f"median |H| of the {k} lowest tones ({f[0]:.0f}-{f[k-1]:.0f} Hz)"
    else:
        gain = float(gain)
        gain_src = "fixed by the caller"
    fm, Hm = f[m], H[m]
    w = 2 * np.pi * fm

    def build(p):
        if model == "static":
            return Plant(gain=gain, dt=dt)
        if model == "one_pole":
            return Plant(gain=gain, tau=float(np.exp(p[0])), dt=dt)
        if model == "two_pole":
            t1, t2 = sorted(np.exp(p), reverse=True)
            return Plant(gain=gain, tau=float(t1), tau2=float(t2), dt=dt)
        return Plant(gain=gain, fn=float(np.exp(p[0])),
                     zeta=float(np.exp(p[1])), dt=dt)

    def resid(p):
        r = np.log(Hm / build(p).response(fm))
        return np.concatenate([r.real, r.imag])

    if model == "static":
        p0 = []
    elif model == "one_pole":
        p0 = [np.log(1 / (2 * np.pi * _phase_crossing(f, H, -45.0, hi / 3)))]
    elif model == "two_pole":
        t0 = 1 / (2 * np.pi * _phase_crossing(f, H, -45.0, hi / 3))
        p0 = [np.log(t0), np.log(t0 / 5)]
    else:
        p0 = [np.log(_phase_crossing(f, H, -90.0, hi / 2)), np.log(1.0)]
    if p0:
        sol = least_squares(resid, p0, x_scale=[0.5] * len(p0))
        plant, r = build(sol.x), sol.fun
    else:
        plant = build([])
        r = resid([])
    n = fm.size
    info = dict(model=model, f_lo=lo, f_hi=hi, n_tones=int(n),
                gain=gain, gain_source=gain_src,
                resid_mag_pct=100 * float(np.sqrt(np.mean(r[:n] ** 2))),
                resid_phase_deg=float(np.degrees(np.sqrt(np.mean(r[n:] ** 2)))))
    return plant, info


def contraction(f, H, plant: Plant, gamma: float) -> tuple[np.ndarray, float | None]:
    """|1 - gamma * H / H_model| per tone, and the lowest tone where it
    reaches 1.

    That factor is what one ILC iteration multiplies drive-coupled error by
    at each frequency when the update divides by `plant` and the chain is
    really H: below 1 the loop contracts there, at or above 1 it does not
    (the Q filter at f_cut then has to shield the band).  Returns
    (lambda per tone, f_boundary or None when no tone reaches 1).
    """
    f = np.asarray(f, float)
    lam = np.abs(1 - gamma * np.asarray(H, complex) / plant.response(f))
    hit = np.flatnonzero(lam >= 1.0)
    return lam, (float(f[hit[0]]) if hit.size else None)


def smooth(x: np.ndarray, dt: float, f_cut: float) -> np.ndarray:
    """Zero-phase low pass. Used for the ILC Q filter and for de-noising traces."""
    fn = 0.5 / dt
    wn = min(f_cut / fn, 0.99)
    b, a = butter(4, wn)
    pad = min(3 * max(len(a), len(b)), len(x) - 1)
    return filtfilt(b, a, x, padlen=pad)
