"""Schema constraint, append-only and access tests.

Each failing case asserts the exact constraint (or error class) that must fire; each passing case
asserts the row is accepted. Fixed timestamps only.
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg import errors

from conftest import as_role, expect_ok, expect_violation

CHECK = errors.CheckViolation
FK = errors.ForeignKeyViolation
UNIQUE = errors.UniqueViolation
DENIED = errors.InsufficientPrivilege
APPEND_ONLY = errors.RestrictViolation


def survey(sid, status, items, idx, q=(None,) * 5, wave="2025Q1", submitted="2025-03-16 10:00+00"):
    qs = ",".join("NULL" if v is None else str(v) for v in q)
    sub = "NULL" if submitted is None else f"'{submitted}'"
    i = "NULL" if idx is None else str(idx)
    return (f"INSERT INTO core.engagement_survey VALUES ('{sid}','EMP-000001','{wave}','2025-03-15',"
            f"'{status}',{qs},{items},{i},{sub},'2025-03-17 09:00+00')")


# ---------------------------------------------------------------- engagement survey
def test_survey_complete_ok(conn):
    expect_ok(conn, survey("SURV-2025-000001", "complete", 5, 4.00, (4, 4, 4, 4, 4)))

def test_survey_partial_ok(conn):
    expect_ok(conn, survey("SURV-2025-000002", "partial", 3, 4.00, (4, 3, 5, None, None)))

def test_survey_no_response_ok(conn):
    expect_ok(conn, survey("SURV-2025-000003", "no_response", 0, None, submitted=None))

def test_survey_fabricated_item_count(conn):
    """Claims 5 answers but only 3 items are present (mean is consistent, so only the count rule fails)."""
    expect_violation(conn, survey("SURV-2025-000004", "complete", 5, 2.40, (4, 4, 4, None, None)),
                     "survey_item_count", CHECK)

def test_survey_fabricated_index_without_answers(conn):
    expect_violation(conn, survey("SURV-2025-000008", "complete", 5, 4.20), None, CHECK)

def test_survey_wrong_mean(conn):
    expect_violation(conn, survey("SURV-2025-000005", "complete", 5, 2.00, (4, 4, 4, 4, 4)), "survey_index_mean", CHECK)

def test_survey_missing_treated_as_value(conn):
    """MISSING_AS_NEGATIVE at the DB layer: a non-response cannot carry an engagement score.
    Both survey_index_presence and survey_index_mean reject it; no division by zero."""
    err = expect_violation(conn, survey("SURV-2025-000006", "no_response", 0, 2.00, submitted=None), None, CHECK)
    assert err.diag.constraint_name in {"survey_index_presence", "survey_index_mean"}

def test_survey_index_needs_three_items(conn):
    expect_violation(conn, survey("SURV-2025-000007", "partial", 2, 4.50, (4, 5, None, None, None)),
                     "survey_index_presence", CHECK)


# ---------------------------------------------------------------- outcomes
def outcome(oid, emp, status, resign, term, sched):
    v = lambda x: "NULL" if x is None else f"'{x}'"
    return (f"INSERT INTO core.attrition_outcome VALUES ('{oid}','{emp}','{status}',{v(resign)},{v(term)},"
            f"{v(sched)},'2026-12-31','2027-01-05 09:00+00')")

def test_outcome_on_notice_ok(db_url):
    with psycopg.connect(db_url) as c:
        c.execute("INSERT INTO core.employee VALUES ('EMP-000003','2021-01-04','2021-01-04 09:00+00')")
        expect_ok(c, outcome("OUT-EMP-000003-2026", "EMP-000003", "on_notice", "2026-12-15", None, "2027-01-15"))
        c.rollback()

def test_outcome_on_notice_without_scheduled_date(db_url):
    with psycopg.connect(db_url) as c:
        c.execute("INSERT INTO core.employee VALUES ('EMP-000003','2021-01-04','2021-01-04 09:00+00')")
        expect_violation(c, outcome("OUT-EMP-000003-2026", "EMP-000003", "on_notice", "2026-11-01", None, None),
                         "outcome_state_matrix", CHECK)
        c.rollback()

def test_outcome_id_must_match_employee(db_url):
    with psycopg.connect(db_url) as c:
        c.execute("INSERT INTO core.employee VALUES ('EMP-000003','2021-01-04','2021-01-04 09:00+00')")
        expect_violation(c, outcome("OUT-EMP-000009-2026", "EMP-000003", "active", None, None, None),
                         "outcome_id_matches_employee", CHECK)
        c.rollback()

def test_outcome_resignation_after_observation_end(db_url):
    with psycopg.connect(db_url) as c:
        c.execute("INSERT INTO core.employee VALUES ('EMP-000003','2021-01-04','2021-01-04 09:00+00')")
        expect_violation(c, outcome("OUT-EMP-000003-2026", "EMP-000003", "on_notice", "2027-01-02", None, "2027-02-01"),
                         "outcome_resignation_observed", CHECK)
        c.rollback()

def test_outcome_voluntary_without_resignation(db_url):
    with psycopg.connect(db_url) as c:
        c.execute("INSERT INTO core.employee VALUES ('EMP-000003','2021-01-04','2021-01-04 09:00+00')")
        expect_violation(c, outcome("OUT-EMP-000003-2026", "EMP-000003", "exited_voluntary", None, "2026-05-31", None),
                         "outcome_state_matrix", CHECK)
        c.rollback()


# ---------------------------------------------------------------- horizon labels
def label(emp, as_of, horizon, status, reason, target, oid):
    v = lambda x: "NULL" if x is None else f"'{x}'"
    t = "NULL" if target is None else str(target)
    return (f"INSERT INTO eval.horizon_label VALUES ('{emp}','{as_of}','{horizon}','{status}',{v(reason)},{t},'{oid}')")

@pytest.mark.parametrize("status", ["voluntary_exit", "employed_through_horizon", "competing_exit"])
def test_label_completed_status_requires_target(conn, status):
    expect_violation(conn, label("EMP-000001", "2025-06-30", "2025-12-31", status, None, None, "OUT-EMP-000001-2026"),
                     "label_target_rule", CHECK)

@pytest.mark.parametrize("status,target", [("voluntary_exit", 1), ("employed_through_horizon", 0), ("competing_exit", 0)])
def test_label_completed_status_ok(conn, status, target):
    expect_ok(conn, label("EMP-000001", "2025-06-30", "2025-12-31", status, None, target, "OUT-EMP-000001-2026"))

def test_label_unresolved_null_target_ok(conn):
    expect_ok(conn, label("EMP-000001", "2026-09-30", "2027-03-31", "unresolved", None, None, "OUT-EMP-000001-2026"))

def test_label_ineligible_null_target_ok(conn):
    expect_ok(conn, label("EMP-000001", "2025-06-30", "2025-12-31", "ineligible", "resignation_known", None,
                          "OUT-EMP-000001-2026"))

def test_label_wrong_target_value(conn):
    expect_violation(conn, label("EMP-000001", "2025-06-30", "2025-12-31", "voluntary_exit", None, 0, "OUT-EMP-000001-2026"),
                     "label_target_rule", CHECK)

def test_label_points_at_other_employees_outcome(conn):
    expect_violation(conn, label("EMP-000002", "2025-06-30", "2025-12-31", "employed_through_horizon", None, 0,
                                 "OUT-EMP-000001-2026"), "label_outcome_same_employee", FK)

def test_label_horizon_must_be_two_quarter_ends(conn):
    expect_violation(conn, label("EMP-000001", "2025-06-30", "2025-09-30", "employed_through_horizon", None, 0,
                                 "OUT-EMP-000001-2026"), "label_horizon_two_quarters", CHECK)


# ---------------------------------------------------------------- workload
def workload(wid, month, sched, avail, actual, util):
    u = "NULL" if util is None else str(util)
    return (f"INSERT INTO core.workload_snapshot VALUES ('{wid}','EMP-000001','{month}',{sched},{avail},{actual},{u},"
            f"10,0,'{month}'::date + 35)")

def test_workload_leave_month_keeps_utilization(conn):
    """5 leave days: capacity 17 days x 8h = 136h, actual 136h -> 100% (leave does not move utilisation)."""
    expect_ok(conn, workload("WLS-EMP-000001-2025-04", "2025-04-01", 22, 136, 136, 100.0))

def test_workload_utilization_arithmetic(conn):
    expect_violation(conn, workload("WLS-EMP-000001-2025-03", "2025-03-01", 21, 128, 128, 150.0),
                     "workload_utilization_arith", CHECK)

def test_workload_exact_quantisation_ok(conn):
    """util 87.3% of 136h -> actual 118.728h exactly; the check is exact, not a tolerance."""
    expect_ok(conn, workload("WLS-EMP-000001-2025-07", "2025-07-01", 23, 136, 118.728, 87.3))

def test_workload_rounded_actual_rejected(conn):
    """The old 0.1h rounding (118.7h) no longer satisfies the exact rule."""
    expect_violation(conn, workload("WLS-EMP-000001-2025-08", "2025-08-01", 21, 136, 118.7, 87.3),
                     "workload_utilization_arith", CHECK)

def test_workload_zero_capacity_requires_null_utilization(conn):
    err = expect_violation(conn, workload("WLS-EMP-000001-2025-05", "2025-05-01", 0, 0, 0, 0.0), None, CHECK)
    assert err.diag.constraint_name in {"workload_zero_capacity_rule", "workload_utilization_arith"}

def test_workload_zero_capacity_positive_hours(conn):
    """No capacity (e.g. a full-leave month) cannot record worked hours."""
    expect_violation(conn, workload("WLS-EMP-000001-2025-05", "2025-05-01", 0, 0, 100, None),
                     "workload_zero_capacity_hours", CHECK)

def test_workload_zero_capacity_null_utilization_ok(conn):
    expect_ok(conn, workload("WLS-EMP-000001-2025-05", "2025-05-01", 0, 0, 0, None))

def test_workload_capacity_bound(conn):
    expect_violation(conn, workload("WLS-EMP-000001-2025-06", "2025-06-01", 10, 120, 100, 83.3),
                     "workload_capacity_bound", CHECK)

def test_workload_id_must_match_month(conn):
    expect_violation(conn, workload("WLS-EMP-000001-2025-07", "2025-08-01", 21, 168, 168, 100.0),
                     "workload_id_matches_month", CHECK)


# ---------------------------------------------------------------- leave ledger
def leave(lid, ltype, txn, days, date, year=None):
    y = year if year is not None else date[:4]
    return (f"INSERT INTO core.leave_ledger VALUES ('{lid}','EMP-000001','{ltype}','{txn}',{days},'{date}',{y},"
            f"'{date} 23:00+00')")

def test_leave_full_year_resets_to_zero(conn):
    for m in range(1, 13):
        expect_ok(conn, leave(f"LVT-2025-{m:07d}", "EL", "accrual", 1, f"2025-{m:02d}-01"))
    expect_ok(conn, leave("LVT-2025-0000100", "EL", "taken", -9, "2025-10-13"))
    expect_ok(conn, leave("LVT-2025-0000101", "EL", "lapse", -3, "2025-12-31"))
    expect_ok(conn, leave("LVT-2025-0000102", "SL", "grant", 3, "2025-01-01"))
    expect_ok(conn, leave("LVT-2025-0000103", "SL", "lapse", -3, "2025-12-31"))
    bal = dict(conn.execute("SELECT leave_type, sum(days) FROM core.leave_ledger "
                            "WHERE employee_id='EMP-000001' AND leave_year=2025 GROUP BY 1").fetchall())
    assert bal == {"EL": 0, "SL": 0}

def test_leave_accrual_must_be_one_el_day(conn):
    expect_violation(conn, leave("LVT-2025-0000001", "EL", "accrual", 2, "2025-01-01"), "leave_accrual_rule", CHECK)

def test_leave_sl_cannot_accrue(conn):
    expect_violation(conn, leave("LVT-2025-0000002", "SL", "accrual", 1, "2025-01-01"), "leave_accrual_rule", CHECK)

def test_leave_lapse_only_on_dec_31(conn):
    expect_violation(conn, leave("LVT-2025-0000003", "EL", "lapse", -1, "2025-11-30"), "leave_lapse_dec31", CHECK)

def test_leave_lapse_must_be_negative(conn):
    expect_violation(conn, leave("LVT-2025-0000004", "EL", "lapse", 2, "2025-12-31"), "leave_sign_rule", CHECK)

def test_leave_no_opening_rows(conn):
    expect_violation(conn, leave("LVT-2025-0000005", "EL", "opening", 4, "2025-01-01"),
                     "leave_ledger_txn_type_check", CHECK)

def test_leave_year_must_match_date(conn):
    expect_violation(conn, leave("LVT-2025-0000006", "EL", "accrual", 1, "2025-02-01", year=2026),
                     "leave_year_matches", CHECK)


# ---------------------------------------------------------------- assignments
def test_assignment_transfer_ok_and_interval_derived(conn):
    expect_ok(conn, "INSERT INTO core.assignment VALUES ('ASGN-2025-000003','EMP-000001','TEAM-0002','ROLE-0001',"
                    "'2025-08-01','transfer','ASGN-2022-000001','2025-08-01 09:00+00')")
    rows = conn.execute("SELECT assignment_id, effective_to FROM core.assignment_interval "
                        "WHERE employee_id='EMP-000001' ORDER BY effective_from").fetchall()
    assert [(r[0], str(r[1])) for r in rows] == [("ASGN-2022-000001", "2025-08-01"), ("ASGN-2025-000003", "None")]

def test_assignment_interval_capped_at_exit(conn):
    expect_ok(conn, "INSERT INTO core.employment_event VALUES ('EEV-2025-000001','EMP-000001','exit','2025-06-30',"
                    "'involuntary',NULL,'2025-06-30 18:00+00')")
    end = conn.execute("SELECT effective_to FROM core.assignment_interval WHERE assignment_id='ASGN-2022-000001'").fetchone()[0]
    assert str(end) == "2025-07-01"

def test_assignment_self_reference(conn):
    expect_violation(conn, "INSERT INTO core.assignment VALUES ('ASGN-2025-000009','EMP-000001','TEAM-0001','ROLE-0001',"
                           "'2025-10-01','transfer','ASGN-2025-000009','2025-10-01 09:00+00')",
                     "assignment_no_self_reference", CHECK)

def test_assignment_second_hire_root(conn):
    expect_violation(conn, "INSERT INTO core.assignment VALUES ('ASGN-2024-000009','EMP-000001','TEAM-0002','ROLE-0001',"
                           "'2024-01-01','hire',NULL,'2024-01-01 09:00+00')",
                     "assignment_one_hire_root", UNIQUE)

def test_assignment_chain_other_employee(conn):
    expect_violation(conn, "INSERT INTO core.assignment VALUES ('ASGN-2025-000010','EMP-000002','TEAM-0002','ROLE-0001',"
                           "'2025-10-01','transfer','ASGN-2022-000001','2025-10-01 09:00+00')",
                     "assignment_chain_same_employee", FK)

def test_assignment_two_starts_same_day(conn):
    expect_violation(conn, "INSERT INTO core.assignment VALUES ('ASGN-2022-000011','EMP-000001','TEAM-0002','ROLE-0001',"
                           "'2022-03-14','transfer','ASGN-2022-000001','2022-03-14 09:00+00')",
                     "assignment_one_start_per_day", UNIQUE)


# ---------------------------------------------------------------- snapshots and events
def snapshot(sid, as_of):
    return (f"INSERT INTO core.tenure_snapshot VALUES ('{sid}','EMP-000001','{as_of}','ASGN-2022-000001',"
            f"37.5,37.5,NULL,'{{}}','{as_of}'::date + 1)")

def test_snapshot_ok(conn):
    expect_ok(conn, snapshot("SNAP-EMP-000001-2025Q2", "2025-06-30"))

def test_snapshot_quarter_mismatch(conn):
    expect_violation(conn, snapshot("SNAP-EMP-000001-2025Q1", "2025-06-30"), "snapshot_id_matches_quarter", CHECK)

def test_snapshot_not_quarter_end(conn):
    expect_violation(conn, snapshot("SNAP-EMP-000001-2025Q2", "2025-06-15"), "snapshot_on_quarter_end", CHECK)

def test_snapshot_assignment_of_other_employee(conn):
    expect_violation(conn, "INSERT INTO core.tenure_snapshot VALUES ('SNAP-EMP-000001-2025Q2','EMP-000001','2025-06-30',"
                           "'ASGN-2023-000002',37.5,37.5,NULL,'{}','2025-07-01')",
                     "snapshot_assignment_same_employee", FK)

def test_event_cannot_be_backdated(conn):
    expect_violation(conn, "INSERT INTO core.employment_event VALUES ('EEV-2025-000002','EMP-000001','resignation_submitted',"
                           "'2025-06-30',NULL,60,'2025-06-01 09:00+00')", "event_not_backdated", CHECK)

def test_event_exit_needs_exit_type(conn):
    expect_violation(conn, "INSERT INTO core.employment_event VALUES ('EEV-2025-000003','EMP-000001','exit',"
                           "'2025-06-30',NULL,NULL,'2025-06-30 18:00+00')", "event_exit_type_rule", CHECK)


# ---------------------------------------------------------------- append-only
@pytest.mark.parametrize("stmt", [
    "UPDATE core.assignment SET team_id='TEAM-0002' WHERE assignment_id='ASGN-2022-000001'",
    "DELETE FROM core.attrition_outcome WHERE outcome_id='OUT-EMP-000001-2026'",
    "TRUNCATE core.employment_event CASCADE",
])
def test_history_is_append_only(conn, stmt):
    expect_violation(conn, stmt, None, APPEND_ONLY)


# ---------------------------------------------------------------- access control
@pytest.mark.parametrize("query", [
    "SELECT 1 FROM eval.horizon_label",
    "SELECT 1 FROM eval.ground_truth",
    "SELECT 1 FROM core.attrition_outcome",
    "SELECT 1 FROM core.leave_ledger",
    "SELECT 1 FROM core.compensation_band",
    "SELECT actual_hours FROM core.workload_snapshot",
    "SELECT available_hours FROM core.workload_snapshot",
    "SELECT q1 FROM core.engagement_survey",
])
def test_app_role_denied(conn, query):
    with as_role(conn, "rs_app"):
        expect_violation(conn, query, None, DENIED)

@pytest.mark.parametrize("query", [
    "SELECT utilization_pct, after_hours_pct FROM core.workload_snapshot",
    "SELECT engagement_index, response_status FROM core.engagement_survey",
    "SELECT * FROM core.earned_leave_ledger",
    "SELECT * FROM core.assignment_interval",
    "SELECT * FROM app.score",
])
def test_app_role_allowed(conn, query):
    with as_role(conn, "rs_app"):
        expect_ok(conn, query)

def test_app_role_cannot_write_history(conn):
    with as_role(conn, "rs_app"):
        expect_violation(conn, "INSERT INTO core.employee VALUES ('EMP-000009','2024-01-01','2024-01-01 09:00+00')",
                         None, DENIED)

def test_eval_role_reads_eval(conn):
    with as_role(conn, "rs_eval"):
        expect_ok(conn, "SELECT count(*) FROM eval.horizon_label")
        expect_ok(conn, "SELECT count(*) FROM core.attrition_outcome")


# ---------------------------------------------------------------- app workflow tables
def test_credential_requires_argon2_hash(conn):
    conn.execute("INSERT INTO app.app_user VALUES ('USR-0001','hr1','HR Partner 01','hr_partner',NULL,true)")
    expect_violation(conn, "INSERT INTO app.user_credential VALUES ('USR-0001','plaintext','2025-01-01 09:00+00')",
                     "user_credential_password_hash_check", CHECK)

def test_manager_user_needs_employee(conn):
    expect_violation(conn, "INSERT INTO app.app_user VALUES ('USR-0002','mgr1','Manager 01','manager',NULL,true)",
                     "account_has_employee", CHECK)

def test_intervention_score_of_other_employee(conn):
    conn.execute("INSERT INTO app.app_user VALUES ('USR-0001','hr1','HR Partner 01','hr_partner',NULL,true)")
    conn.execute("INSERT INTO app.score_run VALUES ('RUN-2026-0001','2026-06-30','logistic_regression','lr-1','abc',"
                 "'2026-04-01 09:00+00','2026-07-01 09:00+00')")
    conn.execute("INSERT INTO app.score VALUES ('SCR-2026-0000001','RUN-2026-0001','EMP-000001',0.12,1,"
                 "'suggested_checkin','[]','{}','[]')")
    conn.execute("INSERT INTO app.review_decision VALUES ('REV-2026-000001','SCR-2026-0000001','USR-0001','approved',"
                 "'supportive_checkin',NULL,'2026-07-02 09:00+00','2026-07-02 09:00+00')")
    expect_violation(conn, "INSERT INTO app.intervention VALUES ('INT-2026-000001','EMP-000002','SCR-2026-0000001',"
                           "'REV-2026-000001','checkin','USR-0001','treatment','2026-07-03 09:00+00','simulated',"
                           "'2026-07-03 09:00+00')", "intervention_score_same_employee", FK)
