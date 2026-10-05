"""Run with python -m eomilc_polarization_finetune from the repository root."""
import argparse
import json
from pathlib import Path

from .core import Settings
from .workflow import initialize, load_session, save_iteration, step, capture_angles, save_capture, tuner_from_state

PREFERENCES = Path(__file__).parent / 'last_case.json'


def main():
    last = json.loads(PREFERENCES.read_text()) if PREFERENCES.exists() else {}
    ap = argparse.ArgumentParser(description='Bounded optical corrections on a converged voltage state')
    sub = ap.add_subparsers(dest='action', required=True)
    calibrate = sub.add_parser('calibrate', help='fit one angle from a measured sweep of the selected EOM alone')
    calibrate.add_argument('--sweep', required=True, help='CSV: hv_V,light_V, measured HV and photodiode voltage')
    calibrate.add_argument('--angle', type=float, required=True, help='analyzer angle from EO zero, degrees')
    calibrate.add_argument('--v-pi-guess', type=float, default=5200.)
    calibrate.add_argument('--dark', type=float, default=0.)
    calibrate.add_argument('--out', required=True)
    init = sub.add_parser('init', help='create a separate optical session; baseline source stays unchanged')
    init.add_argument('--voltage-state', default=last.get('voltage_state'))
    init.add_argument('--channel', choices=['EO1', 'EO2'], default=None,
                      help='optional check that the selected voltage state belongs to this channel')
    init.add_argument('--hv-for-90', type=float, default=None,
                      help='nominal target HV corresponding to 90 degrees; default channel v90 calibration')
    init.add_argument('--target-zero-hv', type=float, default=0.,
                      help='original target HV defined as zero rotation')
    init.add_argument('--angle-offset-deg', type=float, default=0.,
                      help='fixed additional target polarization contribution')
    init.add_argument('--cal', action='append', required=True, metavar='DEGREES=FRINGE.JSON')
    init.add_argument('--out-dir', required=True)
    init.add_argument('--frf', help='measured voltage FRF CSV; specify explicitly if baseline used one')
    init.add_argument('--frf-use', type=float, default=15000.)
    init.add_argument('--frf-max', type=float, default=22000.)
    for key, value in vars(Settings()).items():
        init.add_argument('--' + key.replace('_', '-'), type=float, default=value)
    capture = sub.add_parser('capture', help='rotate analyzer and capture currently playing drive; no AWG upload')
    capture.add_argument('--session', required=True)
    capture.add_argument('--out', required=True)
    capture.add_argument('--port', default=last.get('port', 'auto'))
    capture.add_argument('--addr', default='0')
    capture.add_argument('--zero', type=float, default=last.get('zero', 0.))
    capture.add_argument('--pd-ch', type=int, choices=[1,2,3,4], default=last.get('pd_ch'))
    capture.add_argument('--mon-ch', type=int, choices=[1,2,3,4], default=None)
    capture.add_argument('--drive-ch', type=int, choices=[1,2,3,4], required=True,
                         help='scope channel monitoring the selected AWG output for timing alignment')
    capture.add_argument('--repeats', type=int, default=64)
    capture.add_argument('--settle', type=float, default=0.2)
    capture.add_argument('--wait', type=float, default=30.)
    capture.add_argument('--home', action='store_true', help='home after power-up; user zero offset remains applied')
    capture.add_argument('--dither-codes', type=int, default=3,
                         help='offset dither across this many ADC codes over the shots (0 = off)')
    update = sub.add_parser('step', help='write one bounded correction, with prominent warnings when rails are reached')
    update.add_argument('--session', required=True)
    update.add_argument('--capture', required=True)
    update.add_argument('--out-dir', required=True)
    update.add_argument('--f-cut', type=float, default=None,
                        help='change selected slow correction bandwidth for this and later steps')
    args = ap.parse_args()
    if args.action == 'calibrate':
        import numpy as np
        from eomilc.polarimetry import fit_fringe
        data = np.genfromtxt(args.sweep, delimiter=',', names=True)
        if not data.dtype.names or not {'hv_V','light_V'} <= set(data.dtype.names):
            ap.error('sweep CSV needs hv_V,light_V headers')
        if not np.isfinite(data['hv_V']).all() or not np.isfinite(data['light_V']).all() or not np.isfinite([args.angle,args.dark,args.v_pi_guess]).all() or args.v_pi_guess <= 0:
            ap.error('sweep and fit parameters must be finite, v-pi positive')
        cal = fit_fringe(data['hv_V'], data['light_V'], n_eom=1,
                         v_pi_guess=args.v_pi_guess, theta_a=np.deg2rad(args.angle), i_dark=args.dark)
        cal.note = 'HV units; only selected EOM swept. Stable illumination required.'
        with open(args.out, 'x') as out:
            json.dump(vars(cal), out, indent=2)
        print(args.out)
    elif args.action == 'init':
        if not args.voltage_state:
            ap.error('--voltage-state required for the first case')
        cals = {}
        for entry in args.cal:
            angle, path = entry.split('=', 1)
            if float(angle) in cals:
                ap.error('duplicate calibration angle')
            cals[float(angle)] = path
        state = initialize(args.voltage_state, cals,
                           Settings(**{k: getattr(args,k) for k in vars(Settings())}),
                           args.frf, args.frf_use, args.frf_max,
                           args.hv_for_90, args.target_zero_hv, args.angle_offset_deg)
        if args.channel and str(state['channel']) != args.channel:
            ap.error('channel differs from voltage state; choose the corresponding state for that EOM')
        print(save_iteration(args.out_dir, state))
        last['voltage_state'] = str(Path(args.voltage_state).resolve())
        last['channel'] = str(state['channel'])
    elif args.action == 'step':
        state = load_session(args.session)
        if args.f_cut is not None:
            settings = json.loads(str(state['settings_json']))
            settings['f_cut'] = args.f_cut
            state['settings_json'] = json.dumps(settings)
        state, result = step(state, args.capture)
        print(json.dumps(result.metrics, indent=2))
        for message in result.messages:
            print('WARNING: ' + message)
        print(save_iteration(args.out_dir, state))
    else:
        # Reuse the repo's scope adapter, simultaneous capture and timing check.
        import ilc_bench as bench
        from .ell14 import ELL14
        state = load_session(args.session)
        tuner = tuner_from_state(state)
        mon_ch = args.mon_ch or (3 if str(state['channel']) == 'EO1' else 4)
        if args.pd_ch is None:
            ap.error('--pd-ch required for the first capture')
        if len({args.pd_ch, mon_ch, args.drive_ch}) != 3:
            ap.error('PD, Trek monitor and drive scope channels must be distinct')
        if args.repeats < 2 or args.wait <= 0 or args.settle < 0:
            ap.error('need repeats >= 2, positive wait, nonnegative settle')
        # Located from this file, not the working directory: the panel and
        # the .bat start from the repo root, a shell may not.
        scope = bench.make_scope(bench.load_module(bench.find_scope_grab(str(Path(__file__).resolve().parents[2])), 'scope_grab'))
        try:
            scope.connect()
            bench.verify_alignment(scope, args.drive_ch, state['current'], state['t'], float(state['t_offset']), args.wait)
            def grab():
                # Offset-dithered: undithered, the per-code pattern is the
                # same in every shot, survives the mean and reads as light.
                cap = bench.capture_all(scope, [args.pd_ch, mon_ch], state['t'],
                                        float(state['t_offset']), repeats=args.repeats,
                                        wait_s=args.wait, settle=0., keep='both',
                                        points=bench.scopeio.scope_points_for(2.2*len(state['t'])),
                                        dither_codes=args.dither_codes)
                if cap.t_raw[0] > state['t'][0] or cap.t_raw[-1] < state['t'][-1]:
                    raise ValueError('scope record does not cover the full target time grid')
                for channel in (args.pd_ch, mon_ch):
                    scale = float(scope.try_get(f':CHANnel{channel}:SCALe'))
                    offset = float(scope.try_get(f':CHANnel{channel}:OFFSet'))
                    if not (scale > 0):
                        raise ValueError('invalid scope vertical scale')
                    raw = cap.raw[f'CH{channel}']
                    if abs(raw-offset).max() >= (3.95 - bench.dither_margin_div(args.dither_codes))*scale:
                        raise ValueError(f'scope CH{channel} is at/outside its acquisition window; fix clipping before fine-tuning')
                return cap[f'CH{args.pd_ch}'], cap[f'CH{mon_ch}']
            with ELL14(args.port, address=args.addr, zero_offset_deg=args.zero) as rot:
                if args.home:
                    rot.home()
                light, monitor, actual = capture_angles(rot, grab, sorted(tuner.calibrations), args.settle)
                save_capture(args.out, state, light, monitor, actual)
            print(args.out)
        finally:
            try:
                scope.run()          # leave the scope running, as hardware_test does
            finally:
                scope.close()
        last.update(pd_ch=args.pd_ch, port=args.port, zero=args.zero)
    PREFERENCES.write_text(json.dumps(last, indent=2) + '\n')


if __name__ == '__main__':
    main()
