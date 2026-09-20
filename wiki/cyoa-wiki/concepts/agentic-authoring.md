# Agentic Authoring

This page records how LLM agents will act on the [story model](story-model.md): first as targeted verbs on a single passage, later as a fan-out that grows an exponential number of storylines, with a coherence layer that keeps every storyline true to one world. It is a design target layered on [the webapp UI](webapp-ui.md), which is built human-first; nothing here is implemented as of 2026-09-19. The intent comes from the owner's 2026-09-19 statement (`sources/20260919-agentic-intent.md`): targeted assistance first, then "very large numbers of agents fanning out", with storyline merging deliberately deferred and agents helping "coordinate storylines around a common baseline of world events".

The page is written in three tiers — decisions, intended direction, open questions — so that guesses are not mistaken for commitments.

## Decisions

**Two kinds of graph.** The story graph is data: passages and choices in SQLite. An agent workflow is a procedure — a LangGraph `StateGraph` — that reads that data and proposes changes to it. Passages are never LangGraph nodes. This gives the [frontend](../systems/frontend.md) two distinct React Flow surfaces (the story map and, later, an Agents view of workflow topology) that should look like one app but never be confused for each other.

**Agents touch the story only through a tool layer.** Tools wrap the [backend](../systems/backend.md): reading passages and neighborhoods, the variables table, the `problems` validator and the play evaluator that [the webapp UI](webapp-ui.md) puts in one Python module. The backend stays the sole owner of condition/effects semantics even when the caller is a model. The only mutating tools are `propose_*`, and — beyond the frontier, below — direct passage creation.

**Reviewed versus frontier.** The human gate in the webapp plan (proposals; only Accept writes) cannot survive a fan-out that produces tens of thousands of passages, and a fan-out with no gate cannot be trusted near human-authored text. The resolution is two independent facts on every passage: `reviewed` (0/1) and `provenance` (`'human'` | `'agent'`). The rule is then one sentence: *agents may write directly where `reviewed = 0`; they must propose where `reviewed = 1`.* Review becomes promotion — flipping `reviewed` on a subtree or a sample — rather than per-passage acceptance. Lore and events carry `reviewed` too, because the world baseline is equally something an agent might extend but must not silently rewrite.

**The story clock.** Storylines are coordinated around shared *time and events*, not shared passages. Each passage may carry a story-time value `t` and, in a `meta` JSON column, its `location`, the characters `present`, and the `events` it depicts. A separate `events` table holds the world's timeline — what happens at each `t` regardless of which branch the reader took — and a `lore` table holds the bible: characters, places, factions, rules. Generation prompts include the lore and events relevant to a passage's `t`, location and cast, which is what keeps the universe consistent across branches *without* requiring branches to converge. This is why merging can be deferred: coherence comes from the clock, not from the graph's shape. `t` is a real column rather than a `meta` key because the timeline, lore slicing and any future merge-candidate search all filter on it, and SQLite cannot index inside JSON.

**Schema delta before phase 1.** Because there are no migrations, these land alongside `start_passage_id` and `ifid`:

```
passages  + t REAL NULL
          + meta TEXT NOT NULL DEFAULT '{}'      -- {"location", "present": [...], "events": [ids]}
          + reviewed INTEGER NOT NULL DEFAULT 0
          + provenance TEXT NOT NULL DEFAULT 'human'
lore      id, story_id, kind ('character'|'place'|'faction'|'rule'), name, body, reviewed
events    id, story_id, t REAL NOT NULL, name, body, reviewed
```

Everything agent-specific (`proposals`, `agent_runs`, the checkpoint store) is additive and waits for the assistant phase.

## Intended direction

**Targeted verbs on LangGraph.** Phase 3 opens with a spike, not a verb: `ChatOllama(...).with_structured_output(schema)` against the default model on one real passage, because every later phase assumes a local model can return a parseable proposal and that has never been checked. If it fails, the `generate` node emits JSON in text and `parse_validate` retries with the parse error appended, decided before the verbs are built on it rather than discovered inside them. The four verbs in [the webapp UI](webapp-ui.md) — draft body, suggest choices, draft target, rewrite — become small explicit graphs (`gather_context → generate → parse_validate → propose`) run synchronously through the same tool layer, with `ChatOllama` from `langchain-ollama` replacing the hand-rolled client in `backend/src/cyoa/llm.py`. Running even single-shot verbs through LangGraph is what lets them be reused as subgraphs of the fan-out and rendered in the Agents view. Workflows are *explicit* graphs with a structured-output `generate` node, not ReAct agents that pick their own tools, both because a local model's tool-calling is unproven and because an explicit graph is the only kind whose picture tells the author anything.

**Proposals as a table.** `proposals(id, story_id, run_id NULL, kind, target_passage NULL, target_choice NULL, payload JSON, status)` with kinds `new_passage`, `new_choice`, `edit_passage`, `edit_choice`, `new_variable`, `note`. Pending proposals render on the map as ghost nodes and dashed edges; the inspector shows a diff with Accept / Reject; Accept is one backend function that materializes rows and resolves references between sibling proposals. Targeted verbs and autonomous runs both write here, so there is one review UI, not two.

**Runs.** `agent_runs(id, story_id, workflow, thread_id UNIQUE, params JSON, scope JSON, status, budget JSON)` where `thread_id` is the LangGraph checkpoint key. A run is started from a map selection (`POST /agents/runs`), executed as a background task streaming events over SSE, and gated with `interrupt()` at review nodes; the author resumes with `Command(resume={"accepted": [...], "rejected": [...], "steer": "..."})`. The `steer` text is the only conversational channel — scoped to a run, merged into the next prompts — which keeps the webapp plan's "verbs, not a chat window" stance. Checkpoints live in a separate `cyoa-agent.db` (`AsyncSqliteSaver`, package `langgraph-checkpoint-sqlite`) so that deleting `cyoa.db` still resets the story store per [the dev loop](../runbooks/dev-loop.md) without entangling LangGraph's opaque blobs.

**Workflow order.** `canon_check(scope)` first: walks passages, compares them to lore and events at their `t`, files `note` and `edit_passage` proposals; read-mostly, cheap, high trust. Then `fill_dangling`, driven by the `problems` endpoint. Then `expand_frontier(root, depth, max_passages)`: LangGraph's `Send` API maps one subgraph per frontier passage, `max_concurrency` throttles to what the local Ollama can serve, and a `check_budget` conditional edge — not the prompt — enforces `max_passages` and a wall-clock cap. Logically the fan-out is thousands of agents; physically it is a queue in front of one 27B model, and the budget is the real governor.

**UI.** The map gains three things: the proposal overlay, an activity highlight on passages a run is currently writing, and multi-selection as the way to scope a run. A bottom run dock lists runs with progress against budget, Pause / Resume / Cancel, and "Review N proposals". The map must be built expecting subtree collapse (a collapsed subtree shows count, depth and reviewed/agent mix) because a frontier of tens of thousands of nodes cannot be drawn. A read-only Agents tab renders each workflow from `compiled.get_graph().to_json()` — verified to return `{"nodes", "edges"}` — highlights the current node during a run, and shows `get_state()`; `get_state_history()` gives a step timeline and, later, time-travel forks for free.

**Phasing.** This is the one phase list for the project; [the webapp UI](webapp-ui.md) specifies the surfaces that phases 1 and 2 build. (1) Schema and storage changes listed there and above, then the editable map and inspectors, variables CRUD, and a Lore surface. (2) Evaluator, `play/start` and `play/choose`, the reader view, clause-row editors, and the `problems` endpoint with map highlighting, pulled forward because every autonomous workflow feeds on it. (3) The structured-output spike, then targeted verbs on LangGraph, the proposals table and overlay, model picker. (4) `canon_check`. (5) `expand_frontier`, run dock, subtree collapse, Agents view. (6) Promotion tooling by subtree, merge candidates, endings consolidation, random-playthrough tester and Twee export.

## Open questions

Whether a **timeline view** (x = `t`, y = storyline lanes, events as bands) is worth a third surface is a guess; the clock makes it possible, and the map's failure to show drift between storylines is what would justify it. Whether `lore` needs subtypes beyond `kind` is unknown until the bible is written. Whether the local model (`bonsai27b` by default) can do structured output reliably enough for `canon_check` to catch real contradictions has not been tested. **Merging** storylines toward a handful of endings is explicitly deferred by the owner; when it arrives, two passages sharing `t`, location and events are the natural merge candidates, and condition-gated text over the variables table is the natural way to paper over their differences.

| Reference | Role |
|-----------|------|
| `sources/20260919-agentic-intent.md` | the owner's intent and the verified LangGraph facts |
| `backend/src/cyoa/db.py` | where the schema delta lands |
| `backend/src/cyoa/llm.py` | the client `ChatOllama` replaces |
| `frontend/src/StoryCanvas.tsx` | the map that gains overlay, activity and collapse |
