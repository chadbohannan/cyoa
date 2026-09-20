# The Webapp UI: Create, Edit, Play

This page scopes the local single-user web UI for making and playing stories. It is a design target, not a description of shipped code: as of 2026-09-19 the [frontend](../systems/frontend.md) only has a story picker and a read-only canvas. The patterns come from the [editor UX survey](../syntheses/editor-ux-survey.md); the data they act on is the [story model](story-model.md).

## Three surfaces

The app has one shell — a top bar with the story title, a story switcher, and Edit / Play tabs — and three surfaces under it.

**Library.** The landing screen lists stories as cards (title, description, passage count, last edited) with New, Rename, Delete, and later Import/Export. It is the only screen that is not story-scoped. Creating a story lands you on a map with one start passage already created.

**Map (edit).** The React Flow canvas already in `StoryCanvas.tsx`, made editable. Passage cards show the title and the first two lines of body; the start passage gets a distinct border; a passage with no outgoing choices gets an "ending" badge. Choices are labelled edges. Interactions, in priority order:

- Drag cards; positions persist (`PATCH /passages/{id}` with `pos_x`/`pos_y`, debounced).
- Double-click the canvas to create a passage at that point; double-click a card to select it in the inspector.
- Drag from a card's handle to another card to create a choice; drag to empty canvas to create a choice *and* a new target passage.
- Click an edge to select the choice in the inspector.
- Context menu: set as start, duplicate, delete (confirm when the passage has inbound choices), play from here.
- A search box that highlights matching cards, and a "problems" toggle that highlights unreachable passages and dangling choices (`to_passage IS NULL`), both fed by the backend's validation endpoint.

**Inspector (edit).** A right-hand panel, not a modal, so the map stays visible. It shows whichever thing is selected. For a passage: title, body (plain textarea), the list of its outgoing choices with inline reorder, and the LLM toolbar. For a choice: text, target (a dropdown of passages), and condition and effects as clause rows — one row per clause (`variable`, `operator`, `value`), with variables drawn from the story's table. For the story: title, description, and the variables table (name, kind, default). Everything autosaves on blur or after a typing pause; there is no Save button.

**Lore (edit).** A plain list-and-form surface for the story's bible and timeline: lore entries by kind and events ordered by story time. It is human-first because it is the baseline that [agentic authoring](agentic-authoring.md) later coordinates every generated storyline around; the passage inspector gains `t`, location and cast fields that reference it.

**Player (play).** Full-width reader view: passage title and body, choice buttons below, Back, Restart, and "play from here" entered from the map. A collapsible debug drawer shows the live variable state, the effects applied by the last choice, and the path taken so far as a list of passage titles; clicking an entry jumps back to that point.

## The backend owns the semantics

Conditions and effects are evaluated in exactly one place: a Python module in the [backend](../systems/backend.md) beside the schema that defines them. The player is stateless from the server's point of view:

- `POST /stories/{id}/play/start` (optional `passage_id`) → `{passage, choices, state}` with `state` initialized from the variables table and `choices` already filtered by their conditions.
- `POST /stories/{id}/play/choose` with `{choice_id, state}` → applies the choice's effects, returns the next `{passage, choices, state}`.

The frontend keeps a stack of these responses; Back pops it, Restart clears it. Validation (`GET /stories/{id}/problems` → unreachable passages, dangling choices, undeclared variables, ending count), a future random-playthrough tester, Twee export, and LLM prompt construction all reuse the same evaluator. The first vocabulary, drawn from what [interchange formats](interchange-formats.md) found in ink, is: conditions `{"var": "brave", "op": "==", "value": true}` combinable with `{"all": [...]}` / `{"any": [...]}`; effects `{"set": {"brave": true}, "add": {"gold": -5}, "give": ["lamp"], "take": ["lamp"]}`. Items are counts (per the [story model](story-model.md)), so `give` increments by one, `take` decrements and floors at zero, and "has a lamp" is `{"var": "lamp", "op": ">", "value": 0}`; `set`/`add` on an item variable are allowed and write the count directly. Operators are `==`, `!=`, `<`, `<=`, `>`, `>=`. A condition naming an undeclared variable is false; an effect naming one is ignored, and both are what `problems` reports. `play/choose` re-evaluates the chosen choice's condition against the submitted `state` and answers 409 if it fails, so the server never trusts the client's filtering; a choice whose `to_passage` is null answers 409 as well, since the map already shows it as dangling. The column comments in `db.py` and the `set_flag` effect in `tests/test_api.py` predate this vocabulary and are replaced when the evaluator lands.

## Schema and storage changes

All made before phase 1, because there are no migrations and nothing should be built on the old shape:

- `stories.start_passage_id` (nullable FK, `ON DELETE SET NULL`) replaces `passages.is_start`. One field, no uniqueness invariant to enforce; "set as start" is a story patch.
- `stories.ifid` (uuid4 on create), per [interchange formats](interchange-formats.md).
- `stories.updated_at`, bumped by every write to the story or its passages, choices, variables, lore and events, because the Library card shows "last edited" and nothing records it today.
- The world-layer columns and tables from [agentic authoring](agentic-authoring.md) — `passages.t`, `meta`, `reviewed`, `provenance`, and the `lore` and `events` tables — for the same no-migrations reason.
- WAL mode and a busy timeout in `connect()`, already in place — the [backend](../systems/backend.md) page has the detail — so a background agent run writing while the map reads does not surface as `database is locked`.

## LLM assistance

The assistant is a set of verbs on the selected passage, not a chat window: *Draft body* (from the title, story description, and the passages that link here), *Suggest choices* (N choice texts the author accepts individually, each optionally creating a stub target), *Draft target* on a dangling choice, and *Rewrite* with a tone preset. Each is a backend endpoint (`POST /stories/{id}/passages/{pid}/assist` with a `verb`) that builds the prompt from the story description, the variables table, and the neighboring passages, then calls the existing Ollama client. Results return as proposals; the inspector shows Accept / Regenerate / Discard and only Accept writes. A model picker in the top bar reads `GET /api/llm/models`.

These verbs are the targeted half of [agentic authoring](agentic-authoring.md), which moves them onto LangGraph, persists their proposals in a table so the map can overlay them, and grows from there toward autonomous fan-out runs.

## Phasing

The single phase list lives in [agentic authoring](agentic-authoring.md), because the later phases reshape the assistant described above; this page's surfaces are phases 1 and 2 there, with the four verbs and the health tooling following.

## Reference

| Surface | Reads | Writes |
| --- | --- | --- |
| Library | `GET /stories` | `POST /stories`, `PATCH /stories/{id}`, `DELETE /stories/{id}` |
| Map + Inspector | `GET /stories/{id}/graph`, `GET /stories/{id}/problems`, `GET /llm/models` | passage / choice / variable `POST`/`PATCH`/`DELETE`, `POST .../assist` |
| Player | `POST .../play/start`, `POST .../play/choose` | nothing |
