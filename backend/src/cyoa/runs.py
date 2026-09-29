"""Autonomous workflow runs: start, stream, pause at review, resume, cancel.

A run is one LangGraph thread. Its row lives in `agent_runs` (cyoa.db) and its
checkpoints in a separate SQLite file (CYOA_AGENT_DB, default ./cyoa-agent.db)
so that deleting cyoa.db still resets the story without entangling LangGraph's
opaque blobs — and so that a paused run survives a server restart and can be
resumed from its checkpoint.

The graph executes in a plain worker thread with the sync `SqliteSaver`: the
nodes block on the model anyway, so a thread keeps FastAPI's event loop free
without making the nodes async. The thread streams `updates` (a node finished,
or the graph interrupted) into an in-memory log per run, and the nodes push
activity and progress into the same log through the `agents.RunHandle` the
launch hands them in config; the SSE route replays that log and tails it. The
log is not persisted: a restart loses the event history, not the run. The
handle also carries the cancel flag, so cancelling is setting an Event that the
next node notices.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from . import agents, db, prompts

AGENT_DB_PATH = Path(os.environ.get("CYOA_AGENT_DB", "cyoa-agent.db"))
TERMINAL = {"done", "failed", "cancelled"}


@dataclass
class Live:
    """In-memory side of a run: its event log and the thread executing it."""

    id: int
    status: str
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class Manager:
    def __init__(self) -> None:
        self._saver: SqliteSaver | None = None
        self._graphs: dict[str, Any] = {}
        self.live: dict[int, Live] = {}
        self._lock = threading.Lock()

    # --- checkpoint store ------------------------------------------------------

    def saver(self) -> SqliteSaver:
        if self._saver is None:
            conn = sqlite3.connect(AGENT_DB_PATH, check_same_thread=False)
            self._saver = SqliteSaver(conn)
            self._saver.setup()
        return self._saver

    def graph(self, workflow: str):
        if workflow not in self._graphs:
            build, _ = agents.WORKFLOWS[workflow]
            self._graphs[workflow] = build().compile(checkpointer=self.saver())
        return self._graphs[workflow]

    def reset(self) -> None:
        """Forget everything (tests, and the lifespan's shutdown)."""
        if self._saver is not None:
            self._saver.conn.close()
        self._saver, self._graphs, self.live = None, {}, {}

    def recover(self) -> None:
        """On startup: a run that was executing when the server stopped cannot be resumed
        mid-node, so it fails; a paused one keeps its checkpoint and can be resumed."""
        with db.transaction() as conn:
            conn.execute(
                "UPDATE agent_runs SET status = 'failed', error = 'server restarted while running', updated_at = datetime('now')"
                " WHERE status = 'running'"
            )

    # --- lifecycle -------------------------------------------------------------

    def start(self, story_id: int, workflow: str, scope: list[int], params: dict[str, Any], budget: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
        build, make_input = agents.WORKFLOWS[workflow]
        # The run asks with the roles as they are now, for its whole life: resuming
        # after a prompt edit keeps the snapshot, and the row records what was used.
        params = {**params, "prompts": prompts.resolve()}
        inp = make_input(story_id, scope, params, budget)
        thread_id = uuid.uuid4().hex
        policy = policy or {}
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO agent_runs (story_id, workflow, thread_id, params, scope, budget, policy) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (story_id, workflow, thread_id, json.dumps(params), json.dumps(scope), json.dumps(inp.get("budget", budget)), json.dumps(policy)),
            )
            run_id = cur.lastrowid
        inp["run_id"] = run_id
        inp["policy"] = policy
        self._launch(run_id, workflow, thread_id, inp)
        return self.get(run_id)

    def resume(self, run_id: int, answer: dict[str, Any]) -> dict[str, Any]:
        row = self.get(run_id)
        if row["status"] != "paused":
            raise ValueError(f"run is {row['status']}, not paused")
        self._launch(run_id, row["workflow"], row["thread_id"], Command(resume=answer))
        return self.get(run_id)

    def cancel(self, run_id: int) -> dict[str, Any]:
        row = self.get(run_id)
        if row["status"] in TERMINAL:
            return row
        if row["status"] == "running":
            self.live[run_id].cancel.set()  # the thread notices at its next node and finishes as cancelled
        else:
            self._set_status(run_id, "cancelled")
        return self.get(run_id)

    def _launch(self, run_id: int, workflow: str, thread_id: str, inp: Any) -> None:
        live = self.live.get(run_id)
        if live is None:
            live = self.live[run_id] = Live(id=run_id, status="running")
        live.status = "running"
        self._set_status(run_id, "running")
        graph = self.graph(workflow)
        handle = agents.RunHandle(emit=lambda e: self._push(live, e), cancel=live.cancel, progress=self.get(run_id)["progress"])
        cfg = {"configurable": {"thread_id": thread_id, "__run": handle}}
        live.thread = threading.Thread(target=self._execute, args=(live, graph, inp, cfg), daemon=True, name=f"run-{run_id}")
        live.thread.start()

    def _execute(self, live: Live, graph: Any, inp: Any, cfg: dict[str, Any]) -> None:
        status, error = "done", None
        try:
            for chunk in graph.stream(inp, cfg, stream_mode="updates"):
                if "__interrupt__" in chunk:
                    self._push(live, {"type": "interrupt", "value": chunk["__interrupt__"][0].value})
                    status = "paused"
                else:
                    for node in chunk:
                        self._push(live, {"type": "node", "node": node})
        except agents.RunCancelled:
            status = "cancelled"
        except Exception as e:  # noqa: BLE001 — anything the model or the graph raised ends the run
            status, error = "failed", f"{type(e).__name__}: {e}"
        else:
            # per-passage problems do not fail a run, but they are the only explanation
            # for a run that wrote less than asked, so they land in `error` too
            errors = (graph.get_state(cfg).values or {}).get("errors") or []
            if errors:
                error = "; ".join(errors)[:2000]
        finally:
            self._set_status(live.id, status, error)
            self._push(live, {"type": "status", "status": status, "error": error})
            live.status = status

    # --- events ------------------------------------------------------------------

    def _push(self, live: Live, event: dict[str, Any]) -> None:
        with live.lock:
            event = {**event, "seq": len(live.events) + 1, "at": time.time()}
            live.events.append(event)
        if event["type"] == "progress":
            self._set_progress(live.id, {k: v for k, v in event.items() if k not in ("type", "seq", "at")})

    def events(self, run_id: int, after: int = 0) -> list[dict[str, Any]]:
        live = self.live.get(run_id)
        if live is None:
            return []
        with live.lock:
            return live.events[after:]

    def is_running(self, run_id: int) -> bool:
        live = self.live.get(run_id)
        return live is not None and live.status == "running"

    # --- rows ------------------------------------------------------------------------

    def get(self, run_id: int) -> dict[str, Any]:
        with db.transaction() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return self._decode(conn, row)

    def list(self, story_id: int) -> list[dict[str, Any]]:
        with db.transaction() as conn:
            return [self._decode(conn, r) for r in conn.execute("SELECT * FROM agent_runs WHERE story_id = ? ORDER BY id DESC", (story_id,))]

    @staticmethod
    def _decode(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for col in ("params", "scope", "budget", "policy", "progress"):
            d[col] = json.loads(d[col])
        d["pending_proposals"] = conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE run_id = ? AND status = 'pending'", (d["id"],)
        ).fetchone()[0]
        return d

    def _set_status(self, run_id: int, status: str, error: str | None = None) -> None:
        with db.transaction() as conn:
            conn.execute("UPDATE agent_runs SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?", (status, error, run_id))

    def _set_progress(self, run_id: int, progress: dict[str, Any]) -> None:
        with db.transaction() as conn:
            conn.execute("UPDATE agent_runs SET progress = ?, updated_at = datetime('now') WHERE id = ?", (json.dumps(progress), run_id))

    # --- introspection (Agents view) ---------------------------------------------------

    def state(self, run_id: int) -> dict[str, Any]:
        """The checkpointed state: current values, what runs next, and the step history."""
        row = self.get(run_id)
        cfg = {"configurable": {"thread_id": row["thread_id"]}}
        graph = self.graph(row["workflow"])
        snap = graph.get_state(cfg)
        history = [
            {"step": h.metadata.get("step") if h.metadata else None, "next": list(h.next), "created_at": h.created_at}
            for h in graph.get_state_history(cfg)
        ]
        return {"values": _compact(snap.values), "next": list(snap.next), "history": history}


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    """State values minus the bulky context dict that the verbs carry."""
    return {k: v for k, v in values.items() if k not in ("context", "prompt")}


manager = Manager()
