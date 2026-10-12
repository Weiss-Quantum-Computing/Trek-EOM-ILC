"""What each scope channel carries, by ROLE -- the same vocabulary as the
ramp polarimeter's config (rampol.config.ROLES), so one channel map describes
the bench for every program that reads the MSO-X.

The panel and the batch used to carry channel NUMBERS: "AWG ch 1, scope ch 3",
and the drive read back from scope CH<awg_ch>. That assumption stopped being
true the day the photodiode took CH1 and the X1 command moved to CH2 (the
polarimeter's wiring, 5 Oct 2026). With roles the bench asks "which channel is
MonX1?" and "is there a CmdX1 channel at all?", and the answer travels with the
data: the state files record the map that was in force when they were measured.

The map lives in the panel's config.json under "scope_channels", in the
polarimeter's shape: {"1": {"role": "PD", "name": "Analyzer PD"}, ...}. The
batch runner reads the same file, so the two agree without a plan key; a plan's
"wiring" block still overrides it per channel.

Roles the Trek chains use:
    MonX1 / MonX2   the Trek monitor (1 V per kV) -- the loop's measurement
    CmdX1 / CmdX2   the Trek input, i.e. the AWG output teed to the scope --
                    the alignment check and Measure FRF read the drive here;
                    with no CmdXn channel assigned those read nothing and say so
    PD              the analyzer photodiode (Measure FRF's aux channel)
Everything else (Ref, Marker, Other, off) is recorded for the polarimeter's
benefit and ignored here.

No Tk, no numpy: importable by the CLI batch and the tests on any interpreter.
"""
from __future__ import annotations

import json
import os
import sys

ROLES = ("off", "PD", "MonX1", "MonX2", "CmdX1", "CmdX2", "Ref", "Marker", "Other")
ROLE_NAMES = {
    "off": "not recorded",
    "PD": "analyzer photodiode",
    "MonX1": "Trek monitor X1",
    "MonX2": "Trek monitor X2",
    "CmdX1": "Trek X1 command",       # the drive into the Trek input (AWG or NI card)
    "CmdX2": "Trek X2 command",
    "Ref": "reference photodiode (before the analyzer)",
    "Marker": "trigger / sequence marker",
    "Other": "recorded, not analysed",
}
CHANNEL_IDS = ("1", "2", "3", "4")

# The bench as wired on 5-12 Oct 2026, the polarimeter's live config.
DEFAULT_CHANNELS = {
    "1": {"role": "PD", "name": "Analyzer PD"},
    "2": {"role": "CmdX1", "name": "Trek input X1"},
    "3": {"role": "MonX1", "name": "Trek monitor X1"},
    "4": {"role": "MonX2", "name": "Trek monitor X2"},
}

# Which AWG output drives which Trek -- fixed cabling, not a scope matter.
AWG_OF = {"X1": 1, "X2": 2}
EOM_OF = {"X1": "EO1", "X2": "EO2"}


def _appdata():
    return (os.environ.get("APPDATA")
            or (os.path.expanduser("~/Library/Application Support")
                if sys.platform == "darwin" else os.path.expanduser("~")))


# The panel's config (ilc_gui.CONFIG_PATH is this) and the polarimeter's.
PANEL_CONFIG_PATH = os.path.join(_appdata(), "EOM-ILC-GUI", "config.json")
POLARIMETER_CONFIG_PATH = os.path.join(_appdata(), "RampPolarimeter", "config.json")


def normalise(channels) -> dict:
    """A clean {"1".."4": {"role", "name"}} from whatever a config holds:
    missing channels are "off", unknown roles are refused (a typo in a
    config must not silently become "off")."""
    out = {}
    src = channels if isinstance(channels, dict) else {}
    for ch in CHANNEL_IDS:
        c = src.get(ch) or src.get(int(ch)) or {}
        role = str(c.get("role", "off") or "off")
        if role not in ROLES:
            raise ValueError(f"CH{ch}: unknown scope channel role {role!r} "
                             f"(one of {', '.join(ROLES)})")
        out[ch] = {"role": role, "name": str(c.get("name", "") or "")}
    return out


def channels_of(cfg) -> dict:
    """The channel map a panel config carries, defaults when it has none.
    `cfg` may be the config dict, a channel map itself, or None."""
    if cfg is None:
        return normalise(DEFAULT_CHANNELS)
    src = cfg.get("scope_channels") if "scope_channels" in cfg else (
        cfg if set(cfg) <= set(CHANNEL_IDS) and cfg else None)
    if not src:
        return normalise(DEFAULT_CHANNELS)
    return normalise(src)


def roles(cfg) -> dict:
    """{channel number: (role, name)} for every recorded channel, in order
    -- the polarimeter's channel_roles()."""
    out = {}
    for ch, c in channels_of(cfg).items():
        if c["role"] != "off":
            out[int(ch)] = (c["role"], c["name"] or ROLE_NAMES[c["role"]])
    return out


def channel_for(cfg, role) -> int | None:
    """The scope channel carrying `role`, or None. A role on two channels is
    an error: the bench cannot guess which one is meant."""
    hits = [ch for ch, (r, _n) in roles(cfg).items() if r == role]
    if len(hits) > 1:
        raise ValueError(f"{role} is on CH{hits[0]} and CH{hits[1]}; a role may be on one channel")
    return hits[0] if hits else None


def wiring(cfg, key) -> dict:
    """The batch's / bench's wiring for X1 or X2 from the roles:
    awg_ch (fixed cabling), mon_ch (MonXn: required), drive_ch (CmdXn, or
    None = no readback, alignment not verifiable)."""
    if key not in AWG_OF:
        raise ValueError(f"wiring is for X1 or X2, not {key!r}")
    n = key[-1]
    mon = channel_for(cfg, f"Mon{key}")
    if mon is None:
        raise ValueError(f"no scope channel carries Mon{key}: the loop has nothing to measure "
                         f"(Scope channels on the Measure tab)")
    return dict(channel=EOM_OF[key], awg_ch=AWG_OF[key], drive_ch=channel_for(cfg, f"Cmd{key}"),
                mon_ch=mon, key=key, n=n)


def describe(cfg) -> str:
    """One line per channel plus what the chains read, for logs and the panel."""
    chans = channels_of(cfg)
    parts = []
    for ch in CHANNEL_IDS:
        c = chans[ch]
        if c["role"] == "off":
            parts.append(f"CH{ch} off")
        else:
            parts.append(f"CH{ch} {c['role']}" + (f" ({c['name']})" if c["name"] else ""))
    lines = ["  ".join(parts)]
    for key in ("X1", "X2"):
        try:
            w = wiring(cfg, key)
        except ValueError as e:
            lines.append(f"{key}: {e}")
            continue
        rb = f"readback CH{w['drive_ch']}" if w["drive_ch"] else "no readback (CmdX unassigned)"
        lines.append(f"{key}: AWG CH{w['awg_ch']} -> monitor CH{w['mon_ch']}, {rb}")
    pd = channel_for(cfg, "PD")
    lines.append(f"PD: CH{pd}" if pd else "PD: none")
    return "\n".join(lines)


def record(cfg) -> str:
    """The map as the state file stores it (a JSON string, numpy-safe)."""
    return json.dumps(channels_of(cfg), sort_keys=True)


def from_record(text) -> dict | None:
    """The map a state file stored, or None when it has none / is garbled."""
    try:
        return normalise(json.loads(str(text)))
    except (ValueError, TypeError):
        return None


def differences(a, b) -> list:
    """Channels whose role differs between two maps: ["CH2: CmdX1 -> off"]."""
    a, b = channels_of(a), channels_of(b)
    return [f"CH{ch}: {a[ch]['role']} -> {b[ch]['role']}" for ch in CHANNEL_IDS
            if a[ch]["role"] != b[ch]["role"]]


def load_panel_config(path=None) -> dict:
    """The panel's config.json (the batch CLI reads the channel map from it),
    {} when there is none."""
    try:
        with open(path or PANEL_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def read_polarimeter(path=None) -> dict | None:
    """The polarimeter's channel map, or None when its config is not there
    (not installed, or never run on this account)."""
    try:
        with open(path or POLARIMETER_CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        return normalise(cfg["channels"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
