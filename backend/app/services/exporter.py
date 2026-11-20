"""导出：Markdown / TXT / DOCX，支持中英对照或纯英文。"""

import base64
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import SourceSegment
from app.models.project import Project
from app.models.qa import QAIssue
from app.models.translation import Translation

FORMATS = {"md": ".md", "txt": ".txt", "docx": ".docx"}


def _current_polished(db: Session, segment_ids: list[str]) -> dict[str, Translation]:
    rows = db.scalars(
        select(Translation)
        .where(Translation.segment_id.in_(segment_ids), Translation.stage == "polished")
        .order_by(Translation.version.desc())
    )
    result: dict[str, Translation] = {}
    for t in rows:
        if t.segment_id not in result:
            result[t.segment_id] = t
    return result


def _load(db: Session, project: Project):
    segs = list(
        db.scalars(
            select(SourceSegment)
            .where(SourceSegment.project_id == project.id)
            .order_by(SourceSegment.segment_no)
        )
    )
    trans = _current_polished(db, [s.id for s in segs])
    issues = list(db.scalars(select(QAIssue).where(QAIssue.project_id == project.id)))
    return segs, trans, issues


def _report(segs, trans, issues) -> dict:
    untranslated = sum(1 for s in segs if not trans.get(s.id))
    high_critical = [i for i in issues if i.severity in ("high", "critical")]
    return {
        "segments": len(segs),
        "untranslated": untranslated,
        "issues_total": len(issues),
        "high_critical_issues": len(high_critical),
    }


def _export_markdown(project: Project, segs, trans, bilingual: bool) -> str:
    lines: list[str] = [f"# {project.name}"]
    for s in segs:
        t = trans.get(s.id)
        text = t.target_text if t else ""
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
    return "\n".join(lines) + "\n"


def _export_txt(project: Project, segs, trans, bilingual: bool) -> str:
    lines: list[str] = [project.name, "=" * 40]
    for s in segs:
        t = trans.get(s.id)
        text = t.target_text if t else ""
        if s.segment_type == "heading":
            lines.append(f"\n{s.source_text.strip()}\n")
        elif s.segment_type == "dialogue":
            speaker = s.speaker_name or ""
            if bilingual:
                lines.append(f"{speaker}（原文）：{s.source_text.strip()}")
                lines.append(f"{speaker}: {text}")
            else:
                lines.append(f"{speaker}: {text}")
        else:
            if bilingual:
                lines.append(s.source_text.strip())
                lines.append(text)
            else:
                lines.append(text)
    return "\n".join(lines) + "\n"


def _export_docx(project: Project, segs, trans, bilingual: bool) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading(project.name, level=0)
    for s in segs:
        t = trans.get(s.id)
        text = t.target_text if t else ""
        if s.segment_type == "heading":
            doc.add_heading(s.source_text.strip(), level=2)
        elif s.segment_type == "dialogue":
            p = doc.add_paragraph()
            run = p.add_run(f"{s.speaker_name or ''}: ")
            run.bold = True
            p.add_run(text)
            if bilingual:
                src = doc.add_paragraph(s.source_text.strip())
                src.runs[0].italic = True
        else:
            doc.add_paragraph(text)
            if bilingual:
                src = doc.add_paragraph(s.source_text.strip())
                src.runs[0].italic = True
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_project(
    db: Session, project: Project, bilingual: bool, fmt: str = "md"
) -> dict:
    if fmt not in FORMATS:
        fmt = "md"
    segs, trans, issues = _load(db, project)
    report = _report(segs, trans, issues)

    if fmt == "docx":
        data = _export_docx(project, segs, trans, bilingual)
        return {
            "filename": f"{project.name}-{'bilingual' if bilingual else 'english'}.docx",
            "format": "docx",
            "content": None,
            "content_base64": base64.b64encode(data).decode("ascii"),
            "report": report,
        }

    if fmt == "txt":
        body = _export_txt(project, segs, trans, bilingual)
    else:
        body = _export_markdown(project, segs, trans, bilingual)
    return {
        "filename": f"{project.name}-{'bilingual' if bilingual else 'english'}.{fmt}",
        "format": fmt,
        "content": body,
        "content_base64": None,
        "report": report,
    }
