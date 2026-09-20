# Fan-out Exhausted the GPU

The first live run of `canon_check` on 2026-09-20 failed on every passage in its scope, and the failure is worth keeping because it is the concrete form of a risk [agentic authoring](../concepts/agentic-authoring.md) had only described: "physically it is a queue in front of one 27B model, and the budget is the real governor."

## What happened

`canon_check` maps one `check_passage` task per passage with LangGraph's `Send`. With six passages in scope, LangGraph ran all six tasks concurrently, each opened its own `ChatOllama` call, and Ollama tried to serve them in parallel by allocating a separate context per request. On a 12 GB GPU already holding the 27B model at ~9 GB, the second context did not fit: `llama-server` died with `cudaMalloc failed: out of memory … failed to allocate compute pp buffers`, and every request — including the first — came back as a 500. The workflow's per-passage error path did what it was meant to: the call returned `201` with six entries in `errors` and no proposals after 308 s of retries, rather than raising.

A second run 5 minutes later failed the same way for a different reason: the [backend](../systems/backend.md) had been started without `--reload` for the smoke test, so the fix had not loaded — and, separately, another `llama-server` on the machine had taken 10 GB of the GPU in the meantime, which left Ollama unable to load the model for even a single request. That second cause is environmental and is noted in [the dev loop](../runbooks/dev-loop.md).

## Fix

The first fix invoked the graph with `config={"max_concurrency": MAX_CONCURRENCY}`, read from `CYOA_AGENT_CONCURRENCY` with a default of **1**. Phase 5 replaced it the same day: with `stream_mode` including `"custom"`, LangGraph 1.2.11's sync `stream` sizes its thread pool to `max_concurrency` and the stream pump takes the only worker, so a run with `max_concurrency=1` never starts a task (reproduced in isolation; `max_concurrency=2` runs but then allows two model calls). Every model call now goes through `agents._ask`, which holds a process-wide `threading.Semaphore(MAX_CONCURRENCY)`. The fan-out stays logical — one `Send` per passage, results reduced into `proposals` — but the model calls are a queue, and the queue also spans concurrent runs and the synchronous verbs, which `max_concurrency` never would have. Raising the value is a deliberate decision for a server known to batch, not a tuning knob. (Later the same day runs stopped using the `custom` stream mode altogether — nodes report through a per-run handle in their config — so the deadlock can no longer occur, but the semaphore stays for the cross-run reason.)

## What it settles for phase 5

`expand_frontier` uses the same `Send` pattern with far larger scopes, and this run shows that the gate is the *only* thing standing between it and taking the model down. It is a process setting rather than a per-run budget entry because it protects the machine, not the story, and one run must not be able to raise it against another. A stubbed model in the test suite cannot exhaust anything, so the gate is not under test; the activity events, emitted only after the semaphore is acquired, are what makes its effect visible in the UI — one card lights at a time. The concurrency the gate does *not* remove — `Send` tasks still run in parallel up to the model call — bit once more in the cleanup: the run handle's progress counter lost ticks until it took a lock.

| Reference | Role |
|-----------|------|
| `backend/src/cyoa/agents.py` | `MAX_CONCURRENCY`, `_GATE`, `_ask` |
| `concepts/agentic-authoring.md` | the budget-as-governor decision this confirms |
