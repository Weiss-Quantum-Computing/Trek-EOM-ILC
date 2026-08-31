# Prompt for continuing the Aug 25-26 campaign analysis

Paste everything below the rule into a fresh Claude Code session and fill
in the ALL-CAPS blanks at the bottom. This continues the f_cut sweep /
model-family study; the general workspace rules still apply.

---

FIRST read `docs\ANALYSIS_PROMPT.md` in the repo at
`C:\Users\mzd416\Desktop\Python Projects\EOM-ILC` (on the MacBook: the
Trek-EOM-ILC clone root; the `run\` folder comes from the bench-PC USB
snapshot `EOM-ILC-run-2026-08-26`). Its Data layout, Loaders, Units-and-traps
and What-already-exists sections apply verbatim -- use the repo's loaders,
treat `run\` as read-only, respect the 8-bit-scope traps. This file adds the
state of knowledge from the 25-26 Aug session so you do not re-derive it.

## Session datasets (all campaigns: EO1, arccos ramp target, gamma 0.6, i00-i20 unless noted)

Stem naming: leading P = PArallel rotation scheme, then model, then f_cut,
then channel. States record their own model (and FRF settings where used).

FRFs:
- `frf_WIDE_X1/X2.csv` -- Schroeder multitone, 2 V, to 80 kHz (Aug 25).
- `frf_FR200Kp5VX1.csv` / `frf_FRF200K2VX1.csv` / `frf_FRF200K6VX1.csv` --
  amplitude sweep 0.5 / 2 / 6 V, to ~190 kHz (Aug 26).

Gain-only (g = 0.5584): PARX1 (5k baseline, 0.0325%), PG1FX1 (1k, stopped
i6, 0.70%), PG25FX1 (2.5k, 0.046%), PG35FX1 (3.5k, 0.036%), PG5FX1 (5k,
0.033%), PG10FX1 (10k, 0.037%), PAGNFX1 (no f_cut, 0.075% and rising).

One-pole (g = 0.560, tau = 70 us): PAR1PX1 (20k, 0.0195%, keeper i08),
PR1PX1B (30k, 0.0146%, keeper i14), PR1PX1C (40k, 0.0108%, keeper i19 --
parametric-era best), PR1PX1D (60k, 0.0236% and rising -- deliberate
boundary demonstration).

FRF-inverse (built from the 0.5 V FRF): PRFRX1A and PRFRX1B (full strength
to 40 kHz, raised-cosine taper to zero at 60 kHz), PRFRX1C (80-160 kHz
taper, stopped at i9). These are freshly taken and NOT yet analysed in
depth -- see the open threads.

MKJX1/X2 are Aug 24, an earlier era; ignore unless comparing eras.

## Established results (do not re-derive; cite or build on)

- Convergence RATE is set by gamma alone: every campaign contracts with
  tau ~ 1.0 iteration = -1/ln(1-gamma). f_cut and model move only the
  floor. Everything a campaign will do has happened by ~i5 (parametric) --
  late iterations only test floor stability.
- Gain-only stability boundary: 5.5 kHz, measured. PAGNFX1's band-resolved
  growth rates (1.01-1.05/iter above 5.5 kHz) match |1 - gamma*H(f)/g|
  from the FRF band by band.
- The Q filter hits the WHOLE drive (`ilc.py update`), not just the update.
  f_cut below the target band therefore ERASES pre-distortion faster than
  learning restores it: PG1FX1's first update wiped the drive above 1 kHz
  and error at 1.5-3 kHz rose above iteration 0. Same mechanism leaves a
  stalled residual in every campaign's Q-filter transition band.
- One-pole parameters came from the FRF, not from identify():
  identify(model="one_pole") returns tau ~ 27 us (group delay, waveform is
  low-f weighted) and under-predicts phase lag 2x at 3 kHz. tau = 70 us
  matches the measured phase below 10 kHz. Keep g fixed at 0.56 -- a
  free-gain complex fit inflates g to 0.70 vs the measured DC 0.56.
- Measured one-pole boundary: ~33 kHz (PR1PX1D bands 34-46 kHz grow at
  1.04-1.05/iter; 31 kHz and below contract). The 2 V FRF predicted
  40.8 kHz; the discrepancy is AMPLITUDE: phase lag increases as probe
  amplitude decreases (-180 deg at 36.2 / 48.1 / 57.1 kHz for 0.5/2/6 V).
  Converged-loop residuals at 30-50 kHz are sub-mV, so the 0.5 V FRF is
  the right one for stability design and for the measured inverse. The
  2 V sets (old WIDE vs new) overlay exactly -- no plant drift.
- PR1PX1C (f_cut 40k) survives above the 33 kHz boundary only because the
  Q filter's |Q|^2 shielding (<= 0.5 above 36 kHz) suppresses learning
  there. Margin is thinner than the 2 V FRF implied; parametric f_cut
  stays 30-40 kHz, never above 40.
- The final-error "ripple" (>2 kHz; split errors at 2 kHz into LF drift vs
  HF ripple with a zero-phase butter3) is 99% repeatable and DRIVE-locked,
  not value-locked: it correlates 0.79-0.94 across campaigns with
  near-identical drives but 0.1-0.3 vs PAGNFX1 (different HF drive
  content). It is real plant ripple, not scope quantisation. It lives only
  where the target moves.
- Non-repeatable noise floor (successive-iteration error diffs / sqrt(2)):
  ~0.18-0.21 mV rms in the HF split. This is the floor any learning can
  reach. Parametric-era best ripple: 0.555 mV (PR1PX1C i19); the gap to
  the floor is the FRF-inverse era's target.
- Band-rate fits saturate: a band that reaches its floor by i5 shows a
  log-slope of ~1.0 regardless of its true contraction. Fit rates only
  where the band is well above floor, and remember content that is not
  drive-coupled (regenerated distortion, floor) does not follow
  |1 - gamma*Q^2*H*lead/g| at all.
- 6 V FRF shows a low-f anomaly (|H| 0.72, -31 deg at 1 kHz, bump ~700 Hz)
  -- the large-signal pseudo-resonance regime. The operating loop does NOT
  see it (measured low-f contraction implies g = 0.56).

Analyses already done this session (in scratchpads, not the repo): f_cut
sweep convergence/spectra/band-split, PAGN and PR1PX1D divergence-rate
fits vs FRF prediction, one-pole parameter/stability scans, amplitude-FRF
comparison, ripple repeatability correlations. The GUI draws all standard
per-campaign plots -- never rebuild those.

## Open threads

1. The FRF-inverse campaigns (PRFRX1A/B/C) are unanalysed. Work them up
   from scratch with fresh eyes: convergence, error spectra, time-domain
   error structure, where the residual lives, how they compare to
   PR1PX1C, what limits each configuration, and whether anything in the
   data warrants a closer look. Trust what the data shows you.
2. Probe-on-trajectory FRF: the exact perturbation response would come
   from a small multitone superimposed on the converged drive. Worth
   designing if FRF-inverse residuals stall unexplained.
3. gamma scheduling (0.6 early, ~0.25 late) predicts a lower noise imprint
   (factor 1 + gamma/(2-gamma)); untested on the bench.
4. Everything so far is X1. X2 has the 12% Trek gain mismatch and its own
   WIDE FRF; replication there is untouched.
5. Thermal drift: no hold-mode re-measurements (`_rMM` files) exist for
   any Aug 25-26 stem.

## Task

Stems of interest: STEMS_HERE
Question to answer: QUESTION_HERE
