"""Ollama client for LLM-assisted authoring.

Talks to a local Ollama server (OLLAMA_URL, default http://localhost:11434)
using its native /api endpoints. Kept deliberately thin: the authoring
prompts belong to the routes that use them, not here.
"""

from __future__ import annotations

import os

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "bonsai27b:latest")


async def list_models() -> list[str]:
    async with httpx.AsyncClient(base_url=OLLAMA_URL, timeout=5) as client:
        r = await client.get("/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]


async def generate(prompt: str, *, model: str | None = None, system: str | None = None) -> str:
    payload: dict = {"model": model or DEFAULT_MODEL, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    async with httpx.AsyncClient(base_url=OLLAMA_URL, timeout=300) as client:
        r = await client.post("/api/generate", json=payload)
        r.raise_for_status()
        return r.json()["response"]
