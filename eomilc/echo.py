"""A spin echo's transports in one ILC record, at the echo's spacing.

Why (13 Oct 2026): the polarization drift during a hold carries into the
next transport -- the crystal remembers leg 1 when leg 2 plays (30 Sep -
1 Oct, leg 2 rotated +0.12-0.18 deg with an identical drive). Trained one at
a time, each transport is learned from a rested crystal; trained in one
record at the echo's spacing, leg 2 is learned in the state leg 1 leaves
behind.

The source's transport is the stretch where the target is above 0.1 % of its
max, widened to its first and last samples off the idle (the suite records
are '200 us 0 | ramp | 200 us 0', and S47's ramp stays under 0.1 % for its
first ~40 us). The record is: the source's lead, transport 1, the idle (the
source's first sample) until leg 2 starts `spacing` after leg 1's start,
transport 2, ..., then `tail_us` of idle. Same grid as the source; the
spacing is rounded to it (16.667 ms on 2 us -> 16.668 ms) and the comments
carry the value used. X2 is built from its own source at the SAME indices, so
the pair shares one grid (sources differing in grid or length are refused, as
is a spacing shorter than the transport).

`make` writes target_<stem><suffix>X1.csv / X2.csv (suffix default E<legs>)
with the ILC header and comment lines carrying the sources, the spacing, each
leg's start and the transport's rise/hold/fall, plus machine-readable lines

    # spacing_ms: 16.668
    # legs_ms: 0.200, 16.868
    # lead_us: 200
    # tail_us: 200
    # transport_us: 10412, 4570, 1272, 4570   (span, rise, hold, fall)

that tools/split_for_card.py cross-checks when it cuts the trained drives
into the card's files. The hold is the common flat stretch the card split
uses (both channels within 0.2 % of their peaks), not the design's nominal
hold. Then each channel is read back with run_ilc.load_target and its flat
first shot put through the limit check, as `run_ilc.py init` does.

Three callers share this: tools/make_echo_target.py (the command line), the
panel's Session tab (Echo... beside the Target box) and ilc_batch (a
campaign's "echo" key builds the record before the campaign runs).
"""
from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEM_CAP = 5            # the panel's pair mode appends X1/X2 to the name stem
CHANNEL = {"X1": "EO1", "X2": "EO2"}
_sfc = None


def _card():
    """tools/split_for_card.py, loaded by path once: its reader, flat-top and
    transport finder are the card split's, so both agree on every cut."""
    global _sfc
    if _sfc is None:
        spec = importlib.util.spec_from_file_location(
            "split_for_card", os.path.join(HERE, "tools", "split_for_card.py"))
        _sfc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_sfc)
    return _sfc


def awg_max_pts():
    """The panel's arb-memory cap (ilc_gui.AWG_MAX_PTS), read the same way."""
    return int(os.environ.get("BK4063B_MAX_PTS", 16384))


def read_target(path):
    """(comment lines without '#', t_us, v) of an ILC target CSV."""
    comments = []
    for line in open(path):
        if line.startswith("#"):
            comments.append(line[1:].strip())
        elif line.strip():
            break
    t, v = _card().read(path)
    return comments, t, v


def name_parts(path):
    """target_S47X1.csv -> ('S47', 'X1')."""
    base = os.path.splitext(os.path.basename(path))[0]
    if base.startswith("target_"):
        base = base[len("target_"):]
    if base[-2:] not in CHANNEL:
        raise ValueError(f"{os.path.basename(path)}: the name must end in X1 or X2 "
                         f"(target_<stem>X1.csv), to know the channel")
    return base[:-2], base[-2:]


def partner(path):
    """The X2 file beside an X1 file (or X1's beside X2's), by name; None when
    it is not there."""
    stem, ch = name_parts(path)
    other = "X2" if ch == "X1" else "X1"
    p = os.path.join(os.path.dirname(os.path.abspath(path)), f"target_{stem}{other}.csv")
    return p if os.path.exists(p) else None


def pieces(t, v, i0, i1):
    """Rise, hold and fall (us) of one channel's transport: the flat top is
    where it is within 0.2 % of the swing of its peak (split_for_card.flat_top)."""
    a, b = _card().flat_top(t[i0:i1 + 1], v[i0:i1 + 1])
    return a - t[i0], b - a, t[i1] - b, (a, b)


def build(paths, spacing_ms, legs=2, tail_us=200.0):
    """The echo record for each source (same indices on every channel).
    Returns a dict with the per-channel arrays and the numbers for the header."""
    sfc = _card()
    if legs < 2:
        raise ValueError("legs must be 2 or more")
    src = [read_target(p) for p in paths]
    t = src[0][1]
    for p, (_c, tt, _v) in zip(paths[1:], src[1:]):
        if len(tt) != len(t) or np.abs(tt - t).max() > 1e-6:
            raise ValueError(f"{os.path.basename(p)} is not on {os.path.basename(paths[0])}'s grid "
                             f"({len(tt)} vs {len(t)} points); the pair must share one record")
    d = np.diff(t)
    dt = float(np.median(d))
    if np.abs(d - dt).max() > 1e-6 * max(dt, 1.0):
        raise ValueError("the source's time grid is not uniform")
    vs = [v for _c, _t, v in src]
    segs = sfc.find_transports(vs, dt)
    if len(segs) != 1:
        raise ValueError(f"the source holds {len(segs)} transports (stretches above 0.1 % of max); "
                         f"give a one-transport target")
    i0, i1 = segs[0]
    span = i1 - i0                                       # intervals, transport = span*dt
    sp = int(np.floor(spacing_ms * 1e3 / dt + 0.5))
    if sp <= span:
        raise ValueError(f"spacing {spacing_ms:g} ms is not longer than the transport "
                         f"({span * dt / 1e3:.3f} ms, {t[i0]:.0f}..{t[i1]:.0f} us); "
                         f"the legs would overlap")
    ntail = int(np.floor(tail_us / dt + 0.5))
    n = i0 + (legs - 1) * sp + span + 1 + ntail
    starts = [i0 + k * sp for k in range(legs)]
    out = []
    for v in vs:
        r = np.full(n, v[0])
        r[:i0] = v[:i0]                                   # the source's lead
        for s in starts:
            r[s:s + span + 1] = v[i0:i1 + 1]
        out.append(r)
    t_new = t[0] + dt * np.arange(n)
    per = [pieces(t, v, i0, i1) for v in vs]
    t_in = max(p[3][0] for p in per)
    t_out = min(p[3][1] for p in per)
    return dict(t=t_new, v=out, dt=dt, i0=i0, i1=i1, span_us=span * dt, sp=sp,
                spacing_ms=sp * dt / 1e3, req_ms=spacing_ms, legs_ms=[t_new[s] / 1e3 for s in starts],
                lead_us=t[i0] - t[0], tail_us=ntail * dt, n=n, per=per, comments=[c for c, _t, _v in src],
                common=(t_in - t[i0], t_out - t_in, t[i1] - t_out), legs=legs)


def lines_for(rec, k, paths, stem, ch, suffix):
    """The comment lines of one channel's file (without the '# ')."""
    dt, legs = rec["dt"], rec["legs"]
    rise, hold, fall, _ = rec["per"][k]
    cr, ch_, cf = rec["common"]
    idle = rec["spacing_ms"] * 1e3 - rec["span_us"]
    src_rec = next((c for c in rec["comments"][k] if c.startswith("record:")), "")
    tr = f"{stem} transport {rec['span_us']:.0f} us"
    segs = [f"{rec['lead_us']:.0f} us 0", tr] + [f"{idle:.0f} us 0", tr] * (legs - 1) \
        + [f"{rec['tail_us']:.0f} us 0"]
    return [
        f"{stem}{suffix} {ch}: spin echo, {legs} transports of {stem} in one record at "
        f"{rec['spacing_ms']:.3f} ms leg spacing (leg start to leg start); eomilc.echo",
        "record: " + " | ".join(segs)
        + f"; {rec['n']} pts at {dt:g} us = {rec['n'] * dt / 1e3:.4f} ms period",
        "source: " + ", ".join(os.path.basename(p) for p in paths)
        + f" (this channel: {os.path.basename(paths[k])}; {os.path.dirname(os.path.abspath(paths[k]))})",
        f"source {src_rec}" if src_rec else "source record: (no record line)",
        f"spacing requested {rec['req_ms']:g} ms, {rec['spacing_ms']:.3f} ms on the {dt:g} us grid; "
        f"legs start at " + ", ".join(f"{x:.3f}" for x in rec["legs_ms"]) + " ms",
        f"transport ({ch}): rise {rise:.0f} us | hold {hold:.0f} us | fall {fall:.0f} us "
        f"(flat to 0.2 % of the swing); common to the pair: rise {cr:.0f} | hold {ch_:.0f} | fall {cf:.0f} us",
        f"card: split the trained drives with tools/split_for_card.py --targets ({2 * legs} files, legs cut "
        f"at the same times on both channels; it prints the pause between legs)",
        f"spacing_ms: {rec['spacing_ms']:.3f}",
        "legs_ms: " + ", ".join(f"{x:.3f}" for x in rec["legs_ms"]),
        f"lead_us: {rec['lead_us']:.0f}",
        f"tail_us: {rec['tail_us']:.0f}",
        f"transport_us: {rec['span_us']:.0f}, {cr:.0f}, {ch_:.0f}, {cf:.0f}   (span, rise, hold, fall; common)",
    ]


def render(rec, k, paths, stem, ch, suffix):
    """The whole file as text (CRLF-free; written with newline='')."""
    body = ["# " + c for c in lines_for(rec, k, paths, stem, ch, suffix)] + ["time_us,voltage_V"]
    body += [f"{x:.6f},{y:.6f}" for x, y in zip(rec["t"], rec["v"][k])]
    return "\n".join(body) + "\n"


def write(path, rec, k, paths, stem, ch, suffix):
    """One channel of the record, ILC header and comment lines. Returns True
    when the file changed (an identical file on disk is left alone, so a
    batch rebuilding its records every start does not touch their dates)."""
    text = render(rec, k, paths, stem, ch, suffix)
    try:
        with open(path, newline="") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    with open(path, "w", newline="") as f:
        f.write(text)
    return True


def check(path, ch):
    """run_ilc.load_target + the init limit check on the flat first shot:
    (points, dt_s, report)."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import run_ilc
    from eomilc import ilc
    from eomilc.config import CHANNELS
    c = CHANNELS[CHANNEL[ch]]
    t, v = run_ilc.load_target(path, c.mon_scale)
    dt = float(np.median(np.diff(t)))
    p = c.plant(float(np.ptp(v)), dt)
    loop = ilc.Loop(plant=p, target=v, dt=dt, channel=c, gamma=0.6, f_cut=5e3, limits=c.limits)
    rep = loop.check(loop.first_shot(flat=True))
    return len(t), dt, rep


def pause_ms(rec, settle_us=200.0):
    """The card's pause between legs for this record: spacing minus the up
    file, the card hold and the down file (split_for_card's rule, with the
    common flat stretch as the hold on the atoms and 2 x settle inside the
    files)."""
    cr, hold, cf = rec["common"]
    up = rec["lead_us"] + cr + settle_us
    down = cf + settle_us + rec["tail_us"]
    card_hold = hold - 2 * settle_us
    return (rec["spacing_ms"] * 1e3 - (up + card_hold + down)) / 1e3


def make(targets, spacing_ms=16.667, legs=2, tail_us=200.0, suffix=None, out_dir=None,
         log=print, do_check=True):
    """Build and write the record for one or two sources (X1 [X2]; a lone X1
    takes its X2 partner from beside it when there is one). Logs what the
    command line prints. Returns dict(files {ch: path}, rec, stem, name,
    written {ch: bool}, checks {ch: (ok, text)}, ok, notes [str]).
    Raises ValueError for a source it cannot use."""
    paths = list(targets)
    if len(paths) == 1:
        p2 = partner(paths[0])
        if p2:
            paths.append(p2)
    if len(paths) > 2:
        raise ValueError("give one or two targets (X1 [X2])")
    parts = [name_parts(p) for p in paths]
    if len({s for s, _ in parts}) != 1 or len({c for _, c in parts}) != len(parts):
        raise ValueError("give one stem's X1 and X2 (target_<stem>X1.csv target_<stem>X2.csv)")
    legs = int(legs)
    suffix = suffix if suffix is not None else f"E{legs}"
    rec = build(paths, float(spacing_ms), legs, float(tail_us))
    stem = parts[0][0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(paths[0]))
    os.makedirs(out_dir, exist_ok=True)
    files, written = {}, {}
    for k, (_s, ch) in enumerate(parts):
        path = os.path.join(out_dir, f"target_{stem}{suffix}{ch}.csv")
        written[ch] = write(path, rec, k, paths, stem, ch, suffix)
        files[ch] = path
        log(f"{'wrote' if written[ch] else 'unchanged'} {path}")
    cr, ch_, cf = rec["common"]
    log(f"record: {rec['n'] * rec['dt'] / 1e3:.4f} ms period ({rec['t'][-1] / 1e3:.3f} ms last sample), "
        f"{rec['n']} points at {rec['dt']:g} us")
    log(f"legs  : {legs} at " + ", ".join(f"{x:.3f}" for x in rec["legs_ms"])
        + f" ms; spacing {rec['spacing_ms']:.3f} ms (requested {spacing_ms:g})")
    log(f"transport: {rec['span_us']:.0f} us (rise {cr:.0f} | hold {ch_:.0f} | fall {cf:.0f} us, "
        f"common flat stretch), lead {rec['lead_us']:.0f} us, tail {rec['tail_us']:.0f} us")
    log(f"card  : pause between legs {pause_ms(rec):.3f} ms with the default 200 us settle "
        f"(split_for_card prints it again from the trained drives)")
    name = stem + suffix
    cap = awg_max_pts()
    notes = []
    log(f"note  : the panel's pair mode appends X1/X2 to the name stem, so stems are capped at "
        f"{STEM_CAP} characters (ilc_batch cuts longer ones to {STEM_CAP}): '{name}' is {len(name)}"
        + ("" if len(name) <= STEM_CAP else " -- TOO LONG for the pair mode; use a shorter suffix"))
    if len(name) > STEM_CAP:
        notes.append(f"stem '{name}' is {len(name)} characters; the pair mode allows {STEM_CAP}")
    log(f"note  : {rec['n']} points against the panel's 4063B cap AWG_MAX_PTS = {cap}"
        + (" -- OVER: raise BK4063B_MAX_PTS before the panel will upload it (the 4063B's "
           "true ceiling is unprobed; a refused upload means re-measure)"
           if rec["n"] > cap else " (BK4063B_MAX_PTS raises it for longer echoes)"))
    if rec["n"] > cap:
        notes.append(f"{rec['n']} points is over the upload cap of {cap}")
    checks, ok = {}, True
    if do_check:
        for ch, path in files.items():
            try:
                npts, dt, rep = check(path, ch)
            except Exception as e:           # the check must say why, not just fail
                checks[ch] = (False, f"load_target: {e}")
                log(f"check {ch} ({CHANNEL[ch]}): FAIL -- load_target: {e}")
                ok = False
                continue
            good = npts == rec["n"] and abs(dt * 1e6 - rec["dt"]) < 1e-6 and bool(rep)
            ok &= good
            txt = f"load_target {npts} pts at {dt * 1e6:g} us; limit check {rep}"
            checks[ch] = (good, txt)
            log(f"check {ch} ({CHANNEL[ch]}): {'PASS' if good else 'FAIL'} -- {txt}".replace("\n", "\n      "))
    return dict(files=files, rec=rec, stem=stem, name=name, suffix=suffix, written=written,
                checks=checks, ok=ok, notes=notes, sources=paths)
