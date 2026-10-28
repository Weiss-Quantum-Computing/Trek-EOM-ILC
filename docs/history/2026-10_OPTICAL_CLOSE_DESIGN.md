# Optical close inside the ILC — design (13 Oct 2026)

What is being built, in four parts that land in this order, and the contracts
between them. Read this before touching any of them.

The ILC makes the Trek MONITOR follow a target. The experiment needs the
LIGHT, and the light differs from the monitors by 0.7–1.7° for ~0.3 ms at the
top of every ramp and 0.3–0.95° in the first ms after the fall (8 Oct 2026,
results_2026-10-08.md §5), which a monitor-trained loop cannot see. The
"optical close" measures light minus monitors with the rotating-analyzer
polarimeter, moves the TARGET by minus that (the Corrections tab's rule:
never the drive, which the next update would undo), re-learns, and verifies.
Until now that needed the polarimeter window, by hand, per shape. It becomes
a stage of the batch (every suite shape, unattended) and an Optical tab of the
panel (step by step, checking as you go). Both chains run at once: the two
Trek+EOM chains are different amplifiers and each keeps its own model.

## The pieces

1. `eomilc/optical.py` — the library (agent A; new files only).
2. Pair mode in `ilc_gui.py` — two sessions, two models, one bench loop
   (agent C; in parallel with 1).
3. `ilc_batch.py` optical stage (agent B; after 1 and 2).
4. `ilc_gui.py` Optical tab (agent D; after 1, 2 and 3).

## What already exists and is reused, never rewritten

- The polarimeter checkout `..\ramp-polarimeter` (package `rampol`), importable
  by path (`ilc_hardware.polarimeter_path()` finds it; add it to `sys.path`).
  Its pieces we call:
  - `rampol.hw.ScopeLink(scope)` wraps the ILC's own scope object
    (`scope_grab.Scope`, the same class both programs load by path) — the
    scan code reads through it. `rampol.hw.Rotator(dev)` wraps the ELL14
    (`rampol.ell14.ELL14(port, address=, zero_offset_deg=)`) or
    `rampol.sim.FakeELL14`.
  - `rampol.scan.ScanRun(outdir, name, sg, link, rot, channels, log, progress,
    cancelled)` with `new(plan, steps, extra)`, `run()`, `run_step(s)`;
    `rampol.scan.angle_list/ordered/build_steps/hold_angles`. Scans land as
    Scope Grab NPZ captures plus `<name>_scan.json`, exactly as the
    polarimeter's own, so its data viewer reads them. How a scan is set up
    with drives playing is in `ramp-polarimeter\tools\campaign.py`
    `_ramp_sequence` (scope preset, timebase from the record length,
    sampling, hold-null angles, interleaving) — copy that recipe.
  - `rampol.ilc_target.compare(scan_folder, x1_state, x2_state, f_cut=,
    pd_delay_us=, split=, leg_gap_ms=, scope_grab_path=, eomilc_path=, log=)`
    writes `target_<stem>_played.csv` and `corr_<stem>_optical.csv` per crystal
    into `<scan>\analysis\target_compare\` and returns the summary dict.
    Pass `leg_gap_ms` LARGER than the record (e.g. 1e6) so it does not fit a
    phantom second leg. Correction = minus (light − monitors), low-passed to
    `f_cut`; band 5000 Hz for the 0.3 ms ramp-top feature, PD delay 1.7 µs,
    split 0.5 for parallel shapes.
  - `rampol.sim`: `Bench`, `make(sg, roles=, bench=, zero_offset_deg=)` →
    (scope, ell14, bench) and `FakeAWG(bench)`. The simulated PD sees the
    light of whatever waves the bench's AWG holds. For the ILC's simulation
    the optical library must put the ILC's current drives into that bench
    (both channels) before scanning, so a corrected drive changes the
    simulated light. `tools\campaign.py connect(simulate=True)` shows the
    simulated wiring.
- `eomilc.corrections`: `read_correction`, `check_target`, `gate(corr, t,
  base_out, GateSettings, gain)`, `check_feasible(...)`. The panel's
  `_corr_compute` / `do_corr_apply` in `ilc_gui.py` (search those names) is the
  reference for applying: target = base_target + total/mon_scale, layers
  recorded in `corr_layers`, `history[-1]["target_changed"]`, saved in the
  state (keys base_target, corr_total, corrections).
- `ilc_batch.py`: `build_run`, `prepare_campaign`, `capture_monitors`,
  `decide`, `save_state`, `write_iteration`, `upload`, `keeper`,
  `outputs_off`, `model_line`. Both channels are measured from one frozen
  acquisition and each `ChannelRun` learns with its own loop.
- `eomilc.diagnostics.measurement_stats` + the stats sidecar
  (`App._measurement_stats` in the panel) — every measurement keeps one.
- `eomilc.channels`: the scope channel role map (PD, CmdX1, MonX1, MonX2).
  The PD is `channels.channel_for(cfg, "PD")`.

## Contracts

### 1. `eomilc/optical.py` (agent A)

```python
class Devices:        # what a scan needs besides the drives playing
    sg, link, rot, roles, pcfg, sim      # sim: the rampol Bench or None
def open_devices(scope, cfg, log=print, simulate=False, pcfg=None) -> Devices
    # scope: the ILC's already-open scope object (never closed here);
    # roles from cfg's scope_channels; ELL14 from the polarimeter's config
    # (ell_port/ell_address/ell_zero_deg); simulate=True builds rampol.sim
    # devices instead (then `scope` may be None and Devices.link wraps the
    # simulated scope).
def close_devices(dev)                   # the rotator only
def sim_play(dev, drives: dict)          # {"EO1": u, "EO2": u} AWG volts on the
                                         # record grid -> the simulated bench's
                                         # light; no-op on real hardware
def scan(dev, outdir, name, t_grid, log=print, progress=None, cancelled=None,
         angles="0:359:22.5", shots=8, points=20000, ref_every=9,
         hold_null=True, ends_deg=None, settle_s=1.0, extra=None) -> str
    # the drives are PLAYING (caller's job). Sets the scope span around the
    # record (0.5 ms before, 30 ms after), sampling, builds the steps
    # (hold-null angles from ends_deg = {"X1": deg, "X2": deg} when given),
    # runs the ScanRun, returns the scan folder. Resumes a scan on disk.
def compare(folder, x1_state, x2_state, band_hz=5000.0, pd_delay_us=1.7,
            split=0.5, log=print) -> dict
    # rampol.ilc_target.compare with leg_gap_ms past the record; returns its
    # summary plus "files": {"EO1": corr_path, "EO2": corr_path},
    # "played": {"EO1": played_target_path, ...}
def apply_correction(loop, t, u, full_scale, corr_path, gain=1.0,
                     settings=None, base_target=None, layers=None) -> Applied
    # the panel's _corr_compute + apply, headless: gate, check_feasible,
    # refuse on any "fail" note (raise CorrectionRefused with the notes),
    # else return Applied(target, base_target, corr_total, layers, notes,
    # info) with loop.target REPLACED (base + total / mon_scale) and
    # history[-1]["target_changed"] set. settings: corrections.GateSettings
    # (band from the file if lower; defaults as the panel's: k 3, floor 0.1 V,
    # cap 300 V, end fade 200 us, active-only).
def verdict(summary) -> dict
    # the numbers a before/after reads: per leg the ramp-top extreme, the
    # after-fall extreme (first ms), light-target rms, correction peak per
    # crystal (mdeg and V at the EOM) — from compare's summary.
```

Tests `tests/test_optical.py` (Anaconda python; the polarimeter checkout must
be present): simulated devices, drives from two of the suite's targets (any
`target_<stem>X1/X2.csv` pair, path as argv[1], else skip with a message),
a scan of a few angles with 2 shots, compare → both corr files exist and carry
`channel` EO1/EO2; apply onto a Loop → target moved by the gated amount, base
kept; a refused apply (cap 1 V) raises; `verdict` returns finite numbers.

### 2. Pair mode in the panel (agent C)

- Session tab: a **Pair (X1 + X2)** tick. On: the State and Target rows take
  the X1 file and the panel derives X2's by name (`...X1...` → `...X2...`,
  e.g. `target_S47X1.csv` → `target_S47X2.csv`, `drive_S47X1.state.npz` →
  `drive_S47X2.state.npz`); stem = the X1 stem with X1/X2 appended. Load and
  Init act on both. `self.pair = {"X1": Session, "X2": Session}` (None off);
  `self.session` is the SHOWN one; a `shown: X1 | X2` radio in the plot bar
  switches it and redraws. Off: exactly today's behaviour.
- Model tab: in pair mode two columns, `X1 | X2`, the second from a parallel
  set of variables (`*_2`); each channel's field memory as today; `Replace
  values with a fit...` per column. `_gather_settings()` returns both.
- Run tab: in pair mode **Run bench loop** plays both drives and reads both
  monitors from one acquisition (`ilc_batch.capture_monitors` or the same
  recipe), updates each session with its own loop, writes both states,
  drive CSVs and stats sidecars, logs both, hints per channel. Outputs ON
  both (one confirmation), OFF at the end, FRQ only with both off. Hold
  likewise. Auto-set sets both AWG channels and all four scope channels.
- Everything else (Scope, Hardware, Corrections on the shown session) stays.
- Smoke block [57] on the simulated bench (`eomilc.simbench.make_bench(frfs,
  channels=app.cfg)`, injected the way `tests/test_batch_gui.py` injects
  `app._connect_pair`): pair Init on a generated target pair in SCRATCH, two
  iterations, both states written, the shown-channel switch redraws. Blocks
  from [54] on must stand alone (`tests/smoke_block.py 57`).

### 3. Batch optical stage (agent B)

Plan key per campaign or in `"optical"` at the plan root:
```json
"optical": {"scan": {"angles": "0:359:22.5", "shots": 8}, "band_hz": 5000,
            "pd_delay_us": 1.7, "split": 0.5, "gains": [0.7, 1.0],
            "iterations": 5, "name": "{stem}_opt{pass}"}
```
After a campaign's keepers: play both keepers (outputs ON), `optical.scan`,
`optical.compare` with the two states, `apply_correction` on both ChannelRuns
(gain = gains[pass]), `iterations` more iterations of the normal loop (same
acquisition, both channels), then a verification scan + compare; next pass
with the next gain (a new file REPLACES the previous in the slot, never
stacks). Everything recorded in the manifest: per pass the scan folder, the
corr files, `verdict` before and after. The final keeper is the last
iteration after the last applied correction. Factor `run_campaign` so the
stage and the panel's pair loop can run "one more iteration" through one
function. Simulated path through `optical.open_devices(simulate=True)` +
`sim_play`; `--dry-run` prints the optical settings. Tests in
`tests/test_batch.py`.

### 4. Optical tab in the panel (agent D)

Numbered steps on a new left tab **Optical** (pair mode required; says so
otherwise): 1 Play keepers (both sessions' current drives, outputs ON, stays
ON until 7), 2 Scan (angles, shots, name; progress; Stop), 3 Compare (band,
PD delay, split) → log + a plot tab **Optical** (per leg: light, monitors,
target in degrees; light − monitors; the correction per crystal), 4 Apply
(gain) to both sessions, 5 Learn N (the pair loop), 6 Verify (scan + compare;
a before/after table from `verdict`), 7 Outputs OFF. Settings in a dialog.
Smoke block [58] on the simulated devices.

## House rules that apply

- CRLF files; `git add` by name, never `-A`; commit messages as the log
  reads (what and why, one paragraph); end with the Co-Authored-By line the
  session prints. Do not push.
- Tests: `tests\run_all.py -q` (Anaconda python, ~100 s) once at the end;
  `tests\smoke_block.py NN` while iterating. A worktree has no `run\*.state.npz`
  fixtures beyond what is tracked; the smoke suite's fixtures are in `tests\data`.
- The scope is one program's at a time; the ELL14 is on COM3; the ILC never
  switches an output ON without asking once, and OFF is where every run ends
  (the FPGA stage must be bypassed for that; it is, on this bench).
- Tk variables and dialogs on the main thread only; workers get plain values;
  results back through `self.msgs` (`("call", fn)`); plots through
  `self._draw(fig)`; buttons into `self._actions`.
- Plot titles are data records; interpretation goes in `_plot_note`. The
  experiment traps neutral atoms; the word for a charged atom never appears.

# Addendum (13 Oct 2026): separate per-crystal measurement

## Why

A joint scan (both drives playing) sees one residual, light minus what both
monitors predict, and splits it between the crystals by a fixed fraction
(`split`, 0.5). The light only needs the sum, so that corrects the light,
but the file is valid only for that pair of drives. The errors are each
crystal's own physics (the 8 Oct creep was X1 only; the ramp-top overshoot
is per crystal), and in a time-split record the holding crystal's creep
shows up in the other's window. So the correction is now MEASURED per
crystal: X1 drives with X2 parked at its idle, then X2 with X1 parked, each
scan compared alone (the whole correction to the driving crystal), both
files applied, the loop learns, and a JOINT scan verifies (the light the
atoms get). The joint verification's own residual is reported, not applied:
the next pass measures solo again. Cost: three scans per pass instead of two.

## What exists (merged on main before the agents start)

- `rampol.ilc_target.compare` reads a state with a FLAT target as "parked":
  the transports, the timing reference, the active stretch and the fade come
  from the leading driven crystal; with one driven crystal `split` is forced
  to it; `summary["solo"]` ("X1" / "X2" / None) and `summary["driven"]`.
- `eomilc.optical`:
  - `park_drive(u)` -> the drive flat at `u[0]`, same length (FRQ, record
    and trigger unchanged; the output stays ON - never OFF to park).
  - `PARK_NAMES = {"X1": "PARK1", "X2": "PARK2"}`: the AWG arb names.
  - `parked_state(path, outdir)` -> a flat copy of a state (what the
    polarimeter is handed for the parked crystal).
  - `compare(..., solo=None | "X1" | "X2")`: with solo the partner's state
    is replaced by its parked copy, split forced, `summary["files"]` holds
    the driving crystal's file ONLY, `summary["solo"]` set. A solo scan
    compared without `solo=` would split one crystal's error over both: the
    caller says which played.
  - `tests/test_optical.py` [7].

## 5. Batch stage, attribution (agent B: `ilc_batch.py`, `tests/test_batch.py`, `WORKFLOW.md`, since merged into `REFERENCE.md` section 6)

Settings: `DEFAULT_OPTICAL` gains `"attribution": "joint"` (today's flow)
and `"joint_before": true`; `optical_settings` refuses anything but "joint"
/ "separate"; `optical_line` says it (e.g. `attribution separate (X1 alone,
X2 alone, then both to verify)`), so the dry run prints it.

`optical_stage` with "separate", per pass p (gain gains[p-1]):

1. Before: on the first pass of this run with nothing carried and
   `joint_before`, a joint scan `<name>_before` + joint compare (split
   `split`) -> `verdict_before`, recorded, NOT applied. Later passes take
   the previous pass's `verdict_after` as their before (the table), as today.
2. Solo scans, X in (X1, X2): the partner's channel gets
   `optical.park_drive(partner.u)` uploaded as `optical.PARK_NAMES[partner]`
   (`ilc_bench.upload_drive`; outputs stay ON; same length so FRQ holds);
   X's keeper plays as uploaded; scan `<name>_X1` / `<name>_X2` (events:
   step "scan", scan=that name); in simulation `sim_play` gets the parked
   drive for the partner (`_sim_drives` takes a drives override; the bridge
   as today); `optical.compare(..., solo=X)` -> that crystal's file. The
   pass record gains `solo_scans {"X1": folder, "X2": folder}`,
   `verdicts_solo {"X1": verdict, "X2": verdict}`; `corr_files` is
   `{"EO1": from X1's scan, "EO2": from X2's}`.
3. Both keepers re-uploaded (both play), both files applied (as today,
   both or neither), `iterations` iterations, keeper per channel as today.
4. Verify as today (joint scan `<name>`, joint compare, `verdict_after`,
   `verify_corr_files` recorded). In separate mode nothing is carried as the
   next pass's correction (`carry` only passes the verdict for the table).

Resume: a pass whose verification scan is complete and recorded done is not
repeated (as today); a solo scan on disk resumes under its name. Outputs OFF
on exit as today.

Tests (`tests/test_batch.py` [11b], after [11], in simulation on S25):
"optical" with `attribution: "separate"`, gains [1.0], iterations 2, PD delay
0, figs off, scan 0:359:45 x 2. Check: the dry run line; folders
`S25_opt1_before` (joint_before), `S25_opt1_X1`, `S25_opt1_X2`, `S25_opt1`;
the pass record's `solo_scans`, `corr_files` EO1 under the X1 folder and EO2
under the X2 folder, `verdicts_solo` finite, both `applied`, `verify_scan`;
the simulated AWG's log shows `("upload", 2, "PARK2")` before the X1 scan
and `("upload", 1, "PARK1")` before the X2 scan, and the keepers uploaded
again before the learn; the X1 scan's manifest `drive.idle_V` has EO2 at the
idle; outputs off at the end; `attribution: "sideways"` refused by
`optical_settings`. `joint_before: false` -> no `_before` folder and
`verdict_before` None. [11] and [12] unchanged.

## 6. Optical tab, solo play (agent D: `ilc_gui.py`, `tests/smoke_test.py`, `WORKFLOW_GUI.md`, since merged into `REFERENCE.md` sections 5 and 8)

- Step 1: a radio `play: both | X1 alone | X2 alone` (`self.optplay_var`,
  values "both" / "X1" / "X2") beside the button, now "Play (outputs ON)".
  `_opt_play_work(ss, wires, which)`: the driving session(s)' keepers as
  today; the parked partner gets `optical.park_drive(partner.u)` uploaded as
  `optical.PARK_NAMES[partner]`, `self._pair_playing[partner] =
  dict(wname=<that name>, u=<parked>, parked=True)`; `self._opt["playing"] =
  which`; the done text says what plays ("X1 keeper S47E2X1_i12, X2 parked
  (PARK2)").
- `_opt_prereq`: steps 2 and 6 want the outputs ON and the AWG holding, for
  each chain, its session's current u (driving) or its park drive (parked);
  step 6 also wants `playing == "both"` ("Verify needs both playing: step 1
  with 'both', or Learn").
- Step 2: the default name ends in `_X1` / `_X2` when playing solo
  (`_opt_default_name`); the scan's drives come from `_pair_playing` as
  today (the parked one included).
- Step 3: `solo = playing if playing != "both" else None` passed to
  `optical.compare`; the missing-file check expects only the driving
  crystal's file. `self._opt["files"]` ACCUMULATES: a joint compare sets
  both, a solo compare sets its crystal's and keeps the other's;
  `self._opt["files_from"] {EOn: scan name}`. `_opt_scan_done` keeps `files`
  / `files_from`; `_opt_reset` clears them. Only a JOINT compare sets
  `o["before"]` (the verify table's before). Done text: "EO1 15.1 V (from
  S47E2_opt1_X1); EO2 from S47E2_opt1_X2 held".
- Step 4, now "Apply to the targets": applies every file in
  `self._opt["files"]` (one or two) to its chain; a refusal puts back what
  was done in this press and nothing is saved (as today); the log says which
  crystal's file came from which scan.
- Step 5: as today; on completion `self._opt["playing"] = "both"` (the pair
  loop uploads both keepers).
- Step 6: joint (solo None) as today. Without a joint before (`o["before"]`
  None, a solo-only flow) the table is skipped with a log line and the
  verdict lines are printed; `o["before"]` then becomes this verdict.
- The state line shows the play mode ("playing both" / "X1 alone, X2
  parked") and the files held ("corrections: EO1 from ..._X1, EO2 from
  ..._X2").
- `_plot_optical` works unchanged on a solo summary.
- Smoke block [60] (standalone, [58]'s stubs and simulated devices; do NOT
  start a comment line inside it with `# [NN]`): play X1 alone -> the AWG
  log shows the X1 keeper on CH1 and `PARK2` on CH2, `_pair_playing["X2"]`
  parked; scan (name ends `_X1`); compare -> files EO1 only, state line says
  so, `before` still None; Verify refused (playing solo); play X2 alone ->
  `PARK1` on CH1; scan (`_X2`); compare -> files both, EO1's kept; apply ->
  both targets moved, one optical layer each, log names both scans; learn 1
  -> playing both; verify -> joint, no table but the verdict logged; off ->
  both OFF. [58] passes unchanged (`tests\smoke_block.py 58 60`).
- `WORKFLOW_GUI.md` (now `REFERENCE.md` section 8): the Optical tab section gains the radio and the solo
  flow (1 X1 alone, 2, 3; 1 X2 alone, 2, 3; 4; 5; 6; 7).

Both agents: a worktree each, CRLF files, `git add` by name, one commit
each (what and why, one paragraph; end with the Co-Authored-By line the
session prints), no push; `tests\run_all.py -q` green at the end (the
targets folder is on OneDrive by default; Anaconda python). Nothing of this
has run on hardware.
