# polymarket-researcher — Strategy & Architecture Plan

Companion project to `btc-oracle-proxy`. Subscribes to the oracle WS feed,
collects Polymarket CLOB data, persists everything to DuckDB, and runs
backtests + live execution of oracle-impulse strategies.

---

## Why DuckDB

| Concern | DuckDB |
|---|---|
| Time-series range queries | Columnar, extremely fast on `WHERE ts BETWEEN …` |
| Python-native | `duckdb.sql("SELECT …").df()` → pandas/polars in one line |
| No server | Just a file, zero ops, works on a laptop |
| Partitioning | Partition by date automatically, keeps query scans tiny |
| Write throughput | ~100k rows/sec insert — more than enough for our volumes |
| Backtesting joins | Analytical SQL with window functions is ideal for this |

Estimated storage: ~500MB / week at full resolution (oracle ticks + CLOB snapshots).
7 days of data = ~3.5GB — trivially manageable.

---

## Data Sources

### 1. btc-oracle-proxy WebSocket (already built)
- `tick` events — 500ms price + indicators + deviation
- `pre_trigger_alert` — convergence signal (earliest)
- `deviation_approach` — 0.07% zone
- `round_triggered` — 0.10% crossed
- `round_settled` — new Chainlink price committed on-chain

### 2. Polymarket Gamma API (market discovery + history)
```
GET https://gamma-api.polymarket.com/markets
    ?tag=crypto&active=true&limit=50
```
Returns: condition_id, question, strike price, resolution time, end date.
Filter for: `"Will BTC be above $X at <time>?"` markets.

### 3. Polymarket CLOB API (live order book)
```
GET https://clob.polymarket.com/book?token_id=<condition_id>
WebSocket wss://ws-subscriptions-clob.polymarket.com/ws/market
```
Returns: best_bid, best_ask, mid, spread, last_trade_price, volume.

### 4. Polymarket outcomes (settlement)
```
GET https://gamma-api.polymarket.com/markets?condition_id=<id>
```
After resolution: `outcome` field set to `"YES"` or `"NO"`.

---

## DuckDB Schema

```sql
-- ── Oracle feed ──────────────────────────────────────────────────────────────

CREATE TABLE oracle_ticks (
    ts               TIMESTAMPTZ NOT NULL,
    market_price     DOUBLE NOT NULL,
    cl_price         DOUBLE,
    cl_age_secs      INTEGER,
    deviation_pct    DOUBLE,
    round_imminent   BOOLEAN DEFAULT FALSE,
    binance          DOUBLE,
    coinbase         DOUBLE,
    kraken           DOUBLE,
    -- indicators
    ema_12           DOUBLE,
    ema_26           DOUBLE,
    rsi_14           DOUBLE,
    roc_10           DOUBLE,
    bb_upper         DOUBLE,
    bb_lower         DOUBLE,
    bb_width_pct     DOUBLE,
    macd             DOUBLE,
    macd_histogram   DOUBLE,
    PRIMARY KEY (ts)
);

-- ── Signal events (rising-edge only, sparse) ─────────────────────────────────

CREATE TABLE signal_events (
    ts           TIMESTAMPTZ NOT NULL,
    event_type   VARCHAR NOT NULL,   -- pre_trigger_alert | round_triggered |
                                     -- round_settled | deviation_approach | bb_breakout
    direction    VARCHAR,            -- UP | DOWN | NULL
    deviation    DOUBLE,
    market_price DOUBLE,
    cl_price     DOUBLE,
    cl_age_secs  INTEGER,
    signals      VARCHAR[],          -- for pre_trigger_alert
    bb_width_pct DOUBLE,
    rsi_14       DOUBLE,
    roc_10       DOUBLE,
    -- for round_settled
    prev_cl_price      DOUBLE,
    round_duration_sec INTEGER,
    PRIMARY KEY (ts, event_type)
);

-- ── Polymarket markets (static metadata) ─────────────────────────────────────

CREATE TABLE pm_markets (
    condition_id       VARCHAR PRIMARY KEY,
    question           VARCHAR,
    asset              VARCHAR DEFAULT 'BTC',
    strike_price       DOUBLE,
    resolution_ts      TIMESTAMPTZ,
    market_type        VARCHAR,      -- 5m | 15m | 1h | 4h | 1d
    created_at         TIMESTAMPTZ,
    resolved_at        TIMESTAMPTZ,
    outcome            VARCHAR,      -- YES | NO | NULL (unresolved)
    final_price        DOUBLE        -- Chainlink price at resolution
);

-- ── Polymarket CLOB snapshots (polled ~every 10s) ────────────────────────────

CREATE TABLE pm_snapshots (
    ts               TIMESTAMPTZ NOT NULL,
    condition_id     VARCHAR NOT NULL,
    yes_best_bid     DOUBLE,
    yes_best_ask     DOUBLE,
    yes_mid          DOUBLE,
    no_best_bid      DOUBLE,
    no_best_ask      DOUBLE,
    spread           DOUBLE,   -- yes_best_ask - yes_best_bid
    last_trade_price DOUBLE,
    volume_24h       DOUBLE,
    secs_to_resolve  INTEGER,  -- resolution_ts - ts in seconds
    PRIMARY KEY (ts, condition_id)
);

-- ── Labelled signal outcomes (built post-resolution, training data) ───────────

CREATE VIEW signal_outcomes AS
SELECT
    s.ts                                          AS signal_ts,
    s.event_type,
    s.direction,
    s.deviation,
    s.cl_age_secs,
    s.bb_width_pct,
    s.rsi_14,
    s.roc_10,
    -- Nearest CLOB snapshot at signal time
    snap.yes_best_ask                             AS yes_ask_at_signal,
    snap.yes_best_bid                             AS yes_bid_at_signal,
    snap.spread                                   AS spread_at_signal,
    snap.secs_to_resolve,
    -- Outcome
    m.outcome,
    m.final_price,
    -- P&L if we bought YES at yes_ask_at_signal
    CASE WHEN m.outcome = 'YES' THEN 1.0 - snap.yes_best_ask
         ELSE -snap.yes_best_ask
    END                                           AS yes_pnl,
    -- P&L if we bought NO at no_best_ask
    CASE WHEN m.outcome = 'NO' THEN 1.0 - snap.no_best_ask
         ELSE -snap.no_best_ask
    END                                           AS no_pnl
FROM signal_events s
JOIN pm_snapshots snap ON
    snap.ts = (
        SELECT MAX(ts) FROM pm_snapshots
        WHERE ts <= s.ts
        AND secs_to_resolve BETWEEN 60 AND 900   -- 1–15 min to resolution
    )
JOIN pm_markets m ON snap.condition_id = m.condition_id
WHERE m.outcome IS NOT NULL;
```

---

## Strategy Taxonomy

### The core insight
Polymarket's 5m/15m BTC markets settle against the Chainlink on-chain price at
a specific timestamp. Our oracle feed detects when a Chainlink update is about
to happen — **before the market reprices**. The edge window is 5–45 seconds.

```
Oracle signal fires
        │
        ▼
Polymarket market is still priced at "old" odds
(hasn't fully repriced because Chainlink hasn't committed yet)
        │
        ▼
You enter at stale odds
        │
        ▼
Chainlink commits new price
        │
        ▼
Market reprices rapidly / resolves in your favour
```

---

### Strategy 1 — Directional Impulse (simplest, highest risk)

**Entry condition:**
- `pre_trigger_alert` fires with direction=UP
- A 5m or 15m market exists resolving in 2–15 min
- Current `market_price > strike_price` (price is above the bar we need to clear)
- `yes_best_ask < 0.70` (market hasn't fully priced in the move yet)

**Action:** Buy YES at market (`yes_best_ask`)

**Exit:** Hold to resolution (binary outcome)

**Expected edge:**
- Chainlink will commit a price above strike within ~45s
- YES settles at 1.00
- P&L = `1.00 - yes_ask_at_entry`

**Risk:** Oracle signal fires but price reverts before Chainlink commits.
This happens. The `round_duration_secs` distribution in your backtest will tell
you how often.

---

### Strategy 2 — Impulse + Hedge (risk-neutral target)

This is the interesting one. The goal is to lock in a guaranteed profit
regardless of which way the market resolves.

**Phase 1 — Early entry on `bb_breakout` or `pre_trigger_alert`:**
- Direction=DOWN, deviation=-0.058%
- YES is cheap (e.g., ask=0.35) because market thinks NO
- Buy YES at 0.35  ← this is the speculative leg

**Phase 2 — Hedge on `round_triggered`:**
- Deviation now -0.131%, round is definitely triggering
- NO is now cheap (ask=0.25) because everyone is piling into NO
- Buy NO at 0.25  ← this is the hedge

**Total cost:** 0.35 + 0.25 = 0.60
**Guaranteed payout:** 1.00 (one side always pays)
**Guaranteed profit:** 0.40 per $1 risked (67% return)

**Why this works:**
- You buy YES when it's mispriced early (low conviction from market)
- You buy NO when it's cheap during the rush (everyone's certain now)
- The spread temporarily widens during the signal, then compresses on both sides
- You're exploiting the *timing* of repricing, not predicting direction

**The constraint:** Needs sufficient liquidity on both sides and a market
resolving in a tight enough window. Works best on 5m markets where
the spread evolution is fast.

**Risk reduction lever:** Only hedge when `yes_pnl_floor + no_pnl_floor > 0`.
Calculate this before entering Phase 2.

---

### Strategy 3 — Sideways / Squeeze Capture

**When:** BB is squeezed (`bb_width_pct < 0.30%`), no strong deviation signal.
Chainlink has committed a recent price, BTC is ranging.

**Setup:**
- Find a 5m market with strike near `chainlink_price ± 0.05%`
- These are ~50/50 markets
- The spread will be wide (bid=0.44, ask=0.56 on YES)

**Entry:** Take the side marginally favoured by the current deviation.
Even +0.02% deviation = slight YES edge.

**Edge source:** Not the oracle signal but the bid/ask spread inefficiency.
If fair value is 52% but you can buy at 48 cents — that's +4 cents EV.

This is low-variance, low-return. Suitable as a baseline while waiting for
impulse signals. Volume play.

---

### Strategy 4 — Settlement Fade

**After** `round_settled` fires and you know the new committed price:
- Find any market resolving in the next 10–30 min
- The market will take 10–30s to fully reprice to the new Chainlink baseline
- Enter the now-correct side before full repricing

Tightest edge window of all strategies. Requires fast execution.

---

## Risk Management Framework

```
MAX_POSITION_SIZE   = $50 per signal event
MAX_CONCURRENT      = 3 open positions
MAX_DAILY_LOSS      = $150 (auto-shutdown)
MIN_SECS_TO_RESOLVE = 90   (don't enter if < 90s to resolution)
MAX_SECS_TO_RESOLVE = 900  (only 5m and 15m markets)
MIN_LIQUIDITY       = $500 volume on target market
```

**Position sizing formula:**

```python
def size(yes_ask: float, edge_confidence: float, max_size: float) -> float:
    """Kelly-inspired sizing. edge_confidence ∈ [0, 1] from calibration model."""
    kelly_fraction = (edge_confidence - (1 - edge_confidence) / (1 / yes_ask - 1))
    # Use quarter-Kelly for safety
    return max_size * max(0, kelly_fraction) * 0.25
```

---

## Decision Tree (v1 — no ML required)

```
Signal fires
    │
    ├─ event_type == pre_trigger_alert ?
    │      ├─ secs_to_resolve in [90, 900] ?
    │      │      ├─ YES: enter Phase 1 (directional leg)
    │      │      └─ NO: skip
    │      └─ NO: skip
    │
    ├─ event_type == round_triggered AND open_position ?
    │      ├─ hedge available (no_ask < 0.40) ?
    │      │      ├─ YES: enter Phase 2 (hedge leg) → locked profit
    │      │      └─ NO: hold to resolution
    │      └─ NO: directional entry (last chance, higher risk)
    │
    └─ event_type == round_settled ?
           └─ close any open positions approaching expiry
```

This alone — no ML — should have a positive edge based on the structural
mechanics. Backtest this first.

---

## ML Roadmap

### When to add ML vs when not to

| Question | Decision tree sufficient? | ML adds value? |
|---|---|---|
| Should I enter? | ✅ Yes | Marginal |
| How much to bet? | ⚠️ Fixed rules miss nuance | ✅ Yes — calibration |
| Which signals are predictive? | ⚠️ Guess | ✅ Yes — feature importance |
| What's the "shape" of a real signal vs noise? | ❌ No | ✅ Yes — sequence model |
| Adapt to changing market conditions | ❌ No | ✅ Yes — RL |

---

### Phase 1 — XGBoost calibration model (after 7 days data)

**Goal:** Given a signal event, predict `P(outcome == direction)`.

**Features:**
```python
features = [
    "event_type",          # encoded: pre_trigger=3, round_triggered=2, approach=1
    "deviation_pct",       # signed, most important feature
    "cl_age_secs",         # staler baseline = more room to move
    "bb_width_pct",        # wider = more energy
    "bb_expanding",        # boolean
    "rsi_14",              # overbought/oversold context
    "roc_10",              # short-term momentum magnitude
    "secs_to_resolve",     # time pressure
    "yes_ask_at_signal",   # current market pricing
    "spread_at_signal",    # market efficiency
    "hour_of_day",         # time-of-day effects (liquidity patterns)
]

target = "outcome_aligned"  # 1 if outcome matches direction, else 0
```

**Why XGBoost over a neural net:**
- Works well with tabular, mixed-type features
- Handles missing values natively (indicators not available at startup)
- Fast training on small datasets (100s of samples is enough)
- Feature importance is directly interpretable — tells you which signals
  are actually predictive vs noise
- No hyperparameter hell

**Training:** `signal_outcomes` DuckDB view + `outcome IS NOT NULL`.
Retrain weekly. Expect 60–75% accuracy — that's sufficient for positive EV
when combined with good position sizing.

---

### Phase 2 — LSTM / Transformer signal classifier (after 30 days data)

**Goal:** Classify whether a signal will "follow through" based on the
30-second price sequence leading up to it.

**Why sequences matter:**
- A `pre_trigger_alert` after a sharp 5-second spike looks different from
  one after a steady 30-second grind
- The grind is more likely to follow through
- The spike often reverts before Chainlink commits

**Architecture:**
```
Input: last 60 oracle_ticks before signal (30s at 500ms) × 8 features
       [market_price, deviation_pct, bb_width, rsi, roc_10, macd_hist,
        volume_proxy, cl_age_delta]

Model: LSTM(64) → Dropout(0.3) → Dense(32) → Dense(1, sigmoid)
  OR
       Transformer with 4 heads, positional encoding, 2 layers
       (better at capturing both local and global patterns in sequence)

Output: P(signal follows through)
```

**When to use Transformer vs LSTM:**
- LSTM is simpler, trains faster, sufficient for 60-step sequences
- Transformer is better if you want to attend to specific moments
  (e.g., "the spike 20s ago is less relevant than the steady build 5s ago")
- For <60 steps, the difference is small. Start with LSTM.

**Data requirement:** ~500 labelled signal events minimum → ~25 days at
current signal frequency.

---

### Phase 3 — Reinforcement Learning (after 90 days data)

**Goal:** Learn optimal position sizing and timing dynamically rather than
using fixed Kelly fractions.

**Why RL fits here:**
- The environment is partially observable (you don't know when the next
  signal fires)
- Actions have delayed rewards (resolution happens 5–15 min after entry)
- The optimal strategy likely changes based on market conditions

**Architecture:** PPO (Proximal Policy Optimization) or SAC (Soft
Actor-Critic for continuous action spaces)

```
State:  current_signals + open_positions + market_state + time_of_day
Action: [position_size_yes, position_size_no] ∈ [0, max_size]
Reward: risk-adjusted P&L (Sharpe ratio over episode)
```

**Simulation:** Build a market simulator from DuckDB data that replays
historical CLOB snapshots with realistic slippage.

**Caution:** RL is data-hungry and prone to overfitting in small financial
datasets. Only attempt after 90+ days of recorded data and only with
strong regularisation. The XGBoost calibration model will likely outperform
RL for a long time.

---

## 7-Day Data Sufficiency Analysis

For impulse strategies, 7 days is the right starting point because:

1. **Signal frequency:** At current BTC volatility, expect 8–20 `round_triggered`
   events/day → 56–140 labelled outcomes in 7 days. Enough for a basic XGBoost
   calibration model with limited features.

2. **No trend dependency:** You're not predicting price direction — you're
   predicting "will Chainlink commit a price consistent with the signal?"
   This is a structural oracle mechanic, not a trend-following bet.
   Bear market, bull market, sideways — the mechanic works the same.

3. **Market freshness:** Polymarket liquidity and spread patterns can change.
   Using data older than 30 days risks training on stale market structure.
   Fresh 7-day windows are actually preferable to months of stale data.

4. **Sideways bonus:** In sideways/low-volatility periods:
   - `pre_trigger_alert` fires less frequently (fewer false positives to filter)
   - When it does fire, the deviation builds more deliberately (higher precision)
   - Strategy 3 (squeeze capture) activates
   - The combination of strategies has **lower variance in sideways markets**
     than in trending ones

**Rolling window training:** Retrain XGBoost every 7 days on the most recent
7-day window. Don't accumulate stale data — the most recent patterns are
the most relevant.

---

## Implementation Phases

### Phase 1 — Data collection (week 1)
```
polymarket_researcher/
├── collector/
│   ├── oracle_consumer.py    # subscribes to btc-oracle-proxy WS
│   ├── clob_poller.py        # polls CLOB every 10s for relevant markets
│   ├── market_discovery.py   # fetches 5m/15m/1h/4h/1d markets from Gamma
│   └── outcome_watcher.py    # polls resolved markets, writes outcomes
├── storage/
│   ├── db.py                 # DuckDB connection + schema init
│   └── schema.sql            # CREATE TABLE statements (above)
└── main.py                   # orchestrates collectors
```

**Goal:** Just collect. Don't trade. Validate data quality.

### Phase 2 — Backtesting (week 2)
```
├── backtest/
│   ├── loader.py             # load signal_outcomes view
│   ├── strategy_v1.py        # decision tree rules
│   ├── metrics.py            # Sharpe, max drawdown, win rate, avg P&L
│   └── visualise.py          # matplotlib equity curves per strategy
```

**Goal:** Validate that the structural edge exists in your data.
Target: strategy v1 win rate > 58% on `round_triggered` directional entries.

### Phase 3 — Calibration model (week 3)
```
├── models/
│   ├── calibration.py        # XGBoost train/eval
│   ├── features.py           # feature engineering from signal_outcomes
│   └── evaluate.py           # calibration curve, AUC, feature importance
```

### Phase 4 — Live paper trading (week 4)
```
├── executor/
│   ├── signal_handler.py     # consumes oracle WS, routes signals
│   ├── market_selector.py    # finds best matching PM market
│   ├── position_manager.py   # tracks open legs, sizing, P&L
│   └── paper_broker.py       # simulates fills using live CLOB data
```

### Phase 5 — Live execution (when paper trading is profitable)
```
├── executor/
│   └── clob_broker.py        # actual py-clob-client orders
```

---

## Key Dependencies

```python
# requirements.txt
websockets>=12.0        # oracle WS consumer
duckdb>=0.10            # storage + analytics
pandas>=2.0             # data manipulation
xgboost>=2.0            # calibration model
scikit-learn>=1.4       # preprocessing, metrics
py-clob-client>=0.18    # Polymarket CLOB API
aiohttp>=3.9            # async HTTP for Gamma API
python-dotenv>=1.0      # env config
```

---

## Risk Warnings

1. **Latency matters.** The edge window is 5–45 seconds. Network latency
   between your oracle service and Polymarket CLOB execution will eat into
   this. Co-locate if serious.

2. **Liquidity depth.** The hedge strategy (Phase 2) requires being able to
   buy both sides at stated prices. Thin markets will gap. Check volume > $500
   before entering.

3. **Chainlink heartbeat ≠ signal.** ~5% of `round_settled` events are
   periodic heartbeats (not deviation-triggered). These won't have an
   associated `round_triggered` signal. Your outcome watcher needs to
   distinguish these.

4. **Market availability.** Polymarket doesn't always have a 5m BTC market
   resolving in the next 5–15 minutes. This limits signal utilisation to
   maybe 40–60% of signals having a tradeable market.

5. **Regulation.** Polymarket restricts US users. Know your jurisdiction.

---

---

---

# btc-oracle-proxy — Implementation Memory

> This section is a precise reference for the oracle feed that the researcher
> will consume. Keep it up to date as the Rust service evolves.
> Last sync: 2026-02-19

---

## Service Location & Config

```
Repo:        /Users/pointcoexpedro/Dev/btc-oracle-proxy
Language:    Rust (tokio async runtime)
WS default:  ws://127.0.0.1:8080/ws          (env: WS_LISTEN_ADDR)
Tick rate:   ~500ms                           (env: AGGREGATION_INTERVAL_MS=500)
```

**Key env vars (from .env):**
```
POLYGON_RPC_URL=https://polygon-mainnet.infura.io/v3/<key>   # Infura Polygon
POLYGON_API_KEY=<key>
WS_LISTEN_ADDR=127.0.0.1:8080
AGGREGATION_INTERVAL_MS=500
PRICE_HISTORY_CAPACITY=1000   # rolling window for indicator calc
```

**Chainlink contract (hardcoded in src/aggregator/chainlink.rs):**
```
Contract:  0xc907E116054Ad103354f2D350FD2514433D57F6F
Network:   Polygon mainnet
Decimals:  8  (answer / 1e8 = USD price)
ABI call:  latestRoundData() selector 0xfeaf968c
Poll:      every 5 seconds
```
> Note: The .env has a stale `CHAINLINK_FEED_ADDRESS` entry — it is never read.
> The contract address is hardcoded in the Rust source. Don't be confused by it.

---

## Signal Detection Thresholds (src/aggregator/calculator.rs)

```
ROUND_TRIGGER_THRESHOLD  = 0.10%   abs(deviation) >= this → round_triggered
APPROACH_THRESHOLD       = 0.07%   abs(deviation) >= this → deviation_approach
PRE_TRIGGER_DEV_THRESHOLD = 0.05%  min deviation for pre_trigger_alert to fire
BB_SQUEEZE_THRESHOLD     = 0.30%   bb_width_pct < this → bands are "squeezed"
```

**pre_trigger_alert fires when:** deviation >= 0.05% AND ≥2 of these 4 signals:
1. `bb_breakout`        — price outside Bollinger Band in deviation direction
2. `deviation_approach` — abs(dev) >= 0.07%
3. `momentum_surge`     — ROC-10 > 0.02% aligned with deviation direction
4. `rsi_extreme`        — RSI > 65 (UP) or < 35 (DOWN) aligned with direction

**bb_breakout rising-edge logic:** fires when price ENTERS outside bands (was
inside previous tick, now outside). Does NOT re-fire while price stays outside.
Resets when price returns inside bands.

---

## Exact WS Event Schemas

Subscribe message (client → server):
```json
{"type": "subscribe", "channels": ["BTC/USD"]}
```

All events share envelope: `{"type": "...", "ts": "<ISO-8601 UTC>", "data": {...}}`

### tick (~500ms, always)
```json
{
  "type": "tick",
  "ts": "2026-02-19T15:06:03.669Z",
  "data": {
    "timestamp": "2026-02-19T15:06:03.669Z",
    "symbol": "BTC/USD",
    "market_price": 66511.60,
    "chainlink_price": 66598.68,        // null until first Chainlink poll succeeds
    "chainlink_age_secs": 13,           // null until first poll
    "deviation_pct": -0.131,            // null until chainlink_price available
    "round_imminent": true,             // false until chainlink_price available
    "exchange_prices": {
      "binance": 66508.00,
      "coinbase": 66514.00,
      "kraken": 66511.60
    },
    "indicators": {
      "ema_12": 66490.00,    // null if < 12 history points
      "ema_26": 66450.00,    // null if < 26 history points
      "ema_50": 66320.00,    // null if < 50 history points
      "rsi_14": 44.2,        // null if < 14 points
      "momentum_10": -0.14,  // null if < 10 points  (field name in JSON)
      "momentum_20": -0.28,  // null if < 20 points
      "volatility": 82.5,    // std dev, null if < 20 points
      "bb_upper": 66764.00,  // null if < 20 points
      "bb_middle": 66511.00, // null if < 20 points  (SMA-20)
      "bb_lower": 66258.00,  // null if < 20 points
      "macd": 40.00,         // null if < 26 points
      "macd_signal": 35.00,  // approximate — EMA(9) of MACD line
      "macd_histogram": 5.00
    }
  }
}
```
> IMPORTANT: `momentum_10` / `momentum_20` are the JSON field names for ROC-10/ROC-20.
> The Rust struct fields are named `momentum_10` / `momentum_20`.

### pre_trigger_alert (rising edge, highest conviction)
```json
{
  "type": "pre_trigger_alert",
  "ts": "2026-02-19T15:05:55.500Z",
  "data": {
    "direction": "DOWN",                // "UP" or "DOWN"
    "signals": ["bb_breakout", "momentum_surge"],
    "deviation_pct": -0.062,
    "market_price": 66557.00,
    "chainlink_price": 66598.68,
    "chainlink_age_secs": 5,
    "bb_width_pct": 0.165,             // null if BB not yet computed
    "rsi_14": 38.2,                    // null if RSI not yet computed
    "momentum_10": -0.042              // null if ROC not yet computed
  }
}
```

### round_triggered (rising edge, 0.10% crossed)
```json
{
  "type": "round_triggered",
  "ts": "...",
  "data": {
    "direction": "DOWN",
    "market_price": 66511.60,
    "chainlink_price": 66598.68,
    "chainlink_age_secs": 13,
    "deviation_pct": -0.131,
    "exchange_prices": {"binance": 66508.00, "coinbase": 66514.00, "kraken": 66511.60}
  }
}
```

### round_settled (new on-chain price committed)
```json
{
  "type": "round_settled",
  "ts": "...",
  "data": {
    "prev_price": 66598.68,
    "new_price": 66511.00,
    "price_delta": -87.68,
    "delta_pct": -0.132,
    "round_duration_secs": 44          // null if no round_triggered preceded it
                                       // (happens on heartbeat settlements)
  }
}
```
> `round_duration_secs` being null = heartbeat settlement, NOT a signal event.
> This is a critical filter for the researcher — only signal-triggered settlements
> have a tradeable window.

### deviation_approach (rising edge, 0.07% zone)
```json
{
  "type": "deviation_approach",
  "ts": "...",
  "data": {
    "direction": "DOWN",
    "deviation_pct": -0.073,
    "market_price": 66549.00,
    "chainlink_price": 66598.68,
    "chainlink_age_secs": 8
  }
}
```

### bb_breakout (rising edge, price exits bands)
```json
{
  "type": "bb_breakout",
  "ts": "...",
  "data": {
    "direction": "DOWN",
    "market_price": 66560.00,
    "bb_upper": 66680.00,
    "bb_lower": 66570.00,
    "bb_width_pct": 0.165,
    "bb_expanding": true,              // width > prev_width
    "deviation_pct": -0.058           // null possible if no CL baseline yet
  }
}
```

### exchange_status (connect/disconnect)
```json
{
  "type": "exchange_status",
  "ts": "...",
  "exchange": "binance",
  "status": "connected",
  "error": null
}
```
> Note: `exchange_status` embeds fields at top level, NOT inside a `data` key.
> All other events have `data`. Handle both patterns in the consumer.

---

## Indicator Computation Notes

- All indicators are computed from `price_history: Vec<f64>` (in-memory ring buffer)
- History is capped at `PRICE_HISTORY_CAPACITY=1000` points (~8.3 min at 500ms)
- Indicators return `null` until sufficient history accumulates:
  - EMA-12: needs 12 points (~6s)
  - EMA-26: needs 26 points (~13s)
  - RSI-14: needs 14 points (~7s)
  - BB/StdDev: needs 20 points (~10s)
  - EMA-50: needs 50 points (~25s)
  - MACD signal line: **approximate** — uses EMA(9) of a computed MACD line
    rather than a true running EMA of historical MACD values. This means
    `macd_signal` and `macd_histogram` are slightly inaccurate, especially
    at short history lengths. Do not rely on `macd_histogram` for high-precision
    signal detection. `macd` (the line itself) is accurate.

---

## Known Quirks & Gotchas

1. **Startup warm-up period:** On first connect, the first ~50 ticks will have
   `null` indicators. The first tick with all indicators populated is when
   `ema_50` becomes non-null. The researcher should discard events where
   `ema_50 IS NULL` for indicator-based features.

2. **Chainlink poll lag:** Chainlink baseline is polled every 5s from Infura.
   There is up to 5s lag between a new on-chain commit and a `round_settled`
   event. The actual oracle-to-settlement latency in real Polymarket resolution
   is therefore `round_duration_secs` + up to 5s.

3. **Heartbeat settlements:** Chainlink BTC/USD heartbeat is up to 24h.
   If the baseline is unchanged for a very long time and then a heartbeat fires,
   a `round_settled` event will emit with `round_duration_secs: null` and a
   very small `delta_pct` (essentially 0). These are noise — filter with:
   `WHERE round_duration_secs IS NOT NULL OR abs(delta_pct) > 0.05`

4. **Exchange name casing:** Exchange prices use lowercase keys in JSON:
   `"binance"`, `"coinbase"`, `"kraken"`. The researcher schema should
   store them as-is.

5. **Direction enum serialisation:** `"UP"` and `"DOWN"` (uppercase strings).
   Not booleans. Not "up"/"down".

6. **MACD signal is approximate:** See indicator notes above. If MACD signal
   line quality matters for a model, compute it properly in Python from the
   raw `ema_12` / `ema_26` sequence in `oracle_ticks`.

7. **BB bands use 2σ:** `bb_upper = sma_20 + 2 * std_dev`, `bb_lower = sma_20 - 2 * std_dev`.
   Standard Bollinger Band configuration.

8. **pre_trigger_alert does NOT contain full exchange prices.** Only
   `market_price`, `chainlink_price`, and a few indicator values. For full
   exchange breakdown at alert time, join with the nearest `oracle_ticks` row
   by timestamp.

---

## Connecting from Python (test_websocket.py)

```python
# File: tests/test_websocket.py (in btc-oracle-proxy repo)
# Run: python tests/test_websocket.py
# Options: --no-ticks (signal events only)
#          --ticks-only (verbose tick stream)
#          ws://custom:port/ws
```

The researcher's `oracle_consumer.py` should be modelled on this file.
Key difference: researcher writes to DuckDB instead of printing.

```python
# Minimal consumer skeleton for researcher
import asyncio, json, duckdb, websockets

async def consume(uri="ws://127.0.0.1:8080/ws"):
    db = duckdb.connect("researcher.db")
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "subscribe", "channels": ["BTC/USD"]}))
        async for raw in ws:
            ev = json.loads(raw)
            t = ev["type"]
            if t == "tick":
                d = ev["data"]
                db.execute("INSERT INTO oracle_ticks VALUES (?, ?, ?, ...)", [...])
            elif t in ("pre_trigger_alert", "round_triggered", "round_settled",
                       "deviation_approach", "bb_breakout"):
                db.execute("INSERT INTO signal_events VALUES (...)", [...])
```

---

## Architecture Decisions Made (and why)

| Decision | Rationale |
|---|---|
| No HTTP server | Personal use — TUI is the dashboard, WS is the feed |
| broadcast channel not pending_events Vec | Vec would only work for single consumer; broadcast gives each WS client its own receiver without holding the state write lock |
| Lock released before broadcast | `drop(s)` before `event_tx.send()` — prevents holding write lock during potentially slow channel sends |
| Rising-edge semantics for all signals | Bot receives ONE alert per state transition, not a flood every 500ms while the condition holds. Each signal must reset (price returns inside BB, deviation drops below threshold) before firing again |
| Heartbeat detection via `round_duration_secs: null` | Chainlink heartbeats emit `round_settled` with no preceding `round_triggered`. This null signals "not a deviation event" to the researcher |
| No persistence in oracle service | Clean separation — oracle service is latency-critical, persistence is researcher's concern |
| MACD signal approximate | Computing proper EMA(9) of MACD history would require O(n²) over price history per tick. Good enough for TUI display; researcher should recompute properly from stored ticks |

---

## File Map (btc-oracle-proxy)

```
src/
├── main.rs                   entry point, wires broadcast channel + atomics
├── config.rs                 env var loading, all defaults
├── models.rs                 ALL event structs + WsEvent enum — source of truth for schema
├── error.rs                  OracleError enum
├── aggregator/
│   ├── mod.rs                main aggregation loop, ALL 5 signal detectors
│   ├── calculator.rs         pure detection fns + thresholds (no state)
│   ├── chainlink.rs          Polygon RPC poller (latestRoundData ABI decode)
│   └── exchange_client.rs    Binance/Coinbase/Kraken WS clients
├── indicators/
│   ├── mod.rs                calculate_all_indicators() — single call site
│   ├── ema.rs                EMA (used by MACD too)
│   ├── momentum.rs           RSI, ROC
│   ├── volatility.rs         StdDev, Bollinger Bands
│   └── composite.rs          MACD (approximate signal line)
├── ws_server/
│   ├── mod.rs                TCP accept loop, per-connection select! handler
│   ├── broadcast.rs          type aliases: EventSender, EventReceiver
│   └── handler.rs            inbound WsClientMessage handler (subscribe stub)
└── tui/
    ├── mod.rs                full TUI: 4-panel layout, 3-column price section
    └── log_layer.rs          custom tracing::Layer → LogBuffer for TUI footer

tests/
└── test_websocket.py         Python WS smoke test / live monitor
```

---

**Last Updated:** 2026-02-19
**btc-oracle-proxy status:** WS server live, all 5 signal types emitting, TUI running
**polymarket-researcher status:** Not started — begin with Phase 1 (data collection)
