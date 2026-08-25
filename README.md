# EOM-ILC — pre-distortion and iterative learning control for the Trek / EOM ramp drive

Corrects the tracking error of the Trek 610E → EOM chain by reshaping the drive
waveform. A model-based pre-distortion takes the first shot; iterative learning
control (ILC) then converges on the residual by dividing the measured error by
the chain's own measured frequency response H(f).

Built around the two bench programs that already exist —
[scope-grab](https://github.com/Weiss-Quantum-Computing/scope-grab) and
[BK4063B-AWG-GUI](https://github.com/Weiss-Quantum-Computing/BK4063B-AWG-GUI) —
whose instrument layers this imports rather than reimplementing, so there is no
second copy of SCPI to keep in step.

**Campaign write-up:** [REPORT.md](REPORT.md) — the 24–25 Aug 2026 report:
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

1. `sysid_make.py` builds the probe (defaults 0.4–24 kHz, 48 tones;
   `--f-hi 80e3 --tones 60` was the wide probe) and its `_awg.csv`.
2. Play it through the normal burst path; capture 64 HRES single shots.
3. `sysid_fit.py` turns the captures into `run/frf_<name>.csv` — magnitude,
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
§4–5; `simulate.py` and `make_validation_fig.py` reproduce the parametric-era
simulations that (correctly, given the fits) rejected the one-pole model.

The ramp fits did get the group delay right (τ ≈ 28 µs = 2ζ/ωₙ), which is why
the model-based first shot still works: `run_ilc.py init` seeds from the
`config.py` fit constants, and the loop's `--frf` path takes over from
iteration 1.

## Layout

| | |
|---|---|
| `eomilc/` | the library: `config` (calibration), `plant` (models + fitting), `ilc` (the loop), `outputs` (file emission), `scope` (capture reader) |
| `run_ilc.py` | manual driver — `init` / `step` / `emit-ni` |
| `ilc_bench.py` | closed-loop driver, upload → capture → update with no hands |
| `sysid_make.py` | build a Schroeder multitone probe for FRF measurement |
| `sysid_fit.py` | probe captures → `run/frf_<name>.csv` (magnitude, phase, coherence) |
| `make_target.py` | build a target from the MKJ waveform at any peak and grid |
| `simulate.py` | validate the loop off the bench |
| `characterisation/` | the 2026-08-21 analysis that produced every constant in `config.py` |
| `waveforms/` | the current targets and iteration-0 drives |
| `run/` | states, iteration drives, and the measured FRFs |
| `WORKFLOW.md` | **the bench procedure** — read this before touching hardware |
| `MKJ_FULL_NOTES.md` | what the MKJ waveform is, headroom arithmetic, DDS behaviour |
| `REPORT.md` | the campaign write-up |

Needs `numpy`, `scipy`, `pandas`, and `pyvisa` for the bench drivers. On the lab
PC that means the Anaconda interpreter, `C:\ProgramData\anaconda3\python.exe` —
it is the only one there with all four.

## Quick start

PowerShell, one command per line. `\` is not a continuation character
there, and bare `python` is the wrong interpreter (see above).

Build the target — **X1**, then **X2**:

```powershell
C:\ProgramData\anaconda3\python.exe make_target.py --channel EO1 --peak-hv 5200 --step 2 --out waveforms\target_MKJX1.csv
```

```powershell
C:\ProgramData\anaconda3\python.exe make_target.py --channel EO2 --peak-hv 5200 --step 2 --out waveforms\target_MKJX2.csv
```

Model-based first shot:

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
`zeta_pts` tables. The measured FRFs in `run/frf_*.csv` come from the 24–25 Aug
probe campaign (`sysid_make.py` / `sysid_fit.py`, raw shots in
`run/wideprobe*.npz`); REPORT.md documents the probe construction and its
verification.
