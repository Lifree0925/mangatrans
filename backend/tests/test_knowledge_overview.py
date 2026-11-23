"""知识库总览与整体硬删除测试。"""

from sqlalchemy import select

import app.core.database as dbmod
from app.models.dialogue import DialogueLine
from app.models.document import Chapter, SourceSegment
from app.models.knowledge import KnowledgeItem
from app.models.project import Project
from app.models.storyline import ChapterStoryline
from app.models.translation import Translation


def _setup(client, wait_task) -> tuple[str, str, str]:
    pid = client.post("/api/v1/projects", json={"name": "知识库总览测试"}).json()["id"]
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("猎魔人.txt", "第1集\n1-1 森林 夜外\nElara：你好。\n".encode(), "text/plain")},
    )
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    with dbmod.SessionLocal() as db:
        ch = db.scalar(select(Chapter).where(Chapter.project_id == pid))
        seg = db.scalar(select(SourceSegment).where(SourceSegment.project_id == pid))
        db.add_all([
            KnowledgeItem(project_id=pid, item_type="character", canonical_key="Elara",
                          data_json="{}", status="approved"),
            KnowledgeItem(project_id=pid, item_type="term", canonical_key="狼王",
                          data_json="{}", status="approved"),
            ChapterStoryline(project_id=pid, chapter_id=ch.id, card_json="{}"),
            DialogueLine(project_id=pid, chapter_id=ch.id, segment_id=seg.id,
                         speaker_name="Elara", source_text="你好。", line_no=1),
        ])
        db.add(Translation(segment_id=seg.id, version=1, stage="polished",
                           target_text="Hello.", review_status="approved",
                           freshness_status="current", source_hash="h"))
        db.commit()
        return pid, ch.id, seg.id


def test_knowledge_overview_lists_project_with_stats(client, wait_task):
    pid, _ch, _seg = _setup(client, wait_task)
    r = client.get("/api/v1/knowledge/overview")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    item = next(i for i in items if i["project_id"] == pid)
    assert item["project_name"] == "知识库总览测试"
    assert item["filename"] == "猎魔人.txt"
    assert item["characters"] == 1
    assert item["terms"] == 1
    assert item["storylines"] == 1
    assert item["dialogue_lines"] == 1


def test_hard_delete_clears_knowledge_but_keeps_project_and_translation(client, wait_task):
    pid, _ch, seg_id = _setup(client, wait_task)
    r = client.delete(f"/api/v1/projects/{pid}/knowledge")
    assert r.status_code == 204, r.text

    with dbmod.SessionLocal() as db:
        assert db.scalar(select(KnowledgeItem).where(KnowledgeItem.project_id == pid)) is None
        assert db.scalar(select(ChapterStoryline).where(ChapterStoryline.project_id == pid)) is None
        assert db.scalar(select(DialogueLine).where(DialogueLine.project_id == pid)) is None
        # 项目与译文保留
        assert db.get(Project, pid) is not None
        assert db.scalar(select(Translation).where(Translation.segment_id == seg_id)) is not None

    r = client.get("/api/v1/knowledge/overview")
    assert all(i["project_id"] != pid for i in r.json()["items"])
