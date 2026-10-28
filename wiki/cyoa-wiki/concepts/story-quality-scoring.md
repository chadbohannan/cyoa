# Story Quality Scoring with a Decision Model

**Concluded 2026-10-18; see the Verdict section below.** Of the two tools built, `quality_map` did not earn its place as a way to focus an authoring LLM. `lint_passages` is useful only as a narrow defect check. Both are still in the code. The sections after the Verdict are kept as the record of what was tried.

Scope, written 2026-10-17: use a [System One decision model](system-one-decision-models.md) (the local `clef-flash` family) as a fast, typed scorer of passages and routes, and feed the scores back to steer the agent that grows the story tree. The pull is that today's judges are free-text LLMs whose answers must be parsed and are slow: the `--judge` option of [Evaluating Generated Storylines](../runbooks/storyline-eval.md) asks a model for `{consistent, readability, issues}`, the same model that wrote the text, and its pass is called weak evidence there. A decision model returns a schema-valid number in tens of milliseconds, so it can run on every candidate rather than on a sampled report.

## Verdict (2026-10-18)

The experiment set out to use cheap, automated scoring to steer an authoring LLM's attention to the parts of a book that need work. For books of the size this project writes, it does not. There are four reasons, and they come from the Jon experiment and its audit below.

- **The author can read the whole book.** A book of 40 to 110 short passages is 4k to 15k tokens, well inside the authoring model's context. A pointer to "the weakest branch" saves it nothing, because it can read every branch and judge for itself.
- **The judge is the weaker reader.** `clef-flash` is a 9B model that answers yes/no and gives no reasons. The authoring model is far stronger and can say why a passage is weak, so the judge's ranking adds little beyond what the author notices by reading.
- **The pointer is unreliable.** Branch means differ by less than one small edit moves a single passage. The ranking favours branches full of endings, because an ending has no next moment to fear and so scores low on `tension`. The averaged score barely registered prose whose sentences had been shuffled.
- **Steering on it rewards pleasing the scorer.** Every gain in the four Jon rounds (book `q` 0.745 to 0.768) came from rewrites written knowing which metric was low. That is what the numbers would show whether or not the prose improved, and no reader checked it.

What survives is the named-defect lint. It fires on defects planted on purpose and stays quiet on clean text. On Ghost Light it found habits the author model repeated (explained clues, endings told as summary), and a fix pass took its flags from 9 to 2. On Jon it flagged nothing. It is a cheap check against a known list, comparable to a regex with judgment, and the authoring model could check the same list if the list were in its instructions. It is not a way to steer attention. The ideas further down this page (best-of-N, a scored auditor, ordering the frontier, `score_route`) are not pursued, for the reasons above.

The technique might pay off where these conditions fail: a book too large for the author to read (hundreds of passages), a small local model doing the authoring, or a question no reading model answers reliably. In those cases, re-run the controls first: a deliberately weak branch planted in a book must rank lowest, and endings must be scored with their own questions.

Not done: route-level scores, reader-weighted aggregation, a check that a death is fair given the choice that led to it, and any test against human readers. Removing `quality_map` and `engine.weakest_subtrees` was offered and left to the user.

## Built: `lint_passages`

Added 2026-10-17 to the [MCP authoring](mcp-authoring.md) server (`lint_passages(story_id, passage_ids=None, threshold=0.6)`, client in `backend/src/cyoa/judge.py`, tests in `test_mcp.py`). For each passage it sends the whole passage plus the end of its first parent as `state` to `clef-flash-8k`, with one `noul` question per named defect from the literary-pass list in the [writing runbook](../runbooks/writing-a-cyoa-book.md): `moral` (ends on a spelled-out lesson), `clock` (ends on a clock time), `summary` (told from a distance, not a scene), `explains_clue` (points out a clue and says what it proves) and `monologue` (mostly one character explaining). It returns the passages with a defect at or above the threshold, most defects first, with the defect names and probabilities. About a quarter of a second per passage; no caching, because a whole book takes about 20 seconds. Configuration: `CYOA_JUDGE_URL` and `CYOA_JUDGE_MODEL`.

This replaced a first tool, `judge_route`, which scored a whole route section by section (a 400-character cut of each passage, `follows` / `prose` / `arc` scores). It was withdrawn the same day: the cut discarded half of every passage, the scores sat in a narrow band, `arc` did not separate solved from failed endings, and a scalar invites ranking, which the spike below shows is the wrong use.

First full run (all 108 passages of the three books, 63 seconds): 26 flagged. Read subjectively, `explains_clue` and `summary` hit real things (the safe-dial passage that names the chalk and says whose it is; a detective recapping the suspects in a clerk's scene; the summary-style "Star Walks Out" ending), `clock` was about half right (it fires on any time mentioned near the end, including "Opening night goes on at eight"), and a sixth question, `leak` ("tells the reader something they have not been shown"), was wrong on the three flags read, so it was dropped. Story 1, the oldest book, drew the most flags (12 of 40, six of them `clock`), consistent with the runbook's note that clock closers survived its literary pass. These flags are for rereading, not a measurement; no one has checked that fixing them improves a book.

Fix-and-relint on the Ghost Light book (2026-10-17): the nine flagged passages were reduced to seven rewrites (the chalk on the dial, Fenn's account, the receipt, Blue Chalk, the front desk, the summary-style "Star Walks Out", and the clock closer in "Pearls in the Hem"), mostly by replacing explained clues with something seen (blue hands to the wrist, a wine ring on the bill) and the summary ending with a scene. A second lint flagged two (`explains_clue` on Blue Chalk and the Front Desk, both 0.65). Both are alibi or exculpation beats, where a character has to say the fact aloud, so they were left. The drop from nine to two is partly self-fulfilling, since the rewrites were written knowing the questions; the check that matters is whether the new text reads better, and by the author's own reading it does, modestly.

## Built: `quality_map` and the Jon experiment (2026-10-18)

A second tool, `quality_map(story_id, metrics=None, min_size=3, top=3)`, turns the same per-passage call into a book-level measure. Each passage is read whole with its first parent and the choices leaving it, and answers yes/no metric questions worded so that yes is good. Defaults: `vivid`, `action`, `clear`, `tight`, `tension`; a caller can pass its own (the experiment added `period`). A passage's quality `q` is the mean P(yes); the book's `baseline` is the mean of each metric and of `q`. `engine.weakest_subtrees` then ranks branches (a passage and everything reachable from it, at least three passages, disjoint, mean shrunk toward the book's mean by two passages' worth so a tiny fluke cannot win) and the tool reports each branch's mean per metric, which says *what* is weak. The scores are for choosing a branch to rewrite and seeing whether it moved, not for ranking passages or comparing books. Cost: a quarter of a second per passage, about 14 seconds for a 41-passage book.

The test book is "Jon the Messenger" (story 4): Waterloo, a courier carrying the Emperor's order to Marshal Ney across the battlefield; 41 passages of 75 words at most, a decision after every section, 22 endings of which 4 deliver the message alive (18%; a random playtest delivered 11%). The whole book is 4.4k tokens with titles and options, so it fits the 8k window as one `state`, which the passage-level calls do not need but whole-book questions do. Five whole-book yes/no questions (pace, danger, variety, choices differ, repetition) scored 0.89, 0.96, 0.88, 0.89 and 0.12 on the final text: all at the ceiling, so they separate nothing; a whole-book question is a sanity check, not a measurement.

Procedure and numbers. Baseline (first rubric): `q` 0.745; weakest metric `clear` (0.60); strongest `period` (0.86) and `tension` (0.81). Lowest branch: the hollow-road branch (3 passages, mean 0.638), weak on `clear` (0.43) and `action` (0.57). Rewrites of the lowest branch, one at a time, re-measured after each:

| Round | Rewritten | Branch mean before -> after | Book `q` |
|---|---|---|---|
| 1 | hollow road, walking past, through the hussars (concrete distances, a threat on the page) | 0.638 -> 0.736 | 0.745 -> 0.752 |
| 2 | the Guard branch, tightened | 0.691 -> 0.692 (one passage up, one down) | 0.752 |
| 2b | not a rewrite: `clear` asked about the reader's options, which endings lack, so it penalised every ending; the wording was changed to "where the reader is and what is happening" | | 0.752 -> 0.760 (new rubric, not comparable with earlier rows) |
| 3 | "In the Column", made clearer | 0.692 -> 0.722 | 0.762 |
| 4 | the drummer branch, more sensory detail | 0.711 -> out of the bottom four | 0.768 |

What it shows. The mechanism works end to end: measure, name the weakest branch and its weak metric, rewrite, re-measure, and the branch usually moves in the intended direction. Two things cut against reading more into it. The book was already uniform: subtree means ran only 0.64-0.76 around a baseline near 0.75, so the "lowest" branch was never far below average and a rewrite of any branch moves the baseline by a point or less. And the scores respond to small text changes (one passage fell 0.08 after an edit that read as tighter, one rose 0.08), so a three-passage branch mean carries a few hundredths of noise; the round-1 gain of 0.10 is probably real, the rest is within it. Improvement here is also partly circular, since the rewrites were written knowing which metric was low. A defect lint over the final book flagged nothing.

Audit of those findings (2026-10-18, same book, final text). Four results change how the numbers above should be read.
- **What "noise" is:** the model is deterministic. Asking every passage twice gave identical answers, so the "noise" above is not sampling. It is sensitivity to wording: a small edit moves a passage by up to 0.08.
- **Endings score lower:** endings averaged 0.742 and middles 0.791. Most of the gap is `tension` (0.71 against 0.90), because an ending has no next moment to fear. A branch full of endings therefore ranks low whatever its prose, and `weakest_subtrees` partly finds ending-heavy branches. Endings need their own metrics, such as whether the death was fair or the last image lands.
- **Averaging hides defects:** with every passage's sentences shuffled, `tight` and `clear` fell on 34 and 29 of 40 passages. But `q` fell only 0.763 to 0.746, less than the round-1 gain, because `vivid`, `action` and `period` judge content rather than order, and `tension` rose.
- **Metrics pull against each other:** the metrics are weakly correlated, so they do measure different things. `action` correlates negatively with `vivid`, `tension` and `period` (-0.26, -0.27, -0.34), so a rewrite for action can lower `q`. That fits round 2 not moving.

The script is `audit_jon.py` in the session scratchpad.

## Spike: per-passage defect checklist and pairwise comparison (2026-10-17)

After the route-level results above, the unit was changed in a test (`scripts/system-one-spike.py`): score one *full* passage with its parent's text as context, with four `noul` defect questions (ends on a moral, ends on a clock time, is a summary not a scene, ends on a hook), and compare two versions with a `choice` question, asked in both orders and averaged. Material: 60 sampled passages from the three current books, with defects planted programmatically (the real pre-literary-pass text of the Kite-Day book no longer exists; the books were reset and the October backups hold other stories), plus the one real before/after pair, the Sheriff ending.

- **Defect questions work.** A planted moral line was flagged on 88% of passages against 0% of untouched originals, and it did not fire on the clock closer. The clock question flagged 100% of planted and 8% of originals (some real). "Summary, not a scene" fired on the real defective Sheriff text (0.72) and not on its rewrite (0.28), against 7% of originals; that is a single real case.
- **Pairwise preference is unreliable for taste.** It preferred the original over a sentence-scrambled copy 30 of 30 times, but preferred the version *with a moral appended* 29 of 30 times, so it rewards explicitness or length. On the real Sheriff pair the two orders disagreed (0.37 and 0.96), a strong position bias that averaging only half cancels. It catches gross damage, not craft.

Consequences. Prefer named-defect `noul` questions, which need no calibration beyond "fires on planted, quiet on clean", over a quality scalar or an A/B verdict. Compare candidate rewrites by defects fired, never by which one the model "prefers". If pairwise is used at all, use it only as a regression gate against broken text. The design suggestion to gate `update_passage` or pick best-of-N on pairwise preference is withdrawn by this result; the built tool follows these consequences.

## Where a score could steer

The agent workflows already have natural gates, so scoring slots in rather than adding a loop.

- **Best-of-N at the frontier.** [Refinement Loops](refinement-loops.md) lists best-of-N as an optional lever. Draft N candidates for a frontier passage, score each with the same questions, keep the highest. This is the cheapest steering and needs no change to the agent's prompts.
- **Auditor in the refine loop.** An auditor today is a prompt role whose issues are quote-verified. A scored auditor would replace "find issues" with "is this good enough" as a `noul` or `score` question, and send only below-threshold drafts to the generative auditor that can say why. That is the escalation pattern the vendors recommend (fast first pass, frontier model for the rest).
- **Choosing which frontier to grow.** `frontier` returns passages whose choices lead nowhere. Scoring the routes that reach them, and growing the weak or the promising ones first, is steering the shape of the tree; it competes with [Act Pacing](act-pacing.md), which already owns depth and branching, so scores should reorder within a pacing act, never override it.
- **MCP agent feedback.** The [MCP authoring](mcp-authoring.md) agent could be given a `score_route` tool beside `canon_check`. Unlike `canon_check`, whose model run can pass the 120 s MCP timeout, a decision call returns fast enough to be called after every `add_passage`.

## What to ask

Questions should be bounded, one judgment each, with a written rubric, because the model scores options and cannot explain. Candidate set for a `state` of `{route_so_far, next_passage}` (the route text is what [Route Canon](route-canon.md) and the summariser already assemble per route):

| Name | Type | Intent |
|---|---|---|
| `follows` | `noul` | Does the passage follow from the previous one and the choice taken? (the failure the storyline-eval judge looks for) |
| `consistent` | `noul` | Does it contradict the route so far or the lore? (cheap pre-filter before `canon_check`) |
| `tension` / `stakes` | `score` | Ordered levels, e.g. flat / some / high, to be compared against the act's guidance |
| `beat` | `choice` | action / reveal / rest / dilemma, to check variety across a route and across sibling options (`choice` options differing in kind is already a shape rule in the MCP instructions) |
| `voice` | `score` | Reader-facing second person, tense, age register (the literary pass checks this by hand today) |

Use `noul` where the truth is binary and `score` where order matters. Put the preferred option first in every `choice`, since ties go to option order. Score *deltas* (this passage against its siblings, or against the route's running mean) rather than absolute values, because absolute calibration on prose is unknown.

## What it cannot replace

- **Route correctness.** `fact_check` proves, deterministically and with witness routes, that each `needs` is revealed on every route. A probabilistic scorer is strictly worse at that, so keep `fact_check` as the gate and use scores only for what no deterministic check sees.
- **Rationale.** A low score is a trigger; fixing it needs the generative auditor or a person. Plan the escalation path before running unattended.
- **Taste.** Whether a score tracks what a reader enjoys is the open question. Steering on a proxy that is not validated optimises the proxy: a best-of-N loop will find drafts the scorer likes, and the scorer's blind spots become the book's blind spots.

## Plan to validate before steering

1. **Pick the tag by memory.** On the 12 GB card only `clef-flash-8k` and smaller load; the base tag's 16k compute buffer does not fit (see [System One Decision Models](system-one-decision-models.md)). Scorer and author model are not resident together, and swapping costs seconds per load, which erases the latency advantage if done per call, so batch scoring between generation phases.
2. **Pin a typed client.** The local shapes are now observed (`score` returns `score`, `legend`, `probabilities`, `confidence`; `noul` returns only the probability), so mirror them in a typed client, in the `api.ts` / `models.py` discipline.
3. **Label a set.** The four frozen books (stories 1–3 plus the Hollenbeck Cup) contain edited passages and known-bad ones fixed during literary passes, as logged in `log.md` for 2026-10-10 to 2026-10-13. Score the before/after pairs where they exist and check that the scorer prefers the edit; count agreement per question. The four `clef-flash` tags share weights, so there is no quality gradient to measure between them; the comparison worth making is against the 27B `clef` or `nimble`, if pulled.
4. **Only then wire a gate**, behind a per-story setting, starting with best-of-N because a wrong score there costs a worse pick, not a blocked run. Log the scores with the run (see [Activity Log](activity-log.md)) so a steered book can be compared against an unsteered one under the same pacing plan, the way the harness arms were compared in [MCP Authoring](mcp-authoring.md).

## Open questions

Written 2026-10-17, before the experiments: whether a 9B model judges narrative quality at all, given that clef-flash trails on inference-style benchmarks (ANLI 59.1); whether `score` expectations are stable across prompt wording; whether a larger decision model (27B `clef`, `nimble`) scores prose materially better than the 9B `clef-flash`.

Partly answered on 2026-10-18. The 9B model detects named, checkable defects. It does not rank craft finely enough to steer an author that can read the book itself. Wording sensitivity is real: the model is deterministic, yet a small edit moves a passage by up to 0.08, and changing the wording of `clear` reset the baseline. A larger decision model is still untested. It would not fix the main objection, which is that the author can read the whole book.
