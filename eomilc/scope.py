"""Reader for the Agilent MSO-X 2014A CSV + TXT pairs produced by the capture script."""
from __future__ import annotations
import os, re
from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass
class Trace:
    t: np.ndarray                 # seconds, trigger at t = 0
    data: dict                    # {channel label: np.ndarray of volts}
    header: dict                  # raw key/value pairs from the .txt sidecar

    @property
    def dt(self) -> float:
        return float(np.median(np.diff(self.t)))

    def __getitem__(self, key):
        if key in self.data:
            return self.data[key]
        for k in self.data:                       # allow "CH3" or a substring
            if k.upper().startswith(key.upper()) or key.upper() in k.upper():
                return self.data[k]
        raise KeyError(f"{key!r} not in {list(self.data)}")

    def lsb(self, key) -> float:
        """Scope quantisation step, inferred from the data itself."""
        u = np.unique(self[key])
        d = np.diff(u)
        d = d[d > 0]
        return float(np.median(d)) if d.size else 0.0

    def volts_per_div(self, ch: str) -> float | None:
        v = self.header.get(f"{ch} V/div")
        return float(v) if v is not None else None


def _read_header(txt_path: str) -> dict:
    h = {}
    if not os.path.exists(txt_path):
        return h
    with open(txt_path, "r", errors="replace") as f:
        for line in f:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            h[k.strip()] = v.strip()
    return h


def load(csv_path: str) -> Trace:
    """Load a capture. The .txt sidecar is picked up automatically if present."""
    df = pd.read_csv(csv_path)
    t = df.iloc[:, 0].to_numpy(dtype=float)
    data = {c: df[c].to_numpy(dtype=float) for c in df.columns[1:]}
    return Trace(t=t, data=data, header=_read_header(os.path.splitext(csv_path)[0] + ".txt"))


def resample(t_src: np.ndarray, y: np.ndarray, t_dst: np.ndarray,
             t_offset: float = 0.0, antialias: bool = True) -> np.ndarray:
    """Put a scope trace onto the waveform grid.

    t_offset shifts the SOURCE time base: use it to remove the fixed
    trigger-to-waveform-start delay.  Keep it constant between ILC iterations,
    otherwise the loop chases its own alignment.
    """
    dt_src = float(np.median(np.diff(t_src)))
    dt_dst = float(np.median(np.diff(t_dst)))
    if antialias and dt_dst > 2 * dt_src:
        n = int(round(dt_dst / dt_src))
        if n > 1:                                  # boxcar to the target rate
            k = np.ones(n) / n
            y = np.convolve(y, k, mode="same")
    return np.interp(t_dst, t_src - t_offset, y)


def find_trigger_offset(t: np.ndarray, y: np.ndarray, frac: float = 0.5) -> float:
    """Time at which y first crosses `frac` of its settled span.

    Use ONCE to calibrate the fixed offset, then hard-code it.  Do not re-fit
    per iteration.
    """
    base = y[t < t[0] + 0.2 * (0 - t[0])].mean() if t[0] < 0 else y[:100].mean()
    span = np.percentile(y, 99.5) - base
    i = int(np.argmax(y - base >= frac * span))
    return float(t[i])
