"""Stage 6 — single self-contained HTML report (charts as inline base64 PNG).

No external assets: everything (CSS + images) is embedded so the file is portable.
"""
from __future__ import annotations

import base64
import html
import io
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .config import Config
from .criteria import HARD_GATES

# metric -> human label + the gate it relates to (for the per-trader stats table)
METRIC_LABELS = [
    ("n_resolved", "Resolved positions"),
    ("account_age_days", "Account age (days)"),
    ("days_since_last_trade", "Days since last trade"),
    ("win_rate", "Win rate"),
    ("roi", "ROI"),
    ("profit_factor", "Profit factor"),
    ("favorite_entry_share", "Favorite-entry share (avgPrice>=fav)"),
    ("loss_share", "Loss share"),
    ("trades_per_day", "Trades / active day"),
    ("size_ratio", "Max/median position size"),
    ("late_entry_share", "Late-entry share"),
    ("median_hold_hours", "Median hold (hours)"),
    ("category_share", "Category share"),
    ("median_market_volume", "Median market volume ($)"),
    ("post_entry_drift", "Post-entry drift ($, 30m)"),
    ("monthly_consistency", "Monthly consistency"),
    ("recent_vs_lifetime", "Recent vs lifetime win rate"),
]


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=90, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _img_tag(b64: str, alt: str) -> str:
    return f'<img alt="{html.escape(alt)}" src="data:image/png;base64,{b64}"/>'


# ---- per-trader charts ------------------------------------------------------
def _chart_cum_pnl(positions: pd.DataFrame) -> Optional[str]:
    p = positions.dropna(subset=["timestamp"]).sort_values("timestamp")
    if p.empty:
        return None
    t = [datetime.fromtimestamp(x, timezone.utc) for x in p["timestamp"]]
    cum = p["realizedPnl"].fillna(0).cumsum()
    fig, ax = plt.subplots(figsize=(6, 2.6))
    ax.plot(t, cum, color="#1b7837")
    ax.axhline(0, color="#999", lw=0.6)
    ax.set_title("Cumulative realized PnL")
    ax.set_ylabel("$")
    fig.autofmt_xdate()
    return _png(fig)


def _chart_entry_hist(positions: pd.DataFrame, fav: float) -> Optional[str]:
    pr = positions["avgPrice"].dropna()
    if pr.empty:
        return None
    fig, ax = plt.subplots(figsize=(6, 2.6))
    ax.hist(pr, bins=np.linspace(0, 1, 21), color="#4393c3", edgecolor="white")
    ax.axvline(fav, color="#b2182b", ls="--", label=f"FAVORITE_PRICE={fav}")
    ax.set_title("Entry-price distribution")
    ax.set_xlabel("avgPrice")
    ax.legend(fontsize=7)
    return _png(fig)


def _chart_hold_dist(holds: List[float]) -> Optional[str]:
    h = [x for x in holds if x and x > 0]
    if not h:
        return None
    fig, ax = plt.subplots(figsize=(6, 2.6))
    ax.hist(h, bins=np.logspace(np.log10(min(h) + 1e-6), np.log10(max(h) + 1), 20),
            color="#8073ac", edgecolor="white")
    ax.set_xscale("log")
    ax.set_title("Hold-time distribution")
    ax.set_xlabel("hours (log)")
    return _png(fig)


def _chart_size_time(positions: pd.DataFrame) -> Optional[str]:
    p = positions.dropna(subset=["timestamp"]).sort_values("timestamp")
    if p.empty:
        return None
    t = [datetime.fromtimestamp(x, timezone.utc) for x in p["timestamp"]]
    fig, ax = plt.subplots(figsize=(6, 2.6))
    ax.scatter(t, p["totalBought"].fillna(0), s=10, color="#d6604d", alpha=0.6)
    ax.set_title("Position size over time")
    ax.set_ylabel("totalBought ($)")
    fig.autofmt_xdate()
    return _png(fig)


def _chart_monthly_wr(positions: pd.DataFrame) -> Optional[str]:
    p = positions.dropna(subset=["timestamp"]).copy()
    if p.empty:
        return None
    p["_m"] = p["timestamp"].apply(lambda x: datetime.fromtimestamp(x, timezone.utc).strftime("%Y-%m"))
    grp = p.groupby("_m")["realizedPnl"].apply(lambda s: (s.fillna(0) > 0).mean())
    fig, ax = plt.subplots(figsize=(6, 2.6))
    ax.bar(range(len(grp)), grp.values, color="#5aae61")
    ax.set_xticks(range(len(grp)))
    ax.set_xticklabels(grp.index, rotation=90, fontsize=6)
    ax.set_ylim(0, 1)
    ax.set_title("Monthly win rate")
    return _png(fig)


def _chart_drift(drift_samples: List[float]) -> Optional[str]:
    d = [x for x in (drift_samples or []) if x is not None]
    if not d:
        return None
    fig, ax = plt.subplots(figsize=(6, 2.6))
    ax.scatter(range(len(d)), d, s=20, color="#762a83")
    ax.axhline(float(np.median(d)), color="#1b7837", ls="--", label="median")
    ax.set_title("Post-entry drift sample (|Δprice| @30m)")
    ax.set_ylabel("$")
    ax.legend(fontsize=7)
    return _png(fig)


# ---- HTML assembly ----------------------------------------------------------
_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;color:#222;}
.page{padding:28px 40px;page-break-after:always;}
h1{font-size:24px;margin:0 0 4px;} h2{font-size:19px;border-bottom:2px solid #333;padding-bottom:4px;}
h3{font-size:14px;color:#555;margin:18px 0 6px;}
table{border-collapse:collapse;font-size:12px;width:100%;margin:8px 0;}
th,td{border:1px solid #ccc;padding:3px 6px;text-align:left;}
th{background:#f0f0f0;} .pass{color:#1b7837;font-weight:bold;} .fail{color:#b2182b;font-weight:bold;}
.charts img{width:48%;margin:1%;border:1px solid #eee;vertical-align:top;}
.kv{font-size:12px;} .kv td:first-child{font-weight:bold;width:34%;}
.muted{color:#777;font-size:12px;} a{color:#1a5fb4;}
.banner{background:#fff3cd;border:1px solid #ffe69c;padding:10px;border-radius:6px;margin:10px 0;}
"""


def _funnel_table(funnel: Dict[str, int]) -> str:
    rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in funnel.items())
    return f'<table class="kv">{rows}</table>'


def _config_table(cfg: Config) -> str:
    items = {
        "CATEGORY": cfg.category, "TIME_PERIODS": ",".join(cfg.time_periods),
        "SAMPLE_SIZE": cfg.sample_size, "RANDOM_SEED": cfg.random_seed, "TOP_N": cfg.top_n,
        "MIN_RESOLVED": cfg.min_resolved, "MIN_ACCOUNT_AGE_DAYS": cfg.min_account_age_days,
        "MAX_DAYS_SINCE_LAST_TRADE": cfg.max_days_since_last_trade,
        "WIN_RATE_MIN/MAX": f"{cfg.win_rate_min}/{cfg.win_rate_max}",
        "FAVORITE_PRICE": cfg.favorite_price, "MAX_FAVORITE_ENTRY_SHARE": cfg.max_favorite_entry_share,
        "MIN_LOSS_SHARE": cfg.min_loss_share, "MIN_ROI": cfg.min_roi,
        "MAX_TRADES_PER_DAY": cfg.max_trades_per_day, "MAX_SIZE_RATIO": cfg.max_size_ratio,
        "MAX_LATE_ENTRY_SHARE": cfg.max_late_entry_share, "MIN_MEDIAN_HOLD_HOURS": cfg.min_median_hold_hours,
        "MIN_MEDIAN_MARKET_VOLUME": cfg.min_median_market_volume,
        "MAX_POST_ENTRY_DRIFT": cfg.max_post_entry_drift, "MIN_CATEGORY_SHARE": cfg.min_category_share,
    }
    rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>" for k, v in items.items())
    return f'<table class="kv">{rows}</table>'


def _fmt(v) -> str:
    if isinstance(v, float):
        if v == float("inf"):
            return "∞"
        return f"{v:,.4f}" if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def _stats_table(metrics: Dict[str, float], gate_results: Dict[str, bool], cfg: Config) -> str:
    gate_by_metric = {
        "n_resolved": "MIN_RESOLVED", "account_age_days": "MIN_ACCOUNT_AGE_DAYS",
        "days_since_last_trade": "MAX_DAYS_SINCE_LAST_TRADE", "win_rate": "WIN_RATE_MIN",
        "roi": "MIN_ROI", "favorite_entry_share": "MAX_FAVORITE_ENTRY_SHARE",
        "loss_share": "MIN_LOSS_SHARE", "trades_per_day": "MAX_TRADES_PER_DAY",
        "size_ratio": "MAX_SIZE_RATIO", "late_entry_share": "MAX_LATE_ENTRY_SHARE",
        "median_hold_hours": "MIN_MEDIAN_HOLD_HOURS", "category_share": "MIN_CATEGORY_SHARE",
        "median_market_volume": "MIN_MEDIAN_MARKET_VOLUME", "post_entry_drift": "MAX_POST_ENTRY_DRIFT",
    }
    desc_by_gate = {g.name: g.desc(cfg) for g in HARD_GATES}
    rows = []
    for key, label in METRIC_LABELS:
        if key not in metrics:
            continue
        gate = gate_by_metric.get(key)
        thr = desc_by_gate.get(gate, "—") if gate else "(soft)"
        if gate is None:
            mark = '<span class="muted">soft</span>'
        else:
            ok = gate_results.get(gate, False)
            mark = '<span class="pass">✅</span>' if ok else '<span class="fail">❌</span>'
        rows.append(f"<tr><td>{html.escape(label)}</td><td>{_fmt(metrics[key])}</td>"
                    f"<td>{html.escape(thr)}</td><td>{mark}</td></tr>")
    return ("<table><tr><th>Metric</th><th>Value</th><th>Threshold</th><th>Pass</th></tr>"
            + "".join(rows) + "</table>")


def build_report(
    cfg: Config,
    funnel: Dict[str, int],
    selected: List[dict],
    candidates_df: pd.DataFrame,
    run_date: datetime,
) -> str:
    os.makedirs(cfg.output_dir, exist_ok=True)
    date_str = run_date.strftime("%Y%m%d")
    out_path = os.path.join(cfg.output_dir, f"report_{cfg.category}_{date_str}.html")

    parts: List[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>polymarket-scout — {cfg.category} — {date_str}</title>",
        f"<style>{_CSS}</style></head><body>",
    ]

    # ---- cover ----
    short = len(selected) < cfg.top_n
    banner = (f"<div class='banner'><b>Note:</b> only {len(selected)} trader(s) passed all "
              f"strict gates — fewer than TOP_N={cfg.top_n}. No criteria were relaxed.</div>"
              if short else "")
    parts.append(
        "<div class='page'>"
        f"<h1>polymarket-scout — {cfg.category}</h1>"
        f"<div class='muted'>Generated {run_date.strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{len(selected)} selected trader(s)</div>"
        f"{banner}"
        "<h2>Run configuration</h2>" + _config_table(cfg) +
        "<h2>Funnel</h2>" + _funnel_table(funnel) +
        "<p class='muted'>All endpoints are public read endpoints; no authentication used.</p>"
        "</div>"
    )

    # ---- per trader ----
    for s in selected:
        m = s["metrics"]
        wallet = s["wallet"]
        uname = s.get("username") or "(anon)"
        positions = s["positions"]
        holds = s.get("holds", [])
        drift_samples = s.get("drift_samples", [])
        charts = [
            _chart_cum_pnl(positions),
            _chart_entry_hist(positions, cfg.favorite_price),
            _chart_hold_dist(holds),
            _chart_size_time(positions),
            _chart_monthly_wr(positions),
            _chart_drift(drift_samples),
        ]
        chart_html = "".join(_img_tag(c, "chart") for c in charts if c)
        profile = f"https://polymarket.com/profile/{wallet}"
        parts.append(
            "<div class='page'>"
            f"<h2>#{s['rank']} · {html.escape(uname)}</h2>"
            "<table class='kv'>"
            f"<tr><td>Wallet</td><td><a href='{profile}'>{wallet}</a></td></tr>"
            f"<tr><td>Composite score</td><td>{s['composite']} (rank {s['rank']})</td></tr>"
            f"<tr><td>Account age</td><td>{m.get('account_age_days',0):.0f} days</td></tr>"
            f"<tr><td>Resolved positions</td><td>{int(m.get('n_resolved',0))}</td></tr>"
            "</table>"
            "<h3>Metrics &amp; gates</h3>" + _stats_table(m, s["gate_results"], cfg) +
            "<h3>Charts</h3><div class='charts'>" + chart_html + "</div>"
            "</div>"
        )

    # ---- appendix ----
    if not candidates_df.empty:
        appendix = candidates_df.head(500).to_html(index=False, border=0, na_rep="",
                                                    float_format=lambda x: f"{x:.3f}")
    else:
        appendix = "<p class='muted'>No candidates.</p>"
    parts.append(
        "<div class='page'><h2>Appendix — all sampled candidates</h2>"
        "<p class='muted'>Full data also written to candidates CSV. Showing up to 500 rows.</p>"
        + appendix + "</div>"
    )

    parts.append("</body></html>")
    with open(out_path, "w") as f:
        f.write("".join(parts))
    return out_path
