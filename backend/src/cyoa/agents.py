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
import contextlib
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
from pydantic import BaseModel, Field, ValidationError

from . import db, engine, llm, prompts, settings

MAX_ATTEMPTS = 3
LORE_CAP = 20
SPINE_CAP = 20  # passages of the reader's path shown in full above the current one
# How many model calls may be in flight across the whole process. Two were
# enough to OOM the GPU with a 27B model on 2026-09-20, so the default is a
# queue. Raise it only for a server that is known to batch.
ENV_CONCURRENCY = int(os.environ.get("CYOA_AGENT_CONCURRENCY", "1"))


class Gate:
    """A counting semaphore whose size can change while calls are in flight: the Settings screen
    resizes it. Raising it lets waiting calls through at once; lowering it never cuts a call in
    progress, it just holds new ones until enough have finished."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self._active = 0
        self._cond = threading.Condition()

    def acquire(self, timeout: float | None = None) -> bool:
        with self._cond:
            if not self._cond.wait_for(lambda: self._active < self.limit, timeout):
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify()

    def resize(self, limit: int) -> None:
        with self._cond:
            self.limit = max(1, limit)
            self._cond.notify_all()


_GATE = Gate(settings.concurrency() or ENV_CONCURRENCY)

T = TypeVar("T")
R = TypeVar("R")


def _fan(items: list[T], worker: Callable[[T], R]) -> list[R]:
    """Run `worker` over `items`, up to the gate's size at once (the auditors and votes of one draft;
    expand_frontier's passages are written one at a time, depth-first). The
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(items), _GATE.limit)) as pool:
        futures = [pool.submit(contextvars.copy_context().run, worker, item) for item in items]
        return [f.result() for f in futures]


class ReplyTruncated(Exception):
    """The model hit its token ceiling (finish_reason 'length') before finishing its answer."""

    def __init__(self, cap: int):
        super().__init__(f"the reply ran past its {cap}-token ceiling and was cut off")
        self.cap = cap


def _ask(params: "Params", schema: type[BaseModel], prompt: str, *, temperature: float = 0.8,
         activity: dict[str, Any] | None = None, reasoning: bool = False, max_tokens: int | None = None) -> BaseModel:
    """One structured-output model call, throttled by the process-wide gate. `activity`
    (passage_id, phase) is reported to the run only once the call actually starts, so
    the map highlights what the model is working on, not what is queued behind it."""
    run = _run()
    http = llm.new_http_client()
    model = llm.chat_model(params.get("model"), temperature=temperature, reasoning=reasoning, max_tokens=max_tokens, http_client=http)
    if not hasattr(schema, "from_prose"):
        model = model.with_structured_output(schema, method=llm.structured_method(), include_raw=True)
    # Cancel must not wait for a slot or for the model: waiting on the gate polls the flag,
    # and the call runs in its own thread so the wait below can give up on it at once.
    while not _GATE.acquire(timeout=0.2):
        run.check()
    try:
        run.check()
        if activity:
            run.emit({"type": "activity", **activity})
        box: dict[str, Any] = {}
        ctx = contextvars.copy_context()

        def call() -> None:
            try:
                box["out"] = ctx.run(model.invoke, prompt)
            except BaseException as e:  # noqa: BLE001 — handed to the waiting thread
                box["err"] = e

        worker = threading.Thread(target=call, daemon=True, name="model-call")
        with run.aborting(http.close if http else None):
            worker.start()
            while worker.is_alive():
                worker.join(0.2)
                run.check()
        if "err" in box:
            raise box["err"]
        out = _unwrap(box["out"], max_tokens or llm.MAX_TOKENS, schema)
    finally:
        _GATE.release()
        if http:
            http.close()
        if activity:
            run.emit({"type": "activity", "passage_id": activity["passage_id"], "phase": "done"})
    return out if isinstance(out, schema) else schema(**(out or {}))


def _unwrap(result: Any, cap: int, schema: type[BaseModel] | None = None) -> Any:
    """The answer from a model call: for a schema with `from_prose`, the plain reply parsed by it;
    otherwise the parsed result of `with_structured_output(include_raw=True)`. A reply the server
    stopped at the token ceiling is raised as ReplyTruncated whether or not it parsed: a cut-off
    reply loses its body or ends mid-sentence, and the cause is the ceiling, not the model."""
    prose = hasattr(schema, "from_prose")
    if not (prose or (isinstance(result, dict) and "raw" in result)):
        return result
    meta = getattr(result if prose else result["raw"], "response_metadata", None) or {}
    if "length" in (meta.get("finish_reason"), meta.get("done_reason")):
        raise ReplyTruncated(cap)
    if prose:
        content = result.content
        return schema.from_prose(content if isinstance(content, str) else "".join(
            b if isinstance(b, str) else b.get("text", "") for b in content))
    if result.get("parsing_error"):
        raise result["parsing_error"]
    return result["parsed"]


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
        self._closers: list[Callable[[], None]] = []
        self.progress = dict(progress or {})

    def emit(self, event: dict[str, Any]) -> None:
        if self._emit:
            self._emit(event)

    @contextlib.contextmanager
    def aborting(self, closer: Callable[[], None] | None):
        """While in the block, `abort()` also calls `closer` (drops the model connection so
        the server stops generating)."""
        if closer:
            with self._lock:
                self._closers.append(closer)
        try:
            yield
        finally:
            if closer:
                with self._lock:
                    if closer in self._closers:
                        self._closers.remove(closer)

    def abort(self) -> None:
        """Cancel now: set the flag and close every open model connection."""
        if self._cancel is not None:
            self._cancel.set()
        with self._lock:
            closers = list(self._closers)
        for close in closers:
            try:
                close()
            except Exception:  # noqa: BLE001 — a connection already gone is the goal
                pass

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


class FrontierChoiceDraft(ChoiceDraft):
    intent: str = Field("", description="one sentence on what the passage this choice leads to should do: what happens "
                        "next, straight after PASSAGE, and how it differs from the other choices' passages")


class FrontierChoiceDrafts(BaseModel):
    choices: list[FrontierChoiceDraft] = Field(description="distinct choices: as many as the TASK allows, each with its intent")


class ChoiceDrafts(BaseModel):
    choices: list[ChoiceDraft] = Field(description="distinct choices: as many as the TASK allows, otherwise two to four")


class PassageDraft(BaseModel):
    title: str = Field(description="a short passage title")
    body: str = Field(description="the passage text, second person present tense, one to three short paragraphs")


_CONTROL_TOKEN = re.compile(r"<\|[a-z_]+>|<[a-z_]+\|>")  # a chat template's own markers, e.g. <|channel>, <|tool_call>
_THE_END = re.compile(r"^\W*the end\W*$", re.IGNORECASE)


class FrontierDraft(BaseModel):
    """expand_frontier's passage. Asked for as plain prose, not a tool call (`from_prose`): a local
    model that writes a thousand words inside a tool call's argument too often leaves it unclosed
    or leaks the call's syntax into the body (2026-10-04). No length in the schema, for the same
    reason as before: the pacing plan's {length} sets it (concepts/act-pacing.md). The title is
    the choices step's (`_pick_title`), so `title` stays blank."""

    title: str = Field(default="", description="unused: the passage's title comes from the choices step")
    body: str = Field(description="the passage text, second person present tense, as long as the TASK asks")

    @classmethod
    def from_prose(cls, text: str) -> "FrontierDraft":
        """The reply as a draft: the text before any chat-template marker the model leaked, with a
        closing line reading 'The End' taken as ends_story (and dropped from the body)."""
        body = _CONTROL_TOKEN.split(text or "")[0].strip()
        lines = body.splitlines()
        ended = bool(lines) and bool(_THE_END.match(lines[-1]))
        if ended:
            body = "\n".join(lines[:-1]).strip()
        extra = {"ends_story": True} if ended and "ends_story" in cls.model_fields else {}
        return cls(body=body, **extra)


class FrontierPassageDraft(FrontierDraft):
    ends_story: bool = Field(False, description="true when the writer closed the passage with 'The End'")


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
    """Read the passage's neighbourhood: story, variables, in/out links, lore."""
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
        spine = _spine(conn, story["start_passage_id"], passage["id"])
        choice = None
        if state["params"].get("choice_id") is not None:
            row = conn.execute("SELECT * FROM choices WHERE id = ?", (state["params"]["choice_id"],)).fetchone()
            choice = dict(row) if row else None
    return {"context": {"story": story, "passage": passage, "variables": variables, "titles": titles,
                        "spine": spine, "inbound": inbound, "outgoing": outgoing, "lore": lore, "choice": choice},
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


def render_context(ctx: dict[str, Any], titles: bool = True) -> str:
    s, p = ctx["story"], ctx["passage"]
    lines = [f"STORY: {s['title']}", s["description"] or "(no description)", ""]
    if ctx["variables"]:
        lines += ["VARIABLES: " + ", ".join(f"{v['name']} ({v['kind']}, default {v['default']})" for v in ctx["variables"]), ""]
    if ctx["lore"]:
        lines += ["WORLD:"] + [f"- [{l['kind']}] {l['name']}: {l['body']}" for l in ctx["lore"]] + [""]
    where = ", ".join(x for x in [
        f"t={_num(p['t'])}" if p["t"] is not None else "",
        f"location: {p['meta'].get('location')}" if p["meta"].get("location") else "",
        f"present: {', '.join(p['meta'].get('present', []))}" if p["meta"].get("present") else "",
    ] if x)
    if ctx.get("arc"):
        lines += [ctx["arc"], ""]
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
    if titles:  # for a writer whose title is not its to pick, the list is noise
        lines += ["ALL PASSAGE TITLES: " + "; ".join(ctx["titles"].values())]
    return "\n".join(lines)


def compose(ctx: dict[str, Any] | None, prompts: dict[str, str], key: str, *, frame: str | None = None,
            steer: str = "", error: str | None = None, **fields: str) -> str:
    """The one place a prompt is assembled: the context frame, the role's TASK text
    formatted with `fields`, then the retry line and the author's steer when set.
    Roles are data (prompts.py); the frame, retry and steer wording stay code. `frame`
    replaces the passage frame for a role that is not about a passage (the interview)."""
    prompt = (render_context(ctx, titles=key != "frontier_passage") if frame is None else frame) + "\n\nTASK: " + prompts[key].format(**fields)
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
                                   "audit_continuity", "audit_canon", "revise_passage", "audit_ending", "revise_ending"],
    ("interview", "converse"): ["interview"],
    ("interview", "review"): ["audit_interview"],
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


# --- interview ----------------------------------------------------------------------
#
# Interview Me (concepts/interview-me.md): one synchronous turn of an agent-led
# conversation that builds the premise (stories.description) and the character
# suite (lore rows of kind 'character'). The shape is the verbs': gather → converse
# → validate ⟲ → apply, no checkpointer. The agent writes the premise and
# unreviewed characters directly; anything it cannot write — a reviewed character,
# a name that is already taken — becomes an open conflict, and conflicts are what
# it asks about next. Only the author's answer resolves one.


# Structured output through a tool call has no way to say "no value" except in words: a model
# asked for `premise: string | null` sometimes returns the string "null" (2026-09-30: it
# overwrote a story's description with it).
_NULLISH = {"null", "none", "nil", "n/a", "na", "undefined"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z']+", text.casefold()))


def _repeats(question: str, earlier: list[str]) -> str | None:
    """The earlier question this one restates: at least half of the words of both in common
    (reworded repeats scored 0.58 to 1.0, unrelated questions 0.26 or less, 2026-09-30)."""
    mine = _words(question)
    for q in earlier:
        theirs = _words(q)
        if mine and theirs and len(mine & theirs) / len(mine | theirs) >= 0.5:
            return q
    return None


def _nullish(text: str | None) -> bool:
    return (text or "").strip().strip(".").casefold() in _NULLISH


class CharacterOp(BaseModel):
    op: Literal["create", "edit", "merge"] = Field(description="create a new character, edit one in CHARACTERS, or merge "
                                                   "duplicates into one")
    id: int | None = Field(default=None, description="edit/merge: the #id from CHARACTERS of the entry to change or keep")
    merge_ids: list[int] = Field(default_factory=list, description="merge only: the #ids folded into `id` and deleted")
    name: str = Field(description="the character's name")
    body: str = Field(description="everything known about the character: who they are, what they want, how they stand "
                      "toward the reader, what they know or hide")


class ConflictDraft(BaseModel):
    about: str = Field(description="what the contradiction concerns, in a few words")
    a: str = Field(description="one statement, quoted from PREMISE, CHARACTERS, OTHER LORE or the conversation")
    b: str = Field(description="the statement that cannot be true alongside it, quoted")


class ReviewIssue(BaseModel):
    change: int = Field(description="the [number] of the proposed change this is about; 0 for the proposed question")
    problem: str = Field(description="what is wrong with writing it, one sentence")


class InterviewReview(BaseModel):
    verdict: Literal["pass", "revise"]
    issues: list[ReviewIssue] = Field(default_factory=list)


class InterviewReply(BaseModel):
    premise: str | None = Field(default=None, description="the whole revised story description, or null to leave it as is")
    characters: list[CharacterOp] = Field(default_factory=list, description="the character changes the latest message establishes")
    conflicts: list[ConflictDraft] = Field(default_factory=list, description="new contradictions; do not resolve them yourself")
    resolved: list[int] = Field(default_factory=list, description="#ids of OPEN CONFLICTS the author's latest message settles")
    reply: str = Field(description="your next message to the author: exactly one short question")


# One interview turn is a short exchange: the agent replies, code answers with everything wrong
# with what it tried to do (a create for someone who already exists, an id that is not there),
# and the agent gets its own reply back to correct. That is at most this many replies.
INTERVIEW_ATTEMPTS = 4


class InterviewState(TypedDict, total=False):
    story_id: int
    interview_id: int
    answer: dict[str, Any] | None  # the author's entry {role, text, mode}, None on the opening turn
    params: Params
    context: dict[str, Any]
    draft: dict[str, Any] | None
    plan: dict[str, Any]
    error: str | None
    attempts: int
    review_failed: bool  # the reviewer could not be reached: the turn fails and writes nothing
    interview: dict[str, Any]


def _key(name: str) -> str:
    return " ".join(name.lower().split())


def interview_context(story_id: int, interview_id: int | None) -> dict[str, Any]:
    """Everything the agent may read: the premise, every character (uncapped, with ids and
    `reviewed`), the rest of the lore, the opening passage, the conversation and its
    conflicts. No interview (the Agents view's preview) reads as an empty conversation."""
    with db.transaction() as conn:
        story = dict(conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone())
        lore = [dict(r) for r in conn.execute("SELECT id, kind, name, body, reviewed FROM lore WHERE story_id = ? ORDER BY kind, name, id",
                                              (story_id,))]
        opening = conn.execute("SELECT title, body FROM passages WHERE id = ?", (story["start_passage_id"],)).fetchone()
        iv = (engine.decode_row("interviews", conn.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone())
              if interview_id is not None else {"transcript": [], "conflicts": []})
    return {"story": story, "characters": [l for l in lore if l["kind"] == "character"],
            "other_lore": [l for l in lore if l["kind"] != "character"], "opening": dict(opening) if opening else None,
            "transcript": iv["transcript"], "conflicts": iv["conflicts"]}


def gather_interview(state: InterviewState) -> InterviewState:
    ctx = interview_context(state["story_id"], state["interview_id"])
    if state.get("answer"):  # the author's entry is part of the conversation the agent reads
        ctx["transcript"] = ctx["transcript"] + [state["answer"]]
    return {"context": ctx, "attempts": 0, "error": None}


def render_interview(ctx: dict[str, Any]) -> str:
    s = ctx["story"]
    lines = [f"STORY: {s['title']}", "", "PREMISE (the story description every writing and review agent reads first):",
             s["description"] or "(empty)", ""]
    lines += ["CHARACTERS (yours to create, edit and merge; a [reviewed] entry is the author's approved text):"]
    lines += [f"- #{c['id']} {c['name']}{' [reviewed]' if c['reviewed'] else ''}: {c['body'] or '(no description yet)'}"
              for c in ctx["characters"]] or ["(none yet)"]
    lines += [""]
    if ctx["other_lore"]:
        lines += ["OTHER LORE (read-only: report contradictions with it as conflicts):"]
        lines += [f"- [{l['kind']}] {l['name']}: {l['body']}" for l in ctx["other_lore"]] + [""]
    if ctx["opening"]:
        lines += [f"OPENING PASSAGE (read-only): {ctx['opening']['title']}", ctx["opening"]["body"] or "(empty)", ""]
    open_ = [c for c in ctx["conflicts"] if c["status"] == "open"]
    if open_:
        lines += ["OPEN CONFLICTS (ask the author to settle these before anything new):"]
        lines += [f"- #{c['id']} {c['about']}: \"{c['a']}\" versus \"{c['b']}\"" for c in open_] + [""]
    lines += ["CONVERSATION (oldest first):"]
    for e in ctx["transcript"]:
        if e["role"] == "agent":
            lines.append(f"YOU: {e['text']}")
        elif e.get("mode") == "skip":
            lines.append("AUTHOR: (skipped your question; do not ask it again in any wording, ask about something else)")
        elif e.get("mode") == "decide":
            lines.append("AUTHOR: (asked you to decide)" + (f" {e['text']}" if e["text"].strip() else ""))
        else:
            lines.append(f"AUTHOR: {e['text']}")
    if not ctx["transcript"]:
        lines.append("(empty: this is the opening turn)")
    return "\n".join(lines)


def converse(state: InterviewState) -> InterviewState:
    params = state["params"]
    prompt = compose(None, _prompts(params), "interview", frame=render_interview(state["context"]), error=state.get("error"))
    try:
        draft = _ask(params, InterviewReply, prompt, temperature=0.7).model_dump()
        return {"draft": draft, "error": None, "attempts": state["attempts"] + 1}
    except Exception as e:  # model returned nothing parseable
        return {"draft": None, "error": f"{type(e).__name__}: {e}", "attempts": state["attempts"] + 1}


def validate_interview(state: InterviewState) -> InterviewState:
    """Checks the schema cannot express. Everything the agent can fix comes back to it in
    one message, with its own reply, and it answers again: unknown ids, ops on non-characters,
    empty text, and a create or rename onto a name that is already taken. A create that
    repeats an existing character word for word, or an edit that changes nothing, is a no-op,
    not a problem. What only the author can settle becomes an open conflict: a change to a
    reviewed character, and (once the agent's attempts are spent) a name it still cannot
    resolve."""
    draft, ctx = state.get("draft"), state["context"]
    if draft is None:
        return {"error": state.get("error") or "no output"}
    chars = {c["id"]: c for c in ctx["characters"]}
    other = {l["id"]: l for l in ctx["other_lore"]}
    if _blank(draft.get("reply")):
        return {"draft": None, "error": "reply was empty; end with one question for the author"}
    premise = draft.get("premise")
    if _nullish(premise):
        premise = None  # the model said "no change" in words
    if premise is not None:
        if _placeholder_body(premise):
            return {"draft": None, "error": "premise was empty or a '...' placeholder; return null to leave it unchanged"}
        if premise.strip() == (ctx["story"]["description"] or "").strip():
            premise = None
    names = {_key(c["name"]): c for c in ctx["characters"]}
    names.update({_key(l["name"]): l for l in ctx["other_lore"]})
    ops: list[dict[str, Any]] = []
    problems: list[str] = []  # the agent can fix these
    collisions: list[tuple[dict[str, Any], str]] = []  # (the conflict it becomes, what to tell the agent)
    conflicts = [{"about": c["about"].strip(), "a": c["a"].strip(), "b": c["b"].strip(), "source": "model"}
                 for c in draft.get("conflicts", []) if not _blank(c.get("about")) and not _blank(c.get("a")) and not _blank(c.get("b"))]
    for i, op in enumerate(draft.get("characters", []), 1):
        kind, name, body = op["op"], (op.get("name") or "").strip(), (op.get("body") or "").strip()
        if _blank(name) or _placeholder_body(body) or _nullish(name) or _nullish(body):
            problems.append(f"character change {i} has an empty name or body; write both out in full")
            continue
        ids = [op.get("id")] + (op.get("merge_ids") or []) if kind != "create" else []
        unknown = [f"#{x} is not a character; only CHARACTERS can be changed" if x in other
                   else f"character change {i} names #{x}, which is not in CHARACTERS" for x in ids if x not in chars]
        if unknown:
            problems += unknown
            continue
        if kind == "merge" and (not op.get("merge_ids") or op["id"] in op["merge_ids"]):
            problems.append(f"merge {i} needs merge_ids: the other entries folded into #{op['id']}")
            continue
        reviewed = [chars[x] for x in ids if chars[x]["reviewed"]]
        if reviewed:
            r = reviewed[0]
            conflicts.append({"about": f"{r['name']} is reviewed", "a": f"{r['name']}: {r['body']}", "b": f"{name}: {body}", "source": "code"})
            continue
        if kind == "edit" and _norm(chars[op["id"]]["name"]) == _norm(name) and _norm(chars[op["id"]]["body"]) == _norm(body):
            continue  # already says exactly this
        taken = names.get(_key(name))
        if taken is not None and taken["id"] not in ids:
            if kind == "create" and _norm(taken["body"]) == _norm(body):
                continue  # already there, word for word: nothing to add
            if taken["id"] in chars:
                tell = (f"character change {i} would create a new character called {name}, but #{taken['id']} {taken['name']} already "
                        f"exists ({taken['body'][:140]}). To change that character use op \"edit\" with id {taken['id']}; to add "
                        f"someone else give them a different name; to add no one leave this change out")
            elif taken["id"] in other:
                tell = (f"character change {i}: the name {name} is already used by the {other[taken['id']]['kind']} "
                        f"{taken['name']} in OTHER LORE; choose a different name")
            else:
                tell = f"character change {i}: the name {name} is used twice in this reply; combine them or rename one"
            collisions.append(({"about": f"the name {name} is taken", "a": f"{taken['name']}: {taken['body']}",
                                "b": f"{name}: {body}", "source": "code"}, tell))
            continue
        names[_key(name)] = {"id": op.get("id"), "name": name, "body": body}
        ops.append({"op": kind, "id": op.get("id"), "merge_ids": op.get("merge_ids") or [], "name": name, "body": body})
    # A restated question is worth a retry, never a failure: conflict questions repeat by design
    # until the author settles them, and on the last attempt the turn keeps what it has.
    if state["attempts"] < INTERVIEW_ATTEMPTS and not any(c["status"] == "open" for c in ctx["conflicts"]):
        again = _repeats(draft["reply"], [e["text"] for e in ctx["transcript"] if e["role"] == "agent"])
        if again:
            problems.append(f"your question restates one you already asked ({again!r}); ask about something different")
    if problems or (collisions and state["attempts"] < INTERVIEW_ATTEMPTS):
        found = problems + [tell for _, tell in collisions]
        return {"draft": None, "error": "these changes could not be made:\n" + "\n".join(f"- {f}" for f in found)
                + "\nYour previous reply was:\n" + json.dumps(draft) + "\nReturn the whole reply again with only these problems fixed"}
    conflicts += [c for c, _ in collisions]  # attempts spent: the author decides
    # Only an author's message can settle a conflict: not the opening turn, not a skip.
    answered = bool(state.get("answer")) and state["answer"].get("mode") != "skip"
    open_ids = {c["id"] for c in ctx["conflicts"] if c["status"] == "open"}
    resolved = sorted({x for x in draft.get("resolved", []) if x in open_ids}) if answered else []
    return {"plan": {"premise": premise, "ops": ops, "conflicts": conflicts, "resolved": resolved,
                     "reply": draft["reply"].strip()}, "error": None}


def after_interview_validate(state: InterviewState) -> Literal["converse", "review", "fail"]:
    if not state.get("error"):
        return "review"
    return "converse" if state["attempts"] < INTERVIEW_ATTEMPTS else "fail"


def _proposed_changes(ctx: dict[str, Any], plan: dict[str, Any]) -> list[tuple[tuple[str, int | None], str]]:
    """What the plan would write, each as (what it is, the text the reviewer reads): the premise
    and every character op, with the text they would replace."""
    chars = {c["id"]: c for c in ctx["characters"]}
    out: list[tuple[tuple[str, int | None], str]] = []
    if plan["premise"] is not None:
        out.append((("premise", None), f"PREMISE\nBEFORE: {ctx['story']['description'] or '(empty)'}\nAFTER: {plan['premise']}"))
    for i, op in enumerate(plan["ops"]):
        if op["op"] == "create":
            text = f"NEW CHARACTER {op['name']}\nAFTER: {op['body']}"
        else:
            old = chars[op["id"]]
            text = (f"{'MERGE INTO' if op['op'] == 'merge' else 'EDIT'} CHARACTER #{old['id']} {old['name']}\n"
                    f"BEFORE: {old['body']}\nAFTER: {op['name']}: {op['body']}")
            if op["op"] == "merge":
                text += "\nFOLDED IN AND DELETED: " + ", ".join(f"#{x} {chars[x]['name']}" for x in op["merge_ids"])
        out.append((("op", i), text))
    return out


def review_interview(state: InterviewState) -> InterviewState:
    """The gate on every write. A second model call reads the proposed changes next to the text
    they replace and the conversation, and reports the ones that should not be written: not
    established by the author, losing facts nobody changed, or not real content (the string
    "null" once overwrote a description; no static check can anticipate the next such thing).
    A note goes back to the agent with its own reply, like validation's problems, and it
    answers again. When its attempts are spent, a change the reviewer still objects to is not
    written; it becomes an open conflict for the author. The agent's question is reviewed too, as
    change [0]: it must not ask what happens next, repeat an earlier question or ask two at once.
    A question is never held back, since the turn needs one; only the writes are. If the reviewer
    cannot be reached the turn fails and writes nothing."""
    plan, ctx = state["plan"], state["context"]
    changes = _proposed_changes(ctx, plan)
    numbered = "\n\n".join(f"[{n}] {text}" for n, (_, text) in enumerate(changes, 1)) or "(none)"
    # The author's latest message is repeated next to the changes: buried in a long CONVERSATION
    # the reviewer read past it and objected to a fact the author had just stated (2026-09-30 probe).
    said = next((e for e in reversed(ctx["transcript"]) if e["role"] == "author"), None)
    if said is None:
        latest = "(none: this is the opening turn)"
    elif said.get("mode") == "skip":
        latest = "(skipped the agent's question)"
    elif said.get("mode") == "decide":
        latest = "(asked the agent to decide)" + (f" {said['text']}" if said["text"].strip() else "")
    else:
        latest = said["text"]
    prompt = compose(None, _prompts(state["params"]), "audit_interview",
                     frame=render_interview(ctx) + f"\n\nTHE AUTHOR'S LATEST MESSAGE: {latest}\n\n"
                     f"PROPOSED QUESTION [0]: {plan['reply']}\n\nPROPOSED CHANGES:\n" + numbered)
    review, last = None, None
    for _ in range(MAX_ATTEMPTS):
        try:
            review = _ask(state["params"], InterviewReview, prompt, temperature=AUDIT_TEMPERATURE)
            break
        except Exception as e:
            last = e
    if review is None:
        return {"review_failed": True, "error": f"the reviewer could not run: {type(last).__name__}: {last}"}
    notes = [(i.change, i.problem.strip()) for i in review.issues if 0 <= i.change <= len(changes) and not _blank(i.problem)]
    if not notes:
        return {}
    if state["attempts"] < INTERVIEW_ATTEMPTS:
        found = "\n".join(f"- {'your question' if n == 0 else f'change [{n}]'}: {problem}" for n, problem in notes)
        return {"draft": None, "error": "a reviewer would not accept this reply:\n" + found
                + "\nYour previous reply was:\n" + json.dumps(state["draft"])
                + "\nReturn the whole reply again with only these problems fixed; leave out any change the author did not establish"}
    held = {n: problem for n, problem in notes if n >= 1}
    if not held:
        return {}  # only the question was objected to, and a turn needs one
    conflicts = list(plan["conflicts"])
    for n, problem in held.items():
        (kind, idx), text = changes[n - 1]
        before, _, after = text.partition("AFTER: ")
        conflicts.append({"about": f"held back: {problem}"[:200], "a": before.split("BEFORE: ", 1)[-1].strip()[:400] or "(nothing yet)",
                          "b": after.strip()[:400], "source": "code"})
    dropped = {changes[n - 1][0] for n in held}
    return {"plan": {**plan, "conflicts": conflicts,
                     "premise": None if ("premise", None) in dropped else plan["premise"],
                     "ops": [op for i, op in enumerate(plan["ops"]) if ("op", i) not in dropped]}, "error": None}


def after_interview_review(state: InterviewState) -> Literal["converse", "apply", "fail"]:
    if state.get("review_failed"):
        return "fail"
    return "converse" if state.get("error") else "apply"


def _lore_fields(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {"name": row["name"], "body": row["body"]}


def apply_interview(state: InterviewState) -> InterviewState:
    """Write the plan in one transaction and append the turn to the transcript. Each
    write records {table, id, before, after}: before None is a create, after None a
    delete, and revert_interview replays them backwards."""
    plan, sid = state["plan"], state["story_id"]
    writes: list[dict[str, Any]] = []
    with db.transaction() as conn:
        if plan["premise"] is not None:
            before = conn.execute("SELECT description FROM stories WHERE id = ?", (sid,)).fetchone()["description"]
            conn.execute("UPDATE stories SET description = ? WHERE id = ?", (plan["premise"], sid))
            writes.append({"table": "stories", "id": sid, "before": {"description": before}, "after": {"description": plan["premise"]}})
        for op in plan["ops"]:
            after = {"name": op["name"], "body": op["body"]}
            if op["op"] == "create":
                row = engine.insert_row(conn, "lore", sid, {"kind": "character", **after})
                writes.append({"table": "lore", "id": row["id"], "before": None, "after": {"kind": "character", **after}})
                continue
            row = conn.execute("SELECT * FROM lore WHERE id = ? AND story_id = ?", (op["id"], sid)).fetchone()
            if row is None:  # deleted by the author since gather
                continue
            conn.execute("UPDATE lore SET name = ?, body = ? WHERE id = ?", (op["name"], op["body"], op["id"]))
            writes.append({"table": "lore", "id": op["id"], "before": _lore_fields(row), "after": after})
            for x in op["merge_ids"]:
                gone = conn.execute("SELECT * FROM lore WHERE id = ? AND story_id = ?", (x, sid)).fetchone()
                if gone is None:
                    continue
                conn.execute("DELETE FROM lore WHERE id = ?", (x,))
                writes.append({"table": "lore", "id": x, "after": None,
                               "before": {"kind": gone["kind"], "reviewed": bool(gone["reviewed"]), **_lore_fields(gone)}})
        iv = engine.decode_row("interviews", conn.execute("SELECT * FROM interviews WHERE id = ?", (state["interview_id"],)).fetchone())
        conflicts, transcript = iv["conflicts"], iv["transcript"]
        answer = state.get("answer")
        for c in conflicts:
            if c["id"] in plan["resolved"]:
                c.update(status="resolved", resolution=(answer or {}).get("text") or "(left to the agent)")
        next_id = max((c["id"] for c in conflicts), default=0) + 1
        opened = []
        for c in plan["conflicts"]:
            conflicts.append({"id": next_id, **c, "status": "open", "resolution": None})
            opened.append(next_id)
            next_id += 1
        if answer:
            transcript.append(answer)
        transcript.append({"role": "agent", "text": plan["reply"], "writes": writes, "opened": opened, "resolved": plan["resolved"]})
        conn.execute("UPDATE interviews SET transcript = ?, conflicts = ?, updated_at = datetime('now') WHERE id = ?",
                     (json.dumps(transcript), json.dumps(conflicts), state["interview_id"]))
        if writes:
            db.touch_story(conn, sid)
        out = engine.decode_row("interviews", conn.execute("SELECT * FROM interviews WHERE id = ?", (state["interview_id"],)).fetchone())
    return {"interview": out}


def fail_interview(state: InterviewState) -> InterviewState:
    return {}


def build_interview_graph():
    g = StateGraph(InterviewState)
    g.add_node("gather", gather_interview)
    g.add_node("converse", converse)
    g.add_node("validate", validate_interview)
    g.add_node("review", review_interview)
    g.add_node("apply", apply_interview)
    g.add_node("fail", fail_interview)
    g.add_edge(START, "gather")
    g.add_edge("gather", "converse")
    g.add_edge("converse", "validate")
    g.add_conditional_edges("validate", after_interview_validate, {"converse": "converse", "review": "review", "fail": "fail"})
    g.add_conditional_edges("review", after_interview_review, {"converse": "converse", "apply": "apply", "fail": "fail"})
    g.add_edge("apply", END)
    g.add_edge("fail", END)
    return g.compile()


INTERVIEW_GRAPH = build_interview_graph()


def run_interview_turn(story_id: int, interview_id: int, answer: dict[str, Any] | None, params: Params) -> dict[str, Any]:
    """One turn, synchronously: the author's entry (None to open) in, the updated interview row
    out. Nothing is written — not even the author's entry — unless the turn succeeds."""
    params = {**params, "prompts": prompts.resolve()}
    final = INTERVIEW_GRAPH.invoke({"story_id": story_id, "interview_id": interview_id, "answer": answer, "params": params})
    if "interview" not in final:
        raise VerbFailed(f"the interview gave up after {final.get('attempts', 0)} attempt(s): {final.get('error')}")
    return final["interview"]


class RevertConflict(Exception):
    pass


def revert_interview(conn: sqlite3.Connection, story_id: int, iv: dict[str, Any], entry: int, write: int | None) -> dict[str, Any]:
    """Undo one agent entry's writes (or just one of them), newest first. A write is undone
    only if its row still matches `after`: an author edit since then wins, and the revert
    refuses rather than clobber it. Conflicts the turn opened or resolved stay as they are."""
    transcript = iv["transcript"]
    if not 0 <= entry < len(transcript) or transcript[entry]["role"] != "agent":
        raise KeyError("no such agent entry")
    writes = transcript[entry].get("writes") or []
    picked = [write] if write is not None else list(range(len(writes)))
    if any(not 0 <= i < len(writes) for i in picked):
        raise KeyError("no such write")
    for i in reversed(picked):
        w = writes[i]
        if w.get("reverted"):
            continue
        if w["table"] == "stories":
            cur = conn.execute("SELECT description FROM stories WHERE id = ?", (story_id,)).fetchone()
            if cur["description"] != w["after"]["description"]:
                raise RevertConflict("the premise has changed since this turn")
            conn.execute("UPDATE stories SET description = ? WHERE id = ?", (w["before"]["description"], story_id))
        else:
            cur = conn.execute("SELECT * FROM lore WHERE id = ? AND story_id = ?", (w["id"], story_id)).fetchone()
            if w["after"] is None:
                if cur is not None:
                    raise RevertConflict(f"lore #{w['id']} exists again")
                b = w["before"]
                conn.execute("INSERT INTO lore (id, story_id, kind, name, body, reviewed) VALUES (?, ?, ?, ?, ?, ?)",
                             (w["id"], story_id, b["kind"], b["name"], b["body"], int(b["reviewed"])))
            else:
                if cur is None or _lore_fields(cur) != _lore_fields(w["after"]):
                    raise RevertConflict(f"{w['after']['name']} has changed since this turn")
                if w["before"] is None:
                    conn.execute("DELETE FROM lore WHERE id = ?", (w["id"],))
                else:
                    conn.execute("UPDATE lore SET name = ?, body = ? WHERE id = ?", (w["before"]["name"], w["before"]["body"], w["id"]))
        w["reverted"] = True
    conn.execute("UPDATE interviews SET transcript = ?, updated_at = datetime('now') WHERE id = ?", (json.dumps(transcript), iv["id"]))
    db.touch_story(conn, story_id)
    return engine.decode_row("interviews", conn.execute("SELECT * FROM interviews WHERE id = ?", (iv["id"],)).fetchone())


def interview_preview(story_id: int, roles: dict[str, str]) -> str:
    """The prompt an opening turn would send for this story, for the Agents view's preview."""
    return compose(None, roles, "interview", frame=render_interview(interview_context(story_id, None)))


# --- canon_check ------------------------------------------------------------------
#
# Reads every passage in scope against the lore and
# files a `note` proposal per inconsistency (optionally with an `edit_passage`
# fix). It is read-mostly and cheap, which is why it is the first autonomous
# workflow: `Send` maps one check per passage, and the reducer on `proposals`
# collects what each wrote. The fan-out is logical; `_ask` keeps the physical
# model calls to a queue. It only runs as a run (runs.py): the synchronous
# route it started life with was dropped once runs existed.


class CanonIssue(BaseModel):
    claim: str = Field(description="what the passage says or implies, quoted or paraphrased briefly")
    conflicts_with: str = Field(description="the lore entry it contradicts, by name")
    severity: Literal["minor", "major"] = Field(description="major if a reader would notice the contradiction")


class CanonReport(BaseModel):
    issues: list[CanonIssue] = Field(description="empty when the passage is consistent with the world")
    fixed_body: str | None = Field(default=None, description="the passage body with only the contradictions repaired, or null if there are no issues")


class AuditIssue(BaseModel):
    # defaulted: models often leave it out, and a missing severity is no reason to lose the whole audit
    severity: Literal["minor", "major"] = Field(default="minor", description="major if a reader would notice")
    problem: str = Field(description="what is wrong, one sentence")
    quote: str = Field(description="a verbatim span of the draft the issue is about")
    evidence: str = Field(default="", description="a verbatim span of the spine or WORLD the draft "
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
        return {"errors": [f"passage {p['id']} ({p['title']}): {brief(e)}"]}
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
# Grows the story from a root passage depth-first, one passage at a time: a storyline is
# followed to its ending before the next branch is started, so the first complete story
# exists as early as possible and can be read for pacing.
#
#   plan (pop the stack) → grow one passage → gather (push its children) → review → plan | finish
#   review —interrupt (a storyline just closed)→ plan
#
# `grow` asks for choices out of a passage, drafts a destination for each new
# one, and writes them the way the reviewed rule says: directly, as agent
# passages, where the parent is unreviewed; as proposals where it is reviewed
# (those are not grown further). The run's budget is a copy of the story's
# pacing plan (concepts/act-pacing.md): a list of acts, then a fixed ending
# level. Each level belongs to one act, whose branching caps the choices out of
# its passages and whose guidance and length fill the frontier prompts. `gather`
# stops the run when the stack is empty or at the wall-clock cap. Each time a storyline
# closes (a passage grown with no children) `review` interrupts so the author can read
# it, steer, or stop.

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

# refinement-loops.md: a passage is drafted, then audited and revised up to `rounds` times.
# Leaving the loop, for any reason, is what "done" means; nothing is marked afterwards.
# `rounds: 0` skips the loop entirely.
DEFAULT_REFINE = {
    "rounds": 3, "votes": 1, "audit_reasoning": False,
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
    base: int  # the root's shortest distance from the start: the level its children are written at
    level: int  # the level of the passage being grown
    stack: list[dict[str, int]]  # passages still to grow, {"pid", "level"}; the last is next (depth-first)
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


def _endings(state: FrontierState) -> int:
    return sum(len(g.get("endings", [])) for g in state.get("grown", []))


def plan(state: FrontierState) -> FrontierState:
    """Take the next passage off the stack: the most recently pushed, so a storyline is followed down."""
    _run().check()
    stack = list(state["stack"])
    nxt = stack.pop()
    _run().report(level=nxt["level"], created=_created(state), frontier=len(stack) + 1, endings=_endings(state), open=len(stack) + 1)
    return {"level": nxt["level"], "stack": stack, "frontier": [nxt["pid"]], "batch": state.get("batch") or uuid.uuid4().hex}


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
            "length": s["length"], "act_guidance": s["guidance"],
            "ending_rule": (" If this passage is a natural conclusion of what the reader chose, a good end or a bad one, "
                            "finish it with a last line reading exactly: The End") if s["ends_story"] else ""}


def flow_fields(ctx: dict[str, Any], choice: tuple[str, str, str], siblings: list[tuple[str, str, str]]) -> dict[str, str]:
    """The handoff slots of frontier_passage and frontier_ending: where the parent ended, what this
    choice should lead to, and what the parent's other choices lead to (written separately, so this
    passage has to be told to keep clear of them)."""
    body = " ".join((ctx["passage"]["body"] or "").split())
    ending = "…" + body[-300:] if len(body) > 300 else body
    text, _, intent = choice
    others = [(t, i or tg) for t, tg, i in siblings if t != text]
    return {"parent_ending": f"\"{ending}\"" if ending else "(PASSAGE has no text yet)",
            "intent": intent or "follow naturally from the choice.",
            "siblings": "; ".join(f"'{t}' ({d})" if d else f"'{t}'" for t, d in others) or "none: this is the only choice",
            # frontier_passage's sentences: left out whole when there is nothing to say, so no slot reads as filler
            "intent_line": f" This choice should lead to: {intent}" if intent else "",
            "siblings_line": (" The reader's other choices from PASSAGE, written separately, lead to "
                              + "; ".join(f"'{t}' ({d})" for t, d in others if d)
                              + ": stay clear of their ground, so this passage is something only this choice could have led to.")
            if any(d for _, d in others) else ""}


def arc_block(plan: dict[str, Any], level: int) -> str:
    """Where the story is in the plan, for the frame of the frontier roles that write at `level`:
    the whole arc, the stage PASSAGE sits in, and the stage the passages written next open. The
    choices and the passage writers read this instead of counting passages."""
    here, dest = stage_at(plan, max(level - 1, 0)), stage_at(plan, level)
    names = [a["name"] for a in plan["acts"]] + ["Resolution"]

    def desc(st: dict[str, Any]) -> str:
        return st["name"] + (f": {st['guidance'].strip()}" if (st.get("guidance") or "").strip() else "")

    lines = ["STORY ARC: " + " → ".join(names) + " (the story ends with the Resolution)."]
    if here["index"] == dest["index"]:
        lines.append(f"PASSAGE and the passages written from it are in {desc(dest)}")
    else:
        lines += [f"PASSAGE is in {desc(here)}",
                  f"The passages written next open {desc(dest)}" + (" Each of them is an ending." if dest["ending"] else "")]
    return "\n".join(lines)


def _preview_level(plan: dict[str, Any], stage: int, role: str) -> int:
    stage = min(max(stage, 0), len(plan["acts"]))
    start = sum(a["levels"] for a in plan["acts"][:stage])
    return start + 1 if role == "frontier_choices" and 0 < stage < len(plan["acts"]) else start


def preview_fields(plan: dict[str, Any], stage: int, role: str) -> dict[str, str]:
    """The pacing slots as `stage` (an act's index, or len(acts) for the ending) fills them
    first: its first passage, or the first choices out of it — the root's, for the first
    act, and the last act's, for the ending, which offers none. The prompt preview uses
    this so it matches `grow`."""
    level = _preview_level(plan, stage, role)
    return choice_fields(plan, level) if role == "frontier_choices" else passage_fields(plan, level)


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
               problem: Callable[[Any], str | None], activity: dict[str, Any], max_tokens: int | None = None) -> Any:
    """`_ask` with the verbs' retry discipline for workflow nodes: up to MAX_ATTEMPTS calls,
    each retry told why the last answer was refused. `problem` returns None for a usable
    answer. Raises ValueError with the last reason when every attempt fails."""
    error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _run().check()
        started, kind, answer = time.monotonic(), "refused", None
        try:
            out = _ask(params, schema, prompt_for(error), activity=activity, max_tokens=max_tokens)
        except RunCancelled:
            raise
        except ReplyTruncated as e:
            # Cut off by the ceiling: ask again with room (up to the limit) and say why, so the writer is brief.
            max_tokens = min(e.cap * 2, max(llm.REPLY_TOKEN_LIMIT, e.cap))
            error, kind = f"{e} (the next try allows {max_tokens}); finish the passage within the length asked", "truncated"
        except Exception as e:  # a failed call, or an answer that fails the schema (an empty tool call)
            error, kind = brief(e), "exception"
        else:
            error = problem(out)
            if error is None:
                return out
            answer = out.model_dump_json()[:600]  # what the model said that was refused
        # Retries are the data that tunes prompts and future audit loops: count them on
        # the run (persisted in progress) and keep each one's reason, kind and duration.
        _run().tick("retries")
        _run().emit({"type": "retry", "passage_id": activity.get("passage_id"), "phase": activity.get("phase"),
                     "attempt": attempt, "kind": kind, "seconds": round(time.monotonic() - started, 1), "reason": error,
                     "schema": schema.__name__, "answer": answer, "model": params.get("model") or llm.DEFAULT_MODEL})
    raise ValueError(f"no usable answer after {MAX_ATTEMPTS} attempts: {error}")


def _norm(text: str) -> str:
    """Case-fold and collapse whitespace, for substring quote/evidence verification."""
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _fingerprint(issue: dict[str, Any]) -> str:
    return f"{_norm(issue.get('quote', ''))}|{_norm(issue.get('problem', ''))}"


def _verify_issue(issue: dict[str, Any], draft_haystack: str, context_haystack: str, evidence_optional: bool = False) -> bool:
    """An issue is trustworthy only if its quote is really in the draft, and its evidence
    is really in the context it claims the draft contradicts. Only a tense or person
    slip may have no evidence, since it contradicts no span. The main defence against a
    model auditing its own output and inventing problems (refinement-loops.md); the
    2026-09-30 autogen showed 88 of 304 kept issues with no evidence at all."""
    quote = _norm(issue.get("quote", ""))
    if not quote or quote not in draft_haystack:
        return False
    evidence = _norm(issue.get("evidence", ""))
    if evidence == "":
        # an ending that does not conclude contradicts no span: its quote is the proof
        return evidence_optional or bool(re.search(r"\b(tense|person)\b", issue.get("problem", ""), re.I))
    return evidence in context_haystack


def _format_issues(issues: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{i['severity']}] {i['problem']} (quote: \"{i['quote']}\")" for i in issues) or "(none)"


def _has_canon_material(conn: sqlite3.Connection, story_id: int) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM lore WHERE story_id = ? LIMIT 1", (story_id,)).fetchone())


AUDIT_TEMPERATURE = 0.2       # canon_check's existing precedent for judgment calls
AUDIT_VOTE_TEMPERATURE = 0.5  # "a moderate temperature" for ensembles (refinement-loops.md)


def brief(e: Exception) -> str:
    """An exception as one readable line: a schema failure names the fields that were wrong
    instead of printing pydantic's multi-line report."""
    if isinstance(e, ValidationError):
        bad = sorted({".".join(str(x) for x in err["loc"]) + f" ({err['type'].replace('_', ' ')})" for err in e.errors()})
        return "the model's answer did not fit the schema: " + ", ".join(bad[:4]) + (" …" if len(bad) > 4 else "")
    line = (str(e).strip().splitlines() or [""])[0]
    return f"{type(e).__name__}: {line[:200]}"


def _run_auditor(role: str, draft: dict[str, Any], ctx: dict[str, Any], params: Params,
                 cfg: dict[str, Any], activity: dict[str, Any], frame: str | None = None) -> tuple[AuditReport, list[str]]:
    """One auditor, `cfg['votes']` times if more than 1 (fanned out, since votes are
    independent samples of the same prompt), keeping only issues a majority of votes
    agree on (by fingerprint). A failed vote is logged, not raised: one bad vote or one
    bad auditor call shouldn't sink the whole passage. Returns its own errors rather than
    appending to a shared list, so concurrent callers (refine's per-round fan-out) never
    touch each other's state."""
    roles = _prompts(params)
    prompt = compose(ctx, roles, role, frame=frame, draft_title=draft["title"], draft_body=draft["body"])
    votes = max(1, cfg.get("votes", 1))
    audit_params: Params = {**params, "model": cfg.get("audit_model") or params.get("model")}
    temperature = AUDIT_VOTE_TEMPERATURE if votes > 1 else AUDIT_TEMPERATURE

    def try_vote(_: int) -> tuple[AuditReport | None, str | None]:
        try:
            return _ask(audit_params, AuditReport, prompt, temperature=temperature,
                       activity=activity, reasoning=bool(cfg.get("audit_reasoning"))), None
        except Exception as e:
            return None, f"{role} on passage {activity.get('passage_id')}: {brief(e)}"

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
          activity: dict[str, Any], has_canon: bool, errors: list[str], max_tokens: int | None = None,
          ending: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """draft (validated via _ask_until) -> audit (model, per auditor) -> revise, at most
    `cfg['rounds']` revisions (refinement-loops.md; "validate before audit, always": a
    structurally invalid draft never reaches an auditor, it just regenerates). Each audit
    with a verified note is followed by a revision that acts on it; an audit with none, a
    revision that cannot be produced, or the last revision ends the loop. Leaving the loop
    is all "done" means: the draft as it stands is the passage, whatever the reason.
    Returns (draft, audit_rows): the rows are plain dicts without ids, for the caller to
    insert once it knows the passage/proposal id. Raises ValueError only when the first
    draft cannot be produced."""
    auditor_roles = [r for r in cfg["auditors"]["passage"] if r != "audit_canon" or has_canon]
    # The reviewers check the draft against the story, not against the arc the writer was steered by.
    review_ctx = {k: v for k, v in ctx.items() if k != "arc"}
    # An ending is reviewed as one, by the frame it was written in: the auditors and the reviser see what the writer saw.
    frame = compose_fields.get("frame") if ending else None
    if ending and "audit_ending" not in auditor_roles:
        auditor_roles.append("audit_ending")
    context_haystack = _norm(render_context(ctx))
    audit_rows: list[dict[str, Any]] = []
    draft = _ask_until(params, schema, lambda error: compose(ctx, roles, draft_role, steer=steer, error=error, **compose_fields),
                       draft_problem, activity, max_tokens).model_dump()
    for round_no in range(1, cfg["rounds"] + 1):
        _run().tick("rounds")
        draft_haystack = _norm(draft["title"] + " " + draft["body"])
        notes: list[dict[str, Any]] = []
        for role, (report, audit_errors) in _fan(auditor_roles, lambda role: (role, _run_auditor(role, draft, review_ctx, params, cfg, activity, frame))):
            errors.extend(audit_errors)
            verified = [d for d in (i.model_dump() for i in report.issues) if _verify_issue(d, draft_haystack, context_haystack, role == "audit_ending")]
            audit_rows.append({"step": draft_role, "round": round_no, "auditor": role, "verdict": report.verdict,
                               "issues": verified, "dropped": len(report.issues) - len(verified), "draft": draft})
            notes += verified
        if not notes:
            break
        _run().check()
        try:
            draft = _ask_until(
                params, schema,
                lambda error: compose(review_ctx, roles, revise_role, frame=frame, steer=steer, error=error,
                                      draft_title=draft["title"], draft_body=draft["body"], issues=_format_issues(notes)),
                draft_problem, activity, max_tokens).model_dump()
        except ValueError as e:
            errors.append(f"passage {activity.get('passage_id')}, revise: {e}")
            break
    return draft, audit_rows


def _fresh_choices(drafts: list[ChoiceDraft], outgoing: list[dict[str, Any]], allot: int) -> list[tuple[str, str, str]]:
    """The drafted choices worth growing, as (text, target title, intent): written out (not blank or '...'), not already on the passage, at most `allot`."""
    seen = {o["text"].strip().lower() for o in outgoing}
    kept = []
    for c in drafts:
        text, target, intent = c.text.strip(), c.target_title.strip(), (getattr(c, "intent", "") or "").strip()
        if _blank(text) or text.lower() in seen or len(kept) >= allot:
            continue
        seen.add(text.lower())
        kept.append((text, target, intent))
    return kept


def grow(state: GrowState) -> GrowState:
    """Choices out of one passage and a drafted destination for each; writes or proposes per the reviewed rule."""
    _run().check()
    pid = state["passage_id"]
    ctx = gather_context({"story_id": state["story_id"], "passage_id": pid, "params": {}})["context"]
    ctx["arc"] = arc_block(state["plan"], state["level"])
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
            state["params"], FrontierChoiceDrafts,
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

    def item(i: int, choice: tuple[str, str, str]) -> dict[str, Any]:
        return _draft_choice(state, ctx, roles, p, pid, i, choice, kept, len(kept), order, role, schema, fields, final, refine_cfg)

    # One at a time, in choice order: the run is depth-first and its log should read like a story
    results = [item(i, c) for i, c in enumerate(kept)]
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


def ending_frame(ctx: dict[str, Any], choice: str) -> str:
    """The frame of frontier_ending: no passage, choices or titles, just the story as prose up to
    where it stands and the reader's last action. A conclusion is written from the story, not from
    a node in a graph, and the gamebook frame makes models write one more decision into it."""
    s = ctx["story"]
    lines = [f"STORY: {s['title']}", s["description"] or "(no description)", ""]
    if ctx["lore"]:
        lines += ["WORLD:"] + [f"- [{l['kind']}] {l['name']}: {l['body']}" for l in ctx["lore"]] + [""]
    lines += ["THE STORY SO FAR (as the reader lived it, oldest first):", ""]
    for step in ctx.get("spine") or []:
        lines += [step["body"] or "(empty)", ""]
    lines += [ctx["passage"]["body"] or "(empty)", "", f"THE LAST THING THE READER DID: {choice}"]
    return "\n".join(lines)


def _unfinished(body: str) -> bool:
    """A body that stops mid-sentence: the model ended its reply early (seen 2026-09-30, 'The problem is')."""
    return not body.rstrip().endswith((".", "!", "?", "…", '"', "\u201d", "'", "\u2019", ")", "*", "_"))


def _pick_title(drafted: str, offered: str, ctx: dict[str, Any]) -> str:
    """The passage's title: the writer's, unless it is blank, the story's own title (the frame
    opens with it, and models copy it), or one already in the story; then the title the choices
    step offered for this destination. The database still makes the final pick unique."""
    taken = {t.strip().casefold() for t in ctx["titles"].values()} | {ctx["story"]["title"].strip().casefold()}
    for t in (drafted, offered):
        if t.strip() and t.strip().casefold() not in taken:
            return t.strip()
    return (drafted.strip() or offered.strip() or "Untitled")


def _draft_choice(state: GrowState, ctx: dict[str, Any], roles: dict[str, str], p: dict[str, Any], pid: int,
                  i: int, choice: tuple[str, str, str], kept: list[tuple[str, str, str]], n_kept: int, order: int, role: str, schema: type[BaseModel],
                  fields: dict[str, str], final: bool, refine_cfg: dict[str, Any]) -> dict[str, Any]:
    """One choice's passage: draft `role` (plain or refined per `refine_cfg`) with the
    level's pacing `fields`, then write it — directly if the parent is unreviewed, as a
    proposal if it is reviewed. A draft that could not be produced at all (exhausted draft
    retries) is caught here and reported as a skip, never raised, so `grow`'s fan-out can run every choice concurrently without one failure taking
    down its siblings."""
    text, target, _ = choice
    fields = {**fields, **flow_fields(ctx, choice, kept)}
    if final:
        fields["frame"] = ending_frame(ctx, text)  # compose() takes it as the frame, not a slot
    pos = {"pos_x": p["pos_x"] + (i - (n_kept - 1) / 2) * 220, "pos_y": p["pos_y"] + 180}
    activity = {"passage_id": pid, "phase": "passage", "choice": text}
    draft_problem = lambda d: ("the body must be written out, not empty or '...'"
                               if _placeholder_body(d.body) else
                               "the body stops mid-sentence: finish it" if _unfinished(d.body) else None)
    empty = {"child": None, "ending": None, "proposed": False, "errors": []}
    # an ending is long, and a passage can be: the reply ceiling has to allow what the plan asks for, with room for overshoot
    cap = llm.reply_cap(fields.get("length", ""), llm.ENDING_MAX_TOKENS if final else llm.MAX_TOKENS)
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
                draft_problem, activity, cap)
        except ValueError as e:
            # Skip this choice (no dangling link is written) and still grow its siblings.
            return {**empty, "errors": [f"passage {pid} ({p['title']}), choice '{text}': {e}"]}
        draft = out.model_dump()
    else:
        with db.transaction() as conn:
            has_canon = _has_canon_material(conn, state["story_id"])
        try:
            draft, audit_rows = refine(
                params=state["params"], ctx=ctx, roles=roles, cfg=refine_cfg, schema=schema,
                draft_role=role, revise_role="revise_ending" if final else "revise_passage", ending=final, steer=state["steer"],
                compose_fields={"choice": text, **fields}, draft_problem=draft_problem,
                activity=activity, has_canon=has_canon, errors=choice_errors, max_tokens=cap)
        except ValueError as e:
            # No draft was ever produced (exhausted MAX_ATTEMPTS on the first draft)
            # — skip this choice the same way the rounds: 0 path does.
            return {**empty, "errors": choice_errors + [f"passage {pid} ({p['title']}), choice '{text}': {e}"]}
    target, body = _pick_title(draft["title"], target, ctx), draft["body"].strip()
    # An ending, forced at the last level or chosen earlier by the writer, is not grown further.
    ending = final or bool(draft.get("ends_story", False))
    with db.transaction() as conn:
        if p["reviewed"]:
            payload = {"text": text, "to_passage": None, "new_target": {"title": target, "body": body, "is_ending": ending, **pos}}
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
            (state["story_id"], db.unique_title(conn, state["story_id"], target), body, pos["pos_x"], pos["pos_y"], p["t"], "{}", int(ending)),
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
    """Push the grown passage's children so the first choice is explored next, the rest after its storyline."""
    level = state["level"]
    children = [c for g in state["grown"] if g["level"] == level and g["parent"] in state["frontier"] for c in g["children"]]
    stack = list(state["stack"]) + [{"pid": c, "level": level + 1} for c in reversed(children)]
    _run().report(level=level, created=_created(state), frontier=len(stack), endings=_endings(state), open=len(stack))
    return {"stack": stack}


def after_gather(state: FrontierState) -> Literal["review", "finish"]:
    if state.get("stop") or not state["stack"] or time.time() > state["deadline"]:
        return "finish"
    return "review"


def review(state: FrontierState) -> FrontierState:
    """Pause when a storyline has just closed (the passage grown had no children), unless the
    skip_review_interrupt policy says not to. Resumed with {"steer": str, "stop": bool}."""
    level = state["level"]
    closed = not any(g["level"] == level and g["parent"] in state["frontier"] and g["children"] for g in state["grown"])
    if not closed or (state.get("policy") or {}).get("skip_review_interrupt"):
        return {}
    answer = interrupt({"level": level, "created": _created(state), "next": len(state["stack"])}) or {}
    steer = answer.get("steer")
    return {"steer": steer if steer is not None else state.get("steer", ""), "stop": bool(answer.get("stop"))}


def after_review(state: FrontierState) -> Literal["plan", "finish"]:
    return "finish" if state.get("stop") else "plan"


def finish(state: FrontierState) -> FrontierState:
    _run().report(level=state["level"], created=_created(state), frontier=0, endings=_endings(state), open=0)
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
    # The plan spans the whole story, so a root part-way down starts part-way through it.
    with db.transaction() as conn:
        start = conn.execute("SELECT start_passage_id FROM stories WHERE id = ?", (story_id,)).fetchone()
        choices = [dict(c) for c in conn.execute("SELECT from_passage, to_passage FROM choices WHERE story_id = ?", (story_id,))]
    base = engine.depth_from_start(scope[0], start["start_passage_id"] if start else None, choices)
    if base >= budget["depth"]:
        raise ValueError(f"the passage is {base} choices from the start, at or past the plan's {budget['depth']} levels: nothing left to write")
    return {"story_id": story_id, "root": scope[0], "budget": budget, "deadline": time.time() + budget["max_seconds"],
            "params": params, "base": base, "level": base, "stack": [{"pid": scope[0], "level": base}], "steer": params.get("steer") or "", "stop": False}


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
         "doc": "Takes the next passage off the stack (depth-first) and allots it its act's branching."},
        {"id": "choices", "label": "choices", "inside": "grow", "roles": ["frontier_choices"],
         "reads": ["branching", "guidance"], "doc": "Choices out of a frontier passage, paced by the parent's act."},
        {"id": "passage", "label": "passage", "inside": "grow", "roles": ["frontier_passage"],
         "reads": ["guidance", "length", "ends_story"], "when": "in an act",
         "doc": "The passage each choice leads to; may end the storyline early where the act allows it."},
        {"id": "ending", "label": "ending", "inside": "grow", "roles": ["frontier_ending"],
         "reads": ["ending.guidance"], "when": "in the Resolution",
         "doc": "At the level past the last act every new passage is an ending."},
        {"id": "continuity", "label": "continuity audit", "inside": "grow", "roles": ["audit_continuity"],
         "reads": ["policy.refine"], "when": "refine on", "doc": "Checks the draft against the story so far."},
        {"id": "canon", "label": "canon audit", "inside": "grow", "roles": ["audit_canon"],
         "reads": ["policy.refine"], "when": "refine on, story has lore",
         "doc": "Checks the draft against WORLD."},
        {"id": "revise", "label": "revise", "inside": "grow", "roles": ["revise_passage"],
         "reads": ["policy.refine"], "when": "an audit verified issues", "doc": "Fixes the verified issues, nothing else."},
        {"id": "ending_audit", "label": "ending audit", "inside": "grow", "roles": ["audit_ending"],
         "reads": ["policy.refine"], "when": "refine on, in the Resolution",
         "doc": "Checks the draft as a conclusion: resolves, no decision made for the reader, no new names, no retelling."},
        {"id": "revise_ending", "label": "revise ending", "inside": "grow", "roles": ["revise_ending"],
         "reads": ["policy.refine"], "when": "an ending audit verified issues",
         "doc": "Fixes the verified issues and keeps the ending's length and closing moment."},
        {"id": "gather", "label": "gather", "reads": ["depth", "max_seconds"],
         "doc": "Pushes the new passages onto the stack; finishes when it is empty or at the time guard."},
        {"id": "review", "label": "review", "reads": ["steer"], "when": "not unattended",
         "doc": "Pauses when a storyline closes, for the author to steer or stop."},
    ],
    "edges": [["plan", "choices"], ["choices", "passage"], ["choices", "ending"],
              ["passage", "continuity"], ["passage", "canon"], ["ending", "continuity"], ["ending", "canon"],
              ["ending", "ending_audit"], ["ending_audit", "revise_ending"], ["revise_ending", "gather"],
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
    graphs = {"verb": VERB_GRAPH.get_graph().to_json(), "interview": INTERVIEW_GRAPH.get_graph().to_json(),
              **{name: build().compile().get_graph().to_json() for name, (build, _) in WORKFLOWS.items()}}
    for name, g in graphs.items():
        for n in g["nodes"]:
            n["prompts"] = NODE_PROMPTS.get((name, n["id"]), [])
        loop = FRONTIER_LOOP if name == "expand_frontier" else _graph_loop(g, name)
        g["loop"] = {"steps": [{"roles": [], "reads": [], **s} for s in loop["steps"]], "edges": loop["edges"]}
    return graphs
