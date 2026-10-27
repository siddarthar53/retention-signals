"""Derived tables (design v0.2 sections 2.2, 5, 6): tenure snapshots, outcomes, horizon labels and
intervention potential outcomes. Everything here is computed from recorded history only."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .core import months_between, stable_json, ts
from .registry import Employee, Registry

OBSERVATION_END = dt.date(2026, 12, 31)
QUARTER_ENDS = [dt.date(y, m, d) for y in (2025, 2026) for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))]


def quarter_end(day: dt.date) -> dt.date:
    m = ((day.month - 1) // 3 + 1) * 3
    nxt = dt.date(day.year + (m == 12), m % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def horizon_end(as_of: dt.date) -> dt.date:
    """Second quarter-end after as_of (as_of is a quarter-end)."""
    y, m = as_of.year, as_of.month + 6
    if m > 12:
        y, m = y + 1, m - 12
    return quarter_end(dt.date(y, m, 1))


def quarter_start(as_of: dt.date) -> dt.date:
    return dt.date(as_of.year, as_of.month - 2, 1)


@dataclass
class Facts:
    """Per-employee facts read from recorded events."""
    hire: dt.date
    resign: dt.date | None
    resign_recorded: str | None
    termination: dt.date | None
    exit_type: str | None


def facts(emp: Employee) -> Facts:
    resign = next((e for e in emp.events if e.event_type == "resignation_submitted"), None)
    exit_ = next((e for e in emp.events if e.event_type == "exit"), None)
    return Facts(emp.hire_date, resign.event_date if resign else None, resign.recorded_at if resign else None,
                 exit_.event_date if exit_ else None, exit_.exit_type if exit_ else None)


def employed_on(f: Facts, day: dt.date) -> bool:
    """E(as_of): hired on or before day and no termination on or before day."""
    return f.hire <= day and (f.termination is None or f.termination > day)


@dataclass
class Snapshot:
    employee: Employee
    as_of: dt.date
    assignment: object
    tenure_months: str
    months_in_role: str
    months_since_promotion: str | None
    derived_from: str
    recorded_at: str

    @property
    def id(self) -> str:
        return f"SNAP-{self.employee.id}-{self.as_of.year}Q{(self.as_of.month - 1) // 3 + 1}"


@dataclass
class Outcome:
    employee: Employee
    final_status: str
    resignation_date: dt.date | None
    termination_date: dt.date | None
    scheduled_termination_date: dt.date | None

    @property
    def id(self) -> str:
        return f"OUT-{self.employee.id}-{OBSERVATION_END.year}"


@dataclass
class Label:
    employee: Employee
    as_of: dt.date
    horizon_end: dt.date
    status: str
    ineligible_reason: str | None
    target: int | None
    outcome: Outcome


@dataclass
class PotentialOutcome:
    employee: Employee
    as_of: dt.date
    y_if_treated: int
    y_if_untreated: int


def _in_force(emp: Employee, day: dt.date):
    cur = None
    for a in emp.assignments:                 # chronological
        if a.effective_from <= day:
            cur = a
    return cur


def _role_start(a) -> dt.date:
    start = a.effective_from
    while a.previous is not None and a.previous.role is a.role:
        a = a.previous
        start = a.effective_from
    return start


def build_snapshots(reg: Registry) -> list[Snapshot]:
    out = []
    for emp in reg.employees:
        f = facts(emp)
        hire_ev = next(e for e in emp.events if e.event_type == "hire")
        for q in QUARTER_ENDS:
            if not employed_on(f, q):
                continue
            a = _in_force(emp, q)
            promos = [p for p in emp.promotions if p.effective_date <= q]
            last_p = promos[-1] if promos else None
            lineage = {"hire_event": hire_ev.id, "assignment": a.id,
                       "promotion": last_p.id if last_p else None}
            out.append(Snapshot(
                emp, q, a, f"{months_between(emp.hire_date, q):.2f}", f"{months_between(_role_start(a), q):.2f}",
                f"{months_between(last_p.effective_date, q):.2f}" if last_p else None,
                stable_json(lineage), ts(q + dt.timedelta(days=1), 6)))
    return out


def build_outcomes(reg: Registry) -> dict[Employee, Outcome]:
    out = {}
    for emp in reg.employees:
        f = facts(emp)
        if f.termination is not None and f.termination <= OBSERVATION_END:
            status = "exited_voluntary" if f.exit_type == "voluntary" else "exited_involuntary"
            out[emp] = Outcome(emp, status, f.resign if status == "exited_voluntary" else None, f.termination, None)
        elif f.resign is not None:
            out[emp] = Outcome(emp, "on_notice", f.resign, None, _scheduled_termination(emp, f))
        else:
            out[emp] = Outcome(emp, "active", None, None, None)
    return out


def _scheduled_termination(emp: Employee, f: Facts) -> dt.date:
    sched = getattr(emp, "_scheduled_termination", None)
    if sched is None:
        raise ValueError(f"{emp.id}: resignation without a scheduled termination")
    return sched


def build_labels(reg: Registry, outcomes: dict) -> list[Label]:
    out = []
    for emp in reg.employees:
        f = facts(emp)
        o = outcomes[emp]
        for q in QUARTER_ENDS:
            if not employed_on(f, q):
                continue                                   # labels exist only for E(as_of)
            h = horizon_end(q)
            if f.resign is not None and f.resign <= q and f.resign_recorded[:10] <= q.isoformat():
                out.append(Label(emp, q, h, "ineligible", "resignation_known", None, o)); continue
            if f.hire > quarter_start(q):
                out.append(Label(emp, q, h, "ineligible", "insufficient_history", None, o)); continue
            if h > OBSERVATION_END:
                out.append(Label(emp, q, h, "unresolved", None, None, o)); continue
            if f.termination is not None and f.termination <= h:
                if f.exit_type == "voluntary":
                    out.append(Label(emp, q, h, "voluntary_exit", None, 1, o))
                else:
                    out.append(Label(emp, q, h, "competing_exit", None, 0, o))
            else:
                out.append(Label(emp, q, h, "employed_through_horizon", None, 0, o))
    return out


JUNE_2026 = dt.date(2026, 6, 30)


def build_potential_outcomes(labels: list[Label], hazard_rng, prevention_prob: float) -> list[PotentialOutcome]:
    """Prevention model (design v0.2 section 6): y0 = canonical label; y1 = 0 if y0 = 1 and v < p, else y0."""
    june = sorted((lb for lb in labels if lb.as_of == JUNE_2026 and lb.target is not None),
                  key=lambda lb: lb.employee.id)
    v = hazard_rng.random(len(june))
    out = []
    for lb, vi in zip(june, v):
        y0 = lb.target
        y1 = 0 if (y0 == 1 and vi < prevention_prob) else y0
        out.append(PotentialOutcome(lb.employee, JUNE_2026, y1, y0))
    return out
