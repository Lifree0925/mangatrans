"""项目 CRUD 路由。业务逻辑不进路由函数，走 service 层。"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.project import Project
from app.schemas.project import ProjectCreate, ProjectOut, ProjectPatch

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    """项目列表，按更新时间倒序。"""
    return list(db.scalars(select(Project).order_by(Project.updated_at.desc())))


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    from pathlib import Path

    custom = (payload.storage_path or "").strip()
    root: Path | None = None
    if custom:
        root = Path(custom).expanduser()
        if not root.is_absolute():
            raise HTTPException(status_code=400, detail="项目文件夹必须是绝对路径")
        occupied = db.scalar(select(Project).where(Project.storage_path == str(root)))
        if occupied is not None:
            raise HTTPException(status_code=400, detail="该文件夹已被其他项目使用")
        try:
            (root / "sources").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"无法创建项目文件夹：{exc}") from exc

    project = Project(
        name=payload.name,
        source_language=payload.source_language,
        target_language=payload.target_language,
        storage_path=str(root) if root else None,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
def patch_project(
    project_id: str, payload: ProjectPatch, db: Session = Depends(get_db)
) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, db: Session = Depends(get_db)) -> None:
    """级联删除项目及其全部关联数据（外键依赖顺序）。"""
    from app.services.project_service import delete_project_cascade

    if not delete_project_cascade(db, project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
