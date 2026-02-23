"""Outcome recorder — polls Gamma API for resolved markets.

Runs every 30s, finds pm_markets where close_time has passed
but resolved_at is NULL, fetches resolution from Gamma API,
and updates outcome + winning_price.

Resolution is derived from outcomePrices (settles to "0"/"1" integers
when resolved) since the outcome text field is often not populated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from collector.pm_market_discovery import _ASSET_SLUG_PREFIX, _TF_SECONDS
from db.connection import get_db, write_lock

load_dotenv()

log = logging.getLogger(__name__)

GAMMA_API = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com")
POLL_INTERVAL = 30  # seconds


def _build_slug(asset: str, timeframe: str, close_time: datetime) -> str | None:
    """Derive the Polymarket slug from close_time + asset + timeframe.

    Slug format: {prefix}-{timeframe}-{window_start_unix}
    window_start = close_time - timeframe_seconds
    """
    prefix = _ASSET_SLUG_PREFIX.get(asset.upper())
    tf_secs = _TF_SECONDS.get(timeframe)
    if not prefix or not tf_secs:
        return None
    window_start = int(close_time.timestamp()) - tf_secs
    return f"{prefix}-{timeframe}-{window_start}"


class OutcomeRecorder:
    def __init__(self):
        self._running = False

    async def _fetch_resolution(
        self, client: httpx.AsyncClient, slug: str, cid: str
    ) -> dict | None:
        """Fetch market by slug (the only reliable Gamma API lookup)."""
        try:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug, "limit": 1},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            if not markets:
                return None
            market = markets[0]
            # Verify the slug actually matched our market (sanity check)
            api_cid = market.get("conditionId") or market.get("condition_id") or ""
            if api_cid.lower() != cid.lower():
                log.debug(
                    "Slug %s returned unexpected CID %s (expected %s) — skipping",
                    slug, api_cid[:12], cid[:12],
                )
                return None
            return market
        except Exception as e:
            log.warning("Outcome fetch failed for slug %s: %s", slug, e)
            return None

    def _parse_outcome(self, data: dict) -> tuple[str | None, float | None]:
        """
        Returns (outcome, winning_price).

        Primary: decode from outcomePrices — the winning token's price settles
        to exactly "1" and the loser's to "0". The 'outcome' text field is
        often not populated for Up/Down markets.
        """
        outcome: str | None = None

        # Strategy 1: outcomePrices ("1" = winner, "0" = loser)
        raw_outcomes = data.get("outcomes", "[]")
        raw_prices = data.get("outcomePrices", "[]")
        if isinstance(raw_outcomes, str):
            try:
                raw_outcomes = json.loads(raw_outcomes)
            except Exception:
                raw_outcomes = []
        if isinstance(raw_prices, str):
            try:
                raw_prices = json.loads(raw_prices)
            except Exception:
                raw_prices = []

        # Gamma API sometimes uses 0.999... or similar before absolute finality
        # but for Up/Down markets it should be 1.0. We'll use 0.99 threshold.
        for name, price in zip(raw_outcomes, raw_prices):
            try:
                p = float(price)
                if p >= 0.99:
                    outcome = name.upper()
                    break
            except (TypeError, ValueError):
                pass

        # Strategy 2: fallback to explicit outcome field
        if not outcome:
            raw = data.get("outcome")
            if raw:
                candidate = str(raw).upper()
                if candidate in ("UP", "DOWN", "YES", "NO"):
                    outcome = candidate

        # Winning price: Chainlink settlement price at resolution
        winning_price = (
            data.get("winningPrice")
            or data.get("winning_price")
            or data.get("resolutionPrice")
        )
        if winning_price is not None:
            try:
                winning_price = float(winning_price)
            except (TypeError, ValueError):
                winning_price = None

        return outcome, winning_price

    async def _poll_pending(self, client: httpx.AsyncClient) -> int:
        db = get_db()
        now = datetime.now(timezone.utc)

        try:
            pending = db.execute(
                """
                SELECT condition_id, asset, timeframe, close_time FROM pm_markets
                WHERE resolved_at IS NULL
                  AND close_time < ?
                  AND close_time IS NOT NULL
                  AND asset IS NOT NULL
                  AND timeframe IS NOT NULL
                ORDER BY close_time DESC
                LIMIT 50
                """,
                [now],
            ).fetchall()
        except Exception as e:
            log.error("DB error fetching pending markets: %s", e)
            return 0

        resolved = 0
        for (cid, asset, timeframe, close_time) in pending:
            slug = _build_slug(asset, timeframe, close_time)
            if not slug:
                log.debug("Can't build slug for %s (%s/%s) — skipping", cid[:12], asset, timeframe)
                continue

            data = await self._fetch_resolution(client, slug, cid)
            if not data:
                continue

            outcome, winning_price = self._parse_outcome(data)
            if not outcome:
                continue

            with write_lock:
                try:
                    db.execute(
                        """
                        UPDATE pm_markets
                        SET outcome = ?, winning_price = ?,
                            resolved_at = ?, last_updated = ?
                        WHERE condition_id = ?
                        """,
                        [outcome, winning_price, now, now, cid],
                    )
                    resolved += 1
                    log.info(
                        "Resolved: %s → %s @ %.2f",
                        cid[:12], outcome, winning_price or 0,
                    )
                except Exception as e:
                    log.error("Failed to update outcome for %s: %s", cid[:12], e)

        return resolved

    async def run(self) -> None:
        self._running = True
        async with httpx.AsyncClient() as client:
            while self._running:
                try:
                    resolved = await self._poll_pending(client)
                    if resolved:
                        log.info("Outcome recorder: resolved %d markets", resolved)
                except Exception as e:
                    log.error("Outcome recorder error: %s", e)
                await asyncio.sleep(POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
