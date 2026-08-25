# Running the loop on the bench

Two ways: by hand with the existing GUIs, or headless with `ilc_bench.py`.
The manual route is worth doing once so you can watch what happens.

Run everything with the Anaconda interpreter — `C:\ProgramData\anaconda3\python.exe`.
It is the only one here with scipy, pandas and pyvisa together.

## Waveform names: 11 characters, and this one bites hard

The generator stores an upload as `<name>.bin` and the stored name is what has
the limit: **15 characters accepted, 16 not**, so a typed name gets 11. Past it
nothing is refused — the upload lands, the right shape comes out, and then the
front panel wedges until you power cycle it.

The loop names its uploads `<stem>_i00`, so **the stem gets 7 characters**.
`MKJX1` and `MKJX2` give `MKJX1_i00`, nine characters, with two to spare.
`ilc_bench.py` reads the cap from the GUI's own `MAX_ARB_NAME` and refuses
anything longer.

## One-time setup

**AWG GUI**, on the channel driving the EO:

| Setting | Value | Why |
|---|---|---|
| Wave type | ARB | |
| Clock | **DDS** | TrueArb blocks `BTWV`, and this bench needs the triggered burst. See below. |
| Ampl | **20.00 Vpp** | with Offset 0 this makes ±1 in the file exactly ±10 V out, which is the fixed mapping every drive file assumes |
| Offset | 0 V | |
| Load | HZ | the Trek input is 20 kΩ; declaring 50 Ω halves the real output |
| Burst | ON, NCYC, 1 cycle, trigger EXT | fired by the square-pulse generator at 50 ms |
| Normalise on upload | **OFF** | see below — this one matters |

**Scope Grab**:

- Trigger source and level to match whatever fires the shot
- Timebase so the whole 10.602 ms ramp plus its settle is on screen — 1.5 ms/div
  gives a 15 ms window, which is right
- **Acquisition: HRES** — the standard scheme since 24 Aug is 64 HRES single
  shots averaged in software (see below). AVER is retired for loop captures;
  the section after next records why, and the traps still apply to anyone
  using AVER for anything else.
- Transfer points: **leave it at max**. On the 24 Aug captures the scope returned
  93750 points across a 15 ms window — 160 ns per sample — which `scope.resample`
  boxcars ×12 down to the 2 µs grid, a free √12 noise reduction. Setting a smaller
  number would *reduce* that. 20000 is the compromise if the 6.6 MB files become a
  nuisance: still 0.75 µs per sample, still above the 1 µs threshold where the
  boxcar engages, at a ×3 rather than ×12.
- Channel name on the monitor channel, so the CSV column is self-describing

### The standard scheme: averaged HRES singles

A digitized hardware average bottoms out on a hard 2.5 mV word lattice
(12-bit, an instrument cap — 1024 averages do not refine it), and a lattice is
a *systematic* error the loop learns as real. The scheme that measures finer
exploits the fact that a **single HRES shot carries the same lattice plus
3.5 mV rms of per-shot analog noise** (measured 24 Aug, slow regions of the
real ramp) — noise larger than the lattice step is exactly the dither
condition, so the mean of M separate shots walks off the lattice and beats
down the noise:

| | lattice step | shot noise | floor at the EOM |
|---|---|---|---|
| 1 HRES shot | 2.51 mV | 3.5 mV | ~4 V |
| mean of 8 | 0.31 mV | 1.2 mV | ~1.3 V |
| mean of 16 | 0.157 mV (the WORD floor) | 0.9 mV | ~1 V |
| **mean of 64 (the campaign standard)** | **0.157 mV** | **~0.4 mV** | **~0.5–1 V** |
| digitized AVER-256, for comparison | 2.51 mV systematic | ~0.2 mV | ~2.5 V |

No new tooling needed:

1. Scope: **Acquisition HRES** (not AVER), timebase the full window
   (1.5 ms/div, position +5.3 ms)
2. Scope Grab: transfer points ~20000, then **Sequence** — runs 64, interval
   0, prefix e.g. `ilch_i01`. Sixteen runs is a usable quick check (~1 V
   floor); 64 takes ~25 s at the 20 Hz trigger and is what every campaign
   measurement used.
3. `run_ilc.py step --measured "...ilch_i01*.csv"` — the glob matches all the
   files and `step` averages them on the grid before updating

`ilc_bench.py` does exactly this automatically (`--repeats`, default 64).
Beyond M ≈ 64 the returns diminish — 60 Hz pickup and drift take over.

### AVER, retired: what GRAB did, and where hardware averaging stops

This was the scheme before 24 Aug; it is kept because the free-running-average
trap below still bites anyone who touches AVER, and because its numbers say
where the hardware path ceilings out.

With the scope set to AVER, **GRAB acquires a fresh block of exactly 256
sweeps** (via `:DIGitize`) — **12.8 s at the 20 Hz trigger** — and reads the
record back afterwards. The phase line shows elapsed seconds while it builds.
The "Wait for trigger" field is a stall limit on the trigger itself — it only
fires if triggers stop arriving. If the build comes back short, the log says
so; check `averages taken` in the `.txt` sidecar before feeding any capture to
`step`.

**Never trust the scope's free-running average for ILC.** Under plain RUN this
scope's averager is a *running* average — each sweep folds in at weight 1/256,
so the record keeps an exponential memory of whatever played before, with a
12.8 s time constant at 20 Hz: after a new drive is uploaded, the on-screen
average carries the previous iteration's waveform for a minute or more (a
full-scale change takes ~128 s to fade completely — measured). A digitized
block only ever contains the waveform that played during it, which is why GRAB
uses it.

An averaged record reads back with **7680 points** (the scope serves nothing
longer from a record stopped out of RUN) — 1.95 µs spacing on the 15 ms
window, which lands right on the 2 µs loop grid.

**What averaging does and does not fix.** Watching the screen tells the truth:
averaging beats down random noise but the quantisation staircase on the
display does not soften, because the display and the old BYTE readback are
8-bit — 40 mV per code at 1 V/div, 40 V at the EOM. The average accumulator
underneath is finer, and since 24 Aug Scope Grab reads WORD, which on this
scope serves **157 µV word steps for an averaged record — 16× under the
display code**.

**Measured on the first real averaged capture (24 Aug, 19:48): the slow parts
of the ramp step at 2.513 mV — 2.5 V at the EOM — against 40.2 V for the
single-shot file taken two hours earlier.** The analog noise dithers enough
that the full 16× shows up across the whole record, not just near code
boundaries. That put the hardware-average floor at ~2.5 V at the EOM, better
than the 5 V the original package documentation hoped for — and it is exactly
that 2.5 mV lattice that the HRES scheme above dithers away.

### The model is only trusted below ~5 kHz — keep the Q filter there

Measured 2026-08-24 with the drive's own grass as a broadband probe (coherence
0.93–0.97): **above ~6 kHz the real chain passes 4–8× more than the
second-order model predicts.** The inverse-model update then diverges in that
band — contraction factor 2.6 at 12 kHz — which showed up as drive grass
tripling per iteration (0.4 → 46 → 130 mV rms) while every capture looked
clean. The default 20 kHz Q filter is too wide for this bench.

**Run `step` with `--f-cut 5e3` once; it persists in the state.** Because the
update low-passes the outgoing drive too, the first 5 kHz step also strips any
grass a previous iteration injected (46 → 0.8 mV rms when it was applied).
The real correction lives below 3 kHz and is untouched.

This confinement applies to the **parametric** update only. With `--frf` the
code filters the error at the FRF's own band edge instead of `f_cut` — the
measured inverse is trusted across its whole measured band, and pre-filtering
at 5 kHz in front of it is the integration bug that once left a repeatable
2 V rms residual untouched at 5–15 kHz. See "The measured inverse" below.

### Sequence prefixes are glob prefixes — keep them unique

`step --measured` averages **every** file the glob matches. A sequence named
`ilc_i01` once swept in two earlier one-off captures *and a 200 µs/div zoomed
capture* that shared the prefix; the zoomed record, extrapolated flat over
81% of the grid, manufactured 172 V of fake error out of a real 26 V. `step`
now refuses any capture that does not span the whole waveform and lists every
file it averages — read that list. Give each sequence a prefix nothing else
uses, and give zoomed inspection grabs prefixes the ILC globs can never match.

### The burst-entry transient: the loop cannot fix the idle level

Measured 24 Aug from the pre-trigger portions of the 64-shot sequences: even
with the drive files pinned to zero, the chain idles at **−9 V (X1) and −41 V
(X2) at the EOM** between bursts, and every burst opens with a ~150 µs
relaxation from that level. This is invisible to the loop — the initial state
is set before the record starts — and it shows up as a stuck error peak at
t = 0 (~40 V on X2) that no iteration reduces.

The offsets decompose as: the AWG's own zero-code offset error (−12 mV on CH1,
−40 mV on CH2 at 20 Vpp — within the generator's spec), plus, on X2 only,
~−16 mV at the Trek input that the AWG never sees — the 1:100 summer's fine
port, which is consistent with something holding ≈ −1.6 V there. Two fixes,
either or both:

- **Trim the generator:** set each channel's OFST to cancel its measured idle
  (≈ +12 mV / +40 mV). The loop re-adapts in one iteration; relax the OFST=0
  check tolerance accordingly.
- **Chase the summer:** find what is parked on X2's fine input.

Re-measure the idle from any sequence's pre-trigger data after either change.

### The measured inverse: how both channels got below 0.1%

The parametric model's resonance does not exist at operating signal levels
(SYSID multitone, 24 Aug) — the chain is a smooth rolloff, nonlinear in
amplitude, and it *drifts on hour scales* (a converged drive re-measured
~15–20 V off at the next session's start, shape not gain). The working
recipe, per channel:

1. `C:\ProgramData\anaconda3\python.exe tools\sysid_make.py --peak 2.0 --name <NAME>` and upload it
   (normalise OFF); 64-shot HRES sequence; then `tools\sysid_fit.py` with that
   channel's drive/monitor columns -> `run\frf_<NAME>.csv`.
2. Iterate with the inverse:
   `ilc_bench.py --resume <state> --frf run\frf_<NAME>.csv --frf-use 20e3 --frf-max 24e3`
3. Budget 2–3 warm-up iterations at every session start for the drift.

Landed 25 Aug, after the 80 kHz probe (`sysid_make --f-hi 80e3`, both chains
coherent at every tone) and extended-band iterations
(`--frf run\frf_WIDE_<ch>.csv --frf-use 50e3 --frf-max 75e3`):

**X1 and X2 both at 2.4 V peak / 0.33-0.48 V rms on the 5.2 kV ramp —
0.046% / 0.006-0.009%.** The ">24 kHz distortion" turned out to be mostly
unmeasured linear response; the true floor is the >80 kHz remainder
(0.09-0.16 V rms) plus measurement noise. Uncorrected, the chain was 2.4%:
the loop is worth 53x on peak and >100x on rms.

Session-start drift is real and growing in the record books (up to 32 V on a
converged drive): always budget 2-3 warm-up iterations.

### The trigger offset

**Measured 2026-08-24 on this bench: `--t-offset 0`.** Cross-correlating the
captured CH1 and CH2 against the drive records that produced them puts the
waveform start at −1.4 µs on both, which on a 2 µs grid is zero. That is what
you would expect: the burst delay is 1.26 µs and the scope triggers off the
same external pulse.

Re-measure only if the trigger wiring changes, with
`scope.measure_t_offset(t, ch1, u_drive, 2e-6)`. Do **not** use
`find_trigger_offset` for this — it returns a mid-ramp 50% crossing, and MKJ
leaves zero so slowly that the two differ by more than a millisecond. And do
not re-fit it per iteration: the loop then chases its own alignment instead of
converging.

## Building the target

```powershell
C:\ProgramData\anaconda3\python.exe tools\make_target.py --channel EO1 --peak-hv 5200 --step 2 --out waveforms\target_MKJX1.csv
```

```powershell
C:\ProgramData\anaconda3\python.exe tools\make_target.py --channel EO2 --peak-hv 5200 --step 2 --out waveforms\target_MKJX2.csv
```

The target is in volts **at the EOM**, which is what makes two channels with
different Trek gains directly comparable: ask both for 5200 V and each gets
whatever drive its own chain needs. At 5200 V that is 9.312 V on X1 and 8.558 V
on X2, leaving 6.9% and 14.4% headroom against the ±10 V full scale.

The 2 µs grid: decimating MKJ_full 20:1 from its native 0.1 µs departs from
the source by 3.2 V peak / 1.3 V rms at the EOM — 0.06% of full scale. That
was negligible against the old ~5 V hardware-average floor; against the ~0.5–1 V
HRES floor and the 2.4 V final residual it is no longer free, but note the loop
tracks the decimated target *exactly* — the departure is versus the 0.1 µs
source shape, not an error the loop sees. `docs\MKJ_FULL_NOTES.md` has the grid
arithmetic if a finer grid ever looks worth it.

## The manual loop

```powershell
C:\ProgramData\anaconda3\python.exe run_ilc.py init --target waveforms\target_MKJX1.csv --channel EO1 --name MKJX1 --out run\drive_MKJX1_iter0.csv
```

```powershell
C:\ProgramData\anaconda3\python.exe run_ilc.py init --target waveforms\target_MKJX2.csv --channel EO2 --name MKJX2 --out run\drive_MKJX2_iter0.csv
```

Each writes two files per iteration — below, `<n>` is 1 or 2:

| file | where | what |
|---|---|---|
| `drive_MKJX<n>_iter0.csv` | `EOM-ILC\run\` | `time_us,voltage_V`, the loop's own record |
| `MKJX<n>_i00.csv` | **the AWG GUI's `Waveforms\`** | **the one to upload** — single column, normalised to ±1 |

Between bursts the AWG holds the record's **first sample**, so sample 0 sets
the standing level on the EOM for the whole inter-burst gap. The rule here
changed on 24 Aug: forcing it to file-zero turned out to fight the loop — the
chain's own idle offsets (generator zero-code error + the preconditioning
network) parked the EOMs at −9/−41 V anyway, and the entry transient could
never converge. The loop now sets the first sample **freely within a ±100 mV
cap at the AWG** (`Limits.idle_awg`), letting it trim the chain to a true-zero
idle; the limit check prints the idle level on every drive. The last sample
carries the same clamp. A drive file from anywhere else should keep its first
line within the same ±100 mV before the burst idles on it.

The upload file goes straight into the generator's own library at

```
C:\Users\mzd416\Desktop\BK4063B-AWG-GUI\Waveforms
```

named exactly as the AWG waveform it becomes, so it appears in the GUI's
memory list and previews like any other stored waveform — no copying by hand.
Change it with `--awg-dir`, or the `BK4063B_WAVEFORMS` environment variable.

### Which channel is which

| | AWG channel | scope drive | scope monitor | `--mon-col` |
|---|---|---|---|---|
| **X1** (EO1) | CH1 | CH1 | **CH3** | `CH3` |
| **X2** (EO2) | CH2 | CH2 | **CH4** | `CH4` |

This pairing is what every characterisation script uses — `(1, 3, 'X1')`,
`(2, 4, 'X2')` in `characterisation/analysis2.py` and five others. Note the
scope's own CH3 **name** said "Monitor from Trek X2" in the August captures,
which is wrong: CH3 carries X1. Fix the channel label in Scope Grab before
capturing, or the CSV column will contradict the data in it.

Then, per channel:

1. AWG GUI: **Load waveform** → `run\drive_MKJX<n>_iter0_awg.csv` → name it
   `MKJX<n>_i00` → confirm **Normalise is unticked** → **Upload** to that channel
2. Scope Grab: **one grab serves both channels.** The capture already holds
   all four channels — CH1/CH2 the two drives, CH3/CH4 the two monitors — so
   set the prefix to `ilc_i00` (not per-channel), press **GRAB**, and both
   `step` commands below read the same file with a different `--mon-col`.

   Leave the output directory where you already have it:

   ```
   C:\Users\mzd416\Desktop\scope_data\EOM ramps day 4
   ```

   That keeps raw captures out of the repo, which is what `run/` being
   gitignored is for, and matches how every previous session was filed.
3. Update — one line each. Both read the **same capture**, differing only in
   `--mon-col`:

   ```powershell
   C:\ProgramData\anaconda3\python.exe run_ilc.py step --state run\drive_MKJX1.state.npz --measured "C:\Users\mzd416\Desktop\scope_data\EOM ramps day 4\ilc_i00*.csv" --mon-col CH3 --t-offset 0
   ```

   ```powershell
   C:\ProgramData\anaconda3\python.exe run_ilc.py step --state run\drive_MKJX2.state.npz --measured "C:\Users\mzd416\Desktop\scope_data\EOM ramps day 4\ilc_i00*.csv" --mon-col CH4 --t-offset 0
   ```

   Each writes `run\drive_MKJX<n>_iter1.csv` and its `_awg.csv` pair.
4. Repeat from 1 with the new file. Three or four rounds.

## The automatic loop

Scope in **HRES** (never AVER — `:SINGle` takes one hit of an average),
full window, outputs on, both GUIs closed (they hold the VISA sessions).

Continue from where the manual loop left off — everything (target, drive,
plant, gamma, f_cut, t-offset) comes from the state, and the state is saved
back every iteration, so manual and automatic runs interleave freely:

```powershell
C:\ProgramData\anaconda3\python.exe ilc_bench.py --resume run\drive_MKJX1.state.npz --awg-ch 1 --scope-ch 3 --iterations 2
```

```powershell
C:\ProgramData\anaconda3\python.exe ilc_bench.py --resume run\drive_MKJX2.state.npz --awg-ch 2 --scope-ch 4 --iterations 2
```

Or from scratch (model first shot, then iterate):

```powershell
C:\ProgramData\anaconda3\python.exe ilc_bench.py --channel EO1 --target waveforms\target_MKJX1.csv --name MKJX1 --awg-ch 1 --scope-ch 3 --t-offset 0 --iterations 3
```

Each iteration: upload (fixed ±10 V mapping, never normalised), then
**--repeats 64 HRES singles averaged in software** (~25 s at the 20 Hz
trigger, dithers the 2.5 mV word lattice to its 0.16 mV floor), update,
save state. Drives land in `run\` and the GUI-previewable copies in the AWG
library, exactly like the manual loop.

It deliberately does **not** set amplitude, offset, load, clock or output
state — set those in the GUI and confirm on the monitor. Before the first
upload it verifies the channel against the drive file's assumptions and
refuses on a mismatch (OFST up to ±60 mV is allowed for the idle trim).
## Uploading by hand: two traps, one file

`Awg.upload_arb(..., normalize=True)` divides the samples by their own peak.
That is right for a one-off waveform and **wrong for ILC**. Each iteration's
drive has a slightly different peak; re-normalising every round pins that peak to
whatever `BSWV AMP` says, which silently rescales the correction the loop just
computed. The error stops falling and you go looking for plant drift that isn't
there.

But turning Normalise **off** means the GUI expects samples **already in ±1** and
clips anything past it. A drive file written in volts peaks at 9.3, so 4749 of
its 5301 samples would come out flat-topped at full scale.

And a two-column `time_us,voltage_V` file needs the GUI's column picker to choose
the second column. `time_us` was not in its list of time-axis names until
2026-08-24, so before that fix it picked column 0 and uploaded the **time axis** —
which normalises to a clean ramp and therefore looks like a plausible arb rather
than like a mistake.

The `_awg.csv` file sidesteps all three: one column, already normalised, no
picker involved. Upload that one, with Normalise off.

(Sending a 0–9.3 V unipolar drive against a ±10 V mapping uses about half the DAC
codes, costing a bit — 0.16 V per LSB at the EOM instead of 0.08. Both sit far
below the 5 V loop floor, so it is not worth chasing.)

## Why DDS and not TrueArb

Your AWG GUI documents this from measurement: `BTWV STATE,ON` and `SWWV STATE,ON`
both read back OFF while the clock is TrueArb. You cannot have both a literal
sample grid and a hardware-triggered single shot on the same channel.

This bench needs the triggered shot, so it runs **DDS + Burst**, fired externally
at 50 ms. DDS resamples the stored record into one period, so the 2 µs grid is
not literal — the period comes from `FRQ`, not from points × sample rate. For a
waveform this smooth that does not matter, but check the played length and the
edge timing on the scope once before trusting it.
