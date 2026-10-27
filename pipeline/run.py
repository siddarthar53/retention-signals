"""Train, freeze, evaluate, score and simulate interventions (G4).

    python -m pipeline.run            # uses the loaded 'retention' database

Writes: app.score_run / app.score (2026-06-30, both scorers), seeded demo users with argon2 hashes
(passwords to data/out/demo_credentials.txt, never committed), simulated HR reviews and 80/20
interventions with events, and reports in data/out/reports/ (model card, evaluation, intervention
summary). The API reads only the aggregate JSON reports, never eval tables.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

from generator.core import ROOT

from . import evaluate as ev
from .features import Sources, build
from .allocation import ALLOCATION_VERSION, assign_arm
from .model import config_hash

REPORTS = ROOT / "data" / "out" / "reports"
DATA_DIR = ROOT / "data" / "out" / "10000"     # generated dataset whose fixtures are audited
SUGGESTED_SHARE, MONITOR_SHARE = 0.05, 0.10
ARM_SEED = 20261022 + 7
PENDING_SHARE = 0.15          # suggested check-ins left unreviewed so the HR queue has live work


def _bands(n: int) -> list[str]:
    k1 = int(round(n * SUGGESTED_SHARE)); k2 = int(round(n * (SUGGESTED_SHARE + MONITOR_SHARE)))
    return ["suggested_checkin" if i < k1 else "monitor" if i < k2 else "none" for i in range(n)]


def _source_refs(c, ts: dict, derived: dict) -> list[dict]:
    """Every source row behind one score, with its exact recorded_at (see features.build lineage)."""
    refs = [{"table": "core.tenure_snapshot", "id": c.snapshot_id, "recorded_at": ts["snapshots"][c.snapshot_id],
             "derived_from": derived.get(c.snapshot_id)}]
    for col, table, key in (("workload_ids", "core.workload_snapshot", "workload"),
                            ("survey_ids", "core.engagement_survey", "surveys"),
                            ("transfer_ids", "core.assignment", "assignments")):
        for rid in (c[col] if isinstance(c[col], list) else []):
            refs.append({"table": table, "id": rid, "recorded_at": ts[key][rid]})
    return refs


def write_scores(conn, src: Sources, fz) -> dict[str, str]:
    """Score IDs are assigned in employee-ID order, never in rank order, so a case identifier reveals
    nothing about a person's position in the list."""
    as_of = ev.TEST_DATE
    X, cov = build(src, as_of)
    id_col = {"snapshots": "snapshot_id", "workload": "workload_id", "surveys": "survey_id", "assignments": "assignment_id"}
    ts = {k: dict(zip(src.tables[k][c], src.tables[k].recorded_ts)) for k, c in id_col.items()}
    snaps = src.tables["snapshots"]
    derived = dict(zip(snaps.snapshot_id, snaps.derived_from))
    assert X.employee_id.is_monotonic_increasing
    runs = {}
    for i, (name, model) in enumerate((("logistic_regression", fz.lr), ("scorecard", fz.sc)), 1):
        run_id = f"RUN-2026-{i:04d}"
        runs[name] = run_id
        conn.execute("INSERT INTO app.score_run VALUES (%s,%s,%s,%s,%s,%s,%s)",
                     (run_id, as_of, name, model.version, config_hash(model.to_dict()), ev.FREEZE_AT,
                      "2026-07-01 06:00:00+00"))
        p = model.predict(X)
        order = np.argsort(-p, kind="stable")
        rank_of = np.empty(len(order), dtype=int)
        rank_of[order] = np.arange(1, len(order) + 1)
        bands = _bands(len(order))
        if name == "logistic_regression":
            contribs = model.contributions(X)
        else:
            _, detail = model.points_detail(X)
            contribs = [[{"rule": d["rule"], "points": d["points"]} for d in row] for row in detail]
        rows = []
        for idx in range(len(X)):                       # employee-ID order
            c = cov.iloc[idx]
            coverage = {"no_answered_survey": bool(c.no_answered_survey),
                        "last_wave_no_response": bool(c.last_wave_no_response),
                        "workload_months_available": int(c.workload_months_available)}
            rank = int(rank_of[idx])
            rows.append((f"SCR-2026-{(i - 1) * len(X) + idx + 1:07d}", run_id, X.employee_id.iloc[idx],
                         round(float(p[idx]), 5), rank, bands[rank - 1], json.dumps(contribs[idx]),
                         json.dumps(coverage), json.dumps(_source_refs(c, ts, derived), default=str)))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO app.score VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    return runs


def seed_users(conn, run_id: str) -> list[tuple[str, str, str]]:
    """Demo personas. Managers are the current managers of the teams with the most suggested check-ins."""
    from argon2 import PasswordHasher
    ph = PasswordHasher()
    as_of = ev.TEST_DATE
    teams = conn.execute("""
        WITH cur AS (
          SELECT a.employee_id, a.team_id, r.is_people_manager FROM core.assignment_interval a
          JOIN core.role r USING (role_id)
          WHERE a.effective_from <= %(d)s AND (a.effective_to IS NULL OR a.effective_to > %(d)s))
        SELECT m.team_id, m.employee_id, count(*) AS n FROM app.score s
        JOIN cur e ON e.employee_id = s.employee_id AND NOT e.is_people_manager
        JOIN cur m ON m.team_id = e.team_id AND m.is_people_manager
        WHERE s.score_run_id = %(r)s AND s.priority_band = 'suggested_checkin'
        GROUP BY m.team_id, m.employee_id ORDER BY n DESC, m.team_id LIMIT 6""", {"d": as_of, "r": run_id}).fetchall()
    people = [("hr1", "HR Partner 01", "hr_partner", None), ("hr2", "HR Partner 02", "hr_partner", None),
              ("itops1", "IT Operations 01", "it_ops", None), ("admin1", "Administrator 01", "admin", None)]
    for i, (team_id, emp, _) in enumerate(teams, 1):
        people.append((f"manager{i}", f"Manager of {team_id}", "manager", emp))
    creds = []
    for i, (username, name, role, emp) in enumerate(people, 1):
        uid = f"USR-{i:04d}"
        pw = secrets.token_urlsafe(12)
        conn.execute("INSERT INTO app.app_user VALUES (%s,%s,%s,%s,%s,true)", (uid, username, name, role, emp))
        conn.execute("INSERT INTO app.user_credential VALUES (%s,%s,%s)", (uid, ph.hash(pw), "2026-06-30 09:00:00+00"))
        creds.append((username, role, pw))
    return creds


INTERVENTION_FOR = {"engagement_latest": "checkin", "engagement_decline": "checkin",
                    "util_mean_3m": "workload_rebalance", "sustained_overload": "workload_rebalance",
                    "after_hours_mean_3m": "workload_rebalance", "oncall_mean_3m": "workload_rebalance",
                    "promo_clock_over_24": "career_conversation", "months_in_role": "career_conversation",
                    "tenure_months": "career_conversation", "new_joiner": "checkin", "recent_transfer": "checkin"}


def simulate_interventions(conn, run_id: str, held_out: frozenset[str] = frozenset()) -> dict:
    """Simulated HR review of every suggested check-in, then a seeded 80/20 arm assignment for approved
    cases. Controls are recorded but never treated. Outcomes come from eval.potential_outcome.
    Employees in the API replay fixture (held_out) are left unreviewed so the replay starts from a known state."""
    rng = np.random.default_rng(ARM_SEED)
    cases = conn.execute("""SELECT score_id, employee_id, contributions FROM app.score
                            WHERE score_run_id = %s AND priority_band = 'suggested_checkin'
                            ORDER BY employee_id""", (run_id,)).fetchall()
    hr = [r[0] for r in conn.execute("SELECT user_id FROM app.app_user WHERE app_role='hr_partner' ORDER BY 1")]
    rev_rows, int_rows, ev_rows, arms = [], [], [], {}
    pending = 0
    held = 0
    for i, (score_id, emp, contribs) in enumerate(cases, 1):
        if emp in held_out:
            held += 1
            continue
        if rng.random() < PENDING_SHARE:                  # left in the HR queue for the live demo
            pending += 1
            continue
        u = rng.random()
        decision, reason = (("approved", "supportive_checkin") if u < 0.90 else
                            ("dismissed", "context_known_to_hr") if u < 0.98 else ("needs_info", "await_manager_input"))
        reviewer = hr[i % len(hr)]
        day = dt.date(2026, 7, 1) + dt.timedelta(days=int(rng.integers(0, 3)))
        rev_id = f"REV-2026-{i:06d}"
        rev_rows.append((rev_id, score_id, reviewer, decision, reason, None, f"{day} 10:00:00+00",
                         f"{day} 10:05:00+00"))
        if decision != "approved":
            continue
        arm = assign_arm(emp, run_id)                 # same versioned rule as the live API
        top = next(c["feature"] for c in contribs if c["feature"] != "intercept")
        itype = INTERVENTION_FOR[top]
        int_id = f"INT-2026-{len(int_rows) + 1:06d}"
        assigned = f"{day} 12:00:00+00"
        int_rows.append((int_id, emp, score_id, rev_id, itype, reviewer, arm, assigned, "simulated", assigned))
        arms[emp] = arm
        if arm == "treatment":
            steps = [(None, "proposed", 0), ("proposed", "scheduled", 2), ("scheduled", "completed", 9)]
        else:
            steps = [(None, "cancelled", 0)]
        for frm, to, offset in steps:
            when = day + dt.timedelta(days=offset)
            ev_rows.append((f"IEV-2026-{len(ev_rows) + 1:07d}", int_id, frm, to, reviewer, f"{when} 15:00:00+00",
                            f"{when} 15:00:00+00",
                            "Control arm: recorded for evaluation, no outreach" if arm == "control" else None))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO app.review_decision VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", rev_rows)
        cur.executemany("INSERT INTO app.intervention VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", int_rows)
        cur.executemany("INSERT INTO app.intervention_event VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", ev_rows)
    po = pd.DataFrame(conn.execute("""SELECT employee_id, y_if_treated, y_if_untreated FROM eval.potential_outcome
                                      WHERE as_of_date = %s""", (ev.TEST_DATE,)).fetchall(),
                      columns=["employee_id", "y1", "y0"]).set_index("employee_id")
    df = pd.DataFrame({"employee_id": list(arms), "arm": list(arms.values())}).join(po, on="employee_id")
    df["observed"] = np.where(df.arm == "treatment", df.y1, df.y0)
    t, c = df[df.arm == "treatment"], df[df.arm == "control"]
    est = float(t.observed.mean() - c.observed.mean())
    se = float(np.sqrt(t.observed.var(ddof=1) / max(len(t), 1) + c.observed.var(ddof=1) / max(len(c), 1)))
    true = float((df.y1 - df.y0).mean())
    return {"suggested": len(cases), "pending_review": pending, "held_for_api_replay": held,
            "reviewed": len(cases) - pending - held,
            "approved": len(int_rows),
            "dismissed": sum(r[3] == "dismissed" for r in rev_rows),
            "needs_info": sum(r[3] == "needs_info" for r in rev_rows),
            "treatment": int(len(t)), "control": int(len(c)),
            "exit_rate_treatment": float(t.observed.mean()), "exit_rate_control": float(c.observed.mean()),
            "estimated_effect": est, "estimated_effect_ci95": [est - 1.96 * se, est + 1.96 * se],
            "true_simulated_effect": true,
            "allocation": ALLOCATION_VERSION,
            "note": "Synthetic prevention assumption (30% of voluntary exits prevented when treated). The 80/20 "
                    "comparison is small and its interval is wide; this demonstrates the workflow, not real-world "
                    "causal evidence."}


def log_data_exceptions(conn, data_dir: Path) -> dict:
    """Run the G2 detectors over the injected fixtures and record each detection in app.exception_log
    (metadata only; raw payloads stay in the fixtures folder)."""
    import csv
    from generator.validate import label_mismatches_in
    from .asof import audit_engagement_feature, audit_feature_source
    found = []
    fx = data_dir / "fixtures"
    with open(fx / "feature_sources.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if audit_feature_source(r["event_date"], r["recorded_at"], r["as_of_date"]):
                found.append(("feature_source", f"{r['employee_id']}@{r['as_of_date']}:{r['source_id']}:{r['variant']}", "FUTURE_LEAK",
                              "pipeline.asof.audit_feature_source", {"as_of": r["as_of_date"], "source": r["source_table"]}))
    with open(fx / "feature_engagement.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if audit_engagement_feature(r["survey_status"], r["engagement_feature"] or None):
                found.append(("feature_engagement", f"{r['employee_id']}@{r['survey_sent_date']}", "MISSING_AS_NEGATIVE",
                              "pipeline.asof.audit_engagement_feature", {"survey_status": r["survey_status"]}))
    conn.execute("CREATE TEMP TABLE fx_survey (LIKE core.engagement_survey INCLUDING CONSTRAINTS)")
    with open(fx / "core.engagement_survey.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                with conn.transaction():
                    conn.execute(f"INSERT INTO fx_survey ({', '.join(r)}) VALUES ({', '.join(['%s'] * len(r))})",
                                 [None if v == "" else v for v in r.values()])
            except psycopg.errors.CheckViolation as e:
                found.append(("core.engagement_survey", r["survey_id"], "QUARANTINED_ROW", "db.check_constraint",
                              {"constraint": e.diag.constraint_name, "case": "MISSING_AS_NEGATIVE"}))
    conn.execute("CREATE TEMP TABLE fx_label (LIKE eval.horizon_label)")
    with conn.cursor() as cur, cur.copy("COPY fx_label FROM STDIN WITH (FORMAT csv, HEADER true)") as cp:
        cp.write((fx / "eval.horizon_label.csv").read_bytes())
    for key in sorted(label_mismatches_in(conn, "fx_label")):
        found.append(("eval.horizon_label", key, "LABEL_DATE_MISMATCH", "generator.validate.label_matches_outcome", {}))
    rows = [(f"EXC-2026-{i:06d}", ent, rid, issue, det, json.dumps(detail), "2026-07-01 05:00:00+00", "open")
            for i, (ent, rid, issue, det, detail) in enumerate(found, 1)]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO app.exception_log VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    counts = {}
    for _, _, issue, _, _ in found:
        counts[issue] = counts.get(issue, 0) + 1
    return counts


def evaluation_markdown(fz, res: dict, iv: dict) -> str:
    L = ["# Model evaluation (temporal hold-out)", "",
         f"Train: {', '.join(map(str, ev.TRAIN_DATES))} ({fz.train_n:,} rows, {fz.train_pos} positives). "
         f"Validation: {ev.VALIDATION_DATE} ({fz.val_n:,} rows, {fz.val_pos} positives). Frozen {ev.FREEZE_AT}. "
         f"Primary test: {ev.TEST_DATE} (outcomes through 2026-12-31).", "",
         "Target: voluntary termination on or before the second quarter-end after the scoring date.", "",
         "| Metric | Logistic regression | Scorecard |", "|---|---:|---:|"]
    lr, sc = res["logistic_regression"], res["scorecard"]
    fmt = lambda v: f"{v:.3f}"
    for label, key in (("ROC-AUC", "roc_auc"), ("PR-AUC", "pr_auc"), ("Brier", "brier"),
                       ("Calibration slope", "calibration_slope")):
        L.append(f"| {label} | {fmt(lr[key])} | {fmt(sc[key])} |")
    for k in ("top5", "top10"):
        pct = "5%" if k == "top5" else "10%"
        L.append(f"| Precision @ top {pct} | {lr[k]['precision']:.1%} | {sc[k]['precision']:.1%} |")
        L.append(f"| Recall @ top {pct} | {lr[k]['recall']:.1%} | {sc[k]['recall']:.1%} |")
        L.append(f"| Lift @ top {pct} | {lr[k]['lift']:.2f}x | {sc[k]['lift']:.2f}x |")
    L.append(f"| Lift @ 5%, 95% CI (employee bootstrap) | {lr['ci95']['lift5'][0]:.2f}-{lr['ci95']['lift5'][1]:.2f} | "
             f"{sc['ci95']['lift5'][0]:.2f}-{sc['ci95']['lift5'][1]:.2f} |")
    L += ["", f"Test population {lr['n']:,}, positives {lr['positives']} (prevalence {lr['prevalence']:.2%}).", "",
          "## Predeclared diagnostics", "", "| Diagnostic | Value | Target | Met |", "|---|---|---|---|"]
    for k, d in res["diagnostics"].items():
        v = d["value"]
        vs = f"[{v[0]:.2f}, {v[1]:.2f}]" if isinstance(v, list) else f"{v:.3f}"
        L.append(f"| {k} | {vs} | {d.get('target', '> 1')} | {'yes' if d['met'] else '**no**'} |")
    L += ["", "## Simulated interventions (2026-06-30 suggested check-ins)", "",
          f"Suggested {iv['suggested']}; left pending in the HR queue {iv['pending_review']}; "
          f"reviewed {iv['reviewed']}: approved {iv['approved']}, dismissed {iv['dismissed']}, needs info "
          f"{iv['needs_info']}. Arms: treatment {iv['treatment']}, control {iv['control']}.", "",
          f"Voluntary-exit rate by horizon end: treatment {iv['exit_rate_treatment']:.1%}, control "
          f"{iv['exit_rate_control']:.1%}. Estimated effect {iv['estimated_effect']:+.1%} "
          f"(95% CI {iv['estimated_effect_ci95'][0]:+.1%} to {iv['estimated_effect_ci95'][1]:+.1%}); "
          f"true simulated effect in the same group {iv['true_simulated_effect']:+.1%}.", "", iv["note"], "",
          "## Limits", "",
          "- Outcomes are generated by documented synthetic rules, so these metrics show that the scorers recover "
          "the simulated signal (with hidden drivers they cannot see). They are not evidence of real-world "
          "predictive validity.",
          "- A score is a suggestion to check in, never an employment decision. Roughly 6 in 7 people in the top 5% "
          "do not leave within the horizon.",
          "- The scorecard missed its predeclared PR-AUC target; reported as missed, not tuned after the test."]
    return "\n".join(L) + "\n"


def main() -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    from dbutil import admin_url, url_for_database
    url = url_for_database(admin_url(), "retention")
    REPORTS.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(url) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        src = Sources.from_db(conn)
        fz = ev.train(conn, src)                       # train/validation labels only
        (REPORTS / "model_card.json").write_text(ev.dumps(ev.model_card(fz)) + "\n", encoding="utf-8")
        res = ev.evaluate_test(conn, src, fz)          # test labels read only after freezing
        runs = write_scores(conn, src, fz)
        creds = seed_users(conn, runs["logistic_regression"])
        replay = json.loads((DATA_DIR / "fixtures" / "intervention_requests.json").read_text(encoding="utf-8"))
        iv = simulate_interventions(conn, runs["logistic_regression"], frozenset(r["employee_id"] for r in replay))
        exc = log_data_exceptions(conn, DATA_DIR)
        conn.commit()
    (REPORTS / "evaluation.json").write_text(ev.dumps({"results": res, "interventions": iv,
                                                       "data_exceptions_logged": exc}) + "\n", encoding="utf-8")
    (REPORTS / "evaluation.md").write_text(evaluation_markdown(fz, res, iv), encoding="utf-8", newline="\n")
    cred_path = ROOT / "data" / "out" / "demo_credentials.txt"
    cred_path.write_text("# Demo logins (local only, never commit)\n" +
                         "\n".join(f"{u}\t{r}\t{p}" for u, r, p in creds) + "\n", encoding="utf-8")
    print(f"Scores written (runs {runs}). Reports: {REPORTS}. Demo logins: {cred_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
