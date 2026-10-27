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


# ---------------- regression tests for the independent review of 91b6df8
def _blocks(ds):
    """Rebuild each taken-leave block as consecutive scheduled workdays from its txn_date."""
    from generator import calendar as cal
    from generator.derived import facts
    out = {}
    for t in ds.reg.leave:
        if t.txn_type != "taken":
            continue
        f = facts(t.employee)
        days = [w for w in cal.workdays_in_month(t.txn_date.year, t.txn_date.month)
                if w >= t.txn_date and w >= t.employee.hire_date and (f.termination is None or w <= f.termination)]
        out.setdefault((t.employee, t.txn_date.year, t.txn_date.month), []).append((t.leave_type, days[:-t.days]))
    return out


def test_el_and_sl_never_share_a_day_and_capacity_counts_each_day_once(run500):
    """Review MAJOR 1: an EL day and an SL day can never be the same workday."""
    ds, _, _ = run500
    blocks = _blocks(ds)
    wl = {(w.employee, w.period_month.year, w.period_month.month): w for w in ds.reg.workload}
    both = 0
    for key, bl in blocks.items():
        sets = [set(d) for _, d in bl]
        union = set().union(*sets)
        assert sum(len(s) for s in sets) == len(union), key
        both += len(bl) > 1
        w = wl[key]
        assert w.available_hours == (w.scheduled_workdays - len(union)) * 8, key
    assert both > 0, "fixture should contain months with both EL and SL"


def test_every_employed_year_has_one_sl_grant_incl_hires_on_the_first(loaded):
    """Review MINOR 4."""
    conn, _, _ = loaded
    assert validate.run(conn)["leave_sl_grant_each_employed_year"] == []
    firsts = conn.execute("""SELECT count(*) FROM core.employee e WHERE e.hire_date >= '2025-01-01'
                             AND extract(day FROM e.hire_date) = 1""").fetchone()[0]
    granted = conn.execute("""SELECT count(*) FROM core.employee e WHERE e.hire_date >= '2025-01-01'
                              AND extract(day FROM e.hire_date) = 1 AND EXISTS (SELECT 1 FROM core.leave_ledger l
                              WHERE l.employee_id = e.employee_id AND l.txn_type = 'grant' AND l.txn_date = e.hire_date)""").fetchone()[0]
    assert firsts == granted


def test_snapshots_are_available_on_their_as_of_date(run500):
    """Review MINOR 5: no exception to the as-of rule is needed for snapshots."""
    from pipeline.asof import is_available
    ds, _, _ = run500
    assert all(is_available(s.as_of, s.recorded_at, s.as_of) for s in ds.snapshots)


def test_intervention_request_cases_and_controls_not_confounded(run500):
    """Review MAJOR 2: cases and controls are drawn from one shuffled pool."""
    ds, _, out_dir = run500
    req = json.loads((out_dir / "fixtures" / "intervention_requests.json").read_text(encoding="utf-8"))
    fam_case = {r["job_family"] for r in req if r["expected"] != "none"}
    fam_ctrl = {r["job_family"] for r in req if r["expected"] == "none"}
    assert len(fam_case & fam_ctrl) >= 3


def test_future_leak_fixtures_cover_both_sides_of_the_cutoff(run500):
    """Review MAJOR 3: boundary variants exist, including a control at 23:59:59 on as_of."""
    import csv as _csv
    _, _, out_dir = run500
    with open(out_dir / "fixtures" / "feature_sources.csv", newline="", encoding="utf-8") as fh:
        variants = {r["variant"] for r in _csv.DictReader(fh)}
    assert {"late_recorded", "future_event", "one_second_late", "at_cutoff", "prior_month", "on_time"} <= variants


def test_label_fixtures_cover_all_variants(run500):
    import csv as _csv
    ds, _, _ = run500
    variants = {g[1].split("-", 2)[2] for g in ds.ground_truth if g[4] == "LABEL_DATE_MISMATCH"}
    assert {"stayed_labelled_exit", "left_labelled_stayed", "target_contradicts_status", "not_employed_at_as_of",
            "edge_exit"} <= variants


# ---------------- regression tests for the ChatGPT/Codex review of 95842be (finding 3)
def _state(clock, mgr_q):
    from generator.history import State
    return State(base_int=1.0, core=1.0, x=1.0, eng_base=3.5, eng=3.5, pull=0.0, mgr_q=mgr_q, clock_start=clock)


def test_survey_career_inputs_ignore_changes_after_submission():
    """A promotion or transfer dated AFTER a survey's submission day cannot change that survey's inputs."""
    from generator.history import career_as_of
    st = _state(dt.date(2020, 1, 1), 0.3)
    st.career_month_start = (dt.date(2020, 1, 1), 0.3)
    st.clock_start, st.mgr_q = dt.date(2026, 6, 25), -0.9          # live state already moved on
    st.career_changes = [(dt.date(2026, 6, 25), dt.date(2026, 6, 25), -0.9)]
    assert career_as_of(st, dt.date(2026, 6, 20)) == (dt.date(2020, 1, 1), 0.3)   # submitted before the change
    assert career_as_of(st, dt.date(2026, 6, 25)) == (dt.date(2026, 6, 25), -0.9)  # same day: change applies
    st.career_changes.insert(0, (dt.date(2026, 6, 3), dt.date(2026, 6, 3), 0.1))   # earlier change applies
    assert career_as_of(st, dt.date(2026, 6, 20)) == (dt.date(2026, 6, 3), 0.1)


def test_generator_reads_survey_career_state_as_of_submission(monkeypatch, tmp_path):
    """The simulator asks for career state on each survey's own submission day, never later."""
    import generator.history as h
    calls = []
    real = h.career_as_of
    monkeypatch.setattr(h, "career_as_of", lambda st, day: (calls.append(day), real(st, day))[1])
    ds, _ = generate(300, tmp_path)
    submitted = sorted(dt.date.fromisoformat(s.submitted_at[:10]) for s in ds.reg.surveys
                       if s.response_status != "no_response")
    assert calls and sorted(calls) == submitted
