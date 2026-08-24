"""Re-do the core numbers with a least-squares affine (gain+offset) fit instead of
plateau-median normalisation, which is biased by up to half an ADC code."""
import numpy as np, json
from scipy.signal import lfilter, bilinear
from scipy.optimize import minimize, minimize_scalar
from scipy.linalg import solve_toeplitz
IDX = json.load(open('cache/index.json'))
from eomlib import amp_of

W_ACT = (-0.30e-3, 11.00e-3)
W_TOP = (4.30e-3, 6.60e-3)
W_UP  = (0.15e-3, 4.10e-3)
W_DN  = (6.70e-3, 10.60e-3)

def win(t, w): return (t >= w[0]) & (t <= w[1])

def affine(f, y, m):
    """least-squares a,b for y ~ a*f + b over mask m; returns a,b,resid(full length)"""
    A = np.vstack([f[m], np.ones(m.sum())]).T
    c, *_ = np.linalg.lstsq(A, y[m], rcond=None)
    return c[0], c[1], y - (c[0]*f + c[1])

def onepole(x, tau, dt):
    if tau <= 1e-12: return x.copy()
    a = dt/(tau+dt)
    return lfilter([a], [1, -(1-a)], x, zi=[(1-a)*x[0]])[0]

def sim2(x, fn, z, dt):
    wn = 2*np.pi*fn
    b, a = bilinear([wn**2], [1, 2*z*wn, wn**2], fs=1/dt)
    return lfilter(b, a, x)

def wiener(x, y, L=1200, reg=1e-6):
    n = len(x); nf = 1 << int(np.ceil(np.log2(2*n)))
    X = np.fft.rfft(x, nf); Y = np.fft.rfft(y, nf)
    rxx = np.fft.irfft(X*np.conj(X), nf)[:L].copy()
    rxy = np.fft.irfft(Y*np.conj(X), nf)[:L]
    rxx[0] *= (1+reg)
    return np.convolve(x, solve_toeplitz((rxx, rxx), rxy))[:n]

rows = []
for b in sorted(IDX):
    if 'trek monitor' not in b and 'conditioning' not in b: continue
    if '0V ampl' in b: continue
    kind = 'cond' if 'conditioning' in b else 'mon'
    a = amp_of(b) or (0.5, 0.5)
    d = np.load(IDX[b]['npy'])
    D = 8 if len(d) > 90000 else 1            # decimate: dynamics of interest are <10 kHz
    d = d[::D]
    t = d[:, 0]; dt = float(np.median(np.diff(t)))
    m = win(t, W_ACT)
    for ci, co, tag in ((1, 3, 'X1'), (2, 4, 'X2')):
        x, y = d[:, ci], d[:, co]
        # --- gain+offset only ---
        g0, o0, r0 = affine(x, y, m)
        fs_ = np.ptp(x[m])                      # full swing in input volts
        # --- gain+offset+1-pole ---
        def c1(lt):
            _, _, r = affine(onepole(x, 10**lt, dt), y, m)
            return np.sqrt(np.mean(r[m]**2))
        r1 = minimize_scalar(c1, bounds=(-8, -3), method='bounded')
        tau = 10**r1.x
        g1, o1, res1 = affine(onepole(x, tau, dt), y, m)
        # --- gain+offset+2nd order ---
        def c2(p):
            fn = np.exp(p[0]); z = 1/(1+np.exp(-p[1]))*1.5
            _, _, r = affine(sim2(x, fn, z, dt), y, m)
            return np.sqrt(np.mean(r[m]**2))
        best = None
        for f0 in (2500, 20000):
            for z0 in (0.25, 0.9):
                rr = minimize(c2, [np.log(f0), np.log(z0/1.5/(1-z0/1.5))], method='Nelder-Mead',
                              options=dict(xatol=1e-4, fatol=1e-10, maxiter=400))
                if best is None or rr.fun < best.fun: best = rr
        fn = np.exp(best.x[0]); zt = 1/(1+np.exp(-best.x[1]))*1.5
        g2, o2, res2 = affine(sim2(x, fn, zt, dt), y, m)
        # --- best arbitrary linear filter ---
        xw = wiener(x[m], y[m], L=1200//D if D>1 else 1200); L = 1200//D if D>1 else 1200
        resw = (y[m] - xw)[L:]
        def pct(r, w=None, arr_t=None):
            if w is None: rr = r[m]
            else: rr = r[win(t, w) & m]
            return np.sqrt(np.mean(rr**2))/fs_*100, np.max(np.abs(rr))/fs_*100
        N = max(3,int(200e-6/dt)//2*2+1)
        smw = np.convolve(resw, np.ones(N)/N, mode='same')[N:-N]
        rows.append(dict(
            kind=kind, ch=tag, amp=(a[0] if tag == 'X1' else a[1]),
            gain_lin=float(g0), gain_1p=float(g1), gain_2p=float(g2),
            tau_us=float(tau*1e6), fn=float(fn), zeta=float(zt),
            rms0=pct(r0)[0], pk0=pct(r0)[1],
            rms1=pct(res1)[0], pk1=pct(res1)[1],
            rms2=pct(res2)[0], pk2=pct(res2)[1],
            rmsw=float(np.sqrt(np.mean(resw**2))/fs_*100),
            rmsw_sm=float(np.sqrt(np.mean(smw**2))/fs_*100),
            pkw_sm=float(np.max(np.abs(smw))/fs_*100),
            hold=pct(res2, W_TOP)[0], up=pct(res2, W_UP)[0], dn=pct(res2, W_DN)[0]))
json.dump(rows, open('results2.json', 'w'), indent=1)

for kind, lab in (('cond', 'AWG -> TREK INPUT   (preconditioning only)'),
                  ('mon',  'AWG -> TREK MONITOR (preconditioning + Trek 610E + EOM)')):
    print(f"\n===== {lab} =====")
    print(f"{'amp':>6s} {'ch':>3s} {'gain':>8s} {'tau_us':>7s} {'fn_Hz':>8s} {'zeta':>6s} |"
          f" {'rms G':>7s} {'rms G+1p':>8s} {'rms G+2p':>8s} {'pk G+2p':>8s} |"
          f" {'rms LTI':>7s} {'struct':>7s} {'pk struct':>9s}")
    for r in sorted([q for q in rows if q['kind'] == kind], key=lambda q: (q['amp'], q['ch'])):
        print(f"{r['amp']:6.2f} {r['ch']:>3s} {r['gain_2p']:8.5f} {r['tau_us']:7.2f} {r['fn']:8.0f}"
              f" {r['zeta']:6.3f} | {r['rms0']:7.3f} {r['rms1']:8.3f} {r['rms2']:8.3f} {r['pk2']:8.3f}"
              f" | {r['rmsw']:7.3f} {r['rmsw_sm']:7.3f} {r['pkw_sm']:9.3f}")
