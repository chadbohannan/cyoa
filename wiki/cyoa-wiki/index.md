# Wiki Index

The structured routing layer for the wiki. Each entry links to a page with a one-line description. This file is updated on every ingest.

## Systems

- [Backend](systems/backend.md) — the uv-managed FastAPI app: SQLite story store (`CYOA_DB`) with the phase-1 schema (`start_passage_id`, `ifid`, `updated_at`, world layer, lore; the events table removed 2026-09-29), uniform CRUD routes for every table via shared helpers, the `engine.py` condition/effects evaluator behind the stateless `play/start` + `play/choose` routes, the `problems` validator (incl. merge candidates), subtree promotion, passage merge, the random-playtest report and the Twee 3 export (`twee.py`), the `agents.py` LangGraph verb, `canon_check` and `expand_frontier` workflows writing proposals or agent passages, the `runs.py` manager (threads, SQLite checkpoints, SSE events, resume/cancel) behind the run routes, and the `ChatOllama` client with reasoning off behind one process-wide call gate; port 8765 by default
- [Frontend](systems/frontend.md) — the Vite/React/TypeScript app: typed `api.ts` gateway, the per-story `useStory` store, autosaving fields, and the phase-1/2 surfaces — Library, editable React Flow map with search and problems overlay, inspector with clause-row condition/effects editors and variables, the Lore view (premise and bible; its timeline removed 2026-09-29), the Player with debug drawer and play-from-here, the assist toolbar, proposal ghost overlay, review panel and model picker, multi-select scoping, the run dock with activity highlights and subtree collapse, promote/merge on the selection and context menu, the random-playtest panel, the Twee download link, and the Agents view, reworked 2026-09-29 into the act-pacing editor (plan timeline above the per-level loop, prompt editors per step)

## Components

*(none yet)*

## Concepts

- [Story Model](concepts/story-model.md) — passages, choices, and variables as a digraph in SQLite; JSON `condition`/`effects` on choices as the mechanism for an evolving vocabulary; what is deliberately not modeled yet
- [Interchange Formats](concepts/interchange-formats.md) — how our tables map onto Twee 3, Twison JSON and ink; why Twee is the first import/export target; the recommendation to adopt IFIDs
- [Agentic Authoring](concepts/agentic-authoring.md) — how agents act on the story: story graph vs. LangGraph workflow graph, a tool layer as the only path to the story, the reviewed/frontier rule, the story clock (`t`, `lore`; the `events` timeline removed 2026-09-29) that keeps fan-out storylines coherent without merging; proposals and runs tables, `canon_check` then `expand_frontier`, the structured-output spike; the handoff architecture enabling the vision of "human-operated but fully automatable" (manual → assisted → automated); owns the project's phase list (1–6 done, 7a–7b for automation)
- [Choice Anchoring and Narrative Coherence](concepts/choice-anchoring.md) — a proposed resolution to the tension between multi-timeline branching and narrative coherence: every choice declares what world-fact it is anchored to, creating constraints on other branches and funneling the frontier toward conclusions; scoped design (not yet built)
- [Act Pacing](concepts/act-pacing.md) — built 2026-09-29 (not yet evaluated against a model): a per-story pacing plan of an ordered act list (levels, branching, `ends_story`, length, guidance set directly) plus a fixed one-level ending whose position hard-caps depth; the climax as a one-level act and the resolution as the ending; per-act prose length; which act paces a passage and which paces its choices; `max_passages` removed; early endings guided, not abrupt; the Agents view reworked into a plan timeline linked to one per-level loop with `grow` expanded into its prompt steps
- [Activity Log](concepts/activity-log.md) — scoped follow-on to act pacing (2026-09-29, not built): the run inspector pruned to its provenance value; a persisted, curated run log (today's `runs.py` event log is in-memory only) shown as a story-level Activity tab, plus a run/act/role provenance line on each passage
- [Interview Me](concepts/interview-me.md) — 2026-09-29, backend (I1) and Lore-tab panel (I2) built, not yet tried against a live model; scoped down twice: an agent-driven interview that asks and clarifies to create or improve the premise text and the character suite; conflicts (incl. edits to reviewed characters) become questions only the author resolves; one structured call per turn, `interviews` table with transcript and conflicts, per-write revert; timeline out of scope
- [Refinement Loops](concepts/refinement-loops.md) — design (2026-09-28), R1 built and simplified 2026-09-30: every passage is drafted, then audited and revised at most `rounds` (3) times, and leaving the loop for any reason is what "done" means (no flags, no held state); auditors are prompt roles with quote-verified issues, an `audits` table logs each round, best-of-N, audit votes and reasoning auditors are optional levers, and an R0–R4 build order measured by the storyline eval
- [The Webapp UI](concepts/webapp-ui.md) — scope for the local create/edit/play app: library, editable map with inspector panel, stateless server-driven player with debug drawer, per-passage LLM verbs; the backend as sole owner of condition/effects semantics and the first condition/effects vocabulary; `start_passage_id`, `ifid`, `updated_at` and WAL storage changes

## Incidents

- [Delete Subtree Left Orphans](incidents/2026-09-29-prune-left-orphans.md) — prune reused the collapse rule, which treats every orphan as an entry point, so earlier plain deletes shielded branches from it; `engine.prune_set` now runs to the leaves protecting only what the start reaches, with a preview route and a Delete subtree button in the inspector
- [An Interview Turn That Never Answered](incidents/2026-09-29-interview-thinking-runaway.md) — gemma-4 12B on llama-server looped on its thinking-channel opener through a whole Interview Me turn; no token cap reached the server (langchain-openai sends `max_completion_tokens`, which llama-server ignores) and thinking was never switched off; both now set in `extra_body` on every call
- [Unattended Runs Stopped After One Level](incidents/2026-09-28-skip-review-stopped-after-one-level.md) — `skip_review_interrupt` routed `after_gather` to `finish`, so unattended `expand_frontier` runs grew one level regardless of depth; the skip moved into `review`, pinned by a test
- [Fan-out Exhausted the GPU](incidents/20260920-fanout-oom.md) — the first `canon_check` run sent six concurrent requests at Ollama and OOMed a 12 GB GPU; a process-wide call gate (default 1) replacing LangGraph's `max_concurrency`, which deadlocked streamed runs

## Runbooks

- [Running the Dev Loop](runbooks/dev-loop.md) — `make backend` / `make frontend`, the env vars for ports and Ollama, resetting the database, `make test`
- [Evaluating Generated Storylines](runbooks/storyline-eval.md) — `make eval`: a private backend grows seeded premises unattended and renders every root-to-leaf storyline as prose for a consistency read, with an optional model judge

## Syntheses

- [The Free CYOA Community](syntheses/community-landscape.md) — survey of free story corpora (Project Aon, itch.io, Choice of Games), the de-facto formats (Twee 3, ink, ChoiceScript, Treaty of Babel), structural research on classic books, and what is reusable here
- [Editor UX Survey](syntheses/editor-ux-survey.md) — patterns from Twine, Inky, Arcweave, Yarn Spinner, CSIDE and AI writing tools, and which ones the webapp adopts
- [Storyline Generation Quality](syntheses/storyline-generation-quality.md) — synthesis of four `expand_frontier` eval runs (2026-09-28): what code-only retries, full-path spine context and budget-by-shape (`depth`+`branching`) fixed versus their limits, a model-swap run (Qwen 3.8 27B Bonsai-2 ternary → Gemma-4 12B) isolating code-level guarantees from model-specific pacing/retry-rate/speed, a new literal-escape-sequence defect, and six concrete proposals for refinement-loops' auditors and the eval harness
