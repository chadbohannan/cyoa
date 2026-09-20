# Wiki Log

Chronological, append-only record of wiki operations. One line per operation:

```
[YYYY-MM-DD] operation | Subject — brief description of what happened
```

[2026-09-19] update | Wiki bootstrap — initialized the wiki skeleton (CLAUDE.md operating rules, README, index, log, category directories, lint script, Obsidian vault config) modeled on the meeseeks and landlords wikis; the repo has no code yet, so no content pages exist
[2026-09-19] ingest | [Bootstrap interview](sources/20260919-bootstrap-interview.md) — recorded the owner's answers on purpose, stack, storage (SQLite), LLM (local Ollama), first UI (React Flow graph editor), and story-model needs (flags + stats/items); created systems/backend.md, systems/frontend.md, concepts/story-model.md, runbooks/dev-loop.md alongside the scaffolded backend/ + frontend/ code; updated index.md
[2026-09-19] ingest | [Community research](sources/20260919-community-research.md) — web survey of the free CYOA/IF ecosystem: story corpora and their licenses, Twee 3 / ink / ChoiceScript / Treaty of Babel formats, CYOA graph-structure research; created syntheses/community-landscape.md and concepts/interchange-formats.md (recommends adding an `ifid` column); updated index.md
[2026-09-19] ingest | [Free-text follow-up](sources/20260919-free-text-followup.md) — corrected the landscape synthesis: Consider the Consequences! (1930) is public domain and The Intercept (ink) is MIT; both noted as import-fixture candidates
[2026-09-19] ingest | [Editor UX research](sources/20260919-editor-ux-research.md) — surveyed Twine 2, Inky, Arcweave, Yarn Spinner, CSIDE and LLM writing tools for create/edit/play UI patterns; created syntheses/editor-ux-survey.md and concepts/webapp-ui.md (scoped surfaces, evaluator, LLM verbs, backend gaps, phasing); updated index.md
[2026-09-19] update | Webapp UI scope — revised concepts/webapp-ui.md: single Python evaluator behind stateless play/start + play/choose endpoints, stories.start_passage_id replaces passages.is_start, clause-row editors only, ifid folded into phase 1
[2026-09-19] ingest | [Agentic intent](sources/20260919-agentic-intent.md) — captured the owner's human-first → targeted agents → exponential fan-out intent with deferred merging and a world-events baseline; created concepts/agentic-authoring.md (decisions / direction / open questions); amended story-model.md (world layer), webapp-ui.md (schema delta, Lore surface, phasing pointer); updated index.md
[2026-09-19] update | Plan review — webapp-ui.md: added `stories.updated_at` and WAL/busy_timeout to the pre-phase-1 changes, defined item semantics for `give`/`take`, operators, undeclared-variable behaviour and `play/choose` 409 cases; phasing consolidated into agentic-authoring.md, which gains the structured-output spike at the head of phase 3; index descriptions updated
[2026-09-19] sync | Backend — `db.connect()` now sets WAL and a 5s timeout; backend.md updated, webapp-ui.md storage item marked done
