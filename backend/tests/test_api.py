import pytest
from fastapi.testclient import TestClient

from cyoa import db
from cyoa.main import app


@pytest.fixture
def client(tmp_path):
    db.DB_PATH = tmp_path / "t.db"
    with TestClient(app) as c:
        yield c


def test_health_and_story_roundtrip(client):
    c = client
    assert c.get("/api/health").json()["ok"] is True

    s = c.post("/api/stories", json={"title": "Cave"}).json()
    assert len(s["ifid"]) == 36 and s["passage_count"] == 1
    start = s["start_passage_id"]
    assert start is not None

    p2 = c.post(f"/api/stories/{s['id']}/passages", json={"title": "Pit"}).json()
    ch = c.post(
        f"/api/stories/{s['id']}/choices",
        json={"from_passage": start, "to_passage": p2["id"], "text": "Jump"},
    ).json()
    g = c.get(f"/api/stories/{s['id']}/graph").json()
    assert [p["title"] for p in g["passages"]] == ["Start", "Pit"]
    assert g["choices"][0]["text"] == "Jump" and "effects" not in g["choices"][0] and "variables" not in g
    assert g["passages"][0]["meta"] == {} and g["passages"][0]["provenance"] == "human"

    # deleting the target dangles the choice rather than removing it
    assert c.delete(f"/api/stories/{s['id']}/passages/{p2['id']}").status_code == 204
    g = c.get(f"/api/stories/{s['id']}/graph").json()
    assert g["choices"][0]["id"] == ch["id"] and g["choices"][0]["to_passage"] is None

    assert c.delete(f"/api/stories/{s['id']}").status_code == 204
    assert c.get(f"/api/stories/{s['id']}/graph").status_code == 404


def test_story_patch_and_start(client):
    c = client
    s = c.post("/api/stories", json={"title": "A"}).json()
    sid = s["id"]
    p2 = c.post(f"/api/stories/{sid}/passages", json={"title": "Two"}).json()

    r = c.patch(f"/api/stories/{sid}", json={"title": "B", "start_passage_id": p2["id"]})
    assert r.status_code == 200 and r.json()["start_passage_id"] == p2["id"]

    other = c.post("/api/stories", json={"title": "Other"}).json()
    r = c.patch(f"/api/stories/{sid}", json={"start_passage_id": other["start_passage_id"]})
    assert r.status_code == 422

    # deleting the start passage clears the pointer
    c.delete(f"/api/stories/{sid}/passages/{p2['id']}")
    assert c.get(f"/api/stories/{sid}/graph").json()["story"]["start_passage_id"] is None


def test_updated_at_bumps_on_child_write(client):
    c = client
    s = c.post("/api/stories", json={"title": "A"}).json()
    sid = s["id"]
    with db.transaction() as conn:
        conn.execute("UPDATE stories SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (sid,))
    c.post(f"/api/stories/{sid}/lore", json={"kind": "place", "name": "Cave"})
    stories = c.get("/api/stories").json()
    assert stories[0]["id"] == sid and stories[0]["updated_at"] != "2000-01-01 00:00:00"


def test_passage_patch_and_duplicate(client):
    c = client
    s = c.post("/api/stories", json={"title": "A"}).json()
    sid, start = s["id"], s["start_passage_id"]
    p2 = c.post(f"/api/stories/{sid}/passages", json={"title": "Two"}).json()
    c.post(f"/api/stories/{sid}/choices", json={"from_passage": start, "to_passage": p2["id"], "text": "go"})

    r = c.patch(
        f"/api/stories/{sid}/passages/{start}",
        json={"body": "hello", "t": 1.5, "meta": {"location": "cave"}, "reviewed": True, "pos_x": 10},
    )
    assert r.status_code == 200
    p = r.json()
    assert (p["body"], p["t"], p["meta"], p["reviewed"], p["pos_x"]) == ("hello", 1.5, {"location": "cave"}, True, 10)
    assert c.patch(f"/api/stories/{sid}/passages/9999", json={"body": "x"}).status_code == 404

    dup = c.post(f"/api/stories/{sid}/passages/{start}/duplicate").json()
    assert dup["title"] == "Start (copy)" and dup["body"] == "hello" and dup["pos_x"] == 50
    g = c.get(f"/api/stories/{sid}/graph").json()
    assert sorted(ch["from_passage"] for ch in g["choices"]) == sorted([start, dup["id"]])


def test_choice_patch_validates_target(client):
    c = client
    s = c.post("/api/stories", json={"title": "A"}).json()
    sid, start = s["id"], s["start_passage_id"]
    ch = c.post(f"/api/stories/{sid}/choices", json={"from_passage": start, "text": "go"}).json()
    assert ch["to_passage"] is None

    other = c.post("/api/stories", json={"title": "Other"}).json()
    r = c.patch(f"/api/stories/{sid}/choices/{ch['id']}", json={"to_passage": other["start_passage_id"]})
    assert r.status_code == 422
    r = c.patch(f"/api/stories/{sid}/choices/{ch['id']}", json={"to_passage": start, "text": "go back"})
    assert r.json()["to_passage"] == start and r.json()["text"] == "go back"
    assert c.delete(f"/api/stories/{sid}/choices/{ch['id']}").status_code == 204
    assert c.delete(f"/api/stories/{sid}/choices/{ch['id']}").status_code == 404


def test_lore(client):
    c = client
    s = c.post("/api/stories", json={"title": "A"}).json()
    sid = s["id"]

    lore = c.post(f"/api/stories/{sid}/lore", json={"kind": "character", "name": "Ada", "body": "engineer"}).json()
    c.patch(f"/api/stories/{sid}/lore/{lore['id']}", json={"reviewed": True})

    g = c.get(f"/api/stories/{sid}/graph").json()
    assert g["lore"][0]["name"] == "Ada" and g["lore"][0]["reviewed"] is True
    assert "events" not in g
    assert c.post(f"/api/stories/{sid}/events", json={"t": 1, "name": "Dawn"}).status_code in (404, 405)
    assert c.delete(f"/api/stories/{sid}/lore/{lore['id']}").status_code == 204
    assert c.get(f"/api/stories/{sid}/graph").json()["lore"] == []


def test_play_and_problems(client):
    c = client
    s = c.post("/api/stories", json={"title": "Cave"}).json()
    sid, start = s["id"], s["start_passage_id"]
    base = f"/api/stories/{sid}"
    pit = c.post(f"{base}/passages", json={"title": "Pit"}).json()
    island = c.post(f"{base}/passages", json={"title": "Island"}).json()
    jump = c.post(f"{base}/choices", json={"from_passage": start, "to_passage": pit["id"], "text": "Jump"}).json()
    fly = c.post(f"{base}/choices", json={"from_passage": start, "to_passage": pit["id"], "text": "Fly", "sort_order": 1}).json()
    dangle = c.post(f"{base}/choices", json={"from_passage": pit["id"], "text": "Dig", "sort_order": 1}).json()

    v = c.post(f"{base}/play/start", json={}).json()
    assert v["passage"]["id"] == start and "state" not in v
    assert [ch["id"] for ch in v["choices"]] == [jump["id"], fly["id"]]

    v = c.post(f"{base}/play/choose", json={"choice_id": jump["id"]}).json()
    assert v["passage"]["id"] == pit["id"]
    assert c.post(f"{base}/play/choose", json={"choice_id": dangle["id"]}).status_code == 409
    assert c.post(f"{base}/play/choose", json={"choice_id": 99999}).status_code == 404

    # play from here
    v = c.post(f"{base}/play/start", json={"passage_id": island["id"]}).json()
    assert v["passage"]["title"] == "Island" and v["choices"] == []

    p = c.get(f"{base}/problems").json()
    assert p == {
        "no_start": False,
        "unreachable": [island["id"]],
        "dangling": [dangle["id"]],
        "endings": [],
        "unfinished": [island["id"]],
        "merge_candidates": [],
    }

    c.delete(f"{base}/passages/{start}")
    assert c.post(f"{base}/play/start", json={}).status_code == 409
    assert c.get(f"{base}/problems").json()["no_start"] is True


def test_is_ending_overrides_choices_at_play(client):
    """A decided ending shows no choices at play even if some are still attached
    to the row (e.g. left over from before it was marked); problems() and the
    edit_passage proposal shape agree with that."""
    c = client
    s = c.post("/api/stories", json={"title": "Grave"}).json()
    base, start = f"/api/stories/{s['id']}", s["start_passage_id"]
    stub = c.post(f"{base}/passages", json={"title": "Stub"}).json()
    c.post(f"{base}/choices", json={"from_passage": start, "to_passage": stub["id"], "text": "Go"})
    c.post(f"{base}/choices", json={"from_passage": stub["id"], "to_passage": start, "text": "Back"})

    p = c.get(f"{base}/problems").json()
    assert p["endings"] == [] and p["unfinished"] == []  # Stub still has a choice, isn't a dead end

    assert c.patch(f"{base}/passages/{stub['id']}", json={"is_ending": True}).json()["is_ending"] is True
    p = c.get(f"{base}/problems").json()
    assert p["endings"] == [stub["id"]] and p["unfinished"] == []

    v = c.post(f"{base}/play/choose", json={"choice_id": c.get(f"{base}/graph").json()["choices"][0]["id"]}).json()
    assert v["passage"]["id"] == stub["id"] and v["choices"] == []  # the stored "Back" choice is ignored


def _chain(c, base, start, titles, **passage):
    """start -> t1 -> t2 ... ; returns the passage ids in order."""
    ids, prev = [], start
    for title in titles:
        p = c.post(f"{base}/passages", json={"title": title, **passage}).json()
        c.post(f"{base}/choices", json={"from_passage": prev, "to_passage": p["id"], "text": f"to {title}"})
        ids.append(p["id"])
        prev = p["id"]
    return ids


def test_promote_subtree_and_explicit(client):
    c = client
    s = c.post("/api/stories", json={"title": "Fork"}).json()
    sid, start = s["id"], s["start_passage_id"]
    base = f"/api/stories/{sid}"
    left = _chain(c, base, start, ["L1", "L2"])
    right = _chain(c, base, start, ["R1"])
    # L2 also links to R1: R1 is reachable around L1, so it is not L1's subtree
    c.post(f"{base}/choices", json={"from_passage": left[1], "to_passage": right[0], "text": "cross"})

    r = c.post(f"{base}/promote", json={"root": left[0]}).json()
    assert r["passage_ids"] == sorted(left)
    reviewed = {p["id"]: p["reviewed"] for p in c.get(f"{base}/graph").json()["passages"]}
    assert reviewed == {start: False, left[0]: True, left[1]: True, right[0]: False}

    r = c.post(f"{base}/promote", json={"passage_ids": [right[0], 9999], "reviewed": True}).json()
    assert r["passage_ids"] == [right[0]]
    r = c.post(f"{base}/promote", json={"root": start, "reviewed": False}).json()
    assert r["passage_ids"] == sorted([start, *left, *right])
    assert not any(p["reviewed"] for p in c.get(f"{base}/graph").json()["passages"])


def test_layout_writes_positions_in_bulk(client):
    c = client
    s = c.post("/api/stories", json={"title": "Layout"}).json()
    sid, start = s["id"], s["start_passage_id"]
    base = f"/api/stories/{sid}"
    o = c.post("/api/stories", json={"title": "Other"}).json()
    other = o["start_passage_id"]
    (a,) = _chain(c, base, start, ["A"])
    r = c.post(f"{base}/layout", json={"positions": [
        {"id": start, "pos_x": 10, "pos_y": 20}, {"id": a, "pos_x": -5.5, "pos_y": 200},
        {"id": other, "pos_x": 1, "pos_y": 1}, {"id": 9999, "pos_x": 1, "pos_y": 1}]}).json()
    assert r == {"passage_ids": sorted([start, a])}
    pos = {p["id"]: (p["pos_x"], p["pos_y"]) for p in c.get(f"{base}/graph").json()["passages"]}
    assert pos == {start: (10, 20), a: (-5.5, 200)}
    theirs = c.get(f"/api/stories/{o['id']}/graph").json()["passages"]
    assert [(p["pos_x"], p["pos_y"]) for p in theirs] == [(0, 0)]  # another story's passage is left alone
    assert c.post("/api/stories/9999/layout", json={"positions": []}).status_code == 404


def test_prune_deletes_choices_and_subtree(client):
    c = client
    s = c.post("/api/stories", json={"title": "Prune"}).json()
    sid, start = s["id"], s["start_passage_id"]
    base = f"/api/stories/{sid}"
    left = _chain(c, base, start, ["L1", "L2", "L3"])
    right = _chain(c, base, start, ["R1"])
    # L2 also links to R1: R1 is reachable around L1, so pruning L1 keeps it
    cross = c.post(f"{base}/choices", json={"from_passage": left[1], "to_passage": right[0], "text": "cross"}).json()

    r = c.post(f"{base}/passages/{left[0]}/prune").json()
    assert r["passage_ids"] == sorted(left[1:])
    g = c.get(f"{base}/graph").json()
    assert {p["id"] for p in g["passages"]} == {start, left[0], right[0]}
    assert cross["id"] in r["choice_ids"]
    assert not any(ch["from_passage"] == left[0] for ch in g["choices"])
    assert [ch["to_passage"] for ch in g["choices"]] == [left[0], right[0]]

    r = c.post(f"{base}/passages/{left[0]}/prune").json()
    assert r == {"passage_ids": [], "choice_ids": []}
    assert c.post(f"{base}/passages/9999/prune").status_code == 404


def test_prune_runs_to_the_leaves_past_orphans(client):
    """An orphan elsewhere that links into the branch must not shield it (the
    old collapse rule did), and a passage the start reaches another way stays."""
    c = client
    s = c.post("/api/stories", json={"title": "Prune deep"}).json()
    sid, start = s["id"], s["start_passage_id"]
    base = f"/api/stories/{sid}"
    a = _chain(c, base, start, ["A1", "A2", "A3", "A4"])
    b = _chain(c, base, start, ["B1"])
    orphan = c.post(f"{base}/passages", json={"title": "Orphan"}).json()["id"]
    c.post(f"{base}/choices", json={"from_passage": orphan, "to_passage": a[2], "text": "in"})
    c.post(f"{base}/choices", json={"from_passage": a[1], "to_passage": b[0], "text": "rejoin"})

    preview = c.get(f"{base}/passages/{a[0]}/prune").json()
    assert preview["passage_ids"] == sorted(a[1:])
    assert len(c.get(f"{base}/graph").json()["passages"]) == 7  # the preview deletes nothing
    assert c.post(f"{base}/passages/{a[0]}/prune").json() == preview
    left = {p["id"] for p in c.get(f"{base}/graph").json()["passages"]}
    assert left == {start, a[0], b[0], orphan}
    assert c.get(f"{base}/passages/9999/prune").status_code == 404


def test_merge_candidates_and_merge(client):
    c = client
    s = c.post("/api/stories", json={"title": "Dup"}).json()
    sid, start = s["id"], s["start_passage_id"]
    base = f"/api/stories/{sid}"
    scene = {"t": 3, "meta": {"location": "The Pit"}}
    a = c.post(f"{base}/passages", json={"title": "Pit A", **scene}).json()
    b = c.post(f"{base}/passages", json={"title": "Pit B", "t": 3, "meta": {"location": "the pit "}}).json()
    other = c.post(f"{base}/passages", json={"title": "Elsewhere", "t": 3, "meta": {"location": "Hall"}}).json()
    c.post(f"{base}/choices", json={"from_passage": start, "to_passage": a["id"], "text": "left"})
    c.post(f"{base}/choices", json={"from_passage": start, "to_passage": b["id"], "text": "right"})
    down = c.post(f"{base}/choices", json={"from_passage": b["id"], "to_passage": other["id"], "text": "down"}).json()
    c.post(f"{base}/choices", json={"from_passage": b["id"], "to_passage": a["id"], "text": "loop"})

    p = c.get(f"{base}/problems").json()
    assert p["merge_candidates"] == [{"t": 3, "location": "the pit", "passage_ids": sorted([a["id"], b["id"]])}]

    assert c.post(f"{base}/passages/merge", json={"into": a["id"], "passage_ids": [a["id"]]}).status_code == 422
    assert c.post(f"{base}/passages/merge", json={"into": a["id"], "passage_ids": [9999]}).status_code == 404
    survivor = c.post(f"{base}/passages/merge", json={"into": a["id"], "passage_ids": [b["id"]]}).json()
    assert survivor["id"] == a["id"]
    g = c.get(f"{base}/graph").json()
    assert sorted(p["id"] for p in g["passages"]) == sorted([start, a["id"], other["id"]])
    links = sorted((ch["from_passage"], ch["to_passage"], ch["text"]) for ch in g["choices"])
    # both inbound choices now point at A, "down" moved to A, the loop B->A became a self-loop and was dropped
    assert links == sorted([(start, a["id"], "left"), (start, a["id"], "right"), (a["id"], other["id"], "down")])
    assert down["id"] in {ch["id"] for ch in g["choices"]}
    assert c.get(f"{base}/problems").json()["merge_candidates"] == []


def test_playtest_and_twee_export(client):
    c = client
    s = c.post("/api/stories", json={"title": "Cave [1]"}).json()
    sid, start = s["id"], s["start_passage_id"]
    base = f"/api/stories/{sid}"
    pit = c.post(f"{base}/passages", json={"title": "Pit", "body": "Dark, and *very* deep.", "reviewed": True, "pos_x": 10.5, "pos_y": 20}).json()
    pit2 = c.post(f"{base}/passages", json={"title": "Pit", "provenance": "agent"}).json()
    assert pit2["title"] == "Pit (2)"  # creation keeps names unique
    with db.transaction() as conn:  # data from before that rule: the export still de-duplicates
        conn.execute("UPDATE passages SET title = 'Pit' WHERE id = ?", (pit2["id"],))
    orphan = c.post(f"{base}/passages", json={"title": "Sky"}).json()
    c.post(f"{base}/choices", json={"from_passage": start, "to_passage": pit["id"], "text": "Jump"})
    c.post(f"{base}/choices", json={"from_passage": pit["id"], "to_passage": pit2["id"], "text": "Deeper"})
    dangle = c.post(f"{base}/choices", json={"from_passage": pit2["id"], "text": "Dig"}).json()

    r = c.post(f"{base}/playtest", json={"runs": 50, "seed": 1}).json()
    assert r["runs"] == 50 and r["unvisited"] == [orphan["id"]] and r["cut"] == 0
    # JSON objects key by string, so the counters come back keyed by str(id)
    assert set(r["visited"]) == {str(start), str(pit["id"]), str(pit2["id"])} and r["visited"][str(start)] == 50
    assert r["endings"] == {} and r["dangling"] == {str(dangle["id"]): 50}
    assert r["mean_length"] == 2.0
    assert c.post(f"{base}/playtest", json={"runs": 0}).status_code == 422

    res = c.get(f"{base}/export.twee")
    assert res.status_code == 200 and res.headers["content-disposition"].endswith('filename="Cave 1.twee"')
    text = res.text
    assert text.startswith(":: StoryTitle\nCave [1]\n")
    assert f'"ifid": "{s["ifid"].upper()}"' in text and '"start": "Start"' in text
    assert "StoryInit" not in text and "<<" not in text  # no variables, so no macros
    assert ':: Pit [reviewed] {"position":"10,20"}\nDark, and //very// deep.\n[[Deeper->Pit (%d)]]\n' % pit2["id"] in text
    assert ":: Pit (%d) [agent]" % pit2["id"] in text and "/* dangling: Dig */" in text
    assert "[[Jump->Pit]]" in text

    c.patch(f"{base}", json={"start_passage_id": None})
    assert c.post(f"{base}/playtest", json={}).status_code == 409


def test_passage_titles_are_unique(client):
    s = client.post("/api/stories", json={"title": "Cave"}).json()
    base = f"/api/stories/{s['id']}"
    a = client.post(f"{base}/passages", json={"title": "New passage"}).json()
    b = client.post(f"{base}/passages", json={"title": "new passage"}).json()  # case-insensitive
    c3 = client.post(f"{base}/passages", json={"title": "New passage"}).json()
    assert [a["title"], b["title"], c3["title"]] == ["New passage", "new passage (2)", "New passage (3)"]
    # a rename to a taken name is refused; keeping its own name, or another case of it, is fine
    assert client.patch(f"{base}/passages/{b['id']}", json={"title": "NEW PASSAGE"}).status_code == 409
    assert client.patch(f"{base}/passages/{b['id']}", json={"title": "new passage (2)"}).status_code == 200
    assert client.patch(f"{base}/passages/{a['id']}", json={"title": "NEW passage"}).status_code == 200
    dup = client.post(f"{base}/passages/{a['id']}/duplicate").json()
    assert dup["title"] == "NEW passage (copy)"


def test_settings_model_and_concurrency(client, monkeypatch):
    from cyoa import agents, llm
    monkeypatch.setattr(agents, "_GATE", agents.Gate(1))
    info = client.get("/api/settings").json()
    assert info["saved"] == {} and info["concurrency"] == 1 and info["model"] == info["env_model"] == llm.DEFAULT_MODEL
    # saving applies at once: the gate resizes, and a call that names no model gets the saved one
    r = client.put("/api/settings", json={"model": "salad-chat", "concurrency": 4})
    assert r.status_code == 200 and r.json()["saved"] == {"model": "salad-chat", "concurrency": 4}
    assert agents._GATE.limit == 4 and r.json()["model"] == "salad-chat"
    assert llm.chat_model().model_name == "salad-chat" if llm.BACKEND == "openai" else True
    assert llm.chat_model("explicit").model_name == "explicit" if llm.BACKEND == "openai" else True
    # a field left out is unchanged; null clears one and falls back to the launch configuration
    assert client.put("/api/settings", json={"concurrency": 2}).json()["saved"] == {"model": "salad-chat", "concurrency": 2}
    cleared = client.put("/api/settings", json={"model": None, "concurrency": None}).json()
    assert cleared["saved"] == {} and cleared["model"] == llm.DEFAULT_MODEL and cleared["concurrency"] == agents.ENV_CONCURRENCY
    assert client.put("/api/settings", json={"concurrency": 0}).status_code == 422
    assert client.put("/api/settings", json={"colour": "red"}).status_code == 422
    assert client.put("/api/settings", json={"model": "  "}).json()["saved"] == {}


def test_gate_resizes_while_calls_wait():
    from cyoa import agents
    gate = agents.Gate(1)
    assert gate.acquire(0.1) and not gate.acquire(0.05)  # full: a second call waits, and times out
    gate.resize(2)
    assert gate.acquire(0.1)  # raised: it gets through
    gate.resize(1)  # lowered below the two in flight: nothing is cut, new calls hold
    assert not gate.acquire(0.05)
    gate.release(); gate.release()
    assert gate.acquire(0.1)
