"""模型包：导入所有模型以注册到 Base.metadata。"""

from app.models.audit import AuditLog
from app.models.document import Chapter, Scene, SourceDocument, SourceSegment
from app.models.qa import QAIssue, WindowQuality
from app.models.knowledge import KnowledgeAlias, KnowledgeEvidence, KnowledgeItem, SceneContextCard
from app.models.project import Project
from app.models.task import Task, TaskItem
from app.models.translation import ProviderConfig, StyleGuide, Translation

__all__ = [
    "Project",
    "AuditLog",
    "SourceDocument",
    "Chapter",
    "Scene",
    "SourceSegment",
    "KnowledgeItem",
    "KnowledgeAlias",
    "KnowledgeEvidence",
    "SceneContextCard",
    "Task",
    "TaskItem",
    "QAIssue",
    "WindowQuality",
    "Translation",
    "ProviderConfig",
    "StyleGuide",
]
