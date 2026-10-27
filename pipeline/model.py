"""Scorers (G4): a transparent points scorecard and logistic regression with exact log-odds
explanations. Both are fitted on training dates only; logistic regression's regularisation is chosen on
the validation date; nothing is tuned on the test date.

Missing engagement is handled with a NEUTRAL reference: it is replaced by the training mean, so its
log-odds contribution is exactly 0 (never a penalty). Coverage is reported separately, not modelled.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score

from .features import FEATURES

FEATURE_LABELS = {
    "engagement_latest": "Engagement score (latest survey)",
    "engagement_decline": "Drop in engagement since the previous survey",
    "util_mean_3m": "Average utilisation, last 3 reported months",
    "sustained_overload": "Sustained overload (3 of last 4 months above 115%)",
    "after_hours_mean_3m": "After-hours work share, last 3 months",
    "oncall_mean_3m": "On-call days per month, last 3 months",
    "promo_clock_over_24": "Months beyond 2 years since last promotion or hire",
    "months_in_role": "Months in current role",
    "tenure_months": "Tenure in months",
    "new_joiner": "Joined within the last 12 months",
    "recent_transfer": "Transferred within the last 6 months",
}


@dataclass
class LogisticScorer:
    C: float
    means: dict = field(default_factory=dict)
    stds: dict = field(default_factory=dict)
    coef: dict = field(default_factory=dict)
    intercept: float = 0.0
    version: str = "lr-v1"

    def _prep(self, X: pd.DataFrame) -> np.ndarray:
        Z = X[FEATURES].astype(float).copy()
        for c in FEATURES:
            Z[c] = (Z[c].fillna(self.means[c]) - self.means[c]) / self.stds[c]
        return Z.to_numpy()

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> "LogisticScorer":
        self.means = {c: float(X[c].astype(float).mean()) for c in FEATURES}
        self.stds = {c: float(X[c].astype(float).std(ddof=0)) or 1.0 for c in FEATURES}
        m = LogisticRegression(C=self.C, max_iter=5000).fit(self._prep(X), y)
        self.coef = dict(zip(FEATURES, (float(v) for v in m.coef_[0])))
        self.intercept = float(m.intercept_[0])
        return self

    def logit(self, X: pd.DataFrame) -> np.ndarray:
        return self._prep(X) @ np.array([self.coef[c] for c in FEATURES]) + self.intercept

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return 1 / (1 + np.exp(-self.logit(X)))

    def contributions(self, X: pd.DataFrame) -> list[list[dict]]:
        """Per person: [{feature, label, value, reference, log_odds}] sorted by |log_odds|; plus intercept.
        log_odds = coef x (value - training mean) / training sd; missing values use the mean -> 0."""
        Z = self._prep(X)
        out = []
        for i in range(len(X)):
            row = []
            for j, c in enumerate(FEATURES):
                v = X.iloc[i][c]
                row.append({"feature": c, "label": FEATURE_LABELS[c],
                            "value": None if pd.isna(v) else round(float(v), 3),
                            "reference": round(self.means[c], 3),
                            "log_odds": round(float(self.coef[c] * Z[i, j]), 4)})
            row.sort(key=lambda r: -abs(r["log_odds"]))
            row.append({"feature": "intercept", "label": "Baseline (average employee)", "value": None,
                        "reference": None, "log_odds": round(self.intercept, 4)})
            out.append(row)
        return out

    def to_dict(self) -> dict:
        return {"type": "logistic_regression", "version": self.version, "C": self.C, "features": FEATURES,
                "means": self.means, "stds": self.stds, "coef": self.coef, "intercept": self.intercept}


# Scorecard rules are written before looking at any outcome (from the design's risk drivers).
SCORECARD_RULES = [
    ("engagement_latest", "Engagement below 3.0", lambda v: v < 3.0, 3),
    ("engagement_latest", "Engagement 3.0-3.4", lambda v: 3.0 <= v < 3.5, 1),
    ("engagement_decline", "Engagement dropped by 0.5 or more", lambda v: v >= 0.5, 2),
    ("sustained_overload", "Sustained overload", lambda v: v >= 1, 2),
    ("promo_clock_over_24", "3+ years without promotion", lambda v: v >= 12, 1),
    ("promo_clock_over_24", "4+ years without promotion", lambda v: v >= 24, 1),
    ("new_joiner", "Joined within 12 months", lambda v: v >= 1, 1),
]


@dataclass
class Scorecard:
    a: float = 0.0
    b: float = 0.0
    version: str = "scorecard-v1"

    @staticmethod
    def points_detail(X: pd.DataFrame) -> tuple[np.ndarray, list[list[dict]]]:
        pts = np.zeros(len(X))
        detail = [[] for _ in range(len(X))]
        for feat, label, rule, p in SCORECARD_RULES:
            vals = X[feat].astype(float).to_numpy()
            for i, v in enumerate(vals):
                if not np.isnan(v) and rule(v):           # missing values never earn points
                    pts[i] += p
                    detail[i].append({"rule": label, "points": p})
        return pts, detail

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> "Scorecard":
        """Map points to a probability with a one-variable logistic fit on training data only."""
        pts, _ = self.points_detail(X)
        m = LogisticRegression(C=1e6, max_iter=5000).fit(pts.reshape(-1, 1), y)
        self.a, self.b = float(m.intercept_[0]), float(m.coef_[0][0])
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        pts, _ = self.points_detail(X)
        return 1 / (1 + np.exp(-(self.a + self.b * pts)))

    def to_dict(self) -> dict:
        return {"type": "scorecard", "version": self.version, "a": self.a, "b": self.b,
                "rules": [(f, l, p) for f, l, _, p in SCORECARD_RULES]}


def select_lr(X_train, y_train, X_val, y_val, grid=(0.01, 0.03, 0.1, 0.3, 1.0, 3.0)) -> tuple[LogisticScorer, list]:
    """Choose C by validation PR-AUC (ties -> stronger regularisation)."""
    trials = []
    for C in grid:
        m = LogisticScorer(C).fit(X_train, y_train)
        trials.append((C, float(average_precision_score(y_val, m.predict(X_val)))))
    best_C = max(trials, key=lambda t: (round(t[1], 4), -t[0]))[0]
    return LogisticScorer(best_C).fit(X_train, y_train), trials


def config_hash(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()
