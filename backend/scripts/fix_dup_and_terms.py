"""一次性修复：seg633 译文重复（重译）+ seg508/1398 术语混用（新版本替换）。"""

import asyncio
import re
import sys

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import app.core.database as dbmod
from app.core.config import settings
from app.models.document import SourceSegment
from app.models.project import Project
from app.models.translation import Translation


def main() -> int:
    engine = dbmod.make_engine(settings.database_url)
    dbmod.engine = engine
    dbmod.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    from app.services.provider_service import default_model_name, set_default_from_db

    with dbmod.SessionLocal() as db:
        set_default_from_db(db)
        model = default_model_name(db)
        proj = db.scalars(select(Project)).first()

    # 1) seg633 重译（译文与 632 重复）
    async def retranslate(no: int):
        from app.llm.registry import get_provider
        from app.services.translation_batch import _persist_window_translations, translate_window_draft_direct, translate_window_polish_direct

        with dbmod.SessionLocal() as db:
            window = list(
                db.scalars(select(SourceSegment).where(
                    SourceSegment.project_id == proj.id,
                    SourceSegment.segment_no == no,
                ))
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
        print(f"seg{no} 重译: {polished[0]['target_text']}", flush=True)

    asyncio.run(retranslate(633))

    # 2) 术语混用替换（新版本）：eternal mark kiss -> Kiss of the Eternal Mark
    with dbmod.SessionLocal() as db:
        for no in (508, 1398):
            seg = db.scalar(
                select(SourceSegment).where(
                    SourceSegment.project_id == proj.id,
                    SourceSegment.segment_no == no,
                )
            )
            t = db.scalar(
                select(Translation)
                .where(Translation.segment_id == seg.id, Translation.stage == "polished")
                .order_by(Translation.version.desc())
                .limit(1)
            )
            new = re.sub(r"\beternal mark kiss\b", "Kiss of the Eternal Mark", t.target_text, flags=re.I)
            db.add(Translation(
                segment_id=t.segment_id, version=t.version + 1, stage="polished",
                target_text=new, review_status=t.review_status,
                freshness_status=t.freshness_status,
                notes_json=t.notes_json, ambiguity_json=t.ambiguity_json,
                model_name=t.model_name, provider_id=t.provider_id,
                prompt_version=t.prompt_version,
                context_snapshot_json=t.context_snapshot_json, source_hash=t.source_hash,
            ))
            print(f"seg{no}: {new[:90]}", flush=True)
        db.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
