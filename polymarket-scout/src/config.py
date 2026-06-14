"""Central configuration. Everything is loaded from `.env` (python-dotenv).

A single frozen `Config` dataclass is built once via `load_config()` and threaded
through every stage, so there is exactly one source of truth and nothing reads the
environment directly after startup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List

from dotenv import load_dotenv

VALID_CATEGORIES = {
    "OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE",
    "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE",
}
VALID_PERIODS = {"DAY", "WEEK", "MONTH", "ALL"}


def _get(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    return int(float(_get(name, str(default))))


def _get_float(name: str, default: float) -> float:
    return float(_get(name, str(default)))


def _sanitize(s: str) -> str:
    """Keep a RUN_LABEL safe for filesystem paths."""
    return "".join(c for c in s if c.isalnum() or c in "-_")


@dataclass(frozen=True)
class Config:
    # run scope
    category: str
    time_periods: List[str]
    sample_size: int
    random_seed: int
    top_n: int

    # hard criteria
    min_resolved: int
    min_account_age_days: float
    max_days_since_last_trade: float
    win_rate_min: float
    win_rate_max: float
    favorite_price: float
    max_favorite_entry_share: float
    min_loss_share: float
    min_roi: float
    roi_trim_top_n: int
    max_trades_per_day: float
    max_size_ratio: float
    max_late_entry_share: float
    min_median_hold_hours: float
    min_median_market_volume: float
    max_post_entry_drift: float
    min_category_share: float

    # cost controls
    max_positions_per_wallet: int
    max_activity_per_wallet: int
    drift_sample_trades: int
    request_rate: float
    leaderboard_max_offset: int
    cache_dir: str
    output_dir: str
    # per-run isolated output folder: output/{CATEGORY}_{YYYY_MM_DD-HHMMSS}[_LABEL]
    run_label: str
    run_dir: str

    # endpoint bases (not in .env; stable constants)
    data_api: str = "https://data-api.polymarket.com"
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_api: str = "https://clob.polymarket.com"


def load_config(env_path: str | None = None) -> Config:
    load_dotenv(env_path, override=False)

    category = _get("CATEGORY", "POLITICS").upper()
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"CATEGORY={category!r} invalid. Choose one of {sorted(VALID_CATEGORIES)}"
        )

    periods = [p.strip().upper() for p in _get("TIME_PERIODS", "WEEK,MONTH,ALL").split(",") if p.strip()]
    bad = set(periods) - VALID_PERIODS
    if bad:
        raise ValueError(f"TIME_PERIODS contains invalid {sorted(bad)}; valid: {sorted(VALID_PERIODS)}")

    output_dir = _get("OUTPUT_DIR", "output")
    run_label = _sanitize(_get("RUN_LABEL", ""))
    stamp = datetime.now(timezone.utc).strftime("%Y_%m_%d-%H%M%S")
    folder = f"{category}_{stamp}" + (f"_{run_label}" if run_label else "")
    run_dir = os.path.join(output_dir, folder)

    return Config(
        category=category,
        time_periods=periods,
        sample_size=_get_int("SAMPLE_SIZE", 400),
        random_seed=_get_int("RANDOM_SEED", 42),
        top_n=_get_int("TOP_N", 12),
        min_resolved=_get_int("MIN_RESOLVED", 90),
        min_account_age_days=_get_float("MIN_ACCOUNT_AGE_DAYS", 120),
        max_days_since_last_trade=_get_float("MAX_DAYS_SINCE_LAST_TRADE", 14),
        win_rate_min=_get_float("WIN_RATE_MIN", 0.52),
        win_rate_max=_get_float("WIN_RATE_MAX", 0.80),
        favorite_price=_get_float("FAVORITE_PRICE", 0.88),
        max_favorite_entry_share=_get_float("MAX_FAVORITE_ENTRY_SHARE", 0.30),
        min_loss_share=_get_float("MIN_LOSS_SHARE", 0.05),
        min_roi=_get_float("MIN_ROI", 0.05),
        roi_trim_top_n=_get_int("ROI_TRIM_TOP_N", 3),
        max_trades_per_day=_get_float("MAX_TRADES_PER_DAY", 25),
        max_size_ratio=_get_float("MAX_SIZE_RATIO", 15),
        max_late_entry_share=_get_float("MAX_LATE_ENTRY_SHARE", 0.20),
        min_median_hold_hours=_get_float("MIN_MEDIAN_HOLD_HOURS", 4),
        min_median_market_volume=_get_float("MIN_MEDIAN_MARKET_VOLUME", 50000),
        max_post_entry_drift=_get_float("MAX_POST_ENTRY_DRIFT", 0.05),
        min_category_share=_get_float("MIN_CATEGORY_SHARE", 0.40),
        max_positions_per_wallet=_get_int("MAX_POSITIONS_PER_WALLET", 1000),
        max_activity_per_wallet=_get_int("MAX_ACTIVITY_PER_WALLET", 5000),
        drift_sample_trades=_get_int("DRIFT_SAMPLE_TRADES", 15),
        request_rate=_get_float("REQUEST_RATE", 10),
        leaderboard_max_offset=_get_int("LEADERBOARD_MAX_OFFSET", 3000),
        cache_dir=_get("CACHE_DIR", ".cache"),
        output_dir=output_dir,
        run_label=run_label,
        run_dir=run_dir,
    )
