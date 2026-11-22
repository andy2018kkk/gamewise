"""JSON 实验与 SQLite 共用的只读预览结构，均不代表正式回答。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.source import NonBlankText

# 仅是同一模式的页面标签写法，不表示不同服共享规则。
_MODE_LABELS = {"sanguosha": (("经典", "经典版本"),)}


def mode_labels(mode: str, category: str) -> tuple[str, ...]:
    """返回已登记类别的等价标签；未知名称保持精确匹配。"""
    return next((labels for labels in _MODE_LABELS.get(category, ()) if mode in labels), (mode,))


class SamePageRelation(BaseModel):
    """同页补充关联的来源指针，不包含技能正文或审核结论。"""
    character: str
    related_to: str
    source_id: str
    source_locator: str
    content_hash: str


class Game(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    game_id: NonBlankText
    name: NonBlankText
    category: NonBlankText = "unknown"


class QueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character: NonBlankText | None = None
    skill_name: NonBlankText | None = None
    mode: NonBlankText | None = None
    package_name: NonBlankText | None = None
    faction: NonBlankText | None = None


PreviewStatus = Literal["sample_available", "no_match_in_sample", "not_collected"]


class CatalogItem(BaseModel):
    game_id: str
    name: str
    status: PreviewStatus
    characters: list[str]
    modes: list[str]
    record_count: int
    packages: list[str] = Field(default_factory=list)
    factions: list[str] = Field(default_factory=list)
    answerable: Literal[False] = False


class SampleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character: NonBlankText
    skill_name: NonBlankText
    mode: NonBlankText
    skill_text: NonBlankText
    record_id: str
    game_id: str
    version_ids: list[str]
    source_id: str
    source_locator: str
    content_path: str
    content_hash: str | None
    locators: list[str]
    parent_record_id: str | None = None
    package_kind: str | None = None
    package_name: str | None = None
    faction: str | None = None
    hp_raw: str | None = None
    tags: list[str] | None = None
    review_status: Literal["pending"] = "pending"
    answerable: Literal[False] = False


class SearchResult(BaseModel):
    game_id: str
    status: PreviewStatus
    records: list[SampleRecord]
    answerable: Literal[False] = False
