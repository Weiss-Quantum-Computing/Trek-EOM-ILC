#!/usr/bin/env python3
"""Closed-loop ILC on the bench: BK4063B upload -> MSO-X capture -> update.

Reuses the instrument layers already written for Scope Grab and the AWG GUI --
`Scope` and `Awg` are plain classes with no Tk dependency, so this imports them
rather than reimplementing SCPI that has already been debugged against the
hardware.

    python ilc_bench.py --channel EO1 --target waveforms/target_MKJX1.csv \
        --name MKJX1 --awg-ch 1 --scope-ch 3 --t-offset 0 --iterations 3

    python ilc_bench.py --resume run/drive_MKJX1.state.npz --awg-ch 1 \
        --scope-ch 3 --iterations 2      # continue where the manual loop left off

MEASUREMENT SCHEME (measured on this bench, 2026-08-24)
-------------------------------------------------------
Set the scope to HRES and let --repeats (default 64) average the single shots
in software.  Do NOT use the scope's AVER mode here: :SINGle takes exactly one
acquisition, so an AVER capture through this script carries one hit while
claiming the full depth.  Averaged HRES singles also dither the instrument's
2.5 mV word lattice away (per-shot noise 3.5 mV rms), putting the measurement
floor near 0.5-1 V at the EOM -- below what hardware averaging can deliver.

Both GUIs hold their own VISA sessions; close or disconnect them first.

SAFETY POSTURE
--------------
This script does NOT set amplitude, offset, load, sample clock, and never
switches an output ON.  Set the channel up in the AWG GUI, turn the output on
there, and confirm on the monitor that you are where you expect.  This script
only:

  * uploads a waveform into user memory and selects it,
  * arms the scope and reads a trace back,
  * switches the driven channel's output OFF when a run that actually played
    something ends -- off is the harmless direction, and a finished run must
    not leave the chain driving.

Before the first upload it VERIFIES the channel is configured the way the drive
file assumes, and refuses to run if it is not.  A mismatch here silently
rescales the drive -- see the note on normalisation below.

THE NORMALISATION TRAP
----------------------
`Awg.upload_arb(..., normalize=True)` divides the samples by their own peak.
That is right for a one-off waveform and WRONG for ILC: each iteration has a
slightly different peak, so re-normalising every round quietly rescales the
correction the loop just computed, and the loop stops converging for reasons
that look like plant drift.

This script uploads with normalize=False against a FIXED full scale, so the
DAC mapping is identical on every iteration and the amplitude correction lands
where it was meant to.
"""
from __future__ import annotations
import argparse, contextlib, importlib.util, os, sys, time
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from eomilc import scope as scopeio, plant as plantmod, ilc, outputs
from eomilc.config import CHANNELS, HV_PER_MON


def find_scope_grab(siblings):
    """The scope repo was renamed keysight-scope-grab on GitHub (26 Aug
    2026); a pre-rename checkout (this bench PC) keeps the old folder
    name. Prefer the new name, fall back to the old, and when neither
    exists return the NEW path so the error message names the right thing
    to clone. The SCOPE_GRAB env var overrides everything."""
    env = os.environ.get("SCOPE_GRAB")
    if env:
        return env
    for name in ("keysight-scope-grab", "scope-grab"):
        p = os.path.join(siblings, name, "scope_grab.py")
        if os.path.exists(p):
            return p
    return os.path.join(siblings, "keysight-scope-grab", "scope_grab.py")


def find_spectrum_grab(siblings):
    """Where sr760.py lives - the SR760 instrument layer and scripting library.

    Same shape as find_scope_grab: the SPECTRUM_GRAB env var wins, then the
    sibling checkout, and when it is missing return the canonical path anyway so
    the error names the right thing to clone. sr760.py sits beside
    spectrum_grab.py in that repo, the way bk4063b.py sits beside its panel.
    """
    env = os.environ.get("SPECTRUM_GRAB")
    if env:
        return env
    for name in ("Spectrum-grab", "spectrum-grab"):
        p = os.path.join(siblings, name, "sr760.py")
        if os.path.exists(p):
            return p
    return os.path.join(siblings, "Spectrum-grab", "sr760.py")


def load_module(path, name):
    """Import one of the bench programs by file path."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise ImportError(f"cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_SHARED_RM = None


def _shared_rm(pyvisa_mod):
    """One ResourceManager for the whole process, owned by neither instrument.

    pyvisa hands every default ResourceManager() caller the same cached
    instance, and both instrument layers close the RM they think they
    created -- so one instrument's close() tears the other's session down
    (measured 2026-08-26: the AWG's close killed the scope's session in the
    middle of auto-set). This RM is handed to both and closed by nobody."""
    global _SHARED_RM
    if _SHARED_RM is not None:
        try:
            _SHARED_RM.session          # raises once the RM has been closed
        except Exception:
            _SHARED_RM = None
    if _SHARED_RM is None:
        _SHARED_RM = pyvisa_mod.ResourceManager()
    return _SHARED_RM


def make_scope(mod):
    """Build the scope on the shared DEFAULT VISA, not Keysight's.

    Two measured traps (2026-08-26). First: scope_grab prefers ktvisa32.dll
    whenever the file exists. The DLL itself is healthy, but Python 3.8+
    ctypes does not search PATH for a DLL's dependencies, so in a fresh
    process it fails to load (harmless: scope_grab falls back to NI).
    Once the AWG has put NI's visa32.dll into the process, though, enough
    of the dependency chain is resident that ktvisa32 half-loads: it
    enumerates the bus and then fails every open_resource with
    VI_ERROR_ALLOC, which Scope.connect swallows per-candidate and reports
    as 'No Keysight USB instrument found'. Second: Scope.close() closes its
    RM, which must not be the shared one -- see _shared_rm."""
    scope = mod.Scope()
    scope._make_rm = lambda: _shared_rm(mod.pyvisa)
    orig_close = scope.close

    def close_keeping_rm():
        scope.rm = None                 # the shared RM is not ours to close
        orig_close()
    scope.close = close_keeping_rm
    return scope


def make_awg(mod):
    """Build the generator object from whichever module carries the class.

    The instrument layer moved out of bk4063b_awg_gui.py into bk4063b.py
    (that repo's commit 18142f9, 'One instrument layer'), renaming Awg to
    BK4063B whose constructor connects immediately unless told not to.
    Accept either vintage, never auto-connect (the callers print the IDN
    from an explicit connect()), and hand over the shared RM -- a given RM
    is one bk4063b never closes."""
    cls = getattr(mod, "BK4063B", None) or getattr(mod, "Awg")
    try:
        return cls(connect=False,
                   resource_manager=_shared_rm(mod.pyvisa))
    except TypeError:                    # the old class took no such kwargs
        try:
            return cls(connect=False)
        except TypeError:
            return cls()


def make_analyzer(mod, addr=None):
    """Build the SR760 on the shared VISA, without connecting.

    The shared RM matters here even for a set that drives only the analyser.
    The C-phase sets need the MSO-X as well - for V_DC, and for the servo bump
    above the SR760's 100 kHz ceiling - and that is exactly where the measured
    mixed-VISA failure bites: a second ResourceManager half-loads and every
    open_resource then returns VI_ERROR_ALLOC. Handing both instruments the same
    RM from the start means the analyser-only sets are not a different code path
    that happens to work.

    Never auto-connects: the caller prints the IDN from an explicit connect(),
    as the scope and AWG paths do.
    """
    return mod.SR760(addr=addr, resource_manager=_shared_rm(mod.pyvisa),
                     connect=False)


# --------------------------------------------------------------------- AWG
def check_awg_channel(awg, ch, expect_rate=None, expect_clock="DDS",
                      full_scale=10.0, tol=0.02):
    """Refuse to upload into a channel that isn't set up the way we assume."""
    blocks = awg.read_channel(ch)
    bswv = awg_parse(blocks["BSWV"])
    srate = awg_parse(blocks["SRATE"])
    outp = awg_parse(blocks["OUTP"])
    problems, notes = [], []

    amp = as_float(bswv.get("AMP"))
    ofst = as_float(bswv.get("OFST"))
    want_amp, want_ofst = 2 * full_scale, 0.0
    if amp is None or abs(amp - want_amp) > tol * want_amp:
        problems.append(f"BSWV AMP is {amp} Vpp; this drive file assumes "
                        f"{want_amp:g} Vpp (full scale +/-{full_scale:g} V)")
    # 60 mV rather than a tight zero: the generator's zero-code output sits
    # -12 mV (CH1) / -40 mV (CH2) off true zero at 20 Vpp (measured 24 Aug from
    # the inter-burst idle), and trimming OFST to cancel that is the sanctioned
    # fix for the EOM idling off zero. Anything bigger is a real setup error.
    if ofst is None or abs(ofst - want_ofst) > 0.06:
        problems.append(f"BSWV OFST is {ofst} V; this drive file assumes ~0 V "
                        f"(idle-trim offsets up to 60 mV are fine)")

    clock = srate.get("MODE")
    if expect_clock and clock != expect_clock:
        problems.append(f"sample clock is {clock}, expected {expect_clock}. "
                        f"DDS resamples the record into one period, so the point "
                        f"grid is not literal -- but it is the only mode that "
                        f"allows the triggered burst this bench runs on.")
    rate = as_float(srate.get("VALUE")) or as_float(srate.get("SRATE"))
    if expect_rate and rate and abs(rate - expect_rate) / expect_rate > 1e-3:
        problems.append(f"sample rate is {rate:g} Sa/s, expected {expect_rate:g}")

    notes.append(f"CH{ch}: output {outp.get('STATE')}, load {outp.get('LOAD')}, "
                 f"{bswv.get('WVTP')}, clock {clock} @ {rate} Sa/s, "
                 f"AMP {amp} Vpp, OFST {ofst} V")
    return problems, notes


def awg_parse(reply):
    """Thin wrapper so this file doesn't care which module parse_reply lives in."""
    return _AWGMOD.parse_reply(reply)


def as_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def upload_drive(awg, ch, name, u_awg, full_scale=10.0):
    """Upload a drive in volts, using a FIXED mapping to the DAC.

    u/full_scale lands in -1..+1, uploaded with normalize=False, so a change of
    one millivolt between iterations is a change of one millivolt at the output
    -- not a change that gets normalised away.
    """
    peak = float(np.abs(u_awg).max())
    if peak > full_scale:
        raise ValueError(f"drive peaks at {peak:.3f} V, past the {full_scale:g} V "
                         f"full scale this mapping assumes")
    # NOT a truncation: the generator accepts the upload, plays the right shape,
    # and then wedges its front panel until it is power cycled.  The limit is on
    # the stored `<name>.bin`, which is 15 characters, so a typed name gets 11.
    # Taken from the GUI so there is one definition of it on this bench.
    limit = getattr(_AWGMOD, "MAX_ARB_NAME", 11)
    if len(name) > limit:
        raise ValueError(f"waveform name {name!r} is {len(name)} chars; the 4063B "
                         f"stores it as {name}.bin and locks its front panel past "
                         f"{limit + 4} stored characters, so the cap is {limit}")
    n = awg.upload_arb(ch, name, u_awg / full_scale, normalize=False)
    return n, peak / full_scale


# ------------------------------------------------------------------- scope
def verify_alignment(scope, drive_ch, u, t_grid, t_off, wait_s):
    """One shot of the DRIVE channel, cross-correlated against the drive we
    just uploaded. A stale --t-offset poisons every measurement invisibly -- a
    250 us leftover once sent a ten-iteration run chasing its own alignment --
    and the capture can simply check, so it does."""
    got = scope.single(wait_s=wait_s)
    if got is not True:
        sys.exit("alignment check: no trigger -- is the burst running?")
    ts, vs = scope.waveform(drive_ch)
    scope.run()
    if float(np.ptp(vs)) < 0.5:
        sys.exit(f"alignment check: scope CH{drive_ch} is flat -- is the AWG "
                 f"output on, and is the drive really on that channel?")
    dt = float(np.median(np.diff(t_grid)))
    meas = scopeio.measure_t_offset(ts, vs, u, dt)
    if abs(meas - t_off) > 10e-6:
        sys.exit(f"alignment check: the drive on scope CH{drive_ch} starts at "
                 f"{meas*1e6:+.1f} us, but t-offset says {t_off*1e6:+.1f} us. "
                 f"Fix --t-offset (measured 0 on this bench) or the trigger "
                 f"wiring before iterating.")
    print(f"       alignment: drive starts {meas*1e6:+.1f} us, "
          f"t-offset {t_off*1e6:+.1f} us -- OK")


def capture_all(scope, channels, t_grid, t_offset,
                repeats=64, wait_s=30.0, points=None, settle=0.5):
    """Take `repeats` single shots and return EVERY channel, un-averaged.

    Returns {"CH<n>": (repeats, len(t_grid)) array}.  Handing back the stack
    rather than the mean is what makes the optical campaign possible: the
    ensemble MEAN is the repeatable error ILC can learn and the ensemble STD is
    the shot-to-shot part it structurally cannot, and collapsing to the mean
    inside the capture throws the second one away.  See eomilc.polarimetry for
    the split.  At 64 shots x 4 channels x a 5501-point grid the stack is a
    few MB, which is not worth optimising away.

    Every channel is read from the SAME frozen acquisition, between :SINGle and
    run(), so the traces are simultaneous and can be cross-correlated.  Reading
    them from separate triggers would silently destroy exactly that.

    The settle wait happens ONCE, not per shot -- it exists to let the chain
    settle after a new upload, and each shot already waits for its own trigger.
    64 HRES singles at the 20 Hz trigger cost ~25 s for two channels, and
    roughly scales with the channel count from there.
    """
    time.sleep(settle)
    out = {f"CH{ch}": [] for ch in channels}
    for i in range(repeats):
        got = scope.single(wait_s=wait_s)
        if got is not True:
            raise RuntimeError(f"no trigger within {wait_s:g} s on repeat {i+1} "
                               f"-- is the sequence running?")
        cols = {}
        for ch in channels:
            t, v = scope.waveform(ch, points=points)
            cols[f"CH{ch}"] = (t, v)
        scope.run()
        for col, (t_src, v_src) in cols.items():
            out[col].append(scopeio.resample(t_src, v_src, t_grid,
                                             t_offset=t_offset))
    return {col: np.asarray(rows, float) for col, rows in out.items()}


def capture(scope, channels, mon_col, t_grid, t_offset,
            repeats=64, wait_s=30.0, points=None, settle=0.5):
    """The averaged monitor trace -- what the ILC loop iterates on.

    A thin wrapper over `capture_all`; reach for that one when you want the
    other channels or the shot-to-shot spread.
    """
    stacks = capture_all(scope, channels, t_grid, t_offset, repeats=repeats,
                         wait_s=wait_s, points=points, settle=settle)
    return ilc.averaged(list(stacks[mon_col]))




# ------------------------------------------------------- scope channel state
#
# A RIN capture and a polarimetry capture want opposite scope setups. RIN needs
# AC coupling at the most sensitive V/div the signal allows, because the whole
# measurement is a few hundred microvolts of noise riding on a volt or two of
# DC, and on a DC-coupled 8-bit scope that noise lands inside one code. The
# polarimetry captures need exactly the opposite: DC coupling, because the DC
# level is what gets inverted to an angle, and AC coupling throws it away.
#
# So the noise setup has to be applied and then taken back off, and it has to
# come back off even when the capture raises. That is the snapshot()/restore()
# split bk4063b.py uses on the generator, kept deliberately in the same shape.

# The per-channel settings a noise capture touches, as {key: SCPI root}. Only
# these three, so restore() cannot reach further than the setup did.
CHANNEL_STATE = {
    "coupling": ":CHANnel{ch}:COUPling",
    "scale": ":CHANnel{ch}:SCALe",
    "offset": ":CHANnel{ch}:OFFSet",
}


def scope_snapshot(scope, channels):
    """Capture enough per-channel state to put the scope back as it was.

    Scoped to `channels` for the same reason bk4063b.snapshot takes a channel
    list: restore() only rewrites what the snapshot holds, so scoping is how a
    channel that is mid-measurement is guaranteed to be left alone.

    A setting the scope declines to report comes back absent rather than
    guessed, and restore() then leaves it where it is.
    """
    state = {"captured": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "instrument": getattr(scope, "idn", ""),
             "channels": {}}
    for ch in channels:
        got = {}
        for key, root in CHANNEL_STATE.items():
            value = scope.try_get(root.format(ch=ch))
            if value is not None:
                got[key] = value
        state["channels"][str(ch)] = got
    return state


def scope_restore(scope, state):
    """Replay a `scope_snapshot`.

    Coupling goes back first: switching from AC to DC moves the trace by the DC
    level, and doing that after the offset has been restored would leave the
    channel briefly off screen. Nothing here is destructive, but the order is
    the same reasoning bk4063b.restore uses for its outputs.
    """
    for ch, got in state.get("channels", {}).items():
        for key in ("coupling", "scale", "offset"):
            if key in got:
                scope.put(CHANNEL_STATE[key].format(ch=int(ch)), got[key])
    errs = scope.errors()
    if errs:
        print(f"       scope complained while restoring: {'; '.join(errs)}")
    return state


def scope_apply(scope, setup):
    """Apply {channel: {coupling/scale/offset: value}} to the scope.

    Coupling is written first so the offset that follows is interpreted against
    the coupling that will actually be in force.
    """
    for ch, want in setup.items():
        for key in ("coupling", "scale", "offset"):
            if key in want:
                scope.put(CHANNEL_STATE[key].format(ch=int(ch)), want[key])
    errs = scope.errors()
    if errs:
        raise RuntimeError(f"the scope rejected the noise setup: {'; '.join(errs)}")


@contextlib.contextmanager
def noise_capture(scope, setup, settle=0.5):
    """Put the scope into a noise configuration for the body, then put it back.

    Use it around `capture_all`:

        setup = {3: {"coupling": "AC", "scale": 0.01, "offset": 0.0}}
        with noise_capture(scope, setup):
            stacks = capture_all(scope, [3], t_grid, t_off, repeats=64)

    The restore runs on the way out however the body ended, which is the point
    of doing it this way rather than as two calls: a capture that raises
    half-way through must not leave the scope AC-coupled at 10 mV/div for the
    next polarimetry run, where it would silently measure the wrong thing
    rather than fail.

    `settle` lets the AC coupling network charge before the first shot. The
    high-pass is around 3.5 Hz on this scope, so a step takes a few hundred
    milliseconds to leave the screen; triggering into that tail measures the
    settling, not the noise.
    """
    saved = scope_snapshot(scope, list(setup))
    scope_apply(scope, setup)
    if settle > 0:
        time.sleep(settle)
    try:
        yield saved
    finally:
        scope_restore(scope, saved)


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", choices=list(CHANNELS),
                    help="required unless --resume carries it")
    ap.add_argument("--resume", default=None, metavar="STATE.NPZ",
                    help="continue from a run_ilc state file instead of starting "
                         "at the model first shot. Target, drive, plant, gamma, "
                         "f_cut and t-offset all come from the state, and the "
                         "state is re-saved after every iteration, so the manual "
                         "and automatic loops interleave freely.")
    ap.add_argument("--name", default=None,
                    help="waveform name stem for the upload (default: the channel "
                         "name). Iteration suffix is appended, and the total must "
                         "fit MAX_ARB_NAME = 11 characters.")
    ap.add_argument("--target", help="target waveform CSV, volts at the EOM "
                                     "(required unless --resume)")
    ap.add_argument("--awg-ch", type=int, default=1, choices=(1, 2))
    ap.add_argument("--scope-ch", type=int, default=3, help="scope channel carrying the monitor")
    ap.add_argument("--iterations", type=int, default=4)
    ap.add_argument("--repeats", type=int, default=64,
                    help="HRES single shots averaged in software per iteration. "
                         "64 dithers the scope's 2.5 mV word lattice to the 0.16 mV "
                         "floor and takes ~25 s at the 20 Hz trigger. Do NOT drop "
                         "this and lean on scope-side AVER instead -- :SINGle takes "
                         "one hit of an average, measured.")
    ap.add_argument("--t-offset", type=float, default=None,
                    help="fixed trigger-to-waveform offset in microseconds. "
                         "Measured 0 on this bench (2026-08-24). Required unless "
                         "--resume supplies it.")
    ap.add_argument("--full-scale", type=float, default=10.0)
    ap.add_argument("--sample-rate", type=float, default=None,
                    help="expected SRATE, only meaningful under TrueArb; under DDS "
                         "the record is resampled into one period and the point "
                         "grid is not literal")
    ap.add_argument("--points", type=int, default=None, help="scope transfer points")
    ap.add_argument("--wait", type=float, default=30.0)
    ap.add_argument("--model", default="resonant", choices=plantmod.MODELS)
    ap.add_argument("--gamma", type=float, default=0.6)
    # 5 kHz: the model is only trusted below ~5 kHz on this bench (see run_ilc)
    ap.add_argument("--f-cut", type=float, default=5e3)
    ap.add_argument("--outdir", default="run")
    # sibling repos found relative to this one -- see run_ilc.SIBLINGS
    siblings = os.path.dirname(HERE)
    ap.add_argument("--scope-grab", default=find_scope_grab(siblings))
    ap.add_argument("--awg-gui",
                    default=os.environ.get(
                        "AWG_GUI",
                        os.path.join(siblings, "BK4063B-AWG-GUI",
                                     "bk4063b.py")),
                    help="the AWG instrument layer -- bk4063b.py since that "
                         "repo moved the class out of its GUI file")
    ap.add_argument("--awg-dir",
                    default=os.environ.get(
                        "BK4063B_WAVEFORMS",
                        os.path.join(siblings, "BK4063B-AWG-GUI", "Waveforms")),
                    help="where the GUI-previewable copy of each uploaded drive "
                         "is written, like the manual loop does")
    ap.add_argument("--frf", default=None, metavar="FRF.CSV",
                    help="use a measured transfer function (sysid_fit output) as "
                         "the update's inverse -- corrects the 3-6 kHz band the "
                         "parametric model stalls in")
    ap.add_argument("--frf-use", type=float, default=15e3,
                    help="full-strength band edge of the measured inverse; "
                         "24 kHz tones were coherent, so up to ~20e3 is backed "
                         "by measurement (taper reaches --frf-max)")
    ap.add_argument("--frf-max", type=float, default=22e3)
    ap.add_argument("--overwrite-state", action="store_true",
                    help="allow a fresh run to replace an existing state file")
    ap.add_argument("--drive-scope-ch", type=int, default=None,
                    help="scope channel carrying the AWG drive, for the alignment "
                         "check (default: same number as --awg-ch)")
    ap.add_argument("--skip-checks", action="store_true",
                    help="upload without verifying the channel setup. Don't.")
    a = ap.parse_args()

    global _AWGMOD
    scopemod = load_module(a.scope_grab, "scope_grab")
    _AWGMOD = load_module(a.awg_gui, "bk4063b_awg_gui")

    # ---- target and loop: fresh from the model, or resumed from a state file
    if a.resume:
        st = {k: z for k, z in np.load(a.resume, allow_pickle=True).items()}
        ch = CHANNELS[str(st["channel"])]
        t, v = st["t"], st["target"]
        dt = float(st["dt"])
        model = plantmod.Plant(gain=float(st["gain"]), tau=float(st["tau"]),
                               offset=float(st["offset"]), tau2=float(st["tau2"]),
                               fn=float(st.get("fn", 0.0)),
                               zeta=float(st.get("zeta", 0.0)), dt=dt)
        loop = ilc.Loop(plant=model, target=v, dt=dt, channel=ch,
                        gamma=float(st["gamma"]), f_cut=float(st["f_cut"]))
        loop.history = list(st["history"])
        u = st["u"]
        k0 = int(st["iteration"])
        stem = a.name or str(st["name"])
        full_scale = float(st["full_scale"])
        t_off = (float(st["t_offset"]) if a.t_offset is None
                 else a.t_offset * 1e-6)
        state_path = a.resume
        print(f"resuming {ch.name} at iteration {k0}, f_cut {loop.f_cut/1e3:g} kHz")
    else:
        if not (a.channel and a.target and a.t_offset is not None):
            sys.exit("--channel, --target and --t-offset are required "
                     "unless --resume supplies them")
        ch = CHANNELS[a.channel]
        df = pd.read_csv(a.target, comment="#")
        t = df.iloc[:, 0].to_numpy(float)
        t = t * 1e-6 if "us" in df.columns[0].lower() else t
        v = df.iloc[:, 1].to_numpy(float) / ch.mon_scale    # measured volts
        dt = float(np.median(np.diff(t)))
        amp = float(np.ptp(v))
        model = ch.plant(amp, dt, model=a.model)
        loop = ilc.Loop(plant=model, target=v, dt=dt, channel=ch,
                        gamma=a.gamma, f_cut=a.f_cut)
        u = loop.first_shot()
        k0 = 0
        stem = a.name or ch.name
        full_scale = a.full_scale
        t_off = a.t_offset * 1e-6
        state_path = os.path.join(a.outdir, f"drive_{stem}.state.npz")
        if os.path.exists(state_path) and not a.overwrite_state:
            sys.exit(f"{state_path} already exists. A fresh run would destroy it "
                     f"- it did once, taking a four-iteration manual state with "
                     f"it. Use --resume {state_path} to continue it, or "
                     f"--overwrite-state to discard it deliberately.")

    if a.frf:
        loop.frf = ilc.FRF(a.frf, f_use=a.frf_use, f_max=a.frf_max)
        print(f"update uses the measured inverse from {a.frf}")

    limit = getattr(_AWGMOD, "MAX_ARB_NAME", 11)
    if len(stem) + 4 > limit:                        # "_i00" is four more
        sys.exit(f"--name {stem!r} is {len(stem)} chars; with the '_i00' suffix "
                 f"that is {len(stem)+4}, past the {limit}-character cap.")
    print(f"channel {ch.name}: {model}")
    print(f"uploads as  : {stem}_i{k0:02d} ... {stem}_i{k0+a.iterations:02d}")
    print(f"target  {np.ptp(v)*ch.mon_scale:.0f} V over {t[-1]*1e3:.2f} ms, "
          f"{len(v)} points at {dt*1e6:.3f} us")

    def save_state(iteration, u_now):
        np.savez(state_path, t=t, target=v, u=u_now, dt=dt, channel=ch.name,
                 gain=loop.plant.gain, tau=loop.plant.tau,
                 offset=loop.plant.offset, tau2=loop.plant.tau2,
                 fn=loop.plant.fn, zeta=loop.plant.zeta,
                 full_scale=full_scale, name=stem, gamma=loop.gamma,
                 f_cut=loop.f_cut, iteration=iteration, t_offset=t_off,
                 history=np.array(loop.history, dtype=object))

    # ---- instruments
    awg = make_awg(_AWGMOD)
    print("AWG:  ", awg.connect())
    scope = make_scope(scopemod)
    print("Scope:", scope.connect())

    problems, notes = check_awg_channel(awg, a.awg_ch, expect_rate=a.sample_rate,
                                        full_scale=a.full_scale)
    for n in notes:
        print("      ", n)
    acq = scope.get(":ACQuire:TYPE")
    print(f"       scope acquisition {acq}, {a.repeats} software repeats")
    if acq.upper().startswith("AVER"):
        problems.append("scope is in AVER, and :SINGle takes exactly ONE hit of "
                        "an average (measured) -- every capture here would be a "
                        "single unaveraged shot claiming full depth. Set the "
                        "scope to HRES; --repeats does the averaging.")
    elif not acq.upper().startswith("HRES"):
        problems.append(f"scope is in {acq}; use HRES -- its intra-sweep boxcar "
                        f"plus software repeats is the measured best scheme "
                        f"(0.5-1 V floor at the EOM).")
    if a.repeats < 16:
        problems.append(f"--repeats {a.repeats} is too few to dither the scope's "
                        f"2.5 mV word lattice (16 reaches the 0.16 mV floor).")
    if problems:
        print("\nSetup problems:")
        for p in problems:
            print("  !", p)
        if not a.skip_checks:
            sys.exit("\nRefusing to upload. Fix the setup in the GUI, or pass --skip-checks.")

    os.makedirs(a.outdir, exist_ok=True)

    uploaded_any = False
    try:
        for k in range(k0, k0 + a.iterations + 1):
            rep = loop.check(u)
            if not rep:
                print("\nlimit check FAILED:", rep)
                break

            name = f"{stem}_i{k:02d}"                    # <= MAX_ARB_NAME
            n, frac = upload_drive(awg, a.awg_ch, name, u, full_scale)
            uploaded_any = True
            print(f"\niter {k}: uploaded {name} ({n} pts, {100*frac:.1f}% of DAC range, "
                  f"peak {np.abs(u).max():.4f} V)")
            outputs.write_awg_csv(os.path.join(a.outdir, f"drive_{name}.csv"), t, u)
            # the GUI-previewable copy, same as the manual loop leaves behind
            os.makedirs(a.awg_dir, exist_ok=True)
            outputs.write_bk_waveform(os.path.join(a.awg_dir, f"{name}.csv"),
                                      u, name, full_scale)

            if k == k0:
                time.sleep(0.5)          # let the new upload start playing
                verify_alignment(scope, a.drive_scope_ch or a.awg_ch,
                                 u, t, t_off, a.wait)

            y = capture(scope, [a.scope_ch], f"CH{a.scope_ch}", t, t_off,
                        repeats=a.repeats, wait_s=a.wait, points=a.points)
            np.save(os.path.join(a.outdir, f"meas_{name}.npy"), y)

            m = loop.metrics(y)
            print(f"         error: peak {m['peak_err_hv']:7.1f} V   "
                  f"rms {m['rms_err_hv']:6.2f} V   ({m['peak_pct']:.2f}% FS)")

            if k < k0 + a.iterations:
                u = loop.update(u, y)
                save_state(k + 1, u)
    finally:
        # A finished (or died) run leaves nothing driving the chain. Only if
        # something was actually played: a run refused at the setup checks
        # leaves the bench exactly as it found it.
        if uploaded_any:
            try:
                awg.set_output(a.awg_ch, False)
                print(f"CH{a.awg_ch} output OFF (end of run)")
            except Exception as e:
                print(f"could not switch CH{a.awg_ch} output off: {e}")
        # both closes leave the shared ResourceManager standing (make_awg /
        # make_scope), so the order no longer matters
        awg.close()
        scope.close()

    print("\n" + loop.report())
    print(f"\ndrives and measurements in {os.path.abspath(a.outdir)}")


if __name__ == "__main__":
    main()
