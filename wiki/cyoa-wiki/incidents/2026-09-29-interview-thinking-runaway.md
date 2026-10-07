# An Interview Turn That Never Answered

On 2026-09-29 the first live [Interview Me](../concepts/interview-me.md) turn hung. It was running on gemma-4 12B under `llama-server`. The model emitted Gemma 4's thinking-channel opener, `<|channel>thought`, over and over, and never reached the tool call. No token cap stopped it, and it was past 28k generated tokens, about ten minutes in, when llama-server was restarted. Two fixes in `backend/src/cyoa/llm.py` came out of it. Every call now carries a working token cap. Every call on the OpenAI-compatible backend also sets thinking off or on explicitly.

**Discovery.** The author reported that their first answer was "taking unusually long". llama-server's `/slots` showed the interview's task at temperature 0.7 with about 1k prompt tokens processed. `n_decoded` was climbing about 45 tokens a second and `n_remain` was `-1`. So this was a runaway generation, not a slow read of a large prompt. To see what it was generating, `scripts/interview_probe.py` sends the prompt of an opening turn without touching the database. The probe reproduced the runaway. Its `--raw` mode, which bypasses langchain, showed where the tokens went: the reply had empty `content`, no tool call, and a `reasoning_content` made entirely of repeated `<|channel>thought\n` openers.

**Root causes.** There were two, and either alone would have been survivable.

- **The model was never told not to think.** `chat_model()` sent `chat_template_kwargs: {"enable_thinking": true}` for reasoning auditors, and sent nothing otherwise. That left thinking to the server's launch flags. The Bonsai setup that `llm.py`'s spike notes describe ran with `--reasoning-budget 0`. The gemma server was launched without it, and under a forced tool call the model looped on the channel opener.
- **The token cap never reached the server.** No call set one. The probe's first attempt passed `max_tokens` through langchain-openai 1.6.2, which sends it as `max_completion_tokens`. This llama-server ignores that field: the slot showed `n_predict -1` and ran on past the cap. So any call could run to the context limit, 65,536 tokens on that server. It would hold the process-wide model gate from the [fan-out OOM incident](20260920-fanout-oom.md) the whole time, blocking every other agent.

**Resolution.** `chat_model()` now puts both in `extra_body`, in fields llama-server reads:

- `max_tokens` is the new `MAX_TOKENS`, `CYOA_MAX_TOKENS`, default 4096. On Ollama the same cap goes in as `num_predict`.
- `chat_template_kwargs.enable_thinking` is set to the `reasoning` flag either way.

`test_chat_model_caps_tokens_and_sets_thinking_explicitly` pins both. With the fix, the same opening turn on Ternary Bonsai 27B, served without a reasoning budget, took 2.5 s and 55 tokens and parsed cleanly. The fix has not been re-run on gemma-4 12B.

**Contributing factors.** Every earlier live run used a server launched with `--reasoning-budget 0`. The code's behaviour silently depended on that flag. The stubbed tests could not see it, because the fake model never thinks. A client that gives up does not help either: llama-server frees a slot when the client disconnects, but the backend's request had no timeout, so only a restart of the backend or of llama-server ends a runaway.

**Pattern.** This is the second time model-server configuration has leaked into behaviour: the first was `llm.py`'s structured-output method differing by backend. Settings that decide whether a call terminates belong in the request, not in how the server happened to be launched. When a live call misbehaves, read the raw response before blaming the prompt: langchain drops `reasoning_content` and tool text it cannot parse.

| Reference | What it anchors |
|---|---|
| `backend/src/cyoa/llm.py` (`MAX_TOKENS`, `chat_model`) | the cap and the explicit thinking switch |
| `scripts/interview_probe.py` (`--raw`, `--extra`) | the read-only probe that found it |
| `backend/tests/test_agents.py` (`test_chat_model_caps_tokens_and_sets_thinking_explicitly`) | the regression test |
