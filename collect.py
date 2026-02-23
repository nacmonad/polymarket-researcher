"""Main collection entrypoint.

Runs all collectors concurrently:
  - Oracle WebSocket consumer (btc-oracle-proxy feed)
  - Polymarket market discovery (Gamma API poller)
  - Polymarket CLOB consumer (order book WS)
  - Outcome recorder (resolution poller)

Also serves a Rich live dashboard showing:
  - Oracle tick rate and latest signal
  - Active PM markets count
  - DB row counts
  - Consumer health

Usage:
  python collect.py               # with dashboard
  python collect.py --no-dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from collector.oracle_consumer import OracleConsumer, metrics as oracle_metrics
from collector.outcome_recorder import OutcomeRecorder
from collector.pm_clob_consumer import ClobConsumer
from collector.pm_market_discovery import MarketDiscovery
from db.connection import get_db
from db.migrate import apply as migrate

load_dotenv()

log = logging.getLogger(__name__)
console = Console()


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _db_counts() -> dict[str, int]:
    try:
        db = get_db()
        ticks = db.execute("SELECT COUNT(*) FROM oracle_ticks").fetchone()[0]
        signals = db.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0]
        markets = db.execute("SELECT COUNT(*) FROM pm_markets").fetchone()[0]
        snapshots = db.execute("SELECT COUNT(*) FROM pm_snapshots").fetchone()[0]
        resolved = db.execute(
            "SELECT COUNT(*) FROM pm_markets WHERE resolved_at IS NOT NULL"
        ).fetchone()[0]
        return {
            "ticks": ticks,
            "signals": signals,
            "markets": markets,
            "snapshots": snapshots,
            "resolved": resolved,
        }
    except Exception:
        return {}


def _make_dashboard() -> Layout:
    counts = _db_counts()
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    # ── Oracle panel ──
    oracle = Table.grid(padding=(0, 1))
    oracle.add_column(style="dim")
    oracle.add_column()

    status_color = "green" if oracle_metrics["connected"] else "red"
    oracle.add_row("Status", Text("● LIVE" if oracle_metrics["connected"] else "● OFFLINE", style=status_color))
    oracle.add_row("Ticks received", str(oracle_metrics["ticks_received"]))
    oracle.add_row("Ticks flushed", str(oracle_metrics["ticks_flushed"]))
    oracle.add_row("Signals received", str(oracle_metrics["signals_received"]))
    oracle.add_row("Gaps detected", str(oracle_metrics["gaps_detected"]))
    oracle.add_row("Reconnects", str(oracle_metrics["reconnects"]))
    last_sig = oracle_metrics["last_signal_type"] or "—"
    last_ts = (oracle_metrics["last_signal_ts"] or "—")[:19]
    oracle.add_row("Last signal", f"{last_sig}  {last_ts}")

    # ── DB panel ──
    db_table = Table.grid(padding=(0, 1))
    db_table.add_column(style="dim")
    db_table.add_column()
    db_table.add_row("oracle_ticks", f"{counts.get('ticks', '?'):,}")
    db_table.add_row("signal_events", f"{counts.get('signals', '?'):,}")
    db_table.add_row("pm_markets", f"{counts.get('markets', '?'):,}  ({counts.get('resolved', '?')} resolved)")
    db_table.add_row("pm_snapshots", f"{counts.get('snapshots', '?'):,}")

    layout = Layout()
    layout.split_row(
        Layout(Panel(oracle, title="Oracle Feed"), name="oracle"),
        Layout(Panel(db_table, title=f"DuckDB  —  {now}"), name="db"),
    )
    return layout


# ── Shutdown ──────────────────────────────────────────────────────────────────

async def run_all(dashboard: bool = True) -> None:
    # Ensure schema is current
    migrate(verbose=False)
    log.info("Schema applied")

    new_market_queue: asyncio.Queue = asyncio.Queue()

    oracle = OracleConsumer()
    discovery = MarketDiscovery(new_market_queue=new_market_queue)
    clob = ClobConsumer(new_market_queue=new_market_queue)
    outcomes = OutcomeRecorder()

    tasks = [
        asyncio.create_task(oracle.run(), name="oracle"),
        asyncio.create_task(discovery.run(), name="discovery"),
        asyncio.create_task(clob.run(), name="clob"),
        asyncio.create_task(outcomes.run(), name="outcomes"),
    ]

    def _cancel_all() -> None:
        log.info("Shutdown signal — cancelling tasks")
        for t in tasks:
            t.cancel()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, _cancel_all)
    loop.add_signal_handler(signal.SIGTERM, _cancel_all)

    if dashboard:
        async def dashboard_loop() -> None:
            with Live(console=console, refresh_per_second=1, screen=False) as live:
                while any(not t.done() for t in tasks):
                    live.update(_make_dashboard())
                    await asyncio.sleep(1)

        tasks.append(asyncio.create_task(dashboard_loop(), name="dashboard"))

    console.print("[bold green]polymarket-researcher[/] starting…")
    console.print(f"  Oracle WS : [cyan]{os.getenv('ORACLE_WS_URL', 'ws://127.0.0.1:8080/ws')}[/]")
    console.print(f"  DB path   : [cyan]{os.getenv('DB_PATH', './data/researcher.db')}[/]")
    console.print(f"  Assets    : [cyan]{os.getenv('COLLECT_ASSETS', 'BTC')}[/]")
    console.print()

    await asyncio.gather(*tasks, return_exceptions=True)

    # Flush any remaining oracle tick buffer before exit
    oracle._flush_ticks()
    console.print("[yellow]Collectors stopped.[/]")


def main() -> None:
    parser = argparse.ArgumentParser(description="polymarket-researcher collector")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable live dashboard")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    asyncio.run(run_all(dashboard=not args.no_dashboard))


if __name__ == "__main__":
    main()
