# Stateless Story

A story in this project is passages joined by choices and nothing else. On 2026-10-07 the `variables` table, the `choices.condition` and `choices.effects` columns, the condition/effects evaluator in `engine.py`, the variable and clause editors in the editor, the player's state and debug panel, and the SugarCube macros in the Twee export were all removed. This page records why and what replaced them. The earlier design, with the JSON vocabulary and its semantics, is described in the pre-2026-10-07 history in [Story Model](story-model.md) and [The Webapp UI](webapp-ui.md).

## Why

The change came out of the [MCP authoring](mcp-authoring.md) experiment, where one long-running agent writes a book as many alternate-timeline short stories under one lore document. Written that way, state outside the prose caused three kinds of trouble, all seen on the first pilot ([Hinge Canon](hinge-canon.md)):

- **The prose could not see it.** A passage has one body. A scene reached by routes with different state therefore read the same on every route, so a fact that mattered (whether the boatman was an ally) could only show up in which choices were offered, never in what the text said. Authors worked around this by keeping scenes neutral, which flattened the routes the experiment wanted to make different.
- **It could contradict the prose.** The state said one thing and a passage asserted another (a boat arriving on a route where the variable said it would not). Nothing in `problems` could catch that, because the prose is free text.
- **It did not travel.** State needed SugarCube macros to leave the editor, so the export was tied to one story format, and everything that read a story (the summariser, the endings comparison, the route search) had to replay effects and honour conditions to be correct.

## What replaces it

- **The route is the state.** What a reader knows on arriving at a passage is the passages they passed and the choices they took. The summariser condenses exactly that for the agent, per route (`summarize.arrivals`).
- **A fact that differs between timelines lives in separate branches.** The prose of each branch can assert it plainly. The cost is more passages, because rejoining routes is only safe where the scene reads the same however the reader arrived. Rejoining is allowed (`add_choice` to an existing passage) and left to the author's judgement.
- **Hinges are lore, not variables.** A fact the reader's decisions decide is described in a `HINGE:` lore entry that names each timeline's value, and each branch's prose must agree with it. `lore_impact` finds the passages that mention the hinge to reread.
- **Divergence is checked on text.** `endings_report` compares the text of every ending and lists pairs that overlap heavily, the sign two timelines converged.

## What changed in the code

`engine.py` keeps only graph analysis: `problems` (no start, unreachable, dangling, endings, unfinished, merge candidates), `subtree`, `prune_set`, `merge_candidates`, `random_playthroughs` and the row helpers. The player returns a passage and its choices and takes only a `choice_id`. `random_playthroughs` has no `stuck` outcome, since every choice is always open. `twee.export` writes plain `[[text->target]]` links with `//emphasis//` and `''strong''` (no `StoryInit`), so a file imports without a story format. Proposals no longer include `new_variable`.

A database created before this date keeps an unused `variables` table and `condition`/`effects` columns. Nothing reads or writes them, and a fresh database does not have them. The LangGraph agents lost the `VARIABLES:` line from their prompts and nothing else; their evaluation history in [Storyline Generation Quality](../syntheses/storyline-generation-quality.md) predates the change.
