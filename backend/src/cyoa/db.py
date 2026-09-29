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
    id               INTEGER PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    -- Treaty of Babel identifier, uuid4 assigned on create; survives export/import
    ifid             TEXT NOT NULL,
    -- the single entry passage; nullable so a story can exist before its map does
    start_passage_id INTEGER REFERENCES passages(id) ON DELETE SET NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    -- bumped by every write to the story or any of its rows (Library "last edited")
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS passages (
    id         INTEGER PRIMARY KEY,
    story_id   INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    -- editor layout hint; the graph editor owns these, the engine ignores them
    pos_x      REAL NOT NULL DEFAULT 0,
    pos_y      REAL NOT NULL DEFAULT 0,
    -- world layer: story-time clock and {"location", "present": [...], "events": [ids]}
    t          REAL,
    meta       TEXT NOT NULL DEFAULT '{}',
    -- agents write directly where reviewed = 0 and must propose where reviewed = 1
    reviewed   INTEGER NOT NULL DEFAULT 0,
    provenance TEXT NOT NULL DEFAULT 'human' CHECK (provenance IN ('human', 'agent')),
    -- a decided, terminal passage ("you are dead"), distinct from a stub that just
    -- has no choices yet; authoritative at play time (see main.py's _view)
    is_ending  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS choices (
    id           INTEGER PRIMARY KEY,
    story_id     INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    from_passage INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    to_passage   INTEGER REFERENCES passages(id) ON DELETE SET NULL,
    text         TEXT NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    -- JSON: {"var": "brave", "op": "==", "value": true} | {"all": [...]} | {"any": [...]}
    condition    TEXT NOT NULL DEFAULT '{}',
    -- JSON: {"set": {...}, "add": {...}, "give": [...], "take": [...]}
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

-- the story bible: what is true of the world regardless of branch
CREATE TABLE IF NOT EXISTS lore (
    id       INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL CHECK (kind IN ('character', 'place', 'faction', 'rule')),
    name     TEXT NOT NULL,
    body     TEXT NOT NULL DEFAULT '',
    reviewed INTEGER NOT NULL DEFAULT 0
);

-- the world timeline: what happens at each t on every branch
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    t        REAL NOT NULL,
    name     TEXT NOT NULL,
    body     TEXT NOT NULL DEFAULT '',
    reviewed INTEGER NOT NULL DEFAULT 0
);

-- what an agent wants to change; only accepting a proposal writes to the tables above.
-- batch groups the proposals one assist call produced so they can be discarded together.
CREATE TABLE IF NOT EXISTS proposals (
    id             INTEGER PRIMARY KEY,
    story_id       INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    batch          TEXT NOT NULL,
    verb           TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('new_passage', 'new_choice', 'edit_passage', 'edit_choice', 'new_variable', 'note')),
    target_passage INTEGER REFERENCES passages(id) ON DELETE CASCADE,
    target_choice  INTEGER REFERENCES choices(id) ON DELETE CASCADE,
    -- JSON, shape depends on kind (see models.py)
    payload        TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    -- the autonomous run that filed it, if any (assist verbs leave it null)
    run_id         INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- an autonomous workflow run. thread_id is the LangGraph checkpoint key in the
-- separate agent db (CYOA_AGENT_DB), so deleting cyoa.db still resets the story.
CREATE TABLE IF NOT EXISTS agent_runs (
    id         INTEGER PRIMARY KEY,
    story_id   INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    workflow   TEXT NOT NULL,
    thread_id  TEXT NOT NULL UNIQUE,
    -- JSON: model, steer, ...
    params     TEXT NOT NULL DEFAULT '{}',
    -- JSON: passage ids the run was started from
    scope      TEXT NOT NULL DEFAULT '[]',
    -- JSON: {"max_passages", "depth", "max_seconds"}
    budget     TEXT NOT NULL DEFAULT '{}',
    -- JSON: {"auto_accept_unreviewed": bool, "skip_review_interrupt": bool, "refine": {...}} — controls automation
    policy     TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'paused', 'done', 'failed', 'cancelled')),
    -- JSON: {"created", "level", ...} as the run reports it
    progress   TEXT NOT NULL DEFAULT '{}',
    error      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- refinement telemetry: one row per (step, round, auditor). Append-only; cascades
-- with the story, and with the specific passage/proposal it audited.
CREATE TABLE IF NOT EXISTS audits (
    id          INTEGER PRIMARY KEY,
    story_id    INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    run_id      INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL,
    proposal_id INTEGER REFERENCES proposals(id) ON DELETE CASCADE,
    passage_id  INTEGER REFERENCES passages(id) ON DELETE CASCADE,
    step        TEXT NOT NULL,   -- the draft role used: 'frontier_passage' | 'frontier_ending'
    round       INTEGER NOT NULL,
    auditor     TEXT NOT NULL,   -- prompt role, e.g. 'audit_continuity'
    verdict     TEXT NOT NULL CHECK (verdict IN ('pass', 'revise', 'error')),
    issues      TEXT NOT NULL DEFAULT '[]',  -- JSON: verified issues only, each {severity, problem, quote, evidence}
    dropped     INTEGER NOT NULL DEFAULT 0,  -- issues this auditor raised whose quote/evidence didn't verify
    draft       TEXT NOT NULL DEFAULT '{}',  -- JSON {title, body}: what was audited this round
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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


def touch_story(conn: sqlite3.Connection, story_id: int) -> None:
    """Bump updated_at; call from every write that belongs to a story."""
    conn.execute("UPDATE stories SET updated_at = datetime('now') WHERE id = ?", (story_id,))
