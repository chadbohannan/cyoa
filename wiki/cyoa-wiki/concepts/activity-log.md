# Activity Log

The Activity Log is a planned story-level feed of what agent runs did: when each run started and what it ran with, what each level produced, where the author steered, and where things went wrong. It is scoped as a follow-on to [act pacing](act-pacing.md), not part of it. On 2026-09-29 the owner agreed to prune the Agents view's run inspector down to this (`sources/20260929-act-pacing-followup.md` records the surrounding decisions). Nothing here is built yet.

## Why it replaces the run inspector

The Agents view's run panel (`RunPanel` in `frontend/src/AgentsView.tsx`, removed with act pacing on 2026-09-29) had four sections: status and progress, params, "prompts it ran with", and the raw checkpoint state with its step history. Only two carried value for an author.

- **Status and progress** repeat the RunDock row, which renders the same `describeProgress` line.
- **Raw state and step history** show LangGraph internals (`frontier`, `grown`, `next`). They matter for debugging a misbehaving run, and `GET .../runs/{id}/state` keeps serving that.
- **Params and the prompt snapshot** are the valuable part: provenance. They record which plan, model and prompt text produced a run's output. The author's workflow is to tune prompts (probably per model), run, read, and tune again. That workflow needs to know whether a passage came from today's wording or last week's.

Provenance is also organised around the wrong object. Authors judge output by reading passages, so the question they ask is "why does *this passage* read like this?" The run panel answers it only indirectly. So act pacing dropped the run panel and the runs list from the Agents view, which becomes config-only. The provenance moves here and onto the passage.

## What exists today

The raw material is already produced. In `backend/src/cyoa/runs.py`, `Manager._push` appends every event to an in-memory log per run, the `Live.events` list, and stamps each event with `seq` and `at`. The SSE route `GET /api/stories/{id}/runs/{run_id}/events` replays and tails that log. The event types are `RunEvent` in `frontend/src/api.ts`:

- `node`: a node finished.
- `activity`: a passage is in phase `check`, `choices`, `passage` or `done`.
- `progress`: the whole progress dict, from `RunHandle.report` and `tick` in `agents.py`.
- `interrupt`: the review pause, with `level`, `created` and `next`.
- `status`: the run's status changed.

`frontend/src/useRuns.ts` consumes the stream only for side effects:
- it pulses passage cards on the map,
- it patches the run row's progress,
- it reloads the graph after `grow`.

The log itself is never shown. Its docstring also notes it is not persisted: "a restart loses the event history, not the run."

## Design

- **A persisted, curated log.** Meaningful events go to a table in `cyoa.db`. The table cannot be named `events`, which is the story timeline from [agentic authoring](agentic-authoring.md). Candidates are `run_log` or `activity`. The persisted events are:
  - run start, with the run's copied plan, model and prompts (these already live on `agent_runs.params` and `budget`, so a reference will do),
  - one summary per level,
  - each passage created, including early endings,
  - each review pause, and the steer given on resume (today this sits only in checkpoint state, yet it shapes every later passage),
  - each unresolved audit,
  - errors, cancel and finish.

  The `activity` pulses and `node` finishes stay in memory. They exist for the live map.
- **Provenance on the passage.** When `grow` inserts a passage, it already writes `meta` (the `{"audit": ...}` of [refinement loops](refinement-loops.md)). It would also record the run id, the act, and the prompt role that drafted the passage. The passage Inspector then shows "written by run #12 · Confrontation · `frontier_passage`", next to its `AuditFlag`, and links to that run in the log. The act is only known once act pacing lands, so the act field waits for it.
- **The feed.** It is an Activity tab beside Map / Lore / Play / Agents, grouped by run, newest first:
  - a run header, with a "ran with" disclosure listing the plan summary, model and prompt text,
  - lines per level showing act, passages, endings and retries,
  - review lines with the steer given,
  - passage lines that link to the map.

  A tab rather than a RunDock expansion, because it spans every run of the story and keeps the dock compact. The dock's "workflow" link opened the Agents view on a run until act pacing removed it; a link in its place would open the run's section of the feed.

## Open questions

- **How far back?** Keep the log for as long as the run rows last, or prune it after N runs.
- **The eval harness.** `scripts/eval_storylines.py` writes its own per-run reports. It could read this table instead of rebuilding the same story from `audits` and the graph.

| Reference | What it anchors |
|---|---|
| `backend/src/cyoa/runs.py` (`Live`, `Manager._push`, `Manager.events`) | the in-memory, unpersisted event log |
| `backend/src/cyoa/agents.py` (`RunHandle.emit`/`report`/`tick`, `grow`'s passage insert) | where events and passage `meta` are produced |
| `backend/src/cyoa/main.py` (`run_events`) | the SSE replay/tail route |
| `frontend/src/api.ts` (`RunEvent`), `frontend/src/useRuns.ts` | the event types and their only consumer today |
| `frontend/src/AgentsView.tsx` (`RunPanel`, removed 2026-09-29) | the run inspector this replaces |
