"""一次性译文修复（运维脚本）：
1. 重译四组错位/漏译段（第 25/43/57 集标签错位链 + 段 251 漏译后半）；
2. 术语统一：wolf queen → Luna、destined mate → fated mate（均为新版本，历史保留）。

用法（容器内）：
    docker exec -w /app mangatrans-backend-1 env PYTHONPATH=/app python /app/scripts/fix_translation_issues.py
"""

import asyncio
import json
import re
import sys

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import app.core.database as dbmod
from app.core.config import settings
from app.models.document import SourceSegment
from app.models.project import Project
from app.models.translation import Translation

# 错位链（segment_no 闭区间）
FIX_GROUPS = [
    [251],
    list(range(611, 635)),
    list(range(1080, 1090)),
    list(range(1447, 1458)),
]


def _luna_replace(t: str) -> str:
    pairs = [
        ("be your wolf queen", "be your Luna"),
        ("you're my wolf queen now", "you're my Luna now"),
        ("become the wolf queen of the Fenrir family", "become the Luna of the Fenrir family"),
        ("accept a demon hunter as wolf queen", "accept a demon hunter as Luna"),
        ("let a demon hunter become wolf queen", "let a demon hunter become Luna"),
        ("you're my wolf queen, Talia", "you're my Luna, Talia"),
        ("Take back the Wolf Queen!", "Take back our Luna!"),
        ("have taken your Wolf Queen", "have taken your Luna"),
    ]
    for a, b in pairs:
        t = t.replace(a, b)
    t = re.sub(r"\bmy wolf queen\b", "my Luna", t, flags=re.I)
    t = re.sub(r"\byour wolf queen\b", "your Luna", t, flags=re.I)
    t = re.sub(r"\bas wolf queen\b", "as Luna", t, flags=re.I)
    return t


async def _retranslate(project_id: str, seg_nos: list[int], model: str) -> None:
    from app.llm.registry import get_provider
    from app.services.translation_batch import (
        _persist_window_translations,
        translate_window_draft_direct,
        translate_window_polish_direct,
    )

    with dbmod.SessionLocal() as db:
        window = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.project_id == project_id)
                .where(SourceSegment.segment_no.in_(seg_nos))
                .order_by(SourceSegment.segment_no)
            )
        )
        if not window:
            print(f"  未找到段 {seg_nos}", flush=True)
            return
        seg_ids = [s.id for s in window]
    provider = get_provider()
    draft, _carry_d, context = await translate_window_draft_direct(
        dbmod.SessionLocal, project_id, window, model
    )
    polished, _carry = await translate_window_polish_direct(
        dbmod.SessionLocal, project_id, window, draft, None, model
    )
    with dbmod.SessionLocal() as db:
        _persist_window_translations(db, window, draft, polished, context, model)
        db.commit()
    print(f"  重译 {len(window)} 段完成", flush=True)
    for src, pol in zip(window, polished):
        src_lines = [x for x in src.source_text.strip().split("\n") if x.strip()]
        tgt_lines = [x for x in pol["target_text"].strip().split("\n") if x.strip()]
        flag = "OK " if len(tgt_lines) >= len(src_lines) else "缺!"
        print(f"    {flag} seg{src.segment_no}: {len(src_lines)}->{len(tgt_lines)} 行", flush=True)


def main() -> int:
    engine = dbmod.make_engine(settings.database_url)
    dbmod.engine = engine
    dbmod.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    from app.services.provider_service import default_model_name, set_default_from_db

    with dbmod.SessionLocal() as db:
        set_default_from_db(db)
        model = default_model_name(db)
        proj = db.scalars(select(Project)).first()
        project_id = proj.id

    print(f"model={model}", flush=True)
    for group in FIX_GROUPS:
        print(f"重译段 {group[0]}-{group[-1]}...", flush=True)
        asyncio.run(_retranslate(project_id, group, model))

    # 术语统一（新版本）
    with dbmod.SessionLocal() as db:
        rows = list(
            db.scalars(
                select(Translation).where(
                    Translation.stage == "polished",
                    Translation.segment_id.in_(
                        select(SourceSegment.id).where(SourceSegment.project_id == project_id)
                    ),
                )
            )
        )
        latest: dict[str, Translation] = {}
        for t in rows:
            latest.setdefault(t.segment_id, t)
        changed_luna = changed_mate = 0
        for t in latest.values():
            new = _luna_replace(t.target_text)
            if new != t.target_text:
                changed_luna += 1
            new2 = re.sub(r"\bdestined mate\b", "fated mate", new, flags=re.I)
            if new2 != new:
                changed_mate += 1
            if new2 != t.target_text:
                base = (
                    db.scalar(
                        select(Translation.version).where(Translation.segment_id == t.segment_id)
                        .order_by(Translation.version.desc()).limit(1)
                    )
                    or 0
                )
                db.add(
                    Translation(
                        segment_id=t.segment_id,
                        version=base + 1,
                        stage="polished",
                        target_text=new2,
                        notes_json=t.notes_json,
                        ambiguity_json=t.ambiguity_json,
                        model_name=t.model_name,
                        provider_id=t.provider_id,
                        prompt_version=t.prompt_version,
                        context_snapshot_json=t.context_snapshot_json,
                        source_hash=t.source_hash,
                    )
                )
        db.commit()
        print(f"wolf queen→Luna: {changed_luna} 行；destined mate→fated mate: {changed_mate} 行", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
