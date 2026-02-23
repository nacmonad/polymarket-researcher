"""Data quality checks for collected data.

Checks:
  1. Tick gap frequency in oracle_ticks (flag > 5s gaps)
  2. Signal sequence integrity (pre_trigger → round_triggered within 30s)
  3. pm_snapshots coverage per active market
  4. Collection uptime % for last 24h / 7d
  5. Signal event counts by type

Usage: python scripts/data_quality.py [--hours 24]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.table import Table

from db.connection import get_db
from db.migrate import apply as migrate

console = Console()


def check_tick_gaps(db, hours: int) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = db.execute(
        """
        WITH gaps AS (
            SELECT
                ts,
                LAG(ts) OVER (PARTITION BY symbol ORDER BY ts) AS prev_ts,
                DATEDIFF('millisecond', LAG(ts) OVER (PARTITION BY symbol ORDER BY ts), ts) / 1000.0 AS gap_secs
            FROM oracle_ticks
            WHERE ts > ?
        )
        SELECT
            COUNT(*) AS total_gaps,
            COUNT(*) FILTER (WHERE gap_secs > 2)  AS gaps_gt_2s,
            COUNT(*) FILTER (WHERE gap_secs > 5)  AS gaps_gt_5s,
            COUNT(*) FILTER (WHERE gap_secs > 30) AS gaps_gt_30s,
            MAX(gap_secs)                          AS max_gap_secs,
            AVG(gap_secs)                          AS avg_gap_secs
        FROM gaps
        WHERE gap_secs IS NOT NULL
        """,
        [since],
    ).fetchone()

    if not result or result[0] == 0:
        console.print("[yellow]⚠ No tick data found for the period[/]")
        return

    total, gt2, gt5, gt30, max_gap, avg_gap = result
    status = "✓" if gt5 == 0 else "✗" if gt5 > 10 else "⚠"
    console.print(f"\n[bold]Tick Gaps (last {hours}h)[/]")
    t = Table(show_header=True)
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    t.add_row("Total gap intervals", f"{total:,}")
    t.add_row("Gaps > 2s", str(gt2))
    t.add_row(f"{status} Gaps > 5s", str(gt5))
    t.add_row("Gaps > 30s", str(gt30))
    t.add_row("Max gap (s)", f"{max_gap:.1f}" if max_gap else "—")
    t.add_row("Avg gap (s)", f"{avg_gap:.3f}" if avg_gap else "—")
    console.print(t)


def check_signal_sequences(db, hours: int) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = db.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'pre_trigger_alert')  AS pre_triggers,
            COUNT(*) FILTER (WHERE event_type = 'round_triggered')     AS round_triggered,
            COUNT(*) FILTER (WHERE event_type = 'round_settled')       AS round_settled,
            COUNT(*) FILTER (WHERE event_type = 'deviation_approach')  AS dev_approach,
            COUNT(*) FILTER (WHERE event_type = 'bb_breakout')         AS bb_breakout
        FROM signal_events
        WHERE ts > ?
        """,
        [since],
    ).fetchone()

    console.print(f"\n[bold]Signal Counts (last {hours}h)[/]")
    t = Table(show_header=True)
    t.add_column("Event Type")
    t.add_column("Count", justify="right")

    if result:
        labels = ["pre_trigger_alert", "round_triggered", "round_settled", "deviation_approach", "bb_breakout"]
        for label, count in zip(labels, result):
            t.add_row(label, str(count or 0))
    console.print(t)

    # Check orphaned round_triggered (no preceding pre_trigger within 60s)
    orphans = db.execute(
        """
        SELECT COUNT(*) FROM signal_events rt
        WHERE rt.event_type = 'round_triggered'
          AND rt.ts > ?
          AND NOT EXISTS (
              SELECT 1 FROM signal_events pt
              WHERE pt.event_type = 'pre_trigger_alert'
                AND pt.symbol = rt.symbol
                AND pt.ts BETWEEN rt.ts - INTERVAL '60 seconds' AND rt.ts
          )
        """,
        [since],
    ).fetchone()[0]

    status = "✓" if orphans == 0 else "⚠"
    console.print(f"  {status} Orphaned round_triggered (no pre_trigger in 60s): {orphans}")


def check_snapshot_coverage(db) -> None:
    result = db.execute(
        """
        SELECT
            m.condition_id,
            m.asset,
            m.timeframe,
            m.close_time,
            COUNT(s.ts) AS snapshot_count,
            MIN(s.ts)   AS first_snapshot,
            MAX(s.ts)   AS last_snapshot
        FROM pm_markets m
        LEFT JOIN pm_snapshots s ON s.condition_id = m.condition_id
        WHERE m.resolved_at IS NULL
          AND m.close_time > now()
        GROUP BY 1, 2, 3, 4
        ORDER BY m.close_time
        LIMIT 20
        """
    ).fetchall()

    console.print("\n[bold]Active Market Snapshot Coverage[/]")
    t = Table(show_header=True)
    t.add_column("condition_id")
    t.add_column("Asset")
    t.add_column("TF")
    t.add_column("Closes")
    t.add_column("Snapshots", justify="right")
    t.add_column("Coverage")

    for row in result:
        cid, asset, tf, close_time, count, first, last = row
        coverage = "✓" if count > 0 else "[red]✗ NONE[/]"
        t.add_row(
            cid[:16] if cid else "—",
            asset or "?",
            tf or "?",
            close_time.strftime("%H:%M") if close_time else "?",
            str(count),
            coverage,
        )

    if not result:
        console.print("  [dim]No active markets found[/]")
    else:
        console.print(t)


def check_uptime(db, hours: int) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = db.execute(
        """
        WITH tick_windows AS (
            SELECT
                DATE_TRUNC('minute', ts) AS minute,
                COUNT(*) AS ticks
            FROM oracle_ticks
            WHERE ts > ?
            GROUP BY 1
        )
        SELECT
            COUNT(*) AS minutes_with_data,
            ? AS total_minutes
        FROM tick_windows
        WHERE ticks > 0
        """,
        [since, hours * 60],
    ).fetchone()

    if result:
        minutes_with_data, total_minutes = result
        uptime_pct = (minutes_with_data / total_minutes * 100) if total_minutes > 0 else 0
        status = "✓" if uptime_pct >= 95 else "⚠" if uptime_pct >= 80 else "✗"
        console.print(f"\n[bold]Collection Uptime (last {hours}h)[/]")
        console.print(f"  {status} {uptime_pct:.1f}% ({minutes_with_data}/{total_minutes} minutes)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Data quality checks")
    parser.add_argument("--hours", type=int, default=24, help="Look-back window in hours")
    args = parser.parse_args()

    migrate(verbose=False)
    db = get_db()

    console.print(f"[bold]polymarket-researcher — Data Quality Report[/]")
    console.print(f"Window: last {args.hours}h\n")

    check_tick_gaps(db, args.hours)
    check_signal_sequences(db, args.hours)
    check_snapshot_coverage(db)
    check_uptime(db, args.hours)

    console.print("\n[dim]Done.[/]")


if __name__ == "__main__":
    main()
