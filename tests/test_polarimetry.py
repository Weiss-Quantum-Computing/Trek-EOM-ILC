"""Synthetic end-to-end check of the polarimetric analysis chain.

No instruments.  The point of this file is to validate the ANALYSIS before any
bench time is spent: build a known phi(t) from the real production target,
push it through the real forward model to a synthetic photodiode trace, add a
known repeatable ripple, a known random component and a known 60 Hz term, and
then confirm the inversion hands all three back.  A sign error or a
slope-normalisation mistake in `invert_linear` would otherwise show up on the
bench looking exactly like physics.

Run with the Anaconda interpreter, like the rest of the suite:
    python tests/test_polarimetry.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eomilc import polarimetry as pol            # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "waveforms", "target_PARX1.csv")

V_PI = 5200.0
N_EOM = 2
checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(msg)
    print(f"  ok  {msg}")


def truth_cal(theta_a_deg=45.0, v_zero=0.0, i_dark=0.0, a=1.0, b=0.98):
    """A FringeCal built from physics rather than from a fit."""
    omega = N_EOM * np.pi / V_PI
    th = np.deg2rad(theta_a_deg)
    return pol.FringeCal(a=a, b=b, omega=omega, psi=omega * v_zero + 2 * th,
                         theta_a=th, n_eom=N_EOM, i_dark=i_dark)


# --------------------------------------------------------------- calibration
print("\n[1] fringe fit recovers the physics it was built from")
cal0 = truth_cal(theta_a_deg=0.0, v_zero=137.0, a=1.0, b=0.97)
v_sweep = np.linspace(-200.0, 5600.0, 4000)
i_sweep = pol.intensity(pol.phi_of_volts(v_sweep, cal0), cal0)
rng = np.random.default_rng(0)
i_noisy = i_sweep + rng.normal(0, 2e-3, i_sweep.shape)

fit = pol.fit_fringe(v_sweep, i_noisy, n_eom=N_EOM, v_pi_guess=5000.0,
                     theta_a=0.0)
ok(abs(fit.v_pi - V_PI) < 5.0, f"v_pi {fit.v_pi:.1f} V recovered (true 5200)")
ok(abs(fit.v_zero - 137.0) < 5.0,
   f"EO zero {fit.v_zero:.1f} V recovered (true 137)")
ok(abs(fit.visibility - 0.97) < 0.01,
   f"visibility {fit.visibility:.4f} recovered (true 0.970)")
ok(abs(fit.sigma_phi - np.sqrt(-np.log(0.97) / 2)) < 1e-3,
   f"sigma_phi {np.rad2deg(fit.sigma_phi):.2f} deg from visibility")

print("\n[2] the visibility -> spread numbers the plan quotes")
for vis, deg in ((0.99, 4.06), (0.98, 5.76)):
    c = pol.FringeCal(a=1.0, b=vis, omega=N_EOM * np.pi / V_PI, psi=0.0,
                      theta_a=0.0, n_eom=N_EOM)
    ok(abs(np.rad2deg(c.sigma_phi) - deg) < 0.02,
       f"visibility {vis} -> {np.rad2deg(c.sigma_phi):.2f} deg (plan says {deg})")

# --------------------------------------------------------------- geometry
print("\n[3] corner sensitivities match the published figure")
cal45 = truth_cal(45.0)
cal00 = truth_cal(0.0)
for name, v_dc, s45, s00 in (("ramp ends", 0.0, 1.000, 0.000),
                             ("hold top", 5200.0, 1.000, 0.000)):
    p = pol.phi_of_volts(v_dc, cal45)
    got45 = float(pol.sensitivity(p, cal45))
    got00 = float(pol.sensitivity(pol.phi_of_volts(v_dc, cal00), cal00))
    ok(abs(got45 - s45) < 1e-6 and abs(got00 - s00) < 1e-6,
       f"{name}: theta_a=45 -> {got45:.3f}, theta_a=0 -> {got00:.3f}")

print("\n[4] absolute inversion round-trips over a full fringe")
phi_true = pol.phi_of_volts(v_sweep, cal45)
phi_back = pol.invert_absolute(pol.intensity(phi_true, cal45), cal45, phi_true)
ok(np.max(np.abs(phi_back - phi_true)) < 1e-9,
   f"max |phi_back - phi| = {np.max(np.abs(phi_back - phi_true)):.2e} rad")

# --------------------------------------------------------------- end to end
print("\n[5] end-to-end: inject a known residual, recover it")
d = np.loadtxt(TARGET, delimiter=",", comments="#", skiprows=5)
t = d[:, 0] * 1e-6
v_target = d[:, 1]
dt = float(np.median(np.diff(t)))
n = v_target.size
ok(abs(dt - 2e-6) < 1e-12 and n == 5501, f"target loaded: {n} pts at {dt*1e6:g} us")

RIPPLE_V = 0.195 * np.sqrt(2)      # rms drive error, both channels, uncorrelated
RANDOM_V = 0.08
HUM_V = 0.311                      # the X2 summer's 60 Hz, in HV volts
N_SHOTS = 64

# repeatable component: drive-locked, lives only where the target moves
moving = np.abs(np.gradient(v_target, dt)) > 1.0
ripple = np.sin(2 * np.pi * 24e3 * t) * moving
ripple *= RIPPLE_V / np.sqrt(np.mean(ripple[moving] ** 2))

stack_pd, stack_mon = [], []
rng = np.random.default_rng(42)
for k in range(N_SHOTS):
    rand = rng.normal(0, RANDOM_V, n)
    hum = HUM_V * np.sqrt(2) * np.sin(2 * np.pi * 60 * t + 0.7)   # phase-locked
    v_true = v_target + ripple + rand + hum
    stack_mon.append(v_true)
    stack_pd.append(pol.intensity(pol.phi_of_volts(v_true, cal45), cal45))
stack_pd = np.asarray(stack_pd)
stack_mon = np.asarray(stack_mon)

ens_pd = pol.ensemble(stack_pd)
dphi, dv, good = pol.invert_linear(ens_pd.mean, v_target, cal45)
ok(good.mean() > 0.75, f"coverage {good.mean()*100:.1f}% at theta_a = 45 deg")

rep_rms = float(np.sqrt(np.mean(dv[good] ** 2)))
expect = float(np.sqrt(np.mean((ripple + hum)[good] ** 2)))
ok(abs(rep_rms - expect) / expect < 0.03,
   f"repeatable residual {rep_rms:.4f} V vs injected {expect:.4f} V "
   f"({100*(rep_rms/expect - 1):+.1f}%)")

# the random part must show up in the shot-to-shot spread, not the mean
dphi_s, dv_s, _ = pol.invert_linear(ens_pd.mean + ens_pd.std, v_target, cal45)
rand_rms = float(np.sqrt(np.mean((dv_s - dv)[good] ** 2)))
ok(abs(rand_rms - RANDOM_V) / RANDOM_V < 0.05,
   f"random component {rand_rms:.4f} V vs injected {RANDOM_V:.4f} V "
   f"({100*(rand_rms/RANDOM_V - 1):+.1f}%)")

print("\n[6] the masked windows are where the plan says they are")
blind = ~good
edges = np.flatnonzero(np.diff(blind.astype(int)) != 0)
wins = [(t[edges[i]] * 1e3, t[edges[i + 1]] * 1e3)
        for i in range(0, len(edges) - 1, 2)]
print(f"      masked: {', '.join(f'{a:.2f}-{b:.2f} ms' for a, b in wins)}")
ok(all(not (4.6 < a < 6.4) for a, b in wins),
   "no masked window inside the hold at theta_a = 45 deg")

print("\n[7] ensemble split and coherence")
ok(ens_pd.n == N_SHOTS, f"n = {ens_pd.n}")
frac = pol.ensemble(np.asarray(stack_mon)).repeatable_fraction(v_target)
ok(0.85 < frac < 1.0, f"repeatable fraction {frac:.3f} (ripple+hum >> noise)")

band_of = lambda f: (f > 1e3) & (f < 60e3)

# THE TRAP: across a ramp the analyser slope sweeps through zero, so the raw
# PD is the monitor times a gain that varies ~1e4 within one record.  That is
# a multiplication in time, i.e. a convolution in frequency, and it destroys
# coherence between channels that are in fact perfectly related.
f, g_raw = pol.coherence_ensemble(stack_mon, stack_pd, dt)
floor = pol.coherence_floor(N_SHOTS)
ok(np.median(g_raw[band_of(f)]) < 10 * floor,
   f"raw monitor vs raw PD collapses to {np.median(g_raw[band_of(f)]):.3f} "
   f"(1/N floor {floor:.4f}) -- the time-varying-slope trap")

# invert to equivalent volts first and the relationship comes back
dv_stack, ok_mask = pol.invert_stack(stack_pd, v_target, cal45)
f, g2 = pol.coherence_ensemble(stack_mon, dv_stack, dt)
ok(np.median(g2[band_of(f)]) > 0.7,
   f"monitor vs INVERTED PD gives {np.median(g2[band_of(f)]):.3f}")

indep = rng.normal(0, 1.0, stack_mon.shape)
f2, g2i = pol.coherence_ensemble(stack_mon, indep, dt)
ok(np.median(g2i) < 4 * floor,
   f"independent channels give {np.median(g2i):.4f}, floor 1/N = {floor:.4f}")

print("\n[8] band_rms obeys Parseval")
y = rng.normal(0, 0.3, 4096)
tot = np.sqrt(sum(v ** 2 for v in
                  pol.band_rms(y, 1e-6, ((0, 1e12),)).values()))
ok(abs(tot - y.std()) / y.std() < 1e-9,
   f"full-band rms {tot:.6f} == std {y.std():.6f}")

parts = pol.band_rms(y, 1e-6, ((0, 100e3), (100e3, 500e3)))
split = float(np.hypot(*parts.values()))
ok(abs(split - y.std()) / y.std() < 1e-9,
   f"two bands recombine in quadrature to {split:.6f} == std {y.std():.6f}")

print("\n[9] the ramped probe: reach the offset, hold, leave")
from tools import sysid_make as sm                      # noqa: E402
from eomilc.config import CHANNELS, HV_PER_MON, LIMITS  # noqa: E402

N_HOLD = 1000
pbins = sm.hold_tone_bins(1e3, 190e3, 48, N_HOLD, dt)
u, (i0, i1) = sm.ramped_multitone(peak=0.5, v_dc=4.0, bins=pbins,
                                  n_hold=N_HOLD, dt=dt)
ok(abs(u[0]) <= 0.1 and abs(u[-1]) <= 0.1,
   f"record starts {u[0]:+.4f} V and ends {u[-1]:+.4f} V, inside the "
   f"+/-100 mV end clamp")
ok(i1 - i0 == N_HOLD and abs((i1 - i0) * dt - 2e-3) < 1e-9,
   f"analysis window {(i1-i0)*dt*1e3:.2f} ms -> {1/((i1-i0)*dt):.0f} Hz resolution")
ok(len(u) <= 16384, f"record {len(u)} pts, inside AWG_MAX_PTS 16384")

w = u[i0:i1] - u[i0:i1].mean()
U = np.abs(np.fft.rfft(w)) ** 2
in_bins = np.zeros(U.size, bool)
in_bins[pbins] = True
ok(U[in_bins].sum() / U.sum() > 0.97,
   f"{100*U[in_bins].sum()/U.sum():.1f}% of hold energy sits on the tone bins")

# the bins must be integer over the HOLD, not the record -- reusing the
# whole-record bins is the trap this check exists to catch
rec_bins = sm.tone_bins(1e3, 190e3, 48, n=len(u), dt=dt)
ok(not np.array_equal(np.asarray(rec_bins), np.asarray(pbins)),
   "hold_tone_bins differs from whole-record tone_bins, as it must")

print("\n[10] rail headroom at the five offsets")
g = CHANNELS["EO1"].gain(5.0)
rows = [(hv, hv / HV_PER_MON / g) for hv in (0, 1300, 2600, 3900, 4900, 5200)]
for hv, awg in rows:
    print(f"      {hv:5.0f} V EOM -> {awg:6.3f} V AWG, "
          f"{100*(1-(awg+0.5)/LIMITS.awg_rail):5.1f}% rail left with a 0.5 V probe")
top = dict(rows)[5200]
ok(top + 0.5 < LIMITS.awg_rail,
   f"5.2 kV + probe = {top+0.5:.3f} V fits the {LIMITS.awg_rail:g} V rail, "
   f"but only just")
ok(dict(rows)[4900] + 0.5 < 0.94 * LIMITS.awg_rail,
   "4.9 kV leaves the >5% rail margin the plan recommends")
try:
    sm.ramped_multitone(peak=0.5, v_dc=9.8, bins=pbins, n_hold=N_HOLD, dt=dt)
    raise AssertionError("a probe past the rail should have been refused")
except ValueError as e:
    ok("rail" in str(e), f"a probe past the rail is refused: {e}")

print("\n[11] round-trip through save/load")
tmp = os.path.join(ROOT, "run", "_test_fringe_cal.json")
cal45.save(tmp)
back = pol.FringeCal.load(tmp)
os.remove(tmp)
ok(abs(back.v_pi - cal45.v_pi) < 1e-9 and abs(back.theta_a - cal45.theta_a) < 1e-12,
   "FringeCal survives json round-trip")

print(f"\nall {checks} checks passed\n")
