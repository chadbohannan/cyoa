# Hinge Canon

Hinge canon is how the [MCP authoring](mcp-authoring.md) experiment keeps a long, many-branch book from collapsing into near-identical endings. In the swarm approach ([Storyline Generation Quality](../syntheses/storyline-generation-quality.md)) lore that stated fixed truths ("light defeats the Answering") pulled every branch toward the same ending whatever the early choices were. The remedy is to stop treating all canon alike. Written 2026-10-07 and revised the same day when variables were removed ([Stateless Story](stateless-story.md)).

## Two kinds of canon

- **Invariants** hold on every branch: characters' natures, the setting's geography, the rules of the world. They live in lore entries (`kind` character, place, faction, rule) and are never contradicted.
- **Hinges** are facts the reader's decisions *decide*. Before a decision they are open; afterwards each timeline holds a different truth. A hinge is a lore entry named `HINGE: <name>` that lists its values, what each means for the world, and which decision settles it. A hinge has no variable: each value lives in its own branch, whose prose asserts it plainly. A passage may state a hinge's value only on routes that have settled it.

Invariants should produce *pressure*, not outcomes. A scarce resource, a clock or an incompatible want forces tradeoffs, and the tradeoffs differ by route. A rule that names the winning move produces one ending.

## Branching rule: a hinge buys a subtree

Without state, a hinge decided early and mattering late splits the book. Every passage after the decision that depends on it exists once per value, so the prose can say what is true. Scenes may rejoin routes only when they read the same however the reader arrived; anything that depends on a hinge does not rejoin. The cost is passage count, which is why the pilot is small and why hinges should be few and consequential.

## The pilot: The Salt Lamp, four decisions

The test story (id 3) is the first pilot. Its first draft (20 passages, about 24k words, 10 endings) used numeric variables for the hinges; it is being rebuilt without them as 33 passages and 16 endings.

**Invariants.** The Answering cannot cross the lamp room threshold while the lamp burns, and mimics the beam half a beat late. Salt-oil is finite (three measures, one a night). The causeway shows only at low tide. Wren will not stay on the rock past dusk. Aldous's last logbook entry stops mid-sentence.

**Hinges.** `wren` (ally or cold, settled at the first decision); `lamp` (lit or dark, at the second); `aldous` (alive and hidden, held by the Answering; settled at the third on the lit route, at the second on the dark one); `wants` (company, a keeper, or release, at the third).

**Decisions.** D1 The Causeway (ask Wren, or go alone). D2 The Lamp Room (light it, or answer the pale light). D3 the first night (cellar and sea cave, the logbook, follow the voice, or ask what it wants). D4 the reckoning, where the endings differ by what the reader does and, in the ally subtree, by Wren's boat arriving at dawn.

**Outcome rule.** Every ending is written up before any D4 passage is drafted: which subtree reaches it, what it costs (who survives, whether ships still wreck, whether the reader leaves). Endings are designed, not discovered. The ending table lives in the story's lore as `ENDINGS (designed table)`.

## Keeping canon true as it changes

1. **Lore edits are recorded.** Each `lore_save` writes a revision (`lore_history`), so a change has a before and an after.
2. **A lore change reports its blast radius.** `lore_impact` lists the passages that mention the entry (whole words, hinge names without the `HINGE:` prefix). The agent rereads those and revises or notes them. Mentions are by text, so routes through the hinge are reread too.
3. **Routes are audited, not just nodes.** After a layer is written, each route is read start to finish against the invariants and the hinge entries.
4. **Endings are compared on text.** `endings_report` lists every ending with its routes and a `similar` list of pairs whose text overlaps heavily. Two endings that differ only in wording are the failure the experiment exists to prevent.

## First pilot run (2026-10-07)

Claude Code (Sonnet 5.5) wrote the first draft of The Salt Lamp through the MCP tools in one session: six invariant lore entries, five `HINGE:` entries and an ending table first, then the passages. `problems` was clean and `playtest` reached all ten endings. What the checks and a reread found, before variables were removed:

- **Hinge violations I wrote myself** were found by rereading against the hinge entries, not by any tool: an ending with Wren's boat at dawn on routes where Wren was cold; a route-specific claim about boots that contradicted the cave; the reader's gender and an invented name, against the invariant that neither is stated. Each is prose asserting something the canon forbids on that route, and `problems` cannot see it.
- **A hinge decided earlier than planned.** Writing the dark route asserted Aldous was held before the third decision; the `HINGE: aldous` entry was changed to match, which is the lore revision workflow working as intended.
- **Rejoining forced neutral prose**, and a gated choice carried the Wren hinge instead. That, with the gated-choice reachability bug in the first `endings_report`, is what led to removing state.
- **Summaries held up.** After the prompt was tightened the route summaries stayed within what the text says, including on a passage reached by two different routes.

## Second pilot run: the stateless binary tree (2026-10-07)

After variables were removed, Claude Code (Sonnet 5.5) wrote The Salt Lamp again from an empty database in one session: six invariant lore entries, three `HINGE:` entries and the ending table first, then 31 passages (about 24k words) as a pure binary tree, four decisions deep, sixteen endings. With no state the natural shape is a tree: every ending belongs to one path, so no passage had to be duplicated to keep a hinge true. `problems` was clean, `playtest` reached all sixteen endings over 1,000 walks, and `endings_report` found no pair of endings with overlapping text (Jaccard of 4-word runs, threshold 0.05).

What the run showed:

- **The same class of slip recurred.** A reread and word searches (`find_passages` for "lass", "tell her", "She told us") found five places where prose broke the canon: the reader gendered twice, a fourth keeper contradicting "three keepers" (one sentence added while writing a dialogue), "alone on the Skerrow" in a branch where Wren sails, and Wren arriving at night where his rule is dawn. All five were mine, written minutes after the lore that forbids them. No structural check can see them; only a sweep against the lore can. The fixes were five `update_passage` calls, each resending a whole body, which is where most of the repair cost went.
- **New canon crept in through dialogue.** A mother mentioned in one ending, a wife and then a hired boy for the second keeper, boots, a parrot. Each is harmless alone and each is a fact no lore entry owns. The fix used here is to add an invariant after the fact (`The Reader's other parent`), which is a lore revision, but the cheaper habit is to check a passage's new proper nouns and relationships against `lore_list` before saving it.
- **Route summaries were hardly needed.** Only two `get_passage` calls were made in 31 passages, because the whole tree fit in the agent's context. The summariser exists for the case where it does not (a long run after the context is compacted, or a much deeper tree); this run does not test it. The two calls took about 17 seconds in total against a local 27B model.
- **`lore_impact` missed a character named by one word** ("Wren Tolliver" matched only the full name). It now searches each word of a character's name too.
- **A binary tree trades breadth for cost, not for quality.** Every decision doubles the passages after it, so the writing cost is the leaves: sixteen endings were 16 of 31 passages and roughly half the words. Seven or eight decisions at this width is 255 to 511 passages.

## Shape and variety (planning guidance, 2026-10-08)

The second pilot's tree was a symmetric binary one: every ending at depth 4, and every decision a version of go or stay, accept or refuse. Readers (and the author) saw too little variation, and no early endings. The server instructions now ask for a plan before writing: a lore table of every intended ending with its depth and what it costs or gives; a ragged tree, with reckless or rash choices that end the story at depth 1 or 2 and other branches that run the full 7 or 8 decisions; branching of 2 or 3 options (three is a hard limit set by the intended reader hardware, enforced by `add_choice` and `add_passage`); and choices that differ in kind (act, refuse, deceive, flee, call for help, destroy). Ragged shape also cuts the 2x-per-decision cost. `nearest_endings` ([[concepts/mcp-authoring]]) is the guard: run it while planning siblings and after writing, and treat a close pair that split early as convergence to fix by changing what one ending costs the reader, not its wording.

## Open questions

- Passage length (1–4k words) against the summariser, whose notes cap at 180 words and may lose detail on long routes.
- Whether eight decisions can stay distinct without a large share of branches ending early, given that every hinge multiplies the passages after it.
