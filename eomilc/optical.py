"""The optical close, headless: measure the light against the monitors with the
rotating-analyzer polarimeter, and move the ILC's TARGET by minus what the
monitors cannot see.

The ILC makes the Trek monitors follow a target. The experiment needs the
light, and the light leaves the monitors for ~0.3 ms at the top of every ramp
and for the first ms after the fall (8 Oct 2026: 0.7-1.7 deg and 0.3-0.95
deg). Nothing the loop measures shows that. This module is the library the
batch's optical stage and the panel's Optical tab both call: open the
analyzer beside the ILC's own scope, scan while the drives play, compare the
light with the target and the monitors, gate the correction and put it on
the target, and read a before/after verdict. It writes nothing of its own
format: a scan is the polarimeter's scan folder (Scope Grab captures plus
<name>_scan.json, so its data viewer opens it), and the comparison is the
polarimeter's analysis/target_compare folder.

What it reuses, never rewrites. The polarimeter checkout (rampol, found by
ilc_hardware.polarimeter_path and put on sys.path inside the functions, so
importing this module never needs it): hw.ScopeLink around the ILC's scope,
hw.Rotator around the ELL14, scan.ScanRun with angle_list / ordered /
build_steps / hold_angles, awg.timebase_for and plan.sampling for the screen
(the recipe of tools/campaign.py _ramp_sequence), ilc_target.compare for the
analysis and the correction files, sim.Bench / sim.make for the simulated
bench. From this package: corrections.gate / check_target / check_feasible
and the panel's apply rule (ilc_gui _corr_compute + do_corr_apply): target =
base target + total / mon_scale, a new file in a slot REPLACES the old one,
history[-1]["target_changed"] stamped.

Traps met building it, in the order they bite:

The scope object must be a headless Scope Grab. ScopeLink reads through
Scope.record / transfer_plan / dither_step / restore_offsets, which only
scope-grab-multi has (dcf2c0b on). This PC's ILC finds the old scope-grab
checkout first (ilc_bench.find_scope_grab), whose Scope has none of them:
open_devices refuses it by name instead of failing mid-scan. Scope Grab is
also loaded once per process: when the ILC's own module has the headless
API it is used as is, because rampol.hw.load_scope_grab with another path
would replace sys.modules["scope_grab"] under the panel's feet (and compare
is handed the same file for the same reason).

The screen. The ILC sets the timebase for its own record (RANGe around the
record, REFerence CENTer); a scan needs REFerence LEFT, a 2-significant-figure
ms/div from 0.5 ms before the trigger to 30 ms after the record (the light's
after-fall tail), and single HRES shots. All of it is put back after the scan
- timebase, acquisition type and count - so the ILC's next iteration measures
on the screen it set up. The polarimeter's preset (channel V/div by role,
trigger) is NOT applied unless asked: the trigger and the verticals belong to
the ILC's bench, and a preset written over them changed the monitors' V/div
under a running campaign. The PD must be on screen by the caller's settings.

The leg gap. ilc_target.compare fits a second leg wherever the record is long
enough to hold one at leg_gap_ms after the first (16.667 ms by default: the
spin-echo sequence). The ILC's bench record has one leg, and the 30 ms tail
is long enough to make a phantom second leg out of nothing; compare is
always called here with leg_gap_ms 1e6.

The base target. compare names files after the state's "name" and builds the
played target and the correction's fingerprint from the state's "target".
After a correction is applied that is base + correction, and a correction
fingerprinted on it fails check_target against the base (the next pass is
then refused) while the 99.9 % hold landmark moves with the correction. So
compare() hands rampol the state with its base_target when the state has
one (a copy, base_<state>.npz in the comparison folder). A state saved
without base_target after an apply cannot be told apart: save it.

The simulated light. In rampol.sim the light IS the monitors' rotation
(17.55 deg per monitor V = 90 / V90), plus Bench.rot_err x sin(2 pi rot /
rot_err_period) - the only light-only term the AWG path has (the crystal
memory belongs to the bench's built-in ramp legs, which stop the moment its
AWG plays). With rot_err 0 the light minus monitors is noise, the gate keeps
nothing and a closure has nothing to do: set dev.sim.rot_err (0.5 deg gives
~14 V per crystal at the EOM) to give it something. The bench's FakeAWG
feeds the bench through two fields - Bench.awg_drive {AWG ch: (period s,
volts)} and Bench.awg_on {ch: bool} (upload_arb: period 1 / FRQ = N dt, the
samples clipped at the AMP 20 Vpp full scale) - and the monitors are then
volts x Bench.awg_gain (0.5594 / 0.5924, the polarimeter's calibration, not
the ILC model's gain). sim_play writes those two fields directly, without the
Session machinery (never-float, dry run, park) that would only stand in the
way. Until sim_play is called the simulated AWG plays flat zero, NOT the
bench's built-in 180 deg ramp - a scan of that would look like a drive and
mean nothing. The simulated PD has no delay either: compare's pd_delay_us
1.7 (the bench's) reads the simulated light 1.7 us late, which on the
suite's 8 V/us edges (0.28 deg/us of rotation on both crystals) is up to
~0.5 deg of light - monitors that is not there. Pass 0 in simulation.

The PD channel and the monitors come from the ILC's scope map
(eomilc.channels, config "scope_channels"): PD, MonX1 and MonX2 are required.
With a CmdX1 channel the comparison's time map fits the recorded command
against the state's drive u; without, the monitor against the target.

Units. A correction file and the gate are in output units (V at the EOM);
loop.target, base_target and corr_total are monitor volts (output / mon_scale)
like the state files' keys of the same names.
"""
from __future__ import annotations

import datetime
import os
import sys
from dataclasses import asdict, dataclass, field

import numpy as np

from . import channels as chanmap
from . import corrections as corrmod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The screen around the record (ms) and the scan defaults the design fixes.
BEFORE_MS = 0.5
AFTER_MS = 8.0            # was 30 until 14 Oct 2026: the verdict reads the first ms after
                          # the fall and X2's fast term is gone in 5; 30 ms left most of
                          # the screen after the record and the edges sampling-limited
# The windows verdict reads, relative to the leg's landmarks (s): the ramp
# top is the hold +- TOP_PAD, the after-fall window the first AFTER_FALL.
TOP_PAD = 0.3e-3
AFTER_FALL = 1.0e-3
# The panel's gate defaults (ilc_gui corr_* fields).
# active_only False (14 Oct 2026): the light after a transport is part of what the
# optical close corrects (the crystal's memory keeps it rotated for tens of ms),
# so a correction is kept over the whole record, faded 200 us at each end
GATE_DEFAULTS = dict(k_sigma=3.0, floor_out=0.1, cap_out=300.0, fade_s=200e-6,
                     active_only=False)
PANEL_BAND_HZ = 2000.0
# rampol.sim.Bench as tools/campaign.py connect(simulate=True) builds it.
SIM_BENCH = dict(pd_noise=0.2e-3, drift=0.0, er_rest=5000.0, er_mid=300.0, legs_ms=(0.0,))
SIM_NOISE_PER_DIV = 0.012
SIM_AWG_FULL_SCALE = 10.0          # FakeAWG: AMP 20 Vpp -> +-10 V


LIGHT_MIN_V = 0.05        # a PD that never rises above this has no light on it
SCAN_MIN_POINTS = 1000    # the readout per capture when points come from the sample spacing
SCAN_MAX_POINTS = 100000  # (the MSO-X serves a RAW single shot at any count its memory holds)
ZERO_TOL_DEG = 1.0        # the rest polarization this far from analyzer 0 is "the zero holds"


class ScanRefused(RuntimeError):
    """The pre-scan shot found the screen unusable (no light on the PD, a
    channel clipped or switched off, the trigger wrong): nothing was scanned.
    `findings` holds every (level, text) the checks produced."""

    def __init__(self, why, findings=()):
        super().__init__(why)
        self.findings = list(findings)


def _dark_steps(run, dev):
    """The done steps of a scan on disk whose PD never rose above LIGHT_MIN_V:
    a scan taken with the light off (13 Oct 2026: 11 such steps resumed
    into a lit scan). One capture per step is read."""
    pd = [ch for ch, role in dev.roles.items() if role == "PD"]
    if not pd:
        return []
    col = f"CH{pd[0]}_"
    dark = []
    for s in run.manifest.get("steps", []):
        if s.get("status") != "done" or not s.get("files"):
            continue
        try:
            cols, data = dev.sg.load_capture(os.path.join(run.folder, s["files"][0]))
            i = next(k for k, c in enumerate(cols) if c.startswith(col))
            if float(np.max(data[:, i])) < LIGHT_MIN_V:
                dark.append(s)
        except (OSError, ValueError, StopIteration, IndexError):
            continue
    return dark


def _check_shot(dev, points, cancelled, log, plan):
    """One single shot of every recorded channel, judged by rampol.checks
    (the polarimeter's own pre-scan checks: trigger sweep and source,
    channels on and DC, clipping and screen use, light on the PD, ramps on
    the monitors and inside the record). Logs the findings; raises
    ScanRefused on any FAIL or on a dark PD. 13 Oct 2026: the first bench
    stage scanned 21 steps x 8 shots of a PD with the light off."""
    from rampol import checks
    link = dev.link
    chans = sorted(dev.roles)
    got = {}
    link.acquire_blocks(chans, "single", 1, 1, dither_codes=0, points=int(points),
                        wait_s=10.0, cancelled=cancelled,
                        on_block=lambda k, recs, hits: got.update(recs))
    rs = getattr(link.scope, "read_settings", None)
    st = rs() if rs else {}
    first = next(iter(got.values()))
    t = first.t()
    tr = {ch: r.v() for ch, r in got.items()}
    found = (checks.settings_checks(st, link.prof, dev.roles, plan)
             + checks.shot_checks(t, tr, st, link.prof, dev.roles, plan))
    for lev, txt in found:
        if lev != "INFO":
            log(f"  check {lev}: {txt}")
    fails = [txt for lev, txt in found if lev == "FAIL"]
    for ch, role in sorted(dev.roles.items()):
        v = tr.get(ch)
        if role == "PD" and v is not None and len(v) and float(np.max(v)) < LIGHT_MIN_V:
            fails.insert(0, f"the PD on CH{ch} never rises above {float(np.max(v)) * 1e3:.0f} mV: "
                            f"the light is off or the beam is blocked")
    if fails:
        raise ScanRefused("scan refused: " + "; ".join(fails), found)
    pd = [ch for ch, role in dev.roles.items() if role == "PD"]
    peak = max(float(np.max(tr[ch])) for ch in pd if ch in tr) if pd else float("nan")
    worst = checks.summary(found)
    log(f"  check done: light on the PD ({peak:.2f} V peak), every channel on screen"
        + ("" if worst == "OK" else f"; worst finding {worst} (above)"))


class CorrectionRefused(RuntimeError):
    """A correction the gate or the feasibility check refused. `notes` is the
    whole [(level, text)] list; the "fail" ones are the reason."""

    def __init__(self, notes):
        self.notes = list(notes)
        fails = [t for lev, t in self.notes if lev == "fail"]
        super().__init__("correction refused: " + "; ".join(fails or ["(no reason given)"]))


@dataclass
class Applied:
    target: np.ndarray            # the loop's new target, monitor V
    base_target: np.ndarray       # monitor V, kept for the state ("base_target")
    corr_total: np.ndarray        # monitor V ("corr_total"); target = base + this
    layers: list                  # the state's "corrections" (JSON-able dicts)
    notes: list                   # [(level, text)] from the gate and the checks
    info: dict = field(default_factory=dict)


class Devices:
    """What a scan needs besides the drives playing.

    sg: the Scope Grab module; link: rampol.hw.ScopeLink; rot: rampol.hw.Rotator;
    roles: {ch: role}; channels: {ch: (role, name)} (ScanRun's); pcfg: the
    polarimeter config (its DEFAULTS under the file); sim: the rampol Bench in
    simulation, else None. drives / dt: what sim_play was last given."""

    def __init__(self, sg, link, rot, roles, channels, pcfg, sim=None, log=print):
        self.sg, self.link, self.rot = sg, link, rot
        self.roles, self.channels = dict(roles), dict(channels)
        self.pcfg, self.sim, self.log = pcfg, sim, log
        self.drives, self.dt = None, None

    @property
    def scope(self):
        return self.link.scope


# ------------------------------------------------------------------ rampol
def _rampol(cfg=None):
    """Put the polarimeter checkout on sys.path and return it."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import ilc_hardware
    path = ilc_hardware.polarimeter_path(cfg)
    if path is None:
        raise RuntimeError("the ramp-polarimeter checkout was not found (config "
                           "'polarimeter_path', RAMP_POLARIMETER, or a sibling folder)")
    if path not in sys.path:
        sys.path.insert(0, path)
    import rampol  # noqa: F401
    return path


def _pcfg(pcfg):
    """The polarimeter's config: its DEFAULTS with the file (or `pcfg`) over them."""
    from rampol import config as rcfg
    if pcfg is None:
        return rcfg.load()
    return rcfg._merge(rcfg.DEFAULTS, pcfg)


HEADLESS = ("record", "transfer_plan", "dither_step", "restore_offsets", "read_settings",
            "metadata")


def _scope_grab(scope, pcfg):
    """The Scope Grab module for this scope: the scope's own when it has the
    headless API (not reloaded under the ILC), else the polarimeter's path."""
    from rampol import hw
    cands = []
    if scope is not None:
        cands.append(sys.modules.get(type(scope).__module__))
        for k in type(scope).__mro__:
            cands.append(sys.modules.get(k.__module__))
    cands.append(sys.modules.get("scope_grab"))
    for m in cands:
        if m is not None and all(hasattr(m, n) for n in ("write_capture", "setting_roots",
                                                          "load_capture", "Scope")):
            return m
    return hw.load_scope_grab(pcfg["scope_grab_path"])


def open_devices(scope, cfg, log=print, simulate=False, pcfg=None):
    """The scan's devices beside the ILC's scope. `scope` is the ILC's open
    scope_grab.Scope (never closed here; None in simulation), `cfg` the ILC
    config (its scope_channels give the roles). Real: the ELL14 from the
    polarimeter config (ell_port, ell_address, ell_zero_deg). simulate=True:
    rampol.sim's scope and mount over one Bench, as campaign.connect builds
    them; the AWG plays flat zero until sim_play."""
    _rampol(cfg)
    from rampol import hw
    pc = _pcfg(pcfg)
    chans = chanmap.roles(cfg)
    roles = {ch: r for ch, (r, _n) in chans.items()}
    for need in ("PD", "MonX1", "MonX2"):
        if chanmap.channel_for(cfg, need) is None:
            raise ValueError(f"no scope channel carries {need}: the optical close needs "
                             f"the PD and both monitors (Scope channels)")
    if simulate:
        from rampol import sim
        sg = _scope_grab(None, pc)
        bench = sim.Bench(**SIM_BENCH)
        # the simulated zero as the bench's: analyzer 0 CROSSED to the rest
        # polarization (the polarimeter's convention, seq_crossed_deg), i.e.
        # the rest's transmission maximum (mount_of_rest_pol) at crossed + 90,
        # not the config's bench number -- so the hold-null angles and the
        # zero check mean the same thing in simulation
        zero_sim = (float(bench.mount_of_rest_pol) - crossed_rest_deg(pc) - 90.0) % 360.0
        sc, ell, bench = sim.make(sg, roles=roles, bench=bench, zero_offset_deg=zero_sim)
        sc.noise_per_div = SIM_NOISE_PER_DIV
        # the AWG outputs on and at zero: nothing plays until sim_play
        n = 16
        bench.awg_drive = {1: (1e-3, np.zeros(n)), 2: (1e-3, np.zeros(n))}
        bench.awg_on = {1: True, 2: True}
        bench.wiring = "treks"
        dev = Devices(sg, hw.ScopeLink(sc, log=log), hw.Rotator(ell, log=log), roles, chans,
                      pc, sim=bench, log=log)
        log("optical: SIMULATED polarimeter (rampol.sim bench, analyzer and scope)")
        return dev
    if scope is None:
        raise ValueError("open_devices needs the ILC's open scope (or simulate=True)")
    missing = [n for n in HEADLESS if not hasattr(scope, n)]
    if missing:
        mod = sys.modules.get(type(scope).__module__)
        raise RuntimeError(
            f"the ILC's scope comes from {getattr(mod, '__file__', '?')}, which has no "
            f"{', '.join(missing)}: the polarimeter's scan needs scope-grab-multi "
            f"(set SCOPE_GRAB to {pc['scope_grab_path']} and restart)")
    sg = _scope_grab(scope, pc)
    from rampol.ell14 import ELL14
    ell = ELL14(pc["ell_port"] or None, address=pc["ell_address"],
                zero_offset_deg=float(pc["ell_zero_deg"]))
    rot = hw.Rotator(ell, log=log)
    try:
        info = ell.info()
        log(f"optical: analyzer ELL{info['type']} S/N {info['serial']} on {ell.port}, at "
            f"{ell.position():.3f} deg")
    except Exception as exc:              # identity only: the scan reads it again
        log(f"optical: analyzer on {ell.port} ({exc})")
    return Devices(sg, hw.ScopeLink(scope, log=log), rot, roles, chans, pc, log=log)


def close_devices(dev):
    """The rotator only: the scope is the ILC's."""
    if dev is not None and dev.rot is not None:
        try:
            dev.rot.close()
        except Exception as exc:
            dev.log(f"optical: analyzer close failed ({exc})")


def sim_play(dev, drives, dt=2e-6, full_scale=SIM_AWG_FULL_SCALE):
    """Put the drives that play into the simulated bench: drives {"EO1": u,
    "EO2": u}, AWG volts on the record grid (spacing dt, from the trigger).
    As FakeAWG.upload_arb does: period N dt, samples clipped at the full
    scale, outputs on. On real hardware nothing is touched (the drives are
    only remembered for the scan's manifest and sampling estimate)."""
    u = {k: np.asarray(drives[k], float) for k in ("EO1", "EO2")}
    dev.drives, dev.dt = u, float(dt)
    if dev.sim is None:
        return
    b = dev.sim
    for name, ch in (("EO1", 1), ("EO2", 2)):
        x = np.clip(u[name], -full_scale, full_scale)
        b.awg_drive[ch] = (len(x) * float(dt), x)
        b.awg_on[ch] = True
    b.wiring = "treks"


# --------------------------------------------------------------------- scan
def _angles(spec):
    from rampol import scan as scanmod
    if isinstance(spec, (list, tuple)):
        return [float(a) for a in spec]
    s = str(spec).strip()
    if ":" in s:
        a, b, c = (float(x) for x in s.split(":"))
        return scanmod.angle_list(a, b, c)
    return [float(x) for x in s.replace(",", " ").split()]


def _peak_rate(drives, dt):
    """deg/ms: the fastest rotation the drives give (rampol.awg.peak_rate)."""
    if not drives or not dt:
        return 0.0
    from rampol import awg as awgmod
    n = len(drives["EO1"])
    w = awgmod.Wave(np.arange(n) * dt, drives, dt, "ILC drives")
    return awgmod.peak_rate(w)


def scan(dev, outdir, name, t_grid, log=print, progress=None, cancelled=None,
         angles="0:359:22.5", shots=8, points=None, ref_every=9, hold_null=True,
         ends_deg=None, settle_s=1.0, extra=None, preset=None, drives=None, states=None,
         check=True, pd_vertical=True, sample_us=2.0):
    """A ramp scan of the light while the drives PLAY (the caller's job:
    outputs on, or sim_play). Returns the scan folder <outdir>/<name>.

    points: the single-shot readout per capture; None (the default since
    14 Oct 2026) takes the screen span over `sample_us` so the light is
    read on the ILC's own 2 us spacing whatever the record length (a 27.5 ms
    spin-echo record gives ~18500 points, S25 ~8000), between SCAN_MIN_POINTS
    and SCAN_MAX_POINTS.

    Before anything is scanned (13 Oct 2026): with pd_vertical the PD's
    channel is set to the polarimeter preset's V/div and offset for the PD
    role (the ILC sets the drives' and monitors' verticals, never the PD's;
    skipped when a whole `preset` is written), the trigger sweep is set to
    NORMAL, and with `check` one single shot of every recorded channel is
    judged by rampol.checks: a dark PD, a clipped or switched-off channel,
    or a wrong trigger raises ScanRefused and nothing is scanned. The PD's
    screen and the sweep are put back with the timebase afterwards.

    The screen spans BEFORE_MS before the trigger to AFTER_MS after the
    record (t_grid, s), REFerence LEFT, ms/div rounded up to 2 significant
    figures (rampol.awg.timebase_for); single HRES shots read out at
    `points`; ScanRun steps from `angles` ("start:stop:step" or a list) in
    the polarimeter config's order, a reference return every `ref_every`,
    and with ends_deg {"X1": deg, "X2": deg} the hold-null angles
    (rampol.scan.hold_angles, the config's seq_* settings). A scan already
    on disk under `name` is resumed. The timebase and acquisition are put
    back afterwards. preset: a polarimeter preset name to write first (off
    by default: see the module notes). drives / states: the drives that
    play (default: the last sim_play's) and {"x1": path, "x2": path} ILC
    states, for the manifest. Raises rampol.hw.Cancelled when stopped
    (the scan is saved and resumes)."""
    import time
    from rampol import __version__, awg as awgmod, config as rcfg, plan as planmod
    from rampol import provenance, scan as scanmod
    pc = dev.pcfg
    link, scope = dev.link, dev.link.scope
    t_grid = np.asarray(t_grid, float)
    dt = float(np.median(np.diff(t_grid)))
    record = awgmod.Wave(t_grid - t_grid[0], {}, dt, "ILC record")
    drives = drives if drives is not None else dev.drives
    sc = dict(pc["scan"])
    grid = _angles(angles)
    order = scanmod.ordered(grid, sc.get("order", "forward"))
    extras = []
    if hold_null and ends_deg:
        a = pc["awg"]
        rot_deg = float(ends_deg.get("X1", 0.0)) + float(ends_deg.get("X2", 0.0))
        extras = scanmod.hold_angles(float(a.get("seq_crossed_deg", 0.0) or 0.0), rot_deg,
                                     float(a.get("seq_null_half_deg", 3.0) or 0.0),
                                     int(a.get("seq_null_points", 3) or 1), grid,
                                     sense=float(a.get("seq_sense", -1.0) or -1.0))
    div, pos = awgmod.timebase_for(record, BEFORE_MS, AFTER_MS)
    rate = _peak_rate(drives, dev.dt or dt)
    if not points:
        points = int(min(SCAN_MAX_POINTS, max(SCAN_MIN_POINTS,
                                              np.ceil(10 * div / (float(sample_us) * 1e-6)))))
        log(f"  readout {points} points: the {10 * div * 1e3:.1f} ms screen at {float(sample_us):g} us")
    dt_us, per = planmod.sampling(10 * div, int(points), rate)

    # what is changed on the ILC's scope, to put back afterwards
    writes = {}
    if preset:
        pre = rcfg.all_presets(pc).get(preset)
        if pre is None:
            raise ValueError(f"no polarimeter preset {preset!r}")
        writes.update(link.preset_writes(pre, dev.roles))
    tb = {":TIMebase:REFerence": "LEFT", ":TIMebase:SCALe": f"{div:.6g}",
          ":TIMebase:POSition": f"{pos:.6g}", ":TRIGger:SWEep": "NORMal"}
    pd_ch = next((ch for ch, role in dev.roles.items() if role == "PD"), None)
    pd_set = None
    if pd_vertical and not preset and pd_ch is not None:
        pres = rcfg.all_presets(pc)
        pre = pres.get(pc.get("preset")) or next(iter(pres.values()), {})
        r_pd = (pre.get("roles") or {}).get("PD")
        if r_pd:
            pd_set = (float(r_pd["scale"]), float(r_pd["offset"]))
            p_ = link.prof
            for root in (p_.ch_scale.format(ch=pd_ch), p_.ch_offset.format(ch=pd_ch)):
                tb.setdefault(root, None)          # saved and restored, written by set_channel
    saved = {}
    for root in list(writes) + list(tb):
        try:
            saved[root] = scope.get(root)
        except Exception:
            pass
    r = scanmod.ScanRun(outdir, name, dev.sg, link, dev.rot, dev.channels, log=log,
                        progress=progress, cancelled=cancelled,
                        clock=(lambda: dev.sim.clock) if dev.sim is not None else time.time)
    try:
        if writes:
            bad, errs = link.apply_checked(writes)
            log(f"  scope set from the preset '{preset}' ({len(writes)} settings"
                + (", every one read back)" if not bad and not errs else ")"))
            for root, (want, got) in bad.items():
                log(f"  ! {root}: wrote {want}, scope reads {got}")
        for root, val in tb.items():
            if val is not None:
                scope.put(root, val)
        if pd_set is not None:
            link.set_channel(pd_ch, pd_set[0], pd_set[1])
            log(f"  PD CH{pd_ch} at {pd_set[0]:g} V/div, offset {pd_set[1]:+g} V (the polarimeter's "
                f"preset); trigger sweep NORMAL")
        log(f"  scope timebase {div*1e3:g} ms/div from {(pos - div)*1e3:+.2f} ms to "
            f"{(pos + 9 * div)*1e3:+.2f} ms (record {record.period*1e3:.3f} ms)")
        log(f"  sampling: {dt_us:.2f} us" + (f", {per:.2f} deg per sample at {rate:.0f} deg/ms"
                                            if rate else " (drives not given: no rate)")
            + (" - WARN: edges sampling-limited" if per > planmod.SAMPLING_WARN_DEG else ""))
        if dev.sim is not None and dev.drives is None:
            log("  ! simulated bench: sim_play was not called - the simulated light is flat")
        created = not r.exists()
        if not created:
            r.load()
            done = sum(1 for s in r.manifest["steps"] if s["status"] == "done")
            if check:
                dark = _dark_steps(r, dev)
                if dark:
                    raise ScanRefused(
                        f"scan refused: {r.name} on disk holds {len(dark)} of its {done} done steps "
                        f"scanned with no light on the PD (peak under {LIGHT_MIN_V * 1e3:.0f} mV); "
                        f"delete {r.folder} and scan again", [])
            log(f"  {r.name}: on disk, {done} steps done - resumed")
        else:
            steps = scanmod.build_steps(order, int(ref_every), float(sc.get("ref_angle", 45.0)),
                                        extra=extras)
            plan = dict(sc, mode="single", shots=int(shots), points=int(points),
                        ref_every=int(ref_every), angles=str(angles),
                        start=min(grid), stop=max(grid),
                        step=(grid[1] - grid[0]) if len(grid) > 1 else 0.0,
                        preset=preset, software=f"rampol {__version__} via eomilc.optical",
                        dark_mode="none", bg_mode="none", hold_angles=list(extras),
                        sampling_us=dt_us, deg_per_sample=per)
            cfg_p = dict(pc, eomilc_path=ROOT,
                         scope_grab_path=getattr(dev.sg, "__file__", pc["scope_grab_path"]),
                         ilc=dict(states or {}))
            info = {"source": "EOM-ILC optical close (eomilc.optical)", "label": r.name,
                    "record_ms": record.period * 1e3, "dt_us": dt * 1e6,
                    "idle_V": ({k: float(v[0]) for k, v in drives.items()} if drives else None),
                    "dry_run_passed": None, "rotation_deg": (sum(ends_deg.values())
                                                             if ends_deg else None),
                    "hold_ms": None, "ends_deg": dict(ends_deg) if ends_deg else None}
            if states:
                info["files"] = dict(states)
            ext = {"zero_deg": float(dev.rot.zero), "provenance": provenance.collect(cfg_p),
                   "drive": info}
            ext.update(extra or {})
            r.new(plan, steps, extra=ext)
            log(f"  {r.name}: {len(steps)} steps ({len(order)} angles"
                + (f" + {len(extras)} hold-null" if extras else "") + f", {int(shots)} shots each)")
        if check:
            # after the scan exists on disk (a Stop during this shot then
            # leaves a scan that resumes, as a Stop during the run does); a
            # REFUSED scan that was only just created is removed again, so a
            # dark PD leaves nothing behind to resume
            try:
                _check_shot(dev, points, cancelled, log,
                            dict(mode="single", points=int(points), dither_codes=0, start=min(grid),
                                 stop=max(grid), step=(grid[1] - grid[0]) if len(grid) > 1 else 0.0))
            except ScanRefused:
                if created:
                    import shutil
                    shutil.rmtree(r.folder, ignore_errors=True)
                raise
        if settle_s and settle_s > 0 and dev.sim is None:
            time.sleep(settle_s)
        r.run()
    finally:
        # put the ILC's screen back: reference first (the position is relative
        # to it), then scale, then position; the preset's settings after
        first3 = (":TIMebase:REFerence", ":TIMebase:SCALe", ":TIMebase:POSition")
        for root in list(first3) + [k for k in saved if k not in first3]:
            if root in saved:
                try:
                    scope.put(root, saved[root])
                except Exception as exc:
                    log(f"  could not restore {root} {saved[root]} ({exc})")
    log(f"  {r.name}: scan done -> {r.folder}")
    return r.folder


# ------------------------------------------------------------------ compare
def _base_state(path, outdir):
    """`path`, or a copy of it holding its base_target as the target when it
    has one (a corrected state): compare must see the BASE target."""
    z = np.load(path, allow_pickle=True)
    if "base_target" not in z.files:
        return path
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, "base_" + os.path.basename(path))
    if not out.endswith(".npz"):
        out += ".npz"
    np.savez(out, t=z["t"], target=np.asarray(z["base_target"], float), u=z["u"],
             name=z["name"], channel=z["channel"],
             dt=z["dt"] if "dt" in z.files else float(np.median(np.diff(z["t"]))),
             source=os.path.abspath(path))
    return out


PARK_NAMES = {"X1": "PARK1", "X2": "PARK2"}      # the AWG arb names of the parked drives


def park_drive(u):
    """The drive that parks a channel while the OTHER crystal is scanned
    alone (13 Oct 2026): flat at the record's idle (the drive's first
    sample), the same length, so FRQ, the record and the trigger stay as
    they are and the output stays ON (an output is never switched off to
    park: the AWG / Trek rule)."""
    u = np.asarray(u, float)
    return np.full_like(u, u[0])


def parked_state(path, outdir):
    """A copy of an ILC state whose target and drive are flat at their idle:
    what compare() hands the polarimeter for the crystal that was PARKED
    during a solo scan. rampol.ilc_target reads a flat target as "parked"
    and attributes the whole correction to the other crystal. Written as
    parked_<state>.npz in the comparison folder."""
    z = np.load(path, allow_pickle=True)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, "parked_" + os.path.basename(path))
    if not out.endswith(".npz"):
        out += ".npz"
    tgt = np.asarray(z["base_target"] if "base_target" in z.files else z["target"], float)
    np.savez(out, t=z["t"], target=np.full_like(tgt, tgt[0]), u=park_drive(z["u"]),
             name=z["name"], channel=z["channel"],
             dt=z["dt"] if "dt" in z.files else float(np.median(np.diff(z["t"]))),
             source=os.path.abspath(path), parked=True)
    return out


def _read_played(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or line.startswith("time_us"):
                continue
            f = line.strip().split(",")
            if len(f) >= 2:
                rows.append((float(f[0]), float(f[1])))
    a = np.asarray(rows, float)
    return a[:, 0] * 1e-6, a[:, 1]


def _extreme(t, x, lo, hi, t0):
    m = (t >= lo) & (t <= hi)
    if not m.any():
        return float("nan"), float("nan")
    i = np.flatnonzero(m)[int(np.argmax(np.abs(x[m])))]
    return float(x[i] * 1e3), float((t[i] - t0) * 1e3)


def _extremes(summary, folder, sg, played, band_hz, pd_delay_us, lock_tol=0.006):
    """Per leg, from the whole record (the comparison's own leg window stops
    0.3 ms after the fall): light - monitors and light - target, band-limited
    at the correction band, at the ramp top and in the first ms after the
    fall, and their rms over the active part."""
    from rampol import analysis as an, ilc_target as it
    d = an.load_scan(folder, sg.load_capture, lock_tol=lock_tol)
    pol = an.polarization(d)
    # where the rest polarization sits in the analyzer frame: the Malus fit
    # over the pre-trigger stretch of every capture, so each scan localises
    # the analyzer's zero for free. The polarimeter's convention (its config's
    # seq_crossed_deg, 0 = the Find zero): analyzer 0 is CROSSED to the rest
    # polarization, so the rest's transmission maximum is expected at
    # crossed + 90 (the bench on 14 Oct 2026 read +89.96); the hold-null
    # angles assume it. zero_off_deg is how far it is from there, mod 180.
    psi_rest = float(pol["psi_rest"])
    zero = float((d.manifest.get("rotator") or {}).get("zero_deg", float("nan")))
    expected, off = rest_expected(psi_rest)
    v90 = {1: summary["V90_mon"][0], 2: summary["V90_mon"][1]}
    sign = float(summary["rotation_sign_vs_target"])
    t, rot, pm, lm, _mon = it.light_minus_monitors(d, pol, sign, v90, pd_delay_us)
    dt = float(np.median(np.diff(t)))
    S = np.asarray(summary["source_landmarks_ms"], float) * 1e-3
    legs_E = [np.asarray(x["landmarks_ms"], float) * 1e-3 for x in summary["timing"]]
    E1 = legs_E[0]
    phi_t = np.zeros_like(t)
    for E in legs_E:
        sh = E[0] - E1[0]
        win = (t > E[0] - TOP_PAD) & (t < E[-1] + AFTER_FALL + TOP_PAD)
        part = np.zeros_like(t)
        for c, (tp, hv) in played.items():
            part += 90.0 * np.interp(t - sh - E1[0] + S[0], tp, hv / 1000.0,
                                     left=0.0, right=0.0) / v90[c]
        phi_t += np.where(win, part, 0.0)
    lm_f = corrmod.lowpass(lm, dt, band_hz)
    lt_f = corrmod.lowpass(rot - phi_t, dt, band_hz)
    out = []
    for k, E in enumerate(legs_E, 1):
        rec = {"leg": k}
        top = (E[1] - TOP_PAD, E[2] + TOP_PAD)
        aft = (E[3], min(E[3] + AFTER_FALL, t[-1]))
        rec["top_window_ms"] = [(top[0] - E[0]) * 1e3, (top[1] - E[0]) * 1e3]
        rec["after_fall_window_ms"] = [(aft[0] - E[0]) * 1e3, (aft[1] - E[0]) * 1e3]
        rec["top_lm_mdeg"], rec["top_lm_at_ms"] = _extreme(t, lm_f, *top, E[0])
        rec["top_lt_mdeg"], rec["top_lt_at_ms"] = _extreme(t, lt_f, *top, E[0])
        rec["after_fall_lm_mdeg"], rec["after_fall_lm_at_ms"] = _extreme(t, lm_f, *aft, E[0])
        rec["after_fall_lt_mdeg"], rec["after_fall_lt_at_ms"] = _extreme(t, lt_f, *aft, E[0])
        act = (t >= E[0]) & (t <= E[3]) & (phi_t > 0.5)
        rec["lt_rms_mdeg"] = float(np.sqrt(np.mean(lt_f[act] ** 2)) * 1e3) if act.any() else float("nan")
        rec["lm_rms_mdeg"] = float(np.sqrt(np.mean(lm_f[act] ** 2)) * 1e3) if act.any() else float("nan")
        out.append(rec)
    return {"band_hz": float(band_hz), "pd_delay_us": float(pd_delay_us),
            "source": "eomilc.optical: whole record, low-passed at the band", "legs": out,
            "psi_rest_deg": psi_rest, "analyzer_zero_deg": zero,
            "rest_expected_deg": expected, "zero_off_deg": off}


def crossed_rest_deg(pcfg=None) -> float:
    """The analyzer angle crossed to the rest polarization, from the
    polarimeter's config (awg.seq_crossed_deg; 0 = its Find zero)."""
    try:
        pc = _pcfg(pcfg)
        return float((pc.get("awg") or {}).get("seq_crossed_deg", 0.0) or 0.0)
    except Exception:
        return 0.0


def rest_expected(psi_rest, pcfg=None):
    """(expected rest azimuth, how far psi_rest is from it in (-90, 90]):
    the rest polarization's transmission maximum is expected 90 deg from the
    crossed angle."""
    expected = (crossed_rest_deg(pcfg) + 90.0) % 180.0
    off = (float(psi_rest) - expected + 90.0) % 180.0 - 90.0
    return float(expected), float(off)


def compare(folder, x1_state, x2_state, band_hz=5000.0, pd_delay_us=1.7, split=0.5,
            log=print, figs=True, extremes=True, solo=None):
    """rampol.ilc_target.compare on a scan, the leg gap past the record (one
    leg). x1_state / x2_state: the ILC states (drive_*.state.npz) for EO1 and
    EO2 - their BASE target when they carry one. Returns compare's summary
    plus "files" {"EO1": corr csv, "EO2": corr csv}, "played" {"EO1": played
    target csv, ...} (keyed by the channel each correction file names) and,
    with extremes=True, "extremes" (the per-leg numbers verdict reads, from
    the whole record). figs: compare's fig1-5 (matplotlib's Agg backend is
    selected for pyplot in this process; the panel draws on Figure objects,
    which it does not touch).

    solo "X1" / "X2" (13 Oct 2026): the scan was taken with that crystal
    driving alone and the other PARKED (park_drive on its channel). The
    parked crystal's state goes to the polarimeter as a flat copy
    (parked_state), which attributes the whole light - monitors to the
    driving crystal (split 1 / 0, whatever `split` says), and "files" holds
    that crystal's correction only. summary["solo"] names it. A solo scan
    compared WITHOUT solo= would split the one crystal's error over both,
    so the caller must say which played."""
    if solo not in (None, "X1", "X2"):
        raise ValueError(f"solo must be None, 'X1' or 'X2', not {solo!r}")
    _rampol()
    from rampol import ilc_target as it
    pc = _pcfg(None)
    sg = _scope_grab(None, pc)
    sg_path = getattr(sg, "__file__", pc["scope_grab_path"])
    out = os.path.join(folder, "analysis", "target_compare")
    x1 = _base_state(x1_state, out)
    x2 = _base_state(x2_state, out)
    for orig, used in ((x1_state, x1), (x2_state, x2)):
        if used != orig:
            log(f"  {os.path.basename(orig)} carries a corrected target: compared on its "
                f"base target ({os.path.basename(used)})")
    if solo == "X1":
        x2 = parked_state(x2_state, out)
        split = 1.0
    elif solo == "X2":
        x1 = parked_state(x1_state, out)
        split = 0.0
    if solo:
        log(f"  {solo} drove alone, {'X2' if solo == 'X1' else 'X1'} parked at its idle: the whole "
            f"correction is {solo}'s")
    summary = it.compare(folder, x1, x2, f_cut=float(band_hz), pd_delay_us=float(pd_delay_us),
                         split=float(split), leg_gap_ms=1e6, scope_grab_path=sg_path,
                         eomilc_path=ROOT, log=log, figs=figs, whole_record=True)
    summary["solo"] = solo
    keep = {None: ("EO1", "EO2"), "X1": ("EO1",), "X2": ("EO2",)}[solo]
    files, played, by_c = {}, {}, {}
    plays = [p for p in summary["correction"]["files"] if p.endswith("_played.csv")]
    for p in summary["correction"]["files"]:
        if not os.path.basename(p).startswith("corr_"):
            continue
        c = corrmod.read_correction(p)
        ch = str(c.meta.get("channel", ""))
        if ch not in keep:
            continue                      # the parked crystal's file is all zero
        files[ch] = p
        tf = c.meta.get("target_file")
        if not (tf and os.path.exists(str(tf))):
            # corr_<name>_optical.csv sits beside target_<name>_played.csv
            stem = os.path.basename(p)[len("corr_"):-len("_optical.csv")]
            tf = os.path.join(os.path.dirname(p), f"target_{stem}_played.csv")
        played[ch] = str(tf)
    # compare writes them crystal by crystal, x1 first: played, corr, played, corr
    for i, p in enumerate(plays[:2], 1):
        by_c[i] = _read_played(p)
    summary["files"], summary["played"] = files, played
    summary["states_compared"] = [os.path.abspath(x1), os.path.abspath(x2)]
    if extremes:
        summary["extremes"] = _extremes(summary, folder, sg, by_c, band_hz, pd_delay_us)
        for k in ("psi_rest_deg", "analyzer_zero_deg", "rest_expected_deg", "zero_off_deg"):
            summary[k] = summary["extremes"][k]
    return summary


# -------------------------------------------------------------------- apply
def _settings(c, settings):
    if settings is not None:
        return settings
    fb = c.meta.get("band_hz")
    band = float(fb) if isinstance(fb, (int, float)) and fb > 0 else PANEL_BAND_HZ
    return corrmod.GateSettings(band_hz=band, **GATE_DEFAULTS)


def apply_correction(loop, t, u, full_scale, corr_path, gain=1.0, settings=None,
                     base_target=None, layers=None, slot=None) -> Applied:
    """The panel's Corrections Apply, headless, for one file on one loop.

    The file becomes a layer (slot from its meta "slot", default "optical";
    a layer already in that slot is REPLACED, never stacked); every layer
    that is on is gated against the BASE target (check_target, gate) and
    the total checked (check_feasible, the loop's learning band: frf.f_use
    with a measured inverse, else f_cut). Any "fail" raises
    CorrectionRefused(notes) and nothing changes. Otherwise loop.target =
    base + total / mon_scale, history[-1]["target_changed"] is stamped, and
    Applied(target, base_target, corr_total, layers, notes, info) comes back
    for the state (keys base_target, corr_total, corrections).

    t: the record grid (s); u: the drive now playing (AWG V); base_target:
    monitor V, None = the loop's target (the first apply - pass the last
    Applied.base_target afterwards, or the corrections stack); layers: the
    state's earlier ones. settings: corrections.GateSettings; default the
    panel's (k 3, floor 0.1 V, cap 300 V, fade 200 us, active only) at the
    file's band."""
    ch = loop.channel
    sc = ch.mon_scale
    t = np.asarray(t, float)
    base = np.asarray(loop.target if base_target is None else base_target, float).copy()
    base_out = base * sc
    new = corrmod.read_correction(corr_path)
    st = _settings(new, settings)
    # slot: the file's own (meta "slot", default "optical") unless the caller
    # names one. Light - monitors is a fresh estimate of the WHOLE error each
    # time, so a new file in the SAME slot replaces the last; a joint residual
    # measured after a solo correction goes in its own slot ("joint2", ...)
    # and adds to it (14 Oct 2026).
    slot = str(slot or new.meta.get("slot", "") or "optical")
    layer = dict(path=os.path.abspath(corr_path), slot=slot, gain=float(gain), on=True,
                 status="added", quantity=str(new.meta.get("quantity", "")))
    lay = [dict(L) for L in (layers or [])]
    same = [i for i, L in enumerate(lay) if L.get("slot") == slot]
    if same:
        lay[same[0]] = layer
        for i in reversed(same[1:]):
            del lay[i]
    else:
        lay.append(layer)
    total = np.zeros_like(base_out)
    notes, bands, per = [], [], {}
    results = []
    for L in lay:
        if not L.get("on", True):
            continue
        tag = os.path.basename(L["path"])
        try:
            c = new if L is layer else corrmod.read_correction(L["path"])
        except (OSError, ValueError) as exc:
            notes.append(("fail", f"{tag}: {exc}"))
            continue
        mch = c.meta.get("channel")
        if mch and str(mch) != ch.name:
            notes.append(("fail", f"{tag}: written for {mch}, this loop is {ch.name}"))
            continue
        for lev, txt in corrmod.check_target(c.meta, base_out, loop.dt):
            notes.append((lev, f"{tag}: {txt}"))
        kept, info = corrmod.gate(c, t, base_out, st, L.get("gain", 1.0))
        for lev, txt in info["notes"]:
            notes.append((lev, f"{tag}: {txt}"))
        L.update(kept_peak=info["kept_peak"], kept_rms=info["kept_rms"],
                 resolved=info["resolved_frac"], band=info["band_hz"])
        bands.append(info["band_hz"])
        per[L["slot"]] = info
        results.append((L, kept))
        total = total + kept
    learn = float(loop.frf.f_use) if loop.frf is not None else float(loop.f_cut)
    if results:
        notes += corrmod.check_feasible(total, base_out, u, loop.dt, ch, loop.limits,
                                        loop.plant.gain, full_scale, learn, max(bands),
                                        st.cap_out)
    if any(lev == "fail" for lev, _ in notes):
        raise CorrectionRefused(notes)
    if not results:
        raise CorrectionRefused(notes + [("fail", "no correction is switched on")])
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    for L, _ in results:
        L["status"], L["applied"] = "applied", stamp
    for L in lay:
        if not L.get("on", True):
            L["status"] = "off"
    corr_total = total / sc
    loop.target = base + corr_total
    if loop.history and isinstance(loop.history[-1], dict):
        loop.history[-1]["target_changed"] = stamp
    info = dict(total_out=total, base_out=base_out, peak_out=float(np.max(np.abs(total))),
                rms_out=float(np.sqrt(np.mean(total ** 2))), learn_band_hz=learn,
                settings=asdict(st), gates=per, applied=stamp,
                drive_peak_est=float(np.max(np.abs(np.asarray(u, float)
                                                    + corr_total / loop.plant.gain))))
    return Applied(target=loop.target, base_target=base, corr_total=corr_total,
                   layers=lay, notes=notes, info=info)


def joint_residual_files(joint_files, totals, split, out_dir, slot, log=print):
    """The files a joint verification contributes on top of corrections
    already applied per crystal (14 Oct 2026).

    Light minus monitors is a fresh estimate of the WHOLE light error every
    time (moving the target moves the monitors with the light), so a joint
    file, -E split by `split`, says what the TOTAL optical correction should
    be, not what is left. With solo corrections c_i already in place (each
    crystal's own error, from its scan alone), the layer that makes the
    total right is the joint file minus each crystal's split share of what
    is already applied on BOTH crystals, in degrees of rotation:

        joint_k,i = joint_i - split_i x k_i x sum_j(total_j / k_j)

    k_j = V90_j x 1000 / 90 V per degree. Where the joint error is just the
    sum of the crystals' own errors this layer is zero; where something
    follows the total rotation (an analyzer or extinction term, or the
    simulated bench's rotator error) it carries exactly the share the solo
    scans could not see. joint_files {EOn: path} (the verification's),
    totals {EOn: V at the EOM on the record grid, the correction now
    applied}, split the X1 fraction. Writes corr_<name>_<slot>.csv in
    out_dir; returns {EOn: path}."""
    from .config import CHANNELS
    frac = {"EO1": float(split), "EO2": 1.0 - float(split)}
    k = {c: float(CHANNELS[c].v90_hv) * 1000.0 / 90.0 for c in ("EO1", "EO2")}
    deg_sum = None
    for c in ("EO1", "EO2"):
        tot = np.asarray(totals.get(c, 0.0), float)
        deg_sum = tot / k[c] if deg_sum is None else deg_sum + tot / k[c]
    os.makedirs(out_dir, exist_ok=True)
    out = {}
    for c, path in joint_files.items():
        corr = corrmod.read_correction(path)
        if deg_sum.shape != corr.delta.shape:
            raise ValueError(f"{os.path.basename(path)}: {len(corr.delta)} points against the record's "
                             f"{deg_sum.size}: the joint file is not on the record grid")
        share = frac[c] * k[c] * deg_sum
        delta = corr.delta - share
        meta = dict(corr.meta, slot=slot,
                    quantity=(f"joint verification residual: -(light - monitors) x {frac[c]:.0%} minus this "
                              f"crystal's share of the corrections already applied on both"),
                    derived_from=os.path.abspath(path))
        stem = os.path.basename(path)
        if stem.startswith("corr_") and stem.endswith("_optical.csv"):
            stem = stem[len("corr_"):-len("_optical.csv")]
        p = os.path.join(out_dir, f"corr_{stem}_{slot}.csv")
        corrmod.write_correction(p, corr.t, delta, corr.sigma, meta)
        out[c] = p
        log(f"  {c}: {slot} = joint {float(np.max(np.abs(corr.delta))):.1f} V peak minus {frac[c]:.0%} of the "
            f"applied total ({float(np.max(np.abs(share))):.1f} V peak): {float(np.max(np.abs(delta))):.1f} V peak "
            f"-> {os.path.basename(p)}")
    return out


# ------------------------------------------------------------------ verdict
def _fallback_extremes(summary):
    """From target_compare.npz when the summary has no "extremes" (a summary
    the polarimeter's own tool wrote): the leg window ends 0.3 ms after the
    fall, so the after-fall window is that, not a ms."""
    z = np.load(os.path.join(summary["out"], "target_compare.npz"))
    tl = z["t_leg1"]
    dt = float(np.median(np.diff(tl)))
    band = float(summary.get("f_cut_Hz", 5000.0))
    E1 = np.asarray(summary["timing"][0]["landmarks_ms"], float) * 1e-3
    out = []
    for L in summary["legs"]:
        k = L["leg"]
        if f"leg{k}_d_lm" not in z.files:
            continue
        lm = corrmod.lowpass(z[f"leg{k}_d_lm"], dt, band)
        lt = corrmod.lowpass(z[f"leg{k}_d_light"], dt, band)
        E = E1                              # every leg is on leg 1's time axis
        top, aft = (E[1] - TOP_PAD, E[2] + TOP_PAD), (E[3], min(E[3] + AFTER_FALL, tl[-1]))
        rec = {"leg": k}
        rec["top_lm_mdeg"], rec["top_lm_at_ms"] = _extreme(tl, lm, *top, E[0])
        rec["top_lt_mdeg"], rec["top_lt_at_ms"] = _extreme(tl, lt, *top, E[0])
        rec["after_fall_lm_mdeg"], rec["after_fall_lm_at_ms"] = _extreme(tl, lm, *aft, E[0])
        rec["after_fall_lt_mdeg"], rec["after_fall_lt_at_ms"] = _extreme(tl, lt, *aft, E[0])
        act = z["phi_target"] > 0.5
        rec["lt_rms_mdeg"] = float(np.sqrt(np.mean(lt[act] ** 2)) * 1e3)
        rec["lm_rms_mdeg"] = float(np.sqrt(np.mean(lm[act] ** 2)) * 1e3)
        rec["after_fall_window_ms"] = [(aft[0] - E[0]) * 1e3, (aft[1] - E[0]) * 1e3]
        out.append(rec)
    return {"legs": out, "source": "target_compare.npz (after-fall window cut at the "
                                    "leg window, 0.3 ms)"}


def verdict(summary) -> dict:
    """The numbers a before/after reads, from compare()'s summary.

    Per leg (summary["extremes"]["legs"], which compare() adds; a summary
    without it is read from <out>/target_compare.npz instead, whose leg
    window ends 0.3 ms after the fall): top_lm_mdeg / top_lt_mdeg, the signed
    extreme of light - monitors / light - target over the hold +- 0.3 ms;
    after_fall_lm_mdeg / after_fall_lt_mdeg, the same over the first ms after
    the fall; lt_rms_mdeg, light - target rms over the active part (target >
    0.5 deg) - all band-limited at the correction band; and lt_std_mdeg /
    lm_std_mdeg / mt_std_mdeg, the comparison's own standard deviations
    (summary["legs"][k]["d_light_fit" / "d_lm_fit" / "d_mon_fit"]["rms_mdeg"]).
    Per crystal (summary["correction"]["per_crystal"][ch]["peak_V"], V at the
    EOM, and summary["V90_mon"] in x1/x2 order): the correction peak in V and
    in mdeg of rotation (peak_V x 90 / (V90 x 1000)); summary["correction"]
    ["peak_mdeg"] is the total. "light - target" is against the target the
    states were compared on - the BASE target, which is what the experiment
    needs the light to follow."""
    ext = summary.get("extremes") or _fallback_extremes(summary)
    fits = {L["leg"]: L for L in summary.get("legs", [])}
    legs = []
    for e in ext["legs"]:
        f = fits.get(e["leg"], {})
        rec = {k: v for k, v in e.items()}
        for key, src in (("lt_std_mdeg", "d_light_fit"), ("lm_std_mdeg", "d_lm_fit"),
                         ("mt_std_mdeg", "d_mon_fit")):
            rec[key] = float((f.get(src) or {}).get("rms_mdeg", float("nan")))
        # A record holding several transports (a spin echo's two legs in
        # one ILC record, 13 Oct 2026): the comparison reports each
        # transport's own numbers under "transports" (per leg, else for leg
        # 1 at the summary root). Carried as given; absent on a comparison
        # from before that change, and then the leg's numbers stand alone.
        tr = f.get("transports") or (summary.get("transports") if e["leg"] == 1 else None)
        if tr:
            rec["transports"] = [dict(t) for t in tr]
            hold_numbers(summary, e["leg"], rec["transports"])
            hv = [t["hold_lt_mdeg"] for t in rec["transports"] if np.isfinite(t.get("hold_lt_mdeg", float("nan")))]
            if hv:
                rec["hold_lt_mdeg"] = max(hv, key=abs)
                rec["hold_lm_mdeg"] = max((t["hold_lm_mdeg"] for t in rec["transports"]), key=abs)
            if len(tr) > 1:
                # the leg's own extremes come from the envelope, whose hold
                # window spans the idle between the transports: take the
                # worst transport instead, so a before/after (and the batch's
                # "halved" test) reads leg 2 when leg 2 is the problem
                for key in ("top_lm_mdeg", "top_lt_mdeg", "after_fall_lm_mdeg",
                            "after_fall_lt_mdeg"):
                    vals = [float(t.get(key, float("nan"))) for t in tr]
                    vals = [v for v in vals if np.isfinite(v)]
                    if vals:
                        rec[key] = max(vals, key=abs)
                rec["from"] = "worst transport"
        legs.append(rec)
    corr = summary["correction"]
    v90 = list(summary.get("V90_mon") or [5.1283, 5.1374])
    crystals = {}
    for i, (ch, pc) in enumerate(corr.get("per_crystal", {}).items()):
        pk = float(pc["peak_V"])
        crystals[ch] = {"peak_V": pk, "peak_mdeg": pk * 90.0 / (v90[min(i, 1)] * 1000.0) * 1e3,
                        "rms_V_active": float(pc.get("rms_V_active", float("nan")))}
    psi = float(summary.get("psi_rest_deg", float("nan")))
    if "zero_off_deg" in summary:
        expected, off = float(summary["rest_expected_deg"]), float(summary["zero_off_deg"])
    else:                                     # a summary from before 14 Oct 2026
        expected, off = rest_expected(psi) if np.isfinite(psi) else (float("nan"), float("nan"))
    return {"scan": summary.get("scan"), "legs": legs,
            "correction": {"peak_mdeg": float(corr["peak_mdeg"]),
                           "rms_mdeg_active": float(corr.get("rms_mdeg_active", float("nan"))),
                           "crystals": crystals},
            "psi_rest_deg": psi, "analyzer_zero_deg": float(summary.get("analyzer_zero_deg", float("nan"))),
            "rest_expected_deg": expected, "zero_off_deg": off,
            "zero_ok": bool(np.isfinite(off) and abs(off) <= ZERO_TOL_DEG),
            "source": ext.get("source", "")}


HOLD_IN = 0.2e-3     # s inside the flat top's ends: the hold's own number, clear of the ramp edges


def hold_numbers(summary, leg, transports):
    """Each transport's light - target and light - monitors averaged over the
    inside of its flat top (HOLD_IN clear of both ends), low-passed at the
    band, from the comparison's target_compare.npz: hold_lt_mdeg,
    hold_lm_mdeg and hold_lt_sd_mdeg on the transport dicts. The verdict's
    "ramp top" extreme is taken over the top with a 0.3 ms pad, where the
    ramp edges sit; the edge artefact of the comparison's time map (14 Oct
    2026: +300..+600 mdeg at the rise foot) dominates it, the hold's own
    number does not see it. NaN when the file or the leg is not there."""
    out = summary.get("out")
    path = os.path.join(out, "target_compare.npz") if out else None
    if not path or not os.path.exists(path):
        for t in transports:
            t.setdefault("hold_lt_mdeg", float("nan"))
            t.setdefault("hold_lm_mdeg", float("nan"))
        return
    z = np.load(path)
    key = f"leg{leg}_d_light"
    if key not in z.files:
        return
    tl = z["t_leg1"]
    ms, dt = (tl - tl[0]) * 1e3, float(np.median(np.diff(tl)))
    band = float(summary.get("f_cut_Hz") or 5000.0)
    lt = corrmod.lowpass(z[key], dt, band) * 1e3
    lm = corrmod.lowpass(z[f"leg{leg}_d_lm"], dt, band) * 1e3
    for t in transports:
        a, b, c, d = (float(x) for x in t["landmarks_ms"])
        m = (ms > b + HOLD_IN * 1e3) & (ms < c - HOLD_IN * 1e3)
        if m.any():
            t["hold_lt_mdeg"], t["hold_lm_mdeg"] = float(np.mean(lt[m])), float(np.mean(lm[m]))
            t["hold_lt_sd_mdeg"] = float(np.std(lt[m]))
        else:
            t["hold_lt_mdeg"] = t["hold_lm_mdeg"] = float("nan")


def verdict_lines(v) -> list:
    """verdict() as log lines."""
    out = []
    for L in v["legs"]:
        out.append(f"leg {L['leg']}: ramp top light-monitors {L['top_lm_mdeg']:+.0f} mdeg, "
                   f"light-target {L['top_lt_mdeg']:+.0f}"
                   + (f" (inside the hold {L['hold_lt_mdeg']:+.0f})" if np.isfinite(L.get("hold_lt_mdeg", float("nan"))) else "")
                   + f"; first ms after the fall "
                   f"{L['after_fall_lm_mdeg']:+.0f} / {L['after_fall_lt_mdeg']:+.0f} mdeg; "
                   f"light-target rms {L['lt_rms_mdeg']:.0f} mdeg")
        for t in L.get("transports") or []:
            if len(L["transports"]) < 2:
                break                     # one transport: the leg line says it
            lm = t.get("landmarks_ms") or [float("nan")] * 4
            out.append(f"   transport {t.get('index', '?')} ({lm[0]:.2f}-{lm[3]:.2f} ms): ramp top "
                       f"light-monitors {t.get('top_lm_mdeg', float('nan')):+.0f} mdeg, light-target "
                       f"{t.get('top_lt_mdeg', float('nan')):+.0f} (inside the hold "
                       f"{t.get('hold_lt_mdeg', float('nan')):+.0f}); after the fall "
                       f"{t.get('after_fall_lm_mdeg', float('nan')):+.0f} / "
                       f"{t.get('after_fall_lt_mdeg', float('nan')):+.0f} mdeg; light-target rms "
                       f"{t.get('lt_rms_mdeg', float('nan')):.0f} mdeg")
    c = v["correction"]
    out.append("correction peak " + f"{c['peak_mdeg']:.0f} mdeg: " + ", ".join(
        f"{ch} {x['peak_mdeg']:.0f} mdeg = {x['peak_V']:.1f} V" for ch, x in c["crystals"].items()))
    psi = v.get("psi_rest_deg", float("nan"))
    if np.isfinite(psi):
        z = v.get("analyzer_zero_deg", float("nan"))
        exp_, off = v.get("rest_expected_deg", float("nan")), v.get("zero_off_deg", float("nan"))
        out.append(f"analyzer zero: the rest polarization sits at {psi:+.2f} deg in the analyzer frame, "
                   f"expected {exp_:+.2f} (crossed angle + 90)"
                   + (f" (zero = mount {z:.3f} deg)" if np.isfinite(z) else "")
                   + (f" -- OK, {off:+.2f} deg off, within {ZERO_TOL_DEG:g}" if v.get("zero_ok") else
                      f" -- OFF by {off:+.2f} deg (more than {ZERO_TOL_DEG:g}): the hold-null angles miss "
                      f"the null; the analyzer zero or the polarimeter's seq_crossed_deg has moved -- "
                      f"fix it there and Copy from polarimeter"))
    return out


# ---------------------------------------------------------- review helpers
# What the panel's Batch window shows of a campaign's optical stage, from
# the files the stage leaves on disk (<outdir>/<stem>_optical.json and the
# scan folders' analysis/target_compare), with no instrument open -- during
# the run, after it, and after a restart (14 Oct 2026).

SCAN_KINDS = {"before": "before (both)", "X1": "X1 alone", "X2": "X2 alone", "verify": "verify (both)"}


def record_scans(rec) -> list:
    """The scans of a campaign's optical record, in the order they ran:
    dicts (pass, gain, kind, label, scan, folder, verdict, status). A pass's
    "before" that is the previous pass's verification (pass >= 2) is not
    repeated. `rec` is the dict in <stem>_optical.json."""
    out, seen = [], set()
    for p in rec.get("passes") or []:
        n, g = int(p.get("pass", 0)), p.get("gain")

        def add(kind, folder, verdict):
            if not folder or folder in seen:
                return
            seen.add(folder)
            if verdict and "zero_off_deg" not in verdict and np.isfinite(
                    verdict.get("psi_rest_deg", float("nan"))):
                # a record from before the crossed-zero convention (14 Oct 2026)
                verdict = dict(verdict)
                exp_, off = rest_expected(verdict["psi_rest_deg"])
                verdict.update(rest_expected_deg=exp_, zero_off_deg=off,
                               zero_ok=abs(off) <= ZERO_TOL_DEG)
            out.append(dict(**{"pass": n}, gain=g, kind=kind, label=SCAN_KINDS[kind],
                            scan=os.path.basename(folder), folder=folder, verdict=verdict,
                            status=p.get("status", "")))
        # "before_scan" with separate attribution, "scan" with joint
        add("before", p.get("before_scan") or p.get("scan"), p.get("verdict_before"))
        for key in ("X1", "X2"):
            add(key, (p.get("solo_scans") or {}).get(key), (p.get("verdicts_solo") or {}).get(key))
        add("verify", p.get("verify_scan"), p.get("verdict_after"))
    return out


def verdict_windows(v):
    """The windows a verdict reads, for shading: [(ms_lo, ms_hi), colour]
    per transport (the ramp top with its pad, the first ms after the fall),
    from the leg when the verdict has no transports."""
    out = []
    for L in v.get("legs") or []:
        tr = L.get("transports") or []
        if tr:
            for t in tr:
                a, b, c, d = (float(x) for x in t["landmarks_ms"])
                out.append(((b - TOP_PAD * 1e3, c + TOP_PAD * 1e3), "#ffe9a8"))
                out.append(((d, d + AFTER_FALL * 1e3), "#cfe8ff"))
        else:
            for win, col in ((L.get("top_window_ms"), "#ffe9a8"),
                             (L.get("after_fall_window_ms"), "#cfe8ff")):
                if win:
                    out.append(((float(win[0]), float(win[1])), col))
    return out


def scan_before(scans, i):
    """The joint scan a verification is measured against: the latest earlier
    joint scan (a before or a verification) in `scans`, else None."""
    for s in reversed(scans[:i]):
        if s["kind"] in ("before", "verify") and s.get("verdict"):
            return s
    return None


def scan_summary(folder):
    """The comparison summary of a scan folder as the panel's rotation and
    correction views take it (analysis/target_compare/summary.json with
    "out", "scan" and "files" {EO1/EO2: the correction csv}); None when the
    scan has no comparison yet."""
    out = os.path.join(folder, "analysis", "target_compare")
    path = os.path.join(out, "summary.json")
    if not os.path.exists(path):
        return None
    import json
    with open(path, encoding="utf-8") as fh:
        s = json.load(fh)
    s["out"] = out
    s.setdefault("scan", os.path.basename(folder))
    files = {}
    for f in (s.get("correction") or {}).get("files") or []:
        b = os.path.basename(f)
        if b.startswith("corr_") and b.endswith(".csv"):
            files["EO1" if "X1_" in b else "EO2" if "X2_" in b else b] = f
    s["files"] = files
    return s


def live_scan(folder, cache=None, load_capture=None, cfg=None):
    """A scan as far as it got, for the panel's live view: the PD intensity
    per analyzer angle and the light's rotation fitted from the angles
    measured so far. Reads only the new steps when `cache` (a dict the
    caller keeps) is passed. Returns a dict:
      name, steps_total, steps_done, running (any step not done),
      last (the last done step: target deg, kind), t_ms,
      theta (deg, the angles with data), I (K x N volts, dark-subtracted),
      rotation_deg (N; None with fewer than 3 angles), rest_deg, sign,
      mon_deg (the monitors of the last step in degrees, None without them),
      note.
    Raises OSError when the folder has no manifest yet. `cfg` is the ILC
    config (its polarimeter_path), as open_devices takes it."""
    import json
    _rampol(cfg)
    from rampol import analysis as an
    if load_capture is None:
        load_capture = _scope_grab(None, _pcfg(None)).load_capture
    d = an.load_scan(folder, load_capture, cache=cache)
    steps = d.manifest.get("steps") or []
    done = [s for s in steps if s.get("status") == "done"]
    out = dict(name=d.name, steps_total=len(steps), steps_done=len(done),
               running=any(s.get("status") not in ("done",) for s in steps),
               last=None, t_ms=None, theta=np.zeros(0), I=None, rotation_deg=None,
               rest_deg=float("nan"), sign=1.0, mon_deg=None, note="")
    if done:
        out["last"] = dict(target=float(done[-1].get("target", float("nan"))), kind=done[-1].get("kind"))
    if d.t is None:
        out["note"] = "no capture read yet"
        return out
    t = np.asarray(d.t, float)
    out["t_ms"] = t * 1e3
    th, I, sem, used = an.scan_matrix(d, "scan")
    out["theta"], out["I"] = th, I
    # the monitors' rotation from the last step with them, in the target's degrees
    for s in reversed(d.steps):
        v = s.get("v") or {}
        if "MonX1" in v and "MonX2" in v:
            v90 = _v90_mon()
            out["mon_deg"] = 90.0 * (np.asarray(v["MonX1"], float) / v90[0]
                                     + np.asarray(v["MonX2"], float) / v90[1])
            break
    if len(th) < 3:
        out["note"] = f"{len(th)} angle(s) so far -- the rotation needs 3"
        return out
    fit = an.harmonic_fit(th, I, sem)
    psi = an.unwrap_psi(fit["psi"])
    rest_m = t < 0
    rest = float(np.median(psi[rest_m])) if rest_m.any() else float(np.median(psi[: max(1, len(psi) // 50)]))
    rot = psi - rest
    rest = rest % 180.0                 # the azimuth is defined mod 180: report it as the bench does (+90)
    # the light's sense against the monitors' (the target's): measured per
    # scan, so the view needs no configured sign
    sign = 1.0
    if out["mon_deg"] is not None and len(out["mon_deg"]) == len(rot):
        m = out["mon_deg"] - np.median(out["mon_deg"][rest_m]) if rest_m.any() else out["mon_deg"]
        c = float(np.dot(rot, m))
        sign = -1.0 if c < 0 else 1.0
    out.update(rotation_deg=sign * rot, rest_deg=rest, sign=sign,
               note=f"{len(th)} angles fitted" + ("" if sign > 0 else ", sign flipped to the monitors' sense"))
    return out


def _v90_mon():
    """Monitor volts for 90 deg per crystal (the polarimeter's calibration)."""
    try:
        from rampol import ilc_target as it
        return [float(it.V90["EO1"]), float(it.V90["EO2"])]
    except Exception:
        return [5.1283, 5.1374]
