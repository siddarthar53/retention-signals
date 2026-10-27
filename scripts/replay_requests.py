"""Replay the injected UNREVIEWED_SCORE fixture through the real API and score detection against ground truth.

Everything runs in ONE database transaction that is rolled back at the end, so the demo database is left
unchanged. Setup (temporary replay users) runs as the admin connection; then the connection switches to
SET LOCAL ROLE rs_app, so every API call is checked against the same grants the live API has.

  python scripts/replay_requests.py            -> data/out/reports/replay.json
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

import psycopg
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from backend.app import db, gate, settings  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from dbutil import admin_url, url_for_database  # noqa: E402

PASSWORD = "replay-only-" + "x" * 8          # exists only inside the rolled-back transaction
HEADERS = {"x-requested-with": "retention-ui"}


def team_manager(conn, team_id: str, at: dt.datetime, exclude: str) -> str | None:
    row = conn.execute("""
        SELECT a.employee_id FROM core.assignment a JOIN core.role r USING (role_id)
        WHERE a.team_id = %(t)s AND r.is_people_manager AND a.employee_id <> %(x)s
          AND a.effective_from <= %(d)s AND a.recorded_at <= %(at)s
          AND NOT EXISTS (SELECT 1 FROM core.assignment b WHERE b.employee_id = a.employee_id
                          AND b.effective_from > a.effective_from AND b.effective_from <= %(d)s AND b.recorded_at <= %(at)s)
          AND NOT EXISTS (SELECT 1 FROM core.employment_event e WHERE e.employee_id = a.employee_id
                          AND e.event_type = 'exit' AND e.event_date <= %(d)s AND e.recorded_at <= %(at)s)
        ORDER BY a.employee_id LIMIT 1""", {"t": team_id, "x": exclude, "d": at.date(), "at": at}).fetchone()
    return row[0] if row else None


def run(conn, cfg: settings.Settings) -> dict:
    db.configure_clock(cfg.now)
    at = db.now()
    requests = json.loads((cfg.data_dir / "fixtures" / "intervention_requests.json").read_text(encoding="utf-8"))
    pw_hash = PasswordHasher().hash(PASSWORD)
    users: dict[str, str] = {}

    def make_user(username: str, role: str, emp: str | None) -> str:
        if username not in users:
            uid = f"USR-{9000 + len(users):04d}"
            conn.execute("INSERT INTO app.app_user VALUES (%s,%s,%s,%s,%s,true)", (uid, username, username, role, emp))
            conn.execute("INSERT INTO app.user_credential VALUES (%s,%s,%s)", (uid, pw_hash, at))
            users[username] = uid
        return username

    run_id = gate.active_run(conn)
    plan, skipped = [], Counter()
    other_teams = [r[0] for r in conn.execute("SELECT team_id FROM core.team ORDER BY team_id")]
    make_user("replay_hr", "hr_partner", None)
    for r in requests:
        emp = r["employee_id"]
        sid = conn.execute("SELECT score_id FROM app.score WHERE score_run_id = %s AND employee_id = %s",
                           (run_id, emp)).fetchone()
        team = gate.team_of(conn, emp, at)
        if sid is None or team is None:
            skipped["no_active_score"] += 1
            continue
        if r["variant"] == "other_team":
            mgr = next((m for t in other_teams if t != team and (m := team_manager(conn, t, at, emp))), None)
        else:
            mgr = team_manager(conn, team, at, emp)
        if mgr is None:
            skipped["no_manager_for_team"] += 1
            continue
        plan.append((r, sid[0], make_user(f"replay_mgr_{mgr.lower()}", "manager", mgr)))

    conn.execute("SET LOCAL ROLE rs_app")              # from here on: the API's own privileges
    app = create_app(cfg, manage_pool=False)

    def same_conn():
        yield conn
    app.dependency_overrides[db.get_conn] = same_conn
    clients: dict[str, TestClient] = {}

    def client(username: str) -> TestClient:
        if username not in clients:
            c = TestClient(app)
            res = c.post("/api/auth/login", json={"username": username, "password": PASSWORD}, headers=HEADERS)
            assert res.status_code == 200, res.text
            clients[username] = c
        return clients[username]

    hr = client("replay_hr")
    rows = []
    for r, sid, mgr in plan:
        v = r["variant"]
        if v == "dismissed_review":
            hr.post(f"/api/cases/{sid}/review", json={"decision": "dismissed", "reason_code": "context_known_to_hr"},
                    headers=HEADERS).raise_for_status()
        elif v in ("stale_score_version", "other_team", "valid"):
            hr.post(f"/api/cases/{sid}/review", json={"decision": "approved", "reason_code": "supportive_checkin"},
                    headers=HEADERS).raise_for_status()
        target = sid
        if v == "stale_score_version":
            target = conn.execute("""SELECT z.score_id FROM app.score z JOIN app.score_run u USING (score_run_id)
                                     WHERE z.employee_id = %s AND u.scorer_type = 'scorecard'""", (r["employee_id"],)).fetchone()[0]
        res = client(mgr).post("/api/interventions", json={"score_id": target, "intervention_type": "checkin"},
                               headers=HEADERS)
        blocked = res.status_code in (403, 409)
        body = res.json()
        rows.append({"case_ref": r["case_ref"], "variant": v, "expected": r["expected"], "http": res.status_code,
                     "blocked": blocked, "issue_type": body["detail"]["issue_type"] if blocked else None,
                     "arm": body.get("arm") if res.status_code == 201 else None})
    for c in clients.values():
        c.close()

    by_variant = {}
    for v in sorted({x["variant"] for x in rows}):
        sub = [x for x in rows if x["variant"] == v]
        by_variant[v] = {"n": len(sub), "blocked": sum(x["blocked"] for x in sub),
                         "issue_types": dict(Counter(x["issue_type"] for x in sub if x["issue_type"])),
                         "http": dict(Counter(str(x["http"]) for x in sub))}
    pos = [x for x in rows if x["expected"] == "block_action"]
    neg = [x for x in rows if x["expected"] == "none"]
    logged = conn.execute("SELECT count(*) FROM app.exception_log WHERE detector = 'backend.gate'").fetchone()[0]
    return {"fixture_requests": len(requests), "replayed": len(rows), "skipped": dict(skipped),
            "caught": sum(x["blocked"] for x in pos), "missed": sum(not x["blocked"] for x in pos),
            "false_positives": sum(x["blocked"] for x in neg), "valid_allowed": sum(not x["blocked"] for x in neg),
            "control_arm_share_of_valid": round(sum(x["arm"] == "control" for x in neg) / max(len(neg), 1), 3),
            "exceptions_logged": logged, "by_variant": by_variant,
            "note": "Replayed through the HTTP API as rs_app inside one transaction, then rolled back."}


def main() -> int:
    cfg = settings.get()
    url = url_for_database(admin_url(), "retention")
    with psycopg.connect(url) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        try:
            result = run(conn, cfg)
        finally:
            conn.rollback()
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    (cfg.reports_dir / "replay.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "by_variant"}, indent=2))
    return 0 if result["missed"] == 0 and result["false_positives"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
