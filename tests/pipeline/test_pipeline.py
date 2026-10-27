"""G4 pipeline: source allowlist, as-of leakage invariance, join contract, neutral missingness,
label-access split, injected FUTURE_LEAK never used, explanation arithmetic."""
from __future__ import annotations

import csv
import datetime as dt

import numpy as np
import pandas as pd
import psycopg
import pytest

from generator.export import load
from generator.run import generate
from pipeline import evaluate as ev
from pipeline.features import ALLOWED_SOURCES, FEATURES, Sources, build
from pipeline.model import LogisticScorer, Scorecard

AS_OFS = [dt.date(2025, 6, 30), dt.date(2025, 9, 30), dt.date(2026, 6, 30)]


@pytest.fixture(scope="module")
def env(tmp_path_factory, db_url):
    out = tmp_path_factory.mktemp("g4")
    ds, _ = generate(500, out)
    conn = psycopg.connect(db_url)
    conn.execute("SET TIME ZONE 'UTC'")
    load(conn, out / "500")
    src = Sources.from_db(conn)
    yield conn, src, out / "500"
    conn.rollback()
    conn.close()


def test_source_allowlist_has_no_restricted_tables():
    text = " ".join(ALLOWED_SOURCES.values()).lower()
    for forbidden in ("eval.", "attrition_outcome", "leave_ledger", "compensation", "team_id", "role_id",
                      "available_hours", "actual_hours", "q1", "q2", "q3", "q4", "q5"):
        assert forbidden not in text, forbidden


@pytest.mark.parametrize("as_of", AS_OFS)
def test_features_identical_when_future_rows_are_removed(env, as_of):
    """Leakage invariance: deleting every row not available at as_of must not change any feature."""
    _, src, _ = env
    full, _ = build(src, as_of)
    t = {k: v.copy() for k, v in src.tables.items()}
    date_col = {"events": "event_date", "assignments": "effective_from", "workload": "period_month",
                "surveys": "sent_date"}
    for name, col in date_col.items():
        df = t[name]
        t[name] = df[(df[col] <= as_of) & (df.recorded_at <= as_of)]
    t["snapshots"] = t["snapshots"][t["snapshots"].as_of_date <= as_of]
    cut, _ = build(Sources(t), as_of)
    pd.testing.assert_frame_equal(full, cut)


@pytest.mark.parametrize("as_of", AS_OFS)
def test_join_contract_features_equal_scoring_population(env, as_of):
    conn, src, _ = env
    f, _ = build(src, as_of)
    s = {r[0] for r in conn.execute("""SELECT employee_id FROM eval.horizon_label
                                       WHERE as_of_date = %s AND label_status <> 'ineligible'""", (as_of,))}
    assert not f.employee_id.duplicated().any()
    assert set(f.employee_id) == s


def test_as_of_month_workload_never_used(env):
    _, src, _ = env
    for as_of in AS_OFS:
        _, lineage = build(src, as_of)
        month = f"-{as_of:%Y-%m}"
        used = [w for ws in lineage.workload_ids if isinstance(ws, list) for w in ws]
        assert used and not any(w.endswith(month) for w in used), as_of


def test_injected_future_leak_sources_never_in_lineage(env):
    _, src, out_dir = env
    with open(out_dir / "fixtures" / "feature_sources.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    leaks = [r for r in rows if r["source_table"] == "core.workload_snapshot"
             and r["variant"] in ("late_recorded", "future_event")]
    controls = [r for r in rows if r["variant"] == "prior_month"]
    assert leaks and controls
    for r in controls:                       # the legitimate prior-month row IS used
        as_of = dt.date.fromisoformat(r["as_of_date"])
        _, lineage = build(src, as_of)
        used = {w for ws in lineage.workload_ids if isinstance(ws, list) for w in ws}
        assert r["source_id"] in used
    for r in leaks:
        as_of = dt.date.fromisoformat(r["as_of_date"])
        _, lineage = build(src, as_of)
        used = {w for ws in lineage.workload_ids if isinstance(ws, list) for w in ws}
        assert r["source_id"] not in used


def test_missing_engagement_contributes_zero_and_earns_no_points(env):
    _, src, _ = env
    f, _ = build(src, dt.date(2025, 6, 30))
    y = (np.arange(len(f)) % 17 == 0).astype(int)
    lr = LogisticScorer(0.1).fit(f, y)
    missing = f[f.engagement_latest.isna()]
    assert len(missing) > 0
    for row in lr.contributions(missing):
        eng = next(c for c in row if c["feature"] == "engagement_latest")
        assert eng["value"] is None and eng["log_odds"] == 0.0
    _, detail = Scorecard.points_detail(missing)
    assert not any(d["rule"].startswith("Engagement") for row in detail for d in row)


def test_contributions_sum_to_model_logit(env):
    _, src, _ = env
    f, _ = build(src, dt.date(2025, 9, 30))
    y = (np.arange(len(f)) % 13 == 0).astype(int)
    lr = LogisticScorer(0.3).fit(f, y)
    logits = lr.logit(f.head(50))
    for row, z in zip(lr.contributions(f.head(50)), logits):
        assert abs(sum(c["log_odds"] for c in row) - z) < 1e-2


def test_training_never_reads_test_labels(env, monkeypatch):
    conn, src, _ = env
    seen = []
    real = ev.labels_for
    monkeypatch.setattr(ev, "labels_for", lambda c, dates: (seen.extend(dates), real(c, dates))[1])
    ev.train(conn, src)
    assert ev.TEST_DATE not in seen and set(seen) == set(ev.TRAIN_DATES + [ev.VALIDATION_DATE])


def test_feature_list_excludes_identifiers_and_leave():
    assert not any(k in f for f in FEATURES for k in ("team", "manager", "band", "leave", "sick", "employee_id"))


# ---------------- regression tests for the ChatGPT/Codex review of 95842be (finding 5)
def _from_lineage(src, lin, drop=lambda col, ids: ids):
    """Sources restricted to ONLY the rows a lineage declares (population tables stay complete)."""
    t = dict(src.tables)
    keep = lambda col: {i for ids in lin[col] if isinstance(ids, list) for i in drop(col, ids)}
    t["snapshots"] = src.tables["snapshots"][src.tables["snapshots"].snapshot_id.isin(set(lin.snapshot_id))]
    t["workload"] = src.tables["workload"][src.tables["workload"].workload_id.isin(keep("workload_ids"))]
    t["surveys"] = src.tables["surveys"][src.tables["surveys"].survey_id.isin(keep("survey_ids"))]
    t["assignments"] = src.tables["assignments"][src.tables["assignments"].assignment_id.isin(keep("transfer_ids"))]
    return Sources(t)


@pytest.mark.parametrize("as_of", AS_OFS)
def test_every_feature_reconstructs_from_its_declared_lineage(env, as_of):
    _, src, _ = env
    full, lin = build(src, as_of)
    again, _ = build(_from_lineage(src, lin), as_of)
    pd.testing.assert_frame_equal(full, again)


def test_lineage_check_detects_an_omitted_dependency(env):
    """Dropping the previous answered survey from the lineage must change engagement_decline somewhere."""
    _, src, _ = env
    as_of = dt.date(2026, 6, 30)
    full, lin = build(src, as_of)
    sv = src.tables["surveys"].set_index("survey_id")

    def drop_prev(col, ids):
        if col != "survey_ids":
            return ids
        answered = sorted((i for i in ids if not pd.isna(sv.loc[i, "engagement_index"])), key=lambda i: sv.loc[i, "sent_date"])
        return [i for i in ids if len(answered) < 2 or i != answered[-2]]
    broken, _ = build(_from_lineage(src, lin, drop_prev), as_of)
    assert not full["engagement_decline"].equals(broken["engagement_decline"])


def test_lineage_has_four_workload_months_and_exact_timestamps(env):
    _, src, _ = env
    _, lin = build(src, dt.date(2026, 6, 30))
    n = lin.workload_ids.map(lambda x: len(x) if isinstance(x, list) else 0)
    assert (n >= lin.workload_months_available).all() and (n == 4).any()      # last 4 months, superset of last 3
    assert src.tables["workload"].recorded_ts.str.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$").all()
