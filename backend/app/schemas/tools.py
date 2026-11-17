"""现有待审查询的工具边界；正式规则范围和问答契约尚未接入。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.query import CatalogItem, QueryFilters, SampleRecord
from app.schemas.source import NonBlankText


class CatalogArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    game_ids: list[NonBlankText] | None = Field(default=None, min_length=1, max_length=12)
    character: NonBlankText | None = None
    package_name: NonBlankText | None = None
    faction: NonBlankText | None = None
    record_type: Literal["skill", "rule", "analysis"] = "skill"


class SearchArguments(CatalogArguments):
    skill_name: NonBlankText | None = None
    mode: NonBlankText | None = None
    scope_id: NonBlankText | None = None
    query: NonBlankText | None = None
    top_k: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def require_filter(self) -> Self:
        if self.record_type != "skill":
            if self.scope_id is None or self.query is None:
                raise ValueError("文本查询须提供范围编号和搜索问题")
            if any(getattr(self, key) is not None for key in
                   ("game_ids", "character", "skill_name", "package_name", "faction", "mode")):
                raise ValueError("文本范围由scope_id限定，不接受武将筛选字段")
            return self
        if self.scope_id is not None or self.query is not None or self.top_k != 3:
            raise ValueError("技能查询使用原有精确字段，不接受文本搜索字段")
        if all(getattr(self, key) is None for key in ("character", "skill_name", "package_name", "faction")):
            raise ValueError("至少提供武将名、技能名、包名或势力")
        return self

    def filters(self) -> QueryFilters:
        return QueryFilters(**self.model_dump(exclude={"game_ids", "record_type", "scope_id", "query", "top_k"}))


class ReadEvidenceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    citation_id: NonBlankText


class TextScopeInfo(BaseModel):
    scope_id: str
    record_type: Literal["rule", "analysis"]
    game_id: str
    applicability: str


class TextPassage(BaseModel):
    record_id: str
    source_id: str
    source_url: str
    scope_id: str
    game_id: str
    record_type: Literal["rule", "analysis"]
    title: str
    heading: str
    text: str
    start: int
    end: int
    context_hash: str
    limitations: list[str]
    truncated: bool = False
    review_status: Literal["pending"] = "pending"
    answerable: Literal[False] = False


class TextEvidence(TextPassage):
    citation_id: str


class ToolRecord(SampleRecord):
    citation_id: str


class ToolScopeResult(BaseModel):
    game_id: str
    status: Literal["catalog_available", "preview_available", "pending_review", "no_match_in_loaded_data", "not_loaded"]
    catalog: CatalogItem | None = None
    records: list[ToolRecord | TextEvidence] = Field(default_factory=list)
    scope: TextScopeInfo | None = None
    has_more: bool = False


class ToolError(BaseModel):
    code: Literal["unknown_tool", "invalid_arguments", "scope_denied", "storage_error", "output_limit", "invalid_citation"]
    message: str


class ToolResponse(BaseModel):
    results: list[ToolScopeResult] = Field(default_factory=list)
    error: ToolError | None = None
    preview: bool = False
    answerable: Literal[False] = False
    notes: list[str] = Field(default_factory=list)
