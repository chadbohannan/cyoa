"""Graph analysis for the story: the one place its structure is read.

A story is passages joined by choices and nothing else: no variables, conditions or effects
(removed 2026-10-07, concepts/stateless-story.md). Whatever a reader has done is carried by
the path they took, so the validator, the player, the random playtest and prompt construction
all walk the same graph through the functions here.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter, deque
from typing import Any, Iterable

from fastapi import HTTPException

# JSON columns that need encoding/decoding; shared with main.py row helpers
JSON_COLS: dict[str, set[str]] = {
    "passages": {"meta"},
    "choices": set(),
    "lore": set(),
    "proposals": {"payload"},
    "agent_runs": {"params", "scope", "budget", "policy", "progress"},
    "interviews": {"transcript", "conflicts"},
}


def problems(
    start_passage_id: int | None,
    passages: Iterable[dict[str, Any]],
    choices: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Static checks over one story's graph; the shape mirrors ``models.Problems``."""
    passages, choices = list(passages), list(choices)
    ids = [p["id"] for p in passages]
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

    return {
        "no_start": start_passage_id not in out_of,
        "unreachable": [pid for pid in ids if pid not in reachable],
        "dangling": [c["id"] for c in choices if c["to_passage"] is None],
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
    runs: int = 200,
    max_steps: int = 200,
    seed: int | None = None,
) -> dict[str, Any]:
    """Walk the story ``runs`` times taking a random choice each step,
    over the same graph the player walks. The shape mirrors ``models.Playtest``:
    what was never visited, which decided endings were reached and how often
    versus which unfinished stubs a walk simply ran out of story at, which
    choices lead nowhere, and how many walks the step cap cut short."""
    passages, choices = list(passages), list(choices)
    ids = [p["id"] for p in passages]
    is_ending = {p["id"]: bool(p.get("is_ending")) for p in passages}
    out = _outgoing(choices)
    rng = random.Random(seed)
    seen_p: Counter[int] = Counter()
    seen_c: Counter[int] = Counter()
    endings: Counter[int] = Counter()
    unfinished: Counter[int] = Counter()
    dangling: Counter[int] = Counter()
    cut, lengths = 0, []
    if start_passage_id in out or start_passage_id in ids:
        for _ in range(runs):
            pid, steps = start_passage_id, 0
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
                if steps >= max_steps:
                    cut += 1
                    break
                c = rng.choice(options)
                seen_c[c["id"]] += 1
                if c["to_passage"] is None:
                    dangling[c["id"]] += 1
                    break
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
        "dangling": dict(dangling),
    }


def shape(start_passage_id: int | None, passages: Iterable[dict[str, Any]], choices: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """How ragged the tree is, decision by decision, and whether the option order gives the reader a tell.

    An option's *reach* is how many more decisions its longest route plays before an ending (0: it ends at once).
    At each decision reached from the start, the option with the greatest reach is the trunk and the others are
    side branches; a book where every side branch has reach 0 is a spine with one-passage exits, however its
    ending depths spread. `trunk_slot` and `exit_slot` count which position (1-based, in the order the reader sees)
    holds the trunk and the instant exits: a single slot that always wins is a pattern the reader can learn.
    `always_pick` follows one slot from the start (the last option when a decision has fewer), for each slot the
    widest decision has."""
    passages, ordered = list(passages), sorted(choices, key=lambda c: (c.get("sort_order") or 0, c["id"]))
    meta = {p["id"]: p.get("meta") or {} for p in passages}
    title = {p["id"]: p["title"] for p in passages}
    is_ending = {p["id"]: bool(p.get("is_ending")) for p in passages}
    out = {pid: [c for c in cs if c["to_passage"] is not None] for pid, cs in _outgoing(ordered).items()}
    reach: dict[int, int] = {}

    def height(pid: int, stack: frozenset[int] = frozenset()) -> int:
        """Decisions still to play on the longest route from `pid` (a cycle counts as ending where it closes)."""
        if pid in reach:
            return reach[pid]
        nxt = [] if is_ending.get(pid) else [c["to_passage"] for c in out.get(pid, []) if c["to_passage"] not in stack]
        h = 1 + max(height(t, stack | {pid}) for t in nxt) if nxt else 0
        reach[pid] = h
        return h

    decisions, seen, queue = [], {start_passage_id}, deque([start_passage_id] if start_passage_id is not None else [])
    while queue:
        pid = queue.popleft()
        opts = [] if is_ending.get(pid) else out.get(pid, [])
        if len(opts) >= 2:
            decisions.append((pid, [height(c["to_passage"]) for c in opts]))
        for c in opts:
            if c["to_passage"] not in seen:
                seen.add(c["to_passage"])
                queue.append(c["to_passage"])

    side: Counter[int] = Counter()
    trunk_slot: Counter[int] = Counter()
    exit_slot: Counter[int] = Counter()
    instant = 0
    for _, hs in decisions:
        top = max(hs)
        if hs.count(top) == 1:
            trunk_slot[hs.index(top) + 1] += 1
        if top > 0:  # a last call where every option ends has no trunk, so no side branches either
            side.update(h for i, h in enumerate(hs) if i != hs.index(top))
        exits = [i + 1 for i, h in enumerate(hs) if h == 0]
        exit_slot.update(exits)
        instant += bool(exits) and top > 0

    def walk(slot: int) -> dict[str, Any]:
        pid, path = start_passage_id, set()
        while pid is not None and pid not in path and not is_ending.get(pid) and out.get(pid):
            path.add(pid)
            opts = out[pid]
            pid = opts[min(slot, len(opts)) - 1]["to_passage"]
        return {"ending": title.get(pid), "verdict": meta.get(pid, {}).get("verdict"), "decisions": len(path)}

    n = len(decisions)
    warnings = []
    if n >= 4 and instant / n > 0.6:
        warnings.append(f"{instant} of {n} decisions are 'end now or go on': let some wrong turns run two or three decisions before they fail")
    if n >= 4 and side and side[0] / sum(side.values()) > 0.8:
        warnings.append("almost every side branch ends at once: the tree is a spine with exits, not ragged")
    for counter, of, what in ((trunk_slot, sum(trunk_slot.values()), "the longest option"), (exit_slot, n, "an option that ends at once")):
        if n >= 4 and counter and counter.most_common(1)[0][1] / of > 0.75:
            slot, k = counter.most_common(1)[0]
            warnings.append(f"slot {slot} holds {what} at {k} of {of} decisions: vary the order")
    picks = {slot: walk(slot) for slot in range(1, max((len(hs) for _, hs in decisions), default=1) + 1)}
    verdicts = Counter(m["verdict"] for pid, m in meta.items() if is_ending.get(pid) and m.get("verdict"))
    for slot, w in picks.items():
        if w["verdict"] and verdicts[w["verdict"]] * 3 <= sum(verdicts.values()):
            warnings.append(f"always picking slot {slot} reaches '{w['ending']}' ({w['verdict']}, a rare outcome) without reading a word")
    return {
        "decisions": n,
        "instant_exit_decisions": instant,
        "side_branch_reach": dict(sorted(side.items())),
        "trunk_slot": dict(sorted(trunk_slot.items())),
        "exit_slot": dict(sorted(exit_slot.items())),
        "always_pick": picks,
        "warnings": warnings,
    }


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
        insert_row(conn, "choices", story_id, {"from_passage": p["target_passage"], "to_passage": to, "text": pl["text"], "sort_order": order})
    # 'note' materialises nothing; accepting it just files it away

    from . import db
    db.touch_story(conn, story_id)


def weakest_subtrees(scores: dict[int, float], start: int | None, choices: list[dict[str, Any]], min_size: int = 3,
                     top: int = 3, shrink: float = 2.0) -> list[dict[str, Any]]:
    """The lowest-scoring subtrees by mean passage score. A subtree is a passage and everything reachable from it (the start's
    is the whole book and is skipped); fewer than `min_size` scored passages is too small to be a rewrite. The mean is shrunk
    toward the book's mean by `shrink` passages' worth, so a two-passage fluke cannot beat a large, steadily weak branch. Results
    are disjoint, lowest first, and each lists its passages."""
    if not scores:
        return []
    out = _outgoing(choices)
    base = sum(scores.values()) / len(scores)
    found = []
    for root in scores:
        if root == start:
            continue
        seen, todo = {root}, [root]
        while todo:
            for c in out.get(todo.pop(), []):
                if c["to_passage"] is not None and c["to_passage"] not in seen:
                    seen.add(c["to_passage"])
                    todo.append(c["to_passage"])
        members = sorted(p for p in seen if p in scores)
        if len(members) < min_size:
            continue
        total = sum(scores[p] for p in members)
        found.append({"root": root, "size": len(members), "mean": total / len(members),
                      "adjusted": (total + shrink * base) / (len(members) + shrink), "passages": members})
    found.sort(key=lambda f: f["adjusted"])
    picked: list[dict[str, Any]] = []
    used: set[int] = set()
    for f in found:
        if used & set(f["passages"]):
            continue
        picked.append(f)
        used |= set(f["passages"])
        if len(picked) >= top:
            break
    return picked
