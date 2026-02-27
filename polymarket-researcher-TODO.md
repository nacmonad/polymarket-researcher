# polymarket-researcher — Implementation Roadmap

**Goal:** Collect real-time oracle signals and Polymarket CLOB data, persist them
in DuckDB, backtest impulse-derived risk-neutral strategies against historical
outcomes, then progress to calibrated ML models and live execution.

**Oracle feed source:** `btc-oracle-proxy` WebSocket at `ws://127.0.0.1:8080/ws`
(see that repo's PLAN.md → "btc-oracle-proxy Implementation Memory" for full
event schemas, quirks, and connection details)

**Stack:** Python 3.12+, DuckDB, asyncio, websockets, pandas/polars, xgboost

---

## PHASE 0 — Project Setup

- [ ] `pyproject.toml` with uv / poetry, Python 3.12+
- [ ] Core dependencies pinned:
  - `duckdb >= 1.0`
  - `websockets >= 13`
  - `httpx` — async HTTP for Polymarket APIs
  - `pandas >= 2` + `pyarrow` — DuckDB interchange
  - `pydantic >= 2` — event model validation
  - `python-dotenv`
  - `rich` — CLI dashboards / logging
  - `py-clob-client` — official Polymarket CLOB SDK
- [ ] ML extras (optional install group):
  - `xgboost`, `scikit-learn`, `optuna`
  - `torch` (LSTM/Transformer, Phase 4+)
  - `stable-baselines3` (RL, Phase 5+)
- [ ] `.env.example` with all required vars (see env vars section below)
- [ ] `Makefile` targets: `collect`, `backtest`, `dashboard`, `paper-trade`
- [ ] `README.md` — architecture diagram, quickstart, signal → trade flow

### Required env vars
```
# Oracle proxy
ORACLE_WS_URL=ws://127.0.0.1:8080/ws

# Polymarket
POLYMARKET_GAMMA_API=https://gamma-api.polymarket.com
POLYMARKET_CLOB_API=https://clob.polymarket.com
POLYMARKET_WS_URL=wss://ws-subscriptions.polymarket.com/ws/

# Optional — for live execution only
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
POLYMARKET_WALLET_PRIVATE_KEY=

# Database
DB_PATH=./data/researcher.db

# Collection behaviour
COLLECT_ASSETS=BTC,ETH,SOL,XRP   # comma-separated
COLLECT_TIMEFRAMES=5m,15m         # which PM resolution windows to track
ORACLE_TICK_BATCH_SIZE=50         # flush to DB every N ticks
SIGNAL_BUFFER_SECS=300            # seconds of signal context to keep in memory
```

---

## PHASE 1 — DuckDB Schema

- [ ] `db/schema.sql` — single source of truth, applied via `db/migrate.py`
- [ ] Table: `oracle_ticks`
  ```sql
  CREATE TABLE oracle_ticks (
      ts                   TIMESTAMPTZ NOT NULL,
      symbol               VARCHAR     NOT NULL,  -- 'BTC/USD'
      market_price         DOUBLE,
      chainlink_price      DOUBLE,
      chainlink_age_secs   INTEGER,
      deviation_pct        DOUBLE,
      round_imminent       BOOLEAN,
      -- per-exchange
      price_binance        DOUBLE,
      price_coinbase       DOUBLE,
      price_kraken         DOUBLE,
      -- indicators (all nullable until warm-up)
      ema_12               DOUBLE,
      ema_26               DOUBLE,
      ema_50               DOUBLE,
      rsi_14               DOUBLE,
      momentum_10          DOUBLE,
      momentum_20          DOUBLE,
      volatility           DOUBLE,
      bb_upper             DOUBLE,
      bb_middle            DOUBLE,
      bb_lower             DOUBLE,
      macd                 DOUBLE,
      macd_signal          DOUBLE,
      macd_histogram       DOUBLE,
      PRIMARY KEY (ts, symbol)
  );
  ```
- [ ] Table: `signal_events`
  ```sql
  CREATE TABLE signal_events (
      id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
      ts            TIMESTAMPTZ NOT NULL,
      event_type    VARCHAR     NOT NULL,  -- pre_trigger_alert | round_triggered |
                                           -- round_settled | deviation_approach |
                                           -- bb_breakout | exchange_status
      symbol        VARCHAR     NOT NULL,
      direction     VARCHAR,               -- 'UP' | 'DOWN' | NULL (for settled/status)
      deviation_pct DOUBLE,
      market_price  DOUBLE,
      chainlink_price DOUBLE,
      chainlink_age_secs INTEGER,
      -- round_settled specific
      prev_price    DOUBLE,
      new_price     DOUBLE,
      price_delta   DOUBLE,
      delta_pct     DOUBLE,
      round_duration_secs INTEGER,         -- NULL = heartbeat settlement (skip)
      -- pre_trigger_alert specific
      signals_json  JSON,                  -- ["bb_breakout", "momentum_surge"]
      bb_width_pct  DOUBLE,
      rsi_14        DOUBLE,
      momentum_10   DOUBLE,
      -- bb_breakout specific
      bb_upper      DOUBLE,
      bb_lower      DOUBLE,
      bb_expanding  BOOLEAN,
      -- raw full event payload for anything not modelled above
      raw_json      JSON
  );
  CREATE INDEX idx_signal_events_ts   ON signal_events (ts);
  CREATE INDEX idx_signal_events_type ON signal_events (event_type, ts);
  ```
- [ ] Table: `pm_markets`
  ```sql
  CREATE TABLE pm_markets (
      condition_id      VARCHAR PRIMARY KEY,
      question          VARCHAR NOT NULL,
      asset             VARCHAR NOT NULL,    -- 'BTC', 'ETH', 'SOL', 'XRP'
      timeframe         VARCHAR,             -- '5m', '15m', '1hr', '4hr', '1d' (parsed)
      direction         VARCHAR,             -- 'UP' | 'DOWN' (parsed from question)
      close_time        TIMESTAMPTZ,
      resolved_at       TIMESTAMPTZ,
      outcome           VARCHAR,             -- 'YES' | 'NO' | NULL
      winning_price     DOUBLE,              -- Chainlink settlement price
      token_yes_id      VARCHAR,
      token_no_id       VARCHAR,
      created_at        TIMESTAMPTZ DEFAULT now(),
      last_updated      TIMESTAMPTZ
  );
  ```
- [ ] Table: `pm_snapshots`
  ```sql
  CREATE TABLE pm_snapshots (
      ts                TIMESTAMPTZ NOT NULL,
      condition_id      VARCHAR     NOT NULL,
      token_id          VARCHAR     NOT NULL,  -- YES or NO token
      best_bid          DOUBLE,
      best_ask          DOUBLE,
      mid_price         DOUBLE,
      spread            DOUBLE,
      bid_depth_1       DOUBLE,               -- $ depth at best bid
      ask_depth_1       DOUBLE,               -- $ depth at best ask
      last_trade_price  DOUBLE,
      last_trade_size   DOUBLE,
      PRIMARY KEY (ts, token_id)
  );
  CREATE INDEX idx_pm_snapshots_cond ON pm_snapshots (condition_id, ts);
  ```
- [ ] Table: `pm_trades`
  ```sql
  CREATE TABLE pm_trades (
      id               VARCHAR PRIMARY KEY,
      ts               TIMESTAMPTZ NOT NULL,
      condition_id     VARCHAR NOT NULL,
      token_id         VARCHAR NOT NULL,
      side             VARCHAR,    -- 'BUY' | 'SELL'
      price            DOUBLE,
      size             DOUBLE,
      taker_side       VARCHAR
  );
  ```
- [ ] View: `signal_outcomes` (research query shortcut)
  ```sql
  CREATE VIEW signal_outcomes AS
  SELECT
      se.id, se.ts, se.event_type, se.symbol, se.direction,
      se.deviation_pct, se.market_price, se.chainlink_price,
      se.round_duration_secs,
      pm.condition_id, pm.timeframe, pm.close_time,
      pm.outcome, pm.winning_price,
      pm.close_time - se.ts AS time_to_resolution,
      -- price at nearest tick after signal
      ot.market_price AS tick_price_at_signal
  FROM signal_events se
  LEFT JOIN pm_markets pm
      ON pm.asset = SPLIT_PART(se.symbol, '/', 1)
      AND pm.close_time BETWEEN se.ts AND se.ts + INTERVAL '30 minutes'
  LEFT JOIN oracle_ticks ot
      ON ot.symbol = se.symbol
      AND ot.ts = (
          SELECT ts FROM oracle_ticks
          WHERE symbol = se.symbol AND ts >= se.ts
          ORDER BY ts LIMIT 1
      )
  WHERE se.event_type NOT IN ('exchange_status')
    AND se.round_duration_secs IS NOT NULL
       OR se.event_type IN ('pre_trigger_alert', 'bb_breakout', 'deviation_approach');
  ```
- [ ] `db/migrate.py` — idempotent schema apply + version tracking
- [ ] `db/connection.py` — singleton DuckDB connection manager (thread-safe)

---

## PHASE 2 — Data Collection

### 2a — Oracle Consumer

- [ ] `collector/oracle_consumer.py`
  - Connect to `ORACLE_WS_URL` via `websockets`
  - Send subscribe: `{"type": "subscribe", "channels": ["BTC/USD"]}`
  - Validate inbound messages against pydantic models (see PLAN.md schemas)
  - Handle both event envelope patterns:
    - Standard: `{type, ts, data: {...}}`
    - `exchange_status`: `{type, ts, exchange, status, error}` (NO `data` key)
  - Buffer ticks in memory, flush to `oracle_ticks` every `ORACLE_TICK_BATCH_SIZE` rows
  - Insert signal events immediately (no buffering) into `signal_events`
  - Skip `round_settled` rows where `round_duration_secs IS NULL` (heartbeats)
  - Auto-reconnect with exponential backoff (1s, 2s, 4s … max 60s)
  - Track gap metrics: log a warning when `ts` gap > 2s between ticks
  - Graceful shutdown on SIGINT / SIGTERM (flush buffer before exit)

### 2b — Polymarket Market Discovery

- [ ] `collector/pm_market_discovery.py`
  - Poll Polymarket Gamma API every 60s:
    `GET /markets?closed=false&tag=crypto`
  - Parse and filter for configured assets (BTC, ETH, SOL, XRP) and timeframes
  - Question parsing regex for timeframe/direction detection:
    - `5m`, `15m`, `1 hour`, `4 hour`, `1 day` → normalise to `5m/15m/1hr/4hr/1d`
    - `above`, `higher`, `up` → `UP`; `below`, `lower`, `down` → `DOWN`
  - Upsert into `pm_markets` (condition_id is PK)
  - Log newly discovered markets with time-to-close
  - Subscribe newly discovered markets to CLOB WS (notify `pm_clob_consumer`)
  - [ ] Parse `close_time` from market data correctly (UTC, ISO-8601)
  - [ ] Store `token_yes_id` / `token_no_id` for order book lookup

### 2c — Polymarket CLOB Consumer

- [ ] `collector/pm_clob_consumer.py`
  - WebSocket: `wss://ws-subscriptions.polymarket.com/ws/`
  - Subscribe to book channels for all active market tokens
  - Maintain in-memory order book per token (bid/ask level 1+2)
  - Snapshot to `pm_snapshots` every 500ms (aligned with oracle tick rate)
  - Store individual trades to `pm_trades` on fill events
  - Handle subscription management: add new markets hot (no restart)
  - Reconnect logic (same as oracle consumer)
  - [ ] Research: Polymarket WS auth requirements (may need API key for CLOB WS)

### 2d — Outcome Recorder

- [ ] `collector/outcome_recorder.py`
  - Poll `pm_markets` for rows where `resolved_at IS NULL AND close_time < now()`
  - Fetch resolution from Gamma API: `GET /markets/{condition_id}`
  - Update `pm_markets.outcome`, `pm_markets.winning_price`, `pm_markets.resolved_at`
  - Run every 30s
  - Log resolution events for audit

### 2e — Collection Orchestrator

- [ ] `collect.py` — entrypoint, runs all consumers concurrently via `asyncio.gather`
- [ ] Health check endpoint (simple HTTP, port 9090): `/health` → JSON with
  consumer statuses, tick rate, last event timestamps, DB row counts
- [ ] `rich` live dashboard showing: oracle tick rate, active PM markets, DB sizes

---

## PHASE 3 — Data Quality & Validation

- [ ] `scripts/data_quality.py`
  - Check gap frequency in `oracle_ticks` (flag any > 5s gaps)
  - Check `signal_events` completeness (verify pre_trigger always precedes round_triggered within 30s)
  - Check `pm_snapshots` coverage (all active markets should have snapshots)
  - Report collection uptime % for last 24h / 7d
- [ ] Minimum collection period before backtesting: **7 days continuous**
- [ ] Document known data gaps with reason (maintenance, network, etc.)

---

## PHASE 3.5 — Collection Optimizations & Historical Backfill

### Performance Insights (2026-02-24 Benchmark Results)

**Key Finding**: polymarket-cli historical data provides 1-minute granularity via
`clob price-history --interval 1m`, which is finer than our live collection
(currently 500ms oracle ticks, but Polymarket snapshots may be rate-limited).

### Optimization Strategy: Sub-Second Sampling + Periodic Backfill

**Current State**:
- `btc-oracle-proxy`: Queries every **500ms** (2 samples/minute)
- `pm_clob_consumer`: Snapshots order book every **500ms** (aligned with oracle)
- polymarket-cli historical: Returns **1-minute** granularity (60 samples/hour)

**Problem**: Risk of 429 rate limits if we increase snapshot frequency beyond 500ms.

**Proposed Solution**:

1. **Increase live sampling frequency** (avoid 429s):
   - Query at **250ms, 500ms, and 750ms** intervals (4 samples/minute)
   - This gives 4x more granular data than CLI historical (1 sample/minute)
   - Stagger requests to distribute load across the second
   
2. **Periodic historical backfill** (gap filling):
   - Run `polymarket clob price-history --interval 1m` daily (e.g., 3:00 AM UTC)
   - Backfill any gaps in `pm_snapshots` from last 24h
   - Use CLI data as ground truth for validation
   - Detect and log discrepancies between live vs historical data
   
3. **Hybrid approach for backtesting**:
   - **Live data** (250/500/750ms): Used for real-time strategy execution
   - **Historical data** (1m via CLI): Used for gap-free backtesting over weeks/months
   - Merge both sources in `signal_outcomes` view with conflict resolution

### Implementation Tasks

- [ ] `collector/pm_clob_consumer.py` — Add configurable snapshot intervals
  - New env var: `PM_SNAPSHOT_INTERVALS=250,500,750` (milliseconds, comma-separated)
  - Stagger requests to avoid rate limits (e.g., offset by 83ms each)
  - Monitor 429 responses, implement exponential backoff per interval
  - [ ] Add rate limit metrics to dashboard (429 count, backoff state)

- [ ] `collector/historical_backfill.py` — CLI-based gap filler
  - Query `pm_markets` for active markets from last 24h
  - For each market, fetch 1m history via `polymarket clob price-history`
  - Upsert into `pm_snapshots` (merge with live data, preserve higher-resolution samples)
  - Conflict resolution: If live data exists at same timestamp, keep live data (it's more granular)
  - Log backfilled rows, gap coverage %
  - [ ] Run as cron job or integrated into collector (e.g., every 6 hours)

- [ ] `scripts/data_quality.py` — Add historical vs live comparison
  - Compare live snapshots against CLI historical for same time windows
  - Flag discrepancies > 1% price deviation
  - Calculate completeness: `(live_samples + backfilled) / expected_samples`
  - Target: ≥99% coverage after backfill

- [ ] `db/schema.sql` — Add `pm_snapshots.source` column
  - Track data source: `'live'` (from CLOB WS) vs `'backfill'` (from CLI history)
  - Allows filtering/validation queries
  - Add index: `CREATE INDEX idx_pm_snapshots_source ON pm_snapshots (source, ts);`

### Benefits

1. **Higher resolution live data**: 4 samples/minute (vs 2 currently) without hitting rate limits
2. **Gap-free historical coverage**: CLI backfill ensures no missed windows
3. **Validation**: Compare live vs historical to detect collection issues
4. **Reduced dependency on live collector**: Historical CLI data can bootstrap backtests
5. **Flexibility**: Can switch to CLI-only if live collection proves unreliable

### Performance Comparison (Latency Benchmarks)

From `perf-tests` results:
- **Python httpx live**: 19.96ms mean latency (winner for real-time)
- **polymarket-cli subprocess**: 142.66ms (7x slower, but acceptable for batch backfill)
- **CLI historical data**: 244ms for 30 days of 1m data (excellent for backtesting)

**Decision**: Keep Python httpx for live collection, use CLI for historical backfill.

### Open Questions

- [ ] What's the actual Polymarket rate limit? (Test with 250ms intervals)
- [ ] Do we need all 4 samples/minute, or is 1m backfill sufficient?
- [ ] Should backfill run continuously (e.g., every hour) or daily batch?
- [ ] How to handle timezone alignment between live (UTC) and CLI data?

---

## PHASE 3.6 — L2 Order Book Data Collection

### Analysis Summary (2026-02-24)

**Discovery**: polymarket-cli `clob book` returns **full L2 order book** (20-100 price levels),
but we're currently only storing **L1** (best bid/ask). See `../perf-tests/L2_DATA_COMPARISON.md`
for comprehensive analysis.

**What We're Missing**:
- Multi-level depth (only have top-of-book)
- Order book imbalance signals
- Accurate slippage estimation for larger orders
- Liquidity scoring and market microstructure
- Spoofing/manipulation detection

**Strategic Impact**: L2 data unlocks:
- Slippage-aware order sizing
- Liquidity-based entry timing
- Order book imbalance prediction
- Volume-weighted mid price (VWMP)
- Price impact estimation

### Implementation Options

**Option 1: Store Full L2** (comprehensive, ~100 MB/day/10 tokens)  
**Option 2: Store Aggregate Metrics** (lightweight, +10% storage)  
**Option 3: Hybrid** (recommended) — aggregate metrics + periodic full snapshots

### Schema Changes

#### Add Aggregate L2 Metrics to `pm_snapshots` (Quick Win)

```sql
ALTER TABLE pm_snapshots ADD COLUMN IF NOT EXISTS
    -- Depth metrics (cumulative $ at multiple levels)
    bid_depth_5          DOUBLE,
    ask_depth_5          DOUBLE,
    bid_depth_10         DOUBLE,
    ask_depth_10         DOUBLE,
    
    -- Order book imbalance
    depth_imbalance_5    DOUBLE,  -- (bid_5 - ask_5) / (bid_5 + ask_5)
    depth_imbalance_10   DOUBLE,
    
    -- Spread at depth
    spread_pct_5         DOUBLE,  -- Spread between L5 bid and L5 ask
    
    -- Estimated slippage for market orders
    slippage_100         DOUBLE,  -- $ slippage for $100 order
    slippage_1000        DOUBLE,  -- $ slippage for $1000 order
    
    -- Book metadata
    total_ask_levels     INTEGER,
    total_bid_levels     INTEGER,
    book_timestamp       BIGINT;  -- Unix timestamp from CLOB API
```

#### New Table: `pm_order_book_levels` (Full L2)

For periodic full snapshots (every 5 minutes):

```sql
CREATE TABLE pm_order_book_levels (
    ts                   TIMESTAMPTZ NOT NULL,
    token_id             VARCHAR     NOT NULL,
    side                 VARCHAR     NOT NULL,  -- 'BID' | 'ASK'
    level                INTEGER     NOT NULL,  -- 1, 2, 3, ...
    price                DOUBLE      NOT NULL,
    size                 DOUBLE      NOT NULL,
    cumulative_size      DOUBLE,                -- Running sum to this level
    PRIMARY KEY (ts, token_id, side, level)
);

CREATE INDEX idx_ob_levels_token_ts ON pm_order_book_levels (token_id, ts);
```

#### View: `pm_book_metrics` (Pre-computed L2 Analytics)

```sql
CREATE VIEW pm_book_metrics AS
SELECT
    ts,
    token_id,
    (best_ask - best_bid) / best_bid AS spread_pct,
    bid_depth_5 / ask_depth_5 AS depth_ratio_5,
    depth_imbalance_10 AS market_pressure,
    CASE
        WHEN bid_depth_10 + ask_depth_10 > 5000 THEN 1.0
        WHEN bid_depth_10 + ask_depth_10 > 1000 THEN 0.5
        ELSE 0.2
    END AS liquidity_score
FROM pm_snapshots
WHERE source = 'live';
```

### Collection Tasks

- [ ] **Phase 1: Aggregate L2 Metrics** (1-2 days, lightweight)
  - [ ] Update `db/schema.sql` with new `pm_snapshots` columns
  - [ ] Implement L2 metrics computation in `pm_clob_consumer.py`:
    - If WS provides L2 → compute from WS messages
    - If WS provides L1 only → poll `polymarket clob book` every minute
  - [ ] Add `compute_l2_metrics(order_book)` helper function:
    ```python
    def compute_l2_metrics(book, levels=[5, 10]):
        """Compute aggregate L2 metrics from order book."""
        metrics = {}
        
        for n in levels:
            bid_depth = sum(float(b['size']) * float(b['price']) 
                          for b in book['bids'][:n])
            ask_depth = sum(float(a['size']) * float(a['price']) 
                          for a in book['asks'][:n])
            
            metrics[f'bid_depth_{n}'] = bid_depth
            metrics[f'ask_depth_{n}'] = ask_depth
            metrics[f'depth_imbalance_{n}'] = (
                (bid_depth - ask_depth) / (bid_depth + ask_depth + 1e-9)
            )
        
        # Estimate slippage for $100, $1000 market orders
        metrics['slippage_100'] = estimate_slippage(book['asks'], 100)
        metrics['slippage_1000'] = estimate_slippage(book['asks'], 1000)
        
        return metrics
    ```
  - [ ] Test with live WS feed or CLI polling (whichever is available)
  - [ ] Backfill last 7 days with CLI book queries (script: `scripts/backfill_l2.py`)

- [ ] **Phase 2: Full L2 Storage** (3-4 days, comprehensive)
  - [ ] Create `pm_order_book_levels` table
  - [ ] Implement L2 level-by-level collection:
    - Store full book every 5 minutes (balance storage vs granularity)
    - Or: store on significant book changes (>5% depth shift at L1-L5)
  - [ ] Add partition by date: `PARTITION BY DATE_TRUNC('day', ts)`
  - [ ] Monitor storage growth, set retention policy (e.g., 90 days)

- [ ] **Phase 3: L2-Based Strategy Components** (1-2 weeks)
  - [ ] `backtest/l2_signals.py` — Order book imbalance indicators
  - [ ] `backtest/slippage.py` — Slippage estimation from L2
  - [ ] `backtest/liquidity.py` — Liquidity scoring and filtering
  - [ ] `backtest/vwmp.py` — Volume-weighted mid price calculation
  - [ ] Integration with existing strategy framework

### L2 Collection Strategy: Hybrid Approach (Recommended)

1. **Real-time aggregate metrics**: Store in `pm_snapshots` every snapshot (250/500/750ms)
   - Low overhead (~10% storage increase)
   - Sufficient for most strategies (imbalance, liquidity, slippage)

2. **Periodic full L2**: Store in `pm_order_book_levels` every 5 minutes
   - Validation and detailed analysis
   - Reconstruct book state for debugging
   - ~100 MB/day for 10 active tokens

3. **Historical price backfill**: Use CLI historical (no L2, but fills gaps)
   - Run daily at 3:00 AM UTC
   - Covers gaps from downtime/rate limits

### Rate Limit Considerations

- [ ] Test `polymarket clob book` polling rate
  - Target: 1 query/minute per token (should be safe)
  - Monitor 429 responses, implement exponential backoff
  - If hit limits: reduce to 1 query/2 minutes or use WS if available

- [ ] Alternative: Check if Polymarket WebSocket provides L2
  - Inspect WS message format from `pm_clob_consumer.py`
  - If WS has L2 → no polling needed (real-time L2 for free!)
  - If WS has L1 only → hybrid: WS for L1 + CLI polling for L2 metrics

### Storage Cost Estimates

**Aggregate L2 only** (Phase 1):
- +8 columns per snapshot
- ~20% size increase per row
- 4 snapshots/min × 60 min × 24 hr = 5,760 snapshots/day/token
- For 10 tokens: ~57,600 rows/day
- Size: ~5 MB/day (negligible)

**Full L2** (Phase 2):
- ~50 levels per snapshot (25 bids, 25 asks)
- 1 full snapshot every 5 minutes = 288 snapshots/day/token
- For 10 tokens: 288 × 50 × 10 = 144,000 rows/day
- Size: ~100 MB/day

**Total with hybrid**: ~105 MB/day (manageable)

### New Strategies Unlocked

1. **Slippage-Aware Sizing**
   - Walk L2 book to calculate exact fill price
   - Adjust order size to stay within slippage threshold
   - Example: If $1000 order has >2% slippage, reduce to $500

2. **Liquidity-Based Entry Timing**
   - Only enter when spread < 1% AND depth > $1000 at L1-L5
   - Avoids illiquid windows with high slippage

3. **Order Book Imbalance Prediction**
   - Predict short-term price movement from bid/ask depth ratio
   - Entry signal: imbalance > 0.2 (more bids → bullish)

4. **VWMP Pricing**
   - Use volume-weighted mid price instead of simple mid
   - More accurate for large orders

5. **Spoofing Detection**
   - Track large orders that appear/cancel without filling
   - Avoid entering when book is manipulated

### Open Questions

- [ ] Does Polymarket WS provide L2 data? (Check `pm_clob_consumer.py` message format)
- [ ] What's the CLI book query rate limit? (Test with 1 query/min)
- [ ] Should we store top 5, 10, or full book levels?
- [ ] Is 5-minute full L2 snapshot frequency sufficient?
- [ ] Should we backfill historical L2? (Not possible via CLI, only going forward)

### References

- Full analysis: `../perf-tests/L2_DATA_COMPARISON.md`
- CLI book command: `polymarket -o json clob book {token_id}`
- Example L2 response: 64 ask levels, 21 bid levels (see comparison doc)

---

## PHASE 4 — Backtesting Framework

### 4a — Data Loader

- [ ] `backtest/loader.py`
  - `load_signals(asset, start, end, types)` → DataFrame from `signal_events`
  - `load_ticks(asset, start, end)` → DataFrame from `oracle_ticks`
  - `load_market_snapshots(condition_id, start, end)` → DataFrame from `pm_snapshots`
  - `load_outcomes(asset, start, end)` → DataFrame from `signal_outcomes` view
  - All queries via DuckDB, return pandas or polars (configurable)

### 4b — Signal–Market Alignment

- [ ] `backtest/aligner.py`
  - For each signal event, find all PM markets with `close_time` in [signal_ts, signal_ts + 30min]
  - Match direction: signal direction UP → look at YES token price
  - Compute:
    - `entry_price` — best_ask at signal time (what you'd pay to enter)
    - `exit_price` — YES/NO token price at round_settled time
    - `final_price` — 1.0 (YES won) or 0.0 (YES lost)
    - `time_to_settle` — from signal to round_settled
  - Store as `AlignedTrade` dataclass for strategy input

### 4c — Strategy Simulator

- [ ] `backtest/simulator.py`
  - Event-driven: iterate aligned trades chronologically
  - Position tracker: max `MAX_CONCURRENT_POSITIONS` (from config)
  - Per-trade P&L: `(exit_price - entry_price) * size` for directional leg
  - Kelly-inspired sizing: `f = (edge * odds) / (odds - 1)` capped at max bet
  - Support multi-leg positions (directional + hedge)
  - Commission model: Polymarket takes 2% on winnings
  - Output: trade log DataFrame, equity curve, per-signal-type breakdown

### 4d — Metrics & Reporting

- [ ] `backtest/metrics.py`
  - Win rate per signal type
  - Expected value (EV) per signal
  - Sharpe ratio on equity curve
  - Max drawdown
  - Signal utilisation rate (signals with a tradeable market / total signals)
  - Direction accuracy: UP signal → YES outcome rate
  - Slippage estimate: `actual_fill - best_ask_at_signal_time`
- [ ] `backtest/report.py` — HTML report with `plotly` charts

---

## PHASE 5 — Strategy Implementations

> See PLAN.md § Strategy Taxonomy for full rationale.
> Implement each as a class inheriting `BaseStrategy(backtest/simulator.py)`.

### Strategy A — Directional Impulse (baseline)

- [ ] Entry trigger: `round_triggered` event
- [ ] Trade: Buy YES (UP) or NO (DOWN) token at market
- [ ] Exit: Hold to resolution (no early exit)
- [ ] Sizing: flat $10 per trade for baseline
- [ ] Expected edge: ~60-65% win rate based on "price moved far enough to trigger
  a round, so it probably stays there through settlement"

### Strategy B — Impulse + Hedge (risk-neutral target)

- [ ] Phase 1: On `pre_trigger_alert`, buy directional leg (e.g. YES at 0.55)
- [ ] Phase 2: On `round_triggered`, buy hedge leg (NO at ≤0.45)
- [ ] Cost basis: `phase1_ask + phase2_ask ≤ 0.98` (profitable regardless of outcome)
- [ ] Guard: skip Phase 2 if NO ask > 0.45 (can't achieve risk-neutral cost)
- [ ] Exit: Hold both legs to resolution (collect $1.00 total, minus commissions)
- [ ] This is the priority strategy — backtest to validate cost basis is achievable

### Strategy C — Sideways / Squeeze Capture

- [ ] Entry trigger: `bb_breakout` with `bb_expanding: false` (squeeze then breakout)
  AND `round_imminent: false` (not already in deviation territory)
- [ ] Trade: Buy directional token based on breakout direction
- [ ] Exit: Close at `bb_middle` reversion OR at 15m timeout
- [ ] This captures non-oracle-triggered momentum moves in PM markets

### Strategy D — Settlement Fade

- [ ] Entry trigger: `round_settled` with large `delta_pct` (> 0.15%)
- [ ] Hypothesis: large fast moves sometimes overshoot; price reverts
- [ ] Trade: Buy AGAINST the settlement direction (counter-trend)
- [ ] Only viable in markets with > 5 min remaining after settlement
- [ ] Smallest expected EV — validate before running live

---

## PHASE 6 — ML Pipeline

> Only begin after ≥ 7 days data collected and baseline strategies validated.
> See PLAN.md § ML Roadmap.

### 6a — Feature Engineering

- [ ] `ml/features.py`
  - Per-signal features: `deviation_pct`, `chainlink_age_secs`, `rsi_14`,
    `macd_histogram`, `bb_width_pct`, `momentum_10`, `signals` (one-hot),
    `time_since_last_settled`, `num_signals_last_5m`
  - Per-market features: `time_to_close`, `entry_price_yes`, `spread`,
    `bid_depth_1`, `ask_depth_1`
  - Market regime features: `ema_50_slope` (rolling 10-tick), `volatility_z_score`
  - Label: `outcome` (1 = directional leg won)
- [ ] Train/test split: **time-based** (not random) — last 20% of data = test
- [ ] Feature importance analysis before model training

### 6b — Calibration Model (XGBoost)

- [ ] `ml/calibration.py`
  - Target: predict P(outcome == YES given direction) for a given signal
  - Model: `XGBClassifier` with `use_label_encoder=False, eval_metric='logloss'`
  - Hyperparameter tuning: `optuna` with 50 trials
  - Validation: calibration curve (reliability diagram) — model should be well-calibrated
  - Output: `P_win` per signal, used to scale Kelly bet size
  - Retrain weekly as new data accumulates

### 6c — Signal Quality Scorer

- [ ] `ml/signal_scorer.py`
  - Binary classifier: "is this signal worth acting on?"
  - Filter out low-confidence signals before execution
  - Based on XGBoost calibration output + threshold (e.g., only act if P_win > 0.60)

### 6d — LSTM / Transformer (stretch goal, Phase 4+)

- [ ] `ml/sequence_model.py`
  - Input: rolling 60-tick window of oracle tick features
  - Architecture: 2-layer LSTM or Transformer encoder
  - Task: predict direction and magnitude of next oracle round
  - Only worth pursuing if XGBoost calibration shows non-linear temporal patterns
  - Minimum data requirement: 30 days

### 6e — RL Position Sizer (stretch goal, Phase 5+)

- [ ] `ml/rl_sizer.py`
  - Framework: `stable-baselines3` PPO
  - Observation: current P_win, Kelly fraction, equity, open positions
  - Action: discrete bet sizes (0, $5, $10, $20, $50)
  - Reward: risk-adjusted P&L
  - Only pursue after XGBoost results are positive

---

## PHASE 7 — Paper Trading

- [ ] `paper_trader/engine.py`
  - Subscribe to live oracle WS feed
  - Apply strategy logic in real-time (same code as backtest, different data source)
  - Simulate order fills using live CLOB bid/ask (no actual orders placed)
  - Track paper P&L vs backtest expectations
  - Log all decisions with reasoning (which signals fired, which thresholds triggered)
- [ ] Run paper trading for minimum **2 weeks** before live
- [ ] Compare paper results to backtest: flag any regime changes

---

## PHASE 8 — Live Execution

- [ ] `executor/clob_client.py` — wraps `py-clob-client`
  - Place limit orders (NOT market orders — control slippage)
  - Cancel stale orders if market moved before fill
  - Track open positions and P&L
- [ ] `executor/risk_manager.py`
  - Max concurrent open positions: 3
  - Max single bet: `min(kelly_f * bankroll, $50)`
  - Daily loss limit: $200 hard stop (halt all trading)
  - Position-level stop: close if unrealised loss > 60% of entry cost
- [ ] `executor/order_monitor.py` — poll fill status, update position tracker
- [ ] `executor/live_engine.py` — real-time loop: signal → score → size → execute
- [ ] Dry-run flag: `LIVE_TRADING=false` runs executor in no-op mode by default
- [ ] Audit log: every action (signal received, order attempted, fill confirmed, error)
  written to `execution_audit.jsonl`

---

## MULTI-ASSET EXTENSION

Start BTC-only. Extend after BTC baseline is validated.

- [ ] ETH/USD Chainlink feed: `0xF9680D99D6C9589e2a93a78A04A279e509205945` (Polygon)
- [ ] SOL/USD Chainlink feed: `0x10C8264C0935b3B9870013e057f330Ff3e9C56dC` (Polygon)
- [ ] XRP/USD Chainlink feed: `0x785ba89291f676b5386652eB12b30cF361020694` (Polygon)
- [ ] btc-oracle-proxy will need to be extended to aggregate multi-asset feeds
  (each asset needs its own exchange WS subscription + Chainlink poller)
- [ ] `COLLECT_ASSETS` env var already provisioned in schema above
- [ ] Correlation analysis: do BTC/ETH signals fire simultaneously?
  If yes, avoid doubling up on correlated directional positions

---

## OUTPUT SCHEMA (data flowing INTO researcher)

> Full schemas in btc-oracle-proxy PLAN.md § "Exact WS Event Schemas".
> Summary of critical fields:

| Event | Key fields for researcher |
|---|---|
| `tick` | `market_price`, `chainlink_price`, `deviation_pct`, `round_imminent`, all indicators |
| `pre_trigger_alert` | `direction`, `signals[]`, `deviation_pct`, `bb_width_pct`, `rsi_14` |
| `round_triggered` | `direction`, `deviation_pct`, `chainlink_age_secs`, `exchange_prices` |
| `round_settled` | `prev_price`, `new_price`, `delta_pct`, `round_duration_secs` |
| `deviation_approach` | `direction`, `deviation_pct`, `chainlink_age_secs` |
| `bb_breakout` | `direction`, `bb_width_pct`, `bb_expanding`, `deviation_pct` |

**Critical filters:**
- `round_settled` where `round_duration_secs IS NULL` = heartbeat, skip for strategy
- Ticks where `ema_50 IS NULL` = warm-up period, skip for indicator features
- `bb_breakout` where `bb_expanding = false` = squeeze scenario (Strategy C)
- `exchange_status` events: log only, not tradeable signals

---

## KNOWN RISKS & MITIGATIONS

1. **Latency:** Oracle signal → Polymarket CLOB fill has network round-trip.
   Target < 2s total. Monitor slippage: if `actual_fill > best_ask_at_signal + 0.05`, signal quality is degrading.

2. **Market availability:** ~40-60% of signals will have no tradeable PM market
   resolving in the right window. Tracked by `signal_utilisation_rate` metric.

3. **Heartbeat settlements:** ~5% of `round_settled` events have no preceding
   trigger. Filter: `round_duration_secs IS NOT NULL`. Already in schema.

4. **MACD signal approximation:** btc-oracle-proxy MACD signal line is approximate.
   For ML features, recompute properly from `ema_12` / `ema_26` stored in `oracle_ticks`.

5. **Thin liquidity on PM:** Strategy B (risk-neutral hedge) requires both YES
   and NO ask prices to be ≤ 0.49. Check `ask_depth_1 > 500` (USD) before entering.

6. **Data gap on startup:** Indicators need 50+ ticks to warm up (~25s after
   oracle connects). Don't collect signal events during this window.
   Filter: `WHERE ema_50 IS NOT NULL` in all feature queries.

---

## RELATED REPOS

| Repo | Role |
|---|---|
| `btc-oracle-proxy` (Rust) | Real-time signal generator — the feed source |
| `polymarket-researcher` (Python) | **This project** — collect, backtest, execute |

**btc-oracle-proxy must be running before researcher can collect data.**
Check oracle health: `wscat -c ws://127.0.0.1:8080/ws` or `python tests/test_websocket.py`

---

**Status:** Pre-collection
**Priority order:** Phase 0 → 1 → 2a → 2b → 2c → 2d → 3 → 4 → 5B (risk-neutral)
**Last updated:** 2026-02-19
