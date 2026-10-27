"""Generator scaffolding: exact totals, determinism, key integrity on load, semantic validator."""
from __future__ import annotations

import psycopg
import pytest

from generator import validate
from generator.core import largest_remainder
from generator.export import load
from generator.run import generate


@pytest.mark.parametrize("total,weights", [(30, [.3, .15, .15, .13, .08, .07, .07, .05]),
                                           (450, [.25, .3, .25, .14, .06]), (7, [1, 1, 1]), (0, [1, 2])])
def test_largest_remainder_sums_exactly(total, weights):
    parts = largest_remainder(total, weights)
    assert sum(parts) == total and all(p >= 0 for p in parts)


def test_largest_remainder_ties_break_by_order():
    assert largest_remainder(1, [1, 1, 1]) == [1, 0, 0]


@pytest.fixture(scope="module")
def starter(tmp_path_factory):
    out = tmp_path_factory.mktemp("gen")
    ds, manifest = generate(500, out)
    return ds, manifest, out / "500"


def test_exact_totals(starter):
    ds, _, _ = starter
    st = ds.stats
    assert st["initial"] == 450 and st["in_window_hires"] == 50 and st["teams"] == 30
    assert len(ds.reg.employees) == 500
    assert len(ds.reg.roles) == 64 and len(ds.reg.bands) == 6 and len(ds.reg.teams) == 30
    assert sum(st["teams_per_family"].values()) == 30
    assert sum(st["heads_per_family"].values()) == 450


def test_role_family_matches_team_for_every_assignment(starter):
    ds, _, _ = starter
    assert all(a.role.job_family == a.team.job_family for a in ds.reg.assignments)


def test_same_seed_same_bytes(tmp_path):
    _, m1 = generate(500, tmp_path / "a")
    _, m2 = generate(500, tmp_path / "b")
    assert m1["files"] == m2["files"]


def test_loads_with_all_keys_and_passes_validator(starter, db_url):
    _, manifest, out_dir = starter
    with psycopg.connect(db_url) as conn:
        counts = load(conn, out_dir)
        for f, info in manifest["files"].items():
            if f.endswith(".csv") and f.split("/")[0] in ("canonical", "eval"):
                table = f.split("/", 1)[1].removesuffix(".csv")
                assert counts[table] == info["rows"], table
        failures = {k: v for k, v in validate.run(conn).items() if v}
        conn.rollback()
    assert failures == {}


def test_validator_catches_a_broken_chain(starter, db_url):
    """Negative control: the predecessor check must fire on a skipped predecessor."""
    _, _, out_dir = starter
    with psycopg.connect(db_url) as conn:
        load(conn, out_dir)
        row = conn.execute("""
            SELECT employee_id, max(assignment_id) FROM core.assignment a
            WHERE NOT EXISTS (SELECT 1 FROM core.employment_event x WHERE x.employee_id = a.employee_id
                              AND x.event_type = 'exit')
            GROUP BY employee_id HAVING count(*) >= 3 ORDER BY 1 LIMIT 1""").fetchone()
        assert row, "expected at least one employee with 3+ assignments"
        emp, last = row
        start = conn.execute("SELECT max(effective_from) + 1 FROM core.assignment WHERE employee_id=%s",
                             (emp,)).fetchone()[0]
        team, role = conn.execute("SELECT team_id, role_id FROM core.assignment WHERE assignment_id=%s",
                                  (last,)).fetchone()
        hire_asg = conn.execute("SELECT assignment_id FROM core.assignment WHERE employee_id=%s AND "
                                "change_reason='hire'", (emp,)).fetchone()[0]
        bad = f"ASGN-{start.year}-999999"
        conn.execute("INSERT INTO core.assignment VALUES (%s,%s,%s,%s,%s,'lateral_move',%s,%s)",
                     (bad, emp, team, role, start, hire_asg, f"{start} 09:00+00"))
        results = validate.run(conn)
        conn.rollback()
    assert bad in results["assignment_predecessor_is_immediately_earlier"]
