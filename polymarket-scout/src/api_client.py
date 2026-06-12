"""Thin client over the public Polymarket read APIs.

Features:
  * global token-bucket rate limit (REQUEST_RATE req/s) shared across all endpoints,
  * retries with exponential backoff on 429 / 5xx (tenacity),
  * transparent on-disk JSON cache keyed by the full URL, so re-runs are ~free.

All endpoints used are public; no authentication.

NOTE (deviation from plan, verified live 2026-06): the Gamma /markets endpoint
defaults to OPEN markets only. To retrieve metadata for *resolved* markets (which is
what /closed-positions gives us) you must pass `closed=true`. `get_markets_by_conditions`
handles this. See README "API notes".
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Config


class RetryableHTTP(Exception):
    """Raised on 429/5xx so tenacity retries; carries the status for logging."""

    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status


class OffsetExhausted(Exception):
    """Raised on HTTP 400 from a paginated endpoint — the API's offset ceiling was hit
    (e.g. /activity rejects offset beyond ~3000). Callers treat it as 'end of data'."""

    def __init__(self, url: str):
        super().__init__(f"offset ceiling reached: {url}")


class _TokenBucket:
    """Simple thread-safe token bucket: `rate` tokens/sec, burst capacity `rate`."""

    def __init__(self, rate: float):
        self.rate = max(0.1, rate)
        self.capacity = max(1.0, rate)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                sleep_for = (1.0 - self.tokens) / self.rate
                time.sleep(sleep_for)


class ApiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bucket = _TokenBucket(cfg.request_rate)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "polymarket-scout/1.0 (+research)"})
        os.makedirs(cfg.cache_dir, exist_ok=True)
        self.cache_hits = 0
        self.network_calls = 0

    # ---- cache helpers ------------------------------------------------------
    def _cache_path(self, url: str) -> str:
        h = hashlib.sha256(url.encode()).hexdigest()[:32]
        return os.path.join(self.cfg.cache_dir, f"{h}.json")

    def _full_url(self, base: str, path: str, params: Optional[Dict[str, Any]]) -> str:
        url = f"{base}{path}"
        if params:
            # doseq=True so repeated keys (e.g. condition_ids) serialize correctly
            url = f"{url}?{urlencode(params, doseq=True)}"
        return url

    # ---- core request -------------------------------------------------------
    def get(
        self,
        base: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
    ) -> Any:
        url = self._full_url(base, path, params)
        cache_path = self._cache_path(url)
        if use_cache and os.path.exists(cache_path):
            self.cache_hits += 1
            with open(cache_path, "r") as f:
                return json.load(f)

        data = self._get_network(url)

        if use_cache:
            tmp = cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, cache_path)
        return data

    @retry(
        retry=retry_if_exception_type((RetryableHTTP, requests.exceptions.RequestException)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get_network(self, url: str) -> Any:
        self.bucket.acquire()
        self.network_calls += 1
        resp = self.session.get(url, timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableHTTP(resp.status_code, url, resp.text)
        if resp.status_code == 400:
            # paginated endpoints (notably /activity) 400 once offset exceeds their cap;
            # this is a normal "stop" signal, not a retryable failure.
            raise OffsetExhausted(url)
        resp.raise_for_status()
        return resp.json()

    # ---- typed endpoint wrappers -------------------------------------------
    def leaderboard(self, category: str, time_period: str, limit: int, offset: int) -> List[dict]:
        return self.get(
            self.cfg.data_api,
            "/v1/leaderboard",
            {"category": category, "timePeriod": time_period, "orderBy": "PNL",
             "limit": limit, "offset": offset},
        )

    def closed_positions(self, user: str, limit: int, offset: int) -> List[dict]:
        return self.get(
            self.cfg.data_api,
            "/closed-positions",
            {"user": user, "limit": limit, "offset": offset, "sortBy": "TIMESTAMP"},
        )

    def activity(self, user: str, limit: int, offset: int) -> List[dict]:
        return self.get(
            self.cfg.data_api,
            "/activity",
            {"user": user, "type": "TRADE", "limit": limit, "offset": offset},
        )

    def traded(self, user: str) -> dict:
        return self.get(self.cfg.data_api, "/traded", {"user": user})

    def get_markets_by_conditions(self, condition_ids: List[str]) -> List[dict]:
        """Batch market metadata for resolved markets. Must pass closed=true."""
        if not condition_ids:
            return []
        return self.get(
            self.cfg.gamma_api,
            "/markets",
            {"condition_ids": condition_ids, "closed": "true", "limit": len(condition_ids)},
        )

    def prices_history(self, token_id: str, start_ts: int, end_ts: int, fidelity: int = 1) -> dict:
        return self.get(
            self.cfg.clob_api,
            "/prices-history",
            {"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity},
        )


# ---- step-0 smoke test ------------------------------------------------------
KNOWN_WALLET = "0xd218e474776403a330142299f7796e8ba32eb5c9"  # a public leaderboard wallet


def smoke_test(client: ApiClient) -> None:
    """Call each endpoint once and assert expected fields exist. Fail loudly."""
    cfg = client.cfg
    failures: List[str] = []

    def check(name: str, ok: bool, sample: Any):
        if not ok:
            failures.append(f"[{name}] schema check FAILED. Raw sample: {json.dumps(sample)[:500]}")

    lb = client.leaderboard(cfg.category, "MONTH", limit=3, offset=0)
    check("leaderboard", isinstance(lb, list) and len(lb) > 0
          and all(k in lb[0] for k in ("proxyWallet", "userName", "pnl", "vol", "rank")), lb)

    cp = client.closed_positions(KNOWN_WALLET, limit=2, offset=0)
    check("closed-positions", isinstance(cp, list) and len(cp) > 0
          and all(k in cp[0] for k in ("avgPrice", "totalBought", "realizedPnl", "timestamp",
                                       "conditionId", "endDate", "outcome")), cp)

    act = client.activity(KNOWN_WALLET, limit=2, offset=0)
    check("activity", isinstance(act, list) and len(act) > 0
          and all(k in act[0] for k in ("timestamp", "side", "size", "price", "conditionId", "asset")), act)

    tr = client.traded(KNOWN_WALLET)
    check("traded", isinstance(tr, dict) and "traded" in tr, tr)

    cid = cp[0]["conditionId"] if cp else None
    if cid:
        mk = client.get_markets_by_conditions([cid])
        check("gamma markets", isinstance(mk, list) and len(mk) > 0
              and "volume" in mk[0] and "conditionId" in mk[0], mk)

    asset = cp[0]["asset"] if cp and "asset" in cp[0] else None
    if asset:
        ts = cp[0]["timestamp"]
        ph = client.prices_history(asset, ts - 3600, ts + 3600, fidelity=1)
        check("prices-history", isinstance(ph, dict) and "history" in ph, ph)

    if failures:
        raise RuntimeError(
            "STEP-0 SMOKE TEST FAILED — an upstream schema changed:\n  " + "\n  ".join(failures)
        )
