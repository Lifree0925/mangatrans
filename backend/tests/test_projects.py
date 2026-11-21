"""项目 CRUD 与本地安全冒烟测试。"""

from fastapi.testclient import TestClient


def test_crud_roundtrip(client: TestClient) -> None:
    r = client.post("/api/v1/projects", json={"name": "测试项目"})
    assert r.status_code == 201
    pid = r.json()["id"]

    r = client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 200
    assert r.json()["name"] == "测试项目"
    assert r.json()["source_language"] == "zh-CN"

    r = client.patch(f"/api/v1/projects/{pid}", json={"name": "改名"})
    assert r.status_code == 200
    assert r.json()["name"] == "改名"

    r = client.get("/api/v1/projects")
    assert r.status_code == 200
    assert any(p["id"] == pid for p in r.json())

    r = client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 204
    assert client.get(f"/api/v1/projects/{pid}").status_code == 404


def test_unauthorized_without_token() -> None:
    from app.core import security
    from app.main import app

    security.set_token("secret-token")
    bare = TestClient(app)
    assert bare.get("/api/v1/projects").status_code == 401


def test_health_is_public(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_delete_project_with_dialogue_lines(client) -> None:
    """删除带台词本/剧情卡/审计的项目不因外键失败。"""
    import app.core.database as dbmod
    from app.models.dialogue import DialogueLine
    from app.models.project import Project

    pid = client.post("/api/v1/projects", json={"name": "待删除项目"}).json()["id"]
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", "林风：你好。\n".encode(), "text/plain")},
    )
    client.post(f"/api/v1/projects/{pid}/parse", json={})
    with dbmod.SessionLocal() as db:
        p = db.get(Project, pid)
        db.add(DialogueLine(project_id=pid, source_document_id=p.active_source_document_id, speaker_name="林风", source_text="你好。"))
        db.commit()
    assert client.delete(f"/api/v1/projects/{pid}").status_code == 204
    assert client.get(f"/api/v1/projects/{pid}").status_code == 404
