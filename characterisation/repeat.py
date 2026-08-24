"""Repeatability test: three 0.5 Vpp monitor captures taken minutes apart.
If the leftover 'structured' residual is real circuit behaviour it repeats;
if it is per-capture ADC quantisation it does not."""
import numpy as np, json
from scipy.signal import bilinear, lfilter
IDX = json.load(open('cache/index.json'))

reps = ['AWG MKJ ramps vs trek monitor_20260820_232535',
        'AWG MKJ ramps vs trek monitor_20260820_232640',
        'AWG MKJ ramps vs trek monitor_20260820_233506']

def win(t, w): return (t >= w[0]) & (t <= w[1])
def sim2(x, fn, z, dt):
    wn = 2*np.pi*fn; b, a = bilinear([wn**2], [1, 2*z*wn, wn**2], fs=1/dt); return lfilter(b, a, x)
def affine(f, y, m):
    A = np.vstack([f[m], np.ones(m.sum())]).T
    c, *_ = np.linalg.lstsq(A, y[m], rcond=None); return y - (c[0]*f + c[1])

R = {q['kind']+q['ch']+str(round(q['amp'], 2)): q for q in json.load(open('results2.json'))}

for ci, co, tag, fn, z in ((1, 3, 'X1', 3003, 0.247), (2, 4, 'X2', 2451, 0.215)):
    curves = []
    for b in reps:
        d = np.load(IDX[b]['npy'])[::8]
        t = d[:, 0]; dt = float(np.median(np.diff(t))); m = win(t, (-0.3e-3, 11e-3))
        r = affine(sim2(d[:, ci], fn, z, dt), d[:, co], m)
        N = max(3, int(200e-6/dt)//2*2+1)
        s = np.convolve(r, np.ones(N)/N, mode='same')
        curves.append(s[m][N:-N]/np.ptp(d[:, ci][m])*100)
    L = min(len(c) for c in curves); C = np.array([c[:L] for c in curves])
    indiv = C.std(axis=1).mean()
    common = C.mean(axis=0)
    scatter = np.sqrt(np.mean(np.var(C, axis=0)))
    print(f"{tag}:  each capture's structured residual = {indiv:.3f} % rms")
    print(f"     common to all three (real, repeatable) = {np.std(common):.3f} % rms")
    print(f"     scatter between captures (not real)    = {scatter:.3f} % rms")
    cc = np.corrcoef(C)
    print(f"     pairwise correlation between captures  = {cc[0,1]:.2f}, {cc[0,2]:.2f}, {cc[1,2]:.2f}")
    print(f"     -> {'REPEATABLE: real circuit behaviour' if cc[0,1]>0.6 else 'NOT repeatable: measurement floor, treat as an upper bound'}\n")

# same test on the preconditioning, using the two 0.5 Vpp captures at DIFFERENT V/div
print("Preconditioning cross-check: same 0.5 Vpp drive captured at two different V/div settings")
pair = ['AWG MKJ ramps before and after conditioning .5V ampl_20260821_011352',
        'AWG MKJ ramps before and after conditioning_20260820_232003']
for ci, co, tag in ((1, 3, 'X1'), (2, 4, 'X2')):
    cs = []
    for b in pair:
        d = np.load(IDX[b]['npy'])[::8]
        t = d[:, 0]; dt = float(np.median(np.diff(t))); m = win(t, (-0.3e-3, 11e-3))
        a1 = dt/(4.2e-6+dt)
        f = lfilter([a1], [1, -(1-a1)], d[:, ci], zi=[(1-a1)*d[0, ci]])[0]
        r = affine(f, d[:, co], m)
        N = max(3, int(200e-6/dt)//2*2+1)
        cs.append(np.convolve(r, np.ones(N)/N, mode='same')[m][N:-N]/np.ptp(d[:, ci][m])*100)
    L = min(len(c) for c in cs); A = np.array([c[:L] for c in cs])
    print(f"  {tag}: rms {A.std(axis=1)[0]:.3f} / {A.std(axis=1)[1]:.3f} %,  correlation {np.corrcoef(A)[0,1]:+.2f}"
          f"  -> {'repeatable' if np.corrcoef(A)[0,1]>0.6 else 'NOT repeatable (measurement floor)'}")
