"""导入与解析集成测试。"""

from pathlib import Path

from fastapi.testclient import TestClient


def _create_project(client: TestClient) -> str:
    r = client.post("/api/v1/projects", json={"name": "导入测试"})
    assert r.status_code == 201
    return r.json()["id"]


def test_import_txt_parse_and_report(client: TestClient, tmp_path: Path) -> None:
    pid = _create_project(client)
    script = """第一集 初遇
【咖啡厅·日内】
林晓：你好。
王强：你是谁？

（两人对视）
"""
    f = tmp_path / "script.txt"
    f.write_text(script, encoding="utf-8")

    r = client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("script.txt", f.read_bytes(), "text/plain")},
    )
    assert r.status_code == 201
    doc = r.json()
    assert doc["version"] == 1

    r = client.post(f"/api/v1/projects/{pid}/parse")
    assert r.status_code == 200
    assert r.json()["coverage_ok"] is True

    r = client.get(f"/api/v1/projects/{pid}/parse-report")
    report = r.json()
    assert report["coverage_ok"] is True
    assert report["dialogue_count"] == 2
    assert report["chapter_count"] >= 1

    segs = client.get(f"/api/v1/projects/{pid}/segments").json()
    assert segs
    assert {s["segment_type"] for s in segs} >= {"heading", "dialogue", "narrative"}


def test_import_rejected_format(client: TestClient, tmp_path: Path) -> None:
    pid = _create_project(client)
    f = tmp_path / "script.pdf"
    f.write_bytes(b"%PDF")
    r = client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("script.pdf", f.read_bytes(), "application/pdf")},
    )
    assert r.status_code == 400


def test_parse_without_import_404(client: TestClient) -> None:
    pid = _create_project(client)
    assert client.post(f"/api/v1/projects/{pid}/parse").status_code == 404
