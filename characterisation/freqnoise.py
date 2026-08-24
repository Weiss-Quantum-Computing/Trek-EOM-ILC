import numpy as np, json
from scipy.signal import csd, welch
IDX=json.load(open('cache/index.json')); from eomlib import amp_of
def win(t,w): return (t>=w[0])&(t<=w[1])
def lv(t,v):
    b=np.median(np.r_[v[win(t,(-2.2e-3,-0.1e-3))],v[win(t,(10.8e-3,12.7e-3))]])
    return b,np.median(v[win(t,(4.3e-3,6.6e-3))])

print("======== FREQUENCY RESPONSE (single-shot, drive-limited SNR) ========")
print(f"{'kind':5s} {'amp':>6s} {'ch':3s} | {'|H| @100Hz':>10s} {'@500Hz':>8s} {'@1k':>8s} {'@2k':>8s} {'@5k':>8s} | {'f_-3dB (1-pole)':>15s} {'phase@1k deg':>12s}")
fr={}
for b in sorted(IDX):
    if '0V ampl' in b or 'Noise' in b or b.endswith('225344'): continue
    kind='cond' if 'conditioning' in b else ('mon' if 'trek monitor' in b else None)
    if kind is None: continue
    a=amp_of(b) or (0.5,0.5)
    d=np.load(IDX[b]['npy']); t=d[:,0]; dt=float(np.median(np.diff(t))); fs=1/dt
    m=(t>=-0.3e-3)&(t<=11.5e-3)
    for ci,co,tag in ((1,3,'X1'),(2,4,'X2')):
        bi,ti=lv(t,d[:,ci]); bo,to=lv(t,d[:,co])
        x=((d[:,ci]-bi)/(ti-bi))[m]; y=((d[:,co]-bo)/(to-bo))[m]
        nper=len(x)
        f,Pxx=welch(x,fs,nperseg=nper,noverlap=0,window='hann',detrend='constant')
        _,Pxy=csd(x,y,fs,nperseg=nper,noverlap=0,window='hann',detrend='constant')
        H=Pxy/Pxx
        def at(fq):
            i=np.argmin(np.abs(f-fq)); return H[i]
        amp=a[0] if tag=='X1' else a[1]
        gm=[np.abs(at(q)) for q in (100,500,1000,2000,5000)]
        ph=np.angle(at(1000),deg=True)
        # -3dB from 1-pole fit to phase at 1k
        tau=-np.tan(np.deg2rad(ph))/(2*np.pi*1000)
        f3=1/(2*np.pi*tau) if tau>0 else np.nan
        print(f"{kind:5s} {amp:6.2f} {tag:3s} | {gm[0]:10.4f} {gm[1]:8.4f} {gm[2]:8.4f} {gm[3]:8.4f} {gm[4]:8.4f} | {f3:15.0f} {ph:12.2f}")
        sel=(f>20)&(f<20000)
        fr[f"{kind}|{amp}|{tag}"]=dict(f=f[sel][::3].tolist(),mag=np.abs(H)[sel][::3].tolist(),
                                       ph=np.angle(H,deg=True)[sel][::3].tolist())
json.dump(fr,open('freq.json','w'))

print("\n======== NOISE (drive at 0 V amplitude / dedicated noise capture) ========")
for b in sorted(IDX):
    if '0V ampl' not in b and 'Noise' not in b: continue
    d=np.load(IDX[b]['npy']); t=d[:,0]; dt=float(np.median(np.diff(t))); fs=1/dt
    hdr=IDX[b]['hdr']
    print(f"\n  {b}   (fs={fs/1e6:.2f} MSa/s, span={ (t[-1]-t[0])*1e3:.0f} ms)")
    cols={}
    for i,h in enumerate(hdr[1:],1):
        v=d[:,i]-d[:,i].mean(); cols[h]=v
        f,P=welch(v,fs,nperseg=min(len(v),1<<14))
        band=lambda lo,hi:np.sqrt(np.trapezoid(P[(f>=lo)&(f<hi)],f[(f>=lo)&(f<hi)]))*1e3
        print(f"    {h:30s} rms={v.std()*1e3:7.3f} mV  pk-pk={np.ptp(v)*1e3:7.3f} mV | "
              f"<1k={band(1,1e3):6.3f}  1-10k={band(1e3,1e4):6.3f}  10-100k={band(1e4,1e5):6.3f}  >100k={band(1e5,fs/2):6.3f} mVrms")
    ks=list(cols)
    for i in range(len(ks)):
        for j in range(i+1,len(ks)):
            c=np.corrcoef(cols[ks[i]],cols[ks[j]])[0,1]
            if abs(c)>0.2: print(f"      corr({ks[i][:12]},{ks[j][:12]}) = {c:+.3f}")
