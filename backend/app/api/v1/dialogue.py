"""台词本 API：抽取任务、列表、编辑回写。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.project import Project
from app.services.provider_service import default_model_name
from app.pipeline.runner import create_task, launch
from app.services import dialogue_service

router = APIRouter(prefix="/projects/{project_id}", tags=["dialogue"])


def _project(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return p


@router.post("/dialogue-lines/extract")
def extract_dialogue(
    project_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    """创建台词本抽取任务（异步执行）。payload: {model?}"""
    project = _project(db, project_id)
    if project.active_source_document_id is None:
        raise HTTPException(status_code=404, detail="尚未解析原文")
    task = create_task(
        db,
        project_id,
        "dialogue",
        {
            "source_document_id": project.active_source_document_id,
            "model": payload.get("model") or default_model_name(db),
        },
    )
    launch(task.id)
    return {"task_id": task.id}


@router.get("/dialogue-lines")
def list_dialogue(project_id: str, db: Session = Depends(get_db)) -> dict:
    _project(db, project_id)
    lines = dialogue_service.list_dialogue_lines(db, project_id)
    # 聚合结构：章节 → 场景 → 行
    chapters: dict[str, dict] = {}
    for ln in lines:
        ch = chapters.setdefault(ln["chapter_id"] or "-", {"chapter_id": ln["chapter_id"], "scenes": {}})
        sc = ch["scenes"].setdefault(
            ln["scene_id"] or "-",
            {"scene_id": ln["scene_id"], "lines": []},
        )
        sc["lines"].append(ln)
    return {
        "count": len(lines),
        "chapters": [{"chapter_id": k, "scenes": list(v["scenes"].values())} for k, v in chapters.items()],
    }


@router.patch("/dialogue-lines/{line_id}")
def update_dialogue(
    project_id: str,
    line_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    _project(db, project_id)
    result = dialogue_service.update_dialogue_line(
        db,
        line_id,
        text=payload.get("source_text"),
        speaker_name=payload.get("speaker_name"),
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result
