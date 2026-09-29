# Act Pacing

Act pacing makes story structure a first-class, mechanical control over `expand_frontier`. A story owns a pacing plan: a list of acts, followed by a fixed ending. Every run copies the plan and paces itself level by level from it, and the Agents view is the plan's editor. The requirements were scoped with the owner on 2026-09-29, in an interview (`sources/20260929-act-pacing-interview.md`) and three follow-up corrections (`sources/20260929-act-pacing-followup.md`). It was built the same day. This page owns the plan's shape, its defaults and the pacing rules; the [backend](../systems/backend.md) and [frontend](../systems/frontend.md) pages describe the code around it.

## Why acts

Before this, the only pacing was one countdown. `grow` passed `{remaining}` (levels left in the storyline) into the frontier prompts, and the last level switched to the `frontier_ending` role. [Storyline generation quality](../syntheses/storyline-generation-quality.md) shows what that achieved.

- **What works:** every storyline ends, so the forced ending holds up as structure.
- **What doesn't:** the softer "pace toward resolution" pressure varied by model. Qwen/Bonsai trees shrank to 10/7/8 of 39 passages, while Gemma-4 filled 21/24/17.
- **Only one shape:** a story could not open up in the middle and narrow toward a climax.
- **Passages are short:** no prompt asked for a length. Worse, the `PassageDraft` schema told the model "one to three short paragraphs" on every call. Each passage read like a short chapter with choices at the seams.

## The pacing plan

`models.PacingPlan` is the shape, and `agents.DEFAULT_PLAN` is what a story uses until its author saves one. It is kept in its own `pacing` table (`story_id`, `plan` JSON) rather than a `stories` column, so an existing `cyoa.db` picks it up without being deleted. The plan has three parts.

- **acts**: an ordered list of at least one. Each act has:
  - `name`,
  - `levels`, how many levels of the storyline it spans (1–10), set directly with no fractions of a total,
  - `branching`, the most choices out of a passage in this act (1–6),
  - `ends_story`, whether a storyline may end early inside this act (default true),
  - `length`, the target prose length as the prompt should say it (e.g. "200–300 words"),
  - `guidance`, free text injected into the frontier prompts.
- **ending**: exactly one level after the last act, with its own `length` and `guidance`. Every passage at this level is an ending written with `frontier_ending`.
- **run-wide settings**: `steer`, `max_seconds` (the time guard) and `policy` (`auto_accept_unreviewed`, `refine`). Unattended (`skip_review_interrupt`) stays a per-run choice in the run dock.

The default plan is Setup, Confrontation and Climax, one level each, with branching 2, 3 and 2, then the Resolution ending. Depth is therefore 4, and the tree holds at most 42 passages.

Depth is derived: the sum of the acts' `levels`, plus one for the ending (`plan_depth`). That sum is a hard cap. The owner wants it enforced: "i do want the story depth to be hard-capped, thus 'forced' is a nessessary concept."

What forces an ending is the end of the plan. No per-act switch does. Per-act "force endings at this act's last level" toggles were considered and dropped. A forced ending mid-plan would end every storyline, leaving the later acts unreachable.

`max_passages` was removed entirely: from the budget, `plan`, `after_gather`, `api.ts`, `runs.ts` and the eval harness, which takes `--plan` instead of `--depth`/`--branching`/`--max-passages`. Levels and branching already bound the tree. A flat passage cap is what produced run 2's unended storylines. A run request that still sends `depth`, `branching` or `max_passages` is a 422, not silently ignored.

### Which act a level belongs to

`stage_at(plan, level)` maps a level to its stage. Level 0 is the run root's children, and the level after the last act is the ending. Two rules decide which act paces a step:

- **A passage** is paced by the stage of the level it is written at. Its `length` and `guidance` fill the prompt, and so does `{act_remaining}`, the levels left in that act counting this one.
- **The choices out of a passage** are paced by that passage's own act, and so is `branching`. The run's root counts as the first act. So the Climax's choices are the decisive ones, each leading to its own resolution. Here `{act_remaining}` counts the levels of the act still to come after the passage, so 0 means these choices lead out of it.

An act with `ends_story: false` drafts its passages without the `ends_story` field (`FrontierDraft` rather than `FrontierPassageDraft`), so the writer cannot end a storyline there. The prompt says "If you are given an ends_story field", so a single role text serves both kinds of act.

### Why the resolution is the ending, not a fourth act

The owner asked whether this is really four acts, the last two one chapter each: a climax, then "longer prose that finds elegant resolution after a climax". The climax is an ordinary act with `levels: 1`. Its guidance calls for the decisive moment, and its choices are the decisive choices. Each climax choice leads to its own resolution, so the number of full resolutions is the number of climax passages times the climax's `branching`.

The resolution is modelled as the plan's fixed `ending`, not as a fourth act. As an act it would need three rules: it must be last, it must be one level, and it must have no choices. An act is a span of levels that offer choices, and a resolution is neither. The timeline still draws it as a fourth stage, so the shape the owner described is what they see:

```
 Premise │ Setup  L1 L2 │ Confrontation L3 L4 L5 │ Climax L6 │ Resolution L7
         │ ≤2 choices   │ ≤3 choices             │ ≤3        │ no choices, long
                                                   decisive     climax → elegant
                                                   choices      resolution
```

## Early endings

The owner wants storylines that end early because the reader chose badly, such as death, capture, or a door that closes for good. These are encouraged, not suppressed. Each act's `ends_story` defaults to allowed, and the Confrontation's default guidance invites endings that follow from a poor choice. They are drafted with `frontier_passage` at the act's `length` and guidance.

They are not made abrupt on purpose. How an early ending lands is up to the author, through each act's guidance. A stochastic element that varies the nature of that guidance was floated and set aside as out of scope. The author will tune the prompts for balance, probably per inference model, since runs 3 and 4 showed the same text behaving differently. Per-model tuning is not an immediate goal either.

## Prompts

The frontier roles in `prompts.py` stay a single set: `frontier_choices`, `frontier_passage` and `frontier_ending`. They are not duplicated per act. They gained three slots, filled in `grow` by `choice_fields` and `passage_fields`:

- `{act_guidance}`, the act's guidance, or the ending's for `frontier_ending`;
- `{length}`, the target prose length, for `frontier_passage` and `frontier_ending`;
- `{act_remaining}`, as defined above, for `frontier_choices` and `frontier_passage`. It sits beside `{remaining}`, the levels left in the storyline, so the model can pace toward the next act boundary and still see the overall horizon.

`frontier_ending` has no `{act_remaining}` or `{remaining}`, since both are always 1 there. Because `prompts.validate` requires every declared placeholder, any saved override of these roles had to be rewritten. `backend/prompts.json` was empty when this landed.

The frontier draft schemas carry no length. `FrontierDraft` replaced the verbs' `PassageDraft` in `grow`, because a schema description reaches the model on every call and would override `{length}`. The assist verbs keep `PassageDraft` and its "one to three short paragraphs".

## The Agents view: a timeline and one loop

The old view drew `compiled.get_graph()`, which for `expand_frontier` is `plan → grow → gather → review → plan`. It was accurate, but the detail was in the wrong place. Every prompt the author tunes sits inside `grow`, and none of it was drawn. The reworked view has two linked layers:

```
 ┌─────────┬── Setup ──┬──── Confrontation ────┬─ Climax ─┬─ Resolution ─┐
 │ Premise │  L1  L2   │  L3   L4   L5          │   L6     │    L7        │
 └─────────┴───────────┴───────────────────────┴──────────┴──────────────┘
                  selected stage ▲ fills the slots below
 ── each level runs ───────────────────────────────────────────────────────
   plan ─▶ ┌ grow (per frontier passage) ─────────────────────────┐ ─▶ gather ─▶ review ─▶ next level
           │ choices ─▶ passage ─▶ continuity ─┐                   │
           │         └▶ ending ──▶ canon ──────┴▶ revise           │
           └───────────────────────────────────────────────────────┘
```

- **Timeline.** It shows the plan. Each act's width is proportional to its `levels`, with a tick per level, and the Resolution stage comes last.
  - Premise opens steer, the time guard, auto-accept and refine rounds, along with the derived depth and the most passages the plan can write.
  - An act opens its name, levels, branching, early endings, length and guidance, plus buttons to move it, remove it or add an act after it. A "+" in the timeline appends one.
  - Resolution opens the ending's length and guidance.
- **Loop.** It shows what runs once per level, drawn once, with `grow` opened into its prompt steps.
  - A prompt step opens that role's editor with its preview.
  - Every step lists the plan fields it reads, with the selected stage's values. For plan, gather and review, which don't call the model, that list is the whole panel.
  - Review still pauses after every level unless the run is unattended. The per-level review that the [unattended-run incident](../incidents/2026-09-28-skip-review-stopped-after-one-level.md) repaired is unchanged.
- **The link between them.** The selected stage dims the steps it never reaches: the ending step in an act, and the passage step in the Resolution. It also fills the preview's slots, since the preview request carries the stage and the unsaved plan (`preview_fields`). The author therefore edits the loop as it runs in that act, which is what lets one set of prompt roles serve every act.
- **Where the shape comes from.** The backend publishes the loop as `loop` beside each workflow's topology in `workflow_json`. For `expand_frontier` that is `FRONTIER_LOOP`: steps with their roles, the plan fields they read, their condition and the node they run inside. `test_loop_matches_node_prompts` holds `grow`'s steps to the roles in `NODE_PROMPTS`, which `test_node_prompts_match_behaviour` holds to what the code composes, so the drawing can't drift from the code. The frontend never hand-codes it.

The assist verbs and `canon_check` sit on a secondary tab and reuse the loop component. Their loops are built from their own graph nodes, and their prompt editors are kept. The view only configures; live run state stays in the RunDock. The RunDock's expand form lost its depth, branching and steer fields for a one-line plan summary, which links to the Agents view, and it keeps Unattended and Run.

The view dropped the run inspector (`RunPanel`) and the runs list. Only their provenance was of value to an author: the plan, model and prompts a run used. The owner agreed on 2026-09-29 to move it to a follow-on, the [Activity Log](activity-log.md). Until that lands, the RunDock row and the `/runs/{id}/state` endpoint are what remain. A run's `budget` column holds its copy of the plan, with the derived `depth`.

## Open question

- **Should the plan record a model?** Tuning is expected to be per model, so the plan could record the model it was tuned for, which would put a model picker on Premise. It was deferred when the plan was built. The header's model picker still chooses the model per browser.

## Not yet verified

The code is covered by unit tests with a stubbed model: `test_acts_pace_the_run`, `test_the_ending_level_takes_the_endings_guidance`, `test_pacing_plan_is_stored_per_story` and the preview test. It has not yet been run against a real model. Whether the model honours `{length}` and act guidance, and how the default plan's branching plays out, needs a [storyline eval](../runbooks/storyline-eval.md) under the default plan. The new view was type-checked and built but has not been clicked through in a browser.

## Relation to other designs

Acts are a structural funnel on the story's shape. [Choice anchoring](choice-anchoring.md) proposes a logical funnel, where accumulating world-fact constraints push branches toward conclusions. The two don't overlap, and a Climax act's guidance could ask the model to honour anchors once they exist. [Agentic authoring](agentic-authoring.md) owns the `expand_frontier` workflow the plan drives. [Refinement loops](refinement-loops.md) owns the audit and revise steps drawn inside `grow`, and its planned pacing auditor now has a concrete target: the act's guidance and `{act_remaining}`.

| Reference | What it anchors |
|---|---|
| `sources/20260929-act-pacing-interview.md`, `sources/20260929-act-pacing-followup.md` | every requirement on this page |
| `backend/src/cyoa/agents.py` (`DEFAULT_PLAN`, `plan_depth`, `stage_at`, `_choice_stage`, `choice_fields`, `passage_fields`, `preview_fields`, `fan_out_frontier`, `grow`, `after_gather`, `FrontierDraft`, `FRONTIER_LOOP`, `workflow_json`) | the plan's defaults, the level-to-act rules, the loop the view draws |
| `backend/src/cyoa/models.py` (`PacingAct`, `PacingEnding`, `PacingPlan`, `PromptPreviewRequest`), `db.py` (`pacing`), `main.py` (`/pacing` routes, `start_run`, `preview_prompt`) | the plan's shape, storage, and how a run copies it |
| `backend/src/cyoa/prompts.py` (`frontier_*` roles) | the `{act_guidance}`, `{length}`, `{act_remaining}` slots |
| `frontend/src/AgentsView.tsx`, `usePacing.ts`, `RunDock.tsx`, `runs.ts` | the timeline and loop, the plan's autosave, the dock summary |
| `backend/tests/test_agents.py` | the tests named above, and `test_loop_matches_node_prompts` |
