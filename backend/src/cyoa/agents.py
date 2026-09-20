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

Every model call goes through `_ask`, which holds a semaphore: Ollama loads one
context per concurrent request and the GPU cannot take two 27B contexts, so the
logical fan-out (`Send`) is physically a queue. The gate lives here rather than
in LangGraph's `max_concurrency` because that setting sizes the executor and
once deadlocked a streamed run (see runs.py), and because a semaphore also
throttles across runs and the synchronous verbs.

A node that belongs to a run (runs.py) reports and checks in through the
`RunHandle` it finds in its config; outside a run the null handle does nothing,
so the verbs pay nothing.
"""

from __future__ import annotations

import json
import operator
import os
import sqlite3
import threading
import time
import uuid
from typing import Annotated, Any, Callable, Literal, TypedDict

from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt
from pydantic import BaseModel, Field

from . import db, llm

MAX_ATTEMPTS = 3
LORE_CAP = 20
EVENTS_CAP = 12
CHOICES_PER_PASSAGE = 3  # expand_frontier asks for this many per passage
# How many model calls may be in flight across the whole process. Two were
# enough to OOM the GPU with a 27B model on 2026-09-20, so the default is a
# queue. Raise it only for a server that is known to batch.
MAX_CONCURRENCY = int(os.environ.get("CYOA_AGENT_CONCURRENCY", "1"))
_GATE = threading.Semaphore(MAX_CONCURRENCY)


def _ask(params: "Params", schema: type[BaseModel], prompt: str, *, temperature: float = 0.8,
         activity: dict[str, Any] | None = None) -> BaseModel:
    """One structured-output model call, throttled by the process-wide gate. `activity`
    (passage_id, phase) is reported to the run only once the call actually starts, so
    the map highlights what the model is working on, not what is queued behind it."""
    model = llm.chat_model(params.get("model"), temperature=temperature).with_structured_output(schema, method=llm.structured_method())
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
    choices: list[ChoiceDraft] = Field(description="two to four distinct choices")


class PassageDraft(BaseModel):
    title: str = Field(description="a short passage title")
    body: str = Field(description="the passage text, second person present tense, one to three short paragraphs")


SCHEMAS: dict[str, type[BaseModel]] = {
    "draft_body": BodyDraft,
    "suggest_choices": ChoiceDrafts,
    "draft_target": PassageDraft,
    "rewrite": BodyDraft,
}


# --- state ------------------------------------------------------------------------


class Params(TypedDict, total=False):
    model: str | None
    tone: str | None
    choice_id: int | None


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
        choice = None
        if state["params"].get("choice_id") is not None:
            row = conn.execute("SELECT * FROM choices WHERE id = ?", (state["params"]["choice_id"],)).fetchone()
            choice = dict(row) if row else None
    return {"context": {"story": story, "passage": passage, "variables": variables, "titles": titles,
                        "inbound": inbound, "outgoing": outgoing, "lore": lore, "events": events, "choice": choice},
            "attempts": 0, "batch": uuid.uuid4().hex}


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
    lines += [f"PASSAGE: {p['title']}" + (f" ({where})" if where else ""), p["body"] or "(empty)", ""]
    if ctx["inbound"]:
        lines += ["REACHED FROM:"] + [f"- '{i['title']}' via choice '{i['text']}': {_excerpt(i['body'])}" for i in ctx["inbound"]] + [""]
    if ctx["outgoing"]:
        lines += ["EXISTING CHOICES:"] + [f"- '{o['text']}' -> {o['target'] or '(no target yet)'}" for o in ctx["outgoing"]] + [""]
    lines += ["ALL PASSAGE TITLES: " + "; ".join(ctx["titles"].values())]
    return "\n".join(lines)


INSTRUCTIONS = {
    "draft_body": "Write the body text for PASSAGE. Stay consistent with WORLD and TIMELINE and with the passages it is reached from. Do not list choices in the body.",
    "suggest_choices": "Suggest new choices for PASSAGE that are distinct from EXISTING CHOICES. A target_title may be an existing title from ALL PASSAGE TITLES when the choice should lead there, otherwise a new title.",
    "draft_target": "The reader took the choice '{choice}' from PASSAGE and it has no destination yet. Write the destination passage: a title and body that follow directly from that choice.",
    "rewrite": "Rewrite the body of PASSAGE in a {tone} tone. Keep every fact, character and place the same; change only the prose.",
}


def generate(state: VerbState) -> VerbState:
    ctx, verb, params = state["context"], state["verb"], state["params"]
    instruction = INSTRUCTIONS[verb].format(
        choice=(ctx["choice"] or {}).get("text", ""), tone=params.get("tone") or "vivid"
    )
    prompt = render_context(ctx) + "\n\nTASK: " + instruction
    if state.get("error"):
        prompt += f"\n\nYour previous answer was rejected: {state['error']}. Try again."
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
        if not draft.get("body", "").strip():
            return {"draft": None, "error": "body was empty"}
        if verb == "rewrite" and draft["body"].strip() == state["context"]["passage"]["body"].strip():
            return {"draft": None, "error": "the rewrite was identical to the original"}
    elif verb == "draft_target":
        if not draft.get("title", "").strip() or not draft.get("body", "").strip():
            return {"draft": None, "error": "title and body must both be non-empty"}
    elif verb == "suggest_choices":
        seen = {o["text"].strip().lower() for o in state["context"]["outgoing"]}
        kept = []
        for c in draft.get("choices", []):
            text = c.get("text", "").strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                kept.append({"text": text, "target_title": c.get("target_title", "").strip()})
        if not kept:
            return {"draft": None, "error": "every suggested choice was empty or already exists"}
        return {"draft": {"choices": kept}, "error": None}
    return {"error": None}


def after_validate(state: VerbState) -> Literal["generate", "propose", "fail"]:
    if not state.get("error"):
        return "propose"
    return "generate" if state["attempts"] < MAX_ATTEMPTS else "fail"


def propose(state: VerbState) -> VerbState:
    """Materialise the draft as proposal rows; the only node that writes."""
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
    with db.transaction() as conn:
        out = [_insert_proposal(conn, state["story_id"], state["batch"], verb, r) for r in rows]
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


CANON_INSTRUCTION = (
    "Compare PASSAGE to WORLD and TIMELINE. Report only genuine contradictions of a stated fact — a character "
    "who cannot be present, a place described against its entry, an event referred to before it happens or "
    "as if it had not happened. Tone, style and omissions are not issues. If there are issues, give fixed_body: "
    "the passage body text only (no title, no heading), rewritten with the smallest edits that repair every issue "
    "listed — none of the contradicted claims may survive in it."
)


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
    prompt = render_context(ctx) + "\n\nTASK: " + CANON_INSTRUCTION
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
# (those are not grown further). The budget is a conditional edge, not the
# prompt: `plan` allots the remaining passage budget across the frontier, and
# `gather` stops the run at max_passages, depth or the wall-clock cap. Between
# levels `review` interrupts so the author can look, steer, or stop.

DEFAULT_BUDGET = {"max_passages": 12, "depth": 2, "max_seconds": 1800}


class Grown(TypedDict):
    parent: int
    level: int
    children: list[int]  # passages written directly (the next frontier)
    proposed: int  # choices filed as proposals instead


class FrontierState(TypedDict, total=False):
    run_id: int | None
    story_id: int
    root: int
    budget: dict[str, Any]
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
    allot: int  # new passages this grow may write
    steer: str
    params: Params
    batch: str
    grown: Annotated[list[Grown], operator.add]
    errors: Annotated[list[str], operator.add]


def _created(state: FrontierState) -> int:
    return sum(len(g["children"]) for g in state.get("grown", []))


def plan(state: FrontierState) -> FrontierState:
    _run().check()
    level = state.get("level", 0)
    if level == 0:
        frontier = [state["root"]]
    else:
        frontier = [c for g in state["grown"] if g["level"] == level - 1 for c in g["children"]]
    _run().report(level=level, created=_created(state), frontier=len(frontier))
    return {"level": level, "frontier": frontier, "batch": state.get("batch") or uuid.uuid4().hex}


def fan_out_frontier(state: FrontierState) -> list[Send]:
    """Split what is left of max_passages across the frontier; passages allotted nothing are skipped."""
    n = len(state["frontier"])
    remaining = max(0, state["budget"]["max_passages"] - _created(state))
    common = {"run_id": state.get("run_id"), "story_id": state["story_id"], "level": state["level"],
              "steer": state.get("steer", ""), "params": state["params"], "batch": state["batch"]}
    sends = []
    for i, pid in enumerate(state["frontier"]):
        allot = remaining // n + (1 if i < remaining % n else 0)
        if allot > 0:
            sends.append(Send("grow", {**common, "passage_id": pid, "allot": allot}))
    return sends or [Send("gather", {})]


FRONTIER_NOTE = (
    " Every target_title must be NEW — not in ALL PASSAGE TITLES — because the story is growing outward here; "
    "do not lead back to an existing passage."
)


def _steer(prompt: str, steer: str) -> str:
    return prompt + (f"\n\nAUTHOR'S DIRECTION for everything you write: {steer.strip()}" if steer.strip() else "")


def grow(state: GrowState) -> GrowState:
    """Choices out of one passage and a drafted destination for each; writes or proposes per the reviewed rule."""
    _run().check()
    pid = state["passage_id"]
    ctx = gather_context({"story_id": state["story_id"], "passage_id": pid, "params": {}})["context"]
    p = ctx["passage"]
    prompt = _steer(render_context(ctx) + "\n\nTASK: " + INSTRUCTIONS["suggest_choices"] + FRONTIER_NOTE, state["steer"])
    try:
        drafts = _ask(state["params"], ChoiceDrafts, prompt, activity={"passage_id": pid, "phase": "choices"}).choices
    except Exception as e:
        return {"errors": [f"passage {pid} ({p['title']}): {type(e).__name__}: {e}"]}
    # Every kept choice gets a new passage: a frontier grows outward, it does not
    # link back (merging is deferred; see agentic-authoring). Existing titles the
    # model offers anyway are drafted afresh under the choice.
    seen = {o["text"].strip().lower() for o in ctx["outgoing"]}
    kept = []
    for c in drafts:
        text, target = c.text.strip(), c.target_title.strip()
        if not text or text.lower() in seen or len(kept) >= state["allot"]:
            continue
        seen.add(text.lower())
        kept.append((text, target))
    if not kept:
        return {"errors": [f"passage {pid} ({p['title']}): every suggested choice duplicated an existing one"],
                "grown": [{"parent": pid, "level": state["level"], "children": [], "proposed": 0}]}

    children: list[int] = []
    proposed = 0
    order = 1 + max((o.get("sort_order", 0) for o in ctx["outgoing"]), default=-1)
    for i, (text, target) in enumerate(kept):
        pos = {"pos_x": p["pos_x"] + (i - (len(kept) - 1) / 2) * 220, "pos_y": p["pos_y"] + 180}
        _run().check()
        try:
            draft = _ask(state["params"], PassageDraft, _steer(render_context(ctx) + "\n\nTASK: " + INSTRUCTIONS["draft_target"].format(choice=text), state["steer"]),
                         activity={"passage_id": pid, "phase": "passage", "choice": text})
            target, body = draft.title.strip() or target, draft.body.strip()
        except Exception as e:
            return {"errors": [f"passage {pid} ({p['title']}), choice '{text}': {type(e).__name__}: {e}"],
                    "grown": [{"parent": pid, "level": state["level"], "children": children, "proposed": proposed}]}
        with db.transaction() as conn:
            if p["reviewed"]:
                payload = {"text": text, "to_passage": None, "new_target": {"title": target, "body": body, **pos}}
                _insert_proposal(conn, state["story_id"], state["batch"], "expand_frontier",
                                 {"kind": "new_choice", "target_passage": pid, "payload": payload}, state.get("run_id"))
                proposed += 1
            else:
                to = conn.execute(
                    "INSERT INTO passages (story_id, title, body, pos_x, pos_y, t, provenance) VALUES (?, ?, ?, ?, ?, ?, 'agent')",
                    (state["story_id"], target, body, pos["pos_x"], pos["pos_y"], p["t"]),
                ).lastrowid
                children.append(to)
                conn.execute(
                    "INSERT INTO choices (story_id, from_passage, to_passage, text, sort_order) VALUES (?, ?, ?, ?, ?)",
                    (state["story_id"], pid, to, text, order + i),
                )
            db.touch_story(conn, state["story_id"])
    return {"grown": [{"parent": pid, "level": state["level"], "children": children, "proposed": proposed}]}


def gather(state: FrontierState) -> FrontierState:
    _run().report(level=state["level"], created=_created(state), frontier=len(state["frontier"]))
    return {}


def after_gather(state: FrontierState) -> Literal["review", "finish"]:
    budget, level = state["budget"], state["level"]
    next_frontier = [c for g in state["grown"] if g["level"] == level for c in g["children"]]
    if (state.get("stop") or not next_frontier or _created(state) >= budget["max_passages"]
            or level + 1 >= budget["depth"] or time.time() > state["deadline"]):
        return "finish"
    return "review"


def review(state: FrontierState) -> FrontierState:
    """Pause between levels. Resumed with {"steer": str, "stop": bool}."""
    level = state["level"]
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
    budget = {**DEFAULT_BUDGET, **budget}
    return {"story_id": story_id, "root": scope[0], "budget": budget, "deadline": time.time() + budget["max_seconds"],
            "params": params, "level": 0, "steer": params.get("steer") or "", "stop": False}


# --- registry ----------------------------------------------------------------------

# Workflows a run can execute: the uncompiled graph (runs.py compiles it with
# its checkpointer) and how to turn a run request into the graph's input.
WORKFLOWS: dict[str, tuple[Any, Any]] = {
    "canon_check": (build_canon_graph, canon_input),
    "expand_frontier": (build_frontier_graph, frontier_input),
}


def workflow_json() -> dict[str, dict[str, Any]]:
    """Topology per workflow for the Agents view: {"nodes": [...], "edges": [...]}."""
    return {"verb": VERB_GRAPH.get_graph().to_json(),
            **{name: build().compile().get_graph().to_json() for name, (build, _) in WORKFLOWS.items()}}
