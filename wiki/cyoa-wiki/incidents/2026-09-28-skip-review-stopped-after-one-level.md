# Unattended Runs Stopped After One Level

From phase 7b (2026-09-22) until 2026-09-28, an `expand_frontier` run with the `skip_review_interrupt` policy grew exactly one level and then reported `done`, whatever `depth` its budget allowed. The phase-7b change had wired the policy into `after_gather` in `backend/src/cyoa/agents.py` as `return "finish"`. That skipped the review pause, but it also skipped the level advance, because the level only moves on inside `review`. Unattended runs, which are the "automated" end of the handoff spectrum described in [agentic authoring](../concepts/agentic-authoring.md), were therefore silently shallow.

**Discovery.** The bug was found while reading the workflow's routing to build the [storyline eval harness](../runbooks/storyline-eval.md). The harness depends on the policy to grow multi-level stories without a human present. No test had exercised the policy, and the phase-7b log entry says only that the policy "skips review interrupt", which is true.

**Resolution.** `after_gather` no longer inspects the policy. `review` checks it first: under `skip_review_interrupt` it returns `{"level": level + 1}` without calling `interrupt`, and `after_review` then routes back to `plan` as it does after a human resume. The budget checks in `after_gather` (max_passages, depth, deadline) still end the run. `test_expand_frontier_skip_review_runs_every_level` in `backend/tests/test_agents.py` pins the behaviour: with depth 2 and six passages of budget, the run ends `done` with all six created.

**Pattern.** This is a routing change that type-checks and never errors: the graph still reaches `END`, just early. Conditional edges in the LangGraph workflows need a test that asserts *how far* a run got, not only that it finished.
