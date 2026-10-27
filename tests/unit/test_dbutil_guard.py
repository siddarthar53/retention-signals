"""The reset helpers must never connect to, or drop, any database outside the project's own three."""
from __future__ import annotations

import pytest

import dbutil
import db_reset


@pytest.fixture()
def no_connect(monkeypatch):
    calls = []
    monkeypatch.setattr(dbutil.psycopg, "connect", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setenv("DATABASE_ADMIN_URL", "postgresql://u:p@localhost:5432/postgres")
    return calls


@pytest.mark.parametrize("name", ["postgres", "template1", "other_app", "retention2", "Retention", ""])
def test_recreate_refuses_other_databases(no_connect, name):
    with pytest.raises(SystemExit, match="Refusing"):
        dbutil.recreate_database(name)
    assert no_connect == []


def test_db_reset_refuses_other_database_name(no_connect, monkeypatch):
    monkeypatch.setenv("DATABASE_NAME", "other_app")
    monkeypatch.setenv("RS_APP_PASSWORD", "x")
    with pytest.raises(SystemExit, match="Refusing"):
        db_reset.main()
    assert no_connect == []


def test_db_reset_requires_app_password_before_dropping(no_connect, monkeypatch):
    monkeypatch.delenv("DATABASE_NAME", raising=False)
    monkeypatch.delenv("RS_APP_PASSWORD", raising=False)
    monkeypatch.setattr(dbutil, "load_env", lambda: None)
    monkeypatch.setattr(db_reset, "load_env", lambda: None)
    with pytest.raises(SystemExit, match="RS_APP_PASSWORD"):
        db_reset.main()
    assert no_connect == []
