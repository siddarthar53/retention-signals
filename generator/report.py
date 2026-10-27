"""G2 report: the reviewable evidence ChatGPT asked for (design v0.2 section 2.3)."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
from pathlib import Path

from pipeline.asof import audit_engagement_feature, audit_feature_source

from . import validate
from .derived import QUARTER_ENDS

TABLE_PKS = {
    "core.compensation_band": ["band_id"], "core.role": ["role_id"], "core.team": ["team_id"],
    "core.employee": ["employee_id"], "core.employment_event": ["event_id"], "core.assignment": ["assignment_id"],
    "core.promotion_history": ["promotion_id"], "core.tenure_snapshot": ["snapshot_id"],
    "core.workload_snapshot": ["workload_id"], "core.leave_ledger": ["leave_txn_id"],
    "core.engagement_survey": ["survey_id"], "core.attrition_outcome": ["outcome_id"],
    "eval.horizon_label": ["employee_id", "as_of_date"], "eval.potential_outcome": ["employee_id", "as_of_date"],
    "eval.ground_truth": ["gt_id"],
}

FK_ORPHAN_SQL = """
SELECT c.conname, c.conrelid::regclass::text, c.confrelid::regclass::text,
       pg_get_constraintdef(c.oid) FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE c.contype = 'f' AND n.nspname IN ('core','app','eval') ORDER BY 2, 1"""


def _fk_orphans(conn) -> list[tuple]:
    out = []
    for name, child, parent, _ in conn.execute(FK_ORPHAN_SQL).fetchall():
        cols = conn.execute("""SELECT array_agg(a.attname ORDER BY k.ord), array_agg(fa.attname ORDER BY k.ord)
            FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY k(ck, fk, ord)
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.ck
            JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = k.fk
            WHERE c.conname = %s AND c.conrelid = %s::regclass GROUP BY c.oid""", (name, child)).fetchone()
        ccols, pcols = cols
        on = " AND ".join(f"p.{p} = c.{c}" for c, p in zip(ccols, pcols))
        notnull = " AND ".join(f"c.{c} IS NOT NULL" for c in ccols)
        n = conn.execute(f"SELECT count(*) FROM {child} c WHERE {notnull} AND NOT EXISTS "
                         f"(SELECT 1 FROM {parent} p WHERE {on})").fetchone()[0]
        out.append((name, child, parent, n))
    return out


def _detection(conn, ds, out_dir: Path) -> list[tuple]:
    """Precision/recall of the detectors available at G2 against ground truth."""
    gt = ds.ground_truth
    results = []

    def score(issue, entity, detected: set[str]):
        rows = [g for g in gt if g[4] == issue and g[2] == entity]
        pos = {g[3] for g in rows if not g[6]}
        neg = {g[3] for g in rows if g[6]}
        tp, fn, fp = len(pos & detected), len(pos - detected), len(neg & detected)
        results.append((issue, entity, len(pos), len(neg), tp, fn, fp))

    # FUTURE_LEAK via the shared as-of rule
    det = set()
    with open(out_dir / "fixtures" / "feature_sources.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if audit_feature_source(r["event_date"], r["recorded_at"], r["as_of_date"]):
                det.add(f"{r['employee_id']}@{r['as_of_date']}:{r['source_id']}")
    score("FUTURE_LEAK", "feature_source", det)
    # MISSING_AS_NEGATIVE (a): rows the database refuses (quarantine)
    det = set()
    with open(out_dir / "fixtures" / "core.engagement_survey.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        with conn.transaction():
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS fx_survey (LIKE core.engagement_survey INCLUDING CONSTRAINTS)")
        try:
            with conn.transaction():
                cols = list(r)
                conn.execute(f"INSERT INTO fx_survey ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                             [None if v == "" else v for v in r.values()])
        except Exception:
            det.add(r["survey_id"])
    score("MISSING_AS_NEGATIVE", "core.engagement_survey", det)
    # MISSING_AS_NEGATIVE (b): feature audit
    det = set()
    with open(out_dir / "fixtures" / "feature_engagement.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if audit_engagement_feature(r["survey_status"], r["engagement_feature"] or None):
                det.add(f"{r['employee_id']}@{r['survey_sent_date']}")
    score("MISSING_AS_NEGATIVE", "feature_engagement", det)
    # LABEL_DATE_MISMATCH via the validator's label rule
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS fx_label (LIKE eval.horizon_label)")
        with conn.cursor() as cur, cur.copy("COPY fx_label FROM STDIN WITH (FORMAT csv, HEADER true)") as cp:
            cp.write((out_dir / "fixtures" / "eval.horizon_label.csv").read_bytes())
    score("LABEL_DATE_MISMATCH", "eval.horizon_label", validate.label_mismatches_in(conn, "fx_label"))
    return results


def write_report(conn, ds, manifest: dict, out_dir: Path) -> Path:
    L = []
    w = L.append
    st, meta = ds.stats, manifest["meta"]
    w(f"# G2 data report: scale {meta['scale']}, seed {meta['seed']}")
    w("")
    w(f"Config SHA-256 `{meta['config_sha256'][:16]}…`, generator config `{meta['generator_config_sha256'][:16]}…`, "
      f"b0 = {meta['b0']}. Generated with `python -m generator.run --scale {meta['scale']} --load`.")
    w("")
    w("## 1. Row counts and keys")
    w("")
    w("| Table | Rows | PK/unique duplicates |")
    w("|---|---:|---:|")
    for table, pk in TABLE_PKS.items():
        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        dup = conn.execute(f"SELECT count(*) - count(DISTINCT ({', '.join(pk)})) FROM {table}").fetchone()[0]
        w(f"| `{table}` | {n:,} | {dup} |")
    w("")
    orphans = _fk_orphans(conn)
    w(f"Foreign keys checked: {len(orphans)} (incl. composite same-employee keys). "
      f"Orphan rows: **{sum(o[3] for o in orphans)}**.")
    w("")
    w("## 2. Semantic validator")
    w("")
    res = validate.run(conn)
    w("| Check | Violations |")
    w("|---|---:|")
    for k, v in res.items():
        w(f"| {k} | {len(v)} |")
    w("")
    w("## 3. Scoring population and join contract (per quarter-end)")
    w("")
    w("| as_of | Employed E | Snapshots | Labels | Ineligible: resignation known | Ineligible: insufficient history | Unresolved | Scoring population S | Positives |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for q in QUARTER_ENDS:
        r = conn.execute("""
            SELECT (SELECT count(*) FROM core.employee e LEFT JOIN core.attrition_outcome o USING (employee_id)
                    WHERE e.hire_date <= %(q)s AND (o.termination_date IS NULL OR o.termination_date > %(q)s)),
                   (SELECT count(*) FROM core.tenure_snapshot WHERE as_of_date = %(q)s),
                   count(*), count(*) FILTER (WHERE ineligible_reason = 'resignation_known'),
                   count(*) FILTER (WHERE ineligible_reason = 'insufficient_history'),
                   count(*) FILTER (WHERE label_status = 'unresolved'),
                   count(*) FILTER (WHERE label_status <> 'ineligible'),
                   count(*) FILTER (WHERE target = 1)
            FROM eval.horizon_label WHERE as_of_date = %(q)s""", {"q": q}).fetchone()
        w(f"| {q} | " + " | ".join(f"{x:,}" for x in r) + " |")
    w("")
    w("E = snapshots = labels for every quarter; S = labels minus ineligible. Feature rows must equal S "
      "(enforced at G4).")
    w("")
    w("## 4. Population, attrition and hires")
    w("")
    oc = dict(conn.execute("SELECT final_status, count(*) FROM core.attrition_outcome GROUP BY 1").fetchall())
    n = sum(oc.values())
    w(f"- Employee histories: **{n:,}** (initial {st['initial']:,} + in-window hires {n - st['initial']:,}).")
    w(f"- **Voluntary attrition (voluntary terminations 2025-2026 / N): "
      f"{100 * oc.get('exited_voluntary', 0) / n:.2f}%** (required 5-15%, target 10%).")
    w(f"- Involuntary exits: {100 * oc.get('exited_involuntary', 0) / n:.2f}%; still on notice at 2026-12-31: "
      f"{oc.get('on_notice', 0)}; active: {oc.get('active', 0)}.")
    hires = st["backfill_hires"] + st["growth_hires"]
    w(f"- In-window hires: backfill {st['backfill_hires']} ({100 * st['backfill_hires'] / max(hires, 1):.1f}%, "
      f"target 70%), growth {st['growth_hires']}; external manager hires {st['external_manager_hires']}; "
      f"internal manager fills {st['internal_manager_fills']}; vacancies open at end {st['vacancies_open_at_end']}.")
    w(f"- Promotions in window {st['promotions']}; transfers {st['transfers']}.")
    surveys = dict(conn.execute("SELECT response_status, count(*) FROM core.engagement_survey GROUP BY 1").fetchall())
    ns = sum(surveys.values())
    w(f"- Survey responses: complete {100 * surveys.get('complete', 0) / ns:.1f}%, partial "
      f"{100 * surveys.get('partial', 0) / ns:.1f}%, none {100 * surveys.get('no_response', 0) / ns:.1f}% "
      f"(design 76/4/20 before workload-related non-response).")
    po = conn.execute("SELECT count(*), sum(y_if_untreated), sum(y_if_treated) FROM eval.potential_outcome").fetchone()
    w(f"- Intervention potential outcomes (June 2026 scoring population): {po[0]:,} people; voluntary exits "
      f"untreated {po[1]}, if all treated {po[2]} (prevention probability "
      f"{ds.gcfg['treatment']['prevention_prob']}; synthetic assumption).")
    w("")
    w("## 5. Injected anomalies vs ground truth (detectors available at G2)")
    w("")
    w("| Issue | Target | Injected | Controls | Caught | Missed | False positives |")
    w("|---|---|---:|---:|---:|---:|---:|")
    for r in _detection(conn, ds, out_dir):
        w("| " + " | ".join(str(x) for x in r) + " |")
    req = json.loads((out_dir / "fixtures" / "intervention_requests.json").read_text(encoding="utf-8"))
    w(f"| UNREVIEWED_SCORE | api.intervention_request | {sum(r['expected'] != 'none' for r in req)} | "
      f"{sum(r['expected'] == 'none' for r in req)} | enforced at G5 | | |")
    w("")
    w("## 6. Reproducibility")
    w("")
    w("Per-file SHA-256 is in `manifest.json`. Two runs with the same seed and config must produce identical "
      "manifests (`tests/generator/test_history.py::test_two_runs_identical`).")
    path = out_dir / "g2_report.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")
    return path
