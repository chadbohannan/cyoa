# System One Decision Models (Jev, Clef)

A System One model is a language model that does not write text. It reads a `state` and a set of typed questions and returns, for each question, a value drawn from a set the caller defined in advance, with probabilities. The class was introduced by TypeSafe's Jev (announced 2026-09-15) and reached local hardware when Ollama 0.35.0 added `POST /v1/systemone` on 2026-09-29. The model installed here, `clef-flash` and its siblings, is Cloudflare's entry in the class. The practical reason to care for CYOA is that the output is always well-formed: there is no JSON to parse, repair or retry, which is the failure the project has fought in every free-text judge ([Storyline Generation Quality](../syntheses/storyline-generation-quality.md), [Evaluating Generated Storylines](../runbooks/storyline-eval.md)). How this could steer an authoring agent is scoped in [Story Quality Scoring](story-quality-scoring.md). Everything below is from the research in `20261017-system-one-research.md` (vendor claims unless marked local).

## What makes it different

A conventional LLM generates tokens one at a time and the caller parses the string. A System One model scores every option of every question jointly in one non-autoregressive pass (Cloudflare's wording for clef-flash; the DataCamp write-up describes Jev's "parallel sampler" the same way and names TypeSafe's training method RLCD, optimising for calibrated probabilities). The consequences, all in the sources:

- **Type-safe by construction.** An answer is an option key, a probability, or a level on a scale you wrote, so an off-schema reply cannot occur. TypeSafe reports 0% structured-output error against 0.6–45% for LLMs; that is a vendor figure, but the mechanism makes it close to a tautology. What is *not* guaranteed is that the answer is right.
- **Probabilities, not prose.** Every answer carries probabilities, so a caller can threshold, rank or average instead of reading a verdict.
- **Fast and cheap.** Median 38.8 ms for clef-flash in Cloudflare's runs, 91 ms per decision for Nimble 9B on an M5 Max, against seconds for an LLM judge. Many questions about one `state` share one call (up to 64).
- **No rationale.** It says *which*, never *why*. A flagged passage still needs a reader or a generative model to say what is wrong.

## The wire format

Request to `POST http://localhost:11434/v1/systemone` (no key locally): `model`, `state` (string or JSON object, required), optional `images` (base64 PNG/JPEG/WebP), optional `keep_alive`, and `questions`, an object keyed by question name (ASCII letters, digits, `_ . -`, up to 100 characters), each `{type, instructions, criteria}`. The user's `~/workspace/chat/clef.py` is a working client and the reference for the shapes.

| Type | `criteria` | Returns |
|---|---|---|
| `choice` | 2–26 options, key → description (list the preferred option first, because ties go to option order) | `choice`, `probabilities` per option, `confidence` |
| `noul` | none; may relabel `"false"` / `"true"` | `noul`, the probability of true (0–1) |
| `score` | ordered list of level labels, worst → best (the docs page says 2–10, the library page 2–26) | `score`, the probability-weighted mean of zero-based level indices, plus `legend`, `probabilities`, `confidence` |

Limits: no streaming; a request without images must fit in 64 KiB; the input window is 65,536 tokens; GGUF only. The CLI (`ollama run nimble --ask ...`) exists, but the official Python and JS libraries do not yet cover `systemone`, so call the HTTP endpoint or use TypeSafe's `typesafe-sdk` pointed at Ollama. Hosted Jev and local models share the format, so moving between them is a base-URL change.

## Reading the numbers

`confidence` measures how concentrated the probabilities are, not how likely the answer is correct (Ollama's docs say so in as many words). The Ollama blog's own example returns a `score` of 0.815 with `confidence` 0.046, so the two are independent axes: for a `score` question the *value* is the quality signal and the confidence says how sharply the model separated adjacent levels. Calibration holds across groups of predictions, not for one answer, so any threshold must be tuned on this project's own passages before it gates anything. The typical failure is a confident, valid, wrong answer.

## The local models

`ollama list` shows `clef-flash`, `clef-flash-4k`, `clef-flash-8k` and `clef-flash-lite` (about 10 GB each; there is no plain `clef` tag). `ollama show clef-flash` reports architecture `qwen35`, 9.1B parameters, Q8_0, capabilities `decision` and vision, and a raw-prompt template. **The four tags are one set of weights.** Their modelfiles point at the same two blobs (`sha256-c9ac3afd…` and the projector `sha256-c8a6e9e0…`) and differ only in `PARAMETER num_ctx`: 16384 (`clef-flash`), 8192 (`-8k`), 4096 (`-4k`), 2048 (`-lite`). A 2026-10-17 probe confirmed it: `-lite`, `-4k` and `-8k` returned bit-identical probabilities for the same request. They are fungible, as the user said, but the gradient is memory and maximum `state` size, not answer quality; "lite" is not a smaller model. Size the tag to the largest route text to be scored.

The probe also fixed the response shape. A `score` answer is `{type, score, legend, probabilities, confidence}`, `legend` mapping zero-based level index to the label; a `noul` answer is `{type, noul}` with no confidence or probabilities; a `choice` answer is `{type, choice, probabilities, confidence}`; `usage` reports `output_tokens: 0`. A three-question request over a ~370-token state took 4–5 s cold (model load) on a 12 GB GPU; warm latency is below. Published numbers for clef-flash are mixed against its 27B sibling: it leads on function-calling and routing-style tasks (BFCL 98.8, home-appliance simulator 97.7) and trails on fine-grained intent classes (CLINC150+OOS 66.8 vs 97.4) and on inference tasks (ANLI 59.1, When2Call 65.6). That profile suggests it judges *structured, bounded* questions well and subtle semantic ones less so, which is a hypothesis for story scoring, not a finding.

Warm latency, measured 2026-10-17 on `clef-flash-8k`, 100% on a 12 GB GPU: the first call after the model was evicted took 4.7 s (load); then a 3-question request over a ~370-token state took a median 194 ms (190–203, n=8) and a 1-question request 145 ms (n=5), so each extra question costs roughly 25 ms. Latency tracks input size, about 0.4 ms per token: 534 tokens 271 ms, 1182 tokens 553 ms, 2397 tokens 1043 ms (medians of 5, three questions). A route of a thousand-plus words is therefore a ~1 s call, still far below an LLM judge, but not the 39 ms Cloudflare reports for short inputs on its own hardware. The server unloads the model after about five minutes idle, so a scoring batch pays the cold load once. After a cleanup of local models only `clef-flash` and `clef-flash-8k` remain pulled.

Memory is the live constraint. On a 12 GB card, `clef-flash` (16384 context) failed even with the GPU empty: `cudaMalloc failed ... buffer of size 3490250880`, the compute buffer for a 16k context, while the 8k, 4k and 2k tags loaded. (An earlier failure the same day came from another process holding about 6.4 GB.) So the default tag is unusable here and `-8k` is the largest that runs; for per-passage or per-route scoring that is ample. The same single-GPU pressure caused [Fan-out Exhausted the GPU](../incidents/20260920-fanout-oom.md); a decision-model client belongs behind the same process-wide call gate in `agents.py`, and loading it evicts whatever else Ollama holds.

## Fit and limits for CYOA

Good fit: bounded judgments about a passage or route with a written rubric (does this follow from the previous passage, which kind of beat is it, how much tension). Poor fit: anything needing a reason, and anything that depends on a fact the model was not given in `state`; the [route canon](route-canon.md) problem (a passage relying on a clue its route never showed) is a graph property that `fact_check` already proves exactly, so a decision model should not replace it. Prompt-injection from passage text into `state` is untested by the vendors, which matters little for locally authored prose.

| Claim | Source |
|---|---|
| Endpoint, question types, limits | Ollama docs, Decision capability |
| clef-flash benchmarks and latency | Ollama library page for clef-flash (Cloudflare figures) |
| Jev architecture, RLCD, vendor error rates | DataCamp Jev explainer |
| Calibration caveats, five-model comparison | kmail.at and the Ollama blog |
| Local tags, architecture, OOM | `ollama list` / `ollama show`, 2026-10-17 probe |
