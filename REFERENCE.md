# EOM-ILC reference: the full grammar

Status of the workflow: 28 October 2026. This is where every field, flag, plan key, file name, limit and measured
number lives. The task guide is `HANDBOOK.md` ("press this, see that"); this document does not repeat a procedure the
handbook already gives in task form, and says "the handbook, chapter N, walks through this" instead.

It merges the two earlier workflow documents (the bench measurement and batch runner, and the panel's click-by-click
manual) and carries the calibration, RIN and protocol-runner sections of the old README. Where those sources disagreed with
each other or with the code, the code was followed. Finished campaigns and design notes are in `docs/history/`. The
experiment traps neutral atoms; what the loop finally has to get right is the light they are given, not the electrical
signal.

Conventions: volts "at the EOM" are thousands of volts (the monitor reads 1 V per kV); "AWG volts" are at the generator
output; `<stem>` is a campaign's short name; paths are relative to the EOM-ILC folder unless they start with a drive
letter. Every Python command uses `C:\ProgramData\anaconda3\python.exe`.

## Contents

1. [The bench measurement](#1-the-bench-measurement)
2. [The loop from the command line](#2-the-loop-from-the-command-line)
3. [The inverse model ladder and Measure FRF](#3-the-inverse-model-ladder-and-measure-frf)
4. [Reading the plots](#4-reading-the-plots)
5. [Every field of the panel](#5-every-field-of-the-panel)
6. [The batch runner and the plan file](#6-the-batch-runner-and-the-plan-file)
7. [Spin-echo records and card files](#7-spin-echo-records-and-card-files)
8. [The optical close by hand](#8-the-optical-close-by-hand)
9. [Files every step writes](#9-files-every-step-writes)
10. [Calibration, and using the loop on another system](#10-calibration-and-using-the-loop-on-another-system)
11. [RIN and the SR760 protocol runner](#11-rin-and-the-sr760-protocol-runner)
12. [Tests](#12-tests)

---

## 1. The bench measurement

Everything below applies to the panel, `ilc_bench.py`, the batch and the manual loop alike.

### 1.1 Waveform names: 11 characters

The 4063B stores an upload as `<name>.bin`, and the limit is on the stored name: 15 characters are accepted, 16 are not,
so a typed name gets 11 (`MAX_ARB_NAME` in the AWG GUI). Past it nothing is refused. The upload lands, the right shape
comes out, and then the front panel wedges until the generator is power-cycled.

The loop names its uploads `<stem>_iNN`, so a stem gets 7 characters (the panel enforces it; pair mode appends `X1` / `X2`
and so allows 5, handbook section 3.4). `MKJX1` gives `MKJX1_i00`, nine characters, with two to spare. `ilc_bench.py`
reads the cap from the GUI's own `MAX_ARB_NAME` and refuses anything longer. The batch's slot names (`BX1A` ...) and park
names (`PARK1`, `PARK2`) are far inside it.

### 1.2 The generator and scope settings the loop assumes

The panel's **Auto-set instruments** writes these (handbook section 2.1). By hand, on the channel driving the EOM:

| AWG GUI setting | value | why |
|---|---|---|
| Wave type | ARB | |
| Clock | **DDS** | TrueArb blocks `BTWV`, and the bench needs the triggered burst (section 1.3) |
| Ampl | **20.00 Vpp** | with Offset 0, +-1 in the file is exactly +-10 V out: the fixed mapping every drive file assumes |
| Offset | 0 V | |
| Load | HZ | the Trek input is 20 kOhm; declaring 50 Ohm halves the real output |
| Burst | ON, NCYC 1 cycle, trigger EXT | fired by the external pulse generator |
| Normalise on upload | **OFF** | section 1.10 |

Scope Grab (the capture program), if captures are taken by hand:

- Trigger source and level to match whatever fires the shot.
- Timebase so the whole ramp plus its settle is on screen. For the 10.602 ms MKJ ramp, 1.5 ms/div gave a 15 ms window.
  Auto-set uses a window 1.3 times the record with the record starting at the left edge.
- **Acquisition HRES** (section 1.4).
- Transfer points: **leave it at max**. On the 24 Aug 2026 captures the scope returned 93750 points across a 15 ms window,
  160 ns per sample, which `scope.resample` boxcars by 12 down to the 2 us grid: a free sqrt(12) noise reduction. A
  smaller number reduces that. 20000 is the compromise if the 6.6 MB files become a nuisance: 0.75 us per sample, still
  above the 1 us threshold where the boxcar engages, at a factor 3 rather than 12. `ilc_bench.py --points` defaults to at
  least 2.2 points per record sample (`scope_points_for`).
- A channel name on the monitor channel, so the CSV column is self-describing.

### 1.3 Why DDS and not TrueArb

The AWG GUI documents from measurement that `BTWV STATE,ON` and `SWWV STATE,ON` both read back OFF while the clock is
TrueArb: a literal sample grid and a hardware-triggered single shot cannot be had on the same channel. The bench needs the
triggered shot, so it runs **DDS + Burst**, fired externally. DDS resamples the stored record into one period, so the
2 us grid is not literal: the period comes from `FRQ`, not from points times sample rate. Check the played length and the
edge timing on the scope once before trusting a new record length. The generator reconstructs smoothly between the stored
samples (answered 25 Aug 2026 from the wide-probe capture: `docs/history/2026-08_MKJ_FULL_NOTES.md`), so the 2 us record
does not step at the EOM.

### 1.4 The standard scheme: averaged HRES single shots

A digitized hardware average bottoms out on a hard 2.5 mV word lattice (12 bit, an instrument cap: 1024 averages do not
refine it), and a lattice is a systematic error the loop learns as real. A single 8-bit trace is worse: a code is worth
about 40 mV on the monitor, 40 V at the EOM, and the staircase is deterministic. The scheme that measures finer uses the
fact that a **single HRES shot carries the same lattice plus 3.5 mV rms of per-shot analog noise** (measured 24 Aug 2026,
slow regions of the real ramp). Noise larger than the lattice step is the dither condition, so the mean of M separate shots
walks off the lattice and beats down the noise:

| | lattice step | shot noise | floor at the EOM |
|---|---|---|---|
| 1 HRES shot | 2.51 mV | 3.5 mV | ~4 V |
| mean of 8 | 0.31 mV | 1.2 mV | ~1.3 V |
| mean of 16 | 0.157 mV (the WORD floor) | 0.9 mV | ~1 V |
| **mean of 64 (the campaign standard)** | **0.157 mV** | **~0.4 mV** | **~0.5-1 V** |
| digitized AVER-256, for comparison | 2.51 mV systematic | ~0.2 mV | ~2.5 V |

Sixteen shots are a usable quick check (~1 V floor; the panel refuses fewer than 16). Beyond M of about 64 the returns
diminish: 60 Hz pickup and drift take over. By hand: scope on HRES, full window (1.5 ms/div, position +5.3 ms for the MKJ
ramp), Scope Grab **Sequence** of 64 runs at interval 0 with a prefix such as `ilch_i01`, then
`run_ilc.py step --measured "...ilch_i01*.csv"`: the glob matches every file and `step` averages them on the grid.

Time per 64-shot measurement depends on the trigger rate: ~25 s at the 20 Hz trigger of the August campaign, ~17 s at
3.7 Hz, ~65 s at 1.0104 Hz. The batch's 36 s per iteration at 3.7 Hz (13 Oct 2026, two channels read per shot) is where
`timing.read_s_per_channel` 0.12 s comes from (section 6.5).

**The offset dither.** Every bench capture steps the scope offset of the monitor channel (and any auxiliary channel: the PD,
the drive readback in Measure FRF) across three ADC codes over the shots and puts it back afterwards. One code would
average the converter's pattern within a code; three also average the code-to-code variation, at 21 phases per code for 64
shots. Why: the MSO-X's 8-bit converter carries a fixed error pattern of about 3.4 mV peak to peak per 40.25 mV code at
1 V/div. A ramp sweeps it at slope divided by code into 15-100 kHz "error" that averaging keeps exactly, because it is a
function of voltage, and the loop then corrects it into real ripple at the EOM. That was the "Trek oscillation" of
P92PX1B/C on 2 Sep 2026, about 1 V rms at the corners, which the flat shot never had. With every shot at a different code
phase the mean takes the pattern's mean. The log names the code size and warns if the scope rounds the offset coarser than
a code. Another scope has another code: set the environment variable `EOMILC_ADC_CODE_PER_VDIV` to its code as a fraction
of the V/div setting (fold a flat shot's residual on monitor voltage to find it), or to 0 to switch the dither off. The
Diagnostics -> code fold view (section 4) reads the result.

### 1.5 AVER, retired

The scheme before 24 Aug 2026, kept because the free-running-average trap still bites and its numbers show where the hardware
path ceilings out. The panel's bench mode refuses AVER.

- GRAB with the scope on AVER acquired a fresh block of exactly 256 sweeps (`:DIGitize`), 12.8 s at 20 Hz; a build that came
  back short said so in the log (check `averages taken` in the `.txt` sidecar). The "Wait for trigger" field is only a stall
  limit. `:SINGle` takes exactly one acquisition, so an "AVER 256" capture through the other scripts carries one hit while
  claiming the full depth.
- Never use the scope's free-running average for ILC: under plain RUN it is a running average (weight 1/256 per sweep, 12.8 s
  time constant at 20 Hz), so after a new drive is uploaded the screen carries the previous iteration for a minute or more (a
  full-scale change takes ~128 s to fade, measured).
- An averaged record reads back with 7680 points (1.95 us spacing on the 15 ms window, on the 2 us grid). The display and the old
  BYTE readback are 8 bit (40 mV per code at 1 V/div, 40 V at the EOM); since 24 Aug Scope Grab reads WORD, 157 uV steps for an
  averaged record. The first real averaged capture (24 Aug, 19:48) stepped at 2.513 mV, 2.5 V at the EOM, against 40.2 V for the
  single-shot file two hours earlier: a hardware-average floor of ~2.5 V (the original documentation hoped for 5 V), which is
  exactly the lattice the HRES scheme dithers away.

### 1.6 The model band: keep the Q filter at 5 kHz

Measured 24 Aug 2026 with the drive's own grass as a broadband probe (coherence 0.93-0.97): above ~6 kHz the real chain
passes 4-8 times more than the second-order model predicts. The inverse-model update then diverges in that band, with a
contraction factor of 2.6 at 12 kHz, which showed up as drive grass tripling per iteration (0.4 -> 46 -> 130 mV rms) while
every capture looked clean. The 20 kHz Q filter the package shipped with is too wide for this bench.

Run `step` with `--f-cut 5e3` once; it persists in the state. The update low-passes the outgoing drive too, so the first
5 kHz step also strips any grass an earlier iteration injected (46 -> 0.8 mV rms when it was applied). The real correction
lives below 3 kHz and is untouched. The drivers default to 5 kHz (`run_ilc.py init` and `ilc_bench.py`), as does the panel;
do not widen it to speed the parametric path up.

This confinement applies to the **parametric** update only. With `--frf` the code filters the error at the FRF's own band
edge (`f_max`) instead: the measured inverse is trusted across its whole measured band, and pre-filtering at 5 kHz in front
of it is the integration bug that once left a repeatable 2 V rms residual untouched at 5-15 kHz. The panel's `f_cut` field
is therefore greyed out in measured-FRF mode.

### 1.7 Sequence prefixes are glob prefixes

`step --measured` (and the panel's Captures field) averages **every** file the glob matches. A sequence named `ilc_i01`
once swept in two earlier one-off captures and a 200 us/div zoomed capture that shared the prefix; the zoomed record,
extrapolated flat over 81 % of the grid, manufactured 172 V of fake error out of a real 26 V. `step` now refuses any
capture that does not span the whole waveform and lists every file it averages: read that list. Give each sequence a prefix
nothing else uses, and give zoomed inspection grabs prefixes the ILC globs can never match.

### 1.8 The burst-entry transient: the loop cannot fix the idle level

Measured 24 Aug 2026 from the pre-trigger portions of the 64-shot sequences: even with the drive files pinned to zero the
chain idles at **-9 V (X1) and -41 V (X2) at the EOM** between bursts, and every burst opens with a ~150 us relaxation from
that level. The loop cannot see it, because the initial state is set before the record starts, and it shows as a stuck error
peak at t = 0 (~40 V on X2) that no iteration reduces. The offsets decompose into the AWG's own zero-code error (-12 mV on
CH1, -40 mV on CH2 at 20 Vpp, within the generator's spec) plus, on X2 only, ~-16 mV at the Trek input that the AWG never
sees: the 1:100 summer's fine port, consistent with something holding about -1.6 V there. Two fixes, either or both: trim
each channel's OFST to cancel its measured idle (about +12 mV / +40 mV; relax the OFST = 0 check tolerance accordingly), or
find what is parked on X2's fine input. Re-measure the idle from any sequence's pre-trigger data after either change.

Between bursts the AWG holds the record's **first sample**, so sample 0 sets the standing level on the EOM for the whole
gap. The rule changed on 24 Aug: forcing it to file-zero fought the loop, because the chain's own idle offsets parked the
EOMs at -9/-41 V anyway and the entry transient could never converge. The loop now sets the first sample **freely within a
+-100 mV cap at the AWG** (`Limits.idle_awg`), letting it trim the chain to a true-zero idle; the limit check prints the idle
on every drive, and the last sample carries the same clamp. A drive file from anywhere else should keep its first line within
+-100 mV before the burst idles on it. `ilc_bench.py` accepts OFST up to +-60 mV for the idle trim. The idle trims the
campaigns learned sit at X1 +20 to +26 mV and X2 +78 to +81 mV at the AWG, which is why `tools\awg_park.py` can fall back
to 0 V and stay inside the cap; a park normally uses the drive's own first sample.

### 1.9 The trigger offset

Measured 2026-08-24 on this bench: **`--t-offset 0`**. Cross-correlating the captured CH1 and CH2 against the drive records
that produced them puts the waveform start at -1.4 us on both, which on a 2 us grid is zero: the burst delay is 1.26 us and
the scope triggers off the same external pulse. Measure once and leave it alone. Re-fitted per iteration, the loop chases its
own alignment and stops converging. Re-measure only if the trigger wiring changes, with
`scope.measure_t_offset(t, ch1, u_drive, 2e-6)`. Do not use `find_trigger_offset` for this: it returns a mid-ramp 50 %
crossing, and MKJ leaves zero so slowly that the two differ by more than a millisecond. `ilc_bench.py`, the panel's bench loop and the batch
cross-check the alignment against the drive they uploaded and refuse if the measured start is more than 10 us from the
t-offset.

`--zero-baseline` (the panel's *zero baseline* tick) subtracts the mean of the first 5 % of the record from the measurement.
It is off by default and must stay off for any waveform already moving in the first 5 % of the record. MKJ is, and enabling
it there subtracts ~130 V of real signal and roughly doubles the reported error.

### 1.10 Uploading by hand: two traps, one file

`Awg.upload_arb(..., normalize=True)` divides the samples by their own peak. That is right for a one-off waveform and wrong
for ILC: each iteration's drive has a slightly different peak, and re-normalising every round pins that peak to whatever
`BSWV AMP` says, which silently rescales the correction the loop just computed. The error stops falling and you go looking
for plant drift that is not there. Turning Normalise **off** means the GUI expects samples already in +-1 and clips anything
past it: a drive file written in volts peaks at 9.3, so 4749 of its 5301 samples would come out flat-topped. And a two-column
`time_us,voltage_V` file needs the GUI's column picker to choose the second column; `time_us` was not in its list of time-axis
names until 2026-08-24, so before that fix it picked column 0 and uploaded the **time axis**, which normalises to a clean
ramp and looks like a plausible arb.

The `_awg.csv` file sidesteps all three: one column, already scaled against the fixed +-10 V (`write_bk_waveform`), no picker.
Upload that one with Normalise off. (A 0-9.3 V unipolar drive against the +-10 V mapping uses about half the DAC codes, 0.16 V
per LSB at the EOM instead of 0.08; both sit far below the 5 V loop floor of the time, so it is not worth chasing.) The
panel's *Upload* button and the bench loop do this for you (section 5.6).

---

## 2. The loop from the command line

Two ways: by hand with the existing GUIs, or hands-off with `ilc_bench.py`. The manual route is worth doing once to watch
what happens. The state file the panel keeps is the same one the command line reads, so panel and command-line steps
interleave freely. The raw captures stay out of the repository (`scope_data\EOM ramps day 4` and its siblings, outside
`run\`).

### 2.1 Building a target

```
C:\ProgramData\anaconda3\python.exe tools\make_target.py --channel EO1 --peak-hv 5200 --step 2 --out waveforms\target_MKJX1.csv
```

(and `--channel EO2 ... target_MKJX2.csv`). A target is in volts **at the EOM**, which makes two channels with different
Trek gains directly comparable: ask both for 5200 V and each gets the drive its own chain needs, here 9.312 V on X1 and
8.558 V on X2, leaving 6.9 % and 14.4 % headroom against the +-10 V full scale. Decimating `MKJ_full` 20:1 from its native
0.1 us to the 2 us grid departs from the source by 3.2 V peak and 1.3 V rms at the EOM (0.06 % of full scale). That was
negligible against the old ~5 V hardware-average floor; against the ~0.5-1 V HRES floor it is no longer free, but the loop
tracks the decimated target exactly, so it is a departure from the 0.1 us source shape and not an error the loop sees. The
grid arithmetic is in `docs/history/2026-08_MKJ_FULL_NOTES.md`. The other target builders are `tools\make_ramp_target.py`,
`tools\make_soft_split.py` (a series split with a soft handover), `tools\make_v90_targets.py` (rescales the 5200 V ramp
targets of `waveforms\` to each channel's measured 90-degree point) and `tools\make_echo_target.py` (section 7).

### 2.2 `run_ilc.py`

| subcommand | flags (default) |
|---|---|
| `init` | `--target` (required), `--channel` (required; EO1, EO2 or GEN), `--out`, `--name` (stem; 11 characters including `_i00`), `--model` (`resonant`; which calibration table seeds the model), `--gain`, `--fn`, `--zeta` (overrides), `--tau` (force a one-pole model, microseconds; diagnostic only, makes the loop diverge at fn), `--gamma` (0.6), `--f-cut` (5e3), `--awg-dir` (the AWG GUI's `Waveforms\`, or `BK4063B_WAVEFORMS`), `--full-scale` (10.0; AMP must be twice this with OFST 0), `--t-offset` (0.0 us), `--shot-gain`, `--model-first-shot`, `--seed-drive DRIVE.CSV`, `--overwrite-state` |
| `step` | `--state` (required), `--measured` (glob of scope CSVs, required), `--mon-col` (CH3), `--out`, `--t-offset` (default: the state's), `--frf FRF.CSV`, `--frf-use` (15e3), `--frf-max` (22e3), `--f-cut` (override, persists), `--awg-dir`, `--refit`, `--model` (for `--refit`), `--zero-baseline` / `--no-zero-baseline` (off), `--force` |
| `emit-ni` | `--state` (required), `--channel`, `--bits` (16), `--dc-offset` (0.0, standing EO-zero offset in summing-node volts): splits the converged drive into coarse and fine channel files (`ni_<ch>_coarse.csv`, `ni_<ch>_fine.csv` on EO2, which has the 1:100 summer; `ni_<ch>_single.csv` elsewhere) |

Notes on the flags:

- The first shot is a **flat conversion** since 26 Aug 2026: the target divided by the first-shot gain, no pre-distortion,
  so the first measurement shows the chain's raw response (~2.4 % tracking error on the Trek ramps) before any correction.
  `--model-first-shot` restores the pre-distorted seed. `--shot-gain` is deliberately a separate knob from the model gain:
  tuning the correction model must not rescale what iteration 0 plays. `--seed-drive` plays a given drive (AWG volts, the
  target's point count and grid) as iteration 0 sample for sample, for an edited converged drive against its edited target;
  it is recorded in the state as `seed_path`.
- `init` refuses to overwrite an existing state; `--overwrite-state` discards it. A fresh run once destroyed a
  four-iteration manual state without asking.
- `step` writes `<out>` (default `drive_<channel>_iter<n+1>.csv`) and the upload-ready `<name>_iNN.csv` into `--awg-dir`.
  It refuses to write a drive that violates a hard limit unless `--force`; read why before forcing.
- Without `--frf` the update falls back to the parametric lead: fine for a first iteration, but it converges to the ~7 V
  parametric floor, not the ~2.4 V the measured inverse reaches (section 3.3).
- The state carries the target, plant, current drive, gamma, `f_cut`, t-offset, error history and the model record, so
  `step` is a single command.

### 2.3 A manual round

Per channel: `init` writes two files, then you play, grab and step.

| file | where | what |
|---|---|---|
| `drive_MKJX<n>_iter0.csv` | `run\` | `time_us,voltage_V`, the loop's own record |
| `MKJX<n>_i00.csv` | the AWG GUI's `Waveforms\` | the one to upload: single column, normalised to +-1 |

1. AWG GUI: **Load waveform** -> the `_awg` file, name it `MKJX<n>_i00`, confirm Normalise is unticked, **Upload** to that
   channel. The upload file goes straight into the generator's library (`C:\Users\mzd416\Desktop\Python Projects\BK4063B-AWG-GUI\Waveforms`)
   named exactly as the AWG waveform it becomes, so it appears in the GUI's memory list and previews like any stored waveform.
   Change the folder with `--awg-dir` or `BK4063B_WAVEFORMS`.
2. Scope Grab: **one grab serves both channels**. The capture holds all four scope channels, so one prefix (`ilc_i00`, not
   per channel) and one GRAB serve both `step` commands, which differ only in `--mon-col`.
3. `run_ilc.py step --state run\drive_MKJX1.state.npz --measured "<capture folder>\ilc_i00*.csv" --mon-col CH3 --t-offset 0`,
   and the same for X2 with `CH4`. Each writes `run\drive_MKJX<n>_iter1.csv` and its `_awg.csv` pair.
4. Repeat from 1 with the new file; three or four rounds.

The panel does the same through **Manual step...** (section 5.6): Upload `<stem>_iNN` -> grab -> Captures box -> Step.

**Which scope channel is which** (the August wiring every characterisation script uses: `(1, 3, 'X1')`, `(2, 4, 'X2')` in
`characterisation/analysis2.py` and five others): X1 is AWG CH1, scope drive CH1, monitor **CH3**, `--mon-col CH3`; X2 is
AWG CH2, scope drive CH2, monitor **CH4**. The scope's own CH3 name said "Monitor from Trek X2" in the August captures,
which is wrong: CH3 carries X1. Fix the channel label in Scope Grab before capturing, or the CSV column contradicts the data
in it. Since 5 October the panel and the batch take the numbers from the Scope tab's role map (section 5.5): the monitors
stay on CH3 and CH4, but CH1 is now the PD and CH2 the X1 command readback, and X2 has no readback.

### 2.4 `ilc_bench.py`: the automatic loop

Scope in HRES (never AVER: `:SINGle` takes one hit of an average), full window, outputs on, both other GUIs closed. Continue
from where the manual loop left off, because everything (target, drive, plant, gamma, `f_cut`, t-offset) comes from the
state and the state is saved back every iteration:

```
C:\ProgramData\anaconda3\python.exe ilc_bench.py --resume run\drive_MKJX1.state.npz --awg-ch 1 --scope-ch 3 --iterations 2
```

or from scratch with `--channel EO1 --target waveforms\target_MKJX1.csv --name MKJX1 --awg-ch 1 --scope-ch 3 --t-offset 0
--iterations 3`. Each iteration: upload (fixed +-10 V mapping, never normalised), **`--repeats` 64** HRES singles averaged in
software, update, save the state; drives land in `run\` and previewable copies in the AWG library.

| flag (default) | meaning |
|---|---|
| `--channel`, `--resume STATE`, `--name`, `--target` | a fresh run needs `--channel`, `--target` and `--t-offset`; `--resume` supplies them |
| `--awg-ch` (1), `--scope-ch` (3), `--drive-scope-ch` | AWG channel, monitor channel, drive channel for the alignment check (default: the AWG channel's number) |
| `--iterations` (4), `--repeats` (64), `--wait` (30 s) | updates to run; shots per measurement; trigger stall limit |
| `--t-offset` (none), `--full-scale` (10.0), `--points`, `--sample-rate` | alignment; DAC mapping; scope transfer points; expected SRATE (only meaningful under TrueArb) |
| `--model` (`resonant`), `--gamma` (0.6), `--f-cut` (5e3) | the parametric model and its band |
| `--frf FRF.CSV`, `--frf-use` (15e3), `--frf-max` (22e3) | the measured inverse (section 3.3); the campaign ended at 50e3 / 75e3 |
| `--outdir` (`run`), `--scope-grab`, `--awg-gui`, `--awg-dir` | where states go; the sibling instrument layers (found beside this repo, or `AWG_GUI`) |
| `--overwrite-state`, `--skip-checks` | a fresh run replaces an existing state; upload without verifying the channel (do not) |
| `--outputs-off-at-end` | switch the driven output OFF at the end instead of parking it; asks for a typed `OFF` first; only with the stage it feeds bypassed |

It deliberately does **not** set amplitude, offset, load, clock or output state: set those in the GUI (or Auto-set) and
confirm on the monitor. Before the first upload it verifies the channel against the drive file's assumptions and refuses on a
mismatch. Like the panel, it ends parked (a flat record at the idle, output left ON).

---

## 3. The inverse model ladder and Measure FRF

### 3.1 Why the inverse is measured

Changed on 24-25 Aug 2026. Schroeder-multitone probes of both chains at 2 V found **no resonance**: a smooth rolloff with the
phase easing to about -140 degrees. A single pole cannot lag past -90 degrees (the chain is at 82 degrees already at the
pole's own 5.7 kHz corner) and the second-order peak is not in the probe data, so the production loop divides the error by the
measured H(f), tapered to zero past the measured band. The wide-probe FRFs ship in `run\frf_WIDE_X1.csv` and
`run\frf_WIDE_X2.csv`, the only data files of `run\` tracked in git. The loop converged at ~0.4 per iteration wherever the taper
was open.

The parametric era ended because ILC contracts only where `|1 - gamma * L * P_true| < 1`, and above ~6 kHz the real chain
passes 4-8 times more than the large-signal ramp fits predict (zeta ~0.21, fn 2.2-3.0 kHz; `characterisation/`, 20-21 Aug): the
drive grass tripled every iteration. `f_cut` 5 kHz froze that but left a repeatable +-3-4 V residual at 3-6 kHz, and a wrong
inverse is not fixed by iterating on it. The probes showed the fitted resonance was a large-signal effect of edges near the
Trek's slew limit, absent at probe level; the fits did get the group delay right (tau ~28 us = 2 zeta / omega_n), which is why
the model-based first shot worked. Contraction numbers per band: `docs/history/2026-08_report/REPORT.md` sections 4-5;
`simulation/simulate.py` and `simulation/make_validation_fig.py` reproduce the simulations that rejected the one-pole model.

### 3.2 The ladder

The **Model** box selects what the update divides the error by:

| rung | parameters | band knob |
|---|---|---|
| gain only (0th) | gain | `f_cut` |
| one pole (1st) | gain, tau | `f_cut` |
| second order | gain, fn, zeta | `f_cut` |
| third order | gain, fn, zeta, tau | `f_cut` |
| measured FRF | the file | `full strength to` / `taper to zero at` |

Parameters are typed, or come from **Replace values with a fit...**, which offers two fits (section 5.4): *Fit to FRF* and
*Fit to measurement*. Nothing is filled from a table any more: the August ramp-fit tables encoded a resonance the FRFs
contradicted, and they were Trek-only (`eomilc/config.py` still carries them for the command-line drivers' seed).

**Third order** is the second-order section times a real pole, the general third-order all-pole form: the zeta above 1 that
the X1 FRFs want makes the section two real poles, and tau is the third. It is worth a rung on this bench: fitted to
`frf_EOX1FRF.csv` up to 60 kHz it takes the phase residual from 11.3 to 5.3 degrees and moves the contraction boundary from
71.6 kHz to 106.8 kHz, which is what decides how high `f_cut` may go. Its parameters are not unique once zeta > 1 (three
real poles split between the section and tau several ways), so compare fits by the residual and the boundary, not by the
fn / zeta / tau triple.

The frequency-ranged FRF cases are one file stepped through different tapers (10/15 -> 24/36 -> 50/75 kHz). The amplitude
family (0.5, 2 and 6 V probes) is compared with **Show FRF** and a semicolon list (section 5.4).

### 3.3 The measured inverse: recipe and numbers

Per channel:

1. `tools\sysid_make.py --peak 2.0 --name <NAME>` builds the probe (defaults 0.4-24 kHz, 48 tones; the wide probe was
   `--f-hi 80e3 --tones 60`) and its `_awg.csv`; upload it with Normalise OFF and capture 64 HRES single shots.
2. `tools\sysid_fit.py` turns the captures into `run\frf_<NAME>.csv` with the channel's drive and monitor columns. The columns
   are `f_Hz, H_mag, H_phase_deg, coherence`, and H is formed from the scope's *measured* drive channel, so drive-side
   rolloff cancels. Tones with coherence below 0.9 are dropped on load.
3. Iterate with the inverse:
   `ilc_bench.py --resume <state> --frf run\frf_<NAME>.csv --frf-use 20e3 --frf-max 24e3`. The flags are in both
   `run_ilc.py step` and `ilc_bench.py`; `--frf-use` sets full strength and `--frf-max` the taper end.
4. Budget 2-3 warm-up iterations at every session start. The chain drifts on hour scales (a converged drive re-measured up
   to 32 V off at the next session's start, shape not gain).

Landed 25 Aug 2026, after the 80 kHz probe (both chains coherent at every tone) and the extended-band iterations
(`--frf run\frf_WIDE_<ch>.csv --frf-use 50e3 --frf-max 75e3`): **X1 and X2 both at 2.4 V peak and 0.33-0.48 V rms on the
5.2 kV ramp, 0.046 % peak and 0.006-0.009 % rms.** The "above 24 kHz distortion" turned out to be mostly unmeasured linear
response; the true floor is the above-80 kHz remainder (0.09-0.16 V rms) plus measurement noise. Uncorrected, the chain was
at 2.4 %: the loop is worth 53 times on peak and more than 100 times on rms.

**Taper rules.** Never taper past the measured band. Above the last coherent tone the inverse holds |H| flat, so the
correction there divides by an extrapolation and the loop builds drive at frequencies nothing was measured at. Since
30 Aug 2026, `f_use` at or above the file's top coherent tone is refused by Init, Step and the bench loop alike, and an
`f_max` past it is called out in the log. On the FRF rung `f_cut` is ignored: the taper is the band edge, and `gamma` still
applies. The history of an FRF-driven campaign is tagged `FRF <use>-<max>k`.

### 3.4 Measure FRF...

The panel's automation of the probe (sysid_make plus sysid_fit with an adjustable band). The dialog takes **probe peak** (V
at the AWG, capped at full scale), `f lo` / `f hi`, the tone count, and a name of at most 11 characters.

- **Construction.** A Schroeder multitone on the loaded session's own record, tones on integer FFT bins (leak-free analysis,
  cosine-tapered ends), uploaded through the bench machinery: setup checks with no skip, an overwrite question, an output-ON
  confirmation (prompts in that order after the demand check). Each shot reads both the drive (the CmdXn scope channel) and
  the monitor from the same acquisition, so H = monitor / drive is immune to AWG flatness; H is averaged over the bench
  panel's `repeats` and the shot-to-shot scatter of H is the coherence. It refuses if the map has no drive readback.
  The shots are offset-dithered like the loop's captures (drive, monitor and any PD channel), because the probe is the same
  waveform every shot: the converter's per-code pattern would otherwise repeat identically, survive the average of H, read as
  coherent, and at the top of the band, where the monitor tone is a tenth of a millivolt, limit H.
- **Output.** `run\frf_<name>.csv`; the **FRF** field points at it and the FRF plot is drawn. Switching the Model to
  *measured FRF* stays your call (measuring is not consenting). The run ends parked: flat at the session drive's idle, output
  ON, and a shortened record's FRQ put back under that flat park.
- **Band regimes**, picked automatically, all keeping the top tone at or below half the probe grid's Nyquist (at least 4
  samples per period; tones riding just under Nyquist lose coherence to DAC images and resampling, and dense records are
  free). `f hi` is free up to a 5 MHz sanity ceiling: the 2 us grid is the analog card's lookup-table constraint, not the
  bench's, and in DDS mode the 4063B resamples the whole stored record into one FRQ period, so a denser record probes higher
  with nothing else changing.
  - up to 125 kHz the probe reuses the session record;
  - up to what the probe record carries (`FRF_MAX_PTS` = 16384 points in the panel, about 386 kHz) a *denser* record of the
    same duration, instruments untouched;
  - beyond that a *shorter* record: FRQ and the scope window are changed for the run (only from an output-off start) and
    restored at the end, and the frequency bins coarsen to 1/record, so raise `f lo`.
  The 16384 is the probe record's own cap, apart from the generator's arb memory (8 Mpts, measured 8 Oct 2026; `BK4063B_MAX_PTS`
  overrides if a dense upload is refused). The scope is asked for a readout dense enough for the band (unprompted it hands
  over 5000 points whatever the window, measured). The verticals are set for the probe (bipolar around zero, drive +-peak,
  monitor +-2 times the flat gain for resonant-peaking headroom; the ramp verticals leave the probe in 1-2 of 8 divisions and
  burn bits) and restored with FRQ and the window. The first shot verifies the record carries the band and refuses if not.
- **Ramped option.** Instead of a multitone around zero, the record ramps to *hold V*, holds it for *hold ms* with the tones
  on top, and comes back, all inside the burst, so the EOMs are never parked at a standing kV. It uses the session's own
  record (FRQ, burst and scope window untouched). Tones sit on integer bins of the *hold*, not the record, so the resolution
  is 1/hold and `f hi` is capped by the session grid; the analysis window is the hold alone. This is the measurement for the
  corners: the EOM capacitance is voltage dependent, and at the ramp corners the chain answers a drive ripple with several
  times what the zero-centred FRF predicts, which is what makes the loop over-correct there. Compare a ramped FRF at the
  ramp's own level against `frf_*` taken around zero.
- **Demand.** Before running, the dialog computes the probe's flat-gain demand (peak EOM slew and the capacitive current it
  implies). Past the 610E's 20 V/us and 2 mA specs it asks for confirmation with the numbers: the Trek cannot exceed its own
  limits, so a hot probe costs time spent current-limiting during the burst (distortion, dead coherence above the band). The
  validated 24 kHz probes carried 10-30 mA on the same measure; halving the peak halves the demand; a flat-gain output past
  6 kV is refused outright. Use 2 V for bands to ~100 kHz and 0.5-1 V above that (demand scales with peak times `f hi`). Past
  ~100 kHz the Trek rolls off hard, so expect low coherence there at low amplitudes.

### 3.5 Measure an FRF and drive with it

1. Load the session, close the AWG GUI and Scope Grab, burst trigger firing, Auto-set with the output off (Measure FRF
   re-checks the setup and has no skip). **Measure FRF...** as above.
2. On the FRF tab find where coherence dies. Set **taper to zero at** (`f_max`) at or below the last coherent tone and
   **full strength to** (`f_use`) at about two thirds of it.
3. **Model -> measured FRF (nonparametric)**.
4. Step or run the bench loop. Judge on the Error spectrum: the residual should fall inside the widened taper band. Error
   growing right of the taper means pull `f_max` in. Budget the usual 2-3 warm-up iterations. A new stem plus the old one in
   Compare shows what the wider band bought.

### 3.6 What the log says at every measurement

Every measurement after the first logs these lines. They are the diagnostics to read before changing a band.

- **Model check.** The update that made this drive, pushed through the model's own forward operator, against the monitor
  change it actually produced, per band, in the stretch of record where that band's update was strongest. Example: *"answered
  the last update at 1.1x (0-5 kHz, corr +0.97), 4.4x (20-40 kHz, corr +0.86)"*. When |1 - gamma * response| reaches 1 in a
  band: *"the update does not contract there; refit the model in that band, or bring the band edge under 20 kHz"*. A
  correlation under 0.3 is reported as the monitor not responding to what was pushed at all. This is the line that would have
  fired at iteration 1 of P92PX1D: a real 0.25 mV feature at the corners, answered at four times the small-signal model and
  over-corrected into the ripple that campaign built.
- **Noise floor.** The error spectrum against the standard error of the shot average, in 5 kHz bands up to the band edge
  (`f_cut`, or the taper's end). *"Nothing left to learn above 38 kHz"* means the error there is under 2 times the
  measurement's own scatter: the update it drives is the inverse's gain times noise (40-65 times at 50-70 kHz on X1 with the
  second-order lead), laid into the drive fresh every iteration, so it never averages away. Lower the band edge to that
  frequency, or stop. P92PX1B (2 Sep): the peak error stopped falling at iteration 5, and iterations 6-20 put 26 mV rms of
  50-70 kHz into the drive and 1.4 V rms of ripple onto the EOM that the flat shot never had.
- **Noise lines.** The same line lists narrow peaks in the shot-to-shot scatter at five times the level around them (a
  free-running oscillation in the chain, not the ramp). The update can only chase those; the line gives the `notch Hz` text
  that makes it blind there, and says "notched" once it is.
- **PLATEAU.** The noise-floor line cannot see ripple the loop created from amplified scatter: it is repeatable, reads as
  genuine error, and the loop keeps "correcting" it while injecting fresh scatter at the same gain. That shows in the
  history. The line fires when the best peak error of the last 5 iterations is not below the best before them by more than
  15 %, and the median update `rms(u_k - u_(k-1))` over those 5 has not shrunk by more than 15 % either (a loop still
  converging moves both; a finished loop collapses the update). It names the best iteration and its drive file: that is the
  drive to keep. Each history entry records `update_rms`; states from before 2 Sep 2026 lack it and the detector stays silent
  on them until five new iterations have run.
- **Restarts.** Every bench run stamps its measurements. The first measurement of a run is not compared with the last of the
  previous one (the change includes whatever the chain did while the output was off), and the plateau only reads the current
  run. A manual Step counts as its own run.
- **Era tags.** Each history entry is tagged with the model that produced it; the Convergence tab draws a divider and label at
  every model change and the Error tab labels traces by model era.

### 3.7 Field memory

Switching channel or model swaps the boxes to what that channel (and model) last had. The first-shot gain, seed drive and
browsed FRF are remembered per channel, the parameter boxes per channel and model, and the memory survives a relaunch in the
config. A channel never used starts with blank boxes: numbers never follow you from one system to another. A model never used
on the current channel keeps the numbers on show as its starting point. The log line at each switch says what came back.

---

## 4. Reading the plots

The handbook (chapter 7) describes the optical views, Error -> legs, the warm-colour rule and Diagnostics -> pd. This
section holds the grammar of the controls and the per-tab reading guide for the rest.

### 4.1 Iterations and Compare

**Iterations shown** (blank = the last two, `all`, a range `2-5`, a list `0,3,6`; Enter or **Redraw** applies it) picks which
stored iterations the Drive (corrections, updates) and Error (error, spectrum) views overlay. Iterations are coloured
dark-to-light by age, newest drawn heaviest. Every measurement the bench loop takes, and every one recalled from `meas_*.npy`
at Load state, is available.

**Compare** overlays other campaigns on the same plots. Its grammar is space-separated keys, each optionally with the
Iterations grammar after a colon: `OLDX1 OLDX2:all OLDX3:0,3`; blank means that campaign's last measured iteration. A key is a
stem in the active state's directory (`drive_<stem>.state.npz` plus its `meas_*.npy` and `drive_*_iNN.csv`), or whatever
**Add campaigns...** mapped to a state file anywhere on disk: an archived run, another bench PC's `run\`, a folder of last
month's captures. Picking a file appends its key to the box, so what is typed stays the record of what is drawn; a picked
campaign whose stem is already spoken for is keyed `STEM@folder`, which lets the same campaign name in two folders be compared
against itself. **Clear** unloads the box, the picked paths and the cached states. The line underneath says what resolved
(green), how many keys did not (amber), or the grammar when nothing is loaded. The log carries the reason, plus each
campaign's channel, grid and stored iterations when it is added, and a NOTE when its channel or time grid differs from the
session's. Compared states load read-only and are never saved or stepped. Each stem gets one colour (hues clear of the active
session's viridis ramp; older selected iterations blend toward white; hold runs ride along dashed when **runs** is on), drawn
on its own time grid and monitor scale against its own reference drive. Overlays land on Error, Error spectrum, Drive
corrections, Drive spectrum, Drive updates, Convergence (the stem's whole peak and rms history, regardless of the iteration
selection) and both Waveforms panes (every selected measurement and drive, plus that stem's target when it differs). The
Convergence tab and the Table always show a compared stem's whole campaign, usually the comparison that matters (X1 against
X2, or the same target before and after a model change). Delta-t labels stay active-session-only. A stem with no state or no
measurements is reported in the log once per spec.

### 4.2 Hold runs, labels, dots, spectra, zoom

- **Hold runs** draw dashed on Error and Error spectrum, labelled `iter k rN`, and appear as open circles on Convergence. The
  **runs** checkbox hides them wholesale. They are excluded from the drive-side tabs by construction (same drive, identical
  correction, zero update).
- **Delta-t labels** append wall-clock offsets to the legend (a run against its iteration's base measurement, a base
  iteration against the previous one) for reading thermalisation timescales straight off the plot. Timestamps come from the
  measurement moment (file mtime for recalled ones), so they survive restarts.
- **Dots** mark real samples, every dot actual data. The *dot every Nth sample* box sets the density: blank is auto (~180 dots
  per trace, which keeps a 5301-point record readable), a number is that literal step, `1` draws every sample. The line always
  passes through every sample regardless.
- **Spectra avg** (Welch segment count, both spectrum tabs): blank or `1` is the raw single-record FFT (full resolution, but a
  periodogram's per-bin scatter is ~100 % and does not average down with record length); `N` is Hann-windowed 50 %-overlap
  averaging over N segments (scatter drops ~sqrt(N), resolution coarsens ~N-fold; tones closer than the new bin width merge
  and burst-edge content smears). Normalised so a pure tone keeps its height in both modes; the broadband floor moves with bin
  width, so only compare curves drawn at the same setting (the title says which). "Looks noisy" is mostly estimator variance:
  raise it. **Native spectrum** (Manual step dialog, section 5.6) extends the band past the grid Nyquist.
- **Toolbar.** Pan, zoom and home work as expected. The *Configure subplots* sliders are inert: the figures use matplotlib's
  constrained layout, which recomputes the margins on every draw. **Link t** applies a rectangle-zoom, pan or home on any
  time-domain plot (Waveforms, Drive corrections, Drive updates, Error) to all of them, and redraws keep it until the next zoom.

### 4.3 What to look for, tab by tab

| tab | what to look for |
|---|---|
| **Waveforms** | target against measured output (top); the drives (bottom) with the current drive's first and last sample idle markers against the idle cap. Follows Iterations and Compare: every selected measurement (hold runs dashed) and every selected iteration's drive, the current drive bold. After Init, before any measurement: the model-predicted output. A fit pins its own drive, measurement and prediction view here until the next iteration or load. |
| **Drive -> corrections** | each selected iteration's AWG waveform minus the target's flat conversion (or minus the stored iteration-0 drive when it exists): what the loop has learned to add at the input, in mV at the AWG. Growth here without matching error shrinkage is the loop learning noise or a wrong inverse. |
| **Drive -> spectrum** | the same corrections in frequency, mirroring the Error spectrum (band edge drawn the same way). The update cannot put power right of the band edge: content there came from a hand-edited drive, a stale state, or a band that was wider earlier, and the current band cannot remove it. Base measurements only; iterations without a stored drive skipped. |
| **Drive -> updates** | `u_k - u_(k-1)`, the update each shown iteration applied, against the immediately prior iteration's drive (from any stored iteration). Shrinking updates are convergence seen from the input; updates that stop shrinking while the error flat-lines mean the loop is re-learning the same correction against noise or drift. |
| **Error -> error** | target minus measured for the selected iterations, the newest with its +-1 standard-error band shaded (from the stats sidecar), peak and rms of the newest in the title. The stuck peak at t = 0 is the burst-entry transient (section 1.8). |
| **Error -> spectrum** | where the residual lives, with the newest measurement's noise floor shaded (standard error of the shot mean per bin, smoothed) and a dotted 2 times line: error above it is learnable, inside it is the shots' scatter the loop would imprint. The update only acts left of the drawn band edge (the `f_cut` line, or the shaded FRF taper). Error growing right of the edge while the in-band falls is the model diverging: tighten the band or climb the ladder. |
| **Convergence -> by iteration** | peak and rms error against iteration, log scale, with model-change annotations and the measurement's own **noise floor rms** dotted (the standard error of the shot mean: nothing under it is learnable, so an rms curve that reaches it has converged). Flat-lining above it means the current inverse has given what it can. |
| **Convergence -> by wall clock** | the same against the clock: iterations numbered, hold runs hollow, a dotted vertical where a new run started (the output was off in between, so the first iteration after it re-measures the same drive worse). Thermal transients and slow floor changes read here. |
| **Diagnostics -> code fold** | the residual (slow part removed) folded on one ADC display code of the monitor channel, per selected iteration with each bin's standard error, and the fold's peak-to-peak against iteration with one-sigma bars (hollow under three sigma). The 8-bit converter's per-code pattern (3.4 mV peak to peak undithered on 2 Sep 2026) stands many sigma up; the dithered scope reads one to three. Needs the stats sidecar (every bench, hold and step measurement from 12 Oct 2026) and the scope's V/div, which the bench reads at capture. |
| **Diagnostics -> lattice** | histogram of the newest selected iteration's raw-rate residual in scope words (10.24 V / 4096 per V/div): values piling on the integer words say the lattice is in the data undithered. Needs the native-rate file (*keep native-rate avg*). |
| **Diagnostics -> bands** | rms error in the campaign bands (0-10, 10-20, 20-30, 30-60 kHz) against iteration, each band's noise floor dotted in its colour (the bound), and the growth factor per iteration over the last five points with its one sigma in the legend: above 1.02 the band is pumping, the September gate. |
| **Diagnostics -> contraction** | abs(1 - gamma * achieved/predicted) per model-check band from every bench iteration after the first of a run: under 1 the band converges, the shaded region above 1 means the correction overshoots; hollow markers where the monitor's change did not correlate with the update (noise). |
| **Diagnostics -> heatmap** | every stored base iteration's error as one image (time across, iteration down, colour symmetric to the 99th percentile): corner ripple, hold creep and end effects separate at a glance; a band growing at the corners is a stripe brightening down the image. |
| **Diagnostics -> DAC step** | the newest selected update's histogram of abs(du) in generator steps (0.31 mV), the fraction under one step (rounded away) and that fraction's history inset: once most of an update is under a step the loop can only limit-cycle on the quantizer. |
| **Diagnostics -> pd** | from `meas_<stem>PD_iNN.npy` (pair loop, batch); a session without them shows a note only. The title reads `PD on CH1 by iteration, <stem>` and carries the analyzer angle when it was known; both chains' monitor rms are in the legend in pair mode. The handbook (section 7.6) has the reading. |
| **Table** | the ledger: one row per stored iteration and hold run (peak and rms error in V and % of full scale, the played drive's peak, wall-clock timestamp, delta-t against the reference), compare stems included; the Iterations box is deliberately not applied. The *model* column names the model that built that row's drive: iteration 0 is blank (Init's flat first shot involves no model), hold runs inherit their base iteration's label, and a model whose drive was never measured does not appear. **Save CSV...** writes exactly what the table shows. |
| **Inputs -> FRF** | measured magnitude, phase and coherence, dropped tones flagged, the taper band shaded, the current parametric model overlaid. |
| **Optical -> scan live** | the scan folder is read every 4 s (new steps only); the title reads `scan <name>: 7 of 21 steps (running)`; colour is the analyzer angle with the last step bold; the light's rotation is fitted from the third angle on and its sign follows the monitors'. It stays on the last read when the scan ends, and the read runs in a thread so the panel keeps up. |
| **Warm-colour iterations** | The tag and the hollow diamonds come from the state's `corr_from` and the correction layers' `from_it`; the batch writes them since 14 Oct 2026, and older states mark the first pass only. |
| **Optical -> passes, legs; Error -> legs** | passes are drawn from each scan's `analysis\target_compare\target_compare.npz`. Error -> legs names the last five iterations only. Dated examples (14 Oct 2026): S25E2 after an optical correction showed the monitors' leg difference at -5 to -7 V, the crystal's +0.13 degree memory; after pass 2 the light's legs differed by +-6 mdeg while the monitors were driven 135 mdeg lower on leg 2 to undo it. |
| **Batch** | the Batch window's Preview (handbook section 4.3). |

---

## 5. Every field of the panel

The handbook (chapter 1) names the tabs and chapter 4 walks through the Batch window. This section is the grammar of every
other field. The Optical and Corrections tabs are in section 8, Measure FRF... in section 3.4, and the plot-bar controls that
are about reading plots (Iterations, Compare, runs, dots, spectra avg, link t) in section 4.

The panel's launch and window are as the handbook describes. Two facts beyond it: the right-hand notebook has nine tabs
(Waveforms, Error, Convergence, Drive, Diagnostics, Table, Inputs, Optical, Batch), the Log is the pane under the tabs and not
a tab, and the status line, when idle, names the next step: Load or Init, then Auto-set and Run, then the lowest-rms
iteration since i08 (the batch's keeper rule); in pair mode it names the shown chain.

Three kinds of persistence are marked in the tables:

- **state**: saved into `drive_<stem>.state.npz` on every step and restored by Load state; changing it changes the campaign.
- **panel**: a per-session choice the state does not carry, so check it after loading.
- **config**: remembered between launches in `%APPDATA%\EOM-ILC-GUI\config.json` (paths and habits, nothing physical).

### 5.1 Session tab

Every path field scrolls to the end of what it holds, so a path too long for its box shows the file name rather than the
drive letter.

| field | what it does |
|---|---|
| **Pair (X1 + X2)** *(config)* | Pair mode, section 5.2. Remembered between launches; the startup reload then loads the pair. |
| **State** *(config)* | Path to a `drive_<stem>.state.npz`. **Load state** rebuilds the whole campaign from it: target, current drive, plant, gamma, `f_cut`, t-offset, iteration counter, error history and the model record (which rung, and for the measured FRF the file and taper, restored into the panel and the loop). It puts the state's own target file back in **Target** (recorded as `target_path` by Init; a state from before that is matched by content against `waveforms\` and `run\`, and one that matches nothing leaves the box alone and says so), recalls every `meas_*.npy` sharing the stem beside it into the plots, and fills the channel defaults (AWG and scope channels, monitor column, the wide FRF with its 50/75 kHz taper) from the state's channel. The remembered state reloads at launch. |
| **Target** *(config)* | CSV of the desired waveform: first column time (`time_us` or `time_s`), second column the value in **output units** (EOM volts on EO1/EO2, measured volts on GEN; divided by the channel's `mon_scale` on load). `#` starts a comment. It plots itself on selection. The box always shows the target the loaded campaign's iterations were measured against. |
| **Build...** | Generates a target from scratch. Fields: *shape* (cosine-edged ramp up-hold-return, or half-sine pulse), *peak* in output units, *lead / rise / hold / fall / tail* in ms (lead and tail are flat zero, the level the AWG idles on), *dt* in us (the loop grid; 2 us is the campaign standard). |
| **Plot** | Re-draws the target preview: file contents on top, and below the AWG output the flat first shot would produce, against the +-full-scale rails (AMP = 2 times full scale, OFST 0), with the DAC-range percentage and a clipping warning. Sends nothing. |
| **Echo...** | The spin-echo builder (handbook section 3.3; grammar in section 7.1). |
| **Channel** | Which chain this campaign runs on. Sets the output scale (`mon_scale`: x1000 on EO1/EO2, x1 on GEN), the divider and the limit set the guard uses (`Channel.limits`: the Trek numbers on EO1/EO2, open apart from the +-10 V generator rail on GEN), the bench wiring defaults (AWG channel, scope channel, monitor column) and the auto-pointed FRF (none on GEN). GEN's wiring defaults put the monitor on scope CH2, because the bench always reads the *drive* back from the scope channel with the AWG's number (Auto-set, the alignment check and Measure FRF rely on it); every bench action refuses a monitor on that same channel. **Switching clears the gains, the model parameters and the FRF file**: on the measured-FRF rung the FRF is the model, so a browsed one is dropped too (logged) and the new channel's default takes its place (nothing on GEN). The last choice is remembered, and the wiring fields follow it at startup. *(state + config)* |
| **Name stem** *(state + config)* | At most 7 characters (section 1.1); the cap is enforced, not advisory. Restored by Load state; the last typed value is remembered. |
| **first-shot gain** *(config)* | The conversion gain of the flat first shot only: `u0 = target / gain`. Separate from the model gain on purpose. Blank falls back to the model gain (logged). Remembered per channel: switching channel swaps in that channel's own value, blank if it never had one (the prior-leak guard). |
| **full scale V** *(state)* | AWG volts at record value 1.0, the fixed DAC mapping every drive file assumes. Default 10, which requires AMP 20 Vpp and OFST 0 (bench mode verifies and refuses on a mismatch). Also draws the preview rails and scales the `_awg.csv` copies. |
| **Seed drive** *(config)* | Optional `time_us,voltage_V` drive CSV in AWG volts (any `run\drive_<stem>_iNN.csv`, or one edited in the Waveform Editor and saved with its *ILC header* box ticked) played as iteration 0 instead of the flat conversion, sample for sample. Must have the target's point count and grid (refused otherwise). The limit check runs on it and Init asks before keeping one that fails; the bench loop and Hold refuse to play a failing drive. Recorded as `seed_path`. This is how an edited converged drive is measured against its edited target without re-learning: edit both files the same way, Init with both, then Hold. Remembered per channel (a drive is in one chain's volts). |
| **Init** | Builds the state file from the target: flat first shot, limit check, drive files written, iteration 0. **Touches no instrument**: no VISA session is opened, the copy written into the AWG GUI's library is a file on disk, and the AWG and scope can be off. The only instrument assumption in the files is the fixed mapping, which bench mode verifies before ever uploading. If the stem already has a state it asks: a yes overwrites the loop's memory (current drive, iteration counter and the Convergence history, which exists nowhere else) and restarts at i00 under the same filenames. Continue a campaign with **Load state**; start a parallel one with another **stem**. |

### 5.2 Pair mode (the two chains together)

The two Trek + EOM chains are different amplifiers, and the ramps play with both moving; the batch has always trained them
together, and pair mode does the same by hand.

1. **State**, **Target** and **Seed drive** take the X1 file; the X2 file is the same name with the last `X1` replaced by `X2`
   (`target_S47X1.csv` -> `target_S47X2.csv`, `drive_S47X1.state.npz` -> `drive_S47X2.state.npz`). A name with no `X1` in it,
   or an X2 file that is not there, is refused with the name it looked for.
2. The **Channel** box is EO1 and greyed. The Model tab's top block is X1's (EO1); a second block under it is X2's (EO2), each
   with its own rung, parameters, gamma, `f_cut`, notches, FRF and taper, its own **Replace values with a fit...** and
   **Show FRF**, and its own field memory (EO2's, the same memory the Channel switch uses).
3. **Init campaign** builds both, each from its own block. The **Name stem** gets `X1` / `X2` appended (so at most 5
   characters); X1's first shot uses the first-shot gain box and X2's its own model gain. Nothing is written until both have
   passed their checks. **Load state** loads both and fills both blocks from their states.
4. **Auto-set** sets both AWG channels and the drive and monitor verticals of both chains in one session of the instruments,
   and refuses while either output is ON. **Run bench loop (X1 + X2)** asks once to switch both outputs ON, uploads both
   drives every iteration, checks the alignment of each chain that has a CmdXn channel, reads **both monitors from the same
   frozen acquisition** on every shot (the offset dither on both), then for each chain: metrics, noise floor, stats sidecar,
   model check, update with its own loop, limit check, state saved, drive files written. Log lines carry `X1` / `X2` in front.
   Both outputs are parked at the end. The two states must share one grid (refused otherwise). *keep native-rate avg* is not
   kept in pair mode (the log says so). **Hold** re-measures both drives from one acquisition per shot (the **iter** box applies
   to both), runs numbered per chain, and does not read the PD.
5. **The PD rides along.** With a PD channel in the Scope channels map the loop reads the analyzer photodiode from the same
   frozen shots (one more channel per shot, dithered like the monitors, no extra trigger) and keeps it as
   `run\meas_<pair stem>PD_iNN.npy`; the pair stem is X1's without its trailing `X1` (`PAIRX1` -> `meas_PAIRPD_i01.npy`). The
   stats sidecar `..._stats.npz` holds the shots, the standard error, the V/div it was read at, the channel, and the analyzer
   angle the Hardware tab last read (NaN when none). The log gets `PD: peak ... V, change vs last ... mV rms` per iteration.
   The batch writes the same files under the campaign's stem (section 6.11); Load state recalls either into both sessions'
   measurements and the Diagnostics -> pd view draws them. The loop trains the monitors; this is the record of what the light
   did meanwhile.
6. The **shown X1 / X2** switch above the plots picks which session the plots, the summary ("... (X2 of a pair)"), the status
   hint and every single-channel tool (Upload, Manual step, Corrections, Measure FRF, the line ripple, and the AWG / scope
   channel numbers) are on. It can be flipped mid-run. Load and Init show X1. Unticking Pair leaves the X1 session loaded and
   the panel as it is without the tick.

### 5.3 Hardware tab, beyond the handbook

The ELL14 and DS345 rows take their port and address from the polarimeter's config, and while its window is open they report
"in use". The **Move** control turns the analyzer to the angle in "move to deg" in the polarimeter's frame, approaching from
below as the polarimeter does (3 degrees short, then a positive step), reads it back and closes the mount; the row then says
`moved to +45.00 deg` with the time, and it is refused, red, with a message while the polarimeter window holds COM3. The angle
a Check or a Move last read is what the pair loop writes into its PD sidecars.

### 5.4 Model tab

| field | what it does |
|---|---|
| **Model** *(panel)* | The rung (section 3.2). Not stored in the state: the state stores the plant *parameters*; which form is active is your choice each session. |
| **X2 (EO2) block** *(pair mode; field memory)* | The same fields again for X2, showing only the rung's own fields, with its own fit and Show FRF (the fit dialog reads this block's FRF and X2's measurements and writes this block's boxes). Measure FRF lands its file in the shown chain's block. |
| **gain** *(state)* | The model's DC gain, AWG volts -> measured volts. Used by every parametric lead (`e/gain` is the whole 0th-order correction) and by the model-predicted-output trace; fallback source for the first-shot gain. |
| **tau us** *(state)* | One-pole time constant in us; only the one-pole model reads it. On the Trek chains it equals the resonance's group delay (~28 us): the lag without the ring. |
| **fn Hz**, **zeta** *(state)* | Second-order natural frequency and damping ratio; any zeta > 0 is accepted. zeta > 1 is two real poles, which is what the Trek FRFs fit (X1 at 0.5 V: fn ~7.3 kHz, zeta ~1.1); the zeta ~0.2 resonance of the August ramp fits was withdrawn. |
| **gamma** *(state)* | The learning gain: the fraction of the computed correction applied per iteration. 0.5-0.7 is the useful range; the loop contracts where gamma times the model error stays under 2, so a smaller gamma buys robustness at the cost of iterations. |
| **f_cut Hz** *(state)* | The zero-phase Q-filter corner for **parametric** updates: learning is confined below it, and the outgoing drive is low-passed at it too. 5 kHz is right on this bench (section 1.6). Greyed out in measured-FRF mode. To effectively disable it enter Nyquist or more (`250e3` on the 2 us grid): the corner is clamped to 0.99 times Nyquist internally, a near-pass-through, which re-arms the measured drive-grass divergence on purpose. |
| **Replace values with a fit...** | Opens a dialog with the two ways the selected rung's parameters can come from data, each explained, and Cancel; either overwrites the boxes, and the loop uses the new values from the next Init, Step or bench iteration. *Fit to FRF*: least squares of the model form to the file in the FRF field (exactly one), log-magnitude and phase weighted equally, coherent tones from the lowest up to *fit up to Hz* (prefilled with `f_cut`); the gain is the median abs(H) of the three lowest tones unless *gain from the lowest tones* is unticked, in which case the gain box is kept (a free gain absorbs model mismatch: 0.70 against a measured 0.56 on X1). The log gives the in-band residual (% magnitude, degrees phase) and the frequency where abs(1 - gamma H / H_model) first reaches 1: keep `f_cut` below it. Works with no session loaded. *Fit to measurement*: `plant.identify` on the last **played** (drive, response) pair (snapshots store the drive that produced each measurement, so the pair is true even after the drive advanced; it falls back to the Captures glob), weighted by the record's own spectrum: right for the gain, 2-3 times short on lag terms for a slow ramp. Both overlay the fitted model (FRF tab, or the Waveforms tab's predicted trace). On the measured-FRF rung the button explains that there is nothing to fit. |
| **FRF** *(config)* | Path to a `sysid_fit.py` or Measure FRF output. Only read in measured-FRF mode. **Show FRF** accepts several, semicolon-separated paths and globs (`run\frf_A*.csv;run\frf_WIDE_X1.csv`), drawn together with per-file colours across magnitude, phase and coherence. The measured-FRF *model* refuses a list: it divides by exactly one file, so put the single file back before stepping. |
| **Measure FRF...** | Section 3.4. |
| **full strength to Hz** *(config)* | `f_use`: the measured inverse acts at full strength up to here. Editable only in measured-FRF mode (each band knob belongs to one side of the ladder). |
| **taper to zero at Hz** *(config)* | `f_max`: a raised cosine takes the correction from full strength at `f_use` to zero here, and the error is also smoothed at `f_max` so out-of-band noise cannot alias into the correction. Nothing above `f_max` is ever corrected. Both edges are checked against the loaded file (section 3.3). The campaign ended at 50/75 kHz. |
| **Show FRF** | Plots the FRF file (magnitude, unwrapped phase, coherence; taper band shaded, dropped tones flagged) with the current parametric model overlaid: the model-against-chain comparison of `docs/history/2026-08_report/REPORT.md` section 5, live. |

### 5.5 Scope tab

**Scope channels** (CH1-CH4 *role / name*, *config*). The role vocabulary is the polarimeter's: `PD` (analyzer photodiode),
`MonX1` / `MonX2` (Trek monitors), `CmdX1` / `CmdX2` (the Trek input, the AWG output teed to the scope), `Ref`, `Marker`,
`Other`, `off`. The Trek chains take their numbers from here: **scope ch** is the MonXn channel; the drive readback (alignment
check, Auto-set's drive verticals, Measure FRF's denominator) is the CmdXn channel or none; the PD for Measure FRF is the PD
channel. GEN keeps the old fixed numbers. With no CmdXn the trigger alignment is not verified on that run (the log says so)
and Measure FRF refuses. The map is written into every state the bench measures (`scope_channels`), printed at the start of
every bench run, and read by the batch from the same config; Load state says when a state's map differs from the panel's now.
A role on two channels is refused. **Copy from polarimeter** reads `%APPDATA%\RampPolarimeter\config.json` and takes its
channel map, logging what changed. **Defaults** is the bench as wired 5-12 Oct 2026: CH1 PD, CH2 CmdX1, CH3 MonX1, CH4 MonX2.

**Capture** (Step and bench):

| field | what it does |
|---|---|
| **t-offset us** *(state)* | The fixed trigger-to-waveform-start delay subtracted when resampling every capture onto the loop grid. Measured 0 on this bench (section 1.9). Bench mode cross-checks it on the first iteration. |
| **zero baseline** *(panel)* | Section 1.9. Off unless you know why; the Step warns when the target's own baseline is not flat. |

**Line (mains) ripple** (the numbers and buttons are in the **Line ripple...** dialog). On a **line-synchronous trigger** the
mains ripple on the monitor is the same in every shot (5 Oct 2026, the experiment's trigger: 0.9 / 1.35 mV at 60 Hz on the X1 /
X2 monitors, ~1 V at each EOM; 23 mdeg on the light; same phase in all 19 analyzer steps over 25 minutes), so the shot average
keeps it and the loop learns a drive that cancels it *at that phase*, which is wrong as soon as the experiment moves its ramps
against its line resync. Measured once and subtracted from every measurement before the update, the loop never sees it. A
free-running trigger (the bench's 1.0104 Hz) already averages it away: leave this off there.

| field | what it does |
|---|---|
| **subtract the measured ripple...** *(state)* | Takes the fitted ripple out of every measurement before the update and the error metrics (`Loop.line`, state key `line_ref`, honoured by the CLIs too); the Error tabs then show the error the loop acts on. Asks when the fit's shots were not phase-coherent. |
| **line Hz / harmonics / window ms** *(config)* | 60 Hz and 5 harmonics by default; the bench measurement's window is 100 ms (6 periods): the record itself, ~11 ms, cannot tell 60 Hz from a slow drift, which is why it is measured separately. |
| **Measure (drive off)** | Bench: the drive parked (a flat record at its idle with the output ON, what the chain sees between bursts; asks if the output is on; with it OFF the reference is taken off), scope widened to the window on the same trigger (restored after), 8-32 dithered shots of the monitor, harmonics fitted to the mean, per-shot phase coherence reported (0.7 or more is line-synchronous). Times out with a hint if the scope triggers on the drive itself. |
| **Fit from captures...** | Offline: Scope Grab CSV or NPZ captures of the monitor (column = *monitor col*) with the drive off, or any capture whose undriven stretches are named in **fit only in ms** (`-12:0, 10:16.5`). Must use the same trigger as the ILC runs. Short stretches fit badly (the log says when). |
| **Clear** | Forgets the fit and switches the subtraction off. |

The batch's log line "the file does not say line (mains) ripple was removed" is about the same effect (handbook chapter 10).

### 5.6 Run tab

| field | what it does |
|---|---|
| **Bench** line | The wiring in force (AWG channel -> monitor channel, readback channel or "none (alignment not verified)") and the last instrument fact a run reported (Auto-set's FRQ, AMP, output; output ON or OFF at a run's end) with its time. In pair mode, one wiring line per chain. |
| **AWG ch / scope ch** *(Bench settings...)* | Generator channel the drive is uploaded to and the scope channel carrying the monitor. EO1/EO2 fill them from the Scope channels map (the MonXn channel) and refuse a number that contradicts it; GEN uses them as typed. |
| **iterations** *(config)* | Updates to run. The loop measures `iterations + 1` times: the last measurement documents the final drive without updating past it. |
| **repeats** *(config)* | HRES single shots averaged in software per iteration; 64 is the campaign standard, 16 a quick check, below 16 is refused. |
| **trigger timeout s** *(Bench settings...)* | Per-shot trigger stall limit; it only fires if triggers stop arriving. Raise it for slower burst rates. |
| **skip setup checks** *(Bench settings...)* | Uploads without verifying AMP, OFST, clock and acquisition mode. A mismatch silently rescales the drive, the one error the loop cannot see. Do not. |
| **keep native-rate avg** *(config, Bench settings...)* | The bench loop and Hold also save each iteration's repeat average at the scope's own sample rate, `meas_<stem>_iNN[_rMM]_native.npz` (`t` = waveform time, `y` = monitor V) beside the decimated `.npy`. Costs ~0.5-1 MB per iteration; point the Captures glob at these files to run *Native spectrum* on a bench campaign afterwards. |
| **keep verticals** *(config, Bench settings...)* | Auto-set leaves the scope's V/div and offset as they are (it still sets FRQ, AMP, OFST, load, burst, timebase and HRES, and prints the verticals it found). Tick it for a session whose target span differs from the campaign it is compared against (an edited endpoint, say) so the 8-bit lattice stays the same; untick and Auto-set re-ranges both channels from the session's own spans. |
| **Upload `<stem>_iNN` to AWG** | Uploads the session's current drive into the generator's user memory and selects it, at the fixed +-full-scale mapping (never normalised). Asks before overwriting a stored waveform of the same name (the generator cannot read a waveform back out, so the local copies in the AWG GUI's library are the only record of the old samples); no dialog when the name is free. Warns in the log when the output is live, since the new waveform plays the moment it is selected. |
| **Auto-set instruments** | Beyond the handbook (section 2.1): the AWG gets the record's period as its arb frequency (1/period), and the session's current waveform (`<stem>_iNN`) selected *if the generator already holds it* (it never uploads; the log says which applied). The scope window is 1.3 times the period with the waveform at the left edge (position = half the period), and the verticals are sized from the drive and target spans (unless *keep verticals*). Waveform and burst readbacks are printed: believe those, not the writes. In pair mode it sets both channels and both chains' verticals and refuses while either output is ON. |
| **Run bench loop** | Before touching the generator it verifies the channel is set up the way the drive file assumes (AMP 20 Vpp, OFST ~0, DDS, HRES, enough repeats) and refuses on any mismatch: fix the setup rather than ticking *skip setup checks*. If the output is OFF it asks (a yes switches it ON for the run and it stays ON, parked, at the end; a no cancels with nothing changed). On the first iteration it cross-checks the trigger alignment against the drive it just uploaded and refuses if `t-offset` looks stale. Each iteration: upload -> repeats HRES singles (progress bar) -> metrics -> plots -> update -> state saved; every measurement is kept as `run\meas_<stem>_iNN.npy`. **Stop** is graceful, between shots or iterations: a stop mid-capture discards only that iteration, and the state on disk is the last completed one. When a run that played anything (or switched an output on) ends, finished, stopped or died, the driven channel is **parked** (a flat record at the drive's idle, `PARK1` / `PARK2`, same length so FRQ still fits; output left ON; the rule is in the handbook, section 2.5). A run refused at the setup checks leaves the bench exactly as it found it. |
| **Run bench loop (X1 + X2)** *(pair mode)* | Section 5.2. |
| **Hold (runs / gap between runs s / iter)** | Re-measures a drive `runs` times, `gap between runs s` apart, with **no update and no state change**: the thermalisation probe. Runs are tagged `iter k rN`, saved as `meas_<stem>_iNN_rMM.npy`, and numbered on from any earlier holds of the same iteration (distinct from the loop's iterations and from the 64 repeats averaged inside every measurement). Same setup checks, output confirmation and park-at-end as the bench loop; Stop works between shots and during the gap. The **iter** box picks which stored iteration to hold: blank is the current drive; a number loads that iteration's `drive_<stem>_iNN.csv` from beside the state, uploads it under its own name and tags the runs to it, so an earlier drive can be re-measured without Init (which would restart the campaign at i00 under the same names). When a run lands, the panel ticks **runs** and adds the held iteration to the **Iterations** box if either would have hidden it, and says so in the log. |

**Manual step...** (Run tab dialog; the panel version of section 2.3):

| field | what it does |
|---|---|
| **Captures** *(config)* | A glob of scope CSVs. The step averages **every** file it matches and lists them in the log: read that list. Browsing to one file of a sequence replaces the trailing run index with `*`. Captures that do not span the whole waveform are refused (section 1.7). |
| **monitor col** | Which CSV column is the monitor trace: CH3 for EO1, CH4 for EO2 (auto-set with the channel). Matching is tolerant of substrings, but a mislabelled scope channel still lies: check the label in Scope Grab. |
| **refit plant** *(panel)* | Re-identifies the parametric model (in the selected form) from this iteration's own drive/response pair before updating and stores the result in the state: the continuous *Fit to measurement*, with that fit's spectral weighting. |
| **force** *(panel)* | Writes the updated drive even when the limit check FAILED. The check exists because the answer is usually no. |
| **Step** | One iteration: average captures -> error metrics -> update -> limit check -> write drive and AWG copy -> save state. |
| **Native spectrum** | The error spectrum at the scope's native sample rate: the target is interpolated onto the capture's time base instead of the capture being boxcar-decimated onto the waveform grid. The record length is the same (the low-frequency bins do not move), but the band extends to the scope Nyquist and the top octave loses the anti-alias boxcar's droop. Uses the Captures glob, the monitor col and the *spectra avg* setting, draws as a black overlay on the Error spectrum tab (the next Redraw clears it), has the same span guard as Step, and reads raw Scope Grab CSVs and the bench loop's kept `meas_*_native.npz` alike. |

### 5.7 Batch window, beyond the handbook

The window is walked through in the handbook (chapter 4). The extras:

- **Check plan** uses the numbers typed in the window but does not write them into the plan; **Simulate** and **Run on bench**
  write them into the plan's `loop` block before running, so the plan stays the record of how a batch was trained.
- **Preview** loads the plan as Check plan does (echo records the plan asks for are built, as the dry run builds them). The
  status line says `previewed 6 campaigns` or `previewed S47E2`. A campaign with a problem in the grid carries it in red, and
  an empty panel means its targets could not be read. In the one-campaign view the marks are the card split's transports and
  their common flat top, with rise / hold / fall lengths written in.
- **Status line and bar** come from `eomilc.progress.BatchProgress`, fed by the batch's events. The bar runs over the plan's
  campaigns and moves within the current one: the electrical part counted to the iteration cap (an early stop completes it),
  weighted against the optical stage by the plan's own estimates, then scan by scan. The estimate's keys are in section 6.5.
- **Optical stage table.** While a batch runs, the table and the passes view follow the running campaign unless another is
  selected. Reviewing another campaign switches *follow in the plots* off, because following would pull the running state
  back at the next iteration; the status line says so. The table reads `run\optical\<stem>_optical.json`.
- **Export for Supertime...** is `eomilc.supertime`; the command-line twin is `tools\supertime_chunks.py` (handbook chapter 9).
- **Double-click** a campaign loads its state (the followed channel) in the panel.

### 5.8 Panel traps the handbook does not carry

- **Init refuses to overwrite silently**: it asks first, and a yes destroys that campaign's history (section 5.1).
- **Editor files need a time column.** The Waveform Editor's default save is `index,value` with no header; the panel refuses it
  by name (it would otherwise read 5500 points of seconds). Tick the editor's *ILC header (time_us,voltage_V)* box with the
  sample rate set (500 kHz for the 2 us grid) and save again.
- **A record of a new length needs a new FRQ.** Bench mode and Hold check FRQ against N times dt and refuse (handbook chapter
  10 has the cause).
- **The first shot is deliberately uncorrected.** Expect the chain's raw tracking error (~2.4 % on the Trek ramps) on iteration 0;
  it is the baseline every later iteration is judged against.
- **The scope channels are roles, not numbers** (12 Oct 2026): section 5.5.

---

## 6. The batch runner and the plan file

`ilc_batch.py` trains a list of target pairs one after another. Each pair (`target_<stem>X1.csv`, `target_<stem>X2.csv`) is one
campaign: both drives play at once, both monitors are read from the same frozen shots, and each channel's loop learns from
its own monitor. The runner uses the panel's capture scheme (HRES singles with the 3-code offset dither) and writes the
panel's state files, so any campaign can be opened with **Load state** afterwards and continued by hand. The handbook (chapters
4-6) walks through the window, the headless run and what a campaign does; this section is the grammar.

### 6.1 The plan file: top-level keys

The example plan is in the handbook (section 4.2). Paths in the plan resolve as follows: `targets_dir` relative to the plan
file, `run_dir` and every `from_state` / `frf` path relative to the EOM-ILC folder (absolute paths are left alone).

| key | default | meaning |
|---|---|---|
| `name` | `plan` | names the manifest `batch_<name>.json` and the log `batch_<name>.log` in the run folder. Rebuilding a plan under the same name is what lets `--redo` line up with the old manifest. |
| `targets_dir` | `targets` | where the target pairs are. |
| `run_dir` | `run` | where states, drives, measurements, keepers, the manifest, the log and `optical\` go. Simulation writes to `<run_dir>\sim` instead. |
| `wiring` | the Scope tab's map | per-channel override, section 6.2. |
| `settings` | the production FRF | per-channel training settings, section 6.3. |
| `loop` | section 6.4 | stop rules and shot counts. |
| `timing` | section 6.5 | for the estimate only. |
| `optical` | none (no optical stage) | section 6.12. At the root it applies to every campaign; a campaign's own block wins key by key. |
| `card` | none (no card files) | section 6.7. |
| `campaigns` | required | the run order, section 6.6. A plan without campaigns is refused. |

### 6.2 `wiring`

Which scope channel is which comes from the panel's Scope channels map (Scope tab; the polarimeter's role names, kept in the
panel's `config.json`): the MonXn channel is the monitor, the CmdXn channel the drive readback for the alignment check. With no
CmdXn assigned that channel's alignment is not verified and the log says so. A plan's block overrides the map per channel:

```json
"wiring": {"X1": {"channel": "EO1", "awg_ch": 1, "drive_ch": 1, "mon_ch": 3}}
```

`channel` is the calibration set (EO1 for X1, EO2 for X2), `awg_ch` the generator output, `drive_ch` the scope channel reading
the drive back (absent or null: not verified), `mon_ch` the monitor channel. If the map cannot be resolved (a role on two
channels, say) the fixed numbers X1 = AWG 1 / drive 1 / monitor 3 and X2 = AWG 2 / drive 2 / monitor 4 are used with a note.
`--dry-run` prints the wiring in force.

### 6.3 `settings`

The two chains are different amplifiers, so each learns with its own inverse: the rung, parameters, gamma, `f_cut`, notches or
FRF. `settings.X1` and `settings.X2` take either

- `{"from_state": "run/drive_P92PX1H.state.npz"}`: copy model, gain, tau, fn, zeta, offset, gamma, `f_cut`, notches and the
  FRF file with its taper from a state that trained well (a missing file refuses the campaign naming it); or
- typed values: `model` (`frf` or a parametric rung), `frf`, `f_use`, `f_max`, `params` (`gain`, `tau` in us, `fn`, `zeta`),
  `gamma` (0.6), `f_cut` (40e3) and `notches` (a list of `[f0_Hz, bandwidth_Hz]`).

With no settings at all the plan uses the production inverse: the measured FRF `run/frf_WIDE_X1.csv` / `run/frf_WIDE_X2.csv`
with full strength to 50 kHz and the taper to 75 kHz, gamma 0.6, `f_cut` 40 kHz. The September keepers (P92 / S92) used other
rungs, so a new ramp is better trained from their states. A campaign that resumes from an existing state keeps that state's
model, not the plan's. `--dry-run` prints one **model** line per channel saying what the inverse is and where its numbers came
from (the FRF file name, the full-strength and taper edges, or the parameters; gamma, `f_cut`, the notches as `f0 kHz/bandwidth`).
The command-line builder's `--x1-from` / `--x2-from` write `from_state` entries.

### 6.4 `loop`

| key | default | meaning |
|---|---|---|
| `max_iterations` | 20 | the iteration cap. |
| `min_iterations` | 8 | the earliest iteration a channel may stop converged, and the earliest the keeper may come from. |
| `stop_rms_v` | 0.8 | stop when the rms error (V at the EOM) is at or below this after `min_iterations`. |
| `plateau` | true | stop on a plateau (the loop re-learning noise, section 3.6). |
| `noise_floor_stop` | true | stop when nothing is left above the noise floor. |
| `diverge_factor` | 2.0 | three measurements in a row above this times the channel's best rms is a divergence. |
| `repeats` | 64 | HRES singles per measurement. |
| `repeats_early` | null (off) | the shots while the error is far above the noise, section 6.9. |
| `early_rms_v` | 2.0 | the rms (V at the EOM) above which the early count is used. |
| `wait_s` | 30.0 | per-shot trigger stall limit. |
| `dither_codes` | 3 | the offset dither's span in ADC codes (0 switches it off). |
| `settle_s` | 0.5 | wait after the outputs go ON and before each capture. |
| `t_offset_us` | 0.0 | the trigger offset (section 1.9). |
| `full_scale` | 10.0 | AWG volts at record value 1.0. |
| `first_shot` | `flat` | `flat` is the flat conversion; anything else asks for the model first shot. |

The numbers typed in the Batch window (max iterations, min, stop at rms V, shots) are written into `loop` when the plan runs.

### 6.5 `timing`

| key | default | meaning |
|---|---|---|
| `trigger_hz` | 3.7 | the trigger rate, for the time estimate only; it does not set the generator. Set it to 1.0104 for echo records so the estimate is honest. |
| `read_s_per_channel` | 0.12 | read time per scope channel per shot. Measured 13 Oct 2026: 36 s per 64-shot iteration at 3.7 Hz with two channels read (0.02 stood here and made every estimate low). |
| `upload_s` | 2.5 | per channel per iteration. |
| `campaign_overhead_s` | 25 | per campaign. |
| `scan_step_overhead_s` | 2 | per optical scan step (the mount move). |
| `compare_s` | 10 | per optical comparison. |

The estimate for a campaign is `campaign_overhead_s + per_iteration * (max_iterations + 1)` with `per_iteration = repeats *
(1/trigger_hz + read_s_per_channel * channels) + upload_s * channels`. An optical stage adds each scan's steps (the angles, two
hold-null angles, a reference return every `ref_every` angles) at `shots * (1/trigger_hz + read_s_per_channel * (channels + 2)) +
scan.settle_s + scan_step_overhead_s` per step, plus the comparisons and the learning iterations. Check plan's totals are at the
iteration cap, so they overstate a plan whose channels stop early. The Batch window's time-left figure corrects the estimate
with what the run measured (handbook section 4.4).

### 6.6 `campaigns[]`

| key | meaning |
|---|---|
| `stem` | the target pair's name. |
| `awg` | the AWG name stem. A stem over 5 characters is cut to 5 automatically (`RS25E2` trains as `RS25E`); two stems that would collide after the cut are refused. |
| `echo` | `{"spacing_ms": 16.667, "legs": 2, "tail_us": 200, "end_ms": null, "suffix": null}` (the shown values are the defaults; suffix null is `E<legs>`). The record is built into the targets folder when the plan loads (an identical file already there is left alone) and the campaign goes on as `<stem><suffix>`. A source that cannot be used is reported with the campaign in `--dry-run` and Check plan, and the campaign is refused rather than training the one-transport stem. Section 7.1. |
| `optical` | this campaign's optical block; `false` switches a root block off for it. |
| `channels` | `["X1", "X2"]` by default. The optical stage needs both. |

### 6.7 `card`

`{"n": 2500, "settle_us": 200}`. When both keepers exist the batch cuts them into the experiment card's up/down files with
`tools\split_for_card.py` (section 7.2); the files and the card pause are in the handbook (section 6.4). A card failure is
recorded as `card_error` in the manifest and does not discard the training. `make-plan --card N` writes this block.

### 6.8 What the batch checks at each campaign start

In order (the handbook, section 4.3, gives the one-paragraph form):

1. Outputs already ON without `--allow-output-on` refuse the campaign before anything is changed. A live channel first plays
   a flat park at its idle (a flat record plays the same at any FRQ, so nothing time-scaled reaches the Trek while FRQ moves).
2. FRQ = 1/record on both channels (`FRQ did not take` refuses).
3. The scope: reference centre, range 1.3 times the record, position half the record. Every drive and monitor channel DC
   coupled, scale 1.25 times the target or drive span over 8 divisions, offset at the span's middle. The PD channel is switched
   on, DC, 1 V/div, offset 2.7 V (the polarimeter's PD preset), not whatever the last scan left. The acquisition must read
   HRES.
4. Each first drive passes the limit check; both drives upload; the channel check (AMP, OFST, load, clock, burst against the
   expected period) refuses on any mismatch; without `--allow-output-on` a campaign whose outputs are off is refused.
5. Outputs ON (those not already ON), `settle_s`, then the alignment check: one frozen shot of both drive channels,
   cross-correlated against the drives. It refuses on no trigger ("is the burst running?"), on a drive channel that is flat
   (under 0.5 V peak to peak), or on a start more than 10 us from the t-offset.
6. The analyzer angle is read once (section 6.11).

During the capture, a missed trigger is re-armed twice before it ends the campaign (three arms); a scope read is retried three
times and a shot that stays unreadable is dropped (fewer than 90 % of the shots, or fewer than 4, refuses the campaign); and a
shot whose monitor reaches within one ADC code of the screen edge refuses the campaign ("clipped"), because the loop would
learn the clipped error: widen the vertical or the first shot.

### 6.9 Stop rules, early shots and the keeper

A channel stops at the first of these, tested in this order: three measurements in a row above `diverge_factor` times its best
rms (the channel goes back to its best drive); `max_iterations`; `stop_rms_v` after `min_iterations`; nothing left above the
noise floor (`noise_floor_stop`, after `min_iterations`); a plateau (`plateau`, after `min_iterations`); or a drive failing the
limit check (the channel stops with reason `limit check: ...`). The channel that stops first keeps playing its last drive until
the other is done. In an optical learning pass the divergence best counts only from the pass's first iteration, because the rms
against the old target says nothing about divergence from the moved one.

**Early shots.** `repeats_early` shots (at least 2) are taken on iteration 0 and while the last measured rms of any active
channel is above `early_rms_v`, and above three times that measurement's own noise floor: few shots have a floor of their own
(2 shots of the simulated chain: 2.5 V rms), and an rms held up by it would keep the early count for good and stop the channel
on that floor. For the same reason a "noise floor" or "plateau" stop judged on an early measurement is not taken; the channel
goes on at `repeats`. A channel at the last iteration its cap allows is always measured at the full count (S25E2, 14 Oct 2026:
a 16-shot keeper after a 3-iteration learn read 1.39 V rms). The iteration line says `(16 shots, early)`, the manifest records
every iteration's shots, and the dry run's loop line ends `64 shots per measurement (16 while rms > 2 V)`. `repeats_early`
absent or equal to `repeats` is off.

**The keeper** is the measured iteration with the lowest rms at or after `min_iterations`, among the iterations since the start
of the current pass (an optical pass keeps a drive of its own: a pass that diverged early must not fall back to the electrical
keeper, learned for the uncorrected target; 14 Oct 2026). It is written to `keep\drive_<stem>X1_keep.csv` with a comment line
naming the iteration, rms, peak and stop reason. A resumed campaign whose per-iteration files are gone has no keeper; it is
recorded as such rather than raising.

### 6.10 Generator memory

Uploads alternate between two slots per channel (`BX1A` / `BX1B`, `BX2A` / `BX2B`), counted across the whole batch, because the
4063B cannot delete stored waveforms remotely. Every iteration is still on disk as `run\drive_<stem>X1_iNN.csv`. `--gui-names`
uploads under `<stem>X1_iNN` instead. A drive over `AWG_MAX_PTS` (8000000; environment `BK4063B_MAX_PTS`) is refused naming the
cap. The park records are `PARK1` and `PARK2`.

### 6.11 The PD and the analyzer angle

**The PD every iteration.** The loop trains the Trek monitors; the light is what the atoms get, and nothing used to record it
while the loop ran. During the electrical iterations the analyzer sits still, so the PD reads cos squared of the light's
rotation at one angle, and its trace iteration by iteration shows how the light answers each update. With a PD role in the
Scope channels map (CH1 on this bench) every measurement reads it from the same frozen shots as the monitors, its offset
dithered like theirs, resampled onto X1's grid, and keeps it as `run\meas_<stem>PD_iNN.npy` (the shot mean, PD volts) with a
sidecar `meas_<stem>PD_iNN_stats.npz` in the monitors' format: standard error per sample and per bin, shots, the V/div it was
read at, the scope channel and `analyzer_deg`; no code fold, which means nothing on the PD. `<stem>` is the campaign's (`S25`,
or its 5-character AWG name) and NN the campaign's iteration (the higher of the two channels' counts when one has stopped).
The log gets `  PD: peak 4.98 V, change vs last 12.3 mV rms` per iteration. A PD on a monitor's own channel is not read; with no PD
role nothing is read and the campaign runs as before. The optical stage's learning iterations record it the same way.

The manifest entry gains an `iterations` list: per iteration `shots`, `early`, `rms_v` and `floor_v` per channel, `pd_peak_v`
and `pd_change_mv` (rms of PD(i) minus PD(i-1), NaN on i00). A resumed campaign's earlier iterations are rebuilt from their PD
files. The panel's Load state recalls the files and Diagnostics -> pd draws them.

**The analyzer angle.** At each campaign start, after the outputs are ON, the batch opens the ELL14 through the polarimeter's
driver (its config's port, address and zero, as the Hardware tab's Check does), reads its position once, closes it again (it
never moves it) and logs `  analyzer at +45.0 deg (polarimeter frame)`. The angle goes into the manifest entry
(`analyzer_deg`) and every PD sidecar of the campaign. A mount that does not answer (the polarimeter window holds COM3) is
logged and recorded as NaN, never a refusal; a simulated run does not read it. The optical stage opens the mount itself later,
after this read has closed it.

### 6.12 The optical stage: settings

At the root of the plan or in a campaign (the campaign's keys win; `"scan"` merges key by key; `"optical": true` takes the
defaults, `false` in a campaign switches a root block off for it). Example, all defaults shown:

```json
"optical": {"scan": {"angles": "0:359:22.5", "shots": 8}, "band_hz": 5000, "pd_delay_us": 1.7, "split": 0.5,
            "gains": [0.7, 1.0], "iterations": 5, "name": "{stem}_opt{pass}"}
```

| key | default | meaning |
|---|---|---|
| `attribution` | `joint` | `joint` scans both drives together and splits the one residual by `split`; `separate` measures each crystal alone (section 6.13). Anything else is refused when the plan is checked. |
| `joint_before` | true | with `separate`, a joint scan before the first pass (the table's before); recorded, not applied. false: `verdict_before` is None. |
| `gains` | `[0.7, 1.0]` | one gain per pass, each in (0, 2], at least one. The 14-15 Oct campaign used `[1.0, 1.0, 1.0]`. |
| `iterations` | 5 | learning iterations after each applied correction, at least 1 (the campaign used 4). |
| `band_hz` | 5000 | the comparison's low-pass and the correction's band. |
| `pd_delay_us` | 1.7 | the photodiode's own delay, removed before the comparison; 0 in simulation unless the plan sets it (the simulated PD has none). |
| `split` | 0.5 | the share of the rotation correction put on EO1 in a joint comparison (0.5 for parallel shapes). |
| `name` | `{stem}_opt{pass}` | the scans' base name (`{stem}` and `{pass}` only). Scans append `_before`, `_X1`, `_X2`; the verification keeps the base name. |
| `outdir` | `<run_dir>\optical` | where scan folders and `<stem>_optical.json` go. |
| `verify` | true | take a verification scan after each pass. |
| `require_keeper` | true | no stage when a channel diverged, failed the limit check or measured nothing. |
| `cap_v` | null | lowers the gate's cap (300 V at the EOM). The campaign used 150. |
| `figs` | true | the polarimeter's comparison figures beside each comparison. |
| `sim_rot_err` | 0.5 | the simulated rotator error in degrees, the light-only term the simulated closure has to remove. |
| `sim_bridge` | true | the simulated polarimeter plays what the simulated ILC chains make of the drives (section 6.15). |
| `scan.angles` | `0:359:22.5` | analyzer angles for the before scan (16 angles). |
| `scan.solo_angles`, `scan.verify_angles` | null | angles of the solo and verification scans when they differ from `angles`. A half circle such as `0:179:22.5` carries the same polarization information in half the time; the full circle is kept for the before scan's 1-theta / 4-theta diagnostics. The suite plans do this since 15 Oct 2026. |
| `scan.shots` | 8 | single shots per angle. |
| `scan.sample_us` | 2.0 | the readout spacing: the screen runs from 0.5 ms before the trigger to 8 ms after the record (30 ms until 14 Oct 2026), read at the ILC's own grid whatever the record length (~8000 points for S25, ~18500 for a 27.5 ms spin-echo record). |
| `scan.points` | null | a number fixes the readout count instead of the spacing. |
| `scan.ref_every` | 9 | a return to the reference angle every N steps. |
| `scan.hold_null` | true | extra angles around the hold's null (90 degrees times each target's peak over its V90). |
| `scan.settle_s` | 1.0 | wait before the first shot. |

The gate settings the stage applies are the Corrections tab's defaults (`optical.GATE_DEFAULTS`: k sigma 3, floor 0.1 V, cap
300 V or `cap_v`, fade 200 us at the record's two ends) except the band (`band_hz`, 5 kHz against the tab's 2 kHz) and
`active_only`, which is off so the whole record is kept. The
dry run prints one line per campaign, for example `optical: 2 passes gains 0.7/1.0, 5 iterations each, scan 0:359:22.5 x 8
shots, band 5 kHz`, with `(solo scans ...)`, `(verify scans ...)`, `attribution separate (X1 alone, X2 alone, then both to
verify)`, `no verification scans` or `gate cap 150 V` appended as they apply. With separate attribution and more than one pass
it ends `pass 1 solo, later passes add the joint verification's residual`.

On the bench the scan reads through the ILC's own scope, which must be **scope-grab-multi** (the headless API). With the old
checkout the stage stops before switching anything on and the log says how to point `SCOPE_GRAB` at the right one. The
analyzer is the ELL14 of the polarimeter's config. Before anything is scanned the PD's channel is set to the polarimeter
preset's V/div and offset, the trigger sweep to NORMAL, and one single shot of every recorded channel is judged: a dark PD
(under 50 mV), a clipped or switched-off channel or a wrong trigger refuses the scan.

### 6.13 The optical stage: internals

The loop makes the Trek monitors follow the target; the light leaves them by 0.7-1.7 degrees at the top of every ramp and
0.3-0.95 degrees in the first ms after the fall (8 Oct 2026), which no monitor shows. With a stage defined, a campaign goes on
after its keepers: both keepers play, the polarimeter scans the light, the comparison writes minus (light minus monitors),
low-passed to `band_hz`, as a correction per crystal, and both targets move by it at `gains[pass]`
(`eomilc.optical.apply_correction`, with the Corrections tab's gate and checks; both channels or neither), `iterations`
iterations of the normal loop learn the moved targets, and a verification scan and comparison give the after. The states then
carry the panel's correction keys (`base_target`, `corr_total`, `corrections`), so Load state shows the corrected target and
its layers, and a later comparison is made on the base target. In a learning pass the iteration cap is the pass's first
iteration plus `iterations` minus 1 and the minimum is the first plus the smaller of `min_iterations` and `iterations` minus 1.

**Joint attribution** (the default) scans with both drives playing and splits the one residual between the crystals by
`split`. That is right for the light, which only needs the sum, but the file holds only for that pair of drives, and each
crystal's own error (the 8 Oct creep was X1's alone; the ramp-top overshoot is per crystal) lands half on the wrong one. Pass 2
applies the verification's correction at `gains[1]`, and it replaces pass 1's in its slot; it never stacks.

**Separate attribution** measures each crystal's correction on its own, per pass:

1. Before: on the first pass, with `joint_before`, a joint scan `<stem>_opt1_before` and a joint comparison give
   `verdict_before`. Later passes take the previous verification as their before (with `verify` false, a joint before scan of
   their own).
2. X1 alone: X2's channel gets its drive flattened at its idle (`optical.park_drive`: the first sample, the same length, so FRQ
   and the trigger hold), uploaded as `PARK2`; the output stays ON. Scan `<stem>_opt1_X1`, compared with `solo="X1"`: the whole
   light minus monitors goes to X1, EO1's file only.
3. X2 alone: X2's keeper back on CH2, X1 parked as `PARK1`, scan `<stem>_opt1_X2`, `solo="X2"`, EO2's file.
4. Both keepers uploaded again, both files applied (both or neither), `iterations` iterations learn.
5. The joint verification scan `<stem>_opt1` gives `verdict_after`; its own residual is recorded (`verify_corr_files`) and never
   applied in that pass.

That is three scans a pass instead of two (four on the first with `joint_before`). The pass record adds `attribution`,
`before_scan`, `solo_scans` {X1, X2}, `verdicts_solo` {X1, X2} and `parked` (name and idle volts); `corr_files` is EO1 from the
X1 scan and EO2 from the X2 scan, in the slot `optical`. A solo scan on disk resumes under its name; a pass recorded done is not
repeated. A light error that follows the total rotation (an analyzer or extinction term, not a crystal's own) is read in full
by both solo scans and applied twice: at gain g the pass leaves minus (2g - 1) of it, so gains over 0.5 overshoot such a term
and 1.0 flips its sign every pass; crystal-own errors converge at any gain up to 1.

**What a second pass does** (14 Oct 2026). Light minus monitors is a fresh estimate of the whole error every time: moving the
target moves the monitors and the light together, so the quantity the polarimeter reads does not shrink after a correction (the
simulation shows it: -479 -> -479 mdeg at the ramp top). A second solo pass at the same gain would only repeat the first, and a
file in the same slot replaces the last. So pass 1 measures each crystal alone and its files take the `optical` slot at gain 1.
The joint verification's files say what the whole correction should be (minus E, split by `split`), not what is left; pass 2 on
applies, in its own slot (`joint2`, `joint3`, ...), the joint file minus each crystal's split share of what is already applied
on both (`optical.joint_residual_files`):

```
joint_i  -  split_i * k_i * sum_j( total_j / k_j ),     k_j = V90_j * 1000 / 90   (V per degree)
```

Where the joint error is just the sum of the crystals' own errors that layer is zero; where something follows the total
rotation it carries the share the solo scans could not see, and the total comes to minus E. Simply adding the joint file would
add minus E again every pass (the simulation showed light minus target worse after each pass). Gains 1.0 / 1.0 (later 1.0 / 1.0
/ 1.0) are the staged plans' setting. This ran on the bench on 14-15 Oct 2026 (handbook chapter 6).

**The time after the ramp** (14 Oct 2026). The crystal's memory keeps the light rotated for tens of ms after a fall, and a spin
echo's second leg starts in it. So the record is built long enough to hold that time (`"end_ms": 33.334` pads it with idle to
the whole 2 x 16.667 ms echo, 16667 points at 2 us; the 4063B holds 8 Mpts per arb). The comparison runs over the whole played
record (rampol `whole_record=True`: the correction is minus the low-passed light minus monitors everywhere the light was
measured, not only where the target is above 0.5 degree, and nothing is faded where the target rests), and the gate keeps it
over the whole record, faded 200 us at the record's two ends. The card's files keep the 200 us design tail; the pad is idle the
card does not play (the target's `# tail_us` / `# pad_us` lines say which).

**Records.** The manifest entry gains an `optical` block, also written to `<outdir>\<stem>_optical.json`: per pass the scan and
verification folders, the correction and played-target files, per channel what was applied (gain, `kept_peak_V`, `resolved`,
status, `target_moved_V`), `verdict_before` / `verdict_after` (`optical.verdict`: per leg the ramp-top and after-fall extremes of
light minus monitors and light minus target, their spreads, the correction peak per crystal), the iterations it learned
(`iterations` {first, kept}, `kept`, `kept_rms_v`, `stops`) and `status`. A refused correction records `refused_channel` and
`refused` and ends the stage. Stage statuses are `done`, `skipped` (no keeper, or a campaign without both crystals), `refused`
(outputs off without `--allow-output-on`), `error` and `running`. A campaign whose stage did not finish is recorded as `optical
<status>` and a plain rerun repeats the stage.

### 6.14 Resuming, redoing and the manifest

Beyond the handbook (section 4.5): a pass whose verification scan is complete on disk and recorded done is not repeated when an
interrupted campaign resumes (a finished pass whose `verify` is on but whose scan is incomplete is repeated), and an unfinished
scan resumes where it stopped. The manifest `batch_<name>.json` has per campaign `started`, `minutes`, `status` (`done`,
`refused`, `error`, `stopped`, or `optical <status>`), `error`, and per channel `state`, `stop` (the reason), `iterations`,
`resumed_at`, `keeper_iteration`, `keeper_rms_v`, `keeper_peak_v`, `keeper_file` and the `history` of (iteration, rms, peak),
plus `optical`, `iterations`, `analyzer_deg`, `card` and `card_error`. An instrument or file error (a VISA timeout, a read that
failed three times) ends that campaign parked and the batch goes on to the next: an overnight run must not end on one bad read
(14 Oct 2026). The batch also appends everything it prints to `batch_<name>.log` in the run folder, whatever you redirect.

### 6.15 Simulation

`--simulate` (or **Simulate**) runs a plan against `eomilc/simbench.py` (the measured FRFs `run\frf_WIDE_X*.csv` with a gain
and delay error) and writes into `run\sim`, never `run\`, so a simulated state cannot be resumed as if measured. The optical
stage scans `rampol.sim`'s polarimeter (`optical.open_devices(simulate=True)`), whose only light-only term is the 0.5 degree
rotator error `sim_rot_err`. Three things keep the two simulations consistent: the polarimeter plays what the simbench chains
make of the drives divided by its own gain (`sim_bridge`; the raw drives read ~5 % off the target in its monitors, ~10 degrees
at the top of a parallel shape, because `rampol.sim`'s chain is a bare gain); it is cabled PD / MonX1 / MonX2 only (its command
channel is made from its monitor, and a time map of that against the learned drive misplaces the target by tens of us); and the
PD delay is 0 us unless the plan sets one. In simulation the spread of light minus target falls pass by pass (290 -> 189 ->
123 mdeg on S25, gains 0.7 then 1.0) while light minus monitors at the ramp top stays (it is the light's own). Separate
attribution proves nothing there: the simulated light-only term is an analyzer error that follows the total rotation, so each
solo scan reads about all of it (S25: 29.5 V on the driving crystal's file against 15.8 V per crystal from the joint split) and
the verification reads worse (light minus target spread 290 -> 416 mdeg after one pass at 1.0).

### 6.16 Running headless

The handbook (chapter 5) gives the procedure. The grammar:

```
ilc_batch.py run <plan> [--dry-run] [--simulate] [--allow-output-on] [--yes] [--outputs-off-at-end] [--redo] [--no-optical] [--gui-names]
ilc_batch.py make-plan --targets <folder> --stems A,B,C --name <name> --out <file> [--x1-from <state>] [--x2-from <state>] [--card N]
```

| flag | meaning |
|---|---|
| `--dry-run` | load every target, build every first drive, check it against the limits, print each campaign's record length, FRQ and time estimate. No instrument. Exit code 1 when anything is refused. Run it on the lab PC, where the `run\` states it names exist. |
| `--simulate` | the simulated bench (implies `--allow-output-on`, no confirmation). |
| `--allow-output-on` | lets the batch switch outputs on. Without it a real run refuses any campaign that needs an output switched on. With it, and without `--yes`, the program asks for a typed `YES` after saying the outputs go ON and stay ON, parked between campaigns and at the end. |
| `--yes` | skips that typed confirmation only. It is what an unattended run needs, because the program would otherwise wait for a person; use it only when the person responsible has said to. The panel's Run on bench asks the same question with a Yes/No dialog. |
| `--outputs-off-at-end` | switches both outputs OFF at the very end instead of parking them; named in the typed confirmation; only with every stage they feed bypassed. |
| `--redo` | repeats campaigns the manifest lists as done. |
| `--no-optical` | skips the optical stage for this run. |
| `--gui-names` | uploads under `<stem>X1_iNN` instead of the two alternating slots. |

**Kill and relaunch.** States are saved after every iteration, so a killed batch resumes with the same command. A killed process
cannot park its own outputs: run `tools\awg_park.py` (flat idle records uploaded as `PARK1` / `PARK2`, outputs left as they
were; `--state1` / `--state2` take an ILC state whose drive's first sample is the idle, `--idle1` / `--idle2` give volts at the
AWG, default 0 V, within the 100 mV cap; `--on` switches an OFF output ON after a typed `ON`). Wait about 60 s after the kill
before running it, and again before relaunching: the generator and VISA refuse while another process still holds them.
`tools\awg_outputs_off.py` (`--yes` skips its typed `OFF` that names both channels) is the OFF counterpart and belongs to the
case where every stage the outputs feed is bypassed. Ctrl-C and Stop end a live batch the same way: the channels it played on
park. A hard kill leaves no `stopped` entry in the manifest; the campaign counts as unfinished and resumes.

---

## 7. Spin-echo records and card files

The reasons (the crystal's memory of leg 1) and the dialog's defaults are in the handbook, sections 3.2 and 3.3.

### 7.1 The builder

One builder (`eomilc.echo`), three front ends: the panel's **Echo...** beside the Target box, a batch campaign's `"echo"` key,
and the command line

```
C:\ProgramData\anaconda3\python.exe tools\make_echo_target.py <suite>\targets\target_S47X1.csv [target_S47X2.csv] --spacing-ms 16.667 [--legs 2] [--tail-us 200] [--end-ms 33.334] [--suffix E2] [--out DIR]
```

A lone X1 takes its X2 from beside it; the output folder defaults to the source's. The Echo... dialog's fields are *Source X1*,
*leg spacing ms* (leg start to leg start, the experiment's echo time, rounded to the record's grid: 16.667 becomes 16.668 ms on
2 us), *legs*, *tail us*, *end ms* (the record padded with idle to this period, so the time after leg 2's fall is inside it for
the loop and the optical close to correct; 33.334 is the whole 2 x 16.667 ms echo on the 2 us grid; blank is no pad), *suffix*
(blank is `E<legs>`) and *Write to* (blank is the source's folder). The last settings are remembered. **Build + check** writes
`target_<stem><suffix>X1/X2.csv`, reads both back with `run_ilc.load_target`, puts the flat first shot through the init limit
check (PASS or FAIL per channel; the command line exits 1 on FAIL), logs the record's numbers and the card pause between the
legs, and puts the new X1 in the Target box with its name in the stem box. An identical file already on disk is left alone.

The record for S47 at 16.667 ms: `target_S47E2X1.csv` / `X2.csv`, 13741 points, 27.482 ms period, legs at 0.200 and 16.868 ms
(without the end pad; with `end_ms` 33.334 it is 16667 points). The transport is where the target is above 0.1 % of its max,
widened to the whole ramp (S47: 200..10612 us). The comment lines carry the sources, the spacing, each leg's start and the
transport's rise / hold / fall, plus machine-readable lines the comparison and the card split read: `# spacing_ms:`,
`# legs_ms:`, `# lead_us:`, `# tail_us:`, `# pad_us:`, `# end_ms:` and `# transport_us:`. It refuses a spacing shorter than the
transport, sources on different grids and a source that already holds two transports.

**Two caps.** Stem: pair mode appends `X1` / `X2`, so a stem is at most 5 characters; `ilc_batch` cuts longer ones to 5 and
refuses a collision (`SF130E2` would train as `SF130`), so shorten the `--suffix` instead; the dialog warns past 5. Points: the
panel uploads at most `AWG_MAX_PTS` (8 Mpts, environment `BK4063B_MAX_PTS`), the 4063B's arb memory measured 8 Oct 2026 by the
polarimeter; the 16384 that stood here until 14 Oct was the DS345's. A 133 ms echo is 66500 points and fits. The generator's
true ceiling beyond what has been played (7001 points by the polarimeter, then the 16667-point spin-echo records) is
unprobed: a refused upload means lower the cap and re-measure.

### 7.2 The card split

```
C:\ProgramData\anaconda3\python.exe tools\split_for_card.py run\keep\drive_S47E2X1_keep.csv run\keep\drive_S47E2X2_keep.csv --targets <suite>\targets\target_S47E2X1.csv <suite>\targets\target_S47E2X2.csv [--n 2500] [--settle-us 200]
```

It finds the transports in the targets and writes four files per channel, `<drive>_leg1_upramp.csv`, `_leg1_downramp.csv`,
`_leg2_upramp.csv`, `_leg2_downramp.csv`, both channels cut at the same times per leg, each leg around its own common flat
stretch. A one-transport record keeps the old `<drive>_upramp.csv` / `_downramp.csv`, cut exactly as before. Per leg it prints
each file's span (program exactly that ramp time) and the card hold; between legs, the pause the record implies. The formulas
and the S47E2 arithmetic (up 4.970 ms, card hold 0.872 ms from the 1.272 ms common flat stretch less 2 x 200 us, down
4.970 ms, pause 5.856 ms, leg 2 the same) are in the handbook, section 6.4. A batch plan's `card` block cuts the same way; the
suite's B2S25 gives eight files and a 102 us pause.

### 7.3 Bench trigger for echo records

The record repeats on every trigger, and the crystal remembers the previous *shot* as well as the previous leg (creep tau ~70
ms, the 7 Oct hold series). The experiment runs at ~6 s, so its leg 1 always meets a relaxed crystal: the bench must be slow
enough that its does too, at least ~0.5 s between shots (7 tau), 1 Hz to be safe, not the 20 Hz or 3.7 Hz of the
single-transport campaigns (handbook, section 2.2). Set the plan's `timing.trigger_hz` to match.

---

## 8. The optical close by hand

The ILC makes the **monitor** follow the target. What the experiment needs is the **light**, and the light does not do exactly
what the monitors say (5 Oct 2026: light minus monitors +-2-3 degrees over the ramps, a static function of rotation plus a
7 us lag). A correction moves the target by minus that error; the loop then learns the drive that puts the monitor on the moved
target, and the light lands on the original target. Moving the *drive* instead does not work: the next ILC update pulls the
monitor back onto the old target. The correction must be **light minus monitors**, not light minus target: the loop removes
the monitor's own error by itself, so a correction built on light minus target removes it twice and puts it back with the
opposite sign.

Nothing here is specific to polarization or to the EOMs. A correction file (`eomilc/corrections.py`: first line
`# EOM-ILC target correction v1`, a `# key: value` header, then `time_us,delta_V,sigma_V`, volts in the target CSV's units) can
come from any measurement of any outcome, for any channel GEN included; the panel checks it against the session's own limits.

The handbook (section 6.6) names the seven steps. The batch runs the same library (`eomilc/optical.py`) over every shape of a
plan, unattended (section 6.13); its design is `docs/history/2026-10_OPTICAL_CLOSE_DESIGN.md`.

### 8.1 Both crystals playing

Pair mode, both chains together (the experiment plays them together, and each Trek keeps its own model). Converge the pair
first (Run tab), Auto-set with both outputs off, close the polarimeter's window (the analyzer is on COM3 and the scope is one
program's at a time), make sure the PD is on screen and in the Scope channels map, then on the Optical tab:

1. **Play**, *both*: both current drives uploaded, both outputs ON (one question). They stay ON until step 7.
2. **Scan**: the analyzer steps through the angles while the keepers play; the scan lands in `run\optical\<stem>_optN\` in the
   polarimeter's own format (its data viewer opens it).
3. **Compare**: light minus monitors and light minus target per leg, a correction file per crystal, the Optical plot tab.
4. **Apply**: both targets move by the gated correction times the gain (under 1 for the first pass: the statistical sigma is
   small, but leg-to-leg and up/down differences of 0.1-0.2 degrees are not in it); the base targets are kept.
5. **Learn**: the pair loop learns the moved targets, outputs still ON.
6. **Verify**: a second scan and comparison with the before / after table. The number to read is light minus **target** (the
   base target): light minus monitors is the crystal's and stays; what the close removes is the light's distance from the
   target the experiment wants. For another pass, Apply again: the verification's correction *replaces* the last. Light minus
   monitors does not depend on the correction already applied (to first order), so each scan is a fresh estimate of the whole
   correction, not of a remainder, and stacking them would apply it twice. Then Learn, Verify.
7. **Outputs OFF** (asks first).

### 8.2 Each crystal alone (13 Oct 2026)

A scan with both playing sees one residual, light minus what both monitors predict, and *split X1* shares it out by a fixed
fraction. That corrects the light, but the files are valid only for that pair of drives, while the errors are each crystal's own
(the 8 Oct creep was X1's alone; in a time-split record the holding crystal's creep shows in the other's window). Measured per
crystal instead, with the **play** radio beside step 1:

1. **Play**, *X1 alone*: X1's keeper plays, X2's channel is parked flat at its idle (`PARK2`, output ON).
2. **Scan** `<stem>_optN_X1`. 3. **Compare**: the whole correction is X1's; EO1's file is held.
4. **Play**, *X2 alone*: `PARK1` on X1's channel, X2's keeper (the outputs are already ON: nothing is asked).
5. **Scan** `<stem>_optN_X2`. 6. **Compare**: EO2's file joins EO1's (the step's note: *EO1 from ..._X1 held*).
7. **Apply**: both files, each to its chain; the log names the scan each came from.
8. **Learn**: both keepers play again.
9. **Verify**: a joint scan `<stem>_optN_verify` (the light the atoms get). With no joint comparison before it there is no
   table: the verdict is logged and is the next pass's before. For another pass measure each crystal alone again, or Apply the
   verification's joint correction (it goes beside the solo files in its own slot, section 6.13).
10. **Outputs OFF**.

A scan taken with one crystal alone must be compared as such; the panel remembers what played at each scan and its manifest
says so (`"solo"`). The batch does the same with `"attribution": "separate"`.

### 8.3 Every field of the Optical tab

Every step runs in the background (Stop, the log and the status line as on the Run tab), opens the scope and the AWG for itself
and closes them again **without touching the outputs**; only step 7 switches them OFF (and a step that dies after switching one
on). A step whose prerequisite is missing is greyed with the reason under it (orange) and refused with that reason if reached
anyway; pair mode off greys all seven; loading or initialising another pair starts the steps over.

| field | what it does |
|---|---|
| **state line** | The pair's stems and iterations; the outputs as last reported; the waveforms the AWG was last given and the play mode (*playing both*, *X1 alone, X2 parked*); the last scan and its verification; the correction files held for Apply (and the scan each came from), their peaks and what Apply put on the targets; the last verdict. |
| **play** *(panel)* | *both*, *X1 alone* or *X2 alone*, beside step 1. Alone: that crystal's keeper plays and the other channel is parked (section 6.13), its output ON. |
| **1 Play (outputs ON)** | Uploads the current drives (`<stem>X1_iNN`, `<stem>X2_iNN`; alone, the partner's park drive) after the bench loop's setup and limit checks (any problem refuses with nothing uploaded), asks once to switch both outputs ON and **leaves them ON** (the Bench line says *kept ON until step 7*). Switching between both and alone with the outputs ON asks nothing. |
| **name** *(panel)* | The scan folder under `run\optical\`: `<stem>_optN` (alone: `_X1` / `_X2`), N the first number without a complete scan, advancing after each complete scan; a typed name is kept. An unfinished scan of that name **resumes**; a complete one is used as it is. |
| **angles**, **shots** *(config)* | `start:stop:step` degrees (`0:359:22.5`, 16 angles) or a list; single HRES shots per angle (8). |
| **Scan / Stop** | Refused when either output is OFF or the AWG holds other drives than the sessions' (Play again). The scan manifest records what played (`"solo"`: X1, X2 or null), and with one crystal alone the hold-null angles come from that crystal's rotation only. Opens the analyzer beside the panel's scope, sets the timebase from 0.5 ms before the trigger to 8 ms after the record with single HRES shots, steps the angles (reference returns, hold-null angles) and puts timebase and acquisition back. The polarimeter's preset is **not** written unless named in Settings. Needs scope-grab-multi (the old checkout is refused by name). Stop saves what was taken; Scan again under the same name resumes. |
| **band Hz** *(config)* | The comparison's low-pass and the correction's band (5000: the 0.3 ms ramp-top feature needs it); must be under each loop's own band or Apply refuses. |
| **PD delay us** *(config)* | The photodiode's own delay, removed before comparing (1.7). |
| **split X1** *(config)* | The share of the correction put on EO1, EO2 the rest (0.5 for parallel shapes); unused for a scan with one crystal alone. |
| **Compare** | `eomilc.optical.compare` on the last scan with both states (their base targets once corrected): per leg light, monitors, target, light minus monitors, light minus target, and `corr_<stem>X1/X2_optical.csv`. A solo scan is compared as such (`solo=`, from what played at the scan, not now): the parked crystal's state goes in flat and only the driving crystal's file is written. Files **accumulate** for Apply (a joint compare sets both, a solo one sets its crystal's and keeps the other's); only a joint compare is a later Verify's *before*. Verdict lines go to the log; the rotation and correction views are drawn. |
| **gain** *(config)* | The fraction of the correction applied (0.7). |
| **Apply to the targets** | `optical.apply_correction` on each loop with its crystal's file (a chain with none is left alone), gated with the Corrections tab's limits at the comparison's band (`active_only` off: the whole record is kept and faded 200 us at the ends), on top of the base target and layers. A file in the same slot **replaces** the last, because light minus monitors is a fresh estimate of the whole error. Solo files take slot `optical` at gain 1; a later joint verification's files go in `joint2`, `joint3`, ... as the joint file minus each crystal's split share of what is applied (section 6.13). The log names each file's scan and slot. All or nothing: a refusal on either restores the other and says why in a dialog. No confirmation: the step is the confirmation. |
| **iterations** *(config)* | Learn's iterations (5); repeats, trigger timeout and the skip tick are the Run tab's. |
| **Learn** | The pair loop (section 5.2) on the corrected targets with the outputs already ON: nothing is asked, an output found OFF refuses before anything is uploaded, and they stay ON at the end (a run that dies parks them, still ON). It uploads both keepers, unparking a parked chain. |
| **Verify (scan + compare)** | Needs both playing; always a **joint** scan. Named `<scan>_verify` (a solo scan's `_X1` / `_X2` dropped, then `_verify2`, ...), same settings, with a before / after table in the log (per leg: ramp-top and first-ms-after-fall extremes of light minus target, its rms, ramp-top light minus monitors, the correction peaks); the status line says whether every leg's ramp-top light minus target **halved**. Without a joint *before* there is no table: the verdict stands alone and becomes the next pass's before. |
| **Outputs OFF (both)** | Asks first (the inputs then float: only with every stage they feed bypassed). The one place the panel switches an output OFF. |
| **Settings...** *(config)* | *points per shot* and *sample us* (2.0): blank points is the screen span over the sample spacing (2 us, the ILC grid, whatever the record length); a number reads out that many points. Blank has been the default since 14 Oct 2026 (20000 was the polarimeter's figure; an older config holding it is reset). *reference every* (9), *settle s* (1.0, before the first shot, bench only), *preset* (written before the scan; it overrides the ILC's V/div and trigger, so leave it blank unless the PD is off screen), *hold-null angles*, *comparison figures* (the fig1-5 PNGs). The analyzer's port and zero come from the polarimeter's config. |

### 8.4 The Corrections tab

Learned changes to the **target** from an error measured elsewhere (the polarimeter), within limits.

| field | what it does |
|---|---|
| **Correction files** | The layers: file, slot (what it corrects; a new file in the same slot replaces the old one), gain, the peak applied after the gate, how much of the active record is resolved, status (added, changed, applied, refused, off). **Add file...**, **Remove**, **On / off**, **gain** with **Set** (0-1.5). |
| **band Hz** *(config)* | Nothing faster than this is applied (the file's own `band_hz` if lower). 2000 by default: the polarimeter is slow and noisy above it, and the ramps' motional band starts at 4.9 kHz. Must be under the loop's own band (`f_cut`, or the FRF's full-strength edge) or Apply refuses. |
| **resolved at k sigma** *(config)* | Where the low-passed correction is under k times its own low-passed standard error (the file's sigma column) it is held at zero: that part is the measurement's noise. 3 by default. |
| **floor V** *(config)* | Smaller than this is held at zero: below what the monitor average can verify (dithered 64-shot mean ~0.05 mV; 0.1 mV is 0.1 V at an EOM). Volts at the output; 0.1 by default. |
| **cap V** *(config)* | The largest total change allowed, volts at the output (300 by default). Past it Apply refuses: lower a gain, or raise the cap on purpose. |
| **end fade us / only where the target is active** *(config)* | Both record ends fade to exactly zero (200 us); with the tick (on by default) the correction is zero wherever the base target sits at its idle level (the AWG holds the first sample between bursts). |
| **Preview** | Gates every switched-on file against the BASE target, checks the total, logs the numbers and draws the *Target corrections* plot tab. Changes nothing. |
| **Apply to target** | Preview, then: refuse on any failure (wrong channel, another target's grid or values, over the cap, band above the loop's, the estimated drive past the Trek limits, the AWG rail or the +-full-scale upload mapping); otherwise confirm with the numbers and set target = base + total. The base target is kept in the state (`base_target`, `corr_total`, `corrections`); the next measurement is against the new target. |
| **Revert to base** | Target back to the base target; the files stay in the box, switched off. |

The gate (`corrections.gate`) puts the correction onto the grid (zero outside its span), low-passes it at the smaller of the
file's band and the tab's, shrinks it to zero where it is under `sqrt((k sigma_lp)^2 + floor^2)`, fades the ends and the
inactive stretch, low-passes again (the weights must not add content above the band), forces the ends exactly to zero and
multiplies by the gain.

### 8.5 The alternative: the polarimeter's window

1. Run the polarimeter scan with the CmdX1/X2 and MonX1/X2 roles (and, for the line ripple, a second scan of the same sequence
   with the ramps disabled; the Optical tab does not take that reference scan).
2. `ramp-polarimeter` -> *ILC target* (or the ramp-polarimeter repository's `tools/target_compare.py`) writes `target_<name>_played.csv` (the ILC target with the
   experiment's timing) and `corr_<name>_optical.csv` per crystal.
3. ILC panel: Init a campaign on `target_<name>_played.csv` and converge it.
4. Corrections -> Add `corr_<name>_optical.csv` -> Preview -> Apply (a gain under 1 for the first pass is the cautious
   choice). Iterate the loop until it has learned the new target.
5. Verification scan and a new correction file. It *replaces* the old one in the same slot (the box asks), for the reason in
   section 8.1 step 6.

---

## 9. Files every step writes

The handbook (section 6.5) lists what a batch campaign leaves behind. For the panel and the command line:

| file | where | what |
|---|---|---|
| `drive_<stem>.state.npz` | `run\` | the campaign, rewritten every step |
| `drive_<stem>_iNN.csv` | `run\` | the iteration's drive, `time_us,voltage_V` (the CLI writes `drive_<channel>_iter<n>.csv` and an `_awg.csv` pair) |
| `<stem>_iNN.csv` | the AWG GUI's `Waveforms\` | upload-ready copy (+-1, Normalise OFF) |
| `meas_<stem>_iNN.npy` | beside the state | the iteration's averaged measurement; bench mode and Step both save it, so every measured iteration reloads with the session |
| `meas_<stem>_iNN_rMM.npy` | beside the state | a Hold run's averaged measurement: run M of iteration N, same drive, no update |
| `meas_<stem>_iNN[_rMM]_native.npz` | beside the state | the same average at the scope's own sample rate (only with *keep native-rate avg*) |
| `meas_<pair stem>PD_iNN.npy` + `_stats.npz` | `run\` | the pair loop's (and the batch's) analyzer photodiode for iteration N on X1's grid, and its sidecar with the analyzer angle; only with a PD channel in the map |
| `frf_<name>.csv` (+ `.png`), `sysid_<name>.npz` | `run\` | a measured transfer function (`f_Hz, H_mag, H_phase_deg, coherence`) and the probe reference a capture is fitted against |
| `ilc_gui.log` | `run\` | timestamped append-only copy of everything the panel logged |
| `<stem>_optN\`, `<stem>_optN_X1\` / `_X2\`, `<stem>_optN_verify\` | `run\optical\` | an Optical-tab scan: the polarimeter's scan folder (Scope Grab captures plus `<name>_scan.json`, readable by its data viewer); the comparison is in its `analysis\target_compare\` (`corr_<stem>X1/X2_optical.csv`, `target_<stem>X1/X2_played.csv`, `summary.json`, `target_compare.npz`) |
| `ni_<channel>_coarse.csv`, `_fine.csv`, `_single.csv` | the current folder | `run_ilc.py emit-ni` |

`run\README.md` has the full taxonomy of `run\` and where finished campaigns get filed. Outputs land in more than one place on
purpose: `run\` gets the loop's own records, the AWG GUI's `Waveforms\` library (a sibling repository) gets a normalised,
upload-ready copy of every drive so it appears in that GUI's memory list and previews, and raw scope captures never come into
the repository at all.

---

## 10. Calibration, and using the loop on another system

### 10.1 Calibration lives in `eomilc/config.py`

Measured 2026-08-20/21, with the optical constants added 31 Aug - 1 Sep 2026. **Update these when the hardware changes.**

| | EO1 | EO2 |
|---|---|---|
| divider (AWG -> Trek in) | 0.6254 +- 0.0038 | 0.6103 +- 0.0037 |
| Trek in -> monitor (`amp_mon_product`) | 0.8926 | 0.9707 (1.0011 until 1 Sep 2026) |
| fn at full scale (ramp fit) | 2326 Hz | 2207 Hz |
| zeta at full scale (ramp fit) | 0.206 | 0.209 |
| noise at Trek input | 144 uV rms | 624 uV rms |
| fine trim channel | none | 1:100 op-amp summer |
| V90 at the monitor (the 90-degree point) | 5128.3 V = 9.168 V commanded | 5137.4 V = 8.672 V commanded |
| command -> HV gain, measured optically | 0.5594 | 0.5924 |
| EO zero (the rotation's zero offset) | -20.7 V | -8.7 V |
| hysteresis (rise against fall, monitor against light) | 0.21 % | 0.047 % |

The fn / zeta rows are the large-signal ramp-fit values; `characterisation/results2.json` is the direct source of the
`fn_pts` / `zeta_pts` tables. They seed the command-line drivers' model (their group delay is
right) but the loop runs on the measured FRF, which supersedes them; the panel no longer reads these tables.

**The Trek mismatch, as resolved optically (31 Aug - 1 Sep 2026).** The old table showed EO1's monitor-over-input at 0.8926
against EO2's 1.0011, a 12 % difference, and asked whether the amplifier gain or the monitor was low. The optical campaign
settled which: V_pi at the monitor agrees between the two EOMs to 0.18 % (5128.3 against 5137.4 V), which rules out the
monitors, and EO1 is right as it stands (measured command-to-HV gain 0.5594 against divider times `amp_mon_product` = 0.5582,
0.2 %). EO2's `amp_mon_product` was 3.0 % high; measured optically it is 0.9707 (8.6718 V commanded for V_pi, gain 0.5924).
The chains still differ: reaching 90 degrees takes 9.168 V commanded on X1 and 8.672 V on X2. One contradiction stands
unexplained: EO2's electrical gain table says 0.6076 near 5 V, 2.5 % above the optical measurement, where for EO1 the two agree
to 0.13 %. One optical point should not overwrite a six-point FRF table, so the table is left alone; do not average them.

**Limits.** `Channel.limits` carries the guard per channel. EO1 and EO2 have the Trek 610E numbers: AWG and Trek-input rails
10 V, slew 20 V/us, current 2 mA, 6 kV at the EOM, and the 100 mV cap on the record's first and last sample. GEN has the open
set (the +-10 V generator rail only). **`LIMITS.load_capacitance` defaults to 200 pF and is a guess** (EOM plus cable plus
strays): measure it and set it, or the current guard means nothing.

### 10.2 A system that is not the Trek chains

The loop knows nothing about Treks or EOMs; that knowledge lives in the channel definitions. The **`GEN`** channel is the blank
one: unity divider, `mon_scale` 1 (target CSVs are read in the same units the scope measures, no x1000 monitor convention), no
calibration tables (asking it for gain, tau, fn or zeta raises instead of interpolating Trek numbers), and no auto-loaded FRF.
Nothing measured on this bench applies to a GEN session unless it is loaded on purpose. Starting from nothing:

1. **Channel** -> `GEN`. (For a Trek channel use `EO1` / `EO2` and skip to the handbook.)
2. **Target**: **Build...** a cosine-edged ramp or half-sine (peak in output units, segment lengths in ms), or browse to any
   `time_us,voltage_V` CSV. It plots itself on selection: the file on top and, once a gain is typed, the AWG output the flat
   first shot would produce against the +-full-scale rails, with the DAC-range percentage and a clipping warning. Nothing is
   sent by the preview, so validate the shape there first.
3. **Name stem** at most 7 characters (section 1.1).
4. Type a **first-shot gain** guess (output over drive) and set **Model** to *gain only (0th order)*. The first-shot gain and
   the model gain are separate knobs: the first fixes what iteration 0 plays, the second belongs to the error correction, so
   tuning or refitting the model later never rescales the first shot. Either box alone is enough to start (each falls back to
   the other, logged). Guess the gain **high**: the loop converges for gamma times g_true over g_model under 2, so a high guess
   only makes the first correction small.
5. **Init**: the first shot is a flat conversion (target over the first-shot gain, no pre-distortion), so the first
   measurement shows the chain's raw response directly. The state file exists from here on.
6. Run one iteration, then **Replace values with a fit... -> Fit to measurement**: the typed guess is replaced by the gain
   identified from the drive/response pair that actually played. Once an FRF exists (**Measure FRF...**, or
   `tools\sysid_make.py` plus `tools\sysid_fit.py`, all system-agnostic), *Fit to FRF* gives the dynamic terms of whichever rung
   the residual demands, with the frequency past which that rung stops contracting. Step up the ladder (section 3.2) as the
   residual demands.

What does not genericize: bench mode's instrument layer is the BK4063B plus MSO-X pair. Another bench brings its own upload and
capture path; the manual step-from-captures loop works with any scope CSV, and the 11-character waveform-name cap is the
4063B's.

### 10.3 Off the bench

On a Mac or any machine without the instruments everything except the bench buttons works:

```
pip install numpy scipy pandas matplotlib
python ilc_gui.py
```

Load state, Step from captured CSVs, Build and preview targets, Fit, the Compare box, every plot tab and the Table all run
offline. `pyvisa` is imported only when a bench action (Auto-set, Upload, Bench loop, Hold) is pressed, so it need not be
installed; those actions need a VISA layer plus the instruments, which live on the bench PC. Campaign data is not in git: copy
the `run\` folder (state, `meas_*.npy`, drive CSVs) over and point **State** at it. The config lives in
`~/Library/Application Support/EOM-ILC-GUI/` there instead of `%APPDATA%`.

---

## 11. RIN and the SR760 protocol runner

### 11.1 `eomilc/rin.py`

Analysis for out-of-loop intensity noise. Pure numpy, so it runs on the system interpreter.

- `splice_segments` merges traces taken at different spans, locked ranges and analog pre-filters, applying each segment's own
  `|H(f)|^2` first. It **returns the overlap disagreement rather than blending it away**: neighbouring segments are the only
  cross-check the chain has, so the step at the join is the honest size of the filter-model, range-calibration and analyser
  error together. (spectrum_grab's start-frequency stitcher is a different operation: it joins bands taken at one span and
  range, where the bin frequencies are shared exactly.)
- `filter_response(dark_with, dark_without)` measures the pre-filter in situ from a pair of dark spectra, which catches the
  cable loading a swept measurement misses; feed it back to `splice_segments`.
- `rin` / `rin_from_psd` / `shot_noise_rin` / `integrate_rin`. `rin` takes an AMPLITUDE density in V/rtHz (the SR760's unit) and
  squares it; `rin_from_psd` takes V^2/Hz. Mixing them up is a factor of two in dB.
- `power_scaling_fit` separates electronics, shot noise and classical RIN by their scaling across an ND sweep and returns the
  transimpedance gain `G = b/(2q)`. **Check `shot_dominance` before treating that gain as a calibration**: the terms separate
  only where shot noise is the biggest thing in the measurement (synthetic data: shot at 0.41 of the total, a 1-2 %
  perturbation moved the gain by +53 %).
- `johnson_check` fits `S = S_floor + 4kT R` across a resistor set and reports the deviation from 1.657e-20 V^2/Hz/ohm.
  Band-average each resistor over its own flat region first (`band_average`): 100 kohm rolls off above ~2 kHz against the input
  and cable capacitance.

Both fits weight by 1/S (`relative_sigma`), because the uncertainty on an averaged spectral density is fractional.
`eomilc/scope.py` has a one-sided auto-PSD in V^2/Hz with ENBW-correct window normalisation, RMS-averaged over the shot stacks
`ilc_bench.capture_all` returns; `Spectrum.asd` is its square root, so a scope PSD and an SR760 trace compare directly in the
30-95 kHz overlap (the SR760 stops at 100 kHz; the servo bump at 150-300 kHz is only visible to the scope). `n_indep` counts
segments that did not share samples, so `rel_err` stays honest under `noverlap`. `ilc_bench.noise_capture` puts the scope into
an AC-coupled high-sensitivity configuration and restores coupling, V/div and offset on the way out; the polarimetry captures
need DC coupling (the DC level is what gets inverted to an angle), so leaving the noise setup applied would measure the wrong
thing silently.

### 11.2 `run_protocol.py`

Drives a declarative measurement set on the SR760 for the RIN validation.

```
python run_protocol.py --list
python run_protocol.py --set C --dry-run
python run_protocol.py --set A1 --outdir run/protocol --v-dc 1.85
python run_protocol.py --set C --scope-ch 3 --outdir run/protocol
```

Other flags: `--addr` (the SR760's VISA address), `--navg`, `--settle-recs`, `--a2-spans`, `--a2-range`, `--a3-spans`,
`--target-s` (8.0), `--scope-addr`, `--vdc-scale` (1.0 V per division). A set is a name, a range policy and an ordered list of
traces (span, start frequency, filter id, average count, notes). Per set the runner ranges once, pins with `pin_range()`, and
verifies with `input_range()` before every save; a range that moved marks the trace `SUSPECT` rather than saving it clean. It
records elapsed time, `record_stats` (T_rec, N_indep, rel_err), the overload state and the full settings snapshot, and writes
through `sr760`'s own writers so the files match panel captures.

| set | what it is | why |
|---|---|---|
| `A1` | four resistors, 50 ohm / 2k / 10k / 100k, one shared pinned range | the fitted 4kT slope calibrates the voltage-noise scale (`rin.johnson_check`) |
| `A2` | every input range on a 50 ohm termination | where the analyser's own floor sits per range; confirms the 2 dB range grid |
| `A3` | one resistor across three spans | a PSD is span-independent, so any span dependence would forge a false slope in a spliced trace |
| `C1` | range-step pairs, each band repeated two notches down | where the two disagree is what a segment disagreement measures |
| `C` | four spans, four filters, four pinned ranges, nested from 0 Hz | the RIN segment set: splice with `rin.splice_segments` |

Each set writes one `manifest.json` (per segment: path, span, pinned range, filter id, v_dc, N_indep, overload flag).
`load_segments()` reads it back as `rin.Segment`s and **squares the CSV column on the way** (amplitude density in V/rtHz to
power density in V^2/Hz; the conversion must happen exactly once). Suspect traces are dropped by default: a trace whose range
slipped is not a worse measurement, it is a different one. `--dry-run` prints the plan and a wall-clock estimate and imports no
instrument code, so `tests/test_protocol.py` runs it on the bare system interpreter. The estimate is `NAVG * T_rec`, true only
because the protocol preset sets `OVLP 0`; 100 averages at the 24.4 Hz span is 27 minutes.

The SR760 is built through `ilc_bench.make_analyzer` on `_shared_rm`, and the scope through `make_scope` on the same manager:
the C-phase needs the MSO-X too, and a second ResourceManager half-loads and returns `VI_ERROR_ALLOC` from every
`open_resource`. `find_spectrum_grab()` locates `sr760.py` the way `find_scope_grab()` locates `scope_grab.py`
(`SPECTRUM_GRAB` overrides).

**V_DC from the MSO-X.** RIN is `S_V / V_DC^2`: a V_DC 5 % wrong makes every RIN in the campaign 10 % wrong. `--scope-ch`
measures it on the scope; `--v-dc` overrides. `ilc_bench.measure_vdc` measures it **DC-coupled** (a channel left AC-coupled at
10 mV/div has the level far off screen, where the scope returns `9.9E+37`), snapshotting and restoring coupling, scale, offset
and display state. Two passes on an 8-bit scope: a coarse one at `--vdc-scale` per division, then a fine one offset to mid
screen at a twentieth of the scale (0.3 % coarse alone, 0.03 % refined, on a simulated scope). The fine value is accepted only
if it agrees with the coarse one to 5 %; `9.9E+37` is never returned as a voltage and an off-screen level raises. The level is
read before and after the traces and both go in the manifest with the drift; the runner reports the drift doubled (RIN goes as
1/V_DC^2) and says so when it exceeds 2 %. `tests/test_protocol.py` is 54 offline checks, `tests/test_rin.py` 87 synthetic
end-to-end checks (either interpreter).

---

## 12. Tests

`tests/run_all.py [<suite>/targets] [-q] [--only smoke,batch,...]` runs every suite in parallel lanes as separate processes
(each builds its own Tk root and scratch folder), prints one line per suite and exits 1 when anything failed. Run it with the
Anaconda interpreter. About 6.5 minutes of wall time with all lanes at once on the bench PC (23 Oct 2026); it was about 21
minutes until the alignment cross-correlation went to an FFT (`np.correlate`'s direct sum took ~10 s a call). Nothing in the
simulated-bench suites is evidence about the real chain.

| lane | suites (names as printed) | needs |
|---|---|---|
| `smoke` (~250 s) | `smoke`: `tests/smoke_test.py`, the panel against MKJX1 campaign data | the committed fixture `tests/data` (a minimal MKJX1 set, ~220 KB) |
| `batch` (~280 s, then ~120 s) | `batch`: `tests/test_batch.py`; then `gui-batch`: `tests/test_batch_gui.py` | the targets folder (below). `gui-batch` follows `batch` in the same lane because its "Stop mid-campaign" block is timing-sensitive and failed when the smoke suite shared the machine |
| `library` (seconds to a minute each) | `corrections`, `diagnostics`, `monitor`, `polarimetry`, `rin`, `scope-files`, `protocol`, `protocol-sr760`, then `optical` (~100 s), `tools` (~30 s), `safety` (~40 s) | `optical`: the ramp-polarimeter checkout beside this one, scipy and the targets folder; `tools`: the targets folder; `protocol-sr760`: the Spectrum-grab checkout, loaded by path |
| `finetune` (~70 s) | `python -m unittest discover eomilc_polarization_finetune/tests` | nothing beyond the package |

The targets folder is the 28 Sep 2026 ramp suite's, by default `%USERPROFILE%\OneDrive - The Pennsylvania State
University\Share Data\Ramps\suite-20260928\targets` when it exists; without it the suites that need it are skipped and say so.

- `test_batch.py`: the ordering rules (FRQ never moves under a live output; uploads alternate slots; reruns skip finished
  campaigns and resume interrupted ones; Stop ends cleanly; a mis-set generator is refused with nothing switched on); the
  optical stage (two passes, Stop in pass 2 and a resume that does not repeat pass 1, the states' correction keys, a refused
  correction leaving the electrical keeper); separate attribution (the `PARK2` / `PARK1` uploads before each solo scan, each
  crystal's file from its own scan, the keepers back before the learn, the joint verification, `joint_before` false); the PD
  (files and sidecars, `pd_peak_v` / `pd_change_mv`, the early shot count with repeats 8 and repeats_early 2, the dry-run line,
  the analyzer read against a stubbed mount: read once, closed, never moved, NaN when held, and a map without a PD).
- `test_batch_gui.py`: the Batch window (plan builder, Check plan, Simulate, Stop, the confirmation, following, reopening),
  with one Tk root, the config redirected to a temporary folder and dialogs stubbed.
- `test_safety.py`: the never-float rules (a run ends parked; OFF only on an explicit confirmed act) and the guards that keep a
  non-finite or mis-set drive away from the AWG, one block per rule, each naming the 15 Oct 2026 review finding it covers.
- The rest: `test_tools.py` (echo builder and card split), `test_optical.py` (the library on the simulated polarimeter),
  and synthetic checks of the analysis chain.

**One block of the smoke suite.** `tests/smoke_block.py 55` (or `54 55`) executes just those numbered blocks against a freshly
loaded S92PX1B session (copied from `run\*S92PX1B*` on the bench PC into a scratch folder) in about 20 s; blocks from [54] on
stand alone. Run the whole suite once at the end. The suite is fast because the panel rasterises only the figure on screen.
