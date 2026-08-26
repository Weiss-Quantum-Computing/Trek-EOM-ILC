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
        msgs.append(f"post-divider input {trek_in:.3f} V exceeds {lim.trek_in_rail:.1f} V")

    hv = v_mon * ch.mon_scale
    hv_pk = float(np.abs(hv).max())
    if hv_pk > lim.hv_max:
        ok = False
        msgs.append(f"peak output {hv_pk:.0f} V exceeds the {lim.hv_max:.0f} V "
                    f"{ch.out_name} limit")

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

    idle = float(u_awg[0])
    if abs(idle) > lim.idle_awg + 1e-9:
        ok = False
        msgs.append(f"first sample {idle*1e3:+.1f} mV exceeds the "
                    f"{lim.idle_awg*1e3:.0f} mV idle cap - the AWG holds it "
                    f"between bursts")
    msgs.append(f"AWG peak {peak:.3f} V, idle (first sample) {idle*1e3:+.1f} mV "
                f"of the {lim.idle_awg*1e3:.0f} mV cap, Trek input {trek_in:.3f} V, "
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
    frf: "FRF | None" = None            # measured inverse; see class FRF

    # ---------------------------------------------------------------- drives
    def first_shot(self, flat: bool = True,
                   gain: float | None = None) -> np.ndarray:
        """The first drive to play.

        flat=True (the default): the target scaled by a DC gain alone --
        u = target / gain, no dynamics inverse, no Q filter -- so the first
        measurement IS the chain's raw response to the waveform, and every
        correction the loop applies afterwards is visible against it.

        `gain` is the CONVERSION gain for that flat shot.  It defaults to
        the plant's gain but is deliberately a separate number: the plant
        gain belongs to the error-correction model and may be tuned, refit
        or swapped without silently rescaling what iteration 0 plays.

        flat=False restores the model-based pre-distortion (full model
        inverse plus Q filter, `gain` ignored).  It lands closer on the
        first shot, at the cost of baking the model's opinion into the very
        measurement you would use to judge the model.
        """
        if flat:
            g = self.plant.gain if gain is None else float(gain)
            return _limit_ends(np.asarray(self.target, float) / g)
        u = self.plant.inverse(self.target)
        return _limit_ends(smooth(u, self.dt, self.f_cut))

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
        if self.frf is not None:
            # The measured inverse carries its own band limit (the taper up to
            # f_max), so the error must NOT be pre-filtered at f_cut here --
            # that mistake fed the inverse only the sub-5 kHz error and left a
            # perfectly repeatable 5-15 kHz residual sitting untouched, 2 V rms
            # of it, while the band below converged to nothing. Smooth only at
            # the top of the measured band to keep out-of-band noise from
            # aliasing into the correction.
            e = smooth(self.target - y_k, self.dt, self.frf.f_max)
            u_next = u_k + self.frf.lead(e, self.dt, self.gamma)
        else:
            e = smooth(self.target - y_k, self.dt, self.f_cut)
            u_next = smooth(u_k + self.gamma * self.plant.lead(e),
                            self.dt, self.f_cut)
        self.history.append(self.metrics(y_k))
        return _limit_ends(u_next)

    # --------------------------------------------------------------- metrics
    def metrics(self, y: np.ndarray) -> dict:
        e = self.target - y
        span = float(np.ptp(self.target))
        scale = self.channel.mon_scale       # the *_hv keys are in output units
        return dict(peak_err_mon=float(np.abs(e).max()),
                    rms_err_mon=float(e.std()),
                    peak_err_hv=float(np.abs(e).max()) * scale,
                    rms_err_hv=float(e.std()) * scale,
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


class FRF:
    """A measured transfer function, used as a nonparametric inverse.

    Born 2026-08-24, when a multitone measurement showed the chain has NO
    resonant peak at operating-signal levels: the second-order model's Q=2.4
    peak at 2.3-2.9 kHz is a large-signal artifact of the ramp fits, and
    inverting that fictitious peak is what stalled the loop at 3-6 kHz (0.65x
    magnitude at +49 degrees of phase error in-band). Dividing the error by
    the MEASURED response sidesteps the model question entirely, in whatever
    band the measurement is coherent.

    Loads the CSV sysid_fit writes. The inverse is regularised three ways:
    tones below `min_coherence` are dropped, the correction band is
    tapered to zero between `f_use` and `f_max` with a raised cosine so the
    update never acts where nothing was measured, and the correction is
    raised-cosined to zero over the first and last `t_guard` seconds so the
    loop does not chase the record-boundary settling transients (see the
    note in `lead`).
    """

    def __init__(self, path, min_coherence=0.9, f_use=15e3, f_max=22e3,
                 t_guard=None):
        if not 0 < f_use < f_max:
            raise ValueError(
                f"the taper needs 0 < f_use < f_max, got {f_use:g}/{f_max:g}."
                f" A zero-width taper is a brick wall -- it rings, and a bin"
                f" landing exactly on the edge divides 0/0 into the drive."
                f" To nearly disable it use f_use ~ 0.9 * f_max.")
        import pandas as pd
        d = pd.read_csv(path)
        m = d["coherence"].to_numpy() >= min_coherence
        self.f = d["f_Hz"].to_numpy()[m]
        H = (d["H_mag"].to_numpy() *
             np.exp(1j * np.radians(d["H_phase_deg"].to_numpy())))[m]
        self.logmag = np.log(np.abs(H))
        self.phase = np.unwrap(np.angle(H))
        self.f_use, self.f_max = f_use, f_max
        # None = auto: three ring times of the inverse (the taper's
        # transition width sets how long 1/H rings), floored at 100 us,
        # capped at 1 ms. The first cut hardcoded 0.5 ms -- 30x wider than
        # the ~15 us artifact it was built to stop -- and blocked the loop
        # from correcting the chain's real settling transient in the last
        # ~60 us of the record: PRFRX1B converged to 1.1 V mid-record but
        # 52 V at the last sample (the one-pole loop, which corrects to the
        # edge, left 0.8 V there). Explicit t_guard still overrides; 0
        # disables (tests/diagnosis only).
        self.t_guard = t_guard
        self.path = str(path)

    def edge_guard_s(self):
        """The effective edge guard: explicit t_guard if set, else three
        ring times of the inverse (1/(taper width)), floored at 100 us,
        capped at 1 ms."""
        if self.t_guard is not None:
            return self.t_guard
        return min(1e-3, max(100e-6, 3.0 / (self.f_max - self.f_use)))

    def interp(self, f):
        """H at arbitrary frequencies: log-magnitude and unwrapped phase,
        both linearly interpolated in log-f, held flat outside the tones."""
        lf = np.log(np.clip(f, self.f[0] * 0.5, None))
        src = np.log(self.f)
        mag = np.exp(np.interp(lf, src, self.logmag))
        ph = np.interp(lf, src, self.phase)
        return mag * np.exp(1j * ph)

    # 1/H is ACAUSAL: the chain delays, so its inverse pre-acts.  In an
    # unpadded FFT that pre-action wraps circularly onto the far END of the
    # record, where _limit_ends then clamps it away -- the correction the
    # record start needs is deleted every iteration, and the start error
    # ratchets instead of converging (PRFRX1A: 28 mV at t=0 by i20, with a
    # -230 mV ghost at the record end).  Pad well past the inverse's
    # pre-action span (~0.1 ms) and crop; the discarded pre-action is
    # unrealisable anyway -- sample 0 is the AWG's inter-burst hold level,
    # nothing can play before it.
    N_PAD = 1024

    def lead(self, e, dt, gamma=1.0):
        """gamma * IFFT( taper(f) * E(f) / H(f) ) -- the measured inverse."""
        n = len(e)
        m = n + self.N_PAD
        E = np.fft.rfft(e, m)
        f = np.fft.rfftfreq(m, dt)
        H = self.interp(np.where(f > 0, f, self.f[0]))
        H[0] = np.abs(self.interp(np.array([self.f[0]])))[0]   # DC: real gain
        taper = np.ones_like(f)
        band = (f >= self.f_use) & (f <= self.f_max)
        taper[band] = 0.5 * (1 + np.cos(np.pi * (f[band] - self.f_use)
                                        / (self.f_max - self.f_use)))
        taper[f > self.f_max] = 0.0
        u = gamma * np.fft.irfft(taper * E / H, n=m)[:n]
        # Time guard: decline to chase the record-boundary settling
        # transients.  They are sharp, so their 20-60 kHz content is broad,
        # and 1/|H| up there is 15-80x -- with the wrap fixed, the loop
        # dutifully built ~0.23 V of drive at the record end to buy the
        # last ~2 mV of edge error (fresh PRFRX1A, i00-i05).  That hammers
        # the Trek at frequencies it cannot follow for error outside the
        # window anyone cares about.  Raised-cosine the CORRECTION (never
        # the drive -- see the f_cut note above) to zero at the ends.
        tg = self.edge_guard_s()
        g = int(round(tg / dt))
        if g > 0 and 2 * g < n:
            # Fade only the FAST part of the correction. The slow residue
            # (below ~0.25/t_guard) is the idle-offset trim -- the reason
            # _limit_ends allows +/-100 mV at the record ends at all (the
            # chain's own offsets once parked the EOMs at -9 V when sample
            # 0 was forced to file-zero) -- and content that slow cannot
            # ring against the clamp. Zeroing the whole correction here
            # would freeze the ends at the flat shot's level and leave the
            # standing idle error uncorrectable forever.
            base = smooth(u, dt, 0.25 / tg)
            ramp = 0.5 * (1 - np.cos(np.pi * np.arange(g) / g))   # 0 -> 1
            w = np.ones(n)
            w[:g] = ramp
            w[n - g:] = ramp[::-1]
            u = base + (u - base) * w
        return u


def _limit_ends(u: np.ndarray, cap: float = LIMITS.idle_awg) -> np.ndarray:
    """Clamp the first and last samples to the idle cap - NOT to zero.

    Between bursts the AWG holds the record's FIRST sample, so sample 0 sets
    the standing level on the EOM for the whole inter-burst gap. Forcing it to
    file-zero (the previous rule) turned out to fight the loop: the chain has
    its own idle offsets - the generator's zero-code error plus the
    preconditioning network's - so file-zero still parked the EOMs at -9 V
    (X1) and -41 V (X2), and every burst opened with a transient the pinned
    drive could never remove. Left free, the update walks the first sample to
    whatever value parks the CHAIN at zero, which kills both the standing
    level and the entry transient.

    The cap bounds how far it may walk: 100 mV at the AWG is ~35-55 V at the
    EOM worst case, a safe idle by the bench's own judgement (2026-08-24).
    The last sample gets the same clamp - the post-burst hold returns to the
    first sample, and keeping the two comparable keeps that hand-off small.
    """
    u = np.asarray(u, float).copy()
    u[0] = float(np.clip(u[0], -cap, cap))
    u[-1] = float(np.clip(u[-1], -cap, cap))
    return u


def averaged(traces: list) -> np.ndarray:
    """Mean of N aligned monitor captures.  Averaging is not optional: a single
    8-bit trace has an LSB worth tens of volts at the output."""
    a = np.asarray(traces, float)
    return a.mean(axis=0)
