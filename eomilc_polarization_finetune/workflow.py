"""Separate optical sessions and verified analyzer-angle capture sets."""
import hashlib
import json
from pathlib import Path
import time
import uuid

import numpy as np

from eomilc.config import CHANNELS
from eomilc.ilc import Loop, FRF
from eomilc.plant import Plant
from eomilc.polarimetry import FringeCal
from .core import FineTuner, Settings, vector


def drive_hash(drive):
    return hashlib.sha256(np.asarray(drive, dtype='<f8').tobytes()).hexdigest()


def initialize(voltage_state, calibration_paths, settings=None,
               frf=None, frf_use=15000., frf_max=22000., hv_for_90=None,
               target_zero_hv=0., angle_offset_deg=0.):
    # Existing voltage states carry object-valued history. Read trusted local
    # repo state files only; the optical session itself never uses pickle.
    with np.load(voltage_state, allow_pickle=True) as z:
        channel = str(z['channel'])
        if channel not in ('EO1', 'EO2'):
            raise ValueError('choose an EO1 or EO2 voltage state')
        t = vector(z['t'], 'time grid')
        dt = float(z['dt'])
        if dt <= 0 or not np.allclose(np.diff(t), dt, rtol=1e-7, atol=1e-12):
            raise ValueError('voltage state must have a finite uniform time grid')
        params = {k: float(z[k]) if k in z else 0. for k in
                  ('gain', 'tau', 'tau2', 'fn', 'zeta', 'offset')}
        notches = np.asarray(z['notches'], float).reshape(-1, 2) if 'notches' in z else np.empty((0, 2))
        base = vector(z['u'], 'voltage baseline', len(t))
        target = vector(z['target'], 'monitor target', len(t))
        full_scale = float(z['full_scale'])
        if not np.isfinite(full_scale) or full_scale <= 0 or max(abs(base)) > full_scale:
            raise ValueError('invalid fixed AWG full scale')
        offset = float(z['t_offset']) if 't_offset' in z else 0.
    cals = {float(a): FringeCal.load(p) for a, p in calibration_paths.items()}
    cfg = settings or Settings()
    hv_for_90 = CHANNELS[channel].v90_hv if hv_for_90 is None else float(hv_for_90)
    if not np.isfinite([hv_for_90, target_zero_hv, angle_offset_deg]).all() or hv_for_90 <= 0:
        raise ValueError('target mapping must be finite with positive HV for 90 degrees')
    # Intended angle comes from the ORIGINAL target, never from the measured
    # monitor or a recalculated corrected waveform. The optical measurements
    # then expose departures from this intended proportional relationship.
    phi = np.deg2rad(angle_offset_deg + 90. *
                     (target * CHANNELS[channel].mon_scale - target_zero_hv) / hv_for_90)
    state = dict(t=t, dt=dt, baseline=base.copy(), current=base.copy(), target=target,
                 phi_target=phi, channel=channel,
                 hv_for_90=hv_for_90, target_zero_hv=target_zero_hv,
                 angle_offset_deg=angle_offset_deg,
                 plant_json=json.dumps(params), settings_json=json.dumps(vars(cfg)),
                 calibrations_json=json.dumps({str(a): vars(c) for a, c in cals.items()}),
                 notches=notches, full_scale=full_scale, t_offset=offset, iteration=0,
                 session_id=uuid.uuid4().hex, voltage_source=str(Path(voltage_state).resolve()),
                 frf_path=str(Path(frf).resolve()) if frf else '',
                 frf_use=frf_use, frf_max=frf_max)
    tuner_from_state(state)
    return state


def tuner_from_state(state):
    channel = CHANNELS[str(state['channel'])]
    plant = Plant(**json.loads(str(state['plant_json'])), dt=float(state['dt']))
    loop = Loop(plant, state['target'], float(state['dt']), channel,
                limits=channel.limits,
                notches=tuple(map(tuple, state['notches'])))
    path = str(state['frf_path'])
    if path:
        loop.frf = FRF(path, f_use=float(state['frf_use']), f_max=float(state['frf_max']))
    cals = {float(a): FringeCal(**c) for a, c in json.loads(str(state['calibrations_json'])).items()}
    return FineTuner(loop, state['baseline'], state['phi_target'], cals,
                     Settings(**json.loads(str(state['settings_json']))))


def load_session(path):
    with np.load(path, allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def save_iteration(folder, state):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"pol_i{int(state['iteration']):03d}"
    path = folder / (stem + '.npz')
    if path.exists():
        raise FileExistsError(f'{path} already exists; use a new session/output folder')
    if np.max(np.abs(state['current'])) > float(state['full_scale']):
        raise ValueError('drive exceeds fixed AWG full scale')
    np.savez(path, **state)
    np.savetxt(folder / (stem + '_drive.csv'), np.column_stack(
        [state['t'] * 1e6, state['current']]), delimiter=',', header='time_us,voltage_V', comments='')
    with open(folder / (stem + '_awg.csv'), 'w') as out:
        out.write(f"# BK4063B-AWG-GUI waveform '{stem}', fixed mapping, full scale {float(state['full_scale']):g} V\n")
        out.write(f"# AMP {2 * float(state['full_scale']):g} Vpp, OFST 0, normalise OFF\n")
        np.savetxt(out, state['current'] / float(state['full_scale']), fmt='%.12g')
    return path


def capture_angles(rotator, grab, angles=(0., 45.), settle=0.2, tolerance=0.05):
    """grab() returns simultaneous aligned (light_stack, monitor_stack).

    Hardware capture happens only after settled position is re-read. The same
    waveform must play throughout this set; angle captures are sequential.
    """
    angles = tuple(map(float, angles))
    if len(angles) < 2 or len(set(angles)) != len(angles) or not np.isfinite(angles).all():
        raise ValueError('need at least two distinct finite analyzer angles')
    if not np.isfinite(settle) or settle < 0 or not np.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('invalid motion settling or tolerance')
    light, monitor, actual = {}, {}, {}
    for angle in angles:
        actual[angle] = rotator.goto(angle, tol=tolerance, settle=settle)
        light[angle], monitor[angle] = grab()
    return light, monitor, actual


def save_capture(path, state, light, monitor, actual):
    angles = sorted(light)
    if set(angles) != set(monitor) or set(angles) != set(actual):
        raise ValueError('capture angle sets differ')
    with open(path, 'xb') as out:
        np.savez(out, angles=angles, actual_angles=[actual[a] for a in angles],
                 light=np.stack([light[a] for a in angles]),
                 monitor=np.stack([monitor[a] for a in angles]),
                 t=state['t'], session_id=state['session_id'],
                 iteration=state['iteration'], drive_hash=drive_hash(state['current']),
                 captured_unix=time.time())


def load_capture(state, capture_path):
    """Read a capture after checking its session, drive, grid and angles."""
    with np.load(capture_path, allow_pickle=False) as z:
        if str(z['session_id']) != str(state['session_id']) or int(z['iteration']) != int(state['iteration']) or str(z['drive_hash']) != drive_hash(state['current']):
            raise ValueError('capture belongs to a different session/iteration/drive')
        if not np.array_equal(z['t'], state['t']):
            raise ValueError('capture time grid differs from session')
        angles = z['angles']
        if len(set(angles)) != len(angles) or z['actual_angles'].shape != angles.shape:
            raise ValueError('invalid capture angle metadata')
        if not np.isfinite(z['actual_angles']).all() or np.max(np.abs((z['actual_angles'] - angles + 180) % 360 - 180)) > 0.05:
            raise ValueError('analyzer did not land within 0.05 degrees')
        if len(z['light']) != len(angles) or len(z['monitor']) != len(angles):
            raise ValueError('capture stack count does not match angles')
        light = dict(zip(map(float, angles), z['light']))
        monitor = dict(zip(map(float, angles), z['monitor']))
        actual = dict(zip(map(float, angles), z['actual_angles']))
    return light, monitor, actual


def step(state, capture_path):
    light, monitor, _ = load_capture(state, capture_path)
    result = tuner_from_state(state).propose(state['current'], light, monitor)
    updated = dict(state, current=result.drive, iteration=int(state['iteration']) + 1,
                   metrics_json=json.dumps(result.metrics), warnings_json=json.dumps(result.messages),
                   optical_error_hv=result.error_hv, optical_sem_hv=result.sem_hv,
                   observable=result.observable,
                   capture_source=str(Path(capture_path).resolve()))
    return updated, result
