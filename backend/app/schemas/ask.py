"""模型动作、工具往返及本地预演结果；不是已发布的HTTP契约。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.source import NonBlankText
from app.schemas.tools import TextEvidence, ToolRecord, ToolResponse


class ConversationTurn(BaseModel):
    """一轮历史文字，仅用于理解追问，不作为本轮已核验的资料。"""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    question: NonBlankText
    answer: NonBlankText


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
    answer: NonBlankText = Field(description="给普通玩家的自然中文回答；直接回应问题，保留来源引用和必要限制，不展示程序字段、状态码或调试过程。")
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
    citations: list[ToolRecord | TextEvidence] = Field(default_factory=list)
    trace: list[ToolExchange] = Field(default_factory=list)
    error: AskError | None = None
    preview: bool = False
    answerable: Literal[False] = False
    notes: list[str] = Field(default_factory=list)
