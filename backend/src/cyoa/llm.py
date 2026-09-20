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

BACKEND = os.environ.get("CYOA_LLM", "ollama")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OPENAI_URL = os.environ.get("CYOA_OPENAI_URL", "http://localhost:8080/v1")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL" if BACKEND == "ollama" else "CYOA_OPENAI_MODEL", "bonsai27b:latest" if BACKEND == "ollama" else "default")


def list_models() -> list[str]:
    if BACKEND == "openai":
        import openai

        return [m.id for m in openai.OpenAI(base_url=OPENAI_URL, api_key="none", timeout=5).models.list()]
    import ollama

    return [m.model for m in ollama.Client(host=OLLAMA_URL, timeout=5).list().models if m.model]


def chat_model(model: str | None = None, *, temperature: float = 0.8):
    if BACKEND == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model or DEFAULT_MODEL, base_url=OPENAI_URL, api_key="none", temperature=temperature)
    from langchain_ollama import ChatOllama

    return ChatOllama(model=model or DEFAULT_MODEL, base_url=OLLAMA_URL, temperature=temperature, reasoning=False)


def structured_method() -> str:
    """The `with_structured_output` method that works on the chosen backend."""
    return "function_calling" if BACKEND == "openai" else "json_schema"
