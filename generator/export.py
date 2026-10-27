"""Deterministic CSV/JSON export, manifest with SHA-256, and single-transaction PostgreSQL load.

Folders: canonical/ (core tables, the clean dataset), eval/ (restricted evaluation tables),
fixtures/ (injected anomalies, never loaded as canonical data). Nothing generated is committed.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from .derived import OBSERVATION_END

# (folder, table, columns). Load order = parents before children. Column order = db/schema.sql.
TABLES: list[tuple[str, str, list[str]]] = [
    ("canonical", "core.compensation_band", ["band_id", "level", "currency", "min_amount", "mid_amount", "max_amount"]),
    ("canonical", "core.role", ["role_id", "role_title", "job_family", "level", "band_id", "is_people_manager"]),
    ("canonical", "core.team", ["team_id", "team_name", "job_family", "created_date"]),
    ("canonical", "core.employee", ["employee_id", "hire_date", "recorded_at"]),
    ("canonical", "core.employment_event", ["event_id", "employee_id", "event_type", "event_date", "exit_type",
                                            "notice_days", "recorded_at"]),
    ("canonical", "core.assignment", ["assignment_id", "employee_id", "team_id", "role_id", "effective_from",
                                      "change_reason", "previous_assignment_id", "recorded_at"]),
    ("canonical", "core.promotion_history", ["promotion_id", "employee_id", "from_role_id", "to_role_id",
                                             "assignment_id", "effective_date", "recorded_at"]),
    ("canonical", "core.tenure_snapshot", ["snapshot_id", "employee_id", "as_of_date", "assignment_id",
                                           "tenure_months", "months_in_role", "months_since_promotion",
                                           "derived_from", "recorded_at"]),
    ("canonical", "core.workload_snapshot", ["workload_id", "employee_id", "period_month", "scheduled_workdays",
                                             "available_hours", "actual_hours", "utilization_pct",
                                             "after_hours_pct", "oncall_days", "recorded_at"]),
    ("canonical", "core.leave_ledger", ["leave_txn_id", "employee_id", "leave_type", "txn_type", "days",
                                        "txn_date", "leave_year", "recorded_at"]),
    ("canonical", "core.engagement_survey", ["survey_id", "employee_id", "wave", "sent_date", "response_status",
                                             "q1", "q2", "q3", "q4", "q5", "items_answered", "engagement_index",
                                             "submitted_at", "recorded_at"]),
    ("canonical", "core.attrition_outcome", ["outcome_id", "employee_id", "final_status", "resignation_date",
                                             "termination_date", "scheduled_termination_date", "observation_end",
                                             "recorded_at"]),
    ("eval", "eval.horizon_label", ["employee_id", "as_of_date", "horizon_end", "label_status", "ineligible_reason",
                                    "target", "outcome_id"]),
    ("eval", "eval.potential_outcome", ["employee_id", "as_of_date", "y_if_treated", "y_if_untreated"]),
    ("eval", "eval.ground_truth", ["gt_id", "case_id", "entity", "record_id", "issue_type", "expected_detection",
                                   "is_control", "rule_version"]),
]
OUTCOME_RECORDED = "2027-01-05 09:00:00+00"


def rows(ds) -> dict[str, list[tuple]]:
    """Table name -> rows sorted by primary key. `ds` is a generated Dataset with IDs assigned."""
    reg = ds.reg
    out = {
        "core.compensation_band": [(b.id, b.level, "SYN", f"{b.min_amount:.2f}", f"{b.mid_amount:.2f}",
                                    f"{b.max_amount:.2f}") for b in reg.bands],
        "core.role": [(r.id, r.title, r.job_family, r.level, r.band.id, r.is_people_manager) for r in reg.roles],
        "core.team": [(t.id, t.name, t.job_family, t.created_date) for t in reg.teams],
        "core.employee": [(e.id, e.hire_date, e.recorded_at) for e in reg.employees],
        "core.employment_event": [(e.id, e.employee.id, e.event_type, e.event_date, e.exit_type, e.notice_days,
                                   e.recorded_at) for e in reg.events],
        "core.assignment": [(a.id, a.employee.id, a.team.id, a.role.id, a.effective_from, a.change_reason,
                             a.previous.id if a.previous else None, a.recorded_at) for a in reg.assignments],
        "core.promotion_history": [(p.id, p.employee.id, p.from_role.id, p.to_role.id, p.assignment.id,
                                    p.effective_date, p.recorded_at) for p in reg.promotions],
        "core.tenure_snapshot": [(s.id, s.employee.id, s.as_of, s.assignment.id, s.tenure_months, s.months_in_role,
                                  s.months_since_promotion, s.derived_from, s.recorded_at) for s in ds.snapshots],
        "core.workload_snapshot": [(w.id, w.employee.id, w.period_month, w.scheduled_workdays,
                                    f"{w.available_hours}.0", w.actual_hours, w.utilization_pct, w.after_hours_pct,
                                    w.oncall_days, w.recorded_at) for w in reg.workload],
        "core.leave_ledger": [(t.id, t.employee.id, t.leave_type, t.txn_type, t.days, t.txn_date, t.txn_date.year,
                               t.recorded_at) for t in reg.leave],
        "core.engagement_survey": [(s.id, s.employee.id, s.wave, s.sent_date, s.response_status, *s.items,
                                    sum(v is not None for v in s.items), s.engagement_index, s.submitted_at,
                                    s.recorded_at) for s in reg.surveys],
        "core.attrition_outcome": [(o.id, o.employee.id, o.final_status, o.resignation_date, o.termination_date,
                                    o.scheduled_termination_date, OBSERVATION_END, OUTCOME_RECORDED)
                                   for o in ds.outcomes.values()],
        "eval.horizon_label": [(lb.employee.id, lb.as_of, lb.horizon_end, lb.status, lb.ineligible_reason, lb.target,
                                lb.outcome.id) for lb in ds.labels],
        "eval.potential_outcome": [(p.employee.id, p.as_of, p.y_if_treated, p.y_if_untreated)
                                   for p in ds.potential_outcomes],
        "eval.ground_truth": [tuple(g) for g in ds.ground_truth],
    }
    keyed = {"eval.horizon_label": lambda r: (r[0], r[1]), "eval.potential_outcome": lambda r: (r[0], r[1])}
    return {k: sorted(v, key=keyed.get(k, lambda r: r[0])) for k, v in out.items()}


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def to_csv(columns: list[str], data: list[tuple]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in data:
        w.writerow([_cell(v) for v in r])
    return buf.getvalue().encode("utf-8")


def to_json(columns: list[str], data: list[tuple]) -> bytes:
    recs = [{c: (None if v is None else (v if isinstance(v, (bool, int)) else str(v))) for c, v in zip(columns, r)}
            for r in data]
    return (json.dumps(recs, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def write_dataset(ds, out_dir: Path, meta: dict, extra_files: dict[str, bytes] | None = None) -> dict:
    """Write CSV + JSON per table, extra files (fixtures, reports) and manifest.json; return the manifest."""
    data = rows(ds)
    files = {}
    for folder, table, cols in TABLES:
        (out_dir / folder).mkdir(parents=True, exist_ok=True)
        for ext, blob in (("csv", to_csv(cols, data[table])), ("json", to_json(cols, data[table]))):
            rel = f"{folder}/{table}.{ext}"
            (out_dir / rel).write_bytes(blob)
            files[rel] = {"rows": len(data[table]), "sha256": hashlib.sha256(blob).hexdigest()}
    for rel, blob in (extra_files or {}).items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        files[rel] = {"sha256": hashlib.sha256(blob).hexdigest()}
    manifest = {"meta": meta, "files": dict(sorted(files.items()))}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
                                           encoding="utf-8", newline="\n")
    return manifest


def load(conn, out_dir: Path, include_eval: bool = True) -> dict[str, int]:
    """COPY every table in dependency order inside the caller's transaction.

    Any PK / FK / composite-FK / UNIQUE / CHECK violation raises; the caller's transaction rolls back.
    """
    counts = {}
    for folder, table, cols in TABLES:
        if folder == "eval" and not include_eval:
            continue
        path = out_dir / folder / f"{table}.csv"
        with conn.cursor() as cur:
            with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv, HEADER true)") as cp:
                cp.write(path.read_bytes())
        counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return counts


def seed_sql(out_dir: Path) -> bytes:
    """A single SQL seed file (psql COPY blocks) that reproduces the loaded tables."""
    parts = ["-- Retention Signals seed data. Load into a database created from db/schema.sql:\n"
             "--   psql -d retention -v ON_ERROR_STOP=1 -1 -f seed.sql\n"]
    for folder, table, cols in TABLES:
        blob = (out_dir / folder / f"{table}.csv").read_text(encoding="utf-8")
        body = blob.split("\n", 1)[1]
        parts.append(f"\nCOPY {table} ({', '.join(cols)}) FROM stdin WITH (FORMAT csv);\n{body}\\.\n")
    return "".join(parts).encode("utf-8")
