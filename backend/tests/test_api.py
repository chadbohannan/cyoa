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
    }

    c.delete(f"{base}/passages/{start}")
    assert c.post(f"{base}/play/start", json={}).status_code == 409
    assert c.get(f"{base}/problems").json()["no_start"] is True
