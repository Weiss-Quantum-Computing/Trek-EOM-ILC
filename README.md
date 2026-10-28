# EOM-ILC: iterative learning control for the Trek / EOM ramp drive

As it stands on 28 October 2026, this repository trains the drive waveforms of the two Trek 610E to electro-optic modulator
(EOM) chains, X1 and X2, so that the polarization ramps the atoms are given come out as designed. The loop is iterative
learning control (ILC): play a drive, average 64 HRES single shots of each Trek monitor, divide the error by the chain's
measured frequency response H(f), correct the drive, repeat. Both channels are trained together, with both monitors read from
the same shots. The optical close then measures the light itself with the rotating-analyzer polarimeter, moves each target by
the light's error and lets the loop learn the moved target. `ilc_batch.py` runs whole plans of target pairs unattended (the
14-15 October 2026 campaign trained 31 spin-echo records), `ilc_gui.py` does the same step by step by hand, and the closed
drives export as the chunks Supertime's custom-ramp slots take.

The code builds on two bench repositories whose instrument layers it imports instead of reimplementing:
[keysight-scope-grab](https://github.com/Weiss-Quantum-Computing/keysight-scope-grab) (a pre-rename checkout may still sit in a
`scope-grab` folder; the bench code accepts either) and
[BK4063B-AWG-GUI](https://github.com/Weiss-Quantum-Computing/BK4063B-AWG-GUI). The optical close also needs the
ramp-polarimeter checkout (found through the panel config's `polarimeter_path`, the `RAMP_POLARIMETER` variable or a sibling
folder).

## Which document to read

| document | read it to |
|---|---|
| `HANDBOOK.md` | **do** something: start the panel, run a campaign, review it, export it. Task-ordered, with screenshots. |
| `REFERENCE.md` | **look** something up: every field, flag, plan key, file name, limit and measured number, and the physics behind the bench measurement. |
| `docs/ANALYSIS_PROMPT.md` | open an offline data-analysis session: a paste-able brief with the data layout, loaders and measured traps. |
| `docs/history/` | read about finished campaigns (the August measured-inverse report, the September ramp-edit plan, the optical-close design). Index in its own README. |
| `run/README.md` | understand the workspace: what each file type in `run\` is, and where finished campaigns are filed. |
| `eomilc_polarization_finetune/README.md` | use the older standalone optical panel, which has its own documents. |

## Layout

| path | what it is |
|---|---|
| `HANDBOOK.md`, `REFERENCE.md` | the two documents above; `docs/handbook/` holds the handbook's screenshots |
| `ilc_gui.py`, `ilc_gui.bat` | the panel and its launcher (Anaconda `pythonw`) |
| `ilc_batch.py` | the batch driver: a plan of target pairs trained one after another, X1 and X2 together, with per-channel stop rules, keepers, the optical stage and the card files; `--dry-run` and `--simulate` need no instrument |
| `ilc_bench.py` | the closed-loop driver for one channel (upload, capture, update) and the shared instrument helpers |
| `run_ilc.py` | the manual driver: `init`, `step`, `emit-ni` |
| `run_protocol.py` | the SR760 measurement sets for the RIN validation |
| `ilc_hardware.py` | the Hardware tab's one-look device checks |
| `eomilc/` | the library: `config` (calibration), `channels` (scope roles), `plant` (models and fits), `ilc` (the loop), `corrections` and `optical` (the optical close), `echo` (spin-echo records), `supertime` (chunk export), `mains`, `monitor`, `diagnostics`, `polarimetry`, `progress`, `rin`, `scope` (capture reader), `outputs`, `simbench` (the simulated bench) |
| `tools/` | command-line scripts: target builders (`make_target`, `make_ramp_target`, `make_soft_split`, `make_v90_targets`, `make_echo_target`), system ID (`sysid_make`, `sysid_fit`), card and Supertime export (`split_for_card`, `supertime_chunks`), campaign reports (`campaign_report`, `campaign_figures`), the AWG safety nets (`awg_park`, `awg_outputs_off`), and the August-September polarimetry campaign scripts (`pd_check`, `fringe_sweep`, `fringe_both`, `frf_optical`, `filter_check`, `nulltest`, `phase4`, `ringdown`, `pol_scout`, `pol_segments`, `lock_figures`) |
| `tests/` | the offline suites and `run_all.py` |
| `simulation/` | the off-bench validation of the parametric-era loop (`simulate.py`, `make_validation_fig.py`) |
| `characterisation/` | the 20-21 August 2026 analysis behind every constant in `eomilc/config.py` |
| `waveforms/` | the checked-in targets and iteration-0 drives of the early ramps (MKJ, PAR, SER, with the `_V90` rescaled targets) |
| `run/` | the active campaign workspace: states, drives, measurements, measured FRFs. Only its README and the two production FRFs `frf_WIDE_X1.csv` / `frf_WIDE_X2.csv` are tracked |
| `docs/` | `ANALYSIS_PROMPT.md`, `handbook/` (screenshots) and `history/` |
| `eomilc_polarization_finetune/` | a separate, older standalone optical panel with its own README, left as it is |
| `archive/` | superseded bench outputs kept for the record (not tracked), including `report-era/`, the frozen 24-25 August analysis scripts behind the report's figures; it has its own README |
| `.gitignore` | keeps data, captures and `archive/` out of the repository; `.claude/`, `.pytest_cache/` and `__pycache__/` are tooling |

## Interpreters, launcher, tests

Use the Anaconda interpreter, `C:\ProgramData\anaconda3\python.exe`, for anything that touches the bench: it is the only one on
the lab PC with numpy, scipy, pandas and pyvisa together, and the python on the PATH lacks pandas and scipy. `ilc_gui.bat`
starts the panel with Anaconda's `pythonw`. `import eomilc` loads its submodules on first use: `plant` needs scipy and
`scope.load` needs pandas, while `config`, `polarimetry` and `rin` are pure numpy, so `from eomilc import polarimetry` works
on either interpreter and `from eomilc import plant` still raises the scipy ImportError on the system one.

`tests\run_all.py [<suite>\targets] -q` runs every offline suite in four parallel lanes (about 6.5 minutes on the bench PC);
the batch, optical and tools suites need the 28 September 2026 ramp suite's targets folder, and none of them opens an
instrument. The lanes and what each suite needs are in `REFERENCE.md`, section 12.

## Provenance

`characterisation/` holds the scripts, fits and figures behind every number in `eomilc/config.py`, from 40 captures taken on
20-21 August 2026 (the raw captures, about 260 MB, are not in the repository; point `EOM_RAMPS_DIR` at them to re-run).
`docs/history/` holds the write-ups of the campaigns since, including the 24-25 August report in which the measured inverse
replaced the parametric one, and the measured FRFs in `run/frf_WIDE_*.csv` come from that probe campaign.
