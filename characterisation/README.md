# EOM ramps day 3 - distortion analysis

Run with the Anaconda interpreter (needs scipy + pandas):
`C:\ProgramData\anaconda3\python.exe`

Order:
1. `mkcache.py`   - converts every .csv to .npy in ./cache (loadtxt on 40 x 6.5 MB is the bottleneck)
2. `analysis2.py` - core fit: gain+offset, +1 pole, +2nd order, +unconstrained FIR. Writes results2.json
3. `summary2.py`  - stage gains, Trek mismatch, resonance vs amplitude, preconditioning pole
4. `final.py`     - the quotable residual numbers (200 us smoothed). Writes final.json
5. `repeat.py`    - repeatability test that separates real residual from the 8-bit scope floor
6. `inl.py`       - static nonlinearity measured inside single captures
7. `freqnoise.py` - frequency response + noise spectra
8. `design.py`    - overshoot vs commanded ramp duration
9. `figs2.py`     - the two report figures

Two traps this code exists to avoid:
- Do NOT normalise by plateau medians. During the hold both channels sit on a single ADC
  code, so that scaling carries up to half an LSB and inflates every residual by ~0.75 %.
  Use the least-squares affine fit in `analysis2.affine`.
- The -0.7 %/decade gain tilt vs amplitude is scope V/div range error, not the circuit.
  It shows up identically on the passive divider, which cannot have one.

Fitted plant, Trek 610E + EOM (large-signal ramp fits):
  2nd order, zeta = 0.222 +/- 0.019, fn = 2.2 - 3.0 kHz (falls with drive amplitude)

Superseded 25 Aug 2026: the 2 V FRF probes found no resonance at probe level -
the peak was an artifact of these large-signal fits (the group delay they imply,
~28 us, was right). The fit still seeds the model-based first shot, but the loop
runs on the measured FRF (--frf); see ../docs/REPORT.md sections 4-5.
