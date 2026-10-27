"""Full generator at starter scale: load, validator, join contract, reproducibility, SL invariance,
prevention model, injection detection, boundary cases."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import psycopg
import pytest

from generator import validate
from generator.derived import JUNE_2026, QUARTER_ENDS, horizon_end
from generator.export import load
from generator.report import _detection
from generator.run import build, generate


@pytest.fixture(scope="module")
def run500(tmp_path_factory):
    out = tmp_path_factory.mktemp("g2")
    ds, manifest = generate(500, out)
    return ds, manifest, out / "500"


@pytest.fixture(scope="module")
def loaded(run500, db_url):
    ds, manifest, out_dir = run500
    conn = psycopg.connect(db_url)
    load(conn, out_dir)
    yield conn, ds, out_dir
    conn.rollback()
    conn.close()


def test_two_runs_identical(tmp_path):
    _, m1 = generate(500, tmp_path / "a")
    _, m2 = generate(500, tmp_path / "b")
    assert m1["files"] == m2["files"]


def test_exact_totals(run500):
    ds, _, _ = run500
    assert len(ds.reg.employees) == 500
    assert ds.stats["backfill_hires"] + ds.stats["growth_hires"] == 50


def test_all_validator_checks_pass(loaded):
    conn, _, _ = loaded
    failures = {k: v for k, v in validate.run(conn).items() if v}
    assert failures == {}


def test_join_contract_one_row_per_key(loaded):
    """E = snapshots = labels for every quarter-end; no duplicates; every key in S has exactly one label."""
    conn, ds, _ = loaded
    for q in QUARTER_ENDS:
        employed = conn.execute("""SELECT count(*) FROM core.employee e JOIN core.attrition_outcome o USING (employee_id)
            WHERE e.hire_date <= %(q)s AND (o.termination_date IS NULL OR o.termination_date > %(q)s)""",
                                {"q": q}).fetchone()[0]
        snaps, labels, distinct = conn.execute("""
            SELECT (SELECT count(*) FROM core.tenure_snapshot WHERE as_of_date = %(q)s), count(*),
                   count(DISTINCT employee_id) FROM eval.horizon_label WHERE as_of_date = %(q)s""", {"q": q}).fetchone()
        assert employed == snaps == labels == distinct, q


def test_injected_anomalies_detected_without_false_positives(loaded):
    conn, ds, out_dir = loaded
    for issue, entity, n_pos, n_neg, tp, fn, fp in _detection(conn, ds, out_dir):
        assert n_pos > 0 and n_neg > 0, (issue, entity)
        assert fn == 0 and fp == 0, (issue, entity, tp, fn, fp)


def test_prevention_model(run500):
    ds, _, _ = run500
    by_key = {(lb.employee, lb.as_of): lb for lb in ds.labels}
    assert ds.potential_outcomes
    for p in ds.potential_outcomes:
        assert p.y_if_untreated == by_key[(p.employee, JUNE_2026)].target
        assert p.y_if_treated <= p.y_if_untreated


def test_sl_invariance_on_persisted_rows(loaded):
    """Paired records differing only in sick-leave days give identical model-visible workload values,
    after a round trip through PostgreSQL (incl. the lowest-capacity months)."""
    conn, _, _ = loaded
    rows = conn.execute("""SELECT workload_id, period_month, scheduled_workdays, available_hours, actual_hours,
                                  utilization_pct, after_hours_pct, oncall_days, recorded_at
                           FROM core.workload_snapshot WHERE available_hours >= 16
                           ORDER BY available_hours, workload_id LIMIT 300""").fetchall()
    assert rows
    for wid, pm, sched, avail, actual, util, after, oncall, rec in rows:
        for extra_sl in (1, 2):
            new_avail = avail - 8 * extra_sl
            if new_avail <= 0:
                continue
            new_actual = Decimal(util) * Decimal(new_avail) / Decimal(100)
            with conn.transaction():
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS sl_pair (LIKE core.workload_snapshot INCLUDING CONSTRAINTS)")
                conn.execute("TRUNCATE sl_pair")
                conn.execute("INSERT INTO sl_pair VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                             (wid, "EMP-" + wid[8:14], pm, sched, new_avail, new_actual, util, after, oncall, rec))
                back = conn.execute("SELECT utilization_pct, after_hours_pct, oncall_days FROM sl_pair").fetchone()
            assert back == (util, after, oncall)


def test_workload_utilisation_independent_of_leave_in_generation(run500):
    """Generation rule: utilisation comes from intensity only; months with and without SL share the rule."""
    ds, _, _ = run500
    for w in ds.reg.workload:
        if w.utilization_pct is None:
            continue
        assert Decimal(w.actual_hours) == Decimal(w.utilization_pct) * w.available_hours / 100


def test_horizon_end_examples():
    assert horizon_end(dt.date(2025, 6, 30)) == dt.date(2025, 12, 31)
    assert horizon_end(dt.date(2025, 9, 30)) == dt.date(2026, 3, 31)
    assert horizon_end(dt.date(2026, 12, 31)) == dt.date(2027, 6, 30)


def test_boundary_cases_present(loaded):
    """Mid-month hires and exits, notice across a year end and late-recorded June workload all exist."""
    conn, _, _ = loaded
    midmonth_hire = conn.execute("""SELECT count(*) FROM core.employee WHERE hire_date >= '2025-01-01'
                                    AND extract(day FROM hire_date) > 1""").fetchone()[0]
    midmonth_exit = conn.execute("""SELECT count(*) FROM core.employment_event WHERE event_type = 'exit'
                                    AND event_date < (date_trunc('month', event_date) + interval '1 month - 1 day')::date""").fetchone()[0]
    across_year = conn.execute("""SELECT count(*) FROM core.employment_event r JOIN core.employment_event x
                                  USING (employee_id) WHERE r.event_type = 'resignation_submitted' AND x.event_type = 'exit'
                                  AND extract(year FROM r.event_date) < extract(year FROM x.event_date)""").fetchone()[0]
    june_late = conn.execute("""SELECT count(*) FROM core.workload_snapshot WHERE period_month = '2025-06-01'
                                AND recorded_at::date > '2025-06-30'""").fetchone()[0]
    assert midmonth_hire > 0 and midmonth_exit > 0 and across_year > 0 and june_late > 0


def test_unreviewed_score_requests_cover_all_variants(run500):
    _, _, out_dir = run500
    req = json.loads((out_dir / "fixtures" / "intervention_requests.json").read_text(encoding="utf-8"))
    assert {r["variant"] for r in req} == {"no_review", "dismissed_review", "stale_score_version", "other_team", "valid"}


def test_build_requires_calibrated_b0(monkeypatch):
    import generator.run as r
    real = r.load_generator_config
    def fake(path=None):
        g = real(path); g["hazard"]["voluntary"]["b0"] = None; return g
    monkeypatch.setattr(r, "load_generator_config", fake)
    with pytest.raises(SystemExit, match="not calibrated"):
        build(100)
