# Interview Me

Interview Me is a conversation in which an agent interviews the author to create or improve two things: the story's **premise** (its description) and its **character suite** (the `character` entries in lore). The agent runs the conversation. It asks the questions, asks for clarification when an answer is vague, and writes what it learns into the Premise and Lore sections as it goes. When it finds a conflict, it does not resolve it on its own. The conflict becomes the next thing it asks about. It was scoped on 2026-09-29 and builds on that day's Premise editor (see the [frontend](../systems/frontend.md)). The backend (I1) and the panel (I2) were built the same day. The backend is verified only against a stubbed model, and the panel only by typecheck and build, not yet in a browser. The Library entry point (I3) is not built.

The scope was narrowed twice on the day it was written (`sources/20260929-interview-me-direction.md`, then `sources/20260929-interview-me-scope-down.md`). A first draft was a checklist-driven extractor covering premise, all lore and the timeline. A second draft made it a worldbuilding agent with authority over the whole prompt frame. That draft grew too large, and the owner cut it back to this conversation-first scope. Everything to do with the timeline is out, including the ticking-clock topics, the event ops and the event context.

## What it owns

- **The conversation.** The agent always holds the turn. Each reply from the agent is exactly one of three things:
  - a question about a gap,
  - a clarification request about the author's last answer ("you said the captain is loyal, loyal to whom?"),
  - a conflict question.

  The author answers, skips, or says **You decide**, which sends a clear request for the agent to invent an answer.
- **The premise text.** This is `stories.description`, the `STORY:` block every agent prompt opens with (`render_context` in `backend/src/cyoa/agents.py`). The agent revises it as a whole once it knows enough, and again whenever an answer changes it. Each revision is shown as a before/after diff.
- **The character suite.** These are the lore rows with `kind = 'character'`. The agent can create a character, edit a character's name and body, and fold a duplicate into an existing entry. What the suite needs:
  - who the reader is,
  - who or what opposes them,
  - the allies and other significant figures,
  - for each character: what they want, how they stand toward the reader, and what they know or hide.

Other lore kinds (place, faction, rule) and the opening passage are shown to the agent as read-only context. That lets it notice a conflict with them, but it does not edit them.

## What the interview is building: a brief, not a plot

The premise is prepended to every generation prompt, and [act pacing](act-pacing.md) already gives the story its arc, so the interview's target is a **brief for a branching generator**: a situation and its tensions, never a route through the story. Added 2026-09-30 after the author found the interview "exploring scene and character but wanting specific actions within the story arc", which is counterproductive to hand a choose-your-own-adventure generator. The evidence was in the data: one interview asked "what does Alice find when she lands?" and "how did his smuggling tie them to the three operations?", and its premise had become a sequence (a cold-case lead, then off-world, then the moons, "there she finds Miles Pierce"), which every branch would inherit as a fact. Another asked near-identical questions three or four times in a row, and after a skip rephrased the same question.

The premise holds who the reader is and where they stand at the start, what they want, what opposes them, what is at stake, the tone, the rules of the world that must stay true, and open questions different branches may answer differently. It leaves out events in sequence, what anyone finds or does next, outcomes and endings, and the test is whether a sentence is true on only one path through the story. A character holds what they want, know or hide and how they stand toward the reader, not scripted actions.

Four rules enforce it, each in the prompt and again in the review gate, because on the real model the prompt alone was not enough (a probe still asked "what does the crew do for a living, and how do they relate to the cold case", and re-asked a skipped topic):
- **Ask about situations, wants, fears, rules and possibilities.** Never what happens next, what someone finds, or how something ends. When the author volunteers a story event the agent asks once whether it holds on every path or is only one option, and records it as background or as an open possibility.
- **The reviewer holds back a write that narrates events in order or fixes an outcome** some branch could resolve differently; a probe held back "on landing, Alice finds the crew alive and learns they smuggled the cargo" and passed a fear and an open question about the threat's nature.
- **Never repeat a question.** A skip is shown to the agent as "do not ask it again in any wording, ask about something else"; the reviewer reports a restated or skipped question; and code compares every new question with the earlier ones (`_repeats`: half of the words in common, calibrated against reworded repeats at 0.58 to 1.0 and unrelated questions at 0.26 or less), skipping the check while a conflict is open because conflict questions repeat by design. A restated question earns a retry and is never a failure.
- **One question.** The reviewer reports a question that asks two things.

What is not built: an agenda or progress row that shows which parts of the brief are settled and when the interview is done. That was offered on 2026-09-30 and not chosen, so the interview still has no visible finish line. A first end-to-end run of three turns on the real model after these changes asked only situational and identity questions and never repeated one, but the premise it started from was already plot-shaped and none of the three turns wrote a change, so how a fresh interview reads is unverified.

## Conflicts drive the conversation

A conflict is anything that cannot all be true at once:

- the latest answer against the premise or against an existing character,
- two characters against each other,
- a character against read-only lore,
- a new character whose name collides with an existing one.

Two places detect conflicts. The model reports the ones it notices in its structured output. Code catches the mechanical ones: an edit aimed at a character whose `reviewed` box is checked, and a name collision the agent could not fix when it was told about it (a plain repeat of an existing character is not a conflict).

Conflicts are recorded on the interview as `open`, each with the two statements quoted. Open conflicts take priority over new topics, so the agent's next turn asks the author which statement holds, or how both can. Only the author's answer closes a conflict. The agent then writes whatever edit the answer implies, and marks the conflict `resolved`.

This also settles the `reviewed` question. The agent writes unreviewed characters and the premise directly. For a reviewed character, it never overwrites. The edit it wanted becomes a conflict question ("*Mara* is marked reviewed as the reader's sister; you just called her a stranger. Which is it?"). This keeps [agentic authoring](agentic-authoring.md)'s rule, that agents propose rather than write where `reviewed = 1`, with the conversation doing the job a proposal would.

## A turn

The turn is synchronous. It is not a [run](../systems/backend.md). The graph is `INTERVIEW_GRAPH` in `backend/src/cyoa/agents.py`, a small LangGraph with no checkpointer, shaped like the verbs: `gather → converse → validate → review → apply`, with both checks able to send the turn back to `converse`, with a `fail` exit after `MAX_ATTEMPTS`. `run_interview_turn` drives it.

- **gather** (`interview_context`) reads the title and description, every character (uncapped, with id and `reviewed`), the read-only lore, the opening passage, the transcript so far plus the author's new entry, and the conflicts. `render_interview` turns that into the frame, which replaces the passage frame: `compose` takes a `frame=` argument for exactly this.
- **converse** is one structured call (`InterviewReply`, temperature 0.7) to the `interview` prompt role. It returns:
  - `premise`: the whole revised description, or null,
  - `characters`: `[{op: create|edit|merge, id?, merge_ids?, name, body}]`, where a merge keeps `id` and deletes `merge_ids`,
  - `conflicts`: `[{about, a, b}]`,
  - `resolved`: the ids of open conflicts the author's latest message settles,
  - `reply`: the question, clarification or conflict question that ends the turn.
- **validate** is code, and it is the agent's second party in a short exchange, not just a gate. It collects everything the agent can fix and sends it back in one message together with the agent's own reply as JSON, and `converse` answers again, up to `INTERVIEW_ATTEMPTS` (4) replies per turn. What comes back:
  - an unknown id, or an op on a non-character row;
  - an empty name or body, and a merge without `merge_ids`;
  - a create, or a rename, onto a name that is already taken, with a message that says who owns it: "`#7 Mirelle` already exists (…). To change that character use op "edit" with id 7; to add someone else give them a different name; to add no one leave this change out";
  - an empty reply.

  The word "null" is not a value. Structured output through a tool call has no way to say "no premise" except in words, and on 2026-09-30 a turn returned the string `"null"` for `premise`, which only the blank check saw past, and overwrote a story's description with it (the write's `before` was still in the transcript, so Revert restored it). `_nullish` now reads a premise of null, none, nil, n/a or undefined (case-, space- and full-stop-folded) as no change, and treats a character name or body like that as an empty one for the agent to fix.

  Two cases are not problems at all. A create that repeats an existing character word for word (name and body, case- and whitespace-folded) is dropped as a no-op, and so is an edit that changes nothing. Both used to become a conflict, and the 2026-09-30 audit of a real database found every one of its four open "name is taken" conflicts was exactly this: the agent re-creating a character the suite already held.

  What only the author can settle becomes a conflict with `source: "code"`: a change aimed at a reviewed character, and a name collision the agent still has not resolved on its last attempt. It keeps `resolved` only when the turn carries an author answer: not on the opening turn, and not on a skip. A premise identical to the current one counts as no change. If problems remain after the last attempt, the turn fails as before.
- **review** is the gate on every write, added 2026-09-30 after the string `"null"` overwrote a story's description: code had checked only that the premise was not blank, and no static rule can anticipate the next such thing. It is a second model call (`audit_interview`, `InterviewReview`, temperature 0.2), and since the same day it also reviews the agent's question, so it runs on every turn. The reviewer reads the whole frame plus the author's latest message repeated next to `PROPOSED QUESTION [0]` and `PROPOSED CHANGES`, a numbered list of every write as BEFORE (the text it replaces) and AFTER. It reports a change only when it should not be written: not established by the author, losing facts nobody changed, empty or a stand-in word, contradicting existing text without saying so, or inventing something when the author did not say "you decide". It also reports the question ([0]) when it asks what happens next, what someone finds or how something ends, repeats an earlier question, asks about something the author skipped, or asks two things. A note goes back to the agent exactly as validation's problems do, with the agent's own reply, and the agent answers again within the same `INTERVIEW_ATTEMPTS`. When the attempts are spent, a change the reviewer still objects to is **not written**: it becomes an open `source: "code"` conflict ("held back: …", the current text against the proposed text) that the agent asks the author about, and the changes it did pass are written. A question is never held back, since the turn needs one: if the reviewer still objects to it on the last attempt it is asked anyway. If the reviewer cannot be reached, the turn fails and writes nothing.

  The author's latest message is repeated because a first probe against the real model (Bonsai 27B) objected to a fact the author had just stated, saying the conversation "never mentioned" it: with the message buried in a long transcript, the reviewer read past it. With it beside the changes, a probe on story 2's premise held back the `"null"` write and an unrelated pirate rewrite and passed a sentence the author had supplied, at 1 to 4 seconds a call. That is one sample each, not an evaluation. The gate makes every turn cost at least one extra model call: a turn on the real model now takes 6 to 25 seconds, and more when the agent is sent back.
- **apply** writes in one transaction, together with the transcript and conflicts. It records `{table, id, before, after}` for each write on the agent's entry, so the panel can show a diff and revert one write or the whole turn. A turn that fails writes nothing, not even the author's entry, so the client can resend it.

The call goes through `_ask`, so it waits behind any run on the process-wide gate (see the [fan-out OOM incident](../incidents/20260920-fanout-oom.md)). The panel shows "waiting for the model" while it does.

## Storage, routes, UI

```
interviews  id, story_id, status ('open'|'done'),
            transcript JSON [{role: 'agent'|'author', text, mode?, writes?: [{table, id, before, after}]}],
            conflicts  JSON [{id, a, b, about, status: 'open'|'resolved', resolution?}],
            created_at, updated_at
```

The table is additive (`CREATE TABLE IF NOT EXISTS`, like `pacing`), so no `cyoa.db` reset is needed. Its routes live under `/api/stories/{id}/interviews`:

- `POST {model?}` starts an interview and returns it with the agent's opening question. The first turn reads what exists and opens on the biggest gap or the first conflict, so a story that already has a premise and characters gets questions about them. If the opening turn fails, the row is deleted.
- `GET` lists interviews, and `GET /{iid}` returns one.
- `POST /{iid}/turn {text, mode: answer|skip|decide, model?}` returns the whole updated interview. An `answer` needs text (422), and a closed interview refuses turns (409).
- `POST /{iid}/revert {entry, write?}` undoes an agent entry's writes newest first, or only the one at index `write`. It is all-or-nothing: if any row no longer matches its `after`, because the author has edited it since, it refuses with 409 and changes nothing. A deleted row comes back with its original id. Revert leaves the conflicts a turn opened or resolved as they are.
- `POST /{iid}/close` sets `status` to `done`.

A model that fails `MAX_ATTEMPTS` times, including by raising, gives 502 (`VerbFailed`), the same as the assist verbs. `models.py` (`Interview`, `InterviewEntry`, `InterviewWrite`, `InterviewConflict`) and `api.ts` (`api.interviews`) mirror these. The `interview` role lives in `prompts.py`. It has no placeholders, because the conversation is in the frame.

**UI.** An **Interview me** button in the Premise header opens a panel docked beside the Lore view. The panel shows:

- the transcript, with a collapsible diff and revert under each agent turn,
- open conflicts pinned above the answer box,
- an answer box with **Skip** and **You decide**.

The premise field and the character cards the last turn touched pulse. Both stay editable, and the next turn reads the author's edits.

As built (I2, 2026-09-29), the panel is `frontend/src/InterviewPanel.tsx`. The button reads **Interview me**, or **Resume interview** when the story has an open one. `×` only hides the panel; **End** closes the interview for good. The hook, `useInterview.ts`, lives in `App.tsx`'s story shell rather than the Lore tab, so a turn waiting on the model survives a tab switch. It picks up the story's latest open interview on load, and reloads the graph after every turn or revert, because both write server-side. The answer box sends on Enter (Shift+Enter for a new line). If a turn fails, the text stays in the box, matching the backend, which records nothing then. **You decide** sends whatever is typed as steering. While a turn is out, the log says "waiting for the model…", or "queued behind a running agent…" when a run holds the gate. Each agent turn's changes sit in a collapsed list, one line per write: a premise before/after, a new character, a renamed or edited one, or a merged-away one. Each write has its own **Revert**, and there is **Revert this turn** when more than one write is live. A revert the backend refuses shows its 409 message in the panel. Conflict badges on a turn name what it raised or settled; code-found conflicts are badged "rule". The flash is a one-shot 2.5 s `touched` animation on the premise textarea and the character cards, and it skips reverted writes.

## Build order

1. **I1, backend.** The `interviews` table, the turn graph, the `interview` role and the routes. Tests go in `test_agents.py` with a stubbed model:
   - an answer creates a character and revises the premise,
   - a name collision opens a conflict instead of writing,
   - an edit to a reviewed character opens a conflict and writes nothing,
   - an answer resolves a conflict and applies its edit,
   - non-character ops are rejected,
   - revert restores the before images.
2. **I2, panel.** The transcript with diffs and revert, the pinned conflicts, the answer modes and the pulses.
3. **I3, entry point.** The Library's new-story flow offers **Start with an interview**.

I1 was built on 2026-09-29 with the tests above in `backend/tests/test_agents.py` (`test_interview_*`), plus a prompt-preview test. The interview graph is registered in `workflow_json` and `NODE_PROMPTS` as `("interview", "converse")`. That puts it on the Agents view's *Assist & canon* tab with an editor for its role, which the scope had planned for later. The preview route renders the interview frame for that role instead of the passage frame. The first live session found a hang that was not the interview's own: see [the interview thinking runaway](../incidents/2026-09-29-interview-thinking-runaway.md). After the fix, an opening turn on Ternary Bonsai 27B parsed cleanly in 2.5 s, though its question bundled two questions into one. Multi-turn reliability, with conflicts and character ops, is still untested live; `scripts/interview_probe.py` prints the raw reply for an opening turn.

## Out of scope

- **Everything to do with the timeline.** The owner judged the Timeline feature "overly aspirational at this time" and discarded it. The owner then chose to remove it from the product outright, the same day: the `events` table, its routes, the Lore tab's Timeline section and the `TIMELINE` prompt block are gone (see [agentic authoring](agentic-authoring.md)).
- Places, factions and rules as write targets, the opening passage, variables, and the pacing plan.
- **The prompt cap.** `gather_context` keeps only `LORE_CAP = 20` lore rows, ordered by kind then name. A large bible therefore silently drops entries from every prompt. That is worth fixing on its own, but it is not this feature's job.

| Reference | What it anchors |
|---|---|
| `sources/20260929-interview-me-direction.md`, `sources/20260929-interview-me-scope-down.md` | the owner's direction and the scope-down |
| `backend/src/cyoa/agents.py` (`render_context`, `_ask`, `parse_validate`, `LORE_CAP`) | the frame the premise heads, the gated call, the retry pattern, the cap |
| `backend/src/cyoa/db.py` (`stories.description`, `lore`, `pacing`) | what the agent writes, and the additive-table precedent |
| `backend/src/cyoa/prompts.py` (`PROMPTS`) | where the `interview` role lives |
| `frontend/src/LoreView.tsx` (`Premise`, `LoreGroup`, `LoreCard`) | the surface the panel docks beside |
