"""One look at the rack: open each device, ask what it is and how it stands,
close it again. The panel's Hardware tab calls these.

The ILC drives only the scope and the generator. The analyzer mount (ELL14)
and the light gate (DS345) belong to the polarimeter, whose drivers and port
settings are read here so the same four devices can be checked from either
window; checking them never moves or programs anything (an identity query
and a position read). A device another program holds -- the polarimeter on
COM3, Scope Grab on the scope -- answers "in use" rather than "missing", and
the text says which.

Two things here do more than look, and only when asked: move_ell14 (the
Hardware tab's Move) turns the analyzer to an angle, and read_analyzer
(the batch, at every campaign start) reads its position once. Both open
the mount through open_ell14, the way check_ell14 does, and close it again.

Every check returns dict(ok, text, detail): one line for the row, more for
the log. Nothing here is imported by the panel at start-up except the
module itself; the instrument layers load on the first check.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SIBLINGS = os.path.dirname(HERE)


def polarimeter_config():
    """The polarimeter's config.json as a dict ({} when it is not there):
    ell_port / ell_address / ell_zero_deg, ds345_addr / ds345_path."""
    from eomilc import channels
    try:
        with open(channels.POLARIMETER_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def polarimeter_path(cfg=None):
    """Where rampol lives (the ILC config's 'polarimeter_path', else the
    sibling checkout), or None when neither exists."""
    cand = [(cfg or {}).get("polarimeter_path"), os.environ.get("RAMP_POLARIMETER"),
            os.path.join(SIBLINGS, "ramp-polarimeter")]
    for p in cand:
        if p and os.path.isdir(os.path.join(p, "rampol")):
            return p
    return None


def _busy_text(exc):
    """The row's one line for a device that did not open. The driver's own
    multi-line diagnosis (Scope Grab says whether Windows sees the scope at
    all, and what sits on its port) goes to the log in full: _failed()."""
    s = str(exc)
    low = s.lower()
    if "access" in low or "in use" in low or "permission" in low or "resource busy" in low \
            or "VI_ERROR_RSRC_BUSY" in s or "VI_ERROR_RSRC_LOCKED" in s:
        return f"in use by another program ({s[:80]})"
    first = s.strip().split("\n")[0].strip()
    return first[:160] if first else s[:160]


def _failed(exc, prefix=""):
    """dict(ok=False, text=<first line>, detail=<the rest>) for a device that
    did not open; the detail is the driver's whole message when it says more
    than one line (the Hardware tab logs it under the row)."""
    s = str(exc).strip()
    lines = [l for l in s.split("\n")]
    detail = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    return dict(ok=False, text=prefix + _busy_text(exc), detail=detail)


# ------------------------------------------------------------------ scope
def check_scope(scopemod, channels_desc=""):
    """Identity, acquisition mode, timebase, the four channels' V/div."""
    import ilc_bench
    scope = ilc_bench.make_scope(scopemod)
    try:
        idn = scope.connect()
    except Exception as exc:
        return _failed(exc)
    try:
        acq = str(scope.try_get(":ACQuire:TYPE") or "?").strip()
        rng = scope.try_get(":TIMebase:RANGe")
        scales = []
        for ch in (1, 2, 3, 4):
            sc = scope.try_get(f":CHANnel{ch}:SCALe")
            on = scope.try_get(f":CHANnel{ch}:DISPlay")
            # one bad reply (a partial answer after a timeout) marks that
            # channel, not the whole row (review G1-1)
            try:
                scales.append(f"CH{ch} {float(sc):g} V/div"
                              + ("" if str(on).strip() in ("1", "ON") else " off"))
            except (TypeError, ValueError):
                scales.append(f"CH{ch} ?")
        try:
            rng_txt = f"{float(rng)*1e3:.4g} ms"
        except (TypeError, ValueError):
            rng_txt = "?"
        text = f"{idn.split(',')[1].strip() if ',' in idn else idn[:24]}: {acq}, window {rng_txt}"
        detail = f"{idn}\n  " + ", ".join(scales) + (f"\n  roles: {channels_desc}" if channels_desc else "")
        return dict(ok=acq.upper().startswith("HRES"), text=text + ("" if acq.upper().startswith("HRES") else " (loop needs HRES)"),
                    detail=detail)
    except Exception as exc:
        return dict(ok=False, text=f"answered but a readback failed: {exc}", detail=str(idn))
    finally:
        try:
            scope.close()
        except Exception:
            pass


# -------------------------------------------------------------- generator
def check_awg(awgmod):
    """Identity, each channel's output state, FRQ and selected waveform."""
    import ilc_bench
    awg = ilc_bench.make_awg(awgmod)
    try:
        idn = awg.connect()
    except Exception as exc:
        return _failed(exc)
    try:
        parts, det = [], [str(idn)]
        states = 0                  # channels whose output state was read
        for ch in (1, 2):
            on = None
            try:
                on = bool(awg.is_on(ch))
            except Exception:
                pass
            frq = name = None
            try:
                # read_channel gives the raw replies: BSWV is
                # "C1:BSWV WVTP,ARB,FRQ,90.893HZ,AMP,20V,..." and ARWV
                # "C1:ARWV INDEX,50,NAME,S92PX1B_i15"; the simulated
                # generator answers with dicts
                rc = awg.read_channel(ch)
                b = rc.get("BSWV", rc) if isinstance(rc, dict) else {}
                if isinstance(b, dict):
                    frq = b.get("FRQ")
                elif isinstance(b, str):
                    m = re.search(r"FRQ,([-+0-9.eE]+)", b)
                    frq = m.group(1) if m else None
                a = rc.get("ARWV") if isinstance(rc, dict) else None
                if isinstance(a, str) and "NAME," in a:
                    name = a.split("NAME,")[-1].split(",")[0].strip()
            except Exception:
                pass
            if name is None:
                try:
                    name = str(awg.query(f"C{ch}:ARWV?")).strip()
                    if "NAME," in name:
                        name = name.split("NAME,")[-1].split(",")[0]
                except Exception:
                    pass
            states += on is not None
            txt = f"CH{ch} {'ON' if on else 'OFF' if on is not None else '?'}"
            if frq is not None:
                try:
                    txt += f" {float(frq):.4g} Hz"
                except (TypeError, ValueError):
                    txt += f" FRQ {frq}"
            if name:
                txt += f" {name}"
            parts.append(txt)
            det.append(f"  {txt}")
        text = (str(idn).split(",")[1].strip() if "," in str(idn) else str(idn)[:20]) + ": " + " | ".join(parts)
        if not states:
            # it answered *IDN? and nothing else: not a green row (review G1-2)
            return dict(ok=False, text=text + " (no channel answered)", detail="\n".join(det))
        return dict(ok=True, text=text, detail="\n".join(det))
    except Exception as exc:
        return dict(ok=False, text=f"answered but a readback failed: {exc}", detail=str(idn))
    finally:
        try:
            awg.close()
        except Exception:
            pass


# ---------------------------------------------------------------- ELL14
class MountError(RuntimeError):
    """The analyzer mount's driver is not there (no polarimeter checkout,
    rampol.ell14 does not import): the row's text, nothing was opened."""


def ell_port(pcfg):
    return (pcfg or {}).get("ell_port") or "COM3"


def open_ell14(cfg=None, pcfg=None):
    """The analyzer mount opened through the polarimeter's driver
    (rampol.ell14.ELL14 with the polarimeter config's port, address and
    zero), the way check_ell14 opens it. Raises MountError when the driver is
    not there, and the driver's own exception when the port does not open
    (the polarimeter window holds COM3: "Access is denied"). The caller
    closes it. The Hardware tab's Move and the batch's analyzer read go
    through here; the tests replace it with a stub."""
    pcfg = polarimeter_config() if pcfg is None else pcfg
    path = polarimeter_path(cfg)
    if path is None:
        raise MountError("polarimeter checkout not found (rampol/)")
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        from rampol.ell14 import ELL14
    except Exception as exc:
        raise MountError(f"rampol.ell14 did not import: {exc}")
    return ELL14(ell_port(pcfg), address=str(pcfg.get("ell_address", "0") or "0"),
                 zero_offset_deg=float(pcfg.get("ell_zero_deg", 0.0) or 0.0))


def wrap_deg(deg):
    """An analyzer angle on (-180, 180]: the mount reads [0, 360)."""
    d = (float(deg) + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def _close(dev):
    try:
        dev.close()
    except Exception:
        pass


def check_ell14(cfg=None, pcfg=None):
    """The analyzer mount through the polarimeter's driver: identity and
    position in the polarimeter's zero frame. In use while the polarimeter
    window is open (it holds COM3). The result carries the position as
    "deg" (polarimeter frame, -180..180) when it answered."""
    pcfg = polarimeter_config() if pcfg is None else pcfg
    port = ell_port(pcfg)
    try:
        dev = open_ell14(cfg, pcfg)
    except MountError as exc:
        return dict(ok=False, text=str(exc), detail="")
    except Exception as exc:
        return _failed(exc, f"{port}: ")
    try:
        info = dev.info()
        pos = dev.position()
        text = (f"ELL{info['type']} S/N {info['serial']} on {port}: at {pos:.2f} deg "
                f"(zero {float(pcfg.get('ell_zero_deg', 0.0) or 0.0):g})")
        return dict(ok=True, text=text, deg=wrap_deg(pos),
                    detail=f"firmware {info['firmware']}, {info['pulses_per_unit']} pulses/rev")
    except Exception as exc:
        return _failed(exc, f"{port}: ")
    finally:
        _close(dev)


def read_analyzer(cfg=None, pcfg=None):
    """The analyzer's position, read once and nothing else: opened, asked,
    closed -- never moved. dict(ok, deg, text): deg in the polarimeter's frame
    (-180..180], NaN when the mount did not answer (text says why: "in use"
    while the polarimeter window holds COM3). The batch reads it at every
    campaign start."""
    pcfg = polarimeter_config() if pcfg is None else pcfg
    port = ell_port(pcfg)
    try:
        dev = open_ell14(cfg, pcfg)
    except MountError as exc:
        return dict(ok=False, deg=float("nan"), text=str(exc))
    except Exception as exc:
        return dict(ok=False, deg=float("nan"), text=_failed(exc, f"{port}: ")["text"])
    try:
        return dict(ok=True, deg=wrap_deg(dev.position()), text=f"{port}")
    except Exception as exc:
        return dict(ok=False, deg=float("nan"), text=_failed(exc, f"{port}: ")["text"])
    finally:
        _close(dev)


def _approach(dev, deg, backoff=3.0, tol=0.05, log=print):
    """rampol.hw.Rotator.approach when the polarimeter's hw module imports
    (it does wherever the driver does), else the same recipe on the driver:
    to deg - backoff, then a positive relative move, so the mount always
    lands from below whatever side of its backlash it started on."""
    try:
        from rampol.hw import Rotator
    except Exception:
        Rotator = None
    if Rotator is not None:
        return Rotator(dev, log=log).approach(deg, backoff=backoff, tol=tol)
    dev.goto(deg - backoff, tol=max(tol, 0.2), settle=0.0)
    dev.move_by(backoff)
    time.sleep(0.1)
    return dev.position()


def move_ell14(deg, cfg=None, pcfg=None, log=print):
    """The analyzer to `deg` (polarimeter frame), approached from below as the
    polarimeter does, the position read back, the mount closed again.
    dict(ok, text, detail, deg, in_use): in_use True when the port did not open
    because another program holds it (the polarimeter window on COM3); the
    move is then refused with nothing touched."""
    try:
        asked = float(deg)
    except (TypeError, ValueError):
        asked = float("nan")
    if asked != asked or asked in (float("inf"), float("-inf")):
        return dict(ok=False, text=f"not an angle: {deg!r}", detail="", deg=float("nan"),
                    in_use=False)
    # on (-180, 180] before anything moves: a typed 3600 is 0, not 3597 on
    # the fallback path (review G1-3)
    deg = wrap_deg(asked)
    pcfg = polarimeter_config() if pcfg is None else pcfg
    port = ell_port(pcfg)
    try:
        dev = open_ell14(cfg, pcfg)
    except MountError as exc:
        return dict(ok=False, text=str(exc), detail="", deg=float("nan"), in_use=False)
    except Exception as exc:
        r = _failed(exc, f"{port}: ")
        r.update(deg=float("nan"), in_use="in use" in r["text"])
        return r
    try:
        _approach(dev, float(deg), log=log)
        got = wrap_deg(dev.position())
        miss = wrap_deg(got - float(deg))
        return dict(ok=True, text=f"moved to {got:+.2f} deg", deg=got, in_use=False,
                    detail=f"asked {asked:+.2f} deg"
                           + (f" (= {deg:+.2f})" if deg != asked else "")
                           + f", read back {got:+.2f} deg "
                           f"({miss:+.3f} deg) on {port}, approached from below")
    except Exception as exc:
        r = _failed(exc, f"{port}: move failed: ")
        r.update(deg=float("nan"), in_use=False)
        return r
    finally:
        _close(dev)


# ---------------------------------------------------------------- DS345
def check_ds345(cfg=None, pcfg=None):
    """The light gate through the DS345 panel's driver: identity and
    address. Nothing is programmed."""
    import ilc_bench
    pcfg = polarimeter_config() if pcfg is None else pcfg
    path = pcfg.get("ds345_path") or os.path.join(SIBLINGS, "DS345-AWG-GUI", "ds345_awg_gui.py")
    if not os.path.exists(path):
        return dict(ok=False, text=f"DS345 driver not found ({path})", detail="")
    try:
        mod = ilc_bench.load_module(path, "ds345_awg_gui")
    except Exception as exc:
        return dict(ok=False, text=f"DS345 driver did not load: {exc}", detail="")
    remembered = ""
    try:
        p = os.path.join(os.environ.get("APPDATA", ""), "DS345-AWG-GUI", "config.json")
        with open(p, encoding="utf-8") as fh:
            remembered = json.load(fh).get("address", "") or ""
    except (OSError, ValueError):
        pass
    dev = mod.Ds345()
    try:
        dev.connect(pcfg.get("ds345_addr") or None, remembered=remembered or None,
                    log=lambda s: None)
        text = f"{getattr(dev, 'idn', '?')[:40]} on {getattr(dev, 'addr', '?')}"
        return dict(ok=True, text=text, detail="")
    except Exception as exc:
        return _failed(exc)
    finally:
        try:
            dev.close()
        except Exception:
            pass


def stamp():
    return time.strftime("%H:%M:%S")
