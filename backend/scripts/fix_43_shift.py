"""一次性修复第 43 集 1080-1089 译文整体右移一格：左移重排 + 重译最后一段。"""

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import app.core.database as dbmod
from app.core.config import settings
from app.models.document import SourceSegment
from app.models.project import Project
from app.models.translation import Translation


def _latest(db, seg_id: str) -> Translation:
    return db.scalar(
        select(Translation)
        .where(Translation.segment_id == seg_id, Translation.stage == "polished")
        .order_by(Translation.version.desc())
        .limit(1)
    )


def _new_version(db, seg_id: str, text: str) -> None:
    old = _latest(db, seg_id)
    base = old.version if old else 0
    db.add(
        Translation(
            segment_id=seg_id,
            version=base + 1,
            stage="polished",
            target_text=text,
            notes_json=old.notes_json if old else "[]",
            ambiguity_json=old.ambiguity_json if old else "[]",
            model_name=old.model_name if old else "repair",
            provider_id=old.provider_id if old else "default",
            prompt_version=old.prompt_version if old else "v1",
            context_snapshot_json=old.context_snapshot_json if old else "{}",
            source_hash=old.source_hash if old else "",
        )
    )


def main() -> int:
    engine = dbmod.make_engine(settings.database_url)
    dbmod.engine = engine
    dbmod.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    from app.services.provider_service import default_model_name, set_default_from_db

    with dbmod.SessionLocal() as db:
        set_default_from_db(db)
        model = default_model_name(db)
        proj = db.scalars(select(Project)).first()
        seg_ids = {
            no: db.scalar(
                select(SourceSegment.id).where(
                    SourceSegment.project_id == proj.id,
                    SourceSegment.segment_no == no,
                )
            )
            for no in range(1080, 1090)
        }
        old = {no: _latest(db, seg_ids[no]) for no in range(1080, 1090)}

    # 左移重排（每段新版本）
    with dbmod.SessionLocal() as db:
        # 1080 = 旧1080 + 旧1081（旧1081即1080第二行场景标签）
        _new_version(db, seg_ids[1080], old[1080].target_text + "\n" + old[1081].target_text)
        for no in range(1081, 1089):
            _new_version(db, seg_ids[no], old[no + 1].target_text)
        db.commit()

    # 1089 单段重译（Talia 手杖落地）
    async def retranslate_1089():
        from app.llm.registry import get_provider
        from app.services.translation_batch import _persist_window_translations, translate_window_draft_direct, translate_window_polish_direct

        with dbmod.SessionLocal() as db:
            window = list(
                db.scalars(
                    select(SourceSegment).where(SourceSegment.id == seg_ids[1089])
                )
            )
        draft, _c, context = await translate_window_draft_direct(
            dbmod.SessionLocal, proj.id, window, model
        )
        polished, _ = await translate_window_polish_direct(
            dbmod.SessionLocal, proj.id, window, draft, None, model
        )
        with dbmod.SessionLocal() as db:
            _persist_window_translations(db, window, draft, polished, context, model)
            db.commit()
        print("seg1089 重译:", polished[0]["target_text"], flush=True)

    asyncio.run(retranslate_1089())
    return 0


if __name__ == "__main__":
    sys.exit(main())
