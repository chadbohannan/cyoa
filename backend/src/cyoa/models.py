"""Pydantic shapes shared by the API. Mirrors the tables in db.py.

Each table has a *Create shape (what a POST accepts), a *Patch shape (every
field optional; only the fields present are written), and the full row shape.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    is_ending: bool = False


class PassagePatch(BaseModel):
    title: str | None = None
    body: str | None = None
    pos_x: float | None = None
    pos_y: float | None = None
    t: float | None = None
    meta: dict[str, Any] | None = None
    reviewed: bool | None = None
    provenance: Provenance | None = None
    is_ending: bool | None = None


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
AssistVerb = Literal["draft_body", "suggest_choices", "draft_target", "rewrite", "suggest_next"]


class Proposal(BaseModel):
    """An agent's proposed change. Payload by kind:

    new_passage   {title, body, pos_x, pos_y}; target_choice is the dangling choice to point at it
    new_choice    {text, to_passage | null, new_target: {title, body?, pos_x?, pos_y?} | null}; target_passage is the source
    edit_passage  {body?} and/or {is_ending?}; target_passage is the passage being edited
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


class PacingAct(BaseModel):
    """One act of a pacing plan (concepts/act-pacing.md): a span of levels whose
    passages share guidance and a target length, and whose choices are capped."""

    model_config = ConfigDict(extra="forbid")

    name: str
    levels: int = Field(ge=1, le=10)
    branching: int = Field(ge=1, le=6)
    ends_story: bool = True  # whether a storyline may end early inside this act
    length: str = ""  # target prose length, as the prompt should say it ("150–250 words")
    guidance: str = ""


class PacingEnding(BaseModel):
    """The plan's fixed last level: every passage there is an ending (frontier_ending)."""

    model_config = ConfigDict(extra="forbid")

    length: str = ""
    guidance: str = ""


class PacingPlan(BaseModel):
    """A story's pacing plan; a run copies it into its budget. Depth is derived:
    the acts' levels plus one for the ending, and it is a hard cap. `steer`,
    `max_seconds` and `policy` (auto_accept_unreviewed, refine) are run-wide."""

    model_config = ConfigDict(extra="forbid")

    acts: list[PacingAct] = Field(min_length=1)
    ending: PacingEnding = Field(default_factory=PacingEnding)
    steer: str = ""
    max_seconds: int = Field(1800, ge=10)
    policy: dict[str, Any] = Field(default_factory=dict)


class RunCreate(BaseModel):
    """Start a workflow from a map selection. For expand_frontier, `budget` is a
    PacingPlan, or empty for the story's saved plan; `steer` and `policy` given here
    override the plan's. `policy` controls automation:
    auto_accept_unreviewed (bool) auto-accepts proposals on unreviewed passages,
    skip_review_interrupt (bool) skips the review pause between levels, refine (dict)
    tunes expand_frontier's draft/audit/revise loop (see agents.DEFAULT_REFINE)."""

    workflow: Workflow
    scope: list[int] = Field(default_factory=list)
    model: str | None = None
    steer: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


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
    policy: dict[str, Any]
    status: RunStatus
    progress: dict[str, Any]
    error: str | None
    pending_proposals: int
    created_at: str
    updated_at: str


class PromptInfo(BaseModel):
    """One prompt role (prompts.PROMPTS). `override` is null when the default is in use;
    `used_by` is the (workflow, node) pairs that compose it (agents.NODE_PROMPTS)."""

    key: str
    label: str
    doc: str
    placeholders: list[str]
    used_by: list[tuple[str, str]]
    default: str
    override: str | None


class PromptUpdate(BaseModel):
    text: str


class PromptPreviewRequest(BaseModel):
    """Assemble a role for one passage without calling the model; `text` previews an unsaved draft.
    `stage` fills the pacing slots as that stage of the plan would (an act's index, or
    len(acts) for the ending; omitted, the first act's), and `plan` previews an unsaved
    plan instead of the story's saved one."""

    passage_id: int
    text: str | None = None
    stage: int | None = None
    plan: PacingPlan | None = None


class PromptPreview(BaseModel):
    prompt: str


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
    """Static checks from ``engine.problems``; ids so the map can highlight them.

    ``endings`` is ``is_ending = true`` passages — a decided, terminal state.
    ``unfinished`` is passages with no outgoing choices that are *not* marked
    ``is_ending`` — a stub still awaiting either choices or that decision.
    """

    no_start: bool
    unreachable: list[int]
    dangling: list[int]
    undeclared: list[UndeclaredVar]
    endings: list[int]
    unfinished: list[int]
    merge_candidates: list[MergeCandidate]


class MergeCandidate(BaseModel):
    """Passages sharing story time, location and events — one scene reached by several routes."""

    t: float
    location: str
    events: list[str]
    passage_ids: list[int]


class PromoteRequest(BaseModel):
    """Flip ``reviewed`` on a set of passages: the subtree under ``root`` (the
    passages reachable only through it, as the map's collapse defines them),
    plus any listed explicitly."""

    root: int | None = None
    passage_ids: list[int] = Field(default_factory=list)
    reviewed: bool = True


class PromoteResult(BaseModel):
    passage_ids: list[int]


class PruneResult(BaseModel):
    """What pruning a passage deleted: its subtree's passages (not the root) and
    every choice out of the root or out of those passages."""

    passage_ids: list[int]
    choice_ids: list[int]


class MergeRequest(BaseModel):
    """Fold ``passage_ids`` into ``into``: their inbound choices are retargeted
    at the survivor, their outgoing choices move to it, and they are deleted.
    Serves both merge candidates and endings consolidation."""

    into: int
    passage_ids: list[int]


class PlaytestRequest(BaseModel):
    runs: int = Field(200, ge=1, le=10000)
    max_steps: int = Field(200, ge=1, le=10000)
    seed: int | None = None


class Playtest(BaseModel):
    """Coverage from ``engine.random_playthroughs``; counters are keyed by id.

    ``endings`` counts landings on an ``is_ending`` passage; ``unfinished`` counts
    landings on a passage with no outgoing choices that isn't marked ``is_ending``.
    """

    runs: int
    mean_length: float
    cut: int
    visited: dict[int, int]
    unvisited: list[int]
    untaken: list[int]
    endings: dict[int, int]
    unfinished: dict[int, int]
    stuck: dict[int, int]
    dangling: dict[int, int]
