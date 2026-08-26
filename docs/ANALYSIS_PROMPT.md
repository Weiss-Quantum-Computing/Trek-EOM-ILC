# Prompt for an offline data-analysis session

Paste everything below the rule into a fresh Claude Code session (bench PC
or MacBook) to analyse campaign data without rebuilding what the repo
already has. Fill in the two ALL-CAPS blanks at the bottom.

---

Analyze EOM-ILC campaign data in the repo at
`C:\Users\mzd416\Desktop\Python Projects\EOM-ILC`
(on the MacBook: wherever the clone lives; copy the `run\` folder from the
bench PC first — campaign data is not in git). Treat `run\` as READ-ONLY;
write any outputs to a scratch folder. On the bench PC run Python as
`C:\ProgramData\anaconda3\python.exe`; on macOS any Python with
numpy/scipy/pandas/matplotlib works. Do NOT rebuild loaders, spectra, or
plotting helpers — the repo has them, use them.

## Data layout (everything in `run\`)

- `drive_<stem>.state.npz` — one campaign. Keys: `t`, `target` (MONITOR
  volts), `u` (current AWG drive, V), `dt`, `channel`, plant params
  (`gain, tau, tau2, fn, zeta, offset`), `full_scale`, `gamma`, `f_cut`,
  `iteration`, `t_offset`, `history` (list of per-iteration metrics dicts).
  `history[i]` = metrics of measurement i. Its optional `"model"` tag names
  the model that CONSUMED measurement i to build drive i+1 — so the model
  that BUILT drive i is `history[i-1]`'s tag, and drive 0 is Init's flat
  conversion `target/gain`, no model involved.
- `meas_<stem>_iNN.npy` — measurement of iteration NN (monitor V, on the
  state's `t` grid, already the average of 64 HRES scope repeats).
  `meas_<stem>_iNN_rMM.npy` — hold-mode re-measurement MM of the SAME
  drive (thermal-drift studies); the file mtime IS the measurement time.
- `drive_<stem>_iNN.csv` — the AWG drive that PLAYED measurement NN
  (`time_us,voltage_V`, `#` comments).
- `frf_WIDE_X<n>.csv` — measured FRF: `f_Hz, H_mag` (mon V per AWG V),
  `H_phase_deg, coherence`. Drop rows with coherence < 0.9.

## Loaders and helpers (import these, do not re-parse files)

```python
import sys; sys.path.insert(0, r"<repo path>")
import numpy as np
import run_ilc                      # no Tk anywhere in this import
st   = run_ilc.load_state(r"run\drive_MKJX1.state.npz")
loop = run_ilc.build_loop(st)       # loop.metrics(y), loop.target, loop.dt
y    = np.load(r"run\meas_MKJX1_i08.npy")

import ilc_gui                      # headless-safe: Tk only starts at App()
s = ilc_gui.load_session(path)      # Session: .t .u .iteration .loop
ilc_gui.recall_snapshots(s)         # fills s.snapshots from disk:
                                    # [{it, y, m, u?, run?, t_wall}]
f, a = ilc_gui.avg_spectrum(e, dt, k)   # amplitude spectrum; k=1 raw FFT,
                                    # k>1 Welch (tone amplitudes invariant,
                                    # noise floor scales with bin width --
                                    # only compare curves at equal k)
from eomilc import plant as plantmod    # plantmod.identify(u, y, dt, model=...)
```

## Units and measured traps (do NOT rediscover these)

- `target` and `meas_*` are MONITOR volts. Multiply by
  `loop.channel.mon_scale` for output/HV volts (unity on GEN, ~x1000 on
  the Treks). Metrics `*_hv` keys are already in output units, `*_mon`
  are not; `peak_pct`/`rms_pct` are % of the target's span.
- The scope is 8-bit. Quantization and V/div changes fake distortion
  terms that are not there — never compare captures taken at different
  V/div as if calibrated, and treat small high-order harmonics with
  suspicion.
- The 2.4 kHz "resonance" was a large-signal artifact; FRF probes found
  none. The ~12% X1-vs-X2 Trek gain mismatch is real.
- A zoomed capture extrapolated flat once manufactured 172 V of fake
  error: anything reading raw scope CSVs must go through
  `ilc_gui.read_captures` (span guard included), not a hand-rolled parser.
- A raw periodogram's per-bin variance is ~100% and does not average
  down with record length — use `avg_spectrum(..., k>1)` before calling
  something "noise". And spectral RESOLUTION is set by record duration
  alone (~94 Hz for the 10.6 ms MKJ record): no resampling or
  interpolation adds information below 1/T, and Welch RAISES the lowest
  resolvable frequency by ~k.
- The stored `meas_*.npy` are boxcar-decimated to the waveform grid
  (Nyquist 250 kHz at dt 2 µs). Content above that — and the top octave
  without the boxcar's sinc droop — needs the RAW Scope Grab CSVs,
  processed by interpolating the target onto the scope's time base (the
  GUI's "Spectrum from captures" button does exactly this; do the same
  in scripts rather than decimating first).

## What already exists (do not re-plot it)

The GUI (`ilc_gui.py`, manual `WORKFLOW_GUI.md`) already draws: waveforms,
drive corrections + their spectrum, drive updates u_k − u_(k−1), error +
error spectrum (both spectra with Welch averaging), convergence,
FRF viewer, cross-campaign overlays (Compare box), and an iteration table
with CSV export. A session like this one is for analysis BEYOND that:
fits, statistics, hypothesis tests, cross-campaign summaries, publication
figures.

## Task

Stems of interest: STEMS_HERE
Question to answer: QUESTION_HERE
