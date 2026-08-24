import numpy as np, json
R = json.load(open('results2.json'))
def S(k, ch, amin=0.4): return [q for q in R if q['kind'] == k and q['ch'] == ch and q['amp'] >= amin]

print("=== STAGE GAIN (least-squares, amplitudes >= 0.5 Vpp) ===")
G = {}
for k in ('cond', 'mon'):
    for ch in ('X1', 'X2'):
        g = np.array([q['gain_2p'] for q in S(k, ch)]); G[(k, ch)] = g
        a = np.array([q['amp'] for q in S(k, ch)])
        sl = np.polyfit(np.log10(a), g, 1)[0]
        print(f"  {k:4s} {ch}: {g.mean():.4f} +/- {g.std():.4f}  (n={len(g)}, "
              f"peak-to-peak spread {(g.max()-g.min())/g.mean()*100:.2f}%, "
              f"trend {sl/g.mean()*100:+.2f}% per decade of amplitude)")
print(f"\n  X1 vs X2 preconditioning gain mismatch : {(G[('cond','X1')].mean()/G[('cond','X2')].mean()-1)*100:+.2f}%")
print(f"  X1 vs X2 whole-chain gain mismatch     : {(G[('mon','X1')].mean()/G[('mon','X2')].mean()-1)*100:+.2f}%")

print("\n=== TREK 610E + EOM STAGE GAIN (monitor / Trek input), matched amplitudes ===")
tg = {}
for ch in ('X1', 'X2'):
    ci = {}
    for q in S('cond', ch): ci.setdefault(q['amp'], []).append(q['gain_2p'])
    mi = {}
    for q in S('mon', ch): mi.setdefault(q['amp'], []).append(q['gain_2p'])
    v = [np.mean(mi[a])/np.mean(ci[a]) for a in sorted(set(ci) & set(mi))]
    tg[ch] = np.array(v)
    print(f"  {ch}: " + "  ".join(f"{x:.4f}" for x in v) + f"   -> mean {np.mean(v):.4f} +/- {np.std(v):.4f}")
print(f"  Trek channel-to-channel mismatch: {(tg['X2'].mean()/tg['X1'].mean()-1)*100:+.1f}%")

print("\n=== RESIDUAL BUDGET (% of full swing, rms / peak), amplitudes >= 2 Vpp ===")
for k, lab in (('cond', 'preconditioning'), ('mon', 'whole chain')):
    q = [x for x in R if x['kind'] == k and x['amp'] >= 2]
    for f, n in (('rms0', 'gain+offset only'), ('rms1', '+ single pole'), ('rms2', '+ 2nd order')):
        pk = {'rms0': 'pk0', 'rms1': 'pk1', 'rms2': 'pk2'}[f]
        print(f"  {lab:16s} {n:20s}: rms {np.mean([x[f] for x in q]):.3f} %  "
              f"(range {min(x[f] for x in q):.3f}-{max(x[f] for x in q):.3f})   "
              f"peak {np.mean([x[pk] for x in q]):.3f} %")
    print(f"  {lab:16s} {'structured part left':20s}: rms {np.mean([x['rmsw_sm'] for x in q]):.3f} %  "
          f"peak {np.mean([x['pkw_sm'] for x in q]):.3f} %\n")

print("=== RESONANCE vs DRIVE AMPLITUDE (whole chain) ===")
for ch in ('X1', 'X2'):
    ba = {}
    for q in R:
        if q['kind'] == 'mon' and q['ch'] == ch: ba.setdefault(q['amp'], []).append(q['fn'])
    aa = sorted(ba)
    print(f"  {ch}: " + "  ".join(f"{a:.1f}V:{np.mean(ba[a]):.0f}Hz" for a in aa))
    lo, hi = np.median(ba[aa[0]]), np.mean(ba[aa[-1]])
    print(f"      {lo:.0f} Hz at {aa[0]} Vpp -> {hi:.0f} Hz at {aa[-1]} Vpp = {(hi/lo-1)*100:+.1f}%"
          f"   implied load-C increase {(lo/hi)**2:.2f}x")
z = [q['zeta'] for q in R if q['kind'] == 'mon']
print(f"  zeta = {np.mean(z):.3f} +/- {np.std(z):.3f}  ->  Q = {1/(2*np.mean(z)):.2f},"
      f"  ring decay 1/(zeta*wn) = {1/(np.mean(z)*2*np.pi*2500)*1e6:.0f} us at 2.5 kHz")

t = [q['tau_us'] for q in R if q['kind'] == 'cond' and q['amp'] >= 1]
print(f"\n=== PRECONDITIONING POLE ===\n  tau = {np.mean(t):.2f} +/- {np.std(t):.2f} us"
      f"  ->  f-3dB = {1/(2*np.pi*np.mean(t)*1e-6)/1e3:.0f} kHz  "
      f"({np.mean([q['tau_us'] for q in R if q['kind']=='mon'])/np.mean(t):.0f}x faster than the Trek stage)")
