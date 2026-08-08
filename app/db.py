import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "setuhaul_freight_operations.db"


def get_db_path() -> Path:
    override = os.environ.get("SETUHAUL_DB_PATH")
    return Path(override) if override else DEFAULT_DB_PATH


def get_connection() -> sqlite3.Connection:
    # isolation_level=None -> autocommit; we drive BEGIN/COMMIT/ROLLBACK explicitly
    # so that request_appointment can use BEGIN IMMEDIATE to grab the write lock
    # up front and let the unique indexes (not a lock error) decide the race.
    conn = sqlite3.connect(get_db_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection | None = None):
    """Yield a connection inside a BEGIN IMMEDIATE / COMMIT block.

    BEGIN IMMEDIATE (not the default deferred BEGIN) takes the write lock
    at the start of the transaction, so two concurrent callers serialize on
    entry and the loser sees the unique-index conflict deterministically
    instead of racing on which SELECT ran first.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        if owns_conn:
            conn.close()
