"""Oracle WebSocket consumer.

Subscribes to btc-oracle-proxy, validates events against pydantic models,
buffers ticks, and flushes to DuckDB. Signal events are written immediately.

Run standalone: python -m collector.oracle_consumer
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections import deque
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv
from pydantic import ValidationError

from collector.models import (
    BbBreakoutEvent,
    DeviationApproachEvent,
    ExchangeStatusEvent,
    PreTriggerEvent,
    RoundSettledEvent,
    RoundTriggeredEvent,
    TickEvent,
)
from db.connection import get_db, write_lock

load_dotenv()

log = logging.getLogger(__name__)

ORACLE_WS_URL = os.getenv("ORACLE_WS_URL", "ws://127.0.0.1:8080/ws")
TICK_BATCH_SIZE = int(os.getenv("ORACLE_TICK_BATCH_SIZE", "50"))
DEFAULT_SYMBOL = "BTC/USD"

# Metrics shared with the dashboard
metrics: dict = {
    "ticks_received": 0,
    "ticks_flushed": 0,
    "signals_received": 0,
    "last_tick_ts": None,
    "last_signal_ts": None,
    "last_signal_type": None,
    "gaps_detected": 0,
    "reconnects": 0,
    "connected": False,
}


class OracleConsumer:
    def __init__(self, uri: str = ORACLE_WS_URL, batch_size: int = TICK_BATCH_SIZE):
        self.uri = uri
        self.batch_size = batch_size
        self._tick_buffer: list[tuple] = []
        self._running = False
        self._last_tick_ts: datetime | None = None

    # ── Flush ─────────────────────────────────────────────────────────────────

    def _flush_ticks(self) -> None:
        if not self._tick_buffer:
            return
        db = get_db()
        with write_lock:
            db.executemany(
                """
                INSERT OR IGNORE INTO oracle_ticks
                    (ts, symbol, market_price, chainlink_price, chainlink_age_secs,
                     deviation_pct, round_imminent,
                     price_binance, price_coinbase, price_kraken,
                     ema_12, ema_26, ema_50, rsi_14,
                     momentum_10, momentum_20, volatility,
                     bb_upper, bb_middle, bb_lower,
                     macd, macd_signal, macd_histogram)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                self._tick_buffer,
            )
        metrics["ticks_flushed"] += len(self._tick_buffer)
        self._tick_buffer.clear()

    def _insert_signal(self, row: tuple) -> None:
        db = get_db()
        with write_lock:
            db.execute(
                """
                INSERT OR IGNORE INTO signal_events
                    (ts, event_type, symbol, direction, deviation_pct,
                     market_price, chainlink_price, chainlink_age_secs,
                     prev_price, new_price, price_delta, delta_pct, round_duration_secs,
                     signals_json, bb_width_pct, rsi_14, momentum_10,
                     bb_upper, bb_lower, bb_expanding,
                     raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                row,
            )

    # ── Event handlers ────────────────────────────────────────────────────────

    def _handle_tick(self, ev: TickEvent) -> None:
        d = ev.data
        ind = d.indicators
        xp = d.exchange_prices
        now_ts = d.timestamp

        # Gap detection
        if self._last_tick_ts is not None:
            gap = (now_ts - self._last_tick_ts).total_seconds()
            if gap > 2.0:
                log.warning("Tick gap: %.1fs at %s", gap, now_ts.isoformat())
                metrics["gaps_detected"] += 1
        self._last_tick_ts = now_ts

        self._tick_buffer.append((
            now_ts, d.symbol, d.market_price,
            d.chainlink_price, d.chainlink_age_secs,
            d.deviation_pct, d.round_imminent,
            xp.binance, xp.coinbase, xp.kraken,
            ind.ema_12, ind.ema_26, ind.ema_50, ind.rsi_14,
            ind.momentum_10, ind.momentum_20, ind.volatility,
            ind.bb_upper, ind.bb_middle, ind.bb_lower,
            ind.macd, ind.macd_signal, ind.macd_histogram,
        ))

        metrics["ticks_received"] += 1
        metrics["last_tick_ts"] = now_ts.isoformat()

        if len(self._tick_buffer) >= self.batch_size:
            self._flush_ticks()

    def _handle_pre_trigger(self, ev: PreTriggerEvent, raw: str) -> None:
        d = ev.data
        self._insert_signal((
            ev.ts, "pre_trigger_alert", DEFAULT_SYMBOL,
            d.direction, d.deviation_pct,
            d.market_price, d.chainlink_price, d.chainlink_age_secs,
            None, None, None, None, None,           # round_settled fields
            json.dumps(d.signals),                  # signals_json
            d.bb_width_pct, d.rsi_14, d.momentum_10,
            None, None, None,                       # bb_breakout fields
            raw,
        ))
        self._log_signal("pre_trigger_alert", ev.ts)

    def _handle_round_triggered(self, ev: RoundTriggeredEvent, raw: str) -> None:
        d = ev.data
        self._insert_signal((
            ev.ts, "round_triggered", DEFAULT_SYMBOL,
            d.direction, d.deviation_pct,
            d.market_price, d.chainlink_price, d.chainlink_age_secs,
            None, None, None, None, None,
            None, None, None, None,
            None, None, None,
            raw,
        ))
        self._log_signal("round_triggered", ev.ts)

    def _handle_round_settled(self, ev: RoundSettledEvent, raw: str) -> None:
        d = ev.data
        # Skip heartbeat settlements (round_duration_secs IS NULL)
        if d.round_duration_secs is None:
            log.debug("Heartbeat settlement — skipping signal insert")
            return
        self._insert_signal((
            ev.ts, "round_settled", DEFAULT_SYMBOL,
            None, d.delta_pct,
            d.new_price, None, None,
            d.prev_price, d.new_price, d.price_delta, d.delta_pct, d.round_duration_secs,
            None, None, None, None,
            None, None, None,
            raw,
        ))
        self._log_signal("round_settled", ev.ts)

    def _handle_deviation_approach(self, ev: DeviationApproachEvent, raw: str) -> None:
        d = ev.data
        self._insert_signal((
            ev.ts, "deviation_approach", DEFAULT_SYMBOL,
            d.direction, d.deviation_pct,
            d.market_price, d.chainlink_price, d.chainlink_age_secs,
            None, None, None, None, None,
            None, None, None, None,
            None, None, None,
            raw,
        ))
        self._log_signal("deviation_approach", ev.ts)

    def _handle_bb_breakout(self, ev: BbBreakoutEvent, raw: str) -> None:
        d = ev.data
        self._insert_signal((
            ev.ts, "bb_breakout", DEFAULT_SYMBOL,
            d.direction, d.deviation_pct,
            d.market_price, None, None,
            None, None, None, None, None,
            None, d.bb_width_pct, None, None,
            d.bb_upper, d.bb_lower, d.bb_expanding,
            raw,
        ))
        self._log_signal("bb_breakout", ev.ts)

    def _log_signal(self, event_type: str, ts: datetime) -> None:
        metrics["signals_received"] += 1
        metrics["last_signal_ts"] = ts.isoformat()
        metrics["last_signal_type"] = event_type
        log.info("Signal: %s @ %s", event_type, ts.isoformat())

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Invalid JSON: %s", raw[:120])
            return

        t = payload.get("type")
        try:
            if t == "tick":
                self._handle_tick(TickEvent.model_validate(payload))
            elif t == "pre_trigger_alert":
                self._handle_pre_trigger(PreTriggerEvent.model_validate(payload), raw)
            elif t == "round_triggered":
                self._handle_round_triggered(RoundTriggeredEvent.model_validate(payload), raw)
            elif t == "round_settled":
                self._handle_round_settled(RoundSettledEvent.model_validate(payload), raw)
            elif t == "deviation_approach":
                self._handle_deviation_approach(DeviationApproachEvent.model_validate(payload), raw)
            elif t == "bb_breakout":
                self._handle_bb_breakout(BbBreakoutEvent.model_validate(payload), raw)
            elif t == "exchange_status":
                ev = ExchangeStatusEvent.model_validate(payload)
                log.info("Exchange status: %s → %s (err=%s)", ev.exchange, ev.status, ev.error)
            else:
                log.debug("Unknown event type: %s", t)
        except ValidationError as e:
            log.warning("Validation error for %s: %s", t, e)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        backoff = 1.0
        subscribe_msg = json.dumps({"type": "subscribe", "channels": ["BTC/USD"]})

        while self._running:
            try:
                log.info("Connecting to oracle: %s", self.uri)
                async with websockets.connect(self.uri, ping_interval=20) as ws:
                    await ws.send(subscribe_msg)
                    metrics["connected"] = True
                    backoff = 1.0
                    log.info("Oracle connected — streaming")

                    async for raw in ws:
                        if not self._running:
                            break
                        self._dispatch(raw)

            except (websockets.ConnectionClosed, OSError) as e:
                metrics["connected"] = False
                self._flush_ticks()
                log.warning("Oracle disconnected: %s — reconnecting in %.0fs", e, backoff)
                metrics["reconnects"] += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

            except asyncio.CancelledError:
                break

        self._flush_ticks()
        metrics["connected"] = False
        log.info("Oracle consumer stopped. Flushed remaining ticks.")

    def stop(self) -> None:
        self._running = False


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from db.migrate import apply
    apply(verbose=False)

    consumer = OracleConsumer()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, consumer.stop)
    loop.add_signal_handler(signal.SIGTERM, consumer.stop)

    await consumer.run()


if __name__ == "__main__":
    asyncio.run(main())
