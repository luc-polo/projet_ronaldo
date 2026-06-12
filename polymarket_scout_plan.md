# PLAN — `polymarket-scout`: automated screening of copy-worthy Polymarket traders

Goal: a small, runnable Python CLI that, for ONE market category per run (set in `.env`),
seeds a large pool of winning wallets from the Polymarket leaderboard, randomly samples a
wide and diverse subset (not just top ranks), enriches each wallet from public APIs,
applies ~20 screening criteria (strict — return fewer than 12 if fewer pass), and outputs
a single HTML report with a one-page stats summary per selected trader.

No authentication is needed: all endpoints used are public read endpoints.

---

## 1. APIs used (verify each responds as described before building on it)

| Base | Endpoint | Use |
|---|---|---|
| `https://data-api.polymarket.com` | `GET /v1/leaderboard?category=&timePeriod=&orderBy=PNL&limit=50&offset=` | Seeding. `category` ∈ {OVERALL, POLITICS, SPORTS, CRYPTO, CULTURE, MENTIONS, WEATHER, ECONOMICS, TECH, FINANCE}; `timePeriod` ∈ {DAY, WEEK, MONTH, ALL}; `limit` ≤ 50; `offset` cap unverified (assumed ~1000–3000; loop stops on empty page). Returns `proxyWallet, userName, pnl, vol, rank`. |
| same | `GET /closed-positions?user=&limit=50&offset=&sortBy=TIMESTAMP` | Per resolved market position: `avgPrice, totalBought, realizedPnl, timestamp, title, slug, eventSlug, outcome, endDate, conditionId`. `offset` ≤ 100000. Core of all performance metrics. |
| same | `GET /activity?user=&type=TRADE&limit=&offset=` | Every trade: `timestamp, side (BUY/SELL), size, price, conditionId, asset`. Account age, hold time, trade frequency, sizing. |
| same | `GET /traded?user=` | Total markets traded (sanity check). |
| `https://gamma-api.polymarket.com` | `GET /markets?condition_ids=...` (batch) | Market metadata: `volume`, `liquidity`, `endDate`, tags. Liquidity filter. |
| `https://clob.polymarket.com` | `GET /prices-history?market=<token_id>&startTs=&endTs=&fidelity=1` | Minute-level price series for one outcome token. Post-entry drift (market speed). |

Build a thin `api_client.py` around `requests` with: token-bucket rate limit
(`REQUEST_RATE` req/s, default 5), retries with exponential backoff on 429/5xx
(`tenacity`), and a transparent on-disk JSON cache keyed by URL (`CACHE_DIR`) so
re-runs are nearly free. **Smoke-test step 0:** call each endpoint once for one known
wallet, assert expected fields exist; fail loudly with the raw response if the schema
changed.

---

## 2. Configuration — everything in `.env` (python-dotenv)

```ini
# --- run scope ---
CATEGORY=POLITICS              # one category per run (enum above)
TIME_PERIODS=WEEK,MONTH,ALL    # leaderboard windows to union for seeding
SAMPLE_SIZE=400                # wallets to enrich; 0 = ALL seeded wallets
RANDOM_SEED=42                 # reproducible random sampling
TOP_N=12

# --- hard criteria (strict gates) ---
MIN_RESOLVED=90                # min resolved positions
MIN_ACCOUNT_AGE_DAYS=120       # >= 4 months
MAX_DAYS_SINCE_LAST_TRADE=14   # must still be active
WIN_RATE_MIN=0.52
WIN_RATE_MAX=0.80              # above this = farming/insider suspicion
FAVORITE_PRICE=0.88
MAX_FAVORITE_ENTRY_SHARE=0.30  # share of entries with avgPrice >= FAVORITE_PRICE
MIN_LOSS_SHARE=0.05            # must have visible losses
MIN_ROI=0.05                   # sum(realizedPnl)/sum(totalBought)
MAX_TRADES_PER_DAY=25          # bot / market-maker filter
MAX_SIZE_RATIO=15              # max position cost / median position cost
MAX_LATE_ENTRY_SHARE=0.20      # entries within 60 min of market endDate (insider pattern)
MIN_MEDIAN_HOLD_HOURS=4        # copyability: not too fast
MIN_MEDIAN_MARKET_VOLUME=50000 # copyability: liquid markets only (USD)
MAX_POST_ENTRY_DRIFT=0.05      # copyability: median |price move| ($) 30 min after entry
MIN_CATEGORY_SHARE=0.40        # specialization in the run's category

# --- cost controls ---
MAX_POSITIONS_PER_WALLET=1000  # pagination cap on /closed-positions
MAX_ACTIVITY_PER_WALLET=5000   # pagination cap on /activity
DRIFT_SAMPLE_TRADES=15         # random entries per wallet for prices-history check
REQUEST_RATE=10               # global req/s; safe under the 15/s /closed-positions floor
LEADERBOARD_MAX_OFFSET=3000    # page until empty page OR this offset, whichever first
CACHE_DIR=.cache
OUTPUT_DIR=output
```

---

## 3. Pipeline (main.py orchestrates; one module per stage)

### Stage 0 — `seeding.py`
1. For each `timePeriod` in `TIME_PERIODS`, page the leaderboard for `CATEGORY`
   with `orderBy=PNL`, `limit=50`, `offset = 0,50,…,LEADERBOARD_MAX_OFFSET` → up to
   ~3,050 rows per window. Stop a window early on the first empty/short page (so the
   real API cap, wherever it is, ends the loop — the offset ceiling is just a safety bound).
2. Union all rows, dedupe by `proxyWallet`, keep `userName` and max seen `pnl`.
3. Keep only winners: `pnl > 0`.
4. **Sampling for diversity:** if `SAMPLE_SIZE > 0` and pool > SAMPLE_SIZE, draw a
   uniform random sample WITHOUT replacement using `random.Random(RANDOM_SEED)` —
   explicitly NOT rank-weighted, so mid/low-rank winners are covered too.
   If `SAMPLE_SIZE=0`, take the whole pool (warn about runtime: ~5–20 calls/wallet).
5. Persist the seed list to `output/seeds_<CATEGORY>.csv` (wallet, username, pnl, vol,
   windows seen).

### Stage 1 — `enrichment.py` (cheap data, every sampled wallet)
For each wallet: paginate `/closed-positions` (up to cap) and `/activity?type=TRADE`
(up to cap). Store raw per-wallet JSON in cache. Build two DataFrames per wallet:
`positions` and `trades`.

### Stage 2 — `metrics.py` (pure functions: DataFrames → metric dict)
Compute per wallet (each formula stated so the agent implements exactly):

- `n_resolved` = len(positions)
- `account_age_days` = (now − min(trades.timestamp)) / 86400
- `days_since_last_trade` = (now − max(trades.timestamp)) / 86400
- `win_rate` = share of positions with realizedPnl > 0
- `roi` = Σ realizedPnl / Σ totalBought
- `favorite_entry_share` = share of positions with avgPrice ≥ FAVORITE_PRICE
- `loss_share` = share of positions with realizedPnl < 0
- `trades_per_day` = len(trades) / max(1, n_distinct_active_days)
- `size_ratio` = max(totalBought) / median(totalBought)
- `late_entry_share`: join positions to first BUY in trades per conditionId; share where
  (market endDate − first buy ts) ≤ 60 min. Missing endDate → exclude from denominator.
- `median_hold_hours`: per conditionId, (last SELL-or-resolution ts − first BUY ts);
  for held-to-resolution positions use position.timestamp as exit. Median across positions.
- `category_share` = share of positions whose eventSlug/tags match the run CATEGORY
  (map via Gamma tags for the wallet's conditionIds; fallback: keyword map on slug).
- `profit_factor` = Σ gains / |Σ losses|  (soft, for ranking)
- `monthly_consistency` = share of calendar months with Σ realizedPnl > 0 (soft)
- `recent_vs_lifetime` = win_rate(last 30 resolved) − lifetime win_rate (soft; decay signal)

### Stage 3 — `criteria.py` (modular gate registry)
Implement each hard criterion as a small object `{name, fn(metrics)→bool, env_threshold}`
held in a list `HARD_GATES` — adding/removing a criterion = one line. Apply all gates;
record per-wallet pass/fail per gate (kept for the report and for `candidates.csv`).
**Strict:** no relaxation anywhere.

Cheap gates (from Stage 2 metrics): MIN_RESOLVED, MIN_ACCOUNT_AGE_DAYS,
MAX_DAYS_SINCE_LAST_TRADE, WIN_RATE_MIN/MAX, MAX_FAVORITE_ENTRY_SHARE, MIN_LOSS_SHARE,
MIN_ROI, MAX_TRADES_PER_DAY, MAX_SIZE_RATIO, MAX_LATE_ENTRY_SHARE,
MIN_MEDIAN_HOLD_HOURS, MIN_CATEGORY_SHARE.

### Stage 4 — expensive copyability gates (survivors of Stage 3 only — two-stage funnel)
- `median_market_volume`: batch Gamma `/markets?condition_ids=` for the wallet's
  positions; gate on `MIN_MEDIAN_MARKET_VOLUME`.
- `post_entry_drift`: sample `DRIFT_SAMPLE_TRADES` random BUY trades; for each, fetch
  `/prices-history` for that token over [entry, entry+30 min], fidelity 1; drift =
  |price(t+30m) − entry price|. Gate: median drift ≤ MAX_POST_ENTRY_DRIFT.
  (Big drift ⇒ market moves too fast for the copy bot to get a comparable fill.)

### Stage 5 — `selection.py`
Rank survivors by composite score (weighted z-scores across survivors):
`0.25·roi + 0.20·profit_factor + 0.20·monthly_consistency + 0.20·copyability + 0.15·category_share`,
where `copyability = 0.25·z(median_hold_hours) + 0.25·z(median_market_volume) − 0.5·z(post_entry_drift)`
(post-entry drift weighted half; the other two split the remaining half).
Penalize negative `recent_vs_lifetime` below −0.10 (decay) by −0.5 score.
Take min(TOP_N, n_survivors). If < TOP_N, proceed and state it prominently in the report.

### Stage 6 — `report.py` (single self-contained HTML)
`output/report_<CATEGORY>_<YYYYMMDD>.html`, charts as inline base64 PNG (matplotlib) —
one file, no external assets. Structure:
- **Cover page:** run config dump, funnel counts (seeded → sampled → enriched → passed
  each gate → final N), date, category.
- **One page (section with page-break CSS) per selected trader:**
  - Identity header: `userName`, full `proxyWallet`, link `https://polymarket.com/profile/<proxyWallet>`, account age, composite score + rank.
  - Stats table: every metric of Stage 2/4 with its threshold and ✅.
  - Charts (≥5): cumulative realized PnL over time; entry-price histogram (0–1, with
    FAVORITE_PRICE line); hold-time distribution (log x); position-size over time;
    monthly win rate bars; post-entry drift sample dots.
- **Appendix:** full `candidates.csv` summary (all sampled wallets, all metrics,
  gate pass/fail matrix) also written separately to `output/candidates_<CATEGORY>.csv`.

---

## 4. Project layout & run

```
polymarket-scout/
  .env  .env.example  requirements.txt  README.md
  src/ config.py api_client.py seeding.py enrichment.py
      metrics.py criteria.py selection.py report.py main.py
```
`requirements.txt`: requests, python-dotenv, pandas, numpy, matplotlib, tenacity, tqdm.
Run: `pip install -r requirements.txt && python -m src.main`.
Re-run with another category = edit one line of `.env` (cache makes overlap cheap).

## 5. Known limitations to state in README (do not silently work around)
- PnL accounting is per outcome-side position as returned by `/closed-positions`;
  NegRisk multi-leg consolidation and SPLIT/MERGE/CONVERSION flows are NOT consolidated
  in v1 — note possible small win-rate distortion for traders using them heavily.
- Seeding pages the leaderboard until an empty page or `LEADERBOARD_MAX_OFFSET` (default
  3000 ⇒ ≤ ~3,050 per window). The API's true offset cap is unverified; the loop stops at
  whichever comes first. This is "the visible leaderboard," not every winning wallet.
- Drift metric uses 15 sampled trades — an estimate, not exhaustive.
- No funding-source clustering (Polygonscan) in v1; flag as future work.
- Rate limits (per [docs](https://docs.polymarket.com/api-reference/rate-limits), sliding 10s
  windows, **throttled not 429-rejected**): data-api general 1000/10s, `/closed-positions`
  150/10s (the binding floor = 15/s), clob `/prices-history` 1000/10s. A single global token
  bucket at `REQUEST_RATE=10` sits safely under all of them; `tenacity` backoff is mainly for 5xx.
