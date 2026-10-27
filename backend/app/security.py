"""Sessions, login throttling, CSRF and role checks.

Identity always comes from the server-side session (cookie -> SHA-256 -> app.user_session); request
bodies never carry an actor. State-changing requests must send the X-Requested-With header (CSRF
defence alongside SameSite=Lax cookies)."""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, HTTPException, Request

COOKIE = "rs_session"
_ph = PasswordHasher()
_failures: dict[str, list[float]] = {}
MAX_FAILURES, WINDOW_SECONDS = 5, 300
_DUMMY_HASH = _ph.hash("not-a-real-password")


@dataclass
class User:
    user_id: str
    username: str
    display_name: str
    role: str
    employee_id: str | None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def throttled(username: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failures.get(username, []) if now - t < WINDOW_SECONDS]
    _failures[username] = recent
    return len(recent) >= MAX_FAILURES


def record_failure(username: str) -> None:
    _failures.setdefault(username, []).append(time.monotonic())


def verify_login(conn, username: str, password: str) -> User | None:
    row = conn.execute("""SELECT u.user_id, u.username, u.display_name, u.app_role, u.employee_id, c.password_hash
                          FROM app.app_user u JOIN app.user_credential c USING (user_id)
                          WHERE u.username = %s AND u.is_active""", (username,)).fetchone()
    if row is None:
        try:                                   # same argon2 cost whether or not the user exists
            _ph.verify(_DUMMY_HASH, password)
        except VerificationError:
            pass
        return None
    try:
        _ph.verify(row[5], password)
    except VerificationError:
        return None
    return User(*row[:5])


def create_session(conn, user: User, now: dt.datetime, hours: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO app.user_session VALUES (%s,%s,%s,%s,NULL)",
                 (token_hash(token), user.user_id, now, now + dt.timedelta(hours=hours)))
    return token


def session_user(conn, token: str | None, now: dt.datetime) -> User | None:
    if not token:
        return None
    row = conn.execute("""SELECT u.user_id, u.username, u.display_name, u.app_role, u.employee_id
                          FROM app.user_session s JOIN app.app_user u USING (user_id)
                          WHERE s.session_id = %s AND s.revoked_at IS NULL AND s.expires_at > %s AND u.is_active""",
                       (token_hash(token), now)).fetchone()
    return User(*row) if row else None


def require_csrf(request: Request) -> None:
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("x-requested-with") != "retention-ui":
        raise HTTPException(403, "Missing X-Requested-With header")


def roles(*allowed: str):
    def dep(request: Request) -> User:
        user: User | None = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(401, "Sign in to continue")
        if user.role not in allowed:
            raise HTTPException(403, "Your role cannot open this page")
        return user
    return Depends(dep)
