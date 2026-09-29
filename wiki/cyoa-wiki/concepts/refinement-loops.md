# Refinement Loops

This page scopes the next phase of [agentic authoring](agentic-authoring.md). Every content-generation step becomes a loop: draft, validate, audit, revise, and repeat until the auditors pass or a round limit ends it. It is a design (scoped 2026-09-28, not built). **Story quality is the objective, and wall-clock time is a guard, not a cost to minimise** (owner, 2026-09-28): a run must not take unbounded time, but a slower run that writes a better story is the right trade. Every default below follows from that. It is the owner's stated intent that review and audit stages follow *every* generation step, not only the frontier, and this page turns that intent into one mechanism with a data model, knobs, costs and a build order.

## What exists already

Three pieces of the loop are already built, each in isolation:

- **Validation loops.** The verb graph's `generate → parse_validate → generate` edge (up to `MAX_ATTEMPTS = 3`) and, since 2026-09-28, `_ask_until` in `grow`. Both are *code* checks: schema failures, empty or `...` placeholder text, duplicate choices. Each retry is told why the last answer was refused. Retries are counted on the run (`progress.retries`) and logged as `retry` events, so the [storyline eval](../runbooks/storyline-eval.md) reports them by reason.
- **An auditor with a fix.** `canon_check` asks the model for structured issues (`claim`, `conflicts_with`, `severity`) against lore and timeline, plus a `fixed_body`. It files the issues as a `note` and the fix as an `edit_passage` proposal. Its open problem, a fix that is never re-checked (the "Quality at scale" question on the agentic-authoring page), is exactly what a loop closes.
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

`quote` must be a verbatim span of the draft. `evidence` is a verbatim span of the spine, WORLD or TIMELINE that the draft contradicts, or empty for craft issues. **Code verifies both quotes by normalised substring match and drops any issue whose quote isn't found.** That is the main defence against the obvious weakness, a model auditing its own output and inventing problems. An issue anchored to text that exists can be checked mechanically. One that isn't anchored is noise.

**Starting auditors:**

| auditor | checks | applies to |
|---|---|---|
| `audit_continuity` | contradicts the spine; ignores the choice that led here; introduces people, objects or knowledge the reader hasn't earned; wrong person or tense | passages (frontier, `draft_body`, `draft_target`, `rewrite`, `suggest_next` continue) |
| `audit_choices` | choices that are near-duplicates, don't follow from the passage, aren't actions, or resolve the central goal early | choice sets (frontier, `suggest_choices`, `suggest_next` branch) |
| `audit_canon` | today's `canon_check` rubric (lore and timeline) | any passage in a story that has lore or events; re-audits every `canon_check` fix |

Each is one call, and every applicable auditor runs on every step. Auditors are not merged into a combined rubric, because that would append roles to each other, which the roles decision forbids. It would also make it impossible to tell which check is catching what. More auditors are expected once R1 shows what the first three miss: prose craft (repetition, clichés, telling instead of showing) and pacing (resolving the central goal too early, which the first eval saw at depth 2). [Storyline generation quality](../syntheses/storyline-generation-quality.md) already has concrete targets from run 2 of the eval, ahead of R1: in-passage physical sequencing slips, bodies that narrate the reader's remaining choices as prose, and passage titles duplicated across branches (the last one is proposed there as a code-level check rather than an auditor, since it's mechanically detectable). A fourth eval run against a different model (Gemma-4 12B in place of Qwen 3.8 27B, same code) found a fifth candidate for a code check rather than an auditor — a literal `\n` escape sequence leaking into passage prose — and confirmed that retry rate and tree width vary by model while the zero-stub, every-storyline-ends guarantees held across the swap; R1's auditors should be scoped against craft issues that survive across models, not ones specific to whichever model happened to generate the training examples for the rubric.

**Decide: converge, don't count.** A round passes when no verified issue remains at or above `block`. Because quality is the objective, the default for `block` is `minor`: every verified issue is worth a revision. Rounds continue while the draft improves. The loop stops on:
- **pass;**
- **plateau:** two rounds in a row where the best draft's (major, minor) count didn't go down, which catches both churn and fixes that introduce new problems;
- **the ceiling:** `rounds` (default 6). This is only an infinite-loop guard, not a cost limit.

Each issue gets a fingerprint (normalised quote + problem), so the plateau test can tell a new issue from one that came back. The draft kept is the best seen by (major count, minor count), not the last one.

**Escalate before giving up.** When revision plateaus with issues still open, the draft may be the wrong one, not merely a flawed one. So the loop throws it away and starts again from a fresh draft, up to `restarts` times (default 2), each one refined the same way. Only when every restart has plateaued does the policy decide: `keep_flagged` (default) writes the best draft seen across all attempts and marks it, and `drop` skips it the way a failed choice is skipped today. Validation failures are never kept, because a placeholder is not content.

**Quality levers that time now allows.** Each is a policy knob and an arm of the R1 comparison, not an assumption:
- **Best-of-N drafts.** Write `candidates` drafts (default 1; try 3), audit each, and refine the one with the fewest issues. This matters most for choice sets, because the choices are the story's skeleton and every later passage inherits them.
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
refine: { rounds: 6, restarts: 2, block: "minor", on_unresolved: "keep_flagged" | "drop",
          candidates: 1, votes: 1, audit_reasoning: false, path_audit: false,
          auditors: { passage: ["audit_continuity", "audit_canon"], choices: ["audit_choices"] },
          audit_model: null }
```

`rounds: 0` gives today's behaviour, which is what makes the comparison below possible. The defaults above are this page's original design target; R1 shipped with `rounds: 2` instead of 6 (`agents.DEFAULT_REFINE`) as a shorter leash until a comparison run justifies the full ceiling — everything else above matches. `candidates`, `votes`, `audit_reasoning`, `audit_model` and `path_audit` start off so that each can be switched on as one comparison arm and its effect seen on its own. Human-triggered verbs use the module default until the Inspector exposes the setting. The first draft of this plan also had an `audit_rate` for auditing only a sample of steps. That's gone: sampling gives up quality to save time.

## Time

On this machine, llama-server with Qwen 3.8 27B (Bonsai-2 ternary quantization) and one slot takes 10–20 s per call. Refinement multiplies that: 3–10× per step depending on pass rate, candidates, votes and reasoning, and more with reasoning on. A 20-passage story that takes 10–20 minutes today could take hours. That is accepted. What is not accepted is time without a bound, so three guards stay:
1. The per-step ceilings `rounds` and `restarts`.
2. The run's existing `max_seconds`, which the eval harness raises for refined runs. It stays a hard stop.
3. Every refinement stage checks for cancellation, as `grow` already does between calls, so a run can always be stopped.

`progress` reports rounds and restarts alongside `retries`, so a slow run shows where its time is going.

**2026-09-28: fanned out, not just gated.** Until this date, every call in the loop — one choice's draft, then the next, then each auditor in a round, then each vote — ran through a plain sequential Python loop, so raising `CYOA_AGENT_CONCURRENCY` bought nothing beyond the frontier-passage level (the one layer LangGraph's own `Send` already parallelizes). `agents._fan`, a small `ThreadPoolExecutor`-based helper, now fans out `grow()`'s per-choice drafting, `refine()`'s per-round auditors, and `_run_auditor`'s per-vote calls, still bounded by the same `_GATE` semaphore — see [the fan-out OOM incident](../incidents/20260920-fanout-oom.md)'s 2026-09-28 update for why a `ThreadPoolExecutor` rather than more `Send` nodes, and the `contextvars`-copying it needed to keep `_run()` working inside a worker thread. A same-day comparison against a load-balancing proxy (`http://localhost:4000/v1`, `CYOA_AGENT_CONCURRENCY=10`) versus the serial gate-of-1 path, same `depth: 3, branching: 3, refine.rounds: 2` policy, three premises: 427/1410/287s concurrent versus 347/1851/187s serial — a real but modest ~11% total improvement (2124s vs 2385s), not the large win the pool size would suggest. The likely reason: `/v1/models` and a direct completion both traced the proxy back to the same single `gemma-4-12B` instance already in use, so the pool behind it may only have one live node today, not the full 1–10 the proxy is designed for — the proxy's own queue then serializes requests to that one node regardless of how many `_GATE` permits at once. The refactor's correctness is independently verified (two new tests use a `threading.Barrier` to prove auditor and per-choice calls actually overlap when the gate is raised, not just that they eventually both complete); the wall-clock payoff should scale once more nodes are live behind the proxy.

## Measuring it

The [storyline eval](../runbooks/storyline-eval.md) is how this phase gets judged:
- The harness gains a `--policy` JSON argument, so the same premises run with `rounds: 0` and `rounds: 2` in one command.
- Each report gains the audit statistics: first-pass rate, rounds, unresolved and dropped counts, and top issue types.
- The comparison that matters is whether refined storylines read better in a human read, not whether the auditors grade them higher.
- With time cheap, the eval can afford what quality measurement needs: several runs per arm (a single run per premise is one sample from a noisy generator), deeper budgets where drift shows, and more premises.

**The eval judge must not be the in-loop auditor.** If it uses the same rubric and model, the loop learns to satisfy its own grader and the eval measures self-agreement (Goodhart's law). The harness keeps its own rubric and should use a different `audit_model` when one is available, and a human read stays the ground truth.

## Build order

- **R0, collecting now.** Baseline with retries only: retry counts by reason, stub and error rates, and a human read of each storyline (eval runs `20260928-143623` without retries, `20260928-152815` with them).
- **R1, built 2026-09-28.** Core loop on `frontier_passage` and `frontier_ending` (endings share the same call site in `grow`, so they went in too): the `refine()` engine (agents.py), the `audits` table (db.py), `audit_continuity`/`audit_canon`/`revise_passage` roles (prompts.py), and quote/evidence verification (`_verify_issue`, `_norm`, `_fingerprint`). `refine` is on by default — `DEFAULT_REFINE["rounds"] = 2`, a shorter leash than this page's original stated default of 6, kept until the comparison below validates the added latency is worth it; `policy.refine.rounds: 0` reproduces exactly the pre-R1 code path (the eval's `rounds: 0` baseline arm). The harness gained `--policy` (JSON merged into the run's policy) and `--repeat` (rerun every premise N times per arm) in `scripts/eval_storylines.py`, plus an `audit_stats()` reader that queries the `audits` table directly from the private backend's sqlite file, and `index.md`/each report gained `unresolved`/`dropped` columns and an "Audit stats" section. A smoke test at depth 2/branching 2 against Gemma-4 12B confirmed the loop runs end to end: `audit_continuity` caught real unearned-detail issues (new characters, objects and events invented mid-draft) at a first-pass rate of 0.09, quote verification dropped 5 of them as unverifiable, and the run still converged to 0 unresolved passages within the round/restart ceiling — costing about 3.4x the wall-clock of the `rounds: 0` baseline on the same premise (124s vs. 36s). `candidates`, `path_audit` and `audit_choices`/`revise_choices` are still R2; the reviewed/proposal path's audit trail is a `payload["audit"]` placeholder, not yet a first-class field.
- **R2.** Choices: `audit_choices`, `revise_choices` and `candidates: 3` on the frontier. `path_audit`. The verbs move onto `refine`, with audits attached to proposals. The `canon_check` fix gets re-audited. `problems.flagged`.
- **R3.** Auditors for prose craft and pacing, chosen from what R1 and R2's human reads found the existing auditors missing. Since [act pacing](act-pacing.md) landed (2026-09-29), a pacing auditor has a concrete target to check a draft against: the current act's guidance and `{act_remaining}`.
- **R4.** UI: an audit trail in the passage inspector, flagged highlights on the map, rounds, restarts and retries in the run dock, the stages of each node in the Agents view, and the auditor roles in the prompt editor. **Partially done 2026-09-29** (see [the frontend](../systems/frontend.md)): the passage inspector and proposal panel both render `meta.audit`/`payload.audit` via a shared `AuditFlag` component, the map badges an unresolved passage the same way it already badges a dangling choice or a canon note, and `describeProgress` now appends retries/rounds/restarts to the run dock's line. The Agents view and prompt editor needed no work — both are already data-driven off the backend's `NODE_PROMPTS`/`PROMPTS`, so the three new roles appeared for free. "The stages of each node in the Agents view" gets its real answer from [act pacing](act-pacing.md)'s per-level loop. That loop draws `grow` expanded into choices → passage → audits → revise, from a step list the backend publishes. Still open: a proper `/audits` endpoint for round-by-round history (today's UI only ever sees the final flagged-or-not state baked into `meta`/`payload`, per the R1 design's own telemetry table) and `problems.flagged` (R2) for map-level flagging that doesn't depend on reading `meta` client-side.

The UI moves last because it doesn't change what gets written. The eval reports are how the author judges quality until then.

This phase comes before [choice anchoring](choice-anchoring.md) because it is the part of anchoring's promise that doesn't need a schema change. The spine gives auditors what to check against today. Anchors would later give `audit_continuity` explicit world-facts to hold branches to, instead of prose it has to infer them from. [Storyline generation quality](../syntheses/storyline-generation-quality.md) draws this same distinction from the eval data: the spine fixed the *reading* half of anchoring's promise (facts now carry to depth 3 without contradiction) while leaving the *structural* half — choices declaring what they're anchored to — as what anchoring alone would still add.

## Open decisions

The defaults above should be confirmed before R1:
- A function inside the node, or a subgraph.
- `keep_flagged` or `drop` after restarts.
- `block: minor`.
- A ceiling of 6 rounds and 2 restarts.
- Audits on human-triggered verbs.

The largest unknown is empirical: whether a 2-bit 27B model can audit its own output well enough that revision improves storylines rather than churning them. R1 answers that. If it can't, the likely fix is the `audit_model` lever, and time no longer rules out a slower model.
