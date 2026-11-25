"""项目 CRUD 与本地安全冒烟测试。"""

from fastapi.testclient import TestClient


def test_crud_roundtrip(client: TestClient, wait_task) -> None:
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


def test_health_is_public(client: TestClient, wait_task) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_reparse_maps_scenes_per_chapter(client, wait_task) -> None:
    """场景号每章重置：第 2 章第 1 场不能被映射到第 1 章第 1 场。"""
    from sqlalchemy import select

    import app.core.database as dbmod
    from app.models.document import Chapter, Scene, SourceSegment

    text = (
        "第1集\n"
        "1-1 洗车行 白天 外\n"
        "△第一场动作。\n"
        "Chloe：第一场台词。\n"
        "1-2 车内 白天 内\n"
        "△第二场动作。\n"
        "第2集\n"
        "2-1 办公室 白天 内\n"
        "△第三场动作。\n"
        "2-2 走廊 夜晚 内\n"
        "△第四场动作。\n"
    )
    pid = client.post("/api/v1/projects", json={"name": "跨章场景"}).json()["id"]
    assert client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("scenes.txt", text.encode(), "text/plain")},
    ).status_code == 201
    tid = client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"]
    assert wait_task(client, tid)["status"] == "completed"
    report = client.get(f"/api/v1/projects/{pid}/parse-report").json()
    assert report["scene_count"] == 4

    with dbmod.SessionLocal() as db:
        rows = db.execute(
            select(SourceSegment, Scene, Chapter)
            .join(Scene, SourceSegment.scene_id == Scene.id)
            .join(Chapter, Scene.chapter_id == Chapter.id)
            .where(SourceSegment.project_id == pid)
        ).all()
        assert len({scene.id for _seg, scene, _ch in rows}) == 4
        expected = {
            "1-1 洗车行 白天 外": (1, 1),
            "第一场动作": (1, 1),
            "第一场台词": (1, 1),
            "第二场动作": (1, 2),
            "第三场动作": (2, 1),
            "第四场动作": (2, 2),
        }
        for needle, (chapter_no, scene_no) in expected.items():
            matches = [
                (seg, scene, chapter)
                for seg, scene, chapter in rows
                if needle in seg.source_text
            ]
            assert matches, f"未找到片段：{needle}"
            for _seg, scene, chapter in matches:
                assert (chapter.chapter_no, scene.scene_no) == (chapter_no, scene_no)
        headings = {
            (chapter.chapter_no, scene.scene_no): scene.heading
            for _seg, scene, chapter in rows
        }
        assert headings[(1, 1)] == "1-1 洗车行 白天 外"
        assert headings[(1, 2)] == "1-2 车内 白天 内"
        assert headings[(2, 1)] == "2-1 办公室 白天 内"
        assert headings[(2, 2)] == "2-2 走廊 夜晚 内"
