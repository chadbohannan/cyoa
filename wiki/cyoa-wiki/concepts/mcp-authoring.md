# MCP Authoring

MCP authoring is the second approach to writing a book with an agent, and the opposite of [Agentic Authoring](agentic-authoring.md). The LangGraph workflows (`agents.py`) cut authoring into small, model-bound steps (draft a passage, audit it, propose choices) so that no single call has much room to go wrong. Here one long-running agent in a general harness (Claude Code, Codex, OpenCode, Hermes) is the author, and the project provides only tools and state. The point is to compare how such an agent handles a whole branching book under one lore document, with the harness and model as the only variables. It lives on the `cyoa-book-mcp` branch, started 2026-10-07.

## What it owns

`backend/src/cyoa/mcp_server.py` is a stdio MCP server (`make mcp`, or the `cyoa-mcp` script) over the same SQLite file the editor uses, so the [map](../systems/frontend.md) shows the book growing while the agent works. It calls `db.py` and `engine.py` directly and imports neither LangGraph nor FastAPI's app. Its tools:

- **Reading:** `story_overview` (premise, counts, lore index, pacing plan, problem counts, frontier size, and `shape`, described under Shape check below), `get_passage`, `find_passages`, `frontier`, `problems`, `playtest`, `export_twee`, `lore_list`, and a `cyoa://story/{id}/canon` resource.
- **Writing:** `create_story`, `edit_story`, `add_passage` (writes and wires in one step, filling a dangling choice or adding one), `update_passage`, `delete_passage`, `prune_below`, `add_choice`, `update_choice`, `delete_choice`, `lore_save`, `lore_delete`, `lore_history`, `lore_impact`, `endings_report`, `nearest_endings`, `fact_check`, `canon_check`, `lint_passages` and `quality_map` (a decision model flags named craft defects per passage, and measures yes/no qualities per passage and per branch to find the weakest branch to rewrite. The experiment concluded on 2026-10-18 that `quality_map` does not help an author that can read the whole book, so it is experimental; see [Story Quality Scoring](story-quality-scoring.md)), `delete_story` (needs the exact title). `add_passage` and `update_passage` take `reveals`, `needs` and `verdict`, which [Route Canon](route-canon.md) explains.

Writes are immediate and ungated by design: there are no proposals and no `reviewed` check, and passages are written with `provenance = 'agent'`. The agent's safety net is `problems` and `playtest`, which walk the same graph as the player. A story has no variables, conditions or effects ([Stateless Story](stateless-story.md)): the route a reader took is their state. The pacing plan is only advice here: it is shown in `story_overview`, and nothing enforces it.

## The story so far is automatic

A long book has many routes to any passage, and rereading ancestors costs context that grows with depth. `get_passage` therefore returns, for up to `routes` shortest routes from the start, the passages passed through, the last choice taken, and a summary of the prose before the passage. There is deliberately no summarise verb; the agent never asks for it (`summarize.py`).

- **The route is the state.** With no variables, what a reader knows is the passages passed and the choices taken, which is exactly what is summarised. The summary prompt tells the model to state only what the text says, since a small model's invented detail would carry down every later summary.
- **Rolling and content-addressed.** The summary of a route prefix condenses the summary of the prefix before it plus the passages since. The `summaries` table is keyed by a hash of the prefix's ids, titles, bodies and choice texts, so editing any passage on a route orphans its stale rows with no invalidation step, and a new passage on a known route costs one call. A cold route catches up in chunks (6 passages or 12k characters), one call each, caching at every chunk end.
- **Degrades, never fails.** If the summariser is unreachable the route gets raw excerpts (`summary_source: "excerpt"`) plus the newest cached summary, and the model is left alone for 30 seconds so a dead server costs one timeout.
- **Configuration.** `CYOA_SUMMARY_URL` (default `http://0.0.0.0:8080/v1`, a local llama-server), `CYOA_SUMMARY_MODEL` (`default`), `CYOA_SUMMARY_TIMEOUT`, `CYOA_SUMMARY_MAX_TOKENS`. The call sends `enable_thinking: false` and the token cap as `max_tokens`, for the reasons in [An Interview Turn That Never Answered](../incidents/2026-09-29-interview-thinking-runaway.md). This is separate from `llm.py`, which the workflows use. Pin one summariser across the runs being compared, since it is itself a variable; its model name is stored on each cached row.

How the agent keeps many branches distinct and consistent with canon, rather than converging, is scoped in [Hinge Canon](hinge-canon.md), which also covers the lore revision, impact and endings tools. Whether each route is correct (every clue a passage relies on was shown on every route into it, and the prose keeps to the canon) is checked by the tools in [Route Canon](route-canon.md).

The step-by-step habits that came out of writing four books this way (lore first, shape planning, tagging, the checks' limits, the literary pass) are collected in the runbook [Writing a CYOA Book](../runbooks/writing-a-cyoa-book.md).

## Comparing runs

Every tool call is logged to `tool_calls` (tool, arguments, ok/error, milliseconds, story) under `CYOA_MCP_LABEL`, which whoever launches the server sets per harness and model (for example `claude-code/opus`). Use the same lore, premise and pacing plan for each arm and a fresh story per run. Comparing arms is then a query over `tool_calls` plus the resulting graph, with the [storyline eval](../runbooks/storyline-eval.md) reading the stories.

## Practical notes

- The server writes wherever `CYOA_DB` points, default `cyoa.db` in the *harness's* working directory. Always set it to an absolute path; the server prints the resolved path to stderr on start.
- A passage takes at most three choices (`MAX_CHOICES` in `mcp_server.py`, the intended reader hardware's limit). `add_choice` and `add_passage` refuse a fourth and roll back, so the agent must fold options together or move a decision later. The editor and REST API do not enforce it, and `problems` does not check it.
- Tool errors that the agent should read are raised as `Refused` (a `ToolError`); with `mcp` 2.x any other exception reaches the model only as "Error executing tool".
- `mcp` 2.x renamed `FastMCP` to `MCPServer` (`mcp.server.mcpserver`).
- Not built: an automatic warm-up of summaries after writes (they are computed on first read), and a way to read `tool_calls` other than SQL.

## Semantic convergence check

`endings_report`'s `similar` is lexical (shared 4-word runs) and found nothing in the second pilot, though two endings in "The Salt Lamp" were the same scene: Wren's boat takes the reader off the rock, with his cap in his hand. `nearest_endings` closes that gap. It embeds each ending (`embed.py`, from the llama-server's `/v1/embeddings`, cached in the `embeddings` table), subtracts the book-wide mean so shared setting words don't dominate, and ranks pairs by cosine similarity with `diverged_after`, the number of passages their shortest routes share. A pair with a high score and a small `diverged_after` has converged. On the pilot, the top pair (The Dawn Boat and Low Tide, 0.395, split at the first choice) was a real duplicate and the second was two father-among-the-drowned scenes; scores are relative to the book, so read the ranking, not a threshold.

Operating constraints learned running it: the chat server must be started with `--embeddings --pooling mean` (`~/bin/gemma4a12`, `~/bin/qwen38t`); inputs are clipped to 900 characters (start and end kept) and sent one per request, because 1500-character inputs batched eight at a time ran the 12 GB GPU out of memory and killed the chat server. A dedicated small embedding model on its own port (`CYOA_EMBED_URL`) would remove that coupling.

The scores are only as good as the chat model's pooled vectors, and those can be too weak to measure the thing it is meant to find. On "The Case of the Silver Paddle" (2026-10-10), with gemma-4-12B serving the embeddings, ten failed endings closed on the same scene (Bram "finds" the paddle at 10:30 and is cheered). Eight were rewritten so each costs the reader something different: sent home, mocked, an innocent blamed, the race called off over a stranger scare, the paddle found by a child. The top pairs barely moved (0.808 to 0.801), because the vectors hardly separate meaning. A probe scored two sentences with the same meaning 0.893 and two with opposite meanings 0.845, before centring. With a chat model's embeddings, judge convergence by reading the outcomes, the way the endings table in the lore lists them. Pooled vectors seem to track style and shared vocabulary more than what happens.

## Shape check

Ending depths alone do not show whether a tree is ragged. "The Case of the Silver Paddle" had three endings at every depth from 2 to 7, which looked ideal, but every wrong turn was one passage long: a spine with exits. Every one of its 18 spine decisions also stored the failure first and the way on second, so a reader who always took the second option never had to think. The user spotted both in the map, which draws options left to right in choice order (`layout.ts`); none of the checks did.

`story_overview` now returns `shape` (`engine.shape`, added 2026-10-10). An option's *reach* is how many more decisions its longest route plays before an ending. At each decision reachable from the start, the option with the greatest reach is the trunk and the others are side branches. A last call where every option ends has no trunk, so it is left out of the side-branch counts. The result reports:

- `instant_exit_decisions`: decisions where one option ends at once and another goes on;
- `side_branch_reach`: a histogram of how far side branches run;
- `trunk_slot` and `exit_slot`: which 1-based position holds the way on and the instant exits;
- `always_pick`: where always choosing slot *k* leads.

It warns when more than 60% of decisions are "end now or go on", when more than 80% of side branches end at once, when one slot holds the trunk or an exit at more than 75% of decisions, and when always picking one slot reaches a rare verdict (at most a third of the endings).

The server instructions now say that ragged means side branches of different lengths, that most wrong turns should run two or three decisions as a false trail with endings of their own, some of them good, and that the order of options should vary.

The three books so far:

| Story | Decisions | Instant exits | Side-branch reach | Trunk slot | Warnings |
|---|---|---|---|---|---|
| Lemonade Heist | 11 | 5 | {0: 6, 2: 1, 3: 2} | spread | 0 (but always picking slot 1 solves it) |
| Silver Paddle | 22 | 18 | {0: 18, 7: 2} | slot 2 at all 18 | 4 |
| Harlow Stone | 22 | 13 | {0: 13, 1: 2, 2: 1, 3: 1, 4: 1, 5: 1} | {1: 6, 2: 7} | 0 |

"The Case of the Harlow Stone" (story 3) was written to these instructions. Several of its false trails end in solved endings of their own. In one, the reader searches Theo's boxes, admits it, and Theo becomes the witness who finds the thumbprint. In another, the reader leaves the found Stone in the crate and waits with Ms. Vance to catch the thief taking it.
