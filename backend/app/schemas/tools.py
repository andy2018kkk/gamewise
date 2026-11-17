"""现有待审查询的工具边界；正式规则范围和问答契约尚未接入。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.query import CatalogItem, QueryFilters, SampleRecord
from app.schemas.source import NonBlankText


class CatalogArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    game_ids: list[NonBlankText] | None = Field(default=None, min_length=1, max_length=12,
        description="仅skill使用：程序允许的发行分支ID。rule/analysis必须省略此字段。")
    character: NonBlankText | None = Field(default=None, description="仅skill：具体武将条目名，不把界/谋等当别名。")
    package_name: NonBlankText | None = Field(default=None, description="仅skill：目录中的武将包名，不是模式。")
    faction: NonBlankText | None = None
    record_type: Literal["skill", "rule", "analysis"] = Field(default="skill",
        description="skill查武将技能，rule查机制规则，analysis查社区策略。文本目录仅传本字段。")
    include_related: bool = Field(default=False,
        description="仅discover_catalog：人物比较未指定系列时设true，并传character作为人物名，发现有来源的系列候选；明确系列用false。不能和包/势力筛选或文本类型组合。")

    @model_validator(mode="after")
    def validate_related_discovery(self) -> Self:
        if self.include_related and (self.record_type != "skill" or self.character is None
                                     or self.package_name is not None or self.faction is not None):
            raise ValueError("关联发现须指定人物character和skill类型，不接受包/势力或文本筛选")
        return self


class SearchArguments(CatalogArguments):
    skill_name: NonBlankText | None = None
    mode: NonBlankText | None = Field(default=None, description="仅skill：使用目录返回的准确模式名。")
    scope_id: NonBlankText | None = Field(default=None,
        description="仅rule/analysis且必填：先discover_catalog(record_type=对应类型)，复制实际返回的scope_id，不自行编造。")
    query: NonBlankText | None = Field(default=None, description="仅rule/analysis且必填：搜索问题或关键词。")
    top_k: int = Field(default=3, ge=1, le=5, description="仅rule/analysis设置返回片段数1–5；skill查询省略。")

    @model_validator(mode="after")
    def require_filter(self) -> Self:
        if self.include_related:
            raise ValueError("关联仅用于目录发现；技能查询须逐个使用具体条目")
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
        return QueryFilters(**self.model_dump(exclude={"game_ids", "record_type", "scope_id", "query", "top_k", "include_related"}))


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


class RelationSource(BaseModel):
    source_url: str
    revision: int
    lines: list[int]


class FamilyCandidate(BaseModel):
    character: str
    modes: list[str]
    packages: list[str]
    skill_count: int
    coverage: Literal["loaded", "not_loaded"]


class FamilyDiscovery(BaseModel):
    person: str
    status: Literal["registered", "not_registered", "conflict"]
    source: RelationSource | None = None
    candidates: list[FamilyCandidate] = Field(default_factory=list)
    review_status: Literal["pending"] = "pending"
    coverage_note: str = "仅覆盖本地已解析的关联行，不代表完整系列、线上最新或上线状态。"


class ToolScopeResult(BaseModel):
    game_id: str
    status: Literal["catalog_available", "preview_available", "pending_review", "no_match_in_loaded_data", "not_loaded"]
    catalog: CatalogItem | None = None
    family: FamilyDiscovery | None = None
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
