"""Thread-safe DuckDB connection manager.

DuckDB in multi-threaded environments: use a single connection with
the write_lock to serialise INSERT/UPDATE statements. Reads can be
done on the same connection (DuckDB supports concurrent reads).
"""

import threading
from pathlib import Path

import duckdb
from dotenv import load_dotenv
import os

load_dotenv()

_conn: duckdb.DuckDBPyConnection | None = None
_lock = threading.Lock()
write_lock = threading.Lock()


def get_db(path: str | None = None) -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is None:
            db_path = path or os.getenv("DB_PATH", "./data/researcher.db")
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _conn = duckdb.connect(db_path)
            # Enable parallel query execution
            _conn.execute("SET threads TO 4")
    return _conn


def close_db() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
