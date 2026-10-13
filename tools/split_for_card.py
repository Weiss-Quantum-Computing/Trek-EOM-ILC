#!/usr/bin/env python3
"""Cut a trained ILC drive pair into the experiment card's up/down files, at the trained rate.

    python split_for_card.py drive_S25X1_i15.csv drive_S25X2_i15.csv [--n 2500] [--settle-us 200]
    python split_for_card.py drive_S47E2X1_keep.csv drive_S47E2X2_keep.csv --targets target_S47E2X1.csv target_S47E2X2.csv

Both channels are cut at the SAME times, around the stretch where both are flat (the hold
at the stop angle). For PAR ramps that is each channel's own flat top; for the series and
shared ramps (HSER, HS81, HSH6, SW*) one channel goes flat long before the other, and
cutting each at its own top would break the relative timing between X1 and X2 on the card.

The up files run from the start to --settle-us into the common flat stretch, the down files
from --settle-us before its end to the end. The hold on the atoms is then the card pause
plus 2 x settle. The default 200 us keeps the ILC's learned post-corner correction (it
settles within ~190 us) inside the files. Each part is resampled to --n points and written
as 'index,value' lines, like Ramps/20260924/drive_*_upramp.csv. The script prints the span
of each file: program exactly that ramp time on the card, for both channels, so the
drives play at the rate the ILC trained them at.

Several transports in one record (13 Oct 2026, the spin echo: tools/make_echo_target.py).
The transports are the stretches where the TARGETS are above 0.1 % of their max (with
--targets; from the drives alone, above 5 %, since a trained drive's idle is trimmed, not
zero). With one transport nothing changes: the files are <drive>_upramp.csv and
<drive>_downramp.csv, cut as above. With N > 1 each transport k gets its own pair,
<drive>_leg<k>_upramp.csv and <drive>_leg<k>_downramp.csv, cut around ITS common flat
stretch: leg 1's up file starts at the record's start (the lead), the last leg's down file
ends at the record's end (the tail), and the inner cuts keep the same lead before each
later leg and the same tail after each earlier one. Between legs the script prints the
card pause the record implies:

    pause after leg k = spacing - (up span + card hold + down span)
    card hold         = hold on the atoms - 2 x settle

spacing = leg k's start to leg k+1's start (the up files' starts), hold on the atoms =
the leg's common flat stretch. That pause is exactly the idle the ILC trained between
the two down/up files, so the card reproduces the record's timing.
"""
import argparse, os
import numpy as np

TARGET_FRAC = 1e-3      # a transport: where a TARGET is above 0.1 % of its max
DRIVE_FRAC = 0.05       # ... or a drive above 5 % (its idle is trimmed, not zero)
MIN_GAP_US = 50.0       # idle gaps shorter than this do not split a transport


def read(path):
    rows = []
    for line in open(path):
        f = line.strip().split(",")
        if line.startswith("#") or len(f) < 2:
            continue
        try:
            rows.append((float(f[0]), float(f[1])))
        except ValueError:          # the time_us,voltage_V header
            continue
    return np.array(rows).T


def read_meta(path):
    """The machine-readable '# key: v1, v2' comment lines of a target
    (make_echo_target writes legs_ms, spacing_ms, lead_us, tail_us,
    transport_us); values as float lists where they parse."""
    meta = {}
    for line in open(path):
        if not line.startswith("#"):
            if line.strip():
                break
            continue
        body = line[1:].strip()
        key, sep, val = body.partition(":")
        if not sep or " " in key.strip():
            continue
        try:
            meta[key.strip()] = [float(x) for x in val.split("(")[0].split(",") if x.strip()]
        except ValueError:
            meta[key.strip()] = val.strip()
    return meta


def flat_top(t, v, tol=0.002):
    idx = np.flatnonzero(v >= v.max() - tol * max(np.ptp(v), 1e-9))
    return t[idx[0]], t[idx[-1]]


def find_transports(vs, dt_us, frac=TARGET_FRAC, min_gap_us=MIN_GAP_US, exact_idle=None):
    """Index pairs (i0, i1) of the transports in a record: the stretches where
    ANY channel is above `frac` of its own max |v| (so both channels of a
    series ramp share one stretch), gaps under `min_gap_us` merged, each
    widened to the whole ramp.

    A target's ramps start and end smoothly: S47 stays under 0.1 % of its peak
    for its first and last ~40 us. With `exact_idle` (default: when frac is
    the target threshold) a stretch is therefore widened out to the first and
    last samples OFF the exact idle (the record's first value), so it holds
    the whole ramp: S47's runs 200..10612 us, the record's own '200 us 0 |
    ramp | 200 us 0'. Otherwise (drives, whose idle is trimmed, not exact) by
    one sample each side, onto the idle."""
    if exact_idle is None:
        exact_idle = frac <= TARGET_FRAC
    n = len(vs[0])
    mask = np.zeros(n, bool)
    busy = np.zeros(n, bool)           # anything off the exact idle
    for v in vs:
        v = np.asarray(v, float)
        a = np.abs(v)
        if a.max() > 0:
            mask |= a > frac * a.max()
            busy |= np.abs(v - v[0]) > 1e-12 * a.max()
    idx = np.flatnonzero(mask)
    if not idx.size:
        return []
    br = np.flatnonzero(np.diff(idx) > 1)
    runs = [[s, e] for s, e in zip(np.r_[idx[0], idx[br + 1]], np.r_[idx[br], idx[-1]])]
    merged = [runs[0]]
    for s, e in runs[1:]:
        if (s - merged[-1][1] - 1) * dt_us < min_gap_us:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    out = []
    for s, e in merged:
        s, e = int(s), int(e)
        if exact_idle:
            while s > 1 and busy[s - 1]:
                s -= 1
            while e < n - 2 and busy[e + 1]:
                e += 1
        if exact_idle:                     # first and last sample off the idle
            i0, i1 = s, e
        else:
            i0, i1 = max(s - 1, 0), min(e + 1, n - 1)
        if out and i0 <= out[-1][1]:       # widened into the previous one: one transport
            out[-1] = (out[-1][0], i1)
        else:
            out.append((i0, i1))
    return out


def _write(path, ts_lo, ts_hi, tt, vv, n, log):
    ts = np.linspace(ts_lo, ts_hi, n)
    vs = np.interp(ts, tt, vv)
    with open(path, "w", newline="") as f:
        for i, x in enumerate(vs, 1):
            f.write(f"{i},{x:.10g}\n")
    log(f"  card: {os.path.basename(path)}: {n} samples over {ts_hi - ts_lo:.1f} us, "
        f"first/last {vs[0]:.5f} / {vs[-1]:.5f} V")


def split_pair(drives, n=2500, settle_us=200.0, log=print, targets=None):
    """Cut drive files (X1, X2) at common times; returns what was written.

    `targets` (the matching target files) locate the hold. A trained drive is
    not flat across its hold to the 0.2 % the drive-only detection asks for --
    the learned correction settles there -- so pass the targets whenever you
    have them; the drives alone are the fallback.

    A record with several transports (a spin echo's legs) is cut per leg; the
    returned dict then carries `legs` (per leg: up/down windows and spans,
    the flat stretch, the card hold) and `pauses_ms` (the card pause between
    legs). `up_ms`/`down_ms` are leg 1's."""
    data = [read(p) for p in drives]
    t = data[0][0]
    for tt, _ in data[1:]:
        if len(tt) != len(t) or np.abs(tt - t).max() > 1e-6:
            raise ValueError("the drives are not on one time grid")
    ref = [read(p) for p in targets] if targets else data
    for tt, _ in ref:
        if len(tt) != len(t) or np.abs(tt - t).max() > 1e-6:
            raise ValueError("the targets are not on the drives' time grid")
    dt = float(np.median(np.diff(t)))
    segs = find_transports([vv for _, vv in ref], dt, TARGET_FRAC if targets else DRIVE_FRAC)

    if len(segs) <= 1:
        # one transport: the whole record, exactly as before 13 Oct 2026
        windows = [(0, len(t) - 1)]
    else:
        mids = [(segs[k][1] + segs[k + 1][0]) // 2 for k in range(len(segs) - 1)]
        windows = list(zip([0] + mids, mids + [len(t) - 1]))
    flats = []
    for k, (a, b) in enumerate(windows):
        tops = [flat_top(tt[a:b + 1], vv[a:b + 1]) for tt, vv in ref]
        t_in, t_out = max(x[0] for x in tops), min(x[1] for x in tops)   # where every channel is flat
        if t_out - t_in < 2 * settle_us:
            where = f"leg {k + 1}'s " if len(windows) > 1 else ""
            raise ValueError(f"{where}common flat stretch is only {t_out - t_in:.0f} us; lower settle_us")
        flats.append((t_in, t_out))

    # the cut windows: leg 1 up from the record's start, last leg down to its
    # end; inner cuts keep the record's lead before and tail after each leg
    if len(windows) == 1:
        cuts = [((t[0], flats[0][0] + settle_us), (flats[0][1] - settle_us, t[-1]))]
    else:
        lead = t[segs[0][0]] - t[0]
        tail = t[-1] - t[segs[-1][1]]
        lo = [t[0]]
        hi = []
        for k in range(len(segs) - 1):
            e, s = t[segs[k][1]], t[segs[k + 1][0]]
            gap = s - e
            if gap >= lead + tail:
                hi.append(e + tail)
                lo.append(s - lead)
            else:                    # no room for both: meet inside the gap, no pause
                cut = e + (gap * tail / (lead + tail) if lead + tail > 0 else gap / 2)
                hi.append(cut)
                lo.append(cut)
        hi.append(t[-1])
        cuts = [((lo[k], flats[k][0] + settle_us), (flats[k][1] - settle_us, hi[k]))
                for k in range(len(segs))]

    legs = []
    for k, ((u0, u1), (d0, d1)) in enumerate(cuts):
        t_in, t_out = flats[k]
        legs.append(dict(up=(u0, u1), down=(d0, d1), up_ms=(u1 - u0) / 1e3, down_ms=(d1 - d0) / 1e3,
                         flat_us=(t_in, t_out), hold_ms=(t_out - t_in) / 1e3,
                         card_hold_ms=(t_out - t_in - 2 * settle_us) / 1e3))
    pauses = []
    for k in range(len(legs) - 1):
        a, b = legs[k], legs[k + 1]
        spacing = (b["up"][0] - a["up"][0]) / 1e3
        pause = spacing - (a["up_ms"] + a["card_hold_ms"] + a["down_ms"])
        pauses.append(dict(spacing_ms=spacing, pause_ms=pause,
                           leg_spacing_ms=(t[segs[k + 1][0]] - t[segs[k][0]]) / 1e3))

    if len(legs) == 1:
        L = legs[0]
        log(f"  card: common flat stretch {L['flat_us'][0]:.0f}..{L['flat_us'][1]:.0f} us; "
            f"hold on the atoms = card pause + {2 * settle_us:.0f} us")
        log(f"  card: program up files {L['up_ms']:.4f} ms, down files {L['down_ms']:.4f} ms, both channels")
    else:
        log(f"  card: {len(legs)} transports in the record; both channels cut at the same times per leg")
        meta = read_meta(targets[0]) if targets else {}
        if isinstance(meta.get("legs_ms"), list) and len(meta["legs_ms"]) == len(segs):
            found = [t[s] / 1e3 for s, _ in segs]
            off = max(abs(x - y) for x, y in zip(found, meta["legs_ms"]))
            if off > 1.5 * dt / 1e3:
                log(f"  card: WARNING the target's legs_ms {meta['legs_ms']} differ from the "
                    f"transports found ({', '.join(f'{x:.3f}' for x in found)} ms) by {off * 1e3:.0f} us")
        for k, L in enumerate(legs):
            log(f"  card: leg {k + 1}: common flat stretch {L['flat_us'][0]:.0f}..{L['flat_us'][1]:.0f} us "
                f"= hold on the atoms {L['hold_ms']:.4f} ms = card hold {L['card_hold_ms']:.4f} ms "
                f"+ {2 * settle_us:.0f} us")
            log(f"  card: leg {k + 1}: program up files {L['up_ms']:.4f} ms "
                f"({L['up'][0]:.0f}..{L['up'][1]:.0f} us), down files {L['down_ms']:.4f} ms "
                f"({L['down'][0]:.0f}..{L['down'][1]:.0f} us), both channels")
            if k < len(pauses):
                P = pauses[k]
                log(f"  card: pause between leg {k + 1} and leg {k + 2} = spacing - (up + card hold + down)"
                    f" = {P['spacing_ms']:.4f} - ({L['up_ms']:.4f} + {L['card_hold_ms']:.4f}"
                    f" + {L['down_ms']:.4f}) = {P['pause_ms']:.4f} ms"
                    f"   [card hold = hold on the atoms - 2 x settle]")
                if abs(P["spacing_ms"] - P["leg_spacing_ms"]) > 1e-6:
                    log(f"  card: NOTE the transports are only {P['leg_spacing_ms']:.4f} ms apart, too close "
                        f"for the record's lead and tail between them; the files meet in the gap")

    written = dict(up_ms=legs[0]["up_ms"], down_ms=legs[0]["down_ms"],
                   hold_extra_us=2 * settle_us, files=[], legs=legs,
                   pauses_ms=[P["pause_ms"] for P in pauses])
    for path, (tt, vv) in zip(drives, data):
        stem = os.path.splitext(path)[0]
        for k, L in enumerate(legs):
            leg = f"_leg{k + 1}" if len(legs) > 1 else ""
            for part, (lo_, hi_) in (("upramp", L["up"]), ("downramp", L["down"])):
                out = f"{stem}{leg}_{part}.csv"
                _write(out, lo_, hi_, tt, vv, n, log)
                written["files"].append(out)
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("drives", nargs="+", help="X1 and X2 drive files (one file only for a single-channel test)")
    ap.add_argument("--n", type=int, default=2500, help="samples per card file")
    ap.add_argument("--settle-us", type=float, default=200.0, help="how far into the flat stretch each file runs")
    ap.add_argument("--targets", nargs="+", help="the matching target files, to locate the hold (recommended)")
    a = ap.parse_args()
    try:
        split_pair(a.drives, a.n, a.settle_us, targets=a.targets)
    except ValueError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
