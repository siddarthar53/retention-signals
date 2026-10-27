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


def run(conn) -> dict[str, list]:
    """Return {check_name: offending rows (max 20)}; empty list = pass."""
    results = {}
    for name, query in CHECKS.items():
        results[name] = [r[0] for r in conn.execute(query).fetchmany(20)]
    return results
