# Running the loop from the panel

Step-by-step instructions for `ilc_gui.py`. The GUI drives the same loop as
`run_ilc.py` / `ilc_bench.py` on the same state files, so anything started
here can be continued from the command line and vice versa.
[WORKFLOW.md](WORKFLOW.md) stays the reference for the bench measurement
physics (HRES vs AVER, the word lattice, trigger alignment); this file is
the click-by-click procedure.

## 0. Launch

Double-click `ilc_gui.bat`, or run:

```powershell
C:\ProgramData\anaconda3\pythonw.exe ilc_gui.py
```

Anaconda's interpreter is required — it is the only one on this machine
with scipy, pandas and pyvisa together. For **bench mode**, close the AWG
GUI and Scope Grab first: both hold their VISA sessions open, and the
instruments only accept one.

## 1. The window at a glance

| panel | what it does |
|---|---|
| **Session** | load a state file, or build one: target + channel + name + first-shot gain → Init |
| **Inverse model** | what the update divides the error by — the model ladder, its parameters, γ and `f_cut`, and the FRF band |
| **Capture post-processing** | how a measurement becomes an error: `t-offset` and baseline handling, shared by Step and Bench. Nothing here touches the first shot. |
| **Step from captured files** | one ILC iteration from scope CSVs you captured yourself |
| **Bench loop** | the hands-off cycle: upload → capture → update |
| **Log** | everything the loop reports, timestamped |
| right side | five plot tabs, refreshed at every step — see §7 |

## 2. Starting from scratch (any system, no prior knowledge)

Nothing carries over from the Trek chains unless you load it on purpose.

1. **Channel** → `GEN` (unity scaling, no calibration tables, no auto-FRF).
   For a Trek channel use `EO1`/`EO2` instead and skip to §3 or use
   *From calibration* in step 4.
2. **Target**: press **Build…** for a cosine-edged ramp or half-sine
   (peak in output units, segment lengths in ms), or browse to any
   `time_us,voltage_V` CSV. On GEN the values are read in the same units
   the scope measures. The moment a target is chosen it plots itself in
   the Waveforms tab (or press **Plot**): the file contents on top, and —
   once a gain is typed — the AWG output the flat first shot would
   produce, drawn against the ±full-scale rails (AMP = 2× full scale,
   OFST 0) with the DAC-range percentage and a clipping warning.
   **Nothing is sent by the preview**; validate the shape here first.
3. **Name stem**: ≤ 7 characters (the `_iNN` suffix brings it to the
   generator's 11-character cap — past that the 4063B's front panel wedges
   until a power cycle).
4. Type a **first-shot gain** guess (output/drive ratio) in the Session
   panel, and set **Model** → *gain only (0th order)*. The first-shot gain
   and the model's gain are **separate knobs**: the first fixes what
   iteration 0 plays, the second belongs to the error correction — tuning
   or refitting the model later never rescales the first shot. Either box
   alone is enough to start (each falls back to the other, logged). Guess
   the gain **high** — the loop converges for γ·g_true/g_model < 2, so a
   high guess just makes the first correction small.
5. Press **Init**. The first shot is a **flat conversion** — the target
   divided by the first-shot gain, no pre-distortion — so the first
   measurement shows the chain's raw response directly. The state file
   (`run\drive_<stem>.state.npz`) now exists; the campaign is resumable
   from here on.
6. Run one iteration (§4 or §5).
7. Press **Fit from measurement** — the typed guess is replaced by the
   identified gain, fitted from the drive/response pair that actually
   played. Step up the model ladder (§6) as the residual demands.

## 3. Resuming an existing campaign

1. **State** → browse to `run\drive_<stem>.state.npz` → **Load state**.
   Everything — target, current drive, plant, γ, f_cut, t-offset, error
   history — comes from the file. The last two measurements beside it are
   recalled into the plots, so you see where the campaign stands.
2. Channel defaults (AWG/scope channels, monitor column, the wide FRF with
   its 50/75 kHz taper) fill themselves from the state's channel.
3. On the Trek bench, budget 2–3 warm-up iterations at every session
   start: the chain drifts on hour scales (up to ~32 V on a converged
   drive), shape not gain.

## 4. A manual iteration (you play and capture by hand)

1. **Upload the drive**: in the AWG GUI, load
   `<stem>_iNN.csv` from its own Waveforms library (the GUI writes a copy
   of every drive there) — it is single-column, already normalised, so
   upload with **Normalise OFF**. Channel setup per WORKFLOW.md:
   ARB, DDS clock, AMP 20 Vpp, OFST 0, load HZ, burst NCYC 1 EXT.
2. **Capture**: scope in **HRES** (never AVER), full waveform window.
   In Scope Grab run a **Sequence** of 64 shots with a prefix nothing else
   uses (e.g. `ilc_i07`). Zoomed inspection grabs must get prefixes the
   ILC glob can never match.
3. **Captures** field: browse to any one file of the sequence — the run
   index is replaced with `*` automatically. **Monitor col**: CH3 for
   EO1, CH4 for EO2 (auto-set with the channel).
4. Press **Step**. Read the list of averaged files in the log — the glob
   averages *everything* it matches, and a stray zoomed capture once
   manufactured 172 V of fake error (a short capture is refused outright).
5. The plots refresh; the new drive lands in `run\` and the AWG library,
   and the state is saved. Repeat from 1 with the new `_iNN` file.

The **Capture post-processing** panel applies here and in bench mode:
`t-offset` is the fixed trigger-to-waveform delay (measured 0 on this
bench — change it only if the trigger wiring changed), and `zero baseline`
must stay off for any waveform already moving at the start of the record
(MKJ is). In the Step panel, `force` overrides a failed limit check —
don't, until you have read why it failed — and `refit plant` re-identifies
the parametric model from each iteration's own data as it steps.

## 5. The hands-off bench loop

1. Both other GUIs closed; scope in HRES on the full window; outputs on;
   burst firing (the external trigger running).
2. Check **AWG ch / scope ch** (auto-set per channel), **iterations**, and
   **repeats** (64 = the campaign standard; 16 is a usable quick check).
3. Press **Run bench loop**. Before touching the generator it verifies the
   channel is set up the way the drive file assumes (AMP 20 Vpp, OFST ~0,
   DDS, HRES, enough repeats) and **refuses** on any mismatch — fix the
   setup rather than ticking *skip setup checks*. On the first iteration
   it also cross-checks the trigger alignment against the drive it just
   uploaded and refuses if `t-offset` looks stale.
4. Each iteration: upload → 64 HRES singles averaged in software (progress
   bar; ~25 s at the 20 Hz trigger) → metrics → plots → update → state
   saved. Every measurement is kept as `run\meas_<stem>_iNN.npy`.
5. **Stop** finishes cleanly: a stop mid-capture discards only that
   iteration; the state on disk is whatever was last completed.

## 6. Stepping through the model ladder (the demo)

The **Model** combobox selects what the update divides the error by:

| rung | parameters | band knob |
|---|---|---|
| gain only (0th) | gain | `f_cut` |
| one pole (1st) | gain, τ | `f_cut` |
| second order | gain, fₙ, ζ | `f_cut` |
| measured FRF | the file | `full strength to` / `taper to zero at` |

- Parameters come from typing, **From calibration** (amplitude-dependent
  tables; refuses on GEN), or **Fit from measurement** (identifies the
  selected form from the last played drive/response pair and overlays the
  fitted model's prediction on the Waveforms tab).
- Switching channel **clears** the parameter boxes — numbers never follow
  you from one system to another.
- Each iteration's history entry is tagged with the model that produced
  it: the Convergence tab draws a divider and label at every model change,
  and the Error tab labels traces by model era.
- **Show FRF** plots the measured response with the current parametric
  model overlaid — the model-vs-chain comparison of REPORT.md §5, live.
- The frequency-ranged FRF cases are one file stepped through different
  tapers (e.g. 10/15 → 24/36 → 50/75 kHz). To measure an FRF in the first
  place: `tools\sysid_make.py` → play + 64-shot sequence →
  `tools\sysid_fit.py` → browse to `run\frf_<name>.csv`.

## 7. Reading the plots

| tab | what to look for |
|---|---|
| **Waveforms** | target vs measured output (top); the drive with its first/last-sample idle markers against the ±100 mV cap (bottom). After Init: the model-predicted output instead of a measurement. |
| **Error** | target − measured, current iteration over the previous one ghosted. Peak/rms in the title. The stuck peak at t = 0 is the burst-entry transient — the loop cannot fix the idle level (WORKFLOW.md). |
| **Error spectrum** | where the residual lives. The update only acts left of the drawn band edge (f_cut line, or the shaded FRF taper). Error growing *right* of the edge while the in-band falls is the model diverging — tighten the band or climb the ladder. |
| **Convergence** | peak and rms error vs iteration, log scale, with model-change annotations. Flat-lining means the current inverse has given what it can. |
| **FRF** | measured magnitude/phase/coherence, dropped tones flagged, taper band shaded, current parametric model overlaid. |

## 8. Files every step writes

| file | where | what |
|---|---|---|
| `drive_<stem>.state.npz` | `run\` | the campaign — rewritten every step |
| `drive_<stem>_iNN.csv` | `run\` | the iteration's drive, `time_us,voltage_V` |
| `<stem>_iNN.csv` | the AWG GUI's `Waveforms\` | upload-ready copy (±1, Normalise OFF) |
| `meas_<stem>_iNN.npy` | `run\` | bench mode's averaged measurement |

`run\README.md` has the full taxonomy and where finished campaigns get
filed.

## 9. Things that bite

- **Name stem ≤ 7 chars.** Past the 11-char stored name the generator
  wedges; the panel enforces it, don't fight it.
- **HRES, never AVER.** `:SINGle` takes one hit of an average; bench mode
  refuses AVER outright. The 64 software repeats are the averaging.
- **Both other GUIs closed** before bench mode — they hold the VISA
  sessions.
- **t-offset is measured once (0 on this bench) and left alone.**
  Re-fitting it per iteration makes the loop chase its own alignment.
- **Unique capture prefixes.** The step averages everything the glob
  matches.
- **Init refuses to overwrite an existing state** — it asks first. A yes
  destroys that campaign's history.
- **The first shot is deliberately uncorrected.** Expect the raw tracking
  error of the chain (~2.4 % on the Trek ramps) on iteration 0 — that is
  the point: it is the baseline every later iteration is judged against.
