"""Pydantic shapes shared by the API. Mirrors the tables in db.py.

Each table has a *Create shape (what a POST accepts), a *Patch shape (every
field optional; only the fields present are written), and the full row shape.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

LoreKind = Literal["character", "place", "faction", "rule"]
VariableKind = Literal["flag", "stat", "item"]
Provenance = Literal["human", "agent"]


class StoryCreate(BaseModel):
    title: str
    description: str = ""


class StoryPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    start_passage_id: int | None = None


class Story(StoryCreate):
    id: int
    ifid: str
    start_passage_id: int | None
    created_at: str
    updated_at: str


class StorySummary(Story):
    """Library card: a story plus the counts the card shows."""

    passage_count: int


class PassageCreate(BaseModel):
    title: str
    body: str = ""
    pos_x: float = 0
    pos_y: float = 0
    t: float | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    reviewed: bool = False
    provenance: Provenance = "human"


class PassagePatch(BaseModel):
    title: str | None = None
    body: str | None = None
    pos_x: float | None = None
    pos_y: float | None = None
    t: float | None = None
    meta: dict[str, Any] | None = None
    reviewed: bool | None = None
    provenance: Provenance | None = None


class Passage(PassageCreate):
    id: int
    story_id: int


class ChoiceCreate(BaseModel):
    from_passage: int
    to_passage: int | None = None
    text: str
    sort_order: int = 0
    condition: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)


class ChoicePatch(BaseModel):
    to_passage: int | None = None
    text: str | None = None
    sort_order: int | None = None
    condition: dict[str, Any] | None = None
    effects: dict[str, Any] | None = None


class Choice(ChoiceCreate):
    id: int
    story_id: int


class VariableCreate(BaseModel):
    name: str
    kind: VariableKind
    default: Any = None


class VariablePatch(BaseModel):
    name: str | None = None
    kind: VariableKind | None = None
    default: Any = None


class Variable(VariableCreate):
    id: int
    story_id: int


class LoreCreate(BaseModel):
    kind: LoreKind
    name: str
    body: str = ""
    reviewed: bool = False


class LorePatch(BaseModel):
    kind: LoreKind | None = None
    name: str | None = None
    body: str | None = None
    reviewed: bool | None = None


class Lore(LoreCreate):
    id: int
    story_id: int


class EventCreate(BaseModel):
    t: float
    name: str
    body: str = ""
    reviewed: bool = False


class EventPatch(BaseModel):
    t: float | None = None
    name: str | None = None
    body: str | None = None
    reviewed: bool | None = None


class Event(EventCreate):
    id: int
    story_id: int


ProposalKind = Literal["new_passage", "new_choice", "edit_passage", "edit_choice", "new_variable", "note"]
ProposalStatus = Literal["pending", "accepted", "rejected"]
AssistVerb = Literal["draft_body", "suggest_choices", "draft_target", "rewrite"]


class Proposal(BaseModel):
    """An agent's proposed change. Payload by kind:

    new_passage   {title, body, pos_x, pos_y}; target_choice is the dangling choice to point at it
    new_choice    {text, to_passage | null, new_target: {title, body?, pos_x?, pos_y?} | null}; target_passage is the source
    edit_passage  {body}; target_passage is the passage being rewritten
    edit_choice   {text}; target_choice
    new_variable  {name, kind, default}
    note          {text, issues?: [{claim, conflicts_with, severity}]}; optional target_passage
    """

    id: int
    story_id: int
    batch: str
    verb: str
    kind: ProposalKind
    target_passage: int | None
    target_choice: int | None
    payload: dict[str, Any]
    status: ProposalStatus
    run_id: int | None = None
    created_at: str


class AssistRequest(BaseModel):
    verb: AssistVerb
    model: str | None = None
    tone: str | None = None  # rewrite only
    choice_id: int | None = None  # draft_target only: the dangling choice


# --- runs ------------------------------------------------------------------------

Workflow = Literal["canon_check", "expand_frontier"]
RunStatus = Literal["running", "paused", "done", "failed", "cancelled"]


class RunCreate(BaseModel):
    """Start a workflow from a map selection. `budget` keys default per workflow
    (expand_frontier: max_passages, depth, max_seconds)."""

    workflow: Workflow
    scope: list[int] = Field(default_factory=list)
    model: str | None = None
    steer: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)


class RunResume(BaseModel):
    """Answer to a review interrupt: new direction for the next level, or stop."""

    steer: str | None = None
    stop: bool = False


class AgentRun(BaseModel):
    id: int
    story_id: int
    workflow: Workflow
    thread_id: str
    params: dict[str, Any]
    scope: list[int]
    budget: dict[str, Any]
    status: RunStatus
    progress: dict[str, Any]
    error: str | None
    pending_proposals: int
    created_at: str
    updated_at: str


class RunState(BaseModel):
    """LangGraph's view of a run: checkpointed values, what runs next, the step history."""

    values: dict[str, Any]
    next: list[str]
    history: list[dict[str, Any]]


class StoryGraph(BaseModel):
    """Everything the graph editor needs to render one story."""

    story: Story
    passages: list[Passage]
    choices: list[Choice]
    variables: list[Variable]
    lore: list[Lore]
    events: list[Event]
    proposals: list[Proposal]  # pending only


# --- play & validation ---------------------------------------------------------

State = dict[str, Any]


class PlayStart(BaseModel):
    """Begin a playthrough at the story's start, or at ``passage_id`` ("play from here")."""

    passage_id: int | None = None


class PlayChoose(BaseModel):
    choice_id: int
    state: State


class PlayView(BaseModel):
    """One step of a playthrough: the passage, the choices open in ``state``, and ``state``."""

    passage: Passage
    choices: list[Choice]
    state: State


class UndeclaredVar(BaseModel):
    choice_id: int
    var: str


class Problems(BaseModel):
    """Static checks from ``engine.problems``; ids so the map can highlight them."""

    no_start: bool
    unreachable: list[int]
    dangling: list[int]
    undeclared: list[UndeclaredVar]
    endings: list[int]
