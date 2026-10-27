"""G5 API: authentication, RBAC, the review gate, reduced manager view and database grants.

Runs against the built demo database (`python -m generator.run --scale 10000 --load` then
`python -m pipeline.run`) inside one transaction that is rolled back; skipped if that database is missing.
API calls run under SET LOCAL ROLE rs_app, i.e. with the live API's privileges."""
from __future__ import annotations

import json

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from backend.app import db, gate, settings
from backend.app.main import create_app
from dbutil import admin_url, url_for_database
from replay_requests import team_manager

H = {"x-requested-with": "retention-ui"}
PW = "test-only-password"


@pytest.fixture(scope="module")
def env():
    try:
        conn = psycopg.connect(url_for_database(admin_url(), "retention"))
        conn.execute("SELECT 1 FROM app.score LIMIT 1")
    except Exception:
        pytest.skip("demo database not built")
    conn.execute("SET TIME ZONE 'UTC'")
    cfg = settings.get()
    db.configure_clock(cfg.now)
    at = db.now()
    run = gate.active_run(conn)
    # a held-out fixture employee: scored, no review, no intervention, team with a manager
    fixture = json.loads((cfg.data_dir / "fixtures" / "intervention_requests.json").read_text(encoding="utf-8"))
    case = None
    for r in fixture:
        if r["variant"] != "valid":
            continue
        sid = conn.execute("SELECT score_id FROM app.score WHERE score_run_id=%s AND employee_id=%s",
                           (run, r["employee_id"])).fetchone()
        team = gate.team_of(conn, r["employee_id"], at)
        mgr = team and team_manager(conn, team, at, r["employee_id"])
        other_team = conn.execute("SELECT team_id FROM core.team WHERE team_id <> %s ORDER BY team_id LIMIT 1",
                                  (team,)).fetchone()[0]
        other = team_manager(conn, other_team, at, r["employee_id"])
        if sid and mgr and other and gate.assign_arm(r["employee_id"], run) == "treatment":
            case = {"score_id": sid[0], "employee_id": r["employee_id"], "mgr": mgr, "other": other}
            break
    assert case
    h = PasswordHasher().hash(PW)
    people = [("t_hr", "hr_partner", None), ("t_mgr", "manager", case["mgr"]), ("t_other", "manager", case["other"]),
              ("t_ops", "it_ops", None), ("t_admin", "admin", None)]
    for i, (u, role, emp) in enumerate(people):
        conn.execute("INSERT INTO app.app_user VALUES (%s,%s,%s,%s,%s,true)", (f"USR-{8000 + i}", u, u, role, emp))
        conn.execute("INSERT INTO app.user_credential VALUES (%s,%s,%s)", (f"USR-{8000 + i}", h, at))
    conn.execute("SET LOCAL ROLE rs_app")
    app = create_app(cfg, manage_pool=False)

    def same():
        yield conn
    app.dependency_overrides[db.get_conn] = same
    clients = {}
    for u, *_ in people:
        c = TestClient(app)
        assert c.post("/api/auth/login", json={"username": u, "password": PW}, headers=H).status_code == 200
        clients[u] = c
    yield conn, app, clients, case
    conn.rollback()
    conn.close()


@pytest.fixture
def step(env):
    """Each test in its own savepoint, rolled back afterwards."""
    conn = env[0]
    with conn.transaction(force_rollback=True):
        yield env


def _exc_count(conn, issue):
    return conn.execute("SELECT count(*) FROM app.exception_log WHERE issue_type = %s", (issue,)).fetchone()[0]


def test_unauthenticated_and_csrf(step):
    _, app, clients, _ = step
    anon = TestClient(app)
    assert anon.get("/api/queue").status_code == 401
    assert clients["t_hr"].post("/api/auth/logout").status_code == 403          # no X-Requested-With


def test_role_boundaries(step):
    _, _, c, case = step
    assert c["t_hr"].get("/api/queue").status_code == 200
    for u in ("t_mgr", "t_ops", "t_admin"):
        assert c[u].get("/api/queue").status_code == 403
    assert c["t_ops"].get(f"/api/cases/{case['score_id']}").status_code == 403
    assert c["t_admin"].get(f"/api/cases/{case['score_id']}").status_code == 403
    assert c["t_admin"].get("/api/users").status_code == 200
    assert c["t_hr"].get("/api/users").status_code == 403
    assert c["t_ops"].get("/api/data-quality").status_code == 200


def test_manager_sees_nothing_before_approval_and_other_team_is_blocked_and_logged(step):
    conn, _, c, case = step
    assert c["t_mgr"].get(f"/api/cases/{case['score_id']}").status_code == 404
    assert case["employee_id"] not in [i["employee_id"] for i in c["t_mgr"].get("/api/my-cases").json()["items"]]
    before = _exc_count(conn, "NOT_AUTHORIZED")
    assert c["t_other"].get(f"/api/cases/{case['score_id']}").status_code == 403
    assert _exc_count(conn, "NOT_AUTHORIZED") == before + 1


def test_review_gate_end_to_end(step):
    conn, _, c, case = step
    sid = case["score_id"]
    r = c["t_mgr"].post("/api/interventions", json={"score_id": sid, "intervention_type": "checkin"}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["issue_type"] == "UNREVIEWED_SCORE"
    assert c["t_hr"].post(f"/api/cases/{sid}/review", json={"decision": "approved", "reason_code": "supportive_checkin"},
                          headers=H).status_code == 200
    r = c["t_other"].post("/api/interventions", json={"score_id": sid, "intervention_type": "checkin"}, headers=H)
    assert r.status_code == 403
    r = c["t_mgr"].post("/api/interventions", json={"score_id": sid, "intervention_type": "checkin"}, headers=H)
    assert r.status_code == 201 and r.json()["arm"] == "treatment" and r.json()["status"] == "proposed"
    iid = r.json()["intervention_id"]
    r = c["t_mgr"].post("/api/interventions", json={"score_id": sid, "intervention_type": "checkin"}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["issue_type"] == "CONFLICTING_INTERVENTION"
    # review is locked once an intervention exists
    r = c["t_hr"].post(f"/api/cases/{sid}/review", json={"decision": "dismissed", "reason_code": "context_known_to_hr"},
                       headers=H)
    assert r.status_code == 409
    # transitions
    assert c["t_mgr"].post(f"/api/interventions/{iid}/status", json={"to_status": "completed"}, headers=H).status_code == 409
    assert c["t_other"].post(f"/api/interventions/{iid}/status", json={"to_status": "scheduled"}, headers=H).status_code == 403
    assert c["t_mgr"].post(f"/api/interventions/{iid}/status", json={"to_status": "scheduled"}, headers=H).status_code == 200
    assert c["t_mgr"].post(f"/api/interventions/{iid}/status", json={"to_status": "completed"}, headers=H).status_code == 200
    assert c["t_mgr"].post(f"/api/interventions/{iid}/status", json={"to_status": "cancelled"}, headers=H).status_code == 409


def test_actor_comes_from_session_not_body(step):
    conn, _, c, case = step
    r = c["t_hr"].post(f"/api/cases/{case['score_id']}/review",
                       json={"decision": "approved", "reason_code": "supportive_checkin", "reviewer_user_id": "USR-8001"},
                       headers=H)
    assert r.status_code == 200
    who = conn.execute("SELECT reviewer_user_id FROM app.review_decision WHERE review_id = %s",
                       (r.json()["review_id"],)).fetchone()[0]
    assert who == "USR-8000"


def test_stale_score_version_blocked(step):
    conn, _, c, case = step
    old = conn.execute("""SELECT z.score_id FROM app.score z JOIN app.score_run u USING (score_run_id)
                          WHERE z.employee_id = %s AND u.scorer_type = 'scorecard'""", (case["employee_id"],)).fetchone()[0]
    r = c["t_hr"].post(f"/api/cases/{old}/review", json={"decision": "approved", "reason_code": "supportive_checkin"},
                       headers=H)
    assert r.status_code == 409 and r.json()["detail"]["issue_type"] == "STALE_SCORE_VERSION"


def test_manager_view_has_no_model_numbers(step):
    _, _, c, case = step
    sid = case["score_id"]
    c["t_hr"].post(f"/api/cases/{sid}/review", json={"decision": "approved", "reason_code": "supportive_checkin"}, headers=H)
    body = c["t_mgr"].get(f"/api/cases/{sid}").json()
    assert body["view"] == "manager"
    keys = set()

    def walk(o):
        if isinstance(o, dict):
            keys.update(o); [walk(v) for v in o.values()]
        elif isinstance(o, list):
            [walk(v) for v in o]
    walk(body)
    assert not keys & {"risk_score", "rank", "contributions", "log_odds", "coverage", "source_refs", "scorecard",
                       "workload", "surveys", "band", "population"}
    hr = c["t_hr"].get(f"/api/cases/{sid}").json()
    assert hr["view"] == "hr" and "risk_score" in hr
    assert any(i["employee_id"] == case["employee_id"] for i in c["t_mgr"].get("/api/my-cases").json()["items"])


def test_unknown_reason_code_rejected(step):
    _, _, c, case = step
    r = c["t_hr"].post(f"/api/cases/{case['score_id']}/review", json={"decision": "approved", "reason_code": "because"},
                       headers=H)
    assert r.status_code == 422


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM eval.horizon_label LIMIT 1",
    "SELECT 1 FROM core.attrition_outcome LIMIT 1",
    "SELECT 1 FROM core.leave_ledger LIMIT 1",
    "SELECT actual_hours FROM core.workload_snapshot LIMIT 1",
    "SELECT q1 FROM core.engagement_survey LIMIT 1",
    "SELECT 1 FROM core.compensation_band LIMIT 1",
    "UPDATE app.review_decision SET decision = 'approved'",
])
def test_api_role_cannot_reach_restricted_data(step, sql):
    conn = step[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            conn.execute(sql)


def test_logout_revokes_session(step):
    _, app, _, _ = step
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "t_ops", "password": PW}, headers=H).status_code == 200
    assert c.get("/api/auth/me").status_code == 200
    assert c.post("/api/auth/logout", headers=H).status_code == 200
    c.cookies.clear()
    assert c.get("/api/auth/me").status_code == 401


def test_bad_password_rejected(step):
    _, app, _, _ = step
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "t_ops", "password": "wrong"}, headers=H).status_code == 401
