# What this folder does, and why

## Why add another layer after voltage ILC?

The existing ILC adjusts the AWG drive until the Trek monitor follows the
requested voltage waveform. That establishes electrical tracking: the
amplifier produces approximately the voltage we asked for.

The experiment ultimately needs a polarization waveform. The voltage target
was already a way to specify that intended polarization, using an assumed
proportional voltage-to-angle relationship. Optical measurements showed
departures from that relationship. A good Trek-monitor trace therefore does
not, by itself, demonstrate that the light has the desired polarization.

This folder adds an optical follow-up layer. It starts with the completed
voltage ILC drive, measures the actual light, and permits small, slow changes
around that baseline. It imports the existing voltage-control library while
keeping the original GUI, source result file and learned baseline intact.

## What comes from the completed ILC results?

Choose the final `drive_<name>.state.npz` with **Load ILC results…**.
The follow-up GUI reads the final drive, original voltage target, selected
EOM, time grid, fixed AWG scaling, recorded voltage model and available
transfer-function metadata. It previews the waveforms and recorded tracking
metrics. There is no need to repeat voltage ILC or supply a new target file.

The intended angle is derived from the original target:

```text
target angle = fixed angle offset
             + 90 degrees × (target HV − target zero HV) / HV for 90 degrees
```

This expresses what the original target was meant to produce. It does not
assert that the measured light still follows that proportional relationship.
The scale and reference convention are adjustable in Advanced settings;
they must match the intention of the original voltage target.

Only the selected EOM is corrected. Keep the other EOM's waveform fixed
during each optical fine-tuning campaign.

## Why measure at several discrete analyzer angles?

The ELL14 rotates the analyzer to selected settings, initially 0° and 45°.
At each setting, the same repeated waveform is played and the scope records
photodiode and Trek-monitor traces from the same acquisition. The analyzer
is moved and settled before the scope starts that set of captures.

In the calibrated linear-polarization geometry, the measured light is modeled
as:

```text
light = dark level + A + B × cos[2 × (polarization angle − analyzer angle)]
```

Near a transmission maximum or minimum, one analyzer setting responds weakly
to a small polarization change. A setting 45° away provides complementary
sensitivity. Combining the settings avoids relying on a nearly blind signal.

Each setting needs a measured fringe calibration for angle conversion and
correction. The mount's mechanical home must be related to the optical zero;
the two are not automatically identical. Calibration also supplies detector
offset, fringe midpoint, contrast and the selected EOM's optical sensitivity.

## First check that the hardware measurement works

**Test analyzer + show traces** performs a live hardware check:

1. Move the ELL14 to each specified setting and verify its settled position.
2. Capture repeated photodiode and simultaneous Trek-monitor traces.
3. Display the traces as each setting finishes, with requested and reported
   analyzer positions and shot-to-shot spread.
4. When calibrations are available, display the polarization angle versus
   time reconstructed from the light signals.
5. Save the acquired traces and converted angles for inspection.

The angle reconstruction uses measured intensity quadratures, rather than
assuming the voltage target is the measured polarization. Linear-polarization
angle is defined modulo 180°; the live hardware test displays a continuous
branch through valid samples.

Raw capture and display work before optical calibration. Conversion requires
calibration at every selected setting and enough angular coverage. A missed
position or clipped acquisition stops the test; already acquired traces are
retained. The test does not update or upload an AWG waveform.

**Check measurement** provides additional numerical diagnostics for a saved
optical-session capture. An independently known input polarization is needed
to check absolute angle accuracy. Agreement with the voltage target alone
cannot establish measurement accuracy.

## How does optical fine-tuning change the drive?

**Start optical follow-up** creates a separate campaign whose baseline is
the completed voltage ILC drive. **Capture light** obtains the optical
measurements. **Calculate small correction** then:

1. Predicts the desired photodiode signal at each analyzer setting from the
   intended angle and fringe calibration.
2. Combines measured light residuals using their calibrated sensitivities
   and repeated-shot uncertainty.
3. Converts the resolved residual to an equivalent HV error.
4. Uses the recorded voltage-response inverse to calculate a small AWG change.
5. Restricts that change to the selected slow modes and correction limits.
6. Writes a new waveform and optical session without replacing the source
   voltage ILC results.

Repeated shots help distinguish a repeatable residual from random noise.
The default correction suppresses errors below three estimated standard
errors of the mean. Filtering applies to the correction, so the voltage
baseline's fast waveform features are preserved. Its first and last samples
are also preserved.

The optical layer does not simultaneously run the old voltage-target update:
that update could pull the drive back toward the original electrical target
and undo the optical adjustment. Trek measurements continue to provide
voltage-limit checks.

Corrected waveforms are uploaded through the existing AWG GUI, with fixed
scaling and normalization OFF. The follow-up panel then takes fresh optical
measurements to assess the result. Its capture metadata identifies the
expected session and drive, but cannot prove that the correct waveform was
actually uploaded to the generator.

## What “small” and “slow” mean here

| Control | Default | Purpose |
|---|---:|---|
| Peak AWG change per step | 2 mV | Limits an individual optical adjustment |
| Total peak AWG change from baseline | 20 mV | Prevents accumulated adjustments from becoming a new large drive |
| Total predicted peak HV change | 10 V | Bounds the voltage-response prediction of the accumulated correction |
| Slow correction bandwidth | 100 Hz | Selects the adjustable slow waveform modes |
| Optical learning gain | 0.2 | Applies a fraction of the inferred correction |

The GUI warns when a proposed correction approaches a rail and bounds its
size. Existing AWG, Trek-input, HV, slew, current and idle guards also apply.
The HV-change bound is a model prediction; inspect the actual monitor
measurements to verify the physical change.

The bandwidth controls variation **within the repeated waveform**. It does
not set a seconds/minutes averaging time or create an unattended drift servo.
Corrections are applied on demand. Because the waveform endpoints are fixed,
the lowest adjustable mode is `1/(2T)`, where `T` is the first-to-last-sample
duration. A bandwidth below that mode produces a warning and zero correction.

## What the measurement can and cannot establish

The implementation uses the repo's calibrated linear-rotation model. Two
complementary analyzer settings can resolve its angle, but do not characterize
arbitrary ellipticity or all Stokes parameters. Additional calibrated settings
provide redundancy for checking model consistency.

There is no simultaneous reference-power measurement. Illumination, detector
gain and optical alignment must remain stable between calibration and the
sequential analyzer-angle captures. Averaging reduces random noise, but a
repeatable light-power change can still resemble a polarization change.

Software tests cover simulated optics, acquisition sequencing, plotting,
correction limits, result import and fake ELL14 serial replies. Real instrument
operation and optical convergence still need verification on the bench.

## Where to look in the code

| File | Role |
|---|---|
| `gui.py` / `results.py` | Guided follow-up panel and import of completed ILC results |
| `ell14.py` | Analyzer mount control and verified positioning |
| `hardware_test.py` / `trace_view.py` | Live discrete-angle capture and trace/angle display |
| `measurement_test.py` | Readout reconstruction and measurement diagnostics |
| `core.py` | Small optical correction, slow-mode restriction and guards |
| `workflow.py` | Separate sessions, capture identity and waveform export |
| `tests/` | Offline checks with simulated data and instruments |

For launch instructions and detailed bench steps, see [README.md](README.md).
For the review of the supplied mount driver, see
[ELL14_REVIEW.md](ELL14_REVIEW.md).
