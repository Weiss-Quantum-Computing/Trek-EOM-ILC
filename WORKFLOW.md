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
- **Acquisition: AVER, count 256.** At a 50 ms trigger period that is 12.8 s per
  capture, so a four-iteration run costs about a minute of averaging.
- Transfer points: 10000–20000 for a 10.6 ms record on a 2 µs grid. Keeps each
  CSV near 100 kB instead of 6.6 MB, and 20000 puts the scope grid fine enough
  that `scope.resample` boxcars rather than bare-interpolates.
- Channel name on the monitor channel, so the CSV column is self-describing

Then measure the trigger-to-waveform offset **once**: grab a trace, find where
the ramp starts relative to t=0, and use that number as `--t-offset` from then
on. Do not re-measure it per iteration.

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

- `run\drive_MKJX<n>_iter0.csv` — `time_us,voltage_V`, the loop's own record
- `run\drive_MKJX<n>_iter0_awg.csv` — **the one to upload**, single column, already
  normalised to ±1 against the ±10 V full scale

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
2. Scope Grab: set the prefix to `MKJX<n>_i00`, press **GRAB**
3. Update. One line each, and substitute the `--t-offset` you measured:

   ```powershell
   C:\ProgramData\anaconda3\python.exe run_ilc.py step --state run\drive_MKJX1.state.npz --measured "run\MKJX1_i00*.csv" --mon-col CH3 --t-offset 250
   ```

   ```powershell
   C:\ProgramData\anaconda3\python.exe run_ilc.py step --state run\drive_MKJX2.state.npz --measured "run\MKJX2_i00*.csv" --mon-col CH4 --t-offset 250
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
