"""Final quotable numbers: residual after gain+offset+2nd-order, both raw and
200 us smoothed (= the reproducible waveform error)."""
import numpy as np, json
from scipy.signal import bilinear, lfilter
IDX = json.load(open('cache/index.json'))
R = json.load(open('results2.json'))
def win(t, w): return (t >= w[0]) & (t <= w[1])
def sim2(x, fn, z, dt):
    wn = 2*np.pi*fn; b, a = bilinear([wn**2], [1, 2*z*wn, wn**2], fs=1/dt); return lfilter(b, a, x)
def onepole(x, tau, dt):
    a = dt/(tau+dt); return lfilter([a], [1, -(1-a)], x, zi=[(1-a)*x[0]])[0]
def affine(f, y, m):
    A = np.vstack([f[m], np.ones(m.sum())]).T
    c, *_ = np.linalg.lstsq(A, y[m], rcond=None); return y - (c[0]*f + c[1])
from eomlib import amp_of

key = {}
for q in R: key[(q['kind'], q['ch'], round(q['amp'], 2))] = q
out = []
for b in sorted(IDX):
    if 'trek monitor' not in b and 'conditioning' not in b: continue
    if '0V ampl' in b: continue
    kind = 'cond' if 'conditioning' in b else 'mon'
    a = amp_of(b) or (0.5, 0.5)
    d = np.load(IDX[b]['npy'])[::8]
    t = d[:, 0]; dt = float(np.median(np.diff(t))); m = win(t, (-0.3e-3, 11e-3))
    N = max(3, int(200e-6/dt)//2*2+1)
    for ci, co, tag in ((1, 3, 'X1'), (2, 4, 'X2')):
        amp = a[0] if tag == 'X1' else a[1]
        q = key[(kind, tag, round(amp, 2))]
        x, y = d[:, ci], d[:, co]; fs_ = np.ptp(x[m])
        mdl = sim2(x, q['fn'], q['zeta'], dt) if kind == 'mon' else onepole(x, q['tau_us']*1e-6, dt)
        r0 = affine(x, y, m); r = affine(mdl, y, m)
        s0 = np.convolve(r0, np.ones(N)/N, mode='same')
        s = np.convolve(r, np.ones(N)/N, mode='same')
        idx = np.where(m)[0][N:-N]
        out.append(dict(kind=kind, ch=tag, amp=amp,
                        g_rms=np.std(s0[idx])/fs_*100, g_pk=np.max(np.abs(s0[idx]-s0[idx].mean()))/fs_*100,
                        m_rms=np.std(s[idx])/fs_*100, m_pk=np.max(np.abs(s[idx]-s[idx].mean()))/fs_*100))
json.dump(out, open('final.json', 'w'))

print("Reproducible waveform error (200 us smoothed, % of full swing)")
print(f"{'':22s} {'gain+offset only':>22s}   {'+ fitted dynamics':>22s}")
print(f"{'':22s} {'rms':>10s} {'peak':>10s}   {'rms':>10s} {'peak':>10s}")
for kind, lab in (('cond', 'preconditioning'), ('mon', 'whole chain')):
    for ch in ('X1', 'X2'):
        q = [z for z in out if z['kind'] == kind and z['ch'] == ch and z['amp'] >= 2]
        print(f"{lab+' '+ch:22s} {np.mean([z['g_rms'] for z in q]):10.3f} {np.mean([z['g_pk'] for z in q]):10.3f}"
              f"   {np.mean([z['m_rms'] for z in q]):10.3f} {np.mean([z['m_pk'] for z in q]):10.3f}")
print("\nper-capture detail, whole chain:")
for z in sorted([q for q in out if q['kind'] == 'mon'], key=lambda q: (q['ch'], q['amp'])):
    print(f"   {z['ch']} {z['amp']:5.1f} Vpp:  gain-only {z['g_rms']:.3f} rms / {z['g_pk']:.3f} pk"
          f"   ->  after 2nd order {z['m_rms']:.3f} rms / {z['m_pk']:.3f} pk")
