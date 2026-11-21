"""一次性补齐台词本（运维脚本，非产品功能）。

背景：早期版本台词抽取在落库前中断，台词行只存在于进程内存、从未写入
dialogue_lines 表（0 行），数据库中无法找回；唯一可靠途径是用当前模型
重新抽取并落库（幂等：先清旧行）。

用法（容器内）：
    docker exec mangatrans-backend-1 python /app/scripts/backfill_dialogue.py
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

    print(f"model={model}, projects={len(projects)}, active_docs={len(docs)}", flush=True)

    async def run(project_id: str, doc: SourceDocument):
        from app.llm.registry import get_provider

        provider = get_provider()

        def cb(p: dict) -> None:
            print(
                f"[{project_id}] 批次 {p['done_batches']}/{p['total_batches']} "
                f"台词 {p['lines']} 行 · 弃用 {p['discarded']} · 失败批 {p['failed_batches']}",
                flush=True,
            )

        result = await dialogue_service.extract_dialogue_lines(
            dbmod.SessionLocal, project_id, doc.id, provider, model, progress_cb=cb
        )
        print(f"[{project_id}] 抽取完成: {result}", flush=True)
        return result

    for p in projects:
        doc = docs.get(p.id)
        if doc is None:
            print(f"[{p.id}] {p.name}: 无 active 文档，跳过", flush=True)
            continue
        print(f"[{p.id}] {p.name}: 开始重抽台词本", flush=True)
        asyncio.run(run(p.id, doc))

    with dbmod.SessionLocal() as db:
        from app.models.dialogue import DialogueLine

        total = len(list(db.scalars(select(DialogueLine))))
        print(f"dialogue_lines 总行数: {total}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
