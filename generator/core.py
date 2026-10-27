"""Config, random streams, allocation and date helpers shared by all generator stages."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
STREAMS = ("master", "population", "careers", "monthly", "hazard", "injection")
DAYS_PER_MONTH = 30.4375


def load_config(path: Path | None = None) -> dict:
    path = path or ROOT / "config" / "master_data.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return cfg


class Streams:
    """Independent, named random streams spawned from one seed.

    Each stage draws only from its own stream, so changing one stage cannot shift another's draws.
    """

    def __init__(self, seed: int):
        children = np.random.SeedSequence(seed).spawn(len(STREAMS))
        self._gens = {name: np.random.default_rng(ss) for name, ss in zip(STREAMS, children)}

    def __getitem__(self, name: str) -> np.random.Generator:
        return self._gens[name]


def largest_remainder(total: int, weights: list[float]) -> list[int]:
    """Split an integer total proportionally to weights; parts sum exactly to total.

    Ties in the remainder are broken by list order (earlier wins), so the result is deterministic.
    """
    if total < 0:
        raise ValueError("total must be >= 0")
    w = np.asarray(weights, dtype=float)
    if (w < 0).any() or w.sum() <= 0:
        raise ValueError("weights must be non-negative with a positive sum")
    quotas = w / w.sum() * total
    base = np.floor(quotas).astype(int)
    short = total - int(base.sum())
    remainders = quotas - base
    order = sorted(range(len(w)), key=lambda i: (-remainders[i], i))
    for i in order[:short]:
        base[i] += 1
    return [int(x) for x in base]


def d(s: str | dt.date) -> dt.date:
    return s if isinstance(s, dt.date) else dt.date.fromisoformat(str(s))


def add_months(day: dt.date, months: int) -> dt.date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    last = (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(day.day, last))


def months_between(start: dt.date, end: dt.date) -> float:
    return (end - start).days / DAYS_PER_MONTH


def ts(day: dt.date, hour: int = 9, minute: int = 0) -> str:
    """Timestamp text for timestamptz columns (UTC, fixed format)."""
    return f"{day.isoformat()} {hour:02d}:{minute:02d}:00+00"


def stable_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))
