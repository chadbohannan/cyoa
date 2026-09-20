"""SQLite access and schema.

The story graph lives in a single SQLite file (CYOA_DB, default ./cyoa.db).
Schema is created on startup; there is no migration tooling yet — while the
model is still moving, delete the file to reset. Conditions and effects on
choices are stored as JSON text so their shape can evolve without a schema
change.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get("CYOA_DB", "cyoa.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS passages (
    id        INTEGER PRIMARY KEY,
    story_id  INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL DEFAULT '',
    is_start  INTEGER NOT NULL DEFAULT 0,
    -- editor layout hint; the graph editor owns these, the engine ignores them
    pos_x     REAL NOT NULL DEFAULT 0,
    pos_y     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS choices (
    id           INTEGER PRIMARY KEY,
    story_id     INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    from_passage INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    to_passage   INTEGER REFERENCES passages(id) ON DELETE SET NULL,
    text         TEXT NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    -- JSON: e.g. {"has_flag": "lantern"} / {"stat_gte": ["strength", 3]}
    condition    TEXT NOT NULL DEFAULT '{}',
    -- JSON: e.g. {"set_flag": "lantern", "add_stat": ["gold", -5]}
    effects      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS variables (
    id        INTEGER PRIMARY KEY,
    story_id  INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('flag', 'stat', 'item')),
    -- JSON default value: false for flags, a number for stats, a count for items
    "default" TEXT NOT NULL DEFAULT 'null',
    UNIQUE (story_id, name)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    # WAL lets a background writer (a future agent run) coexist with the editor's
    # reads; the timeout makes short write contention wait instead of raising.
    conn = sqlite3.connect(path or DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def transaction(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
