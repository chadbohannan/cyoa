# Refinement Loops

This page scopes the next phase of [agentic authoring](agentic-authoring.md). Every content-generation step becomes a loop: draft, validate, audit, revise, and repeat until the auditors pass or a round limit ends it. It is a design (scoped 2026-09-28, not built). **Story quality is the objective, and wall-clock time is a guard, not a cost to minimise** (owner, 2026-09-28): a run must not take unbounded time, but a slower run that writes a better story is the right trade. Every default below follows from that. It is the owner's stated intent that review and audit stages follow *every* generation step, not only the frontier, and this page turns that intent into one mechanism with a data model, knobs, costs and a build order.

## What exists already

Three pieces of the loop are already built, each in isolation:

- **Validation loops.** The verb graph's `generate → parse_validate → generate` edge (up to `MAX_ATTEMPTS = 3`) and, since 2026-09-28, `_ask_until` in `grow`. Both are *code* checks: schema failures, empty or `...` placeholder text, duplicate choices. Each retry is told why the last answer was refused. Retries are counted on the run (`progress.retries`) and logged as `retry` events, so the [storyline eval](../runbooks/storyline-eval.md) reports them by reason.
- **An auditor with a fix.** `canon_check` asks the model for structured issues (`claim`, `conflicts_with`, `severity`) against lore (and the timeline, until its removal on 2026-09-29), plus a `fixed_body`. It files the issues as a `note` and the fix as an `edit_passage` proposal. Its open problem, a fix that is never re-checked (the "Quality at scale" question on the agentic-authoring page), is exactly what a loop closes.
- **A continuity judge.** The eval harness's `--judge` rubric (contradictions, ignored choices, unintroduced references, person and tense) graded whole storylines after the fact. As an in-loop auditor it would run per step, against the spine that `gather_context` now provides.

The refinement phase generalises these three into one engine rather than adding a fourth pattern.

## The loop

```
draft ─▶ validate (code) ─▶ audit (model, per auditor) ─▶ decide ─▶ accept
            │ fail                                        │ revise
            └──────── regenerate with reason ◀────────────┴─▶ revise (model) ─▶ validate ─▶ audit ...
```

**Validate before audit, always.** Code checks are free and settle the failures a model call would be wasted on. A validation failure *regenerates* with the reason, which is today's retry. An audit finding *revises* instead: a revise role gets the draft plus the verified issues and must fix those issues and change nothing else. Regenerating would throw away the parts that were fine and invite new problems. That makes revision the step that converges.

**Auditors are prompt roles.** Each auditor is a role in `prompts.py`, which keeps the global-roles decision on the agentic-authoring page: one text per role, editable in the Agents view, snapshotted per run. Every auditor returns the same schema:

```
Audit { verdict: "pass" | "revise",
        issues: [{ severity: "minor"|"major", problem, quote, evidence }] }
```

`quote` must be a verbatim span of the draft. `evidence` is a verbatim span of the spine or WORLD that the draft contradicts (TIMELINE too, until 2026-09-29), or empty for craft issues. **Code verifies both quotes by normalised substring match and drops any issue whose quote isn't found.** That is the main defence against the obvious weakness, a model auditing its own output and inventing problems. An issue anchored to text that exists can be checked mechanically. One that isn't anchored is noise.

**Starting auditors:**

| auditor | checks | applies to |
|---|---|---|
| `audit_continuity` | contradicts the spine; ignores the choice that led here; introduces people, objects or knowledge the reader hasn't earned; wrong person or tense | passages (frontier, `draft_body`, `draft_target`, `rewrite`, `suggest_next` continue) |
| `audit_choices` | choices that are near-duplicates, don't follow from the passage, aren't actions, or resolve the central goal early | choice sets (frontier, `suggest_choices`, `suggest_next` branch) |
| `audit_canon` | today's `canon_check` rubric (lore) | any passage in a story that has lore; re-audits every `canon_check` fix |

Each is one call, and every applicable auditor runs on every step. Auditors are not merged into a combined rubric, because that would append roles to each other, which the roles decision forbids. It would also make it impossible to tell which check is catching what. More auditors are expected once R1 shows what the first three miss: prose craft (repetition, clichés, telling instead of showing) and pacing (resolving the central goal too early, which the first eval saw at depth 2). [Storyline generation quality](../syntheses/storyline-generation-quality.md) already has concrete targets from run 2 of the eval, ahead of R1: in-passage physical sequencing slips, bodies that narrate the reader's remaining choices as prose, and passage titles duplicated across branches (the last one is proposed there as a code-level check rather than an auditor, since it's mechanically detectable). A fourth eval run against a different model (Gemma-4 12B in place of Qwen 3.8 27B, same code) found a fifth candidate for a code check rather than an auditor — a literal `\n` escape sequence leaking into passage prose — and confirmed that retry rate and tree width vary by model while the zero-stub, every-storyline-ends guarantees held across the swap; R1's auditors should be scoped against craft issues that survive across models, not ones specific to whichever model happened to generate the training examples for the rubric.

**The loop, and what "done" means.** A passage is either in its refinement loop or it is not. The loop drafts, then audits and revises at most `rounds` times (default 3, simplified 2026-09-30):

```
draft = write()
for _ in range(rounds):       # rounds = the most revisions allowed
    notes = audit(draft)      # every verified issue is a note, whatever its severity
    if not notes: break
    draft = revise(draft, notes)
return draft
```

Leaving the loop for any reason (a clean audit, the last revision, a revision that cannot be produced, a cancel) is all that "done" means. What comes out is the passage, and the story grows from it like any other. Nothing is marked and nothing is queued. Every audit's notes get acted on, and the last revision is not audited again.

This replaced a design with more moving parts. The original R1 stopped early on a plateau, restarted from a fresh draft up to `restarts` times, kept the best draft seen, blocked only on issues at or above a `block` severity, and ended with a policy (`keep_flagged` or `drop`) that wrote the passage with a `meta.audit` flag. A 2026-09-30 detour (`hold`: write it flagged but do not grow from it) made the flag load-bearing. Both were removed the same day. The flag was written and never read by the workflow, only by the UI; it went stale when the author edited a passage, was lost when a flagged proposal was accepted, and was bypassed by "Expand from here". A passage that is still being refined is in the loop, and one that is not is done, so there is no third state to track. The `audits` table stays as a log of each round's notes for the author to read, and nothing reads it to decide anything. `meta.audit` values already in a database are inert.

**Auditors must be able to pass.** With no flag to soften a failure, an auditor that never passes just spends every revision on every passage. The continuity auditor originally flagged anything "the reader has not yet earned", which a new scene cannot avoid, and the 2026-09-30 autogen returned "revise" on 78 of 85 audits (224 of 254 issues rated major), including "Your name is Lina" as a shift to first person. Its prompt now reports only a contradiction of a specific line of the story so far or the world, says that a merely new detail is never an issue, and says that addressing the reader as "you" is correct. `_verify_issue` requires evidence for every issue except a tense or person slip; before, 88 of 304 kept issues had none.

**Notes can come from code as well as models.** A cheap code check (a title already used, a body that copies its parent, a literal `\n`, a slip out of second person) can contribute notes to the same loop and be revised the same way. That is where the repeat-draft guard from the autogen analysis belongs. Not yet built.

**Quality levers that time now allows.** Each is a policy knob and an arm of the R1 comparison, not an assumption:
- **Best-of-N drafts.** Write `candidates` drafts (default 1; try 3), audit each, and refine the one with the fewest issues. This matters most for choice sets, because the choices are the story's skeleton and every later passage inherits them. A fast typed scorer ([Story Quality Scoring](story-quality-scoring.md)) could pick the draft instead of counting audit issues.
- **Audit ensembles.** Run each auditor `votes` times at a moderate temperature and keep the issues found in a majority. Voting cuts both invented issues and missed ones, working alongside quote verification.
- **Reasoning on for auditors.** Reasoning is off everywhere because it made calls about 170× slower (the structured-output spike on the agentic-authoring page). An auditor is where thinking pays off, so `audit_reasoning` turns it back on for audit calls only. On llama-server that is the chat template's thinking switch. It needs its own check that structured output still parses with thinking on.
- **A stronger audit model.** `audit_model` can name a bigger, slower local model than the drafting one. Auditing needs judgement more than fluency, and slowness no longer counts against it.
- **Path audits.** After a leaf is written, audit the whole storyline from the start with the continuity rubric. Fixes land on the newest passage only: ancestors are shared by every branch below them, so the leaf has to accommodate its past rather than rewrite it. Revising an ancestor would mean re-auditing its whole subtree.

## Where it plugs in

**One engine, every step.** `_ask_until` grows into `refine(step, ctx, params)`. A `Step` names the output schema, the draft role, the revise role, the code validators and the auditors. `grow` calls it for the choice set and again for each passage. The verb graph's `generate`/`parse_validate` retry edge collapses into a single `generate` node that calls `refine`. `canon_check`'s fix goes through `refine` with `audit_canon` as its auditor. Human-triggered verbs still end in proposals when the target is reviewed. Their audit trail is attached to the proposal, so the author can see what the auditors caught and what the revise step changed.

**A function inside the node, not a LangGraph subgraph (recommended).** Each generation step stays one node, so `Send` fan-out, checkpoints, cancel checks and the call gate don't change. A crash mid-loop loses one step's rounds, which the audit table (below) still records. A subgraph would give a checkpoint per round and draw the loop in the Agents view, but it would make each fan-out task a nested graph, and nothing here needs to resume mid-step. The Agents view can show the stages from metadata instead: `NODE_PROMPTS` becomes per-node draft, revise and auditor roles, and the node inspector lists them. Revisit this if a single step ever grows long enough that resuming it matters.

## Data

```
audits  id, story_id, run_id NULL, proposal_id NULL, passage_id NULL,
        step TEXT,          -- 'frontier_passage', 'frontier_choices', 'draft_body', 'canon_fix', …
        round INTEGER, auditor TEXT, verdict TEXT,
        issues JSON,        -- verified issues only; `dropped` counts the unverifiable ones
        draft JSON, created_at
```

The table is the telemetry. It records first-pass rate per auditor, rounds per step, which issues recur, and how many issues were dropped as unverifiable (a measure of how much the auditor invents). Passages written with issues still open get `meta.audit = {rounds, verdict: "unresolved", issues}`. `problems` gains `flagged` (those passage ids), so the map highlights them the same way it highlights dangling links today. The table is append-only and keyed to the passage, so deleting a story cascades to it.

## Knobs

A run's `policy` gains a `refine` block, next to `skip_review_interrupt`:

```
refine: { rounds: 3, votes: 1, audit_reasoning: false,
          auditors: { passage: ["audit_continuity", "audit_canon"], choices: ["audit_choices"] },
          audit_model: null }
```

`rounds: 0` skips the loop and gives today's behaviour, which is what makes the comparison below possible. The original design also had `restarts`, `block`, `on_unresolved`, `candidates` and `path_audit`; all were removed 2026-09-30, and `candidates` and `path_audit` return with R2 if they are built. `votes`, `audit_reasoning` and `audit_model` start off so that each can be switched on as one comparison arm and its effect seen on its own. Human-triggered verbs use the module default until the Inspector exposes the setting. The first draft of this plan also had an `audit_rate` for auditing only a sample of steps. That's gone: sampling gives up quality to save time.

## Time

On this machine, llama-server with Qwen 3.8 27B (Bonsai-2 ternary quantization) and one slot takes 10–20 s per call. Refinement multiplies that: 3–10× per step depending on pass rate, candidates, votes and reasoning, and more with reasoning on. A 20-passage story that takes 10–20 minutes today could take hours. That is accepted. What is not accepted is time without a bound, so three guards stay:
1. The per-step ceiling `rounds`.
2. The run's existing `max_seconds`, which the eval harness raises for refined runs. It stays a hard stop.
3. Every refinement stage checks for cancellation, as `grow` already does between calls, so a run can always be stopped.

`progress` reports rounds alongside `retries`, so a slow run shows where its time is going.

**2026-09-28: fanned out, not just gated.** Until this date, every call in the loop — one choice's draft, then the next, then each auditor in a round, then each vote — ran through a plain sequential Python loop, so raising `CYOA_AGENT_CONCURRENCY` bought nothing beyond the frontier-passage level (the one layer LangGraph's own `Send` already parallelizes). `agents._fan`, a small `ThreadPoolExecutor`-based helper, now fans out `grow()`'s per-choice drafting, `refine()`'s per-round auditors, and `_run_auditor`'s per-vote calls, still bounded by the same `_GATE` semaphore — see [the fan-out OOM incident](../incidents/20260920-fanout-oom.md)'s 2026-09-28 update for why a `ThreadPoolExecutor` rather than more `Send` nodes, and the `contextvars`-copying it needed to keep `_run()` working inside a worker thread. A same-day comparison against a load-balancing proxy (`http://localhost:4000/v1`, `CYOA_AGENT_CONCURRENCY=10`) versus the serial gate-of-1 path, same `depth: 3, branching: 3, refine.rounds: 2` policy, three premises: 427/1410/287s concurrent versus 347/1851/187s serial — a real but modest ~11% total improvement (2124s vs 2385s), not the large win the pool size would suggest. The likely reason: `/v1/models` and a direct completion both traced the proxy back to the same single `gemma-4-12B` instance already in use, so the pool behind it may only have one live node today, not the full 1–10 the proxy is designed for — the proxy's own queue then serializes requests to that one node regardless of how many `_GATE` permits at once. The refactor's correctness is independently verified (two new tests use a `threading.Barrier` to prove auditor and per-choice calls actually overlap when the gate is raised, not just that they eventually both complete); the wall-clock payoff should scale once more nodes are live behind the proxy.

## Measuring it

The [storyline eval](../runbooks/storyline-eval.md) is how this phase gets judged:
- The harness gains a `--policy` JSON argument, so the same premises run with `rounds: 0` and `rounds: 2` in one command.
- Each report gains the audit statistics: first-pass rate, rounds, dropped counts, and top issue types.
- The comparison that matters is whether refined storylines read better in a human read, not whether the auditors grade them higher.
- With time cheap, the eval can afford what quality measurement needs: several runs per arm (a single run per premise is one sample from a noisy generator), deeper budgets where drift shows, and more premises.

**The eval judge must not be the in-loop auditor.** If it uses the same rubric and model, the loop learns to satisfy its own grader and the eval measures self-agreement (Goodhart's law). The harness keeps its own rubric and should use a different `audit_model` when one is available, and a human read stays the ground truth.

## Build order

- **R0, collecting now.** Baseline with retries only: retry counts by reason, stub and error rates, and a human read of each storyline (eval runs `20260928-143623` without retries, `20260928-152815` with them).
- **R1, built 2026-09-28.** Core loop on `frontier_passage` and `frontier_ending` (endings share the same call site in `grow`, so they went in too): the `refine()` engine (agents.py), the `audits` table (db.py), `audit_continuity`/`audit_canon`/`revise_passage` roles (prompts.py), and quote/evidence verification (`_verify_issue`, `_norm`, `_fingerprint`). `refine` is on by default — `DEFAULT_REFINE["rounds"]` is 3 since the 2026-09-30 simplification (it shipped as 2); `policy.refine.rounds: 0` reproduces exactly the pre-R1 code path (the eval's `rounds: 0` baseline arm). The harness gained `--policy` (JSON merged into the run's policy) and `--repeat` (rerun every premise N times per arm) in `scripts/eval_storylines.py`, plus an `audit_stats()` reader that queries the `audits` table directly from the private backend's sqlite file, and `index.md`/each report gained `unresolved`/`dropped` columns and an "Audit stats" section. A smoke test at depth 2/branching 2 against Gemma-4 12B confirmed the loop runs end to end: `audit_continuity` caught real unearned-detail issues (new characters, objects and events invented mid-draft) at a first-pass rate of 0.09, quote verification dropped 5 of them as unverifiable, and the run still converged within the round ceiling — costing about 3.4x the wall-clock of the `rounds: 0` baseline on the same premise (124s vs. 36s). `candidates`, `path_audit` and `audit_choices`/`revise_choices` are still R2; the reviewed/proposal path writes no audit marker at all since 2026-09-30.
- **R2.** Choices: `audit_choices`, `revise_choices` and `candidates: 3` on the frontier. `path_audit`. The verbs move onto `refine`, with audits attached to proposals. The `canon_check` fix gets re-audited. `problems.flagged`.
- **R3.** Auditors for prose craft and pacing, chosen from what R1 and R2's human reads found the existing auditors missing. Since [act pacing](act-pacing.md) landed (2026-09-29), a pacing auditor has a concrete target to check a draft against: the current act's guidance and `{act_remaining}`.
- **R4.** UI: an audit trail in the passage inspector, rounds and retries in the run dock, the stages of each node in the Agents view, and the auditor roles in the prompt editor. **Partially done 2026-09-29, then trimmed 2026-09-30** (see [the frontend](../systems/frontend.md)): `describeProgress` appends retries and audit rounds to the run dock's line, and the Agents view and prompt editor needed no work — both are data-driven off the backend's `NODE_PROMPTS`/`PROMPTS`, so the three new roles appeared for free. The passage inspector's and proposal panel's audit flag and the map's "refinement flagged this passage" badge were removed with the flag itself. "The stages of each node in the Agents view" gets its real answer from [act pacing](act-pacing.md)'s per-level loop, which draws `grow` expanded into choices → passage → audits → revise from a step list the backend publishes. Still open: a proper `/audits` endpoint so the UI can show round-by-round history from the `audits` table.

The UI moves last because it doesn't change what gets written. The eval reports are how the author judges quality until then.

This phase comes before [choice anchoring](choice-anchoring.md) because it is the part of anchoring's promise that doesn't need a schema change. The spine gives auditors what to check against today. Anchors would later give `audit_continuity` explicit world-facts to hold branches to, instead of prose it has to infer them from. [Storyline generation quality](../syntheses/storyline-generation-quality.md) draws this same distinction from the eval data: the spine fixed the *reading* half of anchoring's promise (facts now carry to depth 3 without contradiction) while leaving the *structural* half — choices declaring what they're anchored to — as what anchoring alone would still add.

## Open decisions

The defaults above should be confirmed before R1:
- A function inside the node, or a subgraph.
- A ceiling of 3 revisions (decided 2026-09-30).
- Audits on human-triggered verbs.

The largest unknown is empirical: whether a 2-bit 27B model can audit its own output well enough that revision improves storylines rather than churning them. R1 answers that. If it can't, the likely fix is the `audit_model` lever, and time no longer rules out a slower model.
