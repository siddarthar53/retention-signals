"""Generate docs/data_dictionary.md from the live database catalog.

Every table, view and column in schemas core/app/eval is listed with type, nullability, keys,
check expressions and its description (COMMENT). Exits non-zero if any table, view or column has
no description, so the dictionary cannot silently fall behind the schema.

Usage:  python scripts/gen_dictionary.py [--check]   (--check: verify only, do not write)
Loads db/schema.sql into a scratch database (retention_dict) first.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg  # noqa: E402

from dbutil import ROOT, load_schema, recreate_database  # noqa: E402

OUT = ROOT / "docs" / "data_dictionary.md"
SCHEMAS = ("core", "app", "eval")

RELATIONS = """
SELECT n.nspname, c.relname, c.relkind, obj_description(c.oid, 'pg_class')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','v')
ORDER BY array_position(%s::text[], n.nspname::text), c.relkind, c.relname
"""
COLUMNS = """
SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), col_description(a.attrelid, a.attnum)
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
CONSTRAINTS = """
SELECT conname, contype, pg_get_constraintdef(oid),
       ARRAY(SELECT attname FROM pg_attribute WHERE attrelid = conrelid AND attnum = ANY(conkey))
FROM pg_constraint WHERE conrelid = %s::regclass ORDER BY contype, conname
"""
UNIQUE_INDEXES = """
SELECT i.relname, pg_get_indexdef(i.oid) FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
WHERE x.indrelid = %s::regclass AND x.indisunique AND NOT x.indisprimary
  AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid)
"""


def md(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def main() -> int:
    check_only = "--check" in sys.argv
    url = recreate_database("retention_dict")
    load_schema(url)
    missing: list[str] = []
    out: list[str] = [
        "# Data dictionary",
        "",
        "Generated from the PostgreSQL catalog by `scripts/gen_dictionary.py` (do not edit by hand).",
        "Types, nullability, keys and check expressions are the enforced definitions; descriptions come "
        "from the `COMMENT`s in `db/schema.sql`. Rules enforced outside the database (validator, service "
        "layer) are listed in `docs/enforcement.md`.",
        "",
    ]
    with psycopg.connect(url) as conn:
        rels = conn.execute(RELATIONS, (list(SCHEMAS), list(SCHEMAS))).fetchall()
        out.append("## Contents\n")
        for schema, name, kind, _ in rels:
            out.append(f"- [`{schema}.{name}`](#{schema}{name}){' (view)' if kind == 'v' else ''}")
        out.append("")
        for schema, name, kind, comment in rels:
            fq = f"{schema}.{name}"
            if not comment:
                missing.append(f"{fq} (table/view description)")
            cols = conn.execute(COLUMNS, (fq,)).fetchall()
            cons = conn.execute(CONSTRAINTS, (fq,)).fetchall() if kind == "r" else []
            uidx = conn.execute(UNIQUE_INDEXES, (fq,)).fetchall() if kind == "r" else []
            col_keys: dict[str, list[str]] = defaultdict(list)
            for cname, ctype, cdef, ccols in cons:
                tag = {"p": "PK", "f": "FK", "u": "UNIQUE"}.get(ctype)
                if tag:
                    for col in ccols:
                        col_keys[col].append(tag if tag != "FK" else "FK " + cdef.split("REFERENCES ")[1])
            out.append(f"## `{fq}`" + (" (view)" if kind == "v" else ""))
            out.append("")
            out.append(md(comment) or "_(no description)_")
            out.append("")
            out.append("| Column | Type | Null | Keys | Default | Description |")
            out.append("|---|---|---|---|---|---|")
            for col, typ, notnull, default, desc in cols:
                if not desc:
                    missing.append(f"{fq}.{col}")
                keys = ", ".join(dict.fromkeys(col_keys.get(col, [])))
                out.append(f"| `{col}` | {typ} | {'no' if notnull else 'yes'} | {md(keys)} | "
                           f"{md(default)} | {md(desc)} |")
            checks = [(n, d) for n, t, d, _ in cons if t == "c"]
            multi = [(n, d) for n, t, d, cc in cons if t in ("u", "f") and len(cc) > 1]
            if checks or multi or uidx:
                out.append("")
                out.append("**Constraints**")
                out.append("")
                for n, d in checks + multi:
                    out.append(f"- `{n}`: `{md(d)}`")
                for n, d in uidx:
                    out.append(f"- `{n}`: `{md(d.split(' USING ')[1] if ' USING ' in d else d)}` (unique index)")
            out.append("")
    if missing:
        print(f"Dictionary coverage FAILED: {len(missing)} item(s) without a description:")
        for m in missing:
            print("  -", m)
        return 1
    if not check_only:
        OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"Wrote {OUT.relative_to(ROOT)}: {len(rels)} tables/views, full column coverage.")
    else:
        print(f"Dictionary coverage OK: {len(rels)} tables/views, every column described.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
