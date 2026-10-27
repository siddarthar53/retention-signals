"""Semantic validator: rules the schema cannot express on one row (docs/enforcement.md, G2 rows).

Each check is a SQL query returning the offending rows; a check passes when it returns none.
Run against a loaded database. Later stages add checks; none is ever removed.
"""
from __future__ import annotations

CHECKS: dict[str, str] = {
    # ---- keys and lineage beyond what FKs prove
    "every_employee_has_one_hire_event": """
        SELECT e.employee_id FROM core.employee e
        LEFT JOIN core.employment_event v ON v.employee_id = e.employee_id AND v.event_type = 'hire'
        GROUP BY e.employee_id, e.hire_date HAVING count(v.*) <> 1 OR min(v.event_date) <> e.hire_date""",
    "every_employee_has_hire_assignment_on_hire_date": """
        SELECT e.employee_id FROM core.employee e
        LEFT JOIN core.assignment a ON a.employee_id = e.employee_id AND a.change_reason = 'hire'
        WHERE a.assignment_id IS NULL OR a.effective_from <> e.hire_date""",
    "assignment_predecessor_is_immediately_earlier": """
        SELECT a.assignment_id FROM (
          SELECT assignment_id, previous_assignment_id,
                 lag(assignment_id) OVER (PARTITION BY employee_id ORDER BY effective_from) AS expected
          FROM core.assignment) a
        WHERE a.previous_assignment_id IS DISTINCT FROM a.expected""",
    "assignment_not_before_hire": """
        SELECT a.assignment_id FROM core.assignment a JOIN core.employee e USING (employee_id)
        WHERE a.effective_from < e.hire_date""",
    "assignment_not_before_team_created": """
        SELECT a.assignment_id FROM core.assignment a JOIN core.team t USING (team_id)
        WHERE a.effective_from < t.created_date""",
    "assignment_role_family_matches_team": """
        SELECT a.assignment_id FROM core.assignment a JOIN core.team t USING (team_id) JOIN core.role r USING (role_id)
        WHERE r.job_family <> t.job_family""",
    "promotion_date_equals_assignment_start": """
        SELECT p.promotion_id FROM core.promotion_history p JOIN core.assignment a USING (assignment_id)
        WHERE p.effective_date <> a.effective_from OR a.change_reason <> 'promotion'""",
    "promotion_raises_level_by_one_from_previous_role": """
        SELECT p.promotion_id FROM core.promotion_history p
        JOIN core.assignment a USING (assignment_id)
        JOIN core.assignment prev ON prev.assignment_id = a.previous_assignment_id
        JOIN core.role f ON f.role_id = p.from_role_id JOIN core.role t ON t.role_id = p.to_role_id
        WHERE prev.role_id <> p.from_role_id OR a.role_id <> p.to_role_id OR t.level <> f.level + 1
           OR (f.is_people_manager AND NOT t.is_people_manager)""",
    "pre_window_promotions_spaced_and_feasible": """
        SELECT promotion_id FROM (
          SELECT p.promotion_id, p.effective_date,
                 coalesce(lag(p.effective_date) OVER (PARTITION BY p.employee_id ORDER BY p.effective_date),
                          e.hire_date) AS prev_date
          FROM core.promotion_history p JOIN core.employee e USING (employee_id)
          WHERE p.effective_date < DATE '2025-01-01') x
        WHERE effective_date - prev_date < 548""",
    "one_manager_per_team_on_window_start": """
        SELECT t.team_id FROM core.team t
        LEFT JOIN core.assignment_interval a ON a.team_id = t.team_id
             AND a.effective_from <= DATE '2025-01-01' AND (a.effective_to IS NULL OR a.effective_to > DATE '2025-01-01')
        LEFT JOIN core.role r ON r.role_id = a.role_id AND r.is_people_manager
        GROUP BY t.team_id HAVING count(r.role_id) <> 1""",
}


TERM = """(SELECT employee_id, event_date AS termination FROM core.employment_event WHERE event_type = 'exit')"""

CHECKS.update({
    # ---- nothing after termination (except the Dec 31 leave close)
    "no_workload_after_termination": f"""
        SELECT w.workload_id FROM core.workload_snapshot w JOIN {TERM} t USING (employee_id)
        WHERE w.period_month > t.termination""",
    "no_survey_after_termination": f"""
        SELECT s.survey_id FROM core.engagement_survey s JOIN {TERM} t USING (employee_id)
        WHERE s.sent_date > t.termination OR s.submitted_at::date > t.termination""",
    "no_leave_after_termination_except_lapse": f"""
        SELECT l.leave_txn_id FROM core.leave_ledger l JOIN {TERM} t USING (employee_id)
        WHERE l.txn_date > t.termination AND l.txn_type <> 'lapse'""",
    "no_assignment_after_termination": f"""
        SELECT a.assignment_id FROM core.assignment a JOIN {TERM} t USING (employee_id)
        WHERE a.effective_from > t.termination""",
    "no_rows_before_hire": """
        SELECT w.workload_id FROM core.workload_snapshot w JOIN core.employee e USING (employee_id)
        WHERE w.period_month < date_trunc('month', e.hire_date)::date
        UNION ALL SELECT l.leave_txn_id FROM core.leave_ledger l JOIN core.employee e USING (employee_id)
        WHERE l.txn_date < e.hire_date""",
    # ---- leave accounting
    "leave_balance_never_negative": """
        SELECT leave_txn_id FROM (
          SELECT leave_txn_id, sum(days) OVER (PARTITION BY employee_id, leave_type, leave_year
                 ORDER BY txn_date, CASE txn_type WHEN 'taken' THEN 1 WHEN 'lapse' THEN 2 ELSE 0 END, leave_txn_id) AS bal
          FROM core.leave_ledger) x WHERE bal < 0""",
    "leave_balance_zero_after_year_close": """
        SELECT employee_id || ':' || leave_type || ':' || leave_year FROM core.leave_ledger
        GROUP BY employee_id, leave_type, leave_year HAVING sum(days) <> 0""",
    "leave_one_el_accrual_per_month": """
        SELECT employee_id || ':' || to_char(txn_date, 'YYYY-MM') FROM core.leave_ledger
        WHERE txn_type = 'accrual' GROUP BY employee_id, to_char(txn_date, 'YYYY-MM') HAVING count(*) > 1""",
    "leave_one_sl_grant_per_year": """
        SELECT employee_id || ':' || leave_year FROM core.leave_ledger WHERE txn_type = 'grant'
        GROUP BY employee_id, leave_year HAVING count(*) > 1""",
    # ---- outcome vs events
    "outcome_matches_events": """
        SELECT o.outcome_id FROM core.attrition_outcome o
        LEFT JOIN core.employment_event r ON r.employee_id = o.employee_id AND r.event_type = 'resignation_submitted'
        LEFT JOIN core.employment_event x ON x.employee_id = o.employee_id AND x.event_type = 'exit'
        WHERE NOT CASE o.final_status
          WHEN 'active' THEN r.event_id IS NULL AND x.event_id IS NULL
          WHEN 'on_notice' THEN r.event_date = o.resignation_date AND x.event_id IS NULL
                                AND o.scheduled_termination_date BETWEEN r.event_date + r.notice_days - 6
                                                                     AND r.event_date + r.notice_days
          WHEN 'exited_voluntary' THEN r.event_date = o.resignation_date AND x.event_date = o.termination_date
                                       AND x.exit_type = 'voluntary'
          WHEN 'exited_involuntary' THEN r.event_id IS NULL AND x.event_date = o.termination_date
                                         AND x.exit_type = 'involuntary' END""",
    # ---- labels vs outcome and population sets
    "label_matches_outcome": """
        SELECT l.employee_id || '@' || l.as_of_date FROM eval.horizon_label l
        JOIN core.attrition_outcome o ON o.outcome_id = l.outcome_id
        WHERE l.label_status IN ('voluntary_exit','employed_through_horizon','competing_exit')
          AND l.label_status <> CASE
                WHEN o.termination_date <= l.horizon_end AND o.final_status = 'exited_voluntary' THEN 'voluntary_exit'
                WHEN o.termination_date <= l.horizon_end AND o.final_status = 'exited_involuntary' THEN 'competing_exit'
                ELSE 'employed_through_horizon' END""",
    "label_only_for_employed": """
        SELECT l.employee_id || '@' || l.as_of_date FROM eval.horizon_label l
        JOIN core.employee e USING (employee_id) JOIN core.attrition_outcome o USING (employee_id)
        WHERE e.hire_date > l.as_of_date OR o.termination_date <= l.as_of_date""",
    "label_unresolved_iff_horizon_after_observation": """
        SELECT l.employee_id || '@' || l.as_of_date FROM eval.horizon_label l
        WHERE l.label_status <> 'ineligible' AND (l.label_status = 'unresolved') <> (l.horizon_end > DATE '2026-12-31')""",
    "label_ineligible_reason_correct": """
        SELECT l.employee_id || '@' || l.as_of_date FROM eval.horizon_label l JOIN core.employee e USING (employee_id)
        LEFT JOIN core.employment_event r ON r.employee_id = l.employee_id AND r.event_type = 'resignation_submitted'
        WHERE (l.ineligible_reason = 'resignation_known') <>
                  (r.event_date IS NOT NULL AND r.event_date <= l.as_of_date AND r.recorded_at::date <= l.as_of_date)
           OR (l.ineligible_reason = 'insufficient_history') <>
                  (NOT (r.event_date IS NOT NULL AND r.event_date <= l.as_of_date AND r.recorded_at::date <= l.as_of_date)
                   AND e.hire_date > date_trunc('quarter', l.as_of_date)::date)""",
    "snapshot_set_equals_label_set": """
        SELECT coalesce(s.employee_id, l.employee_id) || '@' || coalesce(s.as_of_date, l.as_of_date)
        FROM core.tenure_snapshot s FULL JOIN eval.horizon_label l USING (employee_id, as_of_date)
        WHERE s.employee_id IS NULL OR l.employee_id IS NULL""",
    "snapshot_assignment_in_force": """
        SELECT s.snapshot_id FROM core.tenure_snapshot s
        JOIN core.assignment_interval a ON a.assignment_id = s.assignment_id
        WHERE a.effective_from > s.as_of_date OR (a.effective_to IS NOT NULL AND a.effective_to <= s.as_of_date)""",
    "potential_outcome_untreated_equals_label": """
        SELECT p.employee_id FROM eval.potential_outcome p
        LEFT JOIN eval.horizon_label l USING (employee_id, as_of_date)
        WHERE l.target IS DISTINCT FROM p.y_if_untreated OR p.y_if_treated > p.y_if_untreated""",
    "potential_outcome_covers_june_scoring_population": """
        SELECT l.employee_id FROM eval.horizon_label l
        LEFT JOIN eval.potential_outcome p USING (employee_id, as_of_date)
        WHERE l.as_of_date = DATE '2026-06-30' AND l.target IS NOT NULL AND p.employee_id IS NULL""",
    # ---- totals
    "voluntary_attrition_within_5_15_percent": """
        SELECT round(100.0 * count(*) FILTER (WHERE o.final_status = 'exited_voluntary') / count(*), 2)
        FROM core.attrition_outcome o
        HAVING count(*) FILTER (WHERE o.final_status = 'exited_voluntary')::numeric / count(*) NOT BETWEEN 0.05 AND 0.15""",
})


def label_mismatches_in(conn, table: str) -> set[str]:
    """Apply the label reconciliation rule to any label-shaped table (used for injected fixtures)."""
    q = CHECKS["label_matches_outcome"].replace("eval.horizon_label", table)
    return {r[0] for r in conn.execute(q).fetchall()}


def run(conn) -> dict[str, list]:
    """Return {check_name: offending rows (max 20)}; empty list = pass."""
    results = {}
    for name, query in CHECKS.items():
        results[name] = [r[0] for r in conn.execute(query).fetchmany(20)]
    return results
