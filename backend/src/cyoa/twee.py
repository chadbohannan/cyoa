"""Twee 3 export: the story tables rendered as a Twine-importable text file.

A story is passages and links with nothing else (concepts/stateless-story.md), which is exactly
what Twee holds, so the export is plain: a title, an IFID, one section per passage and a
`[[text->target]]` link per choice. StoryData names SugarCube, but nothing in the file needs a
particular story format (emphasis is written `//x//` and `''x''`, which SugarCube and Harlowe both
render). The mapping table lives in the wiki's interchange-formats page.
"""

from __future__ import annotations

import json
import re
from typing import Any

FORMAT = {"format": "SugarCube", "format-version": "2.37.3"}


def export(story: dict[str, Any], passages: list[dict[str, Any]], choices: list[dict[str, Any]]) -> str:
    names = _unique_names(passages)
    start = names.get(story["start_passage_id"]) if story["start_passage_id"] is not None else None
    data: dict[str, Any] = {"ifid": story["ifid"].upper(), **FORMAT}
    if start:
        data["start"] = start
    out = [f":: StoryTitle\n{story['title']}\n", f":: StoryData\n{json.dumps(data, indent=2)}\n"]
    by_passage: dict[int, list[dict[str, Any]]] = {}
    for c in sorted(choices, key=lambda c: (c["sort_order"], c["id"])):
        by_passage.setdefault(c["from_passage"], []).append(c)
    for p in sorted(passages, key=lambda p: p["id"]):
        tags = [t for t, on in (("reviewed", p["reviewed"]), ("agent", p["provenance"] == "agent")) if on]
        header = f":: {_escape(names[p['id']])}"
        if tags:
            header += f" [{' '.join(tags)}]"
        header += f' {{"position":"{int(p["pos_x"])},{int(p["pos_y"])}"}}'
        body = _body(p["body"])
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


def _body(text: str) -> str:
    """Passage prose as the editor holds it (``*emphasis*``, ``**strong**``) in SugarCube/Harlowe
    markup (`//` and `''`), since a bare asterisk shows literally in both."""
    text = re.sub(r"\*\*(.+?)\*\*", r"''\1''", text.rstrip(), flags=re.S)
    return re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"//\1//", text, flags=re.S)


def _link(c: dict[str, Any], names: dict[int, str]) -> str:
    if c["to_passage"] is None or c["to_passage"] not in names:
        return f"/* dangling: {c['text']} */"
    return f"[[{c['text']}->{names[c['to_passage']]}]]"
