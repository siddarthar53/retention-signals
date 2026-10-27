# Retention Signals

Synthetic workforce data and an explainable, HR-reviewed workflow for **prioritising supportive
retention conversations**. A score is a suggestion to check in with someone. It is never an employment, pay or
performance decision. All data is synthetic: no real people, names or sensitive attributes.

| | |
|---|---|
| Data | 10 entities, 10,000 employees, 24 months (2025-2026), voluntary attrition 10.3% (required 5-15%) |
| Edge cases | 4 planted issue types with look-alike controls and ground truth: future leakage, missing survey treated as low, label/date mismatch, action without HR approval |
| Checks | 30 semantic validator rules, all 31 foreign keys, byte-identical reruns (SHA-256 manifest), 163 tests |
| Model | Logistic regression + points scorecard, temporal split (train 2025 Q1-Q2, validate Q3, test 2026-06-30 once) |
| App | FastAPI + PostgreSQL + React. HR reviews every suggestion before a manager sees it; managers never see scores |

## Run it on Windows (about 5 minutes)

Needs PostgreSQL 16 running locally, Python 3.12+ and Git. Node is **not** needed: the built UI is committed.

```powershell
git clone https://github.com/srikar045p/retention-signals.git
cd retention-signals
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env        # set your postgres password in DATABASE_ADMIN_URL and choose RS_APP_PASSWORD
```

Build the database, the model and the demo users, then start the app:

```powershell
python scripts/db_reset.py                         # DELETES and recreates database "retention"
python -m generator.run --scale 10000 --load       # generate, validate, load 818,803 rows
python -m pipeline.run                             # train, evaluate, score, demo users
python scripts/replay_requests.py                  # replay planted requests through the API (rolled back)
python -m uvicorn backend.app.main:app --port 8000
```

Open <http://127.0.0.1:8000>. Logins are in `data\out\demo_credentials.txt` (local only, never committed):
`hr1`/`hr2` (HR partner), `manager1`..`manager6`, `itops1`, `admin1`.

Notes for Windows:

- If a password in `DATABASE_ADMIN_URL` contains `@ : / # %`, URL-encode it (`@` is `%40`).
- Windows Smart App Control blocks the DLLs in pandas 3.1.0. `requirements.txt` caps pandas below 3.1;
  3.0.5 is tested. Do not turn Smart App Control off to work around it.
- The app runs on a simulated clock (`APP_NOW`, default 2026-07-15) so the 30 Jun 2026 list is the current one.
- To change the UI: `cd frontend; npm ci; npm run dev` (port 5173, proxies `/api` to 8000), then `npm run build`
  and `python scripts/frontend_provenance.py`. The provenance file records SHA-256 hashes of the UI source,
  lockfile, config and `dist/` together with the build command and Node/Vite versions; its check (and a test)
  proves the committed files still match that record. It does not by itself prove `dist/` was rebuilt from that
  source; to confirm, run `npm ci && npm run build` and the check again.

## What each role sees

| Role | Pages | Never sees |
|---|---|---|
| HR partner | Overview, review queue, full case (factors, workload, survey, scorecard), conversations, data quality, evaluation | Pay, sick leave, raw survey answers |
| Manager | Approved cases in their own team only: themes, conversation guide, progress | Scores, ranks, model numbers, other teams, unapproved cases |
| IT operations | Data quality, evaluation, scoring runs | Individual cases |
| Administrator | Overview, users, runs, data quality, evaluation | Individual cases |

Employees are the subject of the data and do not sign in.

**The approval rule.** Starting a conversation is checked in this order, in one transaction: requester's team scope (403),
current score version (409), latest HR decision is "approved" (409), person still eligible (409), no existing or
comparison-group action (409). Every blocked attempt is written to `app.exception_log`. A manager's authority is
re-checked on every read and write: their own current role must be a people-manager role in the person's current
team, and they must not have left. Owning a conversation never overrides that. Teams with a vacant manager seat
are handled by HR. Approved cases are split 80/20 into conversation and comparison groups by one versioned rule
(`pipeline/allocation.py`), used by both the simulated reviews and the live API.

**What a manager can infer.** Being listed at all tells a manager that HR chose a check-in for that person; this
is inherent to the workflow. Case IDs follow employee order, list order is by employee ID, and topics are
neutral ("Workload and priorities") in a fixed order, so neither rank nor the strongest factor is revealed.

**Sessions** expire on the real clock (8 hours) and are revoked server-side on sign-out; the simulated business
clock is used only for workforce data.

**Least privilege.** The API connects as `rs_app_login`, which cannot read labels, outcomes, the leave ledger, pay bands,
capacity/actual hours or raw survey items, and can only insert into the audit tables (tests assert each denial).

## Results (seed 20261022, test list 30 Jun 2026)

| Measure | Model | Scorecard |
|---|---:|---:|
| Leavers in top 5% vs average (lift) | 6.8x (95% CI 5.5-8.2) | 5.7x |
| Recall in top 10% | 52% | 49% |
| ROC AUC | 0.82 | 0.79 |
| Calibration slope | 0.94 | 0.88 |

Pre-declared checks: three met; "scorecard reaches 75% of the model's PR-AUC" was **not met** (0.51) and is
reported, not tuned away. These are the corrected results after correction C1 (see `docs/corrections.md`); the
test list had already been inspected, so this is a corrected evaluation, not a fresh untouched test.

Replay of all 700 planted approval-rule requests through the API, each required to return its exact status and
reason: 350/350 blocked with the expected reason (no review, dismissed, superseded LR score, other team), 350/350
valid requests created a conversation record with the shared allocation rule, 0 skipped (vacant-manager teams are
replayed by HR). Full reports: `data/out/10000/g2_report.md`, `data/out/reports/evaluation.md`,
`data/out/reports/replay.json`.

## Limitations

- **Synthetic signal strength.** The generator's hazard model makes signals cleaner than real HR data; real-world lift
  would likely be lower. The evaluation rules were fixed before the test list was scored and not retuned afterwards.
- **Circularity.** The model learns the generator's own assumptions (promotion clock, engagement, workload). The results
  show the pipeline works, not that these factors drive attrition in a real company.
- **Intervention effect.** The 80/20 comparison has 60 controls. The estimate (-12.5 points, 95% CI -23.2 to -1.8)
  is far from the simulated truth (-3.2), and it moved by about 5 points after small changes to the simulated review
  cohort and allocation rule (see `docs/corrections.md` C2). It demonstrates the measurement workflow, not causal
  evidence.
- **Single timeline.** Interventions do not re-simulate history; outcomes for treated people come from pre-computed
  potential outcomes (30% prevention assumption).
- Logins are local demo accounts. For a hosted demo, set `SECURE_COOKIES=true` behind HTTPS.

## Commands

| What | Command |
|---|---|
| **DELETES** and recreates database `retention`, loads the schema, creates API login | `python scripts/db_reset.py` |
| Run all tests (creates/drops `retention_test`; API tests use the built `retention` database) | `python -m pytest` |
| Regenerate / check the data dictionary | `python scripts/gen_dictionary.py [--check]` |
| Generate data (starter 500 / full 10,000) into `data/out/<scale>/` | `python -m generator.run --scale 500` |
| Generate, **DELETE and reload** `retention`, validate, write `g2_report.md` | `python -m generator.run --scale 10000 --load` |
| Re-calibrate the attrition intercept on development seeds (only if dynamics change) | `python -m generator.calibrate` |
| Train, freeze, evaluate, write scores, demo logins and simulated interventions | `python -m pipeline.run` |
| Replay the planted approval-rule requests through the API | `python scripts/replay_requests.py` |
| Start the app | `python -m uvicorn backend.app.main:app --port 8000` |

Exports: `data/out/10000/canonical/*.csv` (+ `eval/`), `seed.sql`, `manifest.json` (SHA-256 per file); the data
dictionary is `docs/data_dictionary.md`.

## Layout

| Path | Contents |
|---|---|
| `db/schema.sql` | PostgreSQL schema (core / app / eval), constraints, grants, column descriptions |
| `config/` | Master-data spec (contracts C1-C5) and generator parameters |
| `generator/` | Registry, monthly simulation, derived snapshots and labels, injection, validator, exports |
| `pipeline/` | As-of features, scorecard + logistic regression, temporal evaluation, scoring |
| `backend/app/` | FastAPI: sessions, roles, approval rule (`gate.py`), read models |
| `frontend/` | React + Vite + TypeScript UI (`dist/` is the committed build) |
| `docs/` | Data dictionary, enforcement map, generator design, correction history |
| `tests/` | pytest: schema, generator, pipeline, API |

The repository is public: never commit `.env`, passwords or generated data (`data/`, `outputs/` are ignored).
