# Where each rule is enforced

The database enforces what a single row (or a key) can prove. Rules that need history, time or the
requesting user are enforced by the validator, the pipeline code or the API service layer, and each
has a test. Status: **DB** = enforced and tested now (`tests/db`); **planned (G2/G4/G5)** = owned by
that gate and must ship with a failing fixture.

| Rule | Layer | Status |
|---|---|---|
| ID formats; IDs embed the right employee / month / quarter | DB (CHECK) | DB |
| Enums, ranges, sign of leave days, accrual = +1 EL, grant = +3 SL, lapse only on 31 Dec | DB (CHECK) | DB |
| Survey: item count = non-null answers; index = mean of answered; no index below 3 answers; no index without answers | DB (CHECK) | DB |
| Workload: utilisation = actual / available (from stored values); NULL at zero capacity; capacity <= workdays x 8 | DB (CHECK) | DB |
| Outcome state matrix (incl. on_notice needs scheduled date; resignation within observation) | DB (CHECK) | DB |
| Label target rule (completed statuses can't have NULL target); horizon = 2nd quarter-end | DB (CHECK) | DB |
| Employee-bound references (assignment chain, snapshot, promotion, label, intervention) | DB (composite FK) | DB |
| One hire assignment per employee; no self-referencing predecessor; one start per day | DB (UNIQUE / CHECK) | DB |
| History tables are append-only (no UPDATE / DELETE / TRUNCATE) | DB (trigger) | DB |
| API role cannot read eval, outcomes, sick leave, pay, capacity/actual hours, raw survey items | DB (grants) | DB |
| Assignment chain: predecessor is the immediately earlier assignment; no cycles; within employment; after team creation; nothing after exit | Validator | planned (G2) |
| Promotion date = started assignment's start date; level increases | Validator | planned (G2) |
| Leave balance >= 0 at every transaction; 0 after each 31 Dec lapse; no zero-day lapse rows; only the 31 Dec close after termination | Validator | planned (G2) |
| No workload / survey / leave-taken rows after termination | Validator | planned (G2) |
| Outcome vs events: final_status, resignation/termination/scheduled dates match employment_event rows | Validator | planned (G2) |
| Labels vs outcome: each label status/target matches the outcome dates and the horizon (incl. LABEL_DATE_MISMATCH detection) | Validator | planned (G2) |
| Pre-window careers feasible: promotions spaced >= 1.5 years, between hire and 2024-12-31, level increases by 1 | Validator | planned (G2) |
| Totals: exact employee count; realised hire mix reported; attrition within 5-15% | Validator | planned (G2) |
| As-of reconstruction: filter by recorded_at <= cutoff and date <= as_of BEFORE deriving intervals | Pipeline code | planned (G4) |
| Features never use eval, SL, compensation, team or manager IDs, or missingness as a risk term | Pipeline code (allowlist) | planned (G4) |
| Trainer reads only train/validation labels; test labels only in the evaluator | Pipeline code | planned (G4) |
| Sick leave cannot be recovered from model inputs (paired-record invariance) | Generator + test | planned (G2) |
| Review gate: approved review, same employee and score version, team scope, eligibility, chronology, arm, no conflicting intervention | API service (one transaction) | planned (G5) |
| Actor identity from the session only; 403 for other teams' cases; 409 for workflow conflicts | API service | planned (G5) |
| Manager responses exclude raw survey items, capacity/actual hours and restricted values | API serializers + tests | planned (G5) |

Pipeline jobs run as the schema owner, so their rules are code-enforced and tested rather than
database-enforced. The API connects as `rs_app_login` (member of `rs_app`), never as the owner.
