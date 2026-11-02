"""用合成证据验证可复用处理器，不访问网络或真实模型。"""

from datetime import date
import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.schemas.source import SourceRecord
from app.services.data_processor import (
    DataProcessor, EvidenceBlock, LLMExtractor, ManualExtractor,
    RuleExtractor, SourceDocument,
)


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    text: str


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_name: str
    count: int


def document(game_id="fixture-game"):
    source = SourceRecord(
        source_id="fixture-source", title="合成原文", game_id=game_id,
        topic="合成测试", content_path="fixtures/synthetic.txt", source_type="local",
        source_locator="fixtures/synthetic.txt", collected_at=date(2026, 10, 28),
    )
    return SourceDocument(source=source, blocks=(
        EvidenceBlock(block_id="a", text="合成技能：若受伤，摸2张牌。", locator="第1段"),
        EvidenceBlock(block_id="b", text="其他资料", locator="第2段"),
    ))


def row():
    return {"data": {"name": "合成技能", "text": "若受伤，摸2张牌。"}, "block_ids": ["a"]}


def test_manual_keeps_source_and_reports_unprocessed_blocks():
    result = DataProcessor(Skill, verbatim_fields=("name", "text")).process(
        document(), ManualExtractor([row()]),
    )
    assert result.review_status == "pending"
    assert result.source.game_id == "fixture-game"
    assert result.source.version_ids == []
    assert result.records[0].evidence[0].locator == "第1段"
    assert result.unprocessed_block_ids == ["b"]
    assert not result.all_blocks_accounted_for


def test_rules_share_validation_and_source_ids_change_record_identity():
    processor = DataProcessor(Skill)
    rules = RuleExtractor(lambda blocks: [row()])
    first = processor.process(document(), rules)
    again = processor.process(document(), rules)
    other_game = processor.process(document("fixture-other-game"), rules)
    assert first.records[0].record_id == again.records[0].record_id
    assert first.records[0].record_id != other_game.records[0].record_id


def test_same_processor_supports_different_expected_fields():
    result = DataProcessor(Item).process(document(), ManualExtractor([
        {"data": {"item_name": "合成道具", "count": 2}, "block_ids": ["a"]},
    ]))
    assert result.records[0].data.count == 2
    assert result.records[0].data.item_name == "合成道具"


def test_changed_evidence_changes_identity_even_without_snapshot_hash():
    processor = DataProcessor(Skill)
    first = processor.process(document(), ManualExtractor([row()]))
    changed = SourceDocument(source=document().source, blocks=(
        EvidenceBlock(block_id="a", text="合成技能：不同原文", locator="第1段"),
    ))
    second = processor.process(changed, ManualExtractor([row()]))
    assert first.records[0].record_id != second.records[0].record_id


@pytest.mark.parametrize("bad_row", [
    {"data": {"name": "合成技能"}, "block_ids": ["a"]},
    {"data": {"name": "合成技能", "text": "原文", "extra": 1}, "block_ids": ["a"]},
    {"data": {"name": 3, "text": "原文"}, "block_ids": ["a"]},
    {"data": {"name": "合成技能", "text": "原文"}, "block_ids": []},
    {"data": {"name": "合成技能", "text": "原文"}, "block_ids": ["missing"]},
    {"data": {"name": "合成技能", "text": "原文"}, "block_ids": ["a", "a"]},
])
def test_invalid_data_or_evidence_is_rejected(bad_row):
    with pytest.raises((ValueError, ValidationError)):
        DataProcessor(Skill).process(document(), ManualExtractor([bad_row]))


def test_verbatim_field_rejects_changed_number_or_lost_condition():
    processor = DataProcessor(Skill, verbatim_fields=("text",))
    changed = row()
    changed["data"]["text"] = "若受伤，摸3张牌。"
    with pytest.raises(ValueError, match="原文"):
        processor.process(document(), ManualExtractor([changed]))
    # 只检查子串不能发现前置条件被截掉；因此这里要求字段与完整证据文本相符。
    exact_doc = SourceDocument(source=document().source, blocks=(
        EvidenceBlock(block_id="a", text="若受伤，摸2张牌。", locator="第1段"),
    ))
    clipped = row()
    clipped["data"]["text"] = "摸2张牌。"
    with pytest.raises(ValueError, match="原文"):
        DataProcessor(Skill, exact_fields=("text",)).process(exact_doc, ManualExtractor([clipped]))


def test_duplicate_records_are_rejected_instead_of_silently_dropped():
    with pytest.raises(ValueError, match="重复"):
        DataProcessor(Skill).process(document(), ManualExtractor([row(), row()]))


class FakeClient:
    def __init__(self, response=None):
        self.calls = []
        self.response = {"records": [row()]} if response is None else response

    def convert(self, *, blocks, output_schema, instruction):
        self.calls.append((blocks, output_schema, instruction))
        return self.response


def test_llm_is_disabled_by_default_without_calling_client():
    client = FakeClient()
    with pytest.raises(ValueError, match="未启用"):
        DataProcessor(Skill).process(document(), LLMExtractor(client))
    assert client.calls == []


def test_llm_fake_gets_custom_schema_and_cannot_set_audit_or_source():
    client = FakeClient()
    result = DataProcessor(Skill).process(document(), LLMExtractor(client, enabled=True))
    blocks, schema, instruction = client.calls[0]
    assert blocks[0] == {"block_id": "a", "text": "合成技能：若受伤，摸2张牌。"}
    assert "Skill" in schema["$defs"]
    assert "证据" in instruction
    assert result.review_status == "pending"
    client.response = {"records": [row()], "review_status": "approved"}
    with pytest.raises(ValidationError):
        DataProcessor(Skill).process(document(), LLMExtractor(client, enabled=True))


def test_llm_failure_remains_a_service_error():
    class BrokenClient:
        def convert(self, **kwargs):
            raise TimeoutError("合成服务超时")
    with pytest.raises(TimeoutError):
        DataProcessor(Skill).process(document(), LLMExtractor(BrokenClient(), enabled=True))


def test_empty_conversion_reports_all_blocks_unprocessed():
    result = DataProcessor(Skill).process(document(), ManualExtractor([]))
    assert result.records == []
    assert result.unprocessed_block_ids == ["a", "b"]
    assert not result.all_blocks_accounted_for


def test_json_and_markdown_exports_preserve_source_without_writing_files():
    processor = DataProcessor(Skill)
    result = processor.process(document(), ManualExtractor([row()]))
    exported = json.loads(processor.render(result, "json"))
    assert exported["source"]["game_id"] == "fixture-game"
    assert exported["records"][0]["data"]["name"] == "合成技能"
    assert "合成技能" in processor.render(result, "markdown")
    with pytest.raises(ValueError, match="格式"):
        processor.render(result, "unsupported")


def test_duplicate_input_blocks_and_unknown_quote_fields_are_rejected():
    with pytest.raises(ValueError, match="重复"):
        SourceDocument(source=document().source, blocks=(document().blocks[0], document().blocks[0]))
    with pytest.raises(ValueError, match="字段"):
        DataProcessor(Skill, verbatim_fields=("not_a_field",))
