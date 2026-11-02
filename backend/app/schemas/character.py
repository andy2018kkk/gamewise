"""三国杀某武将在某模式下的资料格式；字段有效不等于事实已审核。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.source import NonBlankText

class CharacterSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: NonBlankText
    skill_text: NonBlankText
    # None＝尚未核查标签；[]＝已核查没有此类标签，不能由缺字段自动推断。
    tags: list[NonBlankText] | None = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("同一技能的标签不能重复")
        return value


class CharacterRecord(BaseModel):
    """一条记录只属于一个具体武将和模式；所属服及来源沿用外层 SourceRecord。"""

    model_config = ConfigDict(extra="forbid")

    character: NonBlankText
    mode: NonBlankText
    package_kind: Literal["武将包", "国战包"] | None = None
    package_name: NonBlankText | None = None
    faction: NonBlankText | None = None
    hp_raw: NonBlankText | None = None
    skills: list[CharacterSkill] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_attributes(self) -> Self:
        names = [skill.skill_name for skill in self.skills]
        if len(set(names)) != len(names):
            raise ValueError("同一武将模式内技能名重复，需核查原文归属")
        return self
