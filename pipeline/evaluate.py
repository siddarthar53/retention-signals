"""Temporal training and evaluation (G4).

Dates (Agreement v1, approved cuts): train 2025-03-31 + 2025-06-30, validate 2025-09-30, freeze
2026-04-01 (after validation labels are available), single primary test 2026-06-30.
Label access is split: training code receives only train/validation labels; test labels are read
only in `evaluate_test`, after the models are frozen.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from .features import Sources, build
from .model import LogisticScorer, Scorecard, config_hash, select_lr

TRAIN_DATES = [dt.date(2025, 3, 31), dt.date(2025, 6, 30)]
VALIDATION_DATE = dt.date(2025, 9, 30)
TEST_DATE = dt.date(2026, 6, 30)
FREEZE_AT = "2026-04-01 09:00:00+00"
DIAGNOSTICS = {   # predeclared before the test run; reported as met / missed, never tuned
    "lr_lift_at_5pct_min": 2.0,
    "lr_lift_ci_excludes_1": True,
    "scorecard_pr_auc_vs_lr_min_ratio": 0.75,
    "calibration_slope_range": [0.8, 1.2],
}


def labels_for(conn, dates: list[dt.date]) -> pd.DataFrame:
    """Labels for the given dates only (the test date is refused until the models are frozen)."""
    cur = conn.execute("""SELECT employee_id, as_of_date, target FROM eval.horizon_label
                          WHERE as_of_date = ANY(%s) AND target IS NOT NULL""", (dates,))
    return pd.DataFrame(cur.fetchall(), columns=["employee_id", "as_of_date", "target"])


def _xy(src, labels, dates):
    frames = []
    for d in dates:
        f, _ = build(src, d)
        lab = labels[labels.as_of_date == d]
        m = f.merge(lab, on=["employee_id", "as_of_date"], how="left", validate="one_to_one")
        if m.target.isna().any():
            raise AssertionError(f"{d}: scoring keys without a resolved label")
        frames.append(m)
    df = pd.concat(frames, ignore_index=True)
    return df, df.target.astype(int).to_numpy()


@dataclass
class Frozen:
    lr: LogisticScorer
    sc: Scorecard
    trials: list
    train_n: int
    train_pos: int
    val_n: int
    val_pos: int


def train(conn, src: Sources) -> Frozen:
    labs = labels_for(conn, TRAIN_DATES + [VALIDATION_DATE])
    X_tr, y_tr = _xy(src, labs, TRAIN_DATES)
    X_va, y_va = _xy(src, labs, [VALIDATION_DATE])
    lr, trials = select_lr(X_tr, y_tr, X_va, y_va)
    sc = Scorecard().fit(X_tr, y_tr)
    return Frozen(lr, sc, trials, len(y_tr), int(y_tr.sum()), len(y_va), int(y_va.sum()))


def at_k(y, s, frac):
    k = max(1, int(round(len(y) * frac)))
    top = np.argsort(-s, kind="stable")[:k]
    prec = y[top].mean()
    return {"k": k, "precision": float(prec), "recall": float(y[top].sum() / max(y.sum(), 1)),
            "lift": float(prec / y.mean()), "positives_in_top": int(y[top].sum())}


def calibration_slope(y, p) -> float:
    from sklearn.linear_model import LogisticRegression
    z = np.log(p / (1 - p)).reshape(-1, 1)
    return float(LogisticRegression(C=1e6, max_iter=5000).fit(z, y).coef_[0][0])


def metrics(y, p) -> dict:
    return {"n": int(len(y)), "positives": int(y.sum()), "prevalence": float(y.mean()),
            "roc_auc": float(roc_auc_score(y, p)), "pr_auc": float(average_precision_score(y, p)),
            "brier": float(brier_score_loss(y, p)), "calibration_slope": calibration_slope(y, p),
            "top5": at_k(y, p, 0.05), "top10": at_k(y, p, 0.10)}


def bootstrap(y, p, ids, n=1000, seed=20261022) -> dict:
    """Percentile CIs, resampling employees (one row per employee on a single test date)."""
    rng = np.random.default_rng(seed)
    uniq = np.unique(ids)
    index = {e: np.where(ids == e)[0] for e in uniq}
    stats = {"lift5": [], "pr_auc": [], "precision5": []}
    for _ in range(n):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        rows = np.concatenate([index[e] for e in pick])
        yy, pp = y[rows], p[rows]
        if yy.sum() == 0:
            continue
        t = at_k(yy, pp, 0.05)
        stats["lift5"].append(t["lift"]); stats["precision5"].append(t["precision"])
        stats["pr_auc"].append(average_precision_score(yy, pp))
    return {k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] for k, v in stats.items()}


def evaluate_test(conn, src: Sources, fz: Frozen) -> dict:
    labs = labels_for(conn, [TEST_DATE])
    X, y = _xy(src, labs, [TEST_DATE])
    out = {}
    for name, model in (("logistic_regression", fz.lr), ("scorecard", fz.sc)):
        p = model.predict(X)
        out[name] = metrics(y, p)
        out[name]["ci95"] = bootstrap(y, p, X.employee_id.to_numpy())
    lr, sc = out["logistic_regression"], out["scorecard"]
    out["diagnostics"] = {
        "lr_lift_at_5pct": {"value": lr["top5"]["lift"], "target": f">= {DIAGNOSTICS['lr_lift_at_5pct_min']}",
                            "met": lr["top5"]["lift"] >= DIAGNOSTICS["lr_lift_at_5pct_min"]},
        "lr_lift_ci_excludes_1": {"value": lr["ci95"]["lift5"], "met": lr["ci95"]["lift5"][0] > 1.0},
        "scorecard_pr_auc_ratio": {"value": sc["pr_auc"] / lr["pr_auc"],
                                   "target": f">= {DIAGNOSTICS['scorecard_pr_auc_vs_lr_min_ratio']}",
                                   "met": sc["pr_auc"] / lr["pr_auc"] >= DIAGNOSTICS["scorecard_pr_auc_vs_lr_min_ratio"]},
        "lr_calibration_slope": {"value": lr["calibration_slope"], "target": str(DIAGNOSTICS["calibration_slope_range"]),
                                 "met": DIAGNOSTICS["calibration_slope_range"][0] <= lr["calibration_slope"]
                                 <= DIAGNOSTICS["calibration_slope_range"][1]},
    }
    return out


def model_card(fz: Frozen) -> dict:
    return {"frozen_at": FREEZE_AT, "train_dates": [str(d) for d in TRAIN_DATES],
            "validation_date": str(VALIDATION_DATE), "train_rows": fz.train_n, "train_positives": fz.train_pos,
            "validation_rows": fz.val_n, "validation_positives": fz.val_pos,
            "lr_C_trials_validation_pr_auc": fz.trials, "logistic_regression": fz.lr.to_dict(),
            "scorecard": fz.sc.to_dict(), "lr_config_hash": config_hash(fz.lr.to_dict()),
            "scorecard_config_hash": config_hash(fz.sc.to_dict())}


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)
