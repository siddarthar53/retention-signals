# Retention Signals

Synthetic workforce data and an explainable, HR-reviewed workflow for **prioritising supportive
retention conversations**. Scores are suggestions to check in with someone; they are never
employment decisions. All data is synthetic: no real people, names or sensitive attributes.

> Status: **G1 accepted; G2 (generator) and G4 (model pipeline) built, awaiting review.** API and UI follow in G5. Design reference: Agreement v1 (Claude + ChatGPT).

## Requirements (Windows or Linux)

- PostgreSQL 16 (local service), Python 3.12+, Node 20+ (UI, later gates)
- Reported test runs so far: Linux, PostgreSQL 16.15, Python 3.13.17 (`requirements.lock`). The target
  Windows machine (Python 3.14.7, PostgreSQL 16.15) has not run the suite yet.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.lock        # exact tested versions (requirements.txt = ranges)
copy .env.example .env            # then edit .env: DATABASE_ADMIN_URL, RS_APP_PASSWORD
```

## Commands

| What | Command |
|---|---|
| **DELETES** and recreates database `retention`, loads the schema, creates API login | `python scripts/db_reset.py` |
| Run all tests (creates/drops its own `retention_test` database) | `python -m pytest` |
| Regenerate the data dictionary (fails if any column is undocumented) | `python scripts/gen_dictionary.py` |
| Check the committed dictionary is complete and up to date | `python scripts/gen_dictionary.py --check` |
| Generate data (starter 500 / full 10,000) into `data/out/<scale>/` | `python -m generator.run --scale 500` |
| Generate, **DELETE and reload** `retention`, validate, write `g2_report.md` | `python -m generator.run --scale 10000 --load` |
| Re-calibrate the attrition intercept on development seeds (only if dynamics change) | `python -m generator.calibrate` |
| Train, freeze, evaluate on the 2026-06-30 hold-out, write scores, demo logins and simulated interventions | `python -m pipeline.run` |

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
