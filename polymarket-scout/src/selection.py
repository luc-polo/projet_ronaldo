"""Stage 5 — rank Stage-4 survivors by a composite z-score and take the top N.

composite = 0.25*z(roi) + 0.20*z(profit_factor) + 0.20*z(monthly_consistency)
            + 0.20*copyability + 0.15*z(category_share)
copyability = z(median_hold_hours) + z(median_market_volume) - z(post_entry_drift)
Penalty: recent_vs_lifetime < -0.10  =>  score -= 0.5  (decay signal)

z-scores are computed across the survivor set only. With a single survivor every
z-score is 0, so composite collapses to just the penalty term — fine for ranking 1.
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np


def _z(values: List[float]) -> Dict[int, float]:
    arr = np.array([v if np.isfinite(v) else 0.0 for v in values], dtype=float)
    std = arr.std()
    if std == 0:
        return {i: 0.0 for i in range(len(arr))}
    mean = arr.mean()
    return {i: float((arr[i] - mean) / std) for i in range(len(arr))}


def rank_survivors(survivors: List[dict]) -> List[dict]:
    """`survivors`: list of {wallet, metrics, ...}. Returns same list with `score` and
    `rank` set, sorted best-first."""
    if not survivors:
        return []

    def col(key: str) -> List[float]:
        return [float(s["metrics"].get(key, 0.0) or 0.0) for s in survivors]

    z_roi = _z(col("roi"))
    z_pf = _z(col("profit_factor"))
    z_mc = _z(col("monthly_consistency"))
    z_hold = _z(col("median_hold_hours"))
    z_vol = _z(col("median_market_volume"))
    z_drift = _z(col("post_entry_drift"))
    z_cat = _z(col("category_share"))

    for i, s in enumerate(survivors):
        copyability = z_hold[i] + z_vol[i] - z_drift[i]
        score = (0.25 * z_roi[i] + 0.20 * z_pf[i] + 0.20 * z_mc[i]
                 + 0.20 * copyability + 0.15 * z_cat[i])
        if float(s["metrics"].get("recent_vs_lifetime", 0.0)) < -0.10:
            score -= 0.5
        s["composite"] = round(float(score), 4)
        s["copyability"] = round(float(copyability), 4)

    ranked = sorted(survivors, key=lambda s: -s["composite"])
    for i, s in enumerate(ranked, start=1):
        s["rank"] = i
    return ranked


def select_top(survivors: List[dict], top_n: int) -> List[dict]:
    ranked = rank_survivors(survivors)
    return ranked[:top_n]
