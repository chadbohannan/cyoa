# Wiki Index

The structured routing layer for the wiki. Each entry links to a page with a one-line description. This file is updated on every ingest.

## Systems

- [Backend](systems/backend.md) — the uv-managed FastAPI app: SQLite story store (`CYOA_DB`) with the phase-1 schema (`start_passage_id`, `ifid`, `updated_at`, world layer, lore/events), uniform CRUD routes for every table via shared helpers, the `engine.py` condition/effects evaluator behind the stateless `play/start` + `play/choose` routes and the `problems` validator, the `agents.py` LangGraph verb, `canon_check` and `expand_frontier` workflows writing proposals or agent passages, the `runs.py` manager (threads, SQLite checkpoints, SSE events, resume/cancel) behind the run routes, and the `ChatOllama` client with reasoning off behind one process-wide call gate; port 8765 by default
- [Frontend](systems/frontend.md) — the Vite/React/TypeScript app: typed `api.ts` gateway, the per-story `useStory` store, autosaving fields, and the phase-1/2 surfaces — Library, editable React Flow map with search and problems overlay, inspector with clause-row condition/effects editors and variables, the Lore/timeline view, the Player with debug drawer and play-from-here, the assist toolbar, proposal ghost overlay, review panel and model picker, multi-select scoping, the run dock with activity highlights and subtree collapse, and the read-only Agents view

## Components

*(none yet)*

## Concepts

- [Story Model](concepts/story-model.md) — passages, choices, and variables as a digraph in SQLite; JSON `condition`/`effects` on choices as the mechanism for an evolving vocabulary; what is deliberately not modeled yet
- [Interchange Formats](concepts/interchange-formats.md) — how our tables map onto Twee 3, Twison JSON and ink; why Twee is the first import/export target; the recommendation to adopt IFIDs
- [Agentic Authoring](concepts/agentic-authoring.md) — how agents act on the story: story graph vs. LangGraph workflow graph, a tool layer as the only path to the story, the reviewed/frontier rule, the story clock (`t`, `lore`, `events`) that keeps fan-out storylines coherent without merging; proposals and runs tables, `canon_check` then `expand_frontier`, the structured-output spike; owns the project's single phase list; open questions kept separate from decisions
- [The Webapp UI](concepts/webapp-ui.md) — scope for the local create/edit/play app: library, editable map with inspector panel, stateless server-driven player with debug drawer, per-passage LLM verbs; the backend as sole owner of condition/effects semantics and the first condition/effects vocabulary; `start_passage_id`, `ifid`, `updated_at` and WAL storage changes

## Incidents

- [Fan-out Exhausted the GPU](incidents/20260920-fanout-oom.md) — the first `canon_check` run sent six concurrent requests at Ollama and OOMed a 12 GB GPU; a process-wide call gate (default 1) replacing LangGraph's `max_concurrency`, which deadlocked streamed runs

## Runbooks

- [Running the Dev Loop](runbooks/dev-loop.md) — `make backend` / `make frontend`, the env vars for ports and Ollama, resetting the database, `make test`

## Syntheses

- [The Free CYOA Community](syntheses/community-landscape.md) — survey of free story corpora (Project Aon, itch.io, Choice of Games), the de-facto formats (Twee 3, ink, ChoiceScript, Treaty of Babel), structural research on classic books, and what is reusable here
- [Editor UX Survey](syntheses/editor-ux-survey.md) — patterns from Twine, Inky, Arcweave, Yarn Spinner, CSIDE and AI writing tools, and which ones the webapp adopts
