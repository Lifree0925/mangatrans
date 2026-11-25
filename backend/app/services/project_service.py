"""项目删除：按外键依赖顺序级联删除全部关联数据。"""

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.document import Chapter, Scene, SourceDocument, SourceSegment
from app.models.knowledge import (
    KnowledgeAlias,
    KnowledgeEvidence,
    KnowledgeItem,
    SceneContextCard,
)
from app.models.project import Project
from app.models.qa import QAIssue, WindowQuality
from app.models.dialogue import DialogueLine
from app.models.task import Task, TaskItem
from app.models.audit import AuditLog
from app.models.storyline import ChapterStoryline
from app.models.continuity import CharacterState, StoryFact
from app.models.translation import StyleGuide, Translation
from app.models.usage import LLMCall


def delete_project_cascade(db: Session, project_id: str) -> bool:
    """删除项目及其所有关联数据；不存在返回 False。"""
    project = db.get(Project, project_id)
    if project is None:
        return False

    task_ids = db.query(Task.id).filter(Task.project_id == project_id).subquery()
    db.execute(delete(LLMCall).where(LLMCall.task_id.in_(task_ids)))
    db.execute(delete(TaskItem).where(TaskItem.task_id.in_(task_ids)))
    db.execute(delete(WindowQuality).where(WindowQuality.task_id.in_(task_ids)))
    db.execute(delete(QAIssue).where(QAIssue.project_id == project_id))
    db.execute(
        delete(Translation).where(
            Translation.segment_id.in_(
                db.query(SourceSegment.id).filter(SourceSegment.project_id == project_id).subquery()
            )
        )
    )
    db.execute(delete(SceneContextCard).where(SceneContextCard.project_id == project_id))
    db.execute(
        delete(KnowledgeEvidence).where(
            KnowledgeEvidence.knowledge_id.in_(
                db.query(KnowledgeItem.id).filter(KnowledgeItem.project_id == project_id).subquery()
            )
        )
    )
    db.execute(
        delete(KnowledgeAlias).where(
            KnowledgeAlias.knowledge_id.in_(
                db.query(KnowledgeItem.id).filter(KnowledgeItem.project_id == project_id).subquery()
            )
        )
    )
    db.execute(delete(KnowledgeItem).where(KnowledgeItem.project_id == project_id))
    db.execute(delete(StyleGuide).where(StyleGuide.project_id == project_id))
    db.execute(delete(DialogueLine).where(DialogueLine.project_id == project_id))
    db.execute(delete(AuditLog).where(AuditLog.project_id == project_id))
    db.execute(delete(StoryFact).where(StoryFact.project_id == project_id))
    db.execute(delete(CharacterState).where(CharacterState.project_id == project_id))
    db.execute(
        delete(ChapterStoryline).where(
            ChapterStoryline.chapter_id.in_(
                db.query(Chapter.id).filter(Chapter.project_id == project_id).subquery()
            )
        )
    )
    db.execute(delete(Task).where(Task.project_id == project_id))
    db.execute(delete(SourceSegment).where(SourceSegment.project_id == project_id))
    db.execute(delete(Scene).where(Scene.project_id == project_id))
    db.execute(delete(Chapter).where(Chapter.project_id == project_id))
    db.execute(delete(SourceDocument).where(SourceDocument.project_id == project_id))
    db.delete(project)
    db.commit()
    return True
