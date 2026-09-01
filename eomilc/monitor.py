"""Monitor-to-crystal correction: what the light does that the Trek monitor does not.

Every ILC campaign is optimised against the Trek monitor.  Measured optically on
1 Sep 2026, the monitor is a faithful proxy below ~10 kHz and is NOT above 20:
the light lags it by about 10 degrees and sits 0.5-1.8 dB below it across the
20-85 kHz band.  A monitor-referenced loop cannot see either, so it will drive
its own error to zero with that difference still there.

    from eomilc import monitor
    Hc = monitor.response(freqs)          # crystal / monitor, complex
    v_crystal = monitor.apply(v_monitor, dt)

WHAT THIS IS AND IS NOT.  It is a TABLE of what was measured, not a fitted
model, because no simple model fits the data: a 90 kHz pole matches the
magnitude and leaves 8 degrees of phase, a 0.30 us delay matches the phase and
leaves 0.7 dB, and fitting both together collapses to zero delay and beats
neither.  The mechanism is not identified.  Series resistance to the crystal,
transit delay, and an unmodelled detection-path term all remain open.  Do not
replace this table with a pole until someone measures at the EOM end.

PROVENANCE.  Six independent 64-shot runs on EO1: bias 1.28/2.56/3.85/4.88 kV,
drive 175/350/700 V peak, and two analyser angles.  The reference is the
geometric mean of a photodiode/monitor SCOPE-CHANNEL SWAP, which cancels
channel-gain mismatch and any inter-channel delay exactly; the other five agree
with it to 0.16 dB and 0.4 degrees in the mean.  The detection chain's own
low-pass was measured electrically (two sections, RC 1.04 us, f_-3dB 57 kHz)
and divided out -- inferring it from the optical data instead is circular and
gave a 40 kHz corner that over-corrected by 1.2 dB at 40 kHz.

The correction does NOT vary with operating point: across bias 1.28-4.88 kV
(phi = 22.6 to 86 degrees) the spread is 1.16 degrees rms against a 10 degree
effect, so one correction serves the whole ramp.  Nor with drive amplitude:
700 V minus 175 V is +0.05 dB and -0.16 degrees over a 16x span in the fringe's
third-order term.
"""
import numpy as np

# (frequency Hz, |crystal/monitor|, phase deg, sd of the six runs in dB, sd in deg)
TABLE = (
    (   428.45,  1.01398,   +0.198,  0.059,  0.72),
    (   714.08,  0.99257,   -0.343,  0.036,  0.39),
    (   999.71,  0.98225,   -0.263,  0.059,  0.52),
    (  1285.35,  0.98651,   -0.085,  0.025,  0.23),
    (  1570.98,  0.99195,   -0.263,  0.025,  0.24),
    (  1856.61,  0.99788,   -0.157,  0.061,  0.20),
    (  2142.25,  1.00295,   -0.094,  0.039,  0.17),
    (  2427.88,  1.00459,   +0.207,  0.081,  0.26),
    (  2713.51,  1.01559,   -0.199,  0.066,  0.18),
    (  2999.14,  1.01189,   -0.125,  0.024,  0.32),
    (  3284.78,  1.01211,   -0.359,  0.038,  0.50),
    (  3570.41,  1.01428,   -1.158,  0.054,  0.34),
    (  3856.04,  1.01026,   -1.082,  0.049,  0.57),
    (  4141.67,  1.02034,   -0.932,  0.123,  0.68),
    (  4712.94,  1.01656,   -1.984,  0.071,  0.57),
    (  5284.20,  1.01494,   -2.325,  0.095,  0.31),
    (  5855.47,  1.01318,   -2.338,  0.063,  0.36),
    (  6426.74,  1.00766,   -4.029,  0.109,  0.80),
    (  6998.00,  1.04573,   -3.027,  0.211,  0.33),
    (  7569.27,  1.00930,   -3.341,  0.045,  0.49),
    (  8426.16,  1.00908,   -4.490,  0.151,  0.46),
    (  9283.06,  1.00651,   -4.033,  0.100,  0.64),
    ( 10425.59,  1.02957,   -4.215,  0.166,  0.66),
    ( 11282.49,  1.01175,   -4.848,  0.251,  0.52),
    ( 12710.65,  0.99732,   -6.507,  0.155,  1.58),
    ( 13853.18,  1.00147,   -7.672,  0.180,  0.91),
    ( 15281.35,  0.99663,   -8.381,  0.096,  1.76),
    ( 16995.14,  0.94983,   -8.026,  0.277,  1.21),
    ( 18708.94,  0.96776,   -6.170,  0.217,  2.41),
    ( 20708.37,  0.92548,  -10.425,  0.259,  0.69),
    ( 22707.80,  0.90959,   -7.945,  0.332,  0.87),
    ( 25278.49,  0.98580,   -7.320,  0.430,  3.14),
    ( 27849.19,  0.90223,  -14.988,  0.253,  4.10),
    ( 30705.51,  0.90996,  -10.259,  0.204,  0.99),
    ( 33847.47,  0.90581,  -10.972,  0.510,  1.25),
    ( 37560.70,  0.93775,   -8.238,  0.115,  3.05),
    ( 41273.92,  0.87574,  -11.625,  0.393,  1.29),
    ( 45558.41,  0.91914,  -12.756,  0.895,  2.51),
    ( 50414.17,  0.86473,   -9.979,  0.318,  3.81),
    ( 55555.56,  0.93741,   -9.149,  1.410,  1.83),
    ( 61553.84,  0.83223,  -13.863,  0.972,  1.54),
    ( 67837.76,  0.80331,   -7.768,  0.579,  4.93),
    ( 74978.58,  0.79633,  -14.620,  0.820,  5.38),
    ( 82690.66,  0.70729,  -16.649,  0.954,  6.93),
)

F_HZ = np.array([r[0] for r in TABLE])
_MAG_DB = 20.0 * np.log10(np.array([r[1] for r in TABLE]))
_PH_DEG = np.array([r[2] for r in TABLE])
_SD_DB = np.array([r[3] for r in TABLE])
_SD_DEG = np.array([r[4] for r in TABLE])

F_MIN, F_MAX = float(F_HZ[0]), float(F_HZ[-1])


def response(f, outside="raise"):
    """Crystal volts per monitor volt, complex, at frequencies `f`.

    Interpolated in log-frequency on dB and degrees, which is how the curve was
    read and keeps the interpolation smooth where the points are sparse.

    Below F_MIN the correction is returned as exactly 1: the measurement says
    it is 1.014 +- 0.06 dB and 0.2 +- 0.7 deg at the bottom of the band, i.e.
    unity to well inside its own scatter, and the physics gives no reason for
    structure below that.  ABOVE F_MAX nothing was measured -- the detection
    filter takes 6-8 dB out of the photodiode while the monitor is unfiltered,
    so optical SNR collapses.  `outside` decides what happens there:
    "raise" (default), "hold" (freeze at the last point, and say so), or
    "unity".  Never silently extrapolate a curve whose shape is not understood.
    """
    f = np.atleast_1d(np.asarray(f, float))
    hi = f > F_MAX
    if hi.any():
        if outside == "raise":
            raise ValueError(
                f"{int(hi.sum())} of {f.size} frequencies are above {F_MAX/1e3:.1f} kHz, "
                f"where this correction was never measured (the anti-alias filter "
                f"kills optical SNR there). Pass outside='hold' or 'unity' if you "
                f"have decided how to treat that band.")
        if outside not in ("hold", "unity"):
            raise ValueError(f"outside must be 'raise', 'hold' or 'unity', got {outside!r}")
    lf = np.log(np.clip(f, F_MIN, F_MAX))
    db = np.interp(lf, np.log(F_HZ), _MAG_DB)
    ph = np.interp(lf, np.log(F_HZ), _PH_DEG)
    out = 10.0 ** (db / 20.0) * np.exp(1j * np.radians(ph))
    out[f < F_MIN] = 1.0
    if hi.any() and outside == "unity":
        out[hi] = 1.0
    return out


def uncertainty(f):
    """(dB, degrees) spread across the six runs, interpolated the same way."""
    lf = np.log(np.clip(np.atleast_1d(np.asarray(f, float)), F_MIN, F_MAX))
    return (np.interp(lf, np.log(F_HZ), _SD_DB),
            np.interp(lf, np.log(F_HZ), _SD_DEG))


def apply(v_monitor, dt, outside="hold"):
    """Monitor volts -> crystal volts, for a real time series.

    Defaults to outside="hold" rather than "raise": a record always contains
    frequencies above the measured band, and refusing to filter a whole trace
    because of its noise floor would be useless.  Holding the last measured
    value there is a choice, not a measurement -- if the content above 83 kHz
    matters to your result, this correction cannot tell you about it.
    """
    x = np.asarray(v_monitor, float)
    n = x.shape[-1]
    freqs = np.fft.rfftfreq(n, dt)
    h = response(freqs, outside=outside)
    return np.fft.irfft(np.fft.rfft(x, axis=-1) * h, n=n, axis=-1)


def summary(bands=((0.0, 3e3), (3e3, 10e3), (10e3, 20e3), (20e3, 30e3),
                   (30e3, 55e3), (55e3, 85e3))):
    """Band-averaged correction, the way the campaign document reports it."""
    out = []
    for lo, hi in bands:
        m = (F_HZ >= lo) & (F_HZ < hi)
        if not m.any():
            continue
        out.append((lo, hi, int(m.sum()), float(_MAG_DB[m].mean()),
                    float(_PH_DEG[m].mean())))
    return out
