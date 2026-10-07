"""Model access for LLM-assisted authoring: Ollama, or any OpenAI-compatible server.

`chat_model()` builds the chat model the agent workflows in agents.py call,
`structured_method()` says which `with_structured_output` method works on that
backend, and `list_models()` asks the server what it has. Prompts live with the
workflows, not here.

CYOA_LLM picks the backend: `ollama` (OLLAMA_URL, OLLAMA_MODEL) or `openai`
(CYOA_OPENAI_URL, CYOA_OPENAI_MODEL — e.g. a local llama-server on :8080/v1;
the key is never checked). Two spikes shape the defaults. 2026-09-20 on
Ollama: qwen3.5-family models spend ~1000 tokens "thinking" per reply unless
reasoning is off, and schema-constrained decoding (`json_schema`) was the only
method that reliably returned on-schema JSON. Later the same day on
llama-server (Ternary Bonsai 27B, reasoning budget 0): `json_schema` fails in
the grammar sampler, `json_mode` leaks empty `<think>` tags into the output,
and `function_calling` returns on-schema output in under 2 s — so that is the
method there.
"""

from __future__ import annotations

import os

from . import settings

BACKEND = os.environ.get("CYOA_LLM", "ollama")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OPENAI_URL = os.environ.get("CYOA_OPENAI_URL", "http://localhost:8080/v1")
# A ceiling on every reply. Without one a model that never closes its JSON keeps
# generating until the context is full: on 2026-09-29 an Interview Me turn on
# gemma-4 12B (llama-server) ran past 28k tokens. Replies here are a passage or a
# small JSON object, far below this; a hit ends the call and the caller retries.
MAX_TOKENS = int(os.environ.get("CYOA_MAX_TOKENS", "4096"))
# Endings are written at length (prompts.py asks for one to two thousand words), so they get a higher
# ceiling than a passage; a 2000-word ending is about 2700 tokens before the JSON around it.
ENDING_MAX_TOKENS = int(os.environ.get("CYOA_ENDING_MAX_TOKENS", "8192"))


# The most a retry may raise a cut-off reply's ceiling to.
REPLY_TOKEN_LIMIT = 16000


def reply_cap(length: str, floor: int) -> int:
    """The token ceiling for a passage asked for `length` ("1200 words", "200–300 words"). Models write
    well past the length they are given (a probe on 2026-10-05 asked for 1200 words and got 2600), and a
    reply that hits the ceiling is cut off mid-sentence or loses its body, so the ceiling follows the plan:
    the largest number in `length`, at about 4 tokens a word (three times its usual 1.4). Never below
    `floor`, never past the server's context."""
    import re

    words = max((int(n) for n in re.findall(r"\d+", length or "")), default=0)
    return max(floor, min(int(words * 4.2), REPLY_TOKEN_LIMIT))


DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL" if BACKEND == "ollama" else "CYOA_OPENAI_MODEL", "bonsai27b:latest" if BACKEND == "ollama" else "default")


def list_models() -> list[str]:
    if BACKEND == "openai":
        import openai

        return [m.id for m in openai.OpenAI(base_url=OPENAI_URL, api_key="none", timeout=5).models.list()]
    import ollama

    return [m.model for m in ollama.Client(host=OLLAMA_URL, timeout=5).list().models if m.model]


# Seconds one model request may take before it is given up on and retried. A CPU-bound model
# can need many minutes for a long passage, and a shorter limit read as a run of retry failures.
REQUEST_TIMEOUT = 1800


def chat_model(model: str | None = None, *, temperature: float = 0.8, reasoning: bool = False, max_tokens: int | None = None, http_client=None):
    """`reasoning=True` is for refinement's auditors (refinement-loops.md): judgment
    benefits from thinking even though drafting turns it off everywhere else. On
    llama-server it is the chat template's `enable_thinking`, sent in `extra_body`
    both ways; whether structured output still parses with it on is unverified.

    The token cap goes in `extra_body` as `max_tokens` on purpose: langchain-openai
    (1.6) sends its own `max_tokens` field as `max_completion_tokens`, which
    llama-server ignores (the slot showed n_predict -1)."""
    cap = max_tokens or MAX_TOKENS
    if BACKEND == "openai":
        from langchain_openai import ChatOpenAI

        # Thinking is set explicitly both ways rather than left to the server's launch
        # flags: on 2026-09-29 gemma-4 12B, served without --reasoning-budget 0, spent a
        # whole Interview Me turn emitting "<|channel>thought" openers and never answered.
        extra: dict = {"max_tokens": cap, "chat_template_kwargs": {"enable_thinking": reasoning}}
        return ChatOpenAI(model=model or settings.model() or DEFAULT_MODEL, base_url=OPENAI_URL, api_key="none", temperature=temperature, extra_body=extra, http_client=http_client,
                          timeout=REQUEST_TIMEOUT, max_retries=0)
    from langchain_ollama import ChatOllama

    return ChatOllama(model=model or settings.model() or DEFAULT_MODEL, base_url=OLLAMA_URL, temperature=temperature, reasoning=reasoning, num_predict=cap, client_kwargs={"timeout": REQUEST_TIMEOUT})


def new_http_client():
    """A connection the caller can close to abort an in-flight call (cancel), or None when
    the backend's client is not injectable (Ollama)."""
    if BACKEND != "openai":
        return None
    import httpx

    return httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10))


def structured_method() -> str:
    """The `with_structured_output` method that works on the chosen backend."""
    return "function_calling" if BACKEND == "openai" else "json_schema"
