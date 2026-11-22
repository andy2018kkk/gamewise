"""网页只传问题及至多两轮历史文字，不传配置或工具记录。"""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.source import NonBlankText
from app.schemas.ask import ConversationTurn


class WebQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: NonBlankText
    history: list[ConversationTurn] = Field(default_factory=list, max_length=2)


class WebSource(BaseModel):
    number: int
    title: str
    game_id: str
    text: str
    url: str | None = None


class WebStep(BaseModel):
    action: str
    records: int
    error: str | None = None


class WebAnswer(BaseModel):
    status: str
    answer: str | None = None
    sources: list[WebSource] = Field(default_factory=list)
    steps: list[WebStep] = Field(default_factory=list)
    error: str | None = None
    elapsed_seconds: float = 0
    preview: bool = True
