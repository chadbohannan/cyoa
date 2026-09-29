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
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agents, db, engine, llm, prompts, runs, twee
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
    MergeRequest,
    PacingPlan,
    Passage,
    PassageCreate,
    PassagePatch,
    PlayChoose,
    PlayStart,
    PlayView,
    Playtest,
    PlaytestRequest,
    Problems,
    PromoteRequest,
    PromoteResult,
    PruneResult,
    PromptInfo,
    PromptPreview,
    PromptPreviewRequest,
    PromptUpdate,
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

# Row helpers moved to engine.py for sharing with agents
_decode = engine.decode_row
_encode = engine.encode_values
JSON_COLS = engine.JSON_COLS


# _insert moved to engine.py for sharing with agents
_insert = engine.insert_row


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
    passage = Passage(**_decode("passages", row))
    # is_ending is authoritative: a decided ending shows no choices even if some
    # are still attached to the row (e.g. left over from before it was marked).
    if passage.is_ending:
        return PlayView(passage=passage, choices=[], state=state)
    choices = [
        _decode("choices", r)
        for r in conn.execute(
            "SELECT * FROM choices WHERE from_passage = ? ORDER BY sort_order, id", (passage_id,)
        ).fetchall()
    ]
    return PlayView(
        passage=passage,
        choices=[Choice(**c) for c in choices if engine.check(c["condition"], state)],
        state=state,
    )


def _variables(conn: sqlite3.Connection, story_id: int) -> list[dict[str, Any]]:
    return [_decode("variables", r) for r in conn.execute("SELECT * FROM variables WHERE story_id = ?", (story_id,))]


def _passages(conn: sqlite3.Connection, story_id: int) -> list[dict[str, Any]]:
    return [_decode("passages", r) for r in conn.execute("SELECT * FROM passages WHERE story_id = ? ORDER BY id", (story_id,))]


def _choices(conn: sqlite3.Connection, story_id: int) -> list[dict[str, Any]]:
    return [_decode("choices", r) for r in conn.execute("SELECT * FROM choices WHERE story_id = ? ORDER BY from_passage, sort_order, id", (story_id,))]


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
        passages, choices = _passages(conn, story_id), _choices(conn, story_id)
        return Problems(**engine.problems(story["start_passage_id"], passages, choices, _variables(conn, story_id)))


@app.post("/api/stories/{story_id}/playtest")
def playtest(story_id: int, body: PlaytestRequest) -> Playtest:
    """Random playthroughs through the same evaluator as the player; a coverage report, not a run."""
    with db.transaction() as conn:
        story = _require_story(conn, story_id)
        if story["start_passage_id"] is None:
            raise HTTPException(409, "story has no start passage")
        return Playtest(
            **engine.random_playthroughs(
                story["start_passage_id"], _passages(conn, story_id), _choices(conn, story_id), _variables(conn, story_id),
                runs=body.runs, max_steps=body.max_steps, seed=body.seed,
            )
        )


@app.get("/api/stories/{story_id}/export.twee", response_class=PlainTextResponse)
def export_twee(story_id: int) -> PlainTextResponse:
    with db.transaction() as conn:
        story = _require_story(conn, story_id)
        text = twee.export(dict(story), _passages(conn, story_id), _choices(conn, story_id), _variables(conn, story_id))
    name = "".join(ch if ch.isalnum() or ch in "-_ " else "" for ch in story["title"]).strip() or "story"
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}.twee"'})


# --- promotion & merging -------------------------------------------------------
#
# Review is promotion: flipping `reviewed` over a branch at once rather than
# passage by passage. Merging folds duplicate scenes (or a crowd of stub
# endings) into one survivor; it is the one edit here that touches several
# rows, so it is a route rather than a series of patches from the frontend.


@app.post("/api/stories/{story_id}/promote")
def promote(story_id: int, body: PromoteRequest) -> PromoteResult:
    with db.transaction() as conn:
        story = _require_story(conn, story_id)
        ids = set(body.passage_ids)
        if body.root is not None:
            ids |= set(engine.subtree(body.root, story["start_passage_id"], _passages(conn, story_id), _choices(conn, story_id)))
        marks = ", ".join("?" for _ in ids)
        rows = conn.execute(f"SELECT id FROM passages WHERE story_id = ? AND id IN ({marks})", (story_id, *ids)).fetchall() if ids else []
        found = sorted(r["id"] for r in rows)
        if found:
            conn.execute(f"UPDATE passages SET reviewed = ? WHERE story_id = ? AND id IN ({marks})", (int(body.reviewed), story_id, *ids))
            db.touch_story(conn, story_id)
        return PromoteResult(passage_ids=found)


@app.post("/api/stories/{story_id}/passages/{passage_id}/prune")
def prune_passage(story_id: int, passage_id: int) -> PruneResult:
    """Delete a passage's choices and its subtree, leaving it a frontier again.

    The subtree is the collapse/promote one, so a passage the rest of the map
    still reaches survives; only the choices leading into it from here go.
    """
    with db.transaction() as conn:
        story = _require_story(conn, story_id)
        if conn.execute("SELECT 1 FROM passages WHERE id = ? AND story_id = ?", (passage_id, story_id)).fetchone() is None:
            raise HTTPException(404, "passage not found")
        doomed = engine.subtree(passage_id, story["start_passage_id"], _passages(conn, story_id), _choices(conn, story_id))[1:]
        sources = [passage_id, *doomed]
        marks = ", ".join("?" for _ in sources)
        choice_ids = sorted(r["id"] for r in conn.execute(
            f"SELECT id FROM choices WHERE story_id = ? AND from_passage IN ({marks})", (story_id, *sources)))
        conn.execute(f"DELETE FROM choices WHERE story_id = ? AND from_passage IN ({marks})", (story_id, *sources))
        if doomed:
            marks = ", ".join("?" for _ in doomed)
            conn.execute(f"DELETE FROM passages WHERE story_id = ? AND id IN ({marks})", (story_id, *doomed))
        if choice_ids or doomed:
            db.touch_story(conn, story_id)
        return PruneResult(passage_ids=sorted(doomed), choice_ids=choice_ids)


@app.post("/api/stories/{story_id}/passages/merge")
def merge_passages(story_id: int, body: MergeRequest) -> Passage:
    with db.transaction() as conn:
        story = _require_story(conn, story_id)
        others = [pid for pid in dict.fromkeys(body.passage_ids) if pid != body.into]
        for pid in (body.into, *others):
            if conn.execute("SELECT 1 FROM passages WHERE id = ? AND story_id = ?", (pid, story_id)).fetchone() is None:
                raise HTTPException(404, f"passage {pid} not found")
        if not others:
            raise HTTPException(422, "nothing to merge")
        marks = ", ".join("?" for _ in others)
        conn.execute(f"UPDATE choices SET to_passage = ? WHERE story_id = ? AND to_passage IN ({marks})", (body.into, story_id, *others))
        conn.execute(f"UPDATE choices SET from_passage = ? WHERE story_id = ? AND from_passage IN ({marks})", (body.into, story_id, *others))
        # a choice from a merged passage that led back into the group is now a self-loop; drop it
        conn.execute("DELETE FROM choices WHERE story_id = ? AND from_passage = ? AND to_passage = ?", (story_id, body.into, body.into))
        if story["start_passage_id"] in others:
            conn.execute("UPDATE stories SET start_passage_id = ? WHERE id = ?", (body.into, story_id))
        conn.execute(f"DELETE FROM passages WHERE story_id = ? AND id IN ({marks})", (story_id, *others))
        db.touch_story(conn, story_id)
        row = conn.execute("SELECT * FROM passages WHERE id = ?", (body.into,)).fetchone()
        return Passage(**_decode("passages", row))


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


# _materialise moved to engine.py for sharing with agents
_materialise = engine.materialise


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
        budget, steer, policy = body.budget, body.steer, body.policy
        if body.workflow == "expand_frontier":
            # The run copies the story's plan (or one given for this run alone); the request's
            # steer and policy go over the plan's run-wide ones.
            plan = _valid_plan(budget) if budget else _load_plan(conn, story_id)
            budget, steer = plan, steer if steer is not None else plan["steer"]
            policy = {**plan["policy"], **policy}
    try:
        row = runs.manager.start(story_id, body.workflow, scope, {"model": body.model, "steer": steer}, budget, policy)
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
    """Topology of each workflow and the loop the Agents view draws for it."""
    return agents.workflow_json()


# --- pacing ------------------------------------------------------------------------
#
# A story's pacing plan (concepts/act-pacing.md): the acts expand_frontier paces a
# run by, then its fixed ending. Stored whole, as the Agents view edits it; a story
# with no saved plan uses agents.DEFAULT_PLAN.


def _valid_plan(data: dict[str, Any]) -> dict[str, Any]:
    try:
        return PacingPlan(**data).model_dump()
    except ValueError as e:
        raise HTTPException(422, f"invalid pacing plan: {e}")


def _load_plan(conn: sqlite3.Connection, story_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT plan FROM pacing WHERE story_id = ?", (story_id,)).fetchone()
    return json.loads(row["plan"]) if row else PacingPlan(**agents.DEFAULT_PLAN).model_dump()


@app.get("/api/stories/{story_id}/pacing")
def get_pacing(story_id: int) -> PacingPlan:
    with db.transaction() as conn:
        _require_story(conn, story_id)
        return PacingPlan(**_load_plan(conn, story_id))


@app.put("/api/stories/{story_id}/pacing")
def save_pacing(story_id: int, body: PacingPlan) -> PacingPlan:
    with db.transaction() as conn:
        _require_story(conn, story_id)
        conn.execute("INSERT INTO pacing (story_id, plan) VALUES (?, ?) ON CONFLICT (story_id) DO UPDATE SET plan = excluded.plan",
                     (story_id, json.dumps(body.model_dump())))
        db.touch_story(conn, story_id)
    return body


@app.delete("/api/stories/{story_id}/pacing")
def reset_pacing(story_id: int) -> PacingPlan:
    """Forget the saved plan; the story is back on the default."""
    with db.transaction() as conn:
        _require_story(conn, story_id)
        conn.execute("DELETE FROM pacing WHERE story_id = ?", (story_id,))
        return PacingPlan(**_load_plan(conn, story_id))


# --- prompts -------------------------------------------------------------------
#
# Prompt roles are global: one text per kind of model call, for every story
# (prompts.py). The story in the preview route only supplies a passage to
# assemble the context frame from.


def _prompt_info(key: str, overrides: dict[str, str]) -> PromptInfo:
    p = prompts.PROMPTS[key]
    return PromptInfo(key=key, label=p.label, doc=p.doc, placeholders=list(p.placeholders),
                      used_by=[wn for wn, keys in agents.NODE_PROMPTS.items() if key in keys],
                      default=p.default, override=overrides.get(key))


def _require_prompt(key: str) -> None:
    if key not in prompts.PROMPTS:
        raise HTTPException(404, f"unknown prompt {key!r}")


@app.get("/api/agents/prompts")
def list_prompts() -> list[PromptInfo]:
    overrides = prompts.overrides()
    return [_prompt_info(k, overrides) for k in prompts.PROMPTS]


@app.put("/api/agents/prompts/{key}")
def save_prompt(key: str, body: PromptUpdate) -> PromptInfo:
    _require_prompt(key)
    try:
        prompts.save(key, body.text)
    except prompts.PromptError as e:
        raise HTTPException(422, str(e))
    return _prompt_info(key, prompts.overrides())


@app.delete("/api/agents/prompts/{key}")
def reset_prompt(key: str) -> PromptInfo:
    _require_prompt(key)
    prompts.reset(key)
    return _prompt_info(key, prompts.overrides())


@app.post("/api/stories/{story_id}/prompts/{key}/preview")
def preview_prompt(story_id: int, key: str, body: PromptPreviewRequest) -> PromptPreview:
    """The exact text `agents.compose` would send for this passage, without the model.
    {choice} is the passage's first outgoing choice, {tone} the rewrite verb's default, and
    the frontier pacing slots what the first level of `body.stage` in the story's plan would use."""
    _require_prompt(key)
    with db.transaction() as conn:
        _require_story(conn, story_id)
        _require_passage(conn, story_id, body.passage_id)
        plan = body.plan.model_dump() if body.plan else _load_plan(conn, story_id)
    roles = prompts.resolve()
    if body.text is not None:
        try:
            prompts.validate(key, body.text)
        except prompts.PromptError as e:
            raise HTTPException(422, str(e))
        roles[key] = body.text
    ctx = agents.gather_context({"story_id": story_id, "passage_id": body.passage_id, "params": {}})["context"]
    choice = ctx["outgoing"][0]["text"] if ctx["outgoing"] else "(example choice)"
    return PromptPreview(prompt=agents.compose(ctx, roles, key, choice=choice, tone="vivid",
                                               **agents.preview_fields(plan, body.stage or 0, key)))


# --- built frontend (make serve) ---------------------------------------------
#
# All API routes are under /api, so mounting the Vite build at "/" is safe: FastAPI
# tries the routes above first and only falls through to static files (and, via
# html=True, to index.html for any unmatched path) otherwise. The app uses a hash
# router, so index.html alone covers every client-side route. Mounted last so it
# never shadows an /api route; absent in dev, where Vite serves the frontend itself.
_FRONTEND_DIST = Path(os.environ.get("CYOA_FRONTEND_DIST", Path(__file__).resolve().parents[3] / "frontend" / "dist"))
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")


def run() -> None:
    reload = os.environ.get("CYOA_RELOAD", "1") not in ("0", "false", "False")
    uvicorn.run("cyoa.main:app", host="127.0.0.1", port=int(os.environ.get("CYOA_PORT", "8765")), reload=reload)
