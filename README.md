# polymarket-researcher

Oracle-impulse data collector, backtester, and strategy executor for Polymarket BTC markets.

Subscribes to the [`btc-oracle-proxy`](../btc-oracle-proxy) WebSocket feed, collects
Polymarket CLOB order book snapshots, persists everything to DuckDB, and provides
backtesting infrastructure for oracle-derived edge strategies.

---

## How it works

```
btc-oracle-proxy (Rust)
    │  ws://127.0.0.1:8080/ws
    │  tick (~500ms), pre_trigger_alert, round_triggered,
    │  round_settled, deviation_approach, bb_breakout
    ▼
oracle_consumer.py ──────────────────────────────► oracle_ticks
                                                    signal_events
                                                         │
Polymarket Gamma API                                     │
    │  (poll every 60s)                                  │
    ▼                                                     │
pm_market_discovery.py ─────────────────────────► pm_markets
    │  new condition_ids                                  │
    ▼                                                     │
pm_clob_consumer.py  ───────────────────────────► pm_snapshots
    │  WS order book (500ms snapshots)                   │  pm_trades
    │                                                     │
outcome_recorder.py  ───────────────────────────► pm_markets.outcome
    │  (poll resolved markets every 30s)                  │
                                                          ▼
                                              signal_outcomes VIEW
                                        (entry price + label + P&L)
                                                          │
                                                          ▼
                                              backtest / ML / paper trade
```

### The edge

Polymarket 5m/15m BTC markets settle against the Chainlink on-chain price at a specific
timestamp. `btc-oracle-proxy` detects when a Chainlink update is imminent — **before the
market reprices**. The window is 5–45 seconds. You enter at stale odds, Chainlink commits,
market resolves in your favour.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+ | Tested on 3.14 |
| [`btc-oracle-proxy`](../btc-oracle-proxy) running | Oracle WS must be live before collecting |
| uv | `brew install uv` |

---

## Setup

```bash
# 1. Copy env config
cp .env.example .env

# 2. Install dependencies into shared venv
make install

# 3. Apply DuckDB schema
make migrate
```

**`.env` — key vars to check:**

```bash
ORACLE_WS_URL=ws://127.0.0.1:8080/ws   # btc-oracle-proxy address
DB_PATH=./data/researcher.db            # DuckDB file location
COLLECT_ASSETS=BTC                      # comma-separated: BTC,ETH,SOL,XRP
COLLECT_TIMEFRAMES=5m,15m               # PM markets to track
```

---

## Running

### Start the oracle proxy first

```bash
cd ../btc-oracle-proxy
cargo run --release
```

Verify it's live:
```bash
wscat -c ws://127.0.0.1:8080/ws
# or
python ../btc-oracle-proxy/tests/test_websocket.py --no-ticks
```

### Start data collection

```bash
make collect
# or directly:
python collect.py
```

This starts four concurrent collectors:

| Collector | What it does |
|---|---|
| `oracle_consumer` | Streams oracle WS, buffers ticks (flush every 50), writes signals immediately |
| `pm_market_discovery` | Polls Gamma API every 60s, upserts new markets, notifies CLOB consumer |
| `pm_clob_consumer` | Subscribes to Polymarket order book WS, snapshots L1 bid/ask every 500ms |
| `outcome_recorder` | Polls for resolved markets every 30s, writes outcome + winning price |

The Rich live dashboard shows oracle connection status, tick/signal counts, and DB row counts.

**Without dashboard (log output only):**
```bash
python collect.py --no-dashboard
python collect.py --no-dashboard --log-level DEBUG
```

**Background / persistent session:**
```bash
# tmux (recommended)
tmux new -s researcher
make collect
# detach: Ctrl+B D

# or nohup
nohup python collect.py --no-dashboard > logs/collect.log 2>&1 &
echo $! > .collect.pid
```

---

## Shutdown

**Graceful (flushes tick buffer before exit):**

```bash
# If running in foreground:
Ctrl+C

# If running via nohup / background:
kill -SIGTERM $(cat .collect.pid)
# or
kill -SIGINT $(cat .collect.pid)
```

Both `SIGINT` and `SIGTERM` trigger a clean shutdown — the oracle consumer flushes any
buffered ticks to DuckDB before exiting. Do **not** use `SIGKILL` (`kill -9`) unless
the process is hung, as it bypasses the flush.

**Verify clean shutdown:**
```bash
# Check the WAL file is gone (merged back into main DB on clean exit)
ls data/
# Should show only: researcher.db   (no researcher.db.wal)
```

If `researcher.db.wal` is present after shutdown, DuckDB will automatically recover
it on next open — no data is lost, but clean shutdown is preferred.

---

## Data quality checks

After collecting for a while:

```bash
make data-quality
# or
python scripts/data_quality.py --hours 24
```

Reports:
- Tick gap frequency (flags > 5s gaps)
- Signal sequence integrity (pre_trigger → round_triggered within 60s)
- Snapshot coverage per active market
- Collection uptime % for the window

---

## Querying collected data

```python
import duckdb
db = duckdb.connect("./data/researcher.db")

# How many signals?
db.sql("SELECT event_type, COUNT(*) FROM signal_events GROUP BY 1").show()

# Signal outcomes with entry price and P&L
db.sql("SELECT * FROM signal_outcomes WHERE outcome IS NOT NULL LIMIT 20").show()

# Tick rate sanity check
db.sql("""
    SELECT DATE_TRUNC('minute', ts) AS minute, COUNT(*) AS ticks
    FROM oracle_ticks
    WHERE ts > NOW() - INTERVAL '1 hour'
    GROUP BY 1 ORDER BY 1
""").show()
```

---

## Backtesting (Phase 4 — after 7 days data)

```bash
make backtest
```

Target: strategy v1 (directional impulse on `round_triggered`) win rate > 58%.
The `signal_outcomes` view provides labelled entry/exit data for all resolved markets.

---

## Project layout

```
polymarket-researcher/
├── collect.py                  entrypoint — runs all collectors
├── db/
│   ├── schema.sql              DuckDB schema (source of truth)
│   ├── migrate.py              idempotent schema apply
│   └── connection.py           singleton connection + write_lock
├── collector/
│   ├── models.py               pydantic v2 models for oracle WS events
│   ├── oracle_consumer.py      oracle WS → oracle_ticks + signal_events
│   ├── pm_market_discovery.py  Gamma API → pm_markets
│   ├── pm_clob_consumer.py     CLOB WS → pm_snapshots + pm_trades
│   └── outcome_recorder.py     resolution polling → pm_markets.outcome
├── backtest/                   (Phase 4)
├── ml/                         (Phase 6)
├── paper_trader/               (Phase 7)
├── executor/                   (Phase 8)
└── scripts/
    └── data_quality.py         gap analysis, uptime, coverage checks
```

---

## Related

| Repo | Role |
|---|---|
| `btc-oracle-proxy` (Rust) | Real-time signal generator — the feed source |
| `polymarket-researcher` (Python) | **This repo** — collect, backtest, execute |

See `PLAN.md` for full strategy taxonomy, ML roadmap, and architecture decisions.
See `polymarket-researcher-TODO.md` for phase-by-phase implementation checklist.
