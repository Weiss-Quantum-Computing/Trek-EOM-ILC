"""One look at the rack: open each device, ask what it is and how it stands,
close it again. The panel's Hardware tab calls these.

The ILC drives only the scope and the generator. The analyzer mount (ELL14)
and the light gate (DS345) belong to the polarimeter, whose drivers and port
settings are read here so the same four devices can be checked from either
window; checking them never moves or programs anything (an identity query
and a position read). A device another program holds -- the polarimeter on
COM3, Scope Grab on the scope -- answers "in use" rather than "missing", and
the text says which.

Every check returns dict(ok, text, detail): one line for the row, more for
the log. Nothing here is imported by the panel at start-up except the
module itself; the instrument layers load on the first check.
"""
from __future__ import annotations

import json
import os
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
    s = str(exc)
    low = s.lower()
    if "access" in low or "in use" in low or "permission" in low or "resource busy" in low \
            or "VI_ERROR_RSRC_BUSY" in s or "VI_ERROR_RSRC_LOCKED" in s:
        return f"in use by another program ({s[:80]})"
    return s[:160]


# ------------------------------------------------------------------ scope
def check_scope(scopemod, channels_desc=""):
    """Identity, acquisition mode, timebase, the four channels' V/div."""
    import ilc_bench
    scope = ilc_bench.make_scope(scopemod)
    try:
        idn = scope.connect()
    except Exception as exc:
        return dict(ok=False, text=_busy_text(exc), detail="")
    try:
        acq = str(scope.try_get(":ACQuire:TYPE") or "?").strip()
        rng = scope.try_get(":TIMebase:RANGe")
        scales = []
        for ch in (1, 2, 3, 4):
            sc = scope.try_get(f":CHANnel{ch}:SCALe")
            on = scope.try_get(f":CHANnel{ch}:DISPlay")
            scales.append(f"CH{ch} {float(sc):g} V/div" + ("" if str(on).strip() in ("1", "ON") else " off")
                          if sc is not None else f"CH{ch} ?")
        rng_txt = f"{float(rng)*1e3:.4g} ms" if rng is not None else "?"
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
        return dict(ok=False, text=_busy_text(exc), detail="")
    try:
        parts, det = [], [str(idn)]
        for ch in (1, 2):
            on = None
            try:
                on = bool(awg.is_on(ch))
            except Exception:
                pass
            frq = name = None
            try:
                rc = awg.read_channel(ch)
                b = rc.get("BSWV", rc) if isinstance(rc, dict) else {}
                frq = b.get("FRQ") if isinstance(b, dict) else None
            except Exception:
                pass
            try:
                name = str(awg.query(f"C{ch}:ARWV?")).strip()
                if "NAME," in name:
                    name = name.split("NAME,")[-1].split(",")[0]
            except Exception:
                pass
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
        return dict(ok=True, text=text, detail="\n".join(det))
    except Exception as exc:
        return dict(ok=False, text=f"answered but a readback failed: {exc}", detail=str(idn))
    finally:
        try:
            awg.close()
        except Exception:
            pass


# ---------------------------------------------------------------- ELL14
def check_ell14(cfg=None, pcfg=None):
    """The analyzer mount through the polarimeter's driver: identity and
    position in the polarimeter's zero frame. In use while the polarimeter
    window is open (it holds COM3)."""
    pcfg = polarimeter_config() if pcfg is None else pcfg
    path = polarimeter_path(cfg)
    if path is None:
        return dict(ok=False, text="polarimeter checkout not found (rampol/)", detail="")
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        from rampol.ell14 import ELL14
    except Exception as exc:
        return dict(ok=False, text=f"rampol.ell14 did not import: {exc}", detail="")
    port = pcfg.get("ell_port") or "COM3"
    try:
        dev = ELL14(port, address=str(pcfg.get("ell_address", "0") or "0"),
                    zero_offset_deg=float(pcfg.get("ell_zero_deg", 0.0) or 0.0))
    except Exception as exc:
        return dict(ok=False, text=f"{port}: {_busy_text(exc)}", detail="")
    try:
        info = dev.info()
        pos = dev.position()
        text = (f"ELL{info['type']} S/N {info['serial']} on {port}: at {pos:.2f} deg "
                f"(zero {float(pcfg.get('ell_zero_deg', 0.0) or 0.0):g})")
        return dict(ok=True, text=text,
                    detail=f"firmware {info['firmware']}, {info['pulses_per_unit']} pulses/rev")
    except Exception as exc:
        return dict(ok=False, text=f"{port}: {_busy_text(exc)}", detail="")
    finally:
        try:
            dev.close()
        except Exception:
            pass


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
        return dict(ok=False, text=_busy_text(exc), detail="")
    finally:
        try:
            dev.close()
        except Exception:
            pass


def stamp():
    return time.strftime("%H:%M:%S")
