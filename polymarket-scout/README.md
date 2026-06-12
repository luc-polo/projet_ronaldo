# polymarket-scout

Automated screening of copy-worthy Polymarket traders. For **one market category per
run** (set in `.env`), it seeds a large pool of winning wallets from the public
leaderboard, draws a wide/diverse random sample, enriches each wallet from public read
APIs, applies ~20 strict screening criteria, and emits a single self-contained HTML
report with a one-page stats summary per selected trader.

No authentication — every endpoint used is a public read endpoint.

## Install & run

```bash
pip install -r requirements.txt
cp .env.example .env          # then edit CATEGORY etc.
python -m src.main
```

Re-run with another category = edit one line of `.env`. The on-disk JSON cache
(`.cache/`) makes overlapping re-runs nearly free.

## Pipeline

`main.py` orchestrates one module per stage:

0. **`seeding.py`** — page the leaderboard for `CATEGORY` across `TIME_PERIODS`, union +
   dedupe, keep winners (`pnl>0`), draw a **uniform** random sample (not rank-weighted).
1. **`enrichment.py`** — per wallet paginate `/closed-positions` and `/activity?type=TRADE`.
2. **`metrics.py`** — pure DataFrame→dict metric functions (win rate, ROI, hold time,
   late-entry share, category specialization, …).
3. **`criteria.py`** — modular `Gate` registry. **Strict**: no relaxation anywhere.
   Cheap gates run on every wallet.
4. **Copyability gates** (expensive) run only on cheap-gate survivors — two-stage funnel:
   median market volume (Gamma) and post-entry price drift (CLOB).
5. **`selection.py`** — rank survivors by a weighted composite z-score; take `TOP_N`.
6. **`report.py`** — one self-contained HTML (`output/report_<CATEGORY>_<DATE>.html`) with
   inline base64 PNG charts, a cover/funnel page, one page per trader, and an appendix.
   Also writes `output/seeds_*.csv` and `output/candidates_*.csv`.

A **step-0 smoke test** calls each endpoint once for a known wallet and fails loudly with
the raw response if any schema changed.

## API notes & deviations from the original plan

All endpoints were verified live against the [Polymarket docs](https://docs.polymarket.com)
before building. Two corrections were made where the plan disagreed with the live API:

1. **Gamma `/markets` defaults to open markets only.** Filtering by `condition_ids`
   returns `[]` for *resolved* markets unless `closed=true` is also passed. Since every
   position from `/closed-positions` is a resolved market, `get_markets_by_conditions`
   always sends `closed=true`. Without this fix the volume gate would silently see zero
   markets. (The `condition_ids` param name itself was correct.)
2. **`/activity` has an offset ceiling (~3000); `/closed-positions` does not.** The plan's
   `MAX_ACTIVITY_PER_WALLET=5000` exceeds it, and the API returns HTTP 400 past the cap.
   Pagination now treats a 400 as a graceful "end of data" (`OffsetExhausted`) instead of
   crashing. `/closed-positions` was verified to accept `offset` up to 100000.

`/activity` returns both `size` (share count) and `usdcSize` (USD); the client keeps both.
Leaderboard `rank` comes back as a string and `pnl`/`vol` as numbers — handled.

## Known limitations (stated, not silently worked around)

- **PnL accounting** is per outcome-side position as returned by `/closed-positions`.
  NegRisk multi-leg consolidation and SPLIT/MERGE/CONVERSION flows are **not** consolidated
  in v1 — possible small win-rate distortion for traders who use them heavily.
- **Account age** is derived from the oldest visible trade in `/activity`. Because that
  endpoint caps at ~3000 most-recent trades (see above), the apparent first-trade date is
  truncated for extremely prolific wallets, which can *underestimate* account age. In
  practice the `MAX_TRADES_PER_DAY` gate removes such bot-like wallets, and 3000 trades at
  ≤25/day still spans well beyond the 120-day age floor for normal traders.
- **Seeding** pages the leaderboard until an empty/short page or `LEADERBOARD_MAX_OFFSET`
  (default 3000 ⇒ ≤ ~3,050 per window), whichever comes first. This is "the visible
  leaderboard," not every winning wallet.
- **Drift metric** uses `DRIFT_SAMPLE_TRADES` (default 15) sampled entries — an estimate.
- **Category match** uses a keyword map over `slug`/`eventSlug`/`title` (the plan's
  documented fallback) rather than Gamma tags, so the cheap Stage-3 gate needs no extra
  network. Edge-case markets may be mis-tagged.
- **No funding-source clustering** (Polygonscan) in v1 — future work.

## Rate limits

Per the [docs](https://docs.polymarket.com/api-reference/rate-limits) (sliding 10s windows,
throttled rather than 429-rejected): data-api general 1000/10s, `/closed-positions`
150/10s (the binding floor = 15/s), CLOB `/prices-history` 1000/10s. A single global token
bucket at `REQUEST_RATE=10` req/s sits safely under all of them; `tenacity` exponential
backoff covers transient 429/5xx.

## Layout

```
polymarket-scout/
  .env  .env.example  requirements.txt  README.md
  src/  config.py api_client.py seeding.py enrichment.py
        metrics.py criteria.py selection.py report.py main.py
```
