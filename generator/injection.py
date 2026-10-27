"""Labelled anomaly injection (design v0.2 section 7, amended after the independent review).

Fixtures are separate files; canonical data is never modified. Every injected case has a ground-truth
row; every type has clean controls that must NOT be flagged (is_control = true). Cases and controls
are drawn in random order from one shuffled pool (no confounding by family or tenure), and boundary
variants sit on both sides of each rule's edge.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json

from .core import add_months
from .derived import JUNE_2026, QUARTER_ENDS, horizon_end

RULE_VERSION = "inject-v2"
TRAIN_TEST_DATES = [dt.date(2025, 3, 31), dt.date(2025, 6, 30), dt.date(2025, 9, 30), dt.date(2026, 6, 30)]


def _csv(header, rows) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows([["" if v is None else v for v in r] for r in rows])
    return buf.getvalue().encode("utf-8")


def _shuffled(rng, items):
    return [items[i] for i in rng.permutation(len(items))] if items else []


def inject(ds, rng) -> None:
    g = ds.gcfg["injection"]
    reg = ds.reg
    scored = [lb for lb in ds.labels if lb.as_of in TRAIN_TEST_DATES and lb.status not in ("ineligible", "unresolved")]
    k = max(int(g["min_cases_per_type"]), round(g["rate"] * len(scored)))
    third = max(1, k // 3)
    gt, case_no = [], 0

    def add(entity, record_id, issue, expected, control, variant):
        nonlocal case_no
        case_no += 1
        gt.append([f"GT-{len(gt) + 1:06d}", f"CASE-{case_no:05d}-{variant}", entity, record_id, issue, expected,
                   control, RULE_VERSION])

    # ---------------- FUTURE_LEAK: a feature snapshot for as_of includes a source row (feature_sources.csv)
    wl_by = {(w.employee, w.period_month): w for w in reg.workload}
    sv_by = {(s.employee, s.sent_date): s for s in reg.surveys}
    pool = _shuffled(rng, scored)
    leak_rows, used = [], 0

    def leak(lb, table, sid, event_date, recorded, issue_expected, control, variant):
        leak_rows.append([lb.employee.id, lb.as_of, table, sid, event_date, recorded, variant])
        add("feature_source", f"{lb.employee.id}@{lb.as_of}:{sid}:{variant}", "FUTURE_LEAK", issue_expected,
            control, variant)

    for lb in pool:
        if used >= 2 * k:
            break
        m0 = dt.date(lb.as_of.year, lb.as_of.month, 1)
        cur, nxt, prev = wl_by.get((lb.employee, m0)), wl_by.get((lb.employee, add_months(m0, 1))), \
            wl_by.get((lb.employee, add_months(m0, -1)))
        sv = sv_by.get((lb.employee, dt.date(lb.as_of.year, lb.as_of.month, 15)))
        i = used % 6
        if i == 0 and cur:          # real row: event on/before as_of, recorded 3-7 days after
            leak(lb, "core.workload_snapshot", cur.id, cur.period_month, cur.recorded_at, "reject_feature", False, "late_recorded")
        elif i == 1 and nxt:        # real row: event after as_of
            leak(lb, "core.workload_snapshot", nxt.id, nxt.period_month, nxt.recorded_at, "reject_feature", False, "future_event")
        elif i == 2 and sv:         # boundary: recorded one second after the end of as_of
            leak(lb, "core.engagement_survey", sv.id, sv.sent_date, f"{lb.as_of + dt.timedelta(days=1)} 00:00:01+00",
                 "reject_feature", False, "one_second_late")
        elif i == 3 and sv:         # control at the boundary: recorded at 23:59:59 on as_of
            leak(lb, "core.engagement_survey", sv.id, sv.sent_date, f"{lb.as_of} 23:59:59+00", "none", True, "at_cutoff")
        elif i == 4 and prev:       # control: previous month's workload, recorded before as_of
            leak(lb, "core.workload_snapshot", prev.id, prev.period_month, prev.recorded_at, "none", True, "prior_month")
        elif i == 5 and sv and sv.recorded_at[:10] <= lb.as_of.isoformat():   # control: real survey recorded on time
            leak(lb, "core.engagement_survey", sv.id, sv.sent_date, sv.recorded_at, "none", True, "on_time")
        else:
            continue
        used += 1

    # ---------------- MISSING_AS_NEGATIVE: (a) DB survey rows; (b) feature-level imputation
    no_resp = _shuffled(rng, [s for s in reg.surveys if s.response_status == "no_response"])
    survey_rows = []
    for s in no_resp[:k]:
        survey_rows.append([s.id, s.employee.id, s.wave, s.sent_date, "no_response", *s.items, 0, "1.00", None,
                            s.recorded_at])
        add("core.engagement_survey", s.id, "MISSING_AS_NEGATIVE", "quarantine_row", False, "nonresponse_scored")
    partial3 = _shuffled(rng, [s for s in reg.surveys if s.response_status == "partial"
                               and sum(v is not None for v in s.items) >= 3])
    for s in partial3[:k]:
        survey_rows.append([s.id, s.employee.id, s.wave, s.sent_date, s.response_status, *s.items,
                            sum(v is not None for v in s.items), s.engagement_index, s.submitted_at, s.recorded_at])
        add("core.engagement_survey", s.id, "MISSING_AS_NEGATIVE", "none", True, "partial_three_items")
    feat_rows = []
    for s in no_resp[k:2 * k]:
        feat_rows.append([s.employee.id, s.sent_date, "no_response", "1.0"])
        add("feature_engagement", f"{s.employee.id}@{s.sent_date}", "MISSING_AS_NEGATIVE", "reject_feature", False,
            "imputed_low")
    for s in no_resp[2 * k:3 * k]:
        feat_rows.append([s.employee.id, s.sent_date, "no_response", None])
        add("feature_engagement", f"{s.employee.id}@{s.sent_date}", "MISSING_AS_NEGATIVE", "none", True, "left_missing")

    # ---------------- LABEL_DATE_MISMATCH (eval.horizon_label-shaped rows)
    label_rows = []
    resolved = [lb for lb in ds.labels if lb.target is not None]
    zeros = _shuffled(rng, [lb for lb in resolved if lb.status == "employed_through_horizon"])
    ones = _shuffled(rng, [lb for lb in resolved if lb.status == "voluntary_exit"])

    def lab(lb_emp, as_of, status, target, outcome, control, variant, reason=None):
        label_rows.append([lb_emp.id, as_of, horizon_end(as_of), status, reason, target, outcome.id])
        add("eval.horizon_label", f"{lb_emp.id}@{as_of}", "LABEL_DATE_MISMATCH", "none" if control else "flag_label",
            control, variant)

    taken = set()
    for lb in zeros[:third]:
        lab(lb.employee, lb.as_of, "voluntary_exit", 1, lb.outcome, False, "stayed_labelled_exit"); taken.add(id(lb))
    for lb in ones[:third]:
        lab(lb.employee, lb.as_of, "employed_through_horizon", 0, lb.outcome, False, "left_labelled_stayed"); taken.add(id(lb))
    for lb in ones[third:2 * third]:
        lab(lb.employee, lb.as_of, "voluntary_exit", 0, lb.outcome, False, "target_contradicts_status"); taken.add(id(lb))
    gone = _shuffled(rng, [o for o in ds.outcomes.values() if o.termination_date is not None
                           and o.termination_date <= dt.date(2026, 6, 29)])
    for o in gone[:third]:                     # labelled at a quarter-end on or after termination
        q = next(q for q in QUARTER_ENDS if q >= o.termination_date)
        lab(o.employee, q, "employed_through_horizon", 0, o, False, "not_employed_at_as_of")
    # controls: correct labels whose termination is closest to the horizon edge, on both sides
    near = sorted((lb for lb in resolved if id(lb) not in taken and lb.outcome.termination_date is not None
                   and lb.status in ("voluntary_exit", "employed_through_horizon")),
                  key=lambda lb: (abs((lb.outcome.termination_date - lb.horizon_end).days), lb.employee.id, lb.as_of))
    for lb in near[:k]:
        lab(lb.employee, lb.as_of, lb.status, lb.target, lb.outcome, True,
            "edge_exit" if lb.target == 1 else "edge_after_horizon")

    # ---------------- UNREVIEWED_SCORE: intervention request templates (enforced at G5)
    june = _shuffled(rng, [lb for lb in ds.labels if lb.as_of == JUNE_2026 and lb.target is not None])
    variants = ["no_review", "dismissed_review", "stale_score_version", "other_team"]
    requests = []
    for i, lb in enumerate(june[:2 * k]):
        variant = variants[i % 4] if i < k else "valid"
        expected = "none" if variant == "valid" else "block_action"
        requests.append({"case_ref": f"REQ-{i + 1:05d}", "employee_id": lb.employee.id,
                         "as_of_date": lb.as_of.isoformat(), "variant": variant, "expected": expected,
                         "job_family": lb.employee.current.team.job_family})
        add("api.intervention_request", f"REQ-{i + 1:05d}", "UNREVIEWED_SCORE", expected, variant == "valid", variant)

    ds.ground_truth = gt
    ds.fixtures = {
        "fixtures/feature_sources.csv": _csv(["employee_id", "as_of_date", "source_table", "source_id",
                                              "event_date", "recorded_at", "variant"], leak_rows),
        "fixtures/core.engagement_survey.csv": _csv(
            ["survey_id", "employee_id", "wave", "sent_date", "response_status", "q1", "q2", "q3", "q4", "q5",
             "items_answered", "engagement_index", "submitted_at", "recorded_at"], survey_rows),
        "fixtures/feature_engagement.csv": _csv(["employee_id", "survey_sent_date", "survey_status",
                                                 "engagement_feature"], feat_rows),
        "fixtures/eval.horizon_label.csv": _csv(["employee_id", "as_of_date", "horizon_end", "label_status",
                                                 "ineligible_reason", "target", "outcome_id"], label_rows),
        "fixtures/intervention_requests.json": (json.dumps(requests, indent=1) + "\n").encode("utf-8"),
    }
