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
    if lim.load_capacitance > 0 and np.isfinite(lim.current):
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
        cap = self.limits.idle_awg
        if flat:
            g = self.plant.gain if gain is None else float(gain)
            return _limit_ends(np.asarray(self.target, float) / g, cap)
        u = self.plant.inverse(self.target)
        return _limit_ends(smooth(u, self.dt, self.f_cut), cap)

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
        return _limit_ends(u_next, self.limits.idle_awg)

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


def _read_frf(path, min_coherence=0.9):
    """The coherent tones of an FRF CSV: (f_Hz, complex H), both cut to
    `min_coherence`.  One reader, so `frf_band` and `FRF` cannot disagree
    about which tones a file actually carries."""
    import pandas as pd
    d = pd.read_csv(path)
    m = d["coherence"].to_numpy() >= min_coherence
    f = d["f_Hz"].to_numpy()[m]
    if f.size == 0:
        raise ValueError(
            f"no tone in {path} reaches coherence {min_coherence:g} -- there "
            f"is nothing measured here to divide the error by.")
    H = (d["H_mag"].to_numpy() *
         np.exp(1j * np.radians(d["H_phase_deg"].to_numpy())))[m]
    return f, H


def frf_band(path, min_coherence=0.9):
    """(f_lo, f_hi) Hz of the tones an FRF file keeps.

    A taper has to stay inside this.  Above f_hi `FRF.interp` holds |H| flat
    at the last measured tone, so a correction up there is divided by an
    extrapolation rather than by a measurement -- and since the real chain
    has rolled off, 1/|H| stays large and the update builds drive nothing
    ever measured.  Split out so a front end can check a taper against a
    file without building the FRF first.
    """
    f, _ = _read_frf(path, min_coherence)
    return float(f.min()), float(f.max())


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

    The taper band is checked against the tones the file actually carries:
    `f_use` past the top coherent tone is refused, and a `f_max` past it is
    reported as `f_extrap` for the caller to warn about.  See `frf_band`.
    """

    def __init__(self, path, min_coherence=0.9, f_use=15e3, f_max=22e3,
                 t_guard=0.0):
        if not 0 < f_use < f_max:
            raise ValueError(
                f"the taper needs 0 < f_use < f_max, got {f_use:g}/{f_max:g}."
                f" A zero-width taper is a brick wall -- it rings, and a bin"
                f" landing exactly on the edge divides 0/0 into the drive."
                f" To nearly disable it use f_use ~ 0.9 * f_max.")
        self.f, H = _read_frf(path, min_coherence)
        # The taper must live inside the tones. f_use at or above the top one
        # means the FULL-strength part of the correction band is divided by
        # `interp`'s flat extrapolation instead of by a measurement: 1/|H|
        # holds at its last measured value where the chain has really rolled
        # off, and the loop dutifully builds drive up there. Measured on
        # frf_WIDE_X1 (top tone 80.0 kHz), 20 mV rms of monitor grass through
        # gamma 0.6: a 50-75 kHz taper asks for 0.57 V peak at the AWG, an
        # 80-160 kHz one for 2.01 V. check_limits cannot see it either -- it
        # takes slew and current from the TARGET, so out-of-band drive is
        # invisible to every guard but the AWG rail.
        self.f_top = float(self.f.max())
        if f_use >= self.f_top:
            raise ValueError(
                f"f_use = {f_use/1e3:g} kHz is at or above the top coherent "
                f"tone of {path} ({self.f_top/1e3:.1f} kHz), so the whole "
                f"full-strength band would be divided by an extrapolated |H| "
                f"rather than a measured one. Pull the taper inside the "
                f"measured band, or measure a wider FRF.")
        # How much of the fading part sits past the tones. Non-zero is a
        # judgement call, not an error -- the taper is already attenuating
        # there -- so callers warn rather than refuse.
        self.f_extrap = max(0.0, f_max - self.f_top)
        self.logmag = np.log(np.abs(H))
        self.phase = np.unwrap(np.angle(H))
        self.f_use, self.f_max = f_use, f_max
        # Edge-guard fade: DISABLED by default (0) -- per Maarten,
        # 26 Aug 2026, after the guard saga: the 0.5 ms first cut blocked
        # the chain's real settling transient (PRFRX1B: 52 V at the last
        # sample vs the one-pole's 0.8 V) and the campaigns still
        # misbehaved, so the baseline is now the RAW taper*E/H inverse,
        # observed before any edge remedy is layered back on. Set
        # t_guard=None for the taper-sized auto guard (3 ring times,
        # floor 100 us, cap 1 ms) or a value in seconds explicitly.
        # The zero-padding above is NOT part of the guard -- it makes the
        # convolution linear instead of circular and stays.
        self.t_guard = t_guard
        self.path = str(path)

    def edge_guard_s(self):
        """The effective edge guard: 0 by default (raw inverse -- the
        baseline being observed); None = three ring times of the inverse
        (1/(taper width)), floored at 100 us, capped at 1 ms; or explicit
        seconds."""
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

    def forward(self, u, dt):
        """The measured chain applied to a drive: IFFT(U * H), zero-padded so
        the convolution is linear.  The forward counterpart of `lead`, for
        predicting what an update will do to the monitor (`model_check`).

        Below the first tone |H| is held flat and the phase taken linearly
        to zero at DC -- a pure delay, which is what a chain does down
        there; holding the first tone's phase flat instead puts a fixed
        time shift on the whole ramp, and that is not a chain anyone has.
        Past the top tone H is rolled off with a Gaussian, so a drive's
        out-of-band content is not fed through at the last measured gain.
        """
        u = np.asarray(u, float)
        n = len(u)
        m = n + self.N_PAD
        f = np.fft.rfftfreq(m, dt)
        H = self.interp(np.where(f > 0, f, self.f[0]))
        low = f < self.f[0]
        ph0 = float(np.angle(self.interp(np.array([self.f[0]])))[0])
        H[low] = np.abs(H[low]) * np.exp(1j * ph0 * f[low] / self.f[0])
        top = float(self.f[-1])
        hi = f > top
        H[hi] = H[hi] * np.exp(-((f[hi] - top) / (0.25 * top)) ** 2)
        return np.fft.irfft(np.fft.rfft(u, m) * H, n=m)[:n]

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


def learnable_band(target, stack, dt: float, f_top: float,
                   factor: float = 2.0, bw: float = 5e3) -> dict:
    """Where the loop has run out of error to learn.

    `stack` is the (n_shots, n) set of single shots whose mean is the
    measurement the update will act on.  Per frequency, the error
    |FFT(target - mean)| is compared with the standard error of that mean,
    sqrt(variance across shots / n_shots), in bands `bw` wide up to `f_top`
    (f_cut on the parametric rungs, the taper's end on the FRF rung).

    Below `factor` times the noise the measured error is mostly the
    measurement's own scatter, and the update it drives is the inverse's
    gain times noise laid into the drive -- 40-65x at 50-70 kHz on X1 with
    the second-order lead, fresh every iteration, so it does not average
    away.  P92PX1B (2 Sep 2026): the peak error stopped falling at
    iteration 5; iterations 6-20 put 26 mV rms of 50-70 kHz into the drive
    and 1.4 V rms of ripple onto the EOM that the flat first shot never had.

    Returns dict(f_floor, ratio_top, n_shots, factor, bands).  f_floor is
    the lowest band edge above which EVERY band up to f_top sits under
    `factor` x noise: None while the loop is still learning at f_top,
    0.0 when nothing is left anywhere.  ratio_top is error/noise in the
    band just below f_top.  bands is [(lo, hi, err_rms, noise_rms)].
    """
    stack = np.asarray(stack, float)
    if stack.ndim != 2 or stack.shape[0] < 2:
        raise ValueError("a repeatability estimate needs at least two shots")
    n_sh, n = stack.shape
    y = stack.mean(axis=0)
    w = np.hanning(n)
    f = np.fft.rfftfreq(n, dt)
    E = np.abs(np.fft.rfft((np.asarray(target, float) - y) * w))
    D = np.fft.rfft((stack - y) * w, axis=1)              # per-shot deviation
    sem = np.sqrt(np.mean(np.abs(D) ** 2, axis=0) / (n_sh - 1))
    edges = np.arange(0.0, float(f_top) + bw, bw)
    bands = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (f >= lo) & (f < min(hi, f_top))
        if not m.any():
            continue
        bands.append((float(lo), float(min(hi, f_top)),
                      float(np.sqrt(np.sum(E[m] ** 2))),
                      float(np.sqrt(np.sum(sem[m] ** 2)))))
    if not bands:
        raise ValueError(f"no FFT bin below f_top = {f_top:g} Hz")
    ratio = [e / s if s > 0 else float("inf") for _, _, e, s in bands]
    f_floor = None
    for i, (lo, _, _, _) in enumerate(bands):
        if all(r < factor for r in ratio[i:]):
            f_floor = lo
            break
    return dict(f_floor=f_floor, ratio_top=ratio[-1], n_shots=int(n_sh),
                factor=float(factor), bands=bands)


def update_rms(u_prev, u_next) -> float:
    """Size of one update, rms volts at the AWG -- the number `plateau`
    watches, recorded into each history entry by the front ends."""
    d = np.asarray(u_next, float) - np.asarray(u_prev, float)
    return float(np.sqrt(np.mean(d * d)))


def plateau(history: list, n: int = 5, tol: float = 0.15):
    """Is the loop re-learning noise?

    `learnable_band` catches error that has fallen under the measurement's
    scatter.  It cannot catch the subtler regime: ripple the loop CREATED
    from amplified scatter is repeatable, so it reads as genuine error and
    the loop keeps "correcting" it -- while injecting fresh scatter at the
    same gain.  The signature is in the history: the peak error stops
    falling, and the update does not shrink.  A loop still converging shows
    both moving; a loop that has finished shows the update collapsing.
    P92PX1B (2 Sep 2026) sat at 2.7-3.5 V from iteration 5 to 20 while the
    update held near 30 mV rms -- fifteen iterations that put 1.4 V rms of
    50-70 kHz ripple onto the EOM.

    Over the last `n` entries: flat when the best peak error there is not
    below the best of everything before it by more than `tol`, AND the
    median update over those `n` is not below the median of the `n` before
    by more than `tol`.  Needs n+1 entries carrying `update_rms`; returns
    None before that.  Otherwise a dict with `flat`, the two bests, the two
    update medians, `best_it` (which iteration's drive was best) and `n`.
    """
    peaks = [h.get("peak_err_hv") for h in history if isinstance(h, dict)]
    upds = [h.get("update_rms") for h in history if isinstance(h, dict)]
    k = len(upds)                                  # trailing run that has it
    while k > 0 and upds[k - 1] is not None:
        k -= 1
    tail = [float(v) for v in upds[k:]]
    if len(peaks) < n + 1 or len(tail) < n + 1:
        return None
    best_recent = float(min(peaks[-n:]))
    best_before = float(min(peaks[:-n]))
    upd_recent = float(np.median(tail[-n:]))
    upd_before = float(np.median(tail[:-n][-n:]))
    flat = (best_recent >= (1 - tol) * best_before
            and upd_recent >= (1 - tol) * upd_before)
    return dict(flat=bool(flat), n=int(n), best_recent=best_recent,
                best_before=best_before, upd_recent=upd_recent,
                upd_before=upd_before, best_it=int(np.argmin(peaks)),
                best=float(min(peaks)))


def _bandpass(x, lo, hi, dt):
    from scipy.signal import butter, sosfiltfilt
    nyq = 0.5 / dt
    if lo <= 0:
        sos = butter(4, min(hi / nyq, 0.99), btype="low", output="sos")
    else:
        sos = butter(4, [lo / nyq, min(hi / nyq, 0.99)], btype="band",
                     output="sos")
    return sosfiltfilt(sos, np.asarray(x, float))


def model_bands(f_top):
    """The octave-ish bands `model_check` reports, clipped to the band edge."""
    edges = (0.0, 5e3, 10e3, 20e3, 40e3, 80e3, 160e3, 320e3)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        if lo >= f_top:
            break
        out.append((lo, min(hi, f_top)))
    return out


def model_check(du, dy, forward, dt: float, f_top: float, gamma: float = 0.6,
                bands=None, min_update: float = 1e-4) -> list:
    """Did the chain answer the last update the way the model said it would?

    `du` is the update that was applied (u_k - u_{k-1}), `dy` the monitor
    change it produced (y_k - y_{k-1}), and `forward` the model's own
    forward operator (Plant.forward less its offset, or FRF.forward).  Per
    band up to `f_top`, the achieved change is compared with the predicted
    one: `ratio` is achieved/predicted rms over the record, `corr` their
    correlation, and `ratio_local` the same ratio inside the stretch of
    record where the update's content in that band is strongest -- the
    corners, when that is where the loop acted -- because a whole-record
    ratio dilutes a corner that answered 4x by everything else that
    answered 1x.  `lam` = |1 - gamma * ratio_local| is the contraction the
    loop actually has there; at or above 1 the next update overshoots.

    Verdicts: "quiet" (the update put less than `min_update` V rms in the
    band -- nothing to check), "unresponsive" (corr < 0.3: the monitor's
    change has nothing to do with what was pushed -- noise, or an artefact
    the drive cannot reach), "no contraction" (lam >= 1), else "ok".

    P92PX1D, iteration 1 (2 Sep 2026): 20-40 kHz answered at 4.4x, corr
    0.86 -- a real 0.25 mV feature at the corners, over-corrected threefold
    by a small-signal model, which is the ripple that campaign built.
    """
    du = np.asarray(du, float)
    dy = np.asarray(dy, float)
    if du.shape != dy.shape or du.ndim != 1:
        raise ValueError("du and dy must be the same 1-d record")
    pred = np.asarray(forward(du), float)
    if pred.shape != du.shape:
        raise ValueError("forward(du) must return a record of du's length")
    n_env = max(int(round(200e-6 / dt)), 3)          # a 0.2 ms rms window
    out = []
    for lo, hi in (bands or model_bands(f_top)):
        d = _bandpass(du, lo, hi, dt)
        a = _bandpass(dy, lo, hi, dt)
        p = _bandpass(pred, lo, hi, dt)
        rms = lambda x: float(np.sqrt(np.mean(x * x)))
        row = dict(lo=float(lo), hi=float(hi), update_rms=rms(d))
        if rms(d) < min_update or rms(p) <= 0:
            row.update(verdict="quiet", ratio=None, corr=None,
                       ratio_local=None, lam=None)
            out.append(row)
            continue
        env = np.sqrt(np.convolve(d * d, np.ones(n_env) / n_env, mode="same"))
        mask = env >= 0.5 * env.max()
        ratio = rms(a) / rms(p)
        corr = float(np.corrcoef(a, p)[0, 1]) if a.std() > 0 and p.std() > 0 else 0.0
        ratio_local = rms(a[mask]) / max(rms(p[mask]), 1e-15)
        lam = abs(1.0 - gamma * ratio_local)
        if corr < 0.3:
            verdict = "unresponsive"
        elif lam >= 1.0:
            verdict = "no contraction"
        else:
            verdict = "ok"
        row.update(verdict=verdict, ratio=ratio, corr=corr,
                   ratio_local=ratio_local, lam=lam)
        out.append(row)
    return out
