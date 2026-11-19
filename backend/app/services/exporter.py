"""导出：英文剧本 / 中英对照 Markdown，及问题报告。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import SourceSegment
from app.models.project import Project
from app.models.qa import QAIssue
from app.models.translation import Translation


def _current_polished(db: Session, segment_ids: list[str]) -> dict[str, Translation]:
    rows = db.scalars(
        select(Translation)
        .where(Translation.segment_id.in_(segment_ids), Translation.stage == "polished")
        .order_by(Translation.version.desc())
    )
    # 同 segment 取最新版本
    result: dict[str, Translation] = {}
    for t in rows:
        if t.segment_id not in result:
            result[t.segment_id] = t
    return result


def export_markdown(db: Session, project: Project, bilingual: bool) -> tuple[str, dict]:
    segs = list(
        db.scalars(
            select(SourceSegment)
            .where(SourceSegment.project_id == project.id)
            .order_by(SourceSegment.segment_no)
        )
    )
    trans = _current_polished(db, [s.id for s in segs])
    issues = list(
        db.scalars(select(QAIssue).where(QAIssue.project_id == project.id))
    )
    high_severity = [i for i in issues if i.severity in ("high", "critical")]

    lines: list[str] = [f"# {project.name}"]
    untranslated = 0
    for s in segs:
        t = trans.get(s.id)
        text = t.target_text if t else ""
        if not text:
            untranslated += 1
        if s.segment_type == "heading":
            lines.append(f"\n## {s.source_text.strip()}")
        elif s.segment_type == "dialogue":
            speaker = s.speaker_name or ""
            if bilingual:
                lines.append(f"\n**{speaker}**（原文）：{s.source_text.strip()}")
                lines.append(f"**{speaker}**：{text}")
            else:
                lines.append(f"\n**{speaker}**：{text}")
        else:
            if bilingual:
                lines.append(f"\n{s.source_text.strip()}\n{text}")
            else:
                lines.append(f"\n{text}")
    body = "\n".join(lines) + "\n"
    report = {
        "segments": len(segs),
        "untranslated": untranslated,
        "issues_total": len(issues),
        "high_critical_issues": len(high_severity),
    }
    return body, report
