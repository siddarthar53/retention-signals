"""Master data (bands, roles, teams) and the population employed on the window start, with
feasible pre-window careers (master_data.yaml contracts C1-C3)."""
from __future__ import annotations

import datetime as dt

import numpy as np

from .core import d, largest_remainder, ts
from .registry import Band, Employee, Registry, Role, Team

FAMILY_LABEL = {"engineering": "Engineering", "operations": "Operations", "support": "Support",
                "sales": "Sales", "data": "Data", "product": "Product",
                "finance_admin": "Finance & Admin", "design": "Design"}


def build_master(reg: Registry, cfg: dict) -> None:
    for b in cfg["compensation_bands"]:
        reg.bands.append(Band(b["level"], b["min"], b["mid"], b["max"]))
    band_by_level = {b.level: b for b in reg.bands}
    order = 0
    for fam in cfg["job_families"]:
        for lvl in cfg["roles"]["ic_levels"]:
            reg.add_role(Role(fam, lvl, False, band_by_level[lvl], f"{FAMILY_LABEL[fam]} IC L{lvl}", order)); order += 1
        for lvl in cfg["roles"]["manager_levels"]:
            reg.add_role(Role(fam, lvl, True, band_by_level[lvl], f"{FAMILY_LABEL[fam]} Manager L{lvl}", order)); order += 1


def sizes(cfg: dict) -> dict:
    """Exact integer totals (C1, C2)."""
    n = int(cfg["scale"]["employees"])
    initial = round(n * cfg["scale"]["initial_share"])
    teams = max(len(cfg["job_families"]), round(initial / cfg["scale"]["team_size_target"]))
    return {"employees": n, "initial": initial, "in_window_hires": n - initial, "teams": teams}


def _sample_tenure_days(rng: np.random.Generator, cfg: dict, window_start: dt.date) -> int:
    """Tenure at window start, truncated (by rejection, not clamping) to [min_months, max_years]."""
    t = cfg["tenure_at_window_start"]
    shape = float(t["shape"]); scale = float(t["mean_years"]) / shape
    lo, hi = t["min_months"] / 12, float(t["max_years"])
    while True:
        years = rng.gamma(shape, scale)
        if lo <= years <= hi:
            return int(round(years * 365.25))


def _promotion_dates(rng: np.random.Generator, hire: dt.date, k: int, min_years: float,
                     last_day: dt.date) -> list[dt.date]:
    """k dates after hire, consecutive gaps >= min_years (also from hire), all on or before last_day."""
    if k == 0:
        return []
    min_gap = int(np.ceil(min_years * 365.25))
    slack = (last_day - hire).days - k * min_gap
    if slack < 0:
        raise ValueError("infeasible promotion count")      # caller caps k so this never happens
    extra = rng.dirichlet(np.ones(k + 1)) * slack            # slack shared across k gaps + the tail
    dates, day = [], hire
    for i in range(k):
        day = day + dt.timedelta(days=min_gap + int(extra[i]))
        dates.append(day)
    return dates


def build_initial_population(reg: Registry, cfg: dict, streams) -> dict:
    rng_pop, rng_car = streams["population"], streams["careers"]
    window_start = d(cfg["window"]["start"])
    last_pre_window_day = window_start - dt.timedelta(days=1)
    sz = sizes(cfg)
    families = list(cfg["job_families"])
    shares = [cfg["job_families"][f] for f in families]

    teams_per_family = largest_remainder(sz["teams"], shares)
    heads_per_family = largest_remainder(sz["initial"], shares)

    # teams (created_date may move earlier below, to the earliest member assignment: C3)
    t_lo, t_hi = (d(x) for x in cfg["teams"]["created_date_range"])
    order = 0
    family_teams: dict[str, list[Team]] = {}
    for fam, nt in zip(families, teams_per_family):
        family_teams[fam] = []
        for j in range(1, nt + 1):
            created = t_lo + dt.timedelta(days=int(rng_pop.integers(0, (t_hi - t_lo).days + 1)))
            team = Team(fam, j, f"{FAMILY_LABEL[fam]} {j:03d}", created, order); order += 1
            reg.teams.append(team); family_teams[fam].append(team)

    ic_levels = cfg["roles"]["ic_levels"]
    ic_mix = [cfg["ic_level_mix"][lvl] for lvl in ic_levels]
    mgr_levels = cfg["roles"]["manager_levels"]
    mgr_mix = [cfg["manager_level_mix"][lvl] for lvl in mgr_levels]
    pc = cfg["pre_window_career"]
    k_values = sorted(pc["promotion_count_prob"])
    k_probs = np.array([pc["promotion_count_prob"][k] for k in k_values], dtype=float)
    k_probs /= k_probs.sum()
    min_gap_years = float(pc["promotion_interval_years"]["min_years"])

    people = []   # (family, team, level, is_manager)
    for fam, nh, teams in zip(families, heads_per_family, (family_teams[f] for f in families)):
        n_mgr = len(teams)
        n_ic = nh - n_mgr
        if n_ic < 0:
            raise ValueError(f"family {fam}: fewer people than teams")
        mgr_counts = largest_remainder(n_mgr, mgr_mix)
        mgr_lvls = [lvl for lvl, c in zip(mgr_levels, mgr_counts) for _ in range(c)]
        rng_pop.shuffle(mgr_lvls)
        for team, lvl in zip(teams, mgr_lvls):
            people.append((fam, team, lvl, True))
        ic_counts = largest_remainder(n_ic, ic_mix)
        ic_lvls = [lvl for lvl, c in zip(ic_levels, ic_counts) for _ in range(c)]
        rng_pop.shuffle(ic_lvls)
        # team sizes vary around the target: weights ~ gamma(8), allocated exactly
        team_ic = largest_remainder(n_ic, list(rng_pop.gamma(8.0, 1.0, len(teams))))
        slots = [t for t, c in zip(teams, team_ic) for _ in range(c)]
        for team, lvl in zip(slots, ic_lvls):
            people.append((fam, team, lvl, False))

    stats = {"k_requested_capped": 0}
    for fam, team, lvl, is_mgr in people:
        tenure_days = _sample_tenure_days(rng_pop, cfg, window_start)
        hire = window_start - dt.timedelta(days=tenure_days)
        # C3: k sampled, then capped by feasibility (time and available level gap)
        k_req = int(rng_car.choice(k_values, p=k_probs))
        gap_available = lvl - 1
        time_cap = int(((last_pre_window_day - hire).days) // int(np.ceil(min_gap_years * 365.25)))
        k = min(k_req, gap_available, max(time_cap, 0))
        stats["k_requested_capped"] += k < k_req
        promo_dates = _promotion_dates(rng_car, hire, k, min_gap_years, last_pre_window_day)
        emp = Employee(hire, ts(hire), sort_key=(hire, team.order, -lvl, is_mgr))
        reg.employees.append(emp)
        reg.add_event(emp, "hire", hire, ts(hire))
        entry_level = lvl - k
        entry_is_mgr = is_mgr and k == 0
        reg.add_assignment(emp, team, reg.role(fam, entry_level, entry_is_mgr), hire, "hire", ts(hire))
        for i, pday in enumerate(promo_dates, 1):
            to_level = entry_level + i
            to_mgr = is_mgr and i == k            # last step of a manager's path is IC -> manager
            reg.add_promotion(emp, reg.role(fam, to_level, to_mgr), pday, ts(pday))
        team.created_date = min(team.created_date, hire)
    stats.update(sz)
    stats["teams_per_family"] = dict(zip(families, teams_per_family))
    stats["heads_per_family"] = dict(zip(families, heads_per_family))
    return stats
