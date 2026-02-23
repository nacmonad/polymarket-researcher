"""Polymarket CLOB WebSocket consumer.

Subscribes to order book channels for all active markets,
maintains in-memory L1 order books, and snapshots to pm_snapshots
every 500ms (aligned with oracle tick rate).

Separate tasks:
  - ws_task: manages WebSocket connection and dispatches messages
  - snapshot_task: flushes in-memory book to DB every 500ms
  - subscription_task: listens for new condition_ids from market discovery

Notes:
  - Polymarket WS does NOT require auth for public order book data.
  - Book update messages use asset_id (= token_id) as key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv

from db.connection import get_db, write_lock

load_dotenv()

log = logging.getLogger(__name__)

PM_WS_URL = os.getenv("POLYMARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
SNAPSHOT_INTERVAL = 0.5  # seconds — matches oracle tick rate


@dataclass
class L1Book:
    token_id: str
    condition_id: str
    best_bid: float | None = None
    best_ask: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None
    last_trade_price: float | None = None
    last_trade_size: float | None = None
    updated_at: float = field(default_factory=time.time)

    @property
    def mid_price(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


class ClobConsumer:
    def __init__(self, new_market_queue: asyncio.Queue | None = None):
        self._queue = new_market_queue
        # token_id → L1Book
        self._books: dict[str, L1Book] = {}
        # condition_id → (yes_token_id, no_token_id)
        self._market_tokens: dict[str, tuple[str | None, str | None]] = {}
        self._running = False
        self._ws: websockets.WebSocketClientProtocol | None = None

    # ── Book management ───────────────────────────────────────────────────────

    def _ensure_book(self, token_id: str, condition_id: str) -> L1Book:
        if token_id not in self._books:
            self._books[token_id] = L1Book(token_id=token_id, condition_id=condition_id)
        return self._books[token_id]

    def _load_active_markets(self) -> None:
        """Load active markets from DB on startup."""
        db = get_db()
        try:
            rows = db.execute(
                """
                SELECT condition_id, token_yes_id, token_no_id
                FROM pm_markets
                WHERE resolved_at IS NULL
                  AND token_yes_id IS NOT NULL
                """
            ).fetchall()
            for cid, yes_id, no_id in rows:
                self._market_tokens[cid] = (yes_id, no_id)
                if yes_id:
                    self._ensure_book(yes_id, cid)
                if no_id:
                    self._ensure_book(no_id, cid)
            log.info("CLOB consumer: loaded %d active markets", len(rows))
        except Exception as e:
            log.warning("Could not load active markets: %s", e)

    # ── Subscribe ─────────────────────────────────────────────────────────────

    def _make_subscribe_msg(self, condition_ids: list[str]) -> str:
        assets = []
        for cid in condition_ids:
            tokens = self._market_tokens.get(cid, (None, None))
            for tok in tokens:
                if tok:
                    assets.append(tok)
        return json.dumps({"type": "subscribe", "channel": "book", "assets_ids": assets})

    async def _subscribe_market(self, condition_id: str) -> None:
        """Subscribe to a newly discovered market."""
        # First fetch token IDs from DB
        db = get_db()
        try:
            row = db.execute(
                "SELECT token_yes_id, token_no_id FROM pm_markets WHERE condition_id = ?",
                [condition_id],
            ).fetchone()
            if row:
                yes_id, no_id = row
                self._market_tokens[condition_id] = (yes_id, no_id)
                if yes_id:
                    self._ensure_book(yes_id, condition_id)
                if no_id:
                    self._ensure_book(no_id, condition_id)
        except Exception as e:
            log.warning("Could not fetch tokens for %s: %s", condition_id, e)
            return

        if self._ws and not self._ws.closed:
            msg = self._make_subscribe_msg([condition_id])
            try:
                await self._ws.send(msg)
                log.info("Subscribed to CLOB for %s", condition_id[:12])
            except Exception as e:
                log.warning("Could not subscribe to %s: %s", condition_id, e)

    # ── Message dispatch ──────────────────────────────────────────────────────

    def _handle_book(self, msg: dict) -> None:
        """Handle book snapshot or update message."""
        asset_id = msg.get("asset_id") or msg.get("market")
        if not asset_id or asset_id not in self._books:
            return

        book = self._books[asset_id]

        # Full book snapshot
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])

        if bids:
            # Sort descending by price, take best
            sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
            if sorted_bids:
                book.best_bid = float(sorted_bids[0]["price"])
                book.bid_depth = float(sorted_bids[0].get("size", 0))

        if asks:
            sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 0)))
            if sorted_asks:
                book.best_ask = float(sorted_asks[0]["price"])
                book.ask_depth = float(sorted_asks[0].get("size", 0))

        book.updated_at = time.time()

    def _handle_price_change(self, msg: dict) -> None:
        """Handle individual price level update."""
        asset_id = msg.get("asset_id")
        if not asset_id or asset_id not in self._books:
            return

        book = self._books[asset_id]
        side = msg.get("side", "").upper()
        price = float(msg.get("price", 0))
        size = float(msg.get("size", 0))

        if side == "BUY":
            book.best_bid = price if size > 0 else None
            book.bid_depth = size if size > 0 else None
        elif side == "SELL":
            book.best_ask = price if size > 0 else None
            book.ask_depth = size if size > 0 else None

        book.updated_at = time.time()

    def _handle_trade(self, msg: dict) -> None:
        """Handle trade fill event."""
        asset_id = msg.get("asset_id")
        if not asset_id or asset_id not in self._books:
            return

        book = self._books[asset_id]
        book.last_trade_price = float(msg.get("price", 0) or 0)
        book.last_trade_size = float(msg.get("size", 0) or 0)

        # Persist to pm_trades
        trade_id = msg.get("id") or msg.get("trade_id")
        if not trade_id:
            return

        db = get_db()
        ts = datetime.now(timezone.utc)
        with write_lock:
            try:
                db.execute(
                    """
                    INSERT OR IGNORE INTO pm_trades
                        (id, ts, condition_id, token_id, side, price, size, taker_side)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        trade_id, ts, book.condition_id, asset_id,
                        msg.get("side"), book.last_trade_price, book.last_trade_size,
                        msg.get("taker_side"),
                    ),
                )
            except Exception as e:
                log.debug("pm_trades insert error: %s", e)

    def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Polymarket WS sends lists of events
        if isinstance(msg, list):
            for item in msg:
                self._dispatch_one(item)
        else:
            self._dispatch_one(msg)

    def _dispatch_one(self, msg: dict) -> None:
        t = msg.get("event_type") or msg.get("type") or ""
        if t in ("book",):
            self._handle_book(msg)
        elif t in ("price_change",):
            self._handle_price_change(msg)
        elif t in ("trade",):
            self._handle_trade(msg)

    # ── Snapshot flush ────────────────────────────────────────────────────────

    async def _snapshot_loop(self) -> None:
        while self._running:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
            if not self._books:
                continue

            ts = datetime.now(timezone.utc)
            rows = []
            for token_id, book in self._books.items():
                if book.best_bid is None and book.best_ask is None:
                    continue
                rows.append((
                    ts, book.condition_id, token_id,
                    book.best_bid, book.best_ask, book.mid_price, book.spread,
                    book.bid_depth, book.ask_depth,
                    book.last_trade_price, book.last_trade_size,
                ))

            if rows:
                db = get_db()
                with write_lock:
                    try:
                        db.executemany(
                            """
                            INSERT OR IGNORE INTO pm_snapshots
                                (ts, condition_id, token_id,
                                 best_bid, best_ask, mid_price, spread,
                                 bid_depth_1, ask_depth_1,
                                 last_trade_price, last_trade_size)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            rows,
                        )
                    except Exception as e:
                        log.debug("Snapshot flush error: %s", e)

    # ── Subscription listener ─────────────────────────────────────────────────

    async def _subscription_listener(self) -> None:
        """Listen for new condition_ids from market discovery."""
        if self._queue is None:
            return
        while self._running:
            try:
                cid = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._subscribe_market(cid)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    # ── Main WS loop ──────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                log.info("Connecting to Polymarket CLOB WS: %s", PM_WS_URL)
                async with websockets.connect(PM_WS_URL, ping_interval=30) as ws:
                    self._ws = ws
                    backoff = 1.0

                    # Subscribe to all known active markets
                    known_cids = list(self._market_tokens.keys())
                    if known_cids:
                        await ws.send(self._make_subscribe_msg(known_cids))

                    async for raw in ws:
                        if not self._running:
                            break
                        self._dispatch(raw)

            except (websockets.ConnectionClosed, OSError) as e:
                log.warning("CLOB WS disconnected: %s — reconnecting in %.0fs", e, backoff)
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except asyncio.CancelledError:
                break

        self._ws = None

    async def run(self) -> None:
        self._running = True
        self._load_active_markets()
        await asyncio.gather(
            self._ws_loop(),
            self._snapshot_loop(),
            self._subscription_listener(),
        )

    def stop(self) -> None:
        self._running = False
