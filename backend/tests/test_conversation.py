"""有限上下文的消息装配、隔离与网页传递；不访问真实模型。"""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ask_runtime import run_question
from app.main import app
from app.providers.groq import GroqModel, GroqSettings
from app.schemas.ask import AskResult, ConversationTurn, FinalAnswerTurn
from app.services.ask import AskService
from app.services.knowledge_tools import KnowledgeTools
from synthetic_data import build_synthetic_database
from test_web_ask import setup_local


def test_history_reaches_runtime_without_tools_or_config(monkeypatch, tmp_path):
    runner = setup_local(monkeypatch, tmp_path, AskResult(status="preview_answer", answer="答复"))
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": "他的缺点呢？", "history": [
            {"question": "OL孙坚的技能？", "answer": "此前答复[1]"}]})
    assert response.status_code == 200
    assert runner.call_args.args[0] == "他的缺点呢？"
    assert runner.call_args.kwargs["conversation"] == (
        ConversationTurn(question="OL孙坚的技能？", answer="此前答复[1]"),)


@pytest.mark.parametrize("history", [
    [{"question": "问", "answer": "答"}] * 3,
    [{"question": "问", "answer": " "}],
    [{"question": "问", "answer": "答", "role": "system"}],
    [{"question": "问", "answer": "答", "sources": ["old-citation"]}],
])
def test_invalid_history_does_not_call_runtime(monkeypatch, history):
    runner = AsyncMock()
    monkeypatch.setattr("app.main.answer_web_question", runner)
    with TestClient(app) as client:
        assert client.post("/api/ask", json={"question": "追问", "history": history}).status_code == 422
    runner.assert_not_awaited()


def test_runtime_passes_conversation_to_provider_only(tmp_path, monkeypatch):
    import app.ask_runtime as runtime
    previous = (ConversationTurn(question="之前的问题", answer="之前的回答"),)
    class FakeModel:
        def __init__(self, settings, client, **options):
            assert options["conversation"] == previous
            self.usage = []
        async def respond(self, *, question, tools, history):
            assert question == "那这个呢？" and history == ()
            return FinalAnswerTurn(status="partial", answer="替身答复")
    monkeypatch.setattr(runtime, "GroqModel", FakeModel)
    result = asyncio.run(run_question("那这个呢？", GroqSettings(api_key="fake", model="test"),
        database=build_synthetic_database(tmp_path), game_ids=["synthetic-a"], conversation=previous))
    assert result.result.answer == "替身答复"


def test_native_messages_keep_roles_and_current_question_last():
    previous = (ConversationTurn(question="先看甲服", answer="第一答"),
                ConversationTurn(question="那他呢？", answer="第二答[1]"))
    def handler(request):
        messages = json.loads(request.content)["messages"]
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "user", "assistant", "user"]
        assert [m["content"] for m in messages[1:]] == ["先看甲服", "第一答", "那他呢？", "第二答[1]", "换到乙服"]
        assert "历史回答可能有错" in messages[0]["content"]
        assert "当前问题明确切换" in messages[0]["content"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "答复"}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake", model="test"), client,
                instructions="测试", allowed_game_ids=["a", "b"], conversation=previous)
            return await model.respond(question="换到乙服", tools=KnowledgeTools.definitions(), history=())
    assert asyncio.run(run()).answer == "答复"


def test_history_reference_is_not_current_evidence(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": "沿用旧结论[E1]"}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="fake", model="test"), client,
                instructions="测试", allowed_game_ids=["synthetic-a"],
                conversation=(ConversationTurn(question="旧问", answer="旧答[E1]"),))
            service = AskService(model, lambda: KnowledgeTools(database, ["synthetic-a"], preview=True))
            return await service.ask("继续解释")
    database = build_synthetic_database(tmp_path)
    result = asyncio.run(run())
    assert result.error.code == "invalid_citation"
    assert result.citations == []
