"""Verb workflows and proposal acceptance, with the model stubbed out."""

import json
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient
from langgraph.config import get_config

from cyoa import agents, db, llm, prompts, runs
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
    # Mark as reviewed so assist will propose instead of writing directly
    client.patch(f"{story['base']}/passages/{story['start']}", json={"reviewed": True})
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
    client.patch(f"{story['base']}/passages/{story['start']}", json={"reviewed": True})
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
    client.patch(f"{story['base']}/passages/{story['start']}", json={"reviewed": True})
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
    client.patch(f"{story['base']}/passages/{story['start']}", json={"reviewed": True})
    use(monkeypatch, {"body": "You stand at the mouth of a cave."}, {"body": "A cave mouth yawns before you."})
    r = client.post(f"{story['base']}/passages/{story['start']}/assist", json={"verb": "rewrite", "tone": "grim"})
    [p] = r.json()
    assert p["payload"]["body"] == "A cave mouth yawns before you."


def test_suggest_next_branch_continue_end(client, story, monkeypatch):
    base, pit = story["base"], story["pit"]
    # Mark as reviewed so assist will propose instead of writing directly
    client.patch(f"{base}/passages/{pit}", json={"reviewed": True})

    use(monkeypatch, {"decision": "branch", "reasoning": "a real fork here",
                       "choices": [{"text": "Left", "target_title": "The Left Path"}, {"text": "Right", "target_title": "The Right Path"}]})
    branch = client.post(f"{base}/passages/{pit}/assist", json={"verb": "suggest_next"}).json()
    assert [p["payload"]["text"] for p in branch] == ["Left", "Right"] and all(p["kind"] == "new_choice" for p in branch)
    for p in branch:
        client.post(f"{base}/proposals/{p['id']}/reject")

    use(monkeypatch, {"decision": "continue", "reasoning": "no real choice yet",
                       "continuation_title": "Deeper In", "continuation_body": "You press on."})
    [p] = client.post(f"{base}/passages/{pit}/assist", json={"verb": "suggest_next"}).json()
    assert p["kind"] == "new_choice" and p["payload"]["new_target"] == {"title": "Deeper In", "body": "You press on.", "pos_x": 0, "pos_y": 380}
    client.post(f"{base}/proposals/{p['id']}/accept")
    g = client.get(f"{base}/graph").json()
    deeper = next(x for x in g["passages"] if x["title"] == "Deeper In")
    assert deeper["body"] == "You press on." and deeper["provenance"] == "agent"

    use(monkeypatch, {"decision": "end", "reasoning": "a quiet close to the story"})
    [p] = client.post(f"{base}/passages/{pit}/assist", json={"verb": "suggest_next"}).json()
    assert p["kind"] == "edit_passage" and p["payload"] == {"is_ending": True, "reasoning": "a quiet close to the story"}
    pit_before = next(x for x in g["passages"] if x["id"] == pit)
    assert pit_before["is_ending"] is False
    client.post(f"{base}/proposals/{p['id']}/accept")
    pit_after = next(x for x in client.get(f"{base}/graph").json()["passages"] if x["id"] == pit)
    assert pit_after["is_ending"] is True


def test_proposal_cascades_with_its_target(client, story, monkeypatch):
    client.patch(f"{story['base']}/passages/{story['pit']}", json={"reviewed": True})
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
                                                   "budget": {"max_passages": 4, "depth": 2},
                                                   "policy": {"refine": {"rounds": 0}}})
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


def test_expand_frontier_skip_review_runs_every_level(client, story, monkeypatch):
    use(monkeypatch, growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": {"max_passages": 6, "depth": 2},
                                                   "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    # no pause between levels: level 0 grows 2, level 1 grows the remaining 4 under them
    assert run["status"] == "done" and run["progress"]["created"] == 6
    g = client.get(f"{story['base']}/graph").json()
    assert len([p for p in g["passages"] if p["provenance"] == "agent"]) == 6


def test_expand_frontier_retries_placeholders_and_empty_answers(client, story, monkeypatch):
    calls = {"passage": 0}

    def flaky(prompt):
        if "Suggest new choices" in prompt:
            if "rejected" not in prompt:  # first try: only '...' choices
                return {"choices": [{"text": "...", "target_title": "..."}]}
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        calls["passage"] += 1
        if calls["passage"] == 1:
            return {}  # an empty tool call: fails the schema
        if calls["passage"] == 2:
            return {"title": "Left", "body": "You fit...\n..."}
        return {"title": "Left", "body": "You go left into the dark."}

    fake = use(monkeypatch, flaky)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": {"max_passages": 1, "depth": 1},
                                                   "policy": {"refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done" and run["error"] is None and run["progress"]["created"] == 1
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert new["body"] == "You go left into the dark."
    assert not any(c["text"] == "..." for c in g["choices"])
    assert sum("Your previous answer was rejected" in p for p in fake.prompts) == 3


def test_expand_frontier_skips_a_choice_whose_passage_never_comes(client, story, monkeypatch):
    def stubborn(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "L"}, {"text": "Go right", "target_title": "R"}]}
        if "took the choice 'Go left'" in prompt:
            return {"title": "L", "body": "..."}
        return {"title": "R", "body": "You go right."}

    use(monkeypatch, stubborn)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": {"max_passages": 2, "depth": 1},
                                                   "policy": {"refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    # the sibling still grows; the failed choice is not written as a dangling link
    assert run["progress"]["created"] == 1 and "Go left" in run["error"]
    g = client.get(f"{story['base']}/graph").json()
    assert [c["text"] for c in g["choices"] if c["from_passage"] == story["start"]] == ["Enter", "Wait", "Go right"]


def test_expand_frontier_branching_caps_choices_and_last_level_ends(client, story, monkeypatch):
    fake = use(monkeypatch, growth)  # offers two choices everywhere
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": {"depth": 2, "branching": 1},
                                                   "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done" and run["progress"]["created"] == 2
    g = client.get(f"{story['base']}/graph").json()
    agent = {p["title"]: p for p in g["passages"] if p["provenance"] == "agent"}
    # one choice kept per passage; the level-0 passage continues, the last level is an ending
    assert set(agent) == {"Go left from Start", "Go left from Go left from Start"}
    assert not agent["Go left from Start"]["is_ending"] and agent["Go left from Go left from Start"]["is_ending"]
    choices = [p for p in fake.prompts if "Suggest new choices" in p]
    assert "Offer at most 1 choices" in choices[0] and "at most 2 passages left" in choices[0]
    assert "at most 1 passages left" in choices[1]
    endings = [p for p in fake.prompts if "is the last in this storyline" in p]
    assert len(endings) == 1
    assert client.get(f"{story['base']}/problems").json()["endings"] == [agent["Go left from Go left from Start"]["id"]]


def test_expand_frontier_writer_may_end_a_storyline_early(client, story, monkeypatch):
    def closing(prompt):
        out = growth(prompt)
        if "Suggest new choices" not in prompt and "took the choice 'Go left'" in prompt:
            out["ends_story"] = True
        return out

    use(monkeypatch, closing)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": {"depth": 3, "branching": 2},
                                                   "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    g = client.get(f"{story['base']}/graph").json()
    by_title = {p["title"]: p for p in g["passages"]}
    # 'Go left' ends its storyline at once and is not grown; 'Go right' keeps branching
    assert by_title["Go left from Start"]["is_ending"]
    assert not any(c["from_passage"] == by_title["Go left from Start"]["id"] for c in g["choices"])
    assert any(c["from_passage"] == by_title["Go right from Start"]["id"] for c in g["choices"])
    assert run["status"] == "done"


def test_context_carries_the_whole_path(client, story, monkeypatch):
    fake = use(monkeypatch, growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": {"max_passages": 7, "depth": 3},
                                                   "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}})
    wait(client, story["base"], r.json()["id"])
    # a level-2 passage prompt shows the start passage (two steps up) and the choices taken since
    deep = [p for p in fake.prompts if re.search(r"^PASSAGE: Go (left|right) from Go", p, re.M)]
    assert deep
    spine = deep[0].split("STORY SO FAR", 1)[1].split("\nPASSAGE: ", 1)[0]
    assert "[Start]\nYou stand at the mouth of a cave." in spine
    assert "(the reader chose: Go left)" in spine or "(the reader chose: Go right)" in spine
    # the link the spine came through is not repeated under REACHED FROM
    assert "REACHED FROM" not in deep[0]


# --- refinement loop (R1) -----------------------------------------------------------


def audits():
    with db.transaction() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audits ORDER BY round, auditor").fetchall()]


def test_refine_passes_on_first_round(client, story, monkeypatch):
    def passing(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt or "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left into the dark."}

    use(monkeypatch, passing)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 1, "depth": 1},
                                                    "policy": {"refine": {"rounds": 2}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done" and run["error"] is None
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert new["body"] == "You go left into the dark." and new["meta"].get("audit") is None
    rows = audits()
    assert len(rows) == 2 and {r["auditor"] for r in rows} == {"audit_continuity", "audit_canon"}
    assert all(r["round"] == 1 and r["verdict"] == "pass" for r in rows)


def test_refine_revises_until_pass(client, story, monkeypatch):
    def revising(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            body = re.search(r"DRAFT BODY: (.+)", prompt).group(1)
            if body.endswith(" Revised."):
                return {"verdict": "pass", "issues": []}
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": "too terse", "quote": body, "evidence": ""}]}
        if "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            return {"verdict": "pass", "issues": []}
        if "DRAFT below was audited" in prompt:
            assert "too terse" in prompt
            title = re.search(r"DRAFT TITLE: (.+)", prompt).group(1)
            body = re.search(r"DRAFT BODY: (.+)", prompt).group(1)
            return {"title": title, "body": body + " Revised."}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, revising)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 1, "depth": 1},
                                                    "policy": {"refine": {"rounds": 2}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert new["body"] == "You go left. Revised."
    rows = audits()
    assert [r["round"] for r in rows] == [1, 1, 2, 2]


def test_refine_drops_unverifiable_quote(client, story, monkeypatch):
    def bad_quote(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": "invented",
                                                      "quote": "this text is not in the draft", "evidence": ""}]}
        if "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, bad_quote)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 1, "depth": 1},
                                                    "policy": {"refine": {"rounds": 2}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    # the unverifiable issue is dropped, so the round reads as a pass
    assert new["body"] == "You go left." and new["meta"].get("audit") is None
    rows = [r for r in audits() if r["auditor"] == "audit_continuity"]
    assert len(rows) == 1 and rows[0]["dropped"] == 1 and rows[0]["issues"] == "[]"


def test_refine_plateau_then_keep_flagged(client, story, monkeypatch):
    def stuck(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": "flat",
                                                      "quote": "You go left.", "evidence": ""}]}
        if "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            return {"verdict": "pass", "issues": []}
        # the same text every time: the issue never gets fixed
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, stuck)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 1, "depth": 1},
                                                    "policy": {"refine": {"rounds": 3, "restarts": 1}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert new["meta"]["audit"]["verdict"] == "unresolved"
    assert new["meta"]["audit"]["issues"][0]["problem"] == "flat"


def test_refine_on_unresolved_drop(client, story, monkeypatch):
    def stuck(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": "flat",
                                                      "quote": "You go left.", "evidence": ""}]}
        if "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, stuck)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 1, "depth": 1},
                                                    "policy": {"refine": {"rounds": 3, "restarts": 1, "on_unresolved": "drop"}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done" and run["progress"]["created"] == 0 and "dropped" in run["error"]
    g = client.get(f"{story['base']}/graph").json()
    assert [c["text"] for c in g["choices"] if c["from_passage"] == story["start"]] == ["Enter", "Wait"]


def test_audit_canon_skipped_without_lore_or_events(client, monkeypatch):
    s = client.post("/api/stories", json={"title": "Bare", "description": ""}).json()
    base = f"/api/stories/{s['id']}"
    client.patch(f"{base}/passages/{s['start_passage_id']}", json={"body": "You stand alone."})

    def passing(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, passing)
    r = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [s["start_passage_id"]],
                                          "budget": {"max_passages": 1, "depth": 1},
                                          "policy": {"refine": {"rounds": 2}}})
    run = wait(client, base, r.json()["id"])
    assert run["status"] == "done"
    assert {r["auditor"] for r in audits()} == {"audit_continuity"}


def test_audit_votes_majority(client, story, monkeypatch):
    calls = {"n": 0}

    def voting(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            calls["n"] += 1
            # a different single issue on every vote: no fingerprint reaches a majority
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": f"issue {calls['n']}",
                                                      "quote": "You go left.", "evidence": ""}]}
        if "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, voting)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 1, "depth": 1},
                                                    "policy": {"refine": {"rounds": 1, "votes": 3}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert new["meta"].get("audit") is None  # no issue reached a majority, so round 1 read as a pass


def test_audit_call_failure_does_not_sink_the_run(client, story, monkeypatch):
    """A failed auditor call (every vote raises) must not crash the run: _run_auditor's
    'error' verdict is a real AuditReport value, not just a DB-column string. Caught the
    hard way in the first live comparison run (2026-09-28): every story failed with
    `ValidationError: Input should be 'pass' or 'revise'` because 'error' wasn't in the
    schema's Literal, so the fallback itself raised."""
    def exploding(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            return RuntimeError("model exploded")
        if "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, exploding)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 1, "depth": 1},
                                                    "policy": {"refine": {"rounds": 1}}})
    run = wait(client, story["base"], r.json()["id"])
    # the run still completes and writes the passage; the failed call is logged, not fatal
    assert run["status"] == "done" and "model exploded" in run["error"]
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert new["body"] == "You go left." and new["meta"].get("audit") is None
    rows = [r for r in audits() if r["auditor"] == "audit_continuity"]
    assert rows[0]["verdict"] == "error" and rows[0]["issues"] == "[]"


def test_refine_rounds_zero_is_a_true_noop(client, story, monkeypatch):
    fake = use(monkeypatch, growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 1, "depth": 1},
                                                    "policy": {"refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    assert audits() == []
    assert not any("DRAFT TITLE" in p or "Check DRAFT" in p for p in fake.prompts)


# --- concurrent fan-out ---------------------------------------------------------


def test_grow_fans_out_choices_concurrently(client, story, monkeypatch):
    """Two choices' passage drafts must actually overlap when the gate allows it, not
    just both eventually succeed — a barrier only releases once both are in flight."""
    monkeypatch.setattr(agents, "MAX_CONCURRENCY", 2)
    monkeypatch.setattr(agents, "_GATE", threading.Semaphore(2))
    barrier = threading.Barrier(2, timeout=5)

    def concurrent_growth(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}, {"text": "Go right", "target_title": "Right"}]}
        barrier.wait()
        choice = re.search(r"took the choice '(.+?)'", prompt).group(1)
        return {"title": f"{choice} target", "body": f"You {choice.lower()}."}

    use(monkeypatch, concurrent_growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 2, "depth": 1},
                                                    "policy": {"refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    # a broken barrier (serial dispatch) would exhaust retries and skip both choices
    assert run["status"] == "done" and run["error"] is None and run["progress"]["created"] == 2


def test_refine_fans_out_auditors_concurrently(client, story, monkeypatch):
    """audit_continuity and audit_canon must actually overlap within one round."""
    monkeypatch.setattr(agents, "MAX_CONCURRENCY", 2)
    monkeypatch.setattr(agents, "_GATE", threading.Semaphore(2))
    barrier = threading.Barrier(2, timeout=5)

    def concurrent_auditing(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt or "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            barrier.wait()
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, concurrent_auditing)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 1, "depth": 1},
                                                    "policy": {"refine": {"rounds": 1}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    # a broken barrier (serial dispatch) would raise inside invoke(), read back as
    # verdict "error" per auditor instead of the "pass" both should reach together
    assert audits() and all(r["verdict"] == "pass" for r in audits())


def test_refine_recovers_from_exhausted_draft_retries(client, story, monkeypatch):
    """The bug the first live comparison run also would have hit if the AuditReport
    Literal fix hadn't been found first: refine()'s initial draft can exhaust
    _ask_until's retries same as the rounds: 0 path; it must skip the choice and let
    its sibling grow, not crash the whole node."""
    def stubborn(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "L"}, {"text": "Go right", "target_title": "R"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt or "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            return {"verdict": "pass", "issues": []}
        if "took the choice 'Go left'" in prompt:
            return {"title": "L", "body": "..."}  # always a placeholder: exhausts retries
        return {"title": "R", "body": "You go right."}

    use(monkeypatch, stubborn)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": {"max_passages": 2, "depth": 1},
                                                    "policy": {"refine": {"rounds": 1}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done" and "Go left" in run["error"]
    g = client.get(f"{story['base']}/graph").json()
    assert [c["text"] for c in g["choices"] if c["from_passage"] == story["start"]] == ["Enter", "Wait", "Go right"]


def test_fan_empty_and_single_item():
    assert agents._fan([], lambda x: x) == []
    assert agents._fan([5], lambda x: x * 2) == [10]


def test_fan_preserves_order():
    def slow(item):
        i, delay = item
        time.sleep(delay)
        return i

    assert agents._fan([(0, 0.05), (1, 0.0), (2, 0.02)], slow) == [0, 1, 2]


def test_verify_issue_normalization():
    draft = agents._norm("You  go\nleft into the dark.")
    ctx = agents._norm("The cave is dark and cold.")
    assert agents._verify_issue({"quote": "you GO   left", "evidence": ""}, draft, ctx)
    assert agents._verify_issue({"quote": "You go left", "evidence": "the CAVE is dark"}, draft, ctx)
    assert not agents._verify_issue({"quote": "not present", "evidence": ""}, draft, ctx)
    assert not agents._verify_issue({"quote": "You go left", "evidence": "not present"}, draft, ctx)


def test_blocking_threshold():
    assert agents._blocking([{"severity": "minor"}], "minor")
    assert not agents._blocking([{"severity": "minor"}], "major")
    assert agents._blocking([{"severity": "major"}], "major")
    assert not agents._blocking([], "minor")


def test_fingerprint_ignores_case_and_whitespace():
    a = agents._fingerprint({"quote": "You  GO left", "problem": "Too  Terse"})
    b = agents._fingerprint({"quote": "you go   left", "problem": "too terse"})
    assert a == b


def test_expand_frontier_proposes_where_reviewed(client, story, monkeypatch):
    use(monkeypatch, growth)
    client.patch(f"{story['base']}/passages/{story['start']}", json={"reviewed": True})
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]], "budget": {"depth": 1},
                                                   "policy": {"refine": {"rounds": 0}}})
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
    r = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]], "budget": {"max_passages": 6},
                                          "policy": {"refine": {"rounds": 0}}})
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
    run_id = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                "policy": {"refine": {"rounds": 0}}}).json()["id"]
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


# --- prompts ------------------------------------------------------------------------


def test_prompt_override_reaches_verb_in_every_story(client, story, monkeypatch):
    fake = use(monkeypatch, {"body": "x"})
    lst = client.get("/api/agents/prompts").json()
    assert [p["key"] for p in lst] == list(prompts.PROMPTS)
    body = next(p for p in lst if p["key"] == "draft_body")
    assert body["override"] is None and body["used_by"] == [["verb", "generate"]]
    assert next(p for p in lst if p["key"] == "frontier_passage")["placeholders"] == ["choice", "remaining"]

    r = client.put("/api/agents/prompts/draft_body", json={"text": "Write it bleakly."})
    assert r.status_code == 200 and r.json()["override"] == "Write it bleakly."
    other = client.post("/api/stories", json={"title": "Other"}).json()
    for base, pid in ((story["base"], story["start"]), (f"/api/stories/{other['id']}", other["start_passage_id"])):
        assert client.post(f"{base}/passages/{pid}/assist", json={"verb": "draft_body"}).status_code == 201
    assert all(p.endswith("TASK: Write it bleakly.") for p in fake.prompts) and len(fake.prompts) == 2

    assert client.delete("/api/agents/prompts/draft_body").json()["override"] is None
    client.post(f"{story['base']}/passages/{story['start']}/assist", json={"verb": "draft_body"})
    assert fake.prompts[-1].endswith("TASK: " + prompts.PROMPTS["draft_body"].default)
    assert client.put("/api/agents/prompts/nope", json={"text": "x"}).status_code == 404
    assert client.delete("/api/agents/prompts/nope").status_code == 404


def test_prompt_roles_are_independent(client, prompts_file):
    client.put("/api/agents/prompts/draft_target", json={"text": "Go where '{choice}' leads."})
    roles = prompts.resolve()
    assert roles["draft_target"] == "Go where '{choice}' leads."
    assert roles["frontier_passage"] == prompts.PROMPTS["frontier_passage"].default
    # sparse, sorted, newline-terminated, and only the override is stored
    assert prompts_file.read_text() == '{\n  "draft_target": "Go where \'{choice}\' leads."\n}\n'


def test_prompt_file_survives_fresh_db(client, tmp_path):
    client.put("/api/agents/prompts/rewrite", json={"text": "Make it {tone}."})
    db.DB_PATH = tmp_path / "fresh.db"
    db.init_db()
    assert client.get("/api/stories").json() == []
    assert next(p for p in client.get("/api/agents/prompts").json() if p["key"] == "rewrite")["override"] == "Make it {tone}."


@pytest.mark.parametrize("key,text,needle", [
    ("draft_body", "Write about {choise}.", "unknown placeholder {choise}"),
    ("draft_target", "Write the destination.", "missing placeholder {choice}"),
    ("draft_body", "A lone { brace", "literal brace"),
    ("draft_body", "  \n", "empty"),
])
def test_prompt_validation(client, prompts_file, key, text, needle):
    r = client.put(f"/api/agents/prompts/{key}", json={"text": text})
    assert r.status_code == 422 and needle in r.json()["detail"] and key in r.json()["detail"]
    assert not prompts_file.exists()


def test_run_keeps_its_prompt_snapshot(client, story, monkeypatch):
    fake = use(monkeypatch, growth)
    base = story["base"]
    run = wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                               "budget": {"max_passages": 4, "depth": 2},
                                                               "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert run["status"] == "paused" and run["params"]["prompts"] == prompts.resolve()
    # edit mid-run: the paused run resumes with the texts it started with
    terse = "Suggest new choices, but tersely: at most {branching}, {remaining} left."
    assert client.put("/api/agents/prompts/frontier_choices", json={"text": terse}).status_code == 200
    n = len(fake.prompts)
    wait(client, base, client.post(f"{base}/runs/{run['id']}/resume", json={}).json()["id"])
    assert fake.prompts[n:] and not any("tersely" in p for p in fake.prompts[n:])
    # a new run picks the override up
    later = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["pit"]], "budget": {"depth": 1},
                                              "policy": {"refine": {"rounds": 0}}}).json()
    assert later["params"]["prompts"]["frontier_choices"] == terse
    wait(client, base, later["id"])
    assert any(p.endswith("TASK: Suggest new choices, but tersely: at most 3, 1 left.") for p in fake.prompts)


def test_preview_equals_what_the_model_receives(client, story, monkeypatch):
    base = story["base"]
    # preview first: the verb writes straight into the unreviewed passage
    r = client.post(f"{base}/prompts/draft_body/preview", json={"passage_id": story["start"]})
    fake = use(monkeypatch, {"body": "x"})
    client.post(f"{base}/passages/{story['start']}/assist", json={"verb": "draft_body"})
    assert r.json()["prompt"] == fake.prompts[-1]

    # {choice} is the first outgoing choice, or a stand-in; an unsaved draft is previewed, not saved
    r = client.post(f"{base}/prompts/frontier_passage/preview", json={"passage_id": story["start"], "text": "After '{choice}', {remaining} left."})
    assert r.json()["prompt"].endswith("TASK: After 'Enter', 2 left.") and prompts.overrides() == {}
    r = client.post(f"{base}/prompts/draft_target/preview", json={"passage_id": story["pit"]})
    assert "took the choice '(example choice)'" in r.json()["prompt"]
    assert client.post(f"{base}/prompts/draft_body/preview", json={"passage_id": story["start"], "text": "{x}"}).status_code == 422

    r = client.post(f"{base}/prompts/frontier_choices/preview", json={"passage_id": story["pit"]})
    fake = use(monkeypatch, growth)
    wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["pit"]],
                                                         "budget": {"depth": 2},
                                                         "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert r.json()["prompt"] == fake.prompts[0]


def test_node_prompts_match_behaviour(client, story, monkeypatch):
    """NODE_PROMPTS restates which roles each node composes; drive every workflow and compare."""
    seen: set[tuple[str, str, str]] = set()
    workflow = {"name": ""}
    real = agents.compose

    def spy(ctx, roles, key, **kw):
        seen.add((workflow["name"], get_config()["metadata"]["langgraph_node"], key))
        return real(ctx, roles, key, **kw)

    monkeypatch.setattr(agents, "compose", spy)
    base = story["base"]
    client.patch(f"{base}/passages/{story['start']}", json={"reviewed": True})
    workflow["name"] = "verb"
    verbs = {"draft_body": {"body": "x"}, "suggest_choices": {"choices": [{"text": "Hide", "target_title": "Hole"}]},
             "draft_target": {"title": "T", "body": "B"}, "rewrite": {"body": "Changed."},
             "suggest_next": {"decision": "end", "reasoning": "done"}}
    for verb, out in verbs.items():
        use(monkeypatch, out)
        extra = {"choice_id": story["dangling"]} if verb == "draft_target" else {}
        assert client.post(f"{base}/passages/{story['start']}/assist", json={"verb": verb, **extra}).status_code == 201, verb
    client.patch(f"{base}/passages/{story['start']}", json={"reviewed": False})

    workflow["name"] = "canon_check"
    use(monkeypatch, {"issues": [], "fixed_body": None})
    wait(client, base, client.post(f"{base}/runs", json={"workflow": "canon_check", "scope": [story["start"]]}).json()["id"])

    def refining(prompt):
        """growth, plus answers for the three refine-loop roles: audit_continuity flags an
        issue on the first pass (so revise_passage gets exercised too), audit_canon always
        passes, both roles are declared in NODE_PROMPTS and must actually get composed."""
        if "Check DRAFT below against STORY SO FAR" in prompt:
            body = re.search(r"DRAFT BODY: (.+)", prompt).group(1)
            if body.endswith(" Revised."):
                return {"verdict": "pass", "issues": []}
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": "flat prose", "quote": body, "evidence": ""}]}
        if "Compare DRAFT below to WORLD and TIMELINE" in prompt:
            return {"verdict": "pass", "issues": []}
        if "DRAFT below was audited" in prompt:
            title = re.search(r"DRAFT TITLE: (.+)", prompt).group(1)
            body = re.search(r"DRAFT BODY: (.+)", prompt).group(1)
            return {"title": title, "body": body + " Revised."}
        return growth(prompt)

    workflow["name"] = "expand_frontier"
    use(monkeypatch, refining)
    # depth 2 so the run writes both a mid-story passage and an ending; default refine
    # policy (rounds: 2) so every declared role, including audit_continuity/audit_canon/
    # revise_passage, actually gets composed at least once.
    wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["pit"]],
                                                         "budget": {"depth": 2}, "policy": {"skip_review_interrupt": True}}).json()["id"])

    declared = {(w, n, k) for (w, n), keys in agents.NODE_PROMPTS.items() for k in keys}
    assert seen == declared
    assert {k for _, _, k in declared} == set(prompts.PROMPTS)
    nodes = {n["id"]: n["prompts"] for n in agents.workflow_json()["expand_frontier"]["nodes"]}
    assert nodes["grow"] == ["frontier_choices", "frontier_passage", "frontier_ending",
                             "audit_continuity", "audit_canon", "revise_passage"] and nodes["plan"] == []
