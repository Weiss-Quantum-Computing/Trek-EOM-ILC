"""One figure set per batch campaign, data as recorded, in the style of the 9 Oct
2026 polarimetry reanalysis (scope_data/polarimetry/reanalysis/rp_common.py):
large type, descriptive titles, x axes on every panel, values in the notes,
no verdicts in the artwork. Run with the Anaconda python.

    python tools/campaign_figures.py <plan.json> [stem ...] [--log <batch log>]
                                     [--copy-to <folder>]

Reads the campaign's states (drive_<stem>X1/X2.state.npz), its stored
measurements (meas_<stem>X?_iNN.npy, meas_<stem>PD_iNN.npy + _stats.npz), its
optical record (<run>/optical/<stem>_optical.json) and the comparison folders
of its scans, and writes <run>/figures/<stem>/fig1..fig8_*.png with a README.md
that carries the numbers. --copy-to mirrors the folder (the OneDrive suite).

Figures:
  1 targets and the learned drives            5 light, monitors and target per scan
  2 monitor error through the iterations      6 light - target through the passes
  3 convergence, optical passes marked        7 the spin echo's second leg against its first
  4 what the loop added to the drive          8 data quality: PD screen use, shots, fit, zero
"""
import argparse
import datetime
import glob
import importlib.util
import json
import os
import re
import shutil
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import run_ilc                                                   # noqa: E402
import ilc_batch                                                 # noqa: E402
from eomilc import optical, corrections as corrmod, echo         # noqa: E402

REANALYSIS = os.path.join(os.path.dirname(ROOT), "scope_data", "polarimetry", "reanalysis")
HV90 = {"X1": 5128.3, "X2": 5137.4}          # HV volts for 90 deg (the targets' V90)
CH_COL = {"X1": "#2a78d6", "X2": "#eb6834"}
# before the close purple, then one hue per pass (no gray: the before scan carries the same weight)
PASS_COL = ["#4a3aa7", "#2a78d6", "#e34948", "#1baf7a", "#eda100", "#e87ba4"]
# a channel's second series (after the close, the moved target): its own hue, never a dash of the first
CH_COL2 = {"X1": "#4a3aa7", "X2": "#e34948"}
LIGHT, MON, TGT = "#2a78d6", "#eb6834", "#0b0b0b"   # light, monitors, target in the optical views
IT_COL = ["#cfd8e6", "#9ec5f4", "#3987e5", "#1c5cab", "#0d366b"]
OPT_COL = [["#f5b48a", "#eb6834"], ["#ef9a9a", "#e34948"], ["#e6c35c", "#c99700"], ["#b39ddb", "#4a3aa7"]]


def legend_out(ax, **kw):
    """The legend beside the axes (constrained layout makes room), never on the data."""
    kw.setdefault("fontsize", 10.5)
    return ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), borderaxespad=0.0, **kw)


def rp():
    """rp_common from the reanalysis folder, by path (its style, finish, note, palette)."""
    spec = importlib.util.spec_from_file_location("rp_common", os.path.join(REANALYSIS, "rp_common.py"))
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, REANALYSIS)
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------------ loading
class Campaign:
    def __init__(self, plan, stem, log_path=None):
        self.plan, self.stem = plan, stem
        self.camp = next(c for c in plan["campaigns"] if c["stem"] == stem)
        self.run_dir = plan["_run_dir"]
        self.short = self.camp.get("awg", stem)
        self.states, self.meas, self.pd = {}, {}, {}
        for k in ("X1", "X2"):
            p = os.path.join(self.run_dir, f"drive_{self.short}{k}.state.npz")
            if os.path.exists(p):
                st = run_ilc.load_state(p)
                self.states[k] = st
                self.meas[k] = {}
                for f in glob.glob(os.path.join(self.run_dir, f"meas_{self.short}{k}_i??.npy")):
                    it = int(re.search(r"_i(\d\d)\.npy$", f).group(1))
                    self.meas[k][it] = np.load(f)
        for f in glob.glob(os.path.join(self.run_dir, f"meas_{self.short}PD_i??.npy")):
            it = int(re.search(r"_i(\d\d)\.npy$", f).group(1))
            s = f[:-4] + "_stats.npz"
            self.pd[it] = (np.load(f), dict(np.load(s)) if os.path.exists(s) else {})
        self.rec = None
        p = os.path.join(self.run_dir, "optical", f"{stem}_optical.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                self.rec = json.load(fh)
        self.scans = optical.record_scans(self.rec) if self.rec else []
        self.log = self._read_log(log_path)

    # -- helpers
    @property
    def t(self):
        return np.asarray(self.states["X1"]["t"], float)

    @property
    def dt(self):
        return float(self.states["X1"]["dt"])

    def target(self, k, base=True):
        st = self.states[k]
        if base and "base_target" in st:
            return np.asarray(st["base_target"], float)
        return np.asarray(st["target"], float)

    def mon_scale(self, k):
        """Monitor volts -> HV volts at the EOM (1000 V/V on the Trek monitor)."""
        return 1000.0

    def history(self, k):
        return list(np.asarray(self.states[k]["history"], dtype=object)) if "history" in self.states[k] else []

    def passes(self, k):
        """The iterations at which the optical corrections took effect."""
        st = self.states[k]
        its = []
        if "corrections" in st:
            try:
                its = sorted({int(L["from_it"]) for L in json.loads(str(st["corrections"]))
                              if L.get("from_it") is not None and L.get("status") == "applied"})
            except (ValueError, KeyError, TypeError):
                its = []
        if not its and "corr_from" in st:
            its = [int(st["corr_from"])]
        return its

    def pass_of(self, k, it):
        return sum(1 for p in self.passes(k) if it >= p)

    def drives(self, k):
        """Every stored drive of a channel: {it: u} from the loop's history is not
        kept; the state holds the current drive and the keepers' csv the kept one."""
        return np.asarray(self.states[k]["u"], float)

    def _read_log(self, path):
        """Lines of the batch log about this campaign: kept shots, dither, dropped."""
        if not path or not os.path.exists(path):
            return []
        out, mine = [], False
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("--- "):
                    mine = line.strip() == f"--- {self.stem}" or line.strip().startswith(f"--- {self.stem}:")
                elif mine and re.search(r"shots kept|dither|dropped|re-arm|never rises|REFUSED|refused", line):
                    out.append(line.rstrip())
        return out

    def transports(self):
        """(a1, b1, shift) samples of the first transport and the shift to the second, or None."""
        try:
            tr = echo._card().find_transports([self.target("X1")], self.dt * 1e6)
        except Exception:
            return None
        if len(tr) < 2:
            return None
        return int(tr[0][0]), int(tr[0][1]), int(tr[1][0] - tr[0][0])


def npz_of(scan):
    p = os.path.join(scan["folder"], "analysis", "target_compare", "target_compare.npz")
    return np.load(p) if os.path.exists(p) else None


def worst(v, key="top_lt_mdeg"):
    legs = (v or {}).get("legs") or []
    return max(legs, key=lambda L: abs(L.get(key, 0.0))) if legs else {}


# ------------------------------------------------------------------ figures
def fig1_targets(c, R, out):
    import matplotlib.pyplot as plt
    tms = c.t * 1e3
    fig, axes = plt.subplots(3, 1, figsize=(12, 10.0), layout="constrained")
    moved = False
    for k in ("X1", "X2"):
        if k not in c.states:
            continue
        axes[0].plot(tms, c.target(k) * c.mon_scale(k) / 1e3, color=CH_COL[k], label=f"{k} base target")
        st = c.states[k]
        axes[1].plot(tms, c.drives(k), color=CH_COL[k], label=f"{k} drive now (iteration {int(st['iteration'])})")
        if "corr_total" in st:
            d = np.asarray(st["corr_total"], float) * c.mon_scale(k) * 90.0 / HV90[k] * 1e3
            axes[2].plot(tms, d, color=CH_COL2[k], label=f"{k}: target now minus base, peak {np.max(np.abs(d)):.0f} mdeg")
            moved = True
    axes[0].set_ylabel("base target at the EOM (kV)")
    axes[1].set_ylabel("drive (V at the AWG)")
    axes[2].set_ylabel("added by the optical passes (mdeg)")
    axes[2].axhline(0, color=R.INK2, lw=0.6)
    if not moved:
        axes[2].text(0.5, 0.5, "no optical correction applied", transform=axes[2].transAxes, ha="center", color=R.MUTED)
    for a in axes:
        a.set_xlabel("time in the record (ms)")
        legend_out(a)
    n = len(c.t)
    R.finish(fig, f"Targets and learned drives, {c.stem}",
             f"Top: the base target pair (HV volts at the EOM, 1000 V per monitor volt). Middle: the drive each "
             f"channel plays now, AWG volts. Bottom: what the optical passes added to each channel's target, "
             f"in degrees of rotation at the EOM. {n} points, {n * c.dt * 1e3:.3f} ms record on a "
             f"{c.dt * 1e6:.1f} us grid; V90 {HV90['X1']:.1f} / {HV90['X2']:.1f} V. "
             f"Plan {c.plan.get('name', '')}, run folder {c.run_dir}.", out)
    plt.close(fig)


def pick_iterations(c, k):
    its = sorted(c.meas.get(k, {}))
    if not its:
        return []
    ps = c.passes(k)
    keep = {its[0], its[-1]}
    el = [i for i in its if not ps or i < ps[0]]
    if el:
        keep.add(el[-1])
        if len(el) > 2:
            keep.add(el[len(el) // 2])
    for j, p in enumerate(ps):
        nxt = ps[j + 1] if j + 1 < len(ps) else its[-1] + 1
        seg = [i for i in its if p <= i < nxt]
        if seg:
            keep.add(seg[0])
            keep.add(seg[-1])
    return sorted(keep)


def fig2_error(c, R, out):
    import matplotlib.pyplot as plt
    tms = c.t * 1e3
    fig, axes = plt.subplots(2, 1, figsize=(12, 7.6), layout="constrained")
    notes = []
    for row, k in enumerate(("X1", "X2")):
        ax = axes[row]
        if k not in c.states:
            continue
        its = pick_iterations(c, k)
        tgt_base = c.target(k)
        st = c.states[k]
        tgt_now = np.asarray(st["target"], float)
        ps = c.passes(k)
        for j, it in enumerate(its):
            y = c.meas[k][it]
            p = c.pass_of(k, it)
            # the target the iteration was measured against: the base before the first pass
            tgt = tgt_base if p == 0 else tgt_now
            e = (tgt - y) * c.mon_scale(k)
            if p:
                pair = OPT_COL[min(p - 1, len(OPT_COL) - 1)]
                col = pair[1] if it == max(i for i in its if c.pass_of(k, i) == p) else pair[0]
            else:
                col = IT_COL[min(j, len(IT_COL) - 1)]
            lab = f"i{it:02d}" + (f" after optical pass {p}" if p else "") + f": rms {np.sqrt(np.mean(e ** 2)):.2f} V"
            ax.plot(tms, e, color=col, lw=1.0 if it == its[-1] else 0.8, label=lab)
        ax.set_ylabel(f"{k}: target - measured (V at the EOM)")
        ax.set_xlabel("time in the record (ms)")
        ax.axhline(0, color=R.INK2, lw=0.6)
        legend_out(ax)
        notes.append(f"{k}: {len(c.meas[k])} iterations stored, optical passes took effect at "
                     + (", ".join(f"i{p:02d}" for p in ps) if ps else "none"))
    R.finish(fig, f"Monitor error through the iterations, {c.stem}",
             "Target minus the measured monitor, HV volts at the EOM, for the first iteration, a middle and the "
             "last electrical one (blues), and the first and last iteration after each optical pass (warm hues, "
             "measured against the moved target). Shot means at the plan's shot count; the early iterations use "
             "16 shots. " + "; ".join(notes) + ".", out)
    plt.close(fig)


def fig3_convergence(c, R, out):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 1, figsize=(12, 5.6), layout="constrained")
    for k in ("X1", "X2"):
        h = c.history(k)
        if not h:
            continue
        pk = [float(m["peak_err_hv"]) for m in h]
        rm = [float(m["rms_err_hv"]) for m in h]
        x = np.arange(len(h))
        ax.semilogy(x, pk, "o-", color=CH_COL[k], label=f"{k} peak error")
        ax.semilogy(x, rm, "s-", color=CH_COL2[k], label=f"{k} rms error")
        for j, p in enumerate(c.passes(k), 1):
            if k == "X1":
                ax.axvline(p - 0.5, color=R.INK2, lw=0.8, ls=":")
                ax.annotate(f"optical pass {j} applied", (p - 0.4, 0.03), xycoords=("data", "axes fraction"),
                            rotation=90, fontsize=10.5, color=R.INK2, va="bottom")
    ax.set_xlabel("iteration")
    ax.set_ylabel("error at the EOM (V)")
    legend_out(ax)
    ax.grid(True, which="both")
    R.finish(fig, f"Convergence, {c.stem}",
             "Peak and rms of target minus measured monitor per iteration, HV volts. The iterations after a "
             "dotted line were measured against the target moved by that optical pass; the rise at each "
             "line is the new target, learned again in the plan's iterations.", out)
    plt.close(fig)


def hold_mask(c):
    tr = c.transports()
    tgt = c.target("X1")
    if tr is None:
        top = tgt > 0.98 * tgt.max()
    else:
        top = np.zeros(len(tgt), bool)
        top[tr[0]:tr[1]] = tgt[tr[0]:tr[1]] > 0.98 * tgt.max()
    return top


def asd(x, dt):
    x = np.asarray(x, float) - np.mean(x)
    n = len(x)
    w = np.hanning(n)
    X = np.fft.rfft(x * w)
    f = np.fft.rfftfreq(n, dt)
    a = np.abs(X) * np.sqrt(2.0 * dt / np.sum(w ** 2))
    return f[1:], a[1:]


def fig4_drive_noise(c, R, out):
    """What the loop added to the drive, and whether it rides on the monitors:
    the drive's change from the first shot (spectrum), the drive's and the
    monitor's content above the loop band on the ramp top, per iteration."""
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(14, 8.4), layout="constrained")
    stats = {}
    notes = []
    for col, k in enumerate(("X1", "X2")):
        if k not in c.states:
            continue
        st = c.states[k]
        u = c.drives(k)
        f_cut = float(st["f_cut"]) if "f_cut" in st else 20e3
        ax = axes[0, col]
        f, a = asd(u, c.dt)
        ax.loglog(f, a, color=CH_COL[k], lw=0.9, label=f"{k} drive now")
        f2, a2 = asd(c.target(k), c.dt)
        ax.loglog(f2, a2 * np.max(a) / max(np.max(a2), 1e-12), color=R.INK2, lw=0.7, alpha=0.6,
                  label="target shape (scaled)")
        ax.axvline(f_cut, color=R.MUTED, lw=0.8, ls=":")
        ax.annotate(f"f_cut {f_cut / 1e3:g} kHz", (f_cut, 0.9), xycoords=("data", "axes fraction"), rotation=90,
                    fontsize=10.5, color=R.MUTED, ha="right", va="top")
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel("drive spectrum (V / sqrt(Hz))")
        ax.set_xlim(50, 0.5 / c.dt)
        legend_out(ax)
        # above-band content on the hold: drive and measured monitor per iteration
        ax = axes[1, col]
        top = hold_mask(c)
        its = sorted(c.meas.get(k, {}))
        hp, shots = [], []
        for it in its:
            y = c.meas[k][it] * c.mon_scale(k)
            band = corrmod.lowpass(y, c.dt, f_cut)
            r = y - band
            hp.append(float(np.sqrt(np.mean(r[top] ** 2))) if top.any() else float("nan"))
            shots.append(int(c.pd[it][1].get("n_shots", 0)) if it in c.pd else 0)
        ax.plot(its, hp, "-", color=CH_COL[k], lw=1.0)
        full = [i for i, n in enumerate(shots) if n == 0 or n == max(shots)]
        early = [i for i, n in enumerate(shots) if n and n < max(shots)]
        ax.plot([its[i] for i in full], [hp[i] for i in full], "o", color=CH_COL[k],
                label=f"{k} measured monitor above {f_cut / 1e3:g} kHz on the hold, {max(shots) or '?'} shots")
        if early:
            ax.plot([its[i] for i in early], [hp[i] for i in early], "o", mfc="none", color=CH_COL[k],
                    label=f"{min(n for n in shots if n)} shots (early iterations)")
        ud = u - corrmod.lowpass(u, c.dt, f_cut)
        u_hp = float(np.sqrt(np.mean(ud[top] ** 2))) * 1e3 if top.any() else float("nan")
        for j, p in enumerate(c.passes(k), 1):
            ax.axvline(p - 0.5, color=R.INK2, lw=0.8, ls=":")
        ax.set_xlabel("iteration")
        ax.set_ylabel("rms above the loop band (V at the EOM)")
        legend_out(ax)
        notes.append(f"{k} drive now above {f_cut / 1e3:g} kHz on the hold: {u_hp:.1f} mV rms at the AWG, "
                     f"{u_hp:.1f} V at the EOM")
        stats[k] = dict(f_cut=f_cut, drive_hp_mV=u_hp, mon_hp_V=hp, its=its)
    R.finish(fig, f"What the loop added to the drive, {c.stem}",
             "Top: amplitude spectral density of the drive each channel plays now against the target's own "
             "shape (scaled to the drive's peak), the loop's cut-off dotted. Bottom: rms of the measured "
             "monitor above the cut-off over the ramp top, per iteration (what rides on the hold beyond "
             "what the loop can place; hollow markers are the 16-shot early iterations, whose shot-mean "
             "noise is twice the 64-shot one). Dotted lines mark the optical passes. "
             + "; ".join(notes) + ".", out)
    plt.close(fig)
    return stats


def fig5_scans(c, R, out):
    import matplotlib.pyplot as plt
    joint = [s for s in c.scans if s["kind"] in ("before", "verify")]
    if not joint:
        return
    n = len(joint)
    fig, axes = plt.subplots(n, 1, figsize=(12, 2.9 * n + 1.2), layout="constrained", squeeze=False)
    for row, s in enumerate(joint):
        ax = axes[row, 0]
        z = npz_of(s)
        if z is None:
            ax.text(0.5, 0.5, f"{s['scan']}: no comparison", transform=ax.transAxes, ha="center")
            continue
        tl = z["t_leg1"]
        ms = (tl - tl[0]) * 1e3
        ax.plot(ms, z["phi_target"], color=TGT, lw=0.9, ls=":", label="target")
        ax.plot(ms, z["leg1_phi_mon"], color=MON, lw=0.9, label="monitors")
        ax.plot(ms, z["leg1_phi_light"], color=LIGHT, lw=0.9, label="light")
        ax.set_ylabel("rotation (deg)")
        ax.set_xlabel("time from the leg start (ms)")
        v = worst(s["verdict"])
        ax.set_title(f"{s['label']}: {s['scan']}" + (f"  --  light - target at the ramp top {v['top_lt_mdeg']:+.0f} mdeg, "
                                                       f"after the fall {v['after_fall_lt_mdeg']:+.0f}, rms {v['lt_rms_mdeg']:.0f}"
                                                       if v else ""))
        legend_out(ax)
    R.finish(fig, f"Light, monitors and target through the record, per joint scan, {c.stem}",
             "The analyzer scan's light rotation (per-sample harmonic fit over the analyzer angles, in the "
             "target's sense), the monitors' rotation (90 deg x V / V90 per channel, summed) and the base "
             "target, for the scan before the close and after each pass.", out)
    plt.close(fig)


def fig6_passes(c, R, out):
    import matplotlib.pyplot as plt
    joint = [s for s in c.scans if s["kind"] in ("before", "verify")]
    solo = {s["kind"]: s for s in c.scans if s["kind"] in ("X1", "X2") and s["pass"] == 1}
    if not joint:
        return
    band = float(((c.rec or {}).get("settings") or {}).get("band_hz") or 5000.0)
    fig, axes = plt.subplots(2, 1, figsize=(12, 8.2), layout="constrained")
    ax = axes[0]
    shaded = False
    for j, s in enumerate(joint):
        z = npz_of(s)
        if z is None:
            continue
        tl = z["t_leg1"]
        ms, dt = (tl - tl[0]) * 1e3, float(np.median(np.diff(tl)))
        d = corrmod.lowpass(z["leg1_d_light"], dt, band) * 1e3
        v = worst(s["verdict"])
        lab = ("before" if s["kind"] == "before" else f"after pass {s['pass']}") + (
            f": top {v['top_lt_mdeg']:+.0f}, after the fall {v['after_fall_lt_mdeg']:+.0f}, rms {v['lt_rms_mdeg']:.0f} mdeg"
            if v else "")
        ax.plot(ms, d, color=PASS_COL[min(j, len(PASS_COL) - 1)], lw=1.1 if j else 1.3, label=lab)
        if not shaded and s.get("verdict"):
            for win, colr in optical.verdict_windows(s["verdict"]):
                ax.axvspan(win[0], win[1], color=colr, alpha=0.5, lw=0, zorder=0)
            shaded = True
    ax.axhline(0, color=R.INK2, lw=0.6)
    ax.set_ylabel("light - target (mdeg)")
    ax.set_xlabel("time from the leg start (ms)")
    legend_out(ax)
    ax = axes[1]
    total = None
    for key, s in sorted(solo.items()):
        z = npz_of(s)
        if z is None:
            continue
        tl = z["t_leg1"]
        ms, dt = (tl - tl[0]) * 1e3, float(np.median(np.diff(tl)))
        d = corrmod.lowpass(z["leg1_d_lm"], dt, band) * 1e3
        v = worst(s["verdict"], "top_lm_mdeg")
        ax.plot(ms, d, color=CH_COL[key], label=f"{key} alone" + (f": top {v['top_lm_mdeg']:+.0f} mdeg" if v else ""))
        total = d if total is None else (total + d if len(total) == len(d) else total)
    if total is not None and len(solo) == 2:
        ax.plot(ms, total, color=R.INK, lw=0.8, ls=":", label="X1 alone + X2 alone")
    z = npz_of(joint[0])
    if z is not None:
        tl = z["t_leg1"]
        ms, dt = (tl - tl[0]) * 1e3, float(np.median(np.diff(tl)))
        ax.plot(ms, corrmod.lowpass(z["leg1_d_lm"], dt, band) * 1e3, color=PASS_COL[0], label="both, before")
    ax.axhline(0, color=R.INK2, lw=0.6)
    ax.set_ylabel("light - monitors (mdeg)")
    ax.set_xlabel("time from the leg start (ms)")
    legend_out(ax)
    R.finish(fig, f"Light against the base target through the optical passes, {c.stem}",
             f"Top: light minus the base target over the record before the close and after each pass, low-passed "
             f"at {band:g} Hz; shaded per transport: the ramp top (yellow) and the first ms after the fall (blue). "
             "Bottom: light minus monitors with X1 driving alone (X2 parked at idle), with X2 alone, their sum, "
             "and both together before the close; the correction each pass applies is minus this difference, "
             "per crystal.", out)
    plt.close(fig)


def fig7_legs(c, R, out):
    import matplotlib.pyplot as plt
    tr = c.transports()
    joint = [s for s in c.scans if s["kind"] in ("before", "verify")]
    if tr is None:
        return None
    a1, b1, shift = tr
    pad = int(round(0.3e-3 / c.dt))
    i0 = max(0, a1 - pad)
    n = min(shift, len(c.t) - (i0 + shift)) - pad
    if n <= 0:
        return None
    sl1, sl2 = slice(i0, i0 + n), slice(i0 + shift, i0 + shift + n)
    tms = (np.arange(n) - (a1 - i0)) * c.dt * 1e3
    fig, axes = plt.subplots(2, 1, figsize=(12, 8.0), layout="constrained")
    ax = axes[0]
    numbers = {}
    for k in ("X1", "X2"):
        if k not in c.meas:
            continue
        its = sorted(c.meas[k])
        ps = c.passes(k)
        show = [i for i in (([i for i in its if not ps or i < ps[0]] or [its[0]])[-1], its[-1])]
        for it in dict.fromkeys(show):
            y = c.meas[k][it] * c.mon_scale(k)
            d = y[sl2] - y[sl1]
            p = c.pass_of(k, it)
            ax.plot(tms, d, color=CH_COL2[k] if p else CH_COL[k], lw=0.9,
                    label=f"{k} i{it:02d}" + (f" after optical pass {p}" if p else " electrical") + f": rms {np.sqrt(np.mean(d ** 2)):.2f} V")
            numbers[f"{k} i{it:02d}"] = float(np.sqrt(np.mean(d ** 2)))
    ax.axhline(0, color=R.INK2, lw=0.6)
    ax.set_ylabel("measured monitor, leg 2 - leg 1 (V at the EOM)")
    ax.set_xlabel("time from the leg's transport start (ms)")
    legend_out(ax)
    ax = axes[1]
    band = float(((c.rec or {}).get("settings") or {}).get("band_hz") or 5000.0)
    for j, s in enumerate((joint[:1] + joint[-1:]) if len(joint) > 1 else joint):
        z = npz_of(s)
        v = s.get("verdict") or {}
        trs = ((v.get("legs") or [{}])[0].get("transports")) or []
        if z is None or len(trs) < 2:
            continue
        tl = z["t_leg1"]
        ms_all, dt = (tl - tl[0]) * 1e3, float(np.median(np.diff(tl)))
        la1, la2 = float(trs[0]["landmarks_ms"][0]), float(trs[1]["landmarks_ms"][0])
        k0 = int(np.searchsorted(ms_all, la1 - 0.3))
        kk = int(round((la2 - la1) / (dt * 1e3)))
        nn = min(kk, len(ms_all) - (k0 + kk)) - int(round(0.3 / (dt * 1e3)))
        if nn <= 0:
            continue
        s1, s2 = slice(k0, k0 + nn), slice(k0 + kk, k0 + kk + nn)
        tt = ms_all[s1] - la1
        before = s["kind"] == "before"
        # four series, four hues: light before (purple) / after (green), monitors before (pink) / after (orange)
        hues = {("light", True): PASS_COL[0], ("light", False): "#1baf7a",
                ("monitors", True): "#e87ba4", ("monitors", False): MON}
        for name, x in (("light", z["leg1_phi_light"]), ("monitors", z["leg1_phi_mon"])):
            d = corrmod.lowpass(x[s2] - x[s1], dt, band) * 1e3
            lab = ("before" if before else f"after pass {s['pass']}") + f", {name}: rms {np.sqrt(np.mean(d ** 2)):.0f} mdeg"
            ax.plot(tt, d, color=hues[(name, before)], lw=1.0, label=lab)
            numbers[f"{s['scan']} {name}"] = float(np.sqrt(np.mean(d ** 2)))
    ax.axhline(0, color=R.INK2, lw=0.6)
    ax.set_ylabel("leg 2 - leg 1 (mdeg)")
    ax.set_xlabel("time from the leg's transport start (ms)")
    legend_out(ax)
    R.finish(fig, f"The spin echo's second leg against its first, {c.stem}",
             f"Legs {shift * c.dt * 1e3:.3f} ms apart. Top: measured monitor on leg 2 minus leg 1 for the last "
             f"electrical iteration (X1 blue, X2 orange) and the last iteration after the optical close (X1 purple, "
             f"X2 red). Bottom: the light and the monitors, leg 2 minus leg 1, before the close (light purple, "
             f"monitors pink) and after the last pass (light green, monitors orange), low-passed at {band:g} Hz.", out)
    plt.close(fig)
    return numbers


def fig8_quality(c, R, out):
    """PD screen use per analyzer angle in the last scan, per-scan fit numbers,
    the analyzer zero, kept shots and dither from the log."""
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(14, 8.4), layout="constrained")
    q = {}
    notes = []
    ax = axes[0, 0]
    last = c.scans[-1] if c.scans else None
    if last:
        try:
            live = optical.live_scan(last["folder"])
            t, I, th = live["t_ms"], live["I"], live["theta"]
            for j in np.argsort(th):
                ax.plot(t, I[j], lw=0.6, color=plt.cm.twilight(float(th[j] % 360) / 360.0))
            st = [s for s in json.load(open(os.path.join(last["folder"], f"{last['scan']}_scan.json"),
                                                 encoding="utf-8"))["steps"] if s.get("scales")]
            if st:
                vdiv, off = (float(x) for x in st[0]["scales"]["1"])
                lo, hi = off - 4 * vdiv, off + 4 * vdiv
                ax.axhline(hi, color=R.INK2, lw=0.8, ls=":")
                ax.axhline(lo, color=R.INK2, lw=0.8, ls=":")
                q["pd_screen"] = (lo, hi, float(np.nanmax(I)), float(np.nanmin(I)))
                notes.append(f"PD screen {lo:+.1f} to {hi:+.1f} V ({vdiv:g} V/div, offset {off:+.2f} V), "
                             f"PD {np.nanmin(I):+.3f} to {np.nanmax(I):+.3f} V")
            ax.set_title(f"PD per analyzer angle, {last['scan']} ({len(th)} angles)")
        except Exception as e:
            ax.text(0.5, 0.5, f"{type(e).__name__}: {e}", transform=ax.transAxes, ha="center", fontsize=10)
    ax.set_xlabel("time from the trigger (ms)")
    ax.set_ylabel("PD (V)")
    ax = axes[0, 1]
    rows = []
    for s in c.scans:
        p = os.path.join(s["folder"], "analysis", "target_compare", "summary.json")
        if not os.path.exists(p):
            continue
        sm = json.load(open(p, encoding="utf-8"))
        v = s.get("verdict") or {}
        rows.append((s["scan"], float(sm.get("fit_rms_mV", np.nan)), float(sm.get("sig_psi_mdeg_median", np.nan)),
                     float(v.get("psi_rest_deg", np.nan)), float(v.get("zero_off_deg", np.nan))))
    if rows:
        x = np.arange(len(rows))
        ax.plot(x, [r[1] for r in rows], "o-", color=CH_COL["X1"], label="harmonic fit residual (mV rms)")
        ax.plot(x, [r[2] for r in rows], "s-", color=CH_COL["X2"], label="angle noise per sample, median (mdeg)")
        ax.set_xticks(x)
        ax.set_xticklabels([r[0].replace(c.stem + "_", "") for r in rows], rotation=30, ha="right", fontsize=10.5)
        legend_out(ax)
        notes.append("analyzer zero off by " + ", ".join(f"{r[4]:+.2f}" for r in rows) + " deg from crossed + 90")
    ax.set_title("Per scan: fit residual and angle noise")
    ax.set_xlabel("scan")
    ax.set_ylabel("mV rms / mdeg")
    q["scans"] = rows
    # kept shots and dither lines from the log
    ax = axes[1, 0]
    kept2 = [(int(m.group(1)), int(m.group(2))) for m in (re.search(r"(\d+) of (\d+) shots kept", ln) for ln in c.log) if m]
    if kept2:
        ax.plot(range(len(kept2)), [a / b for a, b in kept2], "o-", color=CH_COL["X1"])
        ax.set_ylim(0, 1.05)
        notes.append(f"{len(kept2)} measurements with dropped shots, lowest {min(a / b for a, b in kept2):.2f} kept")
    else:
        ax.text(0.5, 0.5, "no dropped shots in the log" if c.log is not None else "no log given",
                transform=ax.transAxes, ha="center", color=R.MUTED)
    ax.set_title("Shots kept per measurement with drops")
    ax.set_xlabel("measurement")
    ax.set_ylabel("fraction kept")
    q["dropped"] = kept2
    ax = axes[1, 1]
    its = sorted(c.pd)
    if its:
        flo = [float(c.pd[i][1].get("floor_rms", np.nan)) for i in its]
        ax.plot(its, np.asarray(flo) * 1e3, "o-", color=CH_COL["X1"], label="PD shot-mean noise floor (mV rms)")
        pk = [float(np.max(c.pd[i][0])) for i in its]
        ax2 = ax.twinx()
        ax2.plot(its, pk, "s-", color=CH_COL["X2"], label="PD peak (V)")
        ax2.set_ylabel("PD peak (V)")
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, loc="upper left", bbox_to_anchor=(1.12, 1.0), borderaxespad=0.0, fontsize=10.5)
        deg = [float(c.pd[i][1].get("analyzer_deg", np.nan)) for i in its]
        notes.append(f"analyzer during training at {np.nanmedian(deg):+.1f} deg (polarimeter frame)")
    ax.set_title("PD during the electrical iterations")
    ax.set_xlabel("iteration")
    ax.set_ylabel("mV rms")
    R.finish(fig, f"Data quality, {c.stem}",
             "Top left: the last scan's PD intensity per analyzer angle against the scope screen's limits "
             "(dotted). Top right: each comparison's harmonic-fit residual and per-sample angle noise, the "
             "analyzer zero's offset from crossed + 90 in the note. Bottom left: measurements the batch "
             "dropped shots from (scope reads that failed), fraction kept. Bottom right: the analyzer "
             "photodiode's noise floor and peak through the electrical iterations. " + "; ".join(notes) + ".", out)
    plt.close(fig)
    return q


# ------------------------------------------------------------------ readme
def readme(c, stats4, legs7, q8, out_dir):
    L = [f"# {c.stem}: figure set", "",
         f"Plan `{c.plan.get('name', '')}`, run folder `{c.run_dir}`, written {datetime.datetime.now():%Y-%m-%d %H:%M}.", ""]
    L += ["| figure | what |", "|---|---|",
          "| fig1_targets_and_drives | base targets, the moved targets, the drives played now |",
          "| fig2_monitor_error_iterations | target minus measured monitor through the iterations |",
          "| fig3_convergence | peak and rms error per iteration, optical passes marked |",
          "| fig4_drive_noise | drive spectrum against the target's shape; above-band content on the hold |",
          "| fig5_light_monitors_target | light, monitors and target per joint scan |",
          "| fig6_light_minus_target_passes | light minus target through the passes; attribution |",
          "| fig7_echo_legs | the second transport against the first, monitors and light |",
          "| fig8_data_quality | PD screen use, fit residuals, dropped shots, analyzer zero |", ""]
    L += ["## Electrical", ""]
    for k in ("X1", "X2"):
        h = c.history(k)
        if h:
            L.append(f"- {k}: {len(h)} iterations, last peak {float(h[-1]['peak_err_hv']):.1f} V, rms "
                     f"{float(h[-1]['rms_err_hv']):.2f} V at the EOM; optical passes at "
                     + (", ".join(f"i{p:02d}" for p in c.passes(k)) or "none"))
    if stats4:
        for k, s in stats4.items():
            L.append(f"- {k} above the loop band ({s['f_cut'] / 1e3:g} kHz) on the hold: drive now {s['drive_hp_mV']:.1f} mV rms "
                     f"at the AWG; measured monitor {', '.join(f'{x:.1f}' for x in s['mon_hp_V'])} V rms by iteration")
    L += ["", "## Optical", ""]
    if c.scans:
        L += ["| pass | scan | top light-target | after fall | lt rms | top light-mon | correction X1 / X2 | zero |",
              "|---|---|---|---|---|---|---|---|"]
        for s in c.scans:
            v = s.get("verdict") or {}
            w = worst(v)
            cr = (v.get("correction") or {}).get("crystals") or {}
            L.append(f"| {s['pass']} | {s['label']}: {s['scan']} | {w.get('top_lt_mdeg', float('nan')):+.0f} | "
                     f"{w.get('after_fall_lt_mdeg', float('nan')):+.0f} | {w.get('lt_rms_mdeg', float('nan')):.0f} | "
                     f"{w.get('top_lm_mdeg', float('nan')):+.0f} | "
                     + " / ".join(f"{cr[x]['peak_mdeg']:.0f}" for x in ("EO1", "EO2") if x in cr)
                     + f" | {v.get('psi_rest_deg', float('nan')):+.2f} ({'OK' if v.get('zero_ok') else 'off ' + format(v.get('zero_off_deg', float('nan')), '+.2f')}) |")
        L.append("")
        L.append("All in mdeg from the worst transport; light minus monitors is the same before and after a "
                 "correction (the monitors move with the light), so a pass shows in light minus target.")
    else:
        L.append("no optical stage on disk")
    if legs7:
        L += ["", "## Spin echo legs", ""] + [f"- {k}: leg 2 minus leg 1 rms {v:.2f}" + (" V" if " i" in k else " mdeg") for k, v in legs7.items()]
    L += ["", "## Data quality", ""]
    if q8:
        if "pd_screen" in q8:
            lo, hi, mx, mn = q8["pd_screen"]
            L.append(f"- PD in the last scan {mn:+.3f} to {mx:+.3f} V on a screen of {lo:+.1f} to {hi:+.1f} V "
                     f"({(mx - mn) / (hi - lo) * 100:.0f} % of it used; nothing at the limits means no clipping)")
        for r in q8.get("scans", []):
            L.append(f"- {r[0]}: fit residual {r[1]:.2f} mV rms, angle noise {r[2]:.1f} mdeg per sample, rest at {r[3]:+.2f} deg ({r[4]:+.2f} off)")
        d = q8.get("dropped") or []
        L.append(f"- measurements with dropped shots: {len(d)}" + (f", lowest fraction kept {min(a / b for a, b in d):.2f}" if d else ""))
    for ln in c.log[:20]:
        L.append(f"- log: {ln.strip()}")
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def make(plan, stem, log_path=None, copy_to=None):
    R = rp()
    R.style()
    c = Campaign(plan, stem, log_path)
    if "X1" not in c.states:
        print(f"{stem}: no state in {c.run_dir}")
        return None
    out_dir = os.path.join(c.run_dir, "figures", stem)
    os.makedirs(out_dir, exist_ok=True)
    f = lambda name: os.path.join(out_dir, name)              # noqa: E731
    fig1_targets(c, R, f("fig1_targets_and_drives.png"))
    fig2_error(c, R, f("fig2_monitor_error_iterations.png"))
    fig3_convergence(c, R, f("fig3_convergence.png"))
    s4 = fig4_drive_noise(c, R, f("fig4_drive_noise.png"))
    fig5_scans(c, R, f("fig5_light_monitors_target.png"))
    fig6_passes(c, R, f("fig6_light_minus_target_passes.png"))
    l7 = fig7_legs(c, R, f("fig7_echo_legs.png"))
    q8 = fig8_quality(c, R, f("fig8_data_quality.png"))
    readme(c, s4, l7, q8, out_dir)
    if copy_to:
        dst = os.path.join(copy_to, stem)
        os.makedirs(dst, exist_ok=True)
        for fn in os.listdir(out_dir):
            shutil.copy2(os.path.join(out_dir, fn), os.path.join(dst, fn))
    print(f"{stem}: {len(os.listdir(out_dir))} files in {out_dir}" + (f", copied to {copy_to}" if copy_to else ""))
    return out_dir


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    ap.add_argument("stems", nargs="*", help="campaign stems (default: every campaign with a state)")
    ap.add_argument("--log", help="the batch log (kept shots, dither, dropped reads)")
    ap.add_argument("--copy-to", help="mirror each campaign's folder there (<folder>/<stem>/)")
    a = ap.parse_args(argv)
    plan = ilc_batch.load_plan(a.plan)
    stems = a.stems or [c["stem"] for c in plan["campaigns"]]
    for stem in stems:
        make(plan, stem, a.log, a.copy_to)


if __name__ == "__main__":
    main()
