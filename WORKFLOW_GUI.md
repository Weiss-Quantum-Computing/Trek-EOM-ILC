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

**On a Mac (or any non-bench machine):** everything except the bench
buttons works —

```bash
pip install numpy scipy pandas matplotlib
python ilc_gui.py
```

Load state, Step from captured CSVs, Build/preview targets, Fit, the
Compare box, every plot tab and the Table all run offline; pyvisa is only
imported when a bench action (Auto-set / Upload / Bench loop / Hold) is
actually pressed, so it does not need to be installed. Those bench
actions are the one hard limit: they need a VISA layer plus the
instruments, which live on the bench PC. Campaign data is not in git —
copy the `run\` folder (state + `meas_*.npy` + drive CSVs) over and
point **State** at it. The config lives in `~/Library/Application
Support/EOM-ILC-GUI/` there rather than `%APPDATA%`.

## 1. The window at a glance

| panel | what it does |
|---|---|
| **Session** | load a state file, or build one: target + channel + name + first-shot gain → Init |
| **Inverse model** | what the update divides the error by — the model ladder, its parameters, γ and `f_cut`, and the FRF band |
| **Capture post-processing** | how a measurement becomes an error: `t-offset` and baseline handling, shared by Step and Bench. Nothing here touches the first shot. |
| **Step from captured files** | one ILC iteration from scope CSVs you captured yourself |
| **Bench loop** | the hands-off cycle: upload → capture → update |
| **Log** | everything the loop reports. A timestamped copy appends to `run\ilc_gui.log` |
| right side | nine tabs (eight figures plus the Table ledger), refreshed at every step, with the iteration selector and Compare box above them — see §7 |

Every individual field is documented in §10.

## 2. Starting from scratch (any system, no prior knowledge)

Nothing carries over from the Trek chains unless you load it on purpose.

1. **Channel** → `GEN` (unity scaling, open limits apart from the ±10 V
   generator rail, no auto-FRF). For a Trek channel use `EO1`/`EO2`
   instead and skip to §3.
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
7. Press **Replace values with a fit…** and choose *Fit to measurement* —
   the typed guess is replaced by the gain identified from the
   drive/response pair that actually played. Once an FRF exists (**Measure
   FRF…**), *Fit to FRF* is the source for the dynamic terms. Step up the
   model ladder (§6) as the residual demands.

## 3. Resuming an existing campaign

Launching the panel **restores the last session automatically**: the
remembered state reloads with every stored measurement sharing its stem,
so closing the program never costs the plots — reopen and continue
looking. To switch campaigns, or if nothing was remembered:

1. **State** → browse to `run\drive_<stem>.state.npz` → **Load state**.
   Everything — target, current drive, plant, γ, f_cut, t-offset, error
   history, and the model record (which rung of the ladder, and for the
   measured FRF the file + taper, restored into the panel and the loop)
   — comes from the file. The last two measurements beside it are
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
7. **Hold** re-measures a drive — the *current* one, or any stored
   iteration typed in the **iter** box — `runs` times with `gap between runs s`
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
| third order | gain, fₙ, ζ, τ | `f_cut` |
| measured FRF | the file | `full strength to` / `taper to zero at` |

- Parameters come from typing, or from **Replace values with a fit…**,
  which offers two fits and explains both: *Fit to FRF* (least squares of
  the selected form to the FRF file, log-magnitude and phase weighted
  equally over the coherent tones up to a chosen frequency; the gain is the
  median |H| of the lowest tones unless you keep the box; the log reports
  the in-band residual and the frequency where |1 − γH/H_model| first
  reaches 1) and *Fit to measurement* (`plant.identify` on the last played
  drive/response pair, weighted by the record's own spectrum — right for
  the gain, short on lag terms for a slow ramp). Nothing is filled from a
  table any more: the Aug ramp-fit tables encoded a resonance the FRFs
  contradicted, and they were Trek-only.
- **Third order** is the second-order section *times* a real pole, which is
  the general third-order all-pole form: the ζ > 1 the X1 FRFs want makes
  the section two real poles, and τ is the third. It is worth a rung on this
  bench — fitted to `frf_EOX1FRF.csv` up to 60 kHz it takes the phase
  residual from 11.3° to 5.3° and moves the contraction boundary from
  71.6 kHz to 106.8 kHz, which is what decides how high `f_cut` may go. Its
  parameters are **not unique** once ζ > 1 (three real poles split between
  the section and τ several ways), so compare fits by the residual and the
  boundary, not by the fₙ/ζ/τ triple.
- **Measure FRF...** has a **ramped** option: instead of a multitone around
  zero, the record ramps to *hold V*, holds it for *hold ms* with the tones
  on top, and comes back — all inside the burst, so the EOMs are never
  parked at a standing kV. It uses the session's own record, so FRQ, the
  burst and the scope window are untouched. Tones sit on integer bins of the
  **hold**, not the record, so resolution is 1/hold and `f hi` is capped by
  the session grid; the analysis window is the hold alone. This is the
  measurement for the corners: the EOM capacitance is voltage dependent, and
  at the ramp corners this chain answers a drive ripple with several times
  what the zero-centred FRF predicts — which is what makes the loop
  over-correct there. Compare a ramped FRF at the ramp's own level against
  `frf_*` taken around zero to see it.
- Every bench capture **dithers the scope offset**: the monitor (and any aux)
  channel's offset is stepped across three ADC codes over the shots and put
  back afterwards (one code averages the pattern within a code; three also
  average the code-to-code variation, at 21 phases per code for 64 shots). The MSO-X's 8-bit converter carries a fixed ~3.4 mV
  pk-pk error pattern per 40.25 mV code at 1 V/div; a ramp sweeps it at
  slope ÷ code into 15–100 kHz "error" that averaging keeps exactly (it is a
  function of voltage), and the loop then corrects it into real ripple at
  the EOM — P92PX1B/C's "Trek oscillation" of 2 Sep, ~1 V rms at the
  corners the flat shot never had. With every shot at a different code
  phase the mean takes the pattern's mean. The log names the code size; a
  warning appears if the scope rounds the offset coarser than a code. Another
  scope has another code size: set `EOMILC_ADC_CODE_PER_VDIV` to its code as a
  fraction of the V/div setting (fold a flat shot's residual on monitor voltage
  to find it), or to 0 to switch the dither off.
- Every measurement after the first also logs a **model check**: the update
  that made this drive, pushed through the model's own forward operator, against
  the monitor change it actually produced — per band, in the stretch of record
  where that band's update was strongest (the corners, when that is where the
  loop acted). *"answered the last update at 1.1x (0-5 kHz, corr +0.97), 4.4x
  (20-40 kHz, corr +0.86)"* — and when |1 − γ × response| reaches 1 in a band,
  *"the update does not contract there; refit the model in that band, or bring
  the band edge under 20 kHz"*. A correlation under 0.3 is reported as the
  monitor not responding to what was pushed at all. This is the line that would
  have fired at iteration 1 of P92PX1D: a real 0.25 mV feature at the corners,
  answered at four times the small-signal model and therefore over-corrected
  into the ripple that campaign built.
- Every measurement logs a **noise floor** line: the error spectrum against
  the standard error of the shot average, in 5 kHz bands up to the band
  edge (`f_cut`, or the taper's end on the FRF rung). *"Nothing left to
  learn above 38 kHz"* means the error there is under 2× the measurement's
  own scatter — the update it drives is the inverse's gain times noise
  (40–65× at 50–70 kHz on X1 with the second-order lead), laid into the
  drive fresh every iteration, so it never averages away. Lower the band
  edge to that frequency, or stop. P92PX1B (2 Sep): the peak error stopped
  falling at iteration 5; iterations 6–20 put 26 mV rms of 50–70 kHz into
  the drive and 1.4 V rms of ripple onto the EOM the flat shot never had.
- Both the model check and the plateau know about **restarts**: every bench run
  stamps its measurements, the first measurement of a run is not compared
  with the last of the previous one (the change includes whatever the chain
  did while the output was off), and the plateau only reads the current run.
  A manual Step counts as its own run.
- The noise-floor line cannot see one thing: ripple the loop *created* from
  amplified scatter is repeatable, so it reads as genuine error and the loop
  keeps "correcting" it while injecting fresh scatter at the same gain. That
  shows in the history instead, and a **PLATEAU** line fires when it does:
  the best peak error of the last 5 iterations is not below the best before
  them by more than 15 %, *and* the median update `rms(u_k − u_{k−1})` over
  those 5 has not shrunk by more than 15 % either. A loop still converging
  moves both; a finished loop collapses the update. The line names the best
  iteration and its drive file — that is the drive to keep. Each history
  entry now records `update_rms`; states from before 2 Sep lack it and the
  detector stays silent on them until five new iterations have run.
- Switching channel **clears** the parameter boxes — numbers never follow
  you from one system to another.
- Each iteration's history entry is tagged with the model that produced
  it: the Convergence tab draws a divider and label at every model change,
  and the Error tab labels traces by model era.
- **Measure FRF…** automates the probe: multitone built on its own
  record (denser or shorter than the session's when the band demands —
  the 2 µs grid is the analog card's constraint, not the bench's), played
  through the bench, H fitted from the scope's own drive+monitor
  channels, `run\frf_<name>.csv` written and adopted into the FRF field.
  Band adjustable to a 5 MHz sanity ceiling; see §10 for the regimes.
- **Show FRF** plots the measured response with the current parametric
  model overlaid — the model-vs-chain comparison of REPORT.md §5, live.
  The FRF field takes semicolon-separated paths or globs, and every
  match draws together (per-file colours on magnitude, phase and
  coherence) — how the amplitude family (0.5/2/6 V probes) is compared.
  The measured-FRF *model* still requires exactly one file.
- The frequency-ranged FRF cases are one file stepped through different
  tapers (e.g. 10/15 → 24/36 → 50/75 kHz). To measure an FRF in the first
  place, use **Measure FRF…** (below), or the CLI route:
  `tools\sysid_make.py` → play + 64-shot sequence →
  `tools\sysid_fit.py` → browse to `run\frf_<name>.csv`.

### Measure an FRF and drive with it, step by step

1. Load the session; close the AWG GUI and Scope Grab; burst trigger
   firing; bench channels/repeats/wait as usual; **Auto-set instruments**
   with the output off. The run re-checks the setup and has no skip.
2. **Measure FRF…**: peak 2 V for bands to ~100 kHz, 0.5–1 V above that
   (demand scales with peak × f hi — the dialog shows the numbers and
   asks); `f lo`/`f hi` pick the record regime automatically (§10);
   name ≤ 11 chars. Prompts in order: demand → overwrite → output-on.
   The shots are offset-dithered like the loop's captures (drive, monitor
   and any photodiode channel), because the probe is the same waveform every
   shot: the converter's per-code pattern would otherwise repeat identically,
   survive the average of H, and read as *coherent* -- and at the top of the
   band, where the monitor tone is a tenth of a millivolt, it is what limits H.
3. It writes `run\frf_<name>.csv`, points the **FRF** field at it, and
   draws the FRF tab. Output OFF at the end; the probe stays the AWG's
   selected waveform until the next bench upload or Auto-set.
4. On the FRF tab, find where coherence dies. Set **taper to zero at**
   (`f_max`) at or below the last coherent tone, **full strength to**
   (`f_use`) ≈ ⅔ of it. Never taper past the measured band — and as of
   30 Aug 2026 you cannot: `f_use` at or above the file's top coherent
   tone is refused by Init, Step and the bench loop alike, and an `f_max`
   past it is called out in the log. Above the last tone the inverse
   holds |H| flat, so the correction there divides by an extrapolation
   and the loop builds drive at frequencies nothing was measured at.
5. **Model → measured FRF (nonparametric)** — measuring is not
   consenting; this is the switch. γ still applies; `f_cut` is ignored
   in FRF mode (the taper is the band edge).
6. Step or run the bench loop; the log names the inverse and the
   history is tagged `FRF <use>-<max>k`. Judge on the Error spectrum:
   the residual should fall inside the widened taper band. Error
   growing right of the taper → pull `f_max` in. Budget the usual 2–3
   warm-up iterations. A new stem + the old one in Compare shows what
   the wider band bought.

## 7. Reading the plots

The **Iterations shown** box above the tabs picks which stored iterations
the Drive corrections, Drive updates, Error and Error spectrum tabs
overlay: blank shows
the last two, `all` shows everything, `2-5` a range, `0,3,6` a list
(Enter or **Redraw** applies it). Iterations are coloured dark-to-light by
age, newest drawn heaviest. Every measurement the bench loop takes — and
every one recalled from `meas_*.npy` beside the state at load — is
available.

The **Compare** box underneath overlays *other campaigns* on the same
plots: space-separated keys (`OLDX1 OLDX2:all OLDX3:0,3` — each
optionally with the Iterations grammar after a colon, blank meaning that
campaign's last measured iteration). A key is a stem in the active
state's directory, or whatever **Add campaigns...** mapped to a state
file *anywhere on disk* — an archived run, another bench PC's `run\`,
a folder of last month's captures. Picking a file appends its key to the
box, so what is typed stays the record of what is drawn; a picked
campaign whose stem is already spoken for is keyed `STEM@folder`, which
is what lets the same campaign name in two folders be compared against
itself. **Clear** unloads the box, the picked paths and the cached
states. The line underneath says what actually resolved (green), how many
keys did not (amber), or the grammar when nothing is loaded — the log
carries the reason, plus each campaign's channel, grid and stored
iterations when it is added, and a NOTE when its channel or time grid
differs from the session it is going on top of. Their states load
read-only and are never saved or stepped; each campaign draws in its own
colour, on its own time grid and monitor scale, against its own
reference drive. The Convergence tab and the Table always show a compared stem's
whole campaign — that is usually the comparison that matters
(X1 vs X2, or the same target before and after a model change).

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
| **Waveforms** | target vs measured output (top); the drives (bottom) with the current drive's first/last-sample idle markers against the idle cap. Follows the **Iterations** box and **Compare** like the other tabs: every selected measurement (hold runs dashed) and every selected iteration's drive, the current drive always drawn bold. After Init, before any measurement: the model-predicted output. A fit pins its own drive/measurement/prediction view here until the next iteration or load. |
| **Drive corrections** | the drive side of the Error tab: each selected iteration's AWG waveform minus the target's flat conversion (or minus the stored iteration-0 drive when it exists) — what the loop has learned to *add* at the input, in mV at the AWG. Growth here without matching error shrinkage is the loop learning noise or a wrong inverse. |
| **Drive spectrum** | the corrections of the tab above, in frequency: where the learned correction lives, mirroring the Error spectrum (band edge drawn the same way). The update cannot put power right of the band edge — content there came from a hand-edited drive, a stale state, or a band that was wider earlier, and the current band cannot remove it. Base measurements only (hold runs replay the same drive), iterations without a stored drive skipped. |
| **Drive updates** | `u_k − u_(k−1)`: the update each shown iteration actually applied, against the immediately prior iteration's drive (which may come from any stored iteration, selected or not). Shrinking updates are convergence seen from the input; updates that stop shrinking while the error flat-lines mean the loop is re-learning the same correction against noise or drift. |
| **Error** | target − measured for the selected iterations. Peak/rms of the newest in the title. The stuck peak at t = 0 is the burst-entry transient — the loop cannot fix the idle level (WORKFLOW.md). |
| **Error spectrum** | where the residual lives. The update only acts left of the drawn band edge (f_cut line, or the shaded FRF taper). Error growing *right* of the edge while the in-band falls is the model diverging — tighten the band or climb the ladder. Looks noisy? That is mostly estimator variance, not signal: raise *spectra avg*. Need the band past the grid Nyquist? *Native spectrum* in the Step panel. |
| **Convergence** | peak and rms error vs iteration, log scale, with model-change annotations. Flat-lining means the current inverse has given what it can. |
| **Table** | the ledger: one row per stored iteration and hold run — peak/rms error (V and %FS), the played drive's peak, wall-clock timestamp, Δt against the reference — compare stems included, the Iterations box deliberately *not* applied. The *model* column names the model that **built that row's drive**: iteration 0 is blank (Init's flat first shot involves no model), hold runs inherit their base iteration's label, and a model whose drive was never measured does not appear. **Save CSV…** writes exactly what the table shows (this tab's equivalent of the figures' toolbar save). |
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
- **Editor files need a time column.** The Waveform Editor's default save
  is `index,value` with no header; the panel refuses it by name (it would
  otherwise read 5500 points of seconds). Tick the editor's *ILC header
  (time_us,voltage_V)* box with the sample rate set — 500 kHz for the 2 µs
  grid — and save again.
- **A record of a new length needs a new FRQ.** Under DDS the generator
  plays the whole record in one FRQ period, so a hold edit that changes the
  point count needs Auto-set (output OFF) before it plays at the right
  speed. Bench mode and Hold now check FRQ against N × dt and refuse.
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

Every path field scrolls to the **end** of what it holds, so a path too long
for its box shows the file name rather than the drive letter; click into one
and it behaves like any entry again.

| field | what it does |
|---|---|
| **State** *(config)* | Path to a `drive_<stem>.state.npz`. **Load state** rebuilds the whole campaign from it: target, current drive, plant, γ, f_cut, t-offset, iteration counter, error history — and recalls every `meas_*.npy` sharing the stem beside it into the plots. The remembered state reloads automatically at launch. |
| **Target** *(config)* | CSV of the desired waveform: first column time (`time_us` or `time_s`), second column value in **output units** (EOM volts on EO1/EO2, measured volts on GEN — divided by the channel's `mon_scale` on load). Comment lines start with `#`. Auto-plots on selection. |
| **Build…** | Generates a target from scratch. Dialog fields: *shape* (cosine-edged ramp up-hold-return, or half-sine pulse), *peak* in output units, *lead / rise / hold / fall / tail* in ms (lead and tail are flat zero — the level the AWG idles on), *dt* in µs (the loop grid; 2 µs is the campaign standard). |
| **Plot** | Re-draws the target preview on demand: file contents on top, the predicted AWG output of the flat first shot below, against the ±full-scale rails. Sends nothing. |
| **Channel** | Which chain this campaign runs on. GEN's wiring defaults put the monitor on scope CH2, because the bench always reads the *drive* back from the scope channel with the AWG's number (Auto-set, the alignment check and Measure FRF all rely on it) — and every bench action refuses a monitor on that same channel. Sets the output scale (`mon_scale`: ×1000 on EO1/EO2, ×1 on GEN), the divider and the limit set used by the limit guard (`Channel.limits`: the Trek numbers on EO1/EO2, open apart from the ±10 V rail on GEN), the bench wiring defaults (AWG ch / scope ch / monitor col), and the auto-pointed FRF (none on GEN). **Switching clears the gains, the model parameters and the FRF file** — numbers never follow you between systems, and on the measured-FRF rung the FRF *is* the model, so a browsed one is dropped too (logged when it happens) and the new channel's default takes its place: nothing on GEN. The last choice is remembered between launches, and the wiring fields follow it at startup. *(state + config)* |
| **Name stem** *(state + config)* | ≤ 7 characters. Uploads are named `<stem>_iNN`; the generator stores `<name>.bin` and wedges its front panel past 15 stored characters, so the cap is enforced, not advisory. Restored by Load state, and the last typed value is remembered between launches. |
| **first-shot gain** *(config)* | The conversion gain for the flat first shot **only**: `u₀ = target / gain`. Deliberately separate from the model gain — tuning or refitting the correction model never rescales what iteration 0 plays. Blank = falls back to the model gain (logged). Remembered between launches (it belongs to the remembered channel), but still **cleared when the channel is switched** — the prior-leak guard. |
| **full scale V** *(state)* | AWG volts at record value 1.0 — the fixed DAC mapping every drive file assumes. Default 10, which requires **AMP 20 Vpp, OFST 0** on the generator (bench mode verifies and refuses on mismatch). Also draws the preview rails and scales the `_awg.csv` copies. |
| **Seed drive** *(config)* | Optional. A `time_us,voltage_V` drive CSV in AWG volts — any `run\drive_<stem>_iNN.csv`, or one edited in the Waveform Editor and saved there with its *ILC header* box ticked — played **as iteration 0 instead of the flat conversion**, sample for sample. Must have the target's point count and grid (refused otherwise). The limit check runs on it and Init asks before keeping one that fails; the bench loop and Hold refuse to play a failing drive. Recorded in the state as `seed_path` and shown in the summary. This is how an edited converged drive is measured against its edited target without re-learning: edit both files the same way, Init with both, then Hold. Cleared when the channel is switched — a drive is in one chain's volts. |
| **Init** | Builds the state file from the target: flat first shot, limit check, drive files written, iteration 0. **Touches no instrument** — no VISA session is opened, and the copy written into the AWG GUI's Waveforms library is a file on disk, not an upload; the AWG and scope can be off. The only instrument assumption baked into the files is the fixed mapping AMP = 2× full scale / OFST 0, which bench mode verifies before ever uploading. If the stem already has a state, it asks: a yes overwrites the loop's memory (current drive, iteration counter, and the Convergence history, which exists nowhere else) and restarts at i00 under the same filenames, clobbering the old per-iteration records as it advances. Continue a campaign with **Load state**; start a parallel one by changing the **stem**. |

### Inverse model

| field | what it does |
|---|---|
| **Model** *(panel)* | What the update divides the error by: gain only (0th), one pole (1st), second order (resonant), third order (resonant + pole), or the measured FRF. Not stored in the state — the state stores the plant *parameters*; which form is active is your choice each session. Every history entry is tagged with the model that produced it. |
| **gain** *(state)* | The model's DC gain, AWG volts → measured volts. Used by every parametric lead (`e/gain` is the whole 0th-order correction) and by the model-predicted-output trace. Fallback source for the first-shot gain. |
| **tau us** *(state)* | One-pole time constant, µs. Only the one-pole model reads it. On the Trek chains it equals the resonance's group delay (~28 µs) — the lag without the ring. |
| **fn Hz**, **zeta** *(state)* | Second-order natural frequency and damping ratio. Only the second-order model reads them. Any ζ > 0 is accepted: ζ > 1 is two real poles, which is what the Trek chains' FRFs fit (X1 at 0.5 V: fₙ ≈ 7.3 kHz, ζ ≈ 1.1); the ζ ≈ 0.2 resonance of the Aug ramp fits was withdrawn. |
| **gamma** *(state)* | The learning gain: the fraction of the computed correction applied per iteration. 0.5–0.7 is the useful range; the loop contracts where γ·(model error) stays under 2, so smaller γ buys robustness at the cost of iterations. |
| **f_cut Hz** *(state)* | Greyed out in measured-FRF mode — the FRF path deliberately never pre-filters at it (doing so once left 2 V rms of repeatable 5–15 kHz residual untouched; the taper is the only band edge there). The zero-phase Q-filter corner for **parametric** updates: learning is confined below it, and the outgoing drive is low-passed at it too. 5 kHz is right on this bench — the parametric models diverge above ~6 kHz. To effectively **disable** it, enter Nyquist or more (`250e3` on the 2 µs grid): the corner is clamped to 0.99×Nyquist internally, a near-pass-through — knowing that unfiltered parametric learning is the measured drive-grass divergence, re-armed on purpose. Ignored in FRF mode, deliberately: pre-filtering the error in front of the measured inverse once left 2 V rms of correctable residual untouched. |
| **Replace values with a fit…** | Opens a dialog with the two ways the selected rung's parameters can come from data, each explained, and Cancel; either overwrites the boxes, and the loop uses the new values from the next Init, Step or bench iteration. *Fit to FRF*: least squares of the model form to the file in the FRF field (exactly one), log-magnitude and phase weighted equally, coherent tones from the lowest up to *fit up to Hz* (prefilled with f_cut); the gain is the median |H| of the three lowest tones unless *gain from the lowest tones* is unticked, in which case the gain box is kept — a free gain absorbs model mismatch (0.70 against a measured 0.56 on X1). The log gives the in-band residual (% magnitude, degrees phase) and the frequency where |1 − γH/H_model| first reaches 1: keep f_cut below it. Works with no session loaded. *Fit to measurement*: `plant.identify` on the last **played** (drive, response) pair — snapshots store the drive that produced each measurement, so the pair is true even after the drive advanced; falls back to the Captures glob. Weighted by the record's own spectrum: right for the gain, 2–3× short on lag terms for a slow ramp. Both overlay the fitted model (FRF tab, or the Waveforms tab's predicted trace). On the measured-FRF rung the button explains that there is nothing to fit. |
| **FRF** *(config)* | Path to a `tools/sysid_fit.py` or **Measure FRF…** output (`f_Hz, H_mag, H_phase_deg, coherence`). Tones with coherence < 0.9 are dropped on load. Only read in measured-FRF mode. **Show FRF accepts several**: semicolon-separated paths and/or globs (`runrf_A*.csv;runrf_WIDE_X1.csv`) draw together with per-file colours across all three panes — the amplitude-family comparison. The measured-FRF *model* refuses a list: it divides by exactly one, so put back the single file before stepping. |
| **Measure FRF…** | Automated system ID from the panel — the sysid_make + sysid_fit procedure with the band adjustable. A dialog takes probe peak (V at the AWG, capped at full scale), `f lo` / `f hi`, tone count, and a name; the Schroeder multitone is built on the loaded session's own record (tones on integer FFT bins → leak-free analysis, cosine-tapered ends), uploaded through the bench machinery (setup checks with no skip, overwrite ask, output-on confirmation, output off at the end), and each shot reads BOTH the drive (scope channel = AWG channel) and the monitor from the same acquisition, so the fitted H = monitor/drive is immune to AWG flatness. H is averaged across the bench panel's `repeats`; shot-to-shot scatter of H is the coherence. Writes `run\frf_<name>.csv`, points the **FRF** field at it, and draws it — switching the Model to *measured FRF* stays your call. `f hi` is free up to a 5 MHz sanity ceiling — the 2 µs grid is the analog *card's* lookup-table constraint, not the bench's, and in DDS mode the 4063B resamples the whole stored record into one FRQ period, so a denser record probes higher with nothing else changing. Three regimes, picked automatically, all keeping the top tone at or below **half the probe grid's Nyquist** (≥4 samples per period — tones riding just under Nyquist lose coherence to DAC images and resampling, and dense records are free): up to 125 kHz the probe reuses the session record; up to what the arb memory carries (~386 kHz at 16 kpts) a *denser* record of the same duration (instruments untouched); beyond that a *shorter* record — FRQ and the scope window are changed for the run (only from an output-off start) and restored at the end, and the frequency bins coarsen to 1/record, so raise `f lo` accordingly. The capture requests a scope readout dense enough for the band (unprompted, the scope hands over 5000 points whatever the window — measured), sets the verticals for the probe (bipolar around zero, drive ±peak, monitor ±2× flat gain for resonant-peaking headroom — the ramp verticals leave the probe in 1–2 of 8 divisions and burn bits), and the first shot verifies the record actually carries the band, refusing if not. Verticals, like FRQ and the window, are restored at the end. Mind the chain: past ~100 kHz the Trek rolls off hard, so expect low coherence there at low probe amplitudes. The 16 kpt arb-memory figure is the datasheet number — unprobed on this bench; `BK4063B_MAX_PTS` overrides it if a dense upload is refused. Before running, the dialog computes the probe's flat-gain **demand** (peak EOM slew and the capacitive current it implies): past the 610E's 20 V/µs / 2 mA specs it asks for confirmation with the numbers — the Trek cannot exceed its own limits, so the real cost of a hot probe is time spent current-limiting during the burst (distortion, dead coherence above the band), and the validated 24 kHz probes carried 10–30 mA on the same measure. Halving the peak halves the demand; a flat-gain output past 6 kV is refused outright. |
| **full strength to Hz** *(config)* | `f_use`: the measured inverse acts at full strength up to here. Editable only in measured-FRF mode (greyed on the parametric rungs, like f_cut in FRF mode — each band knob belongs to one side of the ladder). |
| **taper to zero at Hz** *(config)* | `f_max`: a raised cosine takes the correction from full strength at `f_use` to zero here; the error is also smoothed at `f_max` so out-of-band noise cannot alias into the correction. Nothing above `f_max` is ever corrected. Both edges are checked against the loaded file: `f_use` past its top coherent tone is refused outright, `f_max` past it warns and names how much of the band is extrapolated. The campaign ended at 50/75 kHz; stepping one FRF through widening tapers is the frequency-ranged demo. |
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
| **refit plant** *(panel)* | Re-identifies the parametric model (in the selected form) from this iteration's own drive/response pair before updating, and stores the result in the state. Continuous version of *Fit to measurement*, with that fit's spectral weighting: right for the gain, short on lag terms for a slow ramp. |
| **force** *(panel)* | Writes the updated drive even when the limit check FAILED. The check exists because the answer is usually no. |
| **Step** | One iteration: average captures → error metrics → update → limit check → write drive + AWG copy → save state. |
| **Native spectrum** | The error spectrum at the SCOPE's native sample rate: the target is interpolated onto the capture's time base instead of the capture being boxcar-decimated onto the waveform grid. Same record length — the low-frequency bins do **not** move — but the band extends to the scope Nyquist and the top octave loses the anti-alias boxcar's droop. Uses the Captures glob + monitor col + the *spectra avg* setting; draws as a black overlay on the Error spectrum tab (next Redraw clears it). Same span guard as Step. Reads raw Scope Grab CSVs and the bench loop's kept `meas_*_native.npz` alike — for bench campaigns, tick *keep native-rate avg* so those files exist. |

### Bench loop

| field | what it does |
|---|---|
| **AWG ch** | Generator channel (1 or 2) the drive is uploaded to and selected on. 1 drives EO1, 2 drives EO2. |
| **scope ch** | Scope channel carrying the **monitor** for this chain: 3 for EO1, 4 for EO2. |
| **iterations** *(config)* | Number of updates to run. The loop measures `iterations + 1` times — the last measurement documents the final drive without updating past it. |
| **repeats** *(config)* | HRES single shots averaged in software per iteration. 64 is the campaign standard (~25 s at the 20 Hz trigger, dithers the scope's 2.5 mV word lattice to its 0.16 mV floor); 16 is a usable quick check; below 16 is refused. |
| **trigger timeout s** | Per-shot trigger stall limit — it only fires if triggers stop arriving. Raise it for slower burst rates. |
| **skip setup checks** *(panel)* | Uploads without verifying AMP/OFST/clock/acquisition mode. A mismatch silently rescales the drive, which is the one error the loop cannot see. Don't. |
| **keep native-rate avg** *(config)* | Bench loop and Hold also save each iteration's repeat average at the scope's own sample rate — `meas_<stem>_iNN[_rMM]_native.npz` (`t` = waveform time, `y` = monitor V) beside the usual decimated `.npy`. Costs ~0.5–1 MB per iteration and nothing else; point the Captures glob at these files to run *Native spectrum* on a bench campaign after the fact. |
| **keep verticals** *(config)* | Auto-set leaves the scope's V/div and offset as they are (it still sets FRQ, AMP, OFST, load, burst, timebase and HRES, and prints the verticals it found). Tick it for a session whose target span differs from the campaign it is compared against — an edited endpoint, say — so the 8-bit lattice stays the same; untick and Auto-set re-ranges both channels from the session's own spans. |
| **Upload `<stem>_iNN` to AWG** | Uploads the session's current drive into the generator's user memory and selects it — the manual-workflow counterpart of what the bench loop does each iteration, at the same fixed ±full-scale mapping (never normalised). Asks before overwriting a stored waveform of the same name; no dialog when the name is free. Warns in the log when the output is live (the new waveform plays the moment it is selected). |
| **Auto-set instruments** | Writes the known-good setup from the session's own numbers: AWG arb frequency = 1/period, AMP = 2× full scale, OFST 0, DDS, load HZ, NCYC-1 EXT burst, and the session's current waveform selected if it is already in the generator's user memory (it never uploads — that is the bench loop's or the AWG GUI's job); scope window 1.3× the period (waveform at the left edge), HRES, verticals from the drive/target spans. Refuses on a live output; never switches outputs or touches the trigger. Waveform and burst readbacks are printed — believe those, not the writes. |
| **Run / Stop** | Run executes upload → capture → update per iteration, saving state each time. If the channel's output is OFF, a confirmation dialog offers to switch it ON for the run — ON always asks, and a no cancels cleanly. Stop is graceful: between shots or iterations; a stop mid-capture discards only that iteration, and the state on disk is the last completed one. Any run that played something (or that switched the output on) switches it OFF on exit — off is the harmless direction, and a finished run must not leave the chain driving. |

| **Hold (runs / gap between runs s / iter)** | Re-measures the current drive `runs` times, `gap between runs s` apart, with **no update and no state change** — the thermalisation probe. Runs are tagged `iter k rN`, saved as `meas_<stem>_iNN_rMM.npy`, and numbered on from any earlier holds of the same iteration. Same setup checks, output confirmation and output-off-at-end as the bench loop; Stop works between shots and during the gap. The **iter** box picks which stored iteration to hold: blank is the current drive; a number loads that iteration's `drive_<stem>_iNN.csv` from beside the state, uploads it under its own name and tags the runs to it (`iter 9 r1…`), so an earlier drive can be re-measured without Init — which would restart the campaign at i00 under the same names. When a run lands, the panel ticks **runs** and adds the held iteration to the **Iterations** box if either would have hidden it, and says so in the log. |

### Plot bar

| field | what it does |
|---|---|
| **Iterations shown** *(config)* | Which stored iterations the Drive corrections, Drive updates, Error and Error spectrum tabs overlay: blank = last two, `all`, a range `2-5`, or a list `0,3,6`. Enter or **Redraw** applies. Draws from this session's measurements plus every `meas_*.npy` recalled at Load state. |
| **Compare** *(config, paths remembered)* | Other campaigns overlaid on the same plots, read-only: space-separated keys, each optionally `key:ITERS` with the Iterations grammar (`OLDX1 OLDX2:all OLDX3:0,3`); a blank selection means that campaign's last measured iteration. A key is a stem in the active state's directory (`drive_<stem>.state.npz` plus its `meas_*.npy` / `drive_*_iNN.csv`), or a state file elsewhere picked with **Add campaigns...** — which appends its key to the box and keys a name collision `STEM@folder`. **Clear** empties the box, the picked paths and the state cache. The status line under the box names what resolved and counts what did not. Never saved or stepped. Each stem gets one colour (hues chosen clear of the active session's viridis ramp), older selected iterations blending toward white; hold runs ride along dashed when **runs** is on. Overlays land on Error, Error spectrum, Drive corrections, Drive spectrum, Drive updates (each stem against its *own* reference and prior drives, on its *own* time grid and monitor scale), Convergence (the stem's whole peak+rms error history, regardless of the iteration selection), and both Waveforms panes (every selected measurement and drive, plus that stem's target when it differs). Δt labels stay active-session-only. A stem with no state or no measurements is reported in the log once per spec. |
| **dot every Nth sample** *(config)* | Marker density on every data trace: blank = auto (~180 dots per trace), a number = that literal subsampling step, `1` = every real sample gets a dot. Enter or **Redraw** applies everywhere, the Waveforms tab and target preview included. |
| **runs** *(config)* | Show or hide Hold runs on the Error / Error spectrum / Convergence tabs. Runs of every selected iteration draw dashed after their base trace. |
| **Δt labels** *(config)* | Append wall-clock offsets to legend labels: runs relative to their iteration's base measurement, base iterations relative to the previous one. Off by default — plot clutter only when the timing question is live. |
| **spectra avg** *(config)* | Welch segment count for BOTH spectrum tabs: blank or `1` = the raw single-record FFT (full resolution, but a periodogram's per-bin scatter is ~100% and does not average down with record length), `N` = Hann-windowed 50%-overlap averaging over N segments (scatter drops ~√N, resolution coarsens ~N-fold — tones closer than the new bin width merge, burst-edge content smears across segments). Normalised so a pure tone keeps its height in both modes; the broadband noise floor moves with bin width, so only compare curves drawn at the same setting — the title says which. |
| **link t** *(config)* | Rectangle-zoom (or pan, or home) on any time-domain plot — Waveforms, Drive corrections, Drive updates, Error — applies that time window to all of them, and redraws keep it until the next zoom. The toolbar zoom itself is unchanged; it just acts everywhere at once. Untick to zoom plots independently. |
