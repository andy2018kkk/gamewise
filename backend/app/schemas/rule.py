"""规则正文结构；规则集版本不代表当前各服适用版本。"""

from pydantic import BaseModel, ConfigDict
from app.schemas.source import NonBlankText


class RuleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ruleset_id: NonBlankText
    chapter: NonBlankText
    heading: NonBlankText
    text: NonBlankText
