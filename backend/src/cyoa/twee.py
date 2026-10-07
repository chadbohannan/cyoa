"""Twee 3 export: the story tables rendered as a Twine-importable text file.

Twee has passages, links and an IFID but no conditions or effects, so those
are rendered as SugarCube macros — `<<if>>` around a gated link, `<<link>>`
with `<<set>>` for a choice with effects, and a `StoryInit` passage declaring
the variables. Everything else maps one to one; the mapping table lives in the
wiki's interchange-formats page.
"""

from __future__ import annotations

import json
from typing import Any

from . import engine

FORMAT = {"format": "SugarCube", "format-version": "2.37.3"}


def export(story: dict[str, Any], passages: list[dict[str, Any]], choices: list[dict[str, Any]], variables: list[dict[str, Any]]) -> str:
    names = _unique_names(passages)
    start = names.get(story["start_passage_id"]) if story["start_passage_id"] is not None else None
    data: dict[str, Any] = {"ifid": story["ifid"].upper(), **FORMAT}
    if start:
        data["start"] = start
    out = [f":: StoryTitle\n{story['title']}\n", f":: StoryData\n{json.dumps(data, indent=2)}\n"]
    if variables:
        sets = "\n".join(f"<<set ${v['name']} to {_literal(v['default'])}>>" for v in variables)
        out.append(f":: StoryInit\n{sets}\n")
    by_passage: dict[int, list[dict[str, Any]]] = {}
    for c in sorted(choices, key=lambda c: (c["sort_order"], c["id"])):
        by_passage.setdefault(c["from_passage"], []).append(c)
    for p in sorted(passages, key=lambda p: p["id"]):
        tags = [t for t, on in (("reviewed", p["reviewed"]), ("agent", p["provenance"] == "agent")) if on]
        header = f":: {_escape(names[p['id']])}"
        if tags:
            header += f" [{' '.join(tags)}]"
        header += f' {{"position":"{int(p["pos_x"])},{int(p["pos_y"])}"}}'
        body = p["body"].rstrip()
        links = [_link(c, names) for c in by_passage.get(p["id"], [])]
        out.append("\n".join(filter(None, [header, body, "\n".join(links) if links else ""])) + "\n")
    return "\n".join(out)


def _unique_names(passages: list[dict[str, Any]]) -> dict[int, str]:
    """Passage names are Twee's keys, so duplicates get their id appended."""
    names: dict[int, str] = {}
    taken: set[str] = set()
    for p in sorted(passages, key=lambda p: p["id"]):
        name = p["title"].strip() or f"Passage {p['id']}"
        if name.casefold() in taken:
            name = f"{name} ({p['id']})"
        taken.add(name.casefold())
        names[p["id"]] = name
    return names


def _escape(name: str) -> str:
    return name.replace("[", "\\[").replace("]", "\\]").replace("{", "\\{").replace("}", "\\}")


def _link(c: dict[str, Any], names: dict[int, str]) -> str:
    if c["to_passage"] is None or c["to_passage"] not in names:
        return f"/* dangling: {c['text']} */"
    target = names[c["to_passage"]]
    sets = _effects(c["effects"])
    if sets:
        line = f'<<link {json.dumps(c["text"])} {json.dumps(target)}>>{sets}<</link>>'
    else:
        line = f"[[{c['text']}->{target}]]"
    cond = _condition(c["condition"])
    return f"<<if {cond}>>{line}<</if>>" if cond else line


def _condition(cond: Any) -> str | None:
    """A condition as a SugarCube expression, or None for the always-true one."""
    if not isinstance(cond, dict) or not cond:
        return None
    for key, joiner in (("all", " and "), ("any", " or ")):
        if key in cond:
            parts = [_condition(c) for c in cond[key]] if isinstance(cond[key], list) else []
            parts = [p for p in parts if p]
            return f"({joiner.join(parts)})" if parts else None
    if cond.get("op") in engine.OPS and isinstance(cond.get("var"), str):
        op = "is" if cond["op"] == "==" else "isnot" if cond["op"] == "!=" else cond["op"]
        return f"${cond['var']} {op} {_literal(cond.get('value'))}"
    return None


def _effects(effects: Any) -> str:
    if not isinstance(effects, dict):
        return ""
    parts = [f"<<set ${n} to {_literal(v)}>>" for n, v in engine._items(effects.get("set"))]
    parts += [f"<<set ${n} += {_literal(v)}>>" for n, v in engine._items(effects.get("add"))]
    parts += [f"<<set ${n} += 1>>" for n in engine._names(effects.get("give"))]
    parts += [f"<<set ${n} to Math.max(0, ${n} - 1)>>" for n in engine._names(effects.get("take"))]
    return "".join(parts)


def _literal(value: Any) -> str:
    return json.dumps(value)  # true/false/null/numbers/strings all read as JS literals
