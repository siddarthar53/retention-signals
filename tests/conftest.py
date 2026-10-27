"""Test fixtures. Tests create their own database (retention_test) from db/schema.sql.

Requires DATABASE_ADMIN_URL (env or .env). Every test runs inside a transaction that is rolled back,
so tests are independent and use fixed timestamps (no now()).
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from dbutil import load_schema, recreate_database  # noqa: E402

TEST_DB = "retention_test"

BASE_ROWS = """
INSERT INTO core.compensation_band VALUES ('BAND-03',3,'SYN',54000,65000,76000),
                                          ('BAND-04',4,'SYN',72000,86000,100000);
INSERT INTO core.role VALUES ('ROLE-0001','Engineering IC L3','engineering',3,'BAND-03',false),
                             ('ROLE-0002','Engineering Manager L4','engineering',4,'BAND-04',true);
INSERT INTO core.team VALUES ('TEAM-0001','Engineering 001','engineering','2015-01-01'),
                             ('TEAM-0002','Engineering 002','engineering','2015-01-01');
INSERT INTO core.employee VALUES ('EMP-000001','2022-03-14','2022-03-14 09:00+00'),
                                 ('EMP-000002','2023-05-01','2023-05-01 09:00+00');
INSERT INTO core.assignment VALUES
  ('ASGN-2022-000001','EMP-000001','TEAM-0001','ROLE-0001','2022-03-14','hire',NULL,'2022-03-14 09:00+00'),
  ('ASGN-2023-000002','EMP-000002','TEAM-0001','ROLE-0001','2023-05-01','hire',NULL,'2023-05-01 09:00+00');
INSERT INTO core.attrition_outcome VALUES
  ('OUT-EMP-000001-2026','EMP-000001','active',NULL,NULL,NULL,'2026-12-31','2027-01-05 09:00+00'),
  ('OUT-EMP-000002-2026','EMP-000002','active',NULL,NULL,NULL,'2026-12-31','2027-01-05 09:00+00');
"""


@pytest.fixture(scope="session")
def db_url() -> str:
    url = recreate_database(TEST_DB)
    load_schema(url)
    return url


@pytest.fixture()
def conn(db_url):
    """A connection inside a transaction with base rows, rolled back after the test."""
    with psycopg.connect(db_url) as c:
        c.execute(BASE_ROWS)
        yield c
        c.rollback()


def expect_ok(c, statement: str, params=None):
    with c.transaction():
        return c.execute(statement, params)


def expect_violation(c, statement: str, constraint: str | None = None,
                     error=psycopg.errors.IntegrityError, params=None):
    """Assert the statement fails with the given error class and (if given) constraint name."""
    with pytest.raises(error) as exc:
        with c.transaction():
            c.execute(statement, params)
    if constraint is not None:
        got = exc.value.diag.constraint_name
        assert got == constraint, f"expected constraint {constraint!r}, got {got!r}: {exc.value}"
    return exc.value


@contextmanager
def as_role(c, role: str):
    c.execute(f"SET LOCAL ROLE {role}")
    try:
        yield
    finally:
        c.execute("RESET ROLE")
