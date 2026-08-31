"""Synthetic end-to-end check of the RIN chain and the scope auto-PSD.

No instruments.  The point, as with test_polarimetry.py, is to validate the
ANALYSIS before any bench time is spent: build a spectrum whose answer is known
in closed form, push it through the real segmenting, filtering and fitting code,
and confirm the numbers come back.  A factor-of-two units slip between V/rtHz
and V^2/Hz, or a window normalisation that forgot its ENBW, would otherwise show
up on the bench looking exactly like physics.

Unlike the rest of the suite this one runs on the SYSTEM interpreter as well as
Anaconda -- eomilc.rin is pure numpy and the package now imports lazily, so
neither scipy nor pandas is needed:

    python tests/test_rin.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eomilc import rin as R                       # noqa: E402
from eomilc import scope as scopeio               # noqa: E402

checks = 0


def ok(label, condition, detail=""):
    global checks
    checks += 1
    mark = "OK " if condition else "FAIL"
    print(f"[{checks}] {mark} {label}" + (f"   {detail}" if detail else ""))
    if not condition:
        raise AssertionError(label + " " + detail)


def close(a, b, rtol=1e-6, atol=0.0):
    return bool(np.all(np.abs(np.asarray(a) - np.asarray(b))
                       <= atol + rtol * np.abs(np.asarray(b))))


# ---------------------------------------------------------------- 1. units
print("\n--- RIN units and integration ---")

r, db = R.rin(np.array([1e-6, 1e-5]), 1.0)
ok("rin() squares the amplitude density", close(r, [1e-12, 1e-10]),
   f"{r}")
ok("rin() dBc/Hz is 10log10", close(db, [-120.0, -100.0], atol=1e-9), f"{db}")

r2, _ = R.rin_from_psd(np.array([1e-12]), 1.0)
ok("rin_from_psd takes power directly", close(r2, [1e-12]))

r3, _ = R.rin(np.array([1e-6]), 2.0)
ok("RIN falls as 1/V_DC^2", close(r3, [2.5e-13]), f"{r3}")

# 2 q G / V_DC
ok("shot_noise_rin = 2qG/V_DC",
   close(R.shot_noise_rin(1.0, 1e4), 2 * R.Q_E * 1e4), "")
ok("shot noise falls as 1/P",
   close(R.shot_noise_rin(2.0, 1e4), R.shot_noise_rin(1.0, 1e4) / 2))

f = np.linspace(0.0, 1e5, 40001)
white = np.full_like(f, 1e-12)
ok("integrate_rin of white RIN", close(R.integrate_rin(white, f), np.sqrt(1e-12 * 1e5),
                                       rtol=1e-9),
   f"{R.integrate_rin(white, f):.6e}")
ok("integrate_rin honours the band",
   close(R.integrate_rin(white, f, (0.0, 1e4)), np.sqrt(1e-12 * 1e4), rtol=1e-6))

# the campaign target, hit exactly by construction
need = R.TARGET_DELTA_I_OVER_I
flat = np.full_like(f, need ** 2 / 1e5)
ok("a spectrum built to hit 2.4e-4 integrates to it",
   close(R.integrate_rin(flat, f), need, rtol=1e-9),
   f"{R.integrate_rin(flat, f):.4e}")

nan_mix = white.copy()
nan_mix[100:200] = np.nan
ok("integrate_rin drops NaNs rather than propagating",
   np.isfinite(R.integrate_rin(nan_mix, f)))


# -------------------------------------------------------- 2. filter response
print("\n--- filter response measured in situ ---")

fh = np.linspace(1.0, 1e5, 2000)
f_c = 1e3
h2_true = 1.0 / (1.0 + (fh / f_c) ** 2)          # one-pole, |H|^2
dark_flat = np.full_like(fh, 4e-16)              # V^2/Hz, filter out
dark_filt = dark_flat * h2_true                  # filter in

f_meas, h2_meas = R.filter_response((fh, dark_filt), (fh, dark_flat))
ok("filter_response recovers |H|^2", close(h2_meas, h2_true, rtol=1e-9),
   f"max err {np.max(np.abs(h2_meas - h2_true)):.2e}")
ok("filter_response returns the measurement grid", close(f_meas, fh))

# off-grid: the un-filtered dark on a coarser grid must still work
coarse = fh[::7]
f2, h22 = R.filter_response((fh, dark_filt), (coarse, dark_flat[::7]))
ok("filter_response interpolates a mismatched grid",
   close(h22, h2_true, rtol=2e-3), f"max err {np.max(np.abs(h22 - h2_true)):.2e}")

bad = R.filter_response((fh, dark_filt), (fh, np.zeros_like(fh)))[1]
ok("a zero reference gives NaN, not infinity", bool(np.all(np.isnan(bad))))


# ------------------------------------------------------------- 3. splicing
print("\n--- segment splicing ---")

def truth(x):
    """A 50 dB tilt across DC-100 kHz, like the suppressed spectrum."""
    return 1e-12 * (1.0 + (x / 300.0) ** -1.6) * 1e-2 + 1e-17


f_a = np.linspace(1.0, 400.0, 400)               # segment A, narrow span
f_b = np.linspace(300.0, 12e3, 400)              # B, overlaps A 300-400
f_c = np.linspace(10e3, 100e3, 400)              # C, overlaps B 10-12k

h2_a = 1.0 / (1.0 + (f_a / 2e3) ** 2)            # each band its own pre-filter
h2_b = 1.0 / (1.0 + (f_b / 30e3) ** 2)
h2_c = np.ones_like(f_c)

segs = [R.Segment(f_a, truth(f_a) * h2_a, "A", range_dbv=-30),
        R.Segment(f_b, truth(f_b) * h2_b, "B", range_dbv=-20),
        R.Segment(f_c, truth(f_c) * h2_c, "C", range_dbv=-10)]
sp = R.splice_segments(segs, [(f_a, h2_a), (f_b, h2_b), None])

ok("splice returns one join per adjacent pair", len(sp.joins) == 2)
ok("a correct chain agrees across every join", sp.ok, sp.report())
ok("the disagreement is ~0 dB when the corrections are right",
   sp.worst_db < 0.05, f"worst {sp.worst_db:.4f} dB")
ok("merged trace spans all three segments",
   close(sp.f[0], f_a[0]) and close(sp.f[-1], f_c[-1]),
   f"{sp.f[0]:.4g}..{sp.f[-1]:.4g} Hz")
ok("merged trace is monotonic in frequency", bool(np.all(np.diff(sp.f) > 0)))
ok("merged PSD matches the truth it was built from",
   close(sp.psd, truth(sp.f), rtol=0.02),
   f"max rel {np.max(np.abs(sp.psd / truth(sp.f) - 1)):.3e}")

# A wrong filter correction must SHOW, not be blended away.
sp_bad = R.splice_segments(segs, [(f_a, h2_a * 0.5), (f_b, h2_b), None])
ok("a 3 dB filter error appears as a 3 dB join",
   close(abs(sp_bad.joins[0].median_db), 3.01, rtol=0.05),
   f"{sp_bad.joins[0].median_db:+.3f} dB")
ok("and the splice reports itself not ok", not sp_bad.ok)
ok("worst_db picks it up", close(sp_bad.worst_db, 3.01, rtol=0.05))

# A gap between segments is unverifiable and must say so.
gap = R.splice_segments([R.Segment(f_a, truth(f_a), "A"),
                         R.Segment(np.linspace(2e3, 1e4, 200),
                                   truth(np.linspace(2e3, 1e4, 200)), "D")])
ok("a gap is flagged, not silently bridged",
   gap.joins[0].gap and not gap.joins[0].within_tol, str(gap.joins[0]))

ok("range labels survive into the join",
   sp.joins[0].lo_range_dbv == -30 and sp.joins[0].hi_range_dbv == -20)

one = R.splice_segments([R.Segment(f_a, truth(f_a), "solo")])
ok("a single segment splices to itself with no joins",
   len(one.joins) == 0 and close(one.psd, truth(f_a)))

ok("splice accepts bare (f, psd) tuples",
   len(R.splice_segments([(f_a, truth(f_a)), (f_b, truth(f_b))]).joins) == 1)

try:
    R.splice_segments(segs, [None, None])
    ok("mismatched correction count is refused", False)
except ValueError:
    ok("mismatched correction count is refused", True)


# ------------------------------------------------------- 4. power scaling
print("\n--- power scaling / in-situ gain ---")

G = 4.75e4                       # V/A, PDA10A2-ish
a_true = 9.0e-16                 # V^2/Hz electronics
c_true = 1.3e-13                 # 1/Hz classical RIN
b_true = 2.0 * R.Q_E * G         # shot term
v_sweep = np.array([0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0])
s_sweep = a_true + b_true * v_sweep + c_true * v_sweep ** 2

fit = R.power_scaling_fit(v_sweep, s_sweep)
ok("electronics term recovered", close(fit.a, a_true, rtol=1e-6), f"{fit.a:.4e}")
ok("shot term recovered", close(fit.b, b_true, rtol=1e-6), f"{fit.b:.4e}")
ok("classical RIN recovered", close(fit.c, c_true, rtol=1e-6), f"{fit.c:.4e}")
ok("transimpedance gain extracted in situ", close(fit.gain, G, rtol=1e-6),
   f"{fit.gain:.4g} V/A vs {G:.4g}")
ok("classical_rin is the quadratic coefficient", close(fit.classical_rin, c_true))
ok("crossover V_DC is where b V = a",
   close(fit.crossover_v_dc(), a_true / b_true, rtol=1e-6),
   f"{fit.crossover_v_dc():.4g} V")

ok("shot RIN from the fitted gain matches the closed form",
   close(R.shot_noise_rin(1.0, fit.gain), b_true, rtol=1e-6),
   f"{R.shot_noise_rin(1.0, fit.gain):.4e}")
ok("shot=classical crossover is b/c",
   close(fit.shot_v_dc(), b_true / c_true, rtol=1e-6), f"{fit.shot_v_dc():.4g} V")

# This sweep has classical RIN dominating almost everywhere, so the shot term
# is only ever a minority of the signal.  That is an experiment-design fact,
# and the fit has to admit it rather than hand back a confident wrong gain.
ok("shot term never dominates this sweep", fit.shot_dominance < 0.5,
   f"peaks at {fit.shot_dominance:.3f} of the total")
ok("so the fit reports the gain as unconstrained", not fit.gain_is_constrained)
ok("and says so when printed", "not a calibration" in str(fit))

pert = 1.0 + np.array([0.01, -0.01, 0.02, -0.015, 0.005, -0.005, 0.01, -0.01])
noisy = s_sweep * pert
fit_n = R.power_scaling_fit(v_sweep, noisy)
fit_u = R.power_scaling_fit(v_sweep, noisy, relative_sigma=False)
# Relative weighting is the whole difference between a usable gain and a
# useless one: PSD errors are fractional, so an unweighted fit is set by the
# brightest point alone.
ok("relative weighting survives a 1-2% perturbation",
   abs(fit_n.gain / G - 1.0) < 0.05, f"{fit_n.gain:.4g} V/A "
   f"({100 * (fit_n.gain / G - 1):+.1f}%)")
ok("an unweighted fit does not",
   abs(fit_u.gain / G - 1.0) > 0.25, f"{fit_u.gain:.4g} V/A "
   f"({100 * (fit_u.gain / G - 1):+.0f}%)")
ok("uncertainties come back finite with spare degrees of freedom",
   bool(np.all(np.isfinite(fit_n.sigma))), f"{fit_n.sigma}")

# A sweep where shot noise really does dominate: the gain becomes a calibration
c_quiet = 2.0e-15
s_quiet = a_true + b_true * v_sweep + c_quiet * v_sweep ** 2
fit_q = R.power_scaling_fit(v_sweep, s_quiet * pert)
ok("a shot-dominated sweep is reported as constrained",
   fit_q.gain_is_constrained, f"shot peaks at {fit_q.shot_dominance:.3f}")
ok("and then the gain is good to a few per cent",
   abs(fit_q.gain / G - 1.0) < 0.03, f"{fit_q.gain:.4g} V/A")

three = R.power_scaling_fit(v_sweep[:3], s_sweep[:3])
ok("an exactly-determined fit reports NaN sigma rather than zero",
   bool(np.all(np.isnan(three.sigma))))

try:
    R.power_scaling_fit([1.0, 1.0, 1.0], [1.0, 2.0, 3.0])
    ok("a sweep that does not move is refused", False)
except ValueError:
    ok("a sweep that does not move is refused", True)


# ---------------------------------------------------------- 5. Johnson noise
print("\n--- Johnson floor ---")

res = np.array([50.0, 2e3, 10e3, 100e3])
floor_true = 2.5e-16
s_johnson = floor_true + R.JOHNSON_SLOPE_300K * res

jf = R.johnson_check(res, s_johnson)
ok("4kT slope recovered", close(jf.slope, R.JOHNSON_SLOPE_300K, rtol=1e-6),
   f"{jf.slope:.5e}")
ok("floor recovered", close(jf.floor, floor_true, rtol=1e-4), f"{jf.floor:.4e}")
ok("deviation from 1.657e-20 is ~0", abs(jf.deviation_pct) < 0.01,
   f"{jf.deviation_pct:+.4f}%")
ok("implied temperature is 300 K", close(jf.implied_temperature_k, 300.0, rtol=1e-4),
   f"{jf.implied_temperature_k:.2f} K")
ok("expected_slope is 4kT at the stated temperature",
   close(jf.expected_slope, 4 * R.K_B * 300.0))

# a 20% gain error must read as a 20% slope error, not be absorbed by the floor
jf_bad = R.johnson_check(res, s_johnson * 0.8)
ok("a 20% scale error shows in the slope",
   close(jf_bad.deviation_pct, -20.0, rtol=0.02), f"{jf_bad.deviation_pct:+.2f}%")
ok("and in dB", close(jf_bad.deviation_db, -0.969, rtol=0.02),
   f"{jf_bad.deviation_db:+.3f} dB")

# 100k rolls off above ~2 kHz: band_average per resistor is the way round it
f_r = np.linspace(10.0, 20e3, 2000)
roll = 1.0 / (1.0 + (f_r / 2e3) ** 2)
psd_100k = (floor_true + R.JOHNSON_SLOPE_300K * 100e3) * roll
wide = R.band_average(f_r, psd_100k, (10.0, 20e3))
narrow = R.band_average(f_r, psd_100k, (10.0, 500.0))
ok("band_average over the flat region beats the wide band",
   narrow > wide * 2, f"narrow {narrow:.3e} vs wide {wide:.3e}")
s_mixed = s_johnson.copy()
s_mixed[3] = narrow
jf_band = R.johnson_check(res, s_mixed)
ok("per-resistor valid bands keep the slope honest",
   abs(jf_band.deviation_pct) < 5.0, f"{jf_band.deviation_pct:+.2f}%")

ok("relative weighting is not defeated by three decades of R",
   abs(R.johnson_check(res, s_johnson, relative_sigma=False).deviation_pct) < 0.01)

try:
    R.johnson_check([1e3], [1e-18])
    ok("a single resistor is refused", False)
except ValueError:
    ok("a single resistor is refused", True)


# ------------------------------------------------------------- 6. scope PSD
print("\n--- scope auto-PSD ---")

rng = np.random.default_rng(20260830)
fs = 2.0e6
n = 8192
stack = rng.normal(0.0, 3.0e-3, size=(24, n))

for name, expect in (("uniform", 1.0), ("hann", 1.5), ("bmh", 2.0044),
                     ("flattop", 3.7702)):
    w = scopeio.window(name, 4096)
    ok(f"ENBW of {name}", close(scopeio.enbw_bins(w), expect, rtol=2e-3),
       f"{scopeio.enbw_bins(w):.4f} bins")

# Parseval: sum(S) df must equal the variance, whichever window was used -- that
# is what the sum(w^2) normalisation buys.  The tolerance differs by window and
# that is not slop: a window correlates neighbouring bins, so the integrated
# total of a NOISE record scatters more the broader the window is.  Uniform
# leaves the bins independent and lands within 1e-4; flat top, at 3.77 bins of
# ENBW, scatters a few tenths of a per cent at this shot count and converges as
# more shots are added (measured: 0.5% at 24 shots, 0.2% at 200).
wide = np.concatenate([stack, rng.normal(0.0, 3.0e-3, size=(72, n))])
for name, rtol in (("uniform", 1e-3), ("hann", 8e-3), ("hamming", 8e-3),
                   ("blackman", 8e-3), ("bmh", 8e-3), ("flattop", 8e-3)):
    s = scopeio.psd(wide, 1.0 / fs, window_name=name)
    total = np.sum(s.psd) * s.df            # rectangle rule is exact Parseval
    ok(f"Parseval holds for {name}", close(total, wide.var(), rtol=rtol),
       f"sum S df = {total:.6e} vs var {wide.var():.6e} "
       f"({100 * (total / wide.var() - 1):+.3f}%)")

s = scopeio.psd(stack, 1.0 / fs)
ok("PSD is one-sided to Nyquist", close(s.f[-1], fs / 2), f"{s.f[-1]:.4g} Hz")
ok("bin spacing is fs/n", close(s.df, fs / n), f"{s.df:.4g} Hz")
ok("n_avg counts the spectra averaged", s.n_avg == 24)
ok("asd is the root of the psd", close(s.asd, np.sqrt(s.psd)))
ok("rel_err is 1/sqrt(n_indep)", close(s.rel_err, 1 / np.sqrt(24)),
   f"{s.rel_err:.4f}")

# A known tone must land in the right bin and carry the right power.  Put it
# exactly on a bin: 100 kHz is not one at this resolution (409.6 bins), and an
# off-bin tone spreads across neighbours, which is a property of the DFT rather
# than of anything here.
df0 = fs / n
f0 = round(100e3 / df0) * df0
amp = 0.05
t = np.arange(n) / fs
tone = np.sin(2 * np.pi * f0 * t)[None, :] * amp
st = scopeio.psd(tone, 1.0 / fs, window_name="hann", detrend=False)
i = int(np.argmax(st.psd))
ok("an on-bin tone lands in its own bin", close(st.f[i], f0, rtol=1e-9),
   f"{st.f[i]:.6g} Hz")
band = np.sum(st.psd[max(0, i - 6):i + 7]) * st.df
ok("the tone integrates to A^2/2", close(band, amp ** 2 / 2, rtol=0.01),
   f"{band:.6e} vs {amp ** 2 / 2:.6e}")
ok("band_power agrees with the direct sum",
   close(st.band_power(f0 - 6 * st.df, f0 + 6 * st.df), band, rtol=0.05),
   f"{st.band_power(f0 - 6 * st.df, f0 + 6 * st.df):.6e}")

# segmenting: more averages, coarser bins, and overlap must not inflate n_indep
seg = scopeio.psd(stack, 1.0 / fs, nperseg=1024)
ok("nperseg raises the average count", seg.n_avg == 24 * 8, f"{seg.n_avg}")
ok("nperseg coarsens the bins", close(seg.df, fs / 1024))
ok("independent count equals segment count with no overlap",
   close(seg.n_indep, 24 * 8), f"{seg.n_indep}")
ovl = scopeio.psd(stack, 1.0 / fs, nperseg=1024, noverlap=512)
ok("overlapped segments average more spectra", ovl.n_avg > seg.n_avg,
   f"{ovl.n_avg} vs {seg.n_avg}")
ok("but n_indep does NOT count them as independent",
   ovl.n_indep <= seg.n_indep + 1e-9,
   f"n_avg {ovl.n_avg} but n_indep {ovl.n_indep:.1f}")

s1 = scopeio.psd(stack[0], 1.0 / fs)
ok("a single record works as well as a stack", s1.n_avg == 1)

try:
    scopeio.psd(stack, 0.0)
    ok("a zero dt is refused", False)
except ValueError:
    ok("a zero dt is refused", True)
try:
    scopeio.psd(stack, 1 / fs, nperseg=n + 1)
    ok("nperseg longer than the record is refused", False)
except ValueError:
    ok("nperseg longer than the record is refused", True)
try:
    scopeio.window("gaussian", 16)
    ok("an unknown window is refused", False)
except ValueError:
    ok("an unknown window is refused", True)


# --------------------------------------------------- 7. the whole chain
print("\n--- end to end: segments -> RIN -> dI/I ---")

V_DC = 1.85
# Build a known S_V, cut it into three filtered segments, splice, convert.
f_full = np.concatenate([f_a, f_b, f_c])
sp_full = R.splice_segments(segs, [(f_a, h2_a), (f_b, h2_b), None])
rin_f, rin_db = R.rin_from_psd(sp_full.psd, V_DC)
got = R.integrate_rin(rin_f, sp_full.f, (1.0, 100e3))
want = R.integrate_rin(truth(sp_full.f) / V_DC ** 2, sp_full.f, (1.0, 100e3))
ok("dI/I through the whole chain matches the truth",
   close(got, want, rtol=0.02), f"{got:.4e} vs {want:.4e}")
ok("RIN in dBc/Hz is finite across the splice",
   bool(np.all(np.isfinite(rin_db))))

# the 3 dB filter error has to move the integrated answer, not hide in it
rin_bad, _ = R.rin_from_psd(sp_bad.psd, V_DC)
got_bad = R.integrate_rin(rin_bad, sp_bad.f, (1.0, 100e3))
ok("a bad join changes the integrated answer",
   abs(got_bad / got - 1.0) > 0.05,
   f"{got_bad:.4e} vs {got:.4e} ({100*(got_bad/got-1):+.1f}%)")

print(f"\nAll {checks} checks passed.")
