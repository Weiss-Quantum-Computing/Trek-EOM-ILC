# MKJ_full.csv — what it is, and the one thing that breaks

## The file

106020 points, unipolar 0 → 1, starting and ending at exactly 0.

At the 10.602 ms period that is **100.000 ns per point — exactly 10 MSa/s**, so
the period was picked to make the record play at its native rate. Nothing
accidental about it.

| | |
|---|---|
| up leg | 4.6875 ms |
| hold at max | ~1.23 ms within 1 ppm (the 1 ms hold plus the settled tails) |
| down leg | 4.6875 ms |
| symmetry | down leg is the exact time-reverse of the up leg, to 5×10⁻⁸ |
| peak slope | 3.432 V/µs at 5400 V — 5.8× under the 20 V/µs limit |

The up leg is the same shape as `waveform_tuned_10kHz_4p8ms.csv` with its
settled tail trimmed: 0.012% RMS agreement on the same time base, and the same
peak slew. So everything already characterised carries over unchanged, and the
down leg — the thing that was missing before — is just as gentle as the up leg.

## The headroom arithmetic

At **AMP 20 Vpp / OFST 0** (±10 V at the generator), and note that 20 Vpp is the
4063B's ceiling — so ±10 V is the end of the road. The only ways to buy room for
the correction are to lower the target or raise the divider ratio ahead of the
Trek.

**Headroom comes from AMP sitting below the generator's ceiling, not from the
record sitting below 1.** With AMP already at maximum, capping the record at 0.95
does not create reserve — it spends reach you do not have.

That was written when the target was 5400 V, where it was a real problem:

| | needs for 5400 V | record cap 0.95 reaches | ceiling at cap 1.0 |
|---|---|---|---|
| **EO1** | 9.674 V | **5303 V — 97 V short** | 5582 V (+3.4%) |
| EO2 | 8.889 V | 5771 V (fine) | 6075 V (+12.5%) |

**Superseded 2026-08-24: the target is now 5200 V on both channels**, chosen so
X1 has definite headroom and the two channels are symmetric. That resolves it —
no record cap needed, and the correction has room to work:

| | needs for 5200 V | record peak | ceiling | spare |
|---|---|---|---|---|
| **EO1** | 9.312 V | 0.9312 | 5584 V | **7.4%** |
| EO2 | 8.558 V | 0.8558 | 6076 V | 16.9% |

Verified against a real correction: simulating a capture with +1% gain, +6% fn
and +10% zeta error, the first ILC step moves X1's drive from 9.312 to 9.478 V —
still 5.2% clear of the rail. At the old 5400 V target the same step tripped the
limit guard.

`outputs.headroom()` computes this for any target, so check before committing to
one rather than finding out at iteration 1.

Raising EO1's divider from 0.6254 to about 0.68 is still worth doing — it moves
the hardware clamp from 5584 V to 6000 V, right at the stated EOM limit rather
than below it, and it is the same change wanted to equalise the 12% Trek gain
mismatch. Two things fixed at once.

## Working in DDS

**The period governs, not the record length.** DDS resamples whatever is stored
into one period, so the point count is free — a 10602-point record at the same
10.602 ms period plays identically to the 106020-point one. That makes the
learning grid an independent choice.

**Decided 2026-08-24: the loop runs on a 2 µs grid — 5301 points — and uploads
at that length.** `resample_points` is still there if you ever want the answer
back at the native 106020, but under DDS the point count is free, so there is no
reason to.

    u_native = outputs.resample_points(u, 106020)   # if you ever need it

Decimating MKJ_full 20:1 costs 3.2 V peak / 1.3 V rms at the EOM against the
0.1 µs source — 0.06% of full scale. That was comfortably under the ~5 V floor
of the 256-average era; the HRES scheme's 0.5–1 V floor and the campaign's
2.4 V final residual have since caught up with it. It still doesn't bite,
though: the loop tracks the decimated target *exactly*, so the departure is
versus the 0.1 µs source shape — not an error the loop sees or chases.

Why not learn at 10 MSa/s directly: the 2 µs grid's Nyquist is 250 kHz,
comfortably above the 75 kHz the measured-inverse campaign ended up correcting
to. (The "20 kHz Q filter" ceiling this note originally cited belonged to the
parametric era and is long superseded — see REPORT.md — but the conclusion
survives the change.) The scope samples at 160 ns, so interpolating
measurements up to a 100 ns grid invents structure that isn't there. And every
iteration would write a 106020-line file for no gain.

Worst case if the generator zero-order-holds between stored points rather than
interpolating: at the 5301-point / 2 µs record the steepest part steps by about
**6.6 V** at the EOM (peak slew 3.30 V/µs at 5200 V) — well above the 0.5–1 V
measurement floor and the 2.4 V residual the campaign reached.

**Answered 25 Aug: it interpolates.** The wide-probe detail capture (REPORT.md,
"the wide probe as actually played") shows the generator reconstructing
smoothly between the stored 2 µs samples — its reconstruction *attenuates* the
fastest content rather than stepping — and the corrected ramps' 2.4 V peak
residual is incompatible with 6.6 V ZOH steps at the steep sections. No grid
change needed.

## Two things to expect

**The corrected drive will not be symmetric.** The target is (up leg =
time-reverse of down leg), but the amplifier's lag is causal: it delays both
legs in the same direction, so the up leg reads low and the down leg reads high.
Pre-distortion therefore has to push the two legs in opposite senses. If someone
sees the asymmetry in the corrected drive and "fixes" it, they undo the
correction.

**Drives can go straight into the generator's own library.** `write_bk_waveform`
emits the single-column, marked format the AWG GUI keeps in its `Waveforms`
folder, so each iteration appears in the memory list and previews like any other
stored waveform:

    outputs.write_bk_waveform(path, u_awg, "MKJX1_i00", full_scale=10.0)

`run_ilc.py` writes one of these automatically beside every drive, as
`*_awg.csv`. **Keep the name to 11 characters** — the generator stores it as
`<name>.bin`, and past 15 stored characters it wedges its front panel until you
power cycle it. `MKJX1_i00` is nine.

It divides by a **fixed** full scale, never by the record's own peak — upload
with Normalise unticked. Normalising per iteration is what silently rescales the
correction the loop just computed.
