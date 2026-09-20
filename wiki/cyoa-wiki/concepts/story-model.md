# Story Model

A story is a directed graph of **passages** joined by **choices**, with per-story **variables** that choices can test and mutate. This is the shape the 2026-09-19 bootstrap interview (`20260919-bootstrap-interview.md`) asked for: plain branching was explicitly not enough — the owner wants variables/flags and inventory/stats, Twine-style — and the model is expected to keep evolving as the tooling does. Everything here describes the schema in `backend/src/cyoa/db.py` as of 2026-09-19; the [backend](../systems/backend.md) owns the tables and the [frontend](../systems/frontend.md) renders them.

## Passages and choices

A passage is a titled block of prose (`title`, `body`) belonging to one story. The story's entry point is `stories.start_passage_id`, a nullable foreign key that clears itself if the start passage is deleted; it replaced a per-passage `is_start` flag on 2026-09-19 because one pointer needs no uniqueness invariant. Creating a story creates its start passage at the same time. Each story also carries an `ifid` (see [interchange formats](interchange-formats.md)) and an `updated_at` that every write to the story or its rows bumps. A choice is a directed edge from one passage (`from_passage`) to another (`to_passage`), with the `text` the reader sees and a `sort_order` that fixes the presentation order among a passage's choices. The graph is a general digraph, not a tree: multiple choices may converge on one passage, and cycles are allowed, since both are ordinary in gamebooks.

`to_passage` is nullable and set to null when its target is deleted. A dangling choice is kept rather than cascade-deleted so an author sees a broken link to repair instead of silently losing a choice's text and effects.

## Variables, conditions, effects

The `variables` table declares the state a story tracks: each row has a `name`, a `kind` of `flag`, `stat`, or `item`, and a JSON `default`. Flags are booleans, stats are numbers, items are counts — the three kinds the interview named, kept as one table rather than three because they differ only in the type of their value.

Choices carry two JSON blobs, `condition` and `effects`, both defaulting to `{}`. A condition gates whether the choice is offered; an effect mutates state when the choice is taken. Their vocabulary — clauses `{"var", "op", "value"}` under `all`/`any`, effects `set`/`add`/`give`/`take` — is defined in [the webapp UI](webapp-ui.md) and interpreted in exactly one place, `engine.py` in the [backend](../systems/backend.md), since 2026-09-20. Storing them as JSON text is the concrete mechanism for "the structure evolves as the tool evolves": the vocabulary can grow without a schema change. The cost is that SQLite cannot query inside them, which is acceptable because the only read path loads a whole story at once.

## The world layer

[Agentic authoring](agentic-authoring.md) adds a second axis to the model, landed on 2026-09-19 alongside the other phase-1 columns because there are no migrations. Passages carry a story-time `t`, a `meta` JSON column (`location`, `present`, `events`), and two independent facts, `reviewed` and `provenance`, that decide whether an agent may write a passage directly or must propose a change. Two small tables, `lore` (characters, places, factions, rules) and `events` (what happens at each `t` on every branch), each with its own `reviewed` bit, hold the world baseline that storylines are coordinated around; that page explains why the clock, not graph convergence, is what keeps the universe consistent.

Since phase 3 (2026-09-20) a seventh table, `proposals`, sits beside these: an agent's intended change to any of them, materialised only when a human accepts it. Its shape and rationale are on [agentic authoring](agentic-authoring.md); it is not part of the story a reader plays.

## What is not modeled yet

The evaluator, the stateless player and the `problems` validator exist as of 2026-09-20 (see the [backend](../systems/backend.md)), but there is still no export target — print/ebook export was offered in the interview and not selected. When export does arrive, [interchange formats](interchange-formats.md) records how these tables map onto Twee 3 and ink and why an `ifid` column is worth adding first. Each of these will become its own page when it exists.

| Reference | Role |
|-----------|------|
| `backend/src/cyoa/db.py` | the six tables |
| `backend/src/cyoa/models.py` | their Pydantic mirrors |
| `sources/20260919-bootstrap-interview.md` | the requirements behind the model |
