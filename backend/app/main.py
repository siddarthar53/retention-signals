"""FastAPI app. Run: uvicorn backend.app.main:app --port 8000  (see README).

Roles: hr_partner (queue, full case, review, interventions), manager (approved cases in own team, reduced
view, start/progress interventions), it_ops (data quality, exceptions, runs), admin (users, runs,
evaluation, exceptions). Nobody but HR sees model numbers for an individual."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, gate, queries, security, settings
from .security import COOKIE, User, roles

HR, MGR, OPS, ADMIN = "hr_partner", "manager", "it_ops", "admin"


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ReviewIn(BaseModel):
    decision: Literal["approved", "dismissed", "needs_info"]
    reason_code: str = Field(max_length=64)
    comment: str | None = Field(default=None, max_length=1000)


class InterventionIn(BaseModel):
    score_id: str = Field(max_length=32)
    intervention_type: Literal["checkin", "workload_rebalance", "career_conversation", "internal_mobility"]


class StatusIn(BaseModel):
    to_status: Literal["scheduled", "completed", "declined", "cancelled"]
    note: str | None = Field(default=None, max_length=1000)


def create_app(cfg: settings.Settings | None = None, *, manage_pool: bool = True) -> FastAPI:
    cfg = cfg or settings.get()
    db.configure_clock(cfg.now)

    @asynccontextmanager
    async def lifespan(_):
        if manage_pool:
            db.open_pool(cfg.app_db_url)
        yield
        if manage_pool:
            db.close_pool()

    app = FastAPI(title="Retention Signals API", version="0.5", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            try:
                security.require_csrf(request)
            except HTTPException as e:
                return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def current_user(request: Request, conn=Depends(db.get_conn)) -> None:
        request.state.user = security.session_user(conn, request.cookies.get(COOKIE), db.now())

    api_deps = [Depends(current_user)]

    def blocked(conn, b: gate.Blocked, user: User, action: str):
        xid = gate.log_blocked(conn, b, user, action, db.now())
        raise HTTPException(b.status, {"issue_type": b.issue_type, "message": b.message, "exception_id": xid})

    # ------------------------------------------------------------ auth
    @app.post("/api/auth/login")
    def login(body: LoginIn, response: Response, conn=Depends(db.get_conn)):
        uname = body.username.strip().lower()
        if security.throttled(uname):
            raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.")
        user = security.verify_login(conn, uname, body.password)
        if user is None:
            security.record_failure(uname)
            raise HTTPException(401, "Username or password is incorrect.")
        with conn.transaction():
            token = security.create_session(conn, user, db.now(), cfg.session_hours)
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=cfg.secure_cookies,
                            max_age=cfg.session_hours * 3600, path="/")
        return {"user": user.__dict__}

    @app.post("/api/auth/logout", dependencies=api_deps)
    def logout(request: Request, response: Response, conn=Depends(db.get_conn)):
        token = request.cookies.get(COOKIE)
        if token:
            with conn.transaction():
                conn.execute("UPDATE app.user_session SET revoked_at = %s WHERE session_id = %s AND revoked_at IS NULL",
                             (db.now(), security.token_hash(token)))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me", dependencies=api_deps)
    def me(user: User = roles(HR, MGR, OPS, ADMIN)):
        return {"user": user.__dict__, "now": db.now().isoformat()}

    # ------------------------------------------------------------ HR
    @app.get("/api/overview", dependencies=api_deps)
    def overview(user: User = roles(HR, ADMIN), conn=Depends(db.get_conn)):
        return queries.overview(conn)

    @app.get("/api/queue", dependencies=api_deps)
    def queue(band: str | None = None, status: str | None = None, family: str | None = None,
              q: str | None = Query(default=None, max_length=20), limit: int = Query(50, ge=1, le=200),
              offset: int = Query(0, ge=0), user: User = roles(HR), conn=Depends(db.get_conn)):
        return queries.queue(conn, band=band, status=status, family=family, q=q, limit=limit, offset=offset)

    @app.get("/api/cases/{score_id}", dependencies=api_deps)
    def case(score_id: str, user: User = roles(HR, MGR), conn=Depends(db.get_conn)):
        at = db.now()
        if user.role == HR:
            out = queries.case_for_hr(conn, score_id, at)
            if out is None:
                raise HTTPException(404, "Case not found")
            return {"view": "hr", **out}
        row = conn.execute("SELECT employee_id FROM app.score WHERE score_id = %s", (score_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Case not found")
        if not gate.in_scope(conn, user, row[0], at):
            blocked(conn, gate.Blocked("NOT_AUTHORIZED", 403, "This case is outside your team.", score_id,
                                       {"employee_id": row[0]}), user, "case_view")
        rev = gate.latest_review(conn, score_id)
        if rev is None or rev[1] != "approved":
            raise HTTPException(404, "Case not found")     # unapproved cases do not exist for managers
        return {"view": "manager", **queries.case_for_manager(conn, score_id, at)}

    @app.post("/api/cases/{score_id}/review", dependencies=api_deps)
    def review(score_id: str, body: ReviewIn, user: User = roles(HR), conn=Depends(db.get_conn)):
        try:
            with conn.transaction():
                rid = gate.record_review(conn, user, score_id, body.decision, body.reason_code, body.comment, db.now())
        except gate.Blocked as b:
            blocked(conn, b, user, "review")
        except LookupError:
            raise HTTPException(404, "Case not found")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"review_id": rid}

    # ------------------------------------------------------------ managers + HR
    @app.get("/api/my-cases", dependencies=api_deps)
    def my_cases(user: User = roles(MGR), conn=Depends(db.get_conn)):
        return queries.manager_cases(conn, user, db.now())

    @app.get("/api/interventions", dependencies=api_deps)
    def list_interventions(status: str | None = None, user: User = roles(HR, MGR), conn=Depends(db.get_conn)):
        return {"items": queries.interventions(conn, user, db.now(), status)}

    @app.post("/api/interventions", status_code=201, dependencies=api_deps)
    def create_intervention(body: InterventionIn, user: User = roles(HR, MGR), conn=Depends(db.get_conn)):
        try:
            with conn.transaction():
                return gate.create_intervention(conn, user, body.score_id, body.intervention_type, db.now())
        except gate.Blocked as b:
            blocked(conn, b, user, "intervention_request")
        except LookupError:
            raise HTTPException(404, "Case not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/interventions/{intervention_id}/status", dependencies=api_deps)
    def set_status(intervention_id: str, body: StatusIn, user: User = roles(HR, MGR), conn=Depends(db.get_conn)):
        try:
            with conn.transaction():
                eid = gate.change_status(conn, user, intervention_id, body.to_status, body.note, db.now())
        except gate.Blocked as b:
            blocked(conn, b, user, "intervention_status")
        except LookupError:
            raise HTTPException(404, "Intervention not found")
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"event_id": eid, "status": body.to_status}

    # ------------------------------------------------------------ quality, evaluation, admin
    @app.get("/api/exceptions", dependencies=api_deps)
    def exceptions(issue_type: str | None = None, entity: str | None = None, limit: int = Query(50, ge=1, le=200),
                   offset: int = Query(0, ge=0), user: User = roles(HR, OPS, ADMIN), conn=Depends(db.get_conn)):
        return queries.exceptions(conn, issue_type, entity, limit, offset)

    @app.get("/api/data-quality", dependencies=api_deps)
    def data_quality(user: User = roles(HR, OPS, ADMIN)):
        return {"generation": queries.read_json(cfg.data_dir / "g2_summary.json"),
                "replay": queries.read_json(cfg.reports_dir / "replay.json")}

    @app.get("/api/evaluation", dependencies=api_deps)
    def evaluation(user: User = roles(HR, OPS, ADMIN)):
        return {"evaluation": queries.read_json(cfg.reports_dir / "evaluation.json"),
                "model_card": queries.read_json(cfg.reports_dir / "model_card.json")}

    @app.get("/api/runs", dependencies=api_deps)
    def runs(user: User = roles(OPS, ADMIN), conn=Depends(db.get_conn)):
        return {"items": queries.runs(conn)}

    @app.get("/api/users", dependencies=api_deps)
    def users(user: User = roles(ADMIN), conn=Depends(db.get_conn)):
        return {"items": queries.users(conn, db.now())}

    # ------------------------------------------------------------ built UI (single origin in production)
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            f = dist / path
            return FileResponse(f if path and f.is_file() else dist / "index.html")

    return app


app = create_app() if __name__ != "__main__" else None
