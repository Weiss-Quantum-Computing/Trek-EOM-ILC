"""Relative intensity noise: segment splicing, RIN, and the two calibrations
that tell you whether the number means anything.

The measurement this exists for is out-of-loop RIN on a Thorlabs PDA10A2 at
843 nm, downstream of an AOM intensity lock whose unity-gain bandwidth is
150-250 kHz. The suppressed spectrum falls by roughly 50 dB across DC-100 kHz,
which is more dynamic range than one analyser setting has, so it is acquired in
segments: each band gets its own span, its own analog pre-filter to keep the
low-frequency content from eating the input range, and its own manually locked
range. `splice_segments` puts those back together.

The thing worth being careful about is that splicing is where the errors of the
whole chain land. Each segment carries its own filter correction, its own range
and its own averaging, and the only place any of that can be checked is where
two segments overlap and have to agree. So this module RETURNS the overlap
disagreement rather than smoothing it away: it is not a cosmetic blemish on the
join, it is the integrated error signal for the filter model, the range
calibration and the analyser's own accuracy at once. A join that disagrees by
3 dB is telling you the answer is wrong by 3 dB somewhere, and blending it into
a pretty curve throws that away.

Two calibrations are here for the same reason. `power_scaling_fit` separates
electronics, shot noise and classical RIN by their different scaling with
photocurrent, and hands back the transimpedance gain as a by-product - measured
end to end through the real chain rather than taken off the data sheet.
`johnson_check` does the same job for the electronics floor alone, against a
resistor set whose noise is known from first principles.

Nothing here imports scipy or pandas: it runs on the system interpreter, which
has numpy only. `polarimetry.py` is the style reference.

Conventions
-----------
* Spectra are one-sided.
* `psd` means POWER spectral density, V^2/Hz. `asd` means AMPLITUDE spectral
  density, V/rtHz, which is what the SR760 displays and what its CSV holds.
  The two differ by a square, so mixing them up is a factor of two in dB - the
  single easiest way to be wrong here. Functions say which they take.
* RIN is single-sided, S_V / V_DC^2, in 1/Hz; dBc/Hz is 10 log10 of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

Q_E = 1.602176634e-19          # C, exact since the 2019 SI redefinition
K_B = 1.380649e-23             # J/K, likewise

# What the campaign is trying to demonstrate: integrated dI/I over the analysis
# band. Kept here so the reporting helpers can say how far off a run is.
TARGET_DELTA_I_OVER_I = 2.4e-4

# 4kT at 300 K, V^2/Hz per ohm -- the slope `johnson_check` should recover.
JOHNSON_SLOPE_300K = 4.0 * K_B * 300.0          # 1.6568e-20


# ------------------------------------------------------------------ segments

@dataclass
class Segment:
    """One acquired band, before any filter correction.

    `range_dbv` and `note` are carried rather than used: when two segments
    disagree across their overlap, the first question is always which of them
    was on which input range, and the answer should be in the object rather
    than in somebody's notebook.
    """
    f: np.ndarray                 # Hz, increasing
    psd: np.ndarray               # V^2/Hz as measured, pre-filter correction
    label: str = ""
    range_dbv: float | None = None
    note: str = ""

    def __post_init__(self):
        self.f = np.asarray(self.f, float).ravel()
        self.psd = np.asarray(self.psd, float).ravel()
        if self.f.size != self.psd.size:
            raise ValueError(f"segment {self.label!r}: {self.f.size} "
                             f"frequencies against {self.psd.size} points")
        if self.f.size < 2:
            raise ValueError(f"segment {self.label!r}: needs at least 2 points")
        if np.any(np.diff(self.f) <= 0):
            raise ValueError(f"segment {self.label!r}: frequencies must "
                             f"increase")


def as_segment(obj, index=0):
    """Accept a Segment, an (f, psd) pair or an (f, psd, label) triple."""
    if isinstance(obj, Segment):
        return obj
    parts = tuple(obj)
    if len(parts) == 2:
        return Segment(parts[0], parts[1], label=f"seg{index}")
    if len(parts) == 3:
        return Segment(parts[0], parts[1], label=str(parts[2]))
    raise ValueError("a segment is a Segment, (f, psd) or (f, psd, label)")


def as_correction(corr, f):
    """A per-segment |H(f)|^2 on the segment's own grid.

    Accepts None (no filter), a scalar, an array already on `f`, an
    (f_h, h2) pair which is interpolated, or a callable of f. Interpolation is
    done on log10|H|^2, because a filter correction spans decades and linear
    interpolation across a rolloff is visibly wrong in between.
    """
    f = np.asarray(f, float)
    if corr is None:
        return np.ones_like(f)
    if callable(corr):
        return np.asarray(corr(f), float)
    arr = np.asarray(corr, dtype=object)
    if arr.ndim == 0:
        return np.full_like(f, float(corr))
    if isinstance(corr, tuple) and len(corr) == 2 \
            and np.ndim(corr[0]) == 1 and np.ndim(corr[1]) == 1:
        f_h = np.asarray(corr[0], float)
        h2 = np.asarray(corr[1], float)
        good = np.isfinite(h2) & (h2 > 0)
        if good.sum() < 2:
            raise ValueError("the filter response has fewer than 2 usable points")
        return 10.0 ** np.interp(f, f_h[good], np.log10(h2[good]))
    out = np.asarray(corr, float).ravel()
    if out.size != f.size:
        raise ValueError(f"a correction array must match its segment: "
                         f"{out.size} against {f.size}")
    return out


@dataclass
class Join:
    """What one pair of neighbouring segments says about each other.

    `median_db` is the headline: the systematic offset between the two, which
    is what a wrong filter correction or a mis-set range produces. `max_db` and
    `rms_db` catch the other failure, where the two agree on average but have
    different SHAPE across the overlap - that is a filter model that is wrong
    in its corner frequency rather than in its gain.
    """
    lo: str
    hi: str
    f_lo: float                   # overlap start, Hz
    f_hi: float                   # overlap end, Hz
    n: int                        # points compared
    median_db: float
    rms_db: float
    max_db: float
    f_split: float                # where the merged trace changes segment
    within_tol: bool
    gap: bool = False             # the two do not overlap at all
    lo_range_dbv: float | None = None
    hi_range_dbv: float | None = None

    def __str__(self):
        if self.gap:
            return (f"{self.lo} | {self.hi}: NO OVERLAP between "
                    f"{self.f_lo:.4g} and {self.f_hi:.4g} Hz - unverifiable")
        return (f"{self.lo} | {self.hi}: {self.median_db:+.2f} dB median over "
                f"{self.f_lo:.4g}-{self.f_hi:.4g} Hz ({self.n} pts, "
                f"rms {self.rms_db:.2f}, max {self.max_db:+.2f})"
                + ("" if self.within_tol else "   <-- OUT OF TOLERANCE"))


@dataclass
class Splice:
    """The merged PSD, and every join that went into it."""
    f: np.ndarray
    psd: np.ndarray
    joins: list = field(default_factory=list)
    segments: list = field(default_factory=list)   # corrected, in order

    @property
    def worst_db(self) -> float:
        """Largest median disagreement across the joins, in dB."""
        vals = [abs(j.median_db) for j in self.joins if not j.gap
                and np.isfinite(j.median_db)]
        return max(vals) if vals else float("nan")

    @property
    def ok(self) -> bool:
        """True only if every join overlapped AND agreed inside tolerance."""
        return bool(self.joins) and all(j.within_tol and not j.gap
                                        for j in self.joins)

    def report(self) -> str:
        head = (f"spliced {len(self.segments)} segments, "
                f"{self.f[0]:.4g}-{self.f[-1]:.4g} Hz, "
                f"worst join {self.worst_db:.2f} dB")
        return "\n".join([head] + ["  " + str(j) for j in self.joins])


def splice_segments(segments, corrections=None, overlap_tol_db=1.0):
    """Merge segments taken at different spans, ranges and pre-filters.

    Each segment is divided by its own |H(f)|^2 first, so `corrections` is a
    list the same length as `segments` - see `as_correction` for what each
    entry may be, and `filter_response` for measuring one. Segments are then
    sorted by start frequency and compared where they overlap.

    **The overlap disagreement is a return value, not a defect to be hidden.**
    Neighbouring segments are the only cross-check the chain has: they see the
    same physical noise through different filters, different ranges and
    different averaging, so where they disagree is the size of the error in all
    of that put together. The merged trace therefore does NOT blend across the
    overlap - each segment owns its band up to the midpoint of the overlap, and
    the step you can see at the join is the honest size of the disagreement.
    Blending would make the picture prettier and the number less true.

    This is not the start-frequency stitcher in spectrum_grab. That one joins
    bands taken at ONE span and ONE range, where the segments share their bin
    frequencies exactly and the join is arithmetic. Here nothing is shared:
    different spans give different bin spacings, and the whole point is the
    comparison.

    Returns a `Splice`. Check `.ok` before believing `.psd`, and print
    `.report()` into the run's notes either way.
    """
    segs = [as_segment(s, i) for i, s in enumerate(segments)]
    if not segs:
        raise ValueError("nothing to splice")
    if corrections is None:
        corrections = [None] * len(segs)
    corrections = list(corrections)
    if len(corrections) != len(segs):
        raise ValueError(f"{len(corrections)} corrections for {len(segs)} "
                         f"segments")

    # Correct first, then order. Dividing by |H|^2 is what takes the analog
    # pre-filter back out; a segment with no filter passes 1 and is unchanged.
    fixed = []
    for seg, corr in zip(segs, corrections):
        h2 = as_correction(corr, seg.f)
        with np.errstate(divide="ignore", invalid="ignore"):
            psd = np.where(h2 > 0, seg.psd / h2, np.nan)
        fixed.append(Segment(seg.f, psd, label=seg.label,
                             range_dbv=seg.range_dbv, note=seg.note))
    fixed.sort(key=lambda s: s.f[0])

    if len(fixed) == 1:
        return Splice(f=fixed[0].f.copy(), psd=fixed[0].psd.copy(),
                      joins=[], segments=fixed)

    joins, splits = [], []
    for lo, hi in zip(fixed[:-1], fixed[1:]):
        join = _compare(lo, hi, overlap_tol_db)
        joins.append(join)
        splits.append(join.f_split)

    # Each segment owns [previous split, next split); the ends run out to the
    # data. Built as masks so a segment whose whole band was swallowed by its
    # neighbours simply contributes nothing rather than producing a bad slice.
    edges = [-np.inf] + splits + [np.inf]
    f_out, p_out = [], []
    for seg, lo_edge, hi_edge in zip(fixed, edges[:-1], edges[1:]):
        keep = (seg.f >= lo_edge) & (seg.f < hi_edge)
        f_out.append(seg.f[keep])
        p_out.append(seg.psd[keep])
    f_all = np.concatenate(f_out)
    p_all = np.concatenate(p_out)
    order = np.argsort(f_all, kind="stable")
    return Splice(f=f_all[order], psd=p_all[order], joins=joins,
                  segments=fixed)


def _compare(lo, hi, tol_db):
    """One join: how far apart two corrected segments are where they overlap."""
    f_lo = max(lo.f[0], hi.f[0])
    f_hi = min(lo.f[-1], hi.f[-1])
    common = dict(lo=lo.label, hi=hi.label, lo_range_dbv=lo.range_dbv,
                  hi_range_dbv=hi.range_dbv)
    if f_hi <= f_lo:
        # A gap is not a small problem. Nothing checks the two against each
        # other, so the merged trace is two unverified pieces end to end.
        gap_lo, gap_hi = lo.f[-1], hi.f[0]
        return Join(f_lo=gap_lo, f_hi=gap_hi, n=0, median_db=float("nan"),
                    rms_db=float("nan"), max_db=float("nan"),
                    f_split=0.5 * (gap_lo + gap_hi), within_tol=False,
                    gap=True, **common)

    # Compare on whichever segment samples the overlap more finely, so the
    # comparison is not limited by the coarser grid.
    in_lo = (lo.f >= f_lo) & (lo.f <= f_hi)
    in_hi = (hi.f >= f_lo) & (hi.f <= f_hi)
    if in_hi.sum() > in_lo.sum():
        grid, a, b = hi.f[in_hi], _interp_db(lo, hi.f[in_hi]), hi.psd[in_hi]
    else:
        grid, a, b = lo.f[in_lo], lo.psd[in_lo], _interp_db(hi, lo.f[in_lo])

    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = 10.0 * np.log10(np.asarray(a, float) / np.asarray(b, float))
    good = np.isfinite(ratio)
    if not good.any():
        return Join(f_lo=float(f_lo), f_hi=float(f_hi), n=0,
                    median_db=float("nan"), rms_db=float("nan"),
                    max_db=float("nan"), f_split=0.5 * (f_lo + f_hi),
                    within_tol=False, gap=False, **common)
    r = ratio[good]
    median = float(np.median(r))
    return Join(f_lo=float(f_lo), f_hi=float(f_hi), n=int(good.sum()),
                median_db=median, rms_db=float(np.sqrt(np.mean(r ** 2))),
                max_db=float(r[np.argmax(np.abs(r))]),
                f_split=float(0.5 * (f_lo + f_hi)),
                within_tol=bool(abs(median) <= tol_db), gap=False, **common)


def _interp_db(seg, f):
    """Sample a segment at `f`, interpolating in dB.

    A spectrum with 50 dB of tilt across it is a straight line in dB and a
    cliff in linear units, so interpolating the linear PSD across a coarse bin
    spacing biases the comparison by more than the disagreement being measured.
    """
    good = np.isfinite(seg.psd) & (seg.psd > 0)
    if good.sum() < 2:
        return np.full(np.shape(f), np.nan)
    return 10.0 ** np.interp(f, seg.f[good], np.log10(seg.psd[good]))


# ------------------------------------------------------------ filter response

def filter_response(dark_with, dark_without, floor=0.0):
    """|H(f)|^2 of the analog pre-filter, measured in situ from two darks.

    `dark_with` is the dark spectrum through the filter, `dark_without` the
    same measurement with the filter taken out. Both are (f, psd) pairs or
    Segments, in V^2/Hz, and both must be taken at the same span, the same
    input range and the same averaging - the ratio is only a filter response if
    nothing else changed between them.

    Measuring it this way rather than sweeping the filter on the bench is the
    point: the response that matters is the one the filter has when it is
    loaded by the actual cable and the analyser's actual input impedance, and a
    swept measurement into a different load misses exactly that. It also picks
    up whatever the cable itself does, which is the same correction as far as
    the data is concerned.

    Feed the result straight to `splice_segments` as that segment's correction.

    `floor` subtracts a known analyser noise floor from both spectra first. The
    correction is only valid where the un-filtered dark is well above the
    analyser's own floor; where it is not, the ratio measures the analyser
    rather than the filter. Points that go non-positive come back NaN, and
    `as_correction` will interpolate across them in log space.
    """
    a = as_segment(dark_with, 0)
    b = as_segment(dark_without, 1)
    f = a.f
    num = a.psd - floor
    den = _interp_db(b, f) - floor if not np.array_equal(a.f, b.f) \
        else b.psd - floor
    with np.errstate(divide="ignore", invalid="ignore"):
        h2 = np.where((den > 0) & (num > 0), num / den, np.nan)
    return f, h2


# --------------------------------------------------------------------- RIN

def rin(psd_v_per_rthz, v_dc):
    """RIN from an AMPLITUDE spectral density in V/rtHz.

    This is the SR760's own unit and the one its CSV carries, which is why it
    is the default here. RIN = S_V / V_DC^2 with S_V the POWER density, so the
    input is squared on the way in - feed this a V^2/Hz array and the answer is
    wrong by a square. Use `rin_from_psd` when you already have power.

    Returns (rin_per_hz, rin_dbc_per_hz).
    """
    asd = np.asarray(psd_v_per_rthz, float)
    return rin_from_psd(asd ** 2, v_dc)


def rin_from_psd(s_v, v_dc):
    """RIN from a POWER spectral density in V^2/Hz. Returns (1/Hz, dBc/Hz)."""
    s_v = np.asarray(s_v, float)
    v_dc = float(v_dc)
    if v_dc == 0:
        raise ValueError("v_dc is zero: RIN is undefined without a DC level")
    r = s_v / (v_dc * v_dc)
    with np.errstate(divide="ignore", invalid="ignore"):
        db = 10.0 * np.log10(np.where(r > 0, r, np.nan))
    return r, db


def shot_noise_rin(v_dc, gain):
    """The shot-noise floor of a photodiode sitting at `v_dc`, in 1/Hz.

    RIN_shot = 2 q G / V_DC, with G the transimpedance in V/A. It falls as
    1/V_DC, which is the whole basis of `power_scaling_fit`: the electronics
    floor does not move with light level, shot noise falls as 1/P, and
    classical RIN does not move at all.

    `gain` is the real end-to-end V/A. Take it from `power_scaling_fit`, which
    measures it through the chain, rather than from the data sheet - the PDA10A2
    has a switched gain and a bandwidth that depends on it.
    """
    v_dc = np.asarray(v_dc, float)
    return 2.0 * Q_E * float(gain) / v_dc


def integrate_rin(rin_per_hz, f, band=None):
    """rms dI/I over a band: sqrt of the integral of RIN.

    RIN is a density in 1/Hz, so integrating it over a band gives the mean
    square fractional intensity fluctuation in that band, and the root of that
    is the number the campaign quotes - 2.4e-4 is the target here.

    `band` is (lo, hi) in Hz; None uses everything. Trapezium rule on whatever
    grid the data is on, NaNs dropped, which is what makes it safe to run
    straight on a spliced trace where a bad join has left holes.
    """
    r = np.asarray(rin_per_hz, float)
    f = np.asarray(f, float)
    if r.shape != f.shape:
        raise ValueError("rin and f must be the same shape")
    sel = np.isfinite(r) & np.isfinite(f)
    if band is not None:
        sel &= (f >= band[0]) & (f <= band[1])
    if sel.sum() < 2:
        return float("nan")
    trapz = getattr(np, "trapezoid", None) or np.trapz
    return float(np.sqrt(trapz(r[sel], f[sel])))


# ------------------------------------------------------- power scaling

@dataclass
class PowerScaling:
    """S_V = a + b V_DC + c V_DC^2, split by how each term scales with light.

    a  electronics, V^2/Hz -- dark noise, independent of the light level
    b  shot noise, V^2/Hz per V -- proportional to photocurrent
    c  classical RIN, 1/Hz -- proportional to power squared

    `c` is the classical RIN directly: S = c V^2 means S / V^2 = c, so the
    quadratic coefficient IS the laser's own RIN in 1/Hz at this frequency,
    with the electronics and shot terms already taken off it. That is the
    cleanest RIN estimate this module produces, because it is the one that does
    not need the other two to be known.

    `gain` is the transimpedance implied by the shot term, G = b / (2q). It is
    an end-to-end calibration through the real chain: cable, filter, analyser
    input and all, which is what makes it worth having next to the data sheet
    number rather than instead of it.
    """
    a: float
    b: float
    c: float
    sigma: tuple                  # 1-sigma on (a, b, c)
    v_dc: np.ndarray
    psd: np.ndarray
    fit: np.ndarray               # the model evaluated at v_dc
    n: int

    @property
    def gain(self) -> float:
        """Transimpedance V/A from the shot term."""
        return self.b / (2.0 * Q_E)

    @property
    def gain_sigma(self) -> float:
        return self.sigma[1] / (2.0 * Q_E)

    @property
    def classical_rin(self) -> float:
        """1/Hz -- the quadratic coefficient, which is RIN by definition."""
        return self.c

    @property
    def residual_frac(self) -> np.ndarray:
        return (self.psd - self.fit) / self.psd

    def crossover_v_dc(self) -> float:
        """DC level where shot noise equals the electronics floor, b V = a.

        Below it the measurement is electronics-limited and says nothing about
        the laser; the ND sweep should straddle it.
        """
        return self.a / self.b if self.b else float("nan")

    def shot_v_dc(self) -> float:
        """DC level where shot noise equals classical RIN, b V = c V^2.

        Above it the laser's own noise buries the shot term. Together with
        `crossover_v_dc` this brackets the window in which the shot term is the
        largest of the three - which is the window the gain is measured in.
        """
        return self.b / self.c if self.c else float("inf")

    @property
    def shot_fraction(self) -> np.ndarray:
        """Fraction of the fitted S_V that the shot term carries, per point."""
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self.fit > 0, self.b * self.v_dc / self.fit, np.nan)

    @property
    def shot_dominance(self) -> float:
        """The best the shot term ever manages, as a fraction of the total.

        **Read this before believing `gain`.** The three terms are separated
        only by their different scaling, so the shot term is well determined
        only where it is actually the biggest thing in the measurement. When
        classical RIN dominates everywhere - which is the normal case for a
        laser that has not been stabilised yet - this sits well below 0.5, the
        fit is ill-conditioned in b, and a 1% error in the data can move the
        extracted gain by tens of per cent. Measured on synthetic data: with
        shot peaking at 0.41 of the total, a 1-2% perturbation moved the gain
        by +53%.

        Above about 0.5 the gain is trustworthy to a few per cent. Below it,
        take more attenuation - or measure the gain some other way and treat
        this fit as a consistency check rather than a calibration.
        """
        frac = self.shot_fraction
        return float(np.nanmax(frac)) if np.any(np.isfinite(frac)) else float("nan")

    @property
    def gain_is_constrained(self) -> bool:
        """Whether the sweep actually pins the shot term down."""
        return bool(self.shot_dominance >= 0.5)

    def __str__(self):
        note = "" if self.gain_is_constrained else \
            "   <-- shot term never dominates; gain is not a calibration"
        return (f"electronics {self.a:.4g} V^2/Hz, shot {self.b:.4g} V^2/Hz/V, "
                f"classical RIN {self.c:.4g} /Hz, gain {self.gain:.4g} V/A "
                f"(shot peaks at {self.shot_dominance:.2f} of total){note}")


def power_scaling_fit(v_dc_array, psd_array, relative_sigma=True):
    """Separate electronics, shot noise and classical RIN by their scaling.

    `v_dc_array` is the DC photodiode level at each step of an ND attenuation
    sweep; `psd_array` is the measured POWER density S_V in V^2/Hz at one
    frequency, at each of those steps. Fits S_V = a + b V + c V^2 by ordinary
    least squares.

    The three terms are separable because they scale differently and for no
    other reason, so the sweep has to cover enough range for that to bite - a
    factor of ten in V_DC or better, straddling the point where shot noise
    passes the electronics floor. `PowerScaling.crossover_v_dc` says where that
    was, and a sweep entirely on one side of it will return a confident fit
    with meaningless coefficients.

    `relative_sigma` weights each point by 1/S, which is the right thing for a
    PSD and is not a detail: the uncertainty on an averaged spectral density is
    FRACTIONAL, so an unweighted fit across two decades of V_DC is set almost
    entirely by the brightest point and throws away the dim ones that carry the
    electronics and shot information. Measured on synthetic data with a 1-2%
    perturbation, the unweighted fit returned the gain 53% high where the
    weighted one was correct to 0.1%. Turn it off only to reproduce a plain
    least-squares fit.

    Check `shot_dominance` on the result before treating `gain` as a
    calibration - it says whether the sweep constrained the shot term at all.

    Needs at least 4 points to leave a degree of freedom for the uncertainties;
    with exactly 3 the fit is exact and sigma comes back NaN, which is honest.
    """
    v = np.asarray(v_dc_array, float).ravel()
    s = np.asarray(psd_array, float).ravel()
    if v.size != s.size:
        raise ValueError("v_dc and psd must be the same length")
    good = np.isfinite(v) & np.isfinite(s)
    v, s = v[good], s[good]
    if v.size < 3:
        raise ValueError("the quadratic needs at least 3 usable points")
    if np.ptp(v) <= 0:
        raise ValueError("the sweep does not move in V_DC")

    M = np.column_stack([np.ones_like(v), v, v * v])
    if relative_sigma and np.all(s > 0):
        w = 1.0 / s
        design = M * w[:, None]
        coef, *_ = np.linalg.lstsq(design, s * w, rcond=None)
        resid = (M @ coef - s) * w
    else:
        design = M
        coef, *_ = np.linalg.lstsq(M, s, rcond=None)
        resid = M @ coef - s
    fit = M @ coef
    dof = v.size - 3
    if dof > 0:
        var = float(np.sum(resid ** 2)) / dof
        try:
            cov = var * np.linalg.inv(design.T @ design)
            sigma = tuple(float(x) for x in np.sqrt(np.abs(np.diag(cov))))
        except np.linalg.LinAlgError:
            sigma = (float("nan"),) * 3
    else:
        sigma = (float("nan"),) * 3
    return PowerScaling(a=float(coef[0]), b=float(coef[1]), c=float(coef[2]),
                        sigma=sigma, v_dc=v, psd=s, fit=fit, n=int(v.size))


# ------------------------------------------------------------ Johnson noise

@dataclass
class JohnsonFit:
    """S_meas = S_floor + 4kT R across a resistor set.

    The slope is a first-principles number: 4kT is 1.6568e-20 V^2/Hz per ohm at
    300 K and depends on nothing but the temperature. Recovering it says the
    whole voltage-noise scale of the measurement - analyser calibration, gain,
    the lot - is right end to end. A slope 20% low is a 20% error in every
    voltage density this campaign reports.
    """
    slope: float                  # V^2/Hz per ohm
    floor: float                  # V^2/Hz at R = 0, the amplifier's own noise
    sigma: tuple                  # 1-sigma on (floor, slope)
    expected_slope: float
    resistances: np.ndarray
    measured: np.ndarray
    fit: np.ndarray
    temperature_k: float

    @property
    def slope_ratio(self) -> float:
        """Fitted slope over 4kT. 1.0 is perfect."""
        return self.slope / self.expected_slope if self.expected_slope else float("nan")

    @property
    def deviation_pct(self) -> float:
        """Signed deviation from 4kT, per cent."""
        return 100.0 * (self.slope_ratio - 1.0)

    @property
    def deviation_db(self) -> float:
        with np.errstate(divide="ignore", invalid="ignore"):
            return float(10.0 * np.log10(self.slope_ratio)) \
                if self.slope_ratio > 0 else float("nan")

    @property
    def implied_temperature_k(self) -> float:
        """The temperature the fitted slope corresponds to.

        Reads as a sanity check with units: a slope that implies 900 K is not a
        hot resistor, it is a gain error of three.
        """
        return self.slope / (4.0 * K_B)

    def __str__(self):
        return (f"4kT slope {self.slope:.4g} V^2/Hz/ohm vs "
                f"{self.expected_slope:.4g} expected "
                f"({self.deviation_pct:+.1f}%, {self.deviation_db:+.2f} dB), "
                f"floor {self.floor:.4g} V^2/Hz, "
                f"implied T {self.implied_temperature_k:.0f} K")


def band_average(f, psd, band):
    """Mean POWER density over a band - one number per resistor.

    Each resistor needs its own band. 100 kohm rolls off above roughly 2 kHz
    against the input plus cable capacitance, so averaging it over the same
    band as the 50 ohm measures the rolloff rather than the Johnson noise and
    pulls the fitted slope down. Pick each band inside that resistor's own flat
    region and pass the numbers to `johnson_check`.
    """
    f = np.asarray(f, float)
    psd = np.asarray(psd, float)
    sel = (f >= band[0]) & (f <= band[1]) & np.isfinite(psd)
    if not sel.any():
        return float("nan")
    return float(np.mean(psd[sel]))


def johnson_check(resistances, measured_psd, temperature_k=300.0,
                  relative_sigma=True):
    """Fit S_meas = S_floor + 4kT R and say how far the slope is from 4kT.

    `measured_psd` is one band-averaged POWER density in V^2/Hz per resistor -
    see `band_average`, and use a band inside each resistor's own flat region.
    A typical set is 50 ohm, 2k, 10k, 100k, which spans enough resistance for
    the slope to be well determined while the 50 ohm point pins the floor.

    `relative_sigma` weights each point by 1/S^2, which is the right thing for
    a PSD: the uncertainty on an averaged spectral density is FRACTIONAL, so an
    unweighted fit across three decades of resistance is set almost entirely by
    the largest resistor and ignores the small ones that determine the floor.
    Turn it off to reproduce a plain unweighted fit.
    """
    r = np.asarray(resistances, float).ravel()
    s = np.asarray(measured_psd, float).ravel()
    if r.size != s.size:
        raise ValueError("resistances and measured_psd must be the same length")
    good = np.isfinite(r) & np.isfinite(s)
    r, s = r[good], s[good]
    if r.size < 2:
        raise ValueError("need at least 2 usable resistors")
    if np.ptp(r) <= 0:
        raise ValueError("every resistor is the same value")

    M = np.column_stack([np.ones_like(r), r])
    if relative_sigma and np.all(s > 0):
        w = 1.0 / s
        coef, *_ = np.linalg.lstsq(M * w[:, None], s * w, rcond=None)
        design = M * w[:, None]
        resid = (M @ coef - s) * w
    else:
        coef, *_ = np.linalg.lstsq(M, s, rcond=None)
        design = M
        resid = M @ coef - s
    fit = M @ coef
    dof = r.size - 2
    if dof > 0:
        var = float(np.sum(resid ** 2)) / dof
        try:
            cov = var * np.linalg.inv(design.T @ design)
            sigma = tuple(float(x) for x in np.sqrt(np.abs(np.diag(cov))))
        except np.linalg.LinAlgError:
            sigma = (float("nan"),) * 2
    else:
        sigma = (float("nan"),) * 2
    return JohnsonFit(slope=float(coef[1]), floor=float(coef[0]), sigma=sigma,
                      expected_slope=4.0 * K_B * float(temperature_k),
                      resistances=r, measured=s, fit=fit,
                      temperature_k=float(temperature_k))
