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
-- P&L: if signal direction == outcome direction we bought the winning side.
--   up_pnl  = P&L for buying the Up  token at up_ask_at_signal
--   down_pnl = P&L for buying the Down token at down_ask_at_signal

CREATE OR REPLACE VIEW signal_outcomes AS
SELECT
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
    DATEDIFF('second', se.ts, pm.close_time)  AS secs_to_resolution,
    -- Up token (token_yes_id) snapshot nearest to signal time
    snap_up.best_ask                          AS up_ask_at_signal,
    snap_up.best_bid                          AS up_bid_at_signal,
    snap_up.spread                            AS spread_at_signal,
    snap_up.bid_depth_1                       AS up_bid_depth,
    snap_up.ask_depth_1                       AS up_ask_depth,
    -- Down token (token_no_id) snapshot nearest to signal time
    snap_dn.best_ask                          AS down_ask_at_signal,
    snap_dn.best_bid                          AS down_bid_at_signal,
    -- P&L if you bought Up at signal time
    CASE WHEN pm.outcome = 'UP'   THEN 1.0 - snap_up.best_ask
         WHEN pm.outcome = 'DOWN' THEN -snap_up.best_ask
         ELSE NULL
    END                                       AS up_pnl,
    -- P&L if you bought Down at signal time
    CASE WHEN pm.outcome = 'DOWN' THEN 1.0 - snap_dn.best_ask
         WHEN pm.outcome = 'UP'   THEN -snap_dn.best_ask
         ELSE NULL
    END                                       AS down_pnl,
    -- P&L for the side aligned with the signal direction
    CASE
        WHEN se.direction = 'UP'   AND pm.outcome = 'UP'   THEN 1.0 - snap_up.best_ask
        WHEN se.direction = 'UP'   AND pm.outcome = 'DOWN' THEN -snap_up.best_ask
        WHEN se.direction = 'DOWN' AND pm.outcome = 'DOWN' THEN 1.0 - snap_dn.best_ask
        WHEN se.direction = 'DOWN' AND pm.outcome = 'UP'   THEN -snap_dn.best_ask
        ELSE NULL
    END                                       AS directional_pnl,
    -- nearest oracle tick at signal time for context
    ot.market_price                           AS tick_price_at_signal
FROM signal_events se
-- PM market resolving within 30 minutes of signal
LEFT JOIN pm_markets pm
    ON pm.asset = SPLIT_PART(se.symbol, '/', 1)
    AND pm.close_time BETWEEN se.ts AND se.ts + INTERVAL '30 minutes'
-- Up token snapshot at signal time
LEFT JOIN pm_snapshots snap_up
    ON snap_up.condition_id = pm.condition_id
    AND snap_up.token_id = pm.token_yes_id
    AND snap_up.ts = (
        SELECT MAX(ts) FROM pm_snapshots s2
        WHERE s2.condition_id = pm.condition_id
          AND s2.ts <= se.ts
          AND s2.token_id = pm.token_yes_id
    )
-- Down token snapshot at signal time
LEFT JOIN pm_snapshots snap_dn
    ON snap_dn.condition_id = pm.condition_id
    AND snap_dn.token_id = pm.token_no_id
    AND snap_dn.ts = (
        SELECT MAX(ts) FROM pm_snapshots s2
        WHERE s2.condition_id = pm.condition_id
          AND s2.ts <= se.ts
          AND s2.token_id = pm.token_no_id
    )
-- nearest oracle tick
LEFT JOIN oracle_ticks ot
    ON ot.symbol = se.symbol
    AND ot.ts = (
        SELECT ts FROM oracle_ticks o2
        WHERE o2.symbol = se.symbol AND o2.ts >= se.ts
        ORDER BY ts LIMIT 1
    )
-- only actionable signals
WHERE se.event_type NOT IN ('exchange_status')
  AND (
    se.round_duration_secs IS NOT NULL
    OR se.event_type IN ('pre_trigger_alert', 'bb_breakout', 'deviation_approach')
  );
