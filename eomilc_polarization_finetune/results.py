"""Read completed voltage ILC results without modifying their source files."""
from pathlib import Path
import numpy as np
from eomilc.config import CHANNELS
from .core import vector


def read_results(path):
    path = Path(path).resolve()
    with np.load(path,allow_pickle=True) as z:
        required = {'t','u','target','dt','channel','gain','full_scale'}
        missing = required-set(z.files)
        if missing:
            raise ValueError('Choose the completed ILC .state.npz file. Missing: '+', '.join(sorted(missing)))
        channel = str(z['channel'])
        if channel not in ('EO1','EO2'):
            raise ValueError('This follow-up supports EO1 and EO2 results.')
        t = vector(z['t'],'ILC time grid')
        drive = vector(z['u'],'final ILC drive',len(t))
        target = vector(z['target'],'original voltage target',len(t))
        dt = float(z['dt'])
        if not np.isfinite(dt) or dt<=0 or not np.allclose(np.diff(t),dt,rtol=1e-7,atol=1e-12):
            raise ValueError('ILC results must have a uniform finite time grid.')
        iteration = int(z['iteration']) if 'iteration' in z else 0
        history = list(z['history']) if 'history' in z else []
        last = history[-1] if history and isinstance(history[-1],dict) else {}
        model = str(z['model']) if 'model' in z else last.get('model','recorded voltage plant')
        frf = str(z['frf_path']) if 'frf_path' in z else ''
        if frf and not Path(frf).exists():
            # Relocated states may record a former absolute location.
            name = frf.replace('\\','/').split('/')[-1]
            candidates = [path.parent/name,Path(__file__).resolve().parent.parent/'run'/name]
            frf = str(next((p for p in candidates if p.exists()),Path(frf)))
        return dict(path=str(path),channel=channel,iteration=iteration,
                    name=str(z['name']) if 'name' in z else path.stem,
                    t=t,drive=drive,target_hv=target*CHANNELS[channel].mon_scale,dt=dt,
                    full_scale=float(z['full_scale']),last_metrics=last,model=model,
                    frf_path=frf,frf_use=float(z['frf_use']) if 'frf_use' in z else 15000.,
                    frf_max=float(z['frf_max']) if 'frf_max' in z else 22000.,
                    hv_for_90=CHANNELS[channel].v90_hv)


def summary(result):
    message = f"{result['channel']} · completed voltage iteration {result['iteration']} · {len(result['t']):,} points · {result['dt']*1e6:g} µs grid · {result['model']}"
    last = result['last_metrics']
    if 'rms_err_hv' in last:
        message += f"\nLast recorded monitor error: {last['rms_err_hv']:.3g} V RMS"
    if 'peak_err_hv' in last:
        message += f"; {last['peak_err_hv']:.3g} V peak"
    return message
