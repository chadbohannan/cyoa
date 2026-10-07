"""Condition and effects evaluator — the one place their semantics live.

The player, the ``problems`` validator and (later) prompt construction and
export all go through these functions, so the JSON vocabulary stored on
``choices.condition`` / ``choices.effects`` is defined here and nowhere else.

Conditions::

    {}                                             always true
    {"var": "brave", "op": "==", "value": true}    one clause
    {"all": [cond, ...]} / {"any": [cond, ...]}    combinators, nestable

Effects::

    {"set": {"brave": true}, "add": {"gold": -5}, "give": ["lamp"], "take": ["lamp"]}

State is a plain ``{name: value}`` dict seeded from the variables table. A
condition naming an undeclared variable is false; an effect naming one is
ignored. Items are counts: ``give`` increments, ``take`` decrements and
floors at zero.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter, deque
from typing import Any, Iterable

from fastapi import HTTPException

State = dict[str, Any]

# JSON columns that need encoding/decoding; shared with main.py row helpers
JSON_COLS: dict[str, set[str]] = {
    "passages": {"meta"},
    "choices": {"condition", "effects"},
    "variables": {"default"},
    "lore": set(),
    "proposals": {"payload"},
    "agent_runs": {"params", "scope", "budget", "policy", "progress"},
    "interviews": {"transcript", "conflicts"},
}

OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def initial_state(variables: Iterable[dict[str, Any]]) -> State:
    return {v["name"]: v["default"] for v in variables}


def check(condition: Any, state: State) -> bool:
    """True when ``condition`` holds in ``state``. Malformed conditions are false."""
    if not isinstance(condition, dict):
        return False
    if not condition:
        return True
    if "all" in condition:
        return isinstance(condition["all"], list) and all(check(c, state) for c in condition["all"])
    if "any" in condition:
        return isinstance(condition["any"], list) and any(check(c, state) for c in condition["any"])
    name, op = condition.get("var"), condition.get("op")
    if name not in state or op not in OPS:
        return False
    try:
        return bool(OPS[op](state[name], condition.get("value")))
    except TypeError:  # e.g. ordering a bool against a string
        return False


def apply(effects: Any, state: State) -> State:
    """Return a new state with ``effects`` applied. Undeclared names are skipped."""
    out = dict(state)
    if not isinstance(effects, dict):
        return out
    for name, value in _items(effects.get("set")):
        if name in out:
            out[name] = value
    for name, value in _items(effects.get("add")):
        if name in out and _numeric(out[name]) and _numeric(value):
            out[name] = out[name] + value
    for name in _names(effects.get("give")):
        if name in out and _numeric(out[name]):
            out[name] = out[name] + 1
    for name in _names(effects.get("take")):
        if name in out and _numeric(out[name]):
            out[name] = max(0, out[name] - 1)
    return out


def condition_vars(condition: Any) -> set[str]:
    """Every variable name a condition mentions."""
    if not isinstance(condition, dict):
        return set()
    found: set[str] = set()
    for key in ("all", "any"):
        if isinstance(condition.get(key), list):
            for c in condition[key]:
                found |= condition_vars(c)
    if isinstance(condition.get("var"), str):
        found.add(condition["var"])
    return found


def effect_vars(effects: Any) -> set[str]:
    """Every variable name an effects object mentions."""
    if not isinstance(effects, dict):
        return set()
    found: set[str] = set()
    for key in ("set", "add"):
        found |= {n for n, _ in _items(effects.get(key))}
    for key in ("give", "take"):
        found |= set(_names(effects.get(key)))
    return found


def problems(
    start_passage_id: int | None,
    passages: Iterable[dict[str, Any]],
    choices: Iterable[dict[str, Any]],
    variables: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Static checks over one story's graph; the shape mirrors ``models.Problems``."""
    passages, choices = list(passages), list(choices)
    ids = [p["id"] for p in passages]
    declared = {v["name"] for v in variables}
    is_ending = {p["id"]: bool(p.get("is_ending")) for p in passages}
    out_of: dict[int, list[dict[str, Any]]] = {pid: [] for pid in ids}
    for c in choices:
        out_of.setdefault(c["from_passage"], []).append(c)

    reachable: set[int] = set()
    if start_passage_id in out_of:
        queue = deque([start_passage_id])
        reachable.add(start_passage_id)
        while queue:
            for c in out_of[queue.popleft()]:
                nxt = c["to_passage"]
                if nxt is not None and nxt in out_of and nxt not in reachable:
                    reachable.add(nxt)
                    queue.append(nxt)

    undeclared = [
        {"choice_id": c["id"], "var": name}
        for c in choices
        for name in sorted((condition_vars(c["condition"]) | effect_vars(c["effects"])) - declared)
    ]
    return {
        "no_start": start_passage_id not in out_of,
        "unreachable": [pid for pid in ids if pid not in reachable],
        "dangling": [c["id"] for c in choices if c["to_passage"] is None],
        "undeclared": undeclared,
        "endings": [pid for pid in ids if is_ending[pid]],
        "unfinished": [pid for pid in ids if not out_of[pid] and not is_ending[pid]],
        "merge_candidates": merge_candidates(passages),
    }


def _outgoing(choices: Iterable[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for c in choices:
        out.setdefault(c["from_passage"], []).append(c)
    return out


def depth_from_start(root: int, start_passage_id: int | None, choices: Iterable[dict[str, Any]]) -> int:
    """Shortest number of choices from the start to `root`; 0 for the start itself, and for
    a root the start cannot reach (or a story with no start), which is treated as a fresh top."""
    if root == start_passage_id or start_passage_id is None:
        return 0
    out = _outgoing(list(choices))
    seen, queue = {start_passage_id}, deque([(start_passage_id, 0)])
    while queue:
        pid, d = queue.popleft()
        for c in out.get(pid, []):
            to = c["to_passage"]
            if to == root:
                return d + 1
            if to is not None and to not in seen:
                seen.add(to)
                queue.append((to, d + 1))
    return 0


def subtree(root: int, start_passage_id: int | None, passages: Iterable[dict[str, Any]], choices: Iterable[dict[str, Any]]) -> list[int]:
    """The root plus every passage reachable from it *only* through it.

    Passages the rest of the map can still reach (from the start or from any
    passage nothing links to) are not part of the subtree, so promoting or
    collapsing a branch never swallows the trunk. Same rule as the map's collapse.
    """
    passages, choices = list(passages), list(choices)
    out = _outgoing(choices)
    ids = {p["id"] for p in passages}
    if root not in ids:
        return []
    inbound = {c["to_passage"] for c in choices if c["to_passage"] is not None}
    # everything reachable without stepping past the root
    visible: set[int] = set()
    queue = deque(pid for pid in ids if pid == start_passage_id or pid not in inbound)
    while queue:
        pid = queue.popleft()
        if pid in visible:
            continue
        visible.add(pid)
        if pid != root:
            queue.extend(c["to_passage"] for c in out.get(pid, []) if c["to_passage"] is not None)
    mine = [root]
    seen = {root}
    queue = deque([root])
    while queue:
        for c in out.get(queue.popleft(), []):
            nxt = c["to_passage"]
            if nxt is None or nxt in seen or nxt in visible or nxt not in ids:
                continue
            seen.add(nxt)
            mine.append(nxt)
            queue.append(nxt)
    return mine


def prune_set(root: int, start_passage_id: int | None, passages: Iterable[dict[str, Any]], choices: Iterable[dict[str, Any]]) -> list[int]:
    """The passages deleting `root`'s subtree removes: everything reachable from
    the root (not the root itself) that the start cannot reach without passing
    through it, so pruning runs to the leaves and leaves no orphans behind.

    Unlike `subtree`, orphans elsewhere on the map do not shield anything: a
    passage nothing links to is already cut off, so what only it reaches would be
    orphaned too. Passages another branch from the start reaches survive.
    """
    passages, choices = list(passages), list(choices)
    out = _outgoing(choices)
    ids = {p["id"] for p in passages}
    if root not in ids:
        return []
    kept: set[int] = set()
    queue = deque([start_passage_id] if start_passage_id in ids and start_passage_id != root else [])
    while queue:
        pid = queue.popleft()
        if pid in kept or pid == root:
            continue
        kept.add(pid)
        queue.extend(c["to_passage"] for c in out.get(pid, []) if c["to_passage"] is not None)
    doomed: list[int] = []
    seen = {root}
    queue = deque([root])
    while queue:
        for c in out.get(queue.popleft(), []):
            nxt = c["to_passage"]
            if nxt is None or nxt in seen or nxt not in ids:
                continue
            seen.add(nxt)
            if nxt in kept:
                continue
            doomed.append(nxt)
            queue.append(nxt)
    return doomed


def merge_candidates(passages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Groups of passages sharing story time and location: the same scene reached
    by different routes, and so the natural things to merge."""
    groups: dict[tuple[Any, ...], list[int]] = {}
    for p in passages:
        meta = p.get("meta") or {}
        location = meta.get("location") if isinstance(meta, dict) else None
        if p.get("t") is None or not location:
            continue
        groups.setdefault((p["t"], str(location).strip().lower()), []).append(p["id"])
    return [
        {"t": t, "location": location, "passage_ids": sorted(ids)}
        for (t, location), ids in sorted(groups.items())
        if len(ids) > 1
    ]


def random_playthroughs(
    start_passage_id: int | None,
    passages: Iterable[dict[str, Any]],
    choices: Iterable[dict[str, Any]],
    variables: Iterable[dict[str, Any]],
    runs: int = 200,
    max_steps: int = 200,
    seed: int | None = None,
) -> dict[str, Any]:
    """Walk the story ``runs`` times taking a random available choice each step,
    through the same ``check``/``apply`` as the player. The shape mirrors
    ``models.Playtest``: what was never visited, where readers got stuck (choices
    exist but none is available in that state), which decided endings were
    reached and how often versus which unfinished stubs a walk simply ran out
    of story at, and how many walks the step cap cut short."""
    passages, choices = list(passages), list(choices)
    ids = [p["id"] for p in passages]
    is_ending = {p["id"]: bool(p.get("is_ending")) for p in passages}
    out = _outgoing(choices)
    rng = random.Random(seed)
    seen_p: Counter[int] = Counter()
    seen_c: Counter[int] = Counter()
    endings: Counter[int] = Counter()
    unfinished: Counter[int] = Counter()
    stuck: Counter[int] = Counter()
    dangling: Counter[int] = Counter()
    cut, lengths = 0, []
    start_state = initial_state(variables)
    if start_passage_id in out or start_passage_id in ids:
        for _ in range(runs):
            pid, state, steps = start_passage_id, dict(start_state), 0
            while True:
                seen_p[pid] += 1
                # is_ending is authoritative, same as _view: a decided ending has no
                # choices at play time even if some are still attached to the row.
                if is_ending.get(pid):
                    endings[pid] += 1
                    break
                options = out.get(pid, [])
                if not options:
                    unfinished[pid] += 1
                    break
                open_ = [c for c in options if check(c["condition"], state)]
                if not open_:
                    stuck[pid] += 1
                    break
                if steps >= max_steps:
                    cut += 1
                    break
                c = rng.choice(open_)
                seen_c[c["id"]] += 1
                if c["to_passage"] is None:
                    dangling[c["id"]] += 1
                    break
                state = apply(c["effects"], state)
                pid, steps = c["to_passage"], steps + 1
            lengths.append(steps)
    return {
        "runs": runs,
        "mean_length": (sum(lengths) / len(lengths)) if lengths else 0.0,
        "cut": cut,
        "visited": {pid: seen_p[pid] for pid in ids if seen_p[pid]},
        "unvisited": [pid for pid in ids if not seen_p[pid]],
        "untaken": [c["id"] for c in choices if not seen_c[c["id"]]],
        "endings": dict(endings),
        "unfinished": dict(unfinished),
        "stuck": dict(stuck),
        "dangling": dict(dangling),
    }


def _items(d: Any) -> list[tuple[str, Any]]:
    return [(k, v) for k, v in d.items() if isinstance(k, str)] if isinstance(d, dict) else []


def _names(xs: Any) -> list[str]:
    return [x for x in xs if isinstance(x, str)] if isinstance(xs, list) else []


def _numeric(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# --- Row mutation helpers (used by agents and routes) -------------------------

def decode_row(table: str, row: sqlite3.Row) -> dict[str, Any]:
    """Decode a row from the database, JSON-decoding columns as needed."""
    d = dict(row)
    for col in JSON_COLS[table]:
        d[col] = json.loads(d[col])
    return d


def encode_values(table: str, values: dict[str, Any]) -> dict[str, Any]:
    """Encode values for insertion, JSON-encoding columns as needed."""
    return {k: json.dumps(v) if k in JSON_COLS[table] else v for k, v in values.items()}


def insert_row(conn: sqlite3.Connection, table: str, story_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Insert a row and return the decoded result. Used by agents and routes."""
    # Import here to avoid circular dependency
    from . import db

    # Verify story exists
    row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "story not found")

    if table == "passages" and values.get("title"):
        values = {**values, "title": db.unique_title(conn, story_id, values["title"])}  # section names are Twee's keys
    values = encode_values(table, {"story_id": story_id, **values})
    cols = ", ".join(f'"{k}"' for k in values)
    marks = ", ".join("?" for _ in values)
    try:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, str(e))
    db.touch_story(conn, story_id)
    return decode_row(table, conn.execute(f"SELECT * FROM {table} WHERE id = ?", (cur.lastrowid,)).fetchone())


def materialise(conn: sqlite3.Connection, story_id: int, p: dict[str, Any]) -> None:
    """Write the rows a proposal describes. The one place proposal kinds become tables.

    This is called both by the accept-proposal route (for explicit human approval)
    and by the agents when a target passage is unreviewed (for direct writes).
    """
    kind, pl = p["kind"], p["payload"]
    if kind == "edit_passage":
        # a suggest_next "end" proposal sets is_ending without touching body, so only
        # write the fields actually present in the payload.
        sets = {k: (v if k != "is_ending" else int(v)) for k, v in pl.items() if k in ("body", "is_ending")}
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(f"UPDATE passages SET {cols} WHERE id = ? AND story_id = ?", (*sets.values(), p["target_passage"], story_id))
    elif kind == "edit_choice":
        conn.execute("UPDATE choices SET text = ? WHERE id = ? AND story_id = ?", (pl["text"], p["target_choice"], story_id))
    elif kind == "new_passage":
        new = insert_row(conn, "passages", story_id, {"title": pl["title"], "body": pl["body"], "pos_x": pl.get("pos_x", 0),
                                                   "pos_y": pl.get("pos_y", 0), "provenance": "agent"})
        if p["target_choice"] is not None:
            conn.execute("UPDATE choices SET to_passage = ? WHERE id = ? AND to_passage IS NULL", (new["id"], p["target_choice"]))
    elif kind == "new_choice":
        to = pl.get("to_passage")
        if to is None and pl.get("new_target"):
            t = pl["new_target"]
            to = insert_row(conn, "passages", story_id, {"title": t["title"], "body": t.get("body", ""), "pos_x": t.get("pos_x", 0),
                                                     "pos_y": t.get("pos_y", 0), "provenance": "agent",
                                                     "is_ending": bool(t.get("is_ending"))})["id"]
        order = conn.execute("SELECT COALESCE(MAX(sort_order) + 1, 0) FROM choices WHERE from_passage = ?", (p["target_passage"],)).fetchone()[0]
        insert_row(conn, "choices", story_id, {"from_passage": p["target_passage"], "to_passage": to, "text": pl["text"], "sort_order": order,
                                            "condition": {}, "effects": {}})
    elif kind == "new_variable":
        insert_row(conn, "variables", story_id, {"name": pl["name"], "kind": pl["kind"], "default": pl.get("default")})
    # 'note' materialises nothing; accepting it just files it away

    from . import db
    db.touch_story(conn, story_id)
