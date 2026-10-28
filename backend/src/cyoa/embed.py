"""Text embeddings from a local llama-server, for the author's divergence checks (mcp_server.nearest_endings).

Vectors are cached by a hash of (model, text), so an unchanged ending costs nothing on the next call.
CYOA_EMBED_URL (default: CYOA_SUMMARY_URL, the chat server started with `--embeddings --pooling mean`), CYOA_EMBED_MODEL.
A chat model's pooled hidden states share a large common component, so callers should `centre()` before comparing.
"""

from __future__ import annotations

import array
import hashlib
import math
import os

from . import db, summarize

URL = os.environ.get("CYOA_EMBED_URL", summarize.URL)
MODEL = os.environ.get("CYOA_EMBED_MODEL", "default")
MAX_CHARS = 900  # about 250 tokens: well inside llama-server's physical batch (512), which aborts or runs out of memory above it
BATCH = 1  # one input per request keeps the server's peak memory at one short sequence


def _key(text: str) -> str:
    return hashlib.sha1(f"{MODEL}\0{text}".encode()).hexdigest()


def _fetch(texts: list[str]) -> list[list[float]]:
    import openai

    client = openai.OpenAI(base_url=URL, api_key="none", timeout=summarize.TIMEOUT, max_retries=0)
    resp = client.embeddings.create(model=MODEL, input=texts)
    return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


def _clip(text: str) -> str:
    """Whitespace-collapsed and cut to MAX_CHARS keeping the start and the end: an ending's outcome is in its last lines."""
    text = " ".join(text.split())
    if len(text) <= MAX_CHARS:
        return text or "(empty)"
    head = MAX_CHARS * 2 // 5
    return text[:head] + " … " + text[-(MAX_CHARS - head - 3):]


def embed(texts: list[str]) -> list[list[float]]:
    """One vector per text (truncated to MAX_CHARS). Raises if the server cannot embed."""
    texts = [_clip(t) for t in texts]
    keys = [_key(t) for t in texts]
    with db.transaction() as conn:
        have = {r["key"]: list(array.array("f", r["vec"])) for r in conn.execute(
            f"SELECT key, vec FROM embeddings WHERE key IN ({','.join('?' for _ in keys)})", keys)}
    todo = list(dict.fromkeys(k for k in keys if k not in have))
    by_key = dict(zip(keys, texts))
    for i in range(0, len(todo), BATCH):
        part = todo[i:i + BATCH]
        for k, vec in zip(part, _fetch([by_key[k] for k in part])):
            have[k] = vec
            with db.transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO embeddings (key, model, vec) VALUES (?, ?, ?)",
                             (k, MODEL, array.array("f", vec).tobytes()))
    return [have[k] for k in keys]


def centre(vecs: list[list[float]]) -> list[list[float]]:
    """Subtract the mean vector and normalise: what is left is how each text differs from the rest."""
    n, dim = len(vecs), len(vecs[0])
    mean = [sum(v[i] for v in vecs) / n for i in range(dim)]
    out = []
    for v in vecs:
        c = [x - m for x, m in zip(v, mean)]
        norm = math.sqrt(sum(x * x for x in c)) or 1.0
        out.append([x / norm for x in c])
    return out


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
