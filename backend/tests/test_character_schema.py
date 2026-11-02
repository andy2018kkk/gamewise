"""武将格式校验；示例均为合成数据，不代表真实武将数值。"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.character import CharacterRecord
from app.schemas.source import SourceRecord
from app.services.data_processor import DataProcessor, EvidenceBlock, ManualExtractor, SourceDocument


def sample():
    return {
        "character": "合成武将", "mode": "经典版本", "package_kind": "武将包",
        "package_name": "合成包", "faction": "合成势力", "hp_raw": "4/5",
        "skills": [{"skill_name": "合成技能", "skill_text": "出牌阶段，摸一张牌。", "tags": ["限定技"]}],
    }


def test_character_holds_mode_attributes_and_multiple_skills():
    payload = sample()
    payload["skills"].append({"skill_name": "另一技能", "skill_text": "准备阶段，摸一张牌。"})
    record = CharacterRecord.model_validate(payload)
    assert record.hp_raw == "4/5"
    assert record.skills[0].tags == ["限定技"]
    assert record.skills[1].tags is None


@pytest.mark.parametrize("hp_raw", ["4/5", "@×2.5", "特殊体力表示"])
def test_raw_hp_does_not_guess_numeric_meaning(hp_raw):
    record = CharacterRecord(character="合成武将", mode="国战版本", hp_raw=hp_raw,
                             skills=[{"skill_name": "合成技能", "skill_text": "原文"}])
    assert "initial_hp" not in record.model_dump() and "max_hp" not in record.model_dump()
    assert record.package_name is None and record.faction is None


def test_missing_hp_and_tags_unknown_distinguished_from_none():
    payload = sample()
    payload["hp_raw"] = None
    payload["skills"][0]["tags"] = []
    assert CharacterRecord.model_validate(payload).hp_raw is None
    assert CharacterRecord.model_validate(payload).skills[0].tags == []
    del payload["skills"][0]["tags"]
    assert CharacterRecord.model_validate(payload).skills[0].tags is None


@pytest.mark.parametrize("change", [
    {"character": " "}, {"mode": ""}, {"package_name": " "}, {"faction": " "},
    {"hp_raw": " "}, {"hp_raw": 4}, {"hp_raw": True}, {"hp_raw": {}},
    {"package_kind": "猜测分类"}, {"skills": []}, {"unexpected": "字段拼写错误"},
    {"initial_hp": 4}, {"max_hp": 5},
])
def test_invalid_character_data_rejected(change):
    with pytest.raises(ValidationError):
        CharacterRecord.model_validate(sample() | change)


@pytest.mark.parametrize("skill", [
    {"skill_name": " ", "skill_text": "原文"},
    {"skill_name": "技能", "skill_text": " "},
    {"skill_name": "技能", "skill_text": "原文", "tags": ["限定技", "限定技"]},
    {"skill_name": "技能", "skill_text": "原文", "tags": [" "]},
    {"skill_name": "技能", "skill_text": "原文", "invented_field": True},
])
def test_invalid_skills_rejected(skill):
    with pytest.raises(ValidationError):
        CharacterRecord.model_validate(sample() | {"skills": [skill]})


def test_duplicate_skill_names_in_one_mode_rejected():
    payload = sample()
    payload["skills"].append(dict(payload["skills"][0]))
    with pytest.raises(ValidationError):
        CharacterRecord.model_validate(payload)


def test_character_format_reuses_processor_without_copying_game_metadata():
    source = SourceRecord(
        source_id="synthetic-source", title="合成资料", game_id="synthetic-game",
        topic="武将格式测试", content_path="fixtures/synthetic.json", source_type="local",
        source_locator="fixtures/synthetic.json", collected_at=date(2026, 10, 28),
    )
    document = SourceDocument(source=source, blocks=(
        EvidenceBlock(block_id="a", text="合成资料，字段值不代表真实游戏事实。", locator="合成段落"),
    ))
    result = DataProcessor(CharacterRecord).process(document, ManualExtractor([
        {"data": sample(), "block_ids": ["a"]},
    ]))
    assert result.source.game_id == "synthetic-game"
    assert result.review_status == "pending"
    assert result.records[0].data.character == "合成武将"
    assert result.records[0].data.skills[0].skill_name == "合成技能"
    assert "game_id" not in result.records[0].data.model_dump()
