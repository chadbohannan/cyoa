"""Canon checks per route for the MCP server: what the reader can know at a passage, and whether the prose keeps to canon.

Shape checks (problems, playtest) and divergence checks (nearest_endings) say nothing about correctness, and correctness
is where a branching book breaks: every route is its own history, so a passage can rely on a clue its route never showed,
or contradict a rule that a sibling branch keeps. Two checks close that gap (concepts/route-canon.md):

- `fact_flow` is deterministic. Facts are lore entries of kind 'fact'; a passage lists in its meta the facts it
  `reveals` and the facts it `needs` (relies on the reader already knowing). A fact is known at a passage when every
  route from the start reveals it earlier, which is a must-analysis over the graph, so routes are never enumerated.
  This is checked when writing, not stored as reader state: the story stays stateless (concepts/stateless-story.md).
- `check_passage` asks the local model (the summariser's server) whether a passage contradicts the canon or the story
  so far on its route, using the cached route summary so the prompt stays bounded at any depth. Results are cached by
  content, like summaries, and each claimed issue must quote the passage: a quote not found in it is marked.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from typing import Any

from . import db, engine, summarize

TIMEOUT = float(os.environ.get("CYOA_CANON_TIMEOUT", "240"))
MAX_TOKENS = int(os.environ.get("CYOA_CANON_MAX_TOKENS", "900"))


def fact_names(meta: dict[str, Any], key: str) -> list[str]:
    v = (meta or {}).get(key) or []
    return [v] if isinstance(v, str) else list(v)


# --- deterministic: what is known where --------------------------------------------


def fact_flow(start: int | None, passages: dict[int, dict[str, Any]], choices: list[dict[str, Any]]) -> dict[int, dict[str, set[str]]]:
    """For each passage the start reaches: `known`, the facts revealed before it on every route, and `maybe`, those
    revealed before it on some route only. A passage's own reveals are not included (they are learned there)."""
    if start is None or start not in passages:
        return {}
    out = engine._outgoing(choices)
    reveals = {pid: set(fact_names(p["meta"], "reveals")) for pid, p in passages.items()}
    must: dict[int, set[str] | None] = {start: set()}
    may: dict[int, set[str]] = {start: set()}
    preds: dict[int, list[int]] = {}
    for c in choices:
        if c["to_passage"] in passages and c["from_passage"] in passages:
            preds.setdefault(c["to_passage"], []).append(c["from_passage"])
    reach, queue = {start}, deque([start])
    while queue:
        for c in out.get(queue.popleft(), []):
            if c["to_passage"] in passages and c["to_passage"] not in reach:
                reach.add(c["to_passage"])
                queue.append(c["to_passage"])
    # iterate to a fixpoint; must starts at "everything" (None) and only shrinks, may starts empty and only grows
    changed = True
    while changed:
        changed = False
        for pid in reach:
            if pid == start:
                continue
            ins = [q for q in preds.get(pid, []) if q in reach]
            sets = [must[q] | reveals[q] for q in ins if must.get(q) is not None]
            new_must = set.intersection(*sets) if sets else None
            new_may = set().union(*(may.get(q, set()) | reveals[q] for q in ins))
            if new_must != must.get(pid) or new_may != may.get(pid):
                must[pid], may[pid] = new_must, new_may
                changed = True
    return {pid: {"known": must.get(pid) or set(), "maybe": may.get(pid, set()) - (must.get(pid) or set())} for pid in reach}


def _route_without(fact: str, target: int, start: int, passages: dict[int, dict[str, Any]], choices: list[dict[str, Any]]) -> list[int]:
    """The shortest route from the start to `target` on which no passage before it reveals `fact`: the reader who arrives not knowing it."""
    out = engine._outgoing(choices)
    prev: dict[int, int | None] = {start: None}
    queue = deque([start] if fact not in fact_names(passages[start]["meta"], "reveals") or start == target else [])
    while queue:
        pid = queue.popleft()
        if pid == target:
            route = [pid]
            while prev[route[-1]] is not None:
                route.append(prev[route[-1]])  # type: ignore[arg-type]
            return route[::-1]
        for c in out.get(pid, []):
            to = c["to_passage"]
            if to in passages and to not in prev and (to == target or fact not in fact_names(passages[to]["meta"], "reveals")):
                prev[to] = pid
                queue.append(to)
    return []


def fact_check(start: int | None, passages: dict[int, dict[str, Any]], choices: list[dict[str, Any]], facts: list[str]) -> dict[str, Any]:
    """Every passage's `needs` against what each route into it has revealed, plus fact names that match no fact entry,
    facts no reachable passage reveals, and the verdicts on endings."""
    flow = fact_flow(start, passages, choices)
    titles = {pid: p["title"] for pid, p in passages.items()}
    declared = {f.casefold() for f in facts}
    unmet, unknown = [], []
    for pid, p in passages.items():
        for key in ("reveals", "needs"):
            for f in fact_names(p["meta"], key):
                if f.casefold() not in declared:
                    unknown.append({"passage": pid, "title": p["title"], key: f})
        if pid not in flow:
            continue
        for f in fact_names(p["meta"], "needs"):
            if f in flow[pid]["known"]:
                continue
            route = _route_without(f, pid, start, passages, choices)  # type: ignore[arg-type]
            unmet.append({"passage": pid, "title": p["title"], "needs": f,
                          "revealed": "on some routes only" if f in flow[pid]["maybe"] else "on no route",
                          "route_without_it": [titles[i] for i in route]})
    revealed = {f.casefold() for pid in flow for f in fact_names(passages[pid]["meta"], "reveals")}
    has_out = {c["from_passage"] for c in choices}
    endings = [p for pid, p in passages.items() if pid in flow and (p["is_ending"] or pid not in has_out)]
    verdicts: dict[str, int] = {}
    for p in endings:
        v = (p["meta"] or {}).get("verdict")
        verdicts[v or "(none)"] = verdicts.get(v or "(none)", 0) + 1
    stray = [{"passage": pid, "title": p["title"], "verdict": p["meta"]["verdict"]} for pid, p in passages.items()
             if (p["meta"] or {}).get("verdict") and p not in endings]
    return {
        "unmet_needs": unmet,
        "unknown_fact_names": unknown,
        "never_revealed": [f for f in facts if f.casefold() not in revealed],
        "verdicts": verdicts,
        "verdict_not_on_an_ending": stray,
        "passages_using_facts": sum(1 for p in passages.values() if fact_names(p["meta"], "reveals") or fact_names(p["meta"], "needs")),
    }


# --- model: does the prose keep to canon ---------------------------------------------


SYSTEM = (
    "You check one passage of an interactive branching book for continuity errors. You are given the CANON (entries that "
    "are true on every branch, including rules about how the book must be written), the FACTS the reader has been shown "
    "on this route, rough NOTES on the story so far (written by a small model: they may be wrong, so prefer the other "
    "texts), the PREVIOUS PASSAGE word for word, and the PASSAGE. Report only real contradictions: a statement in the "
    "passage that conflicts with the canon, breaks a canon rule, or conflicts with what came before. Something the earlier "
    "texts simply do not mention is NOT a contradiction; do not report it, and do not report style. Each issue must quote "
    "the passage AND quote the exact words it conflicts with from the CANON, FACTS, NOTES or PREVIOUS PASSAGE; an issue "
    "you cannot back with both quotes is not an issue. At most 5 issues, each `why` under 25 words. Reply with JSON only: "
    "{\"issues\": [{\"quote\": \"exact words from the passage\", \"evidence\": \"exact words it conflicts with\", "
    "\"why\": \"...\"}]}. An empty list is the right answer for a consistent passage."
)

FACTS_SYSTEM = (
    "You read one passage of a book and a list of FACTS. For each fact, decide whether some sentence of the passage states "
    "what that fact says (a character says it, the reader sees it, or the reader reasons from it). A sentence that only "
    "mentions the same person or thing does not count, and neither does one saying the reader does not know it. Most "
    "passages state only a few of the facts, often none. Reply with JSON only: {\"facts_used\": [{\"name\": \"fact name "
    "exactly as listed\", \"sentence\": \"the sentence copied exactly from the passage\"}]}."
)


def canon_text(story: dict[str, Any], lore: list[dict[str, Any]]) -> str:
    parts = [f"Premise: {story['description']}"] if story.get("description") else []
    parts += [f"[{r['kind']}] {r['name']}: {r['body']}" for r in lore if r["kind"] != "fact"]
    return "\n\n".join(parts)


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[\u2018\u2019]", "'", re.sub(r"[\u201c\u201d]", '"', s)).casefold().split())


def _json(system: str, user: str, max_tokens: int) -> dict[str, Any]:
    resp = summarize._client().with_options(timeout=TIMEOUT).chat.completions.create(
        model=summarize.MODEL, temperature=0.1, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    text = re.sub(r"<think>.*?</think>", "", resp.choices[0].message.content or "", flags=re.S).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"reply was not JSON: {text[:200]!r}")
    return json.loads(m.group(0))


def _ask(prompt: dict[str, str]) -> list[dict[str, Any]]:
    user = (f"CANON:\n{prompt['canon']}\n\nFACTS SHOWN ON THIS ROUTE:\n{prompt['facts'] or '(none yet)'}\n\n"
            f"NOTES (story so far, may be wrong):\n{prompt['notes'] or '(this is the opening passage)'}\n\n"
            f"PREVIOUS PASSAGE:\n{prompt['previous'] or '(none)'}\n\nPASSAGE: {prompt['passage']}")
    issues = _json(SYSTEM, user, MAX_TOKENS).get("issues") or []
    return [{"quote": str(i.get("quote", "")), "evidence": str(i.get("evidence", "")), "why": str(i.get("why", ""))}
            for i in issues if isinstance(i, dict)]


def _ask_facts(facts: dict[str, str], passage: str) -> list[dict[str, str]]:
    listed = "\n".join(f"- {n}: {b}" for n, b in facts.items())
    got = _json(FACTS_SYSTEM, f"FACTS:\n{listed}\n\nPASSAGE: {passage}", 600).get("facts_used") or []
    names = {n.casefold(): n for n in facts}
    return [{"name": names[str(f.get("name", "")).strip().casefold()], "sentence": str(f.get("sentence", ""))}
            for f in got if isinstance(f, dict) and str(f.get("name", "")).strip().casefold() in names]


def _retry(fn, *args):
    try:
        return fn(*args)
    except (json.JSONDecodeError, ValueError):  # a small model's JSON breaks now and then; one retry
        return fn(*args)


def check_passage(story_id: int, canon: str, facts: dict[str, str], steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Check the last of `steps` (a route from the start, each step with its passage `meta`) against the canon, the facts
    this route revealed before it, the previous passage verbatim and the summary of the route before it. Two model calls,
    cached together by content: contradictions, each kept only if both its quotes are found (one from the passage, one
    from the texts it was given); and which facts the prose uses, compared here with what the route revealed. Raises if
    the model is unavailable: a canon check that silently passed would lie."""
    target, before = steps[-1], steps[:-1]
    so_far, source = summarize.route_summary(story_id, before[:-1])
    if source == "excerpt" and len(before) > 1:
        raise ValueError(f"local model unavailable ({summarize.URL}): the route summary could not be made")
    shown = list(dict.fromkeys(f for s in before for f in fact_names(s.get("meta") or {}, "reveals") if f in facts))
    prompt = {
        "canon": canon,
        "facts": "\n".join(f"- {f}: {facts[f]}" for f in shown),
        "notes": so_far,
        "previous": f"{before[-1]['title']}\n{before[-1]['body']}" if before else "",
        "passage": f"{target['title']}\n" + (f"(reached by choosing: {target['via']})\n" if target["via"] else "") + target["body"],
    }
    key = hashlib.sha1(json.dumps([SYSTEM, FACTS_SYSTEM, summarize.MODEL, prompt, facts]).encode()).hexdigest()
    with db.transaction() as conn:
        row = conn.execute("SELECT issues FROM canon_checks WHERE key = ?", (key,)).fetchone()
    if row:
        got, cached = json.loads(row["issues"]), True
    else:
        try:
            got = {"issues": _retry(_ask, prompt), "facts_used": _retry(_ask_facts, facts, prompt["passage"]) if facts else []}
        except (json.JSONDecodeError, ValueError) as e:
            return {"issues": [], "dropped": 0, "cached": False, "error": f"unreadable model reply: {e}"}
        cached = False
        with db.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO canon_checks (key, story_id, passage_id, issues, model) VALUES (?, ?, ?, ?, ?)",
                         (key, story_id, target["id"], json.dumps(got), summarize.MODEL))
    body = _norm(prompt["passage"])
    given = {"canon": _norm(canon + "\n" + prompt["facts"]), "previous passage": _norm(prompt["previous"]), "notes": _norm(so_far)}
    kept, dropped = [], 0
    for i in got["issues"]:
        where = next((k for k, text in given.items() if i["evidence"] and _norm(i["evidence"]) in text), None)
        if i["quote"] and _norm(i["quote"]) in body and where:
            kept.append({**i, "evidence_from": where})
        else:
            dropped += 1
    own = set(fact_names(target.get("meta") or {}, "reveals"))
    used = set()
    for u in got["facts_used"]:
        if not (u["sentence"] and _norm(u["sentence"]) in body):
            dropped += 1  # a fact "used" without a sentence really in the passage is the model guessing
            continue
        used.add(u["name"])
        if u["name"] not in shown and u["name"] not in own:
            kept.append({"quote": u["sentence"], "evidence": f"{u['name']}: {facts[u['name']]}", "evidence_from": "fact flow",
                         "why": f"states fact '{u['name']}', which this route has not revealed before here and the passage does not tag as revealed"})
    return {"issues": kept, "dropped": dropped, "cached": cached, "tagged_but_unseen": sorted(own - used)}
