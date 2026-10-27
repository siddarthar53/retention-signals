# Data dictionary

Generated from the PostgreSQL catalog by `scripts/gen_dictionary.py` (do not edit by hand).
Types, nullability, keys and check expressions are the enforced definitions; descriptions come from the `COMMENT`s in `db/schema.sql`. Rules enforced outside the database (validator, service layer) are listed in `docs/enforcement.md`.

## Contents

- [`core.assignment`](#coreassignment)
- [`core.attrition_outcome`](#coreattrition_outcome)
- [`core.compensation_band`](#corecompensation_band)
- [`core.employee`](#coreemployee)
- [`core.employment_event`](#coreemployment_event)
- [`core.engagement_survey`](#coreengagement_survey)
- [`core.leave_ledger`](#coreleave_ledger)
- [`core.promotion_history`](#corepromotion_history)
- [`core.role`](#corerole)
- [`core.team`](#coreteam)
- [`core.tenure_snapshot`](#coretenure_snapshot)
- [`core.workload_snapshot`](#coreworkload_snapshot)
- [`core.assignment_interval`](#coreassignment_interval) (view)
- [`core.earned_leave_ledger`](#coreearned_leave_ledger) (view)
- [`app.app_user`](#appapp_user)
- [`app.exception_log`](#appexception_log)
- [`app.intervention`](#appintervention)
- [`app.intervention_event`](#appintervention_event)
- [`app.review_decision`](#appreview_decision)
- [`app.score`](#appscore)
- [`app.score_run`](#appscore_run)
- [`app.user_credential`](#appuser_credential)
- [`app.user_session`](#appuser_session)
- [`eval.ground_truth`](#evalground_truth)
- [`eval.horizon_label`](#evalhorizon_label)
- [`eval.potential_outcome`](#evalpotential_outcome)

## `core.assignment`

Employee-team-role history (the N:M bridge). Each row starts an interval; the interval ends where the next row for the employee starts, or at termination (see core.assignment_interval).

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `assignment_id` | text | no | PK, UNIQUE |  | ASGN-YYYY-NNNNNN, YYYY = effective_from year. |
| `employee_id` | text | no | FK core.assignment(assignment_id, employee_id), FK core.employee(employee_id), UNIQUE |  | Employee (EMP-NNNNNN) this row belongs to. |
| `team_id` | text | no | FK core.team(team_id) |  | Team for this interval. |
| `role_id` | text | no | FK core.role(role_id) |  | Role for this interval. |
| `effective_from` | date | no | UNIQUE |  | First day of this team/role assignment. |
| `change_reason` | text | no |  |  | hire \| transfer (team change) \| promotion (level up) \| lateral_move (role change, same level) \| reorg. |
| `previous_assignment_id` | text | yes | FK core.assignment(assignment_id, employee_id) |  | The assignment this one replaces (same employee). NULL only for the hire assignment. |
| `recorded_at` | timestamp with time zone | no |  |  | When the row became known to the system (availability time used by as-of filters). |

**Constraints**

- `assignment_change_reason_check`: `CHECK ((change_reason = ANY (ARRAY['hire'::text, 'transfer'::text, 'promotion'::text, 'lateral_move'::text, 'reorg'::text])))`
- `assignment_first_is_hire`: `CHECK (((change_reason = 'hire'::text) = (previous_assignment_id IS NULL)))`
- `assignment_id_format`: `CHECK ((assignment_id ~ '^ASGN-\d{4}-\d{6}$'::text))`
- `assignment_no_self_reference`: `CHECK ((previous_assignment_id IS DISTINCT FROM assignment_id))`
- `assignment_chain_same_employee`: `FOREIGN KEY (previous_assignment_id, employee_id) REFERENCES core.assignment(assignment_id, employee_id)`
- `assignment_assignment_id_employee_id_key`: `UNIQUE (assignment_id, employee_id)`
- `assignment_one_start_per_day`: `UNIQUE (employee_id, effective_from)`
- `assignment_one_hire_root`: `btree (employee_id) WHERE (change_reason = 'hire'::text)` (unique index)

## `core.attrition_outcome`

One terminal outcome per employee at the end of observation (derived from employment events). Not readable by the app role.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `outcome_id` | text | no | PK, UNIQUE |  | OUT-<employee-id>-YYYY, YYYY = observation-end year. |
| `employee_id` | text | no | FK core.employee(employee_id), UNIQUE |  | Employee (EMP-NNNNNN) this row belongs to. |
| `final_status` | text | no |  |  | active \| on_notice (resigned, still employed at observation end) \| exited_voluntary \| exited_involuntary. |
| `resignation_date` | date | yes |  |  | Date the resignation was submitted (voluntary exits and on_notice only). |
| `termination_date` | date | yes |  |  | Observed last working day (<= observation_end). The voluntary-attrition target uses this date. |
| `scheduled_termination_date` | date | yes |  |  | Only for on_notice: last working day falling after observation_end. |
| `observation_end` | date | no |  |  | 2026-12-31. |
| `recorded_at` | timestamp with time zone | no |  |  | When the row became known to the system (availability time used by as-of filters). |

**Constraints**

- `attrition_outcome_final_status_check`: `CHECK ((final_status = ANY (ARRAY['active'::text, 'on_notice'::text, 'exited_voluntary'::text, 'exited_involuntary'::text])))`
- `outcome_id_format`: `CHECK ((outcome_id ~ '^OUT-EMP-\d{6}-\d{4}$'::text))`
- `outcome_id_matches_employee`: `CHECK ((substr(outcome_id, 5, 10) = employee_id))`
- `outcome_resign_before_exit`: `CHECK (((resignation_date IS NULL) OR (resignation_date <= COALESCE(termination_date, scheduled_termination_date))))`
- `outcome_resignation_observed`: `CHECK (((resignation_date IS NULL) OR (resignation_date <= observation_end)))`
- `outcome_state_matrix`: `CHECK (COALESCE( CASE final_status     WHEN 'active'::text THEN ((resignation_date IS NULL) AND (termination_date IS NULL) AND (scheduled_termination_date IS NULL))     WHEN 'on_notice'::text THEN ((resignation_date IS NOT NULL) AND (termination_date IS NULL) AND (scheduled_termination_date IS NOT NULL) AND (scheduled_termination_date > observation_end))     WHEN 'exited_voluntary'::text THEN ((resignation_date IS NOT NULL) AND (termination_date IS NOT NULL) AND (scheduled_termination_date IS NULL))     WHEN 'exited_involuntary'::text THEN ((resignation_date IS NULL) AND (termination_date IS NOT NULL) AND (scheduled_termination_date IS NULL))     ELSE NULL::boolean END, false))`
- `outcome_termination_observed`: `CHECK (((termination_date IS NULL) OR (termination_date <= observation_end)))`
- `attrition_outcome_outcome_id_employee_id_key`: `UNIQUE (outcome_id, employee_id)`

## `core.compensation_band`

Synthetic pay bands per level. Kept for completeness; never used as a predictor.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `band_id` | text | no | PK |  | BAND-NN, NN = level. |
| `level` | smallint | no | UNIQUE |  | Career level 1 (entry) to 6 (senior leader). |
| `currency` | text | no |  | 'SYN'::text | Always SYN (synthetic units; no real currency). |
| `min_amount` | numeric(12,2) | no |  |  | Annual band minimum, SYN units. |
| `mid_amount` | numeric(12,2) | no |  |  | Annual band midpoint, SYN units. |
| `max_amount` | numeric(12,2) | no |  |  | Annual band maximum, SYN units. |

**Constraints**

- `band_id_format`: `CHECK ((band_id ~ '^BAND-\d{2}$'::text))`
- `band_ordering`: `CHECK (((min_amount < mid_amount) AND (mid_amount < max_amount)))`
- `compensation_band_level_check`: `CHECK (((level >= 1) AND (level <= 6)))`
- `compensation_band_min_amount_check`: `CHECK ((min_amount > (0)::numeric))`

## `core.employee`

Immutable identity and hire date only. The dataset contains no names, birth dates, gender, ethnicity, marital status, location or nationality. Documented exception: a restricted sick-leave ledger exists at the user's request; it is never a feature, hazard input or manager-visible value.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `employee_id` | text | no | PK |  | EMP-NNNNNN, zero-padded, immutable. |
| `hire_date` | date | no |  |  | First day of employment (single employment spell per person). |
| `recorded_at` | timestamp with time zone | no |  |  | When the record became known to the system (availability time). |

**Constraints**

- `employee_id_format`: `CHECK ((employee_id ~ '^EMP-\d{6}$'::text))`

## `core.employment_event`

Employment lifecycle events. Status is derived from these; Employee is never updated.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `event_id` | text | no | PK |  | EEV-YYYY-NNNNNN, YYYY = event year. |
| `employee_id` | text | no | FK core.employee(employee_id), UNIQUE |  | Employee (EMP-NNNNNN) this row belongs to. |
| `event_type` | text | no | UNIQUE |  | hire \| resignation_submitted \| exit. At most one of each per employee. |
| `event_date` | date | no |  |  | Date the event took effect. For exit: last working day. |
| `exit_type` | text | yes |  |  | voluntary \| involuntary; only on exit events. |
| `notice_days` | smallint | yes |  |  | Notice period in calendar days; only on resignation events (synthetic config, 30-90). |
| `recorded_at` | timestamp with time zone | no |  |  | When the event was recorded (>= event_date). |

**Constraints**

- `employment_event_event_type_check`: `CHECK ((event_type = ANY (ARRAY['hire'::text, 'resignation_submitted'::text, 'exit'::text])))`
- `employment_event_exit_type_check`: `CHECK ((exit_type = ANY (ARRAY['voluntary'::text, 'involuntary'::text])))`
- `employment_event_notice_days_check`: `CHECK (((notice_days >= 0) AND (notice_days <= 120)))`
- `event_exit_type_rule`: `CHECK (((event_type = 'exit'::text) = (exit_type IS NOT NULL)))`
- `event_id_format`: `CHECK ((event_id ~ '^EEV-\d{4}-\d{6}$'::text))`
- `event_not_backdated`: `CHECK (((recorded_at)::date >= event_date))`
- `event_notice_rule`: `CHECK (((event_type = 'resignation_submitted'::text) = (notice_days IS NOT NULL)))`
- `employment_event_employee_id_event_type_key`: `UNIQUE (employee_id, event_type)`

## `core.engagement_survey`

Quarterly pulse survey: one row per employee per wave, including non-responses. Missing answers are never imputed.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `survey_id` | text | no | PK |  | SURV-YYYY-NNNNNN, YYYY = wave year. |
| `employee_id` | text | no | FK core.employee(employee_id), UNIQUE |  | Employee (EMP-NNNNNN) this row belongs to. |
| `wave` | text | no | UNIQUE |  | YYYYQn. Sent in the 3rd month of the quarter. |
| `sent_date` | date | no |  |  | Date the survey wave was sent (15th of the quarter's last month). |
| `response_status` | text | no |  |  | complete (5 items) \| partial (1-4 items) \| no_response (0 items). |
| `q1` | smallint | yes |  |  | Q1 "I feel valued for my work" (1 strongly disagree - 5 strongly agree). |
| `q2` | smallint | yes |  |  | Q2 "My workload is manageable" (1-5). |
| `q3` | smallint | yes |  |  | Q3 "I see a future for myself here" (1-5). |
| `q4` | smallint | yes |  |  | Q4 "I get the support I need from my manager" (1-5). |
| `q5` | smallint | yes |  |  | Q5 "I would recommend this team as a place to work" (1-5). |
| `items_answered` | smallint | no |  |  | Count of non-null q1-q5. |
| `engagement_index` | numeric(3,2) | yes |  |  | Mean of answered items, 2 decimals; NULL when fewer than 3 answered. |
| `submitted_at` | timestamp with time zone | yes |  |  | When the employee submitted answers; NULL for no_response. |
| `recorded_at` | timestamp with time zone | no |  |  | When the row became known to the system (availability time used by as-of filters). |

**Constraints**

- `engagement_survey_q1_check`: `CHECK (((q1 >= 1) AND (q1 <= 5)))`
- `engagement_survey_q2_check`: `CHECK (((q2 >= 1) AND (q2 <= 5)))`
- `engagement_survey_q3_check`: `CHECK (((q3 >= 1) AND (q3 <= 5)))`
- `engagement_survey_q4_check`: `CHECK (((q4 >= 1) AND (q4 <= 5)))`
- `engagement_survey_q5_check`: `CHECK (((q5 >= 1) AND (q5 <= 5)))`
- `engagement_survey_response_status_check`: `CHECK ((response_status = ANY (ARRAY['complete'::text, 'partial'::text, 'no_response'::text])))`
- `engagement_survey_wave_check`: `CHECK ((wave ~ '^\d{4}Q[1-4]$'::text))`
- `survey_chronology`: `CHECK (((submitted_at IS NULL) OR (((submitted_at)::date >= sent_date) AND (recorded_at >= submitted_at))))`
- `survey_id_format`: `CHECK ((survey_id ~ '^SURV-\d{4}-\d{6}$'::text))`
- `survey_index_mean`: `CHECK ( CASE     WHEN (engagement_index IS NULL) THEN true     WHEN (items_answered = 0) THEN false     ELSE (engagement_index = round(((((((COALESCE((q1)::integer, 0) + COALESCE((q2)::integer, 0)) + COALESCE((q3)::integer, 0)) + COALESCE((q4)::integer, 0)) + COALESCE((q5)::integer, 0)))::numeric / (items_answered)::numeric), 2)) END)`
- `survey_index_presence`: `CHECK (((items_answered >= 3) = (engagement_index IS NOT NULL)))`
- `survey_item_count`: `CHECK ((items_answered = num_nonnulls(q1, q2, q3, q4, q5)))`
- `survey_status_rule`: `CHECK ( CASE response_status     WHEN 'complete'::text THEN (items_answered = 5)     WHEN 'partial'::text THEN ((items_answered >= 1) AND (items_answered <= 4))     ELSE (items_answered = 0) END)`
- `survey_submit_rule`: `CHECK (((response_status = 'no_response'::text) = (submitted_at IS NULL)))`
- `engagement_survey_employee_id_wave_key`: `UNIQUE (employee_id, wave)`

## `core.leave_ledger`

Leave transactions. Policy: EL +1 per employed month; SL +3 on 1 Jan or on hire date; all unused EL and SL lapse on 31 Dec. Balance per type resets to 0 each year and never goes negative (validator). SL rows are restricted: never used as features or shown to managers.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `leave_txn_id` | text | no | PK |  | LVT-YYYY-NNNNNNN. |
| `employee_id` | text | no | FK core.employee(employee_id) |  | Employee (EMP-NNNNNN) this row belongs to. |
| `leave_type` | text | no |  |  | EL (earned leave) \| SL (sick leave, restricted). |
| `txn_type` | text | no |  |  | accrual (EL +1) \| grant (SL +3) \| taken (negative) \| lapse (negative, 31 Dec only). |
| `days` | smallint | no |  |  | Signed whole days: positive credits, negative debits. |
| `txn_date` | date | no |  |  | Date of the transaction (for taken: first day of the leave block). |
| `leave_year` | smallint | no |  |  | Calendar leave year (= year of txn_date). |
| `recorded_at` | timestamp with time zone | no |  |  | When the row became known to the system (availability time used by as-of filters). |

**Constraints**

- `leave_accrual_rule`: `CHECK (((txn_type <> 'accrual'::text) OR ((leave_type = 'EL'::text) AND (days = 1))))`
- `leave_grant_rule`: `CHECK (((txn_type <> 'grant'::text) OR ((leave_type = 'SL'::text) AND (days = 3))))`
- `leave_lapse_dec31`: `CHECK (((txn_type <> 'lapse'::text) OR ((EXTRACT(month FROM txn_date) = (12)::numeric) AND (EXTRACT(day FROM txn_date) = (31)::numeric))))`
- `leave_ledger_days_check`: `CHECK ((days <> 0))`
- `leave_ledger_leave_type_check`: `CHECK ((leave_type = ANY (ARRAY['EL'::text, 'SL'::text])))`
- `leave_ledger_txn_type_check`: `CHECK ((txn_type = ANY (ARRAY['accrual'::text, 'grant'::text, 'taken'::text, 'lapse'::text])))`
- `leave_not_backdated`: `CHECK (((recorded_at)::date >= txn_date))`
- `leave_sign_rule`: `CHECK (((txn_type = ANY (ARRAY['taken'::text, 'lapse'::text])) = (days < 0)))`
- `leave_txn_id_format`: `CHECK ((leave_txn_id ~ '^LVT-\d{4}-\d{7}$'::text))`
- `leave_year_matches`: `CHECK (((leave_year)::numeric = EXTRACT(year FROM txn_date)))`

## `core.promotion_history`

Level increases. Each promotion starts exactly one assignment.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `promotion_id` | text | no | PK |  | PROMO-YYYY-NNNNNN. |
| `employee_id` | text | no | FK core.assignment(assignment_id, employee_id), FK core.employee(employee_id) |  | Employee (EMP-NNNNNN) this row belongs to. |
| `from_role_id` | text | no | FK core.role(role_id) |  | Role held before the promotion. |
| `to_role_id` | text | no | FK core.role(role_id) |  | Role held after the promotion (higher level). |
| `assignment_id` | text | no | FK core.assignment(assignment_id, employee_id), UNIQUE |  | The assignment the promotion starts (same employee, same date). |
| `effective_date` | date | no |  |  | Equals the started assignment's effective_from (validator rule). |
| `recorded_at` | timestamp with time zone | no |  |  | When the row became known to the system (availability time used by as-of filters). |

**Constraints**

- `promotion_changes_role`: `CHECK ((from_role_id <> to_role_id))`
- `promotion_id_format`: `CHECK ((promotion_id ~ '^PROMO-\d{4}-\d{6}$'::text))`
- `promotion_assignment_same_employee`: `FOREIGN KEY (assignment_id, employee_id) REFERENCES core.assignment(assignment_id, employee_id)`

## `core.role`

Job definitions: one per job family x level x (individual contributor \| people manager).

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `role_id` | text | no | PK |  | ROLE-NNNN. |
| `role_title` | text | no |  |  | Display title, e.g. "Engineering IC L3". |
| `job_family` | text | no | UNIQUE |  | engineering \| data \| product \| design \| operations \| support \| sales \| finance_admin. |
| `level` | smallint | no | UNIQUE |  | Career level 1-6. Individual contributors use 1-5, people managers 4-6. |
| `band_id` | text | no | FK core.compensation_band(band_id) |  | FK to compensation_band for this level. |
| `is_people_manager` | boolean | no | UNIQUE |  | True for roles that lead a team. |

**Constraints**

- `role_id_format`: `CHECK ((role_id ~ '^ROLE-\d{4}$'::text))`
- `role_job_family_check`: `CHECK ((job_family = ANY (ARRAY['engineering'::text, 'data'::text, 'product'::text, 'design'::text, 'operations'::text, 'support'::text, 'sales'::text, 'finance_admin'::text])))`
- `role_level_check`: `CHECK (((level >= 1) AND (level <= 6)))`
- `role_job_family_level_is_people_manager_key`: `UNIQUE (job_family, level, is_people_manager)`

## `core.team`

Organisational teams. Names are generic ("Engineering 014").

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `team_id` | text | no | PK |  | TEAM-NNNN. |
| `team_name` | text | no | UNIQUE |  | Generic display name, unique. |
| `job_family` | text | no |  |  | Primary job family of the team. |
| `created_date` | date | no |  |  | Date the team was formed. |

**Constraints**

- `team_id_format`: `CHECK ((team_id ~ '^TEAM-\d{4}$'::text))`

## `core.tenure_snapshot`

Quarter-end tenure facts, derived from employment events, assignments and promotions (never generated independently).

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `snapshot_id` | text | no | PK |  | SNAP-<employee-id>-YYYYQn. |
| `employee_id` | text | no | FK core.assignment(assignment_id, employee_id), FK core.employee(employee_id), UNIQUE |  | Employee (EMP-NNNNNN) this row belongs to. |
| `as_of_date` | date | no | UNIQUE |  | Quarter-end date. |
| `assignment_id` | text | no | FK core.assignment(assignment_id, employee_id) |  | Assignment in force on as_of_date. |
| `tenure_months` | numeric(6,2) | no |  |  | Months since hire_date (days / 30.4375). |
| `months_in_role` | numeric(6,2) | no |  |  | Months since the current role started. |
| `months_since_promotion` | numeric(6,2) | yes |  |  | Months since the latest promotion; NULL if never promoted in this company. |
| `derived_from` | jsonb | no |  |  | Lineage: IDs of the source events, assignments and promotions. |
| `recorded_at` | timestamp with time zone | no |  |  | When the row became known to the system (availability time used by as-of filters). |

**Constraints**

- `snapshot_id_format`: `CHECK ((snapshot_id ~ '^SNAP-EMP-\d{6}-\d{4}Q[1-4]$'::text))`
- `snapshot_id_matches_employee`: `CHECK ((substr(snapshot_id, 6, 10) = employee_id))`
- `snapshot_id_matches_quarter`: `CHECK ((substr(snapshot_id, 17) = to_char((as_of_date)::timestamp with time zone, 'YYYY"Q"Q'::text)))`
- `snapshot_on_quarter_end`: `CHECK ((as_of_date = core.quarter_end(as_of_date)))`
- `tenure_snapshot_months_in_role_check`: `CHECK ((months_in_role >= (0)::numeric))`
- `tenure_snapshot_months_since_promotion_check`: `CHECK ((months_since_promotion >= (0)::numeric))`
- `tenure_snapshot_tenure_months_check`: `CHECK ((tenure_months >= (0)::numeric))`
- `snapshot_assignment_same_employee`: `FOREIGN KEY (assignment_id, employee_id) REFERENCES core.assignment(assignment_id, employee_id)`
- `tenure_snapshot_employee_id_as_of_date_key`: `UNIQUE (employee_id, as_of_date)`

## `core.workload_snapshot`

Monthly workload per employee. actual_hours = intensity x available capacity, so leave lowers both hours and capacity and does not change utilisation.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `workload_id` | text | no | PK |  | WLS-<employee-id>-YYYY-MM. |
| `employee_id` | text | no | FK core.employee(employee_id), UNIQUE |  | Employee (EMP-NNNNNN) this row belongs to. |
| `period_month` | date | no | UNIQUE |  | First day of the month. |
| `scheduled_workdays` | smallint | no |  |  | Mon-Fri working days in the month while employed, minus company holidays. |
| `available_hours` | numeric(5,1) | no |  |  | (scheduled_workdays - leave days taken) x 8 hours. |
| `actual_hours` | numeric(5,1) | no |  |  | Hours worked in the month. |
| `utilization_pct` | numeric(5,1) | yes |  |  | actual_hours / available_hours x 100, rounded to 0.1 from the stored values; NULL when available_hours = 0. |
| `after_hours_pct` | numeric(5,1) | no |  |  | Share of hours worked outside 09:00-18:00, percent. |
| `oncall_days` | smallint | no |  |  | Days on call in the month. |
| `recorded_at` | timestamp with time zone | no |  |  | When the row became known to the system (availability time used by as-of filters). |

**Constraints**

- `workload_capacity_bound`: `CHECK ((available_hours <= ((scheduled_workdays * 8))::numeric))`
- `workload_id_format`: `CHECK ((workload_id ~ '^WLS-EMP-\d{6}-\d{4}-\d{2}$'::text))`
- `workload_id_matches_employee`: `CHECK ((substr(workload_id, 5, 10) = employee_id))`
- `workload_id_matches_month`: `CHECK ((substr(workload_id, 16, 7) = to_char((period_month)::timestamp with time zone, 'YYYY-MM'::text)))`
- `workload_snapshot_actual_hours_check`: `CHECK (((actual_hours >= (0)::numeric) AND (actual_hours <= (400)::numeric)))`
- `workload_snapshot_after_hours_pct_check`: `CHECK (((after_hours_pct >= (0)::numeric) AND (after_hours_pct <= (100)::numeric)))`
- `workload_snapshot_available_hours_check`: `CHECK ((available_hours >= (0)::numeric))`
- `workload_snapshot_oncall_days_check`: `CHECK (((oncall_days >= 0) AND (oncall_days <= 31)))`
- `workload_snapshot_period_month_check`: `CHECK ((EXTRACT(day FROM period_month) = (1)::numeric))`
- `workload_snapshot_scheduled_workdays_check`: `CHECK (((scheduled_workdays >= 0) AND (scheduled_workdays <= 23)))`
- `workload_snapshot_utilization_pct_check`: `CHECK (((utilization_pct >= (0)::numeric) AND (utilization_pct <= (250)::numeric)))`
- `workload_utilization_arith`: `CHECK ( CASE     WHEN (utilization_pct IS NULL) THEN true     WHEN (available_hours = (0)::numeric) THEN false     ELSE (abs((utilization_pct - round(((actual_hours * (100)::numeric) / available_hours), 1))) <= 0.05) END)`
- `workload_zero_capacity_hours`: `CHECK (((available_hours <> (0)::numeric) OR (actual_hours = (0)::numeric)))`
- `workload_zero_capacity_rule`: `CHECK (((available_hours = (0)::numeric) = (utilization_pct IS NULL)))`
- `workload_snapshot_employee_id_period_month_key`: `UNIQUE (employee_id, period_month)`

## `core.assignment_interval` (view)

FINAL-HISTORY view: assignments with derived exclusive end = earlier of next assignment start and day after termination; NULL if current. Not as-of safe; never used for features.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `assignment_id` | text | yes |  |  | ASGN-YYYY-NNNNNN, YYYY = effective_from year. |
| `employee_id` | text | yes |  |  | Employee (EMP-NNNNNN) this row belongs to. |
| `team_id` | text | yes |  |  | Team for this interval. |
| `role_id` | text | yes |  |  | Role for this interval. |
| `effective_from` | date | yes |  |  | First day of this team/role assignment. |
| `change_reason` | text | yes |  |  | hire \| transfer (team change) \| promotion (level up) \| lateral_move (role change, same level) \| reorg. |
| `previous_assignment_id` | text | yes |  |  | The assignment this one replaces (same employee). NULL only for the hire assignment. |
| `recorded_at` | timestamp with time zone | yes |  |  | When the row became known to the system (availability time used by as-of filters). |
| `effective_to` | date | yes |  |  | Derived exclusive end: earlier of the next assignment start and the day after termination; NULL if current. |

## `core.earned_leave_ledger` (view)

EL rows only. The app role reads this view; it has no access to SL rows.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `leave_txn_id` | text | yes |  |  | LVT-YYYY-NNNNNNN. |
| `employee_id` | text | yes |  |  | Employee (EMP-NNNNNN) this row belongs to. |
| `txn_type` | text | yes |  |  | accrual (EL +1) \| grant (SL +3) \| taken (negative) \| lapse (negative, 31 Dec only). |
| `days` | smallint | yes |  |  | Signed whole days: positive credits, negative debits. |
| `txn_date` | date | yes |  |  | Date of the transaction (for taken: first day of the leave block). |
| `leave_year` | smallint | yes |  |  | Calendar leave year (= year of txn_date). |
| `recorded_at` | timestamp with time zone | yes |  |  | When the row became known to the system (availability time used by as-of filters). |

## `app.app_user`

People who use the app. Employees are data subjects, not users. A manager's scope is the team of their current assignment.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `user_id` | text | no | PK |  | USR-NNNN. |
| `username` | text | no | UNIQUE |  | Login name, unique. |
| `display_name` | text | no |  |  | Name shown in the UI (persona, e.g. "HR Partner 01"). |
| `app_role` | text | no |  |  | manager \| hr_partner \| it_ops \| admin. |
| `employee_id` | text | yes | FK core.employee(employee_id) |  | Employee record of a manager user (required for managers; scope = team of their current assignment). |
| `is_active` | boolean | no |  | true | Inactive users cannot log in. |

**Constraints**

- `app_user_app_role_check`: `CHECK ((app_role = ANY (ARRAY['manager'::text, 'hr_partner'::text, 'it_ops'::text, 'admin'::text])))`
- `manager_has_employee`: `CHECK (((app_role <> 'manager'::text) OR (employee_id IS NOT NULL)))`
- `user_id_format`: `CHECK ((user_id ~ '^USR-\d{4}$'::text))`

## `app.exception_log`

Detected data issues and blocked actions (metadata only; raw bad payloads stay in the restricted quarantine folder).

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `exception_id` | text | no | PK |  | EXC-YYYY-NNNNNN. |
| `entity` | text | no |  |  | Table, file or API action the issue concerns. |
| `record_id` | text | no |  |  | Business ID of the affected record or request. |
| `issue_type` | text | no |  |  | Coded issue; one of the listed values. |
| `detector` | text | no |  |  | Rule or component that detected it (e.g. validator.survey_null_consistency, api.review_gate). |
| `detail` | jsonb | no |  | '{}'::jsonb | Structured detail (rule version, HTTP status, field). No restricted values. |
| `detected_at` | timestamp with time zone | no |  |  | When it was detected. |
| `status` | text | no |  |  | open \| acknowledged \| resolved. |

**Constraints**

- `exception_id_format`: `CHECK ((exception_id ~ '^EXC-\d{4}-\d{6}$'::text))`
- `exception_log_issue_type_check`: `CHECK ((issue_type = ANY (ARRAY['FUTURE_LEAK'::text, 'MISSING_AS_NEGATIVE'::text, 'LABEL_DATE_MISMATCH'::text, 'UNREVIEWED_SCORE'::text, 'NOT_AUTHORIZED'::text, 'STALE_SCORE_VERSION'::text, 'NOT_ELIGIBLE'::text, 'CONFLICTING_INTERVENTION'::text, 'CONTROL_ARM_BLOCKED'::text, 'QUARANTINED_ROW'::text, 'INSUFFICIENT_HISTORY'::text, 'UNRESOLVED_FOLLOWUP'::text, 'OTHER'::text])))`
- `exception_log_status_check`: `CHECK ((status = ANY (ARRAY['open'::text, 'acknowledged'::text, 'resolved'::text])))`

## `app.intervention`

Supportive action for an approved case. arm is assigned before treatment (80/20); controls never receive treatment. The service layer also checks approval, score version, team scope, eligibility and conflicts.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `intervention_id` | text | no | PK |  | INT-YYYY-NNNNNN. |
| `employee_id` | text | no | FK app.score(score_id, employee_id) |  | Employee (EMP-NNNNNN) this row belongs to. |
| `score_id` | text | no | FK app.score(score_id, employee_id) |  | Approved score that justified the intervention (same employee). |
| `review_id` | text | no | FK app.review_decision(review_id) |  | Approved HR review (service checks decision, version, scope, chronology). |
| `intervention_type` | text | no |  |  | checkin \| workload_rebalance \| career_conversation \| internal_mobility. |
| `owner_user_id` | text | no | FK app.app_user(user_id) |  | User responsible for carrying it out. |
| `arm` | text | no |  |  | treatment \| control (seeded 80/20, assigned before treatment; controls are never treated). |
| `assigned_at` | timestamp with time zone | no |  |  | When the arm was assigned. |
| `source` | text | no |  |  | simulated (generator) \| app (created through the UI). |
| `created_at` | timestamp with time zone | no |  |  | When the record was created. |

**Constraints**

- `intervention_arm_check`: `CHECK ((arm = ANY (ARRAY['treatment'::text, 'control'::text])))`
- `intervention_created_after_assignment`: `CHECK ((created_at >= assigned_at))`
- `intervention_id_format`: `CHECK ((intervention_id ~ '^INT-\d{4}-\d{6}$'::text))`
- `intervention_intervention_type_check`: `CHECK ((intervention_type = ANY (ARRAY['checkin'::text, 'workload_rebalance'::text, 'career_conversation'::text, 'internal_mobility'::text])))`
- `intervention_source_check`: `CHECK ((source = ANY (ARRAY['simulated'::text, 'app'::text])))`
- `intervention_score_same_employee`: `FOREIGN KEY (score_id, employee_id) REFERENCES app.score(score_id, employee_id)`

## `app.intervention_event`

Append-only status history. Current status = latest event.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `event_id` | text | no | PK |  | IEV-YYYY-NNNNNNN. |
| `intervention_id` | text | no | FK app.intervention(intervention_id) |  | Intervention whose status changed. |
| `from_status` | text | yes |  |  | Previous status; NULL for the first event. |
| `to_status` | text | no |  |  | proposed \| scheduled \| completed \| declined \| cancelled. |
| `actor_user_id` | text | no | FK app.app_user(user_id) |  | User who made the change (from the session). |
| `event_at` | timestamp with time zone | no |  |  | When the change happened. |
| `recorded_at` | timestamp with time zone | no |  |  | When the row became known to the system (availability time used by as-of filters). |
| `note` | text | yes |  |  | Optional note. |

**Constraints**

- `intervention_event_changes_status`: `CHECK ((from_status IS DISTINCT FROM to_status))`
- `intervention_event_chronology`: `CHECK ((recorded_at >= event_at))`
- `intervention_event_from_status_check`: `CHECK ((from_status = ANY (ARRAY['proposed'::text, 'scheduled'::text, 'completed'::text, 'declined'::text, 'cancelled'::text])))`
- `intervention_event_id_format`: `CHECK ((event_id ~ '^IEV-\d{4}-\d{7}$'::text))`
- `intervention_event_to_status_check`: `CHECK ((to_status = ANY (ARRAY['proposed'::text, 'scheduled'::text, 'completed'::text, 'declined'::text, 'cancelled'::text])))`

## `app.review_decision`

HR review of a score. Nothing reaches a manager without an approved review.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `review_id` | text | no | PK |  | REV-YYYY-NNNNNN. |
| `score_id` | text | no | FK app.score(score_id) |  | Score (and therefore score version) being reviewed. |
| `reviewer_user_id` | text | no | FK app.app_user(user_id) |  | HR partner who decided (taken from the session, never the request body). |
| `decision` | text | no |  |  | approved \| dismissed \| needs_info. |
| `reason_code` | text | no |  |  | Coded reason for the decision. |
| `comment` | text | yes |  |  | Optional free-text note. |
| `decided_at` | timestamp with time zone | no |  |  | When the decision was made. |
| `recorded_at` | timestamp with time zone | no |  |  | When the decision was stored. |

**Constraints**

- `review_decision_decision_check`: `CHECK ((decision = ANY (ARRAY['approved'::text, 'dismissed'::text, 'needs_info'::text])))`
- `review_id_format`: `CHECK ((review_id ~ '^REV-\d{4}-\d{6}$'::text))`
- `review_recorded_after_decision`: `CHECK ((recorded_at >= decided_at))`

## `app.score`

Per-employee result of a score run. A score is a suggestion to check in, never an employment decision.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `score_id` | text | no | PK, UNIQUE |  | SCR-YYYY-NNNNNNN. |
| `score_run_id` | text | no | FK app.score_run(score_run_id), UNIQUE |  | Run that produced the score (fixes the score version). |
| `employee_id` | text | no | FK core.employee(employee_id), UNIQUE |  | Employee (EMP-NNNNNN) this row belongs to. |
| `risk_score` | numeric(6,5) | no |  |  | Estimated probability of voluntary exit by the horizon end, 0-1. |
| `rank_in_run` | integer | no | UNIQUE |  | 1 = highest score in the run. |
| `priority_band` | text | no |  |  | suggested_checkin \| monitor \| none (thresholds versioned in config). |
| `contributions` | jsonb | no |  |  | [{feature, value, reference, log_odds}] plus intercept. Log-odds, not probabilities or causal effects. |
| `coverage` | jsonb | no |  |  | Data-coverage notes shown to reviewers (e.g. no survey response last wave). Never weighted. |
| `source_refs` | jsonb | no |  |  | [{table, id, recorded_at}] for every source row used. |

**Constraints**

- `score_id_format`: `CHECK ((score_id ~ '^SCR-\d{4}-\d{7}$'::text))`
- `score_priority_band_check`: `CHECK ((priority_band = ANY (ARRAY['suggested_checkin'::text, 'monitor'::text, 'none'::text])))`
- `score_rank_in_run_check`: `CHECK ((rank_in_run >= 1))`
- `score_risk_score_check`: `CHECK (((risk_score >= (0)::numeric) AND (risk_score <= (1)::numeric)))`
- `score_score_id_employee_id_key`: `UNIQUE (score_id, employee_id)`
- `score_score_run_id_employee_id_key`: `UNIQUE (score_run_id, employee_id)`
- `score_score_run_id_rank_in_run_key`: `UNIQUE (score_run_id, rank_in_run)`

## `app.score_run`

One scoring run: a frozen scorer applied at one as-of date.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `score_run_id` | text | no | PK |  | RUN-YYYY-NNNN. |
| `as_of_date` | date | no |  |  | Quarter-end the run scores (inputs recorded on or before this date only). |
| `scorer_type` | text | no |  |  | scorecard (points rules) \| logistic_regression. |
| `model_version` | text | no |  |  | Version label of the frozen scorer. |
| `config_hash` | text | no |  |  | SHA-256 of the scorer configuration and coefficients. |
| `model_frozen_at` | timestamp with time zone | no |  |  | When the scorer was frozen (after validation labels became available). |
| `executed_at` | timestamp with time zone | no |  |  | When the run executed (>= model_frozen_at). |

**Constraints**

- `score_run_after_freeze`: `CHECK ((executed_at >= model_frozen_at))`
- `score_run_id_format`: `CHECK ((score_run_id ~ '^RUN-\d{4}-\d{4}$'::text))`
- `score_run_scorer_type_check`: `CHECK ((scorer_type = ANY (ARRAY['scorecard'::text, 'logistic_regression'::text])))`

## `app.user_credential`

argon2 password hashes, kept apart from user profiles.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `user_id` | text | no | FK app.app_user(user_id), PK |  | User the credential belongs to. |
| `password_hash` | text | no |  |  | argon2 hash (starts with $argon2). Plain passwords are never stored. |
| `updated_at` | timestamp with time zone | no |  |  | When the password was last set. |

**Constraints**

- `user_credential_password_hash_check`: `CHECK ((password_hash ~~ '$argon2%'::text))`

## `app.user_session`

Server-side sessions. session_id stores the SHA-256 of the cookie token, never the token itself.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `session_id` | text | no | PK |  | SHA-256 hex of the session cookie token. |
| `user_id` | text | no | FK app.app_user(user_id) |  | Logged-in user. |
| `created_at` | timestamp with time zone | no |  |  | Login time. |
| `expires_at` | timestamp with time zone | no |  |  | Session expiry (fixed lifetime). |
| `revoked_at` | timestamp with time zone | yes |  |  | Logout time; NULL while active. |

**Constraints**

- `session_expiry_after_creation`: `CHECK ((expires_at > created_at))`

## `eval.ground_truth`

Answer key for injected anomalies and clean controls (is_control = true, expected_detection = none). Never used as a feature.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `gt_id` | text | no | PK |  | GT-NNNNNN. |
| `case_id` | text | no |  |  | Injection case identifier (groups the rows of one injected scenario). |
| `entity` | text | no |  |  | Table/file or workflow action the case targets. |
| `record_id` | text | no |  |  | Business ID of the injected or control record. |
| `issue_type` | text | no |  |  | FUTURE_LEAK \| MISSING_AS_NEGATIVE \| LABEL_DATE_MISMATCH \| UNREVIEWED_SCORE (or the control's twin type). |
| `expected_detection` | text | no |  |  | reject_feature \| block_action \| flag_label \| quarantine_row \| none (controls). |
| `is_control` | boolean | no |  |  | True for clean near-valid controls that must NOT be flagged. |
| `rule_version` | text | no |  |  | Version of the injection rule set. |

**Constraints**

- `ground_truth_expected_detection_check`: `CHECK ((expected_detection = ANY (ARRAY['reject_feature'::text, 'block_action'::text, 'flag_label'::text, 'quarantine_row'::text, 'none'::text])))`
- `gt_id_format`: `CHECK ((gt_id ~ '^GT-\d{6}$'::text))`

## `eval.horizon_label`

Target per employee and quarter-end: voluntary termination on or before the second following quarter-end. Evaluation-only.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `employee_id` | text | no | FK core.employee(employee_id), FK core.attrition_outcome(outcome_id, employee_id), PK |  | Employee (EMP-NNNNNN) this row belongs to. |
| `as_of_date` | date | no | PK |  | Scoring quarter-end. |
| `horizon_end` | date | no |  |  | Second quarter-end after as_of_date. |
| `label_status` | text | no |  |  | voluntary_exit (1) \| employed_through_horizon (0, incl. resigned after as_of but still serving notice) \| competing_exit (0, involuntary) \| unresolved (horizon beyond observation; excluded) \| ineligible (excluded). |
| `ineligible_reason` | text | yes |  |  | resignation_known \| insufficient_history \| not_employed; only for ineligible. |
| `target` | smallint | yes |  |  | 1 voluntary_exit; 0 employed_through_horizon or competing_exit; NULL otherwise. |
| `outcome_id` | text | no | FK core.attrition_outcome(outcome_id, employee_id) |  | Terminal outcome of the same employee. |

**Constraints**

- `horizon_label_ineligible_reason_check`: `CHECK ((ineligible_reason = ANY (ARRAY['resignation_known'::text, 'insufficient_history'::text, 'not_employed'::text])))`
- `horizon_label_label_status_check`: `CHECK ((label_status = ANY (ARRAY['voluntary_exit'::text, 'employed_through_horizon'::text, 'competing_exit'::text, 'unresolved'::text, 'ineligible'::text])))`
- `horizon_label_target_check`: `CHECK ((target = ANY (ARRAY[0, 1])))`
- `label_horizon_two_quarters`: `CHECK ((horizon_end = core.quarter_end(((as_of_date + '6 mons'::interval))::date)))`
- `label_ineligible_reason`: `CHECK (((label_status = 'ineligible'::text) = (ineligible_reason IS NOT NULL)))`
- `label_on_quarter_end`: `CHECK ((as_of_date = core.quarter_end(as_of_date)))`
- `label_target_rule`: `CHECK (COALESCE( CASE label_status     WHEN 'voluntary_exit'::text THEN (target = 1)     WHEN 'employed_through_horizon'::text THEN (target = 0)     WHEN 'competing_exit'::text THEN (target = 0)     ELSE (target IS NULL) END, false))`
- `label_outcome_same_employee`: `FOREIGN KEY (outcome_id, employee_id) REFERENCES core.attrition_outcome(outcome_id, employee_id)`

## `eval.potential_outcome`

For intervention-eligible cases: voluntary exit by horizon end if treated / if not treated, from common random draws. y_if_untreated equals the recorded history. Evaluation-only.

| Column | Type | Null | Keys | Default | Description |
|---|---|---|---|---|---|
| `employee_id` | text | no | FK core.employee(employee_id), PK |  | Employee (EMP-NNNNNN) this row belongs to. |
| `as_of_date` | date | no | PK |  | Scoring date of the intervention list. |
| `y_if_treated` | smallint | no |  |  | 1 if the person would exit voluntarily by the horizon end when treated. |
| `y_if_untreated` | smallint | no |  |  | 1 if the person would exit voluntarily by the horizon end when untreated (= recorded history). |

**Constraints**

- `potential_outcome_y_if_treated_check`: `CHECK ((y_if_treated = ANY (ARRAY[0, 1])))`
- `potential_outcome_y_if_untreated_check`: `CHECK ((y_if_untreated = ANY (ARRAY[0, 1])))`

