"""G5 API: authentication, RBAC, the review gate, reduced manager view and database grants.

Runs against the built demo database (`python -m generator.run --scale 10000 --load` then
`python -m pipeline.run`) inside one transaction that is rolled back. Skipped ONLY when that database
does not exist or has no scoring run yet (optional demo data absent); any other setup problem fails.
API calls run under SET LOCAL ROLE rs_app, i.e. with the live API's privileges."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
from pathlib import Path

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
    except psycopg.OperationalError as e:
        if "does not exist" in str(e):
            pytest.skip("demo database 'retention' not built")
        raise
    if conn.execute("SELECT count(*) FROM app.score_run").fetchone()[0] == 0:
        pytest.skip("pipeline has not been run on the demo database")
    conn.execute("SET TIME ZONE 'UTC'")
    cfg = settings.get()
    db.configure_clock(cfg.now)
    at = db.now()
    run = gate.active_run(conn)
    # a held-out fixture employee: scored, no review, no intervention, team with a manager
    fixture = json.loads((cfg.data_dir / "fixtures" / "intervention_requests.json").read_text(encoding="utf-8"))
    case = vac = None
    no_exit = lambda e: not conn.execute("SELECT 1 FROM core.employment_event WHERE employee_id = %s AND "
                                         "event_type IN ('exit','resignation_submitted')", (e,)).fetchone()
    for r in fixture:
        if r["variant"] != "valid":
            continue
        sid = conn.execute("SELECT score_id FROM app.score WHERE score_run_id=%s AND employee_id=%s",
                           (run, r["employee_id"])).fetchone()
        team = gate.team_of(conn, r["employee_id"], at)
        mgr = team and team_manager(conn, team, at, r["employee_id"])
        if sid and team and not mgr and vac is None:
            vac = {"score_id": sid[0], "employee_id": r["employee_id"], "team": team}
        if case is None and sid and mgr and no_exit(mgr) and gate.assign_arm(r["employee_id"], run) == "treatment":
            other_team = conn.execute("SELECT team_id FROM core.team WHERE team_id <> %s ORDER BY team_id LIMIT 1",
                                      (team,)).fetchone()[0]
            other = team_manager(conn, other_team, at, r["employee_id"])
            case = {"score_id": sid[0], "employee_id": r["employee_id"], "mgr": mgr, "other": other,
                    "team": team, "other_team": other_team}
    assert case, "required fixture case not found: the replay fixture and the database do not match"
    assert vac, "required vacancy-team fixture case not found"
    case["vac"] = vac
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
    token = c.cookies.get("rs_session")
    assert c.post("/api/auth/logout", headers=H).status_code == 200
    replay = TestClient(app)                       # same cookie value presented again after logout
    replay.cookies.set("rs_session", token)
    assert replay.get("/api/auth/me").status_code == 401


def test_session_expires_on_real_clock_not_business_clock(step, monkeypatch):
    _, app, _, _ = step
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "t_ops", "password": PW}, headers=H).status_code == 200
    assert c.get("/api/auth/me").status_code == 200
    later = db.real_now() + dt.timedelta(hours=9)  # past the 8-hour session lifetime
    monkeypatch.setattr(db, "real_now", lambda: later)
    assert c.get("/api/auth/me").status_code == 401


def test_bad_password_rejected(step):
    _, app, _, _ = step
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "t_ops", "password": "wrong"}, headers=H).status_code == 401


# ---------------- regression tests for the ChatGPT/Codex review of 95842be
@contextlib.contextmanager
def admin(conn):
    """Do setup as the owner inside the current savepoint, then drop back to the API role."""
    conn.execute("RESET ROLE")
    try:
        yield
    finally:
        conn.execute("SET LOCAL ROLE rs_app")


def _approve(c, sid):
    r = c["t_hr"].post(f"/api/cases/{sid}/review", json={"decision": "approved", "reason_code": "supportive_checkin"},
                       headers=H)
    assert r.status_code == 200, r.text


def _move(conn, emp, team=None, manager_role=None, days_ago=1):
    """Record a transfer (or demotion) effective yesterday, recorded before the business clock."""
    at = db.now()
    cur = conn.execute("""SELECT a.assignment_id, a.team_id, a.role_id, r.job_family, r.level, a.effective_from
                          FROM core.assignment a JOIN core.role r USING (role_id) WHERE a.employee_id = %s
                          ORDER BY a.effective_from DESC LIMIT 1""", (emp,)).fetchone()
    role = cur[2]
    if manager_role is not None:
        role = conn.execute("""SELECT role_id FROM core.role WHERE job_family = %s AND is_people_manager = %s
                               ORDER BY abs(level - %s) LIMIT 1""", (cur[3], manager_role, cur[4])).fetchone()[0]
    day = at.date() - dt.timedelta(days=days_ago)
    assert day > cur[5]
    conn.execute("INSERT INTO core.assignment VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                 (f"ASGN-2026-{990000 + int(emp[-4:]):06d}", emp, team or cur[1], role, day,
                  "transfer" if team else "reorg", cur[0], at - dt.timedelta(hours=1)))


SENTINEL = "sentinel-outside-dist-not-a-secret"


@pytest.mark.parametrize("path", ["/..%5c_rs_sentinel.txt", "/..%2f_rs_sentinel.txt", "/%2e%2e%5c_rs_sentinel.txt",
                                  "/assets/..%5c..%5c_rs_sentinel.txt", "/_rs_sentinel.txt"])
def test_spa_route_never_serves_files_outside_dist(step, path):
    """Finding 1: encoded traversal (incl. Windows backslash) must fall back to index.html, anonymously."""
    _, app, _, _ = step
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    sentinel = dist.parent / "_rs_sentinel.txt"
    sentinel.write_text(SENTINEL, encoding="utf-8")
    try:
        r = TestClient(app).get(path)
        assert SENTINEL not in r.text
        assert r.status_code in (200, 404)
    finally:
        sentinel.unlink()


def test_spa_serves_real_assets_and_index(step):
    _, app, _, _ = step
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    asset = next((dist / "assets").glob("*.js"))
    anon = TestClient(app)
    assert anon.get(f"/assets/{asset.name}").status_code == 200
    assert '<div id="root">' in anon.get("/some/client/route").text


def test_owner_loses_access_when_employee_transfers_out(step):
    """Finding 2: ownership does not override current team scope, for reads or writes."""
    conn, _, c, case = step
    sid = case["score_id"]
    _approve(c, sid)
    r = c["t_mgr"].post("/api/interventions", json={"score_id": sid, "intervention_type": "checkin"}, headers=H)
    assert r.status_code == 201
    iid = r.json()["intervention_id"]
    with admin(conn):
        _move(conn, case["employee_id"], team=case["other_team"])
    assert c["t_mgr"].post(f"/api/interventions/{iid}/status", json={"to_status": "scheduled"}, headers=H).status_code == 403
    assert c["t_mgr"].get(f"/api/cases/{sid}").status_code == 403
    assert all(i["intervention_id"] != iid for i in c["t_mgr"].get("/api/interventions").json()["items"])
    assert c["t_hr"].post(f"/api/interventions/{iid}/status", json={"to_status": "scheduled"}, headers=H).status_code == 200


@pytest.mark.parametrize("change", ["demoted", "transferred", "exited"])
def test_manager_authority_requires_current_active_manager_role(step, change):
    conn, _, c, case = step
    sid = case["score_id"]
    _approve(c, sid)
    with admin(conn):
        if change == "demoted":
            _move(conn, case["mgr"], manager_role=False)
        elif change == "transferred":
            _move(conn, case["mgr"], team=case["other_team"])
        else:
            at = db.now()
            conn.execute("INSERT INTO core.employment_event VALUES (%s,%s,'exit',%s,'voluntary',NULL,%s)",
                         (f"EEV-2026-{990000 + int(case['mgr'][-4:]):06d}", case["mgr"], at.date() - dt.timedelta(days=1),
                          at - dt.timedelta(hours=1)))
    assert c["t_mgr"].get(f"/api/cases/{sid}").status_code == 403
    items = c["t_mgr"].get("/api/my-cases").json()["items"]
    assert case["employee_id"] not in [i["employee_id"] for i in items]
    if change != "transferred":                  # a transferred manager now sees only the new team
        assert items == []
    r = c["t_mgr"].post("/api/interventions", json={"score_id": sid, "intervention_type": "checkin"}, headers=H)
    assert r.status_code == 403


def test_vacant_team_case_is_handled_by_hr_and_not_by_an_out_of_scope_manager(step):
    _, _, c, case = step
    sid = case["vac"]["score_id"]
    _approve(c, sid)
    assert c["t_other"].post("/api/interventions", json={"score_id": sid, "intervention_type": "checkin"},
                             headers=H).status_code == 403
    r = c["t_hr"].post("/api/interventions", json={"score_id": sid, "intervention_type": "checkin"}, headers=H)
    assert r.status_code == 201, r.text


def test_superseded_lr_score_is_stale_even_when_approved(step):
    """An approved score from an older logistic-regression run must be refused as STALE (not unreviewed)."""
    conn, _, c, case = step
    run = gate.active_run(conn)
    with admin(conn):
        r0 = conn.execute("SELECT as_of_date, model_version, config_hash, model_frozen_at, executed_at "
                          "FROM app.score_run WHERE score_run_id = %s", (run,)).fetchone()
        conn.execute("INSERT INTO app.score_run VALUES ('RUN-2026-0901',%s,'logistic_regression',%s,%s,%s,%s)",
                     (r0[0], r0[1], r0[2], r0[3], r0[4] - dt.timedelta(hours=6)))
        conn.execute("""INSERT INTO app.score SELECT 'SCR-2026-9100001','RUN-2026-0901', employee_id, risk_score, 1,
                        'suggested_checkin', contributions, coverage, source_refs FROM app.score WHERE score_id = %s""",
                     (case["score_id"],))
        conn.execute("""INSERT INTO app.review_decision VALUES ('REV-2026-910001','SCR-2026-9100001','USR-8000',
                        'approved','supportive_checkin',NULL,%s,%s)""", (r0[4] - dt.timedelta(hours=5),) * 2)
    assert gate.active_run(conn) == run
    r = c["t_mgr"].post("/api/interventions", json={"score_id": "SCR-2026-9100001", "intervention_type": "checkin"},
                        headers=H)
    assert r.status_code == 409 and r.json()["detail"]["issue_type"] == "STALE_SCORE_VERSION"


def test_case_ids_follow_employee_order_not_rank(step):
    """Manager-visible identifiers must not encode a person's position in the list."""
    conn = step[0]
    rows = conn.execute("SELECT score_id, employee_id, rank_in_run FROM app.score WHERE score_run_id = %s",
                        (gate.active_run(conn),)).fetchall()
    by_id = [e for _, e, _ in sorted(rows)]
    assert by_id == sorted(by_id)
    assert [r for _, _, r in sorted(rows)] != sorted(r for _, _, r in rows)


def test_manager_topics_are_neutral_and_in_fixed_order(step):
    from backend.app.queries import MANAGER_TOPIC_ORDER, MANAGER_TOPICS
    _, _, c, case = step
    _approve(c, case["score_id"])
    body = c["t_mgr"].get(f"/api/cases/{case['score_id']}").json()
    keys = [t["theme"] for t in body["themes"]]
    assert keys == [k for k in MANAGER_TOPIC_ORDER if k in keys]
    assert all(t["text"] == MANAGER_TOPICS[t["theme"]] for t in body["themes"])
    listing = c["t_mgr"].get("/api/my-cases").json()
    assert "survey" not in json.dumps(listing["items"]).lower()
    assert [i["employee_id"] for i in listing["items"]] == sorted(i["employee_id"] for i in listing["items"])
