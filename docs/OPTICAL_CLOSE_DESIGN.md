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
  experiment traps neutral atoms; never write "ion".
