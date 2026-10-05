# Polarization fine-tuning

This folder adds an optical layer around an already converged EO1 or EO2
voltage drive. The original library, GUI, drivers and voltage state are
unchanged. This is a separate application that imports their existing voltage
plant, measured FRF, guards and polarimetry equations.

For the motivation, measurement model and design choices, read
[What this folder does, and why](EXPLANATION.md).

On the lab PC, double-click **Start Polarization GUI.bat** inside this folder.
It starts the separate follow-up panel with Anaconda and sets the working
directory automatically. The original `ilc_gui.py` and its launcher are not
modified.

Alternatively, run from the repository root:

```powershell
C:\ProgramData\anaconda3\python.exe -m eomilc_polarization_finetune.gui
```

The GUI starts **after voltage ILC has finished**:

The optional **Manual control** tab is the last tab and also works before loading any ILC results.
Enter the ELL14 serial port (or `auto`) and analyzer zero offset, then click
**Connect** to check the device identity without moving it. Use **Home** after
power-up, **Move to angle** or the **0° / 45° / 90° / 135°** buttons to test rotation,
and **Read position** / **Read status** to inspect the mount. Moves verify the
reported landing angle. Commands run in the background, with results and
errors shown in the panel. No scope or photodiode is needed for these tests.
The port and zero settings are shared with **Setup → Acquisition**; the zero is
applied when connecting, and reported angles use that analyzer frame.
**Disconnect** before capturing light or running the analyzer trace test.
Closing the GUI releases the connection after any active operation finishes.

Tabs are **Fine-tune**, **Plots**, **Setup**, then **Manual control**. The last
tab is optional; the normal optical workflow moves the analyzer automatically.

1. **Import ILC results…**: choose the completed `drive_<name>.state.npz` from
   the existing ILC GUI. The panel imports the final drive, original target,
   EOM channel, time grid, fixed AWG full scale and recorded plant/FRF. It
   displays the waveform and last recorded monitor-error metrics. You do not
   re-enter the voltage target or run voltage ILC again.
2. **Optical acquisition** on the main page: select the photodiode channel and
   analyzer angles, then click **Validate optical acquisition**. This checks
   discrete-angle acquisition and displays raw traces. Add fringe calibration
   files in **Setup → Calibration** to also display converted polarization.
   Mount port, zero offset and other scope wiring live in **Setup → Acquisition**.
3. **Bounded polarization correction** on the same page: choose bandwidth, then
   click **Initialize correction session**. This copies the imported final drive
   into a new optical campaign. If that final drive is already playing, no
   new baseline upload is needed.
4. Select **Single iteration** or **N iterations**, then **Run iterations**.
   Each iteration acquires fresh traces and saves one correction. For multiple
   iterations, upload each exported CSV through the existing AWG GUI, then
   select **Continue after AWG upload**. **Open results folder** locates the files.

## How corrections and iterations work

**Single iteration** acquires the current waveform at the calibrated analyzer
angles, computes one bounded correction and exports the next `pol_iNNN` state
and AWG CSV. **N iterations** requests N such measured correction updates,
starting from the current optical session rather than resetting its baseline.
The sequence pauses after each update except the last. The status line reports
progress and the exact CSV required for the next manual upload.

Before **Run iterations**, the AWG must already be playing the current session's
command. At each pause, upload the exported CSV with fixed full scale and
normalization OFF, review the plots, then select **Continue after AWG upload**.
That acknowledgement starts a new acquisition followed by one new correction;
the sequence never reuses the preceding capture to calculate another step.
The application does not upload waveforms, enable outputs, or modify the
other EOM. Captures are checked against session, iteration, time grid and
saved drive identity. Neither this check nor the upload acknowledgement proves
which waveform is physically playing.

**Stop** ends the sequence at an operation boundary: an active acquisition or
calculation finishes, but no next operation starts. Stopping at an upload pause
is immediate. Acquisition/voltage-check errors stop the sequence; correction
warnings, including rail warnings, also stop it for review. Saved data remain
available. The final exported command is not uploaded or measured automatically:
upload it and use **Acquire optical traces** for a final validation.

For individual operations, **Acquire optical traces** remains available on the
main page; **Compute correction** and **Evaluate measurement quality** are in
**Setup → Files & diagnostics**. A computed update is always one iteration.

For each update, the software:

1. Holds the intended polarization from the original voltage target fixed.
   Fringe calibrations predict the light signal at each analyzer angle.
2. Compares those predictions with the averaged photodiode traces. A joint
   noise-weighted fit using calibrated optical slopes gives a small equivalent
   HV error; optically blind slopes are excluded and errors below the default
   three-standard-error gate are suppressed.
3. Uses the existing voltage plant or recorded FRF inverse to translate this
   error into an AWG correction, with learning gain 0.2. Only slow modes below
   the selected bandwidth remain; the waveform endpoints stay fixed. This
   bandwidth refers to variation within the waveform, not drift over minutes.
   Below the first adjustable mode `1/(2T)`, the correction is zero.
4. Bounds the step and accumulated change from the finished voltage ILC
   baseline, and applies the existing voltage guards. Defaults are 2 mV per
   AWG step, 20 mV total AWG change, and 10 V predicted HV change. Approaching
   these rails produces a warning. Missing optical coverage holds the update
   at zero. The next drive is the current drive plus the bounded update.

There is no automatic convergence stop or guaranteed iteration count. Inspect
the residual and measurement noise after each upload. Stop when the remaining
error is acceptable or unresolved by the measurement; investigate rail warnings
instead of repeatedly pushing against a limit. **Initialize correction session**
starts a new campaign at iteration zero; it is not the next-iteration button.

## Plots in the GUI

**Plots** shows six synchronized panels after session initialization, capture,
or correction: saved AWG drive and the original baseline; original voltage
target and measured Trek voltage; measured photodiode signals at each analyzer
angle with expected calibrated signals; intended and measured polarization;
polarization error modulo 180°; and total/latest AWG correction.
Light means show ±1 standard error; the angle plot also shows its uncertainty.
Trek monitor measurements are converted to HV using the selected channel scale.
The AWG drive panel shows saved commands, not a scope measurement of the AWG.
After a correction, traces still belong to the previous, measured iteration;
the new drive is explicitly marked **not yet measured**. Upload and acquire
again to assess its effect. Matplotlib controls allow zooming and saving plots.
The raw analyzer hardware test retains its separate live trace window.

The required contemporaneous locked-intensity baseline at about −32 ms before
spin echo is documented below and in EXPLANATION.md. Automatic acquisition and
normalization to that baseline are not implemented. Until they are, a
**light-level gate** stands in: see *Light level and scope captures* below.

**Resume correction session…** reopens an existing optical session. The last
voltage case is remembered on launch. To use the other EOM, load its completed
ILC state; the EOM and default monitor/drive channels follow the selected
results. Review physical scope wiring, especially the photodiode input.
Setup contains the correction limits, voltage-to-angle convention, optional FRF
override, report reference/window and output workspace. The main page displays
the active correction limits. Waveform preview and operation details are hidden
until requested; actions become available as results and measurements are ready. A relocated recorded
FRF is found alongside the state or in the repo's `run/` folder when available;
if it is unavailable, select its actual file in Setup → Files & diagnostics.

Corrected drive uploads use the existing AWG GUI, with
normalization OFF, fixed full scale, AMP = twice the state's full scale and
OFST = 0. Set the record frequency to 1/(N dt), with the existing burst
trigger setup. Capture operates the analyzer and scope; the fine-tune app
does not upload or enable the AWG. Ensure the drive being played is the
session's current exported drive. The timing check checks alignment, not
the identity or amplitude of the uploaded waveform. Leave the other EOM's
waveform fixed throughout a fine-tuning campaign.

## Calculated target and calibration

The original voltage target already represents the intended polarization.
The fine-tuner derives the desired angle directly from that same target:

`phi_deg(t) = angle_offset_deg + 90 * (target_HV(t)-target_zero_HV) / HV_for_90`.

There is no separate polarization target file. This relationship defines
the original intended angle, rather than assuming the measured light still
obeys it. At each chosen discrete analyzer angle the app calculates the
corresponding desired photodiode signal, measures the same repeated waveform,
and uses the difference for small corrections. It does not scan the analyzer
or attempt an independent angle measurement at every sample.

The default `HV_for_90` is the selected channel's `v90_hv` from `config.py`
(5128.3 V on EO1, 5137.4 V on EO2). If the original target defined 5200 V
as 90°, enter 5200 so that intended relationship is retained. The default
target zero and fixed angle offset are both 0. These are **target** mapping
parameters, distinct from a measured calibration's EO zero. For a fixed
contribution from the other EOM, enter the corresponding angle offset.

The optical model is
the repo's linear-rotation geometry: Glan → EOMs → aligned QWP → analyzer
→ photodiode. Use the optical sign and EO-zero convention of your setup.
Two linear analyzer settings are sufficient for the small residual in this
calibrated linear-polarization model; they do not establish arbitrary
ellipticity or a full Stokes vector.

At each analyzer setting, supply a `FringeCal` JSON using **EOM/HV volts**,
with `n_eom=1`, obtained by sweeping **only the EOM being corrected**.
The other EOM is held fixed during that calibration. `omega/2` is the
selected EOM's rotation sensitivity in radians per HV volt. `theta_a` is
the analyzer angle in radians from EO zero. The analyzer mount's zero
offset must express that same optical reference. An arbitrary mechanical
home is not automatically the optical zero.

You can fit a recorded sweep using the new CLI:

```powershell
python -m eomilc_polarization_finetune calibrate --sweep sweep0.csv --angle 0 --dark 0.01 --out cal0.json
python -m eomilc_polarization_finetune calibrate --sweep sweep45.csv --angle 45 --dark 0.01 --out cal45.json
```

Sweep CSV headers are `hv_V,light_V`; these are measured Trek HV equivalent
and photodiode volts. Set `--dark` to the measured blocked-beam reading.
Each sweep should span enough of a fringe to constrain its amplitude and
period. Inspect the fit and verify the angle/voltage sensitivity on the
bench. Calibration acquisition is manual; this command fits recorded data.

There is no reference-power channel. Keep illumination, detector gain,
optical path and scope vertical settings stable between calibration and
the sequential 0°/45° measurements. Repeated-shot averaging suppresses
random noise but cannot distinguish repeatable power drift from a
polarization error. Removing the analyzer is not part of each iteration.

**Spin-echo baseline requirement:** with the first leg starting at t = 0,
measure the locked-intensity baseline at approximately **−32 ms**. The lock
normally engages at **−40 ms**, or can engage at **−100 ms** if more settling
time is needed. Repeat the baseline measurement at each analyzer setting for
the associated shot or measurement set. Locked intensity can drift even with
the lock engaged, so **all optical corrections must be referenced to that
measured drift**, rather than a fixed historical light level. This is an
optical reference measurement; the voltage ILC drive remains the baseline
for correction-size limits. Automatic acquisition of this pre-leg window and
drift compensation are not yet implemented; the light-level gate above holds
the correction when the level has moved, but does not normalize it. See the
[timing and baseline notes](EXPLANATION.md#spin-echo-timing-and-the-locked-intensity-baseline).

## Light level and scope captures

**Light-level gate.** A pure polarization change only rotates the two
calibrated quadratures, (I − dark − A)/B at 0° and 45°; their length (the
*contrast radius*) stays 1. A change in the light **level** — locked intensity
drifting since the calibration, detector gain, a stale calibration — moves it
off 1, and the model would otherwise read it as rotation: on a 20° hold, a
0.5 % intensity drift reads as ~24 mdeg. Each step reports the median radius
as `light_level`; when it is further from 1 than **Light-level tolerance**
(default 0.005, Setup → Limits & target, or `--intensity-tolerance` at Init)
the correction is held at zero with a warning. Real rotations pass at any size.
The gate catches drifts above about 0.2 % in intensity; smaller ones still
leak up to ~10 mdeg, which is what the locked-intensity normalization is for.

**Calibration units.** Fringe calibrations must be fitted against Trek HV
(`hv_V`). One fitted against monitor volts would make every correction 1000×
too small; Init refuses a calibration whose v_pi is not within a factor of two
of the channel's measured HV for 90°.

**Offset dither.** Every capture steps the scope's channel offsets across
three ADC codes over the shots (`--dither-codes`, 0 = off) and restores them.
The MSO-X 2014A's per-code error (3.4 mV pk-pk per code at 1 V/div) is the
same in every undithered shot, so it survives the shot mean, passes the
standard-error gate, and on a photodiode hold reads as ~19 mdeg of rotation.
Dithered, it averages to under 1 mdeg. The clipping check allows for the
offsets the dither visits.

## Small correction rails

Defaults, configurable during Init:

| Bound | Default |
|---|---:|
| AWG change in one optical step | 2 mV peak |
| Accumulated AWG change from voltage baseline | 20 mV peak |
| Accumulated predicted HV change from baseline | 10 V peak |
| Optical learning gain | 0.2 |
| Maximum slow correction mode frequency | 100 Hz |
| Resolved-mean threshold | 3 standard errors |
| Light-level tolerance (contrast radius) | 0.5 % |

Each step and the accumulated correction are bounded. A proposed step
that reaches a rail raises a Python warning, prints `WARNING:` in the CLI,
and shows a warning dialog in the panel. Warnings start at 98% of a rail.
The baseline's first and last samples are preserved exactly. The original
AWG rail, post-divider rail, HV, slew, current and idle guards still apply;
simultaneous Trek monitor measurements also pass those guards. HV correction
rails are **predictions** from the voltage response, not a guarantee of the
physical voltage. Confirm the actual small changes on the monitor.

The user selects `--f-cut` (Hz), also exposed as **Slow correction bandwidth**
in the panel. The final correction contains only sine modes up to that
frequency over the record; fast residuals remain diagnostic and do not enter
the drive. Both endpoints remain unchanged. The lowest adjustable mode is
1/(2T), where T is the first-to-last-sample record duration. If the selected
bandwidth is below that, the app warns and holds the correction at zero.
For an 11 ms record the first mode is about 45 Hz. This frequency setting
controls variation **within the waveform**, not the seconds/minutes cadence
between measurement runs. Measurements and corrections occur on demand;
there is no timed unattended drift servo.

The update combines the two calibrated light slopes with repeated-shot
variance weights, masks optically blind samples, and converts the resolved
light residual to equivalent HV error. It uses the voltage inverse to
produce a small AWG correction. Only this correction is filtered and projected
onto the selected slow modes;
the converged voltage baseline is never filtered or relearned. This layer
does not run a simultaneous monitor-error update, which would pull the
drive back toward the old voltage target and undo the optical correction.

Use the measured voltage FRF explicitly if that was your baseline's model;
old voltage states do not always store its path. Without `--frf`, the
recorded parametric plant supplies the inverse. The optical response can
differ from the monitor response, so inspect measured improvement after
each step. A cap warning is a reason to inspect calibration/illumination
and the residual before continuing, not to increase the rails automatically.

## Command-line workflow

```powershell
python -m eomilc_polarization_finetune init --voltage-state run\drive_EO1.state.npz --channel EO1 --cal 0=cal0.json --cal 45=cal45.json --f-cut 100 --frf run\frf_WIDE_X1.csv --out-dir eomilc_polarization_finetune\sessions\case1
```

Init writes `pol_i000.npz`, `pol_i000_drive.csv` (AWG volts versus time)
and `pol_i000_awg.csv` (normalized to fixed full scale, ready for the
existing AWG GUI). Upload that drive first, then:

```powershell
python -m eomilc_polarization_finetune capture --session eomilc_polarization_finetune\sessions\case1\pol_i000.npz --port COM5 --zero 12.3 --pd-ch 2 --mon-ch 3 --drive-ch 1 --home --out eomilc_polarization_finetune\sessions\case1\capture_i000.npz
python -m eomilc_polarization_finetune step --session eomilc_polarization_finetune\sessions\case1\pol_i000.npz --capture eomilc_polarization_finetune\sessions\case1\capture_i000.npz --out-dir eomilc_polarization_finetune\sessions\case1
```

The example wiring is explicit, not a requirement. Home after power-up;
omit `--home` on subsequent captures. Step writes `pol_i001` and reports
optical-error RMS, observable fraction, step size, total change and rail
scaling. The session also stores the equivalent optical-error trace, its
standard error and observable mask. Use a fresh capture of the newly
uploaded drive for every step. Capture/session ID, iteration, time grid,
drive fingerprint and analyzer landing angle checks reject stale or
mismatched files. This metadata check assumes you uploaded the correct
drive; it cannot verify the generator's memory from an NPZ file.

Captured light and monitor shots are simultaneous within each analyzer
setting. The analyzer moves and its settled position is queried before
each set. Acquisition uses the existing scope adapter, rejects an incomplete
record or a channel at its vertical acquisition boundary, and retains shot
stacks in the capture NPZ. The scope must already be set for the desired
trigger, coupling, bandwidth, full-record time window and vertical ranges.
No auto-ranging is applied between angles.

## Test the polarization measurement before tuning

Use **Validate optical acquisition** for the live hardware check:

1. Select a voltage state (or existing optical session), set the ELL14 port
   and optical zero, and enter the photodiode, Trek monitor and AWG-output
   scope channels. The waveform must already be playing with the existing
   burst/trigger setup. An optical session takes precedence if both source
   fields are filled.
2. Enter **Hardware test analyzer angles**, such as `0,45`. These settings
   control the live test independently of the fine-tune session's angle list.
3. Click **Validate optical acquisition**. A plot window opens. The ELL14 moves to
   each requested position, verifies it after settling, and acquires repeated
   scope traces. The plot updates as each angle completes, showing requested
   versus actual analyzer position, photodiode traces and simultaneous Trek
   monitor traces. Shading shows repeat-shot spread; faint points show a
   native acquired photodiode trace. Pan/zoom controls are available.
4. After the angle set completes, the third plot shows polarization versus
   time with its uncertainty, reconstructed directly from the calibrated
   light signals. This angle is continuous within valid segments, with an
   arbitrary initial branch modulo 180°. It does not use the voltage target
   as evidence of the measured angle.
5. Inspect the traces for the expected change with analyzer position, clipping,
   noise and repeatability before fine-tuning. This test applies no corrections
   and makes no AWG writes. The analyzer stays at its last tested position.

Raw trace testing works **before optical Init**, using the existing voltage
state solely for the waveform/time grid. Calibration files are optional for
this raw hardware check. Angle conversion requires a fringe calibration for
every selected setting and at least two complementary angles. Missing
calibration leaves the light traces displayed with a message explaining why
conversion is unavailable. Clipped traces are displayed and saved, but stop
the test before conversion. Traces from successful earlier angles remain
available if a later move or read fails.

Each run creates a fresh `hardware_test_<timestamp>` folder containing
per-angle raw/aligned shot stacks, a combined `hardware_traces.npz`, and,
when conversion is available, `polarization_angle.npz` and
`polarization_angle.csv`. These are hardware-test artifacts, distinct from
the iteration-bound captures used by **Step**. The live GUI display uses
Matplotlib, already used by the original ILC GUI.

The hardware test can also be run as a separate module (the panel provides
the live plot window; this CLI emits progress and saves data):

```powershell
python -m eomilc_polarization_finetune.hardware_test --voltage-state run\drive_EO1.state.npz --angles 0,45 --port COM5 --zero 12.3 --pd-ch 2 --mon-ch 3 --drive-ch 1 --repeats 16 --cal 0=cal0.json --cal 45=cal45.json --out-dir eomilc_polarization_finetune\sessions\hardware_check
```

For the separate numerical diagnostics, use **Capture**, then **Check saved
measurement** in the panel. This check reads the
current capture, writes a diagnostic report, and applies no drive correction.
It reconstructs polarization angle directly from the calibrated light signals
at the discrete analyzer settings, without using the voltage target to infer
the angle. The voltage target is used only for a separate tracking-error
report. A real optical deviation does not by itself fail measurement health.

For an absolute accuracy test, prepare an **independently known linear
polarization** and enter its angle in the Acquisition tab. If that reference
is valid only over part of the waveform, enter the corresponding time-window
start/end in microseconds. A nominal EOM voltage is not an independent optical
reference. Without a known reference, accuracy is explicitly **UNVERIFIED**,
even when measurement health passes.

The report checks analyzer landing and angular coverage, repeated-shot angular
uncertainty, calibrated contrast consistency, redundant-angle fit consistency
when available, and simultaneous monitor voltage guards. Two complementary
angles such as 0°/45° resolve the angle, but have no redundant fit check; three
or more calibrated discrete settings give a stronger consistency test. Add
extra settings with repeated `--cal DEGREES=FILE` arguments during CLI Init.

For example, after capturing the current unchanged drive:

```powershell
python -m eomilc_polarization_finetune.measurement_test --session sessions\case1\pol_i000.npz --capture sessions\case1\capture_i000.npz --out-dir sessions\case1\measurement_check
```

To test an independently known 25° polarization on a selected plateau:

```powershell
python -m eomilc_polarization_finetune.measurement_test --session sessions\case1\pol_i000.npz --capture sessions\case1\capture_i000.npz --known-angle-deg 25 --start-us 3000 --end-us 5000 --out-dir sessions\case1\reference_check
```

The example assumes you have physically established that reference and window.
To examine reproducibility, take another full Capture without changing the
drive and pass `--compare-capture SECOND_CAPTURE.npz`. A difference can be
real drift or a measurement change; the report does not call it a sensor fault.

Default limits are 0.1° peak standard error of the mean, 3% RMS contrast
inconsistency, 2% normalized redundant-fit RMS, 0.2° peak independent-reference
error and 0.2° repeat-difference warning. Change the CLI thresholds for your
experiment's required precision; see `measurement_test --help`. Estimated
uncertainty covers repeat-shot noise, not systematic calibration, analyzer-zero
or quantization bias. Light-amplitude tests can flag some illumination changes,
but cannot eliminate the ambiguity without a reference-power measurement.

Each fresh report directory contains `measurement_report.json` (health,
accuracy and diagnostic findings) and `measurement_traces.csv` (measured angle,
uncertainty, target deviation, equivalent voltage residual and coverage mask).
The CLI exits with status 1 if health or independent-reference accuracy fails;
an unverified absolute angle is reported explicitly, with health still assessed.
All inputs and drive files are left unchanged.

## ELL14 driver review

The pasted driver was not error-free. The corrected copy is `ell14.py`.
Review and protocol reference: [ELL14_REVIEW.md](ELL14_REVIEW.md).
The initial mechanical home is separate from the optical zero. Its helper
`find_polarizer_zero` requires known linear input, adequate contrast and a
stable power reading; after fitting it moves to the new user zero.

Use an explicit serial port on the bench. Commands sharing an injected
serial object must be serialized by the caller; this is not a concurrent
multi-device bus manager. Only owned connections are closed.

Offline dependencies: NumPy and SciPy; GUI also needs Tk. Bench capture
uses the existing scope stack (pandas, pyvisa and the sibling scope repo)
and adds `pyserial` for ELL14. Install these in the same interpreter as the
existing bench GUI. No new dependencies are added to the original app.

Tests use simulated optics and fake serial replies:

```powershell
python -m unittest discover -s eomilc_polarization_finetune/tests -v
```

Hardware motion, real scope acquisition and actual optical convergence have
not been validated on connected instruments.
