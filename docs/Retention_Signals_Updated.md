# Retention Signals: prompts and build workflow

Oct 30, 2026 · @Siddarth Madgula

Retention Signals was built in about 48 hours (26–28 Oct 2026) by Claude, reviewed by ChatGPT/Codex in Slack #problemstate, and steered by Siddarth's prompts. This doc records the prompt to reuse, the prompts as given, and the gated workflow that produced the app.

## The problem statement

The brief: analyze synthetic engagement, tenure, role and workload signals to prioritize supportive retention conversations. It is two builds in one, a synthetic data generator and a prioritization workflow, and the acceptance criteria judge both.

- **Data:** 10 related entities (Employee, Role, Team, TenureSnapshot, EngagementSurvey, WorkloadSnapshot, CompensationBand, PromotionHistory, AttritionOutcome, Intervention), 10,000 employees over 24 months, 5–15% attrition, deterministic IDs, a fixed seed.
- **Edge cases:** 4 labelled anomalies with a separate ground-truth file: future information leaking into features, missing survey treated as negative, outcome label inconsistent with the termination date, and an individual score acted on without explanation or human review.
- **Proof:** a data dictionary, validation of keys, joins, dates and derived values, CSV + JSON + SQL exports, README, source manifest, and a same-seed reproducibility check.
- **Acceptance:** temporal evaluation (train earlier, test later), explainable factors for every score, no sensitive attributes, and supportive signals with human review, never automatic employment decisions.
- **Actors:** employee, manager, HR partner, IT/ops partner, system admin.

## The master prompt

Use this single prompt to reproduce the build. It folds every decision made over the 48 hours into one instruction, so a new session starts where we finished rather than where we began.

```text
Role: You are the builder. A second AI (the reviewer) checks every step in Slack channel #problemstate.
Do not agree by default. Challenge each other, ask for proof, and fix confirmed findings before moving on.
Agree each gate with the reviewer BEFORE writing code, to avoid rewrites of code and master data.

Goal: within 48 hours, a working, tested solution that analyzes synthetic engagement, tenure, role and
workload signals to prioritize SUPPORTIVE retention conversations. A score is a prompt for a check-in,
never a performance, pay or employment decision.

Data (synthetic only, no real people, no sensitive attributes or proxies):
- 10 entities: Employee, Role, Team, TenureSnapshot, EngagementSurvey, WorkloadSnapshot,
  CompensationBand, PromotionHistory, AttritionOutcome, Intervention. PK on every table, FK on every
  relationship, effective-dated append-only history, an employee-team assignment bridge.
- 10,000 employees, 24 months (2025-2026), voluntary attrition 5-15%, fixed seed 20261022,
  a --scale switch so starter and full sets come from one generator.
- Generate from a monthly causal simulation (latent engagement, workload, external pull, manager
  quality), so outcomes follow documented rules, not coin flips. Include benign overload, transfers,
  missing surveys. Derived values (tenure, averages, labels) are computed from source rows.
- Leave policy: 1 earned-leave day accrues per month (mid-year joiners from their join month),
  3 sick-leave days a year, all unused leave lapses on 31 December. Sick leave never feeds the model.
- Inject 4 labelled edge cases with look-alike controls and a separate ground truth: future leak,
  missing survey treated as low, label/date mismatch, action on an unreviewed score.
- Validate keys, joins, dates, arithmetic and lifecycles; export CSV, JSON and SQL with a SHA-256
  manifest; prove two same-seed runs are byte-identical.

Model: as-of features (event date AND recorded_at <= cutoff), missing data neutral, never negative.
Logistic regression plus a rule-based scorecard. Train on 2025 Q1-Q2, tune on Q3, freeze, test once
on 30 Jun 2026. Report ROC, PR-AUC, lift and calibration with bootstrap intervals, and pre-declare
the pass checks. Every score carries per-factor contributions and full source lineage.

App: PostgreSQL 16 (local), Python FastAPI, React + Vite + TypeScript, Windows-runnable.
- HR partner: review queue, full case with factors and evidence, approve / dismiss / needs info.
- Manager: only HR-approved cases in their current team, neutral topics, no scores or ranks.
- Employee: own records only (role, team, manager, tenure, workload, earned leave), never a score.
- IT ops / admin: data quality, evaluation, runs, users.
- A server-side gate blocks and logs any action on an unreviewed, stale, out-of-scope or
  ineligible case. Least-privilege DB role, sessions on the real clock, 80/20 comparison group.

Done means: all tests pass with zero skips on Windows, the edge-case replay is exact, the reviewer
approves, and the README states the limitations honestly.
```

## The prompts as given

Twenty-four prompts steered the build. They are listed in order, with spelling tidied and the meaning unchanged; setup chatter (password resets, terminal output) is left out.

| # | Phase | Prompt | What it led to |
| --- | --- | --- | --- |
| 1 | Understand | Go through the problem statement and tell me each and every point of it clearly. | 10-part breakdown: two builds in one, 8 open decisions |
| 2 | Understand | Check access to my new Slack channel, problemstate. | Channel #problemstate confirmed |
| 3 | Understand | Send everything on this problem statement to that channel and wait for ChatGPT. | Breakdown posted in 5 parts for ChatGPT |
| 4 | Collaborate | Don't agree with whatever ChatGPT shares: cross-check, question and ask for proof of concept, and ask it to do the same with you. | Mutual challenge rule for the whole build |
| 5 | Scope | We have 48 hours for working code. Aim for good UI, backend and database. Don't lock the stack yet; a free-tier DB like Supabase and a Python backend are fine; good UI is required, better UX if time permits. Finalize architecture, schema and master data before code. | Architecture-first plan, 48-hour clock |
| 6 | Scope | Use PostgreSQL instead of SQLite: local for dev, Supabase or equivalent for the demo. Include earned leave in workload; check other points with ChatGPT. | PostgreSQL 16; leave added to the data model |
| 7 | Scope | Leave: keep it simple. Mid-year joiners accrue for the months left, plus 3 sick leaves. Get the architecture, table schema and flow diagrams agreed with ChatGPT. | Leave rules fixed; diagrams agreed |
| 8 | Scope | Unused leave lapses and can't be carried into next year. ChatGPT builds, you review. | No carry-forward; first role split |
| 9 | Design | How will we train the model, which model fits, logins for employees, and how will we generate records to train and test for success rate? | Logistic regression + scorecard, temporal split, employees as data subjects |
| 10 | Design | Tell me when we start the real work and where. | Gate plan G1–G5 |
| 11 | Setup | Created the repo retention-signals; check what ChatGPT can't run and how to test. | Repo set up; Codex sandbox limits found |
| 12 | Setup | Why install Ubuntu for a POC? Are we over-complicating it? | Dropped WSL; native Windows + PostgreSQL |
| 13 | Build | Let's move with option B: Claude builds, ChatGPT reviews. How will we get the records? | Role swap; generator design |
| 14 | Build | Check with Codex step by step to avoid code rewrites and master-data rework; proceed once both agree. | Agree-before-code rule per gate |
| 15 | Build | How will master data be generated, and will all primary and other keys map correctly? It is the core for the ML model. | Registry with deterministic IDs and composite keys |
| 16 | Build | Codex hit its limit: carry on with data generation and keep posting in Slack; for crucial things use a new Claude agent as reviewer. | Independent Claude reviewer; Slack as the log |
| 17 | Verify | How far did we get? Is data generation complete? | Status check; work resumed |
| 18 | Verify | I can't see any data in the DB. | Windows setup steps; first local build |
| 19 | Verify | Smart App Control blocked the last command. | pandas pinned below 3.1 |
| 20 | Verify | Checked all logins and screens; the URL trick loaded the manager UI only. Now ask Codex to finish its checks. | Codex review: 1 blocker, 5 majors fixed |
| 21 | Polish | After Approve the button is still clickable, and I must scroll to the bottom to start a conversation. | Review form collapses; next step moves up |
| 22 | Extend | Add an employee login to see own workload, leave, tenure, manager and team; and how can HR or managers see the data behind a decision? | Scope addendum v1 |
| 23 | Extend | First check these with Codex. HR evidence only; managers stay the same; employee data is up to you and Claude. | HR evidence panel + bounded My data page |
| 24 | Release | Done, changes are working as expected for me. | Windows evidence for the merge candidate |

## Build workflow

Every gate ran the same loop: agree the design, build, test, independent review, then a run on Siddarth's Windows laptop. A finding at either check sent the work back to build before the next gate could start.

&#91;embedded content: build workflow · one loop per gate\]

Gate numbers follow the original plan; G3 has no separate entry in this log.

## Gate log

Six gates, each agreed before code and closed only on evidence. Commits are on github.com/srikar045p/retention-signals.

| Gate | Delivered | Review | Evidence | Commit |
| --- | --- | --- | --- | --- |
| G0 Agree | Problem breakdown, 8 open decisions, architecture, schema and flow diagrams | ChatGPT challenged each decision in Slack | Design page v0.5, Agreement v1 | none (design) |
| G1 Schema | PostgreSQL schema: 24 tables, 2 views, named constraints, append-only triggers, least-privilege roles, data dictionary | ChatGPT found 6 schema bugs, all fixed | Schema tests pass | aa37bb3 |
| G2 Data | Monthly causal generator, 4 injected edge cases with controls, 30-rule validator, CSV/JSON/SQL exports, SHA-256 manifest | Independent Claude reviewer: 3 major, 5 minor findings, all fixed | 818,803 rows load; 0 violations; same-seed runs byte-identical; attrition 10.29% | 91b6df8 → 1d1fee7 |
| G4 Model | As-of features with lineage, logistic regression + scorecard, temporal split, bootstrap intervals, pre-declared checks | Same review cycle as G2 | ROC 0.82, top-5% lift 6.8× (CI 5.5–8.2); scorecard check reported as not met | 52780cb → 1d1fee7 |
| G5 App | FastAPI with sessions, roles and the review gate; React UI for HR, managers, IT ops, admin; API replay of the edge cases | ChatGPT/Codex: 1 blocker (file traversal on Windows), 5 majors, 2 privacy leaks, all fixed; survey timing bug corrected | 163 tests, 0 skipped; replay 700/700 exact | a15c014 → 822bf0b |
| G6 Extend | HR evidence panel; employee My data page | Codex scope addendum v1 with permission rules | 176 tests, 0 skipped, on Windows; replay 700/700 | 930e81c |

## How the two AIs worked together

The rule that mattered most: neither AI accepts the other's claim without proof, and a finding is fixed before the next gate starts.

| Who | Role | How |
| --- | --- | --- |
| Siddarth | Product owner and decider | Set scope, stack and leave policy; ran every build on Windows; approved scope changes |
| Claude | Builder (from G2) and first reviewer of G1 | Wrote schema, generator, model, API and UI; posted every step and result to #problemstate |
| ChatGPT / Codex | Reviewer (builder for G1) | Read the code and diffs, not only reported results; issued findings as blocker / major / minor |
| Fresh Claude agent | Independent reviewer | Audited G2 and G4 while Codex was rate-limited, without seeing how the work was produced |

- **Agree, then build.** Each gate's design was posted and agreed in Slack before code, to avoid master-data rework.
- **Evidence, not claims.** Every hand-off carried a commit hash, exact test counts (including skips), replay counts and checksums.
- **Honest corrections.** When a defect was found after the test list had been seen, the fix was logged in `docs/corrections.md` with before and after hashes and metrics, and nothing was retuned against the test.
- **Windows is the target.** Results counted only after they reproduced on Siddarth's laptop.

## Status and what is left

The build is complete and verified on Windows at commit 930e81c (176 tests passed, 0 skipped; edge-case replay 700 of 700 exact). It is not yet on `main`, which still holds only the G1 design commit.

- [ ] Codex gives final approval for merge candidate 930e81c
- [ ] Merge `g6-evidence-employee` into `main`
- [ ] Optional: host the demo database (Supabase or equivalent) and set `SECURE_COOKIES=true` behind HTTPS
