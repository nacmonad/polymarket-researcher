#!/usr/bin/env python3
"""
Backfill closed BTC Up/Down markets and their outcomes.

Computes slugs for every window in the past N hours, fetches each from the
Gamma API, upserts into pm_markets with the resolved outcome if available.

Run manually or every 15m from cron / Makefile:
    uv run python scripts/backfill_markets.py --hours 24
    uv run python scripts/backfill_markets.py --hours 4  # quick catch-up
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()

from collector.pm_market_discovery import (
    COLLECT_ASSETS,
    COLLECT_TIMEFRAMES,
    GAMMA_API,
    _TF_SECONDS,
    _ASSET_SLUG_PREFIX,
    _extract_tokens,
    _parse_close_time,
)
from db.connection import get_db, write_lock
from db.migrate import apply as apply_schema

log = logging.getLogger("backfill")


def past_window_slugs(
    asset: str,
    timeframe: str,
    hours: float,
    now: datetime | None = None,
) -> list[str]:
    """All window slugs for the past N hours (plus 1 future window as buffer)."""
    if now is None:
        now = datetime.now(timezone.utc)
    tf_secs = _TF_SECONDS.get(timeframe, 300)
    ts = int(now.timestamp())
    window_start = ts - (ts % tf_secs)
    n_past = int(hours * 3600 / tf_secs) + 1
    prefix = _ASSET_SLUG_PREFIX.get(asset, f"{asset.lower()}-updown")
    return [f"{prefix}-{timeframe}-{window_start - i * tf_secs}" for i in range(n_past)]


def parse_outcome(data: dict) -> tuple[str | None, float | None, datetime | None]:
    """
    Return (outcome, winning_price, resolved_at) from a Gamma market dict.

    Primary: derive from outcomePrices — when a market settles, the winning
    token's price goes to exactly "1" and the loser's to "0". This is populated
    even when the 'outcome' text field remains null.

    Format: outcomes=["Up","Down"], outcomePrices=["0","1"] → winner=Down
    """
    outcome: str | None = None

    # Strategy 1: outcomePrices ("1" = winner, "0" = loser)
    if data.get("closed"):
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

    winning_price = data.get("winningPrice") or data.get("winning_price")
    if winning_price is not None:
        try:
            winning_price = float(winning_price)
        except (TypeError, ValueError):
            winning_price = None

    resolved_at: datetime | None = None
    if outcome:
        for field in ("resolvedAt", "resolved_at", "updatedAt"):
            raw = data.get(field)
            if raw:
                try:
                    resolved_at = datetime.fromisoformat(
                        str(raw).replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    break
                except ValueError:
                    pass
        if not resolved_at:
            # Use endDate as proxy for resolution time
            close = _parse_close_time(data)
            resolved_at = close or datetime.now(timezone.utc)

    return outcome, winning_price, resolved_at


async def backfill(
    hours: float = 24.0,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    apply_schema(verbose=False)
    db = get_db()
    now = datetime.now(timezone.utc)

    # Pre-load known condition_ids to skip already-resolved markets
    known: dict[str, str | None] = {}  # cid → outcome
    try:
        rows = db.execute("SELECT condition_id, outcome FROM pm_markets").fetchall()
        known = {r[0]: r[1] for r in rows}
    except Exception:
        pass

    log.info(
        "Backfilling %s assets × %s timeframes for last %.0f hours (%d known markets)",
        COLLECT_ASSETS, COLLECT_TIMEFRAMES, hours, len(known),
    )

    all_slugs: list[tuple[str, str, str]] = []  # (slug, asset, tf)
    for asset in COLLECT_ASSETS:
        for tf in COLLECT_TIMEFRAMES:
            for slug in past_window_slugs(asset, tf, hours=hours, now=now):
                all_slugs.append((slug, asset, tf))

    log.info("Fetching %d slugs…", len(all_slugs))

    inserted = updated = skipped = errors = 0

    async with httpx.AsyncClient() as client:
        for slug, asset, tf in all_slugs:
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
                    skipped += 1
                    continue

                m = markets[0]
                cid = m.get("conditionId") or m.get("condition_id")
                if not cid:
                    skipped += 1
                    continue

                question = m.get("question") or ""
                close_time = _parse_close_time(m)
                up_id, down_id = _extract_tokens(m)
                outcome, winning_price, resolved_at = parse_outcome(m)

                if verbose:
                    log.debug(
                        "  %s | end=%s | outcome=%s | closed=%s",
                        question[:55],
                        close_time.isoformat() if close_time else "?",
                        outcome,
                        m.get("closed"),
                    )

                if cid in known:
                    # Already in DB — update outcome if newly resolved
                    if outcome and known[cid] is None:
                        if not dry_run:
                            with write_lock:
                                db.execute(
                                    """UPDATE pm_markets
                                       SET outcome=?, winning_price=?, resolved_at=?, last_updated=?
                                       WHERE condition_id=?""",
                                    [outcome, winning_price, resolved_at, now, cid],
                                )
                        updated += 1
                        log.info("Updated outcome: %s → %s", cid[:14], outcome)
                    else:
                        skipped += 1
                else:
                    # New market — insert with outcome if available
                    if not up_id or not down_id:
                        skipped += 1
                        continue
                    if not dry_run:
                        with write_lock:
                            db.execute(
                                """INSERT OR IGNORE INTO pm_markets
                                   (condition_id, question, asset, timeframe, direction,
                                    close_time, resolved_at, outcome, winning_price,
                                    token_yes_id, token_no_id, created_at, last_updated)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                [
                                    cid, question, asset, tf, None,
                                    close_time, resolved_at, outcome, winning_price,
                                    up_id, down_id, now, now,
                                ],
                            )
                    inserted += 1
                    known[cid] = outcome
                    log.info(
                        "Inserted: %s | %s | %s | outcome=%s",
                        cid[:14], tf,
                        close_time.strftime("%H:%M") if close_time else "?",
                        outcome or "unresolved",
                    )

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    skipped += 1  # window doesn't exist (gap in schedule)
                else:
                    errors += 1
                    log.warning("HTTP error for %s: %s", slug, e)
            except Exception as e:
                errors += 1
                log.warning("Error for %s: %s", slug, e)

    log.info(
        "Done — inserted=%d  outcome_updates=%d  skipped=%d  errors=%d",
        inserted, updated, skipped, errors,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill closed BTC Up/Down markets and outcomes")
    parser.add_argument("--hours", type=float, default=24.0,
                        help="How many hours back to backfill (default: 24)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and log but don't write to DB")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    asyncio.run(backfill(hours=args.hours, dry_run=args.dry_run, verbose=args.verbose))


if __name__ == "__main__":
    main()
