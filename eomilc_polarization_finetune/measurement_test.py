"""Read-only polarization readout checks on discrete analyzer-angle captures.

python -m eomilc_polarization_finetune.measurement_test --session SESSION.npz
    --capture CAPTURE.npz --out-dir REPORT

No drive updates. Absolute accuracy requires independently known input
polarization; agreement with the intended voltage target cannot establish it.
"""
import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path

import numpy as np

from .core import optical_error, validate_calibrations, vector
from .workflow import load_capture, load_session, tuner_from_state


@dataclass(frozen=True)
class Limits:
    max_angle_sem_deg: float = 0.1
    max_contrast_error: float = 0.03
    max_fit_rms: float = 0.02
    max_reference_error_deg: float = 0.2
    max_repeat_difference_deg: float = 0.2
    max_geometry_condition: float = 10.

    def validate(self):
        values = tuple(asdict(self).values())
        if not np.isfinite(values).all() or min(values) <= 0:
            raise ValueError('measurement-test limits must be finite and positive')


def angle_difference_deg(a, b):
    """Linear polarization has 180-degree periodicity."""
    return (np.asarray(a) - np.asarray(b) + 90.) % 180. - 90.


def reconstruct(stacks, calibrations):
    """Infer angle directly from calibrated intensity quadratures.

    (I-dark-A)/B = cos(2phi) cos(2theta) + sin(2phi) sin(2theta).
    This does not use the voltage target or voltage-to-angle proportionality.
    Multiple analyzer angles permit an overdetermined model-consistency check.
    """
    validate_calibrations(calibrations)
    if set(stacks) != set(calibrations):
        raise ValueError('capture angles and calibration angles differ')
    angles = sorted(stacks)
    normalized, variance, shot_noise, means = [], [], [], []
    n = None
    for angle in angles:
        shots = np.asarray(stacks[angle], float)
        if shots.ndim != 2 or min(shots.shape) < 2 or not np.isfinite(shots).all():
            raise ValueError('each angle needs >=2 finite repeated shots')
        n = shots.shape[1] if n is None else n
        if shots.shape[1] != n or n < 16:
            raise ValueError('angle captures must share a waveform grid of >=16 samples')
        cal = calibrations[angle]
        mean = shots.mean(axis=0)
        std = shots.std(axis=0, ddof=1)
        normalized.append((mean-cal.i_dark-cal.a)/cal.b)
        # Floor prevents a quantized/identical repeat stack claiming exact truth.
        variance.append(np.maximum((std/cal.b)**2/len(shots), 1e-12))
        shot_noise.append(std)
        means.append(mean)
    theta = np.array([calibrations[a].theta_a for a in angles])
    matrix = np.column_stack([np.cos(2*theta), np.sin(2*theta)])
    if np.linalg.matrix_rank(matrix) < 2:
        raise ValueError('analyzer angles cannot resolve both polarization quadratures; use complementary angles such as 0 and 45 deg')
    condition = float(np.linalg.cond(matrix))
    y, var = np.array(normalized), np.array(variance)
    # Solve all sample-specific weighted 2x2 systems in one batch.
    weight = 1./var
    normal = np.einsum('ai,an,aj->nij', matrix, weight, matrix)
    rhs = np.einsum('ai,an,an->ni', matrix, weight, y)
    covariance = np.linalg.inv(normal)
    quadratures = np.linalg.solve(normal, rhs[...,None])[...,0]
    c, s = quadratures.T
    radius = np.hypot(c,s)
    valid = radius > 1e-8
    phi = np.full(n,np.nan)
    phi[valid] = .5*np.arctan2(s[valid],c[valid])
    gradient = np.zeros((n,2))
    gradient[valid,0] = -.5*s[valid]/radius[valid]**2
    gradient[valid,1] = .5*c[valid]/radius[valid]**2
    sem = np.full(n,np.inf)
    sem[valid] = np.sqrt(np.maximum(0.,np.einsum('ni,nij,nj->n',gradient,covariance,gradient)[valid]))
    residual = y - matrix @ quadratures.T
    return dict(angle_deg=np.rad2deg(phi), angle_sem_deg=np.rad2deg(sem),
                contrast_radius=radius, normalized_fit_residual=residual,
                geometry_condition=condition, angles=angles,
                means=np.array(means), shot_std=np.array(shot_noise), valid=valid)


def evaluate(state, light, monitor, actual, limits=None, known_angle_deg=None,
             start_us=None, end_us=None, repeat_light=None):
    limits = limits or Limits()
    limits.validate()
    tuner = tuner_from_state(state)
    t = vector(state['t'], 'time grid', len(tuner.phi_target))
    selected = np.ones(len(t),bool)
    for bound in (start_us,end_us,known_angle_deg):
        if bound is not None and not np.isfinite(bound):
            raise ValueError('reference angle and time bounds must be finite')
    if start_us is not None:
        selected &= t*1e6 >= start_us
    if end_us is not None:
        selected &= t*1e6 <= end_us
    if not selected.any():
        raise ValueError('measurement test window contains no samples')
    measurement = reconstruct(light,tuner.calibrations)
    if measurement['angle_deg'].shape != t.shape:
        raise ValueError('light captures do not match the session time grid')
    failures, notes = [], []
    def check(ok, message):
        if not ok:
            failures.append(message)
    check(measurement['geometry_condition'] <= limits.max_geometry_condition,
          'Analyzer settings have poor angular coverage.')
    check(measurement['valid'][selected].all(), 'Light captures do not resolve a polarization angle.')
    check(np.max(measurement['angle_sem_deg'][selected]) <= limits.max_angle_sem_deg,
          'Repeated-shot angle uncertainty exceeds the selected limit.')
    contrast_error = np.abs(measurement['contrast_radius']-1.)
    contrast_rms = float(np.sqrt(np.mean(contrast_error[selected]**2)))
    check(contrast_rms <= limits.max_contrast_error,
          'Light amplitudes are inconsistent with calibrated polarization contrast; inspect power stability, calibration and detector clipping.')
    fit_rms = float(np.sqrt(np.mean(measurement['normalized_fit_residual'][:,selected]**2)))
    check(fit_rms <= limits.max_fit_rms, 'Different analyzer angles do not fit one polarization signal.')
    if len(light)==2:
        notes.append('Two angles have no redundant fit check. Add a third calibrated angle for stronger consistency testing.')
    if set(actual)!=set(light) or any(not np.isfinite(actual[a]) or abs((actual[a]-a+180)%360-180)>.05 for a in light):
        failures.append('Analyzer landing angle does not match the requested setting.')
    monitor_means = []
    if set(monitor)!=set(light):
        raise ValueError('simultaneous monitor measurements required for every angle')
    for a,shots in monitor.items():
        shots = np.asarray(shots,float)
        if shots.shape != np.asarray(light[a]).shape or not np.isfinite(shots).all():
            raise ValueError('monitor stack must match its light stack')
        mean = shots.mean(axis=0)
        check(bool(tuner.loop.check(state['current'],mean)), f'Trek monitor fails voltage guards at analyzer angle {a:g}.')
        monitor_means.append(mean)
    monitor_spread = np.ptp(np.array(monitor_means),axis=0)*tuner.loop.channel.mon_scale
    target_error = angle_difference_deg(measurement['angle_deg'],np.rad2deg(tuner.phi_target))
    error_hv, sem_hv, observable = optical_error(tuner.phi_target,light,tuner.calibrations,tuner.settings)
    reference_status, reference_error = 'UNVERIFIED', None
    if known_angle_deg is not None:
        reference_error = angle_difference_deg(measurement['angle_deg'],known_angle_deg)
        accuracy_ok = measurement['valid'][selected].all() and np.max(np.abs(reference_error[selected])) <= limits.max_reference_error_deg
        reference_status = 'PASS' if accuracy_ok else 'FAIL'
    else:
        notes.append('Absolute angle accuracy is unverified: provide an independently known linear polarization, not the voltage target as ground truth.')
    repeat_peak = None
    if repeat_light is not None:
        repeat = reconstruct(repeat_light,tuner.calibrations)
        if repeat['angle_deg'].shape != t.shape:
            raise ValueError('repeat capture waveform length differs')
        repeat_peak = float(np.max(np.abs(angle_difference_deg(repeat['angle_deg'][selected],measurement['angle_deg'][selected]))))
        if not np.isfinite(repeat_peak) or repeat_peak > limits.max_repeat_difference_deg:
            notes.append('Repeat angle difference exceeds the selected limit; this can be real drift or a measurement change.')
    notes.append('Without a reference-power signal, coherent illumination changes can mimic polarization changes.')
    report = dict(health_status='FAIL' if failures else 'PASS', accuracy_status=reference_status,
                  failures=failures, notes=notes, limits=asdict(limits),
                  channel=str(state['channel']), analyzer_angles_deg=measurement['angles'],
                  samples_tested=int(selected.sum()),
                  angle_sem_peak_deg=float(np.max(measurement['angle_sem_deg'][selected])),
                  contrast_error_rms=contrast_rms, normalized_fit_rms=fit_rms,
                  geometry_condition=measurement['geometry_condition'],
                  target_tracking_rms_deg=float(np.sqrt(np.mean(target_error[selected]**2))),
                  known_angle_deg=known_angle_deg,
                  reference_error_peak_deg=None if reference_error is None else float(np.max(np.abs(reference_error[selected]))),
                  repeat_difference_peak_deg=repeat_peak,
                  monitor_difference_peak_hv=float(np.max(monitor_spread[selected])),
                  fine_tune_observable_fraction=float(observable[selected].mean()))
    traces = dict(time_us=t*1e6, measured_angle_deg=measurement['angle_deg'],
                  angle_sem_deg=measurement['angle_sem_deg'],
                  target_angle_deg=np.rad2deg(tuner.phi_target), target_error_deg=target_error,
                  contrast_radius=measurement['contrast_radius'],
                  equivalent_error_hv=error_hv, equivalent_sem_hv=sem_hv,
                  observable=observable.astype(int), tested=selected.astype(int))
    return report,traces


def save_report(folder, report, traces):
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    for name in ('measurement_report.json','measurement_traces.csv'):
        if (folder/name).exists():
            raise FileExistsError(f'{folder/name} already exists; choose a fresh report directory')
    # JSON uses null for undefined measurements instead of nonstandard NaN/Inf.
    def clean(value):
        if isinstance(value,dict):
            return {k:clean(v) for k,v in value.items()}
        if isinstance(value,list):
            return [clean(v) for v in value]
        return None if isinstance(value,float) and not np.isfinite(value) else value
    with open(folder/'measurement_report.json','x') as out:
        json.dump(clean(report),out,indent=2,allow_nan=False)
    with open(folder/'measurement_traces.csv','x') as out:
        np.savetxt(out,np.column_stack(list(traces.values())),delimiter=',',
                   header=','.join(traces),comments='')


def main():
    ap = argparse.ArgumentParser(description='Read-only test of polarization readout at discrete analyzer angles')
    ap.add_argument('--session',required=True)
    ap.add_argument('--capture',required=True)
    ap.add_argument('--compare-capture',help='optional second capture of the same unchanged drive')
    ap.add_argument('--known-angle-deg',type=float,help='independently known static polarization in the selected time window')
    ap.add_argument('--start-us',type=float)
    ap.add_argument('--end-us',type=float)
    ap.add_argument('--out-dir',required=True)
    for key,value in asdict(Limits()).items():
        ap.add_argument('--'+key.replace('_','-'),type=float,default=value)
    args = ap.parse_args()
    state = load_session(args.session)
    light,monitor,actual = load_capture(state,args.capture)
    repeat = load_capture(state,args.compare_capture)[0] if args.compare_capture else None
    report,traces = evaluate(state,light,monitor,actual,
                             Limits(**{k:getattr(args,k) for k in asdict(Limits())}),
                             args.known_angle_deg,args.start_us,args.end_us,repeat)
    save_report(args.out_dir,report,traces)
    print(f"Measurement health: {report['health_status']}; independent angle accuracy: {report['accuracy_status']}")
    for message in report['failures']:
        print('FAIL: '+message)
    for message in report['notes']:
        print('NOTE: '+message)
    print(str(Path(args.out_dir)/'measurement_report.json'))
    return 1 if report['health_status']=='FAIL' or report['accuracy_status']=='FAIL' else 0


if __name__=='__main__':
    raise SystemExit(main())
