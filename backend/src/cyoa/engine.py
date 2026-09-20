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

from collections import deque
from typing import Any, Iterable

State = dict[str, Any]

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
        "endings": [pid for pid in ids if not out_of[pid]],
    }


def _items(d: Any) -> list[tuple[str, Any]]:
    return [(k, v) for k, v in d.items() if isinstance(k, str)] if isinstance(d, dict) else []


def _names(xs: Any) -> list[str]:
    return [x for x in xs if isinstance(x, str)] if isinstance(xs, list) else []


def _numeric(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)
