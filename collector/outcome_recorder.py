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

from db.connection import get_db, write_lock

load_dotenv()

log = logging.getLogger(__name__)

GAMMA_API = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com")
POLL_INTERVAL = 30  # seconds


class OutcomeRecorder:
    def __init__(self):
        self._running = False

    async def _fetch_resolution(
        self, client: httpx.AsyncClient, condition_id: str
    ) -> dict | None:
        try:
            resp = await client.get(
                f"{GAMMA_API}/markets/{condition_id}",
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning("Outcome fetch failed for %s: %s", condition_id[:12], e)
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
        for name, price in zip(raw_outcomes, raw_prices):
            try:
                if float(price) == 1.0:
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
                SELECT condition_id FROM pm_markets
                WHERE resolved_at IS NULL
                  AND close_time < ?
                  AND close_time IS NOT NULL
                ORDER BY close_time
                LIMIT 50
                """,
                [now],
            ).fetchall()
        except Exception as e:
            log.error("DB error fetching pending markets: %s", e)
            return 0

        resolved = 0
        for (cid,) in pending:
            data = await self._fetch_resolution(client, cid)
            if not data:
                continue

            outcome, winning_price = self._parse_outcome(data)
            if not outcome:
                continue

            resolved_at = now
            with write_lock:
                try:
                    db.execute(
                        """
                        UPDATE pm_markets
                        SET outcome = ?, winning_price = ?,
                            resolved_at = ?, last_updated = ?
                        WHERE condition_id = ?
                        """,
                        [outcome, winning_price, resolved_at, now, cid],
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
