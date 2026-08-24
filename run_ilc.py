#!/usr/bin/env python3
"""Driver for the EO pre-distortion / ILC loop.

Typical session
---------------
1. Emit the model-based first shot for a target waveform:

     python run_ilc.py init --target waveform_tuned_10kHz_4p8ms.csv --channel EO1 \
            --out drive_EO1_iter0.csv

2. Play drive_EO1_iter0.csv, capture >=256 averages, then:

     python run_ilc.py step --state drive_EO1.state.npz \
            --measured "scope/EO1_iter0*.csv" --mon-col CH3 \
            --out drive_EO1_iter1.csv

   Repeat step 2 until the reported peak error stops falling.

3. Emit NI coarse/fine channel files for the converged drive:

     python run_ilc.py emit-ni --state drive_EO1.state.npz --channel EO2 --bits 16

The state file carries the target, the plant, the current drive and the error
history, so each `step` is a single command.
"""
from __future__ import annotations
import argparse, glob, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eomilc import scope, plant as plantmod, ilc, outputs
from eomilc.config import CHANNELS, LIMITS, HV_PER_MON

# The AWG GUI lists and previews whatever lives in its Waveforms folder, so a
# drive written there shows up in the memory list without being moved by hand.
# WAVE_CACHE in bk4063b_awg_gui.py is <that repo>/Waveforms; keep these in step.
AWG_WAVEFORMS = os.environ.get(
    "BK4063B_WAVEFORMS",
    r"C:\Users\mzd416\Desktop\BK4063B-AWG-GUI\Waveforms")


# --------------------------------------------------------------------- utils
def load_target(path: str):
    """Target waveform in EOM volts -> (t seconds, v monitor volts)."""
    df = pd.read_csv(path, comment="#")
    cols = {c.lower(): c for c in df.columns}
    tcol = cols.get("time_us") or cols.get("time_s") or df.columns[0]
    vcol = cols.get("voltage_v") or df.columns[1]
    t = df[tcol].to_numpy(float)
    t = t * 1e-6 if "us" in tcol.lower() else t
    return t, df[vcol].to_numpy(float) / HV_PER_MON


def save_state(path, **kw):
    np.savez(path, **kw)
    return path


def load_state(path):
    z = np.load(path, allow_pickle=True)
    return {k: z[k] for k in z.files}


def build_loop(state):
    ch = CHANNELS[str(state["channel"])]
    p = plantmod.Plant(gain=float(state["gain"]), tau=float(state["tau"]),
                       offset=float(state["offset"]), tau2=float(state.get("tau2", 0.0)),
                       fn=float(state.get("fn", 0.0)), zeta=float(state.get("zeta", 0.0)),
                       dt=float(state["dt"]))
    loop = ilc.Loop(plant=p, target=state["target"], dt=float(state["dt"]), channel=ch,
                    gamma=float(state["gamma"]), f_cut=float(state["f_cut"]))
    loop.history = list(state["history"]) if "history" in state else []
    return loop


# ---------------------------------------------------------------------- init
def cmd_init(a):
    t, v = load_target(a.target)
    dt = float(np.median(np.diff(t)))
    ch = CHANNELS[a.channel]
    amp = float(np.ptp(v))

    p = ch.plant(amp, dt, model=a.model)
    if a.gain:
        p.gain = a.gain
    if a.fn:
        p.fn = a.fn
    if a.zeta:
        p.zeta = a.zeta
    if a.tau:
        p.tau, p.fn, p.zeta = a.tau * 1e-6, 0.0, 0.0   # explicit one-pole override
    gain, tau = p.gain, p.tau

    loop = ilc.Loop(plant=p, target=v, dt=dt, channel=ch, gamma=a.gamma, f_cut=a.f_cut)
    u = loop.first_shot()

    print(f"channel     : {ch.name}")
    print(f"target      : {np.ptp(v)*HV_PER_MON:.0f} V peak-to-peak over {t[-1]*1e3:.2f} ms")
    print(f"plant       : {p}")
    print(f"uncorrected : peak error {np.abs(p.forward(v/gain)-v).max()*HV_PER_MON:.0f} V")
    print(f"modelled    : peak error {np.abs(p.forward(u)-v).max()*HV_PER_MON:.1f} V "
          f"(what the first shot should land at if the model is right)")
    print(f"drive       : peak {np.abs(u).max():.4f} V, "
          f"overshoot {(np.abs(u).max()-np.abs(v).max()/gain)*1e3:.0f} mV over the flat-top demand")
    print("\nlimit check :", loop.check(u))

    out = a.out or f"drive_{ch.name}_iter0.csv"
    outputs.write_awg_csv(out, t, u, comment=f"{ch.name} ILC iteration 0 (model-based)\n{p}")
    wname = f"{a.name or ch.name}_i00"
    gui = os.path.join(a.awg_dir, wname + ".csv")
    os.makedirs(a.awg_dir, exist_ok=True)
    outputs.write_bk_waveform(gui, u, wname, a.full_scale)
    st = os.path.splitext(out)[0].rsplit("_iter", 1)[0] + ".state.npz"
    save_state(st, t=t, target=v, u=u, dt=dt, channel=ch.name, gain=gain, tau=tau,
               offset=0.0, tau2=0.0, fn=p.fn, zeta=p.zeta,
               full_scale=a.full_scale, name=(a.name or ch.name),
               gamma=a.gamma, f_cut=a.f_cut, iteration=0,
               t_offset=a.t_offset * 1e-6, history=np.array([], dtype=object))
    print(f"\nwrote {out}\n      {gui}  (GUI-ready, normalised)\nstate {st}")


# ---------------------------------------------------------------------- step
def cmd_step(a):
    st = load_state(a.state)
    loop = build_loop(st)
    t = st["t"]; u_k = st["u"]; it = int(st["iteration"])
    t_off = float(st["t_offset"]) if a.t_offset is None else a.t_offset * 1e-6

    files = sorted(glob.glob(a.measured))
    if not files:
        sys.exit(f"no scope files matched {a.measured!r}")
    traces = []
    for f in files:
        tr = scope.load(f)
        traces.append(scope.resample(tr.t, tr[a.mon_col], t, t_offset=t_off))
    y = ilc.averaged(traces)
    if a.zero_baseline:
        base = y[t < t[0] + 0.05 * (t[-1] - t[0])].mean()
        tgt_base = st["target"][t < t[0] + 0.05 * (t[-1] - t[0])].mean()
        if abs(tgt_base) > 0.01 * np.ptp(st["target"]):
            print(f"  WARNING: --zero-baseline, but the target already averages "
                  f"{tgt_base*HV_PER_MON:.0f} V over that window -- this is "
                  f"subtracting signal, not baseline.")
        y = y - base

    m = loop.metrics(y)
    print(f"iteration {it}: {len(files)} trace(s) averaged")
    print(f"  measured error : peak {m['peak_err_hv']:7.1f} V   rms {m['rms_err_hv']:6.2f} V"
          f"   ({m['peak_pct']:.2f}% of full scale)")

    if a.refit:
        p2, info = plantmod.identify(u_k, y, loop.dt, model=a.model)
        print(f"  refit plant    : {p2}  (residual {info['resid_peak_pct']:.2f}% peak)")
        loop.plant = p2

    u_next = loop.update(u_k, y)
    rep = loop.check(u_next)
    print("  limit check    :", rep)
    if not rep and not a.force:
        sys.exit("refusing to write a drive that violates a hard limit (use --force to override)")

    out = a.out or f"drive_{st['channel']}_iter{it+1}.csv"
    outputs.write_awg_csv(out, t, u_next,
                          comment=f"{st['channel']} ILC iteration {it+1}\n{loop.plant}")
    wname = f'{st["name"]}_i{it+1:02d}'
    gui = os.path.join(a.awg_dir, wname + ".csv")
    os.makedirs(a.awg_dir, exist_ok=True)
    outputs.write_bk_waveform(gui, u_next, wname, float(st["full_scale"]))
    save_state(a.state, t=t, target=st["target"], u=u_next, dt=loop.dt, channel=str(st["channel"]),
               gain=loop.plant.gain, tau=loop.plant.tau, offset=loop.plant.offset,
               tau2=loop.plant.tau2, fn=loop.plant.fn, zeta=loop.plant.zeta,
               full_scale=float(st["full_scale"]), name=str(st["name"]),
               gamma=loop.gamma, f_cut=loop.f_cut,
               iteration=it + 1, t_offset=t_off,
               history=np.array(loop.history, dtype=object))
    print(f"\n{loop.report()}\n\nwrote {out}\n      {gui}  (GUI-ready, normalised)")


# ------------------------------------------------------------------- emit-ni
def cmd_emit_ni(a):
    st = load_state(a.state)
    ch = CHANNELS[a.channel or str(st["channel"])]
    t, u = st["t"], st["u"]
    print(outputs.resolution_table(ch))
    if ch.has_fine_channel:
        cf = outputs.split_coarse_fine(u, ch, bits=a.bits, dc_offset=a.dc_offset)
        print("\n", cf)
        err = np.abs(cf.realised - u).max()
        print(f" worst quantisation error {err*1e6:.2f} uV "
              f"= {err*ch.divider*ch.amp_mon_product*HV_PER_MON*1e3:.2f} mV at the EOM")
        outputs.write_awg_csv(f"ni_{ch.name}_coarse.csv", t, cf.coarse, "coarse channel")
        outputs.write_awg_csv(f"ni_{ch.name}_fine.csv", t, cf.fine, "fine channel (1:%d)" % ch.fine_ratio)
        print(f"wrote ni_{ch.name}_coarse.csv and ni_{ch.name}_fine.csv")
    else:
        q, lsb = outputs.quantise(u, bits=a.bits)
        err = np.abs(q - u).max()
        print(f"\n single channel, {a.bits} bit: LSB {lsb*1e6:.1f} uV "
              f"= {lsb*ch.divider*ch.amp_mon_product*HV_PER_MON:.3f} V at the EOM")
        print(f" worst quantisation error {err*ch.divider*ch.amp_mon_product*HV_PER_MON:.3f} V at the EOM")
        outputs.write_awg_csv(f"ni_{ch.name}_single.csv", t, q, "single channel, no fine trim")
        print(f"wrote ni_{ch.name}_single.csv")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="model-based first shot")
    i.add_argument("--target", required=True)
    i.add_argument("--channel", required=True, choices=list(CHANNELS))
    i.add_argument("--out")
    i.add_argument("--model", default="resonant", choices=plantmod.MODELS,
                   help="which calibration table to seed from (default: resonant, "
                        "which is what this chain actually is)")
    i.add_argument("--gain", type=float, help="override AWG->monitor gain")
    i.add_argument("--fn", type=float, help="override resonance, Hz")
    i.add_argument("--zeta", type=float, help="override damping ratio")
    i.add_argument("--tau", type=float,
                   help="force a ONE-POLE model with this tau in microseconds. "
                        "Diagnostic only -- it makes the loop diverge at fn.")
    i.add_argument("--gamma", type=float, default=0.6)
    i.add_argument("--f-cut", type=float, default=20e3)
    i.add_argument("--awg-dir", default=AWG_WAVEFORMS,
                   help="where to write the upload-ready waveform. Defaults "
                        "to the AWG GUI's Waveforms folder, so it shows up in "
                        "its memory list directly. Override with the "
                        "BK4063B_WAVEFORMS environment variable.")
    i.add_argument("--full-scale", type=float, default=10.0,
                   help="AWG volts at DAC full scale; AMP must be twice this "
                        "with OFST 0 (default 10.0 -> AMP 20 Vpp)")
    i.add_argument("--name", help="waveform name stem for the AWG (max 11 chars "
                                  "including the '_i00' suffix)")
    i.add_argument("--t-offset", type=float, default=0.0,
                   help="fixed trigger-to-waveform offset, microseconds")
    i.set_defaults(func=cmd_init)

    s = sub.add_parser("step", help="one ILC iteration from measured traces")
    s.add_argument("--state", required=True)
    s.add_argument("--measured", required=True, help="glob of scope CSVs to average")
    s.add_argument("--mon-col", default="CH3")
    s.add_argument("--out")
    s.add_argument("--t-offset", type=float, default=None)
    s.add_argument("--awg-dir", default=AWG_WAVEFORMS,
                   help="where to write the upload-ready waveform. Defaults "
                        "to the AWG GUI's Waveforms folder, so it shows up in "
                        "its memory list directly. Override with the "
                        "BK4063B_WAVEFORMS environment variable.")
    s.add_argument("--refit", action="store_true",
                   help="re-identify the plant from this iteration")
    s.add_argument("--model", default="resonant", choices=plantmod.MODELS,
                   help="model form for --refit")
    s.add_argument("--zero-baseline", action=argparse.BooleanOptionalAction, default=False,
                   help="subtract the mean of the first 5%% of the record. ONLY valid "
                        "if the waveform is actually flat there -- MKJ is already "
                        "ramping by then, so this subtracts real signal and roughly "
                        "doubles the reported error. Was previously on by default and "
                        "impossible to switch off.")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_step)

    n = sub.add_parser("emit-ni", help="split the converged drive for NI cards")
    n.add_argument("--state", required=True)
    n.add_argument("--channel", choices=list(CHANNELS))
    n.add_argument("--bits", type=int, default=16)
    n.add_argument("--dc-offset", type=float, default=0.0,
                   help="standing EO-zero offset in summing-node volts")
    n.set_defaults(func=cmd_emit_ni)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
