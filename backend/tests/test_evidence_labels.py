"""验证模式和包在模型阅读文本中有独立归属，不改变原始记录。"""
import pytest

from app.providers.groq import _evidence_text
from app.schemas.tools import SkillTarget, ToolRecord, ToolResponse, ToolScopeResult


@pytest.mark.parametrize("mode", ["经典版本", "国战版本", "1V1版本"])
def test_card_labels_keep_mode_package_and_body_separate(mode):
    records = [ToolRecord(character=f"样本条目{index}", skill_name=f"技能{index}",
        mode=mode, package_name=package, package_kind="武将包", skill_text="原文：摸两张牌，每回合限一次。",
        record_id=f"r{index}", citation_id=f"c{index}", game_id="synthetic-a", version_ids=[],
        source_id="s", source_locator="local:test", content_path="test.json", content_hash=None,
        locators=[]) for index, package in enumerate(["样本包甲", "样本包乙"], 1)]
    response = ToolResponse(results=[ToolScopeResult(game_id="synthetic-a", status="preview_available",
        target=SkillTarget(game_id="synthetic-a", character="样本条目1", mode=mode), records=records)])
    original = response.model_dump()
    aliases = {}
    rendered = _evidence_text(response, aliases)
    assert rendered.count(f"发行分支：synthetic-a；玩法模式：{mode}；武将条目：") == 2
    assert "武将包：样本包甲" in rendered and "武将包：样本包乙" in rendered
    assert "玩法模式：样本包甲" not in rendered and "玩法模式：样本包乙" not in rendered
    assert rendered.count("技能原文：\n原文：摸两张牌，每回合限一次。") == 2
    assert aliases == {"E1": "c1", "E2": "c2"}
    assert response.model_dump() == original
