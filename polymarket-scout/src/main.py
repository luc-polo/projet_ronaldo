"""Orchestrator: seed -> enrich -> cheap metrics+gates -> expensive gates -> select -> report.

Run:  python -m src.main          (uses .env)
Two-stage funnel: only Stage-3 (cheap-gate) survivors pay for the expensive Stage-4
copyability checks, keeping network cost down.
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd
from tqdm import tqdm

from .api_client import ApiClient, smoke_test
from .config import Config, load_config
from .criteria import CHEAP_GATES, EXPENSIVE_GATES, HARD_GATES, evaluate, passed_all
from .enrichment import enrich_wallet
from .metrics import (
    compute_cheap_metrics,
    compute_median_market_volume,
    compute_post_entry_drift,
)
from .report import build_report
from .seeding import seed_wallets


def _hold_list(positions: pd.DataFrame, trades: pd.DataFrame) -> List[float]:
    """Recompute per-position hold hours for charting (mirrors metrics logic)."""
    from .metrics import _iso_to_ts  # reuse
    first_buy, last_sell = {}, {}
    if not trades.empty:
        for cid, g in trades[trades["side"] == "BUY"].groupby("conditionId"):
            first_buy[cid] = float(g["timestamp"].min())
        for cid, g in trades[trades["side"] == "SELL"].groupby("conditionId"):
            last_sell[cid] = float(g["timestamp"].max())
    holds = []
    for _, p in positions.iterrows():
        cid = p.get("conditionId")
        fb = first_buy.get(cid)
        if fb is None:
            continue
        ex = last_sell.get(cid)
        if ex is None or ex < fb:
            ex = float(p["timestamp"]) if pd.notna(p.get("timestamp")) else None
        if ex is not None and ex >= fb:
            holds.append((ex - fb) / 3600.0)
    return holds


def run(cfg: Config) -> str:
    client = ApiClient(cfg)
    tqdm.write("STEP 0 — smoke-testing endpoints...")
    smoke_test(client)
    tqdm.write("  smoke test OK\n")

    funnel: Dict[str, int] = {}

    # ---- Stage 0: seeding ----
    tqdm.write("STAGE 0 — seeding from leaderboard")
    seeds = seed_wallets(client, cfg)
    funnel["seeded_then_sampled"] = len(seeds)

    # ---- Stage 1-3: enrich + cheap metrics + cheap gates ----
    tqdm.write("\nSTAGE 1-3 — enrich + cheap gates")
    candidate_rows: List[dict] = []
    survivors: List[dict] = []
    cheap_pass_counts = {g.name: 0 for g in CHEAP_GATES}

    for s in tqdm(seeds, desc="wallets", unit="w"):
        wallet = s.wallet
        positions, trades = enrich_wallet(client, cfg, wallet)
        metrics = compute_cheap_metrics(positions, trades, cfg)
        cheap_results = evaluate(CHEAP_GATES, metrics, cfg)
        for name, ok in cheap_results.items():
            if ok:
                cheap_pass_counts[name] += 1

        row = {"wallet": wallet, "username": s.username, "seed_pnl": round(s.pnl, 2)}
        row.update({k: round(v, 4) if isinstance(v, float) else v for k, v in metrics.items()})
        row.update({f"gate_{k}": ok for k, ok in cheap_results.items()})

        if passed_all(cheap_results):
            survivors.append({
                "wallet": wallet, "username": s.username, "metrics": metrics,
                "gate_results": dict(cheap_results), "positions": positions, "trades": trades,
            })
        candidate_rows.append(row)

    funnel["passed_cheap_gates"] = len(survivors)

    # ---- Stage 4: expensive copyability gates on survivors only ----
    tqdm.write(f"\nSTAGE 4 — copyability gates on {len(survivors)} survivor(s)")
    rng = random.Random(cfg.random_seed)
    final_survivors: List[dict] = []
    for s in tqdm(survivors, desc="copyability", unit="w"):
        positions, trades = s["positions"], s["trades"]
        med_vol = compute_median_market_volume(client, positions)
        drift = compute_post_entry_drift(client, cfg, trades, rng)
        s["metrics"]["median_market_volume"] = med_vol
        s["metrics"]["post_entry_drift"] = drift if drift is not None else float("nan")
        exp_results = evaluate(EXPENSIVE_GATES, s["metrics"], cfg)
        s["gate_results"].update(exp_results)
        s["holds"] = _hold_list(positions, trades)
        # store the drift sample points for the report chart
        s["drift_samples"] = _drift_points(client, cfg, trades)
        # update candidate row with expensive metrics + gates
        for r in candidate_rows:
            if r["wallet"] == s["wallet"]:
                r["median_market_volume"] = round(med_vol, 2)
                r["post_entry_drift"] = round(drift, 4) if drift is not None else None
                for k, ok in exp_results.items():
                    r[f"gate_{k}"] = ok
        if passed_all(s["gate_results"]):
            final_survivors.append(s)

    funnel["passed_all_gates"] = len(final_survivors)

    # ---- Stage 5: selection ----
    from .selection import select_top
    selected = select_top(final_survivors, cfg.top_n)
    funnel["selected"] = len(selected)

    # ---- candidates.csv ----
    candidates_df = pd.DataFrame(candidate_rows)
    os.makedirs(cfg.run_dir, exist_ok=True)
    cand_path = os.path.join(cfg.run_dir, f"candidates_{cfg.category}.csv")
    candidates_df.to_csv(cand_path, index=False)
    tqdm.write(f"\n  wrote {cand_path} ({len(candidates_df)} rows)")

    # ---- Stage 6: report ----
    run_date = datetime.now(timezone.utc)
    # enrich funnel with per-gate cheap pass counts (visibility into the funnel shape)
    full_funnel = {"seeded_sampled": funnel["seeded_then_sampled"]}
    for g in CHEAP_GATES:
        full_funnel[f"pass_{g.name}"] = cheap_pass_counts[g.name]
    full_funnel["passed_ALL_cheap_gates"] = funnel["passed_cheap_gates"]
    full_funnel["passed_copyability_gates"] = funnel["passed_all_gates"]
    full_funnel["FINAL_selected"] = funnel["selected"]

    report_path = build_report(cfg, full_funnel, selected, candidates_df, run_date)
    tqdm.write(f"\nDONE. Report: {report_path}")
    tqdm.write(f"  network calls={client.network_calls}  cache hits={client.cache_hits}")
    return report_path


def _drift_points(client: ApiClient, cfg: Config, trades: pd.DataFrame) -> List[float]:
    """Return the individual |drift| values for the chart (same sample logic as the gate)."""
    import numpy as np
    if trades.empty:
        return []
    buys = trades[(trades["side"] == "BUY") & trades["asset"].notna()
                  & trades["price"].notna() & trades["timestamp"].notna()]
    if buys.empty:
        return []
    sample = (buys.sample(n=min(cfg.drift_sample_trades, len(buys)), random_state=cfg.random_seed)
              if len(buys) > cfg.drift_sample_trades else buys)
    out: List[float] = []
    for _, t in sample.iterrows():
        try:
            hist = client.prices_history(str(t["asset"]), int(t["timestamp"]),
                                         int(t["timestamp"]) + 1800, fidelity=1)
        except Exception:
            continue
        pts = hist.get("history") if isinstance(hist, dict) else None
        if pts:
            out.append(abs(float(pts[-1]["p"]) - float(t["price"])))
    return out


def main() -> None:
    cfg = load_config()
    run(cfg)


if __name__ == "__main__":
    main()
