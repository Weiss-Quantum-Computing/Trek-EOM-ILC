# docs/history: finished work, kept for the record

Nothing here describes the current workflow. These are the write-ups, plans
and design notes of campaigns that are over, kept because the code and the
calibration constants still rest on their results. For how the loop is run
today read `HANDBOOK.md` at the top of the repository; for the grammar of
every field, file and plan key read `REFERENCE.md` beside it.

| when | what | why it is still worth having |
|---|---|---|
| 20-21 Aug 2026 | `../../characterisation/` (not in this folder: it has scripts) | the 40-capture analysis behind every constant in `eomilc/config.py` |
| 24-25 Aug 2026 | `2026-08_report/REPORT.md` and its `figures/` | the campaign that replaced the parametric inverse with the measured FRF: the capture ladder (HRES singles over AVER), the probe, the model-form comparison, the final numbers and the instrument catalog. The panel's FRF overlay still cites its section 5 |
| 25 Aug 2026 | `2026-08_MKJ_FULL_NOTES.md` | what the original MKJ waveform was, its headroom arithmetic and how the generator's DDS mode plays it |
| 1-2 Sep 2026 | `2026-09_ramp_edit_plan/PLAN.md` and its `analysis/` CSVs | the September methods plan: one-pole baselines, the second-order rung on X1, edited-drive transfer, warm start, with the band tables the phases were costed from |
| 13 Oct 2026 | `2026-10_OPTICAL_CLOSE_DESIGN.md` | the design the optical close was built to (library, pair mode, batch stage, Optical tab) and its addendum on per-crystal measurement. The code comments in `ilc_gui.py` and `ilc_batch.py` still point here for the contracts |

Superseded bench outputs (iteration drives, scratch campaigns, the probe-era
files) are not in the repository at all; they sit untracked in `archive/`
on the bench PC, with their own README.
