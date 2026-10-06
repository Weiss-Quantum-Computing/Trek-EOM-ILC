"""Learned target corrections and the line-ripple fit, without instruments.

eomilc.corrections (the file format, the gate that keeps noise, quantization
and out-of-band content out of a target, the feasibility check against the
Trek limits) and eomilc.mains (the line-ripple fit and Loop.line), checked on
synthetic signals whose answers are known:

    C:\\ProgramData\\anaconda3\\python.exe tests\\test_corrections.py
"""
import os
import sys
import tempfile

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from eomilc import corrections as C, mains, ilc, plant as plantmod  # noqa: E402
from eomilc.config import CHANNELS  # noqa: E402
import run_ilc  # noqa: E402

checks = 0


def ok(label, cond, detail=""):
    global checks
    if not cond:
        raise AssertionError(f"{label}: {detail}")
    checks += 1
    print(f"[ok] {label}" + (f" -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="eomilc-corr-")
dt = 2e-6
t = np.arange(5501) * dt
ch = CHANNELS["EO1"]
# a ramp-hold-return base target, V at the EOM
base_out = 5000 * np.clip(np.minimum((t - 0.5e-3) / 4e-3, (10.5e-3 - t) / 4e-3), 0, 1)
base_out = C.lowpass(base_out, dt, 5e3)
base_out[[0, -1]] = 0.0
rng = np.random.default_rng(7)

# ---- 1. file round trip, and a target CSV is refused --------------------------
t_f = np.arange(0, t[-1] + 1e-9, 0.5e-6)                 # the polarimeter's grid
smooth = 40 * np.sin(2 * np.pi * 700 * t_f) * (base_out.max() > 0)
sig_f = np.full_like(t_f, 20.0)                           # V per 0.5 us sample
noisy = smooth + rng.normal(0, 20.0, t_f.size)
meta = dict(channel="EO1", slot="optical", band_hz=2000.0, line_removed=True,
            quantity="test", **C.target_fingerprint(base_out, dt))
p = C.write_correction(os.path.join(TMP, "corr.csv"), t_f, noisy, sig_f, meta)
c = C.read_correction(p)
ok("round trip", np.allclose(c.delta, noisy, rtol=1e-8, atol=1e-6)
   and c.meta["band_hz"] == 2000.0 and c.meta["line_removed"] is True
   and c.slot == "optical", f"{len(c.t)} pts, meta keys {sorted(c.meta)}")
tp = os.path.join(TMP, "target.csv")
with open(tp, "w") as fh:
    fh.write("# a target\ntime_us,voltage_V\n0,0\n2,1\n")
try:
    C.read_correction(tp)
    ok("target CSV refused", False, "read without complaint")
except ValueError as e:
    ok("target CSV refused", "not a target correction" in str(e), str(e)[:60])

# ---- 2. the gate keeps the signal, drops the noise, stays in band -------------
st = C.GateSettings(band_hz=2000, k_sigma=3, floor_out=0.1, fade_s=200e-6,
                    active_only=False)
kept, info = C.gate(c, t, base_out, st)
truth = np.interp(t, t_f, smooth)
inner = (t > 1e-3) & (t < 10e-3)
err = np.sqrt(np.mean((kept - truth)[inner] ** 2))
ok("signal kept through noise", err < 3.0,
   f"rms error {err:.2f} V on a 40 V, 700 Hz correction under 20 V/sample "
   f"noise; filtered sigma {info['sigma_lp_median']:.2f} V")
ok("ends exactly zero", kept[0] == 0.0 and kept[-1] == 0.0)
F = np.abs(np.fft.rfft(kept))
f = np.fft.rfftfreq(len(kept), dt)
ok("nothing above the band", F[f > 2500].max() < 0.01 * F.max(),
   f"max above 2.5 kHz {F[f > 2500].max() / F.max():.2e} of the peak")

c0 = C.Correction(t=t_f, delta=rng.normal(0, 20.0, t_f.size), sigma=sig_f,
                  meta=dict(meta))
k0, i0 = C.gate(c0, t, base_out, st)
ok("pure noise is held at zero", np.max(np.abs(k0)) < 1.0,
   f"peak {np.max(np.abs(k0)):.3f} V from 20 V/sample noise "
   f"(resolved {100*i0['resolved_frac']:.1f}%)")

tiny = C.Correction(t=t, delta=0.05 * np.sin(2 * np.pi * 500 * t), sigma=None,
                    meta=dict(meta))
kt, it_ = C.gate(tiny, t, base_out, st)
ok("under the floor is held", np.max(np.abs(kt)) < 1e-3,
   f"0.05 V correction, floor 0.1 V -> {np.max(np.abs(kt)):.2e} V; "
   f"no-sigma note: {any('no sigma' in n for _, n in it_['notes'])}")

st_act = C.GateSettings(band_hz=2000, k_sigma=3, floor_out=0.1, active_only=True)
flat = C.Correction(t=t, delta=np.full_like(t, 30.0), sigma=None, meta=dict(meta))
ka, _ = C.gate(flat, t, base_out, st_act)
idle = base_out < 1e-3 * base_out.max()
ok("idle stretches untouched", np.max(np.abs(ka[idle])) < 0.5,
   f"30 V everywhere -> {np.max(np.abs(ka[idle])):.3f} V where the target idles")
nl = C.Correction(t=t, delta=np.zeros_like(t), sigma=None,
                  meta={k: v for k, v in meta.items() if k != "line_removed"})
_, inl = C.gate(nl, t, base_out, st)
ok("line ripple not removed -> warned",
   any("line (mains)" in n for _, n in inl["notes"]))
kg, _ = C.gate(c, t, base_out, st, gain=0.5)
ok("gain scales", np.allclose(kg, 0.5 * kept, atol=1e-9))

# ---- 3. the target it was measured against -------------------------------------
ok("same target passes", not [x for x in C.check_target(meta, base_out, dt)
                              if x[0] == "fail"])
bad = C.check_target(meta, base_out * 1.01, dt)
ok("another target fails", any(l == "fail" for l, _ in bad), bad[0][1][:70])
bad2 = C.check_target(meta, base_out[:-1], dt)
ok("another grid fails", any(l == "fail" for l, _ in bad2))

# ---- 4. feasibility against the Trek chain --------------------------------------
g = 0.5594
u = base_out / ch.mon_scale / g
good = C.check_feasible(kept, base_out, u, dt, ch, ch.limits, g, 10.0,
                        learn_band_hz=50e3, band_hz=2000, cap_out=300)
ok("a small correction is feasible", not [x for x in good if x[0] == "fail"],
   [x[1][:60] for x in good][0])
big = np.full_like(t, 2000.0) * (base_out > 0)
r = C.check_feasible(big, base_out, u, dt, ch, ch.limits, g, 10.0, 50e3, 2000,
                     cap_out=5000)
ok("past the AWG rail / full scale fails",
   sum(l == "fail" for l, _ in r) >= 1, "; ".join(x[1][:50] for x in r if x[0] == "fail"))
r = C.check_feasible(kept, base_out, u, dt, ch, ch.limits, g, 10.0, 50e3, 2000,
                     cap_out=10)
ok("over the cap fails", any("cap" in x[1] for x in r if x[0] == "fail"))
r = C.check_feasible(kept, base_out, u, dt, ch, ch.limits, g, 10.0, 1000, 2000,
                     cap_out=300)
ok("band above the loop's fails", any("band" in x[1] for x in r if x[0] == "fail"))

# ---- 5. line ripple: fit, and the loop blind to it --------------------------------
tl = np.arange(-10e-3, 90e-3, 5e-6)
amp, ph = [0.9e-3, 0.3e-3, 0.3e-3], [44.0, -48.0, -151.0]


def ripple(tt):
    return sum(a * np.cos(2 * np.pi * 60 * (h + 1) * tt + np.deg2rad(p_))
               for h, (a, p_) in enumerate(zip(amp, ph)))


y = ripple(tl) + 2e-3 + 1e-3 * tl + rng.normal(0, 0.4e-3, tl.size)
fit = mains.fit_line(tl, y, 60, 3)
ok("line fit amplitudes", np.allclose(fit["amp"], amp, rtol=0.05),
   mains.describe(fit, 1e3, "mV"))
ok("line fit phases", np.allclose(fit["phase_deg"], ph, atol=3))
ok("line wave reproduces the ripple",
   np.sqrt(np.mean((mains.line_wave(t, fit) - ripple(t)) ** 2)) < 0.03e-3)
rec = mains.from_record(mains.to_record(fit))
ok("record round trip", np.allclose(rec["a"], fit["a"]))

p_ = plantmod.Plant(gain=g, dt=dt)
tgt = base_out / ch.mon_scale
loop = ilc.Loop(plant=p_, target=tgt, dt=dt, channel=ch, gamma=0.6,
                f_cut=20e3, limits=ch.limits, line=mains.line_wave(t, fit))
u0 = tgt / g
y_meas = tgt + ripple(t)                 # perfect tracking, plus the ripple
u1 = loop.update(u0, y_meas)
# the reference: the same loop, no ripple anywhere (the Q filter alone moves
# the drive a little; that is not what is being tested)
loop0 = ilc.Loop(plant=p_, target=tgt, dt=dt, channel=ch, gamma=0.6,
                 f_cut=20e3, limits=ch.limits)
u_ref = loop0.update(u0, tgt)
ok("loop blind to the measured ripple", np.max(np.abs(u1 - u_ref)) < 2e-5,
   f"update differs from the ripple-free one by "
   f"{np.max(np.abs(u1 - u_ref))*1e3:.4f} mV with the ripple subtracted")
ok("metrics see the line-free error", loop.history[-1]["peak_err_mon"] < 3e-5)
loop2 = ilc.Loop(plant=p_, target=tgt, dt=dt, channel=ch, gamma=0.6,
                 f_cut=20e3, limits=ch.limits)
u2 = loop2.update(u0, y_meas)
ok("without it the loop learns the ripple", np.max(np.abs(u2 - u_ref)) > 5e-4,
   f"update differs by {np.max(np.abs(u2 - u_ref))*1e3:.3f} mV")

# ---- 6. the CLIs carry the panel's keys ---------------------------------------------
st_ = {"t": t, "line_ref": mains.line_wave(t, fit), "base_target": tgt,
       "corrections": "[]", "other": 1}
ok("passthrough keys", sorted(run_ilc.passthrough(st_)) ==
   ["base_target", "corrections", "line_ref"])
ok("state_line on the grid", run_ilc.state_line(st_) is not None and
   run_ilc.state_line({"t": t, "line_ref": np.zeros(10)}) is None)

print(f"\nALL {checks} CHECKS PASSED")
