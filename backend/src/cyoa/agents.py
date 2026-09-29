"""Authoring agents as LangGraph workflows: the targeted verbs, canon_check and
expand_frontier.

Each verb is the same explicit four-node graph — gather_context → generate →
parse_validate → propose — over a story neighbourhood, with a retry edge from
parse_validate back to generate. The model returns structured output for a
verb-specific Pydantic schema; parse_validate does the checks a schema cannot
(titles resolve, variables exist, text is non-empty) and feeds the error back
into the next attempt. Only `propose` writes, and it writes proposals, never
passages: the human accepts them in the inspector (see main.py).

Running single-shot verbs through LangGraph rather than a plain function is
deliberate — the fan-out workflows below reuse their nodes and prompts, and
every graph's topology is drawn in the Agents view (workflow_json).

Every model call goes through `_ask`, which holds a semaphore: against a single
local model instance, the GPU cannot take two contexts at once, so the logical
fan-out (`Send`, and the in-process `_fan` helper below) is physically a queue
of one. Against a load-balancing proxy fronting a pool of instances, the proxy
owns request queuing itself, so the gate is raised to match the pool size
instead (`CYOA_AGENT_CONCURRENCY`) and calls really do run concurrently. The
gate lives here rather than in LangGraph's `max_concurrency` because that
setting sizes LangGraph's own executor and once deadlocked a streamed run when
set low (see runs.py); left unset, as it is, LangGraph already runs `Send`
tasks with plenty of headroom (Python's default `ThreadPoolExecutor` sizing),
so `_GATE` is the only real limiter either way. A semaphore also throttles
across runs and the synchronous verbs, which a per-graph setting would not.

A node that belongs to a run (runs.py) reports and checks in through the
`RunHandle` it finds in its config; outside a run the null handle does nothing,
so the verbs pay nothing.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import json
import operator
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Annotated, Any, Callable, Literal, TypedDict, TypeVar

from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt
from pydantic import BaseModel, Field

from . import db, engine, llm, prompts

MAX_ATTEMPTS = 3
LORE_CAP = 20
EVENTS_CAP = 12
SPINE_CAP = 20  # passages of the reader's path shown in full above the current one
# How many model calls may be in flight across the whole process. Two were
# enough to OOM the GPU with a 27B model on 2026-09-20, so the default is a
# queue. Raise it only for a server that is known to batch.
MAX_CONCURRENCY = int(os.environ.get("CYOA_AGENT_CONCURRENCY", "1"))
_GATE = threading.Semaphore(MAX_CONCURRENCY)

T = TypeVar("T")
R = TypeVar("R")


def _fan(items: list[T], worker: Callable[[T], R]) -> list[R]:
    """Run `worker` over `items` breadth-first, up to MAX_CONCURRENCY at once. The
    actual concurrent *model-call* count is still bounded by _GATE regardless of how
    many items are in flight; this only controls how many are waiting on it at a time.
    `worker` must not raise — a caller that needs per-item failure isolation (e.g.
    _draft_choice) catches its own exceptions and returns a result describing the
    failure instead.

    A plain ThreadPoolExecutor does not inherit contextvars, so a worker thread would
    otherwise lose LangGraph's `get_config()` (get_config() raises "outside of a
    runnable context") and, with it, `_run()`'s access to the RunHandle — silently
    turning progress, cancellation and activity/retry events into no-ops inside every
    fanned-out call. Each item gets its own `contextvars.copy_context()` snapshot,
    submitted as `ctx.run(worker, item)`, the same per-task-copy pattern LangGraph's
    own sync executor uses (langgraph.pregel._executor.BackgroundExecutor.submit) —
    a Context object can't be entered by more than one call at a time, so it must be
    copied fresh per task, not shared across the pool."""
    if not items:
        return []
    if len(items) == 1:
        return [worker(items[0])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(items), max(1, MAX_CONCURRENCY))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, worker, item) for item in items]
        return [f.result() for f in futures]


def _ask(params: "Params", schema: type[BaseModel], prompt: str, *, temperature: float = 0.8,
         activity: dict[str, Any] | None = None, reasoning: bool = False) -> BaseModel:
    """One structured-output model call, throttled by the process-wide gate. `activity`
    (passage_id, phase) is reported to the run only once the call actually starts, so
    the map highlights what the model is working on, not what is queued behind it."""
    model = llm.chat_model(params.get("model"), temperature=temperature, reasoning=reasoning).with_structured_output(schema, method=llm.structured_method())
    with _GATE:
        run = _run()
        if activity:
            run.emit({"type": "activity", **activity})
        try:
            out = model.invoke(prompt)
        finally:
            if activity:
                run.emit({"type": "activity", "passage_id": activity["passage_id"], "phase": "done"})
    return out if isinstance(out, schema) else schema(**(out or {}))


def _blank(text: str | None) -> bool:
    """No word characters at all: empty, or a bare '...' the model left as a placeholder."""
    return not re.sub(r"[\W_]", "", text or "")


def _placeholder_body(text: str | None) -> bool:
    """A body that is blank, or whose every line trails off in an ellipsis ('You fit...' / '...').
    llama-served models return these instead of prose often enough to need catching (storyline eval, 2026-09-28)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return _blank(text) or all(ln.endswith(("...", "\u2026")) for ln in lines)


# --- run plumbing -------------------------------------------------------------


class RunCancelled(Exception):
    pass


class RunHandle:
    """What a node knows about the run it belongs to: where to report events and
    progress, and whether the author has cancelled. runs.py builds one per launch
    and passes it as config["configurable"]["__run"] (the prefix keeps it out of
    checkpoint metadata). `progress` is the run's cumulative progress dict; every
    change is emitted whole, so nobody downstream has to add things up."""

    def __init__(self, emit: Callable[[dict[str, Any]], None] | None = None, cancel: threading.Event | None = None,
                 progress: dict[str, Any] | None = None) -> None:
        self._emit = emit
        self._cancel = cancel
        self._lock = threading.Lock()  # Send tasks report concurrently
        self.progress = dict(progress or {})

    def emit(self, event: dict[str, Any]) -> None:
        if self._emit:
            self._emit(event)

    def check(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise RunCancelled("run cancelled")

    def report(self, **fields: Any) -> None:
        with self._lock:
            self.progress.update(fields)
            self.emit({"type": "progress", **self.progress})

    def tick(self, key: str) -> None:
        with self._lock:
            self.progress[key] = self.progress.get(key, 0) + 1
            self.emit({"type": "progress", **self.progress})


NULL_HANDLE = RunHandle()


def _run() -> RunHandle:
    """The current run's handle, or the null handle outside a run (verbs, tests)."""
    try:
        return (get_config().get("configurable") or {}).get("__run") or NULL_HANDLE
    except RuntimeError:  # not inside a graph at all
        return NULL_HANDLE


# --- output schemas -------------------------------------------------------------


class BodyDraft(BaseModel):
    body: str = Field(description="the passage text, second person present tense, one to three short paragraphs")


class ChoiceDraft(BaseModel):
    text: str = Field(description="what the reader sees on the choice button, under ten words")
    target_title: str = Field(description="title of the passage this leads to: an existing title, or a new one")


class ChoiceDrafts(BaseModel):
    choices: list[ChoiceDraft] = Field(description="distinct choices: as many as the TASK allows, otherwise two to four")


class PassageDraft(BaseModel):
    title: str = Field(description="a short passage title")
    body: str = Field(description="the passage text, second person present tense, one to three short paragraphs")


class FrontierDraft(BaseModel):
    """expand_frontier's passage. No length in the schema: it reaches the model on every
    call and would override the pacing plan's {length} (concepts/act-pacing.md)."""

    title: str = Field(description="a short passage title")
    body: str = Field(description="the passage text, second person present tense, as long as the TASK asks")


class FrontierPassageDraft(FrontierDraft):
    ends_story: bool = Field(False, description="true only if this passage brings the storyline to a natural conclusion")


class NextStep(BaseModel):
    decision: Literal["branch", "continue", "end"] = Field(
        description="'branch' if PASSAGE should offer the reader 2 to 3 meaningfully different choices; "
        "'continue' if the story should advance on a single path with no real decision yet; "
        "'end' if PASSAGE is a satisfying stopping point and should get no more choices"
    )
    reasoning: str = Field(description="one sentence explaining the call, shown to the author")
    choices: list[ChoiceDraft] = Field(default_factory=list, description="2 to 3 choices; populate only when decision is 'branch'")
    continuation_title: str = Field(default="", description="the next passage's title; populate only when decision is 'continue'")
    continuation_body: str = Field(default="", description="the next passage's body, following directly from PASSAGE; populate only when decision is 'continue'")


SCHEMAS: dict[str, type[BaseModel]] = {
    "draft_body": BodyDraft,
    "suggest_choices": ChoiceDrafts,
    "draft_target": PassageDraft,
    "rewrite": BodyDraft,
    "suggest_next": NextStep,
}


# --- state ------------------------------------------------------------------------


class Params(TypedDict, total=False):
    model: str | None
    tone: str | None
    choice_id: int | None
    steer: str | None
    prompts: dict[str, str]  # role -> text; a run's snapshot (runs.py), a verb's resolve()


class VerbState(TypedDict, total=False):
    story_id: int
    passage_id: int
    verb: str
    params: Params
    context: dict[str, Any]
    prompt: str
    draft: dict[str, Any] | None
    error: str | None
    attempts: int
    batch: str
    proposals: list[dict[str, Any]]


# --- nodes ------------------------------------------------------------------------


def gather_context(state: VerbState) -> VerbState:
    """Read the passage's neighbourhood: story, variables, in/out links, lore, events at t."""
    with db.transaction() as conn:
        story = dict(conn.execute("SELECT * FROM stories WHERE id = ?", (state["story_id"],)).fetchone())
        passage = dict(conn.execute("SELECT * FROM passages WHERE id = ?", (state["passage_id"],)).fetchone())
        passage["meta"] = json.loads(passage["meta"])
        variables = [dict(r) for r in conn.execute("SELECT name, kind, \"default\" FROM variables WHERE story_id = ? ORDER BY name", (story["id"],))]
        titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM passages WHERE story_id = ? ORDER BY id", (story["id"],))}
        inbound = [
            dict(r)
            for r in conn.execute(
                "SELECT p.title, p.body, c.text FROM choices c JOIN passages p ON p.id = c.from_passage"
                " WHERE c.to_passage = ? ORDER BY c.id",
                (passage["id"],),
            )
        ]
        outgoing = [
            dict(r)
            for r in conn.execute(
                "SELECT c.id, c.text, c.sort_order, p.title AS target FROM choices c LEFT JOIN passages p ON p.id = c.to_passage"
                " WHERE c.from_passage = ? ORDER BY c.sort_order, c.id",
                (passage["id"],),
            )
        ]
        lore = [dict(r) for r in conn.execute("SELECT kind, name, body FROM lore WHERE story_id = ? ORDER BY kind, name LIMIT ?", (story["id"], LORE_CAP))]
        if passage["t"] is None:
            events = [dict(r) for r in conn.execute("SELECT t, name, body FROM events WHERE story_id = ? ORDER BY t LIMIT ?", (story["id"], EVENTS_CAP))]
        else:
            events = [dict(r) for r in conn.execute("SELECT t, name, body FROM events WHERE story_id = ? AND t <= ? ORDER BY t DESC LIMIT ?", (story["id"], passage["t"], EVENTS_CAP))][::-1]
        spine = _spine(conn, story["start_passage_id"], passage["id"])
        choice = None
        if state["params"].get("choice_id") is not None:
            row = conn.execute("SELECT * FROM choices WHERE id = ?", (state["params"]["choice_id"],)).fetchone()
            choice = dict(row) if row else None
    return {"context": {"story": story, "passage": passage, "variables": variables, "titles": titles,
                        "spine": spine, "inbound": inbound, "outgoing": outgoing, "lore": lore, "events": events, "choice": choice},
            "attempts": 0, "batch": uuid.uuid4().hex}


def _spine(conn: sqlite3.Connection, start: int | None, passage_id: int) -> list[dict[str, Any]]:
    """The reader's path from the story start to this passage, oldest first, as
    {title, body, choice} steps where `choice` is the one taken out of that step. Each
    step back follows the earliest inbound choice; the walk stops at the start, at a
    passage nothing leads to, on a cycle, or after SPINE_CAP steps."""
    steps: list[dict[str, Any]] = []
    seen, cur = {passage_id}, passage_id
    while cur != start and len(steps) < SPINE_CAP:
        row = conn.execute(
            "SELECT p.id, p.title, p.body, c.text FROM choices c JOIN passages p ON p.id = c.from_passage"
            " WHERE c.to_passage = ? ORDER BY c.id LIMIT 1", (cur,)).fetchone()
        if row is None or row["id"] in seen:
            break
        seen.add(row["id"])
        steps.append({"title": row["title"], "body": row["body"], "choice": row["text"]})
        cur = row["id"]
    return steps[::-1]


def render_context(ctx: dict[str, Any]) -> str:
    s, p = ctx["story"], ctx["passage"]
    lines = [f"STORY: {s['title']}", s["description"] or "(no description)", ""]
    if ctx["variables"]:
        lines += ["VARIABLES: " + ", ".join(f"{v['name']} ({v['kind']}, default {v['default']})" for v in ctx["variables"]), ""]
    if ctx["lore"]:
        lines += ["WORLD:"] + [f"- [{l['kind']}] {l['name']}: {l['body']}" for l in ctx["lore"]] + [""]
    if ctx["events"]:
        lines += ["TIMELINE (what has happened by now):"] + [f"- t={_num(e['t'])} {e['name']}: {e['body']}" for e in ctx["events"]] + [""]
    where = ", ".join(x for x in [
        f"t={_num(p['t'])}" if p["t"] is not None else "",
        f"location: {p['meta'].get('location')}" if p["meta"].get("location") else "",
        f"present: {', '.join(p['meta'].get('present', []))}" if p["meta"].get("present") else "",
    ] if x)
    spine = ctx.get("spine") or []
    if spine:
        # Everything the reader has read on the way here, so facts set several passages up still bind.
        lines += ["STORY SO FAR (the reader's path to this passage, oldest first; stay consistent with it):"]
        for step in spine:
            lines += [f"[{step['title']}]", step["body"] or "(empty)", f"(the reader chose: {step['choice']})", ""]
    lines += [f"PASSAGE: {p['title']}" + (f" ({where})" if where else ""), p["body"] or "(empty)", ""]
    # The spine already shows the inbound link it came through; list only the others.
    last = (spine[-1]["title"], spine[-1]["choice"]) if spine else None
    inbound = [i for i in ctx["inbound"] if (i["title"], i["text"]) != last]
    if inbound:
        lines += ["ALSO REACHED FROM:" if spine else "REACHED FROM:"] + [f"- '{i['title']}' via choice '{i['text']}': {_excerpt(i['body'])}" for i in inbound] + [""]
    if ctx["outgoing"]:
        lines += ["EXISTING CHOICES:"] + [f"- '{o['text']}' -> {o['target'] or '(no target yet)'}" for o in ctx["outgoing"]] + [""]
    lines += ["ALL PASSAGE TITLES: " + "; ".join(ctx["titles"].values())]
    return "\n".join(lines)


def compose(ctx: dict[str, Any], prompts: dict[str, str], key: str, *, steer: str = "", error: str | None = None,
            **fields: str) -> str:
    """The one place a prompt is assembled: the context frame, the role's TASK text
    formatted with `fields`, then the retry line and the author's steer when set.
    Roles are data (prompts.py); the frame, retry and steer wording stay code."""
    prompt = render_context(ctx) + "\n\nTASK: " + prompts[key].format(**fields)
    if error:
        prompt += f"\n\nYour previous answer was rejected: {error}. Try again."
    if steer.strip():
        prompt += f"\n\nAUTHOR'S DIRECTION for everything you write: {steer.strip()}"
    return prompt


def _prompts(params: Params) -> dict[str, str]:
    """The roles a call uses: the snapshot in params, over today's texts for any role
    the snapshot predates."""
    return {**prompts.resolve(), **(params.get("prompts") or {})}


# (workflow, node) -> the roles that node composes. Drawn on the Agents view and
# checked against what the nodes actually call by test_node_prompts_match_behaviour.
NODE_PROMPTS: dict[tuple[str, str], list[str]] = {
    ("verb", "generate"): ["draft_body", "suggest_choices", "draft_target", "rewrite", "suggest_next"],
    ("canon_check", "check_passage"): ["canon_check"],
    ("expand_frontier", "grow"): ["frontier_choices", "frontier_passage", "frontier_ending",
                                   "audit_continuity", "audit_canon", "revise_passage"],
}


def generate(state: VerbState) -> VerbState:
    ctx, verb, params = state["context"], state["verb"], state["params"]
    prompt = compose(ctx, _prompts(params), verb, error=state.get("error"),
                     choice=(ctx["choice"] or {}).get("text", ""), tone=params.get("tone") or "vivid")
    try:
        draft = _ask(params, SCHEMAS[verb], prompt).model_dump()
        return {"prompt": prompt, "draft": draft, "error": None, "attempts": state["attempts"] + 1}
    except Exception as e:  # model returned nothing parseable
        return {"prompt": prompt, "draft": None, "error": f"{type(e).__name__}: {e}", "attempts": state["attempts"] + 1}


def parse_validate(state: VerbState) -> VerbState:
    """Checks a JSON schema cannot express. Sets `error` (and clears `draft`) on failure."""
    draft, verb = state.get("draft"), state["verb"]
    if draft is None:
        return {"error": state.get("error") or "no output"}
    if verb in ("draft_body", "rewrite"):
        if _placeholder_body(draft.get("body")):
            return {"draft": None, "error": "body was empty or a '...' placeholder; write the full passage"}
        if verb == "rewrite" and draft["body"].strip() == state["context"]["passage"]["body"].strip():
            return {"draft": None, "error": "the rewrite was identical to the original"}
    elif verb == "draft_target":
        if _blank(draft.get("title")) or _placeholder_body(draft.get("body")):
            return {"draft": None, "error": "title and body must both be written out, not empty or '...'"}
    elif verb == "suggest_choices":
        seen = {o["text"].strip().lower() for o in state["context"]["outgoing"]}
        kept = []
        for c in draft.get("choices", []):
            text = c.get("text", "").strip()
            if not _blank(text) and text.lower() not in seen:
                seen.add(text.lower())
                kept.append({"text": text, "target_title": c.get("target_title", "").strip()})
        if not kept:
            return {"draft": None, "error": "every suggested choice was empty or already exists"}
        return {"draft": {"choices": kept}, "error": None}
    elif verb == "suggest_next":
        decision = draft.get("decision")
        reasoning = draft.get("reasoning", "").strip() or "no reasoning given"
        if decision == "branch":
            seen = {o["text"].strip().lower() for o in state["context"]["outgoing"]}
            kept = []
            for c in draft.get("choices", []):
                text = c.get("text", "").strip()
                if not _blank(text) and text.lower() not in seen:
                    seen.add(text.lower())
                    kept.append({"text": text, "target_title": c.get("target_title", "").strip()})
            if not kept:
                return {"draft": None, "error": "decision was 'branch' but no distinct choices were suggested"}
            return {"draft": {"decision": "branch", "reasoning": reasoning, "choices": kept}, "error": None}
        if decision == "continue":
            title = draft.get("continuation_title", "").strip()
            body = draft.get("continuation_body", "").strip()
            if _blank(title) or _placeholder_body(body):
                return {"draft": None, "error": "decision was 'continue' but the next passage's title or body was empty"}
            return {"draft": {"decision": "continue", "reasoning": reasoning, "continuation_title": title, "continuation_body": body}, "error": None}
        if decision == "end":
            return {"draft": {"decision": "end", "reasoning": reasoning}, "error": None}
        return {"draft": None, "error": "decision must be 'branch', 'continue' or 'end'"}
    return {"error": None}


def after_validate(state: VerbState) -> Literal["generate", "propose", "fail"]:
    if not state.get("error"):
        return "propose"
    return "generate" if state["attempts"] < MAX_ATTEMPTS else "fail"


def propose(state: VerbState) -> VerbState:
    """Materialise the draft as proposal rows (if reviewed) or directly (if unreviewed).

    The reviewed/frontier rule: agents write directly where reviewed=0 and must propose where reviewed=1.
    This extends the same rule that expand_frontier's grow() node uses to targeted verbs.
    """
    ctx, draft, verb = state["context"], state["draft"] or {}, state["verb"]
    p = ctx["passage"]
    rows: list[dict[str, Any]] = []
    if verb in ("draft_body", "rewrite"):
        rows.append({"kind": "edit_passage", "target_passage": p["id"], "payload": {"body": draft["body"].strip()}})
    elif verb == "draft_target":
        choice = ctx["choice"]
        rows.append({"kind": "new_passage", "target_choice": choice["id"], "target_passage": p["id"],
                     "payload": {"title": draft["title"].strip(), "body": draft["body"].strip(),
                                 "pos_x": p["pos_x"], "pos_y": p["pos_y"] + 180}})
    elif verb == "suggest_choices":
        by_title = {t.lower(): i for i, t in ctx["titles"].items()}
        for i, c in enumerate(draft["choices"]):
            existing = by_title.get(c["target_title"].lower())
            rows.append({"kind": "new_choice", "target_passage": p["id"],
                         "payload": {"text": c["text"], "to_passage": existing,
                                     "new_target": None if existing else {"title": c["target_title"] or "New passage",
                                                                          "pos_x": p["pos_x"] + (i - 1) * 220, "pos_y": p["pos_y"] + 180}}})
    elif verb == "suggest_next":
        decision = draft["decision"]
        if decision == "branch":
            by_title = {t.lower(): i for i, t in ctx["titles"].items()}
            for i, c in enumerate(draft["choices"]):
                existing = by_title.get(c["target_title"].lower())
                rows.append({"kind": "new_choice", "target_passage": p["id"],
                             "payload": {"text": c["text"], "to_passage": existing,
                                         "new_target": None if existing else {"title": c["target_title"] or "New passage",
                                                                              "pos_x": p["pos_x"] + (i - 1) * 220, "pos_y": p["pos_y"] + 180}}})
        elif decision == "continue":
            rows.append({"kind": "new_choice", "target_passage": p["id"],
                         "payload": {"text": "Continue", "to_passage": None,
                                     "new_target": {"title": draft["continuation_title"], "body": draft["continuation_body"],
                                                    "pos_x": p["pos_x"], "pos_y": p["pos_y"] + 180}}})
        else:  # end
            rows.append({"kind": "edit_passage", "target_passage": p["id"],
                         "payload": {"is_ending": True, "reasoning": draft["reasoning"]}})

    # Extended reviewed/frontier rule: unreviewed passages get direct writes, reviewed ones get proposals
    with db.transaction() as conn:
        if p.get("reviewed"):
            # Target is reviewed (author-owned): propose for human approval
            out = [_insert_proposal(conn, state["story_id"], state["batch"], verb, r) for r in rows]
        else:
            # Target is unreviewed (experimental space): write directly
            for r in rows:
                engine.materialise(conn, state["story_id"], r)
            out = []
        db.touch_story(conn, state["story_id"])
    return {"proposals": out}


def fail(state: VerbState) -> VerbState:
    return {"proposals": []}


def _insert_proposal(conn: sqlite3.Connection, story_id: int, batch: str, verb: str, r: dict[str, Any],
                     run_id: int | None = None) -> dict[str, Any]:
    cur = conn.execute(
        "INSERT INTO proposals (story_id, batch, verb, kind, target_passage, target_choice, payload, run_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (story_id, batch, verb, r["kind"], r.get("target_passage"), r.get("target_choice"), json.dumps(r["payload"]), run_id),
    )
    row = dict(conn.execute("SELECT * FROM proposals WHERE id = ?", (cur.lastrowid,)).fetchone())
    row["payload"] = json.loads(row["payload"])
    return row


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def _excerpt(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# --- the graph ------------------------------------------------------------------


def build_verb_graph():
    g = StateGraph(VerbState)
    g.add_node("gather_context", gather_context)
    g.add_node("generate", generate)
    g.add_node("parse_validate", parse_validate)
    g.add_node("propose", propose)
    g.add_node("fail", fail)
    g.add_edge(START, "gather_context")
    g.add_edge("gather_context", "generate")
    g.add_edge("generate", "parse_validate")
    g.add_conditional_edges("parse_validate", after_validate, {"generate": "generate", "propose": "propose", "fail": "fail"})
    g.add_edge("propose", END)
    g.add_edge("fail", END)
    return g.compile()


VERB_GRAPH = build_verb_graph()


class VerbFailed(Exception):
    pass


def run_verb(story_id: int, passage_id: int, verb: str, params: Params) -> list[dict[str, Any]]:
    """Run one verb synchronously; returns the proposal rows it wrote."""
    params = {**params, "prompts": prompts.resolve()}
    final = VERB_GRAPH.invoke({"story_id": story_id, "passage_id": passage_id, "verb": verb, "params": params})
    if final.get("error") and not final.get("proposals"):
        raise VerbFailed(f"{verb} gave up after {final.get('attempts', 0)} attempt(s): {final['error']}")
    return final["proposals"]


# --- canon_check ------------------------------------------------------------------
#
# Reads every passage in scope against the lore and the events at its t and
# files a `note` proposal per inconsistency (optionally with an `edit_passage`
# fix). It is read-mostly and cheap, which is why it is the first autonomous
# workflow: `Send` maps one check per passage, and the reducer on `proposals`
# collects what each wrote. The fan-out is logical; `_ask` keeps the physical
# model calls to a queue. It only runs as a run (runs.py): the synchronous
# route it started life with was dropped once runs existed.


class CanonIssue(BaseModel):
    claim: str = Field(description="what the passage says or implies, quoted or paraphrased briefly")
    conflicts_with: str = Field(description="the lore entry or timeline event it contradicts, by name")
    severity: Literal["minor", "major"] = Field(description="major if a reader would notice the contradiction")


class CanonReport(BaseModel):
    issues: list[CanonIssue] = Field(description="empty when the passage is consistent with the world")
    fixed_body: str | None = Field(default=None, description="the passage body with only the contradictions repaired, or null if there are no issues")


class AuditIssue(BaseModel):
    severity: Literal["minor", "major"] = Field(description="major if a reader would notice")
    problem: str = Field(description="what is wrong, one sentence")
    quote: str = Field(description="a verbatim span of the draft the issue is about")
    evidence: str = Field(default="", description="a verbatim span of the spine, WORLD or TIMELINE the draft "
                          "contradicts, or empty for an issue with no such span")


class AuditReport(BaseModel):
    # "error" is never asked of the model (the role text only offers pass/revise); it's
    # _run_auditor's own value when every vote's call failed, so refine() and the audits
    # table can tell "no issues" from "couldn't tell."
    verdict: Literal["pass", "revise", "error"]
    issues: list[AuditIssue] = Field(default_factory=list)


class CanonState(TypedDict, total=False):
    run_id: int | None
    story_id: int
    passage_ids: list[int]
    params: Params
    batch: str
    proposals: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]


class CheckState(TypedDict, total=False):
    run_id: int | None
    story_id: int
    passage_id: int
    params: Params
    batch: str
    proposals: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]


def fan_out(state: CanonState) -> list[Send]:
    common = {"run_id": state.get("run_id"), "story_id": state["story_id"], "params": state["params"], "batch": state["batch"]}
    return [Send("check_passage", {**common, "passage_id": pid}) for pid in state["passage_ids"]]


def start_canon(state: CanonState) -> CanonState:
    return {"batch": uuid.uuid4().hex, "proposals": [], "errors": []}


def check_passage(state: CheckState) -> CheckState:
    """One passage: gather, ask, validate, propose — the verb pipeline inlined."""
    run = _run()
    run.check()
    try:
        return _check_one(state)
    finally:
        run.tick("checked")


def _check_one(state: CheckState) -> CheckState:
    ctx = gather_context({"story_id": state["story_id"], "passage_id": state["passage_id"], "params": {}})["context"]
    p = ctx["passage"]
    if not p["body"].strip():
        return {}
    prompt = compose(ctx, _prompts(state["params"]), "canon_check")
    try:
        report = _ask(state["params"], CanonReport, prompt, temperature=0.2, activity={"passage_id": p["id"], "phase": "check"})
    except Exception as e:
        return {"errors": [f"passage {p['id']} ({p['title']}): {type(e).__name__}: {e}"]}
    issues = [i for i in report.issues if i.claim.strip() and i.conflicts_with.strip()]
    if not issues:
        return {}
    rows = [{"kind": "note", "target_passage": p["id"],
             "payload": {"text": "; ".join(f"[{i.severity}] {i.claim} — conflicts with {i.conflicts_with}" for i in issues),
                         "issues": [i.model_dump() for i in issues]}}]
    fixed = (report.fixed_body or "").strip()
    if fixed and fixed != p["body"].strip():
        rows.append({"kind": "edit_passage", "target_passage": p["id"], "payload": {"body": fixed}})
    with db.transaction() as conn:
        out_rows = [_insert_proposal(conn, state["story_id"], state["batch"], "canon_check", r, state.get("run_id")) for r in rows]
        db.touch_story(conn, state["story_id"])
    return {"proposals": out_rows}


def collect(state: CanonState) -> CanonState:
    return {}


def build_canon_graph() -> StateGraph:
    g = StateGraph(CanonState)
    g.add_node("start_canon", start_canon)
    g.add_node("check_passage", check_passage)
    g.add_node("collect", collect)
    g.add_edge(START, "start_canon")
    g.add_conditional_edges("start_canon", fan_out, ["check_passage"])
    g.add_edge("check_passage", "collect")
    g.add_edge("collect", END)
    return g


def canon_input(story_id: int, scope: list[int], params: Params, budget: dict[str, Any]) -> CanonState:
    """Run input for canon_check: the scope, or every passage when it is empty."""
    if not scope:
        with db.transaction() as conn:
            scope = [r["id"] for r in conn.execute("SELECT id FROM passages WHERE story_id = ? ORDER BY id", (story_id,))]
    return {"story_id": story_id, "passage_ids": scope, "params": params}


# --- expand_frontier ---------------------------------------------------------------
#
# Grows the story outward from a root passage, one level at a time:
#
#   plan → [Send grow per frontier passage] → gather → (review | finish)
#   review —interrupt→ plan
#
# `grow` asks for choices out of a passage, drafts a destination for each new
# one, and writes them the way the reviewed rule says: directly, as agent
# passages, where the parent is unreviewed; as proposals where it is reviewed
# (those are not grown further). The run's budget is a copy of the story's
# pacing plan (concepts/act-pacing.md): a list of acts, then a fixed ending
# level. Each level belongs to one act, whose branching caps the choices out of
# its passages and whose guidance and length fill the frontier prompts. `gather`
# stops the run at the plan's depth or the wall-clock cap. Between levels
# `review` interrupts so the author can look, steer, or stop.

# The plan a story uses until its author saves one. Depth is derived (the acts'
# levels plus the ending) and is a hard cap; max_seconds is only the time guard.
DEFAULT_PLAN: dict[str, Any] = {
    "acts": [
        {"name": "Setup", "levels": 1, "branching": 2, "ends_story": True, "length": "150–250 words",
         "guidance": "Establish who the reader is, where they are and what they want, and put the central problem "
                     "in front of them."},
        {"name": "Confrontation", "levels": 1, "branching": 3, "ends_story": True, "length": "200–300 words",
         "guidance": "Raise the stakes with obstacles, reversals and costly choices. A reckless or poor choice may "
                     "end the storyline here, as a consequence the reader can see coming."},
        {"name": "Climax", "levels": 1, "branching": 2, "ends_story": True, "length": "200–300 words",
         "guidance": "Bring the central conflict to its decisive moment. The choices out of it are the decisive "
                     "choices of the story."},
    ],
    "ending": {"length": "400–600 words",
               "guidance": "The climax has been decided. Resolve the central question and let the consequences land, "
                           "for better or worse, and close with a sense of an ending."},
    "steer": "",
    "max_seconds": 1800,
    "policy": {},
}


def plan_depth(plan: dict[str, Any]) -> int:
    """Levels a plan grows below the run's root: every act's, plus the ending's one."""
    return sum(a["levels"] for a in plan["acts"]) + 1


def stage_at(plan: dict[str, Any], level: int) -> dict[str, Any]:
    """The stage whose passages are written at `level` (0 = the root's children): an
    act, or the ending past the last act. `index` is the act's position (len(acts) for
    the ending) and `end` the first level after it."""
    start = 0
    for i, act in enumerate(plan["acts"]):
        if level < start + act["levels"]:
            return {**act, "index": i, "end": start + act["levels"], "ending": False}
        start += act["levels"]
    return {**plan["ending"], "name": "Resolution", "index": len(plan["acts"]), "end": start + 1,
            "ending": True, "branching": 0, "ends_story": True}

# refinement-loops.md's R1: draft -> validate (code, via _ask_until) -> audit (model,
# per auditor) -> revise, repeated per passage. `rounds: 2` is a shorter leash than the
# design doc's stated default of 6, kept until a comparison run validates the loop is
# worth the added latency; `candidates` and `path_audit` are accepted here so the shape
# matches later phases but R1 never drafts more than one candidate or runs a path audit.
DEFAULT_REFINE = {
    "rounds": 2, "restarts": 2, "block": "minor", "on_unresolved": "keep_flagged",
    "candidates": 1, "votes": 1, "audit_reasoning": False, "path_audit": False,
    "auditors": {"passage": ["audit_continuity", "audit_canon"], "choices": ["audit_choices"]},
    "audit_model": None,
}


class Grown(TypedDict):
    parent: int
    level: int
    children: list[int]  # passages written directly (the next frontier)
    endings: list[int]  # passages written directly as endings (not grown further)
    proposed: int  # choices filed as proposals instead


class FrontierState(TypedDict, total=False):
    run_id: int | None
    story_id: int
    root: int
    budget: dict[str, Any]  # the run's copy of the pacing plan, plus its derived "depth"
    policy: dict[str, Any]  # {"auto_accept_unreviewed": bool, "skip_review_interrupt": bool}
    deadline: float
    params: Params
    batch: str
    level: int
    frontier: list[int]
    steer: str
    stop: bool
    grown: Annotated[list[Grown], operator.add]
    errors: Annotated[list[str], operator.add]


class GrowState(TypedDict, total=False):
    run_id: int | None
    story_id: int
    passage_id: int
    level: int
    allot: int  # new passages this grow may write: the branching of the parent's act
    plan: dict[str, Any]  # the run's pacing plan; the level past its last act writes endings
    steer: str
    params: Params
    policy: dict[str, Any]  # {"refine": {...}} — see DEFAULT_REFINE
    batch: str
    grown: Annotated[list[Grown], operator.add]
    errors: Annotated[list[str], operator.add]


def _created(state: FrontierState) -> int:
    return sum(len(g["children"]) + len(g.get("endings", [])) for g in state.get("grown", []))


def plan(state: FrontierState) -> FrontierState:
    _run().check()
    level = state.get("level", 0)
    if level == 0:
        frontier = [state["root"]]
    else:
        frontier = [c for g in state["grown"] if g["level"] == level - 1 for c in g["children"]]
    _run().report(level=level, created=_created(state), frontier=len(frontier))
    return {"level": level, "frontier": frontier, "batch": state.get("batch") or uuid.uuid4().hex}


def _choice_stage(plan: dict[str, Any], level: int) -> dict[str, Any]:
    """The act the choices grown at `level` come out of: the parent passage's (the one
    written at level - 1), or the first act's for the run's root. Its branching caps them,
    and act_remaining counts the levels of that act still to be written from here."""
    stage = stage_at(plan, max(level - 1, 0))
    return {**stage, "act_remaining": max(stage["end"] - level, 0)}


def choice_fields(plan: dict[str, Any], level: int) -> dict[str, str]:
    """frontier_choices' pacing slots for the choices grown at `level`."""
    s = _choice_stage(plan, level)
    return {"branching": str(s["branching"]), "remaining": str(plan_depth(plan) - level),
            "act_remaining": str(s["act_remaining"]), "act_guidance": s["guidance"]}


def passage_fields(plan: dict[str, Any], level: int) -> dict[str, str]:
    """frontier_passage's and frontier_ending's pacing slots for a passage written at `level`."""
    s = stage_at(plan, level)
    return {"remaining": str(plan_depth(plan) - level), "act_remaining": str(s["end"] - level),
            "length": s["length"], "act_guidance": s["guidance"]}


def preview_fields(plan: dict[str, Any], stage: int, role: str) -> dict[str, str]:
    """The pacing slots as `stage` (an act's index, or len(acts) for the ending) fills them
    first: its first passage, or the first choices out of it — the root's, for the first
    act, and the last act's, for the ending, which offers none. The prompt preview uses
    this so it matches `grow`."""
    stage = min(max(stage, 0), len(plan["acts"]))
    start = sum(a["levels"] for a in plan["acts"][:stage])
    if role == "frontier_choices":
        return choice_fields(plan, start + 1 if 0 < stage < len(plan["acts"]) else start)
    return passage_fields(plan, start)


def fan_out_frontier(state: FrontierState) -> list[Send]:
    """Allot each frontier passage its act's branching, one grow each."""
    plan = state["budget"]
    allot = _choice_stage(plan, state["level"])["branching"]
    common = {"run_id": state.get("run_id"), "story_id": state["story_id"], "level": state["level"],
              "plan": plan, "steer": state.get("steer", ""), "params": state["params"], "batch": state["batch"],
              "policy": state.get("policy") or {}, "allot": allot}
    sends = [Send("grow", {**common, "passage_id": pid}) for pid in state["frontier"]]
    return sends or [Send("gather", {})]


def _ask_until(params: Params, schema: type[BaseModel], prompt_for: Callable[[str | None], str],
               problem: Callable[[Any], str | None], activity: dict[str, Any]) -> Any:
    """`_ask` with the verbs' retry discipline for workflow nodes: up to MAX_ATTEMPTS calls,
    each retry told why the last answer was refused. `problem` returns None for a usable
    answer. Raises ValueError with the last reason when every attempt fails."""
    error = None
    for _ in range(MAX_ATTEMPTS):
        _run().check()
        try:
            out = _ask(params, schema, prompt_for(error), activity=activity)
        except RunCancelled:
            raise
        except Exception as e:  # e.g. an empty tool call that fails the schema
            error = f"the answer did not match the required fields ({type(e).__name__})"
        else:
            error = problem(out)
            if error is None:
                return out
        # Retries are the data that tunes prompts and future audit loops: count them on
        # the run (persisted in progress) and log each reason.
        _run().tick("retries")
        _run().emit({"type": "retry", "passage_id": activity.get("passage_id"), "phase": activity.get("phase"), "reason": error})
    raise ValueError(f"no usable answer after {MAX_ATTEMPTS} attempts: {error}")


def _norm(text: str) -> str:
    """Case-fold and collapse whitespace, for substring quote/evidence verification."""
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _fingerprint(issue: dict[str, Any]) -> str:
    return f"{_norm(issue.get('quote', ''))}|{_norm(issue.get('problem', ''))}"


def _verify_issue(issue: dict[str, Any], draft_haystack: str, context_haystack: str) -> bool:
    """An issue is trustworthy only if its quote is really in the draft, and its evidence
    (when given) is really in the context it claims the draft contradicts. The main
    defence against a model auditing its own output and inventing problems
    (refinement-loops.md)."""
    quote = _norm(issue.get("quote", ""))
    if not quote or quote not in draft_haystack:
        return False
    evidence = _norm(issue.get("evidence", ""))
    return evidence == "" or evidence in context_haystack


def _blocking(issues: list[dict[str, Any]], block: str) -> bool:
    """Whether any verified issue is severe enough to force a revision, per policy.refine.block."""
    return any(block == "minor" or i.get("severity") == "major" for i in issues)


def _format_issues(issues: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{i['severity']}] {i['problem']} (quote: \"{i['quote']}\")" for i in issues) or "(none)"


def _has_canon_material(conn: sqlite3.Connection, story_id: int) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM lore WHERE story_id = ? UNION SELECT 1 FROM events WHERE story_id = ? LIMIT 1",
        (story_id, story_id)).fetchone())


AUDIT_TEMPERATURE = 0.2       # canon_check's existing precedent for judgment calls
AUDIT_VOTE_TEMPERATURE = 0.5  # "a moderate temperature" for ensembles (refinement-loops.md)


def _run_auditor(role: str, draft: dict[str, Any], ctx: dict[str, Any], params: Params,
                 cfg: dict[str, Any], activity: dict[str, Any]) -> tuple[AuditReport, list[str]]:
    """One auditor, `cfg['votes']` times if more than 1 (fanned out, since votes are
    independent samples of the same prompt), keeping only issues a majority of votes
    agree on (by fingerprint). A failed vote is logged, not raised: one bad vote or one
    bad auditor call shouldn't sink the whole passage. Returns its own errors rather than
    appending to a shared list, so concurrent callers (refine's per-round fan-out) never
    touch each other's state."""
    roles = _prompts(params)
    prompt = compose(ctx, roles, role, draft_title=draft["title"], draft_body=draft["body"])
    votes = max(1, cfg.get("votes", 1))
    audit_params: Params = {**params, "model": cfg.get("audit_model") or params.get("model")}
    temperature = AUDIT_VOTE_TEMPERATURE if votes > 1 else AUDIT_TEMPERATURE

    def try_vote(_: int) -> tuple[AuditReport | None, str | None]:
        try:
            return _ask(audit_params, AuditReport, prompt, temperature=temperature,
                       activity=activity, reasoning=bool(cfg.get("audit_reasoning"))), None
        except Exception as e:
            return None, f"{role} on passage {activity.get('passage_id')}: {type(e).__name__}: {e}"

    results = _fan(list(range(votes)), try_vote)
    reports = [r for r, _ in results if r is not None]
    errors = [e for _, e in results if e is not None]
    if not reports:
        return AuditReport(verdict="error", issues=[]), errors
    counts: dict[str, int] = {}
    by_fp: dict[str, AuditIssue] = {}
    for r in reports:
        for issue in r.issues:
            fp = _fingerprint(issue.model_dump())
            counts[fp] = counts.get(fp, 0) + 1
            by_fp.setdefault(fp, issue)
    kept = [by_fp[fp] for fp, n in counts.items() if n > len(reports) // 2]
    return AuditReport(verdict="revise" if kept else "pass", issues=kept), errors


def refine(*, params: Params, ctx: dict[str, Any], roles: dict[str, str], cfg: dict[str, Any],
          schema: type[BaseModel], draft_role: str, revise_role: str, steer: str,
          compose_fields: dict[str, str], draft_problem: Callable[[Any], str | None],
          activity: dict[str, Any], has_canon: bool, errors: list[str]
          ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    """draft (validated via _ask_until) -> audit (model, per auditor) -> revise, repeated
    per refinement-loops.md ("validate before audit, always": a structurally invalid draft
    never reaches an auditor, it just regenerates the way it does today). Stops on pass (no
    verified issue at/above `block`), plateau (two rounds with non-decreasing issue counts),
    or the round ceiling; restarts from a fresh draft up to `restarts` times when an attempt
    plateaus with issues open. Returns (draft, meta, audit_rows): `draft` is None only when
    on_unresolved == 'drop'; `audit_rows` are plain dicts without ids, for the caller to
    insert once it knows the passage/proposal id."""
    auditor_roles = [r for r in cfg["auditors"]["passage"] if r != "audit_canon" or has_canon]
    context_haystack = _norm(render_context(ctx))
    audit_rows: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None

    def draft_prompt(error: str | None) -> str:
        return compose(ctx, roles, draft_role, steer=steer, error=error, **compose_fields)

    for attempt in range(cfg["restarts"] + 1):
        if attempt > 0:
            _run().tick("restarts")
        try:
            draft = _ask_until(params, schema, draft_prompt, draft_problem, activity).model_dump()
        except ValueError:
            # No usable draft at all this attempt (exhausted MAX_ATTEMPTS, same failure
            # _ask_until already raises today). If no attempt has ever produced a draft,
            # there is nothing to keep or flag — the caller skips this choice, same as
            # the rounds: 0 path already does on an exhausted-retries failure.
            if best is None:
                raise
            break
        prev_counts: tuple[int, int] | None = None
        plateau_streak = 0
        for round_no in range(1, cfg["rounds"] + 1):
            _run().tick("rounds")
            draft_haystack = _norm(draft["title"] + " " + draft["body"])
            round_issues: list[dict[str, Any]] = []
            for role, (report, audit_errors) in _fan(auditor_roles, lambda role: (role, _run_auditor(role, draft, ctx, params, cfg, activity))):
                errors.extend(audit_errors)
                verified, dropped = [], 0
                for issue in report.issues:
                    d = issue.model_dump()
                    if _verify_issue(d, draft_haystack, context_haystack):
                        verified.append(d)
                    else:
                        dropped += 1
                audit_rows.append({"step": draft_role, "round": round_no, "auditor": role,
                                   "verdict": report.verdict, "issues": verified, "dropped": dropped, "draft": draft})
                round_issues += verified
            counts = (sum(1 for i in round_issues if i["severity"] == "major"),
                     sum(1 for i in round_issues if i["severity"] == "minor"))
            if best is None or counts < best["counts"]:
                best = {"draft": draft, "counts": counts, "issues": round_issues, "rounds": round_no}
            if not _blocking(round_issues, cfg["block"]):
                return best["draft"], {"verdict": "resolved", "rounds": round_no, "issues": []}, audit_rows
            plateau_streak = plateau_streak + 1 if prev_counts is not None and counts >= prev_counts else 0
            prev_counts = counts
            _run().check()
            if plateau_streak >= 2 or round_no >= cfg["rounds"]:
                break
            try:
                draft = _ask_until(
                    params, schema,
                    lambda error: compose(ctx, roles, revise_role, steer=steer, error=error,
                                          draft_title=draft["title"], draft_body=draft["body"],
                                          issues=_format_issues(round_issues)),
                    draft_problem, activity).model_dump()
            except ValueError as e:
                # Couldn't produce a usable revision this round: treat it like a plateau
                # rather than losing the attempt's best draft so far — the round loop
                # ends here and falls through to restart-or-give-up below.
                errors.append(f"passage {activity.get('passage_id')}, revise: {e}")
                break
    if cfg["on_unresolved"] == "drop":
        return None, {"verdict": "dropped", "rounds": best["rounds"] if best else 0,
                      "issues": best["issues"] if best else []}, audit_rows
    return best["draft"], {"verdict": "unresolved", "rounds": best["rounds"], "issues": best["issues"]}, audit_rows


def _fresh_choices(drafts: list[ChoiceDraft], outgoing: list[dict[str, Any]], allot: int) -> list[tuple[str, str]]:
    """The drafted choices worth growing: written out (not blank or '...'), not already on the passage, at most `allot`."""
    seen = {o["text"].strip().lower() for o in outgoing}
    kept = []
    for c in drafts:
        text, target = c.text.strip(), c.target_title.strip()
        if _blank(text) or text.lower() in seen or len(kept) >= allot:
            continue
        seen.add(text.lower())
        kept.append((text, target))
    return kept


def grow(state: GrowState) -> GrowState:
    """Choices out of one passage and a drafted destination for each; writes or proposes per the reviewed rule."""
    _run().check()
    pid = state["passage_id"]
    ctx = gather_context({"story_id": state["story_id"], "passage_id": pid, "params": {}})["context"]
    p = ctx["passage"]
    roles = _prompts(state["params"])
    # Pacing: `left` counts the passages this storyline still has, including the one each choice
    # leads to. The choices come out of the parent's act; the passages they lead to belong to this
    # level's stage, and past the last act that is the ending, where every new passage is one.
    plan, level = state["plan"], state["level"]
    stage = stage_at(plan, level)
    final = stage["ending"]
    fields = passage_fields(plan, level)
    # An act that forbids early endings drafts without the ends_story field, so the writer cannot set it.
    role, schema = (("frontier_ending", FrontierDraft) if final else
                    ("frontier_passage", FrontierPassageDraft if stage["ends_story"] else FrontierDraft))
    # Every kept choice gets a new passage: a frontier grows outward, it does not
    # link back (merging is deferred; see agentic-authoring). Existing titles the
    # model offers anyway are drafted afresh under the choice.
    try:
        drafts = _ask_until(
            state["params"], ChoiceDrafts,
            lambda error: compose(ctx, roles, "frontier_choices", steer=state["steer"], error=error,
                                  **choice_fields(plan, level)),
            lambda out: None if _fresh_choices(out.choices, ctx["outgoing"], state["allot"])
            else "every suggested choice was empty, '...', or already exists",
            {"passage_id": pid, "phase": "choices"}).choices
    except ValueError as e:
        return {"errors": [f"passage {pid} ({p['title']}): {e}"],
                "grown": [{"parent": pid, "level": state["level"], "children": [], "endings": [], "proposed": 0}]}
    kept = _fresh_choices(drafts, ctx["outgoing"], state["allot"])

    refine_cfg = {**DEFAULT_REFINE, **((state.get("policy") or {}).get("refine") or {})}
    order = 1 + max((o.get("sort_order", 0) for o in ctx["outgoing"]), default=-1)

    def item(i: int, choice: tuple[str, str]) -> dict[str, Any]:
        return _draft_choice(state, ctx, roles, p, pid, i, choice, len(kept), order, role, schema, fields, final, refine_cfg)

    results = _fan(list(enumerate(kept)), lambda pair: item(*pair))
    children: list[int] = []
    endings: list[int] = []
    proposed = 0
    errors: list[str] = []
    for r in results:
        if r["child"] is not None:
            children.append(r["child"])
        if r["ending"] is not None:
            endings.append(r["ending"])
        proposed += r["proposed"]
        errors += r["errors"]
    return {"grown": [{"parent": pid, "level": state["level"], "children": children, "endings": endings, "proposed": proposed}],
            "errors": errors}


def _draft_choice(state: GrowState, ctx: dict[str, Any], roles: dict[str, str], p: dict[str, Any], pid: int,
                  i: int, choice: tuple[str, str], n_kept: int, order: int, role: str, schema: type[BaseModel],
                  fields: dict[str, str], final: bool, refine_cfg: dict[str, Any]) -> dict[str, Any]:
    """One choice's passage: draft `role` (plain or refined per `refine_cfg`) with the
    level's pacing `fields`, then write it — directly if the parent is unreviewed, as a
    proposal if it is reviewed. Every failure mode (exhausted draft retries, refine giving
    up under on_unresolved: 'drop') is caught here and reported as a skip, never raised,
    so `grow`'s fan-out can run every choice concurrently without one failure taking
    down its siblings."""
    text, target = choice
    pos = {"pos_x": p["pos_x"] + (i - (n_kept - 1) / 2) * 220, "pos_y": p["pos_y"] + 180}
    activity = {"passage_id": pid, "phase": "passage", "choice": text}
    draft_problem = lambda d: ("title and body must both be written out, not empty or '...'"
                               if _blank(d.title) or _placeholder_body(d.body) else None)
    empty = {"child": None, "ending": None, "proposed": False, "errors": []}
    meta_audit: dict[str, Any] | None = None
    audit_rows: list[dict[str, Any]] = []
    # Errors accumulated along the way (e.g. a failed auditor vote) that don't stop this
    # choice from succeeding still belong on the final result — every return below must
    # carry this, not just the give-up paths.
    choice_errors: list[str] = []
    if refine_cfg["rounds"] <= 0:
        # rounds: 0 is a true no-op — today's behaviour exactly, no audits rows written.
        try:
            out = _ask_until(
                state["params"], schema,
                lambda error: compose(ctx, roles, role, steer=state["steer"], error=error, choice=text, **fields),
                draft_problem, activity)
        except ValueError as e:
            # Skip this choice (no dangling link is written) and still grow its siblings.
            return {**empty, "errors": [f"passage {pid} ({p['title']}), choice '{text}': {e}"]}
        draft = out.model_dump()
    else:
        with db.transaction() as conn:
            has_canon = _has_canon_material(conn, state["story_id"])
        try:
            draft, meta_audit, audit_rows = refine(
                params=state["params"], ctx=ctx, roles=roles, cfg=refine_cfg, schema=schema,
                draft_role=role, revise_role="revise_passage", steer=state["steer"],
                compose_fields={"choice": text, **fields}, draft_problem=draft_problem,
                activity=activity, has_canon=has_canon, errors=choice_errors)
        except ValueError as e:
            # No draft was ever produced (exhausted MAX_ATTEMPTS on the very first
            # attempt) — skip this choice the same way the rounds: 0 path does.
            return {**empty, "errors": choice_errors + [f"passage {pid} ({p['title']}), choice '{text}': {e}"]}
        if draft is None:
            return {**empty, "errors": choice_errors + [f"passage {pid} ({p['title']}), choice '{text}': dropped after "
                                                         f"{refine_cfg['restarts']} restart(s) with unresolved issues"]}
    target, body = draft["title"].strip() or target, draft["body"].strip()
    # An ending, forced at the last level or chosen earlier by the writer, is not grown further.
    ending = final or bool(draft.get("ends_story", False))
    unresolved = meta_audit if meta_audit and meta_audit["verdict"] == "unresolved" else None
    with db.transaction() as conn:
        if p["reviewed"]:
            payload = {"text": text, "to_passage": None, "new_target": {"title": target, "body": body, "is_ending": ending, **pos}}
            if unresolved:
                payload["audit"] = unresolved
            proposal = _insert_proposal(conn, state["story_id"], state["batch"], "expand_frontier",
                             {"kind": "new_choice", "target_passage": pid, "payload": payload}, state.get("run_id"))
            for row in audit_rows:
                conn.execute(
                    "INSERT INTO audits (story_id, run_id, proposal_id, step, round, auditor, verdict, issues, dropped, draft)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (state["story_id"], state.get("run_id"), proposal["id"], row["step"], row["round"], row["auditor"],
                     row["verdict"], json.dumps(row["issues"]), row["dropped"], json.dumps(row["draft"])))
            db.touch_story(conn, state["story_id"])
            return {**empty, "proposed": True, "errors": choice_errors}
        to = conn.execute(
            "INSERT INTO passages (story_id, title, body, pos_x, pos_y, t, meta, provenance, is_ending) VALUES (?, ?, ?, ?, ?, ?, ?, 'agent', ?)",
            (state["story_id"], target, body, pos["pos_x"], pos["pos_y"], p["t"],
             json.dumps({"audit": unresolved}) if unresolved else "{}", int(ending)),
        ).lastrowid
        conn.execute(
            "INSERT INTO choices (story_id, from_passage, to_passage, text, sort_order) VALUES (?, ?, ?, ?, ?)",
            (state["story_id"], pid, to, text, order + i),
        )
        for row in audit_rows:
            conn.execute(
                "INSERT INTO audits (story_id, run_id, passage_id, step, round, auditor, verdict, issues, dropped, draft)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (state["story_id"], state.get("run_id"), to, row["step"], row["round"], row["auditor"],
                 row["verdict"], json.dumps(row["issues"]), row["dropped"], json.dumps(row["draft"])))
        db.touch_story(conn, state["story_id"])
        return {**empty, "child": None if ending else to, "ending": to if ending else None, "errors": choice_errors}


def gather(state: FrontierState) -> FrontierState:
    _run().report(level=state["level"], created=_created(state), frontier=len(state["frontier"]))
    return {}


def after_gather(state: FrontierState) -> Literal["review", "finish"]:
    budget, level = state["budget"], state["level"]
    next_frontier = [c for g in state["grown"] if g["level"] == level for c in g["children"]]
    if (state.get("stop") or not next_frontier or level + 1 >= budget["depth"]
            or time.time() > state["deadline"]):
        return "finish"
    return "review"


def review(state: FrontierState) -> FrontierState:
    """Pause between levels. Resumed with {"steer": str, "stop": bool}.
    Under the skip_review_interrupt policy it advances to the next level without pausing."""
    level = state["level"]
    if (state.get("policy") or {}).get("skip_review_interrupt"):
        return {"level": level + 1}
    answer = interrupt({"level": level, "created": _created(state),
                        "next": len([c for g in state["grown"] if g["level"] == level for c in g["children"]])}) or {}
    steer = answer.get("steer")
    return {"steer": steer if steer is not None else state.get("steer", ""), "stop": bool(answer.get("stop")), "level": level + 1}


def after_review(state: FrontierState) -> Literal["plan", "finish"]:
    return "finish" if state.get("stop") else "plan"


def finish(state: FrontierState) -> FrontierState:
    _run().report(level=state["level"], created=_created(state), frontier=0)
    return {}


def build_frontier_graph() -> StateGraph:
    g = StateGraph(FrontierState)
    g.add_node("plan", plan)
    g.add_node("grow", grow)
    g.add_node("gather", gather)
    g.add_node("review", review)
    g.add_node("finish", finish)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", fan_out_frontier, ["grow", "gather"])
    g.add_edge("grow", "gather")
    g.add_conditional_edges("gather", after_gather, {"review": "review", "finish": "finish"})
    g.add_conditional_edges("review", after_review, {"plan": "plan", "finish": "finish"})
    g.add_edge("finish", END)
    return g


def frontier_input(story_id: int, scope: list[int], params: Params, budget: dict[str, Any]) -> FrontierState:
    if len(scope) != 1:
        raise ValueError("expand_frontier grows from exactly one root passage")
    plan = {**DEFAULT_PLAN, **budget}  # an empty budget runs the default plan
    budget = {**plan, "depth": plan_depth(plan)}
    return {"story_id": story_id, "root": scope[0], "budget": budget, "deadline": time.time() + budget["max_seconds"],
            "params": params, "level": 0, "steer": params.get("steer") or "", "stop": False}


# --- registry ----------------------------------------------------------------------

# Workflows a run can execute: the uncompiled graph (runs.py compiles it with
# its checkpointer) and how to turn a run request into the graph's input.
WORKFLOWS: dict[str, tuple[Any, Any]] = {
    "canon_check": (build_canon_graph, canon_input),
    "expand_frontier": (build_frontier_graph, frontier_input),
}


# expand_frontier's per-level loop as the Agents view draws it: the graph's nodes in
# run order, with `grow` opened into the steps it runs per frontier passage. `roles` is
# the prompt a step composes, `reads` the plan fields it takes (act fields unprefixed),
# `when` the condition it runs under. test_loop_matches_node_prompts keeps grow's steps
# and NODE_PROMPTS in agreement.
FRONTIER_LOOP: dict[str, Any] = {
    "steps": [
        {"id": "plan", "label": "plan", "reads": ["branching"],
         "doc": "Collects the frontier and allots each passage its act's branching."},
        {"id": "choices", "label": "choices", "inside": "grow", "roles": ["frontier_choices"],
         "reads": ["branching", "guidance"], "doc": "Choices out of a frontier passage, paced by the parent's act."},
        {"id": "passage", "label": "passage", "inside": "grow", "roles": ["frontier_passage"],
         "reads": ["guidance", "length", "ends_story"], "when": "in an act",
         "doc": "The passage each choice leads to; may end the storyline early where the act allows it."},
        {"id": "ending", "label": "ending", "inside": "grow", "roles": ["frontier_ending"],
         "reads": ["ending.guidance", "ending.length"], "when": "in the Resolution",
         "doc": "At the level past the last act every new passage is an ending."},
        {"id": "continuity", "label": "continuity audit", "inside": "grow", "roles": ["audit_continuity"],
         "reads": ["policy.refine"], "when": "refine on", "doc": "Checks the draft against the story so far."},
        {"id": "canon", "label": "canon audit", "inside": "grow", "roles": ["audit_canon"],
         "reads": ["policy.refine"], "when": "refine on, story has lore or events",
         "doc": "Checks the draft against WORLD and TIMELINE."},
        {"id": "revise", "label": "revise", "inside": "grow", "roles": ["revise_passage"],
         "reads": ["policy.refine"], "when": "an audit verified issues", "doc": "Fixes the verified issues, nothing else."},
        {"id": "gather", "label": "gather", "reads": ["depth", "max_seconds"],
         "doc": "Collects the level; finishes at the plan's depth or the time guard."},
        {"id": "review", "label": "review", "reads": ["steer"], "when": "not unattended",
         "doc": "Pauses between levels for the author to steer or stop."},
    ],
    "edges": [["plan", "choices"], ["choices", "passage"], ["choices", "ending"],
              ["passage", "continuity"], ["passage", "canon"], ["ending", "continuity"], ["ending", "canon"],
              ["continuity", "revise"], ["canon", "revise"], ["passage", "gather"], ["ending", "gather"],
              ["revise", "gather"], ["gather", "review"], ["review", "plan"]],
}


def _graph_loop(g: dict[str, Any], name: str) -> dict[str, Any]:
    """A loop drawn straight from a graph's topology, for workflows with no hand-opened node."""
    steps = [{"id": n["id"], "label": n["id"], "roles": NODE_PROMPTS.get((name, n["id"]), [])}
             for n in g["nodes"] if not n["id"].startswith("__")]
    ids = {s["id"] for s in steps}
    return {"steps": steps, "edges": [[e["source"], e["target"]] for e in g["edges"]
                                      if e["source"] in ids and e["target"] in ids]}


def workflow_json() -> dict[str, dict[str, Any]]:
    """Topology per workflow for the Agents view: {"nodes": [...], "edges": [...], "loop": {...}},
    each node carrying the prompt roles it composes (NODE_PROMPTS), and `loop` the steps
    the view draws (FRONTIER_LOOP, or the nodes themselves)."""
    graphs = {"verb": VERB_GRAPH.get_graph().to_json(),
              **{name: build().compile().get_graph().to_json() for name, (build, _) in WORKFLOWS.items()}}
    for name, g in graphs.items():
        for n in g["nodes"]:
            n["prompts"] = NODE_PROMPTS.get((name, n["id"]), [])
        loop = FRONTIER_LOOP if name == "expand_frontier" else _graph_loop(g, name)
        g["loop"] = {"steps": [{"roles": [], "reads": [], **s} for s in loop["steps"]], "edges": loop["edges"]}
    return graphs
