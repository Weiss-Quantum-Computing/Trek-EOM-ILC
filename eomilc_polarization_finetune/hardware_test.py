"""Live ELL14/scope trace test, followed by optional polarization conversion.

No AWG writes and no ILC step. Progress and each trace file are emitted as
they arrive so the panel can display successful angles even if a later move
or acquisition fails.
"""
import argparse
import json
from pathlib import Path

import numpy as np

from .core import vector
from .measurement_test import reconstruct
from .workflow import load_session
from eomilc.polarimetry import FringeCal


def acquire(scope, rotator, state, angles, pd_ch, mon_ch, folder,
            repeats=16, settle=0.2, wait=30., on_progress=None, capture_function=None,
            dither_codes=3):
    """Capture each requested angle and persist its light/monitor shot stacks."""
    native_capture = capture_function is None
    if native_capture:
        import ilc_bench as bench
        capture_function = bench.capture_all
    angles = tuple(map(float,angles))
    if not angles or len(set(angles))!=len(angles) or not np.isfinite(angles).all():
        raise ValueError('specify distinct finite analyzer angles')
    if pd_ch==mon_ch or pd_ch not in (1,2,3,4) or mon_ch not in (1,2,3,4):
        raise ValueError('light and monitor scope channels must be distinct and between 1 and 4')
    if repeats<2 or not np.isfinite([settle,wait]).all() or settle<0 or wait<=0:
        raise ValueError('need repeats >=2, nonnegative settling and positive trigger timeout')
    t = vector(state['t'],'time grid')
    points = bench.scopeio.scope_points_for(2.2*len(t)) if native_capture else None
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    if any(folder.iterdir()):
        raise FileExistsError('hardware-test output folder must be empty')
    notify = on_progress or (lambda event:None)
    light,monitor,actual = {},{},{}
    for index,angle in enumerate(angles):
        notify(dict(kind='moving',angle=angle))
        got = rotator.goto(angle,tol=.05,settle=settle)
        if not np.isfinite(got) or abs((got-angle+180)%360-180)>.05:
            raise ValueError(f'ELL14 did not land at requested {angle:g} deg')
        notify(dict(kind='capturing',angle=angle,actual=got))
        # Offset-dithered: the scope's per-code pattern is identical in every
        # undithered shot, so it survives the mean and reads as light.
        cap = capture_function(scope,[pd_ch,mon_ch],t,float(state.get('t_offset',0.)),
                               repeats=repeats,wait_s=wait,settle=0.,keep='both',points=points,
                               dither_codes=dither_codes)
        if cap.t_raw[0]>t[0] or cap.t_raw[-1]<t[-1]:
            raise ValueError('scope trace does not cover the full waveform')
        clipping = []
        for ch in (pd_ch,mon_ch):
            scale = float(scope.try_get(f':CHANnel{ch}:SCALe'))
            offset = float(scope.try_get(f':CHANnel{ch}:OFFSet'))
            if not np.isfinite([scale,offset]).all() or scale<=0:
                raise ValueError(f'invalid scope CH{ch} vertical settings')
            raw = np.asarray(cap.raw[f'CH{ch}'],float)
            if not np.isfinite(raw).all():
                raise ValueError(f'nonfinite scope CH{ch} samples')
            if np.max(np.abs(raw-offset))>=(3.95-_margin(dither_codes))*scale:
                clipping.append(ch)
        light[angle] = np.asarray(cap[f'CH{pd_ch}'],float)
        monitor[angle] = np.asarray(cap[f'CH{mon_ch}'],float)
        if light[angle].shape!=(repeats,len(t)) or monitor[angle].shape!=light[angle].shape or not np.isfinite(light[angle]).all() or not np.isfinite(monitor[angle]).all():
            raise ValueError('scope returned mismatched or invalid repeated traces')
        actual[angle] = got
        path = folder/f'angle_{index:02d}.npz'
        np.savez(path,t=t,requested_angle=angle,actual_angle=got,light=light[angle],
                 monitor=monitor[angle],t_raw=cap.t_raw,
                 pd_channel=pd_ch,monitor_channel=mon_ch,eom_channel=state['channel'],
                 light_raw=cap.raw[f'CH{pd_ch}'],monitor_raw=cap.raw[f'CH{mon_ch}'])
        notify(dict(kind='trace',angle=angle,actual=got,path=str(path.resolve()),clipping_channels=clipping))
        if clipping:
            raise ValueError(f'scope channel(s) {clipping} appear clipped; displayed traces retained, angle conversion stopped')
    np.savez(folder/'hardware_traces.npz',t=t,angles=angles,
             actual_angles=[actual[a] for a in angles],
             light=np.stack([light[a] for a in angles]),monitor=np.stack([monitor[a] for a in angles]))
    return light,monitor,actual


def _margin(codes):
    """Divisions the dither moves a shot's window from the restored offset."""
    from eomilc.scope import ADC_CODE_PER_VDIV
    return 0.5*max(int(codes or 0),0)*ADC_CODE_PER_VDIV


def convert(folder,light,calibrations,t):
    measurement = reconstruct(light,calibrations)
    # Polarization is modulo 180 deg; continuity unwraps each finite segment
    # without using the voltage target to choose the measured angle.
    angle = measurement['angle_deg'].copy()
    valid = np.isfinite(angle)
    for indices in np.split(np.flatnonzero(valid),np.flatnonzero(np.diff(np.flatnonzero(valid))>1)+1):
        if len(indices):
            angle[indices] = np.rad2deg(.5*np.unwrap(2*np.deg2rad(angle[indices])))
    path = Path(folder)/'polarization_angle.npz'
    np.savez(path,t=t,angle_deg=angle,angle_sem_deg=measurement['angle_sem_deg'],
             contrast_radius=measurement['contrast_radius'],valid=measurement['valid'])
    np.savetxt(Path(folder)/'polarization_angle.csv',np.column_stack(
        [np.asarray(t)*1e6,angle,measurement['angle_sem_deg'],measurement['contrast_radius']]),
        delimiter=',',header='time_us,polarization_deg,angle_sem_deg,contrast_radius',comments='')
    return path


def main():
    ap = argparse.ArgumentParser(description='Move ELL14 to discrete angles, acquire live light traces, optionally reconstruct polarization')
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument('--session')
    source.add_argument('--voltage-state',help='allows raw hardware test before optical Init/calibration')
    ap.add_argument('--angles',help='comma-separated analyzer angles; default session angles or 0,45')
    ap.add_argument('--cal',action='append',default=[],metavar='DEGREES=FRINGE.JSON')
    ap.add_argument('--port',default='auto')
    ap.add_argument('--addr',default='0')
    ap.add_argument('--zero',type=float,default=0.)
    ap.add_argument('--home',action='store_true')
    ap.add_argument('--pd-ch',type=int,choices=[1,2,3,4],required=True)
    ap.add_argument('--mon-ch',type=int,choices=[1,2,3,4])
    ap.add_argument('--drive-ch',type=int,choices=[1,2,3,4],required=True)
    ap.add_argument('--repeats',type=int,default=16)
    ap.add_argument('--settle',type=float,default=.2)
    ap.add_argument('--wait',type=float,default=30.)
    ap.add_argument('--dither-codes',type=int,default=3,
                    help='offset dither across this many ADC codes over the shots (0 = off)')
    ap.add_argument('--out-dir',required=True)
    args = ap.parse_args()
    if args.session:
        state = load_session(args.session)
    else:
        with np.load(args.voltage_state,allow_pickle=True) as z:
            state = {k:z[k] for k in ('t','dt','u','channel')}
            state['current'] = state.pop('u')
            state['t_offset'] = z['t_offset'] if 't_offset' in z else 0.
    calibrations = {float(a):FringeCal(**c) for a,c in
                    json.loads(str(state.get('calibrations_json','{}'))).items()}
    for entry in args.cal:
        angle,path = entry.split('=',1)
        calibrations[float(angle)] = FringeCal.load(path)
    angles = [float(x) for x in args.angles.split(',')] if args.angles else sorted(calibrations) or [0.,45.]
    mon_ch = args.mon_ch or (3 if str(state['channel'])=='EO1' else 4)
    if len({args.pd_ch,mon_ch,args.drive_ch})!=3:
        ap.error('PD, monitor and AWG-output scope channels must be distinct')
    def notify(event):
        print('EVENT '+json.dumps(event),flush=True)
    import ilc_bench as bench
    from .ell14 import ELL14
    scope = bench.make_scope(bench.load_module(bench.find_scope_grab(str(Path(__file__).resolve().parents[2])),'scope_grab'))
    try:
        scope.connect()
        bench.verify_alignment(scope,args.drive_ch,state['current'],state['t'],float(state['t_offset']),args.wait)
        with ELL14(args.port,address=args.addr,zero_offset_deg=args.zero) as rot:
            if args.home:
                rot.home()
            light,_,_ = acquire(scope,rot,state,angles,args.pd_ch,mon_ch,args.out_dir,
                                args.repeats,args.settle,args.wait,notify,
                                dither_codes=args.dither_codes)
        if len(light)>=2 and all(a in calibrations for a in angles):
            path = convert(args.out_dir,light,{a:calibrations[a] for a in angles},state['t'])
            notify(dict(kind='conversion',path=str(path.resolve())))
        else:
            notify(dict(kind='note',message='Traces captured. Supply fringe calibrations for every angle and at least two complementary settings to convert light to polarization.'))
        notify(dict(kind='done',folder=str(Path(args.out_dir).resolve())))
    finally:
        try:
            scope.run()
        finally:
            scope.close()


if __name__=='__main__':
    main()
