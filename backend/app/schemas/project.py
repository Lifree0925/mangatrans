"""项目 API Schema。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_language: str = "zh-CN"
    target_language: str = "en"


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    source_language: str | None = None
    target_language: str | None = None
    style_config_json: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    source_language: str
    target_language: str
    active_source_document_id: str | None
    created_at: datetime
    updated_at: datetime
