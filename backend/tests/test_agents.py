"""Verb workflows and proposal acceptance, with the model stubbed out."""

import json
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from cyoa import agents, db, llm, runs
from cyoa.main import app


class FakeModel:
    """Stands in for ChatOllama: returns queued outputs (dicts, exceptions, or a callable of the prompt)."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def with_structured_output(self, schema, method=None):
        self.schema = schema
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if callable(out):
            out = out(prompt)
        if isinstance(out, Exception):
            raise out
        return self.schema(**out)


@pytest.fixture
def client(tmp_path):
    db.DB_PATH = tmp_path / "t.db"
    runs.AGENT_DB_PATH = tmp_path / "agent.db"
    with TestClient(app) as c:
        yield c


@pytest.fixture
def story(client):
    s = client.post("/api/stories", json={"title": "The Lamp", "description": "A cave."}).json()
    base = f"/api/stories/{s['id']}"
    client.patch(f"{base}/passages/{s['start_passage_id']}", json={"body": "You stand at the mouth of a cave."})
    client.post(f"{base}/variables", json={"name": "gold", "kind": "stat", "default": 5})
    client.post(f"{base}/lore", json={"kind": "character", "name": "The Peddler", "body": "Sells lamps."})
    client.post(f"{base}/events", json={"t": 1, "name": "Storm", "body": "A storm rolls in."})
    pit = client.post(f"{base}/passages", json={"title": "The Pit", "pos_x": 0, "pos_y": 200}).json()
    client.post(f"{base}/choices", json={"from_passage": s["start_passage_id"], "to_passage": pit["id"], "text": "Enter"})
    dangling = client.post(f"{base}/choices", json={"from_passage": s["start_passage_id"], "text": "Wait", "sort_order": 1}).json()
    return {"base": base, "id": s["id"], "start": s["start_passage_id"], "pit": pit["id"], "dangling": dangling["id"]}


def use(monkeypatch, *outputs):
    fake = FakeModel(outputs)
    monkeypatch.setattr(llm, "chat_model", lambda model=None, **kw: fake)
    return fake


def test_draft_body_context_and_accept(client, story, monkeypatch):
    fake = use(monkeypatch, {"body": "Wind howls from the dark."})
    r = client.post(f"{story['base']}/passages/{story['start']}/assist", json={"verb": "draft_body"})
    assert r.status_code == 201, r.text
    [p] = r.json()
    assert p["kind"] == "edit_passage" and p["target_passage"] == story["start"] and p["status"] == "pending"
    # the prompt carried the neighbourhood
    prompt = fake.prompts[0]
    for needle in ("STORY: The Lamp", "gold (stat", "[character] The Peddler", "t=1 Storm", "'Enter' -> The Pit", "'Wait' -> (no target yet)"):
        assert needle in prompt

    g = client.get(f"{story['base']}/graph").json()
    assert [x["id"] for x in g["proposals"]] == [p["id"]]

    assert client.post(f"{story['base']}/proposals/{p['id']}/accept").json()["status"] == "accepted"
    g = client.get(f"{story['base']}/graph").json()
    assert g["passages"][0]["body"] == "Wind howls from the dark." and g["proposals"] == []
    assert client.post(f"{story['base']}/proposals/{p['id']}/accept").status_code == 409


def test_retry_then_give_up(client, story, monkeypatch):
    fake = use(monkeypatch, {"body": "   "}, RuntimeError("boom"), {"body": ""})
    r = client.post(f"{story['base']}/passages/{story['start']}/assist", json={"verb": "draft_body"})
    assert r.status_code == 502 and "3 attempt" in r.json()["detail"]
    assert "previous answer was rejected: body was empty" in fake.prompts[1]
    assert client.get(f"{story['base']}/graph").json()["proposals"] == []


def test_suggest_choices_resolves_titles(client, story, monkeypatch):
    use(monkeypatch, {"choices": [
        {"text": "Enter", "target_title": "The Pit"},          # duplicate of an existing choice: dropped
        {"text": "Run back", "target_title": "the pit"},        # existing title, case-insensitive
        {"text": "Climb", "target_title": "The Ledge"},         # new stub target
    ]})
    r = client.post(f"{story['base']}/passages/{story['start']}/assist", json={"verb": "suggest_choices"})
    props = r.json()
    assert [p["payload"]["text"] for p in props] == ["Run back", "Climb"]
    assert props[0]["payload"]["to_passage"] == story["pit"] and props[0]["payload"]["new_target"] is None
    assert props[1]["payload"]["to_passage"] is None and props[1]["payload"]["new_target"]["title"] == "The Ledge"

    client.post(f"{story['base']}/proposals/{props[1]['id']}/accept")
    client.post(f"{story['base']}/proposals/{props[0]['id']}/reject")
    g = client.get(f"{story['base']}/graph").json()
    ledge = next(p for p in g["passages"] if p["title"] == "The Ledge")
    assert ledge["provenance"] == "agent" and ledge["reviewed"] is False
    climb = next(c for c in g["choices"] if c["text"] == "Climb")
    assert climb["to_passage"] == ledge["id"] and climb["sort_order"] == 2
    assert g["proposals"] == []


def test_draft_target_points_dangling_choice(client, story, monkeypatch):
    use(monkeypatch, {"title": "The Long Wait", "body": "Nothing comes."})
    base = story["base"]
    assert client.post(f"{base}/passages/{story['start']}/assist", json={"verb": "draft_target"}).status_code == 422
    r = client.post(f"{base}/passages/{story['start']}/assist", json={"verb": "draft_target", "choice_id": story["dangling"]})
    [p] = r.json()
    assert p["kind"] == "new_passage" and p["target_choice"] == story["dangling"]
    client.post(f"{base}/proposals/{p['id']}/accept")
    g = client.get(f"{base}/graph").json()
    new = next(x for x in g["passages"] if x["title"] == "The Long Wait")
    assert next(c for c in g["choices"] if c["id"] == story["dangling"])["to_passage"] == new["id"]
    # a choice that already has a target is refused up front
    r = client.post(f"{base}/passages/{story['start']}/assist", json={"verb": "draft_target", "choice_id": story["dangling"]})
    assert r.status_code == 409


def test_rewrite_rejects_identical(client, story, monkeypatch):
    use(monkeypatch, {"body": "You stand at the mouth of a cave."}, {"body": "A cave mouth yawns before you."})
    r = client.post(f"{story['base']}/passages/{story['start']}/assist", json={"verb": "rewrite", "tone": "grim"})
    [p] = r.json()
    assert p["payload"]["body"] == "A cave mouth yawns before you."


def test_proposal_cascades_with_its_target(client, story, monkeypatch):
    use(monkeypatch, {"body": "x"})
    [p] = client.post(f"{story['base']}/passages/{story['pit']}/assist", json={"verb": "draft_body"}).json()
    client.delete(f"{story['base']}/passages/{story['pit']}")
    assert client.get(f"{story['base']}/graph").json()["proposals"] == []
    assert client.post(f"{story['base']}/proposals/{p['id']}/accept").status_code == 404


def test_workflow_topology():
    g = agents.workflow_json()["verb"]
    names = {n["id"] for n in g["nodes"]}
    assert {"gather_context", "generate", "parse_validate", "propose", "fail"} <= names


# --- runs -------------------------------------------------------------------------


def wait(client, base, run_id, timeout=10):
    """Poll until the run's worker thread has stopped (paused or terminal)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"{base}/runs/{run_id}").json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run still running")


def passage_title(prompt):
    return re.search(r"^PASSAGE: (.+?)(?: \(|$)", prompt, re.M).group(1)


def growth(prompt):
    """Fake model for expand_frontier: two choices per passage, targets named after the parent."""
    if "Suggest new choices" in prompt:
        t = passage_title(prompt)
        return {"choices": [{"text": "Go left", "target_title": f"L {t}"}, {"text": "Go right", "target_title": f"R {t}"}]}
    choice = re.search(r"took the choice '(.+?)'", prompt).group(1)
    return {"title": f"{choice} from {passage_title(prompt)}", "body": f"You {choice.lower()}."}


def test_expand_frontier_run_pauses_and_resumes(client, story, monkeypatch):
    fake = use(monkeypatch, growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": {"max_passages": 4, "depth": 2}})
    assert r.status_code == 201, r.text
    run = wait(client, story["base"], r.json()["id"])
    # level 0 grew two agent passages under the (unreviewed) start, then paused for review
    assert run["status"] == "paused"
    assert run["progress"] == {"level": 0, "created": 2, "frontier": 1}
    g = client.get(f"{story['base']}/graph").json()
    agent = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert sorted(p["title"] for p in agent) == ["Go left from Start", "Go right from Start"]
    assert all(p["body"] for p in agent) and all(not p["reviewed"] for p in agent)
    assert [c["text"] for c in g["choices"] if c["from_passage"] == story["start"]] == ["Enter", "Wait", "Go left", "Go right"]
    # the event log has the activity the map highlights and the interrupt
    events = client.get(f"{story['base']}/runs/{run['id']}/events").text.split("\n\n")
    kinds = [json.loads(e[6:])["type"] for e in events if e.startswith("data: ")]
    assert kinds[:4] == ["progress", "node", "activity", "activity"] and kinds[-4:] == ["progress", "node", "interrupt", "status"]
    assert [e["phase"] for e in (json.loads(x[6:]) for x in events if x.startswith("data: ")) if e["type"] == "activity"][:4] == ["choices", "done", "passage", "done"]
    # resume with a steer: level 1 has two frontier passages and two passages of budget left
    r = client.post(f"{story['base']}/runs/{run['id']}/resume", json={"steer": "make it darker"})
    assert r.status_code == 200 and r.json()["status"] == "running"
    run = wait(client, story["base"], run["id"])
    assert run["status"] == "done" and run["progress"]["created"] == 4
    assert any("AUTHOR'S DIRECTION for everything you write: make it darker" in p for p in fake.prompts)
    g = client.get(f"{story['base']}/graph").json()
    assert len([p for p in g["passages"] if p["provenance"] == "agent"]) == 4
    # LangGraph's checkpoint is readable after the fact
    st = client.get(f"{story['base']}/runs/{run['id']}/state").json()
    assert st["values"]["level"] == 1 and st["values"]["steer"] == "make it darker" and st["next"] == []
    assert len(st["history"]) > 5
    assert client.post(f"{story['base']}/runs/{run['id']}/resume", json={}).status_code == 409


def test_expand_frontier_proposes_where_reviewed(client, story, monkeypatch):
    use(monkeypatch, growth)
    client.patch(f"{story['base']}/passages/{story['start']}", json={"reviewed": True})
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]], "budget": {"depth": 1}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done" and run["pending_proposals"] == 2
    g = client.get(f"{story['base']}/graph").json()
    assert all(p["provenance"] == "human" for p in g["passages"])
    q = g["proposals"][0]
    assert q["kind"] == "new_choice" and q["run_id"] == run["id"] and q["payload"]["new_target"]["body"]
    client.post(f"{story['base']}/proposals/{q['id']}/accept")
    g = client.get(f"{story['base']}/graph").json()
    new = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert len(new) == 1 and new[0]["body"] == "You go left."
    assert client.get(f"{story['base']}/runs").json()[0]["pending_proposals"] == 1


def test_run_validation_and_cancel(client, story, monkeypatch):
    use(monkeypatch, growth)
    base = story["base"]
    assert client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"], story["pit"]]}).status_code == 422
    assert client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [999]}).status_code == 422
    assert client.get(f"{base}/runs/999").status_code == 404
    r = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]], "budget": {"max_passages": 6}})
    run = wait(client, base, r.json()["id"])
    assert run["status"] == "paused"
    assert client.post(f"{base}/runs/{run['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"{base}/runs/{run['id']}/resume", json={}).status_code == 409


def test_cancel_while_running(client, story, monkeypatch):
    """Cancel lands while the model is mid-call: the run ends `cancelled` at the next node, having written nothing more."""
    gate = threading.Event()
    def slow(prompt):
        gate.wait(5)
        return growth(prompt)
    use(monkeypatch, slow)
    base = story["base"]
    run_id = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]]}).json()["id"]
    time.sleep(0.2)  # let the worker reach the model call
    assert client.post(f"{base}/runs/{run_id}/cancel").json()["status"] == "running"
    gate.set()
    run = wait(client, base, run_id)
    assert run["status"] == "cancelled" and run["error"] is None
    assert all(p["provenance"] == "human" for p in client.get(f"{base}/graph").json()["passages"])
    events = [json.loads(e[6:]) for e in client.get(f"{base}/runs/{run_id}/events").text.split("\n\n") if e.startswith("data: ")]
    assert events[-1] == {**events[-1], "type": "status", "status": "cancelled"}


def test_canon_check_run_files_notes_and_fixes(client, story, monkeypatch):
    base = story["base"]
    # Two passages with bodies: Start (consistent) and Pit (contradiction + fix). Empty ones are skipped but still count.
    client.patch(f"{base}/passages/{story['pit']}", json={"body": "The Peddler waves from the sunny meadow."})
    # checks run concurrently under Send, so answer by prompt rather than by order
    fake = use(monkeypatch, lambda prompt: (
        {"issues": [{"claim": "a sunny meadow", "conflicts_with": "The Pit is underground", "severity": "major"}],
         "fixed_body": "The Peddler waves from the dark."}
        if "PASSAGE: The Pit" in prompt else {"issues": [], "fixed_body": None}))
    r = client.post(f"{base}/runs", json={"workflow": "canon_check", "scope": []})
    assert r.status_code == 201, r.text
    run = wait(client, base, r.json()["id"])
    assert run["status"] == "done" and run["error"] is None and run["progress"] == {"checked": 2}
    props = client.get(f"{base}/graph").json()["proposals"]
    kinds = sorted((p["kind"], p["target_passage"]) for p in props)
    assert kinds == [("edit_passage", story["pit"]), ("note", story["pit"])]
    note = next(p for p in props if p["kind"] == "note")
    fix = next(p for p in props if p["kind"] == "edit_passage")
    assert note["verb"] == "canon_check" and note["run_id"] == run["id"] and note["payload"]["issues"][0]["severity"] == "major"
    assert "[major] a sunny meadow" in note["payload"]["text"]
    assert len({p["batch"] for p in props}) == 1 and run["pending_proposals"] == 2
    assert len(fake.prompts) == 2 and all("Compare PASSAGE to WORLD" in p for p in fake.prompts)
    # progress ticks once per passage, as whole progress events
    events = [json.loads(e[6:]) for e in client.get(f"{base}/runs/{run['id']}/events").text.split("\n\n") if e.startswith("data: ")]
    assert [e["checked"] for e in events if e["type"] == "progress"] == [1, 2]

    # accepting a note only files it; accepting the fix rewrites the body
    client.post(f"{base}/proposals/{note['id']}/accept")
    client.post(f"{base}/proposals/{fix['id']}/accept")
    g = client.get(f"{base}/graph").json()
    assert next(p for p in g["passages"] if p["id"] == story["pit"])["body"] == "The Peddler waves from the dark."


def test_canon_check_run_scope_and_errors(client, story, monkeypatch):
    base = story["base"]
    use(monkeypatch, RuntimeError("model exploded"))
    run = wait(client, base, client.post(f"{base}/runs", json={"workflow": "canon_check", "scope": [story["start"]]}).json()["id"])
    # a per-passage model failure does not fail the run, but it is the run's explanation
    assert run["status"] == "done" and run["pending_proposals"] == 0 and "model exploded" in run["error"]

    other = client.post("/api/stories", json={"title": "Other"}).json()
    assert client.post(f"{base}/runs", json={"workflow": "canon_check", "scope": [other["start_passage_id"]]}).status_code == 422
    # a story with no lore or events has nothing to check against
    assert client.post(f"/api/stories/{other['id']}/runs", json={"workflow": "canon_check", "scope": []}).status_code == 422


def test_workflow_topologies():
    wf = agents.workflow_json()
    assert set(wf) == {"verb", "canon_check", "expand_frontier"}
    edges = {(e["source"], e["target"]) for e in wf["expand_frontier"]["edges"]}
    assert {("plan", "grow"), ("grow", "gather"), ("gather", "review"), ("review", "plan"), ("finish", "__end__")} <= edges
