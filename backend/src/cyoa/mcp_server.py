"""CYOA as an MCP server (stdio): the story store and engine, handed to one long-running agent.

The LangGraph workflows in agents.py cut authoring into small model-bound steps. This is the opposite
experiment (concepts/mcp-authoring.md): no workflow, no proposals, no review gate. A general harness (Claude
Code, Codex, OpenCode, ...) is the author and these tools are its hands. They write straight to the same
SQLite file the editor reads, so the map shows the book growing live.

A story here is passages and choices only: no variables, conditions or effects (concepts/stateless-story.md).
What a reader has done is the route they took, so there is no state to track or declare.

Tools are deterministic except that `get_passage` attaches a "story so far" per route from a local summariser
(summarize.py). That is automatic, not a verb. Every call is logged to `tool_calls` under CYOA_MCP_LABEL so
runs by different harnesses can be compared.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import sqlite3
import time
import uuid
from typing import Any

from fastapi import HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import canon as canon_mod, db, embed, engine, judge as judge_mod, summarize, twee



class Refused(ToolError, ValueError):
    """A call the store cannot honour (unknown id, duplicate title, ...). A ToolError so the model is shown the
    message; any other exception is a crash and the model sees only "Error executing tool"."""


LABEL = os.environ.get("CYOA_MCP_LABEL", "")
# the intended reader hardware shows at most this many options on a screen: a hard limit, not a style note
MAX_CHOICES = 3

mcp = MCPServer(
    "cyoa",
    instructions=(
        "Tools for writing a branching choose-your-own-adventure book stored in SQLite. Start with "
        "story_overview, read the canon (lore_list), then grow the book from frontier(): passages whose choices "
        "lead nowhere yet. get_passage returns a passage with a summary of the story so far on each route that "
        "reaches it. A story has no variables: a route is its own history, so a fact that differs between "
        "timelines belongs in separate branches, not in a flag. Writes are immediate; run problems(), playtest() and "
        "endings_report() to check your work. Passage titles are unique per story. Plan the shape before writing: "
        "record in the lore a table of every intended ending with its depth and what it costs or gives, make the tree "
        "ragged (some decisions offer two or three options: never more than three, a hard limit of the reader's hardware), "
        "and make the choices differ in kind (act, refuse, deceive, flee, call for help, destroy), not just yes/no. "
        "Ragged means side branches of different lengths, not only endings at different depths: a few choices may end the "
        "story at once, but most wrong turns should run two or three decisions as a false trail with choices of its own "
        "(and endings of its own, good as well as bad) before they end. A book where every decision is 'end now or go on' "
        "is a spine with exits. Vary the order of options too: if the way on or the instant exit sits in the same slot at "
        "most decisions, the reader learns the slot instead of the story. story_overview's `shape` measures both. "
        "Run nearest_endings() while planning siblings and after writing; two endings that are close and split early "
        "have converged, so change what one of them costs the reader. Canon that a route must show before a passage can "
        "rely on it (a clue, a secret, a promise) goes in the lore as kind 'fact', one entry per fact; tag each passage with "
        "the facts it `reveals` and the facts it `needs` the reader to know already, and give each ending a `verdict` "
        "(e.g. solved / failed). fact_check() then proves every route reveals what its passages rely on, and get_passage "
        "shows which facts the reader knows on arrival. canon_check() has a local model read passages against the canon "
        "and the story so far on their route; run it on what you wrote, and reread the passages it flags. lint_passages() has a decision model check passages for named craft "
        "defects (a closing moral, a clock-time closer, a summary instead of a scene); reread what it flags. quality_map() "
        "measures every passage on yes/no qualities and names the weakest branch to rewrite."),
)


def tool(fn):
    """Register `fn` as a tool, logging each call and turning HTTP errors from the shared helpers into messages."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        t0, err = time.monotonic(), None
        try:
            return fn(*args, **kwargs)
        except HTTPException as e:
            err = str(e.detail)
            raise Refused(err) from None
        except ValueError as e:
            err = str(e)
            raise Refused(err) from None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            raise
        finally:
            try:
                with db.transaction() as conn:
                    sid = inspect.signature(fn).bind_partial(*args, **kwargs).arguments.get("story_id")
                    if sid is not None and conn.execute("SELECT 1 FROM stories WHERE id = ?", (sid,)).fetchone() is None:
                        sid = None
                    conn.execute(
                        "INSERT INTO tool_calls (story_id, label, tool, args, ok, error, ms, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (sid, LABEL, fn.__name__, json.dumps(kwargs or list(args), default=str)[:2000], err is None, err,
                         int((time.monotonic() - t0) * 1000), time.time()))
            except Exception:
                pass  # logging must never fail a tool
    return mcp.tool()(run)


# --- helpers -------------------------------------------------------------------


def _story(conn: sqlite3.Connection, story_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if row is None:
        raise ValueError(f"story {story_id} not found")
    return row


def _rows(conn: sqlite3.Connection, table: str, story_id: int, order: str = "id") -> list[dict[str, Any]]:
    return [engine.decode_row(table, r) for r in conn.execute(f"SELECT * FROM {table} WHERE story_id = ? ORDER BY {order}", (story_id,))]


def _passage(conn: sqlite3.Connection, story_id: int, passage_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM passages WHERE id = ? AND story_id = ?", (passage_id, story_id)).fetchone()
    if row is None:
        raise ValueError(f"passage {passage_id} is not in story {story_id}")
    return engine.decode_row("passages", row)


def _brief(p: dict[str, Any]) -> dict[str, Any]:
    return {"id": p["id"], "title": p["title"], "body": p["body"], "is_ending": bool(p["is_ending"]), "t": p["t"], "meta": p["meta"]}


def _choice_out(c: dict[str, Any], titles: dict[int, str]) -> dict[str, Any]:
    return {"id": c["id"], "text": c["text"], "to_passage": c["to_passage"], "to_title": titles.get(c["to_passage"])}


def _add_choice(conn: sqlite3.Connection, story_id: int, from_passage: int, text: str, to_passage: int | None) -> dict[str, Any]:
    have = conn.execute("SELECT COUNT(*) FROM choices WHERE from_passage = ?", (from_passage,)).fetchone()[0]
    if have >= MAX_CHOICES:
        raise ValueError(f"passage {from_passage} already has {have} choices: the reader's hardware shows at most {MAX_CHOICES}. "
                         "Fold two options into one, or move one decision to a later passage")
    order = conn.execute("SELECT COALESCE(MAX(sort_order) + 1, 0) FROM choices WHERE from_passage = ?", (from_passage,)).fetchone()[0]
    return engine.insert_row(conn, "choices", story_id, {
        "from_passage": from_passage, "to_passage": to_passage, "text": text, "sort_order": order})


def _revise(conn: sqlite3.Connection, story_id: int, lore_id: int, action: str) -> None:
    """Keep the version of a lore entry that `action` is about to replace."""
    conn.execute("INSERT INTO lore_revisions (story_id, lore_id, kind, name, body, action, label) "
                 "SELECT story_id, id, kind, name, body, ?, ? FROM lore WHERE id = ? AND story_id = ?", (action, LABEL, lore_id, story_id))


def _facts(conn: sqlite3.Connection, story_id: int) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM lore WHERE story_id = ? AND kind = 'fact' ORDER BY id", (story_id,))]


def _tag(conn: sqlite3.Connection, story_id: int, meta: dict[str, Any], reveals: list[str] | None, needs: list[str] | None,
         verdict: str | None) -> dict[str, Any]:
    """`meta` with the given fact tags and verdict set (None leaves a field as it is, an empty value clears it). Fact names
    must match a lore entry of kind 'fact' (case-insensitive) and are stored as that entry spells them."""
    meta = dict(meta or {})
    names = {f.casefold(): f for f in _facts(conn, story_id)}
    for key, given in (("reveals", reveals), ("needs", needs)):
        if given is None:
            continue
        unknown = [f for f in given if f.strip().casefold() not in names]
        if unknown:
            raise ValueError(f"{key}: {unknown} match no lore entry of kind 'fact' (known facts: {sorted(names.values()) or 'none yet'}). "
                             "Add the fact with lore_save first")
        meta[key] = list(dict.fromkeys(names[f.strip().casefold()] for f in given))
        if not meta[key]:
            del meta[key]
    if verdict is not None:
        if verdict.strip():
            meta["verdict"] = verdict.strip()
        else:
            meta.pop("verdict", None)
    return meta


def _depths(story: sqlite3.Row, choices: list[dict[str, Any]], ids: list[int]) -> dict[int, int]:
    return {pid: engine.depth_from_start(pid, story["start_passage_id"], choices) for pid in ids}


# --- reading -------------------------------------------------------------------


@tool
def list_stories() -> list[dict[str, Any]]:
    """The stories in the database: id, title, premise, passage count."""
    with db.transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT s.id, s.title, s.description, (SELECT COUNT(*) FROM passages p WHERE p.story_id = s.id) AS passages "
            "FROM stories s ORDER BY s.updated_at DESC")]


@tool
def story_overview(story_id: int) -> dict[str, Any]:
    """Premise, start passage, counts, lore index, the pacing plan, open problems, how much frontier is left and `shape`:
    how ragged the tree is (how far each side branch runs before its ending, how many decisions are just "end now or go
    on"), which option slot holds the way on and the instant exits, where always picking one slot leads, and warnings.
    Read this first, and again after writing."""
    from . import agents

    with db.transaction() as conn:
        story = _story(conn, story_id)
        passages, choices = _rows(conn, "passages", story_id), _rows(conn, "choices", story_id)
        probs = engine.problems(story["start_passage_id"], passages, choices)
        plan = conn.execute("SELECT plan FROM pacing WHERE story_id = ?", (story_id,)).fetchone()
        return {
            "id": story_id, "title": story["title"], "premise": story["description"], "start_passage_id": story["start_passage_id"],
            "passages": len(passages), "choices": len(choices),
            "lore": [{"id": r["id"], "kind": r["kind"], "name": r["name"]} for r in conn.execute(
                "SELECT id, kind, name FROM lore WHERE story_id = ? ORDER BY kind, name", (story_id,))],
            "pacing": json.loads(plan["plan"]) if plan else agents.DEFAULT_PLAN,
            "problems": {k: (v if k in ("no_start",) else len(v)) for k, v in probs.items()},
            "frontier": len(probs["dangling"]) + len(probs["unfinished"]),
            "shape": engine.shape(story["start_passage_id"], passages, choices),
        }


@tool
def get_passage(story_id: int, passage_id: int, routes: int = 2) -> dict[str, Any]:
    """A passage with its outgoing choices and how a reader arrives at it: for up to `routes` shortest routes
    from the start, the passages passed through, the last choice taken, and a summary of the story so far along
    that route (so you need not reread the ancestors). `facts_on_arrival` lists the lore facts every route here has
    revealed (`known`) and those only some routes have: the passage may rely on the first, never on the second."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        passages = {p["id"]: p for p in _rows(conn, "passages", story_id)}
        if passage_id not in passages:
            raise ValueError(f"passage {passage_id} is not in story {story_id}")
        choices = _rows(conn, "choices", story_id, "from_passage, sort_order, id")
    titles = {pid: p["title"] for pid, p in passages.items()}
    flow = canon_mod.fact_flow(story["start_passage_id"], passages, choices).get(passage_id)
    return {
        "passage": _brief(passages[passage_id]),
        "facts_on_arrival": {"known": sorted(flow["known"]), "on_some_routes_only": sorted(flow["maybe"])} if flow else None,
        "choices": [_choice_out(c, titles) for c in choices if c["from_passage"] == passage_id],
        "arrival": summarize.arrivals(story_id, passage_id, story["start_passage_id"], passages, choices, max(1, routes)),
        "incoming": [{"choice_id": c["id"], "from_passage": c["from_passage"], "from_title": titles.get(c["from_passage"]), "text": c["text"]}
                     for c in choices if c["to_passage"] == passage_id],
    }


@tool
def find_passages(story_id: int, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Passages whose title or body contains `query` as whole words (case-insensitive), with a short excerpt around the match."""
    pat = _word_pattern(query)
    out = []
    with db.transaction() as conn:
        for p in _rows(conn, "passages", story_id):
            hay = f"{p['title']}\n{p['body']}"
            m = pat.search(hay)
            if m:
                out.append({"id": p["id"], "title": p["title"], "excerpt": hay[max(0, m.start() - 80): m.end() + 120].replace("\n", " ")})
    return out[:limit]


@tool
def frontier(story_id: int, limit: int = 25) -> dict[str, Any]:
    """What is left to write, shallowest first: choices that lead nowhere yet, and passages with no choices that
    are not marked as endings. Grow the book from here."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        passages = {p["id"]: p for p in _rows(conn, "passages", story_id)}
        choices = _rows(conn, "choices", story_id)
    depth = _depths(story, choices, list(passages))
    has_out = {c["from_passage"] for c in choices}
    dangling = [{"choice_id": c["id"], "text": c["text"], "from_passage": c["from_passage"], "from_title": passages[c["from_passage"]]["title"],
                 "depth": depth[c["from_passage"]] + 1} for c in choices if c["to_passage"] is None and c["from_passage"] in passages]
    unfinished = [{"passage_id": p["id"], "title": p["title"], "depth": depth[p["id"]]} for p in passages.values()
                  if p["id"] not in has_out and not p["is_ending"]]
    return {"dangling_choices": sorted(dangling, key=lambda d: d["depth"])[:limit], "unfinished_passages": sorted(unfinished, key=lambda d: d["depth"])[:limit],
            "totals": {"dangling_choices": len(dangling), "unfinished_passages": len(unfinished)}}


@tool
def problems(story_id: int) -> dict[str, Any]:
    """Static checks: no start, unreachable passages, dangling choices, unfinished passages, merge candidates."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        return engine.problems(story["start_passage_id"], _rows(conn, "passages", story_id), _rows(conn, "choices", story_id))


@tool
def playtest(story_id: int, runs: int = 200, seed: int | None = None) -> dict[str, Any]:
    """Random playthroughs over the real graph: what is never visited, which endings are reached and how often, which choices
    lead nowhere, and `verdicts`: how many walks ended in each ending verdict."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        if story["start_passage_id"] is None:
            raise ValueError("story has no start passage")
        passages = _rows(conn, "passages", story_id)
        out = engine.random_playthroughs(story["start_passage_id"], passages, _rows(conn, "choices", story_id), runs=runs, seed=seed)
    verdict = {p["id"]: (p["meta"] or {}).get("verdict") or "(none)" for p in passages}
    out["verdicts"] = {}
    for pid, n in {**out["endings"], **out["unfinished"]}.items():
        out["verdicts"][verdict[pid]] = out["verdicts"].get(verdict[pid], 0) + n
    return out


@tool
def export_twee(story_id: int) -> str:
    """The whole story as Twee 3 text."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        return twee.export(dict(story), _rows(conn, "passages", story_id), _rows(conn, "choices", story_id))


@mcp.resource("cyoa://story/{story_id}/canon")
def canon(story_id: int) -> str:
    """The premise and lore as one document."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        lore = _rows(conn, "lore", story_id, "kind, name")
    parts = [f"# {story['title']}\n\n{story['description']}"]
    parts += [f"## {r['name']} ({r['kind']})\n\n{r['body']}" for r in lore]
    return "\n\n".join(parts)


# --- writing -------------------------------------------------------------------


@tool
def create_story(title: str, premise: str = "") -> dict[str, Any]:
    """A new, empty story (no passages yet; the first add_passage becomes the start)."""
    with db.transaction() as conn:
        cur = conn.execute("INSERT INTO stories (title, description, ifid) VALUES (?, ?, ?)", (title, premise, str(uuid.uuid4()).upper()))
        return {"id": cur.lastrowid, "title": title}


@tool
def edit_story(story_id: int, title: str | None = None, premise: str | None = None, start_passage_id: int | None = None) -> dict[str, Any]:
    """Change the title, premise or start passage."""
    with db.transaction() as conn:
        _story(conn, story_id)
        if start_passage_id is not None:
            _passage(conn, story_id, start_passage_id)
        sets = {k: v for k, v in (("title", title), ("description", premise), ("start_passage_id", start_passage_id)) if v is not None}
        if sets:
            conn.execute(f"UPDATE stories SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), story_id))
            db.touch_story(conn, story_id)
        row = _story(conn, story_id)
        return {"id": story_id, "title": row["title"], "premise": row["description"], "start_passage_id": row["start_passage_id"]}


@tool
def add_passage(story_id: int, title: str, body: str, fill_choice: int | None = None, from_passage: int | None = None,
                choice_text: str | None = None, is_ending: bool = False, reveals: list[str] | None = None,
                needs: list[str] | None = None, verdict: str | None = None) -> dict[str, Any]:
    """Write a new passage and wire it in one step. Either `fill_choice` (a dangling choice id from frontier(),
    which then leads here) or `from_passage` + `choice_text` (a new choice out of that passage). With neither, it
    becomes the start if the story has none, else it is left unlinked. A duplicate title gets a ' (2)' suffix.
    An ending is a decided, terminal passage; mark it is_ending=True and give it no choices. `reveals` and `needs` name
    lore facts (kind 'fact') this passage shows the reader and relies on them already knowing; `verdict` labels an
    ending's outcome (e.g. solved / failed). See fact_check()."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        meta = _tag(conn, story_id, {}, reveals, needs, verdict)
        if fill_choice is not None:
            ch = conn.execute("SELECT * FROM choices WHERE id = ? AND story_id = ?", (fill_choice, story_id)).fetchone()
            if ch is None:
                raise ValueError(f"choice {fill_choice} is not in story {story_id}")
            if ch["to_passage"] is not None:
                raise ValueError(f"choice {fill_choice} already leads to passage {ch['to_passage']}; use update_choice to repoint it")
        elif from_passage is not None:
            _passage(conn, story_id, from_passage)
            if not choice_text:
                raise ValueError("choice_text is required with from_passage")
        p = engine.insert_row(conn, "passages", story_id, {"title": title, "body": body, "is_ending": is_ending, "provenance": "agent", "meta": meta})
        if fill_choice is not None:
            conn.execute("UPDATE choices SET to_passage = ? WHERE id = ?", (p["id"], fill_choice))
        elif from_passage is not None:
            _add_choice(conn, story_id, from_passage, choice_text, p["id"])
        elif story["start_passage_id"] is None:
            conn.execute("UPDATE stories SET start_passage_id = ? WHERE id = ?", (p["id"], story_id))
        db.touch_story(conn, story_id)
        return {"id": p["id"], "title": p["title"]}


@tool
def update_passage(story_id: int, passage_id: int, title: str | None = None, body: str | None = None, is_ending: bool | None = None,
                   t: float | None = None, meta: dict[str, Any] | None = None, reveals: list[str] | None = None,
                   needs: list[str] | None = None, verdict: str | None = None) -> dict[str, Any]:
    """Rewrite a passage. Only the fields you pass change. `t` is story time, `meta` {"location", "present": [...]} replaces
    the whole meta; `reveals`, `needs` and `verdict` set just those (an empty list or string clears one)."""
    with db.transaction() as conn:
        current = _passage(conn, story_id, passage_id)
        if reveals is not None or needs is not None or verdict is not None:
            meta = _tag(conn, story_id, meta if meta is not None else current["meta"], reveals, needs, verdict)
        if title is not None and db.title_taken(conn, story_id, title, passage_id):
            raise ValueError(f"another passage is already titled '{title.strip()}': titles must be unique")
        sets = {k: v for k, v in (("title", title), ("body", body), ("is_ending", None if is_ending is None else int(is_ending)),
                                  ("t", t), ("meta", None if meta is None else json.dumps(meta))) if v is not None}
        if sets:
            conn.execute(f"UPDATE passages SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), passage_id))
            db.touch_story(conn, story_id)
        return _brief(_passage(conn, story_id, passage_id))


@tool
def delete_passage(story_id: int, passage_id: int) -> dict[str, Any]:
    """Delete one passage and its outgoing choices. Choices that led to it become dangling (frontier again)."""
    with db.transaction() as conn:
        _passage(conn, story_id, passage_id)
        dangling = [r["id"] for r in conn.execute("SELECT id FROM choices WHERE to_passage = ?", (passage_id,))]
        conn.execute("DELETE FROM passages WHERE id = ?", (passage_id,))
        db.touch_story(conn, story_id)
        return {"deleted": passage_id, "now_dangling_choices": dangling}


@tool
def prune_below(story_id: int, passage_id: int) -> dict[str, Any]:
    """Delete everything below a passage (its choices and every passage only reachable through it), leaving it a frontier
    passage again. Passages another route from the start still reaches survive."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        _passage(conn, story_id, passage_id)
        passages, choices = _rows(conn, "passages", story_id), _rows(conn, "choices", story_id)
        doomed = engine.prune_set(passage_id, story["start_passage_id"], passages, choices)
        sources = [passage_id, *doomed]
        gone = [c["id"] for c in choices if c["from_passage"] in sources]
        conn.executemany("DELETE FROM choices WHERE id = ?", [(i,) for i in gone])
        conn.executemany("DELETE FROM passages WHERE id = ?", [(i,) for i in doomed])
        db.touch_story(conn, story_id)
        return {"deleted_passages": sorted(doomed), "deleted_choices": sorted(gone)}


@tool
def add_choice(story_id: int, from_passage: int, text: str, to_passage: int | None = None) -> dict[str, Any]:
    """A choice out of a passage. Omit to_passage to leave it dangling (it shows up in frontier()). Pass an existing passage only to
    rejoin routes at a scene whose prose reads the same however the reader got there: there is no state, so a rejoined passage cannot
    know which route came in."""
    with db.transaction() as conn:
        _passage(conn, story_id, from_passage)
        if to_passage is not None:
            _passage(conn, story_id, to_passage)
        c = _add_choice(conn, story_id, from_passage, text, to_passage)
        return {"id": c["id"], "from_passage": from_passage, "to_passage": to_passage}


@tool
def update_choice(story_id: int, choice_id: int, text: str | None = None, to_passage: int | None = None, unlink: bool = False) -> dict[str, Any]:
    """Change a choice's text. to_passage repoints it; unlink=True makes it dangling again."""
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM choices WHERE id = ? AND story_id = ?", (choice_id, story_id)).fetchone()
        if row is None:
            raise ValueError(f"choice {choice_id} is not in story {story_id}")
        if to_passage is not None:
            _passage(conn, story_id, to_passage)
        sets: dict[str, Any] = {k: v for k, v in (("text", text), ("to_passage", to_passage)) if v is not None}
        if unlink:
            sets["to_passage"] = None
        if sets:
            conn.execute(f"UPDATE choices SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), choice_id))
            db.touch_story(conn, story_id)
        return engine.decode_row("choices", conn.execute("SELECT * FROM choices WHERE id = ?", (choice_id,)).fetchone())


@tool
def delete_choice(story_id: int, choice_id: int) -> dict[str, Any]:
    """Delete a choice (the passage it led to is kept)."""
    with db.transaction() as conn:
        if conn.execute("DELETE FROM choices WHERE id = ? AND story_id = ?", (choice_id, story_id)).rowcount == 0:
            raise ValueError(f"choice {choice_id} is not in story {story_id}")
        db.touch_story(conn, story_id)
        return {"deleted": choice_id}


# --- canon -----------------------------------------------------------------------


@tool
def lore_list(story_id: int) -> list[dict[str, Any]]:
    """The canon: every lore entry (character, place, faction, rule, fact) in full. Treat as true on every branch."""
    with db.transaction() as conn:
        _story(conn, story_id)
        return [{"id": r["id"], "kind": r["kind"], "name": r["name"], "body": r["body"]} for r in _rows(conn, "lore", story_id, "kind, name")]


@tool
def lore_save(story_id: int, name: str, kind: str, body: str, lore_id: int | None = None) -> dict[str, Any]:
    """Add or revise a lore entry. Updates lore_id if given, else the entry with the same kind and name, else creates one.
    kind: character | place | faction | rule | fact. A fact is one thing a route must show the reader before a passage
    may rely on it; give it a short name (passages cite it by name in `reveals` / `needs`) and the full statement as body.
    Renaming a fact leaves passages citing the old name, which fact_check() reports."""
    if kind not in ("character", "place", "faction", "rule", "fact"):
        raise ValueError("kind must be character, place, faction, rule or fact")
    with db.transaction() as conn:
        _story(conn, story_id)
        row = (conn.execute("SELECT id FROM lore WHERE id = ? AND story_id = ?", (lore_id, story_id)) if lore_id is not None else
               conn.execute("SELECT id FROM lore WHERE story_id = ? AND kind = ? AND name = ?", (story_id, kind, name))).fetchone()
        if row:
            _revise(conn, story_id, row["id"], "update")
            conn.execute("UPDATE lore SET kind = ?, name = ?, body = ? WHERE id = ?", (kind, name, body, row["id"]))
            db.touch_story(conn, story_id)
            return {"id": row["id"], "updated": True}
        return {"id": engine.insert_row(conn, "lore", story_id, {"kind": kind, "name": name, "body": body})["id"], "updated": False}


@tool
def lore_delete(story_id: int, lore_id: int) -> dict[str, Any]:
    """Remove a lore entry."""
    with db.transaction() as conn:
        _revise(conn, story_id, lore_id, "delete")
        if conn.execute("DELETE FROM lore WHERE id = ? AND story_id = ?", (lore_id, story_id)).rowcount == 0:
            raise ValueError(f"lore {lore_id} is not in story {story_id}")
        db.touch_story(conn, story_id)
        return {"deleted": lore_id}


@tool
def lore_history(story_id: int, lore_id: int) -> list[dict[str, Any]]:
    """Earlier versions of a lore entry, newest first (what each lore_save or lore_delete replaced)."""
    with db.transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, name, body, action, at FROM lore_revisions WHERE story_id = ? AND lore_id = ? ORDER BY id DESC", (story_id, lore_id))]


def _word_pattern(text: str):
    import re

    q = re.escape(text.strip())
    return re.compile((r"\b" if text.strip()[:1].isalnum() else "") + q + (r"\b" if text.strip()[-1:].isalnum() else ""), re.I)


@tool
def lore_impact(story_id: int, lore_id: int) -> dict[str, Any]:
    """Where a lore entry is in play, to review after changing it: the passages whose title or body mention its name (whole words,
    case-insensitive, ignoring a leading 'HINGE:' or 'The ' and any parenthetical; for a character each word of the name counts too, so 'Wren' finds Wren Tolliver). Mentions are by text, so a paraphrase is missed:
    reread the routes through the hinge, not just the hits."""
    import re

    with db.transaction() as conn:
        entry = conn.execute("SELECT * FROM lore WHERE id = ? AND story_id = ?", (lore_id, story_id)).fetchone()
        if entry is None:
            raise ValueError(f"lore {lore_id} is not in story {story_id}")
        passages = _rows(conn, "passages", story_id)
    term = re.sub(r"^\s*HINGE:\s*", "", entry["name"], flags=re.I)
    term = re.sub(r"\s*\(.*?\)", "", term).strip()
    term = re.sub(r"^the\s+", "", term, flags=re.I)
    # a character is mostly named by one word ("Wren" for Wren Tolliver), so each word of its name counts too
    terms = [term] + ([w for w in re.findall(r"[A-Za-z']{4,}", term) if w.casefold() != "the"] if entry["kind"] == "character" else [])
    pats = [_word_pattern(t) for t in dict.fromkeys(terms)]
    hits = []
    for p in passages:
        text = f"{p['title']}\n{p['body']}"
        n = sum(len(pat.findall(text)) for pat in pats)
        if n:
            hits.append({"id": p["id"], "title": p["title"], "mentions": n})
    out = {"entry": {"id": lore_id, "kind": entry["kind"], "name": entry["name"]}, "searched_for": list(dict.fromkeys(terms)), "passages_mentioning": hits}
    if entry["kind"] == "fact":
        cites = lambda key: [{"id": p["id"], "title": p["title"]} for p in passages if entry["name"] in canon_mod.fact_names(p["meta"], key)]
        out |= {"passages_revealing": cites("reveals"), "passages_needing": cites("needs")}
    return out


def _shingles(text: str, n: int = 4) -> set[tuple[str, ...]]:
    import re

    words = re.findall(r"[a-z']+", text.casefold())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


@tool
def endings_report(story_id: int, routes: int = 3, similar_over: float = 0.15) -> dict[str, Any]:
    """Every ending (decided endings and dead-end passages) with up to `routes` shortest routes to it and its opening lines, and
    `similar`: pairs of endings whose text overlaps (share many 4-word runs, Jaccard score), the usual sign that two timelines have
    converged on the same ending. Compare them for different consequences, not different wording."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        passages = {p["id"]: p for p in _rows(conn, "passages", story_id)}
        choices = _rows(conn, "choices", story_id)
    has_out = {c["from_passage"] for c in choices}
    out, grams = [], {}
    for p in passages.values():
        if not p["is_ending"] and p["id"] in has_out:
            continue
        rs = summarize.routes_to(p["id"], story["start_passage_id"], choices, max(1, routes))
        out.append({"id": p["id"], "title": p["title"], "is_ending": bool(p["is_ending"]), "verdict": (p["meta"] or {}).get("verdict"),
                    "reachable": p["id"] == story["start_passage_id"] or len(rs[0]) > 1,
                    "opening": " ".join(p["body"].split())[:240], "routes": [[passages[i]["title"] for i, _ in r] for r in rs]})
        grams[p["title"]] = _shingles(p["body"])
    titles = list(grams)
    similar = []
    for i, a in enumerate(titles):
        for b in titles[i + 1:]:
            union = grams[a] | grams[b]
            score = len(grams[a] & grams[b]) / len(union) if union else 0.0
            if score >= similar_over:
                similar.append({"a": a, "b": b, "score": round(score, 3)})
    return {"endings": out, "similar": sorted(similar, key=lambda x: -x["score"])}


@tool
def nearest_endings(story_id: int, nearest: int = 2, top: int = 8) -> dict[str, Any]:
    """Semantic convergence check over the endings. Each ending's text is embedded, the story-wide average is subtracted (so shared
    setting and vocabulary don't count) and cosine similarity is taken. Returns `closest`: the `top` most similar pairs, each with
    `diverged_after` (how many passages their shortest routes share: a pair that split early yet ends alike is the convergence to fix),
    and `per_ending`: each ending's `nearest` neighbours. Scores are relative to this book, not absolute: look at the ranking and read the
    pairs at the top, do not treat a number as a verdict. Run it while planning siblings as well as after writing."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        passages = {p["id"]: p for p in _rows(conn, "passages", story_id)}
        choices = _rows(conn, "choices", story_id)
    has_out = {c["from_passage"] for c in choices}
    ends = [p for p in passages.values() if p["is_ending"] or p["id"] not in has_out]
    if len(ends) < 3:
        raise ValueError("need at least three endings to compare")
    try:
        vecs = embed.centre(embed.embed([f"{p['title']}. {p['body']}" for p in ends]))
    except Exception as e:
        raise ValueError(f"embedding server unavailable ({embed.URL}): {e}") from e
    route = {p["id"]: [i for i, _ in summarize.routes_to(p["id"], story["start_passage_id"], choices, 1)[0]] for p in ends}

    def shared(a: int, b: int) -> int:
        n = 0
        for x, y in zip(route[a], route[b]):
            if x != y:
                break
            n += 1
        return n

    sims = {(i, j): embed.cosine(vecs[i], vecs[j]) for i in range(len(ends)) for j in range(i + 1, len(ends))}

    def sim(i: int, j: int) -> float:
        return sims[(min(i, j), max(i, j))]

    def pair(i: int, j: int) -> dict[str, Any]:
        return {"a": ends[i]["title"], "b": ends[j]["title"], "similarity": round(sim(i, j), 3),
                "diverged_after": shared(ends[i]["id"], ends[j]["id"])}

    closest = [pair(i, j) for (i, j), _ in sorted(sims.items(), key=lambda kv: -kv[1])[:max(1, top)]]
    per = [{"ending": ends[i]["title"], "nearest": [
        {"title": ends[j]["title"], "similarity": round(sim(i, j), 3), "diverged_after": shared(ends[i]["id"], ends[j]["id"])}
        for j in sorted((j for j in range(len(ends)) if j != i), key=lambda j: -sim(i, j))[:max(1, nearest)]]}
        for i in range(len(ends))]
    values = list(sims.values())
    return {"endings": len(ends), "mean_similarity": round(sum(values) / len(values), 3), "closest": closest, "per_ending": per}


@tool
def fact_check(story_id: int) -> dict[str, Any]:
    """Route-aware fact check, deterministic. For every passage, each fact it `needs` must be revealed by an earlier
    passage on EVERY route from the start; `unmet_needs` lists each one that is not, with a route that arrives without it
    (fix by revealing the fact on that route, or by not relying on it). Also: `unknown_fact_names` (tags that match no
    fact entry, e.g. after a rename), `never_revealed` (facts no reachable passage shows), `verdicts` (ending outcomes,
    counting endings with none), and verdicts set on passages that are not endings. It checks only the tags: whether the
    prose matches them is canon_check()'s job, and yours."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        passages = {p["id"]: p for p in _rows(conn, "passages", story_id)}
        choices = _rows(conn, "choices", story_id)
        facts = _facts(conn, story_id)
    return canon_mod.fact_check(story["start_passage_id"], passages, choices, facts)


@tool
def canon_check(story_id: int, passage_ids: list[int] | None = None, limit: int = 4) -> dict[str, Any]:
    """A local model reads each passage, on up to two routes in, against the canon (premise and lore, rules included), the
    facts that route has revealed, the previous passage word for word and a summary of the rest, and lists contradictions.
    An issue is kept only if it quotes the passage and the text it conflicts with (`evidence_from` says where; "notes" is
    the summary, the least reliable); unbacked ones are counted in `dropped_unbacked`. A second pass asks which facts the
    prose uses: one this route has not revealed and the passage does not tag is reported (evidence_from "fact flow"), and
    `hints` lists tagged reveals the prose may not actually show. Checks `passage_ids` in order, or with none every
    reachable passage from the start down; at most `limit` passages need the model per call (results are cached by content,
    so rechecking unchanged passages is free) and `remaining` lists the rest. `unchecked`: replies unreadable even after a
    retry. The model is small: an issue is a pointer to reread, a clean result is no proof."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        passages = {p["id"]: p for p in _rows(conn, "passages", story_id)}
        choices = _rows(conn, "choices", story_id, "from_passage, sort_order, id")
        lore = _rows(conn, "lore", story_id, "kind, name")
    canon = canon_mod.canon_text(dict(story), lore)
    facts = {r["name"]: r["body"] for r in lore if r["kind"] == "fact"}
    if passage_ids is None:
        flow = canon_mod.fact_flow(story["start_passage_id"], passages, choices)
        order = {pid: engine.depth_from_start(pid, story["start_passage_id"], choices) for pid in flow}
        passage_ids = sorted(flow, key=lambda pid: (order[pid], pid))
    for pid in passage_ids:
        if pid not in passages:
            raise ValueError(f"passage {pid} is not in story {story_id}")
    flagged, clean, remaining, unchecked, hints, spent, dropped = [], 0, [], [], [], 0, 0
    for pid in passage_ids:
        if spent >= max(1, limit):
            remaining.append(pid)
            continue
        found, unseen = [], set()
        for route in summarize.routes_to(pid, story["start_passage_id"], choices, 2):
            steps = [{"id": i, "title": passages[i]["title"], "body": passages[i]["body"], "meta": passages[i]["meta"],
                      "via": via["text"] if via else None} for i, via in route]
            try:
                r = canon_mod.check_passage(story_id, canon, facts, steps)
            except Exception as e:
                raise ValueError(str(e) if isinstance(e, ValueError) else f"local model unavailable ({summarize.URL}): {e}") from e
            spent += 0 if r["cached"] else 1
            if r.get("error"):
                unchecked.append({"passage": pid, "title": passages[pid]["title"], "error": r["error"]})
            dropped += r["dropped"]
            unseen |= set(r.get("tagged_but_unseen", []))
            for i in r["issues"]:
                if not any((i["quote"], i["evidence"]) == (f["quote"], f["evidence"]) for f in found):
                    found.append(i | {"route": [passages[x]["title"] for x, _ in route[:-1]]})
        if unseen:
            hints.append({"passage": pid, "title": passages[pid]["title"], "tagged_reveals_not_seen_in_prose": sorted(unseen)})
        if found:
            flagged.append({"passage": pid, "title": passages[pid]["title"], "issues": found})
        elif not any(u["passage"] == pid for u in unchecked):
            clean += 1
    return {"flagged": flagged, "clean": clean, "remaining": remaining, "unchecked": unchecked, "hints": hints,
            "dropped_unbacked": dropped}


def _judging(story_id: int) -> tuple[sqlite3.Row, dict[int, dict[str, Any]], list[dict[str, Any]], dict[int, int], dict[int, list[str]]]:
    """What the decision-model tools read: the story, its passages, its choices, each passage's first parent (lowest choice id)
    and the choice texts leaving each passage."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        passages = {p["id"]: p for p in _rows(conn, "passages", story_id)}
        choices = _rows(conn, "choices", story_id, "from_passage, sort_order, id")
    parent = {c["to_passage"]: c["from_passage"] for c in sorted(choices, key=lambda c: -c["id"]) if c["to_passage"] is not None}
    options: dict[int, list[str]] = {}
    for c in choices:
        options.setdefault(c["from_passage"], []).append(c["text"])
    return story, passages, choices, parent, options


@tool
def lint_passages(story_id: int, passage_ids: list[int] | None = None, threshold: float = 0.6) -> dict[str, Any]:
    """A local decision model (System One, e.g. clef-flash) checks each passage, read whole with the passage before it, for
    named craft defects, each a yes/no question with a probability: `moral` (ends on a spelled-out lesson), `clock` (ends on
    a clock time), `summary` (told from a distance, not a scene), `explains_clue` (points out a clue and says what it proves) and
    `monologue` (mostly one character explaining). Returns the passages with a defect at or above `threshold`, most defects first,
    each with the defect names and probabilities. Checks `passage_ids`, or every passage. About a quarter of a second each. A flag
    is a pointer to reread, not a verdict: the model gives no reason and over-fires on any time mentioned near the end (`clock`).
    Use it to find what to reread, and after a rewrite to see whether the defect is gone; do not rank passages by it."""
    story, passages, choices, parent, _ = _judging(story_id)
    ids = passage_ids if passage_ids is not None else sorted(passages)
    for pid in ids:
        if pid not in passages:
            raise ValueError(f"passage {pid} is not in story {story_id}")
    flagged = []
    try:
        for pid in ids:
            got = judge_mod.lint(passages[pid]["body"], passages[parent[pid]]["body"] if pid in parent else "")
            hit = {k: v for k, v in got.items() if v >= threshold}
            if hit:
                flagged.append({"id": pid, "title": passages[pid]["title"], "defects": hit})
    except ConnectionError as e:
        raise ValueError(str(e)) from e
    flagged.sort(key=lambda f: (-len(f["defects"]), -max(f["defects"].values())))
    return {"checked": len(ids), "flagged": flagged}


@tool
def quality_map(story_id: int, metrics: dict[str, str] | None = None, min_size: int = 3, top: int = 3) -> dict[str, Any]:
    """Where the book is weakest. A local decision model (System One, e.g. clef-flash) answers yes/no `metrics` about every
    passage, read whole with the passage before it and the choices leaving it; a passage's quality `q` is the mean P(yes). Default
    metrics: vivid (specific sensory detail), action (something physically happening now), clear (where the reader is and what is
    happening), tight (no padding) and tension (something could go wrong next). Pass your own as {name: yes/no question worded so
    that yes is good} to measure what this book needs (period feel, humour, a mystery's fairness). Returns `baseline` (mean of
    each metric and overall `q` across the book: the book's average, against which its parts are weak or strong),
    `lowest_passages`, and `weakest_subtrees`: the lowest-scoring branches (a passage and everything reachable from it, at least
    `min_size` passages, disjoint, mean shrunk toward the book's so a tiny fluke cannot win), each with its mean per metric to show
    what is wrong. A quarter of a second per passage. The scores are relative: use them to choose which branch to rewrite and
    to see whether a rewrite moved its branch, not to rank passages or to compare different books."""
    story, passages, choices, parent, options = _judging(story_id)
    qs = metrics or judge_mod.METRICS
    rows: dict[int, dict[str, float]] = {}
    try:
        for pid in sorted(passages):
            rows[pid] = judge_mod.ask(passages[pid]["body"], passages[parent[pid]]["body"] if pid in parent else "", qs, options.get(pid, ()))
    except ConnectionError as e:
        raise ValueError(str(e)) from e
    if not rows:
        raise ValueError(f"story {story_id} has no passages")
    q = {pid: sum(r.values()) / len(r) for pid, r in rows.items()}
    mean = lambda xs: round(sum(xs) / len(xs), 3)  # noqa: E731
    title = {pid: passages[pid]["title"] for pid in passages}
    return {
        "checked": len(rows), "metrics": list(qs),
        "baseline": {"q": mean(q.values()), **{k: mean([r[k] for r in rows.values()]) for k in qs}},
        "lowest_passages": [{"id": pid, "title": title[pid], "q": round(q[pid], 3), "weakest": min(rows[pid], key=rows[pid].get)}
                            for pid in sorted(q, key=q.get)[:5]],
        "weakest_subtrees": [{"root": f["root"], "title": title[f["root"]], "size": f["size"], "mean": round(f["mean"], 3),
                              "adjusted": round(f["adjusted"], 3),
                              "metrics": {k: mean([rows[p][k] for p in f["passages"]]) for k in qs},
                              "passages": [{"id": p, "title": title[p], "q": round(q[p], 3)} for p in f["passages"]]}
                             for f in engine.weakest_subtrees(q, story["start_passage_id"], choices, min_size, top)],
    }


@tool
def delete_story(story_id: int, confirm_title: str) -> dict[str, Any]:
    """Delete a whole story: passages, choices, lore, everything. `confirm_title` must equal the story's title exactly."""
    with db.transaction() as conn:
        story = _story(conn, story_id)
        if confirm_title != story["title"]:
            raise ValueError(f"confirm_title must be exactly {story['title']!r}")
        conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
        return {"deleted": story_id, "title": story["title"]}


def main() -> None:
    import sys

    db.init_db()
    # stdout is the protocol; say which file we are writing on stderr, since a harness picks our working directory
    print(f"cyoa mcp: db={db.DB_PATH.resolve()} summariser={summarize.URL} label={LABEL!r}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
