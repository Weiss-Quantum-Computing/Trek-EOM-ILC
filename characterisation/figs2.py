import numpy as np, json, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.signal import bilinear, lfilter, welch
from scipy.linalg import solve_toeplitz
plt.rcParams.update({'font.size':9,'axes.grid':True,'grid.alpha':.25,'figure.facecolor':'white',
                     'axes.titlesize':10,'axes.labelsize':9,'legend.fontsize':7.5,
                     'axes.edgecolor':'#555','text.color':'#111','axes.labelcolor':'#111'})
IDX=json.load(open('cache/index.json'))
R=json.load(open('results2.json'))

def win(t,w): return (t>=w[0])&(t<=w[1])
def affine(f,y,m):
    A=np.vstack([f[m],np.ones(m.sum())]).T
    c,*_=np.linalg.lstsq(A,y[m],rcond=None)
    return y-(c[0]*f+c[1])
def onepole(x,tau,dt):
    a=dt/(tau+dt); return lfilter([a],[1,-(1-a)],x,zi=[(1-a)*x[0]])[0]
def sim2(x,fn,z,dt):
    wn=2*np.pi*fn; b,a=bilinear([wn**2],[1,2*z*wn,wn**2],fs=1/dt); return lfilter(b,a,x)
def wiener(x,y,L=1200,reg=1e-6):
    n=len(x); nf=1<<int(np.ceil(np.log2(2*n)))
    X=np.fft.rfft(x,nf); Y=np.fft.rfft(y,nf)
    rxx=np.fft.irfft(X*np.conj(X),nf)[:L].copy(); rxy=np.fft.irfft(Y*np.conj(X),nf)[:L]
    rxx[0]*=(1+reg); return np.convolve(x,solve_toeplitz((rxx,rxx),rxy))[:n]
def smooth(v,dt,w=200e-6):
    N=max(3,int(w/dt)//2*2+1); return np.convolve(v,np.ones(N)/N,mode='same'),N

# ================= FIG 1 =================
fig=plt.figure(figsize=(13,8.6))
bc='AWG MKJ ramps before and after conditioning 19.2 and 17.6V ampl_20260821_010640'
bm='AWG MKJ ramps vs trek monitor 19.2 and 17.6V amp_20260821_001057'
dc=np.load(IDX[bc]['npy']); dm=np.load(IDX[bm]['npy'])
t=dc[:,0]; dt=float(np.median(np.diff(t)))

ax=fig.add_subplot(2,2,1)
ax.plot(t*1e3,dc[:,1],lw=1,color='#B58900',label='AWG Ch1  -  9.65 Vpp')
ax.plot(t*1e3,dc[:,3],lw=1,color='#2F5FD0',label='Trek input X1 (after divider)  -  6.03 Vpp')
ax.plot(t*1e3,dm[:,3],lw=1,color='#C2410C',label='Trek monitor X1 (= HV / 1000)  -  5.39 Vpp')
ax.set_title('a.  One commanded ramp, measured at three points in the X1 chain')
ax.set_xlabel('time [ms]'); ax.set_ylabel('volts'); ax.legend(loc='upper left')
ax.annotate('arccos-shaped drive:\nfast - slow - fast',xy=(3.1,4.6),xytext=(4.6,2.4),fontsize=7.5,
            arrowprops=dict(arrowstyle='->',lw=.8,color='#555'),color='#333')

ax=fig.add_subplot(2,2,2)
m=win(t,(-0.3e-3,11e-3))
x=dc[:,1];
for lab,v,c in (('Trek input X1',dc[:,3],'#2F5FD0'),('Trek monitor X1',dm[:,3],'#C2410C')):
    A=np.vstack([x[m],np.ones(m.sum())]).T
    cc,*_=np.linalg.lstsq(A,v[m],rcond=None)
    ax.plot(t*1e3,(v-cc[1])/cc[0],lw=1,color=c,label=lab+'  (gain removed)')
ax.plot(t*1e3,x,lw=1,color='#B58900',label='AWG Ch1  (reference)')
ax.set_xlim(2.6,4.4); ax.set_ylim(5.0,10.0)
ax.set_title('b.  Same three after removing gain and offset only - zoom on the fastest segment')
ax.set_xlabel('time [ms]'); ax.set_ylabel('volts, referred to AWG'); ax.legend(loc='lower right')

ax=fig.add_subplot(2,1,2)
x=dm[:,1]; y=dm[:,3]
i0=int(np.argmax(m)); nact=int(m.sum())
rw=np.full(len(t),np.nan); rw[i0:i0+nact]=y[m]-wiener(x[m],y[m])
fs_=np.ptp(x[m])
series=[('gain and offset removed',affine(x,y,m),'#C2410C'),
        ('and best single pole (tau = 27.6 us)',affine(onepole(x,27.6e-6,dt),y,m),'#B58900'),
        ('and best 2nd order (fn = 2.33 kHz, zeta = 0.21)',affine(sim2(x,2326,0.206,dt),y,m),'#2F5FD0'),
        ]
for lab,res,c in series:
    s,N=smooth(np.nan_to_num(res),dt)
    mm=m.copy(); idx=np.where(mm)[0]; mm[idx[:N*8]]=False; mm[idx[-N*2:]]=False
    ax.plot(t[mm]*1e3,s[mm]/fs_*100,lw=1.4,color=c,
            label=f"{lab}   -   rms {np.sqrt(np.mean((s[mm]/fs_)**2))*100:.2f} %")
ax.axhline(0,color='k',lw=.6)
ax.set_title('c.  What is left over, whole chain, X1 at 19.2 Vpp.  Residual after removing each model in turn (200 us smoothing)')
ax.set_xlabel('time [ms]'); ax.set_ylabel('residual [% of full swing]'); ax.legend(loc='upper right')
plt.tight_layout(); plt.savefig('fig1_residuals.png',dpi=115); plt.close()

# ================= FIG 2 =================
fig,axs=plt.subplots(2,3,figsize=(14,7.8))
ax=axs[0,0]
for k,ch,c,lab in (('cond','X1','#2F5FD0','X1  divider -> Trek in'),('cond','X2','#7AA2FF','X2  summer -> Trek in'),
                   ('mon','X1','#C2410C','X1  whole chain -> monitor'),('mon','X2','#E8A33D','X2  whole chain -> monitor')):
    p=sorted([(q['amp'],q['gain_2p']) for q in R if q['kind']==k and q['ch']==ch])
    ax.semilogx([q[0] for q in p],[q[1] for q in p],'o-',ms=4,lw=1,color=c,label=lab)
ax.set_xlabel('AWG amplitude setting [Vpp]'); ax.set_ylabel('stage gain [V/V]')
ax.set_title('a.  Gain vs drive amplitude, 400:1 range'); ax.legend(loc='center left'); ax.set_ylim(0.545,0.655)
ax.text(0.05,0.585,'the common -0.7 %/decade tilt appears\neven on the passive divider: it is the\nscope V/div range error, not the circuit',
        fontsize=6.8,color='#555')

ax=axs[0,1]
C=json.load(open('inl.json'))
for k,lab,c in (('cond|19.2|X1','X1 divider','#2F5FD0'),('cond|17.6|X2','X2 summer','#7AA2FF'),
                ('mon|19.2|X1','X1 whole chain','#C2410C'),('mon|17.6|X2','X2 whole chain','#E8A33D')):
    v=C[k]; ax.plot(v['cen'],v['inl'],lw=1.2,color=c,label=lab)
ax.axhline(0,color='k',lw=.6); ax.set_ylim(-0.5,0.5)
ax.set_xlabel('input level [fraction of full swing]'); ax.set_ylabel('INL [% of full swing]')
ax.set_title('b.  Static nonlinearity, measured inside one capture'); ax.legend(loc='lower left')

ax=axs[0,2]
for ch,c in (('X1','#C2410C'),('X2','#E8A33D')):
    ba={}
    for q in R:
        if q['kind']=='mon' and q['ch']==ch: ba.setdefault(q['amp'],[]).append(q['fn'])
    aa=sorted(ba); ff=[np.median(ba[a]) for a in aa]; er=[np.ptp(ba[a])/2 for a in aa]
    ax.errorbar(aa,ff,yerr=er,fmt='o-',ms=4,lw=1.4,color=c,capsize=3,label=f'{ch} chain')
ax.set_xlabel('AWG amplitude [Vpp]'); ax.set_ylabel('resonant frequency  fn  [Hz]')
ax.set_title('c.  Trek + EOM resonance softens as drive grows\n(the only real nonlinearity found)'); ax.legend()

ax=axs[1,0]
F=json.load(open('freq.json'))
for k,c,lab in (('cond|19.2|X1','#2F5FD0','preconditioning only (X1)'),
                ('mon|19.2|X1','#C2410C','whole chain X1'),('mon|17.6|X2','#E8A33D','whole chain X2')):
    v=F[k]; f=np.array(v['f']); mg=np.array(v['mag']); s=f<1600
    ax.semilogx(f[s],20*np.log10(mg[s]),lw=1.2,color=c,label=lab)
ax.axhline(0,color='k',lw=.6); ax.set_ylim(-4,3)
ax.set_xlabel('frequency [Hz]'); ax.set_ylabel('|H| [dB]')
ax.set_title('d.  Measured response (drive has no energy above ~1.5 kHz)'); ax.legend(loc='upper left')

ax=axs[1,1]
b='AWG MKJ ramps before and after conditioning 0V ampl 10x time_20260821_011846'
d=np.load(IDX[b]['npy']); fsr=1/float(np.median(np.diff(d[:,0])))
for i,lab,c in ((3,'Trek input X1  (passive divider)','#2F5FD0'),(4,'Trek input X2  (op-amp summer)','#C2410C'),
                (1,'AWG Ch1 alone','#9AA6B2'),(2,'AWG Ch2 alone','#E8A33D')):
    v=d[:,i]-d[:,i].mean(); f,P=welch(v,fsr,nperseg=1<<15); s=(f>10)&(f<5000)
    ax.loglog(f[s],np.sqrt(P[s])*1e6,lw=.8,color=c,label=lab)
ax.set_xlabel('frequency [Hz]'); ax.set_ylabel('noise density [uV/sqrt(Hz)]')
ax.set_title('e.  Quiescent noise at the two Trek inputs'); ax.legend(fontsize=7,loc='lower left')
ax.annotate('60 Hz and harmonics,\nsummer branch only',xy=(60,60),xytext=(200,25),fontsize=7,
            arrowprops=dict(arrowstyle='->',lw=.8,color='#555'),color='#333')

ax=axs[1,2]
dtq=2e-7
for fn,z,c,lab in ((2326,0.206,'#C2410C','full-scale drive  (fn = 2.33 kHz)'),
                   (3003,0.247,'#2F5FD0','small signal  (fn = 3.00 kHz)')):
    wn=2*np.pi*fn; bb,aa=bilinear([wn**2],[1,2*z*wn,wn**2],fs=1/dtq)
    Ts=np.logspace(np.log10(2e-5),np.log10(1e-2),44); ov=[]
    for T in Ts:
        n=int(30e-3/dtq); tt=np.arange(n)*dtq
        yy=lfilter(bb,aa,np.clip(tt/T,0,1)); ov.append((yy.max()-1)*100)
    ax.loglog(Ts*1e3,np.maximum(ov,1e-3),lw=1.7,color=c,label=lab)
ax.axvline(1.0,color='#333',ls='--',lw=.9); ax.text(1.1,16,'edge speed used\nin this dataset',fontsize=7,color='#333')
ax.axhline(1,color='gray',ls=':',lw=.9); ax.text(0.023,1.25,'1 % overshoot',fontsize=7,color='gray')
ax.set_xlabel('commanded linear ramp duration [ms]'); ax.set_ylabel('overshoot at the corner [% of step]')
ax.set_title('f.  How fast you can ramp before the resonance rings'); ax.legend(fontsize=7,loc='lower left')
plt.tight_layout(); plt.savefig('fig2_characterisation.png',dpi=115); plt.close()
print('figs done')
