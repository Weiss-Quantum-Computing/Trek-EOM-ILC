import numpy as np, json
from scipy.linalg import solve_toeplitz
IDX=json.load(open('cache/index.json')); from eomlib import amp_of
def win(t,w): return (t>=w[0])&(t<=w[1])
def lv(t,v):
    b=np.median(np.r_[v[win(t,(-2.2e-3,-0.1e-3))],v[win(t,(10.8e-3,12.7e-3))]])
    return b,np.median(v[win(t,(4.3e-3,6.6e-3))])
def wiener(x,y,L,reg=1e-6):
    n=len(x); nf=1<<int(np.ceil(np.log2(2*n)))
    X=np.fft.rfft(x,nf); Y=np.fft.rfft(y,nf)
    rxx=np.fft.irfft(X*np.conj(X),nf)[:L].copy(); rxy=np.fft.irfft(Y*np.conj(X),nf)[:L]
    rxx[0]*=(1+reg); h=solve_toeplitz((rxx,rxx),rxy)
    return h,np.convolve(x,h)[:n]

L=1200
print("INL measured WITHIN a single capture (immune to scope range-to-range gain error)")
print("xf = input passed through best-fit linear dynamics; then y vs xf fitted with a straight line.\n")
print(f"{'kind':5s} {'amp':>6s} {'ch':3s} | {'INLrms %FS':>10s} {'INLpk %FS':>9s} | {'2nd-order coeff':>15s} {'up-vs-down split %FS':>20s}")
curves={}
for b in sorted(IDX):
    if '0V ampl' in b or 'Noise' in b or b.endswith('225344'): continue
    kind='cond' if 'conditioning' in b else ('mon' if 'trek monitor' in b else None)
    if kind is None: continue
    a=amp_of(b) or (0.5,0.5)
    d=np.load(IDX[b]['npy']); t=d[:,0]; dt=float(np.median(np.diff(t)))
    for ci,co,tag in ((1,3,'X1'),(2,4,'X2')):
        vin,vout=d[:,ci],d[:,co]
        bi,ti=lv(t,vin); bo,to=lv(t,vout)
        xn=(vin-bi)/(ti-bi); yn=(vout-bo)/(to-bo)
        m=(t>=-0.3e-3)&(t<=11.5e-3); x=xn[m]; y=yn[m]; tt=t[m]
        h,xf=wiener(x,y,L)
        x2=xf[L:]; y2=y[L:]; t2=tt[L:]
        # bin by level, separately for rising and falling halves
        edges=np.linspace(0.02,0.98,49); cen=0.5*(edges[1:]+edges[:-1])
        rise=t2< 5.3e-3; fall=t2>=5.3e-3
        def prof(msk):
            out=np.full(len(cen),np.nan)
            for i in range(len(cen)):
                s=msk&(x2>=edges[i])&(x2<edges[i+1])
                if s.sum()>30: out[i]=np.mean(y2[s]-x2[s])
            return out
        pr,pf=prof(rise),prof(fall)
        both=np.nanmean(np.vstack([pr,pf]),0)
        ok=~np.isnan(both)
        # remove residual linear term
        A=np.vstack([cen[ok],np.ones(ok.sum())]).T
        c,_,_,_=np.linalg.lstsq(A,both[ok],rcond=None)
        inl=both.copy(); inl[ok]-=A@c
        # quadratic coeff
        q=np.polyfit(cen[ok],both[ok],2)[0]
        split=np.nanmax(np.abs(pr-pf))*100 if np.isfinite(pr-pf).any() else np.nan
        amp=a[0] if tag=='X1' else a[1]
        print(f"{kind:5s} {amp:6.2f} {tag:3s} | {np.nanstd(inl)*100:10.4f} {np.nanmax(np.abs(inl))*100:9.4f} | {q*100:15.4f} {split:20.4f}")
        curves[f"{kind}|{amp}|{tag}"]=dict(cen=cen.tolist(),inl=(inl*100).tolist(),
                                           rise=(pr*100).tolist(),fall=(pf*100).tolist())
json.dump(curves,open('inl.json','w'))
