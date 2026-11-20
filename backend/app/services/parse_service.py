"""解析服务：执行解析并把结果持久化。"""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.document import Chapter, Scene, SourceDocument, SourceSegment
from app.parsers.script_parser import ParseResult, parse_script
from app.services.importer import sha256


def parse_and_persist(db: Session, doc: SourceDocument, text: str) -> ParseResult:
    """解析并写入章节/场景/片段；同文档版本重新解析时先清空旧结构与派生数据。

    依赖链清理顺序（外键约束）：
    QA 问题 → 译文 → 片段；场景卡 → 场景 → 章节。
    """
    from app.models.knowledge import SceneContextCard
    from app.models.qa import QAIssue
    from app.models.translation import Translation

    seg_ids = db.scalars(
        select(SourceSegment.id).where(SourceSegment.source_document_id == doc.id)
    ).all()
    if seg_ids:
        db.execute(delete(QAIssue).where(QAIssue.translation_id.in_(
            select(Translation.id).where(Translation.segment_id.in_(seg_ids))
        )))
        db.execute(delete(Translation).where(Translation.segment_id.in_(seg_ids)))
    scene_ids = db.scalars(
        select(Scene.id).where(Scene.source_document_id == doc.id)
    ).all()
    if scene_ids:
        db.execute(delete(SceneContextCard).where(SceneContextCard.scene_id.in_(scene_ids)))
    db.execute(delete(SourceSegment).where(SourceSegment.source_document_id == doc.id))
    db.execute(delete(Scene).where(Scene.source_document_id == doc.id))
    db.execute(delete(Chapter).where(Chapter.source_document_id == doc.id))
    db.commit()

    result = parse_script(text)
    project_id = doc.project_id

    # 注意：new_id 是 Python 端 default，flush 后才生成，先 flush 再建 ID 映射
    chapter_objs: list[tuple[int, Chapter]] = []
    for cb in result.chapters:
        ch = Chapter(
            project_id=project_id,
            source_document_id=doc.id,
            chapter_no=cb.no,
            title=cb.title or None,
            source_start=cb.start,
            source_end=cb.end,
        )
        db.add(ch)
        chapter_objs.append((cb.no, ch))
    db.flush()
    chapter_map: dict[int, str] = {no: ch.id for no, ch in chapter_objs}

    scene_objs: list[tuple[int, Scene]] = []
    for sb in result.scenes:
        sc = Scene(
            project_id=project_id,
            source_document_id=doc.id,
            chapter_id=chapter_map[sb.chapter_no],
            scene_no=sb.no,
            heading=sb.heading,
            source_start=sb.start,
            source_end=sb.end,
        )
        db.add(sc)
        scene_objs.append((sb.no, sc))
    db.flush()
    scene_map: dict[int, str] = {no: sc.id for no, sc in scene_objs}

    for no, seg in enumerate(result.segments, start=1):
        db.add(
            SourceSegment(
                project_id=project_id,
                source_document_id=doc.id,
                chapter_id=chapter_map.get(seg.chapter_no),
                scene_id=scene_map.get(seg.scene_no),
                segment_no=no,
                segment_type=seg.type,
                speaker_name=seg.speaker,
                source_text=seg.text,
                source_start=seg.start,
                source_end=seg.end,
                source_hash=sha256(seg.text),
            )
        )
    db.commit()
    return result
