# Choice Anchoring and Narrative Coherence

This page explores a fundamental tension in multi-timeline interactive narratives and proposes a resolution centered on **world-state anchoring**: the idea that every choice a reader makes must be explicitly grounded in a declared fact about the world, and that fact becomes a constraint on all other timelines to prevent narrative incoherence.

## The Coherence Problem

The current design treats [passages](story-model.md) and the [story clock](agentic-authoring.md#the-story-clock) as the coherence layer: agents generate new branches guided by lore, location, and the passage's position in story-time `t`. This works for *world consistency* — a character's biography stays true, events happen once on every branch — but it fails at *narrative coherence*.

A book is about something specific. It has a through-line, tone, and set of concerns. When `expand_frontier` generates two branches from a passage, they inherit only the immediate passage text and the world facts; they do not see the narrative arc that led there. One agent might write "the queen betrays you" and another might write "the queen rewards you," both valid locally but contradictory in meaning. A reader following one path feels a coherent story; a reader who later learns the other path exists learns that the author did not choose.

The cause is architectural: **agents see local context (passage + neighborhood + lore) but not the full narrative spine that makes choices meaningful.**

The consequence is that without further constraint, a frontier expands exponentially, and each branch is narratively independent of the others — a collection of text blobs rather than a book that fans out.

## The Solution: World-State Anchoring

**Every choice must declare what about the world changes at that decision point, and that declaration becomes a constraint on other branches.**

When an agent creates a choice (targeted verb `suggest_choices`/`suggest_next`, or autonomous `expand_frontier`/`grow`), it supplies not just choice text and a target passage, but also a **world-fact anchor**: an entry in the `lore` table or an `events` table row (or a new one, created on accept) that this choice is *predicated on*.

For example:
- Choice: "the queen rewarded you"
  - Anchor: Lore entry "queen" updated to include "She rewards loyal servants"
  - Semantic meaning: This branch exists because the queen is merciful

- Choice: "the bridge is out"
  - Anchor: Event at `t=100` "the river flooded" (created if absent)
  - Semantic meaning: This branch exists because of a world event that other branches might avoid

Other agents then see these anchors as **constraints**: "If the queen is cruel, this branch (which requires her mercy) cannot be taken. If the bridge is intact, this branch is impossible." The constraint set grows as the frontier expands, naturally funneling branches toward coherence.

An agent generating from a frontier passage sees:
1. The full narrative text from root to that passage
2. The existing choice anchors that define what world-state *this* branch has committed to
3. The available lore/events it can anchor to (or create)
4. Which untaken anchors remain — what other story possibilities still exist

## Data Model Changes

The `events` table this design anchors to was removed on 2026-09-29 along with the timeline (see [agentic authoring](agentic-authoring.md)), so as things stand an anchor can only be a lore entry; `event_id` below would return only with the timeline.

### Choices gain an anchor

Extend `choices` to add:

```sql
lore_id       INTEGER REFERENCES lore(id) ON DELETE SET NULL
event_id      INTEGER REFERENCES events(id) ON DELETE SET NULL
anchor_reasoning TEXT -- why this choice is anchored here
```

A choice may have zero (legacy), one lore anchor, one event anchor, or both. A dangling choice (no target yet) must have an anchor to be narratively meaningful to an agent.

### Proposals reflect anchors

The `new_choice` proposal kind gains anchor fields:

```json
{
  "text": "choice text",
  "to_passage": null | int,
  "new_target": {title, body, ...} | null,
  "lore_anchor": null | {"id": int, "kind": string, "name": string},
  "event_anchor": null | {"id": int, "t": number, "name": string},
  "anchor_reasoning": "why this world-fact gates this choice"
}
```

When accepted, `_materialise` writes the anchor fields to the choice row.

### Structured output for agents

Extend the JSON schemas for `ChoiceDrafts` and `NextStep` (the models agents output):

```python
class ChoiceDraft(BaseModel):
    text: str
    target_title: str
    lore_anchor: dict[str, str] | None = None  # {"name": "...", "kind": "character|place|faction|rule"}
    event_anchor: dict[str, str | int] | None = None  # {"t": ..., "name": "..."}
    anchor_reasoning: str | None = None
```

The agent is asked, in the prompt, to declare *why* each choice is meaningful — which world fact makes it true.

## Prompting Changes

### The full narrative spine

Instead of `render_context()` returning just `passage + neighborhood + lore + events`, it returns:

```
NARRATIVE SPINE:
<full passage text from root to current passage>

WORLD CONSTRAINTS AT THIS POINT:
- lore anchors already taken (what the branch has committed to)
- event anchors already taken (what world-events this branch has experienced)
- available lore/events (what the branch could anchor to next)

PASSAGE: <current passage title and body>
```

This is expensive — a root-to-leaf path could be 50+ passages × 500 words each. But it is *essential* for coherence. A 27B model with context window 4096+ can handle it for depth 5–10; a larger model or longer context changes the calculus.

### The choice generation prompt

Currently the model is asked: "Suggest new choices for PASSAGE that are distinct from EXISTING CHOICES."

Under this design:

```
Suggest new choices for PASSAGE. For each choice:
1. Declare what about the world or timeline makes this choice true. 
   - Create a NEW lore entry, or reference an existing one.
   - Create a NEW world event, or reference an existing one.
   - Explain the reasoning (why this world-fact gates this choice).
2. If this choice leads to an ending, anchor it to the reason (e.g., "the curse is lifted" or "you are doomed").
3. Avoid contradicting the anchors already taken on this branch (shown above).
4. Consider whether other branches might make different choices from the same decision point, 
   and whether those choices need different anchors.
```

The model is now *asked to think about narrative branching* — not just locally, but in terms of world-state.

## Frontier Dynamics: The Funnel Effect

As `expand_frontier` proceeds, the constraint set grows:

- **Level 1** (5 branches): Each declares an anchor. The lore and events tables now have 5 new entries.
- **Level 2** (5 choices per frontier passage = 25 new passages): Each agent sees the Level 1 anchors as constraints. Some choices become impossible — e.g., "the queen rewards you" contradicts "the queen is cruel" from another branch. Fewer new anchors are created because some branches reconverge (they both lead to the same world-state through different prose).
- **Level 3+**: The constraint pressure intensifies. By depth 5, unanchored story possibilities are exhausted. Agents naturally route toward conclusions: passages that declare `is_ending=true` *anchored to* a world-state (e.g., "you defeated the dragon" → "the dragon is dead").

**This is not forced merging.** Branches can remain textually distinct. But they are *constrained to be semantically coherent*. Two branches that both say "the dragon is dead" are narratively consistent even if they describe different battles.

## Tradeoffs

### Coherence vs. Context Size

The model now reads the full spine. For a 27B local model with 4K context, this limits depth to ~5, with ~10 passages per level. A larger model or a 32K context changes this. The frontier size and depth budgets become *context budgets* rather than *passage count budgets*.

### Authorial Intent vs. Agent Autonomy

An agent declaring world-facts is now *explicitly* choosing the meaning of a story, not just generating prose. A `suggest_choices` call might return anchors the author disagrees with. This is actually valuable — it surfaces what the model thinks the story *means* — but it changes the role of the author from "accept/reject prose" to "accept/reject semantic direction."

A choice with a contested anchor can be rejected or edited to reanchor it differently. This is a new form of author-agent collaboration.

### Legibility vs. Complexity

The system is more complex: passages, choices, anchors, and constraints form a larger state space. But it is also *more legible*. An author can read a choice and see *exactly* why the agent thought it was true. The anchors serve as a form of automated documentation.

## What Happens to Existing Features

### `suggest_next` and endings

An agent deciding `decision="end"` already sets `is_ending=true`. Under this design, it also declares the anchor: "why is this a good ending?" The anchor might be "the curse is lifted" (lore) or "the final battle is won" (event). This makes endings *semantically distinctive*, not just narratively terminal.

### `expand_frontier` / `grow`

`grow()` currently calls `draft_target()` for each choice without anchor information. Under this design:

1. `grow()` first calls `suggest_choices()` with the spine and constraints.
2. Each choice now has an anchor.
3. For each choice, `draft_target()` is called *with* the anchor pre-supplied, so the passage text can reflect what the anchor declares.

Example: "You found the lost amulet" (choice) anchored to a lore entry "Lost Amulet: a relic that breaks curses" (anchor). The target passage can then read: "With the amulet in hand, you approach the cursed door..." The passage text *flows from* the anchor, not independently chosen.

### Proposals and review

Anchors are part of the proposal payload. An author reviewing a `suggest_choices` batch can:
- Accept the choices and anchors as-is
- Accept the choices but reject the anchors (proposing different anchors)
- Reject specific choices outright

This makes the review phase *semantic*, not just cosmetic. The author is asked, for each choice: "Is this the right world-fact to justify this branch?"

### `canon_check`

The canon checker already compares passages to lore at their `t`. Under this design, it also verifies:
- Do the passage and its inbound choice anchors agree? (If the anchor says "the queen is cruel," does the passage treat her that way?)
- Do the anchors on sibling choices contradict each other? (Can't have "the queen is cruel" and "the queen is merciful" on the same passage unless they gate different branches.)

This is a new class of canon error: *anchor-narrative mismatch*.

## Open Questions

**Context budgets in practice:** How deep can a frontier actually expand with a 27B model reading full spines? Does the model degrade with deep nesting? Partial answer as of 2026-09-28: the owner's llama-server reports `n_ctx: 216064` (`curl localhost:8080/props`), well past the 4K figure this page assumed, so context size is not the limiting factor on this setup; `_spine` (`SPINE_CAP = 20`) already reads the whole root-to-leaf path at depth 3 without degrading coherence (see [storyline generation quality](../syntheses/storyline-generation-quality.md)), though that's prose-only spine reading, not the anchor/constraint reading this page proposes on top of it.

**Anchor creation heuristics:** When should an agent create a *new* lore/event entry versus anchoring to an existing one? Should the model be asked to minimize new entries (toward coherence) or create freely (toward expressiveness)?

**Reconvergence detection:** When two branches anchor to the same world-facts, should the system detect this and propose merging them? Or is narrative reconvergence ("both paths survived the forest") distinct from topological merging?

**Conditional anchors:** Can a choice anchor be conditional — "this branch exists if the queen is cruel, but not if she is wise"? This requires anchors to participate in `condition`/`effects`, which couples the two systems.

**Scale limits:** At what frontier size does narrative coherence break down, anchor-based constraints or not? Is there a story-structure size beyond which a *book* becomes incomprehensible?

## Reference

| File | Role |
|------|------|
| `backend/src/cyoa/db.py` | Where `choices` schema gains anchor columns |
| `backend/src/cyoa/agents.py` | `gather_context()` expands to include spine + constraints; `suggest_choices`, `suggest_next`, `grow` output anchors |
| `backend/src/cyoa/main.py` | `_materialise()` writes anchor fields; `_view()` / `play` is unchanged |
| `backend/src/cyoa/engine.py` | `canon_check` gains anchor-narrative mismatch detection |
| `frontend/src/Proposals.tsx` | Proposal review UI shows anchors alongside choice text |
| `frontend/src/Inspector.tsx` | Choice panel shows inbound anchors (why this choice is possible) |
