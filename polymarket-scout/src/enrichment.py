"""Stage 1 — cheap per-wallet data fetch.

For every sampled wallet paginate /closed-positions and /activity?type=TRADE (each up
to its cap) and return two pandas DataFrames. Raw pages are cached transparently by the
ApiClient, so re-runs are nearly free.
"""
from __future__ import annotations

from typing import List, Tuple

import pandas as pd

from .api_client import ApiClient, OffsetExhausted
from .config import Config

PAGE = 50  # data-api page size cap for these endpoints

POSITION_COLS = [
    "conditionId", "asset", "avgPrice", "totalBought", "realizedPnl", "curPrice",
    "title", "slug", "eventSlug", "outcome", "endDate", "timestamp",
]
TRADE_COLS = [
    "conditionId", "asset", "timestamp", "side", "size", "usdcSize", "price",
    "outcome", "title", "slug",
]


def _paginate(fetch, cap: int) -> List[dict]:
    out: List[dict] = []
    offset = 0
    while len(out) < cap:
        try:
            page = fetch(limit=PAGE, offset=offset)
        except OffsetExhausted:
            break  # API offset ceiling reached (e.g. /activity ~3000) — stop gracefully
        if not page:
            break
        out.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    return out[:cap]


def _frame(rows: List[dict], cols: List[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=cols)
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    # numeric coercions
    for c in ("avgPrice", "totalBought", "realizedPnl", "curPrice", "size", "usdcSize",
              "price", "timestamp"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def enrich_wallet(client: ApiClient, cfg: Config, wallet: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    positions_raw = _paginate(
        lambda limit, offset: client.closed_positions(wallet, limit, offset),
        cfg.max_positions_per_wallet,
    )
    trades_raw = _paginate(
        lambda limit, offset: client.activity(wallet, limit, offset),
        cfg.max_activity_per_wallet,
    )
    positions = _frame(positions_raw, POSITION_COLS)
    trades = _frame(trades_raw, TRADE_COLS)
    # only TRADE rows survive (activity is already type=TRADE, but be defensive)
    if "side" in trades.columns and not trades.empty:
        trades = trades[trades["side"].isin(["BUY", "SELL"])].reset_index(drop=True)
    return positions, trades
