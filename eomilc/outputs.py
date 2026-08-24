"""Emit a computed drive as an AWG file or as an NI coarse/fine channel pair."""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd

from .config import Channel, HV_PER_MON


def write_awg_csv(path: str, t: np.ndarray, u: np.ndarray, comment: str = "") -> str:
    """time_us, voltage_V -- the same layout as the target waveform files."""
    df = pd.DataFrame({"time_us": t * 1e6, "voltage_V": u})
    with open(path, "w", newline="") as f:
        if comment:
            for line in comment.splitlines():
                f.write(f"# {line}\n")
        df.to_csv(f, index=False)
    return path


BK_CACHE_MARK = "BK4063B-AWG-GUI waveform"


def write_bk_waveform(path: str, u_awg: np.ndarray, name: str,
                      full_scale: float = 10.0) -> str:
    """Write a drive in the AWG GUI's own single-column waveform format.

    `u_awg` is in volts at the generator output.  It is divided by `full_scale`,
    **not** by its own peak, and that distinction is the whole point of this
    function: normalising each iteration by its own peak silently rescales the
    correction the loop just computed, and the error stops falling for reasons
    that look like plant drift.  Upload with Normalise UNTICKED and
    AMP = 2 x full_scale, OFST = 0.

    Three separate things make this the right file to hand to the GUI rather
    than the `time_us,voltage_V` one, all learned the hard way:

    * With Normalise off the GUI expects samples ALREADY in -1..+1 and clips
      past it -- a file in volts comes out flat-topped across most of its span.
    * With Normalise on it divides by the record's own peak, which is the trap
      above.
    * A two-column file depends on the GUI's column picker choosing the second
      column.  `time_us` was not among its time-axis names until 2026-08-24, so
      before that it picked column 0 and uploaded the TIME AXIS -- which
      normalises to a clean ramp and so looks like a plausible arb.

    One column, already scaled, no picker involved.  The marker on the first
    line is what the GUI uses to recognise its own waveforms, so files written
    here drop straight into its `Waveforms` library and preview like any other.
    """
    u = np.asarray(u_awg, float).ravel()
    s = u / full_scale
    peak = float(np.abs(s).max())
    if peak > 1.0:
        raise ValueError(f"drive peaks at {peak*full_scale:.3f} V, past the "
                         f"{full_scale:g} V full scale this mapping assumes")
    with open(path, "w", newline="") as f:
        f.write(f"# {BK_CACHE_MARK} '{name}', {s.size} points, one sample per "
                f"line, fixed mapping 1.000 = {full_scale:g} V "
                f"(peak {peak:.4f} = {peak*full_scale:.4f} V)\n")
        f.write(f"# set AMP {2*full_scale:g} Vpp, OFST 0, and UNTICK "
                f"normalise on upload\n")
        np.savetxt(f, s, fmt="%.7g")
    return path


def read_bk_waveform(path: str) -> np.ndarray:
    """Samples from a BK4063B cache file (or any single-column list)."""
    return np.asarray(np.loadtxt(path, ndmin=1), dtype=np.float64).ravel()


def resample_points(y: np.ndarray, n: int) -> np.ndarray:
    """Stretch or squeeze a record to `n` points, endpoints preserved.

    Under DDS the stored record is resampled into one period regardless of its
    length, so the point count is free: do the learning on a grid that matches
    the measurement, then put the answer back at whatever length you want to
    upload.
    """
    y = np.asarray(y, float).ravel()
    if y.size == n:
        return y.copy()
    return np.interp(np.linspace(0.0, 1.0, n),
                     np.linspace(0.0, 1.0, y.size), y)


def headroom(target_hv: float, gain: float, full_scale: float = 10.0,
             record_cap: float = 1.0) -> dict:
    """What a record capped at `record_cap` can actually reach.

    Headroom comes from AMP sitting below the generator's ceiling, not from the
    record sitting below 1.  The 4063B's ceiling IS 20 Vpp, so full_scale = 10 V
    is the end of the road: the only way to buy room for the correction is to
    lower the target or raise the divider ratio ahead of the Trek.
    """
    need = target_hv / gain / HV_PER_MON
    reach = record_cap * full_scale * gain * HV_PER_MON
    ceiling = full_scale * gain * HV_PER_MON
    return dict(need_v=need, reach_hv=reach, ceiling_hv=ceiling,
                short_hv=max(0.0, target_hv - reach),
                spare_pct=100 * (ceiling / target_hv - 1),
                record_needed=need / full_scale)


@dataclass
class CoarseFine:
    coarse: np.ndarray        # volts to command on the coarse NI channel
    fine: np.ndarray          # volts to command on the fine NI channel
    realised: np.ndarray      # what the summing node actually produces
    lsb_coarse: float
    lsb_effective: float
    fine_headroom: float      # fraction of the fine channel's range still free

    def __repr__(self):
        return (f"CoarseFine(coarse LSB {self.lsb_coarse*1e6:.1f} uV, "
                f"effective LSB {self.lsb_effective*1e6:.2f} uV, "
                f"fine headroom {100*self.fine_headroom:.0f}%)")


def split_coarse_fine(u: np.ndarray, ch: Channel, bits: int = 16,
                      full_scale: float = 10.0, dc_offset: float = 0.0) -> CoarseFine:
    """Split a drive into coarse + fine for a 1:N summing network.

    The summing node produces   u = coarse + fine / N.
    The coarse channel carries the waveform, quantised at the card's LSB; the
    fine channel carries the quantisation residue at N times the resolution,
    which is what buys back the bits you need to sit on an EO zero.

    dc_offset is the standing EO-zero offset (in summing-node volts) that the
    fine channel must also carry -- it is reserved out of the fine range.
    """
    if not ch.has_fine_channel:
        raise ValueError(f"{ch.name} has no fine channel -- use quantise() instead")

    N = ch.fine_ratio
    lsb = 2 * full_scale / (2 ** bits)

    # reserve the standing offset out of the fine budget
    u_wave = np.asarray(u, float) - dc_offset

    coarse = np.round(u_wave / lsb) * lsb
    residue = u_wave - coarse                       # |residue| <= lsb/2
    fine_cmd = (residue + dc_offset) * N            # volts to command on the fine channel
    fine_cmd = np.round(fine_cmd / lsb) * lsb       # the fine card is quantised too

    clipped = np.clip(fine_cmd, -full_scale, full_scale)
    if not np.allclose(clipped, fine_cmd):
        raise ValueError(
            f"fine channel saturates: needs +/-{np.abs(fine_cmd).max():.3f} V "
            f"but the card is +/-{full_scale:.1f} V. Reduce dc_offset or raise the "
            f"coarse resolution.")

    realised = coarse + clipped / N
    headroom = 1.0 - float(np.abs(clipped).max()) / full_scale
    return CoarseFine(coarse=coarse, fine=clipped, realised=realised,
                      lsb_coarse=lsb, lsb_effective=lsb / N, fine_headroom=headroom)


def quantise(u: np.ndarray, bits: int = 16, full_scale: float = 10.0):
    """Single-channel quantisation, for a chain with no fine trim (EO1 today)."""
    lsb = 2 * full_scale / (2 ** bits)
    q = np.round(np.asarray(u, float) / lsb) * lsb
    return q, lsb


def resolution_table(ch: Channel, bits_list=(12, 16), full_scale: float = 10.0) -> str:
    """How many volts at the EOM one card LSB is worth, with and without fine trim."""
    rows = [f"{ch.name}: divider {ch.divider:.4f}, amp x monitor {ch.amp_mon_product:.4f}"]
    rows.append(f"{'bits':>5} {'card LSB':>11} {'HV per LSB':>12} {'with fine 1:%.0f' % ch.fine_ratio:>18}")
    for b in bits_list:
        lsb = 2 * full_scale / (2 ** b)
        hv = lsb * ch.divider * ch.amp_mon_product * HV_PER_MON
        fine = hv / ch.fine_ratio if ch.has_fine_channel else float("nan")
        s = f"{fine:14.4f} V" if ch.has_fine_channel else "        (none)"
        rows.append(f"{b:>5} {lsb*1e6:8.1f} uV {hv:10.3f} V {s}")
    return "\n".join(rows)
