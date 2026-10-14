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

The settings sit in seven tabs on the left, in the order a campaign uses
them; the session summary, the log and the status line underneath are always
visible.

| tab | what it holds |
|---|---|
| **Session** | load a state file, or build one: target + channel + name + first-shot gain → Init. The **Pair (X1 + X2)** tick on top makes Load and Init act on both chains at once (§5) |
| **Model** | *Inverse model*: what the update divides the error by — the model ladder, its parameters, γ and `f_cut`, the learning notches, and the FRF band. In pair mode a second block under it is X2's, with its own everything |
| **Hardware** | one look at the rack: Scope, AWG, ELL14, DS345, each with a **Check** that opens the device, asks its identity and how it stands (acquisition mode and V/div; outputs ON/OFF, FRQ and waveform; mount position; gate address), closes it again, and writes the answer and the time into the row, red when it did not answer. The mount and the DS345 are the polarimeter's: their port and address come from its config, and while its window is open they report "in use". Nothing is moved or programmed by a Check. The ELL14 row has one control that does move: **move to deg** and **Move** turn the analyzer to that angle (the polarimeter's frame), approached from below as the polarimeter does (3° short, then a positive step), read it back and close the mount; the row then says `moved to +45.00 deg` with the time. Refused, red and with a message, while the polarimeter window holds COM3 ("in use"). The angle a Check or a Move last read is what the pair loop writes into its PD sidecars. |
| **Scope** | the *Scope channels* map (which channel is the PD, the monitors, the Trek commands — the polarimeter's roles), the capture settings (`t-offset`, baseline), and the line-ripple switch with its **Line ripple…** dialog |
| **Run** | the bench flow in order: a *Bench* line (wiring in force, the last thing a run reported about the instruments), **1 Instruments** (Auto-set, Upload), **2 Learn** (iterations, repeats, Run, Stop), **3 Hold**, then **Batch…**, **Manual step…** (one iteration from captures you took) and **Bench settings…** (trigger timeout, the skip/keep ticks, GEN's channel numbers). In pair mode Auto-set, Run and Hold act on both chains |
| **Optical** | the optical close by hand, both chains (pair mode only): a state line (the pair, the outputs as last reported, what the AWG was last given and whether one crystal plays alone, the last scan, the correction files held, the last verdict) and seven numbered steps — **1 Play** (both, X1 alone or X2 alone), **2 Scan**, **3 Compare**, **4 Apply**, **5 Learn**, **6 Verify**, **7 Outputs OFF** — each greyed, with the reason under it, until the step before has a result; **Settings…** for the scan's rarely changed numbers — §11 |
| **Corrections** | learned target corrections: move the TARGET by an error measured elsewhere (the polarimeter), within limits — §11 |
| **Log** | everything the loop reports. A timestamped copy appends to `run\ilc_gui.log` |
| right side | eight tabs, refreshed at every step, with the iteration selector (in pair mode led by a **shown X1 / X2** switch) and Compare box above them: Waveforms, Error (error / spectrum), Convergence, Drive (corrections / spectrum / updates), Diagnostics, Table, Inputs (FRF / target corrections), Optical (rotation / correction / before / after: drawn by the Optical tab's Compare and Verify) — a tab with several figures has a view bar on top; see §7 |
| status line | what is running, or when idle the next step: Load/Init, then Auto-set and Run, then the lowest-rms iteration since i08 (the batch's keeper rule); in pair mode it names the shown chain |

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
2. Read the **Bench** line (the channel numbers come from the Scope tab's
   map), set **iterations** and **repeats** (64 = the campaign standard; 16
   is a usable quick check).
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

### Both chains at once (pair mode)

The two Trek + EOM chains are different amplifiers, and the ramps play with
both moving; the batch has always trained them together. Tick **Pair
(X1 + X2)** on the Session tab and the panel does the same, by hand:

1. **State** and **Target** take the **X1** file; the X2 file is the same name
   with the last `X1` replaced by `X2` (`target_S47X1.csv` →
   `target_S47X2.csv`, `drive_S47X1.state.npz` → `drive_S47X2.state.npz`). A
   name with no `X1` in it, or an X2 file that is not there, is refused with
   the name it looked for. A seed drive pairs the same way.
2. The **Channel** box is EO1 and greyed: the Model tab's top block is X1's
   (EO1) and a second block under it is X2's (EO2), each with its own rung,
   parameters, γ, `f_cut`, notches, FRF and taper, its own **Replace values
   with a fit…** and **Show FRF**, and its own field memory (EO2's, the same
   memory the Channel switch uses).
3. **Init campaign** builds both, each from its own block: the **Name stem**
   gets `X1` / `X2` appended (so at most 5 characters), X1's first shot uses
   the first-shot gain box and X2's its own model gain. Nothing is written
   until both have passed their checks. **Load state** loads both and fills
   both blocks from their states.
4. **Auto-set** sets both AWG channels and the drive and monitor verticals of
   both chains in one session of the instruments, and refuses while either
   output is ON. **Run bench loop (X1 + X2)** asks once to switch both
   outputs ON, uploads both drives every iteration, checks the alignment of
   each chain that has a CmdXn channel, reads **both monitors from the same
   frozen acquisition** on every shot (the offset dither on both), then for
   each chain: metrics, noise floor, stats sidecar, model check, update with
   its own loop, state saved, drive files written. Log lines carry `X1` /
   `X2` in front. Both outputs go OFF at the end. **Hold** re-measures both
   drives the same way, runs numbered per chain.

   **The PD rides along.** With a PD channel in the Scope channels map the
   loop also reads the analyzer photodiode from the same frozen shots (one
   more channel per shot, dithered like the monitors, no extra trigger) and
   keeps it as `run\meas_<pair stem>PD_iNN.npy` — the pair stem is X1's
   without its trailing `X1` (`PAIRX1` → `meas_PAIRPD_i01.npy`) — with a
   stats sidecar (`…_stats.npz`: shots, standard error, the V/div it was read
   at, the channel, and the analyzer angle the Hardware tab last read, NaN
   when none). The log gets `PD: peak … V, change vs last … mV rms` per
   iteration. The batch writes the same files under the campaign's stem, so
   **Load state** recalls either into both sessions' measurements and the
   Diagnostics → **pd** view draws them. The loop trains the monitors; this is
   the record of what the light did meanwhile. Hold does not read it.
5. The **shown X1 / X2** switch above the plots picks which session the
   plots, the summary ("… (X2 of a pair)"), the status hint and every
   single-channel tool — Upload, Manual step, Corrections, Measure FRF, the
   line ripple — are on. It can be flipped mid-run.

Unticking leaves the X1 session loaded and the panel exactly as it is
without the tick.

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
- The noise-floor line also lists **noise lines**: narrow peaks in the
  shot-to-shot scatter at five times the level around them (a
  free-running oscillation somewhere in the chain, not the ramp). The
  update can only chase those; the line gives the `notch Hz` text that
  makes it blind there, and says "notched" once it is.
- Switching channel or model **swaps** the boxes to what that channel (and
  that model) last had: the first-shot gain, seed drive and browsed FRF are
  remembered per channel, the parameter boxes per channel *and* model, and
  the memory survives a relaunch with the rest of the config. A channel never
  used starts with blank boxes — numbers never follow you from one system to
  another. A model never used on the current channel keeps the numbers on
  show as its starting point. The log line at each switch says what came
  back.
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
the Drive (corrections, updates) and Error (error, spectrum) views
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
| **Drive → corrections** | the drive side of the Error tab: each selected iteration's AWG waveform minus the target's flat conversion (or minus the stored iteration-0 drive when it exists) — what the loop has learned to *add* at the input, in mV at the AWG. Growth here without matching error shrinkage is the loop learning noise or a wrong inverse. |
| **Drive → spectrum** | the corrections of the view above, in frequency: where the learned correction lives, mirroring the Error spectrum (band edge drawn the same way). The update cannot put power right of the band edge — content there came from a hand-edited drive, a stale state, or a band that was wider earlier, and the current band cannot remove it. Base measurements only (hold runs replay the same drive), iterations without a stored drive skipped. |
| **Drive → updates** | `u_k − u_(k−1)`: the update each shown iteration actually applied, against the immediately prior iteration's drive (which may come from any stored iteration, selected or not). Shrinking updates are convergence seen from the input; updates that stop shrinking while the error flat-lines mean the loop is re-learning the same correction against noise or drift. |
| **Error → error** | target − measured for the selected iterations, the newest with its ±1 standard-error band shaded (from the stats sidecar). Peak/rms of the newest in the title. The stuck peak at t = 0 is the burst-entry transient — the loop cannot fix the idle level (WORKFLOW.md). |
| **Error → spectrum** | where the residual lives, with the newest measurement's noise floor shaded (the standard error of the shot mean per bin, smoothed) and a dotted 2× line: error above it is learnable, inside it is the shots' scatter the loop would imprint. The update only acts left of the drawn band edge (f_cut line, or the shaded FRF taper). Error growing *right* of the edge while the in-band falls is the model diverging — tighten the band or climb the ladder. Looks noisy? That is mostly estimator variance, not signal: raise *spectra avg*. Need the band past the grid Nyquist? *Native spectrum* in the Step panel. |
| **Convergence → by iteration** | peak and rms error vs iteration, log scale, with model-change annotations and the measurement's own **noise floor rms** dotted (the standard error of the shot mean: nothing under it is learnable, so an rms curve that reaches it has converged). Flat-lining above it means the current inverse has given what it can. |
| **Convergence → by wall clock** | the same against the clock: iterations numbered, hold runs hollow, a dotted vertical where a new run started (the output was off in between: the first iteration after it re-measures the same drive worse). Thermal transients and slow floor changes read here. |
| **Diagnostics → code fold** | the residual (slow part removed) folded on one ADC display code of the monitor channel, per selected iteration with each bin's standard error, and the fold's peak-to-peak against iteration with one-sigma bars (hollow under three sigma). The 8-bit converter's per-code pattern (3.4 mV pk-pk undithered on 2 Sep 2026) stands many sigma up; the dithered scope reads one to three. Needs the stats sidecar (every bench, hold and step measurement from 12 Oct 2026) and the scope's V/div, which the bench reads at capture. |
| **Diagnostics → lattice** | histogram of the newest selected iteration's RAW-rate residual in scope words (10.24 V / 4096 per V/div): values piling on the integer words say the lattice is in the data undithered. Needs the native-rate file (`keep native-rate avg` in Bench settings). |
| **Diagnostics → bands** | rms error in the campaign bands (0–10, 10–20, 20–30, 30–60 kHz) against iteration, each band's noise floor dotted in its colour (the bound), and the growth factor per iteration over the last five points with its one sigma in the legend: above 1.02 the band is pumping, the September gate. |
| **Diagnostics → contraction** | |1 − γ × achieved/predicted| per model-check band from every bench iteration after the first of a run: under 1 the band converges, the shaded region above 1 means the correction overshoots; hollow markers where the monitor's change did not correlate with the update (noise). |
| **Diagnostics → heatmap** | every stored base iteration's error as one image (time across, iteration down, colour symmetric to the 99th percentile): corner ripple, hold creep and end effects separate at a glance; a band growing at the corners is a stripe brightening down the image. |
| **Diagnostics → DAC step** | the newest selected update's |Δu| histogram in generator steps (0.31 mV), the fraction under one step (rounded away) and that fraction's history inset: once most of an update is under a step the loop can only limit-cycle on the quantizer. |
| **Diagnostics → pd** | the analyzer photodiode per stored iteration of the shown session (top, volts against ms, coloured as the Waveforms tab; title `PD on CH1 by iteration, <stem>` with the analyzer angle when it was known) and each iteration's PD minus the last stored one in mV (bottom), each iteration's monitor error rms in the legend (both chains in pair mode). At a fixed analyzer angle the PD reads cos² of the light's rotation, so its change tracks how the light answered each update: most sensitive with the analyzer ~45° from the polarization, blind at extinction or full transmission. From `meas_<stem>PD_iNN.npy` (pair loop, batch); a session without them shows a note only. |
| **Table** | the ledger: one row per stored iteration and hold run — peak/rms error (V and %FS), the played drive's peak, wall-clock timestamp, Δt against the reference — compare stems included, the Iterations box deliberately *not* applied. The *model* column names the model that **built that row's drive**: iteration 0 is blank (Init's flat first shot involves no model), hold runs inherit their base iteration's label, and a model whose drive was never measured does not appear. **Save CSV…** writes exactly what the table shows (this tab's equivalent of the figures' toolbar save). |
| **Inputs → FRF** | measured magnitude/phase/coherence, dropped tones flagged, taper band shaded, current parametric model overlaid. |
| **Batch** | the Batch window's **Preview** (§10, Batch window): with nothing selected, one small panel per campaign of the plan (3 columns), both targets in kV at the EOM against ms in the channel colours, titled `<stem>  <N> pts  <period> ms  FRQ <f> Hz` plus `optical` / `echo of <source>`; a campaign whose row has a problem carries it in red (an empty panel when its targets could not be read). Under the grid the model line of each channel and the total time, as the dry run prints them. With a campaign selected, that campaign alone: its targets with the ramp's start, top, hold end and fall end marked (the card split's transports and their common flat top; rise / hold / fall lengths written in) and each channel's V90 dotted, and below them the first drive each channel will play (the flat first shot, or the state's current drive when the campaign resumes) with the limit check's verdict, peak slew and peak current in the legend. Look for both channels on one grid, the idle at 0 at both ends, the peaks at V90 (5128 / 5137 V), no edge faster than 0.3 ms. |

## 8. Files every step writes

| file | where | what |
|---|---|---|
| `drive_<stem>.state.npz` | `run\` | the campaign — rewritten every step |
| `drive_<stem>_iNN.csv` | `run\` | the iteration's drive, `time_us,voltage_V` |
| `<stem>_iNN.csv` | the AWG GUI's `Waveforms\` | upload-ready copy (±1, Normalise OFF) |
| `meas_<stem>_iNN.npy` | beside the state | the iteration's averaged measurement — bench mode and Step both save it, so every measured iteration reloads with the session |
| `meas_<stem>_iNN_rMM.npy` | beside the state | a Hold run's averaged measurement — run M of iteration N, same drive, no update |
| `meas_<pair stem>PD_iNN.npy` + `_stats.npz` | `run\` | the pair loop's (and the batch's) analyzer photodiode for iteration N, shot mean on X1's grid, and its sidecar with the analyzer angle; only with a PD channel in the map |
| `ilc_gui.log` | `run\` | timestamped append-only copy of everything the panel logged |
| `<stem>_optN\`, `<stem>_optN_X1\` / `_X2\` (one crystal alone), `<stem>_optN_verify\` | `run\optical\` | an Optical-tab scan: the polarimeter's scan folder (Scope Grab captures + `<name>_scan.json`, readable by its data viewer); the comparison in its `analysis\target_compare\` (`corr_<stem>X1/X2_optical.csv`, `target_<stem>X1/X2_played.csv`, `summary.json`, `target_compare.npz`) |

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
- **The scope channels are ROLES, not numbers (12 Oct 2026).** The bench
  loop reads the monitor from the channel with the MonXn role and the
  drive back from the CmdXn channel of the *Scope channels* map on the
  Scope tab — the same map the polarimeter uses. No CmdXn channel means
  the trigger alignment is NOT verified on that run (the log says so) and
  Measure FRF refuses (it is monitor / drive). The map is written into every
  state the bench measures, and Load state says when it differs from the
  panel's now.

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
| **Pair (X1 + X2)** *(config)* | Pair mode (§5): State, Target and Seed drive take the X1 file and the X2 file is found by name (the last `X1` → `X2`; refused when there is no `X1` or no such file); Channel is EO1 and greyed; the stem gets `X1` / `X2` appended; Load and Init act on both; the Model tab grows X2's block and the plots a **shown** switch. Unticked, the X1 session stays loaded. Remembered between launches, and the startup reload then loads the pair. |
| **State** *(config)* | Path to a `drive_<stem>.state.npz`. **Load state** rebuilds the whole campaign from it: target, current drive, plant, γ, f_cut, t-offset, iteration counter, error history — puts the state's own target file back in the **Target** box (recorded as `target_path` by Init; a state from before that is matched by content against `waveforms\` and `run\`, and one that matches nothing leaves the box alone and says so) — and recalls every `meas_*.npy` sharing the stem beside it into the plots. The remembered state reloads automatically at launch. |
| **Target** *(config)* | CSV of the desired waveform: first column time (`time_us` or `time_s`), second column value in **output units** (EOM volts on EO1/EO2, measured volts on GEN — divided by the channel's `mon_scale` on load). Comment lines start with `#`. Auto-plots on selection. Recorded in the state by Init and restored by Load state, so the box always shows the target the loaded campaign's iterations were measured against. |
| **Build…** | Generates a target from scratch. Dialog fields: *shape* (cosine-edged ramp up-hold-return, or half-sine pulse), *peak* in output units, *lead / rise / hold / fall / tail* in ms (lead and tail are flat zero — the level the AWG idles on), *dt* in µs (the loop grid; 2 µs is the campaign standard). |
| **Plot** | Re-draws the target preview on demand: file contents on top, the predicted AWG output of the flat first shot below, against the ±full-scale rails. Sends nothing. |
| **Echo…** | Builds a spin-echo record from a one-transport target pair (`eomilc.echo`, the same builder as `tools\make_echo_target.py` and a batch campaign's `"echo"` key): *Source X1* (its X2 is the same name with X2, beside it), *leg spacing ms* (leg start to leg start, the experiment's echo time; rounded to the record's grid, 16.667 → 16.668 ms on 2 µs), *legs*, *tail µs*, *end ms* (the record padded with idle to this period, so the time after leg 2's fall is inside it for the loop and the optical close to correct; 32.768 = the generator's 16 384-point cap at 2 µs, the whole 16.667 ms echo being 33.333; blank = no pad), *suffix* (blank = `E<legs>`), *Write to* (blank = the source's folder). **Build + check** writes `target_<stem><suffix>X1/X2.csv` — the source's lead, the transport, idle until the next leg starts one spacing after the first, the transport again, the tail — reads both back with the init limit check, logs the record's numbers and the card pause between legs, and puts the new X1 in the **Target** box with its name in the stem box, so Init in pair mode trains both legs in one campaign. An identical file already on disk is left alone. Warns when the name is over the pair mode's 5 characters (shorten the suffix) and refuses a spacing shorter than the transport. The last settings are remembered *(config)*. |
| **Channel** | Which chain this campaign runs on. GEN's wiring defaults put the monitor on scope CH2, because the bench always reads the *drive* back from the scope channel with the AWG's number (Auto-set, the alignment check and Measure FRF all rely on it) — and every bench action refuses a monitor on that same channel. Sets the output scale (`mon_scale`: ×1000 on EO1/EO2, ×1 on GEN), the divider and the limit set used by the limit guard (`Channel.limits`: the Trek numbers on EO1/EO2, open apart from the ±10 V rail on GEN), the bench wiring defaults (AWG ch / scope ch / monitor col), and the auto-pointed FRF (none on GEN). **Switching clears the gains, the model parameters and the FRF file** — numbers never follow you between systems, and on the measured-FRF rung the FRF *is* the model, so a browsed one is dropped too (logged when it happens) and the new channel's default takes its place: nothing on GEN. The last choice is remembered between launches, and the wiring fields follow it at startup. *(state + config)* |
| **Name stem** *(state + config)* | ≤ 7 characters. Uploads are named `<stem>_iNN`; the generator stores `<name>.bin` and wedges its front panel past 15 stored characters, so the cap is enforced, not advisory. Restored by Load state, and the last typed value is remembered between launches. |
| **first-shot gain** *(config)* | The conversion gain for the flat first shot **only**: `u₀ = target / gain`. Deliberately separate from the model gain — tuning or refitting the correction model never rescales what iteration 0 plays. Blank = falls back to the model gain (logged). Remembered between launches and **per channel**: switching channel swaps in that channel's own value, blank if it never had one — the prior-leak guard. |
| **full scale V** *(state)* | AWG volts at record value 1.0 — the fixed DAC mapping every drive file assumes. Default 10, which requires **AMP 20 Vpp, OFST 0** on the generator (bench mode verifies and refuses on mismatch). Also draws the preview rails and scales the `_awg.csv` copies. |
| **Seed drive** *(config)* | Optional. A `time_us,voltage_V` drive CSV in AWG volts — any `run\drive_<stem>_iNN.csv`, or one edited in the Waveform Editor and saved there with its *ILC header* box ticked — played **as iteration 0 instead of the flat conversion**, sample for sample. Must have the target's point count and grid (refused otherwise). The limit check runs on it and Init asks before keeping one that fails; the bench loop and Hold refuse to play a failing drive. Recorded in the state as `seed_path` and shown in the summary. This is how an edited converged drive is measured against its edited target without re-learning: edit both files the same way, Init with both, then Hold. Remembered per channel — a drive is in one chain's volts, so switching channel swaps in that channel's own seed (blank if none). |
| **Init** | Builds the state file from the target: flat first shot, limit check, drive files written, iteration 0. **Touches no instrument** — no VISA session is opened, and the copy written into the AWG GUI's Waveforms library is a file on disk, not an upload; the AWG and scope can be off. The only instrument assumption baked into the files is the fixed mapping AMP = 2× full scale / OFST 0, which bench mode verifies before ever uploading. If the stem already has a state, it asks: a yes overwrites the loop's memory (current drive, iteration counter, and the Convergence history, which exists nowhere else) and restarts at i00 under the same filenames, clobbering the old per-iteration records as it advances. Continue a campaign with **Load state**; start a parallel one by changing the **stem**. |

### Inverse model

| field | what it does |
|---|---|
| **Model** *(panel)* | What the update divides the error by: gain only (0th), one pole (1st), second order (resonant), third order (resonant + pole), or the measured FRF. Not stored in the state — the state stores the plant *parameters*; which form is active is your choice each session. Every history entry is tagged with the model that produced it. |
| **X2 (EO2) block** *(pair mode; field memory)* | The same fields again for X2 — Model, gain / tau / fn / zeta, gamma, f_cut, FRF, the taper, notch — showing only the rung's own fields, with its own **Replace values with a fit…** (the fit dialog reads this block's FRF and X2's measurements and writes this block's boxes) and **Show FRF**. Init and the pair loop give X2 these numbers and X1 the top block's; Load fills each from its own state. Remembered as EO2's field memory, which the Channel switch reads too. Measure FRF lands its file in the shown chain's block. |
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

### Scope channels (Scope tab)

| field | what it does |
|---|---|
| **CH1–CH4 role / name** *(config)* | What each scope channel carries, in the polarimeter's vocabulary: `PD` (analyzer photodiode), `MonX1`/`MonX2` (Trek monitors), `CmdX1`/`CmdX2` (the Trek input = the AWG output teed to the scope), `Ref`, `Marker`, `Other`, `off`. The Trek chains take their numbers from here: **scope ch** = the MonXn channel, the drive readback (alignment check, Auto-set's drive verticals, Measure FRF's denominator) = the CmdXn channel or none, the PD for Measure FRF = the PD channel. GEN keeps the old fixed numbers. The map is written into every state the bench measures (`scope_channels`), printed at the start of every bench run, and the batch runner reads it from the same config. A role on two channels is refused. |
| **Copy from polarimeter** | Reads `%APPDATA%\RampPolarimeter\config.json` and takes its channel map, logging what changed — the one way to keep the two programs' views of the rack identical. |
| **Defaults** | The bench as wired 5–12 Oct 2026: CH1 PD, CH2 CmdX1, CH3 MonX1, CH4 MonX2. |

### Capture (Scope tab; Step + Bench)

| field | what it does |
|---|---|
| **t-offset us** *(state)* | The fixed trigger-to-waveform-start delay, subtracted when resampling every capture onto the loop grid. Measured **0** on this bench (2026-08-24). Measure once, leave alone: re-fitting it per iteration makes the loop chase its own alignment. Bench mode cross-checks it against the uploaded drive on the first iteration and refuses if stale. |
| **zero baseline** *(panel)* | Subtracts the mean of the first 5 % of the record from the measurement. Only valid when the waveform is actually flat there — MKJ is already moving, so on MKJ this subtracts real signal and roughly doubles the reported error (the step warns when the target's own baseline is not flat). Off unless you know why. |

### Line (mains) ripple (Scope tab; the numbers and buttons in the **Line ripple…** dialog)

On a **line-synchronous trigger** the mains ripple on the monitor is the same
in every shot (5 Oct 2026, the experiment's trigger: 0.9 / 1.35 mV at 60 Hz on
the X1 / X2 monitors, ~1 V at each EOM; 23 mdeg on the light; same phase in
all 19 analyzer steps over 25 minutes), so the shot average keeps it and the
loop learns a drive that cancels it *at that phase* — which is wrong as soon
as the experiment moves its ramps against its line resync. Measured once and
subtracted from every measurement before the update, the loop never sees it.
A free-running trigger already averages it away (the old bench practice):
leave this off there.

| field | what it does |
|---|---|
| **subtract the measured ripple…** *(state)* | Takes the fitted ripple out of every measurement before the update and the error metrics (`Loop.line`, state key `line_ref`, honoured by the CLIs too). The Error tabs show the error the loop acts on. Refuses (asks) when the fit's shots were not phase-coherent. |
| **line Hz / harmonics / window ms** *(config)* | 60 Hz and 5 harmonics by default; the window of the bench measurement (100 ms = 6 periods — the record itself, ~11 ms, cannot tell 60 Hz from a slow drift, which is why it is measured separately). |
| **Measure (drive off)** | Bench: AWG output OFF (asks if it is on, leaves it off), scope widened to the window on the same trigger (restored after), 8–32 dithered shots of the monitor, harmonics fitted to the mean, per-shot phase coherence reported (≥ 0.7 = line-synchronous). Times out with a hint if the scope triggers on the drive itself. |
| **Fit from captures…** | Offline: Scope Grab CSV/NPZ captures of the monitor (column = *monitor col*), drive off — or any capture whose undriven stretches are named in **fit only in ms** (`-12:0, 10:16.5`). Must use the same trigger as the ILC runs. Short stretches fit badly (the log says when). |
| **Clear** | Forgets the fit and switches the subtraction off. |

### Manual step… (Run tab dialog)

| field | what it does |
|---|---|
| **Captures** *(config)* | A glob of scope CSVs; the step averages **every** file it matches and lists them in the log — read that list. Browsing to one file of a sequence replaces the trailing run index with `*`. Captures that do not span the whole waveform are refused (a zoomed file extrapolated flat once manufactured 172 V of fake error). |
| **monitor col** | Which CSV column is the monitor trace: CH3 for EO1, CH4 for EO2 (the fixed bench wiring; auto-set with the channel). Substring matching is tolerant, but a mislabeled scope channel still lies — check the label in Scope Grab. |
| **refit plant** *(panel)* | Re-identifies the parametric model (in the selected form) from this iteration's own drive/response pair before updating, and stores the result in the state. Continuous version of *Fit to measurement*, with that fit's spectral weighting: right for the gain, short on lag terms for a slow ramp. |
| **force** *(panel)* | Writes the updated drive even when the limit check FAILED. The check exists because the answer is usually no. |
| **Step** | One iteration: average captures → error metrics → update → limit check → write drive + AWG copy → save state. |
| **Native spectrum** | The error spectrum at the SCOPE's native sample rate: the target is interpolated onto the capture's time base instead of the capture being boxcar-decimated onto the waveform grid. Same record length — the low-frequency bins do **not** move — but the band extends to the scope Nyquist and the top octave loses the anti-alias boxcar's droop. Uses the Captures glob + monitor col + the *spectra avg* setting; draws as a black overlay on the Error spectrum tab (next Redraw clears it). Same span guard as Step. Reads raw Scope Grab CSVs and the bench loop's kept `meas_*_native.npz` alike — for bench campaigns, tick *keep native-rate avg* so those files exist. |

### Run tab

| field | what it does |
|---|---|
| **Bench** line | The wiring in force (AWG channel → monitor channel, readback channel or "none (alignment not verified)") and the last instrument fact a run reported (Auto-set's FRQ/AMP/output, output ON/OFF at a run's end), with its time. In pair mode, one wiring line per chain from the Scope channels map. |
| **AWG ch / scope ch** *(Bench settings…)* | Generator channel the drive is uploaded to and the scope channel carrying the monitor. EO1/EO2 fill them from the *Scope channels* map (the MonXn channel) and refuse a number that contradicts it; GEN uses them as typed. |
| **iterations** *(config)* | Number of updates to run. The loop measures `iterations + 1` times — the last measurement documents the final drive without updating past it. |
| **repeats** *(config)* | HRES single shots averaged in software per iteration. 64 is the campaign standard (~25 s at the 20 Hz trigger, dithers the scope's 2.5 mV word lattice to its 0.16 mV floor); 16 is a usable quick check; below 16 is refused. |
| **trigger timeout s** *(Bench settings…)* | Per-shot trigger stall limit — it only fires if triggers stop arriving. Raise it for slower burst rates. |
| **skip setup checks** *(Bench settings…)* | Uploads without verifying AMP/OFST/clock/acquisition mode. A mismatch silently rescales the drive, which is the one error the loop cannot see. Don't. |
| **keep native-rate avg** *(config, Bench settings…)* | Bench loop and Hold also save each iteration's repeat average at the scope's own sample rate — `meas_<stem>_iNN[_rMM]_native.npz` (`t` = waveform time, `y` = monitor V) beside the usual decimated `.npy`. Costs ~0.5–1 MB per iteration and nothing else; point the Captures glob at these files to run *Native spectrum* on a bench campaign after the fact. |
| **keep verticals** *(config, Bench settings…)* | Auto-set leaves the scope's V/div and offset as they are (it still sets FRQ, AMP, OFST, load, burst, timebase and HRES, and prints the verticals it found). Tick it for a session whose target span differs from the campaign it is compared against — an edited endpoint, say — so the 8-bit lattice stays the same; untick and Auto-set re-ranges both channels from the session's own spans. |
| **Upload `<stem>_iNN` to AWG** | Uploads the session's current drive into the generator's user memory and selects it — the manual-workflow counterpart of what the bench loop does each iteration, at the same fixed ±full-scale mapping (never normalised). Asks before overwriting a stored waveform of the same name; no dialog when the name is free. Warns in the log when the output is live (the new waveform plays the moment it is selected). |
| **Auto-set instruments** | Writes the known-good setup from the session's own numbers: AWG arb frequency = 1/period, AMP = 2× full scale, OFST 0, DDS, load HZ, NCYC-1 EXT burst, and the session's current waveform selected if it is already in the generator's user memory (it never uploads — that is the bench loop's or the AWG GUI's job); scope window 1.3× the period (waveform at the left edge), HRES, verticals from the drive/target spans. Refuses on a live output; never switches outputs or touches the trigger. Waveform and burst readbacks are printed — believe those, not the writes. |
| **Run / Stop** | Run executes upload → capture → update per iteration, saving state each time. If the channel's output is OFF, a confirmation dialog offers to switch it ON for the run — ON always asks, and a no cancels cleanly. Stop is graceful: between shots or iterations; a stop mid-capture discards only that iteration, and the state on disk is the last completed one. Any run that played something (or that switched the output on) switches it OFF on exit — off is the harmless direction, and a finished run must not leave the chain driving. |
| **Run bench loop (X1 + X2)** *(pair mode)* | The same loop on both chains: both drives uploaded each iteration (CH1 X1, CH2 X2), one question for both outputs ON, alignment checked on each chain with a CmdXn channel, both monitors read from the **same** acquisition on every shot with the offset dither, then per chain the metrics, noise floor, stats sidecar, model check, update with its own model, limit check, state and drive files — `meas_<stem>X1_iNN.npy` and `…X2…` side by side. Log lines start `X1` / `X2`. Both outputs OFF at the end. The two states must share one grid (refused otherwise). *keep native-rate avg* is not kept in pair mode (the log says so). Auto-set sets both AWG channels and both chains' verticals and refuses while either output is ON; Hold re-measures both drives (the **iter** box applies to both) from one acquisition per shot. |

| **Hold (runs / gap between runs s / iter)** | Re-measures the current drive `runs` times, `gap between runs s` apart, with **no update and no state change** — the thermalisation probe. Runs are tagged `iter k rN`, saved as `meas_<stem>_iNN_rMM.npy`, and numbered on from any earlier holds of the same iteration. Same setup checks, output confirmation and output-off-at-end as the bench loop; Stop works between shots and during the gap. The **iter** box picks which stored iteration to hold: blank is the current drive; a number loads that iteration's `drive_<stem>_iNN.csv` from beside the state, uploads it under its own name and tags the runs to it (`iter 9 r1…`), so an earlier drive can be re-measured without Init — which would restart the campaign at i00 under the same names. When a run lands, the panel ticks **runs** and adds the held iteration to the **Iterations** box if either would have hidden it, and says so in the log. |

### Batch window (Run tab, **Batch…**)

The plan format, the runner and its stops are in WORKFLOW.md (Batch runs); the window's buttons:

| field | what it does |
|---|---|
| **Check plan** | Points, record length and time estimate per campaign, and the problems, without instruments; the numbers typed in the window are used but not written into the plan. |
| **Preview** | Loads the plan as Check plan does (echo records the plan asks for are built, as the dry run builds them) and draws it on the **Batch** plot tab (§7): with nothing selected, every campaign's targets, one panel each, the problems in red and the models and total time underneath; with a campaign selected in the table, that campaign alone, its targets with the ramp marks above the first drive it will play and each channel's limit verdict. Check here what the batch will fit to before anything runs. The window's status line says `previewed 6 campaigns` / `previewed S47E2`; **Esc** in the table clears the selection, so Preview shows the whole plan again. |
| **Simulate** / **Run on bench** | The plan on the simulated bench (files in `run\sim`), or on both Treks after one confirmation. |
| **status line and the bar** (14 Oct 2026) | Both come from `eomilc.progress.BatchProgress`, fed by the batch's events. The bar runs over the plan's campaigns and moves within the current one: the electrical part counted to the iteration cap (an early stop completes it), weighted against the optical stage by the plan's own estimates, then scan by scan. The window's line reads `S47E2 2/6 \| i05 of <=20 (X1 0.54 V, X2 0.58 V) \| elapsed 12 min \| this campaign ~9 min left (at the iteration cap) \| batch ~2:10 h left`, or during the close `optical pass 1: scan S47E2_opt1_X1 (2/5)`. Time left is the plan's estimate corrected by this run: the iteration time from the gaps between iteration events, the scan time from the gaps between optical events, and for the queued campaigns the plan's totals scaled by what the finished ones really took. The main window's status line carries the short form. The plan's `timing.read_s_per_channel` is 0.12 s since 14 Oct (measured 36 s per 64-shot iteration); `scan_step_overhead_s` 2 and `compare_s` 10 are the optical stage's rough numbers. |
| double-click a campaign | Loads its state (the followed channel) in the panel. |

### Plot bar

| field | what it does |
|---|---|
| **shown X1 / X2** *(pair mode)* | Which session of the pair the plots, the summary, the status hint and the single-channel tools (Upload, Manual step, Corrections, Measure FRF, line ripple; the AWG / scope channel numbers follow it) are on. Switching redraws every tab from that session. Load and Init show X1. |
| **Iterations shown** *(config)* | Which stored iterations the Drive corrections, Drive updates, Error and Error spectrum tabs overlay: blank = last two, `all`, a range `2-5`, or a list `0,3,6`. Enter or **Redraw** applies. Draws from this session's measurements plus every `meas_*.npy` recalled at Load state. |
| **Compare** *(config, paths remembered)* | Other campaigns overlaid on the same plots, read-only: space-separated keys, each optionally `key:ITERS` with the Iterations grammar (`OLDX1 OLDX2:all OLDX3:0,3`); a blank selection means that campaign's last measured iteration. A key is a stem in the active state's directory (`drive_<stem>.state.npz` plus its `meas_*.npy` / `drive_*_iNN.csv`), or a state file elsewhere picked with **Add campaigns...** — which appends its key to the box and keys a name collision `STEM@folder`. **Clear** empties the box, the picked paths and the state cache. The status line under the box names what resolved and counts what did not. Never saved or stepped. Each stem gets one colour (hues chosen clear of the active session's viridis ramp), older selected iterations blending toward white; hold runs ride along dashed when **runs** is on. Overlays land on Error, Error spectrum, Drive corrections, Drive spectrum, Drive updates (each stem against its *own* reference and prior drives, on its *own* time grid and monitor scale), Convergence (the stem's whole peak+rms error history, regardless of the iteration selection), and both Waveforms panes (every selected measurement and drive, plus that stem's target when it differs). Δt labels stay active-session-only. A stem with no state or no measurements is reported in the log once per spec. |
| **dot every Nth sample** *(config)* | Marker density on every data trace: blank = auto (~180 dots per trace), a number = that literal subsampling step, `1` = every real sample gets a dot. Enter or **Redraw** applies everywhere, the Waveforms tab and target preview included. |
| **runs** *(config)* | Show or hide Hold runs on the Error / Error spectrum / Convergence tabs. Runs of every selected iteration draw dashed after their base trace. |
| **Δt labels** *(config)* | Append wall-clock offsets to legend labels: runs relative to their iteration's base measurement, base iterations relative to the previous one. Off by default — plot clutter only when the timing question is live. |
| **spectra avg** *(config)* | Welch segment count for BOTH spectrum tabs: blank or `1` = the raw single-record FFT (full resolution, but a periodogram's per-bin scatter is ~100% and does not average down with record length), `N` = Hann-windowed 50%-overlap averaging over N segments (scatter drops ~√N, resolution coarsens ~N-fold — tones closer than the new bin width merge, burst-edge content smears across segments). Normalised so a pure tone keeps its height in both modes; the broadband noise floor moves with bin width, so only compare curves drawn at the same setting — the title says which. |
| **link t** *(config)* | Rectangle-zoom (or pan, or home) on any time-domain plot — Waveforms, Drive corrections, Drive updates, Error — applies that time window to all of them, and redraws keep it until the next zoom. The toolbar zoom itself is unchanged; it just acts everywhere at once. Untick to zoom plots independently. |

### Corrections tab

| field | what it does |
|---|---|
| **Correction files** | The layers: file, slot (what it corrects — a new file in the same slot replaces the old one), gain, the peak applied after the gate, how much of the active record is resolved, status (added / changed / applied / refused / off). **Add file…**, **Remove**, **On / off**, **gain** + **Set** (0–1.5). |
| **band Hz** *(config)* | Nothing faster than this is applied (the file's own `band_hz` if lower). 2 kHz default: the polarimeter is slow and noisy above it, and the ramps' motional band starts at 4.9 kHz. Must be under the loop's own band (f_cut, or the FRF's full-strength edge) or Apply refuses. |
| **resolved at k sigma** *(config)* | Where the low-passed correction is under k × its own low-passed standard error (the file's sigma column) it is held at zero — that part is the measurement's noise. 3 by default. |
| **floor V** *(config)* | Smaller than this is held at zero: below what the monitor average can verify (dithered 64-shot mean ~0.05 mV; 0.1 mV = 0.1 V at an EOM). Volts at the output. |
| **cap V** *(config)* | Largest total change allowed, volts at the output. Past it Apply refuses — lower a gain, or raise the cap on purpose. |
| **end fade us / only where the target is active** *(config)* | Both record ends fade to exactly zero; with the tick, the correction is zero wherever the base target sits at its idle level (the AWG holds the first sample between bursts). |
| **Preview** | Gates every switched-on file against the BASE target, checks the total, logs the numbers and draws the *Target corrections* plot tab. Changes nothing. |
| **Apply to target** | Preview, then: refuse on any failure (wrong channel, another target's grid or values, over the cap, band above the loop's, the estimated drive past the Trek limits, the AWG rail or the ±full-scale upload mapping); otherwise confirm with the numbers and set target = base + total. The base target is kept in the state (`base_target`, `corr_total`, `corrections`); the next measurement is against the new target. |
| **Revert to base** | Target back to the base target; the files stay in the box, switched off. |

### Optical tab (pair mode)

Every step runs in the background (Stop, the log, the status line as on the
Run tab), opens the scope and the AWG for itself and closes them again
**without touching the outputs** — only step 7 switches them OFF, and a step
that dies after switching one on. A step whose prerequisite is missing is
greyed with the reason under it (orange), and refused with that reason if it
is reached anyway; pair mode off greys all seven. Loading or initialising
another pair starts the steps over.

| field | what it does |
|---|---|
| **state line** | The pair's stems and iterations; the outputs as last reported (by step 1 or 7, by a scan or Learn that found them OFF, or by any pair run that switched them OFF); the waveforms the AWG was last given and the play mode (*playing both*, *X1 alone, X2 parked*); the last scan (and its verification); the correction files held for Apply and the scan each came from (*corrections: EO1 from …_X1, EO2 from …_X2*), their peaks and what Apply put on the targets; the last verdict. |
| **play** *(panel)* | *both* \| *X1 alone* \| *X2 alone*, beside step 1. Alone: that crystal's keeper plays and the other channel is **parked** — its drive held flat at the record's idle (its first sample), the same length so FRQ and the trigger hold, uploaded as `PARK1` / `PARK2`, its output ON like the other's (an output is never switched off to park). |
| **1 Play (outputs ON)** | Uploads the **current** drives (`<stem>X1_iNN`, `<stem>X2_iNN`; alone, the partner's park drive) after the same setup and limit checks as the bench loop (refused with nothing uploaded on any problem; Auto-set with both outputs off first), asks once to switch both outputs ON, and **leaves them ON** — the Bench line says *kept ON until step 7*. Switching between both / X1 alone / X2 alone with the outputs ON asks nothing: only the waveforms change. |
| **name** *(panel)* | The scan's folder name under `run\optical\`. Filled with `<stem>_optN` (playing alone: `<stem>_optN_X1` / `_X2`), N the first number without a complete scan of that name, and moves on after each complete scan; a typed name is kept. A name whose scan is on disk unfinished (stopped) **resumes** it; a complete one is used as it is (nothing new is taken). |
| **angles** *(config)* | Analyzer angles: `start:stop:step` in degrees (`0:359:22.5`, 16 angles) or a list. |
| **shots** *(config)* | Single HRES shots per angle (8). |
| **Scan / Stop** | Refused when either output is OFF or the AWG holds other drives than the sessions' (or, for a parked chain, its park drive): Play again. The scan's manifest says what played (`"solo": "X1"` / `"X2"` / null), and with one crystal alone its hold-null angles come from that crystal's rotation only. Opens the analyzer (ELL14, the polarimeter's port and zero) beside the panel's scope, sets the scope's timebase from 0.5 ms before the trigger to 30 ms after the record and single HRES shots, steps the angles (reference returns, hold-null angles), and puts the timebase and acquisition back. The polarimeter's preset is **not** written unless named in Settings (the trigger and the verticals are the ILC's). Needs the headless Scope Grab (scope-grab-multi): with the old checkout the scan is refused by name. Stop saves what was taken; Scan again under the same name resumes. |
| **band Hz** *(config)* | The comparison's low-pass and the correction's band (5000: the 0.3 ms ramp-top feature needs it); Apply gates at this band. Must be under each loop's own band or Apply refuses. |
| **PD delay us** *(config)* | The photodiode's own delay, read off the light before it is compared (1.7 µs on this bench). |
| **split X1** *(config)* | The share of the rotation correction put on EO1; EO2 gets the rest (0.5 for parallel shapes). Not used for a scan taken with one crystal alone: the whole correction is that crystal's. |
| **Compare** | `eomilc.optical.compare` on the last scan with both states (their **base** targets once corrected): per leg light, monitors and target, light − monitors, light − target, and `corr_<stem>X1/X2_optical.csv`. A scan taken with one crystal alone is compared as such (`solo=`, from what played at the scan, not now): the parked crystal's state goes in flat and only the driving crystal's file is written. The files **accumulate** for Apply — a joint compare sets both, a solo one sets its crystal's and keeps the other's (the step's note: *EO1 15.1 V (from S47E2_opt1_X1); EO2 from S47E2_opt1_X2 held*). Only a joint compare is a later Verify's *before*. The verdict lines go to the log; the Optical plot tab's *rotation* and *correction* views are drawn. |
| **gain** *(config)* | The fraction of the correction applied (0.7: the statistical sigma is small, leg-to-leg and up/down differences are not in it). |
| **Apply to the targets** | `eomilc.optical.apply_correction` on each loop with the file held for its crystal (EO1's to X1, EO2's to X2; a chain with no file held is left alone), gated with the Corrections tab's limits (k sigma, floor, cap, end fade; active-only is off for these files, the light after the ramp being part of what is corrected, so the whole record is kept and faded 200 µs at the ends) at the band of the comparison the file came from, on top of the session's base target and layers — a file in the same slot **replaces** the last, because light − monitors is a fresh estimate of the whole error every time (moving the target moves the monitors with the light). Solo files take the `optical` slot at gain 1, each crystal's own error in full. A joint verification's files applied afterwards go beside them in their own slot (`joint2`, `joint3`, …) as the joint file **minus each crystal's split share of what is already applied on both** (`eomilc.optical.joint_residual_files`; the joint file says what the whole correction should be, not what is left): zero where the joint error is just the sum of the crystals' own, and otherwise the share the solo scans could not see. The log says which crystal's file came from which scan and which slot it went to. All or nothing: a refusal on either puts the other back, logs the notes and says why in a dialog. The base targets are kept in the states, the moved ones are saved, and the Corrections tab lists the shown session's layer. No confirmation: the step is the confirmation. |
| **iterations** *(config)* | Iterations of Learn (5); repeats, trigger timeout and the skip tick are the Run tab's. |
| **Learn** | The pair bench loop (§5) on the corrected targets with the outputs **already ON**: nothing is asked, an output found OFF refuses the run before anything is uploaded, and the outputs stay ON when it ends — finished, stopped or at a failed limit check (only a run that dies switches them OFF). It uploads both keepers, so afterwards both play (a parked chain is unparked). |
| **Verify (scan + compare)** | Needs both playing (step 1 with *both*, or Learn): the verification is always a **joint** scan, the light the atoms get. A scan named `<scan>_verify` (a solo scan's `_X1` / `_X2` dropped: `<stem>_optN_verify`, then `_verify2`, …) with the same settings, its comparison, and a before / after table in the log: per leg the ramp-top and first-ms-after-the-fall extremes of light − target, its rms, the ramp-top light − monitors, and the correction peaks. The Optical plot tab's *before / after* view draws the magnitudes; the status line says whether every leg's ramp-top light − target **halved**. Without a joint *before* (a pass measured one crystal at a time) there is no table: the log says so, the verdict lines stand alone and become the next pass's before. The verification then serves as the next pass's comparison: Apply takes its correction (replacing the last), Learn, Verify again — or measure each crystal alone again. |
| **Outputs OFF (both)** | Both outputs OFF; the Bench line and the state line say so. Where every Optical session ends. |
| **Settings…** *(config)* | *points per shot* (20000) and beside it *sample us* (2.0): points blank = the screen span / sample spacing, computed by `optical.scan` (2 µs matches the ILC grid, so the light is read on the loop's own spacing whatever the record length); a number reads out that many points and the spacing is not used. *reference every* (a return to the reference angle every N steps, 9), *settle s* (before the first shot, bench only), *preset* (a polarimeter preset written before the scan; blank = none), *hold-null angles* (extra angles around the hold's null, from each target's peak: 90° × peak / V90 per crystal), *comparison figures* (the polarimeter's fig1–5 PNGs beside the comparison). |

## 11. Learned target corrections (the outer loop)

The ILC makes the **monitor** follow the target. What the experiment needs is
the **light**, and the light does not do exactly what the monitors say
(5 Oct 2026: light − monitors ±2–3° over the ramps, a static function of
rotation plus a 7 µs lag). A correction moves the target by minus that
error; the loop then learns the drive that puts the monitor on the moved
target, and the light lands on the original target. Moving the *drive*
instead does not work: the next ILC update pulls the monitor back onto the
old target.

The correction must be **light − monitors**, not light − target: the loop
removes the monitor's own error by itself, so a correction built on
light − target removes it twice and puts it back with the opposite sign.

Nothing here is specific to polarization or to the EOMs. A correction file
(`eomilc/corrections.py`: `# EOM-ILC target correction v1`, `# key: value`
header, `time_us,delta_V,sigma_V`, volts in the target CSV's units) can come
from any measurement of any outcome, for any channel GEN included; the panel
checks it against the session's own limits.

### By hand: the Optical tab

Pair mode, both chains together (the experiment plays them together, and
each Trek keeps its own model). Converge the pair first (Run tab), Auto-set
with both outputs off, close the polarimeter's window (the analyzer is on
COM3 and the scope is one program's at a time), make sure the PD is on
screen and on the Scope channels map, then on the **Optical** tab:

1. **Play**, *both* — both current drives uploaded, both outputs ON (one
   question). They stay ON until step 7.
2. **Scan** — the analyzer steps through the angles while the keepers play;
   the scan lands in `run\optical\<stem>_optN\`, in the polarimeter's own
   format (its data viewer opens it).
3. **Compare** — light − monitors and light − target per leg, a correction
   file per crystal, the Optical plot tab.
4. **Apply** — both targets move by the gated correction × gain (under 1 for
   the first pass: the statistical sigma is small, but leg-to-leg and up/down
   differences of 0.1–0.2° are not in it); the base targets are kept.
5. **Learn** — the pair loop learns the moved targets, outputs still ON.
6. **Verify** — a second scan and comparison; the before / after table. The
   number to read is light − **target** (the base target): light − monitors
   is the crystal's and stays; what the close removes is the light's
   distance from the target the experiment wants. For another pass, Apply
   again: the verification's correction *replaces* the last. Light − monitors
   does not depend on the correction already applied (to first order), so
   each scan is a fresh estimate of the WHOLE correction, not of a remainder;
   stacking them would apply it twice. Then Learn, Verify.
7. **Outputs OFF**.

**Each crystal alone (13 Oct 2026).** A scan with both playing sees one
residual, light minus what both monitors predict, and *split X1* shares it
out by a fixed fraction: that corrects the light, but the files are valid
only for that pair of drives, while the errors are each crystal's own (the
8 Oct creep was X1's alone; in a time-split record the holding crystal's
creep shows in the other's window). Measured per crystal instead, with the
**play** radio beside step 1:

1. **Play**, *X1 alone* — X1's keeper plays, X2's channel is parked flat at
   its idle (`PARK2`, output ON).
2. **Scan** — `<stem>_optN_X1`.
3. **Compare** — the whole correction is X1's: EO1's file is held.
1. **Play**, *X2 alone* — `PARK1` on X1's channel, X2's keeper (the outputs
   are already ON: nothing is asked).
2. **Scan** — `<stem>_optN_X2`.
3. **Compare** — EO2's file joins EO1's (the step's note: *EO1 from
   …_X1 held*).
4. **Apply** — both files, each to its chain; the log names the scan each
   came from.
5. **Learn** — both keepers play again.
6. **Verify** — a joint scan `<stem>_optN_verify` (the light the atoms get).
   With no joint comparison before it there is no table: the verdict is
   logged and is the next pass's before. For another pass measure each
   crystal alone again (1 X1 alone, 2, 3; 1 X2 alone, 2, 3), or Apply the
   verification's joint correction.
7. **Outputs OFF**.

A scan taken with one crystal alone must be compared as such; the panel
remembers what played at each scan and its manifest says so (`"solo"`). The
batch does the same with `"attribution": "separate"` (WORKFLOW.md).

The batch's optical stage (docs/OPTICAL_CLOSE_DESIGN.md §3) runs the same
library (`eomilc/optical.py`) over every shape of a plan, unattended.

### The alternative: the polarimeter's window

1. Run the polarimeter scan with the CmdX1/X2 and MonX1/X2 roles (and, for
   the line ripple, a second scan of the same sequence with the ramps
   disabled — the Optical tab does not take that reference scan).
2. `ramp-polarimeter` → *ILC target* (or `tools/target_compare.py`): writes
   `target_<name>_played.csv` (the ILC target with the experiment's timing)
   and `corr_<name>_optical.csv` per crystal.
3. ILC panel: Init a campaign on `target_<name>_played.csv`, converge it.
4. Corrections → Add `corr_<name>_optical.csv` → Preview → Apply (a gain
   under 1 for the first pass is the cautious choice). Iterate the loop
   until it has learned the new target.
5. Verification scan, and a new correction file. It *replaces* the old one
   in the same slot (the box asks), for the reason in step 6 above.
