"""Generator scaffolding: exact totals, determinism, key integrity on load, semantic validator."""
from __future__ import annotations

import collections
import json

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
    reg, manifest = generate(500, out)
    return reg, manifest, out / "500"


def test_exact_totals(starter):
    reg, manifest, _ = starter
    sizes = manifest["meta"]["sizes"]
    assert sizes["initial"] == 450 and sizes["in_window_hires"] == 50 and sizes["teams"] == 30
    assert len(reg.employees) == sizes["initial"]
    assert len(reg.roles) == 64 and len(reg.bands) == 6 and len(reg.teams) == 30
    assert sum(sizes["teams_per_family"].values()) == 30
    assert sum(sizes["heads_per_family"].values()) == 450


def test_one_manager_per_team_and_family_consistency(starter):
    reg, _, _ = starter
    mgrs = collections.Counter(e.current.team.id for e in reg.employees if e.current.role.is_people_manager)
    assert set(mgrs) == {t.id for t in reg.teams} and set(mgrs.values()) == {1}
    assert all(e.current.role.job_family == e.current.team.job_family for e in reg.employees)


def test_same_seed_same_bytes(tmp_path):
    _, m1 = generate(500, tmp_path / "a")
    _, m2 = generate(500, tmp_path / "b")
    assert m1["files"] == m2["files"]


def test_loads_with_all_keys_and_passes_validator(starter, db_url):
    _, manifest, out_dir = starter
    with psycopg.connect(db_url) as conn:
        counts = load(conn, out_dir)
        for f, info in manifest["files"].items():
            table = f.removeprefix("canonical/").removesuffix(".csv")
            assert counts[table] == info["rows"], table
        failures = {k: v for k, v in validate.run(conn).items() if v}
        conn.rollback()
    assert failures == {}


def test_validator_catches_a_broken_chain(starter, db_url):
    """Negative control: the predecessor check must fire on a skipped predecessor."""
    _, _, out_dir = starter
    with psycopg.connect(db_url) as conn:
        load(conn, out_dir)
        emp, first, second = conn.execute("""
            SELECT employee_id, min(assignment_id), max(assignment_id) FROM core.assignment
            GROUP BY employee_id HAVING count(*) >= 3 LIMIT 1""").fetchone() or (None, None, None)
        if emp is None:
            pytest.skip("no employee with 3+ assignments at this scale")
        start = conn.execute("SELECT max(effective_from) + 400 FROM core.assignment WHERE employee_id=%s",
                             (emp,)).fetchone()[0]
        team, role = conn.execute("SELECT team_id, role_id FROM core.assignment WHERE assignment_id=%s",
                                  (second,)).fetchone()
        hire_asg = conn.execute("SELECT assignment_id FROM core.assignment WHERE employee_id=%s AND "
                                "change_reason='hire'", (emp,)).fetchone()[0]
        conn.execute("INSERT INTO core.assignment VALUES (%s,%s,%s,%s,%s,'lateral_move',%s,%s)",
                     (f"ASGN-{start.year}-999999", emp, team, role, start, hire_asg, f"{start} 09:00+00"))
        results = validate.run(conn)
        conn.rollback()
    assert f"ASGN-{start.year}-999999" in results["assignment_predecessor_is_immediately_earlier"]
