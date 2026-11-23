"""回归测试：术语匹配（词边界/大小写/变体）与译文原子插入（UNIQUE 冲突根除）。"""

import json

from sqlalchemy import select

import app.core.database as dbmod
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeItem
from app.models.translation import Translation
from app.services.qa_service import deterministic_qa
from app.services.term_match import case_drifted, target_hits_term
from app.services.translation_batch import _forced_terms_for_text, insert_translation_row


def test_target_hits_term_word_boundary():
    # werewolves 不再误判为缺 werewolf；连字符拼接（werewolf-hunter）也视为独立词命中
    assert target_hits_term("The werewolves attacked", "werewolf") == ""
    assert target_hits_term("A werewolf attacked", "werewolf") == "werewolf"
    assert target_hits_term("He is a werewolf-hunter", "werewolf") == "werewolf"
    assert target_hits_term("the werewolf's den", "werewolf") == "werewolf"


def test_target_hits_term_case_insensitive():
    assert target_hits_term("the wolf king spoke", "Wolf King") == "Wolf King"
    assert target_hits_term("Wolf King spoke", "Wolf King") == "Wolf King"


def test_target_hits_term_variants():
    assert target_hits_term("two werewolves", "werewolf", ["werewolves"]) == "werewolves"
    assert target_hits_term("handcuffs clicked shut", "handcuff", ["handcuffs", "Handcuffs"]) != ""


def test_case_drifted():
    assert case_drifted("the wolf king spoke", "Wolf King") is True
    assert case_drifted("Wolf King spoke", "Wolf King") is False
    assert case_drifted("the Wolf King spoke", "Wolf King") is False


def test_cjk_term_substring():
    assert target_hits_term("狼王走进大厅", "狼王") == "狼王"


class _FakeTr:
    target_text = ""


def _setup_project(client, wait_task) -> tuple[str, str]:
    pid = client.post("/api/v1/projects", json={"name": "术语测试"}).json()["id"]
    client.post(f"/api/v1/projects/{pid}/documents/import",
                files={"file": ("s.txt", "狼人冲了过来。\n".encode(), "text/plain")})
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    segs = client.get(f"/api/v1/projects/{pid}/segments").json()
    return pid, segs[0]["id"]


def test_deterministic_qa_plural_not_false_positive(client, wait_task):
    pid, sid = _setup_project(client, wait_task)
    with dbmod.SessionLocal() as db:
        seg = db.get(SourceSegment, sid)
        db.add(KnowledgeItem(id="k1", project_id=pid, item_type="term",
                             canonical_key="狼人", status="approved",
                             data_json=json.dumps({"translation": "werewolf",
                                                   "variants": ["werewolves"]})))
        db.commit()
        forced = _forced_terms_for_text(db, pid, "狼人冲了过来")
        assert forced["狼人"]["en"] == "werewolf"
        assert forced["狼人"]["variants"] == ["werewolves"]

        tr = _FakeTr()
        tr.target_text = "The werewolves charged forward"
        issues = deterministic_qa(db, seg, tr, forced)
        # 复数变体命中：不再误报 high forced_term（历史 70% 误报根因）
        assert not any(i["issue_type"] == "forced_term" for i in issues)

        tr.target_text = "The werefox charged forward"  # 完全没命中
        issues = deterministic_qa(db, seg, tr, forced)
        assert any(i["issue_type"] == "forced_term" and i["severity"] == "high"
                   for i in issues)

        db.add(KnowledgeItem(id="k2", project_id=pid, item_type="term",
                             canonical_key="狼王", status="approved",
                             data_json=json.dumps({"translation": "Wolf King"})))
        seg2 = SourceSegment(id="s2", project_id=pid, source_document_id=seg.source_document_id,
                             segment_no=2, segment_type="narrative", source_text="狼王来了",
                             source_start=0, source_end=5, source_hash="x2")
        db.add(seg2)
        db.commit()
        forced2 = _forced_terms_for_text(db, pid, "狼王来了")
        tr.target_text = "The wolf king charged forward"  # 大小写漂移：QA 不再检查（小问题）
        issues = deterministic_qa(db, seg2, tr, forced2)
        assert not any(i["issue_type"] == "term_case" for i in issues)


def test_insert_translation_row_atomic_and_ordered(client, wait_task):
    pid, sid = _setup_project(client, wait_task)
    with dbmod.SessionLocal() as db:
        t1 = insert_translation_row(db, segment_id=sid, stage="draft", target_text="A")
        t2 = insert_translation_row(db, segment_id=sid, stage="polished", target_text="B")
        assert t1.version == 1
        assert t2.version == 2
        rows = list(db.scalars(select(Translation).where(Translation.segment_id == sid)))
        assert sorted(r.version for r in rows) == [1, 2]


def test_insert_translation_row_concurrent_threads(client, wait_task):
    """多线程并发写同一段：不出现 UNIQUE(segment_id, version) 冲突，版本连续。"""
    import threading

    pid, sid = _setup_project(client, wait_task)
    errors: list[Exception] = []

    def write(n: int):
        try:
            s = dbmod.SessionLocal()
            insert_translation_row(s, segment_id=sid, stage="draft", target_text=f"T{n}")
            s.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发插入出现异常: {errors[0] if errors else ''}"
    with dbmod.SessionLocal() as db:
        rows = list(db.scalars(select(Translation).where(Translation.segment_id == sid)))
    versions = sorted(r.version for r in rows)
    assert versions == list(range(1, 9)), f"版本应为 1..8 连续，实际 {versions}"
