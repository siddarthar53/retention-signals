"""Deterministic CSV export, manifest with SHA-256, and single-transaction PostgreSQL load."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from .registry import Registry

# Load order = parents before children. Column order = db/schema.sql.
TABLES: list[tuple[str, list[str]]] = [
    ("core.compensation_band", ["band_id", "level", "currency", "min_amount", "mid_amount", "max_amount"]),
    ("core.role", ["role_id", "role_title", "job_family", "level", "band_id", "is_people_manager"]),
    ("core.team", ["team_id", "team_name", "job_family", "created_date"]),
    ("core.employee", ["employee_id", "hire_date", "recorded_at"]),
    ("core.employment_event", ["event_id", "employee_id", "event_type", "event_date", "exit_type",
                               "notice_days", "recorded_at"]),
    ("core.assignment", ["assignment_id", "employee_id", "team_id", "role_id", "effective_from",
                         "change_reason", "previous_assignment_id", "recorded_at"]),
    ("core.promotion_history", ["promotion_id", "employee_id", "from_role_id", "to_role_id",
                                "assignment_id", "effective_date", "recorded_at"]),
]


def rows(reg: Registry) -> dict[str, list[tuple]]:
    """Table name -> rows sorted by primary key. IDs must already be assigned."""
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
    }
    return {k: sorted(v, key=lambda r: r[0]) for k, v in out.items()}


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


def write_canonical(reg: Registry, out_dir: Path, meta: dict) -> dict:
    """Write one CSV per table and manifest.json; return the manifest."""
    canon = out_dir / "canonical"
    canon.mkdir(parents=True, exist_ok=True)
    data = rows(reg)
    files = {}
    for table, cols in TABLES:
        blob = to_csv(cols, data[table])
        name = f"{table}.csv"
        (canon / name).write_bytes(blob)
        files[f"canonical/{name}"] = {"rows": len(data[table]), "sha256": hashlib.sha256(blob).hexdigest()}
    manifest = {"meta": meta, "files": files}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
                                           encoding="utf-8", newline="\n")
    return manifest


def load(conn, out_dir: Path) -> dict[str, int]:
    """COPY every canonical table in dependency order inside the caller's transaction.

    Any PK / FK / composite-FK / UNIQUE / CHECK violation raises and the caller's transaction rolls back.
    """
    counts = {}
    for table, cols in TABLES:
        path = out_dir / "canonical" / f"{table}.csv"
        with conn.cursor() as cur:
            with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv, HEADER true)") as cp:
                cp.write(path.read_bytes())
        counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return counts
