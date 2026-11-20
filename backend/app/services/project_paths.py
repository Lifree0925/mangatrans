"""项目文件路径：支持用户自定义项目文件夹。"""

from pathlib import Path

from app.core.config import settings
from app.models.project import Project


def project_root(project: Project) -> Path:
    """项目根目录：自定义路径优先，否则使用统一数据目录下的项目子目录。"""
    if project.storage_path:
        return Path(project.storage_path)
    return settings.data_dir / "projects" / project.id


def ensure_project_root(project: Project) -> Path:
    root = project_root(project)
    (root / "sources").mkdir(parents=True, exist_ok=True)
    return root
