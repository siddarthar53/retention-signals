"""In-memory registry of generated entities.

Children hold references to parent OBJECTS, never typed ID strings. Business IDs are assigned once, at
the end, by a deterministic sort per entity type, so every reference in the export is consistent by
construction. The database load then re-checks every key.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(eq=False)
class Band:
    level: int
    min_amount: int
    mid_amount: int
    max_amount: int
    id: str = ""


@dataclass(eq=False)
class Role:
    job_family: str
    level: int
    is_people_manager: bool
    band: Band
    title: str
    order: int
    id: str = ""


@dataclass(eq=False)
class Team:
    job_family: str
    family_seq: int
    name: str
    created_date: dt.date
    order: int
    id: str = ""


@dataclass(eq=False)
class Employee:
    hire_date: dt.date
    recorded_at: str
    sort_key: tuple
    id: str = ""
    # generation-only state (never exported)
    current: "Assignment | None" = None
    assignments: list = field(default_factory=list)
    promotions: list = field(default_factory=list)
    events: list = field(default_factory=list)


@dataclass(eq=False)
class Assignment:
    employee: Employee
    team: Team
    role: Role
    effective_from: dt.date
    change_reason: str
    previous: "Assignment | None"
    recorded_at: str
    id: str = ""


@dataclass(eq=False)
class Promotion:
    employee: Employee
    from_role: Role
    to_role: Role
    assignment: Assignment
    effective_date: dt.date
    recorded_at: str
    id: str = ""


@dataclass(eq=False)
class EmploymentEvent:
    employee: Employee
    event_type: str
    event_date: dt.date
    exit_type: str | None
    notice_days: int | None
    recorded_at: str
    id: str = ""


class Registry:
    def __init__(self):
        self.bands: list[Band] = []
        self.roles: list[Role] = []
        self.teams: list[Team] = []
        self.employees: list[Employee] = []
        self.assignments: list[Assignment] = []
        self.promotions: list[Promotion] = []
        self.events: list[EmploymentEvent] = []
        self._role_index: dict[tuple, Role] = {}

    # ---- creation helpers: children always receive parent objects ----
    def add_role(self, role: Role) -> Role:
        self.roles.append(role)
        self._role_index[(role.job_family, role.level, role.is_people_manager)] = role
        return role

    def role(self, family: str, level: int, manager: bool) -> Role:
        return self._role_index[(family, level, manager)]

    def add_assignment(self, emp: Employee, team: Team, role: Role, start: dt.date,
                       reason: str, recorded_at: str) -> Assignment:
        prev = emp.current
        if (prev is None) != (reason == "hire"):
            raise ValueError("first assignment must be the hire; later ones need a predecessor")
        if prev is not None and start <= prev.effective_from:
            raise ValueError("assignment must start after its predecessor")
        a = Assignment(emp, team, role, start, reason, prev, recorded_at)
        emp.assignments.append(a)
        emp.current = a
        self.assignments.append(a)
        return a

    def add_promotion(self, emp: Employee, new_role: Role, start: dt.date, recorded_at: str) -> Promotion:
        old = emp.current
        if old is None or new_role.level != old.role.level + 1:
            raise ValueError("a promotion raises the level by exactly one")
        if old.role.is_people_manager and not new_role.is_people_manager:
            raise ValueError("a promotion cannot move a manager back to an individual-contributor role")
        a = self.add_assignment(emp, old.team, new_role, start, "promotion", recorded_at)
        p = Promotion(emp, old.role, new_role, a, start, recorded_at)
        emp.promotions.append(p)
        self.promotions.append(p)
        return p

    def add_event(self, emp: Employee, event_type: str, day: dt.date, recorded_at: str,
                  exit_type: str | None = None, notice_days: int | None = None) -> EmploymentEvent:
        e = EmploymentEvent(emp, event_type, day, exit_type, notice_days, recorded_at)
        emp.events.append(e)
        self.events.append(e)
        return e

    # ---- deterministic ID assignment ----
    def assign_ids(self) -> None:
        for i, b in enumerate(sorted(self.bands, key=lambda b: b.level), 1):
            b.id = f"BAND-{b.level:02d}"
        for i, r in enumerate(sorted(self.roles, key=lambda r: r.order), 1):
            r.id = f"ROLE-{i:04d}"
        for i, t in enumerate(sorted(self.teams, key=lambda t: t.order), 1):
            t.id = f"TEAM-{i:04d}"
        for i, e in enumerate(sorted(self.employees, key=lambda e: e.sort_key), 1):
            e.id = f"EMP-{i:06d}"
        _per_year(self.assignments, "ASGN", 6, lambda a: (a.effective_from, a.employee.id))
        _per_year(self.promotions, "PROMO", 6, lambda p: (p.effective_date, p.employee.id))
        _per_year(self.events, "EEV", 6, lambda e: (e.event_date, e.employee.id, e.event_type))


def _per_year(items, prefix: str, width: int, key) -> None:
    counters: dict[int, int] = {}
    for item in sorted(items, key=key):
        year = key(item)[0].year
        counters[year] = counters.get(year, 0) + 1
        item.id = f"{prefix}-{year}-{counters[year]:0{width}d}"
