"""Verb workflows and proposal acceptance, with the model stubbed out."""

import json
import re
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.config import get_config

from cyoa import engine, agents, db, llm, prompts, runs
from cyoa.main import app


class Truncated:
    """A queued FakeModel output: the reply hit the token ceiling."""


class FakeModel:
    """Stands in for ChatOllama: returns queued outputs (dicts, exceptions, or a callable of the prompt)."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        # Interview Me's reviewer is a separate call with its own queue, passing by default
        # so the tests of the interview itself need not script it; tests of the gate set `reviews`.
        self.reviews = [{"verdict": "pass", "issues": []}]
        self.review_prompts = []

    def with_structured_output(self, schema, method=None, include_raw=False):
        return Structured(self, schema, include_raw)

    def invoke(self, prompt):
        """A plain (prose) call: the queued dict's body, closed with 'The End' when it ends the story."""
        out = self._next(prompt)
        if isinstance(out, Truncated):
            return SimpleNamespace(content="You walk toward the", response_metadata={"finish_reason": "length"})
        text = out["body"] + ("\n\nThe End" if out.get("ends_story") else "") if isinstance(out, dict) else out
        return SimpleNamespace(content=text, response_metadata={"finish_reason": "stop"})

    def _next(self, prompt):
        self.prompts.append(prompt)
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if callable(out):
            out = out(prompt)
        if isinstance(out, Exception):
            raise out
        return out


class Structured:
    """FakeModel.with_structured_output: the tool-call path, for the schemas still asked that way."""

    def __init__(self, fake, schema, include_raw):
        self.fake, self.schema, self.include_raw = fake, schema, include_raw

    def invoke(self, prompt):
        out = self._answer(prompt)
        if not self.include_raw:
            return out
        if isinstance(out, Truncated):
            return {"raw": SimpleNamespace(response_metadata={"finish_reason": "length"}), "parsed": None, "parsing_error": ValueError("cut")}
        return {"raw": SimpleNamespace(response_metadata={"finish_reason": "stop"}), "parsed": out, "parsing_error": None}

    def _answer(self, prompt):
        if self.schema is agents.InterviewReview:
            self.fake.review_prompts.append(prompt)
            out = self.fake.reviews.pop(0) if len(self.fake.reviews) > 1 else self.fake.reviews[0]
            if isinstance(out, Exception):
                raise out
            return self.schema(**out)
        out = self.fake._next(prompt)
        if isinstance(out, Truncated):
            return out
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
    client.post(f"{base}/lore", json={"kind": "character", "name": "The Peddler", "body": "Sells lamps."})
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
    for needle in ("STORY: The Lamp", "[character] The Peddler", "'Enter' -> The Pit", "'Wait' -> (no target yet)"):
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
    m = re.search(r"^PASSAGE: (.+?)(?: \(|$)", prompt, re.M)
    return m.group(1) if m else "the end"  # an ending's frame has no PASSAGE block


def growth(prompt):
    """Fake model for expand_frontier: two choices per passage, targets named after the parent."""
    if "Suggest new choices" in prompt:
        t = passage_title(prompt)
        return {"choices": [{"text": "Go left", "target_title": f"Go left from {t}"}, {"text": "Go right", "target_title": f"Go right from {t}"}]}
    choice = re.search(r"(?:took the choice|reader did was:) '(.+?)'", prompt).group(1)
    return {"title": f"{choice} from {passage_title(prompt)}", "body": f"You {choice.lower()}."}


def enders(model):
    """A fake whose passage drafts end their storyline (the act allows it): a run then writes one level and
    is done, which keeps these tests on one grow instead of a whole depth-first descent."""
    def wrapped(prompt):
        out = model(prompt)
        if isinstance(out, dict) and "title" in out and "body" in out:
            out = {**out, "ends_story": True}
        return out
    return wrapped


def plan(*branching, ending=None, **act):
    """A pacing plan with one single-level act per branching value, then the ending."""
    return {"acts": [{"name": f"Act {i + 1}", "levels": 1, "branching": b, **act} for i, b in enumerate(branching)],
            "ending": ending or {}}


def test_run_from_a_subtree_root_starts_at_its_depth(client, story, monkeypatch):
    """The Pit is one choice from the start, so a two-level plan leaves it only the last level: one that writes endings and ends the run."""
    use(monkeypatch, growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["pit"]],
                                                   "budget": plan(2), "policy": {"refine": {"rounds": 0}}})
    assert r.status_code == 201, r.text
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done" and run["progress"]["level"] == 1 and run["progress"]["created"] == 2
    g = client.get(f"{story['base']}/graph").json()
    new = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert len(new) == 2 and all(p["is_ending"] for p in new)


def test_passage_prompt_carries_the_handoff(client, story, monkeypatch):
    """Each new passage is told how its parent ended, what its choice should lead to, and what the sibling choices lead to."""
    def model(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left", "intent": "Find the lamp."},
                                {"text": "Go right", "target_title": "Right", "intent": "Meet the peddler."}]}
        return growth(prompt)
    fake = use(monkeypatch, model)
    run = wait(client, story["base"], client.post(f"{story['base']}/runs", json={
        "workflow": "expand_frontier", "scope": [story["pit"]], "budget": plan(3, 1), "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert run["status"] == "paused"
    left = next(p for p in fake.prompts if "took the choice 'Go left'" in p)
    assert "Find the lamp." in left and "'Go right' (Meet the peddler.)" in left and "'Go left' (" not in left.split("TASK:")[1]
    pit_body = " ".join(next(p for p in client.get(f"{story['base']}/graph").json()["passages"] if p["id"] == story["pit"])["body"].split())
    assert pit_body[-40:] in left.split("TASK:")[1] or not pit_body


def test_agent_passage_titles_are_never_the_story_title_or_taken(client, story, monkeypatch):
    """A writer that titles every passage with the story's title (the frame opens with it) gets the
    title the choices step offered; a duplicate of that is numbered, so export names stay unique."""
    def model(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Hall"}, {"text": "Go right", "target_title": "Hall"}]}
        return {"title": "The Lamp", "body": "You walk on."}
    use(monkeypatch, enders(model))
    run = wait(client, story["base"], client.post(f"{story['base']}/runs", json={
        "workflow": "expand_frontier", "scope": [story["start"]], "budget": plan(2, ends_story=True), "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert run["status"] == "done"
    g = client.get(f"{story['base']}/graph").json()
    agent = sorted(p["title"] for p in g["passages"] if p["provenance"] == "agent")
    assert agent == ["Hall", "Hall (2)"]
    titles = [p["title"].casefold() for p in g["passages"]]
    assert len(titles) == len(set(titles))


def test_depth_from_start():
    ch = [{"from_passage": 1, "to_passage": 2}, {"from_passage": 2, "to_passage": 3}, {"from_passage": 1, "to_passage": 3}]
    assert [engine.depth_from_start(r, 1, ch) for r in (1, 2, 3)] == [0, 1, 1]  # shortest path wins
    assert engine.depth_from_start(9, 1, ch) == 0 and engine.depth_from_start(2, None, ch) == 0


def test_expand_frontier_run_pauses_and_resumes(client, story, monkeypatch):
    fake = use(monkeypatch, growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": plan(2),
                                                   "policy": {"refine": {"rounds": 0}}})
    assert r.status_code == 201, r.text
    run = wait(client, story["base"], r.json()["id"])
    # depth-first: the start grew two passages, the first of them grew its two endings, and that
    # closed a storyline, so the run paused for review with the second passage still to grow
    assert run["status"] == "paused"
    assert run["progress"] == {"level": 1, "created": 4, "frontier": 1, "endings": 2, "open": 1}
    g = client.get(f"{story['base']}/graph").json()
    agent = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert [p["title"] for p in agent] == ["Go left from Start", "Go right from Start",
                                           "Go left from Go left from Start", "Go right from Go left from Start"]
    assert [p["is_ending"] for p in agent] == [False, False, True, True]
    assert all(p["body"] for p in agent) and all(not p["reviewed"] for p in agent)
    assert [c["text"] for c in g["choices"] if c["from_passage"] == story["start"]] == ["Enter", "Wait", "Go left", "Go right"]
    # the event log has the activity the map highlights and the interrupt
    events = client.get(f"{story['base']}/runs/{run['id']}/events").text.split("\n\n")
    kinds = [json.loads(e[6:])["type"] for e in events if e.startswith("data: ")]
    assert kinds[:4] == ["progress", "node", "activity", "activity"] and kinds[-4:] == ["progress", "node", "interrupt", "status"]
    assert [e["phase"] for e in (json.loads(x[6:]) for x in events if x.startswith("data: ")) if e["type"] == "activity"][:4] == ["choices", "done", "passage", "done"]
    # resume with a steer: the second passage is grown, and the stack is empty
    r = client.post(f"{story['base']}/runs/{run['id']}/resume", json={"steer": "make it darker"})
    assert r.status_code == 200 and r.json()["status"] == "running"
    run = wait(client, story["base"], run["id"])
    assert run["status"] == "done" and run["progress"]["created"] == 6
    assert any("AUTHOR'S DIRECTION for everything you write: make it darker" in p for p in fake.prompts)
    g = client.get(f"{story['base']}/graph").json()
    assert len([p for p in g["passages"] if p["provenance"] == "agent"]) == 6
    # LangGraph's checkpoint is readable after the fact
    st = client.get(f"{story['base']}/runs/{run['id']}/state").json()
    assert st["values"]["level"] == 1 and st["values"]["steer"] == "make it darker" and st["next"] == []
    assert len(st["history"]) > 5
    assert client.post(f"{story['base']}/runs/{run['id']}/resume", json={}).status_code == 409


def test_expand_frontier_is_depth_first(client, story, monkeypatch):
    """A storyline is written to its ending before the next branch starts: passages appear in
    the order a reader would meet them, not level by level."""
    use(monkeypatch, growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": plan(2, 2),
                                                   "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    g = client.get(f"{story['base']}/graph").json()
    by_id = {p["id"]: p["title"] for p in g["passages"]}
    parent = {c["to_passage"]: c["from_passage"] for c in g["choices"]}
    agent = [p for p in sorted(g["passages"], key=lambda p: p["id"]) if p["provenance"] == "agent"]
    depth = []
    for p in agent:
        d, cur = 0, p["id"]
        while cur in parent and cur != story["start"]:
            cur, d = parent[cur], d + 1
        depth.append(d)
    # siblings are written together, then the first one is followed all the way down: 1 1 | 2 2 | 3 3 3 3 | ...
    assert depth[:2] == [1, 1] and depth[2:4] == [2, 2] and depth[4:6] == [3, 3]
    assert depth.index(3) < depth.index(2, 4)  # the first branch's endings come before the second branch's passages


def test_expand_frontier_skip_review_runs_every_level(client, story, monkeypatch):
    use(monkeypatch, growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": plan(2),
                                                   "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    # no pause between levels: level 0 grows 2, the ending level 4 under them
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

    fake = use(monkeypatch, enders(flaky))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": plan(1, ends_story=True),
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

    use(monkeypatch, enders(stubborn))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": plan(2, ends_story=True),
                                                   "policy": {"refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    # the sibling still grows; the failed choice is not written as a dangling link
    assert run["progress"]["created"] == 1 and "Go left" in run["error"]
    g = client.get(f"{story['base']}/graph").json()
    assert [c["text"] for c in g["choices"] if c["from_passage"] == story["start"]] == ["Enter", "Wait", "Go right"]


def test_expand_frontier_branching_caps_choices_and_last_level_ends(client, story, monkeypatch):
    fake = use(monkeypatch, growth)  # offers two choices everywhere
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": plan(1),
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
    endings = [p for p in fake.prompts if "Write the ending of this story" in p]
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
                                                   "budget": plan(2, 2),
                                                   "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    g = client.get(f"{story['base']}/graph").json()
    by_title = {p["title"]: p for p in g["passages"]}
    # 'Go left' ends its storyline at once and is not grown; 'Go right' keeps branching
    assert by_title["Go left from Start"]["is_ending"]
    assert not any(c["from_passage"] == by_title["Go left from Start"]["id"] for c in g["choices"])
    assert any(c["from_passage"] == by_title["Go right from Start"]["id"] for c in g["choices"])
    assert run["status"] == "done"


def test_pacing_plan_is_stored_per_story(client, story):
    base = story["base"]
    assert client.get(f"{base}/pacing").json() == agents.DEFAULT_PLAN
    mine = {**plan(2, 3, guidance="Push."), "steer": "Grim.", "max_seconds": 60, "policy": {"refine": {"rounds": 0}}}
    r = client.put(f"{base}/pacing", json=mine)
    assert r.status_code == 200
    got = client.get(f"{base}/pacing").json()
    assert [a["branching"] for a in got["acts"]] == [2, 3] and got["steer"] == "Grim." and got["ending"] == {"length": "", "guidance": ""}
    other = client.post("/api/stories", json={"title": "Other"}).json()
    assert client.get(f"/api/stories/{other['id']}/pacing").json() == agents.DEFAULT_PLAN
    for bad in ({**mine, "acts": []}, {**mine, "max_passages": 4}, plan(2, levels=0)):
        assert client.put(f"{base}/pacing", json=bad).status_code == 422
    assert client.delete(f"{base}/pacing").json() == agents.DEFAULT_PLAN
    assert client.get("/api/stories/999/pacing").status_code == 404


def test_acts_pace_the_run(client, story, monkeypatch):
    """Each level takes its act's guidance, length and branching; the choices out of a passage
    take its act's; an act that forbids early endings cannot end a storyline; the level past
    the last act is the ending, with the ending's guidance. The saved plan's steer applies."""
    def eager(prompt):
        out = growth(prompt)
        if "Suggest new choices" not in prompt:
            out["ends_story"] = True  # the writer tries to end every storyline at once
        return out

    fake = use(monkeypatch, eager)
    base = story["base"]
    client.put(f"{base}/pacing", json={
        "acts": [{"name": "Setup", "levels": 1, "branching": 1, "ends_story": False, "length": "LEN-A", "guidance": "GUIDE-A"},
                 {"name": "Climax", "levels": 1, "branching": 2, "ends_story": True, "length": "LEN-B", "guidance": "GUIDE-B"}],
        "ending": {"length": "LEN-E", "guidance": "GUIDE-E"}, "steer": "Grim.", "policy": {"refine": {"rounds": 0}}})
    run = wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                               "policy": {"skip_review_interrupt": True}}).json()["id"])
    assert run["status"] == "done" and run["budget"]["depth"] == 3 and run["params"]["steer"] == "Grim."
    assert run["policy"] == {"refine": {"rounds": 0}, "skip_review_interrupt": True}
    g = client.get(f"{base}/graph").json()
    by_title = {p["title"]: p for p in g["passages"]}
    # Setup forbids early endings: its one passage is grown despite ends_story; Climax allows them
    setup = by_title["Go left from Start"]
    assert not setup["is_ending"] and "Go right from Start" not in by_title
    climax = by_title["Go left from Go left from Start"]
    assert climax["is_ending"] and not any(c["from_passage"] == climax["id"] for c in g["choices"])
    choices = [p for p in fake.prompts if "Suggest new choices" in p]
    assert len(choices) == 2  # the root's, then the Setup passage's; Climax passages ended early
    assert "Offer at most 1 choices" in choices[0] and all("STORY ARC: Setup → Climax → Resolution" in c for c in choices)
    # the arc says where PASSAGE sits and what the next passages open: the root is in Setup with its children;
    # the Setup passage's choices lead into the Climax
    assert "PASSAGE and the passages written from it are in Setup: GUIDE-A" in choices[0]
    assert "PASSAGE is in Setup: GUIDE-A" in choices[1] and "The passages written next open Climax: GUIDE-B" in choices[1]
    passages = {passage_title(p): p for p in fake.prompts if "Suggest new choices" not in p}
    assert "about LEN-A" in passages["Start"] and "GUIDE-A" in passages["Start"]
    assert "about LEN-B" in passages["Go left from Start"] and "GUIDE-B" in passages["Go left from Start"]
    assert all(p.rstrip().endswith("AUTHOR'S DIRECTION for everything you write: Grim.") for p in fake.prompts)
    assert not any("GUIDE-E" in p for p in fake.prompts)  # every Climax passage ended, so no ending level


def test_the_ending_level_takes_the_endings_guidance(client, story, monkeypatch):
    fake = use(monkeypatch, growth)
    base = story["base"]
    run = wait(client, base, client.post(f"{base}/runs", json={
        "workflow": "expand_frontier", "scope": [story["start"]],
        "budget": plan(1, length="LEN-A", ending={"length": "LEN-E", "guidance": "GUIDE-E"}),
        "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}}).json()["id"])
    assert run["status"] == "done"
    [ending] = [p for p in fake.prompts if "Write the ending of this story" in p]
    # an ending is not a gamebook passage: the story as prose, the reader's last action, no length target
    assert "GUIDE-E" in ending and "LEN-E" not in ending and "LEN-A" not in ending
    assert "THE STORY SO FAR" in ending and "THE LAST THING THE READER DID: Go left" in ending
    assert "PASSAGE:" not in ending and "EXISTING CHOICES" not in ending and "(the reader chose" not in ending
    assert [p["is_ending"] for p in client.get(f"{base}/graph").json()["passages"] if p["provenance"] == "agent"] == [False, True]


def test_context_carries_the_whole_path(client, story, monkeypatch):
    fake = use(monkeypatch, growth)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                   "budget": plan(2, 2),
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
        if "Check DRAFT below against STORY SO FAR" in prompt or "Compare DRAFT below to WORLD." in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left into the dark."}

    use(monkeypatch, enders(passing))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(1, ends_story=True),
                                                    "policy": {"refine": {"rounds": 2}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done" and run["error"] is None
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert new["body"] == "You go left into the dark."
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
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": "tense: too terse", "quote": body, "evidence": ""}]}
        if "Compare DRAFT below to WORLD." in prompt:
            return {"verdict": "pass", "issues": []}
        if "DRAFT below was audited" in prompt:
            assert "too terse" in prompt
            title = re.search(r"DRAFT TITLE: (.*)", prompt).group(1)
            body = re.search(r"DRAFT BODY: (.+)", prompt).group(1)
            return {"title": title, "body": body + " Revised."}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, enders(revising))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(1, ends_story=True),
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
        if "Compare DRAFT below to WORLD." in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, enders(bad_quote))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(1, ends_story=True),
                                                    "policy": {"refine": {"rounds": 2}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    # the unverifiable issue is dropped, so the round reads as a pass
    assert new["body"] == "You go left."
    rows = [r for r in audits() if r["auditor"] == "audit_continuity"]
    assert len(rows) == 1 and rows[0]["dropped"] == 1 and rows[0]["issues"] == "[]"


def test_refine_stops_at_the_revision_limit_and_grows_on(client, story, monkeypatch):
    """Notes that never clear cost exactly `rounds` revisions; the passage as it stands
    then is just a passage, and the next level grows from it."""
    revisions = []

    def stuck(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": "tense: flat",
                                                      "quote": "You go left.", "evidence": ""}]}
        if "Compare DRAFT below to WORLD." in prompt or "as the ENDING of this story" in prompt:
            return {"verdict": "pass", "issues": []}
        if "DRAFT below was audited" in prompt:
            revisions.append(prompt)
        # the same text every time: the note never gets fixed
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, stuck)
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(1, 1),
                                                    "policy": {"skip_review_interrupt": True, "refine": {"rounds": 3}}})
    run = wait(client, story["base"], r.json()["id"])
    g = client.get(f"{story['base']}/graph").json()
    agent = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert len(agent) == 3 and run["error"] is None  # two acts and the ending: each level grew from the last
    assert all(p["meta"] == {} for p in agent)  # nothing is marked
    # per passage: three audits and three revisions, the last revision unaudited
    assert len(revisions) == 9
    rounds = [r["round"] for r in audits() if r["auditor"] == "audit_continuity"]
    assert sorted(rounds) == [1] * 3 + [2] * 3 + [3] * 3


def test_refine_exits_on_a_clean_audit(client, story, monkeypatch):
    def clean_after_one(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            body = re.search(r"DRAFT BODY: (.+)", prompt).group(1)
            if body.endswith(" Revised."):
                return {"verdict": "pass", "issues": []}
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": "tense: flat", "quote": body, "evidence": ""}]}
        if "Compare DRAFT below to WORLD." in prompt:
            return {"verdict": "pass", "issues": []}
        if "DRAFT below was audited" in prompt:
            body = re.search(r"DRAFT BODY: (.+)", prompt).group(1)
            return {"title": "Left", "body": body + " Revised."}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, enders(clean_after_one))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(1, ends_story=True), "policy": {"refine": {"rounds": 3}}})
    wait(client, story["base"], r.json()["id"])
    [new] = [p for p in client.get(f"{story['base']}/graph").json()["passages"] if p["provenance"] == "agent"]
    assert new["body"] == "You go left. Revised."
    assert sorted({r["round"] for r in audits()}) == [1, 2]  # stopped at the first clean audit


def test_audit_canon_skipped_without_lore(client, monkeypatch):
    s = client.post("/api/stories", json={"title": "Bare", "description": ""}).json()
    base = f"/api/stories/{s['id']}"
    client.patch(f"{base}/passages/{s['start_passage_id']}", json={"body": "You stand alone."})

    def passing(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, enders(passing))
    r = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [s["start_passage_id"]],
                                          "budget": plan(1, ends_story=True),
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
        if "Compare DRAFT below to WORLD." in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, enders(voting))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(1, ends_story=True),
                                                    "policy": {"refine": {"rounds": 1, "votes": 3}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]


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
        if "Compare DRAFT below to WORLD." in prompt:
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, enders(exploding))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(1, ends_story=True),
                                                    "policy": {"refine": {"rounds": 1}}})
    run = wait(client, story["base"], r.json()["id"])
    # the run still completes and writes the passage; the failed call is logged, not fatal
    assert run["status"] == "done" and "model exploded" in run["error"]
    g = client.get(f"{story['base']}/graph").json()
    [new] = [p for p in g["passages"] if p["provenance"] == "agent"]
    assert new["body"] == "You go left."
    rows = [r for r in audits() if r["auditor"] == "audit_continuity"]
    assert rows[0]["verdict"] == "error" and rows[0]["issues"] == "[]"


def test_refine_rounds_zero_is_a_true_noop(client, story, monkeypatch):
    fake = use(monkeypatch, enders(growth))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(1, ends_story=True),
                                                    "policy": {"refine": {"rounds": 0}}})
    run = wait(client, story["base"], r.json()["id"])
    assert run["status"] == "done"
    assert audits() == []
    assert not any("DRAFT TITLE" in p or "Check DRAFT" in p for p in fake.prompts)


# --- concurrent fan-out ---------------------------------------------------------


def test_refine_fans_out_auditors_concurrently(client, story, monkeypatch):
    """audit_continuity and audit_canon must actually overlap within one round."""
    monkeypatch.setattr(agents, "_GATE", agents.Gate(2))
    barrier = threading.Barrier(2, timeout=5)

    def concurrent_auditing(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Left"}]}
        if "Check DRAFT below against STORY SO FAR" in prompt or "Compare DRAFT below to WORLD." in prompt:
            barrier.wait()
            return {"verdict": "pass", "issues": []}
        return {"title": "Left", "body": "You go left."}

    use(monkeypatch, enders(concurrent_auditing))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(1, ends_story=True),
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
        if "Check DRAFT below against STORY SO FAR" in prompt or "Compare DRAFT below to WORLD." in prompt:
            return {"verdict": "pass", "issues": []}
        if "took the choice 'Go left'" in prompt:
            return {"title": "L", "body": "..."}  # always a placeholder: exhausts retries
        return {"title": "R", "body": "You go right."}

    use(monkeypatch, enders(stubborn))
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                    "budget": plan(2, ends_story=True),
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
    assert agents._verify_issue({"quote": "you GO   left", "evidence": "", "problem": "shifts tense"}, draft, ctx)
    # no evidence is only excusable for a tense/person slip
    assert not agents._verify_issue({"quote": "You go left", "evidence": "", "problem": "a detail nobody earned"}, draft, ctx)
    assert agents._verify_issue({"quote": "You go left", "evidence": "the CAVE is dark"}, draft, ctx)
    assert not agents._verify_issue({"quote": "not present", "evidence": "", "problem": "tense"}, draft, ctx)
    assert not agents._verify_issue({"quote": "You go left", "evidence": "not present"}, draft, ctx)


def test_fingerprint_ignores_case_and_whitespace():
    a = agents._fingerprint({"quote": "You  GO left", "problem": "Too  Terse"})
    b = agents._fingerprint({"quote": "you go   left", "problem": "too terse"})
    assert a == b


def test_expand_frontier_proposes_where_reviewed(client, story, monkeypatch):
    use(monkeypatch, growth)
    client.patch(f"{story['base']}/passages/{story['start']}", json={"reviewed": True})
    r = client.post(f"{story['base']}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]], "budget": plan(2),
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
    # the old flat cap is gone, and a plan must have an act
    for bad in ({"max_passages": 6}, {"depth": 2}, {"acts": []}, plan(0), plan(7)):
        r = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]], "budget": bad})
        assert r.status_code == 422, bad
    # an empty budget runs the story's plan (here the default), which pauses after its first level
    r = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                          "policy": {"refine": {"rounds": 0}}})
    run = wait(client, base, r.json()["id"])
    assert run["budget"] == {**agents.DEFAULT_PLAN, "depth": agents.plan_depth(agents.DEFAULT_PLAN)}
    assert run["status"] == "paused"
    assert client.post(f"{base}/runs/{run['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"{base}/runs/{run['id']}/resume", json={}).status_code == 409


def test_cancel_while_running(client, story, monkeypatch):
    """Cancel lands while the model is mid-call: the run is `cancelled` at once and its thread stops
    without waiting for the model, having written nothing more."""
    gate = threading.Event()
    def slow(prompt):
        gate.wait(5)
        return growth(prompt)
    use(monkeypatch, slow)
    base = story["base"]
    run_id = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                "policy": {"refine": {"rounds": 0}}}).json()["id"]
    time.sleep(0.2)  # let the worker reach the model call
    assert client.post(f"{base}/runs/{run_id}/cancel").json()["status"] == "cancelled"
    t0 = time.time()
    run = wait(client, base, run_id)  # the model is still blocked: the thread must not wait for it
    assert time.time() - t0 < 3 and not gate.is_set()
    gate.set()
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
    # a story with no lore has nothing to check against
    assert client.post(f"/api/stories/{other['id']}/runs", json={"workflow": "canon_check", "scope": []}).status_code == 422


def test_workflow_topologies():
    wf = agents.workflow_json()
    assert set(wf) == {"verb", "interview", "canon_check", "expand_frontier"}
    assert [s["id"] for s in wf["interview"]["loop"]["steps"]] == ["gather", "converse", "validate", "review", "apply", "fail"]
    assert next(s for s in wf["interview"]["loop"]["steps"] if s["id"] == "converse")["roles"] == ["interview"]
    assert next(s for s in wf["interview"]["loop"]["steps"] if s["id"] == "review")["roles"] == ["audit_interview"]
    edges = {(e["source"], e["target"]) for e in wf["expand_frontier"]["edges"]}
    assert {("plan", "grow"), ("grow", "gather"), ("gather", "review"), ("review", "plan"), ("finish", "__end__")} <= edges
    # the other workflows' loops are their own nodes
    assert [s["id"] for s in wf["canon_check"]["loop"]["steps"]] == [n["id"] for n in wf["canon_check"]["nodes"]
                                                                     if not n["id"].startswith("__")]


def test_loop_matches_node_prompts():
    """The Agents view draws expand_frontier from FRONTIER_LOOP: every graph node but finish
    appears, grow's steps compose exactly the roles NODE_PROMPTS declares for it, and every
    edge joins two steps."""
    loop = agents.workflow_json()["expand_frontier"]["loop"]
    steps = {s["id"]: s for s in loop["steps"]}
    inside = [s for s in loop["steps"] if s.get("inside") == "grow"]
    assert [r for s in inside for r in s["roles"]] == agents.NODE_PROMPTS[("expand_frontier", "grow")]
    nodes = {n["id"] for n in agents.workflow_json()["expand_frontier"]["nodes"]} - {"__start__", "__end__", "finish", "grow"}
    assert nodes == {s["id"] for s in loop["steps"] if "inside" not in s}
    assert all(a in steps and b in steps for a, b in loop["edges"])


# --- prompts ------------------------------------------------------------------------


def test_prompt_override_reaches_verb_in_every_story(client, story, monkeypatch):
    fake = use(monkeypatch, {"body": "x"})
    lst = client.get("/api/agents/prompts").json()
    assert [p["key"] for p in lst] == list(prompts.PROMPTS)
    body = next(p for p in lst if p["key"] == "draft_body")
    assert body["override"] is None and body["used_by"] == [["verb", "generate"]]
    assert next(p for p in lst if p["key"] == "frontier_passage")["placeholders"] == [
        "choice", "parent_ending", "intent_line", "siblings_line", "remaining", "act_remaining", "length", "ending_rule"]

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
                                                               "budget": plan(2),
                                                               "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert run["status"] == "paused" and run["params"]["prompts"] == prompts.resolve()
    # edit mid-run: the paused run resumes with the texts it started with
    terse = "Suggest new choices, but tersely: at most {branching}, {remaining} left."
    assert client.put("/api/agents/prompts/frontier_choices", json={"text": terse}).status_code == 200
    n = len(fake.prompts)
    wait(client, base, client.post(f"{base}/runs/{run['id']}/resume", json={}).json()["id"])
    assert fake.prompts[n:] and not any("tersely" in p for p in fake.prompts[n:])
    # a new run picks the override up
    later = client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]], "budget": plan(3, guidance="Hurry."),
                                              "policy": {"refine": {"rounds": 0}}}).json()
    assert later["params"]["prompts"]["frontier_choices"] == terse
    wait(client, base, later["id"])
    assert any(p.endswith("TASK: Suggest new choices, but tersely: at most 3, 2 left.") for p in fake.prompts)


def test_preview_equals_what_the_model_receives(client, story, monkeypatch):
    base = story["base"]
    # preview first: the verb writes straight into the unreviewed passage
    r = client.post(f"{base}/prompts/draft_body/preview", json={"passage_id": story["start"]})
    fake = use(monkeypatch, {"body": "x"})
    client.post(f"{base}/passages/{story['start']}/assist", json={"verb": "draft_body"})
    assert r.json()["prompt"] == fake.prompts[-1]

    # {choice} is the first outgoing choice, or a stand-in; an unsaved draft is previewed, not saved;
    # the pacing slots are the story's plan's, at the stage asked for
    client.put(f"{base}/pacing", json=plan(2, 3, length="short", guidance="Open.",
                                           ending={"length": "long", "guidance": "Close."}))
    draft = "After '{choice}', {remaining} left, {act_remaining} in act, {length}: [{intent_line}|{siblings_line}|{parent_ending}]{ending_rule}"
    r = client.post(f"{base}/prompts/frontier_passage/preview", json={"passage_id": story["start"], "text": draft})
    assert "TASK: After 'Enter', 3 left, 1 in act, short: [" in r.json()["prompt"] and prompts.overrides() == {}
    r = client.post(f"{base}/prompts/frontier_passage/preview", json={"passage_id": story["start"], "text": draft, "stage": 2})
    assert "TASK: After 'Enter', 1 left, 1 in act, long: [" in r.json()["prompt"]
    # an unsaved plan previews without being saved
    r = client.post(f"{base}/prompts/frontier_passage/preview", json={"passage_id": story["start"], "text": draft,
                                                                      "plan": plan(2, length="tiny", guidance="Draft.")})
    assert "TASK: After 'Enter', 2 left, 1 in act, tiny: [" in r.json()["prompt"]
    assert client.get(f"{base}/pacing").json()["acts"][0]["guidance"] == "Open."
    r = client.post(f"{base}/prompts/draft_target/preview", json={"passage_id": story["pit"]})
    assert "took the choice '(example choice)'" in r.json()["prompt"]
    assert client.post(f"{base}/prompts/draft_body/preview", json={"passage_id": story["start"], "text": "{x}"}).status_code == 422

    # a later act's choices are the ones out of its first passage; a run with no budget of its own
    # takes the saved plan, and stage 0's choices preview is the root's, the run's first prompt
    r = client.post(f"{base}/prompts/frontier_choices/preview", json={"passage_id": story["pit"], "stage": 1})
    assert "Offer at most 3 choices" in r.json()["prompt"] and "STORY ARC: Act 1 → Act 2 → Resolution" in r.json()["prompt"]
    assert "Open." in r.json()["prompt"]
    r = client.post(f"{base}/prompts/frontier_choices/preview", json={"passage_id": story["start"]})
    fake = use(monkeypatch, growth)
    run = wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                               "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert run["budget"]["depth"] == 3 and run["status"] == "paused"
    wait(client, base, client.post(f"{base}/runs/{run['id']}/resume", json={"stop": True}).json()["id"])
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
            return {"verdict": "revise", "issues": [{"severity": "minor", "problem": "tense: flat prose", "quote": body, "evidence": ""}]}
        if "Compare DRAFT below to WORLD." in prompt or "as the ENDING of this story" in prompt:
            return {"verdict": "pass", "issues": []}
        if "DRAFT below was audited" in prompt:
            title = re.search(r"DRAFT TITLE: (.*)", prompt).group(1)
            body = re.search(r"DRAFT BODY: (.+)", prompt).group(1)
            return {"title": title, "body": body + " Revised."}
        return growth(prompt)

    workflow["name"] = "expand_frontier"
    use(monkeypatch, refining)
    # the Pit is a level down, so a two-act plan leaves a mid-story passage and an ending; default refine
    # policy (rounds: 2) so every declared role, including audit_continuity/audit_canon/
    # revise_passage, actually gets composed at least once.
    wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["pit"]],
                                                         "budget": plan(1, 1), "policy": {"skip_review_interrupt": True}}).json()["id"])

    workflow["name"] = "interview"
    use(monkeypatch, {"reply": "Who are you?"})
    iv = client.post(f"{base}/interviews", json={})
    assert iv.status_code == 201
    # a turn that writes is the one that composes the reviewer
    use(monkeypatch, {"premise": "You are a lamp thief.", "reply": "Why?"})
    assert client.post(f"{base}/interviews/{iv.json()['id']}/turn", json={"text": "A lamp thief."}).status_code == 200

    declared = {(w, n, k) for (w, n), keys in agents.NODE_PROMPTS.items() for k in keys}
    assert seen == declared
    assert {k for _, _, k in declared} == set(prompts.PROMPTS)
    nodes = {n["id"]: n["prompts"] for n in agents.workflow_json()["expand_frontier"]["nodes"]}
    assert nodes["grow"] == ["frontier_choices", "frontier_passage", "frontier_ending",
                             "audit_continuity", "audit_canon", "revise_passage", "audit_ending", "revise_ending"] and nodes["plan"] == []


# --- interview ------------------------------------------------------------------------


def characters(client, base):
    return {l["name"]: l for l in client.get(f"{base}/graph").json()["lore"] if l["kind"] == "character"}


def test_interview_opens_then_writes_premise_and_characters(client, story, monkeypatch):
    base = story["base"]
    peddler = characters(client, base)["The Peddler"]
    fake = use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()
    assert iv["status"] == "open" and [e["role"] for e in iv["transcript"]] == ["agent"]
    assert iv["transcript"][0]["text"] == "Who is the reader?" and iv["transcript"][0]["writes"] == []
    # the frame is the conversation, with every character addressable by id
    prompt = fake.prompts[0]
    for needle in ("PREMISE (the story description", "A cave.", f"#{peddler['id']} The Peddler: Sells lamps.",
                   "OPENING PASSAGE (read-only): Start", "(empty: this is the opening turn)"):
        assert needle in prompt, needle

    fake = use(monkeypatch, {
        "premise": "You are the Peddler's apprentice, hunting the thief who stole his last lamp.",
        "characters": [{"op": "create", "name": "Wren", "body": "The thief: quick, hungry, and afraid of the dark."},
                       {"op": "edit", "id": peddler["id"], "name": "The Peddler", "body": "Sells lamps; your master, robbed."}],
        "reply": "What does Wren want the lamp for?"})
    r = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "I'm his apprentice. A girl called Wren stole it."})
    assert r.status_code == 200, r.text
    iv = r.json()
    assert "AUTHOR: I'm his apprentice." in fake.prompts[0]
    assert [e["role"] for e in iv["transcript"]] == ["agent", "author", "agent"]
    writes = iv["transcript"][2]["writes"]
    assert [(w["table"], w["before"] is None) for w in writes] == [("stories", False), ("lore", True), ("lore", False)]
    g = client.get(f"{base}/graph").json()
    assert g["story"]["description"].startswith("You are the Peddler's apprentice")
    chars = characters(client, base)
    assert chars["Wren"]["body"].startswith("The thief") and chars["Wren"]["reviewed"] is False
    assert chars["The Peddler"]["body"] == "Sells lamps; your master, robbed."
    assert client.get(f"{base}/interviews").json()[0]["id"] == iv["id"]


def test_interview_turns_collisions_and_reviewed_edits_into_conflicts(client, story, monkeypatch):
    base = story["base"]
    peddler = characters(client, base)["The Peddler"]
    client.patch(f"{base}/lore/{peddler['id']}", json={"reviewed": True})
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()

    fake = use(monkeypatch, {
        "characters": [{"op": "edit", "id": peddler["id"], "name": "The Peddler", "body": "A stranger."},
                       {"op": "create", "name": "the  peddler", "body": "Another lamp seller."}],
        "conflicts": [{"about": "the cave", "a": "A cave.", "b": "there is no cave"}],
        "reply": "Is the Peddler your master or a stranger?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "The peddler is a stranger. There is no cave."}).json()
    assert iv["transcript"][-1]["writes"] == []
    assert characters(client, base)["The Peddler"]["body"] == "Sells lamps."
    assert [(c["id"], c["source"], c["status"]) for c in iv["conflicts"]] == [(1, "model", "open"), (2, "code", "open"), (3, "code", "open")]
    assert iv["conflicts"][1]["about"] == "The Peddler is reviewed" and iv["conflicts"][2]["about"] == "the name the  peddler is taken"
    assert iv["transcript"][-1]["opened"] == [1, 2, 3]

    # open conflicts reach the next prompt; a skip cannot resolve one, an answer can
    fake = use(monkeypatch, {"resolved": [1, 99], "reply": "And the Peddler?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"mode": "skip"}).json()
    assert "OPEN CONFLICTS" in fake.prompts[0] and '#1 the cave: "A cave." versus "there is no cave"' in fake.prompts[0]
    assert "AUTHOR: (skipped your question; do not ask it again in any wording" in fake.prompts[0]
    assert all(c["status"] == "open" for c in iv["conflicts"])
    use(monkeypatch, {"resolved": [1, 99], "reply": "And the Peddler?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Keep the cave."}).json()
    assert [c["status"] for c in iv["conflicts"]] == ["resolved", "open", "open"]
    assert iv["conflicts"][0]["resolution"] == "Keep the cave." and iv["transcript"][-1]["resolved"] == [1]


def test_interview_agent_fixes_a_duplicate_create_on_retry(client, story, monkeypatch):
    """Creating someone who already exists is fed back with the agent's own reply, and its
    corrected edit is what gets written; the author is never asked about it."""
    base = story["base"]
    peddler = characters(client, base)["The Peddler"]
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()

    fake = use(monkeypatch,
               {"characters": [{"op": "create", "name": "The Peddler", "body": "Sells lamps; your master, robbed."}], "reply": "Who robbed him?"},
               {"characters": [{"op": "edit", "id": peddler["id"], "name": "The Peddler", "body": "Sells lamps; your master, robbed."}],
                "reply": "Who robbed him?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "The peddler was robbed."}).json()
    assert len(fake.prompts) == 2
    retry = fake.prompts[1]
    assert f"#{peddler['id']} The Peddler already exists" in retry and f'use op "edit" with id {peddler["id"]}' in retry
    assert '"op": "create"' in retry  # the agent sees what it said
    assert iv["conflicts"] == []
    assert [(w["table"], w["before"] is None) for w in iv["transcript"][-1]["writes"]] == [("lore", False)]
    assert characters(client, base)["The Peddler"]["body"] == "Sells lamps; your master, robbed."
    assert list(characters(client, base)).count("The Peddler") == 1


def test_interview_repeating_a_character_word_for_word_is_a_no_op(client, story, monkeypatch):
    base = story["base"]
    peddler = characters(client, base)["The Peddler"]
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()
    fake = use(monkeypatch, {"characters": [
        {"op": "create", "name": "the peddler", "body": peddler["body"]},
        {"op": "edit", "id": peddler["id"], "name": peddler["name"], "body": peddler["body"]}], "reply": "What does the Peddler want most?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Tell me more."}).json()
    assert len(fake.prompts) == 1 and iv["conflicts"] == [] and iv["transcript"][-1]["writes"] == []
    assert len(characters(client, base)) == 1


def test_interview_null_in_words_is_no_change(client, story, monkeypatch):
    """A model that says "no premise" as the string "null" must not overwrite the description."""
    base = story["base"]
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()
    fake = use(monkeypatch, {"premise": " Null. ", "reply": "And the thief?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Nothing new."}).json()
    assert client.get(f"{base}/graph").json()["story"]["description"] == "A cave."
    assert iv["transcript"][-1]["writes"] == [] and len(fake.prompts) == 1
    # a character whose body is the word null is a fixable problem, not a write
    fake = use(monkeypatch, {"characters": [{"op": "create", "name": "Wren", "body": "null"}], "reply": "Why the lamp?"},
               {"characters": [{"op": "create", "name": "Wren", "body": "The thief."}], "reply": "Why the lamp?"})
    client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Wren."})
    assert "empty name or body" in fake.prompts[1] and characters(client, base)["Wren"]["body"] == "The thief."


def test_interview_reviewer_sends_a_bad_write_back_to_the_agent(client, story, monkeypatch):
    """A model reviews every write before it is saved; its note goes back to the agent, which
    corrects the change, and only the corrected text is written."""
    base = story["base"]
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()
    fake = use(monkeypatch,
               {"premise": "Pirates sail the seven seas.", "reply": "Which cave is this exactly?"},
               {"premise": "You are the Peddler's apprentice, in a cave.", "reply": "What is the apprentice afraid of?"})
    fake.reviews = [{"verdict": "revise", "issues": [{"change": 1, "problem": "throws away the cave and is unrelated to the answer"}]},
                    {"verdict": "pass", "issues": []}]
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "I'm the peddler's apprentice."}).json()
    assert len(fake.prompts) == 2 and len(fake.review_prompts) == 2
    # the reviewer read the text being replaced, the new text, and the conversation
    for needle in ("BEFORE: A cave.", "AFTER: Pirates sail", "AUTHOR: I'm the peddler's apprentice.", "[1] PREMISE",
                   "THE AUTHOR'S LATEST MESSAGE: I'm the peddler's apprentice."):
        assert needle in fake.review_prompts[0], needle
    # the agent got the note, and its own reply back
    assert "a reviewer would not accept this reply" in fake.prompts[1]
    assert "change [1]: throws away the cave" in fake.prompts[1] and "Pirates sail" in fake.prompts[1]
    assert client.get(f"{base}/graph").json()["story"]["description"] == "You are the Peddler's apprentice, in a cave."
    assert iv["conflicts"] == [] and len(iv["transcript"][-1]["writes"]) == 1


def test_interview_write_the_reviewer_still_rejects_is_held_for_the_author(client, story, monkeypatch):
    base = story["base"]
    peddler = characters(client, base)["The Peddler"]
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()
    fake = use(monkeypatch, {"premise": "Pirates sail the seven seas.",
                             "characters": [{"op": "create", "name": "Wren", "body": "The thief."},
                                            {"op": "edit", "id": peddler["id"], "name": "The Peddler", "body": "A ghost."}],
                             "reply": "Who is Wren?"})
    fake.reviews = [{"verdict": "revise", "issues": [{"change": 1, "problem": "unrelated"}, {"change": 3, "problem": "erases him"},
                                                     {"change": 99, "problem": "no such change"}]}]
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Wren stole the lamp."}).json()
    assert len(fake.prompts) == agents.INTERVIEW_ATTEMPTS  # it was told each time, then the writes were held
    # what the reviewer passed is written; what it did not is not, and the author is asked
    assert client.get(f"{base}/graph").json()["story"]["description"] == "A cave."
    chars = characters(client, base)
    assert chars["Wren"]["body"] == "The thief." and chars["The Peddler"]["body"] == "Sells lamps."
    assert [w["table"] for w in iv["transcript"][-1]["writes"]] == ["lore"]
    assert [(c["source"], c["status"]) for c in iv["conflicts"]] == [("code", "open"), ("code", "open")]
    assert iv["conflicts"][0]["about"] == "held back: unrelated" and iv["conflicts"][0]["a"] == "A cave."
    assert iv["conflicts"][0]["b"] == "Pirates sail the seven seas."
    assert iv["conflicts"][1]["a"] == "Sells lamps." and iv["conflicts"][1]["b"].endswith("A ghost.")


def test_interview_reviewer_rewrites_a_plot_question_and_never_holds_the_question_back(client, story, monkeypatch):
    base = story["base"]
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()
    fake = use(monkeypatch, {"reply": "What does the reader find when they enter the cave?"},
               {"reply": "What is the reader most afraid of in the cave?"})
    fake.reviews = [{"verdict": "revise", "issues": [{"change": 0, "problem": "asks what someone finds"}]},
                    {"verdict": "pass", "issues": []}]
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "They enter the cave."}).json()
    assert "your question: asks what someone finds" in fake.prompts[1]
    assert iv["transcript"][-1]["text"] == "What is the reader most afraid of in the cave?"

    # a question the reviewer never accepts is still asked, since the turn needs one; nothing is held
    fake = use(monkeypatch, {"reply": "What happens next?"})
    fake.reviews = [{"verdict": "revise", "issues": [{"change": 0, "problem": "asks what happens next"}]}]
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "I'm not sure."}).json()
    assert len(fake.prompts) == agents.INTERVIEW_ATTEMPTS
    assert iv["transcript"][-1]["text"] == "What happens next?" and iv["conflicts"] == []


def test_interview_writes_nothing_when_the_reviewer_cannot_run(client, story, monkeypatch):
    base = story["base"]
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()
    fake = use(monkeypatch, {"premise": "You are a thief.", "reply": "Why?"})
    fake.reviews = [RuntimeError("model down")]
    r = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "A thief."})
    assert r.status_code == 502 and "reviewer could not run" in r.json()["detail"]
    assert client.get(f"{base}/graph").json()["story"]["description"] == "A cave."
    assert len(client.get(f"{base}/interviews/{iv['id']}").json()["transcript"]) == 1
    # the question is reviewed too, so even a turn that writes nothing needs the reviewer
    fake = use(monkeypatch, {"reply": "And?"})
    fake.reviews = [RuntimeError("model down")]
    assert client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Hm."}).status_code == 502
    assert "PROPOSED QUESTION [0]: And?" in fake.review_prompts[0] and "PROPOSED CHANGES:\n(none)" in fake.review_prompts[0]


def test_interview_retries_bad_ops_and_records_nothing_on_failure(client, story, monkeypatch):
    base = story["base"]
    place = client.post(f"{base}/lore", json={"kind": "place", "name": "The Pit", "body": "Lightless."}).json()
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()

    fake = use(monkeypatch,
               {"characters": [{"op": "edit", "id": place["id"], "name": "The Pit", "body": "Bright."}], "reply": "Next?"},
               {"characters": [{"op": "edit", "id": 999, "name": "Ghost", "body": "Boo."}], "reply": "Next?"},
               {"characters": [{"op": "create", "name": "Wren", "body": "The thief."}], "reply": "Next?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Wren is the thief."}).json()
    assert f"#{place['id']} is not a character" in fake.prompts[1] and "names #999, which is not in CHARACTERS" in fake.prompts[2]
    assert "Wren" in characters(client, base) and len(iv["transcript"]) == 3

    use(monkeypatch, {"reply": "  "})
    r = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Hello?"})
    assert r.status_code == 502 and "reply was empty" in r.json()["detail"]
    assert len(client.get(f"{base}/interviews/{iv['id']}").json()["transcript"]) == 3  # the author's line too

    assert client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": " "}).status_code == 422
    assert client.post(f"{base}/interviews/{iv['id']}/close").json()["status"] == "done"
    assert client.post(f"{base}/interviews/{iv['id']}/turn", json={"mode": "decide"}).status_code == 409

    use(monkeypatch, RuntimeError("model down"))
    assert client.post(f"{base}/interviews", json={}).status_code == 502  # retried, then gave up
    assert len(client.get(f"{base}/interviews").json()) == 1  # the failed opening left no row


def test_interview_merge_and_revert(client, story, monkeypatch):
    base = story["base"]
    wren = client.post(f"{base}/lore", json={"kind": "character", "name": "Wren", "body": "The thief."}).json()
    girl = client.post(f"{base}/lore", json={"kind": "character", "name": "The girl", "body": "Seen at the cave."}).json()
    use(monkeypatch, {"reply": "Who is the reader?"})
    iv = client.post(f"{base}/interviews", json={}).json()
    use(monkeypatch, {"premise": "A heist in the dark.",
                      "characters": [{"op": "merge", "id": wren["id"], "merge_ids": [girl["id"]], "name": "Wren",
                                      "body": "The thief, seen at the cave."}],
                      "reply": "Why the lamp?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "The girl is Wren."}).json()
    assert set(characters(client, base)) == {"The Peddler", "Wren"}
    entry = len(iv["transcript"]) - 1

    # an author edit since the turn blocks reverting that write, and the whole revert with it
    client.patch(f"{base}/lore/{wren['id']}", json={"body": "Edited by hand."})
    r = client.post(f"{base}/interviews/{iv['id']}/revert", json={"entry": entry})
    assert r.status_code == 409 and "Wren has changed" in r.json()["detail"]
    assert "The girl" not in characters(client, base)

    # reverting one write at a time: the delete comes back with its id, the premise goes back
    iv = client.post(f"{base}/interviews/{iv['id']}/revert", json={"entry": entry, "write": 2}).json()
    assert characters(client, base)["The girl"]["id"] == girl["id"]
    iv = client.post(f"{base}/interviews/{iv['id']}/revert", json={"entry": entry, "write": 0}).json()
    assert client.get(f"{base}/graph").json()["story"]["description"] == "A cave."
    assert [w["reverted"] for w in iv["transcript"][entry]["writes"]] == [True, False, True]
    assert client.post(f"{base}/interviews/{iv['id']}/revert", json={"entry": 0}).status_code == 200  # no writes: a no-op
    assert client.post(f"{base}/interviews/{iv['id']}/revert", json={"entry": 1}).status_code == 404  # an author entry


def test_interview_retries_a_restated_question_but_keeps_it_on_the_last_attempt(client, story, monkeypatch):
    base = story["base"]
    use(monkeypatch, {"reply": "Who does the reader trust most in the cave?"})
    iv = client.post(f"{base}/interviews", json={}).json()
    fake = use(monkeypatch, {"reply": "Which person does the reader trust most in the cave?"},
               {"reply": "What is the reader afraid of losing?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Nobody."}).json()
    assert "restates one you already asked" in fake.prompts[1]
    assert iv["transcript"][-1]["text"] == "What is the reader afraid of losing?"
    # a model that keeps restating gets the same treatment as any other bad question: asked after the last try
    fake = use(monkeypatch, {"reply": "What is the reader afraid of losing, really?"})
    iv = client.post(f"{base}/interviews/{iv['id']}/turn", json={"text": "Home."}).json()
    assert len(fake.prompts) == agents.INTERVIEW_ATTEMPTS and iv["transcript"][-1]["text"].endswith("really?")


def test_interview_prompts_aim_at_a_brief_not_a_plot():
    """The roles state what the premise is for, so the agent asks about situations and the
    reviewer holds back a write that fixes a route through the story."""
    ask, gate = prompts.PROMPTS["interview"].default, prompts.PROMPTS["audit_interview"].default
    for needle in ("BRIEF for a choose-your-own-adventure generator", "true on only one path", "holds on every path or is only one option",
                   "never about what happens next", "Never ask what CONVERSATION already asked"):
        assert needle in ask, needle
    assert "fixes an outcome" in gate and "not a route through it" in gate


def test_interview_prompt_preview(client, story):
    r = client.post(f"{story['base']}/prompts/interview/preview", json={"passage_id": story["start"]}).json()
    assert r["prompt"].startswith("STORY: The Lamp") and "CHARACTERS (yours" in r["prompt"]
    assert r["prompt"].endswith("TASK: " + prompts.PROMPTS["interview"].default)


def test_chat_model_caps_tokens_and_sets_thinking_explicitly(monkeypatch):
    """llama-server ignores langchain-openai's max_completion_tokens and thinks unless told
    not to (incidents/2026-09-29-interview-thinking-runaway.md): both go in extra_body."""
    monkeypatch.setattr(llm, "BACKEND", "openai")
    m = llm.chat_model()
    assert m.extra_body == {"max_tokens": llm.MAX_TOKENS, "chat_template_kwargs": {"enable_thinking": False}}
    m = llm.chat_model(reasoning=True, max_tokens=100)
    assert m.extra_body == {"max_tokens": 100, "chat_template_kwargs": {"enable_thinking": True}}


def test_a_body_that_stops_mid_sentence_is_retried(client, story, monkeypatch):
    bodies = iter(["You walk toward the", "You walk toward the door."])
    def model(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Hall"}]}
        return {"title": "Hall", "body": next(bodies)}
    use(monkeypatch, enders(model))
    run = wait(client, story["base"], client.post(f"{story['base']}/runs", json={
        "workflow": "expand_frontier", "scope": [story["start"]], "budget": plan(2, ends_story=True), "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert [p["body"] for p in client.get(f"{story['base']}/graph").json()["passages"] if p["provenance"] == "agent"] == ["You walk toward the door."]
    assert run["progress"].get("retries") == 1


def test_endings_get_a_higher_token_ceiling_and_a_soft_length(client, story, monkeypatch):
    caps = []
    fake = FakeModel([growth])
    monkeypatch.setattr(llm, "chat_model", lambda model=None, **kw: (caps.append(kw.get("max_tokens")), fake)[1])
    wait(client, story["base"], client.post(f"{story['base']}/runs", json={
        "workflow": "expand_frontier", "scope": [story["start"]], "budget": plan(1), "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}}).json()["id"])
    # choices (default ceiling), the level-0 passage (default), then the ending: only that one is raised
    assert caps[-1] == llm.ENDING_MAX_TOKENS and all(c in (None, llm.MAX_TOKENS) for c in caps[:-1]) and llm.ENDING_MAX_TOKENS > llm.MAX_TOKENS
    [ending] = [p for p in fake.prompts if "Write the ending of this story" in p]
    assert "one to two thousand words" in ending and "and no more" not in ending


def test_endings_are_reviewed_as_endings(client, story, monkeypatch):
    """At the ending level the auditors and the reviser see the ending frame the writer saw; audit_ending's
    issue needs a quote from the draft but no evidence span, and revise_ending replaces the draft."""
    seen = {"audit": [], "revise": []}
    def model(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Hall"}]}
        if "as the ENDING of this story" in prompt:
            seen["audit"].append(prompt)
            if "And so it ends." in prompt:  # the revised draft passes
                return {"verdict": "pass", "issues": []}
            return {"verdict": "revise", "issues": [{"severity": "major", "problem": "it does not conclude",
                                                      "quote": "You keep walking.", "evidence": ""}]}
        if "audited as the ending" in prompt:
            seen["revise"].append(prompt)
            return {"title": "Hall", "body": "You keep walking. And so it ends."}
        if "Check DRAFT below against STORY SO FAR" in prompt or "Compare DRAFT below to WORLD." in prompt:
            return {"verdict": "pass", "issues": []}
        if "Write the ending of this story" in prompt:
            return {"title": "Hall", "body": "You keep walking."}
        return {"title": "Hall", "body": "You go on."}
    use(monkeypatch, model)
    run = wait(client, story["base"], client.post(f"{story['base']}/runs", json={
        "workflow": "expand_frontier", "scope": [story["start"]], "budget": plan(1),
        "policy": {"skip_review_interrupt": True, "refine": {"rounds": 1}}}).json()["id"])
    assert run["status"] == "done" and run["error"] is None
    assert len(seen["audit"]) == 1 and len(seen["revise"]) == 1  # audited, revised once; the last revision is not re-audited
    for prompt in seen["audit"] + seen["revise"]:
        assert "THE LAST THING THE READER DID: Go left" in prompt and "PASSAGE:" not in prompt
    assert "it does not conclude" in seen["revise"][0]
    [end] = [p for p in client.get(f"{story['base']}/graph").json()["passages"] if p["is_ending"]]
    assert end["body"] == "You keep walking. And so it ends."


def test_arc_reaches_the_writers_but_not_the_reviewers(client, story, monkeypatch):
    fake = use(monkeypatch, growth)
    base = story["base"]
    wait(client, base, client.post(f"{base}/runs", json={
        "workflow": "expand_frontier", "scope": [story["start"]], "budget": plan(1, 1, guidance="GUIDE"),
        "policy": {"skip_review_interrupt": True, "refine": {"rounds": 1}}}).json()["id"])
    writers = [p for p in fake.prompts if "Suggest new choices" in p or "no destination yet" in p]
    assert writers and all("STORY ARC: Act 1 → Act 2 → Resolution" in p for p in writers)
    reviewers = [p for p in fake.prompts if "Check DRAFT below" in p or "Compare DRAFT below" in p]
    assert reviewers and not any("STORY ARC" in p for p in reviewers)


def test_an_audit_issue_without_severity_still_counts_and_schema_errors_read_briefly():
    rep = agents.AuditReport.model_validate({"verdict": "revise", "issues": [{"problem": "p", "quote": "q"}]})
    assert rep.issues[0].severity == "minor"
    with pytest.raises(Exception) as e:
        agents.AuditReport.model_validate({"verdict": "revise", "issues": [{"severity": "minor"}]})
    msg = agents.brief(e.value)
    assert "\n" not in msg and "issues.0.problem (missing)" in msg and "pydantic" not in msg


def test_clear_history_removes_finished_runs_only(client, story, monkeypatch):
    use(monkeypatch, enders(growth))
    base = story["base"]
    done = wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                                 "budget": plan(2, ends_story=True), "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert done["status"] == "done"
    with db.transaction() as conn:  # a run that is still going, and one with a proposal to review
        conn.execute("INSERT INTO agent_runs (story_id, workflow, thread_id, status) VALUES (?, 'expand_frontier', 'live', 'running')", (story["id"],))
        conn.execute("INSERT INTO agent_runs (story_id, workflow, thread_id, status) VALUES (?, 'expand_frontier', 'review', 'done')", (story["id"],))
        rid = conn.execute("SELECT id FROM agent_runs WHERE thread_id = 'review'").fetchone()["id"]
        conn.execute("INSERT INTO proposals (story_id, batch, verb, kind, payload, run_id) VALUES (?, 'b', 'expand_frontier', 'new_choice', '{}', ?)", (story["id"], rid))
    n_passages = len(client.get(f"{base}/graph").json()["passages"])
    assert client.delete(f"{base}/runs").json() == {"cleared": 1}
    left = {r["thread_id"] for r in client.get(f"{base}/runs").json()}
    assert left == {"live", "review"}
    assert len(client.get(f"{base}/graph").json()["passages"]) == n_passages


def test_retry_events_are_kept_with_their_reason_and_cleared_with_history(client, story, monkeypatch):
    answers = iter([RuntimeError("connection reset"), {"title": "Hall", "body": "..."}])

    def model(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "Hall"}]}
        try:
            return next(answers)
        except StopIteration:
            return {"title": "Hall", "body": "You walk on.", "ends_story": True}

    use(monkeypatch, model)
    base = story["base"]
    run = wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                                "budget": plan(1, ends_story=True), "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert run["status"] == "done" and run["progress"]["retries"] == 2
    with db.transaction() as conn:
        rows = [(r["type"], json.loads(r["data"])) for r in conn.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run["id"],))]
    retries = [d for t, d in rows if t == "retry"]
    assert [(d["attempt"], d["kind"]) for d in retries] == [(1, "exception"), (2, "refused")]
    assert "connection reset" in retries[0]["reason"] and "seconds" in retries[0]
    assert rows[-1][0] == "status" and not any(t == "progress" for t, _ in rows)
    # after a restart the log is replayed from disk
    runs.manager.live.clear()
    assert [e["type"] for e in runs.manager.events(run["id"])].count("retry") == 2
    assert client.delete(f"{base}/runs").json() == {"cleared": 1}
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0


def test_a_draft_without_a_title_takes_the_one_the_choice_offered(client, story, monkeypatch):
    def model(prompt):
        if "Suggest new choices" in prompt:
            return {"choices": [{"text": "Go left", "target_title": "The Hall"}]}
        return {"body": "You walk on.", "ends_story": True}  # no title at all

    use(monkeypatch, model)
    base = story["base"]
    run = wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                                "budget": plan(1, ends_story=True), "policy": {"refine": {"rounds": 0}}}).json()["id"])
    assert run["status"] == "done" and not run["progress"].get("retries")
    assert [p["title"] for p in client.get(f"{base}/graph").json()["passages"] if p["provenance"] == "agent"] == ["The Hall"]


def test_reply_cap_follows_the_plans_length():
    assert llm.reply_cap("150–250 words", 4096) == 4096  # short passages keep the floor
    assert llm.reply_cap("1200 words", 4096) == 5040  # a long one gets room to overshoot
    assert llm.reply_cap("", 4096) == 4096 and llm.reply_cap("as long as it needs", 8192) == 8192
    assert llm.reply_cap("100000 words", 4096) == 16000


def test_retries_are_saved_with_their_reason_and_cleared_with_the_history(client, story, monkeypatch):
    calls = {"n": 0}

    def flaky(prompt):
        out = enders(growth)(prompt)
        if "Suggest new choices" not in prompt:
            calls["n"] += 1
            if calls["n"] == 1:
                return {"title": "X", "body": "..."}  # a placeholder body: refused
        return out

    use(monkeypatch, flaky)
    base = story["base"]
    run = wait(client, base, client.post(f"{base}/runs", json={"workflow": "expand_frontier", "scope": [story["start"]],
                                                                "budget": plan(1, ends_story=True), "policy": {"refine": {"rounds": 0}}}).json()["id"])
    saved = client.get(f"{base}/runs/{run['id']}/log", params={"type": "retry"}).json()
    assert len(saved) == run["progress"]["retries"] == 1
    assert saved[0]["kind"] == "refused" and "written out" in saved[0]["reason"] and '"body":"..."' in saved[0]["answer"]
    assert saved[0]["attempt"] == 1 and saved[0]["phase"] == "passage"
    assert {"node", "status"} <= {e["type"] for e in client.get(f"{base}/runs/{run['id']}/log").json()}
    assert client.delete(f"{base}/runs").json() == {"cleared": 1}
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0


def test_a_reply_cut_off_at_the_ceiling_is_retried_with_more_room(client, story, monkeypatch):
    caps = []
    fake = FakeModel([Truncated(), growth])
    monkeypatch.setattr(llm, "chat_model", lambda model=None, **kw: (caps.append(kw.get("max_tokens")), fake)[1])
    run = wait(client, story["base"], client.post(f"{story['base']}/runs", json={
        "workflow": "expand_frontier", "scope": [story["start"]], "budget": plan(1), "policy": {"skip_review_interrupt": True, "refine": {"rounds": 0}}}).json()["id"])
    assert run["progress"].get("retries", 0) >= 1
    assert any("cut off" in p for p in fake.prompts[1:2]) and caps[1] == 2 * (caps[0] or llm.MAX_TOKENS)


def test_a_passage_is_asked_for_as_prose_not_a_tool_call_and_the_end_line_ends_the_story():
    plain = agents.FrontierPassageDraft.from_prose("You step inside.\n\nThe door shuts.\n\nThe End")
    assert plain.body == "You step inside.\n\nThe door shuts." and plain.ends_story
    assert not agents.FrontierPassageDraft.from_prose("You step inside.").ends_story
    # an act that forbids early endings has no field for it: the line is dropped, not obeyed
    assert agents.FrontierDraft.from_prose("You step inside.\n\nThe end.").body == "You step inside."


def test_leaked_chat_template_markers_are_cut_from_a_prose_reply():
    leaked = "You step inside.\n<|channel>thought\n<channel|><|tool_call>call:FrontierPassageDraft{body:"
    assert agents.FrontierDraft.from_prose(leaked).body == "You step inside."


def test_a_prose_reply_cut_off_at_the_ceiling_is_truncated():
    with pytest.raises(agents.ReplyTruncated):
        agents._unwrap(SimpleNamespace(content="You walk", response_metadata={"finish_reason": "length"}), 100, agents.FrontierDraft)
