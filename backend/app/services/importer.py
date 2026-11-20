"""原文导入：读文件、规范化、留档、哈希。"""

import hashlib
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import SourceDocument
from app.models.project import Project
from app.services.project_paths import ensure_project_root

SUPPORTED_FORMATS = {"txt", "md", "docx"}


def _extract_text(path: Path, fmt: str) -> str:
    if fmt == "docx":
        from docx import Document  # 延迟导入

        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    return path.read_text(encoding="utf-8", errors="replace")


def _normalize(text: str) -> str:
    """统一换行符为 \\n，保留其余内容。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def import_document(
    db: Session, project: Project, raw_path: Path, filename: str
) -> SourceDocument:
    """保存原始文件副本与规范化文本，创建文档版本记录。"""
    fmt = filename.rsplit(".", 1)[-1].lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的格式：{fmt}，仅支持 TXT/MD/DOCX")

    doc_dir = ensure_project_root(project) / "sources"
    next_version = (
        db.scalar(
            select(func.max(SourceDocument.version)).where(
                SourceDocument.project_id == project.id
            )
        )
        or 0
    ) + 1

    saved_raw = doc_dir / f"v{next_version}_{filename}"
    saved_raw.write_bytes(raw_path.read_bytes())

    text = _extract_text(saved_raw, fmt)
    normalized = _normalize(text)
    norm_path = doc_dir / f"v{next_version}_normalized.txt"
    norm_path.write_text(normalized, encoding="utf-8")

    doc = SourceDocument(
        project_id=project.id,
        version=next_version,
        filename=filename,
        format=fmt,
        raw_path=str(saved_raw),
        normalized_text_path=str(norm_path),
        content_hash=sha256(normalized),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def read_normalized(doc: SourceDocument) -> str:
    return Path(doc.normalized_text_path).read_text(encoding="utf-8")
