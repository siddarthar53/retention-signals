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
        leaks = [r for r in csv.DictReader(fh) if r["source_table"] == "core.workload_snapshot"]
    assert leaks
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
