"""The "story so far" for a passage, kept by a small local model so the authoring agent need not reread it.

A branching book has many routes to any passage, and what the reader knows on arrival depends on the
route. `arrivals()` finds the shortest routes from the start to a passage and summarises the prose along each
route. A story has no variables (concepts/stateless-story.md), so the route is all a reader carries.

Summaries are *rolling and cached*: the summary of a route prefix is the model's condensation of the
summary of the prefix before it plus the passages since. Cache keys hash the prefix's content (ids, titles,
bodies, choice text taken), so an edit anywhere on a route simply makes the stale rows unreachable, and a
new passage on a known route costs one short call. Cold routes catch up in chunks, one call each.

Nothing here is exposed as a verb. mcp_server.get_passage calls `arrivals()`, and when the model server is
down it degrades to excerpts (`source: "excerpt"`) rather than failing, and stops retrying for a while.

CYOA_SUMMARY_URL (default http://0.0.0.0:8080/v1, a local llama-server), CYOA_SUMMARY_MODEL ("default"),
CYOA_SUMMARY_TIMEOUT (seconds per call), CYOA_SUMMARY_MAX_TOKENS. Pin one summariser across harness runs
being compared: it is a variable in the comparison, and its model name is stored on every cached row.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from typing import Any

from . import db, engine

URL = os.environ.get("CYOA_SUMMARY_URL", "http://0.0.0.0:8080/v1")
MODEL = os.environ.get("CYOA_SUMMARY_MODEL", "default")
TIMEOUT = float(os.environ.get("CYOA_SUMMARY_TIMEOUT", "90"))
MAX_TOKENS = int(os.environ.get("CYOA_SUMMARY_MAX_TOKENS", "500"))
# passages / characters of new prose condensed per model call; a cold route catches up chunk by chunk
CHUNK_PASSAGES = 6
CHUNK_CHARS = 12000
# after a failed call the model is left alone this long, so a dead server costs one timeout, not one per read
COOLDOWN = 30.0
EXCERPT_CHARS = 280
MAX_EXPANSIONS = 5000  # route search budget, for stories with many merges

SYSTEM = (
    "You keep continuity notes for the author of an interactive branching book. Given the notes so far and the "
    "next passages the reader met (each preceded by the choice they made to reach it), write the updated notes: "
    "who the reader is and where and when they are; what they have done, chosen, found or lost; named characters "
    "and their state toward the reader; promises, threats and mysteries still open; the tone. Plain past-tense "
    "prose, at most 180 words. Keep earlier facts that still matter, drop detail that no longer does. Do not "
    "invent anything and do not infer: state only what the text says (if someone vanished, do not say they died; do "
    "not guess the reader's gender, name or relationships beyond what is written). Call the reader \"you\". Do not "
    "address the author, no preamble."
)

_down_until = 0.0


def _client():
    import openai

    return openai.OpenAI(base_url=URL, api_key="none", timeout=TIMEOUT, max_retries=0)


def _ask(base: str, steps: list[dict[str, Any]]) -> str:
    """One condensation call; raises on any failure or an empty reply."""
    prose = "\n\n".join(
        (f"[chose: {s['via']}]\n" if s["via"] else "") + f"## {s['title']}\n{s['body'].strip()}" for s in steps)
    user = f"Notes so far:\n{base or '(the story begins here)'}\n\nNext passages:\n\n{prose}"
    # thinking off and the token cap in the body: llama-server honours both there (incidents/2026-09-29-interview-thinking-runaway.md)
    resp = _client().chat.completions.create(
        model=MODEL, temperature=0.2, max_tokens=MAX_TOKENS,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    text = re.sub(r"<think>.*?</think>", "", resp.choices[0].message.content or "", flags=re.S).strip()
    if not text:
        raise ValueError("empty summary")
    return text


def _key(steps: list[dict[str, Any]]) -> str:
    # the prompt and model are part of the key: a cached summary is only reusable by the summariser that wrote it
    return hashlib.sha1(json.dumps([SYSTEM, MODEL, [[s["id"], s["title"], s["body"], s["via"]] for s in steps]]).encode()).hexdigest()


def _excerpts(base: str, steps: list[dict[str, Any]]) -> str:
    lines = [base] if base else []
    for s in steps:
        body = " ".join(s["body"].split())
        lines.append(f"{'(chose: ' + s['via'] + ') ' if s['via'] else ''}{s['title']}: "
                     f"{body[:EXCERPT_CHARS]}{'…' if len(body) > EXCERPT_CHARS else ''}")
    return "\n".join(lines)


def _chunks(steps: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    size = 0
    for s in steps:
        if cur and (len(cur) >= CHUNK_PASSAGES or size + len(s["body"]) > CHUNK_CHARS):
            out.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(s["body"])
    return out + ([cur] if cur else [])


def route_summary(story_id: int, steps: list[dict[str, Any]]) -> tuple[str, str]:
    """(text, source) for everything the reader met along `steps`. source is "cache" (no call needed),
    "model" (summary current as of the last step) or "excerpt" (model unavailable: the newest cached
    summary, if any, plus raw excerpts of what came after it)."""
    global _down_until
    if not steps:
        return "", "cache"
    keys = [_key(steps[: n + 1]) for n in range(len(steps))]
    with db.transaction() as conn:
        rows = {r["key"]: r["text"] for r in conn.execute(
            f"SELECT key, text FROM summaries WHERE key IN ({','.join('?' for _ in keys)})", keys)}
    done = max((n for n in range(len(steps)) if keys[n] in rows), default=-1)
    base = rows.get(keys[done], "") if done >= 0 else ""
    if done == len(steps) - 1:
        return base, "cache"
    pending = steps[done + 1:]
    if time.monotonic() < _down_until:
        return _excerpts(base, pending), "excerpt"
    at = done
    try:
        for chunk in _chunks(pending):
            base = _ask(base, chunk)
            at += len(chunk)
            with db.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO summaries (key, story_id, passage_id, depth, text, model) VALUES (?, ?, ?, ?, ?, ?)",
                    (keys[at], story_id, steps[at]["id"], at + 1, base, MODEL))
    except Exception:
        _down_until = time.monotonic() + COOLDOWN
        return _excerpts(base, steps[at + 1:]), "excerpt"
    return base, "model"


# --- routes ------------------------------------------------------------------


def routes_to(target: int, start: int | None, choices: list[dict[str, Any]], limit: int) -> list[list[tuple[int, dict[str, Any] | None]]]:
    """Up to `limit` shortest simple routes to `target`, each a list of (passage_id, choice_taken_to_get_there).

    From the start when it reaches the target; otherwise from the passages nothing links to (a branch the map
    has not connected yet) that do; otherwise the target alone. Breadth-first, so shortest first."""
    out = engine._outgoing(choices)
    linked = {c["to_passage"] for c in choices if c["to_passage"] is not None}
    sources = [start] if start is not None else []
    for attempt in range(2):
        found: list[list[tuple[int, dict[str, Any] | None]]] = []
        queue = deque([[(s, None)] for s in sources])
        budget = MAX_EXPANSIONS
        while queue and len(found) < limit and budget:
            budget -= 1
            path = queue.popleft()
            pid = path[-1][0]
            if pid == target:
                found.append(path)
                continue
            seen = {p for p, _ in path}
            for c in out.get(pid, []):
                if c["to_passage"] is not None and c["to_passage"] not in seen:
                    queue.append([*path, (c["to_passage"], c)])
        if found or attempt:
            return found or [[(target, None)]]
        sources = sorted({c["from_passage"] for c in choices} - linked)  # roots; passages with no choices are their own
    return [[(target, None)]]


def arrivals(story_id: int, target: int, start: int | None, passages: dict[int, dict[str, Any]],
             choices: list[dict[str, Any]], limit: int = 2) -> dict[str, Any]:
    """How a reader can arrive at `target`: per route, the passages and choices taken and the summary of the
    prose before `target`. With no state in a story, the route is everything the reader knows."""
    routes = routes_to(target, start, choices, limit + 1)
    result = []
    for route in routes[:limit]:
        steps = [{"id": pid, "title": passages[pid]["title"], "body": passages[pid]["body"], "via": via["text"] if via else None}
                 for pid, via in route]
        text, source = route_summary(story_id, steps[:-1])
        result.append({
            "route": [{"id": s["id"], "title": s["title"]} for s in steps[:-1]],
            "last_choice": steps[-1]["via"],
            "summary": text,
            "summary_source": source,
        })
    return {"routes": result, "more_routes": len(routes) > limit}
