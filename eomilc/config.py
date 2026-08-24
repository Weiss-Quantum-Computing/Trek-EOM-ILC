"""Measured calibration constants for the two EO drive chains.

All numbers here come from the 2026-08-20/21 sweeps:
  * divider sweep   : "AWG MKJ ramps before and after conditioning *V ampl"  (AWG -> Trek input)
  * monitor sweep   : "AWG MKJ ramps vs trek monitor *V amp"                 (AWG -> Trek monitor)
  * noise           : "... 0V ampl" and "... 0V ampl 10x time"
  * resonance       : analysis/results2.json, kind="mon" (2nd-order fits)

The dynamics are SECOND ORDER (zeta ~ 0.21, fn 2.2-3.0 kHz), and fn falls with
drive amplitude because the EOM capacitance is voltage dependent.  Seed the model
with fn/zeta, not tau -- see the note at the top of plant.py for why the one-pole
version makes the ILC loop diverge.

Convention used throughout the package
--------------------------------------
    awg      volts at the AWG / NI card output   (before the conditioning network)
    trek_in  volts at the Trek input terminal    (after the divider or summer)
    mon      volts at the Trek voltage monitor
    hv       volts at the EOM                    ( = mon * MON_PER_KV_INV, i.e. 1 V -> 1000 V )

The monitor is nominally 1 V per kV, so `hv = 1000 * mon`.  For EO1 that
identity is exactly what is in question -- see AMP_MON_PRODUCT below.
"""
from dataclasses import dataclass, field
import numpy as np

HV_PER_MON = 1000.0          # nominal monitor scale, V of output per V of monitor


@dataclass
class Channel:
    name: str
    divider: float           # AWG -> Trek input, measured, amplitude independent
    divider_tol: float       # 1-sigma spread across the amplitude sweep
    amp_mon_product: float   # Trek input -> monitor.  1.000 means gain 1000 + monitor 1000:1
    # gain (AWG -> monitor) and tau vs monitor amplitude, from the monitor sweep
    amp_pts: np.ndarray = field(repr=False)      # monitor amplitude, V
    gain_pts: np.ndarray = field(repr=False)     # AWG -> monitor
    tau_pts: np.ndarray = field(repr=False)      # seconds, one-pole fit
    fn_pts: np.ndarray = field(repr=False)       # Hz, 2nd-order fit
    zeta_pts: np.ndarray = field(repr=False)     # damping ratio, 2nd-order fit
    noise_trek_in_rms: float = 0.0               # V rms at the Trek input, undriven
    has_fine_channel: bool = False               # coarse+fine summer present?
    fine_ratio: float = 100.0                    # coarse:fine attenuation

    def gain(self, amplitude_mon: float) -> float:
        """AWG -> monitor gain at a given monitor amplitude (mild compression)."""
        return float(np.interp(amplitude_mon, self.amp_pts, self.gain_pts))

    def tau(self, amplitude_mon: float) -> float:
        """One-pole group delay (s) at a given monitor amplitude.

        Kept for comparison only.  It equals 2*zeta/wn of the second-order fit to
        within 1.5% across the whole sweep -- which is the point: the one-pole
        model captures the lag and nothing about the ring.
        """
        return float(np.interp(amplitude_mon, self.amp_pts, self.tau_pts))

    def fn(self, amplitude_mon: float) -> float:
        """Resonant frequency (Hz).  Falls with drive -- the EOM capacitance is
        voltage dependent, and it is the only real nonlinearity in the set."""
        return float(np.interp(amplitude_mon, self.amp_pts, self.fn_pts))

    def zeta(self, amplitude_mon: float) -> float:
        """Damping ratio at a given monitor amplitude."""
        return float(np.interp(amplitude_mon, self.amp_pts, self.zeta_pts))

    def f3db(self, amplitude_mon: float) -> float:
        return 1.0 / (2 * np.pi * self.tau(amplitude_mon))

    def plant(self, amplitude_mon: float, dt: float, model: str = "resonant"):
        """Build the Plant for this channel at the amplitude actually in use.

        Use this rather than constructing Plant by hand.  `tau` and `fn` describe
        the same lag, so a Plant carrying both applies it twice; going through
        here makes that impossible to write by accident.
        """
        from .plant import Plant
        if model == "resonant":
            return Plant(gain=self.gain(amplitude_mon), dt=dt,
                         fn=self.fn(amplitude_mon), zeta=self.zeta(amplitude_mon))
        if model == "one_pole":
            return Plant(gain=self.gain(amplitude_mon), tau=self.tau(amplitude_mon), dt=dt)
        raise ValueError(f"no calibration table for model {model!r}; "
                         f"fit one with plant.identify()")


_AMP = np.array([0.557, 1.113, 2.236, 3.344, 4.470, 5.361])

EO1 = Channel(
    name="EO1",
    divider=0.6254, divider_tol=0.0038,
    amp_mon_product=0.8926,          # <-- 11% low.  Unresolved: gain or monitor.
    amp_pts=_AMP,
    gain_pts=np.array([0.5633, 0.5601, 0.5633, 0.5619, 0.5593, 0.5582]),
    tau_pts=np.array([25.76, 25.77, 26.71, 27.13, 27.60, 27.83]) * 1e-6,
    fn_pts=np.array([2950.2, 2781.9, 2541.8, 2453.6, 2435.6, 2326.0]),
    zeta_pts=np.array([0.2407, 0.2295, 0.2144, 0.2109, 0.2124, 0.2062]),
    noise_trek_in_rms=143.7e-6,
    has_fine_channel=False,
)

EO2 = Channel(
    name="EO2",
    divider=0.6103, divider_tol=0.0037,
    amp_mon_product=1.0011,          # closes exactly
    amp_pts=np.array([0.609, 1.217, 2.441, 3.665, 4.881, 5.367]),
    gain_pts=np.array([0.6123, 0.6098, 0.6126, 0.6125, 0.6079, 0.6075]),
    tau_pts=np.array([27.09, 27.88, 29.18, 29.54, 29.84, 29.59]) * 1e-6,
    fn_pts=np.array([2513.8, 2429.3, 2184.7, 2204.4, 2253.7, 2206.9]),
    zeta_pts=np.array([0.2142, 0.2163, 0.2014, 0.2074, 0.2143, 0.2094]),
    noise_trek_in_rms=623.5e-6,
    has_fine_channel=True, fine_ratio=100.0,
)

CHANNELS = {"EO1": EO1, "EO2": EO2}


@dataclass
class Limits:
    """Hard limits the emitted waveform must respect."""
    awg_rail: float = 10.0            # V peak at the AWG / NI output
    trek_in_rail: float = 10.0        # V peak at the Trek input
    slew_hv: float = 20.0e6           # V/s at the EOM (610E spec, >20 V/us)
    current: float = 2.0e-3           # A, 610E output current limit
    load_capacitance: float = 200e-12 # F, EOM + HV cable + strays -- MEASURE THIS
    hv_max: float = 6000.0            # EOM safe working voltage

LIMITS = Limits()
