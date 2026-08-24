"""Iterative learning control for the AWG -> Trek -> monitor chain.

Update law (norm-optimal / plant-inverse ILC):

    u_{k+1} = Q[ u_k + gamma * P^-1 (v_target - y_k) ]

`P^-1` is the identified lead network, so one iteration removes essentially all
of the modelled error and later iterations chip away at what the model missed.
`Q` is a zero-phase low pass: it confines learning to the band where the model
is trustworthy and stops the loop from learning measurement noise.  Without it
ILC will happily converge to a drive full of noise that reproduces one
particular captured trace.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np

from .plant import Plant, smooth
from .config import Channel, Limits, LIMITS, HV_PER_MON


@dataclass
class GuardReport:
    ok: bool
    messages: list

    def __bool__(self):
        return self.ok

    def __str__(self):
        head = "PASS" if self.ok else "FAIL"
        return head + "".join("\n    " + m for m in self.messages)


def check_limits(u_awg: np.ndarray, v_mon: np.ndarray, dt: float,
                 ch: Channel, lim: Limits = LIMITS) -> GuardReport:
    """Verify a candidate drive against every hard limit before you play it."""
    msgs, ok = [], True

    peak = float(np.abs(u_awg).max())
    if peak > lim.awg_rail:
        ok = False
        msgs.append(f"AWG peak {peak:.3f} V exceeds the {lim.awg_rail:.1f} V rail "
                    f"by {1e3*(peak-lim.awg_rail):.0f} mV")
    elif peak > 0.97 * lim.awg_rail:
        msgs.append(f"AWG peak {peak:.3f} V is within 3% of the rail -- little room to iterate")

    trek_in = peak * ch.divider
    if trek_in > lim.trek_in_rail:
        ok = False
        msgs.append(f"Trek input {trek_in:.3f} V exceeds {lim.trek_in_rail:.1f} V")

    hv = v_mon * HV_PER_MON
    hv_pk = float(np.abs(hv).max())
    if hv_pk > lim.hv_max:
        ok = False
        msgs.append(f"peak output {hv_pk:.0f} V exceeds the {lim.hv_max:.0f} V EOM limit")

    slew = float(np.abs(np.gradient(hv, dt)).max())
    if slew > lim.slew_hv:
        ok = False
        msgs.append(f"peak slew {slew/1e6:.2f} V/us exceeds {lim.slew_hv/1e6:.0f} V/us")
    elif slew > 0.5 * lim.slew_hv:
        msgs.append(f"peak slew {slew/1e6:.2f} V/us is over half the limit")

    i_pk = lim.load_capacitance * slew
    if i_pk > lim.current:
        ok = False
        msgs.append(f"peak current {i_pk*1e3:.2f} mA into {lim.load_capacitance*1e12:.0f} pF "
                    f"exceeds {lim.current*1e3:.1f} mA "
                    f"(max load here is {lim.current/slew*1e12:.0f} pF)")
    else:
        msgs.append(f"peak current {i_pk*1e3:.2f} mA of {lim.current*1e3:.1f} mA "
                    f"(assumes {lim.load_capacitance*1e12:.0f} pF -- measure it)")

    msgs.append(f"AWG peak {peak:.3f} V, Trek input {trek_in:.3f} V, "
                f"output {hv_pk:.0f} V, slew {slew/1e6:.2f} V/us")
    return GuardReport(ok, msgs)


@dataclass
class Loop:
    """One ILC loop for one channel.

    target : desired MONITOR waveform, volts (i.e. desired HV / 1000)
    """
    plant: Plant
    target: np.ndarray
    dt: float
    channel: Channel
    gamma: float = 0.6                  # learning gain; 0.5-0.7 is the useful range
    f_cut: float = 20e3                 # Q filter corner -- above the plant
                                        # resonance, below where the model stops
                                        # being trusted.  NOTE this filters the
                                        # whole drive, not just the update, so
                                        # dropping it near fn destroys the
                                        # pre-distortion instead of stabilising
                                        # the loop.  Fix divergence with the
                                        # right model, or with gamma.
    limits: Limits = field(default_factory=lambda: LIMITS)
    history: list = field(default_factory=list)

    # ---------------------------------------------------------------- drives
    def first_shot(self) -> np.ndarray:
        """Model-based pre-distortion. This alone should get you to ~1%."""
        u = self.plant.inverse(self.target)
        return smooth(u, self.dt, self.f_cut)

    def update(self, u_k: np.ndarray, y_k: np.ndarray) -> np.ndarray:
        """One ILC step.  y_k is the measured monitor trace, already aligned
        and resampled onto the target grid (see scope.resample).

        The correction is the plant's own inverse applied to the error, so this
        follows whatever model the Loop was given.  It used to hardcode the
        one-pole lead `(e + tau*de/dt)/gain`, which silently ignored a
        second-order plant and made the loop diverge at the resonance.

        The error is filtered BEFORE the lead as well as after: the resonant
        inverse differentiates twice, and unfiltered 8-bit scope noise through
        d2/dt2 is larger than the correction it is meant to carry.
        """
        e = smooth(self.target - y_k, self.dt, self.f_cut)
        u_next = smooth(u_k + self.gamma * self.plant.lead(e), self.dt, self.f_cut)
        self.history.append(self.metrics(y_k))
        return u_next

    # --------------------------------------------------------------- metrics
    def metrics(self, y: np.ndarray) -> dict:
        e = self.target - y
        span = float(np.ptp(self.target))
        return dict(peak_err_mon=float(np.abs(e).max()),
                    rms_err_mon=float(e.std()),
                    peak_err_hv=float(np.abs(e).max()) * HV_PER_MON,
                    rms_err_hv=float(e.std()) * HV_PER_MON,
                    peak_pct=100 * float(np.abs(e).max()) / span,
                    rms_pct=100 * float(e.std()) / span)

    def check(self, u_awg: np.ndarray, y: np.ndarray | None = None) -> GuardReport:
        v = self.target if y is None else y
        return check_limits(u_awg, v, self.dt, self.channel, self.limits)

    def report(self) -> str:
        if not self.history:
            return "no iterations yet"
        w = ["iter   peak err        rms err      peak %"]
        for i, m in enumerate(self.history):
            w.append(f"{i:>4}   {m['peak_err_hv']:7.1f} V   {m['rms_err_hv']:7.2f} V   {m['peak_pct']:6.2f}%")
        return "\n".join(w)


def averaged(traces: list) -> np.ndarray:
    """Mean of N aligned monitor captures.  Averaging is not optional: a single
    8-bit trace has an LSB worth tens of volts at the output."""
    a = np.asarray(traces, float)
    return a.mean(axis=0)
