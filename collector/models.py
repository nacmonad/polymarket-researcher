"""Pydantic models for btc-oracle-proxy WebSocket events.

Mirrors the exact JSON schema documented in PLAN.md §"Exact WS Event Schemas".
All fields match field names from the Rust structs — do not rename them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ── Tick ─────────────────────────────────────────────────────────────────────

class ExchangePrices(BaseModel):
    binance: float | None = None
    coinbase: float | None = None
    kraken: float | None = None


class Indicators(BaseModel):
    ema_12: float | None = None
    ema_26: float | None = None
    ema_50: float | None = None
    rsi_14: float | None = None
    momentum_10: float | None = None   # ROC-10
    momentum_20: float | None = None   # ROC-20
    volatility: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None


class TickData(BaseModel):
    timestamp: datetime
    symbol: str
    market_price: float
    chainlink_price: float | None = None
    chainlink_age_secs: int | None = None
    deviation_pct: float | None = None
    round_imminent: bool = False
    exchange_prices: ExchangePrices = Field(default_factory=ExchangePrices)
    indicators: Indicators = Field(default_factory=Indicators)


class TickEvent(BaseModel):
    type: Literal["tick"]
    ts: datetime
    data: TickData


# ── pre_trigger_alert ────────────────────────────────────────────────────────

class PreTriggerData(BaseModel):
    direction: Literal["UP", "DOWN"]
    signals: list[str]
    deviation_pct: float
    market_price: float
    chainlink_price: float | None = None
    chainlink_age_secs: int | None = None
    bb_width_pct: float | None = None
    rsi_14: float | None = None
    momentum_10: float | None = None


class PreTriggerEvent(BaseModel):
    type: Literal["pre_trigger_alert"]
    ts: datetime
    data: PreTriggerData


# ── round_triggered ──────────────────────────────────────────────────────────

class RoundTriggeredData(BaseModel):
    direction: Literal["UP", "DOWN"]
    market_price: float
    chainlink_price: float | None = None
    chainlink_age_secs: int | None = None
    deviation_pct: float
    exchange_prices: ExchangePrices = Field(default_factory=ExchangePrices)


class RoundTriggeredEvent(BaseModel):
    type: Literal["round_triggered"]
    ts: datetime
    data: RoundTriggeredData


# ── round_settled ────────────────────────────────────────────────────────────

class RoundSettledData(BaseModel):
    prev_price: float
    new_price: float
    price_delta: float
    delta_pct: float
    round_duration_secs: int | None = None   # None = heartbeat, skip for strategy


class RoundSettledEvent(BaseModel):
    type: Literal["round_settled"]
    ts: datetime
    data: RoundSettledData


# ── deviation_approach ───────────────────────────────────────────────────────

class DeviationApproachData(BaseModel):
    direction: Literal["UP", "DOWN"]
    deviation_pct: float
    market_price: float
    chainlink_price: float | None = None
    chainlink_age_secs: int | None = None


class DeviationApproachEvent(BaseModel):
    type: Literal["deviation_approach"]
    ts: datetime
    data: DeviationApproachData


# ── bb_breakout ───────────────────────────────────────────────────────────────

class BbBreakoutData(BaseModel):
    direction: Literal["UP", "DOWN"]
    market_price: float
    bb_upper: float | None = None
    bb_lower: float | None = None
    bb_width_pct: float | None = None
    bb_expanding: bool | None = None
    deviation_pct: float | None = None


class BbBreakoutEvent(BaseModel):
    type: Literal["bb_breakout"]
    ts: datetime
    data: BbBreakoutData


# ── exchange_status ───────────────────────────────────────────────────────────
# Note: fields are at TOP LEVEL, no `data` key

class ExchangeStatusEvent(BaseModel):
    type: Literal["exchange_status"]
    ts: datetime
    exchange: str
    status: str
    error: str | None = None


# ── Union type for dispatch ───────────────────────────────────────────────────

OracleEvent = (
    TickEvent
    | PreTriggerEvent
    | RoundTriggeredEvent
    | RoundSettledEvent
    | DeviationApproachEvent
    | BbBreakoutEvent
    | ExchangeStatusEvent
)
