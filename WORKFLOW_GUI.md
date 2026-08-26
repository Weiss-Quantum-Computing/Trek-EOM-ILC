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
| **Log** | everything the loop reports. A timestamped copy appends to `run\ilc_gui.log` |
| right side | seven plot tabs, refreshed at every step, with an iteration selector above them — see §7 |

Every individual field is documented in §10.

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

Launching the panel **restores the last session automatically**: the
remembered state reloads with every stored measurement sharing its stem,
so closing the program never costs the plots — reopen and continue
looking. To switch campaigns, or if nothing was remembered:

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

1. **Upload the drive**: press **Upload `<stem>_iNN` to AWG** (bench
   panel — the button names the session's current drive). It uploads at
   the fixed ±full-scale mapping and selects the waveform; if a waveform
   of that name is already stored it asks before overwriting (the
   generator cannot read a waveform back out, so the local copies in the
   AWG GUI's Waveforms library are the only record of the old samples).
   Alternative: the AWG GUI, loading `<stem>_iNN.csv` from its Waveforms
   library with **Normalise OFF**. Channel setup either way per Auto-set
   or WORKFLOW.md: ARB, DDS clock, AMP 20 Vpp, OFST 0, load HZ, burst
   NCYC 1 EXT.
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

1. Both other GUIs closed; burst firing (the external trigger running).
2. Check **AWG ch / scope ch** (auto-set per channel), **iterations**, and
   **repeats** (64 = the campaign standard; 16 is a usable quick check).
3. **Auto-set instruments** configures both from what the session already
   knows: the AWG gets the record's period as its arb frequency (DDS),
   AMP = 2× full scale / OFST 0, load HZ, the NCYC-1 EXT burst, **and the
   session's current waveform (`<stem>_iNN`) selected — if the generator
   already holds it** (selection is all auto-set does; uploading stays the
   bench loop's or the AWG GUI's job, and the log says which applied); the
   scope gets a window 1.3× the period with the waveform starting at the
   left edge (position = half the period), HRES acquisition, and verticals
   sized from the drive and target spans. It **refuses if the channel's
   output is ON** (changing FRQ/AMP under a live output moves real
   voltage), and never touches an output switch or the trigger — confirm
   the shot still fires. The output can stay OFF: the bench loop offers to
   switch it on itself (next step).
4. Press **Run bench loop**. Before touching the generator it verifies the
   channel is set up the way the drive file assumes (AMP 20 Vpp, OFST ~0,
   DDS, HRES, enough repeats) and **refuses** on any mismatch — fix the
   setup rather than ticking *skip setup checks*. If the channel's output
   is OFF it asks — a yes switches it ON for the run (and OFF again at the
   end); a no cancels with nothing changed. On the first iteration it also
   cross-checks the trigger alignment against the drive it just uploaded
   and refuses if `t-offset` looks stale.
5. Each iteration: upload → 64 HRES singles averaged in software (progress
   bar; ~25 s at the 20 Hz trigger) → metrics → plots → update → state
   saved. Every measurement is kept as `run\meas_<stem>_iNN.npy`.
6. **Stop** finishes cleanly: a stop mid-capture discards only that
   iteration; the state on disk is whatever was last completed. When any
   run that actually played something ends — finished, stopped, or died —
   the driven channel's **output switches OFF** automatically; a run
   refused at the setup checks leaves the bench exactly as it found it.
7. **Hold** re-measures the *current* drive `runs` times with `gap s`
   between measurements, **without ever updating** — for thermalisation
   studies: how does one fixed drive's error evolve over minutes? Each
   measurement is a **run** (`iter k r1, r2, …`, saved as
   `meas_<stem>_iNN_rMM.npy`), deliberately distinct from the loop's
   iterations and from the 64 *repeats* averaged inside every single
   measurement. The state and iteration counter are untouched; repeated
   holds keep counting runs up. Setup checks, the output-ON confirmation
   and the output-OFF-at-end policy all apply as in the bench loop.

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

The **Iterations shown** box above the tabs picks which stored iterations
the Drive corrections, Drive updates, Error and Error spectrum tabs
overlay: blank shows
the last two, `all` shows everything, `2-5` a range, `0,3,6` a list
(Enter or **Redraw** applies it). Iterations are coloured dark-to-light by
age, newest drawn heaviest. Every measurement the bench loop takes — and
every one recalled from `meas_*.npy` beside the state at load — is
available.

Hold **runs** draw dashed on the Error and Error spectrum tabs, labelled
`iter k rN`, and appear as open circles on the Convergence tab; the
**runs** checkbox hides them wholesale, and they are excluded from the
drive-side tabs by construction (same drive → identical correction, zero
update). The **Δt labels** checkbox appends wall-clock offsets to the
legend — a run against its iteration's base measurement, a base iteration
against the previous one — for reading thermalisation timescales straight
off the plot; timestamps come from the measurement moment (file mtime for
recalled ones), so they survive restarts. Leave it off when the clutter
is not earning its place.

Traces carry small dots marking **real samples** — every dot is actual
data, and the *dot every Nth sample* box sets the density: blank = auto
(~180 dots per trace, keeps a 5301-point record readable), any number =
that literal step, `1` = every sample drawn (the line always passes
through every sample regardless; zoom in with the toolbar to inspect a
region). About that toolbar: pan/zoom/home work
as expected, but the *Configure subplots* sliders are inert — the figures
use matplotlib's constrained layout, which recomputes the margins on
every draw and overrides anything the sliders set.

| tab | what to look for |
|---|---|
| **Waveforms** | target vs measured output (top); the drive with its first/last-sample idle markers against the ±100 mV cap (bottom). After Init: the model-predicted output instead of a measurement. |
| **Drive corrections** | the drive side of the Error tab: each selected iteration's AWG waveform minus the target's flat conversion (or minus the stored iteration-0 drive when it exists) — what the loop has learned to *add* at the input, in mV at the AWG. Growth here without matching error shrinkage is the loop learning noise or a wrong inverse. |
| **Drive updates** | `u_k − u_(k−1)`: the update each shown iteration actually applied, against the immediately prior iteration's drive (which may come from any stored iteration, selected or not). Shrinking updates are convergence seen from the input; updates that stop shrinking while the error flat-lines mean the loop is re-learning the same correction against noise or drift. |
| **Error** | target − measured for the selected iterations. Peak/rms of the newest in the title. The stuck peak at t = 0 is the burst-entry transient — the loop cannot fix the idle level (WORKFLOW.md). |
| **Error spectrum** | where the residual lives. The update only acts left of the drawn band edge (f_cut line, or the shaded FRF taper). Error growing *right* of the edge while the in-band falls is the model diverging — tighten the band or climb the ladder. |
| **Convergence** | peak and rms error vs iteration, log scale, with model-change annotations. Flat-lining means the current inverse has given what it can. |
| **FRF** | measured magnitude/phase/coherence, dropped tones flagged, taper band shaded, current parametric model overlaid. |

## 8. Files every step writes

| file | where | what |
|---|---|---|
| `drive_<stem>.state.npz` | `run\` | the campaign — rewritten every step |
| `drive_<stem>_iNN.csv` | `run\` | the iteration's drive, `time_us,voltage_V` |
| `<stem>_iNN.csv` | the AWG GUI's `Waveforms\` | upload-ready copy (±1, Normalise OFF) |
| `meas_<stem>_iNN.npy` | beside the state | the iteration's averaged measurement — bench mode and Step both save it, so every measured iteration reloads with the session |
| `meas_<stem>_iNN_rMM.npy` | beside the state | a Hold run's averaged measurement — run M of iteration N, same drive, no update |
| `ilc_gui.log` | `run\` | timestamped append-only copy of everything the panel logged |

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

## 10. Every field, what it does

Three kinds of persistence, marked in the tables:

- **state** — saved into `drive_<stem>.state.npz` on every step, restored
  by Load state; changing it changes the campaign.
- **panel** — a per-session choice; the state does not carry it, so check
  it after loading.
- **config** — remembered between launches in
  `%APPDATA%\EOM-ILC-GUI\config.json` (paths and habits, nothing physical).

### Session

| field | what it does |
|---|---|
| **State** *(config)* | Path to a `drive_<stem>.state.npz`. **Load state** rebuilds the whole campaign from it: target, current drive, plant, γ, f_cut, t-offset, iteration counter, error history — and recalls every `meas_*.npy` sharing the stem beside it into the plots. The remembered state reloads automatically at launch. |
| **Target** *(config)* | CSV of the desired waveform: first column time (`time_us` or `time_s`), second column value in **output units** (EOM volts on EO1/EO2, measured volts on GEN — divided by the channel's `mon_scale` on load). Comment lines start with `#`. Auto-plots on selection. |
| **Build…** | Generates a target from scratch. Dialog fields: *shape* (cosine-edged ramp up-hold-return, or half-sine pulse), *peak* in output units, *lead / rise / hold / fall / tail* in ms (lead and tail are flat zero — the level the AWG idles on), *dt* in µs (the loop grid; 2 µs is the campaign standard). |
| **Plot** | Re-draws the target preview on demand: file contents on top, the predicted AWG output of the flat first shot below, against the ±full-scale rails. Sends nothing. |
| **Channel** | Which chain this campaign runs on. Sets the output scale (`mon_scale`: ×1000 on EO1/EO2, ×1 on GEN), the divider used by the limit guard, the bench wiring defaults (AWG ch / scope ch / monitor col), and the auto-pointed FRF (none on GEN). **Switching clears the gains and model parameters** — numbers never follow you between systems. The last choice is remembered between launches, and the wiring fields follow it at startup. *(state + config)* |
| **Name stem** *(state + config)* | ≤ 7 characters. Uploads are named `<stem>_iNN`; the generator stores `<name>.bin` and wedges its front panel past 15 stored characters, so the cap is enforced, not advisory. Restored by Load state, and the last typed value is remembered between launches. |
| **first-shot gain** *(config)* | The conversion gain for the flat first shot **only**: `u₀ = target / gain`. Deliberately separate from the model gain — tuning or refitting the correction model never rescales what iteration 0 plays. Blank = falls back to the model gain (logged). Remembered between launches (it belongs to the remembered channel), but still **cleared when the channel is switched** — the prior-leak guard. |
| **full scale V** *(state)* | AWG volts at record value 1.0 — the fixed DAC mapping every drive file assumes. Default 10, which requires **AMP 20 Vpp, OFST 0** on the generator (bench mode verifies and refuses on mismatch). Also draws the preview rails and scales the `_awg.csv` copies. |
| **Init** | Builds the state file from the target: flat first shot, limit check, drive files written, iteration 0. **Touches no instrument** — no VISA session is opened, and the copy written into the AWG GUI's Waveforms library is a file on disk, not an upload; the AWG and scope can be off. The only instrument assumption baked into the files is the fixed mapping AMP = 2× full scale / OFST 0, which bench mode verifies before ever uploading. If the stem already has a state, it asks: a yes overwrites the loop's memory (current drive, iteration counter, and the Convergence history, which exists nowhere else) and restarts at i00 under the same filenames, clobbering the old per-iteration records as it advances. Continue a campaign with **Load state**; start a parallel one by changing the **stem**. |

### Inverse model

| field | what it does |
|---|---|
| **Model** *(panel)* | What the update divides the error by: gain only (0th), one pole (1st), second order (resonant), or the measured FRF. Not stored in the state — the state stores the plant *parameters*; which form is active is your choice each session. Every history entry is tagged with the model that produced it. |
| **gain** *(state)* | The model's DC gain, AWG volts → measured volts. Used by every parametric lead (`e/gain` is the whole 0th-order correction) and by the model-predicted-output trace. Fallback source for the first-shot gain. |
| **tau us** *(state)* | One-pole time constant, µs. Only the one-pole model reads it. On the Trek chains it equals the resonance's group delay (~28 µs) — the lag without the ring. |
| **fn Hz**, **zeta** *(state)* | Second-order resonance frequency and damping ratio. Only the resonant model reads them. Amplitude-dependent on the Trek chains (fₙ falls with drive), which is why **From calibration** needs the target loaded. |
| **gamma** *(state)* | The learning gain: the fraction of the computed correction applied per iteration. 0.5–0.7 is the useful range; the loop contracts where γ·(model error) stays under 2, so smaller γ buys robustness at the cost of iterations. |
| **f_cut Hz** *(state)* | The zero-phase Q-filter corner for **parametric** updates: learning is confined below it, and the outgoing drive is low-passed at it too. 5 kHz is right on this bench — the parametric models diverge above ~6 kHz. Ignored in FRF mode, deliberately: pre-filtering the error in front of the measured inverse once left 2 V rms of correctable residual untouched. |
| **From calibration** | Fills gain/τ/fₙ/ζ from the measured amplitude-dependent tables (2026-08-20/21) at the loaded target's amplitude. Refuses on GEN — there are no tables to interpolate. |
| **Fit from measurement** | Runs `plant.identify` with the selected model form on the last **played** (drive, response) pair — snapshots store the drive that produced each measurement, so the pair is true even after the drive advanced. Falls back to the Captures glob if no measurement is in session. Fills the boxes and overlays the fitted model's prediction. |
| **FRF** *(config)* | Path to a `tools/sysid_fit.py` output (`f_Hz, H_mag, H_phase_deg, coherence`). Tones with coherence < 0.9 are dropped on load. Only read in measured-FRF mode. |
| **full strength to Hz** *(config)* | `f_use`: the measured inverse acts at full strength up to here. |
| **taper to zero at Hz** *(config)* | `f_max`: a raised cosine takes the correction from full strength at `f_use` to zero here; the error is also smoothed at `f_max` so out-of-band noise cannot alias into the correction. Nothing above `f_max` is ever corrected. The campaign ended at 50/75 kHz; stepping one FRF through widening tapers is the frequency-ranged demo. |
| **Show FRF** | Plots the FRF file (magnitude / unwrapped phase / coherence, taper band shaded, dropped tones flagged) with the current parametric model overlaid for comparison. |

### Capture post-processing (Step + Bench)

| field | what it does |
|---|---|
| **t-offset us** *(state)* | The fixed trigger-to-waveform-start delay, subtracted when resampling every capture onto the loop grid. Measured **0** on this bench (2026-08-24). Measure once, leave alone: re-fitting it per iteration makes the loop chase its own alignment. Bench mode cross-checks it against the uploaded drive on the first iteration and refuses if stale. |
| **zero baseline** *(panel)* | Subtracts the mean of the first 5 % of the record from the measurement. Only valid when the waveform is actually flat there — MKJ is already moving, so on MKJ this subtracts real signal and roughly doubles the reported error (the step warns when the target's own baseline is not flat). Off unless you know why. |

### Step from captured files

| field | what it does |
|---|---|
| **Captures** *(config)* | A glob of scope CSVs; the step averages **every** file it matches and lists them in the log — read that list. Browsing to one file of a sequence replaces the trailing run index with `*`. Captures that do not span the whole waveform are refused (a zoomed file extrapolated flat once manufactured 172 V of fake error). |
| **monitor col** | Which CSV column is the monitor trace: CH3 for EO1, CH4 for EO2 (the fixed bench wiring; auto-set with the channel). Substring matching is tolerant, but a mislabeled scope channel still lies — check the label in Scope Grab. |
| **refit plant** *(panel)* | Re-identifies the parametric model (in the selected form) from this iteration's own drive/response pair before updating, and stores the result in the state. Continuous version of Fit from measurement. |
| **force** *(panel)* | Writes the updated drive even when the limit check FAILED. The check exists because the answer is usually no. |
| **Step** | One iteration: average captures → error metrics → update → limit check → write drive + AWG copy → save state. |

### Bench loop

| field | what it does |
|---|---|
| **AWG ch** | Generator channel (1 or 2) the drive is uploaded to and selected on. 1 drives EO1, 2 drives EO2. |
| **scope ch** | Scope channel carrying the **monitor** for this chain: 3 for EO1, 4 for EO2. |
| **iterations** *(config)* | Number of updates to run. The loop measures `iterations + 1` times — the last measurement documents the final drive without updating past it. |
| **repeats** *(config)* | HRES single shots averaged in software per iteration. 64 is the campaign standard (~25 s at the 20 Hz trigger, dithers the scope's 2.5 mV word lattice to its 0.16 mV floor); 16 is a usable quick check; below 16 is refused. |
| **wait s** | Per-shot trigger stall limit — it only fires if triggers stop arriving. Raise it for slower burst rates. |
| **skip setup checks** *(panel)* | Uploads without verifying AMP/OFST/clock/acquisition mode. A mismatch silently rescales the drive, which is the one error the loop cannot see. Don't. |
| **Upload `<stem>_iNN` to AWG** | Uploads the session's current drive into the generator's user memory and selects it — the manual-workflow counterpart of what the bench loop does each iteration, at the same fixed ±full-scale mapping (never normalised). Asks before overwriting a stored waveform of the same name; no dialog when the name is free. Warns in the log when the output is live (the new waveform plays the moment it is selected). |
| **Auto-set instruments** | Writes the known-good setup from the session's own numbers: AWG arb frequency = 1/period, AMP = 2× full scale, OFST 0, DDS, load HZ, NCYC-1 EXT burst, and the session's current waveform selected if it is already in the generator's user memory (it never uploads — that is the bench loop's or the AWG GUI's job); scope window 1.3× the period (waveform at the left edge), HRES, verticals from the drive/target spans. Refuses on a live output; never switches outputs or touches the trigger. Waveform and burst readbacks are printed — believe those, not the writes. |
| **Run / Stop** | Run executes upload → capture → update per iteration, saving state each time. If the channel's output is OFF, a confirmation dialog offers to switch it ON for the run — ON always asks, and a no cancels cleanly. Stop is graceful: between shots or iterations; a stop mid-capture discards only that iteration, and the state on disk is the last completed one. Any run that played something (or that switched the output on) switches it OFF on exit — off is the harmless direction, and a finished run must not leave the chain driving. |

| **Hold (runs / gap s)** | Re-measures the current drive `runs` times, `gap s` apart, with **no update and no state change** — the thermalisation probe. Runs are tagged `iter k rN`, saved as `meas_<stem>_iNN_rMM.npy`, and numbered on from any earlier holds of the same iteration. Same setup checks, output confirmation and output-off-at-end as the bench loop; Stop works between shots and during the gap. |

### Plot bar

| field | what it does |
|---|---|
| **Iterations shown** *(config)* | Which stored iterations the Drive corrections, Drive updates, Error and Error spectrum tabs overlay: blank = last two, `all`, a range `2-5`, or a list `0,3,6`. Enter or **Redraw** applies. Draws from this session's measurements plus every `meas_*.npy` recalled at Load state. |
| **dot every Nth sample** *(config)* | Marker density on every data trace: blank = auto (~180 dots per trace), a number = that literal subsampling step, `1` = every real sample gets a dot. Enter or **Redraw** applies everywhere, the Waveforms tab and target preview included. |
| **runs** *(config)* | Show or hide Hold runs on the Error / Error spectrum / Convergence tabs. Runs of every selected iteration draw dashed after their base trace. |
| **Δt labels** *(config)* | Append wall-clock offsets to legend labels: runs relative to their iteration's base measurement, base iterations relative to the previous one. Off by default — plot clutter only when the timing question is live. |
