"""Stage 2 (cheap) + Stage 4 (expensive) metric computation.

Cheap metrics are PURE: (positions_df, trades_df, cfg, now) -> dict. They never touch
the network, so they are fully unit-testable. The expensive copyability metrics
(`median_market_volume`, `post_entry_drift`) need the API and live in their own
functions, called from main only for Stage-3 survivors.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .api_client import ApiClient
from .config import Config

# keyword fallback to map a market to the run category (plan: Gamma tags primary,
# keyword on slug fallback; we use keyword on slug/title/eventSlug which is robust
# and needs no extra network for the cheap Stage-3 gate).
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "POLITICS": ["election", "president", "senate", "congress", "trump", "biden", "vote",
                 "governor", "primary", "parliament", "minister", "political", "democrat",
                 "republican", "poll", "nomin", "cabinet", "supreme-court"],
    "SPORTS": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "tennis",
               "ufc", "boxing", "f1", "premier-league", "laliga", "atp", "wta", "itf",
               "champions-league", "world-cup", "olympics", "cricket", "golf", "cs2",
               "counter-strike", "dota", "valorant", "esports", "match", "vs-"],
    "CRYPTO": ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "coin", "token",
               "binance", "dogecoin", "xrp", "stablecoin", "defi", "nft"],
    "CULTURE": ["movie", "oscar", "grammy", "music", "celebrity", "tv", "award", "box-office",
                "album", "song", "rotten", "emmy", "netflix"],
    "MENTIONS": ["mention", "say", "tweet", "says"],
    "WEATHER": ["weather", "temperature", "hurricane", "snow", "rain", "climate", "storm"],
    "ECONOMICS": ["fed", "inflation", "cpi", "gdp", "rate-cut", "interest-rate", "jobs",
                  "unemployment", "recession", "economic"],
    "TECH": ["openai", "google", "apple", "tesla", "ai-", "gpt", "tech", "spacex", "nvidia",
             "microsoft", "meta", "chip"],
    "FINANCE": ["stock", "s-p-500", "sp500", "nasdaq", "dow", "earnings", "ipo", "market-cap",
                "shares", "treasury", "bond"],
}


def _now_ts(now: Optional[float]) -> float:
    return float(now) if now is not None else datetime.now(timezone.utc).timestamp()


def _iso_to_ts(val) -> Optional[float]:
    if val is None or (isinstance(val, float) and np.isnan(val)) or val == "":
        return None
    try:
        s = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _safe_div(a: float, b: float) -> float:
    return float(a) / float(b) if b else 0.0


def _match_category(row: pd.Series, category: str) -> bool:
    if category == "OVERALL":
        return True
    kws = CATEGORY_KEYWORDS.get(category, [])
    blob = " ".join(str(row.get(c) or "") for c in ("slug", "eventSlug", "title")).lower()
    return any(k in blob for k in kws)


def compute_cheap_metrics(
    positions: pd.DataFrame,
    trades: pd.DataFrame,
    cfg: Config,
    now: Optional[float] = None,
) -> Dict[str, float]:
    now_ts = _now_ts(now)
    m: Dict[str, float] = {}
    n = len(positions)
    m["n_resolved"] = float(n)
    m["n_trades"] = float(len(trades))

    # --- account age / recency from trade activity ---
    if not trades.empty and trades["timestamp"].notna().any():
        first_ts = float(trades["timestamp"].min())
        last_ts = float(trades["timestamp"].max())
        m["account_age_days"] = (now_ts - first_ts) / 86400.0
        m["days_since_last_trade"] = (now_ts - last_ts) / 86400.0
        active_days = trades["timestamp"].dropna().apply(
            lambda t: datetime.fromtimestamp(t, timezone.utc).date()).nunique()
        m["trades_per_day"] = _safe_div(len(trades), max(1, active_days))
    else:
        m["account_age_days"] = 0.0
        m["days_since_last_trade"] = 1e9
        m["trades_per_day"] = 0.0

    if n == 0:
        # nothing more to compute; fill zeros so gates fail cleanly
        for k in ("win_rate", "roi", "favorite_entry_share", "loss_share", "size_ratio",
                  "late_entry_share", "median_hold_hours", "category_share",
                  "profit_factor", "monthly_consistency", "recent_vs_lifetime"):
            m[k] = 0.0
        return m

    pnl = positions["realizedPnl"].fillna(0.0)
    bought = positions["totalBought"].fillna(0.0)

    m["win_rate"] = _safe_div((pnl > 0).sum(), n)
    m["loss_share"] = _safe_div((pnl < 0).sum(), n)
    m["roi"] = _safe_div(pnl.sum(), bought.sum())
    m["favorite_entry_share"] = _safe_div(
        (positions["avgPrice"].fillna(0.0) >= cfg.favorite_price).sum(), n)

    med_bought = float(bought[bought > 0].median()) if (bought > 0).any() else 0.0
    m["size_ratio"] = _safe_div(float(bought.max()), med_bought) if med_bought else 0.0

    gains = pnl[pnl > 0].sum()
    losses = pnl[pnl < 0].sum()
    m["profit_factor"] = _safe_div(float(gains), abs(float(losses))) if losses != 0 else float("inf") if gains > 0 else 0.0

    # --- monthly consistency ---
    pos = positions.copy()
    pos = pos[pos["timestamp"].notna()]
    if not pos.empty:
        months = pos["timestamp"].apply(
            lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m"))
        by_month = pos.assign(_m=months.values).groupby("_m")["realizedPnl"].sum()
        m["monthly_consistency"] = _safe_div((by_month > 0).sum(), len(by_month))
    else:
        m["monthly_consistency"] = 0.0

    # --- recent vs lifetime win rate (last 30 resolved by exit timestamp) ---
    pos_sorted = positions.sort_values("timestamp")
    recent = pos_sorted.tail(30)
    recent_wr = _safe_div((recent["realizedPnl"].fillna(0.0) > 0).sum(), len(recent))
    m["recent_vs_lifetime"] = recent_wr - m["win_rate"]

    # --- category specialization (keyword fallback map) ---
    matches = positions.apply(lambda r: _match_category(r, cfg.category), axis=1).sum()
    m["category_share"] = _safe_div(int(matches), n)

    # --- late entry + hold time (need first BUY per conditionId from trades) ---
    first_buy: Dict[str, float] = {}
    last_sell: Dict[str, float] = {}
    if not trades.empty:
        buys = trades[trades["side"] == "BUY"]
        for cid, g in buys.groupby("conditionId"):
            first_buy[cid] = float(g["timestamp"].min())
        sells = trades[trades["side"] == "SELL"]
        for cid, g in sells.groupby("conditionId"):
            last_sell[cid] = float(g["timestamp"].max())

    late_num = late_den = 0
    holds: List[float] = []
    for _, p in positions.iterrows():
        cid = p.get("conditionId")
        fb = first_buy.get(cid)
        end_ts = _iso_to_ts(p.get("endDate"))
        # late entry: first buy within 60 min of market endDate
        if fb is not None and end_ts is not None:
            late_den += 1
            if (end_ts - fb) <= 3600:
                late_num += 1
        # hold time: exit = last SELL if any else resolution (position.timestamp)
        if fb is not None:
            exit_ts = last_sell.get(cid)
            if exit_ts is None or exit_ts < fb:
                exit_ts = float(p["timestamp"]) if pd.notna(p.get("timestamp")) else None
            if exit_ts is not None and exit_ts >= fb:
                holds.append((exit_ts - fb) / 3600.0)

    m["late_entry_share"] = _safe_div(late_num, late_den) if late_den else 0.0
    m["median_hold_hours"] = float(np.median(holds)) if holds else 0.0
    return m


# ---- Stage 4 expensive copyability metrics (need network) -------------------
def compute_median_market_volume(
    client: ApiClient, positions: pd.DataFrame
) -> float:
    cids = [c for c in positions["conditionId"].dropna().unique().tolist() if c]
    if not cids:
        return 0.0
    vol_by_cid: Dict[str, float] = {}
    # batch in chunks to keep URLs sane
    CHUNK = 20
    for i in range(0, len(cids), CHUNK):
        chunk = cids[i:i + CHUNK]
        markets = client.get_markets_by_conditions(chunk)
        for mk in markets:
            cid = mk.get("conditionId")
            vol = mk.get("volume")
            if cid is not None and vol is not None:
                try:
                    vol_by_cid[cid] = float(vol)
                except (TypeError, ValueError):
                    pass
    vols = [vol_by_cid[c] for c in cids if c in vol_by_cid]
    return float(np.median(vols)) if vols else 0.0


def compute_post_entry_drift(
    client: ApiClient, cfg: Config, trades: pd.DataFrame, rng: random.Random
) -> Optional[float]:
    """Median |price(entry+30m) - entry price| over sampled BUY trades.

    Returns None if no usable sample (caller decides how to gate). Big drift => market
    moves too fast for a copy bot to get a comparable fill.
    """
    if trades.empty:
        return None
    buys = trades[(trades["side"] == "BUY") & trades["asset"].notna()
                  & trades["price"].notna() & trades["timestamp"].notna()]
    if buys.empty:
        return None
    sample = buys.sample(
        n=min(cfg.drift_sample_trades, len(buys)), random_state=cfg.random_seed
    ) if len(buys) > cfg.drift_sample_trades else buys

    drifts: List[float] = []
    for _, t in sample.iterrows():
        token = str(t["asset"])
        entry_ts = int(t["timestamp"])
        entry_price = float(t["price"])
        try:
            hist = client.prices_history(token, entry_ts, entry_ts + 1800, fidelity=1)
        except Exception:
            continue
        pts = hist.get("history") if isinstance(hist, dict) else None
        if not pts:
            continue
        last_p = float(pts[-1]["p"])
        drifts.append(abs(last_p - entry_price))
    if not drifts:
        return None
    return float(np.median(drifts))
