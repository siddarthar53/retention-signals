"""As-of feature builder (G4).

Source allowlist (nothing else is read): core.employee, core.employment_event, core.assignment,
core.tenure_snapshot, core.workload_snapshot (utilisation, after-hours, on-call only),
core.engagement_survey (status and engagement index only). Never read: eval.*, core.attrition_outcome,
core.leave_ledger (EL is context only, SL restricted), core.compensation_band, team/manager IDs.

Every source row is filtered with pipeline.asof.is_available (event date AND recorded_at <= as_of)
BEFORE any aggregation. Missing survey data is never converted into a low engagement value: the
feature stays NaN and is reported as coverage; the model replaces it with a neutral training reference.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

ALLOWED_SOURCES = {
    "employee": "SELECT employee_id, hire_date FROM core.employee",
    "events": "SELECT event_id, employee_id, event_type, event_date, recorded_at FROM core.employment_event",
    "assignments": "SELECT assignment_id, employee_id, change_reason, effective_from, recorded_at FROM core.assignment",
    "snapshots": """SELECT snapshot_id, employee_id, as_of_date, tenure_months, months_in_role,
                           months_since_promotion, derived_from, recorded_at FROM core.tenure_snapshot""",
    "workload": """SELECT workload_id, employee_id, period_month, utilization_pct, after_hours_pct, oncall_days,
                          recorded_at FROM core.workload_snapshot""",
    "surveys": """SELECT survey_id, employee_id, wave, sent_date, response_status, engagement_index, recorded_at
                  FROM core.engagement_survey""",
}

FEATURES = ["engagement_latest", "engagement_decline", "util_mean_3m", "sustained_overload",
            "after_hours_mean_3m", "oncall_mean_3m", "promo_clock_over_24", "months_in_role",
            "tenure_months", "new_joiner", "recent_transfer"]
OVERLOAD_UTIL = 115.0


@dataclass
class Sources:
    tables: dict

    @classmethod
    def from_db(cls, conn) -> "Sources":
        out = {}
        for name, q in ALLOWED_SOURCES.items():
            cur = conn.execute(q)
            out[name] = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
        return cls(_typed(out))


def _typed(t: dict) -> dict:
    for name, df in t.items():
        for c in df.columns:
            if c in ("hire_date", "event_date", "effective_from", "as_of_date", "period_month", "sent_date"):
                df[c] = pd.to_datetime(df[c]).dt.date
            elif c == "recorded_at":
                ts = pd.to_datetime(df[c], utc=True)
                df["recorded_ts"] = ts.dt.strftime("%Y-%m-%dT%H:%M:%SZ")     # exact time, for lineage only
                df[c] = ts.dt.date
            elif c in ("tenure_months", "months_in_role", "months_since_promotion", "utilization_pct",
                       "after_hours_pct", "engagement_index"):
                df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return t


def scoring_population(src: Sources, as_of: dt.date) -> pd.DataFrame:
    """Employed on as_of, no resignation known at as_of, hired on or before the quarter's first day."""
    emp = src.tables["employee"]
    ev = src.tables["events"]
    ev = ev[(ev.event_date <= as_of) & (ev.recorded_at <= as_of)]
    exited = set(ev.loc[ev.event_type == "exit", "employee_id"])
    resigned = set(ev.loc[ev.event_type == "resignation_submitted", "employee_id"])
    q_start = dt.date(as_of.year, as_of.month - 2, 1)
    pop = emp[(emp.hire_date <= q_start) & ~emp.employee_id.isin(exited) & ~emp.employee_id.isin(resigned)]
    return pop[["employee_id"]].sort_values("employee_id").reset_index(drop=True)


def build(src: Sources, as_of: dt.date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (features, lineage) with exactly one row per employee in the scoring population."""
    pop = scoring_population(src, as_of)
    keys = pop.copy()

    snap = src.tables["snapshots"]
    snap = snap[(snap.as_of_date == as_of) & (snap.recorded_at <= as_of)]
    f = keys.merge(snap[["employee_id", "snapshot_id", "tenure_months", "months_in_role", "months_since_promotion"]],
                   on="employee_id", how="left", validate="one_to_one")
    clock = f["months_since_promotion"].fillna(f["tenure_months"])        # promotion clock: never NULL
    f["promo_clock_over_24"] = (clock - 24).clip(lower=0)
    f["new_joiner"] = (f["tenure_months"] < 12).astype(float)

    wl = src.tables["workload"]
    wl = wl[(wl.period_month <= as_of) & (wl.recorded_at <= as_of) & wl.employee_id.isin(keys.employee_id)]
    wl = wl.sort_values(["employee_id", "period_month"])
    last3 = wl.groupby("employee_id").tail(3)
    last4 = wl.groupby("employee_id").tail(4)
    agg3 = last3.groupby("employee_id").agg(util_mean_3m=("utilization_pct", "mean"),
                                             after_hours_mean_3m=("after_hours_pct", "mean"),
                                             oncall_mean_3m=("oncall_days", "mean"),
                                             workload_months=("workload_id", "count"))
    over = last4.assign(o=last4.utilization_pct > OVERLOAD_UTIL).groupby("employee_id")["o"].sum()
    f = f.merge(agg3, on="employee_id", how="left", validate="one_to_one")
    f["sustained_overload"] = f["employee_id"].map(over).fillna(0).ge(3).astype(float)

    sv = src.tables["surveys"]
    sv = sv[(sv.sent_date <= as_of) & (sv.recorded_at <= as_of) & sv.employee_id.isin(keys.employee_id)]
    sv = sv.sort_values(["employee_id", "sent_date"])
    last_status = sv.groupby("employee_id")["response_status"].last()
    answered = sv[sv.engagement_index.notna()]
    latest = answered.groupby("employee_id").tail(1).set_index("employee_id")
    prev = answered.groupby("employee_id").nth(-2).set_index("employee_id")
    f["engagement_latest"] = f["employee_id"].map(latest["engagement_index"])
    f["engagement_decline"] = (f["employee_id"].map(prev["engagement_index"]) - f["engagement_latest"]).clip(lower=0)
    f["latest_survey_status"] = f["employee_id"].map(last_status)
    last_any = sv.groupby("employee_id").tail(1).set_index("employee_id")

    asg = src.tables["assignments"]
    recent = asg[(asg.change_reason == "transfer") & (asg.effective_from <= as_of) & (asg.recorded_at <= as_of)
                 & (asg.effective_from > as_of - dt.timedelta(days=182))]
    f["recent_transfer"] = f["employee_id"].isin(recent.employee_id).astype(float)

    # Lineage: every row each feature (and coverage note) was computed from.
    #   tenure, months_in_role, promo_clock, new_joiner  <- snapshot (its own derived_from lists the
    #                                                       events/assignments/promotions behind it)
    #   util/after-hours/on-call means                   <- last 3 workload rows; sustained_overload <- last 4
    #   engagement_latest / decline                      <- latest and previous ANSWERED surveys
    #   last_wave_no_response (coverage)                 <- latest survey of any status
    #   recent_transfer                                  <- transfer assignments in the 182-day window
    wl_refs = last4.groupby("employee_id")["workload_id"].agg(list)
    sv_refs = pd.concat([latest["survey_id"], prev["survey_id"], last_any["survey_id"]]).groupby(level=0).agg(
        lambda s: sorted(set(s)))
    tr_refs = recent[recent.employee_id.isin(keys.employee_id)].groupby("employee_id")["assignment_id"].agg(list)

    f.insert(1, "as_of_date", as_of)
    coverage = pd.DataFrame({
        "employee_id": f.employee_id,
        "no_answered_survey": f.engagement_latest.isna(),
        "last_wave_no_response": f.latest_survey_status.eq("no_response"),
        "workload_months_available": f.workload_months.fillna(0).astype(int),
    })
    lineage = pd.DataFrame({"employee_id": f.employee_id, "snapshot_id": f.snapshot_id,
                            "workload_ids": f.employee_id.map(wl_refs), "survey_ids": f.employee_id.map(sv_refs),
                            "transfer_ids": f.employee_id.map(tr_refs)})
    out = f[["employee_id", "as_of_date"] + FEATURES].reset_index(drop=True)
    if out.employee_id.duplicated().any() or len(out) != len(pop):
        raise AssertionError("join contract broken: features must have exactly one row per scoring key")
    return out, coverage.merge(lineage, on="employee_id").reset_index(drop=True)
