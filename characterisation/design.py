import numpy as np, json
from scipy.signal import bilinear, lfilter, welch
IDX=json.load(open('cache/index.json'))

print("=== Ramp-rate design curve: 2nd-order Trek+EOM, worst-case fit (fn=2137 Hz, z=0.189) ===")
print("   (linear ramp of duration T, then hold; overshoot at the top corner)\n")
print(f"{'ramp T':>10s} {'overshoot %FS':>14s} {'settle to 0.1% (ms)':>20s}")
dt=2e-7
for fn,z,lab in ((2137,0.189,'19.2V drive'),(2981,0.238,'0.5V drive')):
    print(f"  -- {lab}: fn={fn} Hz, zeta={z} --")
    wn=2*np.pi*fn
    b,a=bilinear([wn**2],[1,2*z*wn,wn**2],fs=1/dt)
    for T in (2e-5,5e-5,1e-4,2e-4,5e-4,1e-3,2e-3,5e-3):
        n=int(20e-3/dt); t=np.arange(n)*dt
        x=np.clip(t/T,0,1)
        y=lfilter(b,a,x)
        ov=(y.max()-1)*100
        # settle
        bad=np.where(np.abs(y-1)>1e-3)[0]
        st=(t[bad[-1]]-T)*1e3 if len(bad) else 0
        print(f"{T*1e3:9.3f}ms {ov:14.2f} {st:20.3f}")

print("\n=== Actual commanded drive: max slope and resulting overshoot in the data ===")
for b in sorted(IDX):
    if 'trek monitor 19.2' not in b and 'trek monitor 2V' not in b: continue
    d=np.load(IDX[b]['npy']); t=d[:,0]; dt=float(np.median(np.diff(t)))
    for ci,co,tag in ((1,3,'X1'),(2,4,'X2')):
        v=d[:,ci]; N=int(50e-6/dt)//2*2+1
        s=np.convolve(v,np.ones(N)/N,mode='same')
        sl=np.gradient(s,dt)
        m=(t>0.2e-3)&(t<4e-3)
        pk=np.abs(sl[m]).max()
        # equivalent 10-90 rise time for this slope
        pp=np.ptp(v)
        print(f"  {b[-22:]:22s} {tag}: pp={pp:6.3f}V  max slope={pk/1e3:8.1f} V/ms  -> equiv 10-90 time = {0.8*pp/pk*1e3:6.3f} ms")

print("\n=== Noise spectral lines (0V-amplitude, 150 ms capture) ===")
b='AWG MKJ ramps before and after conditioning 0V ampl 10x time_20260821_011846'
d=np.load(IDX[b]['npy']); t=d[:,0]; fs=1/float(np.median(np.diff(t)))
for i,h in enumerate(IDX[b]['hdr'][1:],1):
    v=d[:,i]-d[:,i].mean()
    f,P=welch(v,fs,nperseg=1<<15)
    sel=(f>10)&(f<2000)
    ff,PP=f[sel],P[sel]
    k=np.argsort(PP)[::-1][:60]
    seen=[]
    for j in sorted(k):
        if any(abs(ff[j]-s)<15 for s in seen): continue
        seen.append(ff[j])
        if len(seen)<=5:
            print(f"  {h:28s} line at {ff[j]:7.1f} Hz  amp={np.sqrt(PP[j]*(ff[1]-ff[0]))*1e6:7.1f} uVrms")
    print()
