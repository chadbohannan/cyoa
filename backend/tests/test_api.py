
from fastapi.testclient import TestClient

from cyoa import db
from cyoa.main import app


def test_health_and_story_roundtrip(tmp_path):
    db.DB_PATH = tmp_path / "t.db"
    with TestClient(app) as c:
        assert c.get("/api/health").json()["ok"] is True

        s = c.post("/api/stories", json={"title": "Cave"}).json()
        p1 = c.post(f"/api/stories/{s['id']}/passages", json={"title": "Entrance", "is_start": True}).json()
        p2 = c.post(f"/api/stories/{s['id']}/passages", json={"title": "Pit"}).json()
        c.post(
            f"/api/stories/{s['id']}/choices",
            json={"from_passage": p1["id"], "to_passage": p2["id"], "text": "Jump", "effects": {"set_flag": "brave"}},
        )
        g = c.get(f"/api/stories/{s['id']}/graph").json()
        assert [p["title"] for p in g["passages"]] == ["Entrance", "Pit"]
        assert g["choices"][0]["effects"] == {"set_flag": "brave"}

        assert c.delete(f"/api/stories/{s['id']}").status_code == 204
        assert c.get(f"/api/stories/{s['id']}/graph").status_code == 404
