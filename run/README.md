# run/ — the active campaign workspace

Everything in here belongs to a campaign that is still being iterated.
Finished campaigns get filed under `../archive/` — see the bottom of this
file — so if it is in `run/`, it is live.

Only this README is tracked by git; the data files are bench records and
stay local.

## What the file types are

| pattern | what it is | written by |
|---|---|---|
| `drive_<stem>.state.npz` | **the loop's memory**: target, current drive, plant, gamma/f_cut/t-offset, iteration counter, error history, and (27 Aug on) the model record — `model` key (`static`/`one_pole`/`resonant`/`frf`) plus `frf_path`/`frf_use`/`frf_max` when the measured inverse drives. Loading it resumes the campaign, model included; deleting it ends it. | `run_ilc.py init`, rewritten by every step |
| `drive_<stem>_iNN.csv` / `_iterN.csv` | the drive played at iteration N (`time_us,voltage_V`) | each step / bench iteration |
| `meas_<stem>_iNN.npy` | the averaged monitor trace measured at iteration N, on the waveform grid | `ilc_bench.py` and the GUI's bench loop |
| `meas_<stem>_iNN[_rMM]_native.npz` | the same average at the SCOPE's own sample rate (`t` = waveform time, `y` = monitor V) — kept only when the GUI's *keep native-rate avg* box is ticked; feeds the native-rate spectrum | the GUI's bench loop and Hold |
| `frf_<name>.csv` (+ `.png`) | a measured transfer function: magnitude, phase, per-tone coherence | `tools/sysid_fit.py` |
| `sysid_<name>.npz` | the probe reference a sysid capture is fitted against | `tools/sysid_make.py` |
| `ilc_gui.log` | timestamped append-only copy of everything the panel logged | `ilc_gui.py` |

`frf_WIDE_X1.csv` / `frf_WIDE_X2.csv` are the production inverses from the
24–25 Aug 2026 probe campaign — the GUI points at them by default. They are
the only run\ data files **tracked in git** (a clone needs a measured
inverse); everything else here is untracked scratch.

## Outputs land in more than one place, on purpose

* `run/` gets the loop's own records (the table above).
* The AWG GUI's `Waveforms/` library (sibling repo) gets a normalised,
  upload-ready copy of every drive, so it appears in that GUI's memory list
  and previews — same waveform, different consumer.
* `docs/figures/` holds the committed report figures; the scripts that made
  them are archived with their data in `../archive/report-era/`.
* Raw scope captures never come here at all — they live in
  `..\..\scope_data\`, outside the repo.

## Where the rest went (filed 2026-08-26)

* `../archive/report-era/` — the frozen 24–25 Aug analysis: the SCRX1/SCRX2
  scratch campaigns, the probe-era `sysid_*` / `frf_SYSID*` / `wideprobe*`
  files, the seed states, and the 14 figure scripts (still runnable there;
  they carry copies of the MKJ states/measurements they read).
* `../archive/duplicates/` — byte-identical copies that used to shadow
  `waveforms/target_*.csv` and `docs/figures/*.png`.
