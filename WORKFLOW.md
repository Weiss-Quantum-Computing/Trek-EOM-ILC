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
- **Acquisition: AVER, count 256** — and see the warning below, because setting
  it is not sufficient. At a 50 ms trigger period a full 256 takes 12.8 s.
- Transfer points: **leave it at max**. On the 24 Aug captures the scope returned
  93750 points across a 15 ms window — 160 ns per sample — which `scope.resample`
  boxcars ×12 down to the 2 µs grid, a free √12 noise reduction. Setting a smaller
  number would *reduce* that. 20000 is the compromise if the 6.6 MB files become a
  nuisance: still 0.75 µs per sample, still above the 1 µs threshold where the
  boxcar engages, at a ×3 rather than ×12.
- Channel name on the monitor channel, so the CSV column is self-describing

### Averaging: what GRAB does, and what it actually buys

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
boundaries. That puts the measurement floor at ~2.5 V at the EOM, better than
the 5 V the original package documentation hoped for.
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
C:\ProgramData\anaconda3\python.exe make_target.py --channel EO1 --peak-hv 5200 --step 2 --out waveforms\target_MKJX1.csv
```

```powershell
C:\ProgramData\anaconda3\python.exe make_target.py --channel EO2 --peak-hv 5200 --step 2 --out waveforms\target_MKJX2.csv
```

The target is in volts **at the EOM**, which is what makes two channels with
different Trek gains directly comparable: ask both for 5200 V and each gets
whatever drive its own chain needs. At 5200 V that is 9.312 V on X1 and 8.558 V
on X2, leaving 6.9% and 14.4% headroom against the ±10 V full scale.

The 2 µs grid costs nothing: decimating MKJ_full 20:1 from its native 0.1 µs
departs from the source by 3.2 V peak / 1.3 V rms at the EOM, well under the ~5 V
floor that 256-average 8-bit capture sets anyway.

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

```powershell
C:\ProgramData\anaconda3\python.exe ilc_bench.py --channel EO1 --target waveforms\target_MKJX1.csv --name MKJX1 --awg-ch 1 --scope-ch 3 --t-offset 250 --iterations 4
```

```powershell
C:\ProgramData\anaconda3\python.exe ilc_bench.py --channel EO2 --target waveforms\target_MKJX2.csv --name MKJX2 --awg-ch 2 --scope-ch 4 --t-offset 250 --iterations 4
```

Run them one at a time, not concurrently — they each open their own VISA
session to the same two instruments.

It imports `Scope` and `Awg` straight out of your two programs, so there is no
second copy of the SCPI to keep in step. It uploads, arms, captures, updates,
and writes every drive and measurement as it goes.

It deliberately does **not** set amplitude, offset, load, clock or output state
— you do that in the GUI and confirm on the monitor. Before the first upload it
checks the channel is set up the way the drive file assumes and refuses to run
if it is not.

Both GUIs hold their own VISA sessions, so disconnect them before starting this.

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
