"""Figures for the AOM intensity-lock validation (protocol phase C, 31 Aug 2026).

Sources, all under run/protocol/:
  C_lockoff2_span{9,11,13,15,19}   lock disengaged, pinned 0 dBV   (retake)
  C_lockoff_span17                 lock disengaged                 (first session;
                                   the retake overloaded span 17 by +32.6 dB)
  C_lockon2_span{9..19}            lock engaged, after the servo was reconfigured
  C_lockon_span{9..19}             lock engaged, misconfigured loop (UGF ~1.5 kHz)
  C1_span{11..19}                  lock engaged at -4 dBV, the range-step reference
  B2_dark_span{11..19}             out-of-loop PDA10A2, beam blocked
  C6_inloop_dark_span{9..19}       in-loop PDA100A2 at 10 dB gain, beam blocked
  C_bump_scope.csv                 MSO-X 2014A, lock engaged, 0-3.1 MHz

Conventions, following the protocol's own analysis notes:
  - Band figures are the MEAN of POWER over the band, never the median.
  - Mains is masked by a frequency window, not a bin count: +-23.4 Hz on every
    band above 90 Hz (the width set by the coarsest span in the comparison),
    +-3 Hz on the two bands below, where +-23.4 Hz would remove half the band.
  - Each span owns the octaves between the preceding span's full span and its own.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import LogLocator, NullFormatter

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "run", "protocol")
OUT = os.path.join(HERE, "..", "docs", "figures")

V_ON, V_OFF, V_INLOOP = 5.7689, 5.7560, 3.762      # volts DC, measured 31 Aug
TARGET = 2.4e-4                                     # converged ILC residual

SPANS = [9, 11, 13, 15, 17, 19]
SPAN_HZ = {s: 100000.0 / 2.0 ** (19 - s) * 0.9975 for s in SPANS}
BANDS = [(0.5, 10, 9), (10, 90, 9), (90, 380, 11), (380, 1500, 13),
         (1500, 6000, 15), (6000, 24000, 17), (24000, 95000, 19)]

plt.rcParams.update({"font.size": 9, "axes.grid": True, "grid.alpha": 0.25,
                     "figure.facecolor": "white", "savefig.facecolor": "white",
                     "axes.axisbelow": True, "legend.framealpha": 0.92})

C_OFF, C_ON, C_BAD, C_DARK, C_IN, C_SCOPE = (
    "#B03A2E", "#1F4E79", "#8E8E8E", "#2E7D52", "#7B4EA8", "#C77B1E")


# ------------------------------------------------------------------ loading

def load(name):
    """(frequency Hz, amplitude spectral density V/rtHz) from an SR760 csv."""
    a = np.loadtxt(os.path.join(DATA, name), delimiter=",", skiprows=1)
    return a[:, 0], a[:, 1]


def mains_mask(f, width):
    """True on bins within `width` Hz of any 60 Hz harmonic."""
    m = np.zeros_like(f, bool)
    for h in range(1, int(f[-1] / 60.0) + 2):
        m |= np.abs(f - 60.0 * h) <= width
    return m


def band_asd(f, asd, lo, hi, width):
    """sqrt of the mean POWER density over [lo, hi], mains-masked. V/rtHz."""
    sel = (f >= lo) & (f <= hi) & np.isfinite(asd) & ~mains_mask(f, width)
    return float(np.sqrt(np.mean(asd[sel] ** 2))) if sel.any() else np.nan


def mask_width(hi):
    return 3.0 if hi <= 90 else 23.4


def composite(stem_for_span, spans=SPANS):
    """Splice spans without blending: each owns up to its own full span."""
    F, A, prev = [], [], 0.0
    for s in spans:
        f, a = load("%s_span%d.csv" % (stem_for_span[s], s))
        keep = (f > prev) & (f <= SPAN_HZ[s])
        F.append(f[keep])
        A.append(a[keep])
        prev = SPAN_HZ[s]
    return np.concatenate(F), np.concatenate(A)


OFF_MAP = {s: "C_lockoff2" for s in SPANS}
OFF_MAP[17] = "C_lockoff"                       # the retake overloaded span 17
ON_MAP = {s: "C_lockon2" for s in SPANS}
BAD_MAP = {s: "C_lockon" for s in SPANS}        # the 1.5 kHz-UGF lock

f_off, a_off = composite(OFF_MAP)
f_on, a_on = composite(ON_MAP)
f_bad, a_bad = composite(BAD_MAP)
f_dk, a_dk = composite({s: "B2_dark" for s in SPANS}, spans=SPANS[1:])
f_in, a_in = composite({s: "C6_inloop_dark" for s in SPANS})
f_c1, a_c1 = composite({s: "C1" for s in SPANS}, spans=SPANS[1:])
f_sc, a_sc = load("C_bump_scope.csv")

# suppression bin by bin, taken within each span so the bins already match
S_f, S_db, _prev = [], [], 0.0
for s in SPANS:
    fo, ao = load("%s_span%d.csv" % (OFF_MAP[s], s))
    fn, an = load("%s_span%d.csv" % (ON_MAP[s], s))
    keep = (fo > _prev) & (fo <= SPAN_HZ[s])
    S_f.append(fo[keep])
    S_db.append(20 * np.log10(ao[keep] / an[keep]))
    _prev = SPAN_HZ[s]
S_f, S_db = np.concatenate(S_f), np.concatenate(S_db)


def band_table():
    rows = []
    for lo, hi, sp in BANDS:
        w = mask_width(hi)
        fo, ao = load("%s_span%d.csv" % (OFF_MAP[sp], sp))
        fn, an = load("%s_span%d.csv" % (ON_MAP[sp], sp))
        fi, ai = load("C6_inloop_dark_span%d.csv" % sp)
        off = band_asd(fo, ao, lo, hi, w)
        on = band_asd(fn, an, lo, hi, w)
        inl = band_asd(fi, ai, lo, hi, w)
        rows.append(dict(lo=lo, hi=hi, span=sp, off=off, on=on,
                         supp=20 * np.log10(off / on),
                         dbc_on=20 * np.log10(on / V_ON),
                         dbc_in=20 * np.log10(inl / V_INLOOP)))
    return rows


ROWS = band_table()


def cumulative(f, asd, v_dc, f0):
    """Running fractional intensity noise sqrt(int S df)/V_DC from f0 upward."""
    k = (f >= f0) & np.isfinite(asd)
    fk, sk = f[k], asd[k] ** 2
    c = np.concatenate([[0.0], np.cumsum(np.diff(fk) * 0.5 * (sk[1:] + sk[:-1]))])
    return fk, np.sqrt(c) / v_dc


def _band_label(r):
    """'90-380 Hz', '1.5-6 kHz' - one unit per label, whichever suits the pair."""
    lo, hi = r["lo"], r["hi"]
    if hi < 1000:
        return "%g$-$%g Hz" % (lo, hi)
    if lo < 1000:
        return "%g Hz$-$%g kHz" % (lo, hi / 1000.0)
    return "%g$-$%g kHz" % (lo / 1000.0, hi / 1000.0)


def style_freq(ax, lo, hi):
    ax.set_xscale("log")
    ax.set_xlim(lo, hi)
    ax.xaxis.set_minor_locator(LogLocator(base=10, subs=np.arange(2, 10) * 0.1,
                                          numticks=99))
    ax.xaxis.set_minor_formatter(NullFormatter())


# ---------------------------------------------------- 1. loop suppression

def fig_suppression(path):
    fig, (ax, bx) = plt.subplots(2, 1, figsize=(9.4, 8.0), sharex=True,
                                 gridspec_kw=dict(height_ratios=[1.55, 1],
                                                  hspace=0.09))
    ax.axvspan(90, 1500, color="0.55", alpha=0.12, lw=0)
    ax.loglog(f_off, a_off, color=C_OFF, lw=0.8, label="lock off (free-running)")
    ax.loglog(f_on, a_on, color=C_ON, lw=0.8, label="lock on, 0 dBV range")
    ax.loglog(f_c1, a_c1, color=C_ON, lw=0.7, ls=(0, (4, 2)), alpha=0.75,
              label="lock on, $-$4 dBV range (C1)")
    ax.loglog(f_dk, a_dk, color=C_DARK, lw=0.9, ls="-.",
              label="out-of-loop detector dark (B2)")
    for lo, hi, fl in [(90, 380, 674e-9), (380, 1500, 320e-9), (1500, 6000, 219e-9)]:
        ax.plot([lo, hi], [fl, fl], color="0.35", lw=1.6, ls=":",
                label="analyser floor implied by the C1 range-step" if lo == 90 else None)
    ax.set_ylabel(r"amplitude spectral density  (V/$\sqrt{\mathrm{Hz}}$)")
    ax.set_ylim(2e-8, 3e-2)
    ax.legend(loc="upper right", fontsize=8)
    rax = ax.twinx()
    rax.grid(False)
    rax.set_ylim(20 * np.log10(2e-8 / V_ON), 20 * np.log10(3e-2 / V_ON))
    rax.set_ylabel("RIN  (dBc/Hz),  $V_{DC}$ = 5.7689 V")
    ax.set_title("Out-of-loop intensity noise with the AOM lock disengaged and engaged, "
                 "and the resulting loop suppression\n"
                 "PDA10A2 after the polarimetry chain; SR760 spans 9$-$19 spliced "
                 "(run/protocol, 31 Aug 2026)", fontsize=9.6, loc="left")

    bx.semilogx(S_f, S_db, color="0.62", lw=0.6, label="per bin")
    for r in ROWS:
        bx.plot([r["lo"], r["hi"]], [r["supp"]] * 2, color=C_ON, lw=2.6,
                solid_capstyle="butt",
                label="band mean of power, mains masked" if r["lo"] == 0.5 else None)
        bx.text(np.sqrt(r["lo"] * r["hi"]), r["supp"] + 2.6,
                "%+.1f" % r["supp"], ha="center", fontsize=8, color=C_ON,
                bbox=dict(fc="white", ec="none", pad=0.6, alpha=0.85))
    bx.axhline(0, color="k", lw=0.7)
    bx.set_ylabel(r"suppression  $|1+L|$  (dB)")
    bx.set_xlabel("frequency (Hz)")
    bx.set_ylim(-22, 58)
    bx.legend(loc="upper right", fontsize=8)
    bx.text(0.5, -21.0, "shading in the upper panel: the C1 range-step leaves only 6.2 and "
            "5.1 dB of margin over 90 Hz$-$1.5 kHz, so lock-on there is an upper limit\n"
            "mains mask $\\pm$3 Hz below 90 Hz, $\\pm$23.4 Hz above;  lock-off span 17 "
            "taken from the first session, the retake having overloaded it by +32.6 dB",
            fontsize=7.4, color="0.3", va="bottom")
    style_freq(bx, 0.4, 1.1e5)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------- 2. servo bump above 100 kHz

def fig_bump(path):
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(12.4, 4.9))
    ax.axvspan(95e3, 250e3, color=C_SCOPE, alpha=0.13, lw=0)
    ax.loglog(f_sc, a_sc, color=C_SCOPE, lw=0.8,
              label="scope, lock on (MSO-X 2014A)")
    k = f_on >= 2e3
    ax.loglog(f_on[k], a_on[k], color=C_ON, lw=0.8, label="SR760, lock on")
    i = int(np.argmax(np.where((f_sc > 2e5) & (f_sc < 8e5), a_sc, 0)))
    rise = 20 * np.log10(a_sc[i] / band_asd(f_sc, a_sc, 24e3, 95e3, 0))
    ax.plot(f_sc[i], a_sc[i], "v", color="k", ms=6, zorder=5)
    ax.annotate("%.0f kHz, %.1f " % (f_sc[i] / 1e3, a_sc[i] * 1e6)
                + r"$\mu$V/$\sqrt{\mathrm{Hz}}$" + "\n+%.1f dB on 24$-$95 kHz" % rise,
                xy=(f_sc[i], a_sc[i]), xytext=(6.5e4, 4.4e-5), fontsize=8,
                arrowprops=dict(arrowstyle="-", lw=0.8, color="k"))
    ax.text(1.45e5, 1.02e-6, "95$-$250 kHz\n" r"$\delta I/I$ = 7.2$\times10^{-4}$",
            fontsize=8, color=C_SCOPE, ha="center")
    ax.set_xlim(2e3, 3.2e6)
    ax.set_ylim(7e-7, 1.1e-4)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel(r"amplitude spectral density  (V/$\sqrt{\mathrm{Hz}}$)")
    ax.legend(loc="lower left", fontsize=8)
    ax.set_title("Locked spectrum across the SR760 ceiling", fontsize=9.6, loc="left")
    sr = band_asd(f_on, a_on, 24e3, 95e3, 23.4)
    sc = band_asd(f_sc, a_sc, 24e3, 95e3, 0.0)
    ax.text(2.4e3, 8.6e-5, "24$-$95 kHz: SR760 %.2f, scope %.2f " % (sr * 1e6, sc * 1e6)
            + r"$\mu$V/$\sqrt{\mathrm{Hz}}$" + "\n(%+.2f dB, two instruments, two "
            "analysis paths)" % (20 * np.log10(sr / sc)),
            fontsize=7.8, color="0.25", va="top")

    fc, cc = cumulative(f_on, a_on, V_ON, 0.5)
    fs, cs = cumulative(f_sc, a_sc, V_ON, 2e3)
    bx.loglog(fc, cc, color=C_ON, lw=1.6, label="SR760, lock on (from 0.5 Hz)")
    bx.loglog(fs, cs, color=C_SCOPE, lw=1.6, label="scope, lock on (from 2 kHz)")
    bx.axhline(TARGET, color=C_OFF, lw=1.2, ls="--")
    bx.text(0.62, TARGET * 1.13, r"2.4$\times10^{-4}$  converged ILC residual",
            color=C_OFF, fontsize=8)
    bx.axvspan(95e3, 250e3, color=C_SCOPE, alpha=0.13, lw=0)
    for f0, lab in [(2.4e4, "24 kHz"), (9.5e4, "95 kHz"), (2.5e5, "250 kHz")]:
        bx.axvline(f0, color="0.6", lw=0.7, ls=":")
        bx.text(f0 * 1.07, 2.6e-3, lab, fontsize=7.5, color="0.4", rotation=90,
                va="top")
    bx.text(0.62, 1.35e-4, "SR760   0.5 Hz$-$95 kHz: %.2e\nscope   2 kHz$-$250 kHz: "
            "%.2e\nscope   2 kHz$-$3 MHz: %.2e" % (np.interp(9.5e4, fc, cc),
                                                   np.interp(2.5e5, fs, cs), cs[-1]),
            fontsize=8, color="0.2", va="top")
    bx.set_xlim(0.5, 3.2e6)
    bx.set_ylim(1e-5, 3e-3)
    bx.set_xlabel("upper limit of integration (Hz)")
    bx.set_ylabel(r"cumulative  $\delta I/I$  (rms)")
    bx.legend(loc="upper left", fontsize=8)
    bx.set_title("Cumulative fractional intensity noise", fontsize=9.6, loc="left")
    fig.suptitle("Locked out-of-loop spectrum from 2 kHz to 3 MHz and the cumulative "
                 "intensity-noise budget it implies (C_bump_scope, 31 Aug 2026)",
                 fontsize=10, x=0.005, ha="left", y=1.005)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# --------------------------------- 3. residual against the in-loop floor

def fig_inloop(path):
    fig, ax = plt.subplots(figsize=(9.4, 5.0))
    x = np.arange(len(ROWS))
    w = 0.36
    base = -178.0
    meas = np.array([r["dbc_on"] for r in ROWS])
    imp = np.array([r["dbc_in"] for r in ROWS])
    ax.bar(x - w / 2, meas - base, w, bottom=base, color=C_ON,
           label="measured out-of-loop residual, lock on")
    ax.bar(x + w / 2, imp - base, w, bottom=base, color=C_IN,
           label=r"floor the in-loop detector can impose,  "
                 r"$S_{\rm in}\,/\,V_{DC,\rm in}^{2}$")
    for i, (m, p) in enumerate(zip(meas, imp)):
        ax.annotate("", xy=(i + 0.02, m), xytext=(i + 0.02, p),
                    arrowprops=dict(arrowstyle="<->", lw=0.9, color="k"))
        ax.text(i + 0.06, (m + p) / 2, "%.0f dB" % (m - p), fontsize=8,
                va="center", ha="left",
                bbox=dict(fc="white", ec="none", pad=0.7, alpha=0.9))
        ax.text(i - w / 2, m + 1.4, "%.1f" % m, ha="center", fontsize=7.6, color=C_ON)
        ax.text(i + 0.12, p + 1.4, "%.1f" % p, ha="left", fontsize=7.6, color=C_IN)
    ax.set_xticks(x)
    ax.set_xticklabels([_band_label(r) for r in ROWS], fontsize=8)
    ax.set_ylim(base, -88)
    ax.set_ylabel("RIN  (dBc/Hz)")
    ax.set_xlabel("band")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.0), fontsize=8, ncol=2)
    ax.set_title("Locked out-of-loop residual against the noise floor the in-loop detector "
                 "can write onto the beam\n"
                 "out-of-loop PDA10A2 at $V_{DC}$ = 5.7689 V; in-loop PDA100A2 at 10 dB "
                 "gain, $V_{DC}$ = 3.762 V, beam blocked (C6, 31 Aug 2026)",
                 fontsize=9.6, loc="left")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------ 4. the misconfigured-lock trap

def fig_ugf(path):
    fig, ax = plt.subplots(figsize=(9.4, 5.2))
    ax.loglog(f_off, a_off, color=C_OFF, lw=0.8, label="lock off")
    ax.loglog(f_bad, a_bad, color=C_BAD, lw=0.9,
              label="lock on, first configuration (06:21)")
    ax.loglog(f_on, a_on, color=C_ON, lw=0.8,
              label="lock on, after reconfiguration (07:06)")
    ax.axvline(1500, color="0.4", lw=0.8, ls=":")
    ax.text(1350, 7e-3, "unity gain of the first\nconfiguration, ~1.5 kHz",
            fontsize=8, color="0.3", va="center", ha="right")
    ax.set_xlim(0.4, 1.1e5)
    ax.set_ylim(1e-6, 8e-2)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel(r"amplitude spectral density  (V/$\sqrt{\mathrm{Hz}}$)")
    ax.legend(loc="upper right", fontsize=8)
    lines = []
    for lo, hi, sp in [(1500, 6000, 15), (6000, 24000, 17), (24000, 95000, 19)]:
        fo, ao = load("%s_span%d.csv" % (OFF_MAP[sp], sp))
        fb, ab = load("C_lockon_span%d.csv" % sp)
        lines.append("%g$-$%g kHz: %+.1f dB" % (
            lo / 1000, hi / 1000,
            20 * np.log10(band_asd(fo, ao, lo, hi, 23.4)
                          / band_asd(fb, ab, lo, hi, 23.4))))
    ax.text(4.5e3, 1.7e-4, "suppression of the first configuration\n" + "\n".join(lines),
            fontsize=8, color="0.25", va="top")
    ax.set_title("Out-of-loop spectrum with the lock disengaged, with the first servo "
                 "configuration, and after reconfiguration\n"
                 "the two locked records were taken 45 minutes apart against the same "
                 "free-running spectrum (C_lockon / C_lockon2 / C_lockoff, 31 Aug 2026)",
                 fontsize=9.6, loc="left")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, fn in [("lock_suppression.png", fig_suppression),
                     ("lock_servo_bump.png", fig_bump),
                     ("lock_inloop_floor.png", fig_inloop),
                     ("lock_ugf_misconfig.png", fig_ugf)]:
        p = os.path.join(OUT, name)
        fn(p)
        print("wrote", os.path.normpath(p))
    print("\nband summary (mean of power, mains masked)")
    print("%16s %12s %11s %8s %10s %15s %12s"
          % ("band", "off nV/rtHz", "on nV/rtHz", "supp dB", "on dBc/Hz",
             "in-loop dBc/Hz", "headroom dB"))
    for r in ROWS:
        print("%16s %12.4g %11.4g %8.2f %10.1f %15.1f %12.1f"
              % (str((r["lo"], r["hi"])), r["off"] * 1e9, r["on"] * 1e9, r["supp"],
                 r["dbc_on"], r["dbc_in"], r["dbc_on"] - r["dbc_in"]))
