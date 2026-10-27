"""Runtime settings from the environment (.env is loaded by scripts/dbutil.load_env)."""
from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from dbutil import load_env, url_for_database  # noqa: E402


@dataclass(frozen=True)
class Settings:
    app_db_url: str
    now: dt.datetime            # the simulation clock: HR works the 2026-06-30 list in July 2026
    session_hours: int
    secure_cookies: bool
    reports_dir: Path
    data_dir: Path


def _app_url() -> str:
    explicit = os.environ.get("DATABASE_APP_URL")
    if explicit:
        return explicit
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    admin = os.environ.get("DATABASE_ADMIN_URL")
    if not admin:
        raise RuntimeError("Set DATABASE_APP_URL, or DATABASE_ADMIN_URL + RS_APP_PASSWORD, in .env")
    p = conninfo_to_dict(url_for_database(admin, "retention"))
    p["user"] = "rs_app_login"
    if os.environ.get("RS_APP_PASSWORD"):
        p["password"] = os.environ["RS_APP_PASSWORD"]
    return make_conninfo(**p)


def get() -> Settings:
    load_env()
    return Settings(
        app_db_url=_app_url(),
        now=dt.datetime.fromisoformat(os.environ.get("APP_NOW", "2026-07-15T10:00:00+00:00")),
        session_hours=int(os.environ.get("SESSION_HOURS", "8")),
        secure_cookies=os.environ.get("SECURE_COOKIES", "false").lower() == "true",
        reports_dir=ROOT / "data" / "out" / "reports",
        data_dir=ROOT / "data" / "out" / os.environ.get("DATA_SCALE", "10000"),
    )
