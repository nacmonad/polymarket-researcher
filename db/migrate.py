"""Idempotent schema migration.

Run: python -m db.migrate
Applies db/schema.sql against the configured DB_PATH.
Safe to run multiple times — all CREATE statements use IF NOT EXISTS.
"""

import sys
from pathlib import Path

from db.connection import get_db


SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def apply(verbose: bool = True) -> None:
    db = get_db()
    sql = SCHEMA_PATH.read_text()

    # Split on semicolons, skip blank/comment-only blocks
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    applied = 0

    for stmt in statements:
        # Skip pure comment blocks
        lines = [l for l in stmt.splitlines() if not l.strip().startswith("--")]
        if not any(l.strip() for l in lines):
            continue
        try:
            db.execute(stmt)
            applied += 1
            if verbose:
                first_line = next(l.strip() for l in stmt.splitlines() if l.strip() and not l.strip().startswith("--"))
                print(f"  ✓ {first_line[:80]}")
        except Exception as e:
            print(f"  ✗ Failed: {e}\n    Statement: {stmt[:120]}", file=sys.stderr)
            raise

    if verbose:
        print(f"\nMigration complete — {applied} statements applied.")


if __name__ == "__main__":
    print(f"Applying schema from {SCHEMA_PATH} …\n")
    apply()
