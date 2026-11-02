"""模型动作、工具往返及本地预演结果；不是已发布的HTTP契约。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.source import NonBlankText
from app.schemas.tools import ToolRecord, ToolResponse


class ToolCallTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["tool_call"] = "tool_call"
    call_id: NonBlankText
    name: NonBlankText
    arguments: NonBlankText


class FinalAnswerTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["final"] = "final"
    status: Literal["answered", "partial", "needs_clarification", "insufficient_evidence"]
    answer: NonBlankText
    citation_ids: list[NonBlankText] = Field(default_factory=list)


ModelTurn = Annotated[ToolCallTurn | FinalAnswerTurn, Field(discriminator="kind")]


class ToolExchange(BaseModel):
    call: ToolCallTurn
    response: ToolResponse
    elapsed_ms: float = 0


class AskError(BaseModel):
    code: Literal["invalid_question", "invalid_model_output", "model_error", "model_timeout", "tool_timeout",
                  "tool_limit", "round_limit", "repeated_call", "duplicate_call_id", "evidence_budget",
                  "storage_error", "invalid_citation", "missing_citation"]
    message: str


class AskResult(BaseModel):
    status: Literal["preview_answer", "partial", "needs_clarification", "insufficient_evidence", "error"]
    answer: str | None = None
    citations: list[ToolRecord] = Field(default_factory=list)
    trace: list[ToolExchange] = Field(default_factory=list)
    error: AskError | None = None
    preview: bool = False
    answerable: Literal[False] = False
    notes: list[str] = Field(default_factory=list)
