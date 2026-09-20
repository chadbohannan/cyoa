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
        json={"from_passage": start, "to_passage": p2["id"], "text": "Jump", "effects": {"set": {"brave": True}}},
    ).json()
    g = c.get(f"/api/stories/{s['id']}/graph").json()
    assert [p["title"] for p in g["passages"]] == ["Start", "Pit"]
    assert g["choices"][0]["effects"] == {"set": {"brave": True}}
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
    c.post(f"/api/stories/{sid}/variables", json={"name": "gold", "kind": "stat", "default": 5})
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
    r = c.patch(
        f"/api/stories/{sid}/choices/{ch['id']}",
        json={"to_passage": start, "condition": {"var": "brave", "op": "==", "value": True}},
    )
    assert r.json()["to_passage"] == start and r.json()["condition"]["var"] == "brave"
    assert c.delete(f"/api/stories/{sid}/choices/{ch['id']}").status_code == 204
    assert c.delete(f"/api/stories/{sid}/choices/{ch['id']}").status_code == 404


def test_variables_lore_events(client):
    c = client
    s = c.post("/api/stories", json={"title": "A"}).json()
    sid = s["id"]

    v = c.post(f"/api/stories/{sid}/variables", json={"name": "brave", "kind": "flag", "default": False}).json()
    assert v["default"] is False
    assert c.post(f"/api/stories/{sid}/variables", json={"name": "brave", "kind": "flag"}).status_code == 409
    assert c.post(f"/api/stories/{sid}/variables", json={"name": "x", "kind": "bogus"}).status_code == 422
    v = c.patch(f"/api/stories/{sid}/variables/{v['id']}", json={"default": None}).json()
    assert v["default"] is None

    lore = c.post(f"/api/stories/{sid}/lore", json={"kind": "character", "name": "Ada", "body": "engineer"}).json()
    ev = c.post(f"/api/stories/{sid}/events", json={"t": 2, "name": "Flood"}).json()
    c.post(f"/api/stories/{sid}/events", json={"t": 1, "name": "Dawn"})
    c.patch(f"/api/stories/{sid}/lore/{lore['id']}", json={"reviewed": True})

    g = c.get(f"/api/stories/{sid}/graph").json()
    assert g["lore"][0]["name"] == "Ada" and g["lore"][0]["reviewed"] is True
    assert [e["name"] for e in g["events"]] == ["Dawn", "Flood"]
    assert c.delete(f"/api/stories/{sid}/events/{ev['id']}").status_code == 204
    assert c.delete(f"/api/stories/{sid}/lore/{lore['id']}").status_code == 204
    assert c.get(f"/api/stories/{sid}/graph").json()["lore"] == []


def test_play_and_problems(client):
    c = client
    s = c.post("/api/stories", json={"title": "Cave"}).json()
    sid, start = s["id"], s["start_passage_id"]
    base = f"/api/stories/{sid}"
    c.post(f"{base}/variables", json={"name": "brave", "kind": "flag", "default": False})
    c.post(f"{base}/variables", json={"name": "gold", "kind": "stat", "default": 3})
    pit = c.post(f"{base}/passages", json={"title": "Pit"}).json()
    island = c.post(f"{base}/passages", json={"title": "Island"}).json()
    jump = c.post(f"{base}/choices", json={"from_passage": start, "to_passage": pit["id"], "text": "Jump",
                                          "effects": {"set": {"brave": True}, "add": {"gold": -1}}}).json()
    fly = c.post(f"{base}/choices", json={"from_passage": start, "to_passage": pit["id"], "text": "Fly",
                                         "condition": {"var": "brave", "op": "==", "value": True}}).json()
    dangle = c.post(f"{base}/choices", json={"from_passage": pit["id"], "text": "Dig", "sort_order": 1,
                                            "effects": {"give": ["shovel"]}}).json()

    v = c.post(f"{base}/play/start", json={}).json()
    assert v["passage"]["id"] == start and v["state"] == {"brave": False, "gold": 3}
    assert [ch["id"] for ch in v["choices"]] == [jump["id"]]  # Fly is hidden by its condition

    # the server re-checks the condition itself
    assert c.post(f"{base}/play/choose", json={"choice_id": fly["id"], "state": v["state"]}).status_code == 409
    v = c.post(f"{base}/play/choose", json={"choice_id": jump["id"], "state": v["state"]}).json()
    assert v["passage"]["id"] == pit["id"] and v["state"] == {"brave": True, "gold": 2}
    assert c.post(f"{base}/play/choose", json={"choice_id": dangle["id"], "state": v["state"]}).status_code == 409

    # play from here
    v = c.post(f"{base}/play/start", json={"passage_id": island["id"]}).json()
    assert v["passage"]["title"] == "Island" and v["choices"] == []

    p = c.get(f"{base}/problems").json()
    assert p == {
        "no_start": False,
        "unreachable": [island["id"]],
        "dangling": [dangle["id"]],
        "undeclared": [{"choice_id": dangle["id"], "var": "shovel"}],
        "endings": [island["id"]],
        "merge_candidates": [],
    }

    c.delete(f"{base}/passages/{start}")
    assert c.post(f"{base}/play/start", json={}).status_code == 409
    assert c.get(f"{base}/problems").json()["no_start"] is True


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


def test_merge_candidates_and_merge(client):
    c = client
    s = c.post("/api/stories", json={"title": "Dup"}).json()
    sid, start = s["id"], s["start_passage_id"]
    base = f"/api/stories/{sid}"
    scene = {"t": 3, "meta": {"location": "The Pit", "events": [1]}}
    a = c.post(f"{base}/passages", json={"title": "Pit A", **scene}).json()
    b = c.post(f"{base}/passages", json={"title": "Pit B", "t": 3, "meta": {"location": "the pit ", "events": [1]}}).json()
    other = c.post(f"{base}/passages", json={"title": "Elsewhere", "t": 3, "meta": {"location": "Hall"}}).json()
    c.post(f"{base}/choices", json={"from_passage": start, "to_passage": a["id"], "text": "left"})
    c.post(f"{base}/choices", json={"from_passage": start, "to_passage": b["id"], "text": "right"})
    down = c.post(f"{base}/choices", json={"from_passage": b["id"], "to_passage": other["id"], "text": "down"}).json()
    c.post(f"{base}/choices", json={"from_passage": b["id"], "to_passage": a["id"], "text": "loop"})

    p = c.get(f"{base}/problems").json()
    assert p["merge_candidates"] == [{"t": 3, "location": "the pit", "events": ["1"], "passage_ids": sorted([a["id"], b["id"]])}]

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
    c.post(f"{base}/variables", json={"name": "brave", "kind": "flag", "default": False})
    pit = c.post(f"{base}/passages", json={"title": "Pit", "body": "Dark.", "reviewed": True, "pos_x": 10.5, "pos_y": 20}).json()
    pit2 = c.post(f"{base}/passages", json={"title": "Pit", "provenance": "agent"}).json()
    gated = c.post(f"{base}/passages", json={"title": "Sky"}).json()
    c.post(f"{base}/choices", json={"from_passage": start, "to_passage": pit["id"], "text": "Jump", "effects": {"set": {"brave": True}}})
    c.post(f"{base}/choices", json={"from_passage": start, "to_passage": gated["id"], "text": "Fly",
                                    "condition": {"var": "brave", "op": "==", "value": True}})
    c.post(f"{base}/choices", json={"from_passage": pit["id"], "to_passage": pit2["id"], "text": "Deeper"})
    dangle = c.post(f"{base}/choices", json={"from_passage": pit2["id"], "text": "Dig"}).json()

    r = c.post(f"{base}/playtest", json={"runs": 50, "seed": 1}).json()
    # brave is never true on the start passage, so Sky is unreachable in play and Fly never taken
    assert r["runs"] == 50 and r["unvisited"] == [gated["id"]] and r["cut"] == 0
    # JSON objects key by string, so the counters come back keyed by str(id)
    assert set(r["visited"]) == {str(start), str(pit["id"]), str(pit2["id"])} and r["visited"][str(start)] == 50
    assert r["endings"] == {} and r["stuck"] == {} and r["dangling"] == {str(dangle["id"]): 50}
    assert r["mean_length"] == 2.0
    assert c.post(f"{base}/playtest", json={"runs": 0}).status_code == 422

    res = c.get(f"{base}/export.twee")
    assert res.status_code == 200 and res.headers["content-disposition"].endswith('filename="Cave 1.twee"')
    text = res.text
    assert text.startswith(":: StoryTitle\nCave [1]\n")
    assert f'"ifid": "{s["ifid"].upper()}"' in text and '"start": "Start"' in text
    assert ":: StoryInit\n<<set $brave to false>>\n" in text
    assert ':: Pit [reviewed] {"position":"10,20"}\nDark.\n[[Deeper->Pit (%d)]]\n' % pit2["id"] in text
    assert ":: Pit (%d) [agent]" % pit2["id"] in text and "/* dangling: Dig */" in text
    assert '<<link "Jump" "Pit">><<set $brave to true>><</link>>' in text
    assert "<<if $brave is true>>[[Fly->Sky]]<</if>>" in text

    c.patch(f"{base}", json={"start_passage_id": None})
    assert c.post(f"{base}/playtest", json={}).status_code == 409
