"""知识提取与场景卡的 LLM 输出 Schema（Pydantic 校验）。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeCandidate(BaseModel):
    canonical_key: str = Field(min_length=1, max_length=200)
    item_type: Literal["character", "term", "relation", "rule"]
    aliases: list[str] = Field(default_factory=list)
    data: dict = Field(default_factory=dict)
    evidence_quotes: list[str] = Field(default_factory=list)
    evidence_type: Literal["explicit", "inferred", "uncertain"] = "explicit"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ExtractionOutput(BaseModel):
    items: list[KnowledgeCandidate] = Field(default_factory=list)


class SceneCardModel(BaseModel):
    """场景卡内容（card_json 的结构）。"""

    location: str = ""
    time_label: str = ""
    cast: list[str] = Field(default_factory=list)
    prior_events: str = ""
    conflict_goal: str = ""
    mood: str = ""
    intent_notes: list[str] = Field(default_factory=list)
    spoiler_guard: list[str] = Field(default_factory=list)
    link_prev: str = ""


class KnowledgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    item_type: str
    canonical_key: str
    data_json: str
    status: str
    source: str
    version: int


class SceneCardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scene_id: str
    card_json: str
    status: str
    version: int
