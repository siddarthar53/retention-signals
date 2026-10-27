# Correction history

Records every change made after the 30 Jun 2026 test list had already been scored and inspected, so the
evaluation can be read honestly. Coefficients, features, thresholds and the test date were **not** retuned
against the test list at any point.

## C1 — survey answers read later career events (review of 95842be, finding 3)

**Defect.** In a month with a promotion or transfer, the generator updated the promotion clock and manager
quality *before* writing that month's survey. A promotion on 25 June could therefore change the career item of a
survey submitted on 18 June, and a later transfer could change its manager item. The survey row was dated before
the event it reflected, which is a future-information leak inside the synthetic data.

**Fix.** Survey career inputs are now taken as of the submission day: month-start values plus only the
changes dated on or before submission (`generator/history.py: career_as_of`). Regression tests:
`tests/generator/test_history.py::test_survey_career_inputs_ignore_changes_after_submission` and
`::test_generator_reads_survey_career_state_as_of_submission`.

**Effect on data.** Same seed (20261022) and configuration. Only `core.engagement_survey` changed:
53 of 71,651 rows (26 career items, 27 manager items, 53 engagement indices).

| File | SHA-256 before | SHA-256 after |
|---|---|---|
| `canonical/core.engagement_survey.csv` | `be82544cbf686560…` | `2084b6a2472b582e…` |

All other exported files are byte-identical.

**Effect on the evaluation (re-run, same frozen procedure).** The model was retrained by the same code on the
corrected training quarters and evaluated once more on the same 30 Jun 2026 list. This is a corrected
evaluation of an already-inspected test list, not a new untouched test.

| Measure | Before | After |
|---|---:|---:|
| LR ROC AUC | 0.8175 | 0.8175 |
| LR PR-AUC | 0.1354 | 0.1354 |
| LR lift in top 5% | 6.82 (5.53-8.17) | 6.82 (5.53-8.16) |
| LR calibration slope | 0.943 | 0.943 |
| Scorecard PR-AUC ratio (diagnostic, target ≥ 0.75) | 0.51, not met | 0.51, not met |

## C2 — one allocation rule for every approved case (review of 95842be)

**Change.** Simulated reviews used a seeded random draw for the 80/20 split while the live API used a hash.
Both now use `pipeline/allocation.py` (`arm-v1`: SHA-256 of rule version, run and employee). In the same change
the simulated review loop was altered: cases are iterated in employee order (C3) and the per-case random draw
for the arm was removed, which shifts the remaining random draws and therefore which cases stay pending or are
dismissed. The illustrative intervention comparison changed:

| | Before | After |
|---|---:|---:|
| Approved / conversation / comparison | 318 / 253 / 65 | 314 / 254 / 60 |
| Estimated effect (95% CI) | -7.4 pts (-17.3 to 2.4) | -12.5 pts (-23.2 to -1.8) |
| True simulated effect | -3.5 pts | -3.2 pts |

The difference reflects all three changes together (a different reviewed and approved cohort, 318 to 314,
hence a different true effect; the new arm rule; the new iteration order). It is not an isolated allocation
experiment. What it does show is that with ~60 controls the comparison moves by several points under small
changes, so it demonstrates the measurement workflow only.

## C3 — case identifiers no longer encode rank

Score IDs were assigned in rank order (`SCR-2026-0000006` was rank 6) and were visible to managers. They are
now assigned in employee-ID order; simulated review and intervention IDs follow employee order too.
