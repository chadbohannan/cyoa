"""FastAPI app: story CRUD, graph read, and an LLM passthrough."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db, llm
from .models import (
    Choice,
    ChoiceCreate,
    Passage,
    PassageCreate,
    Story,
    StoryCreate,
    StoryGraph,
    Variable,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="cyoa", version="0.1.0", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "db": str(db.DB_PATH)}


# --- stories ---------------------------------------------------------------


@app.get("/api/stories")
def list_stories() -> list[Story]:
    with db.transaction() as conn:
        rows = conn.execute("SELECT * FROM stories ORDER BY id").fetchall()
    return [Story(**dict(r)) for r in rows]


@app.post("/api/stories", status_code=201)
def create_story(body: StoryCreate) -> Story:
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO stories (title, description) VALUES (?, ?)",
            (body.title, body.description),
        )
        row = conn.execute("SELECT * FROM stories WHERE id = ?", (cur.lastrowid,)).fetchone()
    return Story(**dict(row))


@app.delete("/api/stories/{story_id}", status_code=204)
def delete_story(story_id: int) -> None:
    with db.transaction() as conn:
        if conn.execute("DELETE FROM stories WHERE id = ?", (story_id,)).rowcount == 0:
            raise HTTPException(404, "story not found")


@app.get("/api/stories/{story_id}/graph")
def story_graph(story_id: int) -> StoryGraph:
    with db.transaction() as conn:
        story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
        if story is None:
            raise HTTPException(404, "story not found")
        passages = conn.execute(
            "SELECT * FROM passages WHERE story_id = ? ORDER BY id", (story_id,)
        ).fetchall()
        choices = conn.execute(
            "SELECT * FROM choices WHERE story_id = ? ORDER BY from_passage, sort_order, id",
            (story_id,),
        ).fetchall()
        variables = conn.execute(
            "SELECT * FROM variables WHERE story_id = ? ORDER BY name", (story_id,)
        ).fetchall()
    return StoryGraph(
        story=Story(**dict(story)),
        passages=[Passage(**dict(p)) for p in passages],
        choices=[_choice(c) for c in choices],
        variables=[Variable(**{**dict(v), "default": json.loads(v["default"])}) for v in variables],
    )


def _choice(row) -> Choice:
    d = dict(row)
    d["condition"] = json.loads(d["condition"])
    d["effects"] = json.loads(d["effects"])
    return Choice(**d)


# --- passages & choices ----------------------------------------------------


@app.post("/api/stories/{story_id}/passages", status_code=201)
def create_passage(story_id: int, body: PassageCreate) -> Passage:
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO passages (story_id, title, body, is_start, pos_x, pos_y)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (story_id, body.title, body.body, int(body.is_start), body.pos_x, body.pos_y),
        )
        row = conn.execute("SELECT * FROM passages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return Passage(**dict(row))


@app.post("/api/stories/{story_id}/choices", status_code=201)
def create_choice(story_id: int, body: ChoiceCreate) -> Choice:
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO choices (story_id, from_passage, to_passage, text, sort_order, condition, effects)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                story_id,
                body.from_passage,
                body.to_passage,
                body.text,
                body.sort_order,
                json.dumps(body.condition),
                json.dumps(body.effects),
            ),
        )
        row = conn.execute("SELECT * FROM choices WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _choice(row)


# --- llm -------------------------------------------------------------------


class GenerateRequest(BaseModel):
    prompt: str
    model: str | None = None
    system: str | None = None


@app.get("/api/llm/models")
async def llm_models() -> list[str]:
    try:
        return await llm.list_models()
    except httpx.HTTPError as e:
        raise HTTPException(503, f"ollama unreachable at {llm.OLLAMA_URL}: {e}")


@app.post("/api/llm/generate")
async def llm_generate(body: GenerateRequest) -> dict:
    try:
        text = await llm.generate(body.prompt, model=body.model, system=body.system)
    except httpx.HTTPError as e:
        raise HTTPException(503, f"ollama error: {e}")
    return {"text": text}


def run() -> None:
    uvicorn.run("cyoa.main:app", host="127.0.0.1", port=int(os.environ.get("CYOA_PORT", "8765")), reload=True)
