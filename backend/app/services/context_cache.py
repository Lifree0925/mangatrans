"""窗口上下文缓存：知识/别名一次加载，多种窗口复用，避免每窗重复全表读取。"""

import json
import threading

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeAlias, KnowledgeItem

_CACHE: dict[tuple, list[dict]] = {}
_LOCK = threading.Lock()
_MAX_ENTRIES = 64


def _signature(db: Session, project_id: str) -> tuple:
    count, updated = db.execute(
        select(func.count(KnowledgeItem.id), func.max(KnowledgeItem.updated_at)).where(
            KnowledgeItem.project_id == project_id
        )
    ).one()
    alias_count = db.scalar(
        select(func.count(KnowledgeAlias.id))
        .join(KnowledgeItem, KnowledgeAlias.knowledge_id == KnowledgeItem.id)
        .where(KnowledgeItem.project_id == project_id)
    ) or 0
    return project_id, int(count or 0), str(updated or ""), int(alias_count)


def invalidate_project_knowledge(project_id: str | None = None) -> None:
    with _LOCK:
        if project_id is None:
            _CACHE.clear()
            return
        for key in [k for k in _CACHE if k[0] == project_id]:
            _CACHE.pop(key, None)


def get_project_knowledge(db: Session, project_id: str) -> list[dict]:
    """返回项目已批准知识 + 别名；只读结构，调用方不得原地修改。"""
    key = _signature(db, project_id)
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    items = list(
        db.scalars(
            select(KnowledgeItem).where(
                KnowledgeItem.project_id == project_id,
                KnowledgeItem.status.in_(["approved", "auto_approved"]),
            )
        )
    )
    aliases: dict[str, list[str]] = {}
    if items:
        for row in db.scalars(
            select(KnowledgeAlias).where(KnowledgeAlias.knowledge_id.in_([i.id for i in items]))
        ):
            aliases.setdefault(row.knowledge_id, []).append(row.alias)
    result = [
        {
            "id": item.id,
            "item_type": item.item_type,
            "canonical_key": item.canonical_key,
            "data": json.loads(item.data_json or "{}"),
            "aliases": aliases.get(item.id, []),
        }
        for item in items
    ]
    with _LOCK:
        if len(_CACHE) >= _MAX_ENTRIES:
            for old_key in list(_CACHE)[: _MAX_ENTRIES // 4]:
                _CACHE.pop(old_key, None)
        _CACHE[key] = result
    return result
