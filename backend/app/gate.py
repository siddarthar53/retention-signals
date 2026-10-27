"""Business rules for reviews and interventions (the review gate).

Every rule failure raises Blocked. The caller rolls back the work transaction, records the attempt in
app.exception_log in its own transaction, and returns the HTTP status carried by Blocked. Check order for
an intervention request: scope (403) -> score version -> approved review -> eligibility -> conflicts, so a
manager outside the team never learns anything about the case."""
from __future__ import annotations

import datetime as dt
import json

from pipeline.allocation import assign_arm  # noqa: F401  (one shared, versioned allocation rule)

from .db import next_id
from .security import User

TERMINAL = {"completed", "declined", "cancelled"}
TRANSITIONS = {"proposed": {"scheduled", "declined", "cancelled"},
               "scheduled": {"completed", "declined", "cancelled"}}
INTERVENTION_TYPES = ("checkin", "workload_rebalance", "career_conversation", "internal_mobility")
REASONS = {
    "approved": ("supportive_checkin", "workload_concern", "career_growth", "new_joiner_support"),
    "dismissed": ("context_known_to_hr", "recent_conversation_held", "data_quality_concern", "not_appropriate_now"),
    "needs_info": ("await_manager_input", "await_data_correction"),
}


class Blocked(Exception):
    def __init__(self, issue_type: str, status: int, message: str, record_id: str, detail: dict | None = None):
        super().__init__(message)
        self.issue_type, self.status, self.message = issue_type, status, message
        self.record_id, self.detail = record_id, detail or {}


class NoActiveRun(Exception):
    pass


# ---------------------------------------------------------------- context
def active_run(conn) -> str:
    """The production run: the latest logistic-regression run (ties broken by execution time, then ID).
    Scores from any other run are stale."""
    row = conn.execute("""SELECT score_run_id FROM app.score_run WHERE scorer_type = 'logistic_regression'
                          ORDER BY as_of_date DESC, executed_at DESC, score_run_id DESC LIMIT 1""").fetchone()
    if row is None:
        raise NoActiveRun()
    return row[0]


def team_of(conn, employee_id: str, at: dt.datetime) -> str | None:
    """Team of the assignment in force at `at`, using only rows recorded by then."""
    row = conn.execute("""SELECT team_id FROM core.assignment
                          WHERE employee_id = %s AND effective_from <= %s AND recorded_at <= %s
                          ORDER BY effective_from DESC LIMIT 1""", (employee_id, at.date(), at)).fetchone()
    return row[0] if row else None


def manager_team(conn, user: User, at: dt.datetime) -> str | None:
    """The team a manager user may act for RIGHT NOW: their own assignment in force at `at` must be a
    people-manager role, and they must not have left. Checked on every read and write."""
    if user.role != "manager" or not user.employee_id:
        return None
    row = conn.execute("""SELECT a.team_id, r.is_people_manager FROM core.assignment a JOIN core.role r USING (role_id)
                          WHERE a.employee_id = %s AND a.effective_from <= %s AND a.recorded_at <= %s
                          ORDER BY a.effective_from DESC LIMIT 1""", (user.employee_id, at.date(), at)).fetchone()
    if row is None or not row[1] or ineligible_reason(conn, user.employee_id, at) == "employee has left":
        return None
    return row[0]


def in_scope(conn, user: User, employee_id: str, at: dt.datetime) -> bool:
    if user.role == "hr_partner":
        return True
    if user.role == "manager":
        team = manager_team(conn, user, at)
        return team is not None and team == team_of(conn, employee_id, at) and employee_id != user.employee_id
    return False


def ineligible_reason(conn, employee_id: str, at: dt.datetime) -> str | None:
    row = conn.execute("""SELECT event_type FROM core.employment_event
                          WHERE employee_id = %s AND event_type IN ('resignation_submitted', 'exit')
                            AND event_date <= %s AND recorded_at <= %s
                          ORDER BY event_date DESC LIMIT 1""", (employee_id, at.date(), at)).fetchone()
    return {"exit": "employee has left", "resignation_submitted": "resignation already submitted"}.get(row[0]) if row else None


def latest_review(conn, score_id: str):
    return conn.execute("""SELECT review_id, decision, reason_code, decided_at FROM app.review_decision
                           WHERE score_id = %s ORDER BY decided_at DESC, review_id DESC LIMIT 1""", (score_id,)).fetchone()


def current_status(conn, intervention_id: str) -> str | None:
    row = conn.execute("""SELECT to_status FROM app.intervention_event WHERE intervention_id = %s
                          ORDER BY event_at DESC, event_id DESC LIMIT 1""", (intervention_id,)).fetchone()
    return row[0] if row else None


def _score(conn, score_id: str):
    return conn.execute("""SELECT s.score_id, s.employee_id, s.score_run_id FROM app.score s
                           WHERE s.score_id = %s""", (score_id,)).fetchone()


# ---------------------------------------------------------------- actions
def record_review(conn, user: User, score_id: str, decision: str, reason_code: str, comment: str | None,
                  at: dt.datetime) -> str:
    if decision not in REASONS or reason_code not in REASONS[decision]:
        raise ValueError("Unknown decision or reason code")
    sc = _score(conn, score_id)
    if sc is None:
        raise LookupError(score_id)
    _, emp, run = sc
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("employee:" + emp,))
    if run != active_run(conn):
        raise Blocked("STALE_SCORE_VERSION", 409, "This score is not from the active scoring run. Open the current case instead.",
                      score_id, {"score_run_id": run})
    why = ineligible_reason(conn, emp, at)
    if why:
        raise Blocked("NOT_ELIGIBLE", 409, f"No review needed: {why}.", score_id, {"reason": why})
    if conn.execute("SELECT 1 FROM app.intervention WHERE score_id = %s", (score_id,)).fetchone():
        raise Blocked("CONFLICTING_INTERVENTION", 409, "An intervention already exists for this case; the review is locked.",
                      score_id)
    rid = next_id(conn, "app.review_decision", "review_id", f"REV-{at.year}-", 6)
    conn.execute("INSERT INTO app.review_decision VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                 (rid, score_id, user.user_id, decision, reason_code, (comment or None), at, at))
    return rid


def create_intervention(conn, user: User, score_id: str, intervention_type: str, at: dt.datetime) -> dict:
    if intervention_type not in INTERVENTION_TYPES:
        raise ValueError("Unknown intervention type")
    sc = _score(conn, score_id)
    if sc is None:
        raise LookupError(score_id)
    _, emp, run = sc
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("employee:" + emp,))
    if not in_scope(conn, user, emp, at):
        raise Blocked("NOT_AUTHORIZED", 403, "This case is outside your team.", score_id,
                      {"employee_id": emp})
    current = conn.execute("SELECT score_id FROM app.score WHERE score_run_id = %s AND employee_id = %s",
                           (active_run(conn), emp)).fetchone()
    if run != active_run(conn) or current is None or current[0] != score_id:
        raise Blocked("STALE_SCORE_VERSION", 409, "This score is not the current version for this person.",
                      score_id, {"score_run_id": run, "current_score_id": current[0] if current else None})
    rev = latest_review(conn, score_id)
    if rev is None or rev[1] != "approved":
        raise Blocked("UNREVIEWED_SCORE", 409, "HR has not approved this case for a conversation.", score_id,
                      {"latest_decision": rev[1] if rev else None})
    why = ineligible_reason(conn, emp, at)
    if why:
        raise Blocked("NOT_ELIGIBLE", 409, f"Not eligible: {why}.", score_id, {"reason": why})
    existing = conn.execute("""SELECT intervention_id, arm FROM app.intervention i JOIN app.score s USING (score_id)
                               WHERE i.employee_id = %s AND s.score_run_id = %s""", (emp, run)).fetchall()
    if any(arm == "control" for _, arm in existing):
        raise Blocked("CONTROL_ARM_BLOCKED", 409, "This case is in the comparison group for this run; no outreach is recorded.",
                      score_id, {"intervention_ids": [i for i, _ in existing]})
    if existing:
        raise Blocked("CONFLICTING_INTERVENTION", 409, "An intervention already exists for this person in this run.",
                      score_id, {"intervention_ids": [i for i, _ in existing]})
    arm = assign_arm(emp, run)
    iid = next_id(conn, "app.intervention", "intervention_id", f"INT-{at.year}-", 6)
    conn.execute("INSERT INTO app.intervention VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'app',%s)",
                 (iid, emp, score_id, rev[0], intervention_type, user.user_id, arm, at, at))
    first, note = ("proposed", None) if arm == "treatment" else \
        ("cancelled", "Comparison group: recorded for evaluation, no outreach")
    _event(conn, iid, None, first, user, at, note)
    return {"intervention_id": iid, "arm": arm, "status": first}


def change_status(conn, user: User, intervention_id: str, to_status: str, note: str | None, at: dt.datetime) -> str:
    row = conn.execute("SELECT employee_id, owner_user_id, arm FROM app.intervention WHERE intervention_id = %s",
                       (intervention_id,)).fetchone()
    if row is None:
        raise LookupError(intervention_id)
    emp, owner, arm = row
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("employee:" + emp,))
    if not in_scope(conn, user, emp, at):          # ownership never overrides current scope
        raise Blocked("NOT_AUTHORIZED", 403, "This intervention is outside your team.", intervention_id,
                      {"employee_id": emp, "owner_user_id": owner})
    frm = current_status(conn, intervention_id)
    if arm == "control":
        raise Blocked("CONTROL_ARM_BLOCKED", 409, "Comparison-group records cannot be progressed.", intervention_id)
    if to_status not in TRANSITIONS.get(frm, set()):
        raise ValueError(f"Cannot move from {frm} to {to_status}")
    return _event(conn, intervention_id, frm, to_status, user, at, note)


def _event(conn, iid, frm, to, user, at, note) -> str:
    eid = next_id(conn, "app.intervention_event", "event_id", f"IEV-{at.year}-", 7)
    conn.execute("INSERT INTO app.intervention_event VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                 (eid, iid, frm, to, user.user_id, at, at, (note or None)))
    return eid


def log_blocked(conn, b: Blocked, user: User, action: str, at: dt.datetime) -> str:
    with conn.transaction():
        xid = next_id(conn, "app.exception_log", "exception_id", f"EXC-{at.year}-", 6)
        conn.execute("INSERT INTO app.exception_log VALUES (%s,%s,%s,%s,%s,%s,%s,'open')",
                     (xid, "api." + action, b.record_id, b.issue_type, "backend.gate",
                      json.dumps({"user_id": user.user_id, "role": user.role, **b.detail}), at))
    return xid
