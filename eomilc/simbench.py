"""A simulated bench: a BK4063B and an MSO-X that play drives through a model chain.

For exercising the bench drivers (ilc_batch.py) on a machine with no
instruments. Nothing here is evidence about the real chain -- the "truth" is
the measured FRF with a deliberate gain and delay error on top, so an ILC loop
has something to learn -- but everything the drivers do to the instruments
(output switching, FRQ, uploads, selections, captures, scope settings) goes
through the same calls as on the bench and is recorded, so the ordering rules
can be checked.

    awg, scope, mod = simbench.make_bench({"X1": "run/frf_WIDE_X1.csv", ...})

`mod` stands in for the bk4063b module (parse_reply, MAX_ARB_NAME): assign it
to ilc_bench._AWGMOD as the real driver does.
"""
from __future__ import annotations

import numpy as np

from . import ilc

MAX_ARB_NAME = 11


def parse_reply(reply):
    """The simulated generator answers read_channel with dicts already."""
    return reply


class SimModule:
    MAX_ARB_NAME = MAX_ARB_NAME
    parse_reply = staticmethod(parse_reply)


class SimAWG:
    """Two channels, user memory, burst on an external trigger."""

    def __init__(self, full_scale=10.0):
        self.fs = full_scale
        self.ch = {c: dict(on=False, frq=100.0, amp=2 * full_scale, ofst=0.0,
                           wave=None, name="") for c in (1, 2)}
        self.memory = {}
        self.log = []                     # (event, channel, detail)

    # connection
    def connect(self):
        return "SIMULATED BK4063B"

    def close(self):
        pass

    # outputs
    def is_on(self, c):
        return self.ch[c]["on"]

    def set_output(self, c, on):
        self.ch[c]["on"] = bool(on)
        self.log.append(("output", c, bool(on)))

    # configuration
    def apply_channel(self, c, blocks, log=lambda s: None):
        bswv = blocks.get("BSWV", {})
        if "FRQ" in bswv:
            if self.ch[c]["on"]:
                self.log.append(("FRQ_UNDER_LIVE_OUTPUT", c, float(bswv["FRQ"])))
            self.ch[c]["frq"] = float(bswv["FRQ"])
            self.log.append(("frq", c, float(bswv["FRQ"])))
        return []

    def read_channel(self, c):
        s = self.ch[c]
        return {"BSWV": {"WVTP": "ARB", "FRQ": s["frq"], "AMP": s["amp"], "OFST": s["ofst"]},
                "SRATE": {"MODE": "DDS", "VALUE": None},
                "OUTP": {"STATE": "ON" if s["on"] else "OFF", "LOAD": "HZ"},
                "ARWV": {"NAME": s["name"]}}

    def list_waveforms(self, user_only=True):
        return list(self.memory)

    def upload_arb(self, c, name, samples, normalize=True, **kw):
        x = np.clip(np.asarray(samples, float), -1, 1)
        self.memory[name] = x
        self.ch[c]["wave"], self.ch[c]["name"] = x, name
        self.log.append(("upload", c, name))
        return len(x)

    def query(self, cmd):
        if cmd.endswith(":ARWV?"):
            c = int(cmd[1])
            return f"C{c}:ARWV NAME,{self.ch[c]['name']}"
        return ""

    # what is playing, in AWG volts, and the period it plays over
    def playing(self, c):
        s = self.ch[c]
        if not s["on"] or s["wave"] is None:
            return None, None
        return s["wave"] * self.fs, 1.0 / s["frq"]


class SimChain:
    """AWG volts -> monitor volts through the measured FRF, times (1 + gain_err),
    delayed by `delay` seconds, plus per-shot noise."""

    def __init__(self, frf_path, gain_err=0.01, delay=2e-6, noise=3.5e-3, seed=0):
        top = ilc.frf_band(frf_path)[1] if hasattr(ilc, "frf_band") else 80e3
        self.frf = ilc.FRF(frf_path, f_use=0.8 * top, f_max=top)
        self.gain_err, self.delay, self.noise = gain_err, delay, noise
        self.rng = np.random.default_rng(seed)

    def respond(self, u, dt):
        y = self.frf.forward(u, dt) * (1 + self.gain_err)
        n = int(round(self.delay / dt))
        if n > 0:
            y = np.r_[np.full(n, y[0]), y[:-n]]
        return y


class SimScope:
    """Four channels: CH1/CH2 read the AWG outputs, CH3/CH4 the chain monitors."""

    def __init__(self, awg, chains, wiring=None):
        self.awg, self.chains = awg, chains          # chains: {awg_ch: SimChain}
        self.wiring = wiring or {1: ("drive", 1), 2: ("drive", 2), 3: ("mon", 1), 4: ("mon", 2)}
        self.settings = {":ACQuire:TYPE": "HRES", ":TIMebase:RANGe": "0.02",
                         ":TIMebase:POSition": "0.01"}
        for c in (1, 2, 3, 4):
            self.settings[f":CHANnel{c}:SCALe"] = "1.0"
            self.settings[f":CHANnel{c}:OFFSet"] = "0.0"
            self.settings[f":CHANnel{c}:COUPling"] = "DC"
        self.frozen = None
        self.shots = 0

    def connect(self):
        return "SIMULATED MSO-X 2014A"

    def close(self):
        pass

    def get(self, scpi):
        return self.settings.get(scpi, "")

    def try_get(self, scpi, timeout_ms=2000):
        return self.settings.get(scpi)

    def put(self, scpi, value):
        self.settings[scpi] = str(value)

    def errors(self):
        return []

    def run(self):
        self.frozen = None

    def is_running(self):
        return self.frozen is None

    def single(self, wait_s=10.0):
        """One trigger: every channel's record, frozen until run()."""
        rng = float(self.settings[":TIMebase:RANGe"])
        out = {}
        for c, (kind, awg_ch) in self.wiring.items():
            u, period = self.awg.playing(awg_ch)
            if u is None:
                out[c] = (rng, None, None)
                continue
            dt = period / len(u)
            if kind == "drive":
                out[c] = (rng, dt, u)
            else:
                ch = self.chains[awg_ch]
                y = ch.respond(u, dt)
                out[c] = (rng, dt, y + ch.rng.normal(0, ch.noise, len(y)))
        self.frozen = out
        self.shots += 1
        return True

    def waveform(self, channel, points_mode="RAW", points=None):
        rng, dt, v = self.frozen[channel]
        n = int(points or 20000)
        t = np.linspace(0, rng, n, endpoint=False)
        if v is None:
            return t, np.zeros(n)
        tv = np.arange(len(v)) * dt
        # the burst holds the last sample after the record, the first before it
        return t, np.interp(t, tv, v, left=v[0], right=v[-1])


def make_bench(frf_paths, full_scale=10.0, gain_err=(0.01, -0.015), delay=(2e-6, 3e-6)):
    """frf_paths: {awg_ch: path}. Returns (awg, scope, module)."""
    awg = SimAWG(full_scale)
    chains = {c: SimChain(p, gain_err=gain_err[i % 2], delay=delay[i % 2], seed=c)
              for i, (c, p) in enumerate(sorted(frf_paths.items()))}
    return awg, SimScope(awg, chains), SimModule
