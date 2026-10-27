"""Monthly simulation 2025-01 .. 2026-12 (generator design v0.2, section 4).

Causal rule: every random outcome in month m is a function of the latent state fixed on the 1st of m
(plus that month's own draws). Code order inside a month is: growth scheduling -> vacancy decisions
-> hires -> leave credits -> latent update -> exit hazards -> career events -> exits/backfills ->
leave taken -> workload -> survey -> Dec 31 lapse. Hazards are drawn before career events and
leave/workload/survey only so those rows can stop at the termination date (and no career event
can fall after an exit); hazards never read anything produced later in the month.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np

from . import calendar as cal
from .core import add_months, d, largest_remainder, months_between, ts
from .registry import Employee, LeaveTxn, Registry, Survey, Team, Workload

ENTRY_LEVEL_MIX = [0.35, 0.35, 0.20, 0.08, 0.02]       # growth hires, IC L1-L5


@dataclass
class State:
    base_int: float
    core: float                          # intensity before team shock and episode lift
    x: float
    eng_base: float
    eng: float
    pull: float
    mgr_q: float
    clock_start: dt.date                 # latest promotion or hire
    spike_left: int = 0
    sust_left: int = 0
    sust_age: int = 0
    fresh_until: dt.date = dt.date.min
    last_transfer: dt.date = dt.date.min
    resign_date: dt.date | None = None
    termination: dt.date | None = None
    exit_type: str | None = None
    eng_hist: list = field(default_factory=list)     # engagement on the 1st of each simulated month
    bal: dict = field(default_factory=dict)          # (year, type) -> balance


def _logit_inv(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


class Simulator:
    def __init__(self, reg: Registry, cfg: dict, gcfg: dict, streams, b0: float):
        self.reg, self.cfg, self.g, self.b0 = reg, cfg, gcfg, b0
        self.rng = streams["monthly"]
        self.hz = streams["hazard"]
        self.window_start, self.window_end = d(cfg["window"]["start"]), d(cfg["window"]["end"])
        cal.configure(cfg["calendar"]["company_holidays_per_year"])
        self.notice = {int(k): int(v) for k, v in cfg["notice_days_by_level"].items()}
        self.state: dict[Employee, State] = {}
        self.team_members: dict[Team, set] = {t: set() for t in reg.teams}
        self.team_manager: dict[Team, Employee | None] = {t: None for t in reg.teams}
        self.pending_hires: list[tuple] = []          # (hire_date, kind, team, role)
        self.pending_vacancies: list[tuple] = []      # (fill_month_first_day, team, role)
        self.stats = {"backfill_hires": 0, "growth_hires": 0, "external_manager_hires": 0,
                      "internal_manager_fills": 0, "vacancies_open_at_end": 0, "promotions": 0,
                      "transfers": 0, "resignations": 0, "voluntary_terminations": 0,
                      "involuntary_terminations": 0}
        n_hires = int(cfg["scale"]["employees"]) - len(reg.employees)
        self.hires_total = n_hires
        self.backfill_quota = round(n_hires * cfg["in_window_hires"]["target_share_backfill"])
        self.hires_scheduled = 0
        self.backfills_scheduled = 0
        for emp in reg.employees:
            self._init_state(emp)
        self._burn_in(int(gcfg["latent"].get("burn_in_months", 0)))

    # ------------------------------------------------------------------ setup
    def _init_state(self, emp: Employee) -> None:
        L, E = self.g["latent"]["intensity"], self.g["latent"]["engagement"]
        r = self.rng
        base = float(r.normal(L["person_mean"], L["person_sd"]))
        eng_base = float(r.normal(E["person_mean"], E["person_sd"]))
        clock = max([emp.hire_date] + [p.effective_date for p in emp.promotions])
        st = State(base_int=base, core=base, x=base, eng_base=eng_base, eng=eng_base + float(r.normal(0, 0.15)),
                   pull=float(r.normal(0, self.g["latent"]["external_pull"]["sd"])),
                   mgr_q=float(r.normal(0, self.g["latent"]["manager_quality"]["sd"])), clock_start=clock)
        self.state[emp] = st
        team = emp.current.team
        self.team_members[team].add(emp)
        if emp.current.role.is_people_manager:
            self.team_manager[team] = emp

    def _burn_in(self, months: int) -> None:
        """Run the latent dynamics for `months` before the window (no events, no rows), so that intensity
        and engagement start at their steady state instead of drifting during the first months of 2025.
        The engagement history from the burn-in is kept, so 'engagement decline' is defined from day one."""
        start = add_months(self.window_start, -months)
        active = list(self.reg.employees)
        for i in range(months):
            day = add_months(start, i)
            self._latent_update(active, day.year, day.month)

    def _plan_growth(self) -> dict[tuple, int]:
        growth = self.hires_total - self.backfill_quota
        months = [(y, m) for y in (2025, 2026) for m in range(1, 13)]
        return dict(zip(months, largest_remainder(growth, [1.0] * len(months)))) if growth > 0 else {}

    # ------------------------------------------------------------------ helpers
    def _employed_on(self, emp: Employee, day: dt.date) -> bool:
        st = self.state[emp]
        return emp.hire_date <= day and (st.termination is None or st.termination >= day)

    def _sched_days(self, emp: Employee, y: int, m: int) -> list[dt.date]:
        st = self.state[emp]
        lo = max(dt.date(y, m, 1), emp.hire_date)
        hi = min(cal.month_end(y, m), st.termination or dt.date.max)
        return [w for w in cal.workdays_in_month(y, m) if lo <= w <= hi]

    def _random_workday(self, y: int, m: int, not_before: dt.date) -> dt.date | None:
        days = [w for w in cal.workdays_in_month(y, m) if w >= not_before]
        return days[int(self.rng.integers(len(days)))] if days else None

    def _bal(self, emp, year, ltype) -> int:
        return self.state[emp].bal.get((year, ltype), 0)

    def _leave(self, emp, ltype, txn, days, day, seq):
        st = self.state[emp]
        st.bal[(day.year, ltype)] = st.bal.get((day.year, ltype), 0) + days
        self.reg.leave.append(LeaveTxn(emp, ltype, txn, days, day, ts(day, 8 if seq == 0 else 18), seq))

    def _new_employee(self, hire: dt.date, team: Team, role) -> Employee:
        emp = Employee(hire, ts(hire), sort_key=(hire, team.order, -role.level, role.is_people_manager))
        self.reg.employees.append(emp)
        self.reg.add_event(emp, "hire", hire, ts(hire))
        self.reg.add_assignment(emp, team, role, hire, "hire", ts(hire))
        self._init_state(emp)
        return emp

    # ------------------------------------------------------------------ monthly steps
    def _schedule_growth_hires(self, y, m, count):
        fams = list(self.cfg["job_families"])
        shares = [self.cfg["job_families"][f] for f in fams]
        for _ in range(count):
            if self.hires_scheduled >= self.hires_total:
                return
            fam = fams[int(self.rng.choice(len(fams), p=np.array(shares) / sum(shares)))]
            teams = [t for t in self.reg.teams if t.job_family == fam]
            team = min(teams, key=lambda t: (len(self.team_members[t]), t.order))
            lvl = int(self.rng.choice(5, p=ENTRY_LEVEL_MIX)) + 1
            day = self._random_workday(y, m, dt.date(y, m, 1))
            self.pending_hires.append((day, "growth", team, self.reg.role(fam, lvl, False)))
            self.hires_scheduled += 1

    def _process_hires(self, y, m):
        due = sorted([h for h in self.pending_hires if (h[0].year, h[0].month) == (y, m)],
                     key=lambda h: (h[0], h[2].order, h[3].order))
        self.pending_hires = [h for h in self.pending_hires if (h[0].year, h[0].month) != (y, m)]
        for day, kind, team, role in due:
            emp = self._new_employee(day, team, role)
            self.team_members[team].add(emp)
            if role.is_people_manager:
                self.team_manager[team] = emp
                self.stats["external_manager_hires"] += 1
            self.stats["backfill_hires" if kind in ("backfill", "manager_backfill") else "growth_hires"] += 1

    def _leave_credits(self, active, y, m):
        first = dt.date(y, m, 1)
        for emp in active:
            day = max(first, emp.hire_date)
            self._leave(emp, "EL", "accrual", 1, day, 0)
            if m == 1 and emp.hire_date <= first:
                self._leave(emp, "SL", "grant", 3, first, 0)
            elif emp.hire_date > first:
                self._leave(emp, "SL", "grant", 3, emp.hire_date, 0)

    def _latent_update(self, active, y, m):
        L, E = self.g["latent"]["intensity"], self.g["latent"]["engagement"]
        r = self.rng
        shocks = {t: float(r.normal(0, L["team_shock_sd"])) for t in self.reg.teams}
        first = dt.date(y, m, 1)
        for emp in active:
            st = self.state[emp]
            st.eng_hist.append(st.eng)                         # engagement as of the 1st
            if st.spike_left == 0 and st.sust_left == 0:
                u = r.random()
                if u < L["benign_spike"]["prob_per_month"]:
                    st.spike_left = int(r.integers(L["benign_spike"]["months"][0], L["benign_spike"]["months"][1] + 1))
                elif u < L["benign_spike"]["prob_per_month"] + L["sustained_overload"]["prob_per_month"]:
                    st.sust_left = int(r.integers(L["sustained_overload"]["months"][0],
                                                  L["sustained_overload"]["months"][1] + 1))
                    st.sust_age = 0
            lift = 0.0
            if st.spike_left:
                lift += L["benign_spike"]["lift"]; st.spike_left -= 1
            if st.sust_left:
                lift += L["sustained_overload"]["lift"]; st.sust_left -= 1; st.sust_age += 1
            else:
                st.sust_age = 0
            st.core = st.base_int + L["ar"] * (st.core - st.base_int) + float(r.normal(0, L["noise_sd"]))
            st.x = float(np.clip(st.core + shocks[emp.current.team] + lift, *L["clip"]))
            clock_m = months_between(st.clock_start, first)
            target = (st.eng_base - E["sustained_overload_drag"] * (st.sust_age >= 3)
                      - E["promotion_clock_drag_per_month"] * max(0.0, clock_m - 24)
                      + E["manager_quality_weight"] * st.mgr_q
                      + E["fresh_start_bump"] * (first <= st.fresh_until))
            st.eng = float(np.clip(st.eng + E["adjust_rate"] * (target - st.eng) + r.normal(0, E["noise_sd"]), 1, 5))

    def _career_events(self, active, y, m):
        C = self.g["careers"]
        first = dt.date(y, m, 1)
        for emp in active:
            st = self.state[emp]
            if (emp.hire_date > first or st.resign_date is not None or st.termination is not None
                    or emp.current.effective_from >= first):
                continue
            role, team = emp.current.role, emp.current.team
            months_in_role = months_between(_role_start(emp), first)
            p = C["promotion_prob_per_month"]
            p_prom = p["after"] if months_in_role >= p["eligible_after_months_in_role"] else p["before"]
            max_lvl = 6 if role.is_people_manager else 5
            if role.level < max_lvl and self.rng.random() < p_prom:
                day = self._random_workday(y, m, first)
                self.reg.add_promotion(emp, self.reg.role(role.job_family, role.level + 1, role.is_people_manager),
                                       day, ts(day))
                st.clock_start = day; st.fresh_until = add_months(day, 6)
                self.stats["promotions"] += 1
                continue
            if not role.is_people_manager and self.rng.random() < C["transfer_prob_per_month"]:
                others = [t for t in self.reg.teams if t.job_family == team.job_family and t is not team]
                if others:
                    new_team = others[int(self.rng.integers(len(others)))]
                    day = self._random_workday(y, m, first)
                    if day and day > emp.current.effective_from:
                        self.reg.add_assignment(emp, new_team, role, day, "transfer", ts(day))
                        self.team_members[team].discard(emp); self.team_members[new_team].add(emp)
                        st.mgr_q = float(self.rng.normal(0, self.g["latent"]["manager_quality"]["sd"]))
                        st.last_transfer = day; st.fresh_until = add_months(day, 6)
                        self.stats["transfers"] += 1

    def _fill_vacancies(self, y, m):
        first = dt.date(y, m, 1)
        due = [v for v in self.pending_vacancies if v[0] == first]
        self.pending_vacancies = [v for v in self.pending_vacancies if v[0] != first]
        for _, team, role in due:
            if self.team_manager[team] is not None:
                continue
            candidates = sorted([e for e in self.team_members[team]
                                 if self._employed_on(e, first) and e.hire_date <= first
                                 and self.state[e].resign_date is None and self.state[e].termination is None
                                 and not e.current.role.is_people_manager and e.current.role.level >= 3],
                                key=lambda e: (-e.current.role.level, e.hire_date, e.sort_key))
            internal = candidates and self.rng.random() < self.g["careers"]["vacancy_internal_share"]
            if internal:
                emp = candidates[0]
                day = self._random_workday(y, m, max(first, emp.current.effective_from + dt.timedelta(days=1)))
                if day is None:
                    internal = False
                else:
                    lvl = emp.current.role.level
                    if lvl == 3:
                        self.reg.add_promotion(emp, self.reg.role(team.job_family, 4, True), day, ts(day))
                        self.state[emp].clock_start = day
                        self.stats["promotions"] += 1
                    else:
                        self.reg.add_assignment(emp, team, self.reg.role(team.job_family, lvl, True), day,
                                                "lateral_move", ts(day))
                    self.state[emp].fresh_until = add_months(day, 6)
                    self.team_manager[team] = emp
                    self.stats["internal_manager_fills"] += 1
            if not internal:
                if self.hires_scheduled < self.hires_total:
                    day = self._random_workday(y, m, first)
                    self.pending_hires.append((day, "manager_backfill", team, role))
                    self.hires_scheduled += 1
                    self.backfills_scheduled += 1
                # else: the vacancy stays open (cases route to HR)

    def _hazards(self, active, y, m):
        H = self.g["hazard"]["voluntary"]
        first = dt.date(y, m, 1)
        p_inv = self.g["hazard"]["involuntary_prob_per_month"]
        for emp in active:
            st = self.state[emp]
            if st.resign_date is not None or st.termination is not None or emp.hire_date > first:
                continue
            u_inv, u_vol = self.hz.random(), self.hz.random()
            if u_inv < p_inv:
                day = self._random_workday(y, m, first)
                st.termination, st.exit_type = day, "involuntary"
                self.reg.add_event(emp, "exit", day, ts(day, 17), exit_type="involuntary")
                self.stats["involuntary_terminations"] += 1
                continue
            decline = max(0.0, (st.eng_hist[-7] if len(st.eng_hist) >= 7 else st.eng_hist[0]) - st.eng)
            clock_m = months_between(st.clock_start, first)
            z = (self.b0 + H["engagement_gap"] * (3.6 - st.eng) + H["engagement_decline"] * decline
                 + H["sustained_overload"] * (st.sust_age >= 3) + H["benign_spike"] * (st.spike_left > 0)
                 + H["promotion_clock"] * max(0.0, clock_m - 24)
                 + H["new_joiner"] * (months_between(emp.hire_date, first) < 12)
                 + H["recent_transfer"] * (st.last_transfer > add_months(first, -6))
                 + H["external_pull"] * st.pull + H["manager_quality"] * st.mgr_q)
            if u_vol < _logit_inv(z):
                day = self._random_workday(y, m, first)
                notice = self.notice[emp.current.role.level]
                term = cal.previous_workday_on_or_before(day + dt.timedelta(days=notice))
                st.resign_date, st.termination, st.exit_type = day, term, "voluntary"
                emp._scheduled_termination = term
                self.reg.add_event(emp, "resignation_submitted", day, ts(day, 11), notice_days=notice)
                self.stats["resignations"] += 1

    def _record_exits_in_month(self, y, m):
        """Exit events for voluntary terminations falling in this month; vacancies and backfills."""
        bf = self.cfg["in_window_hires"]["backfill_delay_months"]
        for emp, st in self.state.items():
            if st.termination is None or (st.termination.year, st.termination.month) != (y, m):
                continue
            if st.exit_type == "voluntary":
                self.reg.add_event(emp, "exit", st.termination, ts(st.termination, 17), exit_type="voluntary")
                self.stats["voluntary_terminations"] += 1
            team, role = emp.current.team, emp.current.role
            self.team_members[team].discard(emp)
            if role.is_people_manager and self.team_manager[team] is emp:
                self.team_manager[team] = None
                delay = int(self.rng.integers(self.g["careers"]["vacancy_fill_delay_months"][0],
                                              self.g["careers"]["vacancy_fill_delay_months"][1] + 1))
                fill = add_months(dt.date(y, m, 1), delay)
                if fill <= self.window_end:
                    self.pending_vacancies.append((fill, team, role))
                continue
            if self.backfills_scheduled < self.backfill_quota and self.hires_scheduled < self.hires_total:
                delay = int(self.rng.integers(bf[0], bf[1] + 1))
                target = add_months(st.termination, delay)
                if target <= self.window_end:
                    day = self._random_workday(target.year, target.month, dt.date(target.year, target.month, 1))
                    self.pending_hires.append((day, "backfill", team, role))
                    self.hires_scheduled += 1
                    self.backfills_scheduled += 1

    def _leave_taken_workload_survey(self, active, y, m):
        U, S, R = self.g["leave_use"], self.g["survey"], self.g["recording_lag_days"]
        r = self.rng
        end = cal.month_end(y, m)
        for emp in active:
            st = self.state[emp]
            sched = self._sched_days(emp, y, m)
            n = len(sched)
            if n == 0:
                continue
            used = 0
            # earned leave
            el_bal = self._bal(emp, y, "EL")
            last_notice_month = st.exit_type == "voluntary" and (st.termination.year, st.termination.month) == (y, m)
            want = 0
            if last_notice_month and U["el_notice_burn"]:
                want = el_bal
            elif r.random() < (U["el_december_prob"] if m == 12 else U["el_block_prob_per_month"]):
                want = el_bal if m == 12 else int(r.integers(U["el_block_days"][0], U["el_block_days"][1] + 1))
            el_days = int(min(want, el_bal, n - 1 - used))
            if el_days > 0:
                start = int(r.integers(0, n - el_days + 1))
                self._leave(emp, "EL", "taken", -el_days, sched[start], 1)
                used += el_days
            # sick leave
            sl_days = 0
            if r.random() < U["sl_prob_per_month"]:
                want = int(r.integers(U["sl_days"][0], U["sl_days"][1] + 1))
                sl_days = int(min(want, self._bal(emp, y, "SL"), n - 1 - used))
                if sl_days > 0:
                    start = int(r.integers(0, n - sl_days + 1))
                    self._leave(emp, "SL", "taken", -sl_days, sched[start], 1)
                    used += sl_days
            # workload (utilisation first, actual exact)
            util10 = int(round(min(st.x * 100, 250.0) * 10))
            available = (n - used) * 8
            actual = Decimal(util10) * Decimal(available) / Decimal(1000)
            after = float(np.clip(max(0.0, st.x - 1.0) * 90 + r.normal(6, 2), 0, 100))
            fam = emp.current.team.job_family
            lam = (1.5 + 2 * max(0.0, st.x - 1)) if fam in ("engineering", "operations", "support") else 0.2
            oncall = int(min(r.poisson(lam), n))
            lag = int(r.integers(R["workload_after_month_end"][0], R["workload_after_month_end"][1] + 1))
            self.reg.workload.append(Workload(
                emp, dt.date(y, m, 1), n, available, f"{actual:.3f}",
                None if available == 0 else f"{util10 / 10:.1f}", f"{after:.1f}", oncall,
                ts(end + dt.timedelta(days=lag), 6), leave_days=used, sl_days=sl_days))
            # quarterly survey
            if m in (3, 6, 9, 12):
                self._survey(emp, st, y, m, S, R)

    def _survey(self, emp, st, y, m, S, R):
        r = self.rng
        sent = dt.date(y, m, 15)
        if not self._employed_on(emp, sent):
            return
        wave = f"{y}Q{(m - 1) // 3 + 1}"
        p_resp = float(np.clip(0.80 - S["intensity_nonresponse_slope"] * (st.x - 1.0), 0.5, 0.95))
        sub_lag = int(r.integers(R["survey_submission_after_send"][0], R["survey_submission_after_send"][1] + 1))
        submit_day = sent + dt.timedelta(days=sub_lag)
        responds = r.random() < p_resp and (st.termination is None or submit_day <= st.termination)
        if not responds:
            self.reg.surveys.append(Survey(emp, wave, sent, "no_response", (None,) * 5, None, None,
                                           ts(sent + dt.timedelta(days=8), 7)))
            return
        clock_m = months_between(st.clock_start, sent)
        means = [st.eng, st.eng - 0.8 * (st.x - 1.0), st.eng - 0.01 * max(0.0, clock_m - 24),
                 st.eng + 0.4 * st.mgr_q, st.eng]
        items = [int(np.clip(round(mu + r.normal(0, S["item_noise_sd"])), 1, 5)) for mu in means]
        if r.random() < 0.05:                                  # partial: keep 1-4 items
            keep = int(r.integers(1, 5))
            idx = set(int(i) for i in r.choice(5, size=keep, replace=False))
            items = [v if i in idx else None for i, v in enumerate(items)]
            status = "partial"
        else:
            status = "complete"
        answered = [v for v in items if v is not None]
        index = None
        if len(answered) >= 3:
            index = f"{(Decimal(sum(answered)) / Decimal(len(answered))).quantize(Decimal('0.01'), rounding='ROUND_HALF_UP')}"
        rec_lag = int(r.integers(R["survey_after_submission"][0], R["survey_after_submission"][1] + 1))
        self.reg.surveys.append(Survey(emp, wave, sent, status, tuple(items), index, ts(submit_day, 10),
                                       ts(submit_day + dt.timedelta(days=rec_lag), 12)))

    def _year_end_lapse(self, y):
        day = dt.date(y, 12, 31)
        for emp, st in self.state.items():
            for lt in ("EL", "SL"):
                b = st.bal.get((y, lt), 0)
                if b > 0:
                    self._leave(emp, lt, "lapse", -b, day, 2)

    # ------------------------------------------------------------------ main loop
    def run(self) -> dict:
        growth_plan = self._plan_growth()
        months = [(y, m) for y in (2025, 2026) for m in range(1, 13)]
        for y, m in months:
            extra = 0
            if (y, m) == months[-1]:
                extra = max(0, self.hires_total - self.hires_scheduled - growth_plan.get((y, m), 0))
            self._schedule_growth_hires(y, m, growth_plan.get((y, m), 0) + extra)
            self._fill_vacancies(y, m)            # decided at month start; may add hires for this month
            self._process_hires(y, m)
            first, end = dt.date(y, m, 1), cal.month_end(y, m)
            active = [e for e in self.reg.employees if self._employed_on(e, end) or
                      (self.state[e].termination is not None and self.state[e].termination >= first
                       and e.hire_date <= end)]
            self._leave_credits(active, y, m)
            self._latent_update(active, y, m)
            self._hazards(active, y, m)
            self._career_events(active, y, m)
            self._record_exits_in_month(y, m)
            self._leave_taken_workload_survey(active, y, m)
            if m == 12:
                self._year_end_lapse(y)
        self.stats["vacancies_open_at_end"] = sum(1 for t, e in self.team_manager.items() if e is None)
        self.stats["hires_scheduled"] = self.hires_scheduled
        return self.stats


def _role_start(emp: Employee) -> dt.date:
    """Start of the current role (unchanged by transfers)."""
    start = emp.current.effective_from
    a = emp.current
    while a.previous is not None and a.previous.role is a.role:
        a = a.previous
        start = a.effective_from
    return start
