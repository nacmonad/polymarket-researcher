"""Polymarket Gamma API market discovery.

Two complementary fetch strategies run every poll:

1. Paginated /events fetch (newest-created first) — catches tomorrow's
   pre-created batch which Polymarket creates ~24h in advance.

2. Slug-based direct fetch — computes the slug for each current and
   upcoming 5-minute window and queries /markets?slug=X directly.
   This reliably finds TODAY's active windows regardless of pagination.

Market format (as of 2026):
  Question: "Bitcoin Up or Down - February 24, 12:00PM-12:05PM ET"
  Slug:     "btc-updown-5m-1771952400"  (timestamp = window START in UTC unix secs)
  Outcomes: ["Up", "Down"]  (stored as token_yes_id=Up, token_no_id=Down)
  Tokens:   clobTokenIds[0]=Up, clobTokenIds[1]=Down

Architecture note: uses asyncio.Queue to publish new condition_ids to
the CLOB consumer without tight coupling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from db.connection import get_db, write_lock

load_dotenv()

log = logging.getLogger(__name__)

GAMMA_API = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com")
COLLECT_ASSETS = [a.strip().upper() for a in os.getenv("COLLECT_ASSETS", "BTC").split(",")]
COLLECT_TIMEFRAMES = [t.strip().lower() for t in os.getenv("COLLECT_TIMEFRAMES", "5m,15m").split(",")]
POLL_INTERVAL = 60  # seconds

# Maps asset name → slug prefix used in Polymarket Up/Down market slugs
_ASSET_SLUG_PREFIX: dict[str, str] = {
    "BTC": "btc-updown",
    "ETH": "eth-updown",
    "SOL": "sol-updown",
    "XRP": "xrp-updown",
}

# Maps asset name → keyword that appears in question text
_ASSET_QUESTION_KEYWORD: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "xrp",
}

# Timeframe → seconds per window
_TF_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# Timeframe labels extracted from slug segment (e.g. "5m", "15m", "1h", "4h", "1d")
_SLUG_TIMEFRAME_RE = re.compile(r"-updown-(\d+[mhd])-")


def _slug_timeframe(slug: str) -> str | None:
    """Extract timeframe label from slug, e.g. 'btc-updown-5m-...' → '5m'."""
    m = _SLUG_TIMEFRAME_RE.search(slug)
    return m.group(1) if m else None


def compute_window_slugs(
    asset: str,
    timeframe: str,
    now: datetime | None = None,
    lookahead: int = 3,
    lookbehind: int = 1,
) -> list[str]:
    """
    Compute expected Polymarket slugs for current and nearby windows.

    The slug timestamp = Unix seconds of the window START time.
    Windows are aligned to the timeframe boundary (e.g. every 5 min at :00, :05, :10…).

    lookahead: number of future windows to include (default 3 = 15 min ahead for 5m)
    lookbehind: number of past windows to include (default 1 = catch in-progress window)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    tf_secs = _TF_SECONDS.get(timeframe, 300)
    ts = int(now.timestamp())
    # Align to window boundary
    window_start = ts - (ts % tf_secs)
    prefix = _ASSET_SLUG_PREFIX.get(asset, f"{asset.lower()}-updown")
    slugs = []
    for i in range(-lookbehind, lookahead + 1):
        slugs.append(f"{prefix}-{timeframe}-{window_start + i * tf_secs}")
    return slugs


def _parse_close_time(market: dict) -> datetime | None:
    for field in ("endDate", "endDateIso", "end_date_iso", "closingTime", "closeTime"):
        raw = market.get(field)
        if raw:
            try:
                s = str(raw)
                if "T" not in s:
                    s += "T00:00:00Z"
                return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                pass
    return None


def _extract_tokens(market: dict) -> tuple[str | None, str | None]:
    """Return (up_token_id, down_token_id) from clobTokenIds.

    clobTokenIds is a JSON-encoded string in the Gamma API response,
    e.g. '["123...", "456..."]' — must be parsed before indexing.
    """
    raw = market.get("clobTokenIds")
    if not raw:
        return None, None
    if isinstance(raw, str):
        try:
            clob_ids = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None, None
    else:
        clob_ids = raw  # already a list (CLOB API returns native lists)
    up_id = clob_ids[0] if len(clob_ids) > 0 else None
    down_id = clob_ids[1] if len(clob_ids) > 1 else None
    return up_id, down_id


class MarketDiscovery:
    def __init__(self, new_market_queue: asyncio.Queue | None = None):
        self._queue = new_market_queue
        self._known: set[str] = set()
        self._running = False

    async def _fetch_events_batch(self, client: httpx.AsyncClient) -> list[dict]:
        """
        Paginated /events fetch (newest-created first).
        Catches tomorrow's pre-created batch which Polymarket creates ~24h in advance.
        """
        try:
            resp = await client.get(
                f"{GAMMA_API}/events",
                params={
                    "closed": "false",
                    "limit": 200,
                    "order": "startDate",
                    "ascending": "false",
                },
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            log.warning("Gamma /events error: %s", e)
            return []

        results: list[dict] = []
        seen: set[str] = set()
        for event in events:
            for m in event.get("markets", []):
                if m.get("closed"):
                    continue
                slug = (m.get("slug") or "").lower()
                question = (m.get("question") or "").lower()

                for asset in COLLECT_ASSETS:
                    prefix = _ASSET_SLUG_PREFIX.get(asset, "").lower()
                    kw = _ASSET_QUESTION_KEYWORD.get(asset, asset.lower())
                    if not (slug.startswith(prefix) or (kw in question and "up or down" in question)):
                        continue
                    timeframe = _slug_timeframe(slug)
                    if COLLECT_TIMEFRAMES and timeframe not in COLLECT_TIMEFRAMES:
                        continue
                    cid = m.get("conditionId") or ""
                    if cid in seen:
                        continue
                    seen.add(cid)
                    m["_asset"] = asset
                    m["_timeframe"] = timeframe
                    results.append(m)
                    break

        return results

    async def _fetch_current_windows(self, client: httpx.AsyncClient) -> list[dict]:
        """
        Slug-based direct fetch for currently-active windows.

        Polymarket pre-creates tomorrow's markets, so the paginated /events
        response is dominated by tomorrow's batch. Today's windows (created
        yesterday) are buried far in pagination. Instead, compute the expected
        slug from the current timestamp and query /markets?slug=X directly.

        Fetches: current window + 3 upcoming + 1 previous (to catch in-progress)
        for each (asset, timeframe) combination.
        """
        now = datetime.now(timezone.utc)
        results: list[dict] = []
        seen: set[str] = set()

        slugs_to_fetch: list[tuple[str, str, str]] = []  # (slug, asset, timeframe)
        for asset in COLLECT_ASSETS:
            for tf in COLLECT_TIMEFRAMES:
                for slug in compute_window_slugs(asset, tf, now=now, lookahead=3, lookbehind=1):
                    slugs_to_fetch.append((slug, asset, tf))

        for slug, asset, tf in slugs_to_fetch:
            try:
                resp = await client.get(
                    f"{GAMMA_API}/markets",
                    params={"slug": slug, "limit": 1},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                markets = data if isinstance(data, list) else data.get("data", [])
                for m in markets:
                    cid = m.get("conditionId") or ""
                    if cid in seen:
                        continue
                    seen.add(cid)
                    m["_asset"] = asset
                    m["_timeframe"] = tf
                    results.append(m)
            except Exception as e:
                log.debug("Slug fetch error for %s: %s", slug, e)

        return results

    async def _fetch_markets(self, client: httpx.AsyncClient) -> list[dict]:
        """Combine both strategies, deduplicated by conditionId."""
        batch, current = await asyncio.gather(
            self._fetch_events_batch(client),
            self._fetch_current_windows(client),
        )
        seen: set[str] = set()
        merged: list[dict] = []
        for m in batch + current:
            cid = m.get("conditionId") or ""
            if cid and cid not in seen:
                seen.add(cid)
                merged.append(m)
        return merged

    def _upsert_markets(self, markets: list[dict]) -> list[str]:
        """Returns list of newly discovered condition_ids."""
        new_ids: list[str] = []
        db = get_db()
        now = datetime.now(timezone.utc)

        rows = []
        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid:
                continue

            question = m.get("question") or ""
            asset = m.get("_asset", "BTC")
            timeframe = m.get("_timeframe")
            close_time = _parse_close_time(m)
            up_id, down_id = _extract_tokens(m)

            if not up_id or not down_id:
                log.debug("Skipping %s — no token IDs (clobTokenIds missing)", cid[:12])
                continue

            rows.append((
                cid, question, asset, timeframe,
                None,           # direction — N/A for Up/Down markets
                close_time,
                None, None, None,   # resolved_at, outcome, winning_price
                up_id, down_id,
                now, now,
            ))

            if cid not in self._known:
                new_ids.append(cid)
                self._known.add(cid)
                log.info(
                    "New market: %s | %s | %s | close=%s",
                    cid[:12], asset, timeframe,
                    close_time.isoformat() if close_time else "?",
                )

        if rows:
            with write_lock:
                db.executemany(
                    """
                    INSERT OR REPLACE INTO pm_markets
                        (condition_id, question, asset, timeframe, direction,
                         close_time, resolved_at, outcome, winning_price,
                         token_yes_id, token_no_id, created_at, last_updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )

        return new_ids

    async def run(self) -> None:
        self._running = True
        db = get_db()
        try:
            rows = db.execute("SELECT condition_id FROM pm_markets").fetchall()
            self._known = {r[0] for r in rows}
            log.info("Market discovery: %d known markets pre-loaded", len(self._known))
        except Exception:
            pass

        async with httpx.AsyncClient() as client:
            while self._running:
                try:
                    markets = await self._fetch_markets(client)
                    new_ids = self._upsert_markets(markets)
                    if self._queue and new_ids:
                        for cid in new_ids:
                            await self._queue.put(cid)
                    log.debug("Discovery poll: %d markets found, %d new", len(markets), len(new_ids))
                except Exception as e:
                    log.error("Market discovery error: %s", e)

                await asyncio.sleep(POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
