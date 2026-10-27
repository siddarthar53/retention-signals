"""Connection pool, simulation clock and ID allocation.

The API connects as rs_app_login (member of rs_app): it cannot read eval.*, outcomes, sick leave, pay,
capacity/actual hours or raw survey items, and can only INSERT into the audit tables. Connections are
autocommit, so every `with conn.transaction()` is a real transaction; inside the replay harness the same
code runs as savepoints of one outer transaction that is rolled back."""
from __future__ import annotations

import datetime as dt
import time
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None
_START = time.monotonic()
_base_now: dt.datetime | None = None


def configure_clock(base: dt.datetime) -> None:
    global _base_now, _START
    _base_now, _START = base, time.monotonic()


def now() -> dt.datetime:
    """Simulation clock: APP_NOW plus real time elapsed since start, so events stay ordered."""
    base = _base_now or dt.datetime.now(dt.timezone.utc)
    return (base + dt.timedelta(seconds=time.monotonic() - _START)).replace(microsecond=0)


def _configure(conn: psycopg.Connection) -> None:
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute("SET statement_timeout = '15s'")


def open_pool(url: str) -> None:
    global _pool
    _pool = ConnectionPool(url, min_size=1, max_size=8, open=True, configure=_configure,
                           kwargs={"autocommit": True})


def close_pool() -> None:
    if _pool is not None:
        _pool.close()


def get_conn() -> Iterator[psycopg.Connection]:
    assert _pool is not None, "pool not opened"
    with _pool.connection() as conn:
        yield conn


def next_id(conn, table: str, column: str, prefix: str, width: int) -> str:
    """Next sequential ID like REV-2026-000123. Must be called inside a transaction; the advisory lock
    serialises concurrent writers for that table until commit."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (table,))
    row = conn.execute(f"SELECT max({column}) FROM {table} WHERE {column} LIKE %s", (prefix + "%",)).fetchone()
    n = int(row[0][len(prefix):]) + 1 if row[0] else 1
    return f"{prefix}{n:0{width}d}"
