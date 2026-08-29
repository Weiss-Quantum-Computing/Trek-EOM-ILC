"""Polarimetric readout of the EOM chain: fringe calibration, inversion, and
the ensemble statistics the optical campaign is built on.

The optical path is Glan-laser prism -> EOM1 -> EOM2 -> QWP -> analyser -> PD.
With the QWP axis along the EO-zero polarisation and the crystal axes at 45
degrees to it, the pair is a true ROTATOR: the light stays linear and turns by
phi = Gamma/2, exactly linear in drive voltage.  The analyser then reads

    I(phi) = A + B cos(2 (phi - theta_a))

with A = (Imax+Imin)/2 and B = (Imax-Imin)/2.  Every angle in this module is
measured from the EO-zero polarisation, so theta_a = 0 is the analyser aligned
with it and phi = 0 is the light unrotated.  That convention matters: it is
anchored to a MEASURED point (the transmission maximum at the EO zero), not to
the crystal axes, because residual static birefringence puts the EO zero
somewhere other than commanded zero volts.

Two inversions live here and they are for different jobs:

* `invert_linear` is the one Phase 5 wants.  It linearises about the operating
  point the monitor predicts and returns the RESIDUAL directly, which sidesteps
  the arccos branch problem entirely and is exact to first order in a residual
  that is ~1e-4 rad.  It masks the windows where the slope vanishes rather than
  dividing by a number close to zero.
* `invert_absolute` is for the calibration sweep, where you genuinely need phi
  over a full fringe and have a monotonic guide to pick the branch with.

Nothing here imports scipy: the GUI runs on Anaconda but the CLIs and the test
suite run on the system interpreter, which has numpy only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict

import numpy as np

# The bands the ILC campaigns report in.  Keeping the optical numbers on the
# same edges is what makes them comparable to the existing ripple tables.
CAMPAIGN_BANDS = ((0.0, 10e3), (10e3, 20e3), (20e3, 30e3), (30e3, 60e3))


# ------------------------------------------------------------------ calibration

@dataclass
class FringeCal:
    """One analyser setting's fringe, in the units the sweep was taken in.

    `v_pi` and every voltage here are EOM volts unless you fed the fit monitor
    volts, in which case they are monitor volts throughout -- the class does not
    convert, it only stays consistent with whatever it was given.  Mixing the
    two is the one way to get a silently wrong answer out of this module.

    `n_eom` is how many crystals the sweep drove: phi = n_eom * pi (V - v_zero)
    / (2 v_pi), so one EOM at v_pi gives 90 degrees and two give 180.
    """
    a: float                  # fringe midpoint
    b: float                  # fringe half-amplitude
    omega: float              # rad of 2(phi-theta_a) per volt
    psi: float                # phase offset, = omega*v_zero + 2*theta_a
    theta_a: float            # analyser angle from the EO zero, RADIANS
    n_eom: int = 2
    i_dark: float = 0.0       # PD reading with the beam blocked
    note: str = ""

    # -- derived ------------------------------------------------------------
    @property
    def v_pi(self) -> float:
        """Half-wave voltage per crystal."""
        return self.n_eom * np.pi / self.omega

    @property
    def dphi_dv(self) -> float:
        """Rotation per volt, rad/V, for the configuration that was swept."""
        return 0.5 * self.omega

    @property
    def v_zero(self) -> float:
        """The EO zero in commanded volts.  Only recoverable once theta_a is
        known independently -- the fit alone sees the two in combination."""
        return (self.psi - 2.0 * self.theta_a) / self.omega

    @property
    def visibility(self) -> float:
        return self.b / self.a if self.a else 0.0

    @property
    def extinction(self) -> float:
        """Imax/Imin.  Dark-subtract before fitting or this reads the PD
        offset rather than the crystals."""
        lo = self.a - self.b
        return float(self.a + self.b) / lo if lo > 0 else np.inf

    @property
    def sigma_phi(self) -> float:
        """Polarisation spread across the beam implied by the visibility.

        For a Gaussian spread the visibility is exp(-2 sigma^2), so a fringe
        that looks excellent is hiding more angle than you would guess: 0.99
        is 4.1 degrees, which is worth ~117 V of drive at two EOMs.  This is a
        STATIC, position-dependent term -- do not add it to the noise budget,
        it dephases atoms across the array rather than fluctuating.
        """
        v = min(max(self.visibility, 1e-12), 1.0)
        return float(np.sqrt(-np.log(v) / 2.0))

    # -- persistence --------------------------------------------------------
    def save(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
        return path

    @classmethod
    def load(cls, path: str) -> "FringeCal":
        with open(path, encoding="utf-8") as fh:
            return cls(**json.load(fh))


def fit_fringe(v, i, n_eom=2, v_pi_guess=5200.0, theta_a=0.0,
               span=0.35, n_scan=601, i_dark=0.0) -> FringeCal:
    """Fit I(V) = A + B cos(omega V - psi) to a bias sweep.

    For a fixed omega the model is linear in (A, B cos psi, B sin psi), so the
    fit is a scan over omega with a least-squares solve inside it and a
    parabolic refinement at the end.  That is more robust here than a general
    nonlinear fit: the residual surface in omega has one deep minimum and a
    forest of aliases, and a gradient method started from a poor guess walks
    into the wrong one.

    `theta_a` (radians, from the mount) is not fitted -- it is degenerate with
    v_zero and only carried through so `v_zero` can be recovered.
    """
    v = np.asarray(v, float).ravel()
    i = np.asarray(i, float).ravel() - i_dark
    if v.size != i.size or v.size < 8:
        raise ValueError("need matching v and i of at least 8 points")
    if np.ptp(v) <= 0:
        raise ValueError("the sweep does not move in voltage")

    def sse(w):
        M = np.column_stack([np.ones_like(v), np.cos(w * v), np.sin(w * v)])
        coef, *_ = np.linalg.lstsq(M, i, rcond=None)
        return float(np.sum((M @ coef - i) ** 2)), coef

    w0 = n_eom * np.pi / float(v_pi_guess)
    grid = np.linspace(w0 * (1 - span), w0 * (1 + span), int(n_scan))
    errs = np.array([sse(w)[0] for w in grid])
    k = int(np.argmin(errs))

    # parabolic refinement on the three points around the minimum; falls back
    # to the grid point at an edge
    w_best = grid[k]
    if 0 < k < len(grid) - 1:
        y0, y1, y2 = errs[k - 1], errs[k], errs[k + 1]
        den = y0 - 2 * y1 + y2
        if den > 0:
            w_best = grid[k] + 0.5 * (y0 - y2) / den * (grid[1] - grid[0])

    _, coef = sse(w_best)
    a, c, s = coef
    b = float(np.hypot(c, s))
    psi = float(np.arctan2(s, c))
    if b > a:                       # a fringe cannot dip below zero intensity
        raise ValueError(f"fit gives B={b:.4g} > A={a:.4g}: the sweep is not a "
                         f"clean fringe (clipping, drift, or a wrong dark level)")
    return FringeCal(a=float(a), b=b, omega=float(w_best), psi=psi,
                     theta_a=float(theta_a), n_eom=int(n_eom),
                     i_dark=float(i_dark))


# ------------------------------------------------------------------ forward model

def phi_of_volts(v, cal: FringeCal):
    """Rotation angle from the EO zero, radians."""
    return cal.dphi_dv * (np.asarray(v, float) - cal.v_zero)


def volts_of_phi(phi, cal: FringeCal):
    """Inverse of `phi_of_volts` -- an angle expressed as equivalent drive."""
    return np.asarray(phi, float) / cal.dphi_dv + cal.v_zero


def intensity(phi, cal: FringeCal):
    """Analyser transmission at rotation `phi`, dark level included."""
    return cal.i_dark + cal.a + cal.b * np.cos(2.0 * (np.asarray(phi, float)
                                                      - cal.theta_a))


def slope(phi, cal: FringeCal):
    """dI/dphi.  Zero at the fringe extrema -- those are the blind windows."""
    return -2.0 * cal.b * np.sin(2.0 * (np.asarray(phi, float) - cal.theta_a))


def sensitivity(phi, cal: FringeCal):
    """|sin 2(phi - theta_a)|, the normalised slope the plan quotes."""
    return np.abs(np.sin(2.0 * (np.asarray(phi, float) - cal.theta_a)))


# ------------------------------------------------------------------ inversion

def invert_linear(i_meas, v_ref, cal: FringeCal, slope_floor=0.30):
    """Residual rotation, linearised about the operating point `v_ref` predicts.

    `v_ref` is the monitor (converted to the cal's voltage units) or the target
    -- whichever you want the residual measured AGAINST.  Returns

        dphi   residual rotation, radians, NaN where the slope is too small
        dv     the same as equivalent drive volts
        ok     boolean mask of samples where |sin 2(phi-theta_a)| >= slope_floor

    Dividing by the instantaneous slope rather than assuming quadrature is the
    whole point: the slope is known exactly at every sample, and near a fringe
    extremum it goes to zero, where the honest answer is "no measurement here"
    rather than a large number.  Two analyser settings 45 degrees apart cover
    each other's masked windows.
    """
    i_meas = np.asarray(i_meas, float)
    phi_ref = phi_of_volts(v_ref, cal)
    d_i = i_meas - intensity(phi_ref, cal)
    s = slope(phi_ref, cal)
    ok = np.abs(s) >= slope_floor * 2.0 * cal.b
    dphi = np.full(i_meas.shape, np.nan)
    np.divide(d_i, s, out=dphi, where=ok)
    return dphi, dphi / cal.dphi_dv, ok


def fill_masked(y, ok):
    """Linearly interpolate across the masked windows so `y` can be FFT'd.

    Interpolating, rather than zero-filling, is deliberate: a zero reads as a
    quiet band instead of an absent measurement and quietly flatters every
    spectrum that crosses a blind window.  Always report `ok.mean()` alongside
    anything computed from a filled array.
    """
    y = np.asarray(y, float)
    ok = np.asarray(ok, bool)
    if not ok.any():
        raise ValueError("every sample is masked -- wrong analyser angle?")
    return np.interp(np.arange(y.size), np.flatnonzero(ok), y[ok])


def invert_stack(stack, v_ref, cal: FringeCal, slope_floor=0.30, fill=True):
    """`invert_linear` applied shot by shot to a repeat stack.

    Returns (dv_stack, ok).  This is the form the ensemble and coherence
    functions want, because both of those need every shot inverted into
    equivalent volts BEFORE they are compared to anything -- see the warning
    in `coherence_ensemble`.
    """
    stack = np.asarray(stack, float)
    if stack.ndim != 2:
        raise ValueError("stack must be (n_shots, n_samples)")
    out, ok = [], None
    for row in stack:
        _, dv, m = invert_linear(row, v_ref, cal, slope_floor=slope_floor)
        ok = m if ok is None else (ok & m)
        out.append(dv)
    out = np.asarray(out)
    if fill:
        out = np.asarray([fill_masked(r, ok) for r in out])
    return out, ok


def invert_absolute(i_meas, cal: FringeCal, phi_guide):
    """Absolute rotation from intensity, branch picked by `phi_guide`.

    arccos is two-valued and periodic, so this needs a guide -- during a
    calibration sweep the commanded voltage is the obvious one.  Use this for
    the fringe sweep; use `invert_linear` for residuals.
    """
    i_meas = np.asarray(i_meas, float) - cal.i_dark
    c = np.clip((i_meas - cal.a) / cal.b, -1.0, 1.0)
    half = 0.5 * np.arccos(c)
    guide = np.asarray(phi_guide, float)
    best = None
    # candidates: theta_a +/- half, shifted by whole multiples of pi
    for sign in (+1.0, -1.0):
        base = cal.theta_a + sign * half
        k = np.round((guide - base) / np.pi)
        cand = base + k * np.pi
        err = np.abs(cand - guide)
        best = cand if best is None else np.where(err < np.abs(best - guide),
                                                  cand, best)
    return best


# ------------------------------------------------------------------ ensembles

@dataclass
class Ensemble:
    """What a stack of repeated captures says, split the way ILC cares about.

    `mean` is the repeatable part -- everything ILC can in principle learn.
    `std` is the shot-to-shot part, which it structurally cannot: no amount of
    iterating removes noise that is different every shot.  `sem` is how well
    the mean itself is known, and a "repeatable" feature smaller than sem is
    not resolved, it is the average of the noise.
    """
    mean: np.ndarray
    std: np.ndarray
    n: int

    @property
    def sem(self) -> np.ndarray:
        return self.std / np.sqrt(self.n)

    def repeatable_fraction(self, target=None) -> float:
        """rms(repeatable error) / rms(total error), 0..1.

        Near 1 means the residual is drive-locked and worth another ILC
        iteration.  Near 0 means it is noise and further iterating buys
        nothing -- that is the number Phase 5 exists to produce.
        """
        err = self.mean if target is None else self.mean - np.asarray(target,
                                                                     float)
        rep = float(np.sqrt(np.mean(err ** 2)))
        rnd = float(np.sqrt(np.mean(self.std ** 2)))
        tot = np.hypot(rep, rnd)
        return rep / tot if tot else 0.0


def ensemble(stack) -> Ensemble:
    """Mean, shot-to-shot std and count from aligned repeat captures."""
    a = np.asarray(stack, float)
    if a.ndim != 2 or a.shape[0] < 1:
        raise ValueError("stack must be (n_shots, n_samples)")
    n = a.shape[0]
    std = a.std(axis=0, ddof=1) if n > 1 else np.zeros(a.shape[1])
    return Ensemble(mean=a.mean(axis=0), std=std, n=n)


# ------------------------------------------------------------------ coherence

def coherence_ensemble(x_stack, y_stack, dt, detrend=True, n_avg=1):
    """Magnitude-squared coherence estimated ACROSS REPEAT SHOTS.

    **Feed this inverted volts, never a raw photodiode trace.**  Across a ramp
    the analyser's slope sweeps through zero twice, so the PD is the monitor
    multiplied by a gain that varies by four orders of magnitude within one
    record.  Multiplication in time is convolution in frequency: it smears
    every component across the whole spectrum and drives gamma^2 to the 1/N
    floor even when the two channels are perfectly related.  Measured on the
    production target, raw monitor against raw PD gives 0.03 while the same
    data through `invert_stack` first gives 0.83.  Use `invert_stack`, then
    compare volts against volts.

    This is also not what the FRF CSVs carry.  Their `coherence` column is a shot-scatter proxy -- 1 - (spread of
    H / |H|)^2 -- which reads ~1 for any repeatable systematic whether or not
    the two channels have anything to do with each other.  What the campaign
    actually asks is "of the light's shot-to-shot fluctuation, how much is
    explained by the monitor's?", and that needs true cross-spectra.

    Averaging over the shot axis rather than over segments within one record
    keeps the full record frequency resolution, which segmenting would throw
    away.  `detrend` removes the ensemble mean first, and it must stay on for
    the question above: with the deterministic ramp left in, both channels see
    the same large signal and gamma^2 is ~1 everywhere, trivially and
    uselessly.

    Returns (f, gamma2).  With N shots the estimator's floor is about 1/N --
    64 shots cannot resolve coherence below ~0.016, so do not read structure
    down there.  `n_avg` band-averages neighbouring bins to trade resolution
    for variance.
    """
    x = np.asarray(x_stack, float)
    y = np.asarray(y_stack, float)
    if x.shape != y.shape or x.ndim != 2:
        raise ValueError("x and y must be the same (n_shots, n_samples)")
    if x.shape[0] < 2:
        raise ValueError("coherence needs at least 2 shots")
    if detrend:
        x = x - x.mean(axis=0)
        y = y - y.mean(axis=0)

    X = np.fft.rfft(x, axis=1)
    Y = np.fft.rfft(y, axis=1)
    sxy = np.mean(np.conj(X) * Y, axis=0)
    sxx = np.mean(np.abs(X) ** 2, axis=0)
    syy = np.mean(np.abs(Y) ** 2, axis=0)

    if n_avg > 1:
        trim = (len(sxy) // n_avg) * n_avg
        rs = lambda a: a[:trim].reshape(-1, n_avg).mean(axis=1)
        sxy, sxx, syy = rs(sxy), rs(sxx), rs(syy)
        f = rs(np.fft.rfftfreq(x.shape[1], dt))
    else:
        f = np.fft.rfftfreq(x.shape[1], dt)

    den = sxx * syy
    g2 = np.zeros_like(sxx)
    np.divide(np.abs(sxy) ** 2, den, out=g2, where=den > 0)
    return f, np.clip(g2, 0.0, 1.0)


def coherence_floor(n_shots: int) -> float:
    """Bias floor of the ensemble estimator: uncorrelated channels still
    return about 1/N.  Anything at or below this is not a measurement."""
    return 1.0 / float(n_shots)


def transfer_ensemble(x_stack, y_stack, dt, detrend=True):
    """H(f) = Sxy/Sxx from the same shot ensemble, alongside its coherence.

    Use it for H_pd/H_mon when the excitation is broadband (the ramp records)
    rather than the tonal probe -- at the probe's tone bins the existing FRF
    path is already the better estimator.
    """
    x = np.asarray(x_stack, float)
    y = np.asarray(y_stack, float)
    if detrend:
        x = x - x.mean(axis=0)
        y = y - y.mean(axis=0)
    X = np.fft.rfft(x, axis=1)
    Y = np.fft.rfft(y, axis=1)
    sxy = np.mean(np.conj(X) * Y, axis=0)
    sxx = np.mean(np.abs(X) ** 2, axis=0)
    h = np.zeros_like(sxy)
    np.divide(sxy, sxx, out=h, where=sxx > 0)
    f, g2 = coherence_ensemble(x_stack, y_stack, dt, detrend=detrend)
    return f, h, g2


# ------------------------------------------------------------------ reporting

def band_rms(y, dt, bands=CAMPAIGN_BANDS, detrend=True):
    """rms of `y` inside each band, by Parseval on the one-sided FFT.

    Returned in the units of `y`.  Band-limiting is also how a signal below
    the raw quantisation floor becomes measurable: restricting to a 10 kHz
    slice of a 2 MHz Nyquist buys a factor of ~14 on white content, which is
    the same route by which the monitor channel resolves 0.195 mV out of a
    multi-volt ramp.
    """
    y = np.asarray(y, float)
    y = y - y.mean() if detrend else y
    n = y.size
    Y = np.fft.rfft(y)
    f = np.fft.rfftfreq(n, dt)
    # one-sided power, normalised so that sum(p) == mean(y**2)
    p = (np.abs(Y) / n) ** 2
    p[1:] *= 2.0
    if n % 2 == 0 and len(p) > 1:
        p[-1] /= 2.0
    # Bands are half-open so neighbours do not double-count -- except the one
    # that reaches Nyquist, where an exclusive top edge would silently drop the
    # last bin and the bands would no longer sum back to the total.
    out = {}
    for lo, hi in bands:
        sel = (f >= lo) & (f <= hi if hi >= f[-1] else f < hi)
        out[(lo, hi)] = float(np.sqrt(p[sel].sum()))
    return out


def summarise(dv, dt, target_rms=None, bands=CAMPAIGN_BANDS):
    """One-line-per-band report of an inverted optical residual.

    `dv` is the residual in equivalent drive volts (the second return of
    `invert_linear`), with NaNs where the slope was masked.  NaNs are dropped
    rather than zero-filled: zeros would read as a quiet band instead of an
    absent measurement.
    """
    dv = np.asarray(dv, float)
    ok = np.isfinite(dv)
    filled = fill_masked(dv, ok)
    out = {"coverage": float(ok.mean()),
           "rms_all": float(np.sqrt(np.mean(dv[ok] ** 2))),
           "peak_all": float(np.max(np.abs(dv[ok]))),
           "bands": band_rms(filled, dt, bands)}
    if target_rms:
        out["vs_target"] = out["rms_all"] / target_rms
    return out
