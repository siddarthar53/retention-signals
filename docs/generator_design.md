# Generator design v0.1 (for agreement before coding)

Implements `config/master_data.yaml` v0.3 (contracts C1-C5) against `db/schema.sql` v0.6.1.
One command produces the whole dataset from one config and one seed:

```
python -m generator.run --scale 500      # starter
python -m generator.run --scale 10000    # full
```

## 1. Determinism

- `numpy.random.SeedSequence(seed)` is spawned into **independent named streams**: `master`,
  `population`, `careers`, `monthly`, `hazard`, `potential_outcomes`, `injection`. Changing one
  component does not shift the random draws of another.
- No wall-clock time anywhere. `recorded_at` is computed from the event date plus a seeded lag
  (e.g. workload: period end + 3-7 days; survey: submission + 0-2 days; events: same day).
- IDs are assigned in a documented sort order (below), so the same seed gives byte-identical files.
  Exports are written with sorted rows and fixed float formatting; SHA-256 per file goes into the
  manifest.

## 2. How keys are guaranteed (parents first, registry-only references)

The generator keeps an in-memory **registry** per table. A child row can only be created by passing
a parent **object** taken from the registry, never a typed ID string, so a child cannot point at a
parent that does not exist.

| Step | Creates | IDs (order of assignment) | References |
|---|---|---|---|
| 1 | compensation_band, role | BAND-01..06; ROLE-0001.. in family order x (IC L1-L5, Mgr L4-L6) | role -> band |
| 2 | team | TEAM-0001.. in family order | - |
| 3 | employee (initial) | EMP-000001.. sorted by (hire_date, team, level) | - |
| 4 | pre-window careers | ASGN / PROMO by effective date | assignment -> employee, team, role; promotion -> its assignment |
| 5 | monthly loop (2025-01..2026-12) | EEV, ASGN, PROMO, LVT, WLS, SURV in (date, employee) order | all -> employee; in-window hires get the next EMP ids in hire-date order |
| 6 | derived | SNAP-<emp>-YYYYQn, OUT-<emp>-2026, labels, potential outcomes | snapshot -> its assignment (same employee); label -> outcome (same employee) |
| 7 | anomaly fixtures + ground truth | GT-000001.. | copies of canonical rows, kept in `fixtures/`, never in canonical data |

Then three independent checks, in order:

1. **Load into PostgreSQL in one transaction** (COPY, parents first). Every PK, FK (including the
   composite same-employee FKs), UNIQUE and CHECK in the schema is enforced; a single orphan,
   duplicate or bad value rolls back the whole load.
2. **Validator** (rules in `docs/enforcement.md`, G2 rows): chains, chronology, balances,
   outcome/event/label reconciliation, totals, nothing after exit.
3. **ML join tests**: the training key is `(employee_id, as_of_date)`. Feature rows, label rows and
   snapshot rows must join 1:1 on that key: no row lost, none duplicated.

## 3. Population and careers

- Totals per C1/C2: initial = round(N x 0.9); teams = round(initial / 15); families, levels and
  managers by largest-remainder allocation; one manager per team.
- Tenure ~ gamma(mean 4y, shape 1.6), capped 1 month-18 years. Pre-window careers per C3:
  k = sampled promotion count capped by feasibility; hired k levels below the 2025 role.
- Team created_date = min(draw in 2012-01-01..2024-06-30, earliest member assignment).

## 4. Monthly loop (causal order inside each month)

For every employee active on the 1st or hired during the month:

1. **Leave credits**: EL +1 on the 1st (or hire date); SL +3 on Jan 1 (or hire date).
2. **Latent state update** (never exported): workload intensity (AR(1) around a personal level,
   plus team shocks, benign spikes of 1-2 months, sustained-overload episodes of 4+ months),
   engagement (drifts down with sustained overload, time since promotion, low manager support;
   recovers after promotion or transfer), external pull (person-level), manager-relationship
   quality (person-team level).
3. **Career events**: promotion (hazard rises with months in role), transfer, lateral move;
   manager exit -> vacancy -> filled by internal promotion or external hire in 1-3 months.
4. **Leave taken**: EL blocks (more in December, before lapse; heavier while on notice), occasional
   SL days. Capped by balance; split per calendar month; workdays only.
5. **Workload**: scheduled_workdays from the calendar and employment days;
   available = (scheduled - leave days) x 8; actual = round(intensity x available, 1);
   utilisation from the stored values. After-hours and on-call depend on intensity only. Sick
   leave therefore cannot change any model input (paired-record test at G2).
6. **Quarter end** (Mar/Jun/Sep/Dec): survey wave sent on the 15th of the last month. Items =
   clip(round(engagement + item noise), 1, 5). Response: 76% complete / 4% partial / 20% none;
   non-response is more likely in high-intensity months (missing at random given workload); no
   dependence on latent disengagement in the main dataset.
7. **Exit hazards** (only for employees not already on notice):
   - voluntary: logit(p_month) = b0 + b_eng x (3.6 - engagement) + b_trend x engagement decline
     + b_sust x sustained_overload + b_promo x months_since_promotion_beyond_24
     + b_new x (tenure < 12 months) - b_transfer x recent_transfer
     + **latent** external_pull + **latent** manager quality + noise.
     Benign spikes carry zero weight. b0 is calibrated once (recorded in config) so cumulative
     voluntary exits over 24 months are ~10% of all histories; the validator requires 5-15%.
   - involuntary: small constant monthly hazard (~2% over the window), independent of the above.
   - A resignation creates `resignation_submitted` (notice days by level) and a later `exit`; the
     person keeps working (and burning EL) during notice. All later rows stop at termination,
     except the Dec 31 leave lapse (C5).
8. **Backfill / growth hires** per C1 (target shares, real exits only, fallback to growth).

## 5. Derived tables

- tenure_snapshot at each quarter-end for employees active that day, with `derived_from` lineage.
- attrition_outcome: one per employee from the employment events (active / on_notice /
  exited_voluntary / exited_involuntary).
- horizon_label for every employee at every quarter-end: ineligible (not employed, resignation
  already recorded, or under one full quarter of history), voluntary_exit, employed_through_horizon,
  competing_exit, unresolved (horizon after 2026-12-31).
- potential_outcome for everyone active on 2026-06-30 and not on notice: every person-month from
  July to December has one pre-drawn uniform number u. Untreated: exit if u < h (this IS the
  recorded history). Treated: exit if u < 0.7h. Common draws make y_if_treated <= y_if_untreated.
  The generator does not know the model. The G4/G5 pipeline creates the 80/20 intervention records
  for the reviewed list and reads their outcomes from this table.

## 6. Anomaly injection (separate `fixtures/`, plus `ground_truth`)

About 1% of eligible rows per case (configurable), each with a clean near-valid control:

| Case | Injected | Must be caught by | Control (must NOT be flagged) |
|---|---|---|---|
| FUTURE_LEAK | copies of workload/survey rows with event date <= as_of but recorded_at after the cutoff | feature builder (reject_feature) | recorded exactly at the cutoff |
| MISSING_AS_NEGATIVE | no-response rows carrying an engagement score; a feature file imputing missing as 1.0 | DB load -> quarantine; feature audit | valid partial response with 3 items |
| LABEL_DATE_MISMATCH | labels contradicting outcome dates (exit after horizon labelled 1; exit before as_of) | validator (flag_label) | termination exactly on horizon_end labelled 1 |
| UNREVIEWED_SCORE | intervention requests: no review, dismissed review, stale score version, other team | API review gate (block_action) at G5 | approved, current, in-scope request |

Detection is reported as precision/recall against ground truth, with false positives on controls.

## 7. Outputs

`data/out/<scale>/canonical/*.csv|json`, `seed.sql` (COPY format), `fixtures/`, `ground_truth.csv|json`,
`manifest.json` (config hash, seed, row counts, SHA-256 per file), `validation_report.md`.
Reproducibility check: run twice, compare manifests.
