"""A bounded optical ILC layer. Angles are radians; drive is AWG volts.

Only the selected EOM changes. Its fringe calibration must have been measured
by varying that EOM alone, with the other EOM held at its operating waveform.
"""
from dataclasses import dataclass
import warnings

import numpy as np
from scipy.fft import dst, idst

from eomilc.ilc import check_limits, notch_filter
from eomilc.plant import smooth
from eomilc.polarimetry import FringeCal, intensity, slope, sensitivity


@dataclass(frozen=True)
class Settings:
    gamma: float = 0.2
    f_cut: float = 100.0
    max_step_awg: float = 0.002
    max_total_awg: float = 0.020
    max_total_hv: float = 10.0
    slope_floor: float = 0.3
    sigma_gate: float = 3.0

    def validate(self, dt):
        values = tuple(vars(self).values())
        if not np.isfinite(values).all() or not np.isfinite(dt) or dt <= 0:
            raise ValueError("settings and dt must be finite, dt positive")
        if not (0 < self.gamma <= 1 and 0 < self.f_cut < 0.5 / dt):
            raise ValueError("need 0 < gamma <= 1 and 0 < f_cut < Nyquist")
        if min(self.max_step_awg, self.max_total_awg, self.max_total_hv) <= 0:
            raise ValueError("correction rails must be positive")
        if not 0 < self.slope_floor < 1 or self.sigma_gate < 0:
            raise ValueError("need 0 < slope_floor < 1 and sigma_gate >= 0")


def vector(x, name, n=None):
    x = np.asarray(x, float)
    if x.ndim != 1 or x.size < 16 or (n is not None and len(x) != n):
        raise ValueError(f"{name} must be a matching 1D waveform (at least 16 points)")
    if not np.isfinite(x).all():
        raise ValueError(f"{name} contains nonfinite samples")
    return x


def slow_correction(step, dt, maximum_hz):
    """Keep slow sine modes k/(2T), preserving both burst endpoints.

    Below the first half-cycle there is no adjustable endpoint-preserving
    mode. Return zero instead of introducing a faster endpoint taper.
    """
    step = vector(step, 'proposed correction')
    frequencies = np.arange(1, len(step)-1) / (2 * (len(step)-1) * dt)
    coefficients = dst(step[1:-1], type=1, norm='ortho')
    coefficients[frequencies > maximum_hz] = 0.
    out = np.zeros_like(step)
    out[1:-1] = idst(coefficients, type=1, norm='ortho')
    return out


def validate_calibrations(calibrations):
    if len(calibrations) < 2:
        raise ValueError("at least two calibrated analyzer angles are required")
    for angle, cal in calibrations.items():
        if not np.isfinite(float(angle)) or not np.isfinite(
            [cal.a, cal.b, cal.omega, cal.psi, cal.theta_a, cal.i_dark]).all():
            raise ValueError("calibration values must be finite")
        if cal.a <= 0 or cal.b <= 0 or cal.omega <= 0 or cal.n_eom != 1:
            raise ValueError("need a positive fringe calibrated with only the selected EOM swept (n_eom=1)")
        delta = (np.rad2deg(cal.theta_a) - float(angle) + 90) % 180 - 90
        if abs(delta) > 0.05:
            raise ValueError("calibration theta_a must match the analyzer user angle from EO zero")


def optical_error(phi_target, stacks, calibrations, settings):
    """Joint least squares of I_target-I_measured against dI/dHV.

    Returns equivalent HV error and its standard error. Complementary slopes
    are combined before inversion; no division by a blind angle's slope.
    Intensity weights use repeat-shot variance, with a floor to avoid treating
    an ADC-flat channel as infinitely precise.
    """
    validate_calibrations(calibrations)
    phi = vector(phi_target, "polarization target")
    numerator, denominator = np.zeros_like(phi), np.zeros_like(phi)
    if set(stacks) != set(calibrations):
        raise ValueError("capture angles must match calibration angles exactly")
    for angle, cal in calibrations.items():
        a = np.asarray(stacks[angle], float)
        if a.ndim != 2 or a.shape[0] < 2 or a.shape[1] != len(phi) or not np.isfinite(a).all():
            raise ValueError("each angle needs >=2 finite shots on the target grid")
        residual = intensity(phi, cal) - a.mean(axis=0)
        derivative = slope(phi, cal) * cal.dphi_dv
        good = sensitivity(phi, cal) >= settings.slope_floor
        variance = a.var(axis=0, ddof=1) / len(a)
        floor = max(float(np.median(variance)), (cal.b * 1e-6) ** 2)
        weight = good / np.maximum(variance, floor)
        numerator += weight * derivative * residual
        denominator += weight * derivative ** 2
    good = denominator > 0
    if not good.any():
        raise ValueError("all samples are optically blind; use complementary analyzer angles")
    error, sem = np.zeros_like(phi), np.full_like(phi, np.inf)
    np.divide(numerator, denominator, out=error, where=good)
    np.sqrt(np.divide(1., denominator, out=np.full_like(phi, np.inf), where=good), out=sem)
    return error, sem, good


@dataclass
class Result:
    drive: np.ndarray
    error_hv: np.ndarray
    sem_hv: np.ndarray
    observable: np.ndarray
    messages: list
    metrics: dict


class FineTuner:
    def __init__(self, loop, baseline, phi_target, calibrations, settings=None):
        self.loop = loop
        self.baseline = vector(baseline, "baseline").copy()
        self.phi_target = vector(phi_target, "polarization target", len(self.baseline)).copy()
        self.calibrations = calibrations
        self.settings = settings or Settings()
        self.settings.validate(loop.dt)
        validate_calibrations(calibrations)
        vector(loop.target, 'monitor target', len(self.baseline))
        params = vars(loop.plant)
        if not np.isfinite(tuple(params.values())).all() or loop.plant.gain <= 0:
            raise ValueError('voltage plant parameters must be finite with positive gain')
        if min(loop.plant.tau, loop.plant.tau2, loop.plant.fn, loop.plant.zeta) < 0:
            raise ValueError('voltage plant time constants/frequency/damping cannot be negative')
        if loop.plant.fn > 0 and loop.plant.zeta <= 0:
            raise ValueError('resonant voltage plant needs positive damping')
        if loop.channel.name not in ("EO1", "EO2"):
            raise ValueError("fine-tuning requires EO1 or EO2")
        report = loop.check(self.baseline)
        if not report:
            raise ValueError(f"baseline fails existing voltage guards: {report}")

    def response(self, delta):
        # The voltage plant remains an approximate local inverse for the optical
        # correction, never a competing monitor-error update.
        if self.loop.frf is not None:
            response = self.loop.frf.forward(delta, self.loop.dt)
        else:
            response = self.loop.plant.forward(delta) - self.loop.plant.offset
        return vector(response, 'predicted monitor correction', len(self.baseline))

    def propose(self, current, stacks, monitor_stacks):
        n = len(self.baseline)
        current = vector(current, "current drive", n)
        cfg, loop = self.settings, self.loop
        messages = []
        def warn(message):
            messages.append(message)
            warnings.warn(message, RuntimeWarning, stacklevel=2)

        existing_delta = current - self.baseline
        if np.max(np.abs(existing_delta)) > cfg.max_total_awg + 1e-12:
            raise ValueError("current drive already exceeds total AWG correction rail")
        if np.max(np.abs(self.response(existing_delta))) * loop.channel.mon_scale > cfg.max_total_hv + 1e-8:
            raise ValueError("current drive already exceeds total predicted HV correction rail")
        if current[0] != self.baseline[0] or current[-1] != self.baseline[-1]:
            raise ValueError("baseline endpoints must be preserved")

        if set(monitor_stacks) != set(stacks):
            raise ValueError("simultaneous monitor stacks required for every angle")
        monitors = []
        for angle, shots in monitor_stacks.items():
            shots = np.asarray(shots, float)
            if shots.shape != np.asarray(stacks[angle]).shape or not np.isfinite(shots).all():
                raise ValueError("monitor and light stacks must have matching finite samples")
            measured = shots.mean(axis=0)
            report = check_limits(current, measured, loop.dt, loop.channel, loop.limits)
            if not report:
                raise ValueError(f"measured monitor fails voltage guards: {report}")
            monitors.append(measured)

        error, sem, good = optical_error(self.phi_target, stacks, self.calibrations, cfg)
        # Soft threshold below the resolved mean error to avoid learning random
        # light fluctuations. Low pass only the correction, never the baseline.
        resolved = np.zeros(n)
        resolved[good] = np.sign(error[good]) * np.maximum(
            np.abs(error[good]) - cfg.sigma_gate * sem[good], 0)
        e_mon = smooth(resolved / loop.channel.mon_scale, loop.dt, cfg.f_cut)
        e_mon = notch_filter(e_mon, loop.dt, loop.notches)
        if loop.frf is not None:
            step = loop.frf.lead(e_mon, loop.dt, cfg.gamma)
        else:
            step = cfg.gamma * loop.plant.lead(e_mon)
        step = slow_correction(step, loop.dt, cfg.f_cut)
        if cfg.f_cut < 1 / (2 * (n-1) * loop.dt):
            warn('Selected slow bandwidth is below the first adjustable mode for this record; correction held at zero. Use a longer record or a wider bandwidth.')
        # Holding individual blind samples would introduce fast changes after
        # slow projection. Require full coverage before correcting instead.
        if not good.all():
            warn('Some samples are optically blind at the selected angles; correction held at zero. Add a complementary analyzer angle.')
            step[:] = 0.
        peak = np.max(np.abs(step))
        if peak >= 0.98 * cfg.max_step_awg:
            warn(f"Fine-tune STEP rail reached: {cfg.max_step_awg:g} V at AWG. Inspect calibration, illumination and residual before continuing.")
        if peak > cfg.max_step_awg:
            step *= cfg.max_step_awg / peak

        # Restrict movement along this proposed direction. A global scale avoids
        # flat-topping the correction and its high-frequency artifacts.
        alpha = 1.0
        def restrict(value, change, rail, label):
            nonlocal alpha
            for sign in (-1, 1):
                increasing = sign * change > 0
                if increasing.any():
                    bound = (rail - sign * value[increasing]) / (sign * change[increasing])
                    alpha = min(alpha, max(0., float(bound.min())))
            if np.max(np.abs(value + change)) >= 0.98 * rail:
                warn(f"Fine-tune {label} rail reached: {rail:g}. Accumulated correction is bounded; further tuning may be blocked.")
        restrict(existing_delta, step, cfg.max_total_awg, "TOTAL AWG (V)")
        hv_delta = self.response(existing_delta) * loop.channel.mon_scale
        hv_step = self.response(step) * loop.channel.mon_scale
        restrict(hv_delta, hv_step, cfg.max_total_hv, "TOTAL predicted HV (V)")
        candidate = current + alpha * step
        candidate[[0, -1]] = self.baseline[[0, -1]]
        if not np.isfinite(candidate).all():
            raise ValueError("inverse produced a nonfinite candidate")
        predicted_change = self.response(candidate - current)
        # Guard both predicted total waveform and measured local operating point.
        prediction = vector(loop.plant.forward(candidate), 'predicted monitor waveform', n)
        report = check_limits(candidate, prediction, loop.dt, loop.channel, loop.limits)
        if not report:
            raise ValueError(f"candidate fails predicted voltage guards: {report}")
        for measured in monitors:
            report = check_limits(candidate, measured + predicted_change, loop.dt, loop.channel, loop.limits)
            if not report:
                raise ValueError(f"candidate fails measured voltage guards: {report}")
        metrics = dict(rms_optical_error_hv=float(np.sqrt(np.mean(error[good] ** 2))),
                       observable_fraction=float(good.mean()),
                       step_peak_awg=float(np.max(np.abs(candidate-current))),
                       total_peak_awg=float(np.max(np.abs(candidate-self.baseline))),
                       total_predicted_peak_hv=float(np.max(np.abs(self.response(candidate-self.baseline))) * loop.channel.mon_scale),
                       rail_scale=alpha)
        if not good.all():
            warn(f"{(~good).sum()} samples have no calibrated optical sensitivity; their correction is held.")
        return Result(candidate, error, sem, good, messages, metrics)
