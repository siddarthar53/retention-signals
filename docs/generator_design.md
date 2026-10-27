# Generator design v0.2 (for agreement before coding)

Implements `config/master_data.yaml` v0.3 (contracts C1-C5) against `db/schema.sql` v0.6.2.
v0.2 applies ChatGPT's review of v0.1 (outcome definition, join contract, SL quantisation, in-month
timing) and the agreed answers on treatment effect, calibration and minimum history.

```
python -m generator.run --scale 500      # starter
python -m generator.run --scale 10000    # full
```

## 1. Determinism

- `numpy.random.SeedSequence(seed)` spawned into independent named streams: `master`, `population`,
  `careers`, `monthly`, `hazard`, `injection`. Changing one component does not shift another's draws.
- No wall clock. `recorded_at` = event date + a seeded lag (rules in section 4).
- IDs assigned in documented sort orders; exports sorted with fixed number formatting; SHA-256 per
  file in the manifest; reproducibility = two runs with identical manifests.
- Hazard coefficients and the calibrated intercept `b0` are **fixed in `config/generator.yaml`** with
  their calibration provenance (development seeds, starter scale, never the test-period outcomes).
  They are not re-solved per run, and seeds are never chosen because a run passed.

## 2. Keys and joins

### 2.1 Creation
A child row can only be created from a parent object held in the in-memory registry (never a typed
ID). Order: bands -> roles -> teams -> employees -> pre-window careers -> monthly history ->
derived tables -> fixtures. IDs: see `docs/data_dictionary.md`.

### 2.2 Scoring population (the ML join contract)
For each quarter-end `as_of` in 2025Q1..2026Q4:

| Set | Definition |
|---|---|
| **Employed set E(as_of)** | hire_date <= as_of and no termination on or before as_of |
| **Snapshot set** | exactly E(as_of): one `tenure_snapshot` per employed person |
| **Label set** | exactly E(as_of): one `horizon_label` per employed person |
| **Scoring population S(as_of)** | E(as_of) minus `ineligible` labels |

Labels are created only for E(as_of) (people not employed at as_of get no label row; the
`not_employed` reason is therefore unused and documented as such).

`ineligible` reasons, evaluated in this order:
1. `resignation_known`: a resignation_submitted event with event_date <= as_of and recorded_at <= as_of.
2. `insufficient_history`: hire_date > first day of the scoring quarter. (April 1 hire is eligible on
   June 30; April 2 is not.) Missing surveys or reporting lag never make someone ineligible.

The feature table has **exactly one row per key in S(as_of)**, built with an explicit key list
(left joins from S, never an inner join that can silently drop people). The G2 report reconciles:
|E| = |snapshots| = |labels|; |S| = |E| - ineligible counts by reason; |features| = |S|;
duplicates = 0; keys lost = 0. IDs are join and lineage keys only, never predictors.

### 2.3 Load and validation gates
1. One-transaction PostgreSQL COPY load: every PK / FK / composite same-employee FK / UNIQUE / CHECK.
2. Validator (`docs/enforcement.md`, G2 rows), including outcome-event-label reconciliation.
3. ML join contract above.

**G2 report (reviewable):** per-table row counts; PK/unique duplicates = 0; FK and composite-FK
orphans = 0; same-employee lineage mismatches = 0; invalid assignment/date relationships = 0;
expected-vs-actual key sets per as_of; join loss/multiplication = 0; exclusions by reason; realised
attrition (with its denominator); realised hire mix; SL paired-invariance result; two-run hashes.

## 3. Population and careers
As v0.1 (C1-C3): exact totals, largest-remainder allocation, feasibility-capped pre-window careers,
team created_date = min(draw, earliest member assignment).

**Promotion clock** (used by hazard and features, never NULL in arithmetic):
`months_since_promotion_or_hire` = months since the latest promotion, or since hire_date if never
promoted. The stored `tenure_snapshot.months_since_promotion` stays NULL for never-promoted people;
the derived clock is a separate, fully defined quantity.

## 4. Monthly loop with explicit dates

Each month m (2025-01 .. 2026-12) is processed in this order. Hires for month m are scheduled
before the month starts (growth hires on fixed dates; backfills chosen 1-3 months earlier), so every
hire has hire-month records.

| Step | Date(s) inside month m | Rule |
|---|---|---|
| 1. Leave credits | 1st (or hire date) | EL +1; SL +3 on Jan 1 (or hire date). |
| 2. Latent state update | effective from the 1st | intensity, engagement, external pull, manager quality. This state is fixed for the whole month. |
| 3. Career events | seeded day in month | promotion / transfer / lateral move; manager vacancy handling. |
| 4. Leave taken | seeded workdays | EL blocks (more in December, heavier on notice), occasional SL days. Capped by balance and by `scheduled_workdays - 1`, so leave alone never zeroes a month's capacity. Split per calendar month. |
| 5. Workload | whole month, recorded 3-7 days after month end | scheduled_workdays = workdays from max(1st, hire_date) to min(month end, termination). utilization_pct = round(intensity x 100, 1) is generated first; available = (scheduled - leave days) x 8; actual = utilization x available / 100, stored exactly. After-hours and on-call depend on intensity only. Zero capacity only when scheduled_workdays = 0. |
| 6. Survey (quarter-end months) | sent 15th; submitted 15th-22nd; recorded 0-2 days later | Answers use the month's latent state (step 2), so nothing after submission leaks into them. Only people employed on the 15th are surveyed. |
| 7. Exit hazards | event day drawn uniformly in the month | Use the month's latent state and the promotion clock. Resignation -> notice days by level -> termination = resignation + notice. Termination can cross months and years. The person stays employed during notice. |
| 8. Backfill / growth scheduling | for future months | C1 targets, real exits only. |

**Availability at a scoring date (as-of rule):** a row is usable at as_of only if its event date
<= as_of **and** recorded_at <= as_of. So June workload (recorded in early July) is not available
on June 30; June survey answers (recorded by ~June 24) are.

**Voluntary hazard (monthly):**
logit(p) = b0 + b_eng x (3.6 - engagement) + b_trend x max(0, engagement decline over 2 quarters)
+ b_sust x sustained_overload + b_promo x max(0, months_since_promotion_or_hire - 24)
+ b_new x (tenure < 12 months) - b_transfer x (transfer in last 6 months)
+ external_pull (latent) + manager_quality_effect (latent).
Benign spikes carry zero weight. Involuntary exits use a small constant monthly hazard.

**Attrition measure (one explicit definition):** voluntary terminations observed between
2025-01-01 and 2026-12-31 / total employee histories (N). Target 10%, required 5-15%. Involuntary
exits and resignations still on notice at 2026-12-31 are reported separately.

## 5. Derived tables
- tenure_snapshot: one per (employee, as_of) in E(as_of), with `derived_from` lineage.
- attrition_outcome: one per employee, from employment events.
- horizon_label: one per (employee, as_of) in E(as_of). Target uses the voluntary **termination**
  date: 1 if voluntary termination on or before horizon_end; 0 if employed at horizon_end (even if
  on notice) or exited involuntarily within the horizon; `unresolved` if horizon_end > 2026-12-31.

## 6. Intervention outcomes (single timeline, no treated re-simulation)

Eligible June population J = S(2026-06-30) (employed, no known resignation, sufficient history).

- **Untreated outcome** y0 = the canonical `horizon_label.target` for (employee, 2026-06-30). This is
  asserted equal for every employee in J.
- **Treated outcome** y1: for each employee in J the canonical `hazard` stream draws one uniform
  v during generation. If y0 = 1 and v < 0.30, the voluntary resignation that produced y0 is
  **prevented** and y1 = 0; otherwise y1 = y0.
- Therefore y1 <= y0 by definition (proved by construction, asserted in tests). The expected
  effect is a 30% relative reduction of voluntary terminations within the horizon among treated
  people. This is a documented synthetic assumption, not real-world causal evidence.
- Notice delay and competing exits are preserved because y0 comes from the canonical label. If y0 = 0
  (stayed, resigned with termination after the horizon, or competing exit) then y1 = 0: treatment
  cannot create an exit.
- The generator does not know the model. G4/G5 creates the 80/20 intervention records for the
  reviewed list and reads y0/y1 from `eval.potential_outcome`.

## 7. Anomaly injection (`fixtures/` only, with ground truth)

| Case | Injected | Caught by | Clean control |
|---|---|---|---|
| FUTURE_LEAK | A feature snapshot for as_of that includes a legitimate source row whose recorded_at is after the cutoff (the source row itself stays valid history) | feature builder: reject the inclusion (reject_feature) | row recorded exactly at the cutoff, included |
| MISSING_AS_NEGATIVE | no-response survey rows carrying an engagement score; a feature file imputing missing as 1.0 | DB load -> quarantine; feature audit | valid partial response with 3 items |
| LABEL_DATE_MISMATCH | labels contradicting outcome dates (termination after horizon labelled 1; termination before as_of labelled) | validator (flag_label) | termination exactly on horizon_end labelled 1 |
| UNREVIEWED_SCORE | intervention requests with no review, a dismissed review, a stale score version, or another team | API review gate (block_action) at G5 | approved, current, in-scope request |

## 8. Boundary fixtures (tests at G2)
Mid-month hire and exit; hire on the first day of a quarter vs the second day; resignation in
December with termination in February (leave accrual, Jan 1 SL grant and Dec 31 lapse across the
boundary); June workload recorded in July excluded at June 30; June survey recorded June 24
included; leave capped so capacity never reaches zero; paired SL records with identical allowed
features (persisted and reloaded from PostgreSQL), including low-capacity months.

## 9. Outputs
`data/out/<scale>/canonical/*.csv|json`, `seed.sql` (COPY format), `fixtures/`,
`ground_truth.csv|json`, `manifest.json` (config hash, seed, row counts, SHA-256 per file),
`g2_report.md`. Nothing generated is committed (public repo).
