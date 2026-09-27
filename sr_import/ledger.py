"""Tool-owned record of what we already imported.

The shared store is never asked "did we import this" -- that is our own
bookkeeping, kept outside the store so a store restore cannot erase it and a
second run cannot double-write.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS imported (
    key          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    entry        TEXT NOT NULL,
    role         TEXT NOT NULL,
    origin       TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    imported_at  TEXT NOT NULL,
    manifest     TEXT NOT NULL,
    disposition  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imported_source ON imported(source, role);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source      TEXT,
    planned     INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    duplicate   INTEGER DEFAULT 0,
    rejected    INTEGER DEFAULT 0,
    note        TEXT
);
"""


def connect() -> sqlite3.Connection:
    config.LEDGER_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(config.LEDGER_DB))
    con.executescript(SCHEMA)
    return con


def known_keys(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute("SELECT key FROM imported")}


def record(con: sqlite3.Connection, rows: list[tuple]) -> None:
    con.executemany(
        """INSERT OR REPLACE INTO imported
           (key,source,entry,role,origin,session_id,occurred_at,imported_at,manifest,disposition)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    con.commit()


def start_run(con: sqlite3.Connection, run_id: str, started_at: str, source: str) -> None:
    con.execute("INSERT OR REPLACE INTO runs(run_id,started_at,source) VALUES (?,?,?)",
                (run_id, started_at, source))
    con.commit()


def finish_run(con: sqlite3.Connection, run_id: str, finished_at: str, stats: dict, note: str = "") -> None:
    con.execute(
        """UPDATE runs SET finished_at=?, planned=?, inserted=?, duplicate=?, rejected=?, note=?
           WHERE run_id=?""",
        (finished_at, stats.get("planned", 0), stats.get("inserted", 0),
         stats.get("duplicate", 0), stats.get("rejected", 0), note, run_id),
    )
    con.commit()


def recent_runs(con: sqlite3.Connection, limit: int = 10) -> list[tuple]:
    return list(con.execute(
        "SELECT run_id,started_at,finished_at,source,planned,inserted,duplicate,rejected,note "
        "FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)))
