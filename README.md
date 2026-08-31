# EOM-ILC — pre-distortion and iterative learning control for the Trek / EOM ramp drive

Corrects the tracking error of the Trek 610E → EOM chain by reshaping the drive
waveform. A model-based pre-distortion takes the first shot; iterative learning
control (ILC) then converges on the residual by dividing the measured error by
the chain's own measured frequency response H(f).

Built around the two bench programs that already exist —
[keysight-scope-grab](https://github.com/Weiss-Quantum-Computing/keysight-scope-grab) (a pre-rename checkout may still sit in a `scope-grab` folder -- the bench code accepts either) and
[BK4063B-AWG-GUI](https://github.com/Weiss-Quantum-Computing/BK4063B-AWG-GUI) —
whose instrument layers this imports rather than reimplementing, so there is no
second copy of SCPI to keep in step.

**Campaign write-up:** [REPORT.md](docs/REPORT.md) — the 24–25 Aug 2026 report:
fixing the measurement, the parametric era and its failure, the measured
inverse, the final numbers, and the instrument catalog.

## The inverse is measured, not modeled

This is the thing to know before anything else — it changed on 24–25 Aug 2026.
Schroeder-multitone probes of both chains at 2 V found **no resonance**: a
smooth rolloff with phase easing to ~−140°, like several distributed poles plus
delay. That rules out both parametric forms this repo grew up with — a single
pole cannot lag past −90° (the chain is at 82° already at the pole's own
5.7 kHz corner), and the second-order peak simply is not in the probe data.
So the production loop divides the error by the measured H(f) directly, tapered
to zero past the measured band. Wide-probe FRFs for both channels ship in
`run/frf_WIDE_X1.csv` and `run/frf_WIDE_X2.csv`; on the bench this converged at
~0.4 per iteration everywhere the taper was open and reached 2.4 V peak
(0.046%) on both channels.

The FRF workflow, all in this repo:

1. `tools/sysid_make.py` builds the probe (defaults 0.4–24 kHz, 48 tones;
   `--f-hi 80e3 --tones 60` was the wide probe) and its `_awg.csv`.
2. Play it through the normal burst path; capture 64 HRES single shots.
3. `tools/sysid_fit.py` turns the captures into `run/frf_<name>.csv` — magnitude,
   phase, and per-tone coherence, formed from the scope's *measured* drive
   channel so drive-side rolloff cancels.
4. Hand that file to the loop with `--frf` (both `run_ilc.py step` and
   `ilc_bench.py` take it, with `--frf-use`/`--frf-max` setting the taper —
   the campaign ended at 50/75 kHz).

**How the parametric era ended, for the record.** The chain had been
characterized from large-signal ramp fits (`characterisation/`, 20–21 Aug) as a
lightly damped second order, ζ ≈ 0.21, fₙ 2.2–3.0 kHz. ILC contracts only
where `|1 − γ·L·Ptrue| < 1`, and above ~6 kHz the real chain passes 4–8× more
than that model predicts: the loop's drive grew high-frequency grass that
tripled every iteration while the captures looked clean. Pulling `--f-cut` to
5 kHz froze the divergence but left a repeatable ±3–4 V residual at 3–6 kHz
that no amount of iteration could remove — a wrong inverse is not fixed by
iterating on it. The probes then showed the fitted resonance was a
*large-signal* phenomenon of edges near the Trek's slew limit, absent at probe
level. The full story, with the contraction numbers per band, is REPORT.md
§4–5; `simulation/simulate.py` and `simulation/make_validation_fig.py` reproduce the parametric-era
simulations that (correctly, given the fits) rejected the one-pole model.

The ramp fits did get the group delay right (τ ≈ 28 µs = 2ζ/ωₙ), which is why
the model-based first shot worked: `run_ilc.py init` seeds from the
`config.py` fit constants, and the loop's `--frf` path takes over from
iteration 1. (Since 26 Aug the *default* first shot is a flat conversion —
target / gain, no pre-distortion — so the chain's raw response is measured
directly before any correction; `--model-first-shot` restores the
pre-distorted seed.)

## Layout

| | |
|---|---|
| `eomilc/` | the library: `config` (calibration), `plant` (models + fitting), `ilc` (the loop), `outputs` (file emission), `scope` (capture reader) |
| `run_ilc.py` | manual driver — `init` / `step` / `emit-ni` |
| `ilc_bench.py` | closed-loop driver, upload → capture → update with no hands |
| `ilc_gui.py` | panel front end for both drivers, with per-iteration plots (waveforms, error, spectrum, convergence, FRF); same state files as the CLIs, so GUI and CLI steps interleave. The inverse model is selectable — gain-only (0th order), one pole, second order, or the measured FRF — with parameters tuned by hand, filled from the calibration tables, or fitted from a measurement; the convergence plot marks where the model changed. `ilc_gui.bat` launches it with the Anaconda interpreter |
| `tools/` | target builders and system-ID: `make_target.py` (MKJ target at any peak and grid), `make_ramp_target.py`, `sysid_make.py` (Schroeder multitone probe), `sysid_fit.py` (probe captures → `run/frf_<name>.csv`) |
| `simulation/` | off-bench validation: `simulate.py`, `make_validation_fig.py` and its figure |
| `characterisation/` | the 2026-08-21 analysis that produced every constant in `config.py` |
| `waveforms/` | the current targets and iteration-0 drives (the one home for targets) |
| `run/` | the **active campaign workspace**: states, iteration drives, measurements, and the measured FRFs — see `run/README.md` for the file taxonomy and where finished campaigns get filed |
| `WORKFLOW.md` | **the bench procedure** — read this before touching hardware |
| `WORKFLOW_GUI.md` | the same procedure as panel clicks: step-by-step for `ilc_gui.py`, from-scratch recipe included |
| `docs/` | the campaign write-up (`REPORT.md` + `figures/`), `MKJ_FULL_NOTES.md` (what the MKJ waveform is, headroom arithmetic, DDS behaviour), `ANALYSIS_PROMPT.md` (paste-able brief for an offline data-analysis session — data layout, loaders, measured traps), and the Scope Grab averaging patch |
| `archive/` | superseded bench outputs kept for the record (not tracked), including `report-era/` — the frozen 24–25 Aug analysis: the SCRX scratch campaigns, the probe-era files, and the still-runnable figure scripts behind `docs/figures/`, with their own README |

Needs `numpy`, `scipy`, `pandas`, and `pyvisa` for the bench drivers. On the lab
PC that means the Anaconda interpreter, `C:\ProgramData\anaconda3\python.exe` —
it is the only one there with all four.

## Quick start

PowerShell, one command per line. `\` is not a continuation character
there, and bare `python` is the wrong interpreter (see above).

Build the target — **X1**, then **X2**:

```powershell
C:\ProgramData\anaconda3\python.exe tools\make_target.py --channel EO1 --peak-hv 5200 --step 2 --out waveforms\target_MKJX1.csv
```

```powershell
C:\ProgramData\anaconda3\python.exe tools\make_target.py --channel EO2 --peak-hv 5200 --step 2 --out waveforms\target_MKJX2.csv
```

First shot — a **flat conversion** by default (target / gain, no
pre-distortion, so the first measurement shows the chain's raw response;
`--model-first-shot` restores the pre-distorted seed):

```powershell
C:\ProgramData\anaconda3\python.exe run_ilc.py init --target waveforms\target_MKJX1.csv --channel EO1 --name MKJX1 --out run\drive_MKJX1_iter0.csv
```

```powershell
C:\ProgramData\anaconda3\python.exe run_ilc.py init --target waveforms\target_MKJX2.csv --channel EO2 --name MKJX2 --out run\drive_MKJX2_iter0.csv
```

Play `run\drive_MKJX<n>_iter0_awg.csv`, capture 64 HRES single shots (not the
scope's AVER mode — see below), then update against the measured FRF. Note the
monitor column differs: **X1 is CH3, X2 is CH4**.

```powershell
C:\ProgramData\anaconda3\python.exe run_ilc.py step --state run\drive_MKJX1.state.npz --measured "run\MKJX1_i00*.csv" --mon-col CH3 --t-offset 250 --frf run\frf_WIDE_X1.csv --frf-use 50e3 --frf-max 75e3
```

```powershell
C:\ProgramData\anaconda3\python.exe run_ilc.py step --state run\drive_MKJX2.state.npz --measured "run\MKJX2_i00*.csv" --mon-col CH4 --t-offset 250 --frf run\frf_WIDE_X2.csv --frf-use 50e3 --frf-max 75e3
```

Without `--frf` the step falls back to the parametric lead — fine for a first
iteration, but it converges to the ~7 V parametric floor, not the ~2.4 V the
measured inverse reaches. `ilc_bench.py` runs the whole
upload → capture → update cycle hands-off and takes the same `--frf` flags.

## Using it on another system, from scratch

The loop itself knows nothing about Treks or EOMs; that knowledge lives in
the channel definitions. The **`GEN`** channel is the blank one for any other
chain: unity divider, `mon_scale = 1` (target CSVs are read in the same units
the scope measures — no ×1000 monitor convention), **no calibration tables**
(asking it for gain/τ/fₙ/ζ raises instead of interpolating Trek numbers), and
no auto-loaded FRF. Nothing measured on this bench applies to a GEN session
unless it is loaded on purpose.

Bootstrapping with no target, no state, and no model:

1. **Target** — the GUI's *Build…* button generates a cosine-edged
   ramp/half-sine target CSV (or point at any `time_us,voltage_V` file).
2. **Init** — channel GEN, model *gain only*, type a conservative gain guess
   (output/drive ratio; ILC converges for `γ·g_true/g_model < 2`, so guessing
   the gain HIGH is the safe direction — the correction comes out small).
   Init writes the state file from scratch; nothing is inherited.
3. **Measure and refine** — after the first iteration, *Fit from measurement*
   replaces the guess with the identified value; step up the model ladder as
   the residual demands, or measure an FRF with `tools/sysid_make.py` +
   `tools/sysid_fit.py` (both system-agnostic: they fit whatever drive and
   response the scope columns carry).

Two things do **not** genericize automatically: `Limits` in
`eomilc/config.py` keeps its Trek-era numbers (rails, slew, 6 kV ceiling) —
review them before trusting the guard on a chain where they could bind
differently — and bench mode's instrument layer is the BK4063B + MSO-X pair;
another bench brings its own upload/capture path (the manual
step-from-captures loop works with any scope CSV).

## Calibration lives in `eomilc/config.py`

Measured 2026-08-20/21 — **update these when the hardware changes.**

| | EO1 | EO2 |
|---|---|---|
| divider (AWG → Trek in) | 0.6254 ± 0.0038 | 0.6103 ± 0.0037 |
| Trek in → monitor | **0.8926** ← see below | 1.0011 ✓ |
| fₙ at full scale (ramp fit) | 2326 Hz | 2207 Hz |
| ζ at full scale (ramp fit) | 0.206 | 0.209 |
| noise at Trek input | 144 µV rms | 624 µV rms |
| fine trim channel | none | 1:100 op-amp summer |

The fₙ/ζ rows are the large-signal ramp-fit values — they seed the first shot
(their group delay is right) but the loop itself should run on the measured FRF,
which supersedes them; see above.

**The 12% Trek mismatch.** EO1's monitor-over-input is 0.8926 where EO2's is
1.0011. Reaching a 90° rotation needs more analog-card voltage on X1 than X2,
which is what you would see if the *amplifier gain* were low (≈896 V/V) and the
monitor divider were honest — rather than the gain being right and the monitor
reading low. Those two cases are distinguished by reading the monitor at the 90°
point: equal on both channels means low gain, X1 low means a bad monitor.

If the monitor is honest, ILC closes the loop on true HV and absorbs the
difference automatically; it just shows up as more drive on X1. Worth checking
the 610E front-panel gain setting on both units and swapping the amplifiers
between channels to see whether the 12% follows the box.

**`LIMITS.load_capacitance` defaults to 200 pF and is a guess.** Measure the real
EOM + cable capacitance and set it, or the current guard means nothing.

## Things that will bite you

**Waveform names: 11 characters.** The generator stores an upload as
`<name>.bin` and the limit is on the stored name — 15 accepted, 16 not. Past it
nothing is refused: the upload lands, the right shape comes out, and then the
front panel wedges until you power cycle it. The loop appends `_i00`, so the stem
gets 7. `ilc_bench.py` reads the cap from the GUI's own `MAX_ARB_NAME`.

**Upload the `_awg.csv`, not the drive file.** With the GUI's Normalise off it
expects samples already in ±1 and clips past it; with it on it divides by the
record's own peak, which silently rescales the correction the loop just computed.
`write_bk_waveform` emits a single-column file already scaled against a fixed
±10 V, which sidesteps both.

**Time alignment.** `--t-offset` is the fixed trigger-to-waveform delay. Measure
it once and leave it alone. Re-fitted per iteration, the loop chases its own
alignment and stops converging.

**Measurement: 64 HRES single shots, averaged in software.** A single 8-bit
trace has a code worth ~40 mV on the monitor — 40 V at the EOM — and the
staircase is deterministic, so the loop learns it as if it were real error. The
scope's AVER mode does not rescue this through these scripts: `:SINGle` takes
exactly one acquisition, so an "AVER 256" capture carries one hit while claiming
the full depth, and even an honest hardware average delivers a hard 2.5 mV word
lattice (2.5 V at the EOM). The scheme that works — and what `ilc_bench.py`
does — is HRES single shots averaged in software: the 3.5 mV per-shot analog
noise dithers the lattice away and the floor lands near 0.5–1 V at the EOM.
REPORT.md §3 and §7 have the details.

**The Q filter and the FRF band.** `--f-cut` (default 5 kHz in the drivers)
confines the *parametric* update to the band the model earned; do not widen it
to speed that path up — divergence above ~5 kHz is how the resonant model
failed. With `--frf` the code filters the error at the FRF's own `f_max`
instead, deliberately: pre-filtering the error at 5 kHz in front of the
measured inverse is exactly the integration bug that once left a perfectly
repeatable 2 V rms residual sitting untouched at 5–15 kHz.

**`--zero-baseline` is off by default** and must stay off for any waveform already
moving in the first 5% of the record. MKJ is, and enabling it there subtracts
~130 V of real signal and roughly doubles the reported error.

## Provenance

`characterisation/` holds the scripts, fits and figures behind every number in
`config.py`, from 40 captures taken 2026-08-20/21. The raw captures themselves
(~260 MB) are not in the repository; point `EOM_RAMPS_DIR` at them to re-run.
`characterisation/results2.json` is the direct source of the `fn_pts` /
`zeta_pts` tables. The measured FRFs in `run/frf_WIDE_*.csv` come from the 24–25 Aug
probe campaign (`sysid_make.py` / `sysid_fit.py`, raw shots and the probe-era
fits now filed in `archive/report-era/`); REPORT.md documents the probe construction and its
verification.

## RIN and wideband noise

`eomilc/rin.py` is the analysis for out-of-loop intensity noise: segment
splicing, RIN, and the two calibrations that say whether the number means
anything. Pure numpy, so it runs on the system interpreter.

- `splice_segments` merges traces taken at different spans, different locked
  ranges and different analog pre-filters, applying each segment's own
  `|H(f)|^2` first. It **returns the overlap disagreement rather than blending
  it away**: neighbouring segments are the only cross-check the chain has, so
  where they disagree is the size of the error in the filter model, the range
  calibration and the analyser's accuracy put together. The merged trace hands
  each segment its own band up to the midpoint of the overlap, and the step at
  the join is the honest size of that disagreement. This is a different
  operation from spectrum_grab's start-frequency stitcher, which joins bands
  taken at one span and one range where the bin frequencies are shared exactly.
- `filter_response(dark_with, dark_without)` measures the pre-filter in situ
  from a pair of dark spectra, which catches the cable loading a swept
  measurement misses. Feed the result straight back to `splice_segments`.
- `rin` / `rin_from_psd` / `shot_noise_rin` / `integrate_rin`. Note the units:
  `rin` takes an AMPLITUDE density in V/rtHz (the SR760's own unit) and squares
  it; `rin_from_psd` takes V^2/Hz. Mixing them up is a factor of two in dB.
- `power_scaling_fit` separates electronics, shot noise and classical RIN by
  their scaling across an ND sweep, and returns the transimpedance gain
  `G = b/(2q)` measured end to end. **Check `shot_dominance` before treating
  that gain as a calibration** - the three terms separate only where the shot
  term is actually the biggest thing in the measurement, and with classical RIN
  dominating it is not. Measured on synthetic data: with shot peaking at 0.41 of
  the total, a 1-2% perturbation moved the extracted gain by +53%.
- `johnson_check` fits `S = S_floor + 4kT R` across a resistor set and reports
  the deviation from 1.657e-20 V^2/Hz/ohm. Band-average each resistor over its
  own flat region first (`band_average`) - 100 kohm rolls off above ~2 kHz
  against the input and cable capacitance, and averaging it over the same band
  as the 50 ohm measures the rolloff instead of the noise.

Both fits weight by 1/S by default (`relative_sigma`), because the uncertainty
on an averaged spectral density is fractional: an unweighted fit across decades
of V_DC or R is set almost entirely by the largest point.

`eomilc/scope.py` gained a one-sided auto-PSD in V^2/Hz with ENBW-correct window
normalisation, RMS-averaged over the shot stacks `ilc_bench.capture_all` already
returns. `Spectrum.asd` is its square root in V/rtHz, so a scope PSD and an
SR760 trace can be compared directly in the 30-95 kHz overlap - which is the
point, since the SR760 stops at 100 kHz and the servo bump at 150-300 kHz is
only visible to the scope. `n_indep` counts segments that did not share samples,
so `rel_err` stays honest under `noverlap`.

`ilc_bench.noise_capture` is a context manager that puts the scope into an
AC-coupled high-sensitivity configuration for a noise capture and restores the
previous per-channel coupling, V/div and offset on the way out, however the body
ended - following the `snapshot()`/`restore()` split in `bk4063b.py`. RIN needs
AC coupling at high sensitivity; the polarimetry captures need DC coupling
because the DC level is what gets inverted to an angle, so leaving the noise
setup applied would silently measure the wrong thing rather than fail.

`tests/test_rin.py` is 87 synthetic end-to-end checks and runs on either
interpreter.

## The protocol runner

`run_protocol.py` drives a declarative measurement set on the SR760 for the RIN
validation, next to `run_ilc.py`.

```
python run_protocol.py --list
python run_protocol.py --set C --dry-run
python run_protocol.py --set A1 --outdir run/protocol --v-dc 1.85
```

A set is a name, a range policy and an ordered list of traces, each carrying
span, start frequency, filter id, average count and free-text notes. Per set the
runner ranges once, pins with `pin_range()`, and verifies with `input_range()`
before every save; a range that moved marks the trace `SUSPECT` rather than
saving it clean. It settles after every change, records elapsed time,
`record_stats` (T_rec, N_indep, rel_err), the overload state from the refreshed
status cache and the full settings snapshot, and writes CSV and metadata through
`sr760`'s own writers so the files are indistinguishable from panel captures.

Built-in sets:

| set | what it is | why |
|-----|-----------|-----|
| `A1` | four resistors, 50 ohm / 2k / 10k / 100k, one shared pinned range | the fitted 4kT slope calibrates the whole voltage-noise scale - `rin.johnson_check` |
| `A2` | every input range on a 50 ohm termination | where the analyser's own floor sits per range, and confirms the 2 dB range grid |
| `A3` | one resistor across three spans | a PSD is span-independent by construction, so any span dependence would forge a false slope in a spliced trace |
| `C1` | range-step pairs, each band repeated two notches down | where the two disagree is what a segment disagreement is really measuring |
| `C` | four spans, four filters, four pinned ranges, nested from 0 Hz | the RIN segment set - splice with `rin.splice_segments` |

Each set writes one `manifest.json` whose per-segment entries carry path, span,
pinned range, filter id, v_dc, N_indep and the overload flag. `load_segments()`
reads it straight back as `rin.Segment`s, ready for `splice_segments`. **It
squares the CSV column on the way**: the file holds an amplitude density in
V/rtHz and `rin.Segment` wants a power density in V^2/Hz, and that conversion
has to happen exactly once. Suspect traces are dropped by default - a trace
whose range slipped or whose front end overloaded is not a worse measurement,
it is a different one.

`--dry-run` prints the full plan and a wall-clock estimate without touching
hardware, and imports no instrument code at all, so the planner is exercised by
`tests/test_protocol.py` on the bare system interpreter. The estimate is
`NAVG * T_rec` exactly, which is only true because the protocol preset sets
`OVLP 0` - with overlap it would understate the clock and overstate the
statistics at the same time. Some of these sets are long: 100 averages at the
24.4 Hz span is 27 minutes, which is worth seeing before committing a session.

The SR760 is built through `ilc_bench.make_analyzer` on `_shared_rm`, even for
sets that drive only the analyser. The C-phase needs the MSO-X as well - for
V_DC and for the servo bump above the SR760's 100 kHz ceiling - and that is
exactly where the mixed-VISA failure bites: a second ResourceManager half-loads
and every `open_resource` then returns `VI_ERROR_ALLOC`. `find_spectrum_grab()`
locates `sr760.py` the way `find_scope_grab()` locates `scope_grab.py`, with a
`SPECTRUM_GRAB` env override.

### V_DC from the MSO-X

```
python run_protocol.py --set C --scope-ch 3 --outdir run/protocol
```

RIN is `S_V / V_DC^2`, so the DC level squares straight into every answer: a
V_DC 5% wrong makes every RIN in the campaign 10% wrong. `--scope-ch` measures
it on the scope instead of taking it off the front panel. `--v-dc` still works
and overrides the scope.

`ilc_bench.measure_vdc` does the reading, and the awkward part is that **V_DC
has to be measured DC-coupled**, which is the opposite of the noise capture's
configuration - a channel left AC-coupled at 10 mV/div has the DC level a long
way off screen, where the scope returns `9.9E+37` rather than a voltage. So it
snapshots the channel, forces DC coupling and a scale wide enough to find the
level, and restores coupling, scale, offset and display state afterwards,
whatever happened in between.

Two passes, because one is not enough on an 8-bit scope. The coarse pass at
`--vdc-scale` per division (default 1 V) finds the level to about a division;
the fine pass then offsets it to mid screen and drops to a twentieth of the
scale, turning a level measured against 8 V of full scale into one measured
against a few hundred millivolts. Measured on a simulated 8-bit scope: 0.3%
from the coarse pass alone, 0.03% after the refinement. The fine reading is
accepted only if it agrees with the coarse one to 5% - if it does not, the fine
pass has almost certainly pushed the trace off screen, and the coarse value is
returned with a note saying so. `9.9E+37` is never returned as a voltage, and a
level that is off screen even at the coarse scale raises rather than guessing.

The level is read before the traces and again after, and both go in the
manifest with the drift between them. RIN goes as `1/V_DC^2`, so the runner
reports the drift doubled - a level that moved 2% moved every RIN in the set by
4% - and says plainly when that is more than 2%.

The scope is opened through `ilc_bench.make_scope` on `_shared_rm`, the same
ResourceManager as the analyser. This is the pairing that RM exists for.

`tests/test_protocol.py` is 54 offline checks: the timing model against the two
figures the protocol was costed with, the set definitions, and a manifest round
trip through `rin.splice_segments`.

## Interpreters

`import eomilc` no longer pulls in every submodule. `plant` needs scipy and
`scope.load` needs pandas, while `config`, `polarimetry` and `rin` are pure
numpy, so eager imports made the whole package unimportable on the system
interpreter - which has numpy but neither of the others. Submodules now load on
first use, so `from eomilc import polarimetry` works on either interpreter and
`from eomilc import plant` still raises the scipy ImportError, which is the
honest answer.
