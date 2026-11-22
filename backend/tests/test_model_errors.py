"""模型故障按安全类型传到问答结果，不转发平台原始正文。"""
import asyncio

import httpx
import pytest

from app.providers.groq import GroqModel, GroqSettings
from app.providers.llm import ModelProviderError
from app.services.ask import AskService
from app.services.knowledge_tools import KnowledgeTools
from synthetic_data import build_synthetic_database


@pytest.mark.parametrize("status,code,expected", [
    (429, "rate_limit_exceeded", "限流"),
    (401, "invalid_api_key", "鉴权失败"),
    (403, None, "拒绝访问"),
    (400, "tool_use_failed", "工具调用被模型平台拒绝"),
    (400, "invalid_request_error", "拒绝了请求"),
    (413, None, "拒绝了请求"),
    (503, None, "暂时不可用"),
])
def test_http_failure_reaches_service_safely(tmp_path, status, code, expected):
    database = build_synthetic_database(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": code,
            "message": "secret-key private-original-request C:/private/path"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="secret-key", model="test"), client,
                instructions="测试", allowed_game_ids=["sanguosha-ol"])
            service = AskService(model, lambda: KnowledgeTools(database, ["sanguosha-ol"], preview=True))
            return await service.ask("测试故障")
    result = asyncio.run(run())
    assert result.status == "error" and result.error.code == "model_error"
    assert expected in result.error.message and f"HTTP {status}" in result.error.message
    assert len(calls) == 1
    assert all(secret not in result.model_dump_json() for secret in
               ("secret-key", "private-original-request", "C:/private/path"))


def test_network_failure_is_not_generic_or_raw(tmp_path):
    database = build_synthetic_database(tmp_path)

    def handler(request):
        raise httpx.ConnectError("private network exception secret-key", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = GroqModel(GroqSettings(api_key="secret-key", model="test"), client,
                instructions="测试", allowed_game_ids=["sanguosha-ol"])
            return await AskService(model, lambda: KnowledgeTools(database, ["sanguosha-ol"], preview=True)).ask("问题")
    result = asyncio.run(run())
    assert "网络连接" in result.error.message
    assert "private" not in result.model_dump_json() and "secret-key" not in result.model_dump_json()


def test_unknown_exception_message_is_never_public():
    error = ModelProviderError("secret-key provider echoed the prompt")
    assert "尚未识别" in error.public_message
    assert "secret-key" not in error.public_message


@pytest.mark.parametrize("kind,expected", [
    ("invalid_response", "格式异常"), ("incomplete_response", "未完整结束"),
])
def test_response_failure_has_distinct_safe_message(kind, expected):
    assert expected in ModelProviderError("raw-content", kind=kind).public_message
