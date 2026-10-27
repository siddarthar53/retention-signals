"""Labelled anomaly injection (design v0.2 section 7). Fixtures are separate files; canonical data is
never modified. Every injected case has a ground-truth row; every type also has clean controls that
must NOT be flagged (is_control = true, expected_detection = none)."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json

from .derived import JUNE_2026

RULE_VERSION = "inject-v1"
TRAIN_TEST_DATES = [dt.date(2025, 3, 31), dt.date(2025, 6, 30), dt.date(2025, 9, 30), dt.date(2026, 6, 30)]


def _csv(header, rows) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows([["" if v is None else v for v in r] for r in rows])
    return buf.getvalue().encode("utf-8")


def _pick(rng, items, k):
    if not items:
        return []
    idx = sorted(rng.choice(len(items), size=min(k, len(items)), replace=False).tolist())
    return [items[i] for i in idx]


def inject(ds, rng) -> None:
    g = ds.gcfg["injection"]
    reg = ds.reg
    scored = [lb for lb in ds.labels if lb.as_of in TRAIN_TEST_DATES and lb.status not in ("ineligible", "unresolved")]
    k = max(int(g["min_cases_per_type"]), round(g["rate"] * len(scored)))
    gt, case_no = [], 0

    def add(entity, record_id, issue, expected, control):
        nonlocal case_no
        case_no += 1
        gt.append([f"GT-{len(gt) + 1:06d}", f"CASE-{case_no:05d}", entity, record_id, issue, expected,
                   control, RULE_VERSION])

    # ---- FUTURE_LEAK: feature snapshot includes the as_of month's workload (recorded after the cutoff)
    wl_by = {(w.employee, w.period_month): w for w in reg.workload}
    sv_by = {(s.employee, s.sent_date): s for s in reg.surveys}
    leak_rows = []
    for lb in _pick(rng, scored, k):
        w = wl_by.get((lb.employee, dt.date(lb.as_of.year, lb.as_of.month, 1)))
        if w is None:
            continue
        leak_rows.append([lb.employee.id, lb.as_of, "core.workload_snapshot", w.id, w.period_month, w.recorded_at])
        add("feature_source", f"{lb.employee.id}@{lb.as_of}:{w.id}", "FUTURE_LEAK", "reject_feature", False)
    for lb in _pick(rng, scored, k):                      # controls: same-quarter survey recorded by as_of
        s = sv_by.get((lb.employee, dt.date(lb.as_of.year, lb.as_of.month, 15)))
        if s is None or s.recorded_at[:10] > lb.as_of.isoformat():
            continue
        leak_rows.append([lb.employee.id, lb.as_of, "core.engagement_survey", s.id, s.sent_date, s.recorded_at])
        add("feature_source", f"{lb.employee.id}@{lb.as_of}:{s.id}", "FUTURE_LEAK", "none", True)

    # ---- MISSING_AS_NEGATIVE: (a) DB-level survey rows; (b) feature-level imputation
    no_resp = [s for s in reg.surveys if s.response_status == "no_response"]
    survey_rows = []
    for s in _pick(rng, no_resp, k):
        survey_rows.append([s.id, s.employee.id, s.wave, s.sent_date, "no_response", *s.items, 0, "1.00", None,
                            s.recorded_at])
        add("core.engagement_survey", s.id, "MISSING_AS_NEGATIVE", "quarantine_row", False)
    partial3 = [s for s in reg.surveys if s.response_status == "partial" and sum(v is not None for v in s.items) >= 3]
    for s in _pick(rng, partial3 or [s for s in reg.surveys if s.response_status == "complete"], k):
        survey_rows.append([s.id, s.employee.id, s.wave, s.sent_date, s.response_status, *s.items,
                            sum(v is not None for v in s.items), s.engagement_index, s.submitted_at, s.recorded_at])
        add("core.engagement_survey", s.id, "MISSING_AS_NEGATIVE", "none", True)
    feat_rows = []
    feat_cases = _pick(rng, no_resp, k)
    for s in feat_cases:
        feat_rows.append([s.employee.id, s.sent_date, "no_response", "1.0"])
        add("feature_engagement", f"{s.employee.id}@{s.sent_date}", "MISSING_AS_NEGATIVE", "reject_feature", False)
    used = {id(s) for s in feat_cases}                    # controls never share a key with a case
    for s in _pick(rng, [s for s in no_resp if id(s) not in used], k):
        feat_rows.append([s.employee.id, s.sent_date, "no_response", None])
        add("feature_engagement", f"{s.employee.id}@{s.sent_date}", "MISSING_AS_NEGATIVE", "none", True)

    # ---- LABEL_DATE_MISMATCH: labels contradicting the outcome dates
    label_rows = []
    resolved = [lb for lb in ds.labels if lb.target is not None]
    zeros = [lb for lb in resolved if lb.status == "employed_through_horizon"]
    ones = [lb for lb in resolved if lb.status == "voluntary_exit"]
    for lb in _pick(rng, zeros, k):                       # stayed, but labelled as a voluntary exit
        label_rows.append([lb.employee.id, lb.as_of, lb.horizon_end, "voluntary_exit", None, 1, lb.outcome.id])
        add("eval.horizon_label", f"{lb.employee.id}@{lb.as_of}", "LABEL_DATE_MISMATCH", "flag_label", False)
    flipped = _pick(rng, ones, k)
    for lb in flipped:                                    # left, but labelled as staying
        label_rows.append([lb.employee.id, lb.as_of, lb.horizon_end, "employed_through_horizon", None, 0,
                           lb.outcome.id])
        add("eval.horizon_label", f"{lb.employee.id}@{lb.as_of}", "LABEL_DATE_MISMATCH", "flag_label", False)
    used = {id(lb) for lb in flipped}                     # a control must never share a key with a case
    on_edge = sorted((lb for lb in ones if id(lb) not in used),
                     key=lambda lb: ((lb.horizon_end - lb.outcome.termination_date).days, lb.employee.id))
    for lb in on_edge[:k]:                                # controls: correct labels, terminations closest to the edge
        label_rows.append([lb.employee.id, lb.as_of, lb.horizon_end, lb.status, None, lb.target, lb.outcome.id])
        add("eval.horizon_label", f"{lb.employee.id}@{lb.as_of}", "LABEL_DATE_MISMATCH", "none", True)

    # ---- UNREVIEWED_SCORE: intervention request templates, materialised and enforced at G5
    june = [lb for lb in ds.labels if lb.as_of == JUNE_2026 and lb.target is not None]
    requests = []
    variants = ["no_review", "dismissed_review", "stale_score_version", "other_team"]
    for i, lb in enumerate(_pick(rng, june, k * 2)):
        variant = variants[i % 4] if i < k else "valid"
        expected = "none" if variant == "valid" else "block_action"
        requests.append({"case_ref": f"REQ-{i + 1:05d}", "employee_id": lb.employee.id,
                         "as_of_date": lb.as_of.isoformat(), "variant": variant, "expected": expected})
        add("api.intervention_request", f"REQ-{i + 1:05d}", "UNREVIEWED_SCORE", expected, variant == "valid")

    ds.ground_truth = gt
    ds.fixtures = {
        "fixtures/feature_sources.csv": _csv(["employee_id", "as_of_date", "source_table", "source_id",
                                              "event_date", "recorded_at"], leak_rows),
        "fixtures/core.engagement_survey.csv": _csv(
            ["survey_id", "employee_id", "wave", "sent_date", "response_status", "q1", "q2", "q3", "q4", "q5",
             "items_answered", "engagement_index", "submitted_at", "recorded_at"], survey_rows),
        "fixtures/feature_engagement.csv": _csv(["employee_id", "survey_sent_date", "survey_status",
                                                 "engagement_feature"], feat_rows),
        "fixtures/eval.horizon_label.csv": _csv(["employee_id", "as_of_date", "horizon_end", "label_status",
                                                 "ineligible_reason", "target", "outcome_id"], label_rows),
        "fixtures/intervention_requests.json": (json.dumps(requests, indent=1) + "\n").encode("utf-8"),
    }
