-- polymarket-researcher DuckDB schema
-- Apply via: python -m db.migrate
-- All TIMESTAMPTZ stored as UTC.

-- ── Oracle feed ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS oracle_ticks (
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
    -- indicators (all nullable until warm-up completes ~25s)
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

-- ── Signal events (rising-edge only, sparse) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS signal_events (
    id                   VARCHAR     NOT NULL DEFAULT gen_random_uuid(),
    ts                   TIMESTAMPTZ NOT NULL,
    event_type           VARCHAR     NOT NULL,
    -- pre_trigger_alert | round_triggered | round_settled
    -- deviation_approach | bb_breakout | exchange_status
    symbol               VARCHAR     NOT NULL,
    direction            VARCHAR,               -- 'UP' | 'DOWN' | NULL
    deviation_pct        DOUBLE,
    market_price         DOUBLE,
    chainlink_price      DOUBLE,
    chainlink_age_secs   INTEGER,
    -- round_settled specific
    prev_price           DOUBLE,
    new_price            DOUBLE,
    price_delta          DOUBLE,
    delta_pct            DOUBLE,
    round_duration_secs  INTEGER,               -- NULL = heartbeat settlement
    -- pre_trigger_alert specific
    signals_json         VARCHAR,               -- JSON array: ["bb_breakout", ...]
    bb_width_pct         DOUBLE,
    rsi_14               DOUBLE,
    momentum_10          DOUBLE,
    -- bb_breakout specific
    bb_upper             DOUBLE,
    bb_lower             DOUBLE,
    bb_expanding         BOOLEAN,
    -- raw full payload
    raw_json             VARCHAR,               -- JSON string
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_signal_events_ts   ON signal_events (ts);
CREATE INDEX IF NOT EXISTS idx_signal_events_type ON signal_events (event_type, ts);

-- ── Polymarket markets (static metadata) ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS pm_markets (
    condition_id         VARCHAR     PRIMARY KEY,
    question             VARCHAR     NOT NULL,
    asset                VARCHAR     NOT NULL,   -- 'BTC', 'ETH', 'SOL', 'XRP'
    timeframe            VARCHAR,                -- '5m', '15m', '1hr', '4hr', '1d'
    direction            VARCHAR,                -- 'UP' | 'DOWN' | NULL (derived)
    close_time           TIMESTAMPTZ,
    resolved_at          TIMESTAMPTZ,
    outcome              VARCHAR,                -- 'YES' | 'NO' | NULL
    winning_price        DOUBLE,
    token_yes_id         VARCHAR,
    token_no_id          VARCHAR,
    created_at           TIMESTAMPTZ DEFAULT now(),
    last_updated         TIMESTAMPTZ
);

-- ── Polymarket CLOB snapshots ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pm_snapshots (
    ts                   TIMESTAMPTZ NOT NULL,
    condition_id         VARCHAR     NOT NULL,
    token_id             VARCHAR     NOT NULL,   -- YES or NO token
    best_bid             DOUBLE,
    best_ask             DOUBLE,
    mid_price            DOUBLE,
    spread               DOUBLE,
    bid_depth_1          DOUBLE,                 -- $ size at best bid
    ask_depth_1          DOUBLE,                 -- $ size at best ask
    last_trade_price     DOUBLE,
    last_trade_size      DOUBLE,
    PRIMARY KEY (ts, token_id)
);

CREATE INDEX IF NOT EXISTS idx_pm_snapshots_cond ON pm_snapshots (condition_id, ts);

-- ── Polymarket trades (fills from WS) ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pm_trades (
    id                   VARCHAR     PRIMARY KEY,
    ts                   TIMESTAMPTZ NOT NULL,
    condition_id         VARCHAR     NOT NULL,
    token_id             VARCHAR     NOT NULL,
    side                 VARCHAR,                -- 'BUY' | 'SELL'
    price                DOUBLE,
    size                 DOUBLE,
    taker_side           VARCHAR
);

-- ── Signal outcomes view (core research query) ───────────────────────────────
--
-- pm_markets stores Up token as token_yes_id, Down token as token_no_id.
-- Outcomes are 'UP' or 'DOWN' (from Gamma API resolved outcome field, uppercased).
--
-- Snapshot join strategy:
--   Primary  – ASOF JOIN finds the latest snapshot at or before the signal time
--              (efficient O(log n) per row, no correlated subqueries).
--   Fallback – if no prior snapshot exists (cold-start / backfilled markets),
--              use the earliest snapshot within +60s of signal time.
--   snap_lag_secs: seconds between snapshot and signal (negative = snapshot came
--                  after signal, i.e. cold-start fallback was used). NULL = no
--                  snapshot found at all for that market.

CREATE OR REPLACE VIEW signal_outcomes AS
WITH
-- ASOF JOIN: latest snapshot at or before signal time, per (condition_id, token_id)
-- We pre-join with pm_markets to get token IDs, then ASOF join snapshots.
base AS (
    -- One row per signal × nearest-resolving market (prevents cartesian explosion).
    -- We want the single market whose close_time is closest to (and after) the signal,
    -- within a 30-minute look-ahead window.  Using DISTINCT ON ordered by close_time ASC
    -- picks the minimum close_time per signal.
    SELECT DISTINCT ON (se.id)
        se.id,
        se.ts,
        se.event_type,
        se.symbol,
        se.direction,
        se.deviation_pct,
        se.market_price,
        se.chainlink_price,
        se.chainlink_age_secs,
        se.round_duration_secs,
        se.bb_width_pct,
        se.rsi_14,
        se.momentum_10,
        pm.condition_id,
        pm.timeframe,
        pm.close_time,
        pm.outcome,
        pm.winning_price,
        pm.token_yes_id,
        pm.token_no_id,
        DATEDIFF('second', se.ts, pm.close_time) AS secs_to_resolution
    FROM signal_events se
    LEFT JOIN pm_markets pm
        ON pm.asset = SPLIT_PART(se.symbol, '/', 1)
        AND pm.close_time > se.ts
        AND pm.close_time <= se.ts + INTERVAL '30 minutes'
    WHERE se.event_type NOT IN ('exchange_status')
      AND (
          se.round_duration_secs IS NOT NULL
          OR se.event_type IN ('pre_trigger_alert', 'bb_breakout', 'deviation_approach')
      )
    ORDER BY se.id, pm.close_time ASC
),
-- Nearest snapshot before or at signal (ASOF semantics via correlated max)
snap_pre AS (
    SELECT DISTINCT ON (b.id, b.token_yes_id)
        b.id        AS signal_id,
        'up'        AS side,
        s.ts        AS snap_ts,
        s.best_ask,
        s.best_bid,
        s.spread,
        s.bid_depth_1,
        s.ask_depth_1
    FROM base b
    JOIN pm_snapshots s
        ON s.condition_id = b.condition_id
        AND s.token_id    = b.token_yes_id
        AND s.ts         <= b.ts
    ORDER BY b.id, b.token_yes_id, s.ts DESC
),
snap_pre_dn AS (
    SELECT DISTINCT ON (b.id, b.token_no_id)
        b.id        AS signal_id,
        s.ts        AS snap_ts,
        s.best_ask,
        s.best_bid
    FROM base b
    JOIN pm_snapshots s
        ON s.condition_id = b.condition_id
        AND s.token_id    = b.token_no_id
        AND s.ts         <= b.ts
    ORDER BY b.id, b.token_no_id, s.ts DESC
),
-- Cold-start fallback: earliest snapshot within +60s if no prior snap exists
snap_post AS (
    SELECT DISTINCT ON (b.id, b.token_yes_id)
        b.id        AS signal_id,
        'up'        AS side,
        s.ts        AS snap_ts,
        s.best_ask,
        s.best_bid,
        s.spread,
        s.bid_depth_1,
        s.ask_depth_1
    FROM base b
    LEFT JOIN snap_pre sp ON sp.signal_id = b.id
    JOIN pm_snapshots s
        ON s.condition_id = b.condition_id
        AND s.token_id    = b.token_yes_id
        AND s.ts BETWEEN b.ts AND b.ts + INTERVAL '60 seconds'
    WHERE sp.signal_id IS NULL
    ORDER BY b.id, b.token_yes_id, s.ts ASC
),
snap_post_dn AS (
    SELECT DISTINCT ON (b.id, b.token_no_id)
        b.id        AS signal_id,
        s.ts        AS snap_ts,
        s.best_ask,
        s.best_bid
    FROM base b
    LEFT JOIN snap_pre_dn spd ON spd.signal_id = b.id
    JOIN pm_snapshots s
        ON s.condition_id = b.condition_id
        AND s.token_id    = b.token_no_id
        AND s.ts BETWEEN b.ts AND b.ts + INTERVAL '60 seconds'
    WHERE spd.signal_id IS NULL
    ORDER BY b.id, b.token_no_id, s.ts ASC
),
-- Merge primary + fallback
snap_up AS (
    SELECT * FROM snap_pre
    UNION ALL
    SELECT * FROM snap_post
),
snap_dn AS (
    SELECT * FROM snap_pre_dn
    UNION ALL
    SELECT * FROM snap_post_dn
)
SELECT
    b.id,
    b.ts,
    b.event_type,
    b.symbol,
    b.direction,
    b.deviation_pct,
    b.market_price,
    b.chainlink_price,
    b.chainlink_age_secs,
    b.round_duration_secs,
    b.bb_width_pct,
    b.rsi_14,
    b.momentum_10,
    b.condition_id,
    b.timeframe,
    b.close_time,
    b.outcome,
    b.winning_price,
    b.secs_to_resolution,
    -- Up token snapshot
    su.best_ask                                   AS up_ask_at_signal,
    su.best_bid                                   AS up_bid_at_signal,
    su.spread                                     AS spread_at_signal,
    su.bid_depth_1                                AS up_bid_depth,
    su.ask_depth_1                                AS up_ask_depth,
    -- Down token snapshot
    sd.best_ask                                   AS down_ask_at_signal,
    sd.best_bid                                   AS down_bid_at_signal,
    -- Snapshot freshness (negative = cold-start fallback used)
    DATEDIFF('second', su.snap_ts, b.ts)          AS snap_lag_secs,
    -- P&L if you bought Up at signal time
    CASE WHEN b.outcome = 'UP'   THEN 1.0 - su.best_ask
         WHEN b.outcome = 'DOWN' THEN       -su.best_ask
         ELSE NULL
    END                                           AS up_pnl,
    -- P&L if you bought Down at signal time
    CASE WHEN b.outcome = 'DOWN' THEN 1.0 - sd.best_ask
         WHEN b.outcome = 'UP'   THEN       -sd.best_ask
         ELSE NULL
    END                                           AS down_pnl,
    -- P&L for the side aligned with signal direction
    CASE
        WHEN b.direction = 'UP'   AND b.outcome = 'UP'   THEN 1.0 - su.best_ask
        WHEN b.direction = 'UP'   AND b.outcome = 'DOWN' THEN       -su.best_ask
        WHEN b.direction = 'DOWN' AND b.outcome = 'DOWN' THEN 1.0 - sd.best_ask
        WHEN b.direction = 'DOWN' AND b.outcome = 'UP'   THEN       -sd.best_ask
        ELSE NULL
    END                                           AS directional_pnl
FROM base b
LEFT JOIN snap_up su ON su.signal_id = b.id
LEFT JOIN snap_dn sd ON sd.signal_id = b.id;
