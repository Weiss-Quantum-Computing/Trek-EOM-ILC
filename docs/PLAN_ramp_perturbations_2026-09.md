# Ramp targets, the second-order rung, and edited-drive transfer

Work plan for the September 2026 EOM-ILC bench campaign. Written 1 Sep 2026
against the repository as it stood at commit `456b025`, the campaign records
in `run\`, and the memory notes of 18 Aug to 1 Sep. Numbers quoted from prior
data name their file. Numbers computed for this plan are marked *computed
1 Sep* and the script that produced them is in the session scratch folder
(`fmt_test.py`, `frf_fit.py`, and the inline analyses of `meas_PARX1_i00.npy`
and `drive_PR1PX1C_i19.csv`). Nothing here has been run on the bench.

Register: this is a methods document. Interpretation is confined to the
decision rules; the steps record what is measured, how, and what the output
file is called.

---

## 0. Inputs, as supplied and as assumed

**The questions** (from Maarten, 1 Sep):

1. Produce converged ILC drives for the four targets `target_PARX1`,
   `target_PARX2`, `target_SERX1`, `target_SERX2`, using the one-pole model
   as the primary inverse.
2. Determine whether the second-order rung, re-parameterised from the
   measured FRF rather than the withdrawn resonance, lowers the residual at
   3 to 25 kHz.
3. Characterise how a converged drive behaves when it is *edited* after the
   fact in the Waveform Editor and played without re-learning: endpoint
   lowered (clip), a sub-stroke cut out (offset then clip), hold shortened,
   hold lengthened, DC offset added.
4. Verify that the Waveform Editor, the ILC panel and the bench layer can
   collect this data with minimal handling.

**Hardware and software** (from the repository and the memory notes; the
scope channel map is *assumed* until step 0.2 confirms it, because the
polarimetry work of 29 Aug to 1 Sep moved a photodiode onto CH2 or CH3 at
times):

| item | state |
|---|---|
| Generator | B&K 4063B, `USB0::0xF4EC::0xEE38::574D22116::INSTR`. CH1 drives the X1 divider and Trek X1; CH2 drives the X2 summer and Trek X2. Arb memory 16 384 points (datasheet, unprobed; largest record stored so far 5501). |
| Trigger | DS345 square wave at 3.6997 Hz, fanned to the generator EXT input and the scope EXT input (memory, 31 Aug). It was 3 Hz for the 26 Aug campaigns. |
| Amplifiers | Trek 610E x1000 on each channel; monitor output = HV/1000. |
| Scope | Keysight MSO-X 2014A, `USB0::0x0957::0x1798::MY63080029::INSTR`, 8-bit, HRES. Production map CH1 AWG Ch1, CH2 AWG Ch2, CH3 Trek Mon X1, CH4 Trek Mon X2 (memory, 31 Aug: "all four are occupied"). |
| ILC panel | `EOM-ILC\ilc_gui.py`, `456b025` plus the 1 Sep changes of Section 13 (seed drive, FRQ check, Hold limit check, keep verticals); state files `run\drive_<stem>.state.npz`; smoke suite 50 checks, green on 1 Sep. |
| Editor | `Waveform-Editor-GUI\waveform_editor_gui.py`, `f96e085` plus the 1 Sep *ILC header* change; `--selftest`, `tests\smoke_test.py` and `tests\check_py27.py` green on 1 Sep. |
| Experiment-side playback | An analog card with a 2 µs lookup-table grid (memory). Its burst and idle behaviour relative to the 4063B are **not established** on this bench. |

**Constraints** (assumed, none were supplied; each is listed so it can be
overruled):

- Targets are rebuilt at each channel's measured 90-degree point (Maarten's
  table, 1 Sep: 5128.3 V at the X1 monitor, 5137.4 V at X2):
  `waveforms\target_<name>_V90.csv`, written by `tools\make_v90_targets.py`
  from the 5200 V files by a pure rescale (x 0.9862 on X1, x 0.9880 on X2).
  The record still starts and ends at 0 V, which is 20.7 V (X1) / 8.7 V (X2)
  from the EO zero; moving the start is a separate decision and is not
  applied. `PR1PX1C` (learned on the 5200 V target) stays the drift
  reference of step 0.3 only.
- The 2 µs grid is fixed by the card. Every edited waveform keeps 2 µs;
  edits that change the hold change the point count, never the step.
- Idle cap `LIMITS.idle_awg = 100 mV` (56 V at the X1 EOM, 61 V at X2)
  stays in force. The offset perturbation is sized inside it.
- `hv_max = 6000 V`. No edited target may exceed 5200 V.
- No Trek input may be left floating (memory, 31 Aug: X2 wandered to
  -4.0 to -5.7 kV with its generator output switched off).
- Bench time: about one day of measurement plus half a day of software.

---

## 1. The questions as decisions

**Q1, the four baselines.** If `PARX2`, `SERX1` and `SERX2` reach the
`PARX1` one-pole floor (rms 0.011 % of span, `drive_PR1PX1C.state.npz`
history i19), the four keepers go to the experiment as the production set
and no further model work is needed for them. If a channel or scheme
stalls above 0.02 % rms, that chain gets its own parameters from its own
small-signal FRF (X2 has none at 0.5 V today) before anything else. If a
campaign converges at the expected rate but to a higher floor with no band
growing, the floor is chain noise (X2 carries 0.31 V rms of 60 Hz on the
HV) and is reported as such, not iterated on.

**Q2, the second-order rung.** If the two-real-pole inverse converges at
f_cut 50 kHz and its converged 10 to 25 kHz residual is at most half of
`PR1PX1C`'s, it replaces the one-pole rung as the production inverse and
the FRF mode stays shelved. If it diverges at 50 kHz but converges at
40 kHz with no mid-band gain over one-pole, the one-pole rung stays. If it
lowers the mid-band but the whole-record rms does not fall (edge effects
from the second derivative), it is used with the 40 kHz cut and the ends
are inspected before it is adopted.

**Q3, editing converged drives.** The decision is a rule table, one row per
edit type: *transfers* (error against the edited target within a
tolerance), *does not transfer* (re-learn per edit), or *transfers with a
known correction* (a level-dependent term the editor could apply). A
provisional tolerance is proposed: peak error at most 10 V at the EOM over
the whole record and at most the converged floor outside a +/-300 µs window
around any new corner. **This tolerance is not the experiment's; it is a
bench number.** dphi/dV = 3.02e-4 rad/V per EOM (memory, 31 Aug), so 10 V
is 3 mrad of rotation. Whoever owns the gate budget sets the real number;
the plan reports sizes either way.

**Q4, tooling.** Two gaps found on 1 Sep blocked Phase 3 outright and were
closed the same day (Section 13); what remains is to confirm the changed
panel is the one running at the bench (step 0.1).

If Q3 comes out "transfers" for every edit, the workflow needs nothing but
the editor. If it comes out "does not transfer" for clips, the experiment
needs a per-endpoint drive library or a bench trip per endpoint, and
Phase 4 measures what a trip costs. Either answer changes what is built
next, so the campaign is worth running.

---

## 2. Measured and assumed

| quantity | MEASURED (source) | ASSUMED (consequence if wrong) |
|---|---|---|
| PAR target geometry | 5501 pts at 2 µs (11.002 ms); flat 0 to 216 µs; rise 218 to 4924 µs; hold 4926 to 6074 µs (1148 µs); fall 6076 to 10782 µs; slew peaks 4.108 V/µs near 1000 V, 0.54 V/µs at 2600 V, 0.74 V/µs at 4000 V (`waveforms\target_PARX1.csv`, computed 1 Sep) | |
| SER target geometry | SERX1 rises 218 to 1804 µs at up to 8.216 V/µs, holds at 5200 V to 9194 µs, falls to 10782 µs. SERX2 holds 0 to 1802 µs, rises to 4948 µs at up to 2.535 V/µs, holds to 6050 µs, falls to 9196 µs (`waveforms\target_SER*.csv`, computed 1 Sep) | |
| Raw tracking error, flat first shot, X1 PAR | peak 185 V, rms 40 V (3.56 % / 0.78 % of span); on-ramp mean +28 V; mid-hold +15 V (`drive_PARX1.state.npz` i00, `meas_PARX1_i00.npy`) | |
| Converged floor, X1 PAR, one-pole f_cut 40 kHz | peak 3.09 V, rms 0.564 V (0.059 % / 0.0108 %); ripple 0.555 mV mon (`drive_PR1PX1C.state.npz` i19, keeper `drive_PR1PX1C_i19.csv`) | Plant unchanged since 26 Aug. Drift of up to 32 V on a converged drive over hours is documented; step 0.3 re-measures. |
| Measurement floor at 64 repeats | 0.25 to 0.30 mV mon (0.25 to 0.30 V at the EOM) from successive-iteration differences i18 to i20 of `PR1PX1C`, computed 1 Sep; 0.18 mV on `PG5` (memory) | Hold-run pairs of one drive give the same floor (step 0.3 measures it directly). |
| Learned correction size, X1 PAR | peak 342 mV, rms 72 mV at the AWG; on-ramp mean +53 mV; mid-hold +24 mV; settles to within 1 mV of the hold value 190 µs after hold entry and 0.3 mV at 286 µs; deviates from it only in the last 20 µs before hold exit (`drive_PR1PX1C_i19.csv`, computed 1 Sep) | |
| Small-signal FRF, X1 | `run\frf_FR200Kp5VX1.csv` (0.5 V probe, 95 coherent tones 364 Hz to 159 kHz); phase -180 deg at 36.2 kHz; also 2 V and 6 V sets (memory) | |
| Small-signal FRF, X2 | none. `run\frf_WIDE_X2.csv` is 2 V, 60 tones to 80 kHz | X2 behaves like X1 at 0.5 V. Wrong by the X1 amplitude spread means the X2 one-pole boundary is anywhere from 26 to 57 kHz. Step 0.4 measures it. |
| One-pole stability boundary, X1 | predicted 25.7 kHz from the 0.5 V FRF (computed 1 Sep, gamma 0.6, g 0.56, tau 70 µs); measured about 33 kHz (`PR1PX1D`, memory); f_cut 40 kHz works through Q-filter shielding, with peak lambda*Q = 1.03 at 27 to 29 kHz (computed 1 Sep) | |
| Two-real-pole fit, X1, 0.5 V | tau1 34.0 µs, tau2 13.9 µs with g fixed 0.56 over 0.4 to 40 kHz; equivalent fn 7311 Hz, zeta 1.10; boundary 53.9 kHz; mean lambda 10 to 25 kHz 0.37 (one-pole: 0.75); with f_cut 50 kHz peak lambda*Q 0.54 (`frf_fit.py`, computed 1 Sep) | The FRF measured at 0.5 V describes the loop's operating point. The 26 Aug campaigns' agreement with the 0.5 V boundary (33 vs 25.7 to 38.3 kHz) supports it; residual amplitude dependence would show as a boundary between the 0.5 V and 2 V predictions (54 to 68 kHz). |
| Gain compression, X1 | AWG-to-monitor gain 0.5633 at 0.56 V mon falling to 0.5582 at 5.36 V mon (`eomilc\config.py` `EO1.gain_pts`, 20 to 21 Aug sweeps) | |
| Predicted plateau error of a clipped drive | +14.2 V at 4000 V, +20.7 V at 2600 V, +4.2 V at 1000 V, from `gain_pts` (computed 1 Sep) | The table's 0.5 % spread is real rather than V/div-switching residue (the scope-analysis notes warn that gain-vs-amplitude tilts are often the scope). Step 3.1/3.2/3.7 test it directly. |
| Predicted new-corner transient of a clipped drive | local slope x 28 µs group delay: 21 V at 4000 V, 15 V at 2600 V, 115 V at 1000 V (computed 1 Sep); decay time not established from i00 because the natural corners are soft (10 to 90 % slope over 362 µs) | Group delay 28 µs (ramp fits, 21 Aug) applies at a hard corner. If the large-signal lag is nearer 70 µs the transients are 2.5x larger. |
| Scope readout | 5000 points unprompted; the panel asks 20 000 for a 5501-point grid (`SCOPE_PTS`, `scope_points_for(2.2*N)`); 62 500 when keep-native is on | |
| Scope verticals used for every X1 campaign | CH1 1.5 V/div offset +4.66 V; CH3 1 V/div offset +2.6 V (`run\ilc_gui.log`, 26 Aug 07:15) | Coupling DC on all four (not in the log). |
| Inter-channel skew | 97 ns between the CH1/2 and CH3/4 pairs (memory, 1 Sep): 1.0 deg at 30 kHz in every cross-channel FRF | |
| 60 Hz on X2 | 311 µV rms at the summer input = 0.311 V rms on the HV; 13.9 mains cycles of phase walk over 64 shots at 3.7 Hz (memory) | The DS345 stays at 3.7 Hz. A mains-locked trigger would turn this into repeatable error on X2. |
| Trek input floating | X2 at -4.0 to -5.7 kV with its generator output OFF (memory, 31 Aug) | |
| Idle cap | 100 mV at the AWG = 56 V (X1) / 61 V (X2) at the EOM (`config.py`, computed 1 Sep) | |
| EO2 gain | `amp_mon_product` 0.9707 from the 1 Sep optical measurement; `gain_pts` 2.5 % higher and left in place (`config.py` comment) | Only the flat first shot on X2 uses it; ILC absorbs a 2.5 % gain error in one iteration. |
| V90, EO zero, end-to-end gain, hysteresis | X1: 5128.3 V at the monitor (9.168 V commanded), EO zero -20.7 V, command-to-HV gain 0.5594 against 0.5586 from the tables, rise/fall hysteresis 0.21 %. X2: 5137.4 V (8.672 V), -8.7 V, 0.5924 against 0.6078, 0.047 % (Maarten's table, 1 Sep; `Channel.v90_hv`, `eo_zero_hv`, `cmd_hv_gain_meas`, `hysteresis_pct` in `config.py`) | The hysteresis is repeatable and drive-locked, so ILC learns it, but it is 10.8 V on X1 and in a single edited record cannot be told from a corner term; the rise-vs-fall asymmetry of every Phase 3 residual is reported separately for that reason. Predictions quoted at 5200 V scale by 0.986 for the V90 targets. |
| Card playback | | The experiment's card plays the CSV on the same grid, one shot per trigger, holding sample 0 between shots, like the 4063B DDS burst. If not, Phase 3's transfer results describe the bench and not the experiment. Not testable here (Section 14). |
| Load capacitance | | 200 pF (`LIMITS.load_capacitance`, a guess). SERX1's 8.2 V/µs draws 1.64 mA at 200 pF, 82 % of the 610E's 2 mA. Above 244 pF the amplifier current-limits on SERX1's ramp and the chain is no longer linear there. Step 1.2 watches for it. |

Assumptions whose failure would invalidate a headline result and are
therefore tested in Phase 0: the channel map (0.2), plant stability since
26 Aug (0.3), the X2 small-signal response (0.4), the Hold path on hardware
(0.3), and the two software gaps (0.1).

---

## 3. Phases and gates

| phase | purpose | gate (numeric) | pass | fail | neither |
|---|---|---|---|---|---|
| 0 Readiness | close the two software gaps, confirm wiring, re-measure today's floor, measure X2's small-signal FRF, decide the idle policy | 0.3: rms of `PR1PX1C_i19` re-measured <= 1.0 V at the EOM and peak <= 6 V, three runs agreeing to 0.4 V rms | proceed; `PR1PX1C_i19` remains the Phase 3 reference | rms > 1.0 V or peak > 6 V: two warm-up iterations on a copy (stem `PRWUX1`), then its last drive is the reference for everything downstream | runs disagree by more than 0.4 V rms among themselves: thermal drift on the run timescale; extend the gap to 120 s and repeat once; if still, the whole campaign runs with bracketing holds (Section 8) |
| 1 Baselines | one-pole campaigns on the four V90 targets (PARX1, PARX2, SERX1, SERX2) | per campaign: rms <= 0.8 V at the EOM (0.015 %) by i15, no frequency band growing faster than 1.02/iteration over i15 to i20 | keeper = smoothest of i15 to i20; go to Phase 2 | a band grows > 1.02/iteration: f_cut down by 10 kHz, resume from the state 5 iterations before the growth began | converges (contraction 0.4/iteration for the first 5 iterations) but flat above 0.8 V with no band growing: chain-noise floor; report the floor and the spectrum, keep the drive, do not iterate further |
| 2 Second order | two-real-pole rung on X1 PAR at f_cut 50 kHz | converged rms < 0.564 V and 10 to 25 kHz residual <= 0.5 x `PR1PX1C`'s (both from the same analysis script, same k); no band growth over i15 to i20 | adopt; re-run the three Phase 1 stems with it only if their Phase 1 floors were model-limited (a band above 10 kHz dominates the residual) | growth at 34 kHz or above > 1.02/iteration: repeat at f_cut 40 kHz (`PR2P4X1`); if 40 kHz gives no mid-band gain over one-pole, one-pole stays | mid-band better, whole-record not: inspect the first and last 200 µs; if the excess is there, the rung is adopted with the whole-record number reported as-is |
| 3 Edit transfer | play edited copies of the reference drive against edited targets, no update | per condition, three hold runs; the reported number is their mean with the run spread; the gate is the tolerance of Section 1 | row = transfers | row = does not transfer; Phase 4 runs on that condition | error is above tolerance only in a predicted, level-dependent way (Section 5): row = transfers with a correction; the correction's size and its uncertainty are the result |
| 4 Warm start | how many iterations an edited seed needs to reach the floor, against a flat start on the same edited target | iterations to reach <= 2 x the Phase 0.3 floor | if <= 4 from the seed and >= 8 from flat: seeding is worth a bench trip; document the recipe | if the seed needs as many as flat: no benefit; the per-endpoint library is a from-scratch campaign per endpoint | seed converges faster but to a higher floor: the edit left content the Q filter cannot remove (Section 10); report both floors |

The order is by cheapest invalidation first: 0.1 and 0.2 cost an hour and
would waste every later measurement; 0.3 and 0.4 cost twenty minutes and
set every number Phases 1 to 3 are judged against.

---

## 4. Configuration A, referenced by every step

Every step below runs in Configuration A unless a delta is listed.

| element | setting |
|---|---|
| Generator, driven channel | ARB, clock DDS, FRQ = 1/(N x 2 µs) (90.893 Hz for N = 5501), AMP 20.00 Vpp, OFST 0, load HZ, burst NCYC 1 cycle, trigger EXT, upload with Normalise OFF at the fixed mapping 1.0 = 10 V. The panel's Auto-set writes exactly this from the session and refuses if the output is ON. |
| Generator, other channel | NOT switched off. It holds a stored all-zero arb with its output ON (idle policy, step 0.5), or, in the two-channel steps, its own drive. |
| Trigger | DS345 square wave, 3.7 Hz (verify 3.6997 Hz on its display), EXT to both instruments. Rate changes only in step 3.8, and are logged. |
| Scope channels | CH1 = AWG CH1 (X1 drive), 1.5 V/div, offset +4.66 V, DC, 1 MOhm. CH2 = AWG CH2 (X2 drive), 1.5 V/div, offset +4.3 V, DC. CH3 = Trek monitor X1, 1 V/div, offset +2.6 V, DC. CH4 = Trek monitor X2, 1 V/div, offset +2.6 V, DC. |
| Scope timebase and acquisition | range 15 ms (1.5 ms/div), position +5.5 ms so the record starts at the left edge; trigger EXT; acquisition HRES (never AVER); readout 20 000 points per shot (62 500 with keep-native, which stays ON). |
| Shots | 64 HRES single shots per measurement, averaged in software after boxcar resampling to the 2 µs grid; 17.3 s at 3.7 Hz. |
| ILC settings | gamma 0.6; t-offset 0 µs; zero baseline OFF; full scale 10 V; model and f_cut per step; first-shot gain equal to the model gain. |
| Verticals rule | The monitor V/div and offset above are fixed for the whole campaign. After any Auto-set on an edited session (needed when N changes), CH3/CH4 are put back to 1 V/div, +2.6 V by hand before measuring. Reason: Section 8. |
| Software | Anaconda interpreter; the AWG GUI and Scope Grab closed during bench actions; the ILC panel restarted after any code change. |

Deviations that a step needs are written as "Delta: ...".

---

## 5. Feasibility arithmetic

All in volts at the EOM (monitor volts x 1000). Floor F = 0.30 V rms per
64-shot measurement (Section 2). The 8-bit code at 1 V/div is 31 mV per
shot on the monitor (31 V at the EOM); HRES brings the word lattice to
2.5 mV and the 3.5 mV rms per-shot analog noise dithers it, so 64 shots
reach 0.44 mV before the boxcar and the measured 0.25 to 0.30 mV after it.

| measurement | expected signal | floor | signal/floor | note |
|---|---|---|---|---|
| Phase 1 raw first shot | 40 V rms | 0.3 V | 130 | |
| Phase 1 converged floor vs PARX1's | difference of order 0.1 to 0.5 V rms between chains | 0.3 V, halves with 4 hold runs for the random part | 0.3 to 2 | Chain-to-chain floor differences near F are quoted as bounds, not values. |
| Phase 2, 10 to 25 kHz band residual | one-pole leaves 0.09 to 0.37 mV mon per band (memory, `PR1PX1B`); two-pole predicted to halve it | spectral floor per band about 0.05 mV mon at k = 16 | 2 to 7 | Marginal at the low end; the 20 to 33 kHz band (0.37 mV) is the discriminating one. |
| 3.1 clip 4000: plateau error | +14 V predicted, sustained over the hold | 0.3 V | 47 | Level term. |
| 3.1 clip 4000: corner transient | 21 V predicted, tens to hundreds of µs | 0.3 V | 70 | Slope term. |
| 3.2 clip 2600 | plateau +21 V, corner 15 V | 0.3 V | 50 to 70 | Level term up, slope term down: separates them. |
| 3.3 split 2600 stroke | two corners, 15 V and up to 115 V (the low corner sits at the 4.1 V/µs part of the ramp), plateau +21 V | 0.3 V | 50 to 380 | |
| 3.4 hold shortened to 600 / 300 / 150 µs | 0 / about 1 / about 3 V predicted from the correction tail (1 mV at 190 µs, 5 mV at 154 µs after entry; x 0.56 x 1000) | 0.3 V | 0 / 3 / 10 | The 600 µs point is a null; if it shows 3 V or more the mechanism is not the tail. |
| 3.5 hold lengthened to 2148 µs | 0 V predicted (linear); Trek droop over the extra millisecond not established | 0.3 V | null test | Reported as an upper bound if within F. |
| 3.6 offset +/-50 V | 0.6 V predicted from gain compression, antisymmetric in sign | 0.3 V, 0.2 V on the +/- difference with 3 runs each | 2 to 3 | Kept only because the +/- difference cancels drift and the clamp; a symmetric result is reported as a bound. |
| 3.7 amplitude scale to 4000 V | plateau +14 V predicted (same level term as 3.1), no corner term | 0.3 V | 47 | The A/B partner of 3.1. |
| 3.8 trigger 3.7 vs 0.37 Hz | not established; hour-scale drift of 32 V exists | 0.3 V | unknown | Measures the size; 5 runs per rate, alternated. |
| 3.9 both channels live (SER) | not established; a ground-loop term would be dV/dt-shaped at 8.2 V/µs (X1's ramp on X2's hold) | 0.3 V | unknown | Null test with a distinctive signature. |
| 3.10 time-stretch x1.2 | 19 V at tau 28 µs, 48 V at tau 70 µs, ramp-shaped | 0.3 V | 60 to 160 | Also discriminates the effective lag. |
| Phase 4 iterations to floor | contraction 0.4/iteration measured on every campaign so far; 21 V corner reaches 0.6 V in 4 iterations, 115 V in 6; a flat start reached the floor at i08 to i19 | | | |

What averages: per-shot analog noise (with repeats); the 2.5 mV lattice
(only because the analog noise dithers it); X2's 60 Hz (across shots at a
non-mains trigger, roughly as 1/sqrt(N)). What does not average:
drive-locked plant ripple (99 % repeatable, memory), the plateau term, the
corner transients, V/div-dependent quantisation, thermal drift within a
run. Nothing in Phase 3 depends on averaging beyond the standard 64
shots; the three hold runs per condition serve to bound the run-to-run
spread, not to reach a floor.

---

## 6. Model-free checks, scheduled first

1. **3.1 against 3.7.** The same 4000 V endpoint reached by clipping and by
   scaling. Both share the plateau term; only the clip has a new corner.
   The difference of the two error traces is the corner term with no model.
   Runs before any warm-start work. Cannot tell why the plateau term has
   the size it has.
2. **+/-50 V offsets (3.6).** Half the difference of the two error traces is
   the part first order in the offset. The idle-cap clamp, drift, and any
   even term cancel. Cannot separate gain compression from a capacitance
   change; both are first order.
3. **The i00 record as the prediction.** `meas_PARX1_i00.npy` is the chain's
   raw answer to the unedited ramp. The transient it shows at the natural
   hold entry (24 V peak, 4.70 ms) is a lower bound for what a hard corner
   produces; no plant parameter enters.
4. **Edited targets built two ways.** Each edited target is produced in the
   editor (the workflow's route) and by numpy from `target_PARX1_V90.csv`
   with the same operation. They must agree to 1e-6 V at every sample before
   the condition is measured. This checks the editor's arithmetic, nothing
   about the chain. (`tools\make_ramp_target.py` cannot be used for this:
   its `forward.csv`/`reverse.csv` inputs are not on this machine.)
5. **X1 against X2 on the same PAR target.** Phase 1's `PR1P3X2` against
   `PR1PX1C`: same target, same model form, different chain. Any difference
   in floor or in the residual spectrum is chain, not model.
6. **Bracketing holds.** Every Phase 3 condition is preceded and followed
   by one hold run of the unedited reference. Drift is read off the
   bracket without a model.

---

## 7. Circularity audit

| correction | measured from | circular? | independent check |
|---|---|---|---|
| Model gain g = 0.56 (X1), first-shot gain | `config.py` tables, 20 to 21 Aug sweeps | no | ILC's DC contraction (1 - gamma at g_model = g_true) is itself a check each campaign |
| One-pole tau = 70 µs | FRF phase below 10 kHz (memory, 26 Aug) | no | |
| Two-pole tau1, tau2 | fit to `frf_FR200Kp5VX1.csv` (26 Aug multitone) | no; never refit from the ramp campaign it corrects (`Fit from measurement` on a ramp record under-weights high frequencies and returned tau = 27 µs against 70) | the boundary it predicts (54 kHz) against the bracketing campaign `PR2P6X1` |
| The floor F | successive-iteration differences of `PR1PX1C` | would be circular if used to judge `PR1PX1C`'s own convergence | step 0.3's hold pairs (same drive, no update) give F by construction |
| t-offset = 0 | cross-correlation of the drive channel against the uploaded drive, every run | no | |
| Predicted plateau term | `gain_pts` | no; the measured plateau in 3.1/3.2/3.7 is compared to it, not corrected by it | |
| Drift subtraction (bracketing holds) | the reference's own hold runs | applied only as a stated correction; both raw and corrected values are reported | |
| X2 f_cut choice | the 0.5 V X2 FRF of step 0.4 | no; the FRF is measured before the campaign it parameterises | |

No correction in the analysis is inferred from the record it is applied to.

---

## 8. Systematics and the exchanges that separate them

**Vertical scale.** Auto-set sizes the monitor V/div from the session's
target span. A clipped 4000 V session would land at 0.5 V/div, a 2600 V one
at 0.5 V/div or below, and the 8-bit lattice changes with it; the
scope-analysis notes record V/div changes producing fake gain tilts of
0.7 %/decade on passive dividers. Rule: all Phase 3 captures at CH3 1 V/div,
+2.6 V, restored by hand after any Auto-set. The same-V/div rule also
applies to Phase 1 (X2 at 1 V/div) so floors are comparable across chains.

**Sign reversal.** The +/-50 V offsets: a term that reverses is first order
in the offset (gain or capacitance dependence on level); the idle clamp,
drift, and the plateau term of a fixed record do not reverse.

**Order reversal.** Phase 3 conditions run in the order listed and then the
level series 3.1, 3.2, 3.7 is repeated in reverse at the end of the day.
Drift is monotonic in time; edit effects are not.

**Bracketing.** One hold of the unedited reference before and after every
condition (about 25 s each). The condition's error is reported raw and with
the bracket mean subtracted, the bracket spread quoted.

**Same drive, two channels.** Not available (the chains differ), so
chain-to-chain claims stay at the level of "X2's floor is a V higher" with
the X2 60 Hz figure quoted alongside.

**Offset-before-averaging.** Not applied: the ramps start at 0 V and the
first 216 µs are flat, so the idle level is part of the record and is a
result, not a nuisance (the loop trims it through the clamped first sample).

---

## 9. Blind spots and coverage

- **Above 250 kHz and the top octave below it.** The 2 µs grid's boxcar
  hides both; keep-native (62 500 points per shot, 0.24 µs) stays on for
  every measurement and the `_native.npz` files carry the band to 2 MHz.
- **Between bursts.** The record covers one burst. The level the EOM sits at
  between bursts is sample 0's level plus the chain's own offset; the
  flat 216 µs lead-in of every record shows it, so it is not blind. For the
  offset conditions (3.6) that lead-in is the measurement of the inter-burst
  level.
- **The monitor versus the crystal.** The monitor is faithful below 10 kHz
  and about 10 degrees off above 20 kHz (polarimetry, memory). Every number
  in this plan is a monitor number; Phase 2's 20 to 33 kHz claims are
  monitor-only and are labelled so.
- **Corner time resolution.** 2 µs against transients of 30 µs and up:
  resolved.
- **The SER handoff instant.** X1's hold entry at 1.80 ms coincides with
  X2's ramp start. Any cross-channel term lands at X1's most sensitive
  instant; 3.9 measures both channels with both live, and separately.
- **Sample 0 artefact.** Raw readouts carry a fixed 1235 mV in sample 0
  (memory, 1 Sep). The resample onto the grid discards it; any analysis of
  `_native.npz` or raw CSVs trims 5 samples from each end.

---

## 10. Numbered steps

Time estimates use 17.3 s per 64-shot measurement, about 25 s per bench
iteration with upload and settle, and 4 min for a 3-run hold with 30 s gaps.

### Phase 0, readiness

**0.1 Software gaps closed and verified offline.** Done on 1 Sep in the
working tree (Section 13): the editor's time,value save, the panel's Seed
drive row, the FRQ-vs-record check, Hold's limit check, keep verticals;
`tests\smoke_test.py` at 50 checks and the editor's three checks all green.
Remaining at the bench: pull or commit that tree, restart the panel, and
confirm the Seed drive row and the keep verticals tick are on screen. Rule:
present: proceed. Absent (a stale checkout): update and restart before any
Phase 3 step. Present but a seeded Init refuses a correct pair of files:
the grid check is reading a different dt from one of them; open both in
the editor and compare the point counts before anything else.

**0.2 Channel map and coupling.** Config A. With the reference drive
selected on CH1 and its output ON, one Scope Grab capture of all four
channels. Expected: CH1 0 to 9.31 V ramp, CH3 0 to 5.2 V ramp, CH2 and CH4
flat (X2 idle). Then the same with the zero arb on CH1 and the X2 keeper
`MKJX2_i05` on CH2: CH2 and CH4 ramp, CH1 and CH3 flat. Read the coupling of
all four from the scope. Rule: map as expected: proceed. A monitor on the
wrong channel: correct the wiring (not the panel defaults) and repeat. A
photodiode still occupying a channel: it moves off or `pd_channel` is set in
`config.json` and that channel is excluded from this campaign; either way
the map is written in the notes. Output: the two `.csv` + `.txt` captures
under `scope_data\2026-09 map\`. 10 min.

**0.3 Today's floor and the Hold path.** Load `drive_PR1PX1C.state.npz`
(iteration 20 in the state; the keeper drive is i19, so first set the
session to i19 by loading and confirming the panel shows i19's drive, or
seed a copy `PRREFX1` from `drive_PR1PX1C_i19.csv` once B2 exists). Config
A. Hold: 3 runs, gap 30 s, keep-native ON. Predicted: rms 0.56 V, peak 3.1 V
(26 Aug). Gate in Section 3. Outputs: `run\meas_<stem>_i19_r01..03.npy` and
`_native.npz`; the Table CSV `run\iterations_<stem>.csv`. 5 min. This is
also the first hardware exercise of Hold: the log must show "hold: uploaded",
three "rN" lines, and "CH1 output OFF (end of hold)".

**0.4 X2 small-signal FRF.** Load or Init an X2 session on `target_PARX2`
(stem `PR1P3X2`, one-pole, gain 0.6076, tau 66 µs, f_cut 30 kHz, gamma 0.6;
Init touches no instrument). Delta: Measure FRF... with probe peak 0.5 V,
f lo 400 Hz, f hi 100 kHz, 72 tones, name `FR100Kp5VX2`; the dialog sets
probe verticals itself and restores them. Predicted: |H| 0.61 at 1 kHz,
coherence >= 0.9 to at least 60 kHz (the X1 0.5 V probe held it to 159 kHz).
Rule: coherent to >= 50 kHz: fit tau and (tau1, tau2) with `frf_fit.py` on
the new file, set X2's f_cut to the largest of 30/40 kHz whose peak
lambda*Q < 0.9, and rename the Phase 1 X2 stems accordingly (`PR1P4X2`,
`SR1P4X2` for 40 kHz). Coherence dies below 30 kHz: raise the probe to 1 V
and repeat once; still failing: X2 runs at 30 kHz on the X1 numbers and
that assumption is carried in the notes. Output: `run\frf_FR100Kp5VX2.csv`,
`.png`. 5 min.

**0.5 Idle policy.** Config A on CH2 with the CH2 output OFF: read the CH4
DC level (`ilc_bench.measure_vdc` on channel 4, or the panel's Auto-set
verticals and a single capture). Then upload an all-zero 5501-point arb
named `ZERO` to CH2, output ON, read again. Predicted from the memory:
kV-scale wander with the output off, within +/-0.1 V with the zero arb on.
Rule: wander > 100 V with the output off: the end-of-run policy for the
whole campaign is "select `ZERO`, output ON" on every channel not being
driven, applied by hand in the AWG GUI after each panel run (the panel
switches the driven output OFF at the end of every run and cannot yet do
otherwise, Section 13 I3). Wander < 10 V: the panel's OFF policy is
acceptable and the note is corrected. Between: the ZERO policy anyway, and
the finding is recorded. Output: the two readings in the notes with the
scope settings. 5 min.

### Phase 1, one-pole baselines

Common: Config A; one-pole rung; gamma 0.6; 20 iterations; 64 repeats;
keep-native ON; Auto-set once with the output OFF, then Run bench loop and
answer yes to output-ON. Warm-up: the first two iterations of each campaign
are expected to carry drift (memory: budget 2 to 3 warm-up iterations at
session start), so gates are read from i15 to i20. Predicted i00 raw error:
about 40 V rms on PAR, more on SERX1 (2x slew), less on SERX2. Predicted
contraction 0.4/iteration for the first 5 iterations on every campaign
(gamma-only, memory). Outputs per campaign: `run\drive_<stem>.state.npz`,
`drive_<stem>_iNN.csv`, `meas_<stem>_iNN.npy`, `_native.npz`, and the Table
CSV. About 10 min each plus setup.

**1.0 `PR1P4X1` (PARX1 at V90, X1).** Config A; target
`target_PARX1_V90.csv`; model gain 0.56, tau 70 µs, f_cut 40 kHz (the
`PR1PX1C` parameters); 20 iterations. Predicted: as `PR1PX1C` (floor 0.56 V
rms, 3.1 V peak) scaled by 0.986. Its keeper is the reference drive for
every Phase 3 condition. Rule per Section 3; if it fails its gate, Phase 3
falls back to `drive_PR1PX1C_i19.csv` with `target_PARX1.csv` (5200 V) and
the notes say so.

**1.1 `PR1P3X2` (PARX2 at V90, X2).** Target `target_PARX2_V90.csv`. Delta: AWG ch 2, scope ch 4, monitor col CH4;
CH2 1.5 V/div offset +4.3 V; model gain 0.6076, tau 66 µs (2 V X2 fit) or
the 0.4 value, f_cut 30 kHz or the 0.4 value. Rule per Section 3, with the
X2-specific expectation that the floor may sit 0.3 V above X1's from the
60 Hz term: a floor of 0.6 to 0.9 V rms with no band growing is "neither"
(chain noise), not "fail".

**1.2 `SR1P4X1` (SERX1 at V90, X1).** Target `target_SERX1_V90.csv`. Delta: none beyond the target; model
gain 0.56, tau 70 µs, f_cut 40 kHz (the `PR1PX1C` parameters). Additional
watch: the ramp slews 8.2 V/µs, 1.64 mA into 200 pF. If i00's error along
the ramp is a straight line rather than the exponential-lag shape seen on
PAR, or the contraction over i00 to i05 is slower than 0.5/iteration, the
amplifier is current- or slew-limiting; then the campaign continues (ILC
still converges on the repeatable part) but the keeper is flagged
"nonlinear regime" and the SER scheme's ramp rate becomes a design question,
not a tuning one. `check_limits` will not warn: it computes current from the
target at the assumed 200 pF.

**1.3 `SR1P3X2` (SERX2 at V90, X2).** Target `target_SERX2_V90.csv`. Delta: as 1.1. SERX2's slew is 2.5 V/µs, the
gentlest of the four; predicted i00 error the smallest.

**1.4 Keepers.** For each campaign the smoothest iteration among i15 to i20
by whole-record rms and by ripple (rms of the error above 2 kHz, from the
Error spectrum at k = 16), named in the notes as `drive_<stem>_iNN.csv`.

### Phase 2, the second-order rung on X1

**2.1 `PR2P5X1`.** Config A; target `target_PARX1_V90.csv`; Model = second
order; gain 0.56 in the gain box, then *Replace values with a fit... > Fit
to FRF* on `frf_FR200Kp5VX1.csv` up to 40 kHz with the gain box kept
(expected fn about 7.2 to 7.3 kHz, zeta 1.10 to 1.14, boundary 54 kHz in
the log; the values of `frf_fit.py` and of the panel agree to the fit
band); f_cut 50 kHz; gamma 0.6; 20 iterations. Predicted: contraction 0.4/iteration; floor below
0.5 V rms; 10 to 25 kHz residual about half of `PR1PX1C`'s; no growth
anywhere below 54 kHz. Rule per Section 3. The panel accepts zeta > 1 (the
entry check requires only zeta > 0) and `Plant.lead` is exact for two real
poles (verified by reading `eomilc\plant.py`, 1 Sep). 10 min.

**2.2 `PR2P6X1`, bracket.** Only if 2.1 converged. f_cut 60 kHz, else as
2.1. Predicted: peak lambda*Q 0.79, still stable on the 0.5 V FRF; on the 2 V
FRF 0.49. Rule: converges: the two-pole boundary is above 60 kHz and
production f_cut is 50 kHz with margin; diverges at 46 to 54 kHz: the
boundary is where predicted and production f_cut is 40 kHz; diverges below
40 kHz: the 0.5 V FRF overstates the large-signal phase margin and Phase 2
is closed with the one-pole rung. 10 min.

**2.3 Band analysis.** Offline: for `PR1PX1C` and `PR2P5X1` (and `PR2P6X1`
if run), the error amplitude spectrum at k = 16 of i15 to i20, band rms in
3 to 10, 10 to 25, 25 to 33, 33 to 40, 40 to 60 kHz, and the per-band growth
factor per iteration over i10 to i20. Output:
`docs\analysis\phase2_bands.csv` with columns
`stem, iteration, band_lo_Hz, band_hi_Hz, rms_mV_mon, k, window, dt_us`.

### Phase 3, edited-drive transfer

Common recipe per condition (about 12 min):

1. In the Waveform Editor: load the Phase 1.0 keeper
   `run\drive_PR1P4X1_iNN.csv` (reads `voltage_V`, verified) and
   `waveforms\target_PARX1_V90.csv`; apply the condition's operation to
   both; with the sample rate box at 500kHz and *ILC header
   (time_us,voltage_V)* ticked, save both (unticked, Save writes the
   header-less file the panel refuses). Files:
   `waveforms\edits\target_<stem>.csv`, `waveforms\edits\drive_<stem>_seed.csv`.
2. Numpy cross-check of the target (Section 6, item 4), logged.
3. Panel: Target = the edited target, **Seed drive** = the edited drive,
   channel EO1, stem as listed, model one-pole, f_cut 40 kHz, Init. The
   limit check runs on the seed and Init asks before keeping a failing
   one; a FAIL stops the condition (expected only for offsets past the
   cap). The state records the seed as `seed_path`.
4. If N changed: tick **keep verticals**, Auto-set with the output OFF
   (FRQ follows the new N x dt; the verticals stay at Configuration A).
   Bench mode and Hold refuse a channel whose FRQ does not match the
   record, so a forgotten Auto-set stops here rather than in the data.
   Read the FRQ line in the log and record it.
5. Bracket hold of the reference (1 run), then Hold on the condition, 3
   runs, gap 30 s, keep-native ON, then bracket hold again.
6. Table CSV saved as `run\iterations_<stem>.csv`.

Per-condition analysis (offline, one script for all): error e = target -
measurement in V at the EOM; whole-record rms and peak; rms in windows
[new corner - 300 µs, new corner + 300 µs], the hold interior (corner +
300 µs to exit - 20 µs), the ramps; the plateau level error as the mean of
e over the hold interior; each also for the bracket runs. Output:
`docs\analysis\phase3_windows.csv` with columns `stem, run, window_name,
t_lo_us, t_hi_us, rms_V, peak_V, mean_V, n_samples, target_file,
drive_file, bracket_before_rms_V, bracket_after_rms_V`.

| step | stem | operation on drive and target | N | prediction (V at EOM) | rule |
|---|---|---|---|---|---|
| 3.1 | `EC40X1` | clip to [0, 7.163 V] on the drive (4000 V / 0.5584 / 1000), [0, 4000 V] on the target | 5501 | plateau +14; corner transient 21 at 3.47 ms decaying over 0.1 to 0.3 ms; elsewhere at floor | Transfers if both are under tolerance. Plateau > 5 V alone: "transfers with a correction" and the correction is the measured plateau. Corner > 10 V: does not transfer. |
| 3.2 | `EC26X1` | clip at 4.656 V / 2600 V | 5501 | plateau +21; corner 15 at 1.80 ms | As 3.1. Plateau rising with a falling level while the corner falls: the two terms are separated. |
| 3.3 | `ES26X1` | subtract 4.656 V / 2600 V, then clip to [0, 4.656 V] / [0, 2600 V] | 5501 | plateau +21; low corner up to 115 (0.83 ms, the 4.1 V/µs part of the ramp), high corner 15 (1.80 ms) | As 3.1; the low corner is expected to fail tolerance, which is the result. |
| 3.4a | `EH60X1` | cut the hold from 1148 to 600 µs by removing 274 samples from its middle (samples 2650 to 2923), in both files | 5227 | at floor | Transfers. Above 3 V: the mechanism is not the correction tail; inspect the Trek settling in `meas_PARX1_i00.npy` at the natural corner. |
| 3.4b | `EH30X1` | hold 300 µs (remove 424 samples) | 5077 | about 1 | Marginal; reported as a value. |
| 3.4c | `EH15X1` | hold 150 µs (remove 499 samples) | 5002 | about 3 | Expected to fail tolerance at the exit corner. |
| 3.5 | `EL21X1` | insert 500 copies of the drive's own mid-hold sample (index 2750) and of the target's (5200 V) at the hold's middle | 6001 | at floor; any droop over the extra ms is the result | Transfers; a droop is quoted as V per ms. |
| 3.6a | `EOP50X1` | add +0.0895 V to the drive, +50 V to the target | 5501 | 0.6 antisymmetric | Report the +/- half-difference and the sum; a half-difference > 1 V means level dependence larger than the gain table. |
| 3.6b | `EON50X1` | add -0.0895 V, -50 V | 5501 | | |
| 3.7 | `EA40X1` | multiply both by 0.76923 (4000/5200) | 5501 | plateau +14; no corner term | The A/B partner of 3.1 (Section 6). |
| 3.8 | `PRTHX1` | seed = unedited reference; Hold 5 runs at 3.7 Hz, then the DS345 set to 0.37 Hz, 5 runs (wait s raised to 10), then 3.7 Hz again | 5501 | not established | Difference of the rate means > 1 V: production drives are converged at the experiment's rate (Phase 1 rerun at that rate); < 0.5 V: the bench rate is representative. |
| 3.9 | `SRXTX1`, `SRXTX2` | seeds = the SER keepers from 1.2 and 1.3. With the AWG GUI: upload the X2 keeper to CH2, burst as Config A, output ON; close it; Hold on `SRXTX1` (CH1/CH3), 3 runs. Then swap roles. | 5501 | at floor if no coupling; a dV/dt-shaped term at 1.80 ms on X2's record would be X1's 8.2 V/µs ramp | Any term > 1 V correlated with the other channel's ramp: the SER scheme needs both channels learned together (a two-channel loop the panel does not have). |
| 3.10 | `ET12X1` | resample both to 6601 points (x1.2) | 6601 | ramp-shaped 19 (tau 28 µs) to 48 (tau 70 µs) | Does not transfer (expected); the measured size against the two predictions estimates the effective lag. |

Time for Phase 3: 13 conditions at about 12 min plus 3.8's 15 min of slow
holds: about 3 h.

### Phase 4, warm start

**4.1 `WC40X1`.** Seed = `EC40X1`'s edited drive and target; one-pole,
f_cut 40 kHz; Run bench loop 6 iterations. Predicted: corner 21 V to
< 0.6 V by i04; whole-record rms at the floor by i04 to i05. Output: the
state and per-iteration files; `iterations_WC40X1.csv`.

**4.2 `FC40X1`.** Init on `target_EC40X1.csv` with the flat first shot (no
seed); 20 iterations. Predicted: floor by i08 to i14.

**4.3 `WS26X1`.** As 4.1 from `ES26X1` (two corners, one at 115 V).
Predicted: floor by i06.

Rule per Section 3. About 25 min.

---

## 11. Traps register

| trap | symptom at the bench |
|---|---|
| Editor output saved with *ILC header* unticked | the panel refuses it at Init/Plot with a message naming the box; nothing is misread any more, but the condition stops until the file is re-saved with the box ticked and the sample rate set |
| Record length changed, FRQ not | bench mode and Hold refuse with "FRQ is ... but this record is ... ms"; run Auto-set (output OFF, keep verticals ticked). If the checks are skipped: the burst plays stretched or compressed, the alignment check may still pass (it only checks the start), and the error trace shows the ramps shifted progressively later or earlier with a peak of tens of volts |
| Verticals re-ranged by Auto-set on an edited session | the Table's floor differs by 0.1 to 0.3 V from the bracket for no other reason; check the log's "scope CH3 (monitor)" line |
| Panel running stale code | after a pull or an edit to `ilc_gui.py`, features behave as before; restart the panel |
| Scope in AVER | bench mode refuses; Hold refuses. If skipped, every capture is one shot |
| AWG GUI or Scope Grab open | "No Keysight USB instrument found" or the mixed-VISA half-load; close both |
| Stem longer than 7 characters | the generator's front panel wedges after upload until power-cycled |
| Trek input floating | a channel whose generator output is OFF: monitor reads kV-scale and wandering (0.5) |
| Offset edit past the idle cap | Init's limit check FAILs on "first sample exceeds the 100 mV idle cap"; do not force; size the offset inside 56 V or change the cap deliberately |
| Clip applied to the drive at the wrong level | the plateau lands at a level other than the target's; the sign tells which of drive/target was mis-scaled (drive volts = target volts / 0.5584 / 1000) |
| Split (3.3) made with clip before subtract | the record starts mid-ramp at a nonzero first sample and the limit check FAILs on the idle cap |
| Hold-shortened by deleting the wrong span | a slope discontinuity where none was intended; compare the edited target against the numpy version before measuring |
| From calibration on the second-order rung | fn 2.3 kHz / zeta 0.21 appear in the boxes; the loop diverges at 3 to 6 kHz within 3 iterations, as in the parametric era |
| Fit from measurement on the second-order rung | returns a resonance fitted to the ramp's low-frequency content; do not adopt it |
| Mains-locked trigger | X2's 60 Hz appears as a repeatable 0.3 V rms error the loop tries to learn; verify the DS345 at 3.7 Hz |
| Zero baseline ticked | the reported error doubles on a record that moves in its first 5 % (not these targets, but the SER B target's flat lead-in tempts it) |
| Hold with a photodiode on CH3 | the "monitor" is the PD: a fringe-shaped trace, error of hundreds of volts |
| Sample 0 of raw readouts | 1235 mV in the first sample of any `_native.npz` or raw CSV; trim 5 samples |
| Two Phase 3 conditions on one stem | run numbers keep counting up and conditions become indistinguishable; one stem per condition |

---

## 12. Provenance rules

- Every quoted number names its file: `run\meas_<stem>_iNN[_rMM].npy` for a
  measurement, `run\drive_<stem>.state.npz` for a history entry,
  `run\iterations_<stem>.csv` for a Table figure, `docs\analysis\*.csv` for
  a windowed or spectral figure.
- Predicted values in this plan are labelled *predicted* when quoted next
  to a measurement. Values derived by subtraction (bracket-corrected) are
  reported next to their raw value, never instead of it.
- Metric definitions live in the output files: `phase3_windows.csv` carries
  window edges in µs, sample counts and the target file; `phase2_bands.csv`
  carries band edges, k and the window. Whole-record rms and peak are the
  panel's: over all N samples, no offset removed, in V at the EOM.
- Stems: `P`/`S` for parallel/series scheme, `R1P`/`R2P` for the rung,
  a digit for f_cut in tens of kHz, `X1`/`X2` for the chain; Phase 3 stems
  start with `E` and code the edit (`EC` clip, `ES` split, `EH`/`EL` hold
  short/long, `EO` offset, `EA` amplitude, `ET` time). One stem per
  condition; hold runs of one stem are one condition.
- Comparisons with the 26 Aug campaigns: same verticals (CH3 1 V/div), same
  repeats (64), same readout (20 000 points, 62 500 with keep-native), same
  gamma; the trigger rate differs (3 Hz then, 3.7 Hz now) and is stated
  wherever a floor is compared.
- Bench notes: one dated text file `docs\notes_2026-09-campaign.md` with,
  per step, the time, the stem, any deviation from Configuration A, the FRQ
  read back, and the idle policy applied.

---

## 13. Software readiness, verified 1 Sep 2026

What was run before the changes: `tests\smoke_test.py` (47 checks,
Anaconda), all passed; the editor's `--selftest` and `tests\smoke_test.py`,
both passed; the editor's `tests\check_py27.py` could not start (parso
0.5.2 was no longer in its target directory). A format round-trip test
across the three programs (`fmt_test.py`). A code read of `ilc_gui.py`
Init, Hold, bench and Auto-set, `ilc_bench.py` checks, `eomilc\ilc.py` and
`plant.py`, and the editor's file and Modify code.

**Status after the changes, 1 Sep 2026 (working tree, uncommitted):** B1,
B2, I1, I2 and I6 below are implemented. `tests\smoke_test.py` now has 50
checks (the header-less refusal, the FRQ-vs-record check with a fake
generator, the seed-drive Init with its state round trip and wrong-grid
refusal) and passes. The editor's `--selftest` (with a time,value round
trip added), its `tests\smoke_test.py`, and `tests\check_py27.py` (parso
0.5.2 restored under `tests\_p2`; 2.7 grammar ok, ASCII, uniform CRLF) all
pass. The editor has still never run on a real Python 2.7 machine. I3 and
I4 are unchanged by design. The list below is kept as the record of what
was found.

**Blocking for Phase 3 (both closed).**

- **B1. The editor's output does not load in the panel.** The editor writes
  `index,value` with no header, by design. The panel, the CLI and the bench
  driver all read targets through `run_ilc.load_target`, which takes the
  first row as a header and the first column as seconds: a 5501-point drive
  came back as 5500 points with dt = 1 s (`fmt_test.py`). The editor reads
  the panel's `time_us,voltage_V` files correctly (it picks `voltage_V`),
  so the break is one-directional. Done both sides: the editor has an *ILC
  header (time_us,voltage_V)* tick that switches Save CSV and Save all to
  that layout (needs the sample-rate box; `write_time_csv`; remembered
  between launches), and `load_target` refuses any file whose first line
  is numbers, naming that box in the message.
- **B2. No way to start a session from an existing drive.** Init always
  plays the flat conversion target/gain; `run_ilc.py init`, `ilc_bench.py`
  and the panel have no seed option, and the state's `u` is written by the
  loop alone. Every Phase 3 and 4 step needs iteration 0 = an edited drive.
  Done: a **Seed drive** path on the panel's Session frame and
  `--seed-drive` on `run_ilc.py init` (`run_ilc.load_seed_drive`: same N
  and dt as the target enforced, limit check applied with a keep-or-not
  question on failure, the path stored in the state as `seed_path` and
  preserved by the CLI step and the bench driver, shown in the session
  summary, cleared when the channel is switched).

**Important, work-arounds exist.**

- **I1. FRQ is not checked against the record.** `check_awg_channel` verifies
  AMP, OFST, clock and (optionally) sample rate, not the period. A record of
  a different length uploaded to a channel still at the previous FRQ plays
  time-scaled; `verify_alignment` cross-correlates only the record start
  and may pass. Auto-set does set FRQ = 1/(N dt) but only with the output
  OFF. Done: `check_awg_channel(expect_period=N*dt)` refuses a FRQ off by
  more than 1e-4 and names the value to set; the bench loop, Hold, Measure
  FRF and the CLI all pass it, and the readback note now prints FRQ.
- **I2. Hold does not run the limit check.** `_hold_work` uploaded `s.u`
  after the setup checks only; the bench loop calls `loop.check(u)` first.
  Once seeds exist, Hold is the path that plays hand-edited data. Done:
  Hold runs the check and refuses on FAIL.
- **I3. End-of-run output OFF versus the floating Trek input.** The panel and
  the CLI switch the driven output OFF whenever a run that played anything
  ends, by design. The 31 Aug measurement says a Trek with its generator
  output off wanders to kV. Step 0.5 decides; if the memory is confirmed,
  the run-end policy should become "select a stored zero arb, output ON",
  which is a behaviour change to agree on, not a bug fix.
- **I4. Second-order rung usable, but two buttons mislead.** zeta > 1 is
  accepted and the lead is correct for two real poles; From calibration
  filled the withdrawn resonance and Fit from measurement is biased on ramp
  records. Done 2 Sep: one button, *Replace values with a fit...*, opens
  a dialog offering *Fit to FRF* (`plant.fit_frf`: equal-weight log
  magnitude and phase over the coherent tones up to a chosen frequency,
  gain kept from the box or taken from the lowest tones, the in-band
  residual and the contraction boundary |1 - gamma H/H_model| = 1
  reported) or *Fit to measurement* (the time-domain identify), each
  explained, and Cancel. From calibration is removed; the Aug tables stay
  in `config.py` as the record and for the CLI seed only. Step 2.1 can
  now take fn and zeta from the button instead of by hand. Also done:
  the limit set is per channel (`Channel.limits`), so the GEN channel is
  bound by the generator rail alone rather than the Trek's 6 kV, slew,
  current and 100 mV idle numbers.
- **I5. Whole-record metrics only.** The Table reports whole-record rms and
  peak; Phase 3's corner windows and plateau means need the offline script
  specified in Phase 3. About 60 lines against `ilc_gui.load_session` and
  `recall_snapshots`.
- **I6. Auto-set re-ranges the monitor.** Verticals follow the session's
  spans, so edited sessions get a different lattice (Section 8). Done: a
  **keep verticals** tick on the bench panel; Auto-set then prints the
  V/div and offset it found and leaves them.

**Adequate as is.**

- The editor has every operation the five example edits need: Clip, scale
  and offset, Cut with 1-based inclusive spans, Assemble with repeats and
  gaps, Resample; `clipped` on the PARX1 target produced the expected 2027
  samples at 4000 V (`fmt_test.py`); its own round trip is exact to 5e-10.
- The AWG GUI reads the editor's header-less pair as (index, value) and
  would upload it correctly; it is not on the Phase 3 path because the
  panel uploads.
- Hold does what Phase 3 needs: N runs of the session's drive, no update,
  files tagged `_rMM`, optional native-rate copy, the setup checks, and the
  Table/Compare machinery already treats runs as first-class. It has not
  yet been exercised on the instruments (0.3 does that).
- Stems of 7 characters fit every name in this plan; the 16 kpt arb memory
  covers the 6601-point record of 3.10 if the datasheet figure holds
  (`BK4063B_MAX_PTS` overrides a refusal).
- Measure FRF from the panel is hardware-proven (the X1 0.5 V family was
  taken with it) and is what 0.4 uses.

Step 0.1 is therefore reduced to: pull the working tree (or the commit that
carries it), restart the panel, and confirm the Seed drive row and the keep
verticals tick are present.

---

## 14. Self-check and the weakest point

*Does any step depend on a later one?* Phase 3 uses the Phase 1.0 keeper
(fallback: the 0.3 reference), not Phase 2's keeper, so Phase 2 can be
skipped or fail without touching Phase 3. Phase 4 needs 3.1 and 3.3's seeds only. 3.9
needs 1.2 and 1.3's keepers. Nothing runs before its inputs exist.

*Two-branch rules?* Every rule in Sections 3 and 10 has a third branch.

*Corrections from their own data?* None (Section 7).

*Headline reachable only through the longest model chain?* Q3's rule table
rests on hold runs against edited targets, no model. Q2's adoption rule
rests on a band analysis of measured residuals; the two-pole parameters
enter only through the drive the loop built, and the bracket 2.2 tests the
predicted boundary independently.

*Reproducible from the files and this plan?* Each step names its inputs,
its stem and its outputs; the metric definitions travel in the CSVs. The
one thing not in a file is the hand-restored verticals (recorded in the
notes with the scope's own readback line from the log).

**Weakest point.** Q3 is answered on the 4063B in DDS burst mode, and the
workflow plays edited drives through a different card whose burst, idle and
grid behaviour are assumed (Section 2). If the card holds a different
inter-shot level, or resamples, the corner and hold results carry over but
the offset and idle findings do not. What would strengthen it: one edited
drive (3.1's) played through the actual card into the same Trek with the
scope on CH1 and CH3, compared with the 4063B's playback of the same file.
Second weakest: the tolerance in Q3 is a bench number, not the experiment's.
