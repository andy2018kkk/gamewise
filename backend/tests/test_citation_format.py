"""引用格式转换与实际来源列表的一致性；无真实模型调用。"""
import asyncio
import json

import httpx
import pytest

from app.providers.groq import GroqModel, GroqSettings, _normalize_answer_citations
from app.services.ask import AskService
from app.services.knowledge_tools import KnowledgeTools
from synthetic_data import build_synthetic_database


@pytest.mark.parametrize("text,expected,ids", [
    ("原文[E1]", "原文[1]", ["source-1"]),
    ("原文【E1】", "原文[1]", ["source-1"]),
    ("原文[E3,E1]", "原文[1][2]", ["source-3", "source-1"]),
    ("原文【 E3， E1 】", "原文[1][2]", ["source-3", "source-1"]),
    ("原文[E2-E4]", "原文[1][2][3]", ["source-2", "source-3", "source-4"]),
    ("原文【E2–E3，E1】", "原文[1][2][3]", ["source-2", "source-3", "source-1"]),
    ("甲[E2]，乙【E1】；仍是甲[E2]。", "甲[1]，乙[2]；仍是甲[1]。", ["source-2", "source-1"]),
    ("原文[E1,E1,E2]", "原文[1][2]", ["source-1", "source-2"]),
    ("技能【杀】、【闪】和普通列表[1]保持不变。", "技能【杀】、【闪】和普通列表[1]保持不变。", []),
    ("没有引用的答案。", "没有引用的答案。", []),
])
def test_supported_formats_preserve_non_citation_text(text, expected, ids):
    aliases = {f"E{i}": f"source-{i}" for i in range(1, 5)}
    assert _normalize_answer_citations(text, aliases) == (expected, ids)


@pytest.mark.parametrize("reference", ["[E1]", "【E1】", "[E1,E2]", "【E1-E2】"])
def test_answer_numbers_match_actual_program_source_list(tmp_path, reference):
    database = build_synthetic_database(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            message = {"tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "get_character_skills", "arguments": '{"characters":["测试武将甲"]}'}}]}
            reason = "tool_calls"
        else:
            # Same source returned by a second query is not needed; E2 would be unknown.
            message = {"content": "保留技能说明和标点：" + reference}
            reason = "stop"
        return httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": message}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake", model="test"), client,
                              instructions="测试", allowed_game_ids=["synthetic-a"])
            return await AskService(model, lambda: KnowledgeTools(database, ["synthetic-a"], preview=True)).ask("技能是什么？")
    result = asyncio.run(run())
    assert len(calls) == 2
    if "E2" in reference:
        assert result.error.code == "invalid_citation"
    else:
        assert result.error is None and result.answer == "保留技能说明和标点：[1]"
        assert len(result.citations) == 1 and result.citations[0].source_id == "synthetic-source"


@pytest.mark.parametrize("text", ["【E99】", "[E1,E99]", "[E1-E999999999999999999999999]", "【E3-E1】"])
def test_unknown_or_invalid_ranges_are_not_silently_dropped(text):
    aliases = {f"E{i}": f"source-{i}" for i in range(1, 4)}
    _, references = _normalize_answer_citations(text, aliases)
    assert any(reference not in aliases.values() for reference in references)
