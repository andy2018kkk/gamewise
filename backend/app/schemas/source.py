"""批量来源文档记录；仅检查格式，不代表知识库准入。"""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_http_url = TypeAdapter(HttpUrl)


class SourceRecord(BaseModel):
    """一份文档的元数据，不是一道问题的答案。"""

    model_config = ConfigDict(extra="forbid")

    source_id: NonBlankText
    title: NonBlankText
    game_id: NonBlankText
    version_ids: list[NonBlankText] = Field(default_factory=list)
    topic: NonBlankText
    content_path: NonBlankText
    source_type: Literal["web", "local"]
    source_locator: NonBlankText
    collected_at: date
    content_hash: NonBlankText | None = None
    usage_note: NonBlankText | None = None
    review_status: Literal["pending", "approved", "rejected"] = "pending"
    review_note: NonBlankText | None = None

    @field_validator("collected_at", mode="before")
    @classmethod
    def validate_collection_date(cls, value: object) -> date:
        """日期必须明确，不能把数字当时间戳或悄悄丢弃时间部分。"""
        if type(value) is date:
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("collected_at 必须是有效日期，例如 2026-10-28") from error
        raise ValueError("collected_at 必须是日期或 ISO 日期字符串")

    @field_validator("source_locator")
    @classmethod
    def validate_source_locator(cls, value: str, info: ValidationInfo) -> str:
        """只检查定位格式，不联网，也不验证本地文件存在。"""
        if info.data.get("source_type") == "web":
            _http_url.validate_python(value)
        return value

    @model_validator(mode="after")
    def validate_review_information(self) -> Self:
        if not self.version_ids and self.review_status != "pending":
            raise ValueError("version_ids 未确认时，review_status 必须保持 pending")
        if self.review_status == "approved":
            required = ("usage_note", "review_note", "content_hash")
            missing = [name for name in required if getattr(self, name) is None]
            if missing:
                raise ValueError("approved 记录缺少：" + ", ".join(missing))
        return self


class SourceBatch(BaseModel):
    """一次检查多份来源记录；空列表表示没有资料。"""

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceRecord]

    @field_validator("sources")
    @classmethod
    def validate_unique_ids(cls, sources: list[SourceRecord]) -> list[SourceRecord]:
        seen: set[str] = set()
        errors = []
        for index, source in enumerate(sources):
            if source.source_id in seen:
                errors.append({
                    "type": "value_error",
                    "loc": (index, "source_id"),
                    "input": source.source_id,
                    "ctx": {"error": ValueError("同一批资料的 source_id 不能重复")},
                })
            seen.add(source.source_id)
        if errors:
            raise ValidationError.from_exception_data(cls.__name__, errors)
        return sources
