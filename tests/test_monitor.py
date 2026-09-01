"""Checks on the monitor-to-crystal correction.

The table is a measurement, so these do not re-derive its values -- they guard
the things that would silently corrupt a result: the extrapolation policy, the
round trip, and the sign of the phase.  A correction applied with the wrong
sign doubles the error it was meant to remove, and nothing downstream would
complain.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eomilc import monitor as m            # noqa: E402


def _check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    return bool(cond)


def main():
    ok = []

    # --- the table itself
    ok.append(_check("table is ordered in frequency",
                     np.all(np.diff(m.F_HZ) > 0)))
    ok.append(_check("no bin is more than a factor 2 from its neighbour",
                     np.all(np.diff(m.F_HZ) / m.F_HZ[:-1] < 1.0),
                     f"max step {np.max(np.diff(m.F_HZ)/m.F_HZ[:-1]):.2f}"))
    ok.append(_check("every quoted uncertainty is positive",
                     np.all(m._SD_DB >= 0) and np.all(m._SD_DEG >= 0)))

    # --- the correction is a LAG, not a lead.  Getting this backwards is the
    #     one error that would make things worse rather than better.
    hi = m.response(np.array([60e3]))[0]
    ok.append(_check("phase is negative in band (the light LAGS the monitor)",
                     np.angle(hi) < 0, f"{np.degrees(np.angle(hi)):+.1f} deg at 60 kHz"))
    ok.append(_check("magnitude droops in band (the monitor OVER-reports)",
                     abs(hi) < 1.0, f"|H| = {abs(hi):.3f} at 60 kHz"))

    # --- below the measured band the correction is exactly unity, not
    #     extrapolated
    lo = m.response(np.array([1.0, 50.0, 400.0]))
    ok.append(_check("below F_MIN the correction is exactly 1",
                     np.allclose(lo, 1.0)))

    # --- above it, nothing is silently invented
    raised = False
    try:
        m.response(np.array([150e3]))
    except ValueError:
        raised = True
    ok.append(_check("above F_MAX the default refuses", raised))
    ok.append(_check("outside='unity' gives 1 above the band",
                     np.allclose(m.response(np.array([150e3]), outside="unity"), 1.0)))
    ok.append(_check("outside='hold' freezes at the last measured point",
                     np.isclose(m.response(np.array([150e3]), outside="hold")[0],
                                m.response(np.array([m.F_MAX]))[0])))
    bad = False
    try:
        m.response(np.array([150e3]), outside="nonsense")
    except ValueError:
        bad = True
    ok.append(_check("an unknown outside= policy is rejected", bad))

    # --- apply(): a real signal in, a real signal out, and DC untouched
    dt = 2e-6
    n = 3501
    t = np.arange(n) * dt
    x = 1.0 + 0.1 * np.sin(2 * np.pi * 5e3 * t)
    y = m.apply(x, dt)
    ok.append(_check("apply returns the same shape", y.shape == x.shape))
    ok.append(_check("apply leaves the mean alone (correction is 1 at DC)",
                     abs(y.mean() - x.mean()) < 1e-9,
                     f"{y.mean()-x.mean():+.2e}"))

    # a pure in-band tone should come out scaled and delayed by the table
    f0 = 25e3
    x = np.sin(2 * np.pi * f0 * np.arange(n) * dt)
    y = m.apply(x, dt)
    h = m.response(np.array([f0]))[0]
    # compare against the analytic answer away from the record edges
    want = abs(h) * np.sin(2 * np.pi * f0 * np.arange(n) * dt + np.angle(h))
    err = np.abs(y[200:-200] - want[200:-200]).max()
    ok.append(_check("a 25 kHz tone comes out with the table's gain and phase",
                     err < 0.02, f"max error {err:.4f}"))

    # --- applying the correction and then undoing it returns the original
    freqs = np.fft.rfftfreq(n, dt)
    h_all = m.response(freqs, outside="hold")
    back = np.fft.irfft(np.fft.rfft(m.apply(x, dt)) / h_all, n=n)
    ok.append(_check("correct then un-correct is the identity",
                     np.abs(back - x).max() < 1e-9,
                     f"max error {np.abs(back-x).max():.2e}"))

    # --- the band summary matches the table it is drawn from
    s = m.summary()
    lo_, hi_, cnt, db, deg = s[0]
    sel = (m.F_HZ >= lo_) & (m.F_HZ < hi_)
    ok.append(_check("summary() averages the same rows",
                     cnt == int(sel.sum()) and
                     np.isclose(deg, m._PH_DEG[sel].mean())))

    print(f"\n{sum(ok)}/{len(ok)} checks passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
