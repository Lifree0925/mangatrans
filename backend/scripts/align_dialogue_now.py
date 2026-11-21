"""一次性台词对齐（运维脚本）：用当前 polished 译文对齐台词行英文。

用法（容器内）：
    docker exec -w /app mangatrans-backend-1 env PYTHONPATH=/app python /app/scripts/align_dialogue_now.py
"""

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import app.core.database as dbmod
from app.core.config import settings
from app.models.document import SourceDocument
from app.models.project import Project


def main() -> int:
    engine = dbmod.make_engine(settings.database_url)
    dbmod.engine = engine
    dbmod.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    from app.services import dialogue_service
    from app.services.provider_service import default_model_name, set_default_from_db

    with dbmod.SessionLocal() as db:
        set_default_from_db(db)
        model = default_model_name(db)
        projects = list(db.scalars(select(Project)))
        docs = {
            d.project_id: d
            for d in db.scalars(
                select(SourceDocument).where(SourceDocument.status == "active")
            )
        }

    async def run(project_id: str, doc: SourceDocument):
        from app.llm.registry import get_provider

        def cb(p: dict) -> None:
            print(f"[{project_id}] 对齐 {p['done']}/{p['total']} 段", flush=True)

        result = await dialogue_service.align_dialogue_translations(
            dbmod.SessionLocal, project_id, doc.id, get_provider(), model, progress_cb=cb
        )
        print(f"[{project_id}] 对齐完成: {result}", flush=True)

    for p in projects:
        doc = docs.get(p.id)
        if doc is None:
            continue
        print(f"[{p.id}] {p.name}: 开始对齐", flush=True)
        asyncio.run(run(p.id, doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
