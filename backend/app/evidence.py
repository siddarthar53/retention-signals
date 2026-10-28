"""HR-only evidence view: every source record behind one score, as it was at the score's as-of date.

Safety rules (agreed with ChatGPT/Codex, scope addendum v1):
  * Table names in app.score.source_refs are never put into SQL. Each reference is matched against the fixed
    allowlist below, which names the table, key column and the model-visible columns that may be shown.
    Unknown tables are reported as rejected, not queried.
  * Every row must belong to the scored employee and must have been available at the SCORE's cutoff
    (event/period date <= as_of and recorded_at on or before as_of), not at today's business time.
  * Only allowed values are returned: never sick leave, pay, raw survey items, outcomes or treatment truth.
  * Dependencies follow pipeline/features.py exactly; a missing record is shown as unavailable, never
    replaced with newer history.
"""
from __future__ import annotations

import datetime as dt

from .queries import iso, num

# table -> (key column, date column used for availability, [allowed display columns])
ALLOWLIST = {
    "core.tenure_snapshot": ("snapshot_id", "as_of_date", ["as_of_date", "tenure_months", "months_in_role",
                                                           "months_since_promotion"]),
    "core.workload_snapshot": ("workload_id", "period_month", ["period_month", "utilization_pct", "after_hours_pct",
                                                               "oncall_days"]),
    "core.engagement_survey": ("survey_id", "sent_date", ["wave", "sent_date", "response_status", "engagement_index"]),
    "core.assignment": ("assignment_id", "effective_from", ["effective_from", "change_reason"]),
    # snapshot inputs (derived_from): shown as lineage of the snapshot, not as direct model inputs
    "core.promotion_history": ("promotion_id", "effective_date", ["effective_date"]),
    "core.employment_event": ("event_id", "event_date", ["event_type", "event_date"]),
}
DERIVED_KEYS = {"promotion": "core.promotion_history", "assignment": "core.assignment", "hire_event": "core.employment_event"}
WHAT = {
    "core.tenure_snapshot": "Quarter-end tenure snapshot", "core.workload_snapshot": "Monthly workload record",
    "core.engagement_survey": "Engagement survey", "core.assignment": "Assignment",
    "core.promotion_history": "Promotion", "core.employment_event": "Employment event",
}
SNAPSHOT_FEATURES = ["tenure_months", "months_in_role", "promo_clock_over_24", "new_joiner"]
WORKLOAD_MEANS = ["util_mean_3m", "after_hours_mean_3m", "oncall_mean_3m"]


def _fetch(conn, table: str, rid: str):
    key, date_col, cols = ALLOWLIST[table]
    sql = f"SELECT employee_id, {date_col}, recorded_at, {', '.join(cols)} FROM {table} WHERE {key} = %s"
    row = conn.execute(sql, (rid,)).fetchone()        # table/columns come from ALLOWLIST only
    if row is None:
        return None
    return {"employee_id": row[0], "date": row[1], "recorded_at": row[2], "values": dict(zip(cols, row[3:]))}


def _record(conn, table: str, rid: str, emp: str, as_of: dt.date) -> dict:
    out = {"table": table, "id": rid, "what": WHAT.get(table, table), "used_for": [], "values": {},
           "recorded_at": None, "status": "ok"}
    if table not in ALLOWLIST:
        out["status"] = "rejected: table not on the evidence allowlist"
        return out
    row = _fetch(conn, table, rid)
    if row is None:
        out["status"] = "missing: record not found"
        return out
    if row["employee_id"] != emp:
        out["status"] = "rejected: record belongs to another employee"
        return out
    out["recorded_at"] = iso(row["recorded_at"])
    if row["date"] > as_of or row["recorded_at"].date() > as_of:
        out["status"] = "not available at the score date"      # should never happen; flagged, values withheld
        return out
    out["values"] = {k: (num(v, 2) if not isinstance(v, (str, dt.date)) and v is not None else iso(v))
                     for k, v in row["values"].items()}
    return out


def evidence(conn, score_id: str) -> dict | None:
    s = conn.execute("""SELECT s.employee_id, s.source_refs, s.contributions, r.as_of_date, r.scorer_type
                        FROM app.score s JOIN app.score_run r USING (score_run_id) WHERE s.score_id = %s""",
                     (score_id,)).fetchone()
    if s is None:
        return None
    emp, refs, contribs, as_of, scorer = s
    labels = {c["feature"]: c.get("label", c["feature"]) for c in contribs if "feature" in c}
    recs = [_record(conn, r.get("table"), r.get("id"), emp, as_of) for r in refs]
    gaps: list[str] = []

    snaps = [r for r in recs if r["table"] == "core.tenure_snapshot"]
    for r in snaps:
        r["used_for"] = SNAPSHOT_FEATURES
        derived = next((x.get("derived_from") for x in refs if x.get("id") == r["id"]), None) or {}
        r["inputs"] = [_record(conn, DERIVED_KEYS[k], v, emp, as_of) for k, v in sorted(derived.items())
                       if k in DERIVED_KEYS and v]
    if not snaps:
        gaps.append("No tenure snapshot for this date: tenure features are unavailable.")

    wl = sorted((r for r in recs if r["table"] == "core.workload_snapshot" and r["status"] == "ok"),
                key=lambda r: r["values"].get("period_month") or "")
    for i, r in enumerate(wl):
        last3 = i >= len(wl) - 3
        r["used_for"] = (WORKLOAD_MEANS + ["sustained_overload"]) if last3 else ["sustained_overload"]
    if len(wl) < 3:
        gaps.append(f"Only {len(wl)} workload month(s) recorded by {as_of}: the 3-month averages use what exists.")
    if len(wl) < 4:
        gaps.append("Fewer than 4 workload months: sustained overload is judged on the months available.")

    sv = sorted((r for r in recs if r["table"] == "core.engagement_survey" and r["status"] == "ok"),
                key=lambda r: r["values"].get("sent_date") or "")
    answered = [r for r in sv if r["values"].get("engagement_index") is not None]
    if answered:
        answered[-1]["used_for"] += ["engagement_latest", "engagement_decline"]
    if len(answered) >= 2:
        answered[-2]["used_for"] += ["engagement_decline"]
    if sv:
        sv[-1]["used_for"] += ["coverage: latest survey status"]
    if not answered:
        gaps.append("No answered survey by this date: engagement uses the neutral training reference, not a low score.")
    elif len(answered) == 1:
        gaps.append("Only one answered survey: the drop in engagement cannot be measured and counts as none.")

    tr = [r for r in recs if r["table"] == "core.assignment"]
    for r in tr:
        r["used_for"] = ["recent_transfer"]
    if not tr:
        gaps.append(f"No transfer recorded in the 182 days before {as_of}: 'transferred recently' is no.")

    for r in recs:
        uf = r["used_for"]
        if all(m in uf for m in WORKLOAD_MEANS):          # one readable line for the three 3-month averages
            uf = ["3-month averages (utilisation, after-hours, on-call)"] + [f for f in uf if f not in WORKLOAD_MEANS]
        r["used_for_labels"] = [labels.get(f, f.replace("coverage: ", "Coverage note, not weighted: ")) for f in uf]
    return {"score_id": score_id, "employee_id": emp, "as_of_date": iso(as_of), "scorer_type": scorer,
            "records": recs, "gaps": gaps,
            "note": "Records as they stood on the score date. Later changes are not shown here and were not used."}
