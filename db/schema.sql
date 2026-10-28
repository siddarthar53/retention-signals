-- =====================================================================================
-- Retention Signals — database schema v0.6.2
-- PostgreSQL 15+ (tested on 16). Load into an EMPTY database:  python scripts/db_reset.py
--
-- v0.6 (build start, 2026-10-27):
--  * Single timeline (approved scope cut): no baseline/intervention re-simulation and no branch_id.
--    The recorded history is the untreated world; interventions carry simulated outcomes taken
--    from evaluation-only potential outcomes (eval.potential_outcome).
--  * Assignment closure: no effective_to column. An assignment ends where the next one for the
--    same employee starts, or at termination, whichever is earlier. An open interval therefore ends
--    without UPDATE; for chain-validated, chronological source data, intervals cannot overlap.
--    core.assignment_interval is a final-history view; features reconstruct as-of in code.
--  * v0.6.2: utilisation is generated first (0.1 steps) and actual_hours stored exactly (numeric(7,3)),
--    so leave (incl. sick leave) cannot change utilisation through rounding (ChatGPT G2 design point 3).
--  * v0.6.1 (ChatGPT review A1-A3): label target can no longer be NULL for completed statuses;
--    no self-referencing / multiple hire-root assignments; column-level grants hide capacity,
--    actual hours and raw survey items from rs_app; API uses a dedicated login role.
--  * Outcome state matrix can no longer evaluate to NULL (on_notice requires a scheduled date).
--  * Two database roles: rs_app (API) and rs_eval (evaluation). rs_app cannot read the eval schema,
--    attrition outcomes, sick-leave rows or compensation. Pipeline jobs run as the schema owner and
--    enforce as-of filtering in code (tested in tests/pipeline).
--  * Auth tables: app.user_credential (argon2 hash) and app.user_session.
--  * Every tested constraint is named, so tests assert the exact constraint that fired.
-- Column meanings, units and allowed values are recorded as COMMENTs and rendered into
-- docs/data_dictionary.md by scripts/gen_dictionary.py.
-- =====================================================================================

CREATE SCHEMA core;   -- master data and recorded history (source of truth)
CREATE SCHEMA app;    -- users, scores, reviews, interventions, exceptions
CREATE SCHEMA eval;   -- labels, potential outcomes, ground truth (restricted)

COMMENT ON SCHEMA core IS 'Master data and append-only recorded history.';
COMMENT ON SCHEMA app  IS 'Application workflow: users, sessions, scores, reviews, interventions, exceptions.';
COMMENT ON SCHEMA eval IS 'Evaluation-only data: horizon labels, potential outcomes, anomaly ground truth. Not readable by the app role.';

CREATE FUNCTION core.forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only table %.%: % not allowed', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'restrict_violation';
END $$;

CREATE FUNCTION core.quarter_end(d date) RETURNS date LANGUAGE sql IMMUTABLE AS $$
  SELECT (date_trunc('quarter', d) + interval '3 months - 1 day')::date
$$;

-- =====================================================================================
-- MASTER DATA
-- =====================================================================================
CREATE TABLE core.compensation_band (
  band_id        text PRIMARY KEY CONSTRAINT band_id_format CHECK (band_id ~ '^BAND-\d{2}$'),
  level          smallint NOT NULL UNIQUE CHECK (level BETWEEN 1 AND 6),
  currency       text NOT NULL DEFAULT 'SYN',
  min_amount     numeric(12,2) NOT NULL CHECK (min_amount > 0),
  mid_amount     numeric(12,2) NOT NULL,
  max_amount     numeric(12,2) NOT NULL,
  CONSTRAINT band_ordering CHECK (min_amount < mid_amount AND mid_amount < max_amount)
);
COMMENT ON TABLE core.compensation_band IS 'Synthetic pay bands per level. Kept for completeness; never used as a predictor.';
COMMENT ON COLUMN core.compensation_band.band_id    IS 'BAND-NN, NN = level.';
COMMENT ON COLUMN core.compensation_band.level      IS 'Career level 1 (entry) to 6 (senior leader).';
COMMENT ON COLUMN core.compensation_band.currency   IS 'Always SYN (synthetic units; no real currency).';
COMMENT ON COLUMN core.compensation_band.min_amount IS 'Annual band minimum, SYN units.';
COMMENT ON COLUMN core.compensation_band.mid_amount IS 'Annual band midpoint, SYN units.';
COMMENT ON COLUMN core.compensation_band.max_amount IS 'Annual band maximum, SYN units.';

CREATE TABLE core.role (
  role_id            text PRIMARY KEY CONSTRAINT role_id_format CHECK (role_id ~ '^ROLE-\d{4}$'),
  role_title         text NOT NULL,
  job_family         text NOT NULL CHECK (job_family IN
                       ('engineering','data','product','design','operations','support','sales','finance_admin')),
  level              smallint NOT NULL CHECK (level BETWEEN 1 AND 6),
  band_id            text NOT NULL REFERENCES core.compensation_band(band_id),
  is_people_manager  boolean NOT NULL,
  UNIQUE (job_family, level, is_people_manager)
);
COMMENT ON TABLE core.role IS 'Job definitions: one per job family x level x (individual contributor | people manager).';
COMMENT ON COLUMN core.role.role_id           IS 'ROLE-NNNN.';
COMMENT ON COLUMN core.role.role_title        IS 'Display title, e.g. "Engineering IC L3".';
COMMENT ON COLUMN core.role.job_family        IS 'engineering | data | product | design | operations | support | sales | finance_admin.';
COMMENT ON COLUMN core.role.level             IS 'Career level 1-6. Individual contributors use 1-5, people managers 4-6.';
COMMENT ON COLUMN core.role.band_id           IS 'FK to compensation_band for this level.';
COMMENT ON COLUMN core.role.is_people_manager IS 'True for roles that lead a team.';

CREATE TABLE core.team (
  team_id        text PRIMARY KEY CONSTRAINT team_id_format CHECK (team_id ~ '^TEAM-\d{4}$'),
  team_name      text NOT NULL UNIQUE,
  job_family     text NOT NULL,
  created_date   date NOT NULL
);
COMMENT ON TABLE core.team IS 'Organisational teams. Names are generic ("Engineering 014").';
COMMENT ON COLUMN core.team.team_id      IS 'TEAM-NNNN.';
COMMENT ON COLUMN core.team.team_name    IS 'Generic display name, unique.';
COMMENT ON COLUMN core.team.job_family   IS 'Primary job family of the team.';
COMMENT ON COLUMN core.team.created_date IS 'Date the team was formed.';

CREATE TABLE core.employee (
  employee_id  text PRIMARY KEY CONSTRAINT employee_id_format CHECK (employee_id ~ '^EMP-\d{6}$'),
  hire_date    date NOT NULL,
  recorded_at  timestamptz NOT NULL
);
COMMENT ON TABLE core.employee IS 'Immutable identity and hire date only. The dataset contains no names, birth dates, gender, ethnicity, marital status, location or nationality. Documented exception: a restricted sick-leave ledger exists at the user''s request; it is never a feature, hazard input or manager-visible value.';
COMMENT ON COLUMN core.employee.employee_id IS 'EMP-NNNNNN, zero-padded, immutable.';
COMMENT ON COLUMN core.employee.hire_date   IS 'First day of employment (single employment spell per person).';
COMMENT ON COLUMN core.employee.recorded_at IS 'When the record became known to the system (availability time).';

-- =====================================================================================
-- RECORDED HISTORY (append-only)
-- =====================================================================================
CREATE TABLE core.employment_event (
  event_id     text PRIMARY KEY CONSTRAINT event_id_format CHECK (event_id ~ '^EEV-\d{4}-\d{6}$'),
  employee_id  text NOT NULL REFERENCES core.employee(employee_id),
  event_type   text NOT NULL CHECK (event_type IN ('hire','resignation_submitted','exit')),
  event_date   date NOT NULL,
  exit_type    text CHECK (exit_type IN ('voluntary','involuntary')),
  notice_days  smallint CHECK (notice_days BETWEEN 0 AND 120),
  recorded_at  timestamptz NOT NULL,
  CONSTRAINT event_exit_type_rule CHECK ((event_type = 'exit') = (exit_type IS NOT NULL)),
  CONSTRAINT event_notice_rule    CHECK ((event_type = 'resignation_submitted') = (notice_days IS NOT NULL)),
  CONSTRAINT event_not_backdated  CHECK (recorded_at::date >= event_date),
  UNIQUE (employee_id, event_type)
);
COMMENT ON TABLE core.employment_event IS 'Employment lifecycle events. Status is derived from these; Employee is never updated.';
COMMENT ON COLUMN core.employment_event.event_id    IS 'EEV-YYYY-NNNNNN, YYYY = event year.';
COMMENT ON COLUMN core.employment_event.event_type  IS 'hire | resignation_submitted | exit. At most one of each per employee.';
COMMENT ON COLUMN core.employment_event.event_date  IS 'Date the event took effect. For exit: last working day.';
COMMENT ON COLUMN core.employment_event.exit_type   IS 'voluntary | involuntary; only on exit events.';
COMMENT ON COLUMN core.employment_event.notice_days IS 'Notice period in calendar days; only on resignation events (synthetic config, 30-90).';
COMMENT ON COLUMN core.employment_event.recorded_at IS 'When the event was recorded (>= event_date).';

-- Bridge table: Employee N:M Team (and Role) over time. Intervals are derived, never closed by UPDATE.
CREATE TABLE core.assignment (
  assignment_id           text PRIMARY KEY CONSTRAINT assignment_id_format CHECK (assignment_id ~ '^ASGN-\d{4}-\d{6}$'),
  employee_id             text NOT NULL REFERENCES core.employee(employee_id),
  team_id                 text NOT NULL REFERENCES core.team(team_id),
  role_id                 text NOT NULL REFERENCES core.role(role_id),
  effective_from          date NOT NULL,
  change_reason           text NOT NULL CHECK (change_reason IN ('hire','transfer','promotion','lateral_move','reorg')),
  previous_assignment_id  text,
  recorded_at             timestamptz NOT NULL,
  UNIQUE (assignment_id, employee_id),
  CONSTRAINT assignment_one_start_per_day UNIQUE (employee_id, effective_from),
  CONSTRAINT assignment_chain_same_employee FOREIGN KEY (previous_assignment_id, employee_id)
    REFERENCES core.assignment(assignment_id, employee_id),
  CONSTRAINT assignment_first_is_hire CHECK ((change_reason = 'hire') = (previous_assignment_id IS NULL)),
  CONSTRAINT assignment_no_self_reference CHECK (previous_assignment_id IS DISTINCT FROM assignment_id)
);
CREATE UNIQUE INDEX assignment_one_hire_root ON core.assignment (employee_id) WHERE change_reason = 'hire';
COMMENT ON TABLE core.assignment IS 'Employee-team-role history (the N:M bridge). Each row starts an interval; the interval ends where the next row for the employee starts, or at termination (see core.assignment_interval).';
COMMENT ON COLUMN core.assignment.assignment_id          IS 'ASGN-YYYY-NNNNNN, YYYY = effective_from year.';
COMMENT ON COLUMN core.assignment.effective_from         IS 'First day of this team/role assignment.';
COMMENT ON COLUMN core.assignment.change_reason          IS 'hire | transfer (team change) | promotion (level up) | lateral_move (role change, same level) | reorg.';
COMMENT ON COLUMN core.assignment.previous_assignment_id IS 'The assignment this one replaces (same employee). NULL only for the hire assignment.';

-- Reconstruction contract (agreed with ChatGPT review, G1 step 1):
--  * This view is FINAL-HISTORY ONLY (what is known today). It must never feed features.
--  * As-of reconstruction (pipeline/features.py) first filters assignments and exits to
--    recorded_at <= cutoff AND effective/event date <= as_of, THEN derives interval ends.
--  * Chain validity (immediate earlier predecessor, one hire root, no cycles, assignment within
--    employment and after team creation, nothing after exit) is checked by the validator.
CREATE VIEW core.assignment_interval AS
SELECT a.*,
       LEAST(LEAD(a.effective_from) OVER (PARTITION BY a.employee_id ORDER BY a.effective_from),
             (SELECT e.event_date + 1 FROM core.employment_event e
               WHERE e.employee_id = a.employee_id AND e.event_type = 'exit')) AS effective_to
FROM core.assignment a;
COMMENT ON VIEW core.assignment_interval IS 'FINAL-HISTORY view: assignments with derived exclusive end = earlier of next assignment start and day after termination; NULL if current. Not as-of safe; never used for features.';

CREATE TABLE core.promotion_history (
  promotion_id   text PRIMARY KEY CONSTRAINT promotion_id_format CHECK (promotion_id ~ '^PROMO-\d{4}-\d{6}$'),
  employee_id    text NOT NULL REFERENCES core.employee(employee_id),
  from_role_id   text NOT NULL REFERENCES core.role(role_id),
  to_role_id     text NOT NULL REFERENCES core.role(role_id),
  assignment_id  text NOT NULL UNIQUE,
  effective_date date NOT NULL,
  recorded_at    timestamptz NOT NULL,
  CONSTRAINT promotion_assignment_same_employee FOREIGN KEY (assignment_id, employee_id)
    REFERENCES core.assignment(assignment_id, employee_id),
  CONSTRAINT promotion_changes_role CHECK (from_role_id <> to_role_id)
);
COMMENT ON TABLE core.promotion_history IS 'Level increases. Each promotion starts exactly one assignment.';
COMMENT ON COLUMN core.promotion_history.promotion_id   IS 'PROMO-YYYY-NNNNNN.';
COMMENT ON COLUMN core.promotion_history.assignment_id  IS 'The assignment the promotion starts (same employee, same date).';
COMMENT ON COLUMN core.promotion_history.effective_date IS 'Equals the started assignment''s effective_from (validator rule).';

CREATE TABLE core.tenure_snapshot (
  snapshot_id            text PRIMARY KEY CONSTRAINT snapshot_id_format CHECK (snapshot_id ~ '^SNAP-EMP-\d{6}-\d{4}Q[1-4]$'),
  employee_id            text NOT NULL REFERENCES core.employee(employee_id),
  as_of_date             date NOT NULL,
  assignment_id          text NOT NULL,
  tenure_months          numeric(6,2) NOT NULL CHECK (tenure_months >= 0),
  months_in_role         numeric(6,2) NOT NULL CHECK (months_in_role >= 0),
  months_since_promotion numeric(6,2) CHECK (months_since_promotion >= 0),
  derived_from           jsonb NOT NULL,
  recorded_at            timestamptz NOT NULL,
  UNIQUE (employee_id, as_of_date),
  CONSTRAINT snapshot_assignment_same_employee FOREIGN KEY (assignment_id, employee_id)
    REFERENCES core.assignment(assignment_id, employee_id),
  CONSTRAINT snapshot_id_matches_employee CHECK (substr(snapshot_id, 6, 10) = employee_id),
  CONSTRAINT snapshot_id_matches_quarter  CHECK (substr(snapshot_id, 17) = to_char(as_of_date, 'YYYY"Q"Q')),
  CONSTRAINT snapshot_on_quarter_end      CHECK (as_of_date = core.quarter_end(as_of_date))
);
COMMENT ON TABLE core.tenure_snapshot IS 'Quarter-end tenure facts, derived from employment events, assignments and promotions (never generated independently).';
COMMENT ON COLUMN core.tenure_snapshot.snapshot_id            IS 'SNAP-<employee-id>-YYYYQn.';
COMMENT ON COLUMN core.tenure_snapshot.as_of_date             IS 'Quarter-end date.';
COMMENT ON COLUMN core.tenure_snapshot.assignment_id          IS 'Assignment in force on as_of_date.';
COMMENT ON COLUMN core.tenure_snapshot.tenure_months          IS 'Months since hire_date (days / 30.4375).';
COMMENT ON COLUMN core.tenure_snapshot.months_in_role         IS 'Months since the current role started.';
COMMENT ON COLUMN core.tenure_snapshot.months_since_promotion IS 'Months since the latest promotion; NULL if never promoted in this company.';
COMMENT ON COLUMN core.tenure_snapshot.derived_from           IS 'Lineage: IDs of the source events, assignments and promotions.';

CREATE TABLE core.workload_snapshot (
  workload_id        text PRIMARY KEY CONSTRAINT workload_id_format CHECK (workload_id ~ '^WLS-EMP-\d{6}-\d{4}-\d{2}$'),
  employee_id        text NOT NULL REFERENCES core.employee(employee_id),
  period_month       date NOT NULL CHECK (extract(day FROM period_month) = 1),
  scheduled_workdays smallint NOT NULL CHECK (scheduled_workdays BETWEEN 0 AND 23),
  available_hours    numeric(5,1) NOT NULL CHECK (available_hours >= 0),
  actual_hours       numeric(7,3) NOT NULL CHECK (actual_hours BETWEEN 0 AND 400),
  utilization_pct    numeric(5,1) CHECK (utilization_pct BETWEEN 0 AND 250),
  after_hours_pct    numeric(5,1) NOT NULL CHECK (after_hours_pct BETWEEN 0 AND 100),
  oncall_days        smallint NOT NULL CHECK (oncall_days BETWEEN 0 AND 31),
  recorded_at        timestamptz NOT NULL,
  UNIQUE (employee_id, period_month),
  CONSTRAINT workload_id_matches_employee CHECK (substr(workload_id, 5, 10) = employee_id),
  CONSTRAINT workload_id_matches_month    CHECK (substr(workload_id, 16, 7) = to_char(period_month, 'YYYY-MM')),
  CONSTRAINT workload_capacity_bound      CHECK (available_hours <= scheduled_workdays * 8),
  CONSTRAINT workload_zero_capacity_rule  CHECK ((available_hours = 0) = (utilization_pct IS NULL)),
  CONSTRAINT workload_zero_capacity_hours CHECK (available_hours <> 0 OR actual_hours = 0),
  CONSTRAINT workload_utilization_arith   CHECK (CASE WHEN utilization_pct IS NULL THEN true
                                                     WHEN available_hours = 0 THEN false
                                                     ELSE utilization_pct = actual_hours * 100 / available_hours END)
);
COMMENT ON TABLE core.workload_snapshot IS 'Monthly workload per employee. actual_hours = intensity x available capacity, so leave lowers both hours and capacity and does not change utilisation.';
COMMENT ON COLUMN core.workload_snapshot.workload_id        IS 'WLS-<employee-id>-YYYY-MM.';
COMMENT ON COLUMN core.workload_snapshot.period_month       IS 'First day of the month.';
COMMENT ON COLUMN core.workload_snapshot.scheduled_workdays IS 'Mon-Fri working days in the month while employed, minus company holidays.';
COMMENT ON COLUMN core.workload_snapshot.available_hours    IS '(scheduled_workdays - leave days taken) x 8 hours.';
COMMENT ON COLUMN core.workload_snapshot.actual_hours       IS 'Hours worked in the month = utilization_pct / 100 x available_hours, stored exactly (3 decimals).';
COMMENT ON COLUMN core.workload_snapshot.utilization_pct    IS 'Generated first in 0.1-point steps from work intensity (independent of leave); actual_hours / available_hours x 100 holds exactly. NULL when available_hours = 0.';
COMMENT ON COLUMN core.workload_snapshot.after_hours_pct    IS 'Share of hours worked outside 09:00-18:00, percent.';
COMMENT ON COLUMN core.workload_snapshot.oncall_days        IS 'Days on call in the month.';

CREATE TABLE core.leave_ledger (
  leave_txn_id  text PRIMARY KEY CONSTRAINT leave_txn_id_format CHECK (leave_txn_id ~ '^LVT-\d{4}-\d{7}$'),
  employee_id   text NOT NULL REFERENCES core.employee(employee_id),
  leave_type    text NOT NULL CHECK (leave_type IN ('EL','SL')),
  txn_type      text NOT NULL CHECK (txn_type IN ('accrual','grant','taken','lapse')),
  days          smallint NOT NULL CHECK (days <> 0),
  txn_date      date NOT NULL,
  leave_year    smallint NOT NULL,
  recorded_at   timestamptz NOT NULL,
  CONSTRAINT leave_sign_rule     CHECK ((txn_type IN ('taken','lapse')) = (days < 0)),
  CONSTRAINT leave_accrual_rule  CHECK (txn_type <> 'accrual' OR (leave_type = 'EL' AND days = 1)),
  CONSTRAINT leave_grant_rule    CHECK (txn_type <> 'grant'   OR (leave_type = 'SL' AND days = 3)),
  CONSTRAINT leave_lapse_dec31   CHECK (txn_type <> 'lapse'   OR (extract(month FROM txn_date) = 12 AND extract(day FROM txn_date) = 31)),
  CONSTRAINT leave_year_matches  CHECK (leave_year = extract(year FROM txn_date)),
  CONSTRAINT leave_not_backdated CHECK (recorded_at::date >= txn_date)
);
COMMENT ON TABLE core.leave_ledger IS 'Leave transactions. Policy: EL +1 per employed month; SL +3 on 1 Jan or on hire date; all unused EL and SL lapse on 31 Dec. Balance per type resets to 0 each year and never goes negative (validator). SL rows are restricted: never used as features or shown to managers.';
COMMENT ON COLUMN core.leave_ledger.leave_txn_id IS 'LVT-YYYY-NNNNNNN.';
COMMENT ON COLUMN core.leave_ledger.leave_type   IS 'EL (earned leave) | SL (sick leave, restricted).';
COMMENT ON COLUMN core.leave_ledger.txn_type     IS 'accrual (EL +1) | grant (SL +3) | taken (negative) | lapse (negative, 31 Dec only).';
COMMENT ON COLUMN core.leave_ledger.days         IS 'Signed whole days: positive credits, negative debits.';
COMMENT ON COLUMN core.leave_ledger.txn_date     IS 'Date of the transaction (for taken: first day of the leave block).';
COMMENT ON COLUMN core.leave_ledger.leave_year   IS 'Calendar leave year (= year of txn_date).';

CREATE VIEW core.earned_leave_ledger AS
  SELECT leave_txn_id, employee_id, txn_type, days, txn_date, leave_year, recorded_at
  FROM core.leave_ledger WHERE leave_type = 'EL';
COMMENT ON VIEW core.earned_leave_ledger IS 'EL rows only. The app role reads this view; it has no access to SL rows.';

CREATE TABLE core.engagement_survey (
  survey_id        text PRIMARY KEY CONSTRAINT survey_id_format CHECK (survey_id ~ '^SURV-\d{4}-\d{6}$'),
  employee_id      text NOT NULL REFERENCES core.employee(employee_id),
  wave             text NOT NULL CHECK (wave ~ '^\d{4}Q[1-4]$'),
  sent_date        date NOT NULL,
  response_status  text NOT NULL CHECK (response_status IN ('complete','partial','no_response')),
  q1 smallint CHECK (q1 BETWEEN 1 AND 5),
  q2 smallint CHECK (q2 BETWEEN 1 AND 5),
  q3 smallint CHECK (q3 BETWEEN 1 AND 5),
  q4 smallint CHECK (q4 BETWEEN 1 AND 5),
  q5 smallint CHECK (q5 BETWEEN 1 AND 5),
  items_answered   smallint NOT NULL,
  engagement_index numeric(3,2),
  submitted_at     timestamptz,
  recorded_at      timestamptz NOT NULL,
  UNIQUE (employee_id, wave),
  CONSTRAINT survey_item_count     CHECK (items_answered = num_nonnulls(q1, q2, q3, q4, q5)),
  CONSTRAINT survey_status_rule    CHECK (CASE response_status
                                            WHEN 'complete' THEN items_answered = 5
                                            WHEN 'partial'  THEN items_answered BETWEEN 1 AND 4
                                            ELSE items_answered = 0 END),
  CONSTRAINT survey_index_presence CHECK ((items_answered >= 3) = (engagement_index IS NOT NULL)),
  CONSTRAINT survey_index_mean     CHECK (CASE WHEN engagement_index IS NULL THEN true
                                               WHEN items_answered = 0 THEN false
                                               ELSE engagement_index =
                                            round((coalesce(q1,0)+coalesce(q2,0)+coalesce(q3,0)+coalesce(q4,0)+coalesce(q5,0))::numeric
                                                  / items_answered, 2) END),
  CONSTRAINT survey_submit_rule    CHECK ((response_status = 'no_response') = (submitted_at IS NULL)),
  CONSTRAINT survey_chronology     CHECK (submitted_at IS NULL OR (submitted_at::date >= sent_date AND recorded_at >= submitted_at))
);
COMMENT ON TABLE core.engagement_survey IS 'Quarterly pulse survey: one row per employee per wave, including non-responses. Missing answers are never imputed.';
COMMENT ON COLUMN core.engagement_survey.survey_id        IS 'SURV-YYYY-NNNNNN, YYYY = wave year.';
COMMENT ON COLUMN core.engagement_survey.wave             IS 'YYYYQn. Sent in the 3rd month of the quarter.';
COMMENT ON COLUMN core.engagement_survey.response_status  IS 'complete (5 items) | partial (1-4 items) | no_response (0 items).';
COMMENT ON COLUMN core.engagement_survey.q1               IS 'Q1 "I feel valued for my work" (1 strongly disagree - 5 strongly agree).';
COMMENT ON COLUMN core.engagement_survey.q2               IS 'Q2 "My workload is manageable" (1-5).';
COMMENT ON COLUMN core.engagement_survey.q3               IS 'Q3 "I see a future for myself here" (1-5).';
COMMENT ON COLUMN core.engagement_survey.q4               IS 'Q4 "I get the support I need from my manager" (1-5).';
COMMENT ON COLUMN core.engagement_survey.q5               IS 'Q5 "I would recommend this team as a place to work" (1-5).';
COMMENT ON COLUMN core.engagement_survey.items_answered   IS 'Count of non-null q1-q5.';
COMMENT ON COLUMN core.engagement_survey.engagement_index IS 'Mean of answered items, 2 decimals; NULL when fewer than 3 answered.';

CREATE TABLE core.attrition_outcome (
  outcome_id                 text PRIMARY KEY CONSTRAINT outcome_id_format CHECK (outcome_id ~ '^OUT-EMP-\d{6}-\d{4}$'),
  employee_id                text NOT NULL UNIQUE REFERENCES core.employee(employee_id),
  final_status               text NOT NULL CHECK (final_status IN ('active','on_notice','exited_voluntary','exited_involuntary')),
  resignation_date           date,
  termination_date           date,
  scheduled_termination_date date,
  observation_end            date NOT NULL,
  recorded_at                timestamptz NOT NULL,
  UNIQUE (outcome_id, employee_id),
  CONSTRAINT outcome_id_matches_employee CHECK (substr(outcome_id, 5, 10) = employee_id),
  CONSTRAINT outcome_state_matrix CHECK (COALESCE(CASE final_status
      WHEN 'active'             THEN resignation_date IS NULL AND termination_date IS NULL AND scheduled_termination_date IS NULL
      WHEN 'on_notice'          THEN resignation_date IS NOT NULL AND termination_date IS NULL
                                     AND scheduled_termination_date IS NOT NULL AND scheduled_termination_date > observation_end
      WHEN 'exited_voluntary'   THEN resignation_date IS NOT NULL AND termination_date IS NOT NULL AND scheduled_termination_date IS NULL
      WHEN 'exited_involuntary' THEN resignation_date IS NULL AND termination_date IS NOT NULL AND scheduled_termination_date IS NULL
    END, false)),
  CONSTRAINT outcome_termination_observed CHECK (termination_date IS NULL OR termination_date <= observation_end),
  CONSTRAINT outcome_resignation_observed CHECK (resignation_date IS NULL OR resignation_date <= observation_end),
  CONSTRAINT outcome_resign_before_exit   CHECK (resignation_date IS NULL OR
                                                 resignation_date <= COALESCE(termination_date, scheduled_termination_date))
);
COMMENT ON TABLE core.attrition_outcome IS 'One terminal outcome per employee at the end of observation (derived from employment events). Not readable by the app role.';
COMMENT ON COLUMN core.attrition_outcome.outcome_id                 IS 'OUT-<employee-id>-YYYY, YYYY = observation-end year.';
COMMENT ON COLUMN core.attrition_outcome.final_status               IS 'active | on_notice (resigned, still employed at observation end) | exited_voluntary | exited_involuntary.';
COMMENT ON COLUMN core.attrition_outcome.termination_date           IS 'Observed last working day (<= observation_end). The voluntary-attrition target uses this date.';
COMMENT ON COLUMN core.attrition_outcome.scheduled_termination_date IS 'Only for on_notice: last working day falling after observation_end.';
COMMENT ON COLUMN core.attrition_outcome.observation_end            IS '2026-12-31.';

-- =====================================================================================
-- APP
-- =====================================================================================
CREATE TABLE app.app_user (
  user_id      text PRIMARY KEY CONSTRAINT user_id_format CHECK (user_id ~ '^USR-\d{4}$'),
  username     text NOT NULL UNIQUE,
  display_name text NOT NULL,
  app_role     text NOT NULL CHECK (app_role IN ('manager','hr_partner','it_ops','admin','employee')),
  employee_id  text REFERENCES core.employee(employee_id),
  is_active    boolean NOT NULL DEFAULT true,
  CONSTRAINT account_has_employee CHECK (app_role NOT IN ('manager','employee') OR employee_id IS NOT NULL)
);
CREATE UNIQUE INDEX app_user_one_employee_account ON app.app_user (employee_id) WHERE app_role = 'employee';
COMMENT ON TABLE app.app_user IS 'People who use the app. A manager''s scope is the team of their current assignment. An employee account sees only its own permitted records (My data page), never scores, reviews or interventions.';

CREATE TABLE app.user_credential (
  user_id       text PRIMARY KEY REFERENCES app.app_user(user_id),
  password_hash text NOT NULL CHECK (password_hash LIKE '$argon2%'),
  updated_at    timestamptz NOT NULL
);
COMMENT ON TABLE app.user_credential IS 'argon2 password hashes, kept apart from user profiles.';

CREATE TABLE app.user_session (
  session_id  text PRIMARY KEY,
  user_id     text NOT NULL REFERENCES app.app_user(user_id),
  created_at  timestamptz NOT NULL,
  expires_at  timestamptz NOT NULL,
  revoked_at  timestamptz,
  CONSTRAINT session_expiry_after_creation CHECK (expires_at > created_at)
);
COMMENT ON TABLE app.user_session IS 'Server-side sessions. session_id stores the SHA-256 of the cookie token, never the token itself.';

CREATE TABLE app.score_run (
  score_run_id    text PRIMARY KEY CONSTRAINT score_run_id_format CHECK (score_run_id ~ '^RUN-\d{4}-\d{4}$'),
  as_of_date      date NOT NULL,
  scorer_type     text NOT NULL CHECK (scorer_type IN ('scorecard','logistic_regression')),
  model_version   text NOT NULL,
  config_hash     text NOT NULL,
  model_frozen_at timestamptz NOT NULL,
  executed_at     timestamptz NOT NULL,
  CONSTRAINT score_run_after_freeze CHECK (executed_at >= model_frozen_at)
);
COMMENT ON TABLE app.score_run IS 'One scoring run: a frozen scorer applied at one as-of date.';

CREATE TABLE app.score (
  score_id      text PRIMARY KEY CONSTRAINT score_id_format CHECK (score_id ~ '^SCR-\d{4}-\d{7}$'),
  score_run_id  text NOT NULL REFERENCES app.score_run(score_run_id),
  employee_id   text NOT NULL REFERENCES core.employee(employee_id),
  risk_score    numeric(6,5) NOT NULL CHECK (risk_score BETWEEN 0 AND 1),
  rank_in_run   integer NOT NULL CHECK (rank_in_run >= 1),
  priority_band text NOT NULL CHECK (priority_band IN ('suggested_checkin','monitor','none')),
  contributions jsonb NOT NULL,
  coverage      jsonb NOT NULL,
  source_refs   jsonb NOT NULL,
  UNIQUE (score_run_id, employee_id),
  UNIQUE (score_run_id, rank_in_run),
  UNIQUE (score_id, employee_id)
);
COMMENT ON TABLE app.score IS 'Per-employee result of a score run. A score is a suggestion to check in, never an employment decision.';
COMMENT ON COLUMN app.score.contributions IS '[{feature, value, reference, log_odds}] plus intercept. Log-odds, not probabilities or causal effects.';
COMMENT ON COLUMN app.score.coverage      IS 'Data-coverage notes shown to reviewers (e.g. no survey response last wave). Never weighted.';
COMMENT ON COLUMN app.score.source_refs   IS '[{table, id, recorded_at}] for every source row used.';

CREATE TABLE app.review_decision (
  review_id        text PRIMARY KEY CONSTRAINT review_id_format CHECK (review_id ~ '^REV-\d{4}-\d{6}$'),
  score_id         text NOT NULL REFERENCES app.score(score_id),
  reviewer_user_id text NOT NULL REFERENCES app.app_user(user_id),
  decision         text NOT NULL CHECK (decision IN ('approved','dismissed','needs_info')),
  reason_code      text NOT NULL,
  comment          text,
  decided_at       timestamptz NOT NULL,
  recorded_at      timestamptz NOT NULL,
  CONSTRAINT review_recorded_after_decision CHECK (recorded_at >= decided_at)
);
COMMENT ON TABLE app.review_decision IS 'HR review of a score. Nothing reaches a manager without an approved review.';

CREATE TABLE app.intervention (
  intervention_id   text PRIMARY KEY CONSTRAINT intervention_id_format CHECK (intervention_id ~ '^INT-\d{4}-\d{6}$'),
  employee_id       text NOT NULL,
  score_id          text NOT NULL,
  review_id         text NOT NULL REFERENCES app.review_decision(review_id),
  intervention_type text NOT NULL CHECK (intervention_type IN ('checkin','workload_rebalance','career_conversation','internal_mobility')),
  owner_user_id     text NOT NULL REFERENCES app.app_user(user_id),
  arm               text NOT NULL CHECK (arm IN ('treatment','control')),
  assigned_at       timestamptz NOT NULL,
  source            text NOT NULL CHECK (source IN ('simulated','app')),
  created_at        timestamptz NOT NULL,
  CONSTRAINT intervention_score_same_employee FOREIGN KEY (score_id, employee_id)
    REFERENCES app.score(score_id, employee_id),
  CONSTRAINT intervention_created_after_assignment CHECK (created_at >= assigned_at)
);
COMMENT ON TABLE app.intervention IS 'Supportive action for an approved case. arm is assigned before treatment (80/20); controls never receive treatment. The service layer also checks approval, score version, team scope, eligibility and conflicts.';

CREATE TABLE app.intervention_event (
  event_id        text PRIMARY KEY CONSTRAINT intervention_event_id_format CHECK (event_id ~ '^IEV-\d{4}-\d{7}$'),
  intervention_id text NOT NULL REFERENCES app.intervention(intervention_id),
  from_status     text CHECK (from_status IN ('proposed','scheduled','completed','declined','cancelled')),
  to_status       text NOT NULL CHECK (to_status IN ('proposed','scheduled','completed','declined','cancelled')),
  actor_user_id   text NOT NULL REFERENCES app.app_user(user_id),
  event_at        timestamptz NOT NULL,
  recorded_at     timestamptz NOT NULL,
  note            text,
  CONSTRAINT intervention_event_chronology CHECK (recorded_at >= event_at),
  CONSTRAINT intervention_event_changes_status CHECK (from_status IS DISTINCT FROM to_status)
);
COMMENT ON TABLE app.intervention_event IS 'Append-only status history. Current status = latest event.';

CREATE TABLE app.exception_log (
  exception_id text PRIMARY KEY CONSTRAINT exception_id_format CHECK (exception_id ~ '^EXC-\d{4}-\d{6}$'),
  entity       text NOT NULL,
  record_id    text NOT NULL,
  issue_type   text NOT NULL CHECK (issue_type IN
                 ('FUTURE_LEAK','MISSING_AS_NEGATIVE','LABEL_DATE_MISMATCH','UNREVIEWED_SCORE',
                  'NOT_AUTHORIZED','STALE_SCORE_VERSION','NOT_ELIGIBLE','CONFLICTING_INTERVENTION',
                  'CONTROL_ARM_BLOCKED','QUARANTINED_ROW','INSUFFICIENT_HISTORY','UNRESOLVED_FOLLOWUP','OTHER')),
  detector     text NOT NULL,
  detail       jsonb NOT NULL DEFAULT '{}',
  detected_at  timestamptz NOT NULL,
  status       text NOT NULL CHECK (status IN ('open','acknowledged','resolved'))
);
COMMENT ON TABLE app.exception_log IS 'Detected data issues and blocked actions (metadata only; raw bad payloads stay in the restricted quarantine folder).';

-- =====================================================================================
-- EVAL (restricted)
-- =====================================================================================
CREATE TABLE eval.horizon_label (
  employee_id       text NOT NULL REFERENCES core.employee(employee_id),
  as_of_date        date NOT NULL,
  horizon_end       date NOT NULL,
  label_status      text NOT NULL CHECK (label_status IN
                      ('voluntary_exit','employed_through_horizon','competing_exit','unresolved','ineligible')),
  ineligible_reason text CHECK (ineligible_reason IN ('resignation_known','insufficient_history','not_employed')),
  target            smallint CHECK (target IN (0,1)),
  outcome_id        text NOT NULL,
  PRIMARY KEY (employee_id, as_of_date),
  CONSTRAINT label_outcome_same_employee FOREIGN KEY (outcome_id, employee_id)
    REFERENCES core.attrition_outcome(outcome_id, employee_id),
  CONSTRAINT label_horizon_two_quarters CHECK (horizon_end = core.quarter_end((as_of_date + interval '6 months')::date)),
  CONSTRAINT label_on_quarter_end       CHECK (as_of_date = core.quarter_end(as_of_date)),
  CONSTRAINT label_ineligible_reason    CHECK ((label_status = 'ineligible') = (ineligible_reason IS NOT NULL)),
  CONSTRAINT label_target_rule          CHECK (COALESCE(CASE label_status
                                                 WHEN 'voluntary_exit' THEN target = 1
                                                 WHEN 'employed_through_horizon' THEN target = 0
                                                 WHEN 'competing_exit' THEN target = 0
                                                 ELSE target IS NULL END, false))
);
COMMENT ON TABLE eval.horizon_label IS 'Target per employee and quarter-end: voluntary termination on or before the second following quarter-end. Evaluation-only.';
COMMENT ON COLUMN eval.horizon_label.label_status IS 'voluntary_exit (1) | employed_through_horizon (0, incl. resigned after as_of but still serving notice) | competing_exit (0, involuntary) | unresolved (horizon beyond observation; excluded) | ineligible (excluded).';

CREATE TABLE eval.potential_outcome (
  employee_id        text NOT NULL REFERENCES core.employee(employee_id),
  as_of_date         date NOT NULL,
  y_if_treated       smallint NOT NULL CHECK (y_if_treated IN (0,1)),
  y_if_untreated     smallint NOT NULL CHECK (y_if_untreated IN (0,1)),
  PRIMARY KEY (employee_id, as_of_date)
);
COMMENT ON TABLE eval.potential_outcome IS 'For intervention-eligible cases: voluntary exit by horizon end if treated / if not treated, from common random draws. y_if_untreated equals the recorded history. Evaluation-only.';

CREATE TABLE eval.ground_truth (
  gt_id              text PRIMARY KEY CONSTRAINT gt_id_format CHECK (gt_id ~ '^GT-\d{6}$'),
  case_id            text NOT NULL,
  entity             text NOT NULL,
  record_id          text NOT NULL,
  issue_type         text NOT NULL,
  expected_detection text NOT NULL CHECK (expected_detection IN ('reject_feature','block_action','flag_label','quarantine_row','none')),
  is_control         boolean NOT NULL,
  rule_version       text NOT NULL
);
COMMENT ON TABLE eval.ground_truth IS 'Answer key for injected anomalies and clean controls (is_control = true, expected_detection = none). Never used as a feature.';

-- =====================================================================================
-- COLUMN DESCRIPTIONS (data dictionary source; scripts/gen_dictionary.py fails on any gap)
-- =====================================================================================
COMMENT ON COLUMN core.attrition_outcome.resignation_date IS 'Date the resignation was submitted (voluntary exits and on_notice only).';
COMMENT ON COLUMN core.engagement_survey.sent_date        IS 'Date the survey wave was sent (15th of the quarter''s last month).';
COMMENT ON COLUMN core.engagement_survey.submitted_at     IS 'When the employee submitted answers; NULL for no_response.';
COMMENT ON COLUMN core.promotion_history.from_role_id     IS 'Role held before the promotion.';
COMMENT ON COLUMN core.promotion_history.to_role_id       IS 'Role held after the promotion (higher level).';
COMMENT ON COLUMN core.assignment.team_id                 IS 'Team for this interval.';
COMMENT ON COLUMN core.assignment.role_id                 IS 'Role for this interval.';

COMMENT ON COLUMN app.app_user.user_id      IS 'USR-NNNN.';
COMMENT ON COLUMN app.app_user.username     IS 'Login name, unique.';
COMMENT ON COLUMN app.app_user.display_name IS 'Name shown in the UI (persona, e.g. "HR Partner 01").';
COMMENT ON COLUMN app.app_user.app_role     IS 'manager | hr_partner | it_ops | admin | employee.';
COMMENT ON COLUMN app.app_user.employee_id  IS 'Linked employee record; required for managers (scope = team of their current assignment) and employees (scope = their own records only; one account per employee).';
COMMENT ON COLUMN app.app_user.is_active    IS 'Inactive users cannot log in.';

COMMENT ON COLUMN app.user_credential.user_id       IS 'User the credential belongs to.';
COMMENT ON COLUMN app.user_credential.password_hash IS 'argon2 hash (starts with $argon2). Plain passwords are never stored.';
COMMENT ON COLUMN app.user_credential.updated_at    IS 'When the password was last set.';

COMMENT ON COLUMN app.user_session.session_id IS 'SHA-256 hex of the session cookie token.';
COMMENT ON COLUMN app.user_session.user_id    IS 'Logged-in user.';
COMMENT ON COLUMN app.user_session.created_at IS 'Login time.';
COMMENT ON COLUMN app.user_session.expires_at IS 'Session expiry (fixed lifetime).';
COMMENT ON COLUMN app.user_session.revoked_at IS 'Logout time; NULL while active.';

COMMENT ON COLUMN app.score_run.score_run_id    IS 'RUN-YYYY-NNNN.';
COMMENT ON COLUMN app.score_run.as_of_date      IS 'Quarter-end the run scores (inputs recorded on or before this date only).';
COMMENT ON COLUMN app.score_run.scorer_type     IS 'scorecard (points rules) | logistic_regression.';
COMMENT ON COLUMN app.score_run.model_version   IS 'Version label of the frozen scorer.';
COMMENT ON COLUMN app.score_run.config_hash     IS 'SHA-256 of the scorer configuration and coefficients.';
COMMENT ON COLUMN app.score_run.model_frozen_at IS 'When the scorer was frozen (after validation labels became available).';
COMMENT ON COLUMN app.score_run.executed_at     IS 'When the run executed (>= model_frozen_at).';

COMMENT ON COLUMN app.score.score_id      IS 'SCR-YYYY-NNNNNNN.';
COMMENT ON COLUMN app.score.score_run_id  IS 'Run that produced the score (fixes the score version).';
COMMENT ON COLUMN app.score.risk_score    IS 'Estimated probability of voluntary exit by the horizon end, 0-1.';
COMMENT ON COLUMN app.score.rank_in_run   IS '1 = highest score in the run.';
COMMENT ON COLUMN app.score.priority_band IS 'suggested_checkin | monitor | none (thresholds versioned in config).';

COMMENT ON COLUMN app.review_decision.review_id        IS 'REV-YYYY-NNNNNN.';
COMMENT ON COLUMN app.review_decision.score_id         IS 'Score (and therefore score version) being reviewed.';
COMMENT ON COLUMN app.review_decision.reviewer_user_id IS 'HR partner who decided (taken from the session, never the request body).';
COMMENT ON COLUMN app.review_decision.decision         IS 'approved | dismissed | needs_info.';
COMMENT ON COLUMN app.review_decision.reason_code      IS 'Coded reason for the decision.';
COMMENT ON COLUMN app.review_decision.comment          IS 'Optional free-text note.';
COMMENT ON COLUMN app.review_decision.decided_at       IS 'When the decision was made.';
COMMENT ON COLUMN app.review_decision.recorded_at      IS 'When the decision was stored.';

COMMENT ON COLUMN app.intervention.intervention_id   IS 'INT-YYYY-NNNNNN.';
COMMENT ON COLUMN app.intervention.score_id          IS 'Approved score that justified the intervention (same employee).';
COMMENT ON COLUMN app.intervention.review_id         IS 'Approved HR review (service checks decision, version, scope, chronology).';
COMMENT ON COLUMN app.intervention.intervention_type IS 'checkin | workload_rebalance | career_conversation | internal_mobility.';
COMMENT ON COLUMN app.intervention.owner_user_id     IS 'User responsible for carrying it out.';
COMMENT ON COLUMN app.intervention.arm               IS 'treatment | control (seeded 80/20, assigned before treatment; controls are never treated).';
COMMENT ON COLUMN app.intervention.assigned_at       IS 'When the arm was assigned.';
COMMENT ON COLUMN app.intervention.source            IS 'simulated (generator) | app (created through the UI).';
COMMENT ON COLUMN app.intervention.created_at        IS 'When the record was created.';

COMMENT ON COLUMN app.intervention_event.event_id        IS 'IEV-YYYY-NNNNNNN.';
COMMENT ON COLUMN app.intervention_event.intervention_id IS 'Intervention whose status changed.';
COMMENT ON COLUMN app.intervention_event.from_status     IS 'Previous status; NULL for the first event.';
COMMENT ON COLUMN app.intervention_event.to_status       IS 'proposed | scheduled | completed | declined | cancelled.';
COMMENT ON COLUMN app.intervention_event.actor_user_id   IS 'User who made the change (from the session).';
COMMENT ON COLUMN app.intervention_event.event_at        IS 'When the change happened.';
COMMENT ON COLUMN app.intervention_event.note            IS 'Optional note.';

COMMENT ON COLUMN app.exception_log.exception_id IS 'EXC-YYYY-NNNNNN.';
COMMENT ON COLUMN app.exception_log.entity       IS 'Table, file or API action the issue concerns.';
COMMENT ON COLUMN app.exception_log.record_id    IS 'Business ID of the affected record or request.';
COMMENT ON COLUMN app.exception_log.issue_type   IS 'Coded issue; one of the listed values.';
COMMENT ON COLUMN app.exception_log.detector     IS 'Rule or component that detected it (e.g. validator.survey_null_consistency, api.review_gate).';
COMMENT ON COLUMN app.exception_log.detail       IS 'Structured detail (rule version, HTTP status, field). No restricted values.';
COMMENT ON COLUMN app.exception_log.detected_at  IS 'When it was detected.';
COMMENT ON COLUMN app.exception_log.status       IS 'open | acknowledged | resolved.';

COMMENT ON COLUMN eval.horizon_label.as_of_date        IS 'Scoring quarter-end.';
COMMENT ON COLUMN eval.horizon_label.horizon_end       IS 'Second quarter-end after as_of_date.';
COMMENT ON COLUMN eval.horizon_label.ineligible_reason IS 'resignation_known | insufficient_history | not_employed; only for ineligible.';
COMMENT ON COLUMN eval.horizon_label.target            IS '1 voluntary_exit; 0 employed_through_horizon or competing_exit; NULL otherwise.';
COMMENT ON COLUMN eval.horizon_label.outcome_id        IS 'Terminal outcome of the same employee.';

COMMENT ON COLUMN eval.potential_outcome.as_of_date     IS 'Scoring date of the intervention list.';
COMMENT ON COLUMN eval.potential_outcome.y_if_treated   IS '1 if the person would exit voluntarily by the horizon end when treated.';
COMMENT ON COLUMN eval.potential_outcome.y_if_untreated IS '1 if the person would exit voluntarily by the horizon end when untreated (= recorded history).';

COMMENT ON COLUMN eval.ground_truth.gt_id              IS 'GT-NNNNNN.';
COMMENT ON COLUMN eval.ground_truth.case_id            IS 'Injection case identifier (groups the rows of one injected scenario).';
COMMENT ON COLUMN eval.ground_truth.entity             IS 'Table/file or workflow action the case targets.';
COMMENT ON COLUMN eval.ground_truth.record_id          IS 'Business ID of the injected or control record.';
COMMENT ON COLUMN eval.ground_truth.issue_type         IS 'FUTURE_LEAK | MISSING_AS_NEGATIVE | LABEL_DATE_MISMATCH | UNREVIEWED_SCORE (or the control''s twin type).';
COMMENT ON COLUMN eval.ground_truth.expected_detection IS 'reject_feature | block_action | flag_label | quarantine_row | none (controls).';
COMMENT ON COLUMN eval.ground_truth.is_control         IS 'True for clean near-valid controls that must NOT be flagged.';
COMMENT ON COLUMN eval.ground_truth.rule_version       IS 'Version of the injection rule set.';

-- Shared column meanings and view columns (inherit the base column description).
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT c.oid::regclass AS rel, a.attname
           FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname IN ('core','app','eval') AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
             AND col_description(c.oid, a.attnum) IS NULL AND a.attname IN ('employee_id','recorded_at')
  LOOP
    EXECUTE format('COMMENT ON COLUMN %s.%I IS %L', r.rel, r.attname,
      CASE r.attname WHEN 'employee_id' THEN 'Employee (EMP-NNNNNN) this row belongs to.'
                     ELSE 'When the row became known to the system (availability time used by as-of filters).' END);
  END LOOP;
  FOR r IN SELECT v.oid::regclass AS view_rel, a.attname, col_description(b.oid, ba.attnum) AS d
           FROM (VALUES ('core.assignment_interval'::regclass, 'core.assignment'::regclass),
                        ('core.earned_leave_ledger'::regclass, 'core.leave_ledger'::regclass)) m(vrel, brel)
           JOIN pg_class v ON v.oid = m.vrel JOIN pg_attribute a ON a.attrelid = v.oid AND a.attnum > 0
           JOIN pg_class b ON b.oid = m.brel
           LEFT JOIN pg_attribute ba ON ba.attrelid = b.oid AND ba.attname = a.attname
  LOOP
    IF r.d IS NOT NULL THEN
      EXECUTE format('COMMENT ON COLUMN %s.%I IS %L', r.view_rel, r.attname, r.d);
    END IF;
  END LOOP;
END $$;
COMMENT ON COLUMN core.assignment_interval.effective_to IS 'Derived exclusive end: earlier of the next assignment start and the day after termination; NULL if current.';

-- =====================================================================================
-- APPEND-ONLY ENFORCEMENT
-- =====================================================================================
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY[
    'core.employee','core.employment_event','core.assignment','core.promotion_history','core.tenure_snapshot',
    'core.workload_snapshot','core.leave_ledger','core.engagement_survey','core.attrition_outcome',
    'app.score_run','app.score','app.review_decision','app.intervention','app.intervention_event']
  LOOP
    EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION core.forbid_mutation()', t);
    EXECUTE format('CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON %s EXECUTE FUNCTION core.forbid_mutation()', t);
  END LOOP;
END $$;

-- =====================================================================================
-- ROLES AND GRANTS
-- rs_app : used by the API (SET ROLE rs_app on every connection). No eval, no outcomes, no SL, no pay.
-- rs_eval: evaluation jobs. Read-only on everything.
-- =====================================================================================
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rs_app')  THEN CREATE ROLE rs_app  NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rs_eval') THEN CREATE ROLE rs_eval NOLOGIN; END IF;
END $$;

REVOKE ALL ON SCHEMA core, app, eval FROM PUBLIC;

-- The API connects as a separate LOGIN role that is a member of rs_app (created by scripts/db_reset.py
-- with a password from the environment), never as the owner/superuser.
GRANT USAGE ON SCHEMA core, app TO rs_app;
GRANT SELECT ON core.role, core.team, core.employee, core.employment_event, core.assignment,
                core.assignment_interval, core.promotion_history, core.tenure_snapshot,
                core.earned_leave_ledger TO rs_app;
-- Column-level grants: no capacity/actual-hours pair (could reveal sick leave), no raw survey items.
GRANT SELECT (workload_id, employee_id, period_month, utilization_pct, after_hours_pct, oncall_days, recorded_at)
  ON core.workload_snapshot TO rs_app;
GRANT SELECT (survey_id, employee_id, wave, sent_date, response_status, items_answered, engagement_index,
              submitted_at, recorded_at)
  ON core.engagement_survey TO rs_app;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO rs_app;
GRANT INSERT ON app.review_decision, app.intervention, app.intervention_event,
                app.exception_log, app.user_session TO rs_app;
GRANT UPDATE (revoked_at) ON app.user_session TO rs_app;

GRANT USAGE ON SCHEMA core, app, eval TO rs_eval;
GRANT SELECT ON ALL TABLES IN SCHEMA core, app, eval TO rs_eval;

CREATE INDEX ON core.assignment (employee_id, effective_from);
CREATE INDEX ON core.employment_event (employee_id, event_date);
CREATE INDEX ON core.workload_snapshot (employee_id, period_month);
CREATE INDEX ON core.leave_ledger (employee_id, txn_date);
CREATE INDEX ON core.engagement_survey (employee_id, wave);
CREATE INDEX ON app.score (score_run_id, rank_in_run);
CREATE INDEX ON app.review_decision (score_id);
