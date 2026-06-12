"""Stage 0 — seed a large, diverse pool of winning wallets from the leaderboard.

We page each requested time window for the run category, union + dedupe by wallet,
keep only winners (pnl > 0), then draw a *uniform* random sample (explicitly NOT
rank-weighted) so mid/low-rank winners are represented, not just the top.
"""
from __future__ import annotations

import csv
import os
import random
from dataclasses import dataclass, field
from typing import Dict, List, Set

from tqdm import tqdm

from .api_client import ApiClient
from .config import Config

PAGE = 50  # leaderboard limit is capped at 50


@dataclass
class Seed:
    wallet: str
    username: str
    pnl: float
    vol: float
    windows: Set[str] = field(default_factory=set)


def _page_window(client: ApiClient, cfg: Config, period: str) -> List[dict]:
    rows: List[dict] = []
    offset = 0
    total_pages = cfg.leaderboard_max_offset // PAGE + 1
    bar = tqdm(total=total_pages, desc=f"  leaderboard[{cfg.category}/{period}]",
               unit="pg", leave=False)
    while offset <= cfg.leaderboard_max_offset:
        page = client.leaderboard(cfg.category, period, limit=PAGE, offset=offset)
        bar.update(1)
        bar.set_postfix(rows=len(rows))
        if not page:
            break
        rows.extend(page)
        if len(page) < PAGE:  # short page => real end of data, stop
            break
        offset += PAGE
    bar.close()
    return rows


def seed_wallets(client: ApiClient, cfg: Config) -> List[Seed]:
    pool: Dict[str, Seed] = {}
    for period in cfg.time_periods:
        rows = _page_window(client, cfg, period)
        tqdm.write(f"  leaderboard[{cfg.category}/{period}]: {len(rows)} rows")
        for r in rows:
            wallet = r.get("proxyWallet")
            if not wallet:
                continue
            pnl = float(r.get("pnl") or 0)
            vol = float(r.get("vol") or 0)
            s = pool.get(wallet)
            if s is None:
                pool[wallet] = Seed(wallet, r.get("userName") or "", pnl, vol, {period})
            else:
                s.pnl = max(s.pnl, pnl)  # keep max seen pnl across windows
                s.vol = max(s.vol, vol)
                s.windows.add(period)
                if not s.username:
                    s.username = r.get("userName") or ""

    winners = [s for s in pool.values() if s.pnl > 0]
    tqdm.write(f"  union={len(pool)} wallets, winners(pnl>0)={len(winners)}")

    sampled = winners
    if cfg.sample_size > 0 and len(winners) > cfg.sample_size:
        rng = random.Random(cfg.random_seed)
        # sort first for determinism independent of dict ordering, then sample
        winners_sorted = sorted(winners, key=lambda s: s.wallet)
        sampled = rng.sample(winners_sorted, cfg.sample_size)
        tqdm.write(f"  uniform random sample (seed={cfg.random_seed}): {len(sampled)} wallets")
    elif cfg.sample_size == 0:
        tqdm.write(f"  SAMPLE_SIZE=0 => enriching ALL {len(winners)} winners (slow: ~5-20 calls/wallet)")

    _persist_seeds(cfg, sampled)
    return sampled


def _persist_seeds(cfg: Config, seeds: List[Seed]) -> None:
    os.makedirs(cfg.output_dir, exist_ok=True)
    path = os.path.join(cfg.output_dir, f"seeds_{cfg.category}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallet", "username", "pnl", "vol", "windows_seen"])
        for s in sorted(seeds, key=lambda s: -s.pnl):
            w.writerow([s.wallet, s.username, f"{s.pnl:.2f}", f"{s.vol:.2f}", "|".join(sorted(s.windows))])
    tqdm.write(f"  wrote {path}")
