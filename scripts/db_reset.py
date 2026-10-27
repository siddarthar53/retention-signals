"""Drop and recreate the application database, then load db/schema.sql.

Usage:  python scripts/db_reset.py            (database name from DATABASE_NAME, default "retention")
Reads DATABASE_ADMIN_URL and RS_APP_PASSWORD from the environment or .env. Prints no secrets.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg  # noqa: E402

from dbutil import ensure_app_login, load_env, load_schema, recreate_database  # noqa: E402


def main() -> None:
    load_env()
    dbname = os.environ.get("DATABASE_NAME", "retention")
    url = recreate_database(dbname)
    load_schema(url)
    with psycopg.connect(url) as conn:
        tables = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema IN ('core','app','eval') AND table_type = 'BASE TABLE'").fetchone()[0]
    login = ensure_app_login(url, os.environ.get("RS_APP_PASSWORD"))
    print(f"Database '{dbname}' recreated: {tables} tables loaded from db/schema.sql.")
    print("API login role rs_app_login: " + ("ready." if login else "skipped (RS_APP_PASSWORD not set)."))


if __name__ == "__main__":
    main()
