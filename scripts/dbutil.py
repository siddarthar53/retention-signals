"""Shared database helpers for scripts and tests (Windows and Linux)."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = ROOT / "db" / "schema.sql"


def load_env() -> None:
    """Load .env from the repo root if python-dotenv is installed; real env vars win."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


def admin_url() -> str:
    load_env()
    url = os.environ.get("DATABASE_ADMIN_URL")
    if not url:
        raise SystemExit("DATABASE_ADMIN_URL is not set. Copy .env.example to .env and fill it in.")
    return url


def url_for_database(url: str, dbname: str) -> str:
    """Return the same connection URL pointing at another database."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(url)
    params["dbname"] = dbname
    return make_conninfo(**params)


# The only databases any script may DROP and recreate. Anything else is refused before connecting.
RESETTABLE_DATABASES = frozenset({"retention", "retention_test", "retention_dict"})


def recreate_database(dbname: str) -> str:
    """DELETE and recreate one of the project's own databases; return a connection URL for it.

    Refuses (before opening any connection) unless dbname is in RESETTABLE_DATABASES.
    """
    if dbname not in RESETTABLE_DATABASES:
        raise SystemExit(f"Refusing to drop database {dbname!r}: only {sorted(RESETTABLE_DATABASES)} "
                         "may be reset by these scripts.")
    base = admin_url()
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    return url_for_database(base, dbname)


def load_schema(url: str) -> None:
    ddl = SCHEMA_SQL.read_text(encoding="utf-8")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(ddl)


def ensure_app_login(url: str, password: str | None) -> bool:
    """Create or update the API login role rs_app_login (member of rs_app). Returns True if done."""
    if not password:
        return False
    with psycopg.connect(url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rs_app_login'").fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE rs_app_login LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE")
                     .format(sql.Literal(password)))
        conn.execute("GRANT rs_app TO rs_app_login")
        dbname = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO rs_app_login").format(sql.Identifier(dbname)))
    return True
