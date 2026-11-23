"""导出：按《剧本交付模板.md》生成交付版。

交付模板约定：
- 顶部：英文剧名行 + 《中文剧名》行 + 空行
- 集标题：第N集（章间空行分隔）
- 场景行：N-M 地点 时间 内外（中文原文）
- 人物行：人物：Role1、Role2（角色名英文化）
- 动作/音效/闪回等叙述行：保留中文原文（制作团队以中文阅读）
- 台词：英文角色名（中文语气说明）：英文台词（唯一被替换为译文的部分）
- 结尾：（剧终）
"""

import base64
import re
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Chapter, Scene, SourceSegment
from app.models.project import Project
from app.models.qa import QAIssue
from app.models.translation import Translation
from app.services.title_service import SourceTitle, detect_source_title

FORMATS = {"md": ".md", "txt": ".txt", "docx": ".docx"}

# 原文台词行："角色（中文语气说明）：台词"
_DIALOGUE_SRC_RE = re.compile(r"^\s*([^（(：:]+?)\s*[（(]([^）)]*)[）)]\s*[:：]\s*(.*)$")
# 译文台词行："Role (english direction): line"
_DIALOGUE_EN_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 .'&-]*?)\s*(?:\([^)]*\))?\s*[:：]\s*(.*)$"
)
_CHAPTER_HEADING_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十百]+\s*[集話话回章]")
_SCENE_HEADING_RE = re.compile(r"^\d+\s*[-–]\s*\d+\s*\S")
_CAST_HEADING_RE = re.compile(r"^\s*(?:人物列表|人物)\s*[：:]")


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
    chapters = {
        c.id: c
        for c in db.scalars(select(Chapter).where(Chapter.project_id == project.id))
    }
    scenes = {
        s.id: s
        for s in db.scalars(select(Scene).where(Scene.project_id == project.id))
    }
    trans = _current_polished(db, [s.id for s in segs])
    current_ids = {t.id for t in trans.values()}
    issues = [
        i for i in db.scalars(select(QAIssue).where(QAIssue.project_id == project.id))
        if i.translation_id in current_ids
    ]
    return segs, chapters, scenes, trans, issues


def _report(segs, trans, issues) -> dict:
    untranslated = sum(1 for s in segs if not trans.get(s.id))
    high_critical = [i for i in issues if i.severity in ("high", "critical")]
    return {
        "segments": len(segs),
        "untranslated": untranslated,
        "issues_total": len(issues),
        "high_critical_issues": len(high_critical),
    }


def _build_role_map(segs, trans) -> dict[str, str]:
    """中文原文角色名 → 英文角色名（从原文/译文台词前缀对齐得到）。"""
    role_map: dict[str, str] = {}
    for s in segs:
        if s.segment_type != "dialogue":
            continue
        t = trans.get(s.id)
        if t is None:
            continue
        src_lines = [l.strip() for l in (s.source_text or "").split("\n") if l.strip()]
        tgt_lines = [l.strip() for l in (t.target_text or "").split("\n") if l.strip()]
        for sl, tl in zip(src_lines, tgt_lines):
            m1 = _DIALOGUE_SRC_RE.match(sl)
            m2 = _DIALOGUE_EN_RE.match(tl)
            if m1 and m2:
                src_role = m1.group(1).strip()
                en_role = m2.group(1).strip()
                if src_role and en_role and src_role != en_role:
                    role_map[src_role] = en_role
    return role_map


def _render_cast_line(line: str, role_map: dict[str, str]) -> str:
    """人物行：角色名逐个替换为英文（标签统一为「人物：」）。"""
    m = _CAST_HEADING_RE.match(line)
    if not m:
        return line.strip()
    names = re.split(r"[、，,]\s*", line.split("：", 1)[-1] if "：" in line else line.split(":", 1)[-1])
    rendered = [role_map.get(n.strip(), n.strip()) for n in names if n.strip()]
    return "人物：" + "、".join(rendered)


def _render_dialogue(src_text: str, tgt_text: str) -> list[str]:
    """台词合成：英文角色名（中文语气说明）：英文台词；原文行与译文行逐行对齐。"""
    src_lines = [l.strip() for l in (src_text or "").split("\n") if l.strip()]
    tgt_lines = [l.strip() for l in (tgt_text or "").split("\n") if l.strip()]
    out: list[str] = []
    for i, sl in enumerate(src_lines):
        tl = tgt_lines[i] if i < len(tgt_lines) else None
        if tl is None:
            out.append(sl)  # 无译文行：保留原文
            continue
        m = _DIALOGUE_SRC_RE.match(sl)
        if not m:
            out.append(tl)  # 原文无「角色（语气）」结构：直接用译文行
            continue
        direction = m.group(2).strip()
        tm = _DIALOGUE_EN_RE.match(tl)
        if tm:
            en_role = tm.group(1).strip()
            en_line = tm.group(2).strip()
            out.append(
                f"{en_role}（{direction}）：{en_line}" if direction else f"{en_role}：{en_line}"
            )
        else:
            out.append(tl)  # 译文行无法解析前缀：保留译文原样
    # 译文行多于原文行（不应出现，validate_window 保证行数一致）：保底输出
    out.extend(tl for tl in tgt_lines[len(src_lines):])
    return out


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


def _export_delivery_markdown(
    project: Project,
    segs,
    chapters,
    scenes,
    trans,
    title_en: str | None,
    doc_title: str | None,
    source_title: SourceTitle | None = None,
) -> str:
    """按交付模板生成 .md 正文。

    原文已带英文标题时直接复用；标题所在片段不在正文重复输出。
    """
    role_map = _build_role_map(segs, trans)
    lines: list[str] = []
    english_title = (
        (title_en or "").strip()
        or (source_title.english_title if source_title else "")
        or project.name
    )
    # 剧本正文中的中英标题优先于文件名；文件名只在没有原文标题时兜底。
    chinese_title = (
        (source_title.chinese_title if source_title else "")
        or (doc_title or "").strip()
        or project.name
    )
    lines.append(english_title)
    lines.append(f"《{chinese_title}》")
    lines.append("")
    title_segment_ids = set(source_title.segment_ids) if source_title else set()

    last_chapter_id = None
    last_scene_id = None
    for s in segs:
        if s.id in title_segment_ids:
            continue
        if s.chapter_id != last_chapter_id:
            ch = chapters.get(s.chapter_id) if s.chapter_id else None
            if lines and lines[-1] != "":
                lines.append("")  # 集间空行
            lines.append(f"第{ch.chapter_no if ch else '?'}集")
            lines.append("")
            last_chapter_id = s.chapter_id
            last_scene_id = None

        if s.scene_id != last_scene_id and s.scene_id:
            sc = scenes.get(s.scene_id)
            if sc is not None and sc.heading:
                lines.append(sc.heading.strip())
            last_scene_id = s.scene_id

        t = trans.get(s.id)
        text = t.target_text.strip() if t else ""

        if s.segment_type == "heading":
            stripped = s.source_text.strip()
            if (
                _CHAPTER_HEADING_RE.match(stripped)
                or _SCENE_HEADING_RE.match(stripped)
                or (
                    s.scene_id
                    and scenes.get(s.scene_id) is not None
                    and (scenes[s.scene_id].heading or "").strip() == stripped
                )
            ):
                continue  # 章/场景行已由上方结构输出
            if _CAST_HEADING_RE.match(stripped):
                lines.append(_render_cast_line(stripped, role_map))
                continue
            lines.append(stripped)
        elif s.segment_type == "dialogue":
            lines.extend(_render_dialogue(s.source_text, text))
        else:
            # 动作 / 音效 / 闪回等：交付模板保留中文原文
            body = s.source_text.strip()
            if body:
                lines.append(body)

    # 结尾：（剧终）；原文已含则不再重复
    last_text = (lines[-1] if lines else "").strip()
    if "剧终" not in last_text:
        lines.append("")
        lines.append("（剧终）")
    return "\n".join(lines) + "\n"


def _delivery_docx(body: str) -> bytes:
    """交付模板内容转 Word：逐行段落，保持与 .md/.txt 完全一致的行结构。"""
    from docx import Document

    doc = Document()
    for line in body.split("\n"):
        if line.strip():
            doc.add_paragraph(line)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_project(
    db: Session,
    project: Project,
    bilingual: bool = True,
    fmt: str = "md",
    title_en: str | None = None,
    doc_title: str | None = None,
    delivery: bool = True,
) -> dict:
    """导出剧本。

    delivery=True：交付版（剧本交付模板.md，中文场景/动作/音效 + 英文台词）；
    delivery=False：常规导出（中英对照 / 纯英文 × md / txt / docx）。
    """
    # 先清理旧版本术语误报，导出报告只反映当前规则下的真实问题。
    from app.services.qa_service import refresh_forced_term_issues

    refresh_forced_term_issues(db, project.id)
    segs, chapters, scenes, trans, issues = _load(db, project)
    report = _report(segs, trans, issues)
    source_title = detect_source_title(segs)

    if fmt not in FORMATS:
        fmt = "md"
    if delivery:
        body = _export_delivery_markdown(
            project, segs, chapters, scenes, trans, title_en, doc_title, source_title
        )
        name = (
            (source_title.chinese_title if source_title else None)
            or (doc_title or "").strip()
            or project.name
        ).strip() or "剧本"
        if fmt == "docx":
            return {
                "filename": f"{name}.docx",
                "format": "docx",
                "content": None,
                "content_base64": base64.b64encode(_delivery_docx(body)).decode("ascii"),
                "report": report,
            }
        return {
            "filename": f"{name}.{fmt}",
            "format": fmt,
            "content": body,
            "content_base64": None,
            "report": report,
        }

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
