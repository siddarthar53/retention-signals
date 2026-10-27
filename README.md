# Retention Signals

Synthetic workforce data and an explainable, HR-reviewed workflow for **prioritising supportive
retention conversations**. Scores are suggestions to check in with someone; they are never
employment decisions. All data is synthetic: no real people, names or sensitive attributes.

> Status: **G1 (foundation)**: schema, tests, data dictionary, master-data spec. The generator,
> scoring, API and UI follow in gates G2-G5. Design reference: Agreement v1 (Claude + ChatGPT).

## Requirements (Windows or Linux)

- PostgreSQL 16 (local service), Python 3.12+ (tested on 3.13 and 3.14), Node 20+ (UI, later gates)

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then edit .env: DATABASE_ADMIN_URL, RS_APP_PASSWORD
```

## Commands

| What | Command |
|---|---|
| Create database `retention` and load the schema | `python scripts/db_reset.py` |
| Run all tests (creates/drops its own `retention_test` database) | `python -m pytest` |
| Regenerate the data dictionary (fails if any column is undocumented) | `python scripts/gen_dictionary.py` |

## Layout

| Path | Contents |
|---|---|
| `db/schema.sql` | PostgreSQL schema (core / app / eval), constraints, grants, column descriptions |
| `config/master_data.yaml` | Master-data spec and generation contracts C1-C5 |
| `docs/data_dictionary.md` | Generated data dictionary |
| `docs/enforcement.md` | Which layer enforces each rule |
| `scripts/` | Database reset and dictionary generator |
| `tests/` | pytest suites |

The repository is public: never commit `.env`, passwords or generated data (`data/`, `outputs/` are ignored).
