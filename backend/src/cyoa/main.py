"""FastAPI app: story / passage / choice / variable / lore / event CRUD, graph read,
the stateless player, the problems validator, the assist verbs and their proposals,
and autonomous runs with their event stream."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import agents, db, engine, llm, runs
from .models import (
    AgentRun,
    AssistRequest,
    Choice,
    ChoiceCreate,
    ChoicePatch,
    Event,
    EventCreate,
    EventPatch,
    Lore,
    LoreCreate,
    LorePatch,
    Passage,
    PassageCreate,
    PassagePatch,
    PlayChoose,
    PlayStart,
    PlayView,
    Problems,
    Proposal,
    RunCreate,
    RunResume,
    RunState,
    Story,
    StoryCreate,
    StoryGraph,
    StoryPatch,
    StorySummary,
    Variable,
    VariableCreate,
    VariablePatch,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    runs.manager.recover()
    yield
    runs.manager.reset()


app = FastAPI(title="cyoa", version="0.1.0", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "db": str(db.DB_PATH)}


# --- row helpers -------------------------------------------------------------
#
# Every child table is handled by the same three functions. JSON_COLS lists the
# columns stored as JSON text so they are encoded on the way in and decoded on
# the way out; everything else passes through unchanged.

JSON_COLS: dict[str, set[str]] = {
    "passages": {"meta"},
    "choices": {"condition", "effects"},
    "variables": {"default"},
    "lore": set(),
    "events": set(),
    "proposals": {"payload"},
}


def _decode(table: str, row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for col in JSON_COLS[table]:
        d[col] = json.loads(d[col])
    return d


def _encode(table: str, values: dict[str, Any]) -> dict[str, Any]:
    return {k: json.dumps(v) if k in JSON_COLS[table] else v for k, v in values.items()}


def _insert(conn: sqlite3.Connection, table: str, story_id: int, values: dict[str, Any]) -> dict[str, Any]:
    _require_story(conn, story_id)
    values = _encode(table, {"story_id": story_id, **values})
    cols = ", ".join(f'"{k}"' for k in values)
    marks = ", ".join("?" for _ in values)
    try:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, str(e))
    db.touch_story(conn, story_id)
    return _decode(table, conn.execute(f"SELECT * FROM {table} WHERE id = ?", (cur.lastrowid,)).fetchone())


def _update(conn: sqlite3.Connection, table: str, story_id: int, row_id: int, patch: BaseModel) -> dict[str, Any]:
    values = _encode(table, patch.model_dump(exclude_unset=True))
    if values:
        sets = ", ".join(f'"{k}" = ?' for k in values)
        try:
            cur = conn.execute(
                f"UPDATE {table} SET {sets} WHERE id = ? AND story_id = ?",
                (*values.values(), row_id, story_id),
            )
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, str(e))
        if cur.rowcount == 0:
            raise HTTPException(404, f"{table[:-1]} not found")
        db.touch_story(conn, story_id)
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND story_id = ?", (row_id, story_id)).fetchone()
    if row is None:
        raise HTTPException(404, f"{table[:-1]} not found")
    return _decode(table, row)


def _delete(conn: sqlite3.Connection, table: str, story_id: int, row_id: int) -> None:
    cur = conn.execute(f"DELETE FROM {table} WHERE id = ? AND story_id = ?", (row_id, story_id))
    if cur.rowcount == 0:
        raise HTTPException(404, f"{table[:-1]} not found")
    db.touch_story(conn, story_id)


def _require_story(conn: sqlite3.Connection, story_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "story not found")
    return row


# --- stories -----------------------------------------------------------------

STORY_SUMMARY_SQL = """
SELECT s.*, (SELECT COUNT(*) FROM passages p WHERE p.story_id = s.id) AS passage_count
FROM stories s
"""


@app.get("/api/stories")
def list_stories() -> list[StorySummary]:
    with db.transaction() as conn:
        rows = conn.execute(STORY_SUMMARY_SQL + " ORDER BY s.updated_at DESC, s.id DESC").fetchall()
    return [StorySummary(**dict(r)) for r in rows]


@app.post("/api/stories", status_code=201)
def create_story(body: StoryCreate) -> StorySummary:
    # A new story lands the author on a map with its start passage already there.
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO stories (title, description, ifid) VALUES (?, ?, ?)",
            (body.title, body.description, str(uuid.uuid4()).upper()),
        )
        story_id = cur.lastrowid
        start = conn.execute(
            "INSERT INTO passages (story_id, title) VALUES (?, ?)", (story_id, "Start")
        ).lastrowid
        conn.execute("UPDATE stories SET start_passage_id = ? WHERE id = ?", (start, story_id))
        row = conn.execute(STORY_SUMMARY_SQL + " WHERE s.id = ?", (story_id,)).fetchone()
    return StorySummary(**dict(row))


@app.patch("/api/stories/{story_id}")
def update_story(story_id: int, body: StoryPatch) -> Story:
    values = body.model_dump(exclude_unset=True)
    with db.transaction() as conn:
        _require_story(conn, story_id)
        if values.get("start_passage_id") is not None:
            owned = conn.execute(
                "SELECT 1 FROM passages WHERE id = ? AND story_id = ?",
                (values["start_passage_id"], story_id),
            ).fetchone()
            if owned is None:
                raise HTTPException(422, "start_passage_id must belong to this story")
        if values:
            sets = ", ".join(f"{k} = ?" for k in values)
            conn.execute(f"UPDATE stories SET {sets} WHERE id = ?", (*values.values(), story_id))
            db.touch_story(conn, story_id)
        row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    return Story(**dict(row))


@app.delete("/api/stories/{story_id}", status_code=204)
def delete_story(story_id: int) -> None:
    with db.transaction() as conn:
        if conn.execute("DELETE FROM stories WHERE id = ?", (story_id,)).rowcount == 0:
            raise HTTPException(404, "story not found")


@app.get("/api/stories/{story_id}/graph")
def story_graph(story_id: int) -> StoryGraph:
    with db.transaction() as conn:
        story = _require_story(conn, story_id)

        def rows(table: str, order: str) -> list[dict[str, Any]]:
            q = f"SELECT * FROM {table} WHERE story_id = ? ORDER BY {order}"
            return [_decode(table, r) for r in conn.execute(q, (story_id,)).fetchall()]

        return StoryGraph(
            story=Story(**dict(story)),
            passages=[Passage(**r) for r in rows("passages", "id")],
            choices=[Choice(**r) for r in rows("choices", "from_passage, sort_order, id")],
            variables=[Variable(**r) for r in rows("variables", "name")],
            lore=[Lore(**r) for r in rows("lore", "kind, name")],
            events=[Event(**r) for r in rows("events", "t, id")],
            proposals=[Proposal(**r) for r in rows("proposals", "id") if r["status"] == "pending"],
        )


# --- passages ----------------------------------------------------------------


@app.post("/api/stories/{story_id}/passages", status_code=201)
def create_passage(story_id: int, body: PassageCreate) -> Passage:
    with db.transaction() as conn:
        return Passage(**_insert(conn, "passages", story_id, body.model_dump()))


@app.patch("/api/stories/{story_id}/passages/{passage_id}")
def update_passage(story_id: int, passage_id: int, body: PassagePatch) -> Passage:
    with db.transaction() as conn:
        return Passage(**_update(conn, "passages", story_id, passage_id, body))


@app.delete("/api/stories/{story_id}/passages/{passage_id}", status_code=204)
def delete_passage(story_id: int, passage_id: int) -> None:
    with db.transaction() as conn:
        _delete(conn, "passages", story_id, passage_id)


@app.post("/api/stories/{story_id}/passages/{passage_id}/duplicate", status_code=201)
def duplicate_passage(story_id: int, passage_id: int) -> Passage:
    """Copy a passage and its outgoing choices, offset so the copy is visible."""
    with db.transaction() as conn:
        src = conn.execute(
            "SELECT * FROM passages WHERE id = ? AND story_id = ?", (passage_id, story_id)
        ).fetchone()
        if src is None:
            raise HTTPException(404, "passage not found")
        values = {k: v for k, v in _decode("passages", src).items() if k not in ("id", "story_id")}
        values["title"] = f"{values['title']} (copy)"
        values["pos_x"] += 40
        values["pos_y"] += 40
        copy = _insert(conn, "passages", story_id, values)
        conn.execute(
            "INSERT INTO choices (story_id, from_passage, to_passage, text, sort_order, condition, effects)"
            " SELECT story_id, ?, to_passage, text, sort_order, condition, effects"
            " FROM choices WHERE from_passage = ?",
            (copy["id"], passage_id),
        )
    return Passage(**copy)


# --- choices -----------------------------------------------------------------


def _require_passage(conn: sqlite3.Connection, story_id: int, passage_id: int | None) -> None:
    if passage_id is None:
        return
    if conn.execute(
        "SELECT 1 FROM passages WHERE id = ? AND story_id = ?", (passage_id, story_id)
    ).fetchone() is None:
        raise HTTPException(422, f"passage {passage_id} is not in this story")


@app.post("/api/stories/{story_id}/choices", status_code=201)
def create_choice(story_id: int, body: ChoiceCreate) -> Choice:
    with db.transaction() as conn:
        _require_passage(conn, story_id, body.from_passage)
        _require_passage(conn, story_id, body.to_passage)
        return Choice(**_insert(conn, "choices", story_id, body.model_dump()))


@app.patch("/api/stories/{story_id}/choices/{choice_id}")
def update_choice(story_id: int, choice_id: int, body: ChoicePatch) -> Choice:
    with db.transaction() as conn:
        if "to_passage" in body.model_fields_set:
            _require_passage(conn, story_id, body.to_passage)
        return Choice(**_update(conn, "choices", story_id, choice_id, body))


@app.delete("/api/stories/{story_id}/choices/{choice_id}", status_code=204)
def delete_choice(story_id: int, choice_id: int) -> None:
    with db.transaction() as conn:
        _delete(conn, "choices", story_id, choice_id)


# --- variables ---------------------------------------------------------------


@app.post("/api/stories/{story_id}/variables", status_code=201)
def create_variable(story_id: int, body: VariableCreate) -> Variable:
    with db.transaction() as conn:
        return Variable(**_insert(conn, "variables", story_id, body.model_dump()))


@app.patch("/api/stories/{story_id}/variables/{variable_id}")
def update_variable(story_id: int, variable_id: int, body: VariablePatch) -> Variable:
    with db.transaction() as conn:
        return Variable(**_update(conn, "variables", story_id, variable_id, body))


@app.delete("/api/stories/{story_id}/variables/{variable_id}", status_code=204)
def delete_variable(story_id: int, variable_id: int) -> None:
    with db.transaction() as conn:
        _delete(conn, "variables", story_id, variable_id)


# --- lore & events -----------------------------------------------------------


@app.post("/api/stories/{story_id}/lore", status_code=201)
def create_lore(story_id: int, body: LoreCreate) -> Lore:
    with db.transaction() as conn:
        return Lore(**_insert(conn, "lore", story_id, body.model_dump()))


@app.patch("/api/stories/{story_id}/lore/{lore_id}")
def update_lore(story_id: int, lore_id: int, body: LorePatch) -> Lore:
    with db.transaction() as conn:
        return Lore(**_update(conn, "lore", story_id, lore_id, body))


@app.delete("/api/stories/{story_id}/lore/{lore_id}", status_code=204)
def delete_lore(story_id: int, lore_id: int) -> None:
    with db.transaction() as conn:
        _delete(conn, "lore", story_id, lore_id)


@app.post("/api/stories/{story_id}/events", status_code=201)
def create_event(story_id: int, body: EventCreate) -> Event:
    with db.transaction() as conn:
        return Event(**_insert(conn, "events", story_id, body.model_dump()))


@app.patch("/api/stories/{story_id}/events/{event_id}")
def update_event(story_id: int, event_id: int, body: EventPatch) -> Event:
    with db.transaction() as conn:
        return Event(**_update(conn, "events", story_id, event_id, body))


@app.delete("/api/stories/{story_id}/events/{event_id}", status_code=204)
def delete_event(story_id: int, event_id: int) -> None:
    with db.transaction() as conn:
        _delete(conn, "events", story_id, event_id)


# --- play & problems ---------------------------------------------------------
#
# The server holds no playthrough state: the client sends the state it was last
# given, the engine re-checks the chosen choice against it, applies the effects
# and returns the next view. Everything here goes through engine.py so the
# player, the validator and the map agree on what a condition means.


def _view(conn: sqlite3.Connection, story_id: int, passage_id: int, state: engine.State) -> PlayView:
    row = conn.execute("SELECT * FROM passages WHERE id = ? AND story_id = ?", (passage_id, story_id)).fetchone()
    if row is None:
        raise HTTPException(404, "passage not found")
    choices = [
        _decode("choices", r)
        for r in conn.execute(
            "SELECT * FROM choices WHERE from_passage = ? ORDER BY sort_order, id", (passage_id,)
        ).fetchall()
    ]
    return PlayView(
        passage=Passage(**_decode("passages", row)),
        choices=[Choice(**c) for c in choices if engine.check(c["condition"], state)],
        state=state,
    )


def _variables(conn: sqlite3.Connection, story_id: int) -> list[dict[str, Any]]:
    return [_decode("variables", r) for r in conn.execute("SELECT * FROM variables WHERE story_id = ?", (story_id,))]


@app.post("/api/stories/{story_id}/play/start")
def play_start(story_id: int, body: PlayStart) -> PlayView:
    with db.transaction() as conn:
        story = _require_story(conn, story_id)
        start = body.passage_id if body.passage_id is not None else story["start_passage_id"]
        if start is None:
            raise HTTPException(409, "story has no start passage")
        return _view(conn, story_id, start, engine.initial_state(_variables(conn, story_id)))


@app.post("/api/stories/{story_id}/play/choose")
def play_choose(story_id: int, body: PlayChoose) -> PlayView:
    with db.transaction() as conn:
        _require_story(conn, story_id)
        row = conn.execute("SELECT * FROM choices WHERE id = ? AND story_id = ?", (body.choice_id, story_id)).fetchone()
        if row is None:
            raise HTTPException(404, "choice not found")
        choice = _decode("choices", row)
        if choice["to_passage"] is None:
            raise HTTPException(409, "choice is dangling")
        # never trust the client's filtering
        if not engine.check(choice["condition"], body.state):
            raise HTTPException(409, "choice condition is not met")
        # names missing from the submitted state get their defaults; unknown names are dropped
        declared = engine.initial_state(_variables(conn, story_id))
        state = {k: body.state.get(k, v) for k, v in declared.items()}
        return _view(conn, story_id, choice["to_passage"], engine.apply(choice["effects"], state))


@app.get("/api/stories/{story_id}/problems")
def story_problems(story_id: int) -> Problems:
    with db.transaction() as conn:
        story = _require_story(conn, story_id)
        passages = conn.execute("SELECT id FROM passages WHERE story_id = ? ORDER BY id", (story_id,)).fetchall()
        choices = [_decode("choices", r) for r in conn.execute("SELECT * FROM choices WHERE story_id = ?", (story_id,))]
        return Problems(
            **engine.problems(story["start_passage_id"], [dict(p) for p in passages], choices, _variables(conn, story_id))
        )


# --- assist & proposals --------------------------------------------------------
#
# A verb runs a LangGraph workflow (agents.py) that reads the passage's
# neighbourhood, asks the model for structured output and writes *proposals*.
# Nothing touches passages or choices until a proposal is accepted here, which
# is the human gate the webapp plan asks for. Verbs run synchronously in
# FastAPI's threadpool (sync def) because a local model answers in seconds.


@app.get("/api/llm/models")
def llm_models() -> list[str]:
    try:
        return llm.list_models()
    except Exception as e:
        raise HTTPException(503, f"ollama unreachable at {llm.OLLAMA_URL}: {e}")


@app.post("/api/stories/{story_id}/passages/{passage_id}/assist", status_code=201)
def assist(story_id: int, passage_id: int, body: AssistRequest) -> list[Proposal]:
    with db.transaction() as conn:
        _require_story(conn, story_id)
        _require_passage(conn, story_id, passage_id)
        if body.verb == "draft_target":
            if body.choice_id is None:
                raise HTTPException(422, "draft_target needs choice_id")
            row = conn.execute(
                "SELECT to_passage FROM choices WHERE id = ? AND story_id = ? AND from_passage = ?",
                (body.choice_id, story_id, passage_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "choice not found on this passage")
            if row["to_passage"] is not None:
                raise HTTPException(409, "choice already has a target")
    try:
        rows = agents.run_verb(story_id, passage_id, body.verb, {"model": body.model, "tone": body.tone, "choice_id": body.choice_id})
    except agents.VerbFailed as e:
        raise HTTPException(502, str(e))
    except Exception as e:  # ollama down, model missing, ...
        raise HTTPException(503, f"assist failed: {e}")
    return [Proposal(**r) for r in rows]


def _has_world(conn: sqlite3.Connection, story_id: int) -> bool:
    return (conn.execute("SELECT COUNT(*) FROM lore WHERE story_id = ?", (story_id,)).fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM events WHERE story_id = ?", (story_id,)).fetchone()[0]) > 0


def _pending(conn: sqlite3.Connection, story_id: int, proposal_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM proposals WHERE id = ? AND story_id = ?", (proposal_id, story_id)).fetchone()
    if row is None:
        raise HTTPException(404, "proposal not found")
    if row["status"] != "pending":
        raise HTTPException(409, f"proposal already {row['status']}")
    return _decode("proposals", row)


def _materialise(conn: sqlite3.Connection, story_id: int, p: dict[str, Any]) -> None:
    """Write the rows a proposal describes. The one place proposal kinds become tables."""
    kind, pl = p["kind"], p["payload"]
    if kind == "edit_passage":
        conn.execute("UPDATE passages SET body = ? WHERE id = ? AND story_id = ?", (pl["body"], p["target_passage"], story_id))
    elif kind == "edit_choice":
        conn.execute("UPDATE choices SET text = ? WHERE id = ? AND story_id = ?", (pl["text"], p["target_choice"], story_id))
    elif kind == "new_passage":
        new = _insert(conn, "passages", story_id, {"title": pl["title"], "body": pl["body"], "pos_x": pl.get("pos_x", 0),
                                                   "pos_y": pl.get("pos_y", 0), "provenance": "agent"})
        if p["target_choice"] is not None:
            conn.execute("UPDATE choices SET to_passage = ? WHERE id = ? AND to_passage IS NULL", (new["id"], p["target_choice"]))
    elif kind == "new_choice":
        to = pl.get("to_passage")
        if to is None and pl.get("new_target"):
            t = pl["new_target"]
            to = _insert(conn, "passages", story_id, {"title": t["title"], "body": t.get("body", ""), "pos_x": t.get("pos_x", 0),
                                                     "pos_y": t.get("pos_y", 0), "provenance": "agent"})["id"]
        order = conn.execute("SELECT COALESCE(MAX(sort_order) + 1, 0) FROM choices WHERE from_passage = ?", (p["target_passage"],)).fetchone()[0]
        _insert(conn, "choices", story_id, {"from_passage": p["target_passage"], "to_passage": to, "text": pl["text"], "sort_order": order,
                                            "condition": {}, "effects": {}})
    elif kind == "new_variable":
        _insert(conn, "variables", story_id, {"name": pl["name"], "kind": pl["kind"], "default": pl.get("default")})
    # 'note' materialises nothing; accepting it just files it away


@app.post("/api/stories/{story_id}/proposals/{proposal_id}/accept")
def accept_proposal(story_id: int, proposal_id: int) -> Proposal:
    with db.transaction() as conn:
        p = _pending(conn, story_id, proposal_id)
        _materialise(conn, story_id, p)
        conn.execute("UPDATE proposals SET status = 'accepted' WHERE id = ?", (proposal_id,))
        db.touch_story(conn, story_id)
        return Proposal(**_decode("proposals", conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()))


@app.post("/api/stories/{story_id}/proposals/{proposal_id}/reject")
def reject_proposal(story_id: int, proposal_id: int) -> Proposal:
    with db.transaction() as conn:
        _pending(conn, story_id, proposal_id)
        conn.execute("UPDATE proposals SET status = 'rejected' WHERE id = ?", (proposal_id,))
        return Proposal(**_decode("proposals", conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()))


# --- runs ----------------------------------------------------------------------
#
# A run executes a workflow from agents.WORKFLOWS in a worker thread (runs.py),
# writes passages or proposals as it goes, pauses at review interrupts and
# streams what it is doing over SSE so the map can highlight it.


def _run(story_id: int, run_id: int) -> dict[str, Any]:
    try:
        row = runs.manager.get(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    if row["story_id"] != story_id:
        raise HTTPException(404, "run not found")
    return row


@app.get("/api/stories/{story_id}/runs")
def list_runs(story_id: int) -> list[AgentRun]:
    with db.transaction() as conn:
        _require_story(conn, story_id)
    return [AgentRun(**r) for r in runs.manager.list(story_id)]


@app.post("/api/stories/{story_id}/runs", status_code=201)
def start_run(story_id: int, body: RunCreate) -> AgentRun:
    with db.transaction() as conn:
        _require_story(conn, story_id)
        owned = {r["id"] for r in conn.execute("SELECT id FROM passages WHERE story_id = ?", (story_id,))}
        scope = list(dict.fromkeys(body.scope))
        if any(i not in owned for i in scope):
            raise HTTPException(422, "scope must be passages of this story")
        if body.workflow == "expand_frontier" and len(scope) != 1:
            raise HTTPException(422, "expand_frontier grows from exactly one selected passage")
        if body.workflow == "canon_check" and not _has_world(conn, story_id):
            raise HTTPException(422, "nothing to check against: the story has no lore or events")
    try:
        row = runs.manager.start(story_id, body.workflow, scope, {"model": body.model, "steer": body.steer}, body.budget)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return AgentRun(**row)


@app.get("/api/stories/{story_id}/runs/{run_id}")
def get_run(story_id: int, run_id: int) -> AgentRun:
    return AgentRun(**_run(story_id, run_id))


@app.post("/api/stories/{story_id}/runs/{run_id}/resume")
def resume_run(story_id: int, run_id: int, body: RunResume) -> AgentRun:
    _run(story_id, run_id)
    try:
        return AgentRun(**runs.manager.resume(run_id, body.model_dump()))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/stories/{story_id}/runs/{run_id}/cancel")
def cancel_run(story_id: int, run_id: int) -> AgentRun:
    _run(story_id, run_id)
    return AgentRun(**runs.manager.cancel(run_id))


@app.get("/api/stories/{story_id}/runs/{run_id}/state")
def run_state(story_id: int, run_id: int) -> RunState:
    _run(story_id, run_id)
    return RunState(**runs.manager.state(run_id))


@app.get("/api/stories/{story_id}/runs/{run_id}/events")
async def run_events(story_id: int, run_id: int, after: int = 0) -> StreamingResponse:
    """Server-sent events: the run's log from `after`, then live until it stops running.
    The stream closes when the run pauses or ends; the client reconnects on resume."""
    _run(story_id, run_id)

    async def gen() -> AsyncIterator[str]:
        seq = after
        while True:
            batch = runs.manager.events(run_id, seq)
            for ev in batch:
                seq = ev["seq"]
                yield f"data: {json.dumps(ev)}\n\n"
            if not batch and not runs.manager.is_running(run_id):
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/agents/workflows")
def agent_workflows() -> dict[str, Any]:
    """Topology of each workflow, for the Agents view."""
    return agents.workflow_json()


def run() -> None:
    uvicorn.run("cyoa.main:app", host="127.0.0.1", port=int(os.environ.get("CYOA_PORT", "8765")), reload=True)
