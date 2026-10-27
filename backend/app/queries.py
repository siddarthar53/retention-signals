"""Read models. Every response is built field by field (no SELECT *), and only from data recorded by
the simulation clock. Managers get a reduced view: no score, rank or model numbers."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import gate
from .security import User

THEMES = {
    "promo_clock_over_24": ("career", "Long time since the last promotion or role change"),
    "months_in_role": ("career", "Long time in the current role"),
    "tenure_months": ("career", "Long tenure; growth conversations may be due"),
    "engagement_latest": ("engagement", "Recent survey responses were lower than typical"),
    "engagement_decline": ("engagement", "Survey responses dropped since the previous wave"),
    "util_mean_3m": ("workload", "Workload has been above typical in recent months"),
    "sustained_overload": ("workload", "Sustained high workload in 3 of the last 4 months"),
    "after_hours_mean_3m": ("workload", "More after-hours work than typical"),
    "oncall_mean_3m": ("workload", "More on-call days than typical"),
    "new_joiner": ("settling_in", "Joined within the last year"),
    "recent_transfer": ("settling_in", "Moved team recently"),
}
CONVERSATION_GUIDE = {
    "career": ["Ask what they would like to be doing in a year.", "Discuss growth options, including internal moves.",
               "Agree one concrete next step and a date."],
    "engagement": ["Ask open questions about what is working and what is not.", "Listen first; do not mention scores or surveys.",
                   "Follow up on anything you can change."],
    "workload": ["Review current priorities together.", "Ask what could be dropped, delayed or shared.",
                 "Check on-call and after-hours load and agree a change."],
    "settling_in": ["Ask how the new team or role is going.", "Check they have the context and contacts they need.",
                    "Offer a buddy or regular short check-ins."],
}
# Managers get neutral conversation topics in a FIXED order: no statement about the person's survey
# answers or workload numbers, and no ordering that reveals which factor weighed most.
MANAGER_TOPIC_ORDER = ("career", "workload", "settling_in", "engagement")
MANAGER_TOPICS = {
    "career": "Career growth and next steps",
    "workload": "Workload and priorities",
    "settling_in": "Settling into the team or role",
    "engagement": "How work is going overall",
}
PURPOSE = ("This is a prompt for a supportive conversation, not a prediction about a person and never a basis for "
           "performance, pay or employment decisions.")


def iso(v):
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v


def num(v, nd=3):
    return None if v is None else round(float(v), nd)


def themes(contributions: list[dict], limit: int = 3) -> list[dict]:
    """Supportive factor themes from positive contributions (largest first), one per theme."""
    out, seen = [], set()
    for c in sorted((c for c in contributions if c["feature"] != "intercept" and c["log_odds"] > 0.05),
                    key=lambda c: -c["log_odds"]):
        theme, text = THEMES[c["feature"]]
        if theme in seen:
            continue
        seen.add(theme)
        out.append({"theme": theme, "text": text, "guide": CONVERSATION_GUIDE[theme]})
        if len(out) == limit:
            break
    return out


def manager_topics(contributions: list[dict]) -> list[dict]:
    found = {t["theme"] for t in themes(contributions)}
    return [{"theme": k, "text": MANAGER_TOPICS[k], "guide": CONVERSATION_GUIDE[k]}
            for k in MANAGER_TOPIC_ORDER if k in found]


def employee_context(conn, emp: str, at: dt.datetime) -> dict:
    row = conn.execute("""
        SELECT a.team_id, t.team_name, r.role_title, r.job_family, r.level, r.is_people_manager, e.hire_date
        FROM core.employee e
        JOIN LATERAL (SELECT team_id, role_id FROM core.assignment WHERE employee_id = e.employee_id
                      AND effective_from <= %(d)s AND recorded_at <= %(t)s ORDER BY effective_from DESC LIMIT 1) a ON true
        JOIN core.team t USING (team_id) JOIN core.role r USING (role_id)
        WHERE e.employee_id = %(e)s""", {"e": emp, "d": at.date(), "t": at}).fetchone()
    keys = ["team_id", "team_name", "role_title", "job_family", "level", "is_people_manager", "hire_date"]
    return {k: iso(v) for k, v in zip(keys, row)} if row else {}


def review_status_sql() -> str:
    return """(SELECT decision FROM app.review_decision r WHERE r.score_id = s.score_id
               ORDER BY decided_at DESC, review_id DESC LIMIT 1)"""


def intervention_status_sql() -> str:
    return """(SELECT ev.to_status FROM app.intervention i JOIN app.intervention_event ev USING (intervention_id)
               WHERE i.score_id = s.score_id ORDER BY ev.event_at DESC, ev.event_id DESC LIMIT 1)"""


def queue(conn, *, band: str | None, status: str | None, family: str | None, q: str | None,
          limit: int, offset: int) -> dict:
    run = gate.active_run(conn)
    where, p = ["s.score_run_id = %(run)s", "s.priority_band <> 'none'"], {"run": run}
    if band in ("suggested_checkin", "monitor"):
        where.append("s.priority_band = %(band)s"); p["band"] = band
    if family:
        where.append("r.job_family = %(fam)s"); p["fam"] = family
    if q:
        where.append("s.employee_id ILIKE %(q)s"); p["q"] = f"%{q.strip()}%"
    base = f"""
        WITH x AS (
          SELECT s.score_id, s.employee_id, s.risk_score, s.rank_in_run, s.priority_band, s.contributions, s.coverage,
                 r.job_family, r.role_title, t.team_name, {review_status_sql()} AS review, {intervention_status_sql()} AS iv
          FROM app.score s
          JOIN core.tenure_snapshot ts ON ts.employee_id = s.employee_id
               AND ts.as_of_date = (SELECT as_of_date FROM app.score_run WHERE score_run_id = s.score_run_id)
          JOIN core.assignment a ON a.assignment_id = ts.assignment_id
          JOIN core.role r ON r.role_id = a.role_id JOIN core.team t ON t.team_id = a.team_id
          WHERE {' AND '.join(where)})
        SELECT * FROM x"""
    if status == "pending":
        base += " WHERE review IS NULL"
    elif status in ("approved", "dismissed", "needs_info"):
        base += " WHERE review = %(st)s"; p["st"] = status
    total = conn.execute(f"SELECT count(*) FROM ({base}) y", p).fetchone()[0]
    rows = conn.execute(base + " ORDER BY rank_in_run LIMIT %(lim)s OFFSET %(off)s",
                        {**p, "lim": limit, "off": offset}).fetchall()
    items = [{"score_id": r[0], "employee_id": r[1], "risk_score": num(r[2], 4), "rank": r[3], "band": r[4],
              "themes": [t["text"] for t in themes(r[5], 2)], "coverage": r[6], "job_family": r[7],
              "role_title": r[8], "team_name": r[9], "review": r[10] or "pending", "intervention": r[11]}
             for r in rows]
    return {"score_run_id": run, "total": total, "items": items}


def _score_row(conn, score_id: str):
    return conn.execute("""SELECT s.score_id, s.employee_id, s.score_run_id, s.risk_score, s.rank_in_run, s.priority_band,
                                  s.contributions, s.coverage, jsonb_array_length(s.source_refs), r.as_of_date,
                                  r.model_version, r.scorer_type,
                                  (SELECT count(*) FROM app.score z WHERE z.score_run_id = s.score_run_id)
                           FROM app.score s JOIN app.score_run r USING (score_run_id) WHERE s.score_id = %s""",
                        (score_id,)).fetchone()


def reviews_and_interventions(conn, score_id: str) -> tuple[list, list]:
    reviews = [{"review_id": r[0], "decision": r[1], "reason_code": r[2], "comment": r[3], "decided_at": iso(r[4]),
                "reviewer": r[5]} for r in conn.execute(
        """SELECT d.review_id, d.decision, d.reason_code, d.comment, d.decided_at, u.display_name
           FROM app.review_decision d JOIN app.app_user u ON u.user_id = d.reviewer_user_id
           WHERE d.score_id = %s ORDER BY d.decided_at, d.review_id""", (score_id,))]
    ivs = []
    for r in conn.execute("""SELECT i.intervention_id, i.intervention_type, i.arm, i.source, i.assigned_at, u.display_name
                             FROM app.intervention i JOIN app.app_user u ON u.user_id = i.owner_user_id
                             WHERE i.score_id = %s ORDER BY i.assigned_at""", (score_id,)):
        events = [{"event_id": e[0], "from": e[1], "to": e[2], "at": iso(e[3]), "actor": e[4], "note": e[5]}
                  for e in conn.execute("""SELECT ev.event_id, ev.from_status, ev.to_status, ev.event_at, u.display_name, ev.note
                                           FROM app.intervention_event ev JOIN app.app_user u ON u.user_id = ev.actor_user_id
                                           WHERE ev.intervention_id = %s ORDER BY ev.event_at, ev.event_id""", (r[0],))]
        ivs.append({"intervention_id": r[0], "type": r[1], "arm": r[2], "source": r[3], "assigned_at": iso(r[4]),
                    "owner": r[5], "status": events[-1]["to"] if events else None, "events": events})
    return reviews, ivs


def case_for_hr(conn, score_id: str, at: dt.datetime) -> dict | None:
    s = _score_row(conn, score_id)
    if s is None:
        return None
    emp = s[1]
    snap = conn.execute("""SELECT tenure_months, months_in_role, months_since_promotion FROM core.tenure_snapshot
                           WHERE employee_id = %s AND as_of_date = %s""", (emp, s[9])).fetchone()
    workload = [{"month": iso(w[0])[:7], "utilization_pct": num(w[1], 1), "after_hours_pct": num(w[2], 1),
                 "oncall_days": w[3], "after_as_of": w[0] > s[9]}
                for w in conn.execute("""SELECT period_month, utilization_pct, after_hours_pct, oncall_days
                                         FROM core.workload_snapshot WHERE employee_id = %s AND recorded_at <= %s
                                         ORDER BY period_month DESC LIMIT 12""", (emp, at))][::-1]
    surveys = [{"wave": v[0], "status": v[1], "engagement_index": num(v[2], 2)}
               for v in conn.execute("""SELECT wave, response_status, engagement_index FROM core.engagement_survey
                                        WHERE employee_id = %s AND recorded_at <= %s ORDER BY wave""", (emp, at))]
    second = conn.execute("""SELECT z.rank_in_run, z.priority_band, z.contributions FROM app.score z
                             JOIN app.score_run r USING (score_run_id)
                             WHERE z.employee_id = %s AND r.scorer_type = 'scorecard' AND r.as_of_date = %s""",
                          (emp, s[9])).fetchone()
    reviews, ivs = reviews_and_interventions(conn, score_id)
    active = gate.active_run(conn)
    return {
        "score_id": s[0], "employee_id": emp, "score_run_id": s[2], "is_active_run": s[2] == active,
        "as_of_date": iso(s[9]), "model_version": s[10], "scorer_type": s[11],
        "risk_score": num(s[3], 4), "rank": s[4], "population": s[12], "band": s[5],
        "contributions": s[6], "coverage": s[7], "source_rows": s[8], "themes": themes(s[6]),
        "context": employee_context(conn, emp, at),
        "tenure": {"tenure_months": num(snap[0], 1), "months_in_role": num(snap[1], 1),
                   "months_since_promotion": num(snap[2], 1)} if snap else None,
        "workload": workload, "surveys": surveys,
        "scorecard": {"rank": second[0], "band": second[1], "points": second[2]} if second else None,
        "eligibility": gate.ineligible_reason(conn, emp, at),
        "reviews": reviews, "interventions": ivs, "reason_codes": gate.REASONS,
        "intervention_types": gate.INTERVENTION_TYPES, "purpose": PURPOSE,
    }


def case_for_manager(conn, score_id: str, at: dt.datetime) -> dict | None:
    """Reduced view: why HR suggested a conversation, how to hold it, and the action log. No model numbers."""
    s = _score_row(conn, score_id)
    if s is None:
        return None
    reviews, ivs = reviews_and_interventions(conn, score_id)
    approved = [r for r in reviews if r["decision"] == "approved"]
    ctx = employee_context(conn, s[1], at)
    snap = conn.execute("""SELECT tenure_months, months_in_role FROM core.tenure_snapshot
                           WHERE employee_id = %s AND as_of_date = %s""", (s[1], s[9])).fetchone()
    return {"score_id": s[0], "employee_id": s[1], "as_of_date": iso(s[9]),
            "context": {k: ctx.get(k) for k in ("team_name", "role_title", "job_family", "hire_date")},
            "tenure_months": num(snap[0], 1) if snap else None, "months_in_role": num(snap[1], 1) if snap else None,
            "themes": manager_topics(s[6]), "approved_by": approved[-1]["reviewer"] if approved else None,
            "approved_reason": approved[-1]["reason_code"] if approved else None,
            "interventions": ivs, "intervention_types": gate.INTERVENTION_TYPES,
            "eligibility": gate.ineligible_reason(conn, s[1], at), "purpose": PURPOSE}


def manager_cases(conn, user: User, at: dt.datetime) -> dict:
    team = gate.manager_team(conn, user, at)
    run = gate.active_run(conn)
    rows = conn.execute(f"""
        SELECT s.score_id, s.employee_id, s.contributions, {review_status_sql()} AS review, {intervention_status_sql()} AS iv
        FROM app.score s WHERE s.score_run_id = %(run)s AND s.employee_id <> %(me)s
          AND (SELECT team_id FROM core.assignment a WHERE a.employee_id = s.employee_id AND a.effective_from <= %(d)s
               AND a.recorded_at <= %(t)s ORDER BY a.effective_from DESC LIMIT 1) = %(team)s
        ORDER BY s.employee_id""",
                        {"run": run, "me": user.employee_id, "d": at.date(), "t": at, "team": team}).fetchall()
    items = [{"score_id": r[0], "employee_id": r[1], "themes": [t["text"] for t in manager_topics(r[2])],
              "intervention": r[4], "context": employee_context(conn, r[1], at)}
             for r in rows if r[3] == "approved"]
    team_name = conn.execute("SELECT team_name FROM core.team WHERE team_id = %s", (team,)).fetchone() if team else None
    return {"team_id": team, "team_name": team_name[0] if team_name else None, "scored_in_team": len(rows), "items": items,
            "purpose": PURPOSE}


def interventions(conn, user: User, at: dt.datetime, status: str | None) -> list[dict]:
    rows = conn.execute(f"""
        SELECT i.intervention_id, i.employee_id, i.score_id, i.intervention_type, i.arm, i.source, i.assigned_at,
               u.display_name,
               (SELECT to_status FROM app.intervention_event ev WHERE ev.intervention_id = i.intervention_id
                ORDER BY ev.event_at DESC, ev.event_id DESC LIMIT 1) AS status,
               (SELECT max(event_at) FROM app.intervention_event ev WHERE ev.intervention_id = i.intervention_id)
        FROM app.intervention i JOIN app.app_user u ON u.user_id = i.owner_user_id
        JOIN app.score s ON s.score_id = i.score_id
        WHERE s.score_run_id = %s ORDER BY i.assigned_at DESC, i.intervention_id DESC""",
                        (gate.active_run(conn),)).fetchall()
    if user.role == "manager":
        team = gate.manager_team(conn, user, at)
        rows = [r for r in rows if team and gate.team_of(conn, r[1], at) == team]
    out = [{"intervention_id": r[0], "employee_id": r[1], "score_id": r[2], "type": r[3], "arm": r[4], "source": r[5],
            "assigned_at": iso(r[6]), "owner": r[7], "status": r[8], "updated_at": iso(r[9])} for r in rows]
    return [o for o in out if not status or o["status"] == status]


def overview(conn) -> dict:
    run = gate.active_run(conn)
    bands = dict(conn.execute("SELECT priority_band, count(*) FROM app.score WHERE score_run_id = %s GROUP BY 1",
                              (run,)).fetchall())
    reviews = dict(conn.execute(f"""SELECT coalesce({review_status_sql()}, 'pending'), count(*) FROM app.score s
                                    WHERE s.score_run_id = %s AND s.priority_band = 'suggested_checkin' GROUP BY 1""",
                                (run,)).fetchall())
    ivs = dict(conn.execute("""SELECT st, count(*) FROM (
                                 SELECT (SELECT to_status FROM app.intervention_event ev WHERE ev.intervention_id = i.intervention_id
                                         ORDER BY ev.event_at DESC, ev.event_id DESC LIMIT 1) st
                                 FROM app.intervention i JOIN app.score s USING (score_id) WHERE s.score_run_id = %s) x
                               GROUP BY 1""", (run,)).fetchall())
    arms = dict(conn.execute("""SELECT arm, count(*) FROM app.intervention i JOIN app.score s USING (score_id)
                                WHERE s.score_run_id = %s GROUP BY 1""", (run,)).fetchall())
    fam = [{"job_family": r[0], "suggested": r[1], "scored": r[2]} for r in conn.execute("""
        SELECT r.job_family, count(*) FILTER (WHERE s.priority_band = 'suggested_checkin'), count(*)
        FROM app.score s JOIN core.tenure_snapshot ts ON ts.employee_id = s.employee_id
             AND ts.as_of_date = (SELECT as_of_date FROM app.score_run WHERE score_run_id = s.score_run_id)
        JOIN core.assignment a ON a.assignment_id = ts.assignment_id JOIN core.role r ON r.role_id = a.role_id
        WHERE s.score_run_id = %s GROUP BY 1 ORDER BY 2 DESC""", (run,))]
    run_row = conn.execute("SELECT as_of_date, model_version, executed_at FROM app.score_run WHERE score_run_id = %s",
                           (run,)).fetchone()
    open_exc = conn.execute("SELECT count(*) FROM app.exception_log WHERE status = 'open'").fetchone()[0]
    return {"score_run_id": run, "as_of_date": iso(run_row[0]), "model_version": run_row[1],
            "executed_at": iso(run_row[2]), "bands": bands, "reviews": reviews, "interventions": ivs,
            "arms": arms, "families": fam, "open_exceptions": open_exc}


def exceptions(conn, issue_type: str | None, entity: str | None, limit: int, offset: int) -> dict:
    where, p = [], {}
    if issue_type:
        where.append("issue_type = %(it)s"); p["it"] = issue_type
    if entity:
        where.append("entity = %(en)s"); p["en"] = entity
    w = ("WHERE " + " AND ".join(where)) if where else ""
    counts = [{"issue_type": r[0], "entity": r[1], "n": r[2]} for r in conn.execute(
        "SELECT issue_type, entity, count(*) FROM app.exception_log GROUP BY 1, 2 ORDER BY 1, 2")]
    total = conn.execute(f"SELECT count(*) FROM app.exception_log {w}", p).fetchone()[0]
    rows = conn.execute(f"""SELECT exception_id, entity, record_id, issue_type, detector, detail, detected_at, status
                            FROM app.exception_log {w} ORDER BY detected_at DESC, exception_id DESC
                            LIMIT %(lim)s OFFSET %(off)s""", {**p, "lim": limit, "off": offset}).fetchall()
    keys = ["exception_id", "entity", "record_id", "issue_type", "detector", "detail", "detected_at", "status"]
    return {"counts": counts, "total": total, "items": [{k: iso(v) for k, v in zip(keys, r)} for r in rows]}


def runs(conn) -> list[dict]:
    return [{"score_run_id": r[0], "as_of_date": iso(r[1]), "scorer_type": r[2], "model_version": r[3],
             "config_hash": r[4], "model_frozen_at": iso(r[5]), "executed_at": iso(r[6]), "scored": r[7],
             "suggested": r[8]} for r in conn.execute("""
        SELECT r.score_run_id, r.as_of_date, r.scorer_type, r.model_version, r.config_hash, r.model_frozen_at, r.executed_at,
               count(s.*), count(s.*) FILTER (WHERE s.priority_band = 'suggested_checkin')
        FROM app.score_run r LEFT JOIN app.score s USING (score_run_id) GROUP BY r.score_run_id ORDER BY r.executed_at DESC, 1""")]


def users(conn, at: dt.datetime) -> list[dict]:
    return [{"user_id": r[0], "username": r[1], "display_name": r[2], "role": r[3], "employee_id": r[4],
             "is_active": r[5], "active_sessions": r[6]} for r in conn.execute("""
        SELECT u.user_id, u.username, u.display_name, u.app_role, u.employee_id, u.is_active,
               (SELECT count(*) FROM app.user_session s WHERE s.user_id = u.user_id AND s.revoked_at IS NULL
                AND s.expires_at > %s)
        FROM app.app_user u ORDER BY u.user_id""", (at,))]


def read_json(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
