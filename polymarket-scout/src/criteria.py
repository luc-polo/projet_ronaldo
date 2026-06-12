"""Stage 3/4 — modular hard-gate registry.

Each gate is a small dataclass {name, predicate(metrics, cfg)->bool, threshold_desc}.
Adding/removing a criterion is one line in HARD_GATES. STRICT: no relaxation anywhere.

`CHEAP_GATES` run on Stage-2 metrics for every sampled wallet. `EXPENSIVE_GATES` run
only on survivors after the Stage-4 copyability metrics are attached (two-stage funnel).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List

from .config import Config

Metrics = Dict[str, float]


@dataclass(frozen=True)
class Gate:
    name: str
    predicate: Callable[[Metrics, Config], bool]
    desc: Callable[[Config], str]


CHEAP_GATES: List[Gate] = [
    Gate("MIN_RESOLVED", lambda m, c: m["n_resolved"] >= c.min_resolved,
         lambda c: f"n_resolved >= {c.min_resolved}"),
    Gate("MIN_ACCOUNT_AGE_DAYS", lambda m, c: m["account_age_days"] >= c.min_account_age_days,
         lambda c: f"account_age_days >= {c.min_account_age_days}"),
    Gate("MAX_DAYS_SINCE_LAST_TRADE", lambda m, c: m["days_since_last_trade"] <= c.max_days_since_last_trade,
         lambda c: f"days_since_last_trade <= {c.max_days_since_last_trade}"),
    Gate("WIN_RATE_MIN", lambda m, c: m["win_rate"] >= c.win_rate_min,
         lambda c: f"win_rate >= {c.win_rate_min}"),
    Gate("WIN_RATE_MAX", lambda m, c: m["win_rate"] <= c.win_rate_max,
         lambda c: f"win_rate <= {c.win_rate_max}"),
    Gate("MAX_FAVORITE_ENTRY_SHARE", lambda m, c: m["favorite_entry_share"] <= c.max_favorite_entry_share,
         lambda c: f"favorite_entry_share <= {c.max_favorite_entry_share}"),
    Gate("MIN_LOSS_SHARE", lambda m, c: m["loss_share"] >= c.min_loss_share,
         lambda c: f"loss_share >= {c.min_loss_share}"),
    Gate("MIN_ROI", lambda m, c: m["roi"] >= c.min_roi,
         lambda c: f"roi >= {c.min_roi}"),
    Gate("MAX_TRADES_PER_DAY", lambda m, c: m["trades_per_day"] <= c.max_trades_per_day,
         lambda c: f"trades_per_day <= {c.max_trades_per_day}"),
    Gate("MAX_SIZE_RATIO", lambda m, c: 0 < m["size_ratio"] <= c.max_size_ratio,
         lambda c: f"size_ratio <= {c.max_size_ratio}"),
    Gate("MAX_LATE_ENTRY_SHARE", lambda m, c: m["late_entry_share"] <= c.max_late_entry_share,
         lambda c: f"late_entry_share <= {c.max_late_entry_share}"),
    Gate("MIN_MEDIAN_HOLD_HOURS", lambda m, c: m["median_hold_hours"] >= c.min_median_hold_hours,
         lambda c: f"median_hold_hours >= {c.min_median_hold_hours}"),
    Gate("MIN_CATEGORY_SHARE", lambda m, c: m["category_share"] >= c.min_category_share,
         lambda c: f"category_share >= {c.min_category_share}"),
]

EXPENSIVE_GATES: List[Gate] = [
    Gate("MIN_MEDIAN_MARKET_VOLUME", lambda m, c: m.get("median_market_volume", 0) >= c.min_median_market_volume,
         lambda c: f"median_market_volume >= {c.min_median_market_volume}"),
    Gate("MAX_POST_ENTRY_DRIFT",
         lambda m, c: (m.get("post_entry_drift") is not None) and m["post_entry_drift"] <= c.max_post_entry_drift,
         lambda c: f"post_entry_drift <= {c.max_post_entry_drift}"),
]

HARD_GATES: List[Gate] = CHEAP_GATES + EXPENSIVE_GATES


def evaluate(gates: List[Gate], metrics: Metrics, cfg: Config) -> Dict[str, bool]:
    """Return {gate_name: passed}. A gate raising on a missing metric counts as fail."""
    out: Dict[str, bool] = {}
    for g in gates:
        try:
            out[g.name] = bool(g.predicate(metrics, cfg))
        except (KeyError, TypeError):
            out[g.name] = False
    return out


def passed_all(results: Dict[str, bool]) -> bool:
    return all(results.values())
