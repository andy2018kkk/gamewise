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


class SearchArguments(CatalogArguments):
    skill_name: NonBlankText | None = None
    mode: NonBlankText | None = None

    @model_validator(mode="after")
    def require_filter(self) -> Self:
        if all(getattr(self, key) is None for key in ("character", "skill_name", "package_name", "faction")):
            raise ValueError("至少提供武将名、技能名、包名或势力")
        return self

    def filters(self) -> QueryFilters:
        return QueryFilters(**self.model_dump(exclude={"game_ids"}))


class ToolRecord(SampleRecord):
    citation_id: str


class ToolScopeResult(BaseModel):
    game_id: str
    status: Literal["catalog_available", "preview_available", "pending_review", "no_match_in_loaded_data", "not_loaded"]
    catalog: CatalogItem | None = None
    records: list[ToolRecord] = Field(default_factory=list)
    has_more: bool = False


class ToolError(BaseModel):
    code: Literal["unknown_tool", "invalid_arguments", "scope_denied", "storage_error", "output_limit"]
    message: str


class ToolResponse(BaseModel):
    results: list[ToolScopeResult] = Field(default_factory=list)
    error: ToolError | None = None
    preview: bool = False
    answerable: Literal[False] = False
    notes: list[str] = Field(default_factory=list)
