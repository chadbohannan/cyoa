"""SQLite access and schema.

The story graph lives in a single SQLite file (CYOA_DB, default ./cyoa.db).
Schema is created on startup; there is no migration tooling yet — while the
model is still moving, delete the file to reset. A story has no variables,
conditions or effects (concepts/stateless-story.md): a database created before
2026-10-07 still carries the unused `variables` table and `choices.condition` /
`effects` columns, which nothing reads or writes.
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
    -- world layer: story-time clock and {"location", "present": [...]}
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
    sort_order   INTEGER NOT NULL DEFAULT 0
);

-- the story bible: what is true of the world regardless of branch
CREATE TABLE IF NOT EXISTS lore (
    id       INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL CHECK (kind IN ('character', 'place', 'faction', 'rule', 'fact')),
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
    -- JSON: expand_frontier's copy of the story's pacing plan, plus its derived "depth"
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

-- a run's event log, kept so retries and failures can be read after the fact; goes with the run
CREATE TABLE IF NOT EXISTS run_events (
    id     INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    seq    INTEGER NOT NULL,   -- position in the run's log within one server process
    at     REAL NOT NULL,      -- unix time
    type   TEXT NOT NULL,      -- node | activity | retry | interrupt | status (progress is not kept: the run row has it)
    data   TEXT NOT NULL DEFAULT '{}'  -- JSON: the event minus type, seq and at
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events(run_id, id);

-- a story's pacing plan (concepts/act-pacing.md; shape in models.PacingPlan). A row
-- only once the author saves one; until then the story uses agents.DEFAULT_PLAN. Its
-- own table rather than a stories column so an existing cyoa.db picks it up.
CREATE TABLE IF NOT EXISTS pacing (
    story_id INTEGER PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    plan     TEXT NOT NULL
);

-- an Interview Me conversation (concepts/interview-me.md): the agent asks, the author
-- answers, and each agent entry in `transcript` carries the premise/character writes it
-- made ({table, id, before, after}) so they can be reverted. `conflicts` are the
-- contradictions found along the way; only the author's answer resolves one.
CREATE TABLE IF NOT EXISTS interviews (
    id         INTEGER PRIMARY KEY,
    story_id   INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    transcript TEXT NOT NULL DEFAULT '[]',
    conflicts  TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- what a run did, kept past a restart: node, activity, retry (with the reason), interrupt and status
-- events as the run pushed them. Progress is not here (the run row keeps the latest). Cascades with the run,
-- so Clear history takes it along.
CREATE TABLE IF NOT EXISTS run_events (
    id     INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    seq    INTEGER NOT NULL,
    at     REAL NOT NULL,
    type   TEXT NOT NULL,
    data   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events(run_id, seq);

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
-- rolling "story so far" summaries for the MCP server (summarize.py). Content-addressed: `key` hashes the
-- route prefix (passage ids, titles, bodies and the choice text taken), so editing any passage on a route
-- makes its old summaries unreachable instead of needing invalidation. A cache, safe to empty.
CREATE TABLE IF NOT EXISTS embeddings (
    key        TEXT PRIMARY KEY,   -- sha1 of model + text
    model      TEXT NOT NULL,
    vec        BLOB NOT NULL,      -- float32 array
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS summaries (
    key        TEXT PRIMARY KEY,
    story_id   INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    passage_id INTEGER NOT NULL,   -- the last passage the summary covers
    depth      INTEGER NOT NULL,   -- passages covered
    text       TEXT NOT NULL,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- the local model's canon check of one passage on one route (canon.py). Content-addressed like summaries: `key`
-- hashes the prompt, model, canon text, route summary and passage, so any edit makes a stale row unreachable.
CREATE TABLE IF NOT EXISTS canon_checks (
    key        TEXT PRIMARY KEY,
    story_id   INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    passage_id INTEGER NOT NULL,
    issues     TEXT NOT NULL,     -- JSON list of {quote, conflicts_with, why}
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- every earlier version of a lore entry, written by the MCP lore tools before they change or delete it
CREATE TABLE IF NOT EXISTS lore_revisions (
    id       INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    lore_id  INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    name     TEXT NOT NULL,
    body     TEXT NOT NULL,
    action   TEXT NOT NULL,   -- 'update' | 'delete': what replaced this version
    label    TEXT NOT NULL DEFAULT '',
    at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS lore_revisions_lore ON lore_revisions(lore_id, id);

-- every MCP tool call, so runs by different harnesses/models can be compared afterwards.
-- `label` is CYOA_MCP_LABEL of the server process (e.g. "claude-code/opus"), set by whoever launches it.
CREATE TABLE IF NOT EXISTS tool_calls (
    id       INTEGER PRIMARY KEY,
    story_id INTEGER REFERENCES stories(id) ON DELETE CASCADE,
    label    TEXT NOT NULL DEFAULT '',
    tool     TEXT NOT NULL,
    args     TEXT NOT NULL DEFAULT '{}',
    ok       INTEGER NOT NULL,
    error    TEXT,
    ms       INTEGER NOT NULL,
    at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_calls_story ON tool_calls(story_id, id);
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


def unique_title(conn: sqlite3.Connection, story_id: int, title: str, exclude_id: int | None = None) -> str:
    """`title` if no other passage of the story has it (case-insensitively, since Twine names
    are the export's keys), else 'title (2)', 'title (3)', ... — the first free one."""
    title = (title or "").strip()
    taken = {r[0].strip().casefold() for r in conn.execute(
        "SELECT title FROM passages WHERE story_id = ? AND id IS NOT ?", (story_id, exclude_id))}
    if title.casefold() not in taken:
        return title
    n = 2
    while f"{title} ({n})".casefold() in taken:
        n += 1
    return f"{title} ({n})"


def title_taken(conn: sqlite3.Connection, story_id: int, title: str, exclude_id: int | None = None) -> bool:
    return unique_title(conn, story_id, title, exclude_id) != (title or "").strip()


def touch_story(conn: sqlite3.Connection, story_id: int) -> None:
    """Bump updated_at; call from every write that belongs to a story."""
    conn.execute("UPDATE stories SET updated_at = datetime('now') WHERE id = ?", (story_id,))
